"""Staged ingestion pipeline with shell-hook architecture (OHM-tjkx).

Provides a pipeline orchestrator that runs deterministic pre/post-processing
hooks around each stage of document ingestion. Hooks are registered in the
``ohm_hooks`` table and executed by :class:`ohm.hooks.HookRunner`.

Pipeline stages (in order):
    1. fetch   — retrieve content from a URL or local path
    2. parse   — extract text and build a document tree
    3. commit  — write the parsed tree into the OHM graph

Each stage fires ``pre_<stage>`` hooks before execution and ``post_<stage>``
hooks after. A ``pre_<stage>`` hook that exits non-zero (and is not marked
optional) aborts the pipeline. An ``on_error`` hook fires when any stage
raises an exception.

CI mode: set ``BEADS_HOOKS=0`` or ``OHM_NO_HOOKS=1`` to bypass all hooks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Result of running the ingestion pipeline for a single item."""

    item_id: str
    stages: list[dict[str, Any]] = field(default_factory=list)
    source_node_id: str | None = None
    error: str | None = None
    aborted_by_hook: str | None = None
    duration_ms: float = 0.0
    skipped_unchanged: bool = False

    @property
    def success(self) -> bool:
        return self.error is None and self.aborted_by_hook is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "success": self.success,
            "source_node_id": self.source_node_id,
            "error": self.error,
            "aborted_by_hook": self.aborted_by_hook,
            "duration_ms": round(self.duration_ms, 2),
            "stages": self.stages,
            "skipped_unchanged": self.skipped_unchanged,
        }


def run_pipeline(
    conn: "DuckDBPyConnection",
    item: dict[str, Any],
    *,
    created_by: str = "ingestion",
    skip_hooks: bool = False,
) -> PipelineResult:
    """Run the ingestion pipeline for a single item with hook insertion.

    The item dict must have at least one of:
        - ``url`` — fetch from a URL
        - ``local_path`` — read from a local file
        - ``content_bytes`` — base64-encoded inline content (with ``filename``)

    After fetch, the content is parsed (PDF → text, HTML/Markdown → tree)
    and committed to the graph as a source node with document-tree children.

    Args:
        conn: DuckDB connection with OHM schema.
        item: Pipeline input item dict.
        created_by: Agent name for attribution.
        skip_hooks: Bypass all hooks (CI mode, --no-hooks flag).

    Returns:
        :class:`PipelineResult` with per-stage status and the source node ID.
    """
    from ohm.hooks import HookRunner, hooks_enabled

    start = time.time()
    item_id = item.get("id", "unknown")
    result = PipelineResult(item_id=item_id)

    hooks_active = hooks_enabled() and not skip_hooks
    runner = HookRunner(conn) if hooks_active else None

    def _run_hooks(event: str, payload: dict) -> list:
        if not hooks_active or runner is None:
            return []
        return runner.run_hooks(event, payload)

    try:
        # ── Stage 1: Fetch ────────────────────────────────────────────
        fetch_payload = {
            "item_id": item_id,
            "url": item.get("url"),
            "local_path": item.get("local_path"),
            "filename": item.get("filename"),
            "created_by": created_by,
        }

        pre_results = _run_hooks("pre_fetch", fetch_payload)
        for r in pre_results:
            if not r.success:
                result.aborted_by_hook = r.hook_id
                result.stages.append({"stage": "fetch", "status": "aborted", "hook": r.hook_id})
                return result

        content_bytes, filename, content_type = _do_fetch(item)
        result.stages.append(
            {
                "stage": "fetch",
                "status": "ok",
                "filename": filename,
                "content_type": content_type,
                "size": len(content_bytes),
                "hooks_run": len(pre_results),
            }
        )

        post_fetch_payload = {**fetch_payload, "filename": filename, "content_type": content_type, "size": len(content_bytes)}
        _run_hooks("post_fetch", post_fetch_payload)

        # OHM-682: Content-hash skip — if the fetched content's SHA-256
        # matches a previously-ingested node, skip parse + commit entirely.
        # This makes re-running the pipeline idempotent for unchanged content.
        import hashlib as _hashlib

        content_hash = _hashlib.sha256(content_bytes).hexdigest()
        from ohm.queries import lookup_content_hash

        existing = lookup_content_hash(conn, content_hash=content_hash)
        if existing:
            result.skipped_unchanged = True
            result.source_node_id = existing[0]["node_id"]
            result.stages.append(
                {
                    "stage": "dedup_check",
                    "status": "skipped_unchanged",
                    "content_hash": content_hash,
                    "existing_node_id": existing[0]["node_id"],
                    "matches": len(existing),
                }
            )
            result.duration_ms = (time.time() - start) * 1000
            return result

        result.stages.append(
            {
                "stage": "dedup_check",
                "status": "new",
                "content_hash": content_hash,
            }
        )

        # ── Stage 2: Parse ─────────────────────────────────────────────
        parse_payload = {
            "item_id": item_id,
            "filename": filename,
            "content_type": content_type,
            "created_by": created_by,
        }
        pre_parse_results = _run_hooks("pre_parse", parse_payload)
        for r in pre_parse_results:
            if not r.success:
                result.aborted_by_hook = r.hook_id
                result.stages.append({"stage": "parse", "status": "aborted", "hook": r.hook_id})
                return result

        extracted_text, tree = _do_parse(content_bytes, content_type, filename)
        result.stages.append(
            {
                "stage": "parse",
                "status": "ok",
                "text_length": len(extracted_text),
                "tree_nodes": len(tree.flat) if tree else 0,
                "hooks_run": len(pre_parse_results),
            }
        )

        _run_hooks("post_parse", {**parse_payload, "text_length": len(extracted_text)})

        # ── Stage 3: Commit ────────────────────────────────────────────
        commit_payload = {
            "item_id": item_id,
            "filename": filename,
            "created_by": created_by,
            "source_url": item.get("url"),
            "provenance": item.get("provenance", "ingestion"),
            "tags": item.get("tags"),
        }
        pre_commit_results = _run_hooks("pre_commit", commit_payload)
        for r in pre_commit_results:
            if not r.success:
                result.aborted_by_hook = r.hook_id
                result.stages.append({"stage": "commit", "status": "aborted", "hook": r.hook_id})
                return result

        source_node_id = _do_commit(conn, tree, extracted_text, commit_payload)
        result.source_node_id = source_node_id

        # OHM-682: Register the content hash so future runs skip unchanged content.
        try:
            from ohm.queries import register_content_hash

            register_content_hash(conn, node_id=source_node_id, content_hash=content_hash)
        except Exception as exc:
            logger.debug("Content hash registration failed for %s: %s", source_node_id, exc)

        result.stages.append(
            {
                "stage": "commit",
                "status": "ok",
                "source_node_id": source_node_id,
                "hooks_run": len(pre_commit_results),
            }
        )

        _run_hooks("post_commit", {**commit_payload, "source_node_id": source_node_id})

    except Exception as exc:
        result.error = str(exc)
        result.stages.append({"stage": "unknown", "status": "error", "error": str(exc)})
        _run_hooks("on_error", {"item_id": item_id, "error": str(exc), "created_by": created_by})
        logger.warning("Pipeline failed for item %s: %s", item_id, exc)

    result.duration_ms = (time.time() - start) * 1000
    return result


def _do_fetch(item: dict[str, Any]) -> tuple[bytes, str, str | None]:
    """Fetch content from a URL, local path, or inline bytes.

    Returns ``(content_bytes, filename, content_type)``.
    """
    import base64
    import mimetypes
    import os
    from pathlib import Path

    if item.get("content_bytes"):
        content = base64.b64decode(item["content_bytes"])
        filename = item.get("filename", "document")
        ct = item.get("content_type") or mimetypes.guess_type(filename)[0]
        return content, filename, ct

    if item.get("local_path"):
        from ohm.net_safety import validate_local_path

        ingestion_root = item.get("_ingestion_root") or os.environ.get("OHM_INGESTION_ROOT")
        safe_path = validate_local_path(item["local_path"], root=ingestion_root)
        path = Path(safe_path)
        content = path.read_bytes()
        filename = path.name
        ct = item.get("content_type") or mimetypes.guess_type(filename)[0]
        return content, filename, ct

    if item.get("url"):
        from ohm.net_safety import safe_fetch_pinned

        content, ct = safe_fetch_pinned(item["url"], timeout=30)
        url = item["url"]
        filename = item.get("filename") or Path(url).name or "download"
        if "." not in filename and ct:
            ext = mimetypes.guess_extension(ct.split(";")[0].strip())
            if ext:
                filename = f"{filename}{ext}"
        return content, filename, ct

    raise ValueError("Item must have 'url', 'local_path', or 'content_bytes'")


def _do_parse(content_bytes: bytes, content_type: str | None, filename: str) -> tuple[str, Any]:
    """Parse content bytes into extracted text and a document tree.

    Returns ``(extracted_text, tree)`` where tree may be None for non-HTML/MD.
    """
    from ohm.documents.extract import extract_text
    from ohm.ingestion.document_tree import parse_document

    extracted = extract_text(content_bytes, content_type, filename=filename)

    ct_lower = (content_type or "").lower().split(";")[0].strip()
    tree = None
    if ct_lower in ("text/html", "text/markdown", "text/x-markdown") or filename.endswith((".html", ".htm", ".md", ".markdown")):
        try:
            tree = parse_document(extracted, content_type="html" if ct_lower == "text/html" else "markdown")
        except Exception:
            pass

    return extracted, tree


def _do_commit(
    conn: "DuckDBPyConnection",
    tree: Any,
    extracted_text: str,
    payload: dict[str, Any],
) -> str:
    """Commit the parsed tree and extracted text to the graph.

    Creates a source node and (if a document tree was parsed) ingests the
    tree structure as child nodes with CONTAINS edges.

    Returns the source node ID.
    """
    from ohm.queries import create_node

    source_node = create_node(
        conn,
        label=payload["filename"],
        node_type="source",
        content=extracted_text[:5000],
        created_by=payload["created_by"],
        url=payload.get("source_url"),
        provenance=payload.get("provenance", "ingestion"),
        tags=payload.get("tags"),
    )
    source_id = source_node["id"]

    if tree is not None:
        try:
            from ohm.ingestion.document_tree_ingest import ingest_document_tree

            ingest_document_tree(
                conn,
                tree,
                created_by=payload["created_by"],
                provenance=payload.get("provenance", "ingestion"),
                source_url=payload.get("source_url"),
                tags=payload.get("tags"),
            )
        except Exception as exc:
            logger.warning("Document tree ingest failed for %s: %s", payload["filename"], exc)

    return source_id
