"""Tests for ohm_list_edges MCP tool and query_edges function (OHM-972).

Covers:
- query_edges() function with all filter combinations
- ohm_list_edges MCP dispatch mapping
- HTTP GET /edges endpoint (also covered in test_server.py)
"""

from __future__ import annotations

import pytest

from ohm.graph.queries import create_edge, create_node, query_edges


@pytest.fixture
def populated_db(test_db):
    """Create a small graph with nodes and edges for query_edges tests.

    Returns (conn, node_ids) where node_ids is a dict with keys 'a', 'b', 'c'.
    """
    node_a = create_node(test_db, label="Node A", node_type="concept", created_by="test")
    node_b = create_node(test_db, label="Node B", node_type="concept", created_by="test")
    node_c = create_node(test_db, label="Node C", node_type="concept", created_by="test")
    create_edge(test_db, from_node=node_a["id"], to_node=node_b["id"], edge_type="CAUSES", layer="L3", created_by="test")
    create_edge(test_db, from_node=node_b["id"], to_node=node_c["id"], edge_type="SUPPORTS", layer="L3", created_by="test")
    create_edge(test_db, from_node=node_a["id"], to_node=node_c["id"], edge_type="CONTAINS", layer="L1", created_by="test")
    return test_db, {"a": node_a["id"], "b": node_b["id"], "c": node_c["id"]}


class TestQueryEdges:
    """Tests for the query_edges() function."""

    def test_query_edges_empty(self, test_db):
        result = query_edges(test_db)
        assert result == []

    def test_query_edges_returns_all(self, populated_db):
        conn, _ = populated_db
        result = query_edges(conn)
        assert len(result) == 3
        for edge in result:
            assert "id" in edge
            assert "from_node" in edge
            assert "to_node" in edge
            assert "edge_type" in edge
            assert "layer" in edge
            assert "confidence" in edge
            assert "probability" in edge
            assert "created_by" in edge
            assert "created_at" in edge
            assert "deleted_at" in edge

    def test_query_edges_filter_by_from_node(self, populated_db):
        conn, ids = populated_db
        result = query_edges(conn, from_node=ids["a"])
        assert len(result) == 2
        assert all(e["from_node"] == ids["a"] for e in result)

    def test_query_edges_filter_by_to_node(self, populated_db):
        conn, ids = populated_db
        result = query_edges(conn, to_node=ids["c"])
        assert len(result) == 2
        assert all(e["to_node"] == ids["c"] for e in result)

    def test_query_edges_filter_by_edge_type_string(self, populated_db):
        conn, _ = populated_db
        result = query_edges(conn, edge_type="CAUSES")
        assert len(result) == 1
        assert result[0]["edge_type"] == "CAUSES"

    def test_query_edges_filter_by_edge_type_list(self, populated_db):
        conn, _ = populated_db
        result = query_edges(conn, edge_type=["CAUSES", "SUPPORTS"])
        assert len(result) == 2
        assert {e["edge_type"] for e in result} == {"CAUSES", "SUPPORTS"}

    def test_query_edges_filter_by_layer(self, populated_db):
        conn, _ = populated_db
        result = query_edges(conn, layer="L1")
        assert len(result) == 1
        assert result[0]["layer"] == "L1"

    def test_query_edges_filter_by_created_by(self, populated_db):
        conn, _ = populated_db
        result = query_edges(conn, created_by="test")
        assert len(result) == 3

    def test_query_edges_pagination(self, populated_db):
        conn, _ = populated_db
        page1 = query_edges(conn, limit=2, offset=0)
        assert len(page1) == 2
        page2 = query_edges(conn, limit=2, offset=2)
        assert len(page2) == 1
        ids1 = {e["id"] for e in page1}
        ids2 = {e["id"] for e in page2}
        assert ids1.isdisjoint(ids2)

    def test_query_edges_excludes_soft_deleted(self, populated_db):
        from ohm.graph.queries import delete_edge

        conn, _ = populated_db
        edges = query_edges(conn, edge_type="CAUSES")
        assert len(edges) == 1
        delete_edge(conn, edge_id=edges[0]["id"], deleted_by="test")
        result = query_edges(conn, edge_type="CAUSES")
        assert len(result) == 0

    def test_query_edges_include_deleted(self, populated_db):
        from ohm.graph.queries import delete_edge

        conn, _ = populated_db
        edges = query_edges(conn, edge_type="CAUSES")
        delete_edge(conn, edge_id=edges[0]["id"], deleted_by="test")
        result = query_edges(conn, edge_type="CAUSES", include_deleted=True)
        assert len(result) == 1
        assert result[0]["deleted_at"] is not None

    def test_query_edges_combined_filters(self, populated_db):
        conn, ids = populated_db
        result = query_edges(
            conn,
            from_node=ids["a"],
            layer="L3",
        )
        assert len(result) == 1
        assert result[0]["from_node"] == ids["a"]
        assert result[0]["layer"] == "L3"

    def test_query_edges_rejects_invalid_identifier(self, populated_db):
        conn, _ = populated_db
        with pytest.raises(ValueError):
            query_edges(conn, from_node="bad'; DROP TABLE--")


class TestMCPDispatch:
    """Tests for the ohm_list_edges MCP dispatch mapping."""

    def test_dispatch_empty_args(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request("ohm_list_edges", {}, "test-agent")
        assert method == "GET"
        assert path.startswith("/edges?")
        assert "limit=100" in path
        assert "offset=0" in path
        assert body is None

    def test_dispatch_with_filters(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request(
            "ohm_list_edges",
            {
                "from_node": "node_a",
                "to_node": "node_b",
                "edge_type": "CAUSES",
                "layer": "L3",
                "created_by": "metis",
                "limit": 50,
                "offset": 10,
            },
            "test-agent",
        )
        assert method == "GET"
        assert path.startswith("/edges?")
        assert "from_node=node_a" in path
        assert "to_node=node_b" in path
        assert "edge_type=CAUSES" in path
        assert "layer=L3" in path
        assert "created_by=metis" in path
        assert "limit=50" in path
        assert "offset=10" in path
        assert body is None

    def test_dispatch_partial_filters(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request(
            "ohm_list_edges",
            {"from_node": "node_a"},
            "test-agent",
        )
        assert method == "GET"
        assert "from_node=node_a" in path
        assert "limit=100" in path
        assert "offset=0" in path
        assert body is None
