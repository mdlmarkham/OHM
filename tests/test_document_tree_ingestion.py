"""Tests for document-tree ingestion (OHM-document-tree PoC)."""

from __future__ import annotations

import pytest

pytest.importorskip("bs4")

from ohm.ingestion.document_tree import (
    DocumentNode,
    DocumentTree,
    keyword_overlap,
    parse_document,
    parse_html_to_tree,
    parse_markdown_to_tree,
)
from ohm.ingestion.document_tree_ingest import ingest_document_tree
from ohm.queries import create_node


# ── Document tree parser tests ─────────────────────────────────────────────


class TestDocumentTreeParser:
    """Unit tests for document_tree.py."""

    def test_parse_empty_document(self):
        tree = parse_document("")
        assert tree.root.node_type == "source"
        assert tree.root.text == ""
        assert tree.flat == [tree.root]

    def test_parse_markdown_headings(self):
        md = """# Title

Intro paragraph.

## Section A

Paragraph in section A.

### Subsection

Deep paragraph.

## Section B

- item one
- item two
"""
        tree = parse_markdown_to_tree(md, source_id="src-md")
        assert tree.title == "Title"
        assert tree.content_type == "markdown"
        assert tree.root.id == "src-md"
        assert tree.root.children

        flat = tree.iter_flat()
        types = [n.node_type for n in flat]
        assert "source" in types
        assert "section" in types
        assert "paragraph" in types
        assert "list" in types

    def test_parse_html_headings(self):
        html = """<html><body>
<h1>Doc Title</h1>
<p>Intro paragraph.</p>
<h2>Section A</h2>
<p>Paragraph A.</p>
<table><tr><td>Cell</td></tr></table>
</body></html>"""
        tree = parse_html_to_tree(html, source_id="src-html")
        assert tree.title == "Doc Title"
        assert tree.content_type == "html"
        flat = tree.iter_flat()
        types = {n.node_type for n in flat}
        assert {"source", "section", "paragraph", "table"}.issubset(types)

    def test_auto_detect_html(self):
        html = "<h1>Auto</h1><p>paragraph</p>"
        tree = parse_document(html, source_id="src-auto")
        assert tree.content_type == "html"
        assert tree.title == "Auto"

    def test_auto_detect_markdown(self):
        md = "# Auto MD\n\nparagraph text"
        tree = parse_document(md, source_id="src-md-auto")
        assert tree.content_type == "markdown"
        assert tree.title == "Auto MD"

    def test_hierarchy_levels(self):
        md = "# H1\n\n## H2\n\n### H3\n\nparagraph"
        tree = parse_markdown_to_tree(md, source_id="src-levels")
        flat = tree.iter_flat()
        levels = {n.id: n.level for n in flat}
        root = tree.root
        assert root.level == 0
        # children of root should include h1 section
        h1 = next(n for n in root.children if n.node_type == "section")
        assert h1.level == 1
        h2 = next(n for n in h1.children if n.node_type == "section")
        assert h2.level == 2
        h3 = next(n for n in h2.children if n.node_type == "section")
        assert h3.level == 3

    def test_parent_assignment(self):
        md = "# Title\n\nParagraph under title.\n\n## Section\n\nParagraph under section."
        tree = parse_markdown_to_tree(md, source_id="src-parent")
        root = tree.root
        h1 = next(c for c in root.children if c.node_type == "section")
        top = [c for c in h1.children if c.node_type == "paragraph" and c.text == "Paragraph under title."]
        assert len(top) == 1
        assert top[0].parent_id == h1.id

        section = next(c for c in h1.children if c.node_type == "section")
        para = [c for c in section.children if c.node_type == "paragraph"]
        assert len(para) == 1
        assert para[0].parent_id == section.id

    def test_keyword_overlap(self):
        matches = keyword_overlap(
            "The AND gate converts demand rationing into an OR gate pattern.",
            ["AND gate", "OR gate", "demand rationing", "climate change"],
        )
        assert len(matches) == 3
        labels = {m[0] for m in matches}
        assert "climate change" not in labels
        assert matches[0][1] >= matches[-1][1]

    def test_keyword_overlap_empty(self):
        assert keyword_overlap("", ["foo"]) == []
        assert keyword_overlap("foo", []) == []


# ── Graph ingestion tests ──────────────────────────────────────────────────


class TestDocumentTreeIngestion:
    """Integration tests for document_tree_ingest.py against DuckDB."""

    def test_ingest_creates_source_node(self, test_db):
        md = "# Source Title\n\nBody text."
        tree = parse_document(md, source_id="src-1")
        result = ingest_document_tree(test_db, tree, created_by="test_agent")
        assert result["source_id"]
        row = test_db.execute("SELECT type FROM ohm_nodes WHERE id = ?", [result["source_id"]]).fetchone()
        assert row[0] == "source"

    def test_ingest_creates_contains_edges(self, test_db):
        md = "# Title\n\nIntro.\n\n## Section A\n\nParagraph A."
        tree = parse_document(md, source_id="src-2")
        result = ingest_document_tree(test_db, tree, created_by="test_agent")
        assert len(result["created_edges"]) >= 2  # source→section, section→paragraph + reverses

        edge_types = test_db.execute("SELECT edge_type, COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL GROUP BY edge_type").fetchall()
        counts = {row[0]: row[1] for row in edge_types}
        assert counts.get("CONTAINS", 0) >= 1
        assert counts.get("PART_OF", 0) >= 1

    def test_ingest_links_concepts(self, test_db):
        # Seed existing concept
        create_node(
            test_db,
            label="Hormuz AND-Gate",
            node_type="concept",
            content="A strategic bottleneck pattern.",
            created_by="test_agent",
        )
        md = "# Article\n\nThe Hormuz AND-Gate controls maritime chokepoints."
        tree = parse_document(md, source_id="src-3")
        result = ingest_document_tree(test_db, tree, created_by="test_agent", concept_labels=["Hormuz AND-Gate"])
        assert result["matched_concepts"]
        assert result["matched_concepts"][0]["concept_label"] == "Hormuz AND-Gate"

    def test_ingest_respects_link_threshold(self, test_db):
        create_node(
            test_db,
            label="Unrelated Concept",
            node_type="concept",
            content="Nothing in common.",
            created_by="test_agent",
        )
        md = "# Article\n\nThis paragraph talks about something completely different."
        tree = parse_document(md, source_id="src-4")
        result = ingest_document_tree(
            test_db,
            tree,
            created_by="test_agent",
            concept_labels=["Unrelated Concept"],
        )
        assert not result["matched_concepts"]

    def test_ingest_metadata(self, test_db):
        md = "# MetaDoc\n\nText.\n\n## Section\n\nMore text."
        tree = parse_document(md, source_id="src-5")
        result = ingest_document_tree(
            test_db,
            tree,
            created_by="test_agent",
            provenance="bookmark",
            source_url="https://example.com",
            tags=["modora", "rag"],
        )
        row = test_db.execute(
            "SELECT provenance, url, tags FROM ohm_nodes WHERE id = ?",
            [result["source_id"]],
        ).fetchone()
        assert row[0] == "bookmark"
        assert row[1] == "https://example.com"
        assert "modora" in row[2]
