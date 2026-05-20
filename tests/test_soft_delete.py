"""Tests for soft-delete functionality (OHM-cpi).

Verifies that DELETE operations mark rows as deleted rather than removing them,
preventing the DuckDB index corruption bug when DuckLake mirror tables are attached.
"""

import pytest

from ohm.store import OhmStore


@pytest.fixture
def store():
    """Create a store with initialized schema using in-memory DB."""
    s = OhmStore(db_path=":memory:", agent_name="test_agent")
    yield s
    s.conn.close()


class TestSoftDeleteNode:
    """Test soft-deleting nodes."""

    def test_soft_delete_marks_node(self, store):
        """Deleting a node sets deleted_at instead of removing it."""
        # Create a node
        node = store.write_node(
            id="test_node_1", label="Test Node", type="concept",
            content="test content", confidence=0.8, provenance="test"
        )
        assert node["id"] == "test_node_1"
        assert node["created"] is True

        # Node should be findable
        found = store.get_node("test_node_1")
        assert found is not None
        assert found["label"] == "Test Node"

        # Delete the node
        result = store.delete_node("test_node_1", deleted_by="test_agent")
        assert result["deleted"] == "test_node_1"
        assert result["soft_delete"] is True

        # Node should NOT be findable via get_node (filtered by deleted_at IS NULL)
        found = store.get_node("test_node_1")
        assert found is None

        # But the row should still exist in the database
        row = store.conn.execute(
            "SELECT id, deleted_at FROM ohm_nodes WHERE id = ?", ["test_node_1"]
        ).fetchone()
        assert row is not None
        assert row[0] == "test_node_1"
        assert row[1] is not None  # deleted_at is set

    def test_soft_delete_with_edges(self, store):
        """Deleting a node soft-deletes its edges too."""
        # Create nodes and edge
        store.write_node(id="node_a", label="Node A", type="concept",
                        content="a", confidence=0.8, provenance="test")
        store.write_node(id="node_b", label="Node B", type="concept",
                        content="b", confidence=0.8, provenance="test")
        edge = store.write_edge(
            from_node="node_a", to_node="node_b", edge_type="RELATED_TO",
            layer="L2", confidence=0.7, provenance="test"
        )
        assert edge["id"] is not None

        # Delete node_a
        result = store.delete_node("node_a", deleted_by="test_agent")
        assert result["edges_removed"] >= 1

        # Node should not be findable
        assert store.get_node("node_a") is None

        # Edge should not be findable
        assert store.get_edge(edge["id"]) is None

    def test_soft_delete_not_found(self, store):
        """Deleting a non-existent node raises NodeNotFoundError."""
        from ohm.exceptions import NodeNotFoundError
        with pytest.raises(NodeNotFoundError):
            store.delete_node("nonexistent_node", deleted_by="test_agent")

    def test_double_delete_idempotent(self, store):
        """Deleting an already-deleted node raises NodeNotFoundError (filtered by deleted_at)."""
        from ohm.exceptions import NodeNotFoundError
        store.write_node(id="del_twice", label="Delete Twice", type="concept",
                        content="test", confidence=0.5, provenance="test")
        store.delete_node("del_twice", deleted_by="test_agent")

        # Second delete should raise NodeNotFoundError (already soft-deleted)
        with pytest.raises(NodeNotFoundError):
            store.delete_node("del_twice", deleted_by="test_agent")

    def test_stats_excludes_soft_deleted(self, store):
        """Stats should not count soft-deleted nodes."""
        store.write_node(id="stats_node", label="Stats Node", type="concept",
                        content="test", confidence=0.5, provenance="test")
        # Count nodes before delete
        count_before = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL"
        ).fetchone()[0]

        store.delete_node("stats_node", deleted_by="test_agent")
        count_after = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL"
        ).fetchone()[0]
        assert count_after == count_before - 1


class TestSoftDeleteEdge:
    """Test soft-deleting edges."""

    def test_soft_delete_marks_edge(self, store):
        """Deleting an edge sets deleted_at instead of removing it."""
        store.write_node(id="edge_node_a", label="A", type="concept",
                        content="a", confidence=0.8, provenance="test")
        store.write_node(id="edge_node_b", label="B", type="concept",
                        content="b", confidence=0.8, provenance="test")
        edge = store.write_edge(
            from_node="edge_node_a", to_node="edge_node_b", edge_type="RELATED_TO",
            layer="L2", confidence=0.7, provenance="test"
        )
        edge_id = edge["id"]

        # Delete the edge
        result = store.delete_edge(edge_id, deleted_by="test_agent")
        assert result["deleted"] == edge_id
        assert result["soft_delete"] is True

        # Edge should not be findable
        assert store.get_edge(edge_id) is None

        # But row still exists in database
        row = store.conn.execute(
            "SELECT id, deleted_at FROM ohm_edges WHERE id = ?", [edge_id]
        ).fetchone()
        assert row is not None
        assert row[0] == edge_id
        assert row[1] is not None

    def test_soft_delete_edge_not_found(self, store):
        """Deleting a non-existent edge raises EdgeNotFoundError."""
        from ohm.exceptions import EdgeNotFoundError
        with pytest.raises(EdgeNotFoundError):
            store.delete_edge("nonexistent_edge_id", deleted_by="test_agent")