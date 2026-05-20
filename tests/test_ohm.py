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

    def test_write_observation_with_notes(self, populated_store):
        """OHM-of8: write_observation should persist notes field."""
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.9, notes="Unusual pattern detected"
        )
        assert obs["notes"] == "Unusual pattern detected"

    def test_write_observation_without_notes(self, populated_store):
        """OHM-of8: write_observation without notes should have notes=None."""
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.7
        )
        assert obs.get("notes") is None

    def test_write_observation_with_source_attribution(self, populated_store):
        """OHM-lmr: write_observation should persist source_name and source_url."""
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.88,
            source_name="Reuters", source_url="https://reuters.com/article/123",
            notes="Initial report"
        )
        assert obs["source_name"] == "Reuters"
        assert obs["source_url"] == "https://reuters.com/article/123"
        assert obs["notes"] == "Initial report"

    def test_write_observation_without_source_attribution(self, populated_store):
        """OHM-lmr: write_observation without source attribution should have None fields."""
        obs = populated_store.write_observation(
            "hungary", "measurement", value=0.75
        )
        assert obs.get("source_name") is None
        assert obs.get("source_url") is None

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

    def test_change_feed_attributed_to_caller_write_node(self, store):
        """OHM-qyn: write_node change feed should use caller agent_name, not store default."""
        store.write_node("cf-test-1", "CF Test", "concept", agent_name="metis")
        feed = store.execute(
            "SELECT agent_name FROM ohm_change_feed WHERE row_id = ?",
            ["cf-test-1"],
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "metis"

    def test_change_feed_attributed_to_caller_write_edge(self, store):
        """OHM-qyn: write_edge change feed should use caller agent_name, not store default."""
        store.write_node("cf-from", "From", "concept")
        store.write_node("cf-to", "To", "concept")
        store.write_edge("cf-from", "cf-to", "RELATES_TO", "L2", agent_name="clio")
        feed = store.execute(
            "SELECT agent_name FROM ohm_change_feed WHERE table_name = 'ohm_edges' AND agent_name = 'clio'",
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "clio"

    def test_change_feed_attributed_to_caller_write_observation(self, store):
        """OHM-qyn: write_observation change feed should use caller agent_name."""
        store.write_node("cf-obs-target", "Obs Target", "concept")
        store.write_observation("cf-obs-target", "measurement", value=1.0, agent_name="hephaestus")
        feed = store.execute(
            "SELECT agent_name FROM ohm_change_feed WHERE table_name = 'ohm_observations' AND agent_name = 'hephaestus'",
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "hephaestus"

    def test_change_log_attributed_to_caller(self, store):
        """OHM-qyn: ohm_change_log should also use caller agent_name."""
        store.write_node("cf-log-test", "Log Test", "concept", agent_name="socrates")
        log = store.execute(
            "SELECT agent_name FROM ohm_change_log WHERE row_id = ?",
            ["cf-log-test"],
        )
        assert len(log) >= 1
        assert log[0]["agent_name"] == "socrates"

    def test_change_feed_uses_default_when_no_override(self, store):
        """When no agent_name override, change feed should use store's default agent."""
        store.write_node("cf-default", "Default Agent", "concept")
        feed = store.execute(
            "SELECT agent_name FROM ohm_change_feed WHERE row_id = ?",
            ["cf-default"],
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "test_agent"

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


class TestDeleteNodeStore:
    """Tests for OhmStore.delete_node() — cascading edge deletion (OHM-cpi)."""

    def test_delete_node_removes_edges(self, store):
        """delete_node removes all edges referencing the node."""
        store.write_node("del-a", "Node A", "concept")
        store.write_node("del-b", "Node B", "concept")
        store.write_edge("del-a", "del-b", "CAUSES", "L3", confidence=0.8)

        result = store.delete_node("del-a", deleted_by="test_agent")
        assert result["deleted"] == "del-a"
        assert result["type"] == "node"
        assert result["edges_removed"] >= 1

        # Node should be gone
        assert store.get_node("del-a") is None

    def test_delete_node_removes_incoming_edges(self, store):
        """delete_node removes edges where node is the target."""
        store.write_node("src-a", "Source", "concept")
        store.write_node("tgt-b", "Target", "concept")
        store.write_edge("src-a", "tgt-b", "CAUSES", "L3", confidence=0.8)

        result = store.delete_node("tgt-b", deleted_by="test_agent")
        assert result["edges_removed"] >= 1

    def test_delete_node_not_found(self, store):
        """delete_node raises NodeNotFoundError for nonexistent node."""
        from ohm.exceptions import NodeNotFoundError
        with pytest.raises(NodeNotFoundError):
            store.delete_node("nonexistent_xyz", deleted_by="test_agent")

    def test_delete_node_no_edges(self, store):
        """delete_node works on a node with no edges."""
        store.write_node("lonely", "Lonely", "concept")
        result = store.delete_node("lonely", deleted_by="test_agent")
        assert result["edges_removed"] == 0
        assert result["observations_removed"] == 0


class TestDeleteEdgeStore:
    """Tests for OhmStore.delete_edge() (OHM-cpi)."""

    def test_delete_edge(self, store):
        """delete_edge removes an edge by ID."""
        store.write_node("e-a", "A", "concept")
        store.write_node("e-b", "B", "concept")
        edge = store.write_edge("e-a", "e-b", "CAUSES", "L3", confidence=0.8)

        result = store.delete_edge(edge["id"], deleted_by="test_agent")
        assert result["deleted"] == edge["id"]
        assert result["type"] == "edge"

        # Edge should be gone
        assert store.get_edge(edge["id"]) is None

    def test_delete_edge_not_found(self, store):
        """delete_edge raises EdgeNotFoundError for nonexistent edge."""
        from ohm.exceptions import EdgeNotFoundError
        with pytest.raises(EdgeNotFoundError):
            store.delete_edge("nonexistent_edge_xyz", deleted_by="test_agent")


class TestEdgeDeduplication:
    """Tests for edge deduplication (OHM-b5c)."""

    def test_write_edge_deduplicates_by_default(self, store):
        """write_edge should update existing edge instead of creating duplicate."""
        store.write_node("dedup-a", "A", "concept")
        store.write_node("dedup-b", "B", "concept")

        # Create first edge
        edge1 = store.write_edge("dedup-a", "dedup-b", "CAUSES", "L3", confidence=0.5)
        assert edge1 is not None

        # Create duplicate edge — should update, not create new
        edge2 = store.write_edge("dedup-a", "dedup-b", "CAUSES", "L3", confidence=0.9)
        assert edge2 is not None
        assert abs(edge2["confidence"] - 0.9) < 0.01

        # Should only have one edge
        count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-a' "
            "AND to_node = 'dedup-b' AND edge_type = 'CAUSES' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count == 1

    def test_write_edge_allows_duplicate_when_disabled(self, store):
        """write_edge with deduplicate=False should create a second edge."""
        store.write_node("dedup-c", "C", "concept")
        store.write_node("dedup-d", "D", "concept")

        # Create first edge
        store.write_edge("dedup-c", "dedup-d", "CAUSES", "L3", confidence=0.5)

        # Create second edge with deduplicate=False
        store.write_edge("dedup-c", "dedup-d", "CAUSES", "L3", confidence=0.9, deduplicate=False)

        # Should have two edges
        count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-c' "
            "AND to_node = 'dedup-d' AND edge_type = 'CAUSES' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count == 2

    def test_write_edge_different_types_not_deduplicated(self, store):
        """Edges with different edge_type should not be deduplicated."""
        store.write_node("dedup-e", "E", "concept")
        store.write_node("dedup-f", "F", "concept")

        store.write_edge("dedup-e", "dedup-f", "CAUSES", "L3", confidence=0.8)
        store.write_edge("dedup-e", "dedup-f", "DEPENDS_ON", "L4", confidence=0.7)

        count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-e' "
            "AND to_node = 'dedup-f' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count == 2

    def test_deduplicate_edges_removes_duplicates(self, store):
        """deduplicate_edges should remove duplicate edges, keeping most recent."""
        store.write_node("dedup-g", "G", "concept")
        store.write_node("dedup-h", "H", "concept")

        # Create duplicate edges (with deduplicate=False)
        store.write_edge("dedup-g", "dedup-h", "CAUSES", "L3", confidence=0.5, deduplicate=False)
        store.write_edge("dedup-g", "dedup-h", "CAUSES", "L3", confidence=0.9, deduplicate=False)

        # Should have 2 edges
        count_before = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-g' "
            "AND to_node = 'dedup-h' AND edge_type = 'CAUSES' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count_before == 2

        # Deduplicate
        removed = store.deduplicate_edges()
        assert removed >= 1

        # Should have 1 edge remaining
        count_after = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-g' "
            "AND to_node = 'dedup-h' AND edge_type = 'CAUSES' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert count_after == 1

    def test_deduplicate_edges_with_layer_filter(self, store):
        """deduplicate_edges with layer filter should only affect that layer."""
        store.write_node("dedup-i", "I", "concept")
        store.write_node("dedup-j", "J", "concept")

        # Create duplicates in L3
        store.write_edge("dedup-i", "dedup-j", "CAUSES", "L3", confidence=0.5, deduplicate=False)
        store.write_edge("dedup-i", "dedup-j", "CAUSES", "L3", confidence=0.9, deduplicate=False)

        # Create duplicates in L4
        store.write_edge("dedup-i", "dedup-j", "DEPENDS_ON", "L4", confidence=0.5, deduplicate=False)
        store.write_edge("dedup-i", "dedup-j", "DEPENDS_ON", "L4", confidence=0.7, deduplicate=False)

        # Deduplicate only L3
        removed = store.deduplicate_edges(layer="L3")
        assert removed >= 1

        # L4 should still have duplicates
        l4_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'dedup-i' "
            "AND to_node = 'dedup-j' AND edge_type = 'DEPENDS_ON' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert l4_count == 2
