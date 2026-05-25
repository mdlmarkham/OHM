"""
OHM Integration Tests — full workflow tests.
"""

import pytest

from ohm.store import OhmStore
from ohm.queries import query_neighborhood
from ohm.exceptions import PermissionDeniedError


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_ohm.duckdb")


@pytest.fixture
def store(db_path):
    s = OhmStore(db_path=db_path, agent_name="metis")
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Graph with realistic AND→OR research data."""
    store.write_node("hungary", "Hungary", "institution", "Republic of Hungary")
    store.write_node("art21", "Article 21(2)", "concept", "Constitutional provision")
    store.write_node("and_or", "AND→OR Conversion", "pattern", "Boolean logic direction")
    store.write_node("democratic_escape", "Democratic Escape", "concept", "Institutional exit mechanism")
    store.write_node("magyar", "Magyar", "person", "Peter Magyar, Hungarian opposition leader")

    store.write_edge("art21", "hungary", "BELONGS_TO", "L1", confidence=1.0)
    store.write_edge("and_or", "art21", "CAUSES", "L3", confidence=0.94, provenance="conversation")
    store.write_edge("and_or", "democratic_escape", "PREDICTS", "L3", confidence=0.94)
    store.write_edge("magyar", "democratic_escape", "SUPPORTS", "L3", confidence=0.75, provenance="research")

    return store


class TestFullWorkflow:
    """Test complete knowledge graph workflows."""

    def test_create_and_query(self, populated_store):
        """Create nodes and edges, then query neighborhood."""
        results = query_neighborhood(populated_store.conn, "hungary", depth=2)
        assert len(results) > 0
        # Should find art21 → hungary and and_or → art21 → hungary
        edge_types = {r["edge_type"] for r in results}
        assert "BELONGS_TO" in edge_types

    def test_challenge_and_support(self, populated_store):
        """Challenge an edge, then support it from another agent."""
        # Get the CAUSES edge
        edge = populated_store.execute_one("SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'")
        assert edge is not None

        # Socrates challenges
        populated_store.agent_name = "socrates"
        challenge = populated_store.challenge_edge(edge["id"], "conditions too narrow for Hungary-specific case", 0.5, "CHALLENGED_BY")
        assert challenge["challenge_type"] == "CHALLENGED_BY"
        assert challenge["created_by"] == "socrates"

        # Clio supports
        populated_store.agent_name = "clio"
        support = populated_store.challenge_edge(edge["id"], "3 additional case studies confirmed", 0.85, "SUPPORTS")
        assert support["challenge_type"] == "SUPPORTS"
        assert support["created_by"] == "clio"

        # Original edge unchanged
        original = populated_store.get_edge(edge["id"])
        assert abs(original["confidence"] - 0.94) < 0.001

    def test_agent_state_coordination(self, populated_store):
        """Test hive mind awareness via agent state."""
        # Metis sets state
        populated_store.agent_name = "metis"
        populated_store.update_agent_state(
            current_focus="Researching AND→OR patterns in Hungary",
            active_patterns=["and-or", "hungary", "democratic-institutions"],
            available_services=["research", "critique", "synthesize"],
        )

        # Clio sets state
        populated_store.agent_name = "clio"
        populated_store.update_agent_state(
            current_focus="Deep research on democratic institutions",
            active_patterns=["democratic-institutions", "europe"],
        )

        # Who is working on hungary?
        results = populated_store.who_is_working_on("hungary")
        assert len(results) >= 1

    def test_ownership_boundary(self, populated_store):
        """Only the owning agent can update their own L3 edges."""
        # Metis owns the CAUSES edge
        edge = populated_store.execute_one("SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'")

        # Metis can update
        populated_store.agent_name = "metis"
        result = populated_store.update_edge_confidence(edge["id"], 0.96)
        assert abs(result["confidence"] - 0.96) < 0.001

        # Socrates cannot update — must challenge instead
        populated_store.agent_name = "socrates"
        with pytest.raises(PermissionDeniedError):
            populated_store.update_edge_confidence(edge["id"], 0.5)

    def test_observations(self, populated_store):
        """Create observations on nodes."""
        obs = populated_store.write_observation("hungary", "measurement", value=0.85, baseline=0.5, sigma=3.5, source="research")
        assert obs["node_id"] == "hungary"
        assert abs(obs["value"] - 0.85) < 0.001

    def test_status(self, populated_store):
        """Check graph status."""
        status = populated_store.status()
        assert status["node_count"] == 5
        assert status["edge_count"] == 4
        assert "L1" in status["edges_by_layer"]
        assert "L3" in status["edges_by_layer"]


class TestLayerModel:
    """Test that layers behave as designed."""

    def test_l1_shared(self, store):
        """L1 edges are shared — any agent can write."""
        store.write_node("site_a", "Site A", "site")
        store.write_node("area_b", "Area B", "area")

        store.agent_name = "metis"
        store.write_edge("area_b", "site_a", "CONTAINS", "L1", confidence=1.0)

        store.agent_name = "clio"
        # Clio can also write L1 edges
        store.write_node("site_c", "Site C", "site")
        store.write_edge("area_b", "site_c", "CONTAINS", "L1", confidence=1.0)

        status = store.status()
        assert status["edge_count"] == 2

    def test_l3_agent_owned(self, store):
        """L3 edges are agent-owned — only owner can update."""
        store.write_node("idea_a", "Idea A", "idea")
        store.write_node("idea_b", "Idea B", "idea")

        store.agent_name = "metis"
        edge = store.write_edge("idea_a", "idea_b", "CAUSES", "L3", confidence=0.8)

        # Metis can update
        result = store.update_edge_confidence(edge["id"], 0.85)
        assert result is not None

        # Clio cannot update
        store.agent_name = "clio"
        with pytest.raises(PermissionDeniedError):
            store.update_edge_confidence(edge["id"], 0.5)

    def test_challenge_preserves_original(self, store):
        """Challenges create new edges, never modify originals."""
        store.write_node("x", "X", "concept")
        store.write_node("y", "Y", "concept")

        store.agent_name = "metis"
        edge = store.write_edge("x", "y", "PREDICTS", "L3", confidence=0.9)
        original_id = edge["id"]

        store.agent_name = "socrates"
        challenge = store.challenge_edge(edge["id"], "insufficient evidence", 0.4)
        assert challenge["challenge_of"] == original_id
        assert challenge["id"] != original_id

        # Original unchanged
        original = store.get_edge(original_id)
        assert abs(original["confidence"] - 0.9) < 0.001
