"""Tests for OHM-vatf: Bayesian propagate_observation."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import TOPO_SCHEMA, initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, TOPO_SCHEMA)
    g = Graph(conn, actor="test_agent")
    for nid, label in [("A", "Root"), ("B", "Middle"), ("C", "Leaf"), ("D", "Branch")]:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, 'concept', 'test')",
            [nid, label],
        )
    return g


def _add_edge(g, from_node, to_node, edge_type="CAUSES", probability=0.8, confidence=0.9, layer="L3"):
    g.create_edge(
        from_node=from_node,
        to_node=to_node,
        edge_type=edge_type,
        probability=probability,
        confidence=confidence,
        layer=layer,
    )


class TestSimpleChain:
    def test_two_hop_chain(self, graph):
        _add_edge(graph, "A", "B", probability=0.8, confidence=0.9)
        _add_edge(graph, "B", "C", probability=0.7, confidence=0.85)
        result = graph.propagate_observation("A", observation_weight=0.95)
        assert len(result) == 2
        assert result[0]["node_id"] == "B"
        assert abs(result[0]["accumulated_weight"] - 0.76) < 0.001
        assert result[1]["node_id"] == "C"
        assert abs(result[1]["accumulated_weight"] - 0.532) < 0.001

    def test_zero_weight_returns_empty(self, graph):
        result = graph.propagate_observation("A", observation_weight=0.0)
        assert result == []

    def test_no_downstream_returns_empty(self, graph):
        result = graph.propagate_observation("C", observation_weight=0.9)
        assert result == []

    def test_nonexistent_source_returns_empty(self, graph):
        result = graph.propagate_observation("nonexistent", observation_weight=1.0)
        assert result == []


class TestBranching:
    def test_two_downstream_branches(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        _add_edge(graph, "A", "D", probability=0.6)
        result = graph.propagate_observation("A", observation_weight=1.0)
        assert len(result) == 2
        weights = {r["node_id"]: r["accumulated_weight"] for r in result}
        assert abs(weights["B"] - 0.8) < 0.001
        assert abs(weights["D"] - 0.6) < 0.001


class TestPosteriorMath:
    def test_posterior_mean_from_uniform_prior(self, graph):
        _add_edge(graph, "A", "B", probability=0.5)
        result = graph.propagate_observation("A", observation_weight=1.0, prior_alpha=1.0, prior_beta=1.0)
        w = result[0]["accumulated_weight"]
        expected_mean = (1.0 + w) / (1.0 + w + 1.0 + 1.0 - w)
        assert abs(result[0]["posterior_mean"] - expected_mean) < 0.001

    def test_custom_prior_parameters(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        result = graph.propagate_observation("A", observation_weight=1.0, prior_alpha=5.0, prior_beta=5.0)
        w = result[0]["accumulated_weight"]
        assert abs(result[0]["posterior_alpha"] - (5.0 + w)) < 0.001
        assert abs(result[0]["posterior_beta"] - (5.0 + 1.0 - w)) < 0.001


class TestTraversalControl:
    def test_depth_limit(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        _add_edge(graph, "B", "C", probability=0.7)
        result = graph.propagate_observation("A", observation_weight=1.0, max_depth=1)
        assert len(result) == 1
        assert result[0]["node_id"] == "B"

    def test_layer_filter(self, graph):
        _add_edge(graph, "A", "B", edge_type="INFLUENCES", layer="L2")
        result = graph.propagate_observation("A", observation_weight=1.0, layers=("L3",))
        assert result == []

    def test_custom_edge_types(self, graph):
        _add_edge(graph, "A", "B", edge_type="DEPENDS_ON", layer="L4")
        result = graph.propagate_observation("A", observation_weight=1.0, edge_types=("DEPENDS_ON",))
        assert len(result) == 1
        result2 = graph.propagate_observation("A", observation_weight=1.0, edge_types=("CAUSES",))
        assert result2 == []


class TestEdgeCases:
    def test_cycle_handling(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        _add_edge(graph, "B", "C", probability=0.7)
        _add_edge(graph, "C", "A", probability=0.5)
        result = graph.propagate_observation("A", observation_weight=1.0)
        assert len(result) == 2
        assert result[0]["node_id"] == "B"
        assert result[1]["node_id"] == "C"

    def test_deleted_edges_excluded(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        graph._conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = 'A' AND to_node = 'B'")
        result = graph.propagate_observation("A", observation_weight=1.0)
        assert result == []

    def test_path_chain_recorded(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        _add_edge(graph, "B", "C", probability=0.7)
        result = graph.propagate_observation("A", observation_weight=0.95)
        assert list(result[0]["path"]) == ["A", "B"]
        assert list(result[1]["path"]) == ["A", "B", "C"]


class TestIntegrationWithEvents:
    def test_event_confidence_as_observation_weight(self, graph):
        _add_edge(graph, "A", "B", probability=0.8)
        _add_edge(graph, "B", "C", probability=0.7)
        event = graph.create_event(
            event_id="evt_failure",
            node_id="A",
            event_class="FAILURE",
            start_ts="2026-07-01 00:00:00",
            confidence=0.95,
            authority="sensor",
        )
        result = graph.propagate_observation(
            "A",
            observation_weight=event.get("confidence", 0.95),
        )
        assert len(result) >= 1
        assert abs(result[0]["accumulated_weight"] - 0.95 * 0.8) < 0.001
