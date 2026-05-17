"""Tests for OHM agent integrations."""

import pytest

from ohm.integrations import (
    ClioIntegration,
    HephaestusIntegration,
    MetisIntegration,
    SocratesIntegration,
)


@pytest.fixture
def graph():
    """Create an SDK Graph connected to an in-memory DB."""
    import ohm.sdk as ohm

    with ohm.connect(":memory:", actor="test_agent") as g:
        yield g


class TestMetisIntegration:
    """Tests for Métis zettelkasten integration."""

    def test_sync_note_creates_node(self, graph):
        metis = MetisIntegration(graph, source="conversation")
        node = metis.sync_note(
            note_id="note-1",
            title="Test Note",
            content="This is a test note about [[note-2]].",
            tags=["test", "integration"],
        )
        assert node["label"] == "Test Note"
        assert node["type"] == "idea"
        assert node["provenance"] == "conversation"

    def test_sync_note_with_wikilinks(self, graph):
        metis = MetisIntegration(graph, source="conversation")
        # First create the target note
        metis.sync_note(note_id="target", title="Target Note", content="Target")
        # Now create a note linking to it
        node = metis.sync_note(
            note_id="source",
            title="Source Note",
            content="Links to [[target]].",
            wikilinks=["target"],
        )
        # Verify the DERIVES_FROM edge was created
        edges = graph.search_edges(edge_type="DERIVES_FROM", layer="L2")
        assert len(edges) >= 1
        assert any(e["from_node"] == node["id"] for e in edges)

    def test_batch_sync(self, graph):
        metis = MetisIntegration(graph)
        notes = [
            {"id": "n1", "title": "Note 1", "content": "First"},
            {"id": "n2", "title": "Note 2", "content": "Second"},
            {"id": "n3", "title": "Note 3", "content": "Third"},
        ]
        results = metis.batch_sync(notes)
        assert len(results) == 3
        assert all(r["type"] == "idea" for r in results)


class TestClioIntegration:
    """Tests for Clio research integration."""

    def test_add_source(self, graph):
        clio = ClioIntegration(graph)
        source = clio.add_source("src-1", "Primary Source Title")
        assert source["type"] == "source"
        assert source["provenance"] == "research"

    def test_add_finding(self, graph):
        clio = ClioIntegration(graph)
        clio.add_source("src-a", "Source A")
        graph.create_node(label="Concept B", node_type="concept")
        edge = clio.add_finding(
            "src-a", "concept_b",
            edge_type="CAUSES",
            confidence=0.85,
            condition="when X > 0",
        )
        assert edge["edge_type"] == "CAUSES"
        assert edge["layer"] == "L3"
        assert edge["confidence"] == pytest.approx(0.85)
        assert edge["provenance"] == "research"

    def test_publish_synthesis(self, graph):
        clio = ClioIntegration(graph)
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")

        synthesis = clio.publish_synthesis(
            "Synthesis Title",
            "A causes B because...",
            supporting_edge_ids=[edge["id"]],
        )
        assert synthesis["type"] == "concept"
        assert synthesis["provenance"] == "research"


class TestHephaestusIntegration:
    """Tests for Hephaestus audit integration."""

    def test_register_entity(self, graph):
        heph = HephaestusIntegration(graph)
        entity = heph.register_entity("sys-1", "Auth Service", entity_type="system")
        assert entity["type"] == "system"
        assert entity["provenance"] == "audit"

    def test_report_finding(self, graph):
        heph = HephaestusIntegration(graph)
        entity = heph.register_entity("vuln-1", "Login Handler")
        obs = heph.report_finding(
            entity["id"],
            "vulnerability",
            value=8.5,
            sigma=2.0,
            description="SQL injection in login form",
        )
        assert obs["type"] == "vulnerability"
        assert obs["value"] == 8.5
        assert obs["sigma"] == 2.0

    def test_link_to_code(self, graph):
        heph = HephaestusIntegration(graph)
        entity = heph.register_entity("code-1", "User Module")
        code_node = graph.create_node(label="user.py", node_type="source")
        edge = heph.link_to_code(entity["id"], code_node["id"])
        assert edge["edge_type"] == "REFERENCES"
        assert edge["layer"] == "L2"
        assert edge["provenance"] == "audit"


class TestSocratesIntegration:
    """Tests for Socrates challenge integration."""

    def test_challenge_creates_challenged_by_edge(self, graph):
        socrates = SocratesIntegration(graph)
        a = graph.create_node(label="Claim A")["id"]
        b = graph.create_node(label="Claim B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")

        challenge = socrates.challenge(
            edge["id"],
            reason="The causal link is not well established.",
            category="insufficient_evidence",
            confidence=0.4,
        )
        assert challenge["edge_type"] == "CHALLENGED_BY"
        assert "insufficient_evidence" in challenge["condition"]

    def test_challenge_invalid_category_raises(self, graph):
        socrates = SocratesIntegration(graph)
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        edge = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")

        with pytest.raises(ValueError, match="Invalid challenge category"):
            socrates.challenge(edge["id"], reason="bad", category="not_a_category")

    def test_review_layer_finds_low_confidence_edges(self, graph):
        socrates = SocratesIntegration(graph)
        a = graph.create_node(label="A")["id"]
        b = graph.create_node(label="B")["id"]
        c = graph.create_node(label="C")["id"]

        # High confidence — should not appear in review
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.9)
        # Low confidence — should appear in review
        low_edge = graph.create_edge(from_node=b, to_node=c, edge_type="PREDICTS", layer="L3", confidence=0.3)

        review = socrates.review_layer("L3", max_confidence=0.5)
        assert len(review) >= 1
        assert any(e["id"] == low_edge["id"] for e in review)

    def test_challenge_categories_are_valid(self):
        assert "logical_flaw" in SocratesIntegration.CHALLENGE_CATEGORIES
        assert "insufficient_evidence" in SocratesIntegration.CHALLENGE_CATEGORIES
        assert "scope_too_narrow" in SocratesIntegration.CHALLENGE_CATEGORIES
        assert "alternative_explanation" in SocratesIntegration.CHALLENGE_CATEGORIES
