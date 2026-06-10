"""Tests for the /islands endpoint (OHM-tr71.3)."""

import pytest
from ohm.graph.methods import find_islands


def _insert_node(conn, nid, label, ntype="concept", confidence=0.8, created_by="test"):
    """Helper to insert a node with all required fields."""
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, provenance, confidence, created_by, created_at) VALUES (?, ?, ?, 'test', ?, ?, CURRENT_TIMESTAMP)",
        [nid, label, ntype, confidence, created_by],
    )


def _insert_edge(conn, from_n, to_n, etype="SUPPORTS", layer="L3", confidence=0.9, created_by="test"):
    """Helper to insert an edge with all required fields."""
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [f"edge-{from_n}-{to_n}", from_n, to_n, etype, layer, confidence, created_by],
    )


class TestFindIslands:
    """Unit tests for find_islands method."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a test store with some connected and disconnected nodes."""
        from ohm.store import OhmStore
        import os

        db_path = os.path.join(str(tmp_path), "test_islands.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        # Create three clusters:
        # Mainland: a → b → c → d (4 connected nodes)
        # Island-1: e → f (2 connected nodes)
        # Island-2: g (1 orphan node)
        for nid, label in [
            ("main-a", "Main A"),
            ("main-b", "Main B"),
            ("main-c", "Main C"),
            ("main-d", "Main D"),
            ("isle-e", "Isle E"),
            ("isle-f", "Isle F"),
            ("orphan-g", "Orphan G"),
        ]:
            _insert_node(store.conn, nid, label)

        # Mainland edges: a→b, b→c, c→d
        for from_n, to_n, etype in [
            ("main-a", "main-b", "SUPPORTS"),
            ("main-b", "main-c", "CAUSES"),
            ("main-c", "main-d", "REFERENCES"),
            ("isle-e", "isle-f", "SUPPORTS"),
        ]:
            _insert_edge(store.conn, from_n, to_n, etype)

        return store

    def test_finds_mainland_and_islands(self, store, tmp_path):
        """find_islands returns the mainland and disconnected islands."""
        result = find_islands(store.conn, min_size=1)

        assert result["total_islands"] >= 3  # mainland + island + orphan
        assert result["largest_island_size"] == 4  # mainland has 4 nodes
        assert result["orphan_count"] >= 1  # orphan-g

    def test_min_size_filters_orphans(self, store, tmp_path):
        """min_size=2 excludes single-node orphans."""
        result = find_islands(store.conn, min_size=2)

        # Should find mainland (4) and isle (2) but NOT orphan
        assert result["islands_of_size_2_plus"] == 2
        assert all(isle["size"] >= 2 for isle in result["islands"])

    def test_coverage_fraction(self, store, tmp_path):
        """Coverage is the fraction of nodes in the mainland."""
        result = find_islands(store.conn, min_size=1)

        # 4 out of 7 nodes in the mainland ≈ 0.571
        assert 0.5 < result["coverage"] < 0.7

    def test_empty_graph(self, tmp_path):
        """find_islands on empty graph returns zeros."""
        import os
        from ohm.store import OhmStore

        db_path = os.path.join(str(tmp_path), "empty_islands.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        result = find_islands(store.conn)
        assert result["islands"] == []
        assert result["total_islands"] == 0

    def test_single_connected_graph(self, tmp_path):
        """When all nodes are connected, there's one island (the mainland)."""
        import os
        from ohm.store import OhmStore

        db_path = os.path.join(str(tmp_path), "single_island.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        # All 3 nodes connected
        for nid in ["n1", "n2", "n3"]:
            _insert_node(store.conn, nid, f"Node {nid}")
        _insert_edge(store.conn, "n1", "n2", "SUPPORTS")
        _insert_edge(store.conn, "n2", "n3", "CAUSES")

        result = find_islands(store.conn, min_size=1)
        assert result["total_islands"] == 1
        assert result["coverage"] == 1.0

    def test_island_metadata(self, store, tmp_path):
        """Each island has metadata: size, nodes, edges, confidence."""
        result = find_islands(store.conn, min_size=2)

        for island in result["islands"]:
            assert "id" in island
            assert "size" in island
            assert "total_nodes" in island
            assert "internal_edges" in island
            assert "nodes" in island
            assert isinstance(island["nodes"], list)

    def test_layer_filter(self, store, tmp_path):
        """Filtering by layer only considers edges in that layer."""
        # Add an L1 edge between the island and mainland
        _insert_edge(store.conn, "isle-e", "main-a", "REFERENCES", layer="L1")

        # L3-only: should still see islands
        result_l3 = find_islands(store.conn, layer="L3", min_size=2)
        assert result_l3["islands_of_size_2_plus"] >= 2

        # All layers: the L1 edge bridges them
        result_all = find_islands(store.conn, min_size=2)
        # Now isle-e and mainland are connected via L1
        # So there's one fewer island
        assert result_all["islands_of_size_2_plus"] <= result_l3["islands_of_size_2_plus"]
