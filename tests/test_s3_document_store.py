"""Tests for the S3/MinIO document store backend."""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from ohm.documents.store import S3DocumentStore


@pytest.fixture
def s3_store(monkeypatch):
    """Return an S3DocumentStore backed by moto's mock S3."""
    monkeypatch.setenv("OHM_S3_BUCKET", "ohm-test-bucket")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")

    with moto.mock_aws():
        store = S3DocumentStore()
        # moto creates the bucket automatically on put_object, but head_object
        # needs it to exist; create explicitly for `exists()` tests.
        store.client.create_bucket(Bucket="ohm-test-bucket")
        yield store


class TestS3DocumentStore:
    def test_save_and_get_roundtrip(self, s3_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        content = b"S3 stored document content."
        record = s3_store.save(
            document_id=document_id,
            filename="note.txt",
            content_bytes=content,
            content_type="text/plain",
        )

        assert record["document_id"] == document_id
        assert record["filename"] == "note.txt"
        assert record["content_type"] == "text/plain"
        assert record["size"] == len(content)
        assert record["uri"].startswith("s3://ohm-test-bucket/")

        assert s3_store.exists(document_id)
        got = s3_store.get(document_id)
        assert got == content

    def test_save_without_extension_uses_content_type(self, s3_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        record = s3_store.save(
            document_id=document_id,
            filename="",
            content_bytes=b"%PDF-1.4 fake pdf",
            content_type="application/pdf",
        )
        assert record["filename"] == "document.pdf"
        assert record["key"].endswith("/document.pdf")

    def test_get_record_returns_metadata(self, s3_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        s3_store.save(
            document_id=document_id,
            filename="report.pdf",
            content_bytes=b"pdf-bytes",
            content_type="application/pdf",
        )
        record = s3_store.get_record(document_id)
        assert record["document_id"] == document_id
        assert record["filename"] == "report.pdf"
        assert "bucket" in record
        assert "key" in record

    def test_update_metadata(self, s3_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        s3_store.save(
            document_id=document_id,
            filename="page.html",
            content_bytes=b"<html></html>",
            content_type="text/html",
        )
        updated = s3_store.update_metadata(document_id, source_node_id="src-123")
        assert updated["source_node_id"] == "src-123"
        assert "updated_at" in updated

        # Re-read should reflect the update
        record = s3_store.get_record(document_id)
        assert record["source_node_id"] == "src-123"

    def test_delete_removes_object_and_metadata(self, s3_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        s3_store.save(
            document_id=document_id,
            filename="delete.me",
            content_bytes=b"temporary",
            content_type="text/plain",
        )
        assert s3_store.exists(document_id)
        s3_store.delete(document_id)
        assert not s3_store.exists(document_id)
        with pytest.raises(FileNotFoundError):
            s3_store.get(document_id)

    def test_missing_document_raises_file_not_found(self, s3_store):
        with pytest.raises(FileNotFoundError):
            s3_store.get("doc-does-not-exist")
        with pytest.raises(FileNotFoundError):
            s3_store.get_record("doc-does-not-exist")


class TestS3Configuration:
    def test_missing_bucket_env_raises(self, monkeypatch):
        monkeypatch.delenv("OHM_S3_BUCKET", raising=False)
        monkeypatch.delenv("AWS_BUCKET_NAME", raising=False)
        with pytest.raises(RuntimeError):
            S3DocumentStore()
