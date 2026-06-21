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


def test_s3_document_store_raises_not_implemented():
    store = S3DocumentStore()
    with pytest.raises(NotImplementedError):
        store.save("doc-1", "x.txt", b"x", "text/plain")
    with pytest.raises(NotImplementedError):
        store.get("doc-1")
    with pytest.raises(NotImplementedError):
        store.exists("doc-1")
