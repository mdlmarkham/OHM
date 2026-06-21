"""OHM document library: store, extract, and ingest files/PDFs as document trees."""

from __future__ import annotations

from ohm.documents.extract import extract_text
from ohm.documents.ingest import ingest_file
from ohm.documents.store import DocumentStore, LocalDocumentStore, S3DocumentStore

__all__ = [
    "DocumentStore",
    "extract_text",
    "ingest_file",
    "LocalDocumentStore",
    "S3DocumentStore",
]
