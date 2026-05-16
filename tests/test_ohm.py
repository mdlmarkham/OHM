"""
OHM Tests — comprehensive test suite for the knowledge graph.
"""

import pytest
import tempfile
import os

from ohm.store import OhmStore
from ohm.graph import build_neighborhood_query, build_path_query, build_impact_query
from ohm.query import parse_query


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

    def test_write_observation(self, populated_store):
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.85, baseline=0.5, sigma=3.5, source="research"
        )
        assert obs["node_id"] == "hungary"
        assert obs["type"] == "measurement"
        assert abs(obs["value"] - 0.85) < 0.001

    def test_agent_state(self, store):
        state = store.update_agent_state(
            current_focus="Testing agent state",
            active_patterns=["testing", "agents"],
            available_services=["research", "critique"],
        )
        assert state["agent_name"] == "test_agent"
        assert state["current_focus"] == "Testing agent state"

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
        sql, params = build_neighborhood_query("hungary", depth=2)
        results = populated_store.execute(sql, params)
        assert len(results) > 0

    def test_path_query(self, populated_store):
        sql, params = build_path_query("and_or", "democratic_escape", max_depth=3)
        results = populated_store.execute(sql, params)
        assert len(results) > 0

    def test_impact_query(self, populated_store):
        sql, params = build_impact_query("and_or", depth=3)
        results = populated_store.execute(sql, params)
        assert len(results) > 0

    def test_neighborhood_depth_limit(self, populated_store):
        sql1, _ = build_neighborhood_query("hungary", depth=1)
        sql3, _ = build_neighborhood_query("hungary", depth=3)
        # Depth 3 should return at least as many results as depth 1
        results1 = populated_store.execute(sql1, ["hungary", "hungary", 1])
        results3 = populated_store.execute(sql3, ["hungary", "hungary", 3])
        assert len(results3) >= len(results1)


class TestQueryParser:
    """Test natural language query parsing."""

    def test_neighborhood_query(self):
        parsed = parse_query("what connects to hungary")
        assert parsed["type"] == "neighborhood"
        assert "hungary" in parsed["params"]

    def test_path_query(self):
        parsed = parse_query("path from hungary to democratic_escape")
        assert parsed["type"] == "path"

    def test_impact_query(self):
        parsed = parse_query("impact of and_or")
        assert parsed["type"] == "impact"

    def test_who_working(self):
        parsed = parse_query("who is working on hungary")
        assert parsed["type"] == "who_working"

    def test_unknown_fallback(self):
        parsed = parse_query("something completely random and long")
        assert parsed["type"] == "unknown"
