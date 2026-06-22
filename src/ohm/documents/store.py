"""Document storage backends for the OHM document library.

Provides a simple abstract base class plus a local-filesystem implementation.
S3 support is stubbed for future expansion.
"""

from __future__ import annotations

import os
import shutil
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DOCUMENT_PATH = "/var/lib/ohm/documents/"


class DocumentStore(ABC):
    """Abstract document storage backend."""

    @abstractmethod
    def save(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Persist a document and return a record with its stable URI/path."""

    @abstractmethod
    def get(self, document_id: str) -> bytes:
        """Return the raw bytes for a stored document."""

    @abstractmethod
    def exists(self, document_id: str) -> bool:
        """Return True if the document exists in this store."""


class LocalDocumentStore(DocumentStore):
    """Store documents under a configurable base path on the local filesystem.

    Files are organised into sharded directories derived from the document id
    so a single directory never holds too many files.
    """

    def __init__(self, base_path: str | None = None) -> None:
        self.base_path = Path(base_path or os.environ.get("OHM_DOCUMENT_PATH", DEFAULT_DOCUMENT_PATH))
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _shard_dir(self, document_id: str) -> Path:
        """Return a two-level sharded directory for ``document_id``."""
        safe_id = Path(document_id).name
        shard = safe_id[:2]
        return self.base_path / shard / safe_id

    def save(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Persist bytes under ``base_path/{shard}/{document_id}/{filename}``."""
        if not filename:
            ext = self._guess_extension(content_type) or "bin"
            filename = f"document.{ext}"
        shard_dir = self._shard_dir(document_id)
        shard_dir.mkdir(parents=True, exist_ok=True)
        file_path = shard_dir / filename
        with open(file_path, "wb") as f:
            f.write(content_bytes)

        # Write a small metadata sidecar with provenance information.
        meta_path = shard_dir / "meta.json"
        import json

        meta = {
            "document_id": document_id,
            "filename": filename,
            "content_type": content_type,
            "size": len(content_bytes),
            "stored_path": str(file_path),
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return {
            "document_id": document_id,
            "stored_path": str(file_path),
            "filename": filename,
            "content_type": content_type,
            "size": len(content_bytes),
            "uri": f"file://{file_path}",
        }

    def get(self, document_id: str) -> bytes:
        shard_dir = self._shard_dir(document_id)
        if not shard_dir.exists():
            raise FileNotFoundError(f"Document not found: {document_id}")
        files = [f for f in shard_dir.iterdir() if f.is_file() and f.name != "meta.json"]
        if not files:
            raise FileNotFoundError(f"Document not found: {document_id}")
        with open(files[0], "rb") as f:
            return f.read()

    def exists(self, document_id: str) -> bool:
        return self._shard_dir(document_id).exists()

    def get_record(self, document_id: str) -> dict[str, Any]:
        """Return the stored metadata record for ``document_id``."""
        shard_dir = self._shard_dir(document_id)
        meta_path = shard_dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Document not found: {document_id}")
        import json

        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def update_metadata(self, document_id: str, **kwargs: Any) -> dict[str, Any]:
        """Update the metadata sidecar for ``document_id`` and return it."""
        shard_dir = self._shard_dir(document_id)
        meta_path = shard_dir / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"Document not found: {document_id}")
        import json

        with open(meta_path, "r", encoding="utf-8") as f:
            record = json.load(f)
        record.update(kwargs)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        return record

    def delete(self, document_id: str) -> None:
        shard_dir = self._shard_dir(document_id)
        if shard_dir.exists():
            shutil.rmtree(shard_dir)

    @staticmethod
    def _guess_extension(content_type: str) -> str | None:
        mapping = {
            "application/pdf": "pdf",
            "text/plain": "txt",
            "text/markdown": "md",
            "text/html": "html",
        }
        return mapping.get(content_type.lower().split(";")[0].strip())


class S3DocumentStore(DocumentStore):
    """AWS S3-backed document store (stub)."""

    def save(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        raise NotImplementedError("S3DocumentStore is not implemented yet")

    def get(self, document_id: str) -> bytes:
        raise NotImplementedError("S3DocumentStore is not implemented yet")

    def exists(self, document_id: str) -> bool:
        raise NotImplementedError("S3DocumentStore is not implemented yet")
