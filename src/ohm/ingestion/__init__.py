"""OHM structured ingestion helpers.

Document-tree ingestion turns HTML/Markdown sources into graph fragments:
source → sections → paragraphs/tables/lists with CONTAINS / PART_OF edges.
"""

from ohm.ingestion.document_tree import (
    DocumentNode,
    DocumentTree,
    keyword_overlap,
    parse_document,
    parse_html_to_tree,
    parse_markdown_to_tree,
)
from ohm.ingestion.document_tree_ingest import ingest_document_tree

__all__ = [
    "DocumentNode",
    "DocumentTree",
    "ingest_document_tree",
    "keyword_overlap",
    "parse_document",
    "parse_html_to_tree",
    "parse_markdown_to_tree",
]
