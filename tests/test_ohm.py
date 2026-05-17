"""
OHM Tests — comprehensive test suite for the knowledge graph.
"""

import pytest

from ohm.store import OhmStore
from ohm.queries import query_neighborhood, query_path, query_impact


@pytest.fixture
def store(tmp_path):
    """Create a temporary store for testing."""
    db_path = str(tmp_path / "test_ohm.duckdb")
    s = OhmStore(db_path=db_path, agent_name="test_agent")
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Create a store with test data."""
    # Create nodes
    store.write_node("hungary", "Hungary", "institution", "Republic of Hungary")
    store.write_node("art21", "Article 21(2)", "concept", "Constitutional provision")
    store.write_node("and_or", "AND→OR Conversion", "pattern", "Boolean logic direction")
    store.write_node("democratic_escape", "Democratic Escape", "concept", "Institutional exit mechanism")

    # Create edges
    store.write_edge("art21", "hungary", "BELONGS_TO", "L1", confidence=1.0)
    store.write_edge("and_or", "art21", "CAUSES", "L3", confidence=0.94)
    store.write_edge("and_or", "democratic_escape", "PREDICTS", "L3", confidence=0.94)

    return store


class TestOhmStore:
    """Test the OHM store operations."""

    def test_write_node(self, store):
        node = store.write_node("test", "Test Node", "concept", "Test content")
        assert node["id"] == "test"
        assert node["label"] == "Test Node"
        assert node["created_by"] == "test_agent"

    def test_write_node_with_agent_name_override(self, store):
        """OHM-y2i.19: write_node should accept agent_name parameter."""
        node = store.write_node("override_node", "Override", "concept", agent_name="metis")
        assert node["created_by"] == "metis"

    def test_write_node_upsert_with_different_agent(self, store):
        """OHM-y2i.19: upsert should update updated_by to the new agent."""
        store.write_node("test", "Original", "concept")
        node = store.write_node("test", "Updated", "concept", agent_name="socrates")
        assert node["label"] == "Updated"
        assert node["updated_by"] == "socrates"

    def test_write_node_upsert(self, store):
        store.write_node("test", "Original", "concept")
        node = store.write_node("test", "Updated", "concept")
        assert node["label"] == "Updated"
        assert node["updated_by"] == "test_agent"

    def test_write_edge(self, populated_store):
        edges = populated_store.execute("SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'")
        assert len(edges) == 1
        assert edges[0]["created_by"] == "test_agent"
        assert edges[0]["layer"] == "L3"

    def test_write_edge_with_agent_name_override(self, populated_store):
        """OHM-y2i.19: write_edge should accept agent_name parameter."""
        edge = populated_store.write_edge(
            "and_or", "hungary", "EXPLAINS", "L3", confidence=0.8, agent_name="metis"
        )
        assert edge["created_by"] == "metis"

    def test_challenge_edge(self, populated_store):
        # Get the CAUSES edge
        edge = populated_store.execute_one(
            "SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'"
        )
        assert edge is not None

        # Challenge it as a different agent
        populated_store.agent_name = "socrates"
        challenge = populated_store.challenge_edge(
            edge["id"], "conditions too narrow", 0.5, "CHALLENGED_BY"
        )
        assert challenge is not None
        assert challenge["challenge_of"] == edge["id"]
        assert challenge["challenge_type"] == "CHALLENGED_BY"
        assert challenge["created_by"] == "socrates"

        # Original edge unchanged
        original = populated_store.get_edge(edge["id"])
        assert abs(original["confidence"] - 0.94) < 0.001

    def test_challenge_edge_with_agent_name_override(self, populated_store):
        """OHM-y2i.19: challenge_edge should accept agent_name parameter."""
        edge = populated_store.execute_one(
            "SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'"
        )
        challenge = populated_store.challenge_edge(
            edge["id"], "doubtful", 0.3, "CHALLENGED_BY", agent_name="metis"
        )
        assert challenge is not None
        assert challenge["created_by"] == "metis"

    def test_update_edge_confidence_owner_only(self, populated_store):
        edge = populated_store.execute_one(
            "SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'"
        )

        # Owner can update
        result = populated_store.update_edge_confidence(edge["id"], 0.96)
        assert abs(result["confidence"] - 0.96) < 0.001

        # Non-owner cannot update
        populated_store.agent_name = "socrates"
        with pytest.raises(PermissionError):
            populated_store.update_edge_confidence(edge["id"], 0.5)

    def test_update_edge_confidence_with_agent_name_override(self, populated_store):
        """OHM-y2i.19: update_edge_confidence should accept agent_name parameter."""
        edge = populated_store.execute_one(
            "SELECT * FROM ohm_edges WHERE edge_type = 'CAUSES'"
        )
        # Owner can update using agent_name parameter
        result = populated_store.update_edge_confidence(
            edge["id"], 0.97, agent_name="test_agent"
        )
        assert abs(result["confidence"] - 0.97) < 0.001

        # Non-owner cannot update even with agent_name parameter
        with pytest.raises(PermissionError):
            populated_store.update_edge_confidence(
                edge["id"], 0.5, agent_name="socrates"
            )

    def test_write_observation(self, populated_store):
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.85, baseline=0.5, sigma=3.5, source="research"
        )
        assert obs["node_id"] == "hungary"
        assert obs["type"] == "measurement"
        assert abs(obs["value"] - 0.85) < 0.001

    def test_write_observation_with_agent_name_override(self, populated_store):
        """OHM-y2i.19: write_observation should accept agent_name parameter."""
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.9, agent_name="metis"
        )
        assert obs["created_by"] == "metis"

    def test_agent_state(self, store):
        state = store.update_agent_state(
            current_focus="Testing agent state",
            active_patterns=["testing", "agents"],
            available_services=["research", "critique"],
        )
        assert state["agent_name"] == "test_agent"
        assert state["current_focus"] == "Testing agent state"

    def test_agent_state_with_agent_name_override(self, store):
        """OHM-y2i.19: update_agent_state should accept agent_name parameter."""
        state = store.update_agent_state(
            current_focus="Socrates focus",
            agent_name="socrates",
        )
        assert state["agent_name"] == "socrates"
        assert state["current_focus"] == "Socrates focus"

    def test_who_is_working_on(self, store):
        store.update_agent_state(
            current_focus="Researching AND→OR patterns in Hungary",
            active_patterns=["and-or", "hungary"],
        )
        results = store.who_is_working_on("hungary")
        assert len(results) > 0
        assert results[0]["agent_name"] == "test_agent"

    def test_status(self, populated_store):
        status = populated_store.status()
        assert status["node_count"] == 4
        assert status["edge_count"] == 3
        assert "L3" in status["edges_by_layer"]

    def test_boundary_rules_l3(self, populated_store):
        """L3 edges are agent-owned, challengeable."""
        # Create edge as metis
        populated_store.agent_name = "metis"
        populated_store.write_edge("and_or", "hungary", "EXPLAINS", "L3", confidence=0.8)

        # Try to update as socrates - should fail
        populated_store.agent_name = "socrates"
        edge = populated_store.execute_one(
            "SELECT * FROM ohm_edges WHERE edge_type = 'EXPLAINS' AND created_by = 'metis'"
        )
        with pytest.raises(PermissionError):
            populated_store.update_edge_confidence(edge["id"], 0.5)

        # But socrates CAN challenge
        challenge = populated_store.challenge_edge(edge["id"], "weak evidence", 0.5)
        assert challenge is not None


class TestGraphQueries:
    """Test recursive CTE queries."""

    def test_neighborhood_query(self, populated_store):
        results = query_neighborhood(populated_store.conn, "hungary", depth=2)
        assert len(results) > 0

    def test_path_query(self, populated_store):
        results = query_path(populated_store.conn, "and_or", "democratic_escape", max_depth=3)
        assert len(results) > 0

    def test_impact_query(self, populated_store):
        results = query_impact(populated_store.conn, "and_or", depth=3)
        assert len(results) > 0

    def test_neighborhood_depth_limit(self, populated_store):
        # Depth 3 should return at least as many results as depth 1
        results1 = query_neighborhood(populated_store.conn, "hungary", depth=1)
        results3 = query_neighborhood(populated_store.conn, "hungary", depth=3)
        assert len(results3) >= len(results1)
