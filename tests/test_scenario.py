"""Tests for the scenario engine for counterfactual inference (OHM-xagx)."""

from __future__ import annotations

import duckdb
import pytest

from ohm.schema import initialize_schema
from ohm.queries import (
    create_node,
    create_edge,
    query_counterfactual_cascade,
    query_compare_scenarios,
)


@pytest.fixture
def test_conn():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


def _seed_chain(conn):
    """Supplier -> Factory -> Distributor with known probabilities."""
    a = create_node(conn, label="Supplier", node_type="concept", created_by="metis")
    b = create_node(conn, label="Factory", node_type="concept", created_by="metis")
    c = create_node(conn, label="Distributor", node_type="concept", created_by="metis")
    e1 = create_edge(conn, from_node=a["id"], to_node=b["id"], edge_type="CAUSES", layer="L3", created_by="metis", probability=0.7)
    e2 = create_edge(conn, from_node=b["id"], to_node=c["id"], edge_type="CAUSES", layer="L3", created_by="metis", probability=0.5)
    return {"a": a, "b": b, "c": c, "e1": e1, "e2": e2}


class TestCounterfactualCascade:
    """Tests for query_counterfactual_cascade() (OHM-xagx)."""

    def test_baseline_cascade_matches_deterministic(self, test_conn):
        nodes = _seed_chain(test_conn)
        result = query_counterfactual_cascade(test_conn, nodes["a"]["id"], failure_probability=1.0)
        labels = {r["node_label"]: r["failure_probability"] for r in result}
        assert labels["Factory"] == pytest.approx(0.7, abs=0.01)
        assert labels["Distributor"] == pytest.approx(0.35, abs=0.01)

    def test_edge_override_changes_downstream(self, test_conn):
        nodes = _seed_chain(test_conn)
        cf = query_counterfactual_cascade(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            edge_overrides={nodes["e1"]["id"]: 0.3},
        )
        labels = {r["node_label"]: r["failure_probability"] for r in cf}
        assert labels["Factory"] == pytest.approx(0.3, abs=0.01)
        assert labels["Distributor"] == pytest.approx(0.15, abs=0.01)

    def test_node_intervention_forces_state(self, test_conn):
        nodes = _seed_chain(test_conn)
        cf = query_counterfactual_cascade(
            test_conn,
            nodes["a"]["id"],
            failure_probability=0.0,
            node_interventions={nodes["b"]["id"]: 0.9},
        )
        labels = {r["node_label"]: r["failure_probability"] for r in cf}
        assert labels["Factory"] == pytest.approx(0.9, abs=0.01)
        assert labels["Distributor"] == pytest.approx(0.45, abs=0.01)
        # The intervened flag should be set
        factory_result = [r for r in cf if r["node_label"] == "Factory"][0]
        assert factory_result["intervened"] is True

    def test_disabled_edge_stops_propagation(self, test_conn):
        nodes = _seed_chain(test_conn)
        cf = query_counterfactual_cascade(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            disabled_edges={nodes["e1"]["id"]},
        )
        assert len(cf) == 0  # No propagation when Supplier->Factory removed

    def test_disabled_node_stops_propagation(self, test_conn):
        nodes = _seed_chain(test_conn)
        cf = query_counterfactual_cascade(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            disabled_nodes={nodes["b"]["id"]},
        )
        labels = {r["node_label"] for r in cf}
        assert "Factory" not in labels
        assert "Distributor" not in labels

    def test_does_not_modify_graph(self, test_conn):
        nodes = _seed_chain(test_conn)
        # Run with overrides
        query_counterfactual_cascade(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            edge_overrides={nodes["e1"]["id"]: 0.01},
        )
        # Verify the edge still has original probability
        row = test_conn.execute(
            "SELECT probability FROM ohm_edges WHERE id = ?",
            [nodes["e1"]["id"]],
        ).fetchone()
        assert row[0] == pytest.approx(0.7, abs=0.01)

    def test_no_downstream_returns_empty(self, test_conn):
        a = create_node(test_conn, label="Isolated", node_type="concept", created_by="test")
        result = query_counterfactual_cascade(test_conn, a["id"], failure_probability=1.0)
        assert result == []

    def test_json_serializable(self, test_conn):
        import json

        nodes = _seed_chain(test_conn)
        result = query_counterfactual_cascade(test_conn, nodes["a"]["id"])
        serialized = json.dumps(result, default=str)
        assert "Factory" in serialized


class TestCompareScenarios:
    """Tests for query_compare_scenarios() (OHM-xagx)."""

    def test_comparison_returns_baseline_and_counterfactual(self, test_conn):
        nodes = _seed_chain(test_conn)
        result = query_compare_scenarios(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            edge_overrides={nodes["e1"]["id"]: 0.3},
        )
        assert "baseline" in result
        assert "counterfactual" in result
        assert "deltas" in result
        assert "summary" in result

    def test_deltas_show_decreased_nodes(self, test_conn):
        nodes = _seed_chain(test_conn)
        result = query_compare_scenarios(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            edge_overrides={nodes["e1"]["id"]: 0.3},
        )
        assert result["summary"]["decreased"] >= 1
        assert result["summary"]["increased"] == 0

    def test_deltas_show_removed_nodes_when_edge_disabled(self, test_conn):
        nodes = _seed_chain(test_conn)
        result = query_compare_scenarios(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
            disabled_edges={nodes["e1"]["id"]},
        )
        assert result["summary"]["removed"] >= 1

    def test_no_changes_when_no_overrides(self, test_conn):
        nodes = _seed_chain(test_conn)
        result = query_compare_scenarios(
            test_conn,
            nodes["a"]["id"],
            failure_probability=1.0,
        )
        assert result["summary"]["unchanged"] >= 1
        assert result["summary"]["decreased"] == 0
        assert result["summary"]["increased"] == 0

    def test_json_serializable(self, test_conn):
        import json

        nodes = _seed_chain(test_conn)
        result = query_compare_scenarios(
            test_conn,
            nodes["a"]["id"],
            edge_overrides={nodes["e1"]["id"]: 0.3},
        )
        serialized = json.dumps(result, default=str)
        assert "baseline" in serialized
        assert "counterfactual" in serialized
