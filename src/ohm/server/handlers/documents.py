"""Document library HTTP handlers.

Provides ``POST /documents/upload`` for ingesting files and URLs.
"""

from __future__ import annotations

import email.policy
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ohm.documents.extract import extract_text
from ohm.documents.ingest import ingest_file
from ohm.documents.store import LocalDocumentStore
from ohm.exceptions import ValidationError


SUPPORTED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "text/html",
}


class DocumentHandlerMixin:
    """Handler mixin for the OHM document library endpoints."""

    def _post_documents_upload(self, path: str, qs: dict, body: Any, agent: str) -> None:
        """POST /documents/upload — ingest a file or fetch a URL.

        Accepts either:
          - multipart/form-data with a ``file`` field, or
          - application/json with ``{"url": "..."}``.

        Returns JSON with ``document_id``, ``source_node_id``, ``stored_path``,
        and ``extracted_text_length``.
        """
        store = self._document_store()
        content_type = self.headers.get("Content-Type", "")

        if content_type.startswith("multipart/form-data"):
            filename, content_bytes, detected_type = self._parse_multipart_upload(body)
            tags = None
        elif content_type == "application/json" or (isinstance(body, dict) and "url" in body):
            filename, content_bytes, detected_type = self._fetch_url_upload(body)
            tags = body.get("tags") if isinstance(body, dict) else None
        else:
            raise ValidationError(
                "Unsupported request: expected multipart/form-data file upload or JSON {'url': '...'}"
            )

        final_type = self._resolve_content_type(filename, detected_type, content_bytes)
        if not self._is_allowed_content_type(final_type, filename):
            raise ValidationError(f"Unsupported content type or extension: {detected_type!r} for {filename!r}")

        result = ingest_file(
            store=store,
            conn=self.current_store.conn,
            content_bytes=content_bytes,
            filename=filename,
            content_type=final_type,
            created_by=agent,
            tags=tags,
            provenance="document-library",
        )

        self._json_response(
            200,
            {
                "document_id": result["document_id"],
                "source_node_id": result["source_node_id"],
                "stored_path": result["stored_record"]["stored_path"],
                "extracted_text_length": result["extracted_text_length"],
            },
        )

    def _document_store(self) -> LocalDocumentStore:
        """Return a LocalDocumentStore instance.

        Uses ``OHM_DOCUMENT_PATH`` env var if available; otherwise a path under
        the same directory as the OHM database so dev/test stores stay isolated.
        """
        if os.environ.get("OHM_DOCUMENT_PATH"):
            return LocalDocumentStore()
        store_root = Path(str(self.current_store.db_path)).parent / "documents"
        return LocalDocumentStore(str(store_root))

    def _parse_multipart_upload(self, raw_body: Any) -> tuple[str, bytes, str | None]:
        """Parse a multipart/form-data body using the stdlib email parser.

        ``raw_body`` is the already-read request body passed in from do_POST.
        Returns ``(filename, content_bytes, content_type)``.
        """
        content_type = self.headers.get("Content-Type", "")
        if not isinstance(raw_body, bytes) or len(raw_body) == 0:
            raise ValidationError("Empty upload body")
        if len(raw_body) > 50 * 1024 * 1024:
            raise ValidationError("Upload too large (max 50 MB)")

        header_block = (
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
        )
        msg = email.message_from_bytes(header_block + raw_body, policy=email.policy.HTTP)
        if not msg.is_multipart():
            raise ValidationError("Malformed multipart upload")

        file_item = None
        for part in msg.iter_parts():
            if part.get_param("name", header="Content-Disposition") == "file":
                file_item = part
                break

        if file_item is None:
            raise ValidationError("Missing 'file' field in multipart upload")

        filename = Path(file_item.get_filename() or "upload").name
        payload = file_item.get_payload(decode=True)
        content_bytes = payload if payload is not None else b""
        detected_type = file_item.get_content_type() or self._detect_content_type(filename)
        return filename, content_bytes, detected_type

    def _fetch_url_upload(self, body: dict) -> tuple[str, bytes, str | None]:
        """Fetch a document from a URL and return its filename, bytes, and type."""
        url = body.get("url") if isinstance(body, dict) else None
        if not url or not isinstance(url, str):
            raise ValidationError("JSON upload requires a 'url' string field")

        try:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "OHM-document-library/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_bytes = resp.read()
                detected_type = resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            raise ValidationError(f"Failed to fetch URL: HTTP {e.code} {e.reason}") from e
        except urllib.error.URLError as e:
            raise ValidationError(f"Failed to fetch URL: {e.reason}") from e
        except TimeoutError as e:
            raise ValidationError("Failed to fetch URL: timed out after 30s") from e

        filename = Path(url).name or "download"
        if "." not in filename:
            ext = self._guess_extension(detected_type)
            if ext:
                filename = f"{filename}.{ext}"
        return filename, content_bytes, detected_type

    def _resolve_content_type(
        self,
        filename: str,
        detected_type: str | None,
        content_bytes: bytes | None = None,
    ) -> str:
        """Resolve a definitive content type from headers, filename, or bytes."""
        if detected_type and detected_type.lower().split(";")[0].strip() in SUPPORTED_CONTENT_TYPES:
            return detected_type.lower().split(";")[0].strip()
        ext = Path(filename).suffix.lower()
        ext_map = {
            ".pdf": "application/pdf",
            ".html": "text/html",
            ".htm": "text/html",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".txt": "text/plain",
        }
        if ext in ext_map:
            return ext_map[ext]
        if detected_type:
            return detected_type.split(";")[0].strip().lower()
        if content_bytes and content_bytes[:4] == b"%PDF":
            return "application/pdf"
        raise ValidationError(f"Cannot determine content type for {filename!r}")

    def _is_allowed_content_type(self, content_type: str | None, filename: str) -> bool:
        if not content_type:
            return False
        base = content_type.split(";")[0].strip().lower()
        if base in SUPPORTED_CONTENT_TYPES:
            return True
        ext = Path(filename).suffix.lower()
        return ext in {".pdf", ".html", ".htm", ".md", ".markdown", ".txt"}

    @staticmethod
    def _detect_content_type(filename: str) -> str | None:
        ctype, _ = mimetypes.guess_type(filename)
        return ctype

    @staticmethod
    def _guess_extension(content_type: str | None) -> str | None:
        if not content_type:
            return None
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext.lstrip(".")
        mapping = {
            "application/pdf": "pdf",
            "text/plain": "txt",
            "text/markdown": "md",
            "text/html": "html",
        }
        return mapping.get(content_type.split(";")[0].strip().lower())

    def _get_document_prefix(self, path: str, qs: dict) -> None:
        """GET /documents/<id> or /documents/<id>/download.

        Returns JSON metadata for the document, or streams the raw file bytes
        for the ``/download`` suffix.
        """
        from ohm.exceptions import NodeNotFoundError

        prefix = "/documents/"
        if not path.startswith(prefix):
            raise ValidationError("Invalid document path")

        remainder = path[len(prefix):]
        if "/" in remainder:
            document_id, action = remainder.split("/", 1)
        else:
            document_id, action = remainder, ""

        if not document_id:
            raise ValidationError("Missing document id")

        store = self._document_store()
        if not store.exists(document_id):
            raise NodeNotFoundError(f"Document {document_id} not found")

        record = store.get_record(document_id)

        if action == "download":
            content_bytes = store.get(document_id)
            content_type = record.get("content_type", "application/octet-stream")
            filename = record.get("filename", "document")
            self._binary_response(200, content_bytes, content_type, filename)
            return

        if action:
            raise ValidationError(f"Unknown document action: {action!r}")

        # Enrich with source node details if available
        source_node_id = record.get("source_node_id")
        if source_node_id:
            try:
                node = self.current_store.get_node(source_node_id)
                if node:
                    record["source_node"] = node
            except Exception:
                pass

        self._json_response(200, record)

    def _binary_response(
        self,
        status: int,
        content_bytes: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        """Send a binary response with appropriate headers."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content_bytes)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(content_bytes)


