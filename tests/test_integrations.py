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


class TestAgentWorkflowE2E:
    """End-to-end agent workflow: Métis → Clio → Socrates → Hephaestus."""

    def test_full_agent_workflow(self, graph):
        """Simulate the complete agent collaboration cycle.

        1. Métis creates zettelkasten notes with wikilinks
        2. Clio researches and publishes findings as L3 edges
        3. Socrates challenges a low-confidence finding
        4. Hephaestus audits the code and reports observations
        """
        # ── Phase 1: Métis creates notes ──────────────────────────────
        metis = MetisIntegration(graph, source="conversation")
        metis.sync_note(
            note_id="note-democracy",
            title="Democratic Backsliding Patterns",
            content="Research on AND→OR conversion in constitutional amendments.",
            tags=["democracy", "constitution"],
            wikilinks=["note-hungary", "note-poland"],
        )
        metis.sync_note(
            note_id="note-hungary",
            title="Hungary Case Study",
            content="Fidesz used Article 21(2) to amend constitution.",
            tags=["hungary", "case-study"],
        )

        # Verify Métis created nodes and edges
        nodes = graph.search_nodes("Democratic")
        assert len(nodes) >= 1
        edges = graph.search_edges(edge_type="DERIVES_FROM", layer="L2")
        assert len(edges) >= 1

        # ── Phase 2: Clio researches ──────────────────────────────────
        clio = ClioIntegration(graph)
        clio.add_source("src-constitution", "Hungarian Constitution Art. 21(2)")
        finding = clio.add_finding(
            "src-constitution", "note-democracy",
            edge_type="CAUSES",
            confidence=0.85,
            condition="when supermajority controls parliament",
        )
        assert finding["edge_type"] == "CAUSES"
        assert finding["provenance"] == "research"

        # Clio publishes a synthesis
        synthesis = clio.publish_synthesis(
            "AND→OR Conversion Mechanism",
            "Supermajority amendments enable democratic backsliding via...",
            supporting_edge_ids=[finding["id"]],
        )
        assert synthesis["type"] == "concept"

        # ── Phase 3: Socrates challenges ──────────────────────────────
        socrates = SocratesIntegration(graph)
        challenge = socrates.challenge(
            finding["id"],
            reason="Scope too narrow — only covers parliamentary systems.",
            category="scope_too_narrow",
            confidence=0.4,
        )
        assert challenge["edge_type"] == "CHALLENGED_BY"
        assert "scope_too_narrow" in challenge["condition"]

        # Verify the original edge is preserved (ADR-003)
        original = graph.get_edge(finding["id"])
        assert original is not None
        assert original["edge_type"] == "CAUSES"

        # ── Phase 4: Hephaestus audits ────────────────────────────────
        heph = HephaestusIntegration(graph)
        entity = heph.register_entity("code-analyzer", "Constitutional Analysis Module")
        obs = heph.report_finding(
            entity["id"],
            "vulnerability",
            value=7.5,
            sigma=1.5,
            description="Missing validation for amendment threshold calculations",
        )
        assert obs["type"] == "vulnerability"
        assert obs["value"] == pytest.approx(7.5)

        # Link audit finding to code
        code_node = graph.create_node(label="analyzer.py", node_type="source")
        edge = heph.link_to_code(entity["id"], code_node["id"])
        assert edge["edge_type"] == "REFERENCES"

        # ── Final verification ────────────────────────────────────────
        stats = graph.stats()
        assert stats["total_nodes"] >= 8  # All nodes created across phases
        assert stats["total_edges"] >= 5  # DERIVES_FROM + CAUSES + CHALLENGED_BY + REFERENCES + SUPPORTS
        assert stats["total_observations"] >= 1
