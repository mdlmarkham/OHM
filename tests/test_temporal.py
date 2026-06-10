"""Tests for temporal causal analysis — Granger causality and edge stability."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import create_sample_edge, create_sample_node, create_sample_observation


try:
    import numpy as np
    from scipy import stats

    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


pytestmark = pytest.mark.skipif(not SCIPY_AVAILABLE, reason="scipy not installed")


def _make_timestamp(offset_days: int) -> str:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    ts = base + timedelta(days=offset_days)
    return ts.isoformat()


class TestGrangerCausality:
    def test_granger_insufficient_observations(self, test_db):
        from ohm.methods import granger_causality

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        result = granger_causality(test_db, node_a, node_b)
        assert result["method"] == "granger_causality"
        assert result["granger_causes"] is False
        assert result["f_statistic"] is None
        assert "Insufficient" in result["error"]

    def test_granger_insufficient_overlapping(self, test_db):
        from ohm.methods import granger_causality

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        for i in range(10):
            create_sample_observation(test_db, node_id=node_a, value=1.0, created_at=_make_timestamp(i))

        for i in range(10, 20):
            create_sample_observation(test_db, node_id=node_b, value=0.0, created_at=_make_timestamp(i))

        result = granger_causality(test_db, node_a, node_b)
        assert result["method"] == "granger_causality"
        assert result["granger_causes"] is False
        assert "overlapping" in result["error"].lower()

    def test_granger_with_overlapping_observations(self, test_db):
        from ohm.methods import granger_causality

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        for i in range(20):
            create_sample_observation(test_db, node_id=node_a, value=float(i % 2), created_at=_make_timestamp(i))
            create_sample_observation(test_db, node_id=node_b, value=float((i + 1) % 2), created_at=_make_timestamp(i))

        result = granger_causality(test_db, node_a, node_b, max_lag=2)
        assert result["method"] == "granger_causality"
        assert result["f_statistic"] is not None
        assert result["p_value"] is not None
        assert result["n_observations"] == 20
        assert result["lag_order"] == 2

    def test_granger_custom_max_lag(self, test_db):
        from ohm.methods import granger_causality

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        for i in range(10):
            create_sample_observation(test_db, node_id=node_a, value=0.8, created_at=_make_timestamp(i))
            create_sample_observation(test_db, node_id=node_b, value=0.6, created_at=_make_timestamp(i))

        result = granger_causality(test_db, node_a, node_b, max_lag=5)
        assert result["method"] == "granger_causality"
        assert result["lag_order"] <= 5


class TestEdgeStability:
    def test_edge_stability_empty_graph(self, test_db):
        from ohm.methods import compute_edge_stability

        result = compute_edge_stability(test_db)
        assert result["method"] == "edge_stability"
        assert result["edges"] == []
        assert result["n_edges"] == 0

    def test_edge_stability_with_edges(self, test_db):
        from ohm.methods import compute_edge_stability

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.9, confidence=0.9)

        result = compute_edge_stability(test_db)
        assert result["method"] == "edge_stability"
        assert result["n_edges"] == 1
        edge = result["edges"][0]
        assert edge["from_node"] == node_a
        assert edge["to_node"] == node_b
        assert edge["edge_type"] == "CAUSES"
        assert edge["stability"] in ("stable", "moderate", "unstable")

    def test_edge_stability_filters_by_type(self, test_db):
        from ohm.methods import compute_edge_stability

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")
        node_c = create_sample_node(test_db, label="c")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8)
        create_sample_edge(test_db, from_node=node_b, to_node=node_c, edge_type="INFLUENCES", probability=0.6)

        result = compute_edge_stability(test_db, edge_types=["CAUSES"])
        assert result["n_edges"] == 1
        assert result["edges"][0]["edge_type"] == "CAUSES"

    def test_edge_stability_stable_edge(self, test_db):
        from ohm.methods import compute_edge_stability

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.9, confidence=0.95)

        result = compute_edge_stability(test_db)
        assert result["n_stable"] >= 1

    def test_edge_stability_unstable_edge(self, test_db):
        from ohm.methods import compute_edge_stability

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.5, confidence=0.5)

        result = compute_edge_stability(test_db)
        edge = result["edges"][0]
        assert edge["stability"] in ("moderate", "unstable")

    def test_edge_stability_layer_filter(self, test_db):
        from ohm.methods import compute_edge_stability

        node_a = create_sample_node(test_db, label="a")
        node_b = create_sample_node(test_db, label="b")

        create_sample_edge(test_db, from_node=node_a, to_node=node_b, edge_type="CAUSES", probability=0.8, layer="L3")

        result = compute_edge_stability(test_db, layer="L3")
        assert result["n_edges"] == 1

        result_l4 = compute_edge_stability(test_db, layer="L4")
        assert result_l4["n_edges"] == 0
