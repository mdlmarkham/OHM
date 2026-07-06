"""Tests for OHM-ivlt: node_path / UNS address as first-class graph identifier."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import DEFAULT_SCHEMA, initialize_schema
from ohm.graph.queries import create_node, set_node_path, get_nodes_by_path_prefix
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, DEFAULT_SCHEMA)
    return Graph(conn, actor="test_agent")


@pytest.fixture
def nodes(graph):
    conn = graph._conn
    ids = []
    for nid, lbl in [
        ("plant_a", "Plant A"),
        ("unit_fm10", "Unit FM10"),
        ("pump_p101", "Pump P101"),
        ("pump_p102", "Pump P102"),
        ("compressor_c1", "Compressor C1"),
    ]:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'test')",
            [nid, lbl],
        )
        ids.append(nid)
    return ids


class TestSetNodePath:
    def test_set_path(self, graph, nodes):
        result = graph.set_node_path("pump_p101", "pns.fm10.pump_p101")
        assert result["node_path"] == "pns.fm10.pump_p101"

    def test_set_path_overwrite(self, graph, nodes):
        graph.set_node_path("pump_p101", "pns.fm10.p101")
        result = graph.set_node_path("pump_p101", "pns.fm20.p101")
        assert result["node_path"] == "pns.fm20.p101"

    def test_set_path_nonexistent_node_raises(self, graph):
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            graph.set_node_path("nonexistent", "pns.x")

    def test_set_path_empty_raises(self, graph, nodes):
        with pytest.raises(ValueError, match="non-empty"):
            graph.set_node_path("pump_p101", "")


class TestGetNodesByPathPrefix:
    def test_exact_prefix_match(self, graph, nodes):
        graph.set_node_path("plant_a", "pns")
        graph.set_node_path("unit_fm10", "pns.fm10")
        graph.set_node_path("pump_p101", "pns.fm10.p101")
        graph.set_node_path("pump_p102", "pns.fm10.p102")
        graph.set_node_path("compressor_c1", "pns.fm20.c1")

        results = graph.get_nodes_by_path_prefix("pns.fm10")
        ids = {r["id"] for r in results}
        assert ids == {"unit_fm10", "pump_p101", "pump_p102"}

    def test_top_level_prefix(self, graph, nodes):
        graph.set_node_path("plant_a", "pns")
        graph.set_node_path("unit_fm10", "pns.fm10")
        graph.set_node_path("compressor_c1", "other.c1")

        results = graph.get_nodes_by_path_prefix("pns")
        ids = {r["id"] for r in results}
        assert ids == {"plant_a", "unit_fm10"}

    def test_no_matches(self, graph, nodes):
        graph.set_node_path("pump_p101", "pns.fm10.p101")
        results = graph.get_nodes_by_path_prefix("xyz")
        assert results == []

    def test_empty_prefix_raises(self, graph):
        with pytest.raises(ValueError, match="non-empty"):
            graph.get_nodes_by_path_prefix("")

    def test_results_ordered_by_path(self, graph, nodes):
        graph.set_node_path("pump_p102", "pns.fm10.p102")
        graph.set_node_path("pump_p101", "pns.fm10.p101")
        graph.set_node_path("unit_fm10", "pns.fm10")

        results = graph.get_nodes_by_path_prefix("pns.fm10")
        paths = [r["node_path"] for r in results]
        assert paths == sorted(paths)

    def test_limit_respected(self, graph, nodes):
        for nid in nodes:
            graph.set_node_path(nid, f"pns.{nid}")
        results = graph.get_nodes_by_path_prefix("pns", limit=2)
        assert len(results) == 2


class TestNodePathSchema:
    def test_node_path_column_exists(self, graph):
        conn = graph._conn
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_nodes'"
        ).fetchall()}
        assert "node_path" in cols

    def test_node_path_index_exists(self, graph):
        conn = graph._conn
        row = conn.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() "
            "WHERE table_name = 'ohm_nodes' AND index_name = 'idx_nodes_path'"
        ).fetchone()
        assert row[0] >= 1

    def test_schema_version_bumped(self, graph):
        from ohm.graph.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION == "0.44.0"