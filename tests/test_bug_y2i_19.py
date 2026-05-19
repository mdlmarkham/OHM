"""Bug OHM-y2i.19: Server routes use ohmd as agent_name — boundary enforcement broken.

When the OHM daemon creates OhmStore(agent_name="ohmd"), all write operations
attribute edges/observations/state to "ohmd" regardless of which agent authenticated.
This means boundary enforcement (only owning agent can update L3/L4 edges) is
completely disabled in production — every edge belongs to "ohmd".
"""

import os
import pytest

from ohm.store import OhmStore


class TestAgentAttributionBug:
    """Verify that write operations attribute to the correct agent, not "ohmd".

    The bug: OhmStore.agent_name is set once at initialization. In the server,
    this is always "ohmd". When agent_a authenticates and creates an edge,
    it's stored as created_by="ohmd", not created_by="agent_a".
    """

    @pytest.fixture
    def shared_db(self, tmp_path):
        """Create a shared DuckDB file for multi-agent testing."""
        db_path = str(tmp_path / "test_ohm.duckdb")
        yield db_path
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_write_node_attributed_to_agent(self, shared_db):
        """Nodes should be attributed to the agent that created them, not 'ohmd'."""
        store = OhmStore(shared_db, agent_name="metis")
        node = store.write_node(id="test_node", label="concept", type="note", content="test")
        assert node["created_by"] == "metis", (
            f"Node created_by should be 'metis', got '{node['created_by']}'. "
            f"In production, this would be 'ohmd' instead of the authenticated agent."
        )
        store.close()

    def test_write_edge_attributed_to_agent(self, shared_db):
        """Edges should be attributed to the agent that created them."""
        store = OhmStore(shared_db, agent_name="metis")
        store.write_node(id="n1", label="concept", type="note")
        store.write_node(id="n2", label="concept", type="note")
        edge = store.write_edge(
            from_node="n1", to_node="n2",
            edge_type="CLAIMS", layer="L3", confidence=0.8,
        )
        assert edge["created_by"] == "metis", (
            f"Edge created_by should be 'metis', got '{edge['created_by']}'. "
            f"Boundary enforcement is meaningless if all edges belong to 'ohmd'."
        )
        store.close()

    def test_observation_attributed_to_agent(self, shared_db):
        """Observations should be attributed to the agent that created them."""
        store = OhmStore(shared_db, agent_name="clio")
        store.write_node(id="obs_node", label="metric", type="observation")
        obs = store.write_observation(
            node_id="obs_node", type="reading", value=42.0,
        )
        assert obs["created_by"] == "clio", (
            f"Observation created_by should be 'clio', got '{obs['created_by']}'. "
            f"Provenance is lost if all observations belong to 'ohmd'."
        )
        store.close()

    def test_agent_state_attributed_to_agent(self, shared_db):
        """Agent state should be recorded for the agent that set it."""
        store = OhmStore(shared_db, agent_name="socrates")
        state = store.update_agent_state(current_focus="challenging assumptions")
        assert state["agent_name"] == "socrates", (
            f"Agent state should belong to 'socrates', got '{state['agent_name']}'. "
            f"Agent state is broken if all state is recorded under 'ohmd'."
        )
        store.close()

    def test_boundary_enforcement_across_agents(self, shared_db):
        """Agent A should NOT be able to update Agent B's L3/L4 edges.

        This is the core boundary rule that's broken in production.
        With all edges owned by 'ohmd', any agent can modify any edge.
        """
        # Agent A creates an L3 edge
        store_a = OhmStore(shared_db, agent_name="agent_a")
        store_a.write_node(id="node1", label="concept", type="note")
        store_a.write_node(id="node2", label="concept", type="note")
        edge = store_a.write_edge(
            from_node="node1", to_node="node2",
            edge_type="CLAIMS", layer="L3", confidence=0.9,
        )
        assert edge["created_by"] == "agent_a"

        # Agent B should NOT be able to update Agent A's edge
        store_b = OhmStore(shared_db, agent_name="agent_b")
        with pytest.raises(Exception) as exc_info:
            store_b.update_edge_confidence(edge["id"], new_confidence=0.1)
        assert "PermissionDeniedError" in type(exc_info.value).__name__ or "cannot update" in str(exc_info.value).lower(), (
            "Agent B should NOT be able to update Agent A's edge, but no error was raised. "
            "Boundary enforcement is broken."
        )

        store_a.close()
        store_b.close()

    def test_boundary_enforcement_with_ohmd_agent(self, shared_db):
        """When server uses agent_name='ohmd', boundary enforcement is bypassed.

        This test demonstrates the production bug: if all edges are created_by='ohmd',
        then the boundary check (edge['created_by'] != self.agent_name) always passes
        for the same store, making boundary enforcement inert.
        """
        store = OhmStore(shared_db, agent_name="ohmd")

        # Create nodes and an edge as 'ohmd'
        store.write_node(id="x1", label="concept", type="note")
        store.write_node(id="x2", label="concept", type="note")
        edge = store.write_edge(
            from_node="x1", to_node="x2",
            edge_type="CLAIMS", layer="L3", confidence=0.9,
        )
        assert edge["created_by"] == "ohmd"

        # Since store.agent_name is also "ohmd", updating "own" edge is allowed
        # But in production, the ACTUAL agent might be "metis" — the boundary
        # check should be comparing against "metis", not "ohmd"
        # This update should fail if agent were "metis", but it succeeds because
        # the store thinks it's "ohmd"
        result = store.update_edge_confidence(edge["id"], new_confidence=0.5)
        assert result is not None, (
            "This should succeed (store agent == edge owner), but demonstrates "
            "the bug: in production, ANY authenticated agent's writes go through "
            "the same 'ohmd' store, bypassing boundary enforcement."
        )

        store.close()

    def test_challenge_edge_preserves_original(self, shared_db):
        """challenge_edge should create a NEW edge, not modify the original."""
        store = OhmStore(shared_db, agent_name="socrates")
        store.write_node(id="c1", label="concept", type="note")
        store.write_node(id="c2", label="concept", type="note")
        original = store.write_edge(
            from_node="c1", to_node="c2",
            edge_type="CLAIMS", layer="L3", confidence=0.9,
        )

        challenge = store.challenge_edge(
            original["id"], reason="I disagree", confidence=0.3,
        )
        assert challenge is not None
        assert challenge["id"] != original["id"], "Challenge must be a new edge"
        assert challenge["challenge_of"] == original["id"]

        # Original should be unchanged
        refreshed = store.get_edge(original["id"])
        assert abs(refreshed["confidence"] - 0.9) < 0.001, "Original edge confidence must not change"

        store.close()
