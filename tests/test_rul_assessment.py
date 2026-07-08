"""Tests for OHM-q4ku: RUL assessment storage hook via DomainTable."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import TOPO_SCHEMA, initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, TOPO_SCHEMA)
    return Graph(conn, actor="test_agent")


@pytest.fixture
def equipment(graph):
    conn = graph._conn
    conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('pump_P101', 'Pump P101', 'concept', 'test')")
    return "pump_P101"


class TestRegisterRulAssessment:
    def test_register_creates_prospect_row(self, graph, equipment):
        result = graph.register_rul_assessment(
            equipment,
            rul_days=45.0,
            risk_class="high",
            model_version="weibull_2p_v3",
        )
        assert result["prospect"]["equipment_id"] == "pump_P101"
        assert result["prospect"]["rul_days"] == 45.0
        assert result["prospect"]["risk_class"] == "high"
        assert result["prospect"]["model_version"] == "weibull_2p_v3"

    def test_register_creates_l4_predicts_edge(self, graph, equipment):
        result = graph.register_rul_assessment(
            equipment,
            rul_days=30.0,
            risk_class="critical",
        )
        assert result["edge_id"] is not None
        conn = graph._conn
        edge = conn.execute("SELECT edge_type, layer FROM ohm_edges WHERE id = ?", [result["edge_id"]]).fetchone()
        assert edge[0] == "PREDICTS"
        assert edge[1] == "L4"

    def test_register_without_existing_node_skips_edge(self, graph):
        result = graph.register_rul_assessment(
            "nonexistent_node",
            rul_days=60.0,
            risk_class="medium",
        )
        assert result["prospect"]["equipment_id"] == "nonexistent_node"
        assert result["edge_id"] is None

    def test_register_with_node_path_stores_in_metadata(self, graph, equipment):
        result = graph.register_rul_assessment(
            equipment,
            rul_days=90.0,
            risk_class="low",
            node_path="plant/unit_a/pump_P101",
        )
        import json

        meta = json.loads(result["prospect"]["metadata"])
        assert meta["node_path"] == "plant/unit_a/pump_P101"

    def test_register_with_site_id(self, graph, equipment):
        result = graph.register_rul_assessment(
            equipment,
            rul_days=15.0,
            risk_class="critical",
            site_id="site_north",
        )
        assert result["prospect"]["site_id"] == "site_north"

    def test_register_negative_rul_raises(self, graph, equipment):
        with pytest.raises(ValueError, match="non-negative"):
            graph.register_rul_assessment(equipment, rul_days=-1.0, risk_class="high")

    def test_register_empty_risk_class_raises(self, graph, equipment):
        with pytest.raises(ValueError, match="risk_class"):
            graph.register_rul_assessment(equipment, rul_days=10.0, risk_class="")


class TestGetRulAssessments:
    def test_get_all_assessments(self, graph, equipment):
        graph.register_rul_assessment(equipment, rul_days=30, risk_class="high")
        graph.register_rul_assessment(equipment, rul_days=60, risk_class="medium")
        results = graph.get_rul_assessments()
        assert len(results) == 2

    def test_get_by_equipment(self, graph, equipment):
        graph.register_rul_assessment(equipment, rul_days=30, risk_class="high")
        results = graph.get_rul_assessments(equipment_node_id=equipment)
        assert len(results) == 1
        assert results[0]["equipment_id"] == "pump_P101"

    def test_get_by_risk_class(self, graph, equipment):
        graph.register_rul_assessment(equipment, rul_days=30, risk_class="high")
        graph.register_rul_assessment(equipment, rul_days=60, risk_class="medium")
        results = graph.get_rul_assessments(risk_class="high")
        assert len(results) == 1
        assert results[0]["risk_class"] == "high"

    def test_get_by_site_id(self, graph, equipment):
        graph.register_rul_assessment(equipment, rul_days=30, risk_class="high", site_id="north")
        graph.register_rul_assessment(equipment, rul_days=60, risk_class="medium", site_id="south")
        results = graph.get_rul_assessments(site_id="north")
        assert len(results) == 1
        assert results[0]["site_id"] == "north"

    def test_get_empty(self, graph):
        results = graph.get_rul_assessments()
        assert results == []


class TestRulSchema:
    def test_topo_prospects_table_exists(self, graph):
        conn = graph._conn
        row = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'topo_prospects'").fetchone()
        assert row[0] == 1
