"""Tests for PERT elicitation SDK methods (OHM-9iyh)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _create_edge(
    conn: DuckDBPyConnection,
    from_node: str,
    to_node: str,
    layer: str = "L3",
    edge_type: str = "CAUSES",
    confidence: float = 0.7,
) -> str:
    edge_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [edge_id, from_node, to_node, layer, edge_type, confidence, "test_agent"],
    )
    return edge_id


@pytest.fixture
def graph(test_db):
    from ohm.sdk import Graph
    return Graph(test_db, actor="test_agent")


class TestBatchUpdateEdges:
    def test_batch_update_empty(self, graph):
        result = graph.batch_update_edges([])
        assert result["count"] == 0

    def test_batch_update_single_edge(self, graph, test_db):
        a = _create_edge(test_db, "node_a", "node_b", confidence=0.5)
        result = graph.batch_update_edges([
            {"id": a, "probability_p50": 0.7, "probability_p05": 0.3, "probability_p95": 0.9},
        ])
        assert result["count"] == 1
        assert a in result["updated"]

        row = test_db.execute("SELECT probability_p50, probability_p05, probability_p95 FROM ohm_edges WHERE id = ?", [a]).fetchone()
        assert row[0] == pytest.approx(0.7, abs=0.01)
        assert row[1] == pytest.approx(0.3, abs=0.01)
        assert row[2] == pytest.approx(0.9, abs=0.01)

    def test_batch_update_multiple_edges(self, graph, test_db):
        e1 = _create_edge(test_db, "n1", "n2", confidence=0.5)
        e2 = _create_edge(test_db, "n3", "n4", confidence=0.6)
        result = graph.batch_update_edges([
            {"id": e1, "probability_p50": 0.8, "probability_p05": 0.5},
            {"id": e2, "probability_p50": 0.9, "probability_p05": 0.7},
        ])
        assert result["count"] == 2

    def test_batch_update_missing_id(self, graph):
        result = graph.batch_update_edges([{"probability_p50": 0.5}])
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error"] == "missing_id"

    def test_batch_update_no_fields(self, graph, test_db):
        e = _create_edge(test_db, "n1", "n2", confidence=0.5)
        result = graph.batch_update_edges([{"id": e}])
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error"] == "no_fields"


class TestAggregateExperts:
    def test_aggregate_two_experts_uniform(self, graph):
        result = graph.aggregate_experts(
            [(0.2, 0.4, 0.6), (0.3, 0.5, 0.7)],
        )
        assert result["p50"] == pytest.approx(0.45, abs=0.01)
        assert result["mean"] > 0
        assert result["total_variance"] > 0

    def test_aggregate_single_expert(self, graph):
        result = graph.aggregate_experts([(0.1, 0.5, 0.9)])
        assert result["p50"] == pytest.approx(0.5, abs=0.01)
        assert result["between_variance"] == 0.0

    def test_aggregate_weighted(self, graph):
        result = graph.aggregate_experts(
            [(0.1, 0.3, 0.5), (0.5, 0.7, 0.9)],
            weights=[0.8, 0.2],
        )
        assert result["p50"] < 0.5  # weighted toward first expert's lower estimate
        assert result["p05"] > 0
        assert result["p95"] < 1

    def test_aggregate_empty_list(self, graph):
        result = graph.aggregate_experts([])
        assert result["mean"] == 0.0
        assert result["variance"] == 0.0

    def test_aggregate_three_experts(self, graph):
        result = graph.aggregate_experts([
            (0.1, 0.3, 0.5),
            (0.3, 0.5, 0.7),
            (0.2, 0.4, 0.6),
        ])
        assert result["p50"] == pytest.approx(0.4, abs=0.02)
        assert result["between_variance"] > 0  # experts disagree
