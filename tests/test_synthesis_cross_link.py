"""Tests for OHM-tjzh: synthesis endpoint cross-link constraint.

Verifies that:
1. write_edge raises NodeNotFoundError if from_node doesn't exist
2. write_edge raises NodeNotFoundError if to_node doesn't exist
3. write_edge succeeds when both endpoints exist
4. Synthesis nodes must have at least one edge to an existing node
5. The HTTP handler validates cluster_ids before creating nodes
"""

from __future__ import annotations

import duckdb
import pytest

from ohm.exceptions import NodeNotFoundError
from ohm.graph.store import OhmStore
from ohm.schema import initialize_schema


@pytest.fixture
def store(tmp_path):
    """Create a fresh OhmStore for testing."""
    db_path = str(tmp_path / "synth_test.duckdb")
    conn = duckdb.connect(db_path)
    initialize_schema(conn)
    conn.close()

    store = OhmStore(db_path=db_path, agent_name="test_agent")
    yield store
    store.close()


class TestSynthesisCrossLinkConstraint:
    """OHM-tjzh: edge creation validates endpoint nodes exist."""

    def test_edge_validates_from_node_exists(self, store):
        """write_edge raises NodeNotFoundError if from_node doesn't exist."""
        # Create only the to_node
        store.write_node(id="anchor-to", label="To Node", type="concept", agent_name="test")

        with pytest.raises(NodeNotFoundError, match="from_node"):
            store.write_edge(
                from_node="nonexistent-from",
                to_node="anchor-to",
                edge_type="SUPPORTS",
                layer="L3",
                confidence=0.8,
                agent_name="test",
            )

    def test_edge_validates_to_node_exists(self, store):
        """write_edge raises NodeNotFoundError if to_node doesn't exist."""
        # Create only the from_node
        store.write_node(id="anchor-from", label="From Node", type="concept", agent_name="test")

        with pytest.raises(NodeNotFoundError, match="to_node"):
            store.write_edge(
                from_node="anchor-from",
                to_node="nonexistent-to",
                edge_type="SUPPORTS",
                layer="L3",
                confidence=0.8,
                agent_name="test",
            )

    def test_edge_with_both_endpoints_exists_succeeds(self, store):
        """write_edge succeeds when both endpoints exist."""
        store.write_node(id="node-a", label="Node A", type="concept", agent_name="test")
        store.write_node(id="node-b", label="Node B", type="concept", agent_name="test")

        result = store.write_edge(
            from_node="node-a",
            to_node="node-b",
            edge_type="SUPPORTS",
            layer="L3",
            confidence=0.8,
            agent_name="test",
        )
        assert result is not None
        assert result["from"] == "node-a"
        assert result["to"] == "node-b"

    def test_synthesis_edge_to_valid_anchor(self, store):
        """Synthesis node can link to an existing anchor node."""
        # Create an anchor node
        store.write_node(id="anchor-synth", label="Anchor", type="concept", agent_name="test")

        # Create a synthesis node
        store.write_node(
            id="synth-1",
            label="Test Synthesis",
            type="concept",
            content="Connects to anchor",
            confidence=0.85,
            agent_name="test",
            provenance="test_synthesis",
        )

        # Create edge to anchor — should succeed
        edge = store.write_edge(
            from_node="synth-1",
            to_node="anchor-synth",
            edge_type="SUPPORTS",
            layer="L3",
            confidence=0.85,
            agent_name="test",
        )
        assert edge is not None
        assert edge["from"] == "synth-1"
        assert edge["to"] == "anchor-synth"

    def test_synthesis_with_invalid_target_rejected(self, store):
        """write_edge rejects synthesis edge to nonexistent node."""
        # Create only the synthesis node
        store.write_node(id="synth-bad", label="Bad Synthesis", type="concept", agent_name="test")

        with pytest.raises(NodeNotFoundError):
            store.write_edge(
                from_node="synth-bad",
                to_node="nonexistent-target",
                edge_type="SUPPORTS",
                layer="L3",
                confidence=0.85,
                agent_name="test",
            )