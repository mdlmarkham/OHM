"""Tests for node merge operation (OHM-g0kv.6)."""

import pytest
from ohm.store import OhmStore
from ohm.exceptions import NodeNotFoundError


class TestMergeNodes:
    """Tests for OhmStore.merge_nodes()."""

    def test_merge_repoints_edges(self, tmp_path):
        """Edges from merged node are repointed to keep node."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")
        store.write_node("target", "Target", "concept")

        store.write_edge(from_node="merge", to_node="target", edge_type="RELATES_TO", layer="L3", confidence=0.8)

        result = store.merge_nodes("keep", "merge", merged_by="test")
        assert result["edges_repointed"] == 1
        assert result["observations_repointed"] == 0

        edge = store.conn.execute("SELECT from_node FROM ohm_edges WHERE to_node = 'target' AND deleted_at IS NULL").fetchone()
        assert edge[0] == "keep"

        store.close()

    def test_merge_soft_deletes_merged_node(self, tmp_path):
        """Merged node is soft-deleted after merge."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")

        store.merge_nodes("keep", "merge", merged_by="test")

        merged = store.get_node("merge")
        assert merged is None  # Soft-deleted

        keep = store.get_node("keep")
        assert keep is not None
        assert keep["label"] == "Keep Node"

        store.close()

    def test_merge_repoints_observations(self, tmp_path):
        """Observations from merged node are repointed to keep node."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")

        store.write_observation(node_id="merge", type="test", value=1.0)

        result = store.merge_nodes("keep", "merge", merged_by="test")
        assert result["observations_repointed"] == 1

        obs = store.conn.execute("SELECT node_id FROM ohm_observations WHERE node_id = 'keep' AND deleted_at IS NULL").fetchone()
        assert obs is not None

        store.close()

    def test_merge_idempotent(self, tmp_path):
        """Merging an already-merged node raises NodeNotFoundError (node is soft-deleted)."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")

        r1 = store.merge_nodes("keep", "merge", merged_by="test")
        assert r1["edges_repointed"] == 0

        with pytest.raises(NodeNotFoundError):
            store.merge_nodes("keep", "merge", merged_by="test")

        store.close()

    def test_merge_nonexistent_keep_raises(self, tmp_path):
        """Merging into a non-existent keep node raises NodeNotFoundError."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("merge", "Merge Node", "concept")

        with pytest.raises(NodeNotFoundError):
            store.merge_nodes("keep", "merge", merged_by="test")

        store.close()

    def test_merge_nonexistent_merge_raises(self, tmp_path):
        """Merging a non-existent node raises NodeNotFoundError."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")

        with pytest.raises(NodeNotFoundError):
            store.merge_nodes("keep", "merge", merged_by="test")

        store.close()

    def test_merge_same_node_raises(self, tmp_path):
        """Merging a node into itself raises ValueError."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("same", "Same Node", "concept")

        with pytest.raises(ValueError, match="nothing to merge"):
            store.merge_nodes("same", "same", merged_by="test")

        store.close()

    def test_merge_skips_duplicate_edges(self, tmp_path):
        """Edges that already exist from keep node are not duplicated."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")
        store.write_node("target", "Target", "concept")

        store.write_edge(from_node="keep", to_node="target", edge_type="RELATES_TO", layer="L3")
        store.write_edge(from_node="merge", to_node="target", edge_type="RELATES_TO", layer="L3")

        result = store.merge_nodes("keep", "merge", merged_by="test")
        assert result["edges_repointed"] == 0

        edges = store.conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE from_node = 'keep' AND to_node = 'target' AND deleted_at IS NULL").fetchone()[0]
        assert edges == 1

        store.close()

    def test_merge_response_shape(self, tmp_path):
        """Merge response contains all expected fields."""
        db_path = str(tmp_path / "test.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        store.write_node("keep", "Keep Node", "concept")
        store.write_node("merge", "Merge Node", "concept")

        result = store.merge_nodes("keep", "merge", merged_by="test_agent")
        assert result["keep"] == "keep"
        assert result["merged"] == "merge"
        assert "edges_repointed" in result
        assert "observations_repointed" in result
        assert result["merged_by"] == "test_agent"

        store.close()
