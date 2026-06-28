"""Tests for the OHM document library (store, extract, ingest, upload)."""

from __future__ import annotations

import json
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from ohm.documents.extract import extract_text
from ohm.documents.ingest import ingest_file
from ohm.documents.store import LocalDocumentStore, S3DocumentStore
from ohm.ingestion.document_tree import parse_document
from ohm.schema import initialize_schema

from tests.conftest import _request, _start_test_server

pytestmark = pytest.mark.integration


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_pdf_bytes(text: str = "Hello, PDF!\nThis is a test document.") -> bytes:
    """Create a minimal valid PDF with embedded text using raw PDF syntax."""
    page_height = 700
    lines = text.split("\n")
    y_positions = [page_height - (i * 20) for i in range(len(lines))]
    text_ops = "\n".join(f"100 {y} Td\n({line}) Tj" for y, line in zip(y_positions, lines))
    stream = f"""BT
/F1 12 Tf
{text_ops}
ET"""
    content_length = len(stream.encode("latin-1"))
    pdf = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length {content_length} >>
stream
{stream}
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000355 00000 n
trailer
<< /Size 6 /Root 1 0 R >>
startxref
500
%%EOF"""
    return pdf.encode("latin-1")


def _multipart_body(filename: str, content: bytes, boundary: str = "----ohm-test-boundary") -> tuple[bytes, str]:
    """Return ``(body_bytes, content_type)`` for a simple multipart upload."""
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("latin-1")
    body += content
    body += f"\r\n--{boundary}--\r\n".encode("latin-1")
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


# ── Local store tests ──────────────────────────────────────────────────────


class TestLocalDocumentStore:
    def test_save_get_exists_roundtrip(self, tmp_path):
        base = tmp_path / "docs"
        store = LocalDocumentStore(str(base))
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        data = b"hello document"
        record = store.save(document_id, "hello.txt", data, "text/plain")
        assert store.exists(document_id)
        assert store.get(document_id) == data
        assert record["size"] == len(data)
        assert record["uri"].startswith("file://")

    def test_pdf_save_uses_pdf_extension(self, tmp_path):
        base = tmp_path / "docs"
        store = LocalDocumentStore(str(base))
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        pdf = _make_pdf_bytes()
        record = store.save(document_id, "report.pdf", pdf, "application/pdf")
        assert Path(record["stored_path"]).exists()
        assert Path(record["stored_path"]).suffix == ".pdf"


# ── Extraction tests ─────────────────────────────────────────────────────


class TestExtractText:
    def test_plain_text_passthrough(self):
        text = "Plain text content."
        result = extract_text(text.encode("utf-8"), "text/plain")
        assert result == text

    def test_pdf_text_extraction(self):
        pytest.importorskip("pdfplumber")
        pdf = _make_pdf_bytes("Hello, PDF!\nExtracted text works.")
        result = extract_text(pdf, "application/pdf")
        assert "Hello, PDF!" in result

    def test_html_extraction(self):
        html = "<html><body><h1>Doc Title</h1><p>Paragraph text.</p></body></html>"
        result = extract_text(html.encode("utf-8"), "text/html")
        assert "Doc Title" in result

    def test_markdown_extraction(self):
        md = "# Title\n\nA paragraph."
        result = extract_text(md.encode("utf-8"), "text/markdown")
        assert "Title" in result


# ── Ingestion tests ────────────────────────────────────────────────────────


class TestIngestFile:
    def test_html_ingestion_creates_source_tree_nodes(self, test_db, tmp_path):
        html = "<html><body><h1>Doc Title</h1><p>Body text.</p></body></html>"
        store = LocalDocumentStore(str(tmp_path / "docs"))
        result = ingest_file(
            store=store,
            conn=test_db,
            content_bytes=html.encode("utf-8"),
            filename="doc.html",
            content_type="text/html",
            created_by="test_agent",
        )
        assert result["document_id"]
        assert result["source_node_id"]
        assert result["extracted_text_length"] > 0
        node = test_db.execute("SELECT type FROM ohm_nodes WHERE id = ?", [result["source_node_id"]]).fetchone()
        assert node is not None
        assert node[0] == "source"


# ── HTTP endpoint tests ──────────────────────────────────────────────────


class TestDocumentUploadEndpoint:
    def test_upload_small_file_returns_source_node_id(self, test_server, tmp_path):
        port, store = test_server
        md = "# Uploaded Document\n\nThis file was uploaded via HTTP."
        body, content_type = _multipart_body("upload.md", md.encode("utf-8"))
        conn = _http_conn(port)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        conn.request("POST", "/documents/upload", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        assert resp.status == 200, data
        result = json.loads(data)
        assert "document_id" in result
        assert "source_node_id" in result
        assert result["extracted_text_length"] > 0
        node = store.execute_one(
            "SELECT type FROM ohm_nodes WHERE id = ?",
            [result["source_node_id"]],
        )
        assert node is not None
        assert node["type"] == "source"

    def test_upload_rejects_unknown_content_type(self, test_server):
        port, _ = test_server
        body, content_type = _multipart_body("data.xyz", b"some unknown format")
        conn = _http_conn(port)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }
        conn.request("POST", "/documents/upload", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        assert resp.status == 400
        assert "Unsupported" in data or "content type" in data.lower()

    def test_get_document_metadata(self, test_server, tmp_path):
        port, store = test_server
        md = "# Uploaded Document\n\nThis file was uploaded via HTTP."
        body, content_type = _multipart_body("upload.md", md.encode("utf-8"))
        status, upload_data = _request(
            "POST",
            port,
            "/documents/upload",
            body=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        assert status == 200, upload_data
        document_id = upload_data["document_id"]

        status, data = _request("GET", port, f"/documents/{document_id}")
        assert status == 200, data
        assert data["document_id"] == document_id
        assert data["filename"] == "upload.md"
        assert data["content_type"] == "text/markdown"
        assert "source_node" in data
        assert data["source_node"]["type"] == "source"

    def test_get_document_download(self, test_server, tmp_path):
        port, store = test_server
        md = "# Downloadable Document\n\nRetrieve me."
        body, content_type = _multipart_body("download.md", md.encode("utf-8"))
        status, upload_data = _request(
            "POST",
            port,
            "/documents/upload",
            body=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        assert status == 200, upload_data
        document_id = upload_data["document_id"]

        conn = _http_conn(port)
        conn.request("GET", f"/documents/{document_id}/download")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "text/markdown"
        assert b"Retrieve me" in body

    def test_get_missing_document_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/documents/doc-doesnotexist1234")
        assert status == 404

    def test_upload_url_fetch(self, test_server, tmp_path):
        port, store = test_server
        md = "# URL Document\n\nFetched from a local test server."
        md_bytes = md.encode("utf-8")

        # Spin up a tiny HTTP server to serve the file
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown")
                self.send_header("Content-Length", str(len(md_bytes)))
                self.end_headers()
                self.wfile.write(md_bytes)

            def log_message(self, *args):
                pass

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        import threading

        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/doc.md"
            status, data = _request("POST", port, "/documents/upload", body={"url": url})
            assert status == 200, data
            assert "source_node_id" in data
            assert data["extracted_text_length"] > 0
        finally:
            srv.shutdown()


def _http_conn(port: int):
    from http.client import HTTPConnection

    return HTTPConnection(f"127.0.0.1:{port}", timeout=15)


# ── S3 stub test ─────────────────────────────────────────────────────────


def test_s3_document_store_requires_bucket_env():
    """S3DocumentStore now requires bucket configuration; verify helpful error."""
    with pytest.raises(RuntimeError, match="OHM_S3_BUCKET"):
        S3DocumentStore()


# ── S3 backend roundtrip tests (OHM-2ibj) ──────────────────────────────────
# Use moto.mock_aws() to provide a fake S3 endpoint in-process so we can
# exercise save/get/exists/get_record/update_metadata/delete against a real
# boto3 client without AWS credentials or network access.


class TestS3DocumentStore:
    """End-to-end S3DocumentStore roundtrip against a moto-mocked S3."""

    @pytest.fixture(autouse=True)
    def _s3_env(self, monkeypatch):
        monkeypatch.setenv("OHM_S3_BUCKET", "ohm-test-bucket")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)

    def _store(self):
        moto = pytest.importorskip("moto")
        ctx = moto.mock_aws()
        ctx.start()
        store = S3DocumentStore()
        store.client.create_bucket(Bucket="ohm-test-bucket")
        # Make the context manager manageable by the test — stop in teardown.
        self._moto_ctx = ctx
        return store

    def teardown_method(self):
        ctx = getattr(self, "_moto_ctx", None)
        if ctx is not None:
            try:
                ctx.stop()
            except RuntimeError:
                pass
            self._moto_ctx = None

    def test_save_get_exists_roundtrip(self):
        store = self._store()
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        data = b"hello s3 document"
        record = store.save(document_id, "hello.txt", data, "text/plain")
        assert store.exists(document_id)
        assert store.get(document_id) == data
        assert record["size"] == len(data)
        assert record["uri"].startswith("s3://")
        assert record["bucket"] == "ohm-test-bucket"
        assert record["key"].endswith("hello.txt")

    def test_pdf_save_uses_pdf_extension(self):
        store = self._store()
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        pdf = _make_pdf_bytes()
        record = store.save(document_id, "report.pdf", pdf, "application/pdf")
        # The stored S3 object key should end with .pdf
        assert record["key"].endswith("report.pdf")
        assert store.get(document_id) == pdf

    def test_get_record_returns_metadata(self):
        store = self._store()
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        store.save(document_id, "note.md", b"# Title", "text/markdown")
        record = store.get_record(document_id)
        assert record["document_id"] == document_id
        assert record["filename"] == "note.md"
        assert record["content_type"] == "text/markdown"
        assert record["size"] == 7

    def test_get_missing_raises_filenotfound(self):
        store = self._store()
        with pytest.raises(FileNotFoundError):
            store.get("does-not-exist-1234")

    def test_get_record_missing_raises_filenotfound(self):
        store = self._store()
        with pytest.raises(FileNotFoundError):
            store.get_record("does-not-exist-1234")

    def test_exists_false_for_unknown(self):
        store = self._store()
        assert store.exists("does-not-exist-1234") is False

    def test_update_metadata_persists(self):
        store = self._store()
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        store.save(document_id, "doc.txt", b"body", "text/plain")
        updated = store.update_metadata(
            document_id,
            source_node_id="node-abc",
            tags=["test", "ingest"],
        )
        assert updated["source_node_id"] == "node-abc"
        assert "updated_at" in updated
        # Re-read to confirm persistence
        reread = store.get_record(document_id)
        assert reread["source_node_id"] == "node-abc"
        assert reread["tags"] == ["test", "ingest"]

    def test_delete_removes_objects(self):
        store = self._store()
        document_id = f"doc-{uuid.uuid4().hex[:12]}"
        store.save(document_id, "gone.txt", b"bye", "text/plain")
        assert store.exists(document_id)
        store.delete(document_id)
        assert store.exists(document_id) is False
        with pytest.raises(FileNotFoundError):
            store.get(document_id)

    def test_custom_prefix_respects_namespace(self, monkeypatch):
        # Re-init with a different prefix to confirm objects land under it.
        monkeypatch.setenv("OHM_S3_PREFIX", "custom/prefix/")
        moto = pytest.importorskip("moto")
        with moto.mock_aws():
            store = S3DocumentStore()
            store.client.create_bucket(Bucket="ohm-test-bucket")
            document_id = f"doc-{uuid.uuid4().hex[:12]}"
            record = store.save(document_id, "f.txt", b"hi", "text/plain")
            assert record["key"].startswith("custom/prefix/")
            assert store.get(document_id) == b"hi"
