"""Tests for graph health diagnostics (OHM-6lvk / DOCTOR)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _create_node(conn: DuckDBPyConnection, **kw) -> str:
    node_id = f"doc_{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
        [node_id, kw.get("label", "test"), kw.get("node_type", "concept"), kw.get("created_by", "agent")],
    )
    return node_id


def _create_edge(
    conn: DuckDBPyConnection,
    from_node: str,
    to_node: str,
    layer: str = "L3",
    edge_type: str = "CAUSES",
    confidence: float = 0.9,
    **kw,
) -> str:
    edge_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [edge_id, from_node, to_node, layer, edge_type, confidence, kw.get("created_by", "agent")],
    )
    return edge_id


@pytest.fixture
def db(test_db):
    return test_db


class TestGraphHealthQuery:
    def test_empty_graph_returns_zero_totals(self, db):
        from ohm.queries import query_graph_health

        h = query_graph_health(db)
        assert h["total_nodes"] == 0
        assert h["total_edges"] == 0
        assert h["orphan_nodes"] == 0

    def test_single_node_is_orphan(self, db):
        _create_node(db, label="lonely")
        from ohm.queries import query_graph_health

        h = query_graph_health(db)
        assert h["orphan_nodes"] >= 1

    def test_node_with_edge_not_orphan(self, db):
        a = _create_node(db, label="A")
        b = _create_node(db, label="B")
        _create_edge(db, from_node=a, to_node=b)
        from ohm.queries import query_graph_health

        h = query_graph_health(db)
        assert h["orphan_nodes"] == 0

    def test_dead_end_detected(self, db):
        a = _create_node(db, label="A")
        b = _create_node(db, label="B")
        _create_edge(db, from_node=a, to_node=b)
        from ohm.queries import query_graph_health

        h = query_graph_health(db)
        assert h["dead_end_count"] >= 1


class TestDoctor:
    def test_empty_graph_returns_healthy(self, db):
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        assert "overall_health" in result
        assert result["total_nodes"] == 0
        assert isinstance(result["checks"], list)
        assert isinstance(result["remediations"], list)

    def test_orphans_appear_in_checks(self, db):
        _create_node(db, label="orphan")
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        check_ids = [c["id"] for c in result["checks"]]
        assert "orphan_nodes" in check_ids
        orphan_check = next(c for c in result["checks"] if c["id"] == "orphan_nodes")
        assert orphan_check["count"] >= 1

    def test_remediation_plan_includes_orphan_action(self, db):
        _create_node(db, label="orphan")
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        actions = [r["action"] for r in result["remediations"]]
        assert "connect_or_delete" in actions

    def test_remediation_plan_includes_dead_end_action(self, db):
        a = _create_node(db, label="A")
        b = _create_node(db, label="B")
        _create_edge(db, from_node=a, to_node=b)
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        actions = [r["action"] for r in result["remediations"]]
        assert "add_outgoing_edges" in actions

    def test_checks_sorted_by_severity_descending(self, db):
        _create_node(db, label="orphan")
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        severities = [c["severity"] for c in result["checks"]]
        assert severities == sorted(severities, reverse=True)

    def test_healthy_graph_has_few_remediations(self, db):
        a = _create_node(db, label="A")
        b = _create_node(db, label="B")
        c = _create_node(db, label="C")
        _create_edge(db, from_node=a, to_node=b)
        _create_edge(db, from_node=b, to_node=c)
        _create_edge(db, from_node=c, to_node=a)
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        assert result["remediation_count"] == 0

    def test_overall_health_is_between_0_and_1(self, db):
        _create_node(db, label="orphan")
        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        assert 0.0 <= result["overall_health"] <= 1.0

    def test_health_drops_with_more_issues(self, db):
        from ohm.methods import graph_doctor

        h1 = graph_doctor(db)["overall_health"]

        _create_node(db, label="orphan1")
        _create_node(db, label="orphan2")
        _create_node(db, label="orphan3")

        h2 = graph_doctor(db)["overall_health"]
        assert h2 <= h1

    def test_dense_cluster_detected(self, db):
        center = _create_node(db, label="hub")
        for i in range(12):
            leaf = _create_node(db, label=f"leaf_{i}")
            _create_edge(db, from_node=center, to_node=leaf)

        from ohm.methods import graph_doctor

        result = graph_doctor(db)
        check_ids = [c["id"] for c in result["checks"]]
        assert "dense_clusters" in check_ids
