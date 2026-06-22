"""Tests for document-library integration with the OHM ingestion pipeline."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ohm.ingestion.document_library_bridge import (
    process_document_item,
    queue_document_item,
    stage_documents,
)


class TestQueueDocumentItem:
    """Tests for queueing document items for pipeline processing."""

    def test_queue_url_document(self, tmp_path):
        queue_dir = tmp_path / "queue"
        item_id = queue_document_item(
            queue_dir,
            title="Test PDF",
            url="https://example.com/doc.pdf",
            filename="doc.pdf",
            content_type="application/pdf",
            source="test",
            tags=["pdf", "test"],
        )

        item_path = queue_dir / "triage_pass" / f"{item_id}.json"
        assert item_path.exists()

        item = json.loads(item_path.read_text())
        assert item["kind"] == "document"
        assert item["title"] == "Test PDF"
        assert item["url"] == "https://example.com/doc.pdf"
        assert item["filename"] == "doc.pdf"
        assert item["content_type"] == "application/pdf"
        assert item["source"] == "test"
        assert item["tags"] == ["pdf", "test"]

    def test_queue_local_document(self, tmp_path):
        doc_path = tmp_path / "local.md"
        doc_path.write_text("# Local Doc\n\nBody text.")
        queue_dir = tmp_path / "queue"

        item_id = queue_document_item(queue_dir, local_path=str(doc_path), title="Local Markdown")

        item_path = queue_dir / "triage_pass" / f"{item_id}.json"
        item = json.loads(item_path.read_text())
        assert item["kind"] == "document"
        assert item["local_path"] == str(doc_path)
        assert "content_bytes" not in item

    def test_queue_inline_document(self, tmp_path):
        queue_dir = tmp_path / "queue"
        content = b"Inline text content."
        item_id = queue_document_item(
            queue_dir,
            content_bytes=content,
            filename="inline.txt",
            content_type="text/plain",
        )

        item_path = queue_dir / "triage_pass" / f"{item_id}.json"
        item = json.loads(item_path.read_text())
        assert item["content_bytes"] == base64.b64encode(content).decode("ascii")
        assert item["filename"] == "inline.txt"


class TestProcessDocumentItem:
    """Tests for processing a single document queue item against OHM."""

    def test_process_url_document_via_upload_endpoint(self, test_server):
        """Process a URL-backed document item via the live HTTP upload endpoint."""
        port, store = test_server

        # Serve a tiny HTML page from a local http.server spun up for the test
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        html = b"<html><body><h1>Remote Doc</h1><p>Remote content.</p></body></html>"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def log_message(self, *args):
                pass

        remote_server = HTTPServer(("127.0.0.1", 0), Handler)
        remote_port = remote_server.server_address[1]
        remote_thread = threading.Thread(target=remote_server.serve_forever, daemon=True)
        remote_thread.start()

        try:
            item = {
                "title": "Remote HTML",
                "url": f"http://127.0.0.1:{remote_port}/doc.html",
                "filename": "doc.html",
            }
            result = process_document_item(item, f"http://127.0.0.1:{port}", "")

            assert "source_node_id" in result
            assert "document_id" in result
            assert result["extracted_text_length"] > 0
        finally:
            remote_server.shutdown()
            remote_thread.join(timeout=2)

    def test_process_inline_html_document(self, test_server):
        port, store = test_server
        html = b"<html><body><h1>Inline Doc</h1><p>Inline content.</p></body></html>"
        item = {
            "title": "Inline HTML",
            "content_bytes": base64.b64encode(html).decode("ascii"),
            "filename": "inline.html",
            "content_type": "text/html",
        }
        result = process_document_item(item, f"http://127.0.0.1:{port}", "")

        assert "source_node_id" in result
        assert result["extracted_text_length"] > 0

    def test_process_unsupported_content_type_raises(self, test_server):
        port, store = test_server
        item = {
            "title": "Bad Doc",
            "content_bytes": base64.b64encode(b"not a pdf").decode("ascii"),
            "filename": "bad.xyz",
            "content_type": "application/xyz",
        }
        with pytest.raises(RuntimeError):
            process_document_item(item, f"http://127.0.0.1:{port}", "")


class TestStageDocuments:
    """Tests for the pipeline stage that processes document queues."""

    def test_stage_documents_processes_triage_pass_items(self, test_server):
        port, store = test_server
        queue_dir = Path(tempfile.mkdtemp(prefix="ohm-doc-queue-"))

        html = b"<html><body><h1>Queued Doc</h1><p>Queued content.</p></body></html>"
        queue_document_item(
            queue_dir,
            title="Queued HTML",
            content_bytes=html,
            filename="queued.html",
            content_type="text/html",
            source="test",
            target_stage="triage_pass",
        )

        processed = stage_documents(f"http://127.0.0.1:{port}", "", queue_dir)
        assert processed == 1

        # Item should have moved to source_created
        source_items = [json.loads(p.read_text()) for p in (queue_dir / "source_created").glob("*.json")]
        assert len(source_items) == 1
        assert "source_node_id" in source_items[0]

        # triage_pass should be empty of document items
        remaining = [json.loads(p.read_text()) for p in (queue_dir / "triage_pass").glob("*.json")]
        assert all(it.get("kind") != "document" for it in remaining)

    def test_stage_documents_ignores_non_document_items(self, test_server):
        port, store = test_server
        queue_dir = Path(tempfile.mkdtemp(prefix="ohm-doc-queue-"))

        # Seed a regular RSS-style item in triage_pass
        item = {
            "id": "rss-123",
            "title": "RSS Article",
            "url": "https://example.com/article",
            "kind": "article",
        }
        from ohm.ingestion.document_library_bridge import _write_queue_item

        _write_queue_item(queue_dir, "triage_pass", item)

        processed = stage_documents(f"http://127.0.0.1:{port}", "", queue_dir)
        assert processed == 0

        # Original item remains in triage_pass untouched
        items = [json.loads(p.read_text()) for p in (queue_dir / "triage_pass").glob("*.json")]
        assert len(items) == 1
        assert items[0]["kind"] == "article"
