"""Tests for OHM-o3rd: TOPO versioned analytical report artifacts."""

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
def node(graph):
    conn = graph._conn
    conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by) VALUES ('pump_A', 'Pump A', 'concept', 'test')")
    return "pump_A"


class TestCreateAndGetReport:
    def test_create_report(self, graph, node):
        result = graph.create_report(
            report_id="rpt_001",
            report_type="sensitivity_analysis",
            node_id=node,
            title="Pump A sensitivity analysis",
            summary="Pump A is sensitive to flow rate changes",
            findings={"key_driver": "flow_rate", "sensitivity": 0.82},
            recommendations={"action": "install_damper", "priority": "high"},
        )
        assert result["id"] == "rpt_001"
        assert result["report_type"] == "sensitivity_analysis"
        assert result["node_id"] == "pump_A"
        assert result["status"] == "draft"
        assert result["version"] == 1

    def test_create_report_minimal(self, graph):
        result = graph.create_report(report_id="rpt_min", report_type="rca_report")
        assert result["id"] == "rpt_min"
        assert result["report_type"] == "rca_report"
        assert result["status"] == "draft"

    def test_get_report(self, graph, node):
        graph.create_report(
            report_id="rpt_get",
            report_type="correlation_study",
            node_id=node,
        )
        result = graph.get_report("rpt_get")
        assert result is not None
        assert result["id"] == "rpt_get"

    def test_get_report_not_found(self, graph):
        result = graph.get_report("nonexistent")
        assert result is None


class TestListReports:
    def test_list_all_reports(self, graph):
        graph.create_report(report_id="r1", report_type="sensitivity_analysis")
        graph.create_report(report_id="r2", report_type="rca_report")
        reports = graph.list_reports()
        assert len(reports) == 2

    def test_list_by_type(self, graph):
        graph.create_report(report_id="r1", report_type="sensitivity_analysis")
        graph.create_report(report_id="r2", report_type="rca_report")
        reports = graph.list_reports(report_type="sensitivity_analysis")
        assert len(reports) == 1
        assert reports[0]["report_type"] == "sensitivity_analysis"

    def test_list_by_node(self, graph, node):
        graph.create_report(report_id="r1", report_type="rca_report", node_id=node)
        graph.create_report(report_id="r2", report_type="rca_report")
        reports = graph.list_reports(node_id=node)
        assert len(reports) == 1
        assert reports[0]["node_id"] == "pump_A"

    def test_list_by_status(self, graph):
        graph.create_report(report_id="r1", report_type="rca_report", status="draft")
        graph.create_report(report_id="r2", report_type="rca_report", status="finalized")
        reports = graph.list_reports(status="draft")
        assert len(reports) == 1
        assert reports[0]["status"] == "draft"


class TestFinalizeReport:
    def test_finalize_sets_status_and_timestamp(self, graph, node):
        graph.create_report(
            report_id="rpt_fin",
            report_type="sensitivity_analysis",
            node_id=node,
        )
        result = graph.finalize_report("rpt_fin")
        assert result["status"] == "finalized"
        assert result["finalized_at"] is not None

    def test_finalize_applies_confidence_adjustments(self, graph, node):
        conn = graph._conn
        conn.execute("INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by) VALUES ('edge_1', 'pump_A', 'valve_B', 'CAUSES', 'L3', 0.5, 'test')")
        graph.create_report(
            report_id="rpt_adj",
            report_type="sensitivity_analysis",
            node_id=node,
        )
        graph.finalize_report("rpt_adj", confidence_adjustments={"edge_1": 0.85})
        edge = conn.execute("SELECT confidence FROM ohm_edges WHERE id = 'edge_1'").fetchone()
        assert abs(edge[0] - 0.85) < 1e-6


class TestSupersedeReport:
    def test_supersede_sets_status_and_ref(self, graph):
        graph.create_report(report_id="rpt_old", report_type="rca_report")
        graph.create_report(report_id="rpt_new", report_type="rca_report")
        graph.supersede_report("rpt_old", "rpt_new")
        old = graph.get_report("rpt_old")
        assert old["status"] == "superseded"
        assert old["superseded_by"] == "rpt_new"


class TestReportSchema:
    def test_topo_reports_table_exists(self, graph):
        conn = graph._conn
        row = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'topo_reports'").fetchone()
        assert row[0] == 1

    def test_topo_reports_has_expected_columns(self, graph):
        conn = graph._conn
        cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'topo_reports'").fetchall()}
        expected = {
            "id",
            "report_type",
            "node_id",
            "plan_id",
            "title",
            "summary",
            "findings",
            "recommendations",
            "confidence_adjustments",
            "status",
            "version",
            "superseded_by",
            "created_by",
            "created_at",
            "updated_at",
            "finalized_at",
            "metadata",
        }
        assert expected.issubset(cols)
