"""Tests for the OHM Python SDK (Graph class)."""

import pytest

from ohm.sdk import connect


@pytest.fixture
def graph():
    """Create an in-memory Graph for testing."""
    g = connect(":memory:", actor="test_agent")
    yield g
    g.close()


class TestGraphWrite:
    """Tests for SDK write operations."""

    def test_create_node(self, graph):
        node_id = graph.create_node(label="Test Node")
        assert node_id.startswith("test_node_")

    def test_create_node_with_type(self, graph):
        node_id = graph.create_node(label="Source A", node_type="source")
        assert node_id

    def test_create_edge(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        edge_id = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        assert edge_id

    def test_challenge(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        edge_id = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        challenge_id = graph.challenge(edge_id, reason="weak evidence", confidence=0.3)
        assert challenge_id

    def test_support(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        edge_id = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        support_id = graph.support(edge_id, reason="additional evidence", confidence=0.8)
        assert support_id

    def test_update_edge(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        edge_id = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3", confidence=0.5)
        graph.update_edge(edge_id, confidence=0.95)
        result = graph.confidence(edge_id)
        assert result["original"]["confidence"] == pytest.approx(0.95)

    def test_update_edge_permission_denied(self, graph, tmp_path):
        """Non-owner cannot update another agent's edge."""
        # Use a shared file DB so both connections see the same data
        db_path = str(tmp_path / "shared.duckdb")
        g1 = connect(db_path, actor="owner")
        a2 = g1.create_node(label="A2")
        b2 = g1.create_node(label="B2")
        e2 = g1.create_edge(from_node=a2, to_node=b2, edge_type="CAUSES", layer="L3")
        g1.close()

        g2 = connect(db_path, actor="other_agent")
        from ohm.exceptions import PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            g2.update_edge(e2, confidence=0.5)
        g2.close()

    def test_observe(self, graph):
        a = graph.create_node(label="A")
        obs_id = graph.observe(a, obs_type="measurement", value=1.5, sigma=0.3)
        assert obs_id

    def test_set_focus(self, graph):
        graph.set_focus("researching patterns")
        state = graph.agent_state("test_agent")
        assert len(state) >= 1


class TestGraphRead:
    """Tests for SDK read operations."""

    def test_neighborhood(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.neighborhood(a, depth=2)
        assert len(results) >= 1

    def test_path(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.path(a, b)
        assert len(results) >= 1

    def test_impact(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        results = graph.impact(a, depth=5)
        assert len(results) >= 1

    def test_confidence(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        edge_id = graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        result = graph.confidence(edge_id)
        assert result["original"] is not None

    def test_listen(self, graph):
        results = graph.listen()
        assert isinstance(results, list)

    def test_agent_state(self, graph):
        graph.set_focus("testing")
        results = graph.agent_state()
        assert isinstance(results, list)

    def test_stats(self, graph):
        a = graph.create_node(label="A")
        b = graph.create_node(label="B")
        graph.create_edge(from_node=a, to_node=b, edge_type="CAUSES", layer="L3")
        stats = graph.stats()
        assert stats["total_nodes"] >= 2
        assert stats["total_edges"] >= 1


class TestGraphContextManager:
    """Tests for context manager protocol."""

    def test_context_manager(self):
        with connect(":memory:", actor="ctx_test") as g:
            node_id = g.create_node(label="Context Test")
            assert node_id
        # Connection should be closed after exiting context


class TestConnect:
    """Tests for the connect() factory function."""

    def test_connect_defaults(self):
        g = connect()
        assert g.actor == "unknown"
        g.close()

    def test_connect_with_actor(self):
        g = connect(actor="metis")
        assert g.actor == "metis"
        g.close()
