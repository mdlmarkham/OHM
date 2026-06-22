"""Unit tests for Bedrock-specific document library HTTP handlers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("boto3")

from ohm.documents.store import BedrockKnowledgeStore
from ohm.exceptions import ValidationError
from ohm.server.handlers.documents import DocumentHandlerMixin


class _FakeHandlerBase:
    """Minimal stand-in for the handler mixin."""

    def __init__(self, store):
        self.current_store = store
        self.responses = []

    def _json_response(self, status: int, body: dict) -> None:
        self.responses.append((status, body))


class _FakeHandler(_FakeHandlerBase, DocumentHandlerMixin):
    pass


class _FakeHandlerBase:
    """Minimal stand-in for the handler mixin."""

    def __init__(self, store):
        self.current_store = store
        self.responses = []

    def _json_response(self, status: int, body: dict) -> None:
        self.responses.append((status, body))


class _FakeHandler(_FakeHandlerBase, DocumentHandlerMixin):
    pass


@pytest.fixture
def patched_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("OHM_BEDROCK_KB_ID", "test-kb-id")
    monkeypatch.setenv("OHM_BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("OHM_DOCUMENT_PATH", str(tmp_path / "docs"))

    from ohm.store import OhmStore

    store = MagicMock(spec=OhmStore)
    store.db_path = str(tmp_path / "ohm.duckdb")
    return _FakeHandler(store)


class TestBedrockHandlerHelpers:
    def test_extract_document_id(self):
        assert DocumentHandlerMixin._extract_document_id("/documents/doc-123/sync-to-bedrock", "sync-to-bedrock") == "doc-123"
        assert DocumentHandlerMixin._extract_document_id("/documents/doc-123/other", "sync-to-bedrock") is None
        assert DocumentHandlerMixin._extract_document_id("/nodes/doc-123/sync-to-bedrock", "sync-to-bedrock") is None


class TestBedrockSyncHandler:
    def test_sync_existing_document(self, patched_handler, monkeypatch):
        handler = patched_handler

        with patch.object(DocumentHandlerMixin, "_bedrock_store") as mock_bedrock_store_cls:
            mock_store = MagicMock()
            mock_store.inner.exists.return_value = True
            mock_store.sync_existing_document.return_value = {"document_id": "doc-abc", "bedrock_sync_status": "synced"}
            mock_bedrock_store_cls.return_value = mock_store

            handler._post_document_sync_to_bedrock("/documents/doc-abc/sync-to-bedrock", {}, {}, "test_agent")

            assert handler.responses == [(200, {"document_id": "doc-abc", "bedrock_sync_status": "synced"})]
            mock_store.sync_existing_document.assert_called_once_with("doc-abc")

    def test_sync_missing_document_returns_404(self, patched_handler):
        from ohm.exceptions import NodeNotFoundError

        handler = patched_handler
        with patch.object(DocumentHandlerMixin, "_bedrock_store") as mock_bedrock_store_cls:
            mock_store = MagicMock()
            mock_store.inner.exists.return_value = False
            mock_bedrock_store_cls.return_value = mock_store

            with pytest.raises(NodeNotFoundError):
                handler._post_document_sync_to_bedrock("/documents/doc-missing/sync-to-bedrock", {}, {}, "test_agent")

    def test_unknown_document_action_returns_404(self, patched_handler):
        handler = patched_handler
        handler._post_document_sync_to_bedrock("/documents/doc-abc/other-action", {}, {}, "test_agent")
        assert handler.responses == [(404, {"error": "Unknown document action: /documents/doc-abc/other-action"})]


class TestBedrockRetrieveHandler:
    def test_retrieve_requires_query(self, patched_handler):
        handler = patched_handler
        with pytest.raises(ValidationError, match="query"):
            handler._post_document_bedrock_retrieve("/documents/bedrock/retrieve", {}, {}, "test_agent")

    def test_retrieve_returns_results(self, patched_handler):
        handler = patched_handler
        with patch.object(DocumentHandlerMixin, "_bedrock_store") as mock_bedrock_store_cls:
            mock_store = MagicMock()
            mock_store.knowledge_base_id = "test-kb-id"
            mock_store.retrieve.return_value = [{"content": {"text": "result"}}]
            mock_bedrock_store_cls.return_value = mock_store

            handler._post_document_bedrock_retrieve(
                "/documents/bedrock/retrieve",
                {},
                {"query": "agents", "number_of_results": 3, "filters": {"equals": {"key": "tag", "value": "ai"}}},
                "test_agent",
            )

            assert handler.responses == [(200, {"results": [{"content": {"text": "result"}}], "knowledge_base_id": "test-kb-id"})]
            mock_store.retrieve.assert_called_once_with(
                query="agents",
                number_of_results=3,
                filters={"equals": {"key": "tag", "value": "ai"}},
            )


class TestBedrockRetrieveAndGenerateHandler:
    def test_retrieve_and_generate_requires_query(self, patched_handler):
        handler = patched_handler
        with pytest.raises(ValidationError, match="query"):
            handler._post_document_bedrock_retrieve_and_generate("/documents/bedrock/retrieve-and-generate", {}, {}, "test_agent")

    def test_retrieve_and_generate_returns_response(self, patched_handler):
        handler = patched_handler
        with patch.object(DocumentHandlerMixin, "_bedrock_store") as mock_bedrock_store_cls:
            mock_store = MagicMock()
            mock_store.knowledge_base_id = "test-kb-id"
            mock_store.retrieve_and_generate.return_value = {"output": {"text": "answer"}}
            mock_bedrock_store_cls.return_value = mock_store

            handler._post_document_bedrock_retrieve_and_generate(
                "/documents/bedrock/retrieve-and-generate",
                {},
                {"query": "what is an agent?", "model_arn": "arn:model", "number_of_results": 5},
                "test_agent",
            )

            assert handler.responses == [(200, {"output": {"text": "answer"}, "knowledge_base_id": "test-kb-id"})]
            mock_store.retrieve_and_generate.assert_called_once_with(
                query="what is an agent?",
                model_arn="arn:model",
                number_of_results=5,
                filters=None,
            )
