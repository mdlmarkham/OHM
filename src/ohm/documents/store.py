"""Document storage backends for the OHM document library.

Provides a simple abstract base class plus local-filesystem, S3, and
AWS Bedrock Knowledge Base implementations.
"""

from __future__ import annotations

import os
import shutil
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
        **kwargs: Any,
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
        **kwargs: Any,
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
            raise RuntimeError("S3DocumentStore requires OHM_S3_BUCKET or AWS_BUCKET_NAME environment variable")

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
        **kwargs: Any,
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


class BedrockKnowledgeStore(DocumentStore):
    """Write-through wrapper that syncs OHM documents to an AWS Bedrock Knowledge Base.

    Wraps an inner ``DocumentStore`` (Local or S3) for raw byte persistence.
    On ``save()`` the document is stored via the inner store AND pushed to a
    Bedrock Knowledge Base for managed embeddings and agentic RAG.

    Two sync strategies:

    1. **S3 reference** — when the inner store is ``S3DocumentStore`` the
       Bedrock KB can use an S3 data source pointing at the same bucket/prefix.
       In this mode ``save()`` only triggers an ingestion job on the data source
       (the S3 → Bedrock sync is handled by the data source configuration).
    2. **Direct upload** — when the inner store is ``LocalDocumentStore`` (or
       any non-S3 store), ``save()`` calls
       ``bedrock-agent-runtime:IngestKnowledgeBaseDocuments`` to push the
       document content directly.

    Configuration (environment variables):

    - ``OHM_BEDROCK_KB_ID`` — Bedrock Knowledge Base ID (required)
    - ``OHM_BEDROCK_REGION`` / ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` —
      region (default: us-east-1)
    - ``OHM_BEDROCK_DATA_SOURCE_ID`` — data source ID inside the KB
      (required for S3 reference mode; optional for direct upload)
    - ``OHM_DOCUMENT_STORE`` — set to ``bedrock`` to use this as the default
      document store backend; the wrapper still needs an inner store for raw
      bytes, so it defaults to ``local`` unless ``s3`` is set
    """

    def __init__(
        self,
        inner_store: DocumentStore | None = None,
        knowledge_base_id: str | None = None,
        data_source_id: str | None = None,
        region: str | None = None,
    ) -> None:
        import boto3
        import logging

        self.inner = inner_store or self._default_inner_store()

        self.knowledge_base_id = knowledge_base_id or os.environ.get("OHM_BEDROCK_KB_ID")
        if not self.knowledge_base_id:
            raise RuntimeError("BedrockKnowledgeStore requires OHM_BEDROCK_KB_ID environment variable")

        self.data_source_id = data_source_id or os.environ.get("OHM_BEDROCK_DATA_SOURCE_ID")

        self.region = region or os.environ.get("OHM_BEDROCK_REGION") or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        self._s3_reference_mode = isinstance(self.inner, S3DocumentStore)
        self._logger = logging.getLogger(__name__)

        session = boto3.Session()
        self._agent_client = session.client(
            "bedrock-agent-runtime",
            region_name=self.region,
        )
        self._bedrock_agent_client = session.client(
            "bedrock-agent",
            region_name=self.region,
        )

    @staticmethod
    def _default_inner_store() -> DocumentStore:
        backend = os.environ.get("OHM_DOCUMENT_STORE", "local").lower()
        # Avoid recursively wrapping BedrockKnowledgeStore around itself.
        if backend == "bedrock":
            backend = "local"
        if backend == "s3":
            return S3DocumentStore()
        return LocalDocumentStore()

    def save(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        metadata = kwargs.pop("metadata", None) or {}
        record = self.inner.save(document_id, filename, content_bytes, content_type, **kwargs)
        try:
            self._sync_to_bedrock(document_id, filename, content_bytes, content_type, metadata=metadata)
        except Exception as exc:
            self._logger.warning("Bedrock KB sync failed for %s: %s", document_id, exc)
            record["bedrock_sync_status"] = "failed"
            record["bedrock_sync_error"] = str(exc)
        else:
            record["bedrock_sync_status"] = "synced"
            record["bedrock_kb_id"] = self.knowledge_base_id
        return record

    def get(self, document_id: str) -> bytes:
        return self.inner.get(document_id)

    def exists(self, document_id: str) -> bool:
        return self.inner.exists(document_id)

    def get_record(self, document_id: str) -> dict[str, Any]:
        return self.inner.get_record(document_id)

    def update_metadata(self, document_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.inner.update_metadata(document_id, **kwargs)

    def delete(self, document_id: str) -> None:
        self.inner.delete(document_id)

    def sync_existing_document(self, document_id: str) -> dict[str, Any]:
        """Sync a document that already exists in the inner store to Bedrock.

        Useful when Bedrock KB is configured after documents were already
        persisted, or when the initial sync failed and needs to be retried.
        """
        record = self.inner.get_record(document_id)
        content_bytes = self.inner.get(document_id)
        filename = record.get("filename", document_id)
        content_type = record.get("content_type", "text/plain")
        metadata = {
            "ohm_document_id": document_id,
            "ohm_filename": filename,
            "ohm_content_type": content_type,
        }
        if "source_node_id" in record:
            metadata["ohm_source_node_id"] = record["source_node_id"]
        if "tags" in record:
            metadata["ohm_tags"] = record["tags"]
        if "provenance" in record:
            metadata["ohm_provenance"] = record["provenance"]

        self._sync_to_bedrock(document_id, filename, content_bytes, content_type, metadata=metadata)
        return {"document_id": document_id, "bedrock_sync_status": "synced"}

    def retrieve(
        self,
        query: str,
        number_of_results: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Query the Bedrock Knowledge Base and return raw retrieval results."""
        body: dict[str, Any] = {
            "retrievalQuery": {"text": query, "type": "TEXT"},
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {
                    "numberOfResults": number_of_results,
                }
            },
        }
        if filters:
            body["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] = filters

        response = self._agent_client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            **body,
        )
        return list(response.get("retrievalResults", []))

    def retrieve_and_generate(
        self,
        query: str,
        model_arn: str | None = None,
        number_of_results: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve from the KB and generate a response with the retrieved context."""
        body: dict[str, Any] = {
            "retrieveAndGenerateConfiguration": {
                "type": "KNOWLEDGE_BASE",
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": self.knowledge_base_id,
                    "modelArn": model_arn or os.environ.get("OHM_BEDROCK_MODEL_ARN"),
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": number_of_results,
                        }
                    },
                },
            },
        }
        if filters:
            body["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] = filters

        response = self._agent_client.retrieve_and_generate(
            input={"text": query, "type": "TEXT"},
            **body,
        )
        return dict(response)

    def get_ingestion_status(self, ingestion_job_id: str) -> dict[str, Any]:
        """Return the status of an S3-reference ingestion job."""
        if not self.data_source_id:
            raise RuntimeError("get_ingestion_status requires OHM_BEDROCK_DATA_SOURCE_ID")
        response = self._bedrock_agent_client.get_ingestion_job(
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
            ingestionJobId=ingestion_job_id,
        )
        return dict(response)

    def _sync_to_bedrock(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._s3_reference_mode and self.data_source_id:
            self._sync_s3_reference(document_id)
        else:
            self._sync_direct_upload(document_id, filename, content_bytes, content_type, metadata=metadata)

    def _sync_s3_reference(self, document_id: str) -> dict[str, Any]:
        """Trigger an S3 data source ingestion job and return job metadata."""
        if not self.data_source_id:
            raise RuntimeError("S3 reference mode requires OHM_BEDROCK_DATA_SOURCE_ID")
        response = self._bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
            # TODO(OHM-tmtm): scope to document_id once Bedrock supports
            # per-document incremental sync; currently triggers a full
            # data-source sync.
        )
        return dict(response)

    def _sync_direct_upload(
        self,
        document_id: str,
        filename: str,
        content_bytes: bytes,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        document: dict[str, Any] = {
            "content": {
                "type": "CUSTOM",
                "custom": {
                    "customDocumentIdentifier": {"id": document_id},
                    "sourceType": "IN_LINE",
                    "inlineContent": {
                        "type": "TEXT",
                        "textContent": {
                            "data": content_bytes.decode("utf-8", errors="replace"),
                            "mimeType": content_type,
                        },
                    },
                },
            },
        }
        bedrock_metadata = self._build_bedrock_metadata(document_id, filename, content_type, metadata)
        if bedrock_metadata:
            document["metadata"] = bedrock_metadata

        self._agent_client.ingest_knowledge_base_documents(
            knowledgeBaseId=self.knowledge_base_id,
            documents=[document],
        )

    @staticmethod
    def _build_bedrock_metadata(
        document_id: str,
        filename: str,
        content_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Build a Bedrock DocumentMetadata object from OHM metadata.

        Supports BOOLEAN, NUMBER, STRING, and STRING_LIST attribute types.
        """
        merged: dict[str, Any] = {
            "ohm_document_id": document_id,
            "ohm_filename": filename,
            "ohm_content_type": content_type,
        }
        if metadata:
            merged.update(metadata)

        attributes: list[dict[str, Any]] = []
        for key, value in merged.items():
            attr = BedrockKnowledgeStore._metadata_attribute(key, value)
            if attr:
                attributes.append(attr)

        if not attributes:
            return None
        return {"type": "IN_LINE_ATTRIBUTE", "inlineAttributes": attributes}

    @staticmethod
    def _metadata_attribute(key: str, value: Any) -> dict[str, Any] | None:
        """Convert a single metadata value to a Bedrock MetadataAttribute."""
        if value is None:
            return None
        if isinstance(value, bool):
            return {
                "key": key,
                "value": {"type": "BOOLEAN", "booleanValue": value},
            }
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return {
                "key": key,
                "value": {"type": "NUMBER", "numberValue": float(value)},
            }
        if isinstance(value, (list, tuple)):
            strs = [str(v) for v in value]
            if not strs:
                return None
            return {
                "key": key,
                "value": {"type": "STRING_LIST", "stringListValue": strs},
            }
        return {
            "key": key,
            "value": {"type": "STRING", "stringValue": str(value)},
        }

    @staticmethod
    def _guess_extension(content_type: str) -> str | None:
        mapping = {
            "application/pdf": "pdf",
            "text/plain": "txt",
            "text/markdown": "md",
            "text/html": "html",
        }
        return mapping.get(content_type.lower().split(";")[0].strip())
