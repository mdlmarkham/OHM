"""Tests for BedrockKnowledgeStore write-through wrapper."""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("boto3")

from ohm.documents.store import BedrockKnowledgeStore, LocalDocumentStore, S3DocumentStore


@pytest.fixture
def local_store(tmp_path):
    return LocalDocumentStore(str(tmp_path))


@pytest.fixture
def bedrock_env(monkeypatch):
    monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("OHM_BEDROCK_DATA_SOURCE_ID", raising=False)
    monkeypatch.delenv("OHM_BEDROCK_REGION", raising=False)


@pytest.fixture
def mock_agent_client():
    with patch("boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        yield mock_client


@pytest.fixture
def bedrock_store(local_store, bedrock_env, mock_agent_client):
    store = BedrockKnowledgeStore(
        inner_store=local_store,
        knowledge_base_id="test-kb-id",
        region="us-east-1",
    )
    store._agent_client = mock_agent_client
    return store


class TestBedrockKnowledgeStore:
    def test_save_and_get_roundtrip_direct_upload(self, bedrock_store, mock_agent_client):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        content = b"Bedrock synced document content."
        record = bedrock_store.save(
            document_id=document_id,
            filename="note.txt",
            content_bytes=content,
            content_type="text/plain",
        )

        assert record["document_id"] == document_id
        assert record["bedrock_sync_status"] == "synced"
        assert record["bedrock_kb_id"] == "test-kb-id"
        assert bedrock_store.exists(document_id)
        assert bedrock_store.get(document_id) == content

        mock_agent_client.ingest_knowledge_base_documents.assert_called_once()
        call_kwargs = mock_agent_client.ingest_knowledge_base_documents.call_args
        assert call_kwargs.kwargs["knowledgeBaseId"] == "test-kb-id"
        docs = call_kwargs.kwargs["documents"]
        assert len(docs) == 1
        assert docs[0]["content"]["type"] == "CUSTOM"

    def test_save_sync_failure_graceful(self, bedrock_store, mock_agent_client):
        mock_agent_client.ingest_knowledge_base_documents.side_effect = Exception("Bedrock unavailable")

        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        content = b"Local-only document."
        record = bedrock_store.save(
            document_id=document_id,
            filename="fail.txt",
            content_bytes=content,
            content_type="text/plain",
        )

        assert record["bedrock_sync_status"] == "failed"
        assert "Bedrock unavailable" in record["bedrock_sync_error"]
        assert bedrock_store.exists(document_id)
        assert bedrock_store.get(document_id) == content

    def test_s3_reference_mode_triggers_ingestion_job(self, monkeypatch, bedrock_env, mock_agent_client):
        monkeypatch.setenv("OHM_S3_BUCKET", "ohm-test-bucket")
        monkeypatch.setenv("OHM_BEDROCK_DATA_SOURCE_ID", "ds-123")

        moto = pytest.importorskip("moto")

        with moto.mock_aws():
            s3_store = S3DocumentStore()
            s3_store.client.create_bucket(Bucket="ohm-test-bucket")

            with patch("boto3.client") as mock_boto3_client:
                mock_agent_client = MagicMock()
                mock_bedrock_agent = MagicMock()
                mock_boto3_client.side_effect = lambda service, **kw: mock_bedrock_agent if service == "bedrock-agent" else MagicMock()

                store = BedrockKnowledgeStore(
                    inner_store=s3_store,
                    knowledge_base_id="test-kb-id",
                    data_source_id="ds-123",
                    region="us-east-1",
                )
                store._agent_client = mock_agent_client

                with patch.object(store, "_sync_s3_reference") as mock_sync_s3:
                    document_id = f"doc-{uuid.uuid4().hex[:12]}"
                    record = store.save(
                        document_id=document_id,
                        filename="ref.txt",
                        content_bytes=b"S3 ref content",
                        content_type="text/plain",
                    )
                    mock_sync_s3.assert_called_once_with(document_id)

    def test_get_delegates_to_inner(self, bedrock_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        bedrock_store.save(
            document_id=document_id,
            filename="get.txt",
            content_bytes=b"test content",
            content_type="text/plain",
        )
        assert bedrock_store.get(document_id) == b"test content"

    def test_exists_delegates_to_inner(self, bedrock_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        assert not bedrock_store.exists(document_id)
        bedrock_store.save(
            document_id=document_id,
            filename="exists.txt",
            content_bytes=b"test",
            content_type="text/plain",
        )
        assert bedrock_store.exists(document_id)

    def test_get_record_delegates_to_inner(self, bedrock_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        bedrock_store.save(
            document_id=document_id,
            filename="record.txt",
            content_bytes=b"test",
            content_type="text/plain",
        )
        record = bedrock_store.get_record(document_id)
        assert record["document_id"] == document_id

    def test_update_metadata_delegates_to_inner(self, bedrock_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        bedrock_store.save(
            document_id=document_id,
            filename="meta.txt",
            content_bytes=b"test",
            content_type="text/plain",
        )
        updated = bedrock_store.update_metadata(document_id, source_node_id="src-456")
        assert updated["source_node_id"] == "src-456"

    def test_delete_delegates_to_inner(self, bedrock_store):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        bedrock_store.save(
            document_id=document_id,
            filename="del.txt",
            content_bytes=b"test",
            content_type="text/plain",
        )
        assert bedrock_store.exists(document_id)
        bedrock_store.delete(document_id)
        assert not bedrock_store.exists(document_id)


class TestBedrockConfiguration:
    def test_missing_kb_id_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OHM_BEDROCK_KB_ID", raising=False)
        with pytest.raises(RuntimeError, match="OHM_BEDROCK_KB_ID"):
            BedrockKnowledgeStore(
                inner_store=LocalDocumentStore(str(tmp_path)),
                knowledge_base_id=None,
            )

    def test_region_fallback_ohm_bedrock_region(self, monkeypatch, local_store, mock_agent_client):
        monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
        monkeypatch.setenv("OHM_BEDROCK_REGION", "eu-west-1")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        store = BedrockKnowledgeStore(
            inner_store=local_store,
            knowledge_base_id="test-kb-id",
        )
        assert store.region == "eu-west-1"

    def test_region_fallback_aws_region(self, monkeypatch, local_store, mock_agent_client):
        monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
        monkeypatch.delenv("OHM_BEDROCK_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        store = BedrockKnowledgeStore(
            inner_store=local_store,
            knowledge_base_id="test-kb-id",
        )
        assert store.region == "ap-southeast-1"

    def test_region_default_us_east_1(self, monkeypatch, local_store, mock_agent_client):
        monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
        monkeypatch.delenv("OHM_BEDROCK_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        store = BedrockKnowledgeStore(
            inner_store=local_store,
            knowledge_base_id="test-kb-id",
        )
        assert store.region == "us-east-1"

    def test_s3_reference_mode_detected(self, monkeypatch, bedrock_env):
        monkeypatch.setenv("OHM_S3_BUCKET", "test-bucket")
        moto = pytest.importorskip("moto")

        with moto.mock_aws():
            s3_store = S3DocumentStore()
            s3_store.client.create_bucket(Bucket="test-bucket")

            with patch("boto3.Session") as mock_session_cls:
                mock_session = MagicMock()
                mock_session_cls.return_value = mock_session
                mock_session.client.return_value = MagicMock()

                store = BedrockKnowledgeStore(
                    inner_store=s3_store,
                    knowledge_base_id="test-kb-id",
                )
                assert store._s3_reference_mode is True


class TestBedrockMetadataAndRetrieval:
    def test_save_propagates_metadata_to_direct_upload(self, bedrock_store, mock_agent_client):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        metadata = {
            "ohm_provenance": "document-library",
            "ohm_tags": ["ai", "agents"],
            "custom_number": 42,
            "custom_bool": True,
        }
        bedrock_store.save(
            document_id=document_id,
            filename="meta.txt",
            content_bytes=b"metadata test",
            content_type="text/plain",
            metadata=metadata,
        )

        mock_agent_client.ingest_knowledge_base_documents.assert_called_once()
        docs = mock_agent_client.ingest_knowledge_base_documents.call_args.kwargs["documents"]
        attrs = {a["key"]: a["value"] for a in docs[0]["metadata"]["inlineAttributes"]}

        assert attrs["ohm_document_id"]["type"] == "STRING"
        assert attrs["ohm_document_id"]["stringValue"] == document_id
        assert attrs["ohm_tags"]["type"] == "STRING_LIST"
        assert attrs["ohm_tags"]["stringListValue"] == ["ai", "agents"]
        assert attrs["custom_number"]["type"] == "NUMBER"
        assert attrs["custom_number"]["numberValue"] == 42.0
        assert attrs["custom_bool"]["type"] == "BOOLEAN"
        assert attrs["custom_bool"]["booleanValue"] is True

    def test_sync_existing_document(self, bedrock_store, mock_agent_client):
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        bedrock_store.inner.save(
            document_id=document_id,
            filename="existing.txt",
            content_bytes=b"existing content",
            content_type="text/plain",
        )
        bedrock_store.inner.update_metadata(document_id, source_node_id="src-789", tags=["tag1"])
        mock_agent_client.reset_mock()

        result = bedrock_store.sync_existing_document(document_id)

        assert result["document_id"] == document_id
        assert result["bedrock_sync_status"] == "synced"
        mock_agent_client.ingest_knowledge_base_documents.assert_called_once()
        docs = mock_agent_client.ingest_knowledge_base_documents.call_args.kwargs["documents"]
        attrs = {a["key"]: a["value"] for a in docs[0]["metadata"]["inlineAttributes"]}
        assert attrs["ohm_source_node_id"]["stringValue"] == "src-789"
        assert attrs["ohm_tags"]["stringListValue"] == ["tag1"]

    def test_retrieve(self, bedrock_store, mock_agent_client):
        mock_agent_client.retrieve.return_value = {
            "retrievalResults": [
                {"content": {"text": "chunk 1"}, "score": 0.95},
                {"content": {"text": "chunk 2"}, "score": 0.87},
            ]
        }
        results = bedrock_store.retrieve("agent systems", number_of_results=3)

        assert len(results) == 2
        mock_agent_client.retrieve.assert_called_once_with(
            knowledgeBaseId="test-kb-id",
            retrievalQuery={"text": "agent systems", "type": "TEXT"},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 3}},
        )

    def test_retrieve_with_filter(self, bedrock_store, mock_agent_client):
        mock_agent_client.retrieve.return_value = {"retrievalResults": []}
        filters = {"equals": {"key": "ohm_tags", "value": "ai"}}
        bedrock_store.retrieve("agents", filters=filters)

        call = mock_agent_client.retrieve.call_args
        assert call.kwargs["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] == filters

    def test_retrieve_and_generate(self, bedrock_store, mock_agent_client):
        mock_agent_client.retrieve_and_generate.return_value = {
            "output": {"text": "agents are systems that act"},
            "citations": [],
        }
        result = bedrock_store.retrieve_and_generate(
            "what is an agent?",
            model_arn="arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet",
            number_of_results=5,
        )

        assert result["output"]["text"] == "agents are systems that act"
        call = mock_agent_client.retrieve_and_generate.call_args
        assert call.kwargs["input"] == {"text": "what is an agent?", "type": "TEXT"}
        kb_config = call.kwargs["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]
        assert kb_config["knowledgeBaseId"] == "test-kb-id"
        assert kb_config["modelArn"] == "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-sonnet"
        assert kb_config["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 5

    def test_get_ingestion_status_requires_data_source_id(self, bedrock_store):
        with pytest.raises(RuntimeError, match="OHM_BEDROCK_DATA_SOURCE_ID"):
            bedrock_store.get_ingestion_status("job-123")

    def test_s3_reference_start_ingestion_job(self, monkeypatch, bedrock_env, mock_agent_client):
        monkeypatch.setenv("OHM_S3_BUCKET", "ohm-test-bucket")
        monkeypatch.setenv("OHM_BEDROCK_DATA_SOURCE_ID", "ds-123")
        moto = pytest.importorskip("moto")

        with moto.mock_aws():
            s3_store = S3DocumentStore()
            s3_store.client.create_bucket(Bucket="ohm-test-bucket")

            store = BedrockKnowledgeStore(
                inner_store=s3_store,
                knowledge_base_id="test-kb-id",
                data_source_id="ds-123",
                region="us-east-1",
            )
            store._agent_client = mock_agent_client
            store._bedrock_agent_client.start_ingestion_job.return_value = {
                "ingestionJobId": "job-abc",
                "status": "STARTED",
            }

            record = store.save(
                document_id="doc-123",
                filename="s3.txt",
                content_bytes=b"s3 content",
                content_type="text/plain",
            )

            assert record["bedrock_sync_status"] == "synced"
            store._bedrock_agent_client.start_ingestion_job.assert_called_once_with(
                knowledgeBaseId="test-kb-id",
                dataSourceId="ds-123",
            )


class TestBedrockDefaultInnerStoreAvoidsRecursion:
    def test_bedrock_document_store_env_defaults_to_local(self, monkeypatch, tmp_path, mock_agent_client):
        monkeypatch.setenv("OHM_DOCUMENT_STORE", "bedrock")
        monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
        store = BedrockKnowledgeStore(knowledge_base_id="test-kb-id")
        assert isinstance(store.inner, LocalDocumentStore)
