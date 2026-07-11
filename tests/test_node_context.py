"""Tests for OHM-807: Generic node_context() envelope."""

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


class TestNodeContext:
    """Test Graph.node_context() — enriched context envelope (OHM-807)."""

    def test_returns_node_not_found_for_missing(self, graph):
        result = graph.node_context("nonexistent")
        assert "error" in result
        assert result["error"] == "node_not_found"

    def test_returns_node_metadata(self, graph):
        node = graph.create_node(label="Test Equipment", node_type="concept")
        ctx = graph.node_context(node["id"])
        assert ctx["node"]["id"] == node["id"]
        assert ctx["node"]["label"] == "Test Equipment"

    def test_includes_neighborhood(self, graph):
        a = graph.create_node(label="A", node_type="concept")
        b = graph.create_node(label="B", node_type="concept")
        graph.create_edge(from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3")
        ctx = graph.node_context(a["id"])
        assert "neighborhood" in ctx
        assert isinstance(ctx["neighborhood"], list)

    def test_includes_observations(self, graph):
        node = graph.create_node(label="Observed", node_type="concept")
        graph.observe(node["id"], obs_type="measurement", value=0.5)
        ctx = graph.node_context(node["id"])
        assert "observations" in ctx
        assert len(ctx["observations"]) >= 1

    def test_includes_signals(self, graph):
        from ohm.graph.queries import create_external_signal

        node = graph.create_node(label="With Signal", node_type="concept")
        create_external_signal(
            graph._conn,
            node_id=node["id"],
            source_type="opc_ua",
            source_id="ns=2;s=TAG",
            domain="topo",
            created_by="test",
        )
        ctx = graph.node_context(node["id"])
        assert "signals" in ctx
        assert len(ctx["signals"]) >= 1
        assert ctx["signals"][0]["source_type"] == "opc_ua"

    def test_filters_signals_by_domain(self, graph):
        from ohm.graph.queries import create_external_signal

        node = graph.create_node(label="Multi Signal", node_type="concept")
        create_external_signal(graph._conn, node_id=node["id"], source_type="opc_ua", source_id="T1", domain="topo", created_by="t")
        create_external_signal(graph._conn, node_id=node["id"], source_type="market_feed", source_id="T2", domain="trading", created_by="t")

        topo_ctx = graph.node_context(node["id"], domain="topo")
        assert all(s["domain"] == "topo" for s in topo_ctx["signals"])
        assert len(topo_ctx["signals"]) == 1

    def test_includes_confidence(self, graph):
        node = graph.create_node(label="With Confidence", node_type="concept")
        ctx = graph.node_context(node["id"])
        assert "confidence" in ctx

    def test_empty_components_for_bare_node(self, graph):
        node = graph.create_node(label="Bare", node_type="concept")
        ctx = graph.node_context(node["id"])
        assert ctx["observations"] == []
        assert ctx["signals"] == []

    def test_all_fields_present(self, graph):
        node = graph.create_node(label="Full", node_type="concept")
        ctx = graph.node_context(node["id"])
        assert {"node", "neighborhood", "observations", "signals", "confidence"}.issubset(ctx.keys())
