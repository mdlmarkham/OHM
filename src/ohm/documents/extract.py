"""Text extraction helpers for the OHM document library.

Dispatch by content type:
  - application/pdf: pypdf or pdfplumber (graceful fallback)
  - text/html and text/markdown: parse via existing document_tree parser
  - text/plain: passthrough
"""

from __future__ import annotations

import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import Any


def extract_text(content_bytes: bytes, content_type: str | None = None, *, filename: str | None = None) -> str:
    """Extract plain text from bytes based on the supplied content type.

    If ``content_type`` is missing or generic (e.g. ``application/octet-stream``),
    it is inferred from ``filename`` when provided.
    """
    ct = _normalize_content_type(content_type, filename)
    if ct == "application/pdf":
        return _extract_pdf(content_bytes)
    if ct in {"text/html", "text/markdown", "text/x-markdown"}:
        from ohm.ingestion.document_tree import parse_document

        text = content_bytes.decode("utf-8", errors="replace")
        tree = parse_document(text, content_type="html" if ct == "text/html" else "markdown")
        return tree.root.text or ""
    if ct in {"text/plain", "text/x-python", "text/javascript", "application/json", "application/xml"}:
        return content_bytes.decode("utf-8", errors="replace")

    raise ValueError(f"Unsupported content type for text extraction: {content_type!r}")


def _normalize_content_type(content_type: str | None, filename: str | None) -> str:
    ct = (content_type or "").strip().lower()
    if ct and ct != "application/octet-stream":
        return ct.split(";")[0].strip()
    if filename:
        ext = Path(filename).suffix.lower()
        mapping = {
            ".pdf": "application/pdf",
            ".html": "text/html",
            ".htm": "text/html",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".txt": "text/plain",
            ".py": "text/plain",
            ".js": "text/plain",
            ".json": "text/plain",
            ".xml": "text/plain",
        }
        return mapping.get(ext, "application/octet-stream")
    return "application/octet-stream"


class UnsupportedDocumentError(RuntimeError):
    """Raised when a document type cannot be extracted."""


def _extract_pdf(content_bytes: bytes) -> str:
    """Extract text from a PDF, trying pypdf first and pdfplumber second."""
    exceptions: list[str] = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(content_bytes)
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:
                exceptions.append(f"pypdf page error: {e}")
        text = "\n".join(parts)
        if text.strip():
            return text
    except Exception as e:
        exceptions.append(f"pypdf: {e}")

    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(content_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                try:
                    page_text = page.extract_text()
                    if page_text:
                        parts.append(page_text)
                except Exception as e:
                    exceptions.append(f"pdfplumber page error: {e}")
            text = "\n".join(parts)
            if text.strip():
                return text
    except Exception as e:
        exceptions.append(f"pdfplumber: {e}")

    raise UnsupportedDocumentError(
        "No PDF extraction backend available. Install pypdf or pdfplumber. "
        f"Errors: {'; '.join(exceptions) if exceptions else 'none'}"
    )


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


def _detect_content_type(filename: str) -> str | None:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype
