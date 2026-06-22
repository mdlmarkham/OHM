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
    """AWS S3 or S3-compatible (MinIO) document store.

    Configuration is read from environment variables:
      - ``OHM_S3_BUCKET`` / ``AWS_BUCKET_NAME`` — target bucket
      - ``OHM_S3_PREFIX`` — optional key prefix (default: ``ohm/documents/``)
      - ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — credentials
      - ``AWS_ENDPOINT_URL`` / ``S3_ENDPOINT_URL`` — for MinIO/compatible stores
      - ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` — region (default: us-east-1)

    Each document is stored as a single S3 object under
    ``{prefix}/{document_id}/{filename}``. Metadata is kept in S3 object
    metadata (``x-amz-meta-*``) as well as a companion ``{key}.meta.json``
    object so it can be read back without parsing content disposition.
    """

    def __init__(
        self,
        bucket: str | None = None,
        prefix: str | None = None,
        endpoint_url: str | None = None,
        region: str | None = None,
    ) -> None:
        import boto3

        self.bucket = bucket or os.environ.get("OHM_S3_BUCKET") or os.environ.get("AWS_BUCKET_NAME")
        if not self.bucket:
            raise RuntimeError(
                "S3DocumentStore requires OHM_S3_BUCKET or AWS_BUCKET_NAME environment variable"
            )

        self.prefix = (prefix or os.environ.get("OHM_S3_PREFIX", "ohm/documents/")).rstrip("/") + "/"
        self.endpoint_url = endpoint_url or os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
        self.region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        session = boto3.Session()
        self.client = session.client(
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
        )

    def _object_key(self, document_id: str, filename: str | None = None) -> str:
        safe_id = Path(document_id).name
        if filename:
            return f"{self.prefix}{safe_id}/{Path(filename).name}"
        return f"{self.prefix}{safe_id}/"

    def _meta_key(self, document_id: str) -> str:
        return f"{self._object_key(document_id)}meta.json"

    def save(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        import json

        if not filename:
            ext = self._guess_extension(content_type) or "bin"
            filename = f"document.{ext}"

        key = self._object_key(document_id, filename)

        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content_bytes,
            ContentType=content_type,
            Metadata={
                "ohm-document-id": document_id,
                "ohm-filename": filename,
                "ohm-content-type": content_type,
            },
        )

        meta = {
            "document_id": document_id,
            "filename": filename,
            "content_type": content_type,
            "size": len(content_bytes),
            "bucket": self.bucket,
            "key": key,
            "uri": f"s3://{self.bucket}/{key}",
            "stored_path": f"s3://{self.bucket}/{key}",
        }

        self.client.put_object(
            Bucket=self.bucket,
            Key=self._meta_key(document_id),
            Body=json.dumps(meta).encode("utf-8"),
            ContentType="application/json",
        )

        return meta

    def get(self, document_id: str) -> bytes:
        record = self.get_record(document_id)
        key = record["key"]
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def exists(self, document_id: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._meta_key(document_id))
            return True
        except Exception:
            return False

    def get_record(self, document_id: str) -> dict[str, Any]:
        import json

        meta_key = self._meta_key(document_id)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=meta_key)
        except Exception as e:
            raise FileNotFoundError(f"Document not found: {document_id}") from e
        return json.loads(response["Body"].read().decode("utf-8"))

    def update_metadata(self, document_id: str, **kwargs: Any) -> dict[str, Any]:
        import json

        record = self.get_record(document_id)
        record.update(kwargs)
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.client.put_object(
            Bucket=self.bucket,
            Key=self._meta_key(document_id),
            Body=json.dumps(record).encode("utf-8"),
            ContentType="application/json",
        )
        return record

    def delete(self, document_id: str) -> None:
        record = self.get_record(document_id)
        keys_to_delete = [self._meta_key(document_id), record["key"]]
        self.client.delete_objects(
            Bucket=self.bucket,
            Delete={"Objects": [{"Key": k} for k in keys_to_delete]},
        )

    @staticmethod
    def _guess_extension(content_type: str) -> str | None:
        mapping = {
            "application/pdf": "pdf",
            "text/plain": "txt",
            "text/markdown": "md",
            "text/html": "html",
        }
        return mapping.get(content_type.lower().split(";")[0].strip())

