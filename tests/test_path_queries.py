"""Tests for OHM-809: generic path-based edge queries via node_path prefix."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return Graph(conn=conn, actor="test-agent")


def _set_path(conn, node_id, path):
    """Helper: set node_path on a node."""
    conn.execute("UPDATE ohm_nodes SET node_path = ? WHERE id = ?", [path, node_id])


class TestGetEdgesByPath:
    """Test Graph.get_edges_by_path() — path-based edge queries (OHM-809)."""

    def test_returns_edges_matching_from_prefix(self, graph):
        mill = graph.create_node(label="RCC Mill", node_type="equipment")
        silo = graph.create_node(label="RCC Silo", node_type="equipment")
        _set_path(graph._conn, mill["id"], "TA.MA.CEM.RCC.MILL_1")
        _set_path(graph._conn, silo["id"], "TA.MA.CEM.RCC.SILO_1")
        graph.create_edge(from_node=mill["id"], to_node=silo["id"], edge_type="CAUSES", layer="L3")

        edges = graph.get_edges_by_path("TA.MA.CEM.RCC", layer="L3")
        assert len(edges) >= 1
        assert any(e["from_path"] == "TA.MA.CEM.RCC.MILL_1" for e in edges)

    def test_filters_by_to_prefix(self, graph):
        source = graph.create_node(label="Source A", node_type="equipment")
        target_a = graph.create_node(label="Target A", node_type="equipment")
        target_b = graph.create_node(label="Target B", node_type="equipment")
        _set_path(graph._conn, source["id"], "TA.MA.CEM.RCC.MILL_1")
        _set_path(graph._conn, target_a["id"], "TA.MA.CEM.RCC.SILO_1")
        _set_path(graph._conn, target_b["id"], "TA.MA.CEM.PNS.SILO_2")
        graph.create_edge(from_node=source["id"], to_node=target_a["id"], edge_type="CAUSES", layer="L3")
        graph.create_edge(from_node=source["id"], to_node=target_b["id"], edge_type="CAUSES", layer="L3")

        edges = graph.get_edges_by_path("TA.MA.CEM", to_prefix="TA.MA.CEM.RCC", layer="L3")
        assert all(e["to_path"].startswith("TA.MA.CEM.RCC") for e in edges)
        assert not any(e["to_path"].startswith("TA.MA.CEM.PNS") for e in edges)

    def test_filters_by_edge_type(self, graph):
        x = graph.create_node(label="Node X", node_type="concept")
        y = graph.create_node(label="Node Y", node_type="concept")
        _set_path(graph._conn, x["id"], "PATH.A.X")
        _set_path(graph._conn, y["id"], "PATH.A.Y")
        graph.create_edge(from_node=x["id"], to_node=y["id"], edge_type="CAUSES", layer="L3")
        graph.create_edge(from_node=x["id"], to_node=y["id"], edge_type="SUPPORTS", layer="L3")

        causes = graph.get_edges_by_path("PATH.A", edge_type="CAUSES")
        assert all(e["edge_type"] == "CAUSES" for e in causes)
        supports = graph.get_edges_by_path("PATH.A", edge_type="SUPPORTS")
        assert all(e["edge_type"] == "SUPPORTS" for e in supports)

    def test_empty_prefix_returns_nothing(self, graph):
        edges = graph.get_edges_by_path("NONEXISTENT.PREFIX")
        assert edges == []

    def test_respects_limit(self, graph):
        for i in range(10):
            n = graph.create_node(label=f"Node {i}", node_type="concept")
            _set_path(graph._conn, n["id"], f"BATCH.A.NODE_{i}")
        nodes = graph._conn.execute("SELECT id FROM ohm_nodes WHERE node_path LIKE 'BATCH.A%' ORDER BY node_path").fetchall()
        for i in range(len(nodes) - 1):
            graph.create_edge(from_node=nodes[i][0], to_node=nodes[i + 1][0], edge_type="CAUSES", layer="L3")

        edges = graph.get_edges_by_path("BATCH.A", layer="L3", limit=3)
        assert len(edges) <= 3

    def test_excludes_deleted_edges(self, graph):
        alive = graph.create_node(label="Alive", node_type="concept")
        dead = graph.create_node(label="Dead", node_type="concept")
        _set_path(graph._conn, alive["id"], "TEST.A.ALIVE")
        _set_path(graph._conn, dead["id"], "TEST.A.DEAD")
        graph.create_edge(from_node=alive["id"], to_node=dead["id"], edge_type="CAUSES", layer="L3")

        graph._conn.execute(
            "UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND to_node = ?",
            [alive["id"], dead["id"]],
        )

        edges = graph.get_edges_by_path("TEST.A", layer="L3")
        assert all(e.get("deleted_at") is None for e in edges)
