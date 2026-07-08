"""Document library HTTP handlers.

Provides ``POST /documents/upload`` for ingesting files and URLs.
"""

from __future__ import annotations

import email.policy
import ipaddress
import mimetypes
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ohm.documents.ingest import ingest_file
from ohm.documents.store import BedrockKnowledgeStore, DocumentStore, LocalDocumentStore, S3DocumentStore
from ohm.exceptions import NodeNotFoundError, ValidationError


SUPPORTED_CONTENT_TYPES = {
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "text/html",
}

# Private/loopback networks blocked by default for URL fetches.
# Loopback (127.0.0.0/8, ::1/128) is allowed by default when
# ``documents.allow_loopback`` is True (the default for local-first OHM).
_FETCH_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local (AWS metadata etc.)
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_LOOPBACK_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


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
            raise ValidationError("Unsupported request: expected multipart/form-data file upload or JSON {'url': '...'}")

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

    def _document_store(self) -> DocumentStore:
        """Return a DocumentStore instance.

        Uses ``OHM_DOCUMENT_STORE`` env var to pick backend (local, s3, or
        bedrock). For local storage, uses ``OHM_DOCUMENT_PATH`` if set;
        otherwise a path under the same directory as the OHM database so
        dev/test stores stay isolated.
        """
        backend = os.environ.get("OHM_DOCUMENT_STORE", "local").lower()
        if backend == "s3":
            return S3DocumentStore()
        if backend == "bedrock":
            return BedrockKnowledgeStore()
        if os.environ.get("OHM_DOCUMENT_PATH"):
            return LocalDocumentStore()
        store_root = Path(str(self.current_store.db_path)).parent / "documents"
        return LocalDocumentStore(str(store_root))

    def _bedrock_store(self) -> BedrockKnowledgeStore:
        """Return a BedrockKnowledgeStore, wrapping the current inner store if possible.

        If the configured document store is already a BedrockKnowledgeStore,
        use it directly. Otherwise wrap the default inner store so that
        explicit sync/retrieve endpoints still work even when the default
        backend is local or s3.
        """
        store = self._document_store()
        if isinstance(store, BedrockKnowledgeStore):
            return store
        return BedrockKnowledgeStore(inner_store=store)

    def _post_document_sync_to_bedrock(self, path: str, qs: dict, body: Any, agent: str) -> None:
        """POST /documents/{id}/sync-to-bedrock — sync an existing document to Bedrock.

        Useful when Bedrock is enabled after documents already exist, or
        when an earlier automatic sync failed.
        """
        document_id = self._extract_document_id(path, suffix="sync-to-bedrock")
        if document_id is None:
            self._json_response(404, {"error": f"Unknown document action: {path}"})
            return

        store = self._bedrock_store()
        if not store.inner.exists(document_id):
            raise NodeNotFoundError(f"Document {document_id} not found")

        result = store.sync_existing_document(document_id)
        self._json_response(200, result)

    def _post_document_bedrock_retrieve(self, path: str, qs: dict, body: Any, agent: str) -> None:
        """POST /documents/bedrock/retrieve — query the Bedrock Knowledge Base."""
        query = body.get("query") if isinstance(body, dict) else None
        if not query or not isinstance(query, str):
            raise ValidationError("JSON body requires a 'query' string field")

        number_of_results = body.get("number_of_results", 5)
        filters = body.get("filters")
        store = self._bedrock_store()
        results = store.retrieve(
            query=query,
            number_of_results=number_of_results,
            filters=filters,
        )
        self._json_response(200, {"results": results, "knowledge_base_id": store.knowledge_base_id})

    def _post_document_bedrock_retrieve_and_generate(self, path: str, qs: dict, body: Any, agent: str) -> None:
        """POST /documents/bedrock/retrieve-and-generate — RAG response from Bedrock."""
        query = body.get("query") if isinstance(body, dict) else None
        if not query or not isinstance(query, str):
            raise ValidationError("JSON body requires a 'query' string field")

        number_of_results = body.get("number_of_results", 5)
        model_arn = body.get("model_arn")
        filters = body.get("filters")
        store = self._bedrock_store()
        result = store.retrieve_and_generate(
            query=query,
            model_arn=model_arn,
            number_of_results=number_of_results,
            filters=filters,
        )
        self._json_response(200, {**result, "knowledge_base_id": store.knowledge_base_id})

    @staticmethod
    def _extract_document_id(path: str, suffix: str) -> str | None:
        """Parse ``/documents/{document_id}/{suffix}`` and return the id."""
        prefix = "/documents/"
        if not path.startswith(prefix):
            return None
        remainder = path[len(prefix) :]
        expected_suffix = f"/{suffix}"
        if not remainder.endswith(expected_suffix):
            return None
        document_id = remainder[: -len(expected_suffix)]
        return document_id if document_id else None

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

        header_block = b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n"
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

    def _validate_fetch_url(self, url: str) -> str:
        """Validate a user-supplied fetch URL to prevent SSRF attacks.

        - Rejects non-http(s) schemes.
        - Resolves the host and rejects private/loopback addresses.
        - Loopback (127.0.0.0/8, ::1) is allowed when
          ``documents.allow_loopback`` is True in config (default: True,
          since OHM is local-first and tests use 127.0.0.1).
        - Additional hosts can be allowlisted via
          ``documents.allowed_fetch_hosts`` in config.

        Returns the validated URL.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValidationError(f"URL fetch requires http or https scheme, got: {parsed.scheme!r}")
        host = parsed.hostname
        if not host:
            raise ValidationError("URL fetch missing host")

        config = self.current_config or {}
        allow_loopback = config.get("documents", {}).get("allow_loopback", True)
        allowed_hosts = set(config.get("documents", {}).get("allowed_fetch_hosts", []))

        if host in allowed_hosts:
            return url

        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            raise ValidationError(f"Cannot resolve fetch URL host: {host!r}")

        for info in infos:
            addr = str(info[4][0])
            ip = ipaddress.ip_address(addr)
            for net in _FETCH_BLOCKED_NETWORKS:
                if ip in net:
                    raise ValidationError(f"URL fetch blocked: host resolves to private address {addr} (SSRF protection)")
            if not allow_loopback:
                for net in _LOOPBACK_NETWORKS:
                    if ip in net:
                        raise ValidationError(f"URL fetch blocked: host resolves to loopback address {addr} (SSRF protection)")

        return url

    def _fetch_url_upload(self, body: dict) -> tuple[str, bytes, str | None]:
        """Fetch a document from a URL and return its filename, bytes, and type."""
        url = body.get("url") if isinstance(body, dict) else None
        if not url or not isinstance(url, str):
            raise ValidationError("JSON upload requires a 'url' string field")

        url = self._validate_fetch_url(url)

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

        remainder = path[len(prefix) :]
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
