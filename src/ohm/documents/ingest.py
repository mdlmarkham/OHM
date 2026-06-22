"""Ingest files into OHM as document-tree sources."""

from __future__ import annotations

import uuid
from typing import Any

from pathlib import Path

from ohm.documents.extract import extract_text
from ohm.documents.store import DocumentStore
from ohm.ingestion.document_tree import parse_document
from ohm.ingestion.document_tree_ingest import ingest_document_tree


def ingest_file(
    store: DocumentStore,
    conn: Any,
    content_bytes: bytes,
    filename: str,
    content_type: str,
    created_by: str,
    *,
    tags: list[str] | None = None,
    provenance: str = "document-library",
    source_url: str | None = None,
) -> dict[str, Any]:
    """Save a file, extract its text, parse a document tree, and ingest into OHM.

    Args:
        store: DocumentStore used to persist raw bytes.
        conn: Active DuckDB connection.
        content_bytes: Raw file contents.
        filename: Original filename (used for extension-based type detection).
        content_type: MIME type of the file.
        created_by: Agent/agent name creating nodes.
        tags: Optional tags for the source node.
        provenance: Provenance tag for created nodes/edges.
        source_url: Optional URL source for the document.

    Returns:
        Dict with ``document_id``, ``stored_record``, ``source_node_id``,
        ``extracted_text_length``, and the underlying tree ingest result.
    """
    document_id = f"doc-{uuid.uuid4().hex[:12]}"

    metadata = {
        "ohm_document_id": document_id,
        "ohm_filename": filename,
        "ohm_content_type": content_type,
        "ohm_provenance": provenance,
    }
    if tags:
        metadata["ohm_tags"] = tags
    if source_url:
        metadata["ohm_source_url"] = source_url

    stored = store.save(
        document_id=document_id,
        filename=filename,
        content_bytes=content_bytes,
        content_type=content_type,
        metadata=metadata,
    )

    text = extract_text(content_bytes, content_type, filename=filename)

    tree = parse_document(
        text,
        source_id=document_id,
        title=Path(filename).name,
        content_type="html" if _is_html(content_type, filename) else "markdown",
    )

    ingest_result = ingest_document_tree(
        conn,
        tree,
        created_by=created_by,
        provenance=provenance,
        source_url=source_url,
        tags=tags,
    )

    # Link the persisted file record to its graph source node.
    source_node_id = ingest_result["source_id"]
    store.update_metadata(document_id, source_node_id=source_node_id)

    return {
        "document_id": document_id,
        "stored_record": stored,
        "source_node_id": source_node_id,
        "extracted_text_length": len(text),
        "tree_result": ingest_result,
    }


def _is_html(content_type: str | None, filename: str) -> bool:
    if content_type:
        return content_type.split(";")[0].strip().lower() == "text/html"
    if filename:
        return filename.lower().endswith((".html", ".htm"))
    return False
