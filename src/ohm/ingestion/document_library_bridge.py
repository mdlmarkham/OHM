"""Bridge between OHM ingestion queues and the document library.

This module lets the staged ingestion pipeline hand links, files, and PDFs to
the document library so they are stored, extracted, and ingested as
OHM document-tree source nodes instead of thin source stubs.

Usage from the pipeline:

    from ohm.ingestion.document_library_bridge import stage_documents

    stage_documents(ohm_url, ohm_token, queue_dir)

Items are expected in the ``triage_pass`` queue with ``kind == "document"``
and one of the following payloads:

- ``url`` — a remote document (PDF, HTML, etc.). The bridge fetches it and
  POSTs the bytes to ``/documents/upload``.
- ``local_path`` — a path on the local filesystem. The bridge reads the file.
- ``content_bytes`` (base64-encoded) — inline document content.

After processing, items are moved to the ``source_created`` queue with
``source_node_id`` populated by the upload response.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ohm.documents.extract import _detect_content_type, UnsupportedDocumentError


QUEUE_STAGES = ("raw", "triage_pass", "triage_fail", "source_created", "assessed")


def _queue_path(queue_dir: Path, stage: str, item_id: str) -> Path:
    return queue_dir / stage / f"{item_id}.json"


def _read_queue_items(queue_dir: Path, stage: str) -> list[dict]:
    stage_dir = queue_dir / stage
    if not stage_dir.exists():
        return []
    items = []
    for p in sorted(stage_dir.glob("*.json")):
        try:
            items.append(json.loads(p.read_text()))
        except Exception:
            pass
    return items


def _write_queue_item(queue_dir: Path, stage: str, item: dict) -> str:
    item_id = item.get("id") or hashlib.md5(json.dumps(item, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()[:16]
    item["id"] = item_id
    path = _queue_path(queue_dir, stage, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(item, f, indent=2, default=str)
    return item_id


def _move_queue_item(queue_dir: Path, item_id: str, from_stage: str, to_stage: str) -> None:
    src = _queue_path(queue_dir, from_stage, item_id)
    dst = _queue_path(queue_dir, to_stage, item_id)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)


def _ensure_queues(queue_dir: Path) -> None:
    for stage in QUEUE_STAGES:
        (queue_dir / stage).mkdir(parents=True, exist_ok=True)


def _api_post(
    url: str,
    headers: dict[str, str],
    json_data: dict[str, Any] | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | str]:
    """POST helper supporting both JSON and raw byte payloads."""

    max_retries = 3
    last_status = 0
    last_body: dict[str, Any] | str = ""

    for attempt in range(max_retries):
        time.sleep(0.15)
        request_headers = dict(headers)
        body: bytes | None = None
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = data
            if content_type:
                request_headers.setdefault("Content-Type", content_type)

        req = Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                last_status = resp.status
                try:
                    last_body = json.loads(raw.decode("utf-8"))
                except Exception:
                    last_body = raw.decode("utf-8", errors="replace")
                return last_status, last_body
        except HTTPError as e:
            last_status = e.code
            try:
                last_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                last_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code == 429:
                wait = 3 * (attempt + 1)
                print(f"    ~ Rate limited, retrying in {wait}s...")
                time.sleep(wait)
                continue
            return last_status, last_body
        except Exception as e:
            last_status = 0
            last_body = str(e)
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return last_status, last_body

    return last_status, last_body


def _fetch_url(url: str, timeout: int = 30) -> tuple[bytes, str | None]:
    """Fetch a URL safely with SSRF protection (DNS pinning, IP validation)."""
    from ohm.net_safety import safe_fetch_pinned

    return safe_fetch_pinned(url, timeout=timeout)


def _build_multipart_body(filename: str, content_bytes: bytes, boundary: str, content_type: str) -> bytes:
    """Build a minimal multipart/form-data body for a file upload."""
    parts = []
    parts.append(f"--{boundary}\r\n".encode("latin-1"))
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("latin-1"))
    parts.append(f"Content-Type: {content_type}\r\n\r\n".encode("latin-1"))
    parts.append(content_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("latin-1"))
    return b"".join(parts)


def _resolve_document_content(item: dict[str, Any]) -> tuple[bytes, str, str]:
    """Resolve an ingestion queue item into (content_bytes, filename, content_type).

    Resolution order:
      1. ``content_bytes`` (base64) supplied inline.
      2. ``local_path`` on the filesystem.
      3. ``url`` fetched remotely.
    """
    filename = item.get("filename", "document")

    if "content_bytes" in item:
        content_bytes = base64.b64decode(item["content_bytes"])
        content_type = item.get("content_type") or _detect_content_type(filename)
        return content_bytes, filename, content_type

    local_path = item.get("local_path")
    if local_path:
        from ohm.net_safety import validate_local_path

        import os as _os

        ingestion_root = item.get("_ingestion_root") or _os.environ.get("OHM_INGESTION_ROOT")
        safe_path = validate_local_path(local_path, root=ingestion_root)
        path = Path(safe_path)
        if not path.exists():
            raise FileNotFoundError(f"Document local path not found: {local_path}")
        content_bytes = path.read_bytes()
        content_type = item.get("content_type") or _detect_content_type(str(path)) or "application/octet-stream"
        if filename == "document":
            filename = path.name
        return content_bytes, filename, content_type

    url = item.get("url")
    if url:
        content_bytes, content_type = _fetch_url(url)
        if not content_type:
            content_type = item.get("content_type") or _detect_content_type(filename) or "application/octet-stream"
        if filename == "document":
            filename = Path(url.split("?")[0].split("#")[0]).name or "document"
        return content_bytes, filename, content_type

    raise ValueError("Document item must include one of: url, local_path, content_bytes")


def process_document_item(
    item: dict[str, Any],
    ohm_url: str,
    ohm_token: str,
) -> dict[str, Any]:
    """Upload a single document-queue item through the OHM document library.

    Returns the updated item with ``source_node_id`` set, or raises on
    terminal failure.
    """
    headers = {"Authorization": f"Bearer {ohm_token}"} if ohm_token else {}

    content_bytes, filename, content_type = _resolve_document_content(item)

    # Ensure we have a usable content type for the upload handler.
    if not content_type or content_type == "application/octet-stream":
        detected = _detect_content_type(filename)
        if detected:
            content_type = detected

    boundary = f"----ohm-doc-{uuid.uuid4().hex[:16]}"
    multipart_body = _build_multipart_body(filename, content_bytes, boundary, content_type)
    upload_url = f"{ohm_url.rstrip('/')}/documents/upload"
    upload_content_type = f"multipart/form-data; boundary={boundary}"

    status, body = _api_post(
        upload_url,
        headers,
        data=multipart_body,
        content_type=upload_content_type,
        timeout=60,
    )

    if status not in (200, 201):
        raise RuntimeError(f"Document upload failed: {status} {body}")

    if isinstance(body, dict):
        item["source_node_id"] = body.get("source_node_id")
        item["document_id"] = body.get("document_id")
        item["stored_path"] = body.get("stored_path")
        item["extracted_text_length"] = body.get("extracted_text_length")
    else:
        raise RuntimeError(f"Unexpected upload response: {body}")

    item["processed_at"] = datetime.now(timezone.utc).isoformat()
    return item


def stage_documents(
    ohm_url: str,
    ohm_token: str,
    queue_dir: Path | str = "/var/lib/ohm/ingestion",
) -> int:
    """Process document items in the ``triage_pass`` queue.

    Reads items with ``kind == "document"`` from ``triage_pass``, uploads them
    via the OHM document library, and moves them to ``source_created``.
    """
    queue_dir = Path(queue_dir)
    _ensure_queues(queue_dir)

    items = _read_queue_items(queue_dir, "triage_pass")
    document_items = [it for it in items if it.get("kind") == "document"]

    if not document_items:
        print("  No document items in triage_pass queue.")
        return 0

    print(f"  Processing {len(document_items)} document items...")

    processed = 0
    for item in document_items:
        title = item.get("title", item.get("filename", item.get("url", "unknown")))[:60]
        item_id = item.get("id", "")
        try:
            item = process_document_item(item, ohm_url, ohm_token)
            # Move the queue entry first, then overwrite with enriched metadata.
            _move_queue_item(queue_dir, item_id, "triage_pass", "source_created")
            _write_queue_item(queue_dir, "source_created", item)
            processed += 1
            node_id = item.get("source_node_id", "?")
            print(f"    + {node_id}: {title}")
        except UnsupportedDocumentError as e:
            print(f"    ! Unsupported document: {title} ({e})")
            item["error"] = str(e)
            _move_queue_item(queue_dir, item_id, "triage_pass", "triage_fail")
            _write_queue_item(queue_dir, "triage_fail", item)
        except Exception as e:
            print(f"    ! Failed to process document: {title} ({e})")
            item["error"] = str(e)
            # Leave original in triage_pass for retry; also log failure copy.
            _write_queue_item(queue_dir, "triage_fail", item)

    print(f"\n  Documents: {processed}/{len(document_items)} ingested")
    return processed


def queue_document_item(
    queue_dir: Path | str,
    *,
    title: str | None = None,
    url: str | None = None,
    local_path: str | None = None,
    content_bytes: bytes | None = None,
    filename: str | None = None,
    content_type: str | None = None,
    source: str = "manual",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    target_stage: str = "triage_pass",
) -> str:
    """Queue a document item for the ingestion pipeline.

    By default items are written directly to ``triage_pass`` so they bypass
    the keyword triage step — documents are presumed relevant because an
    agent or user explicitly supplied them.
    """
    queue_dir = Path(queue_dir)
    _ensure_queues(queue_dir)

    item: dict[str, Any] = {
        "kind": "document",
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if title:
        item["title"] = title
    if url:
        item["url"] = url
    if local_path:
        item["local_path"] = local_path
    if content_bytes is not None:
        item["content_bytes"] = base64.b64encode(content_bytes).decode("ascii")
    if filename:
        item["filename"] = filename
    if content_type:
        item["content_type"] = content_type
    if tags:
        item["tags"] = list(tags)
    if metadata:
        item["metadata"] = dict(metadata)

    item_id = _write_queue_item(queue_dir, target_stage, item)
    return item_id
