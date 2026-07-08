"""Tests for OHM-08uk: TOPO DataProductRun execution tracking."""

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
def report(graph):
    return graph.create_report(report_id="rpt_001", report_type="sensitivity_analysis")


class TestCreateAndGetRun:
    def test_create_run(self, graph, report):
        result = graph.create_run(
            run_id="run_001",
            report_id=report["id"],
            run_type="notebook",
            inputs={"data_source": "topo_observations", "date_range": "2026-06-01/2026-06-30"},
        )
        assert result["id"] == "run_001"
        assert result["report_id"] == "rpt_001"
        assert result["run_type"] == "notebook"
        assert result["status"] == "pending"

    def test_create_run_minimal(self, graph):
        result = graph.create_run(run_id="run_min", run_type="correlation_study")
        assert result["id"] == "run_min"
        assert result["status"] == "pending"

    def test_get_run(self, graph):
        graph.create_run(run_id="run_get", run_type="rca_report")
        result = graph.get_run("run_get")
        assert result is not None
        assert result["id"] == "run_get"

    def test_get_run_not_found(self, graph):
        assert graph.get_run("nonexistent") is None


class TestListRuns:
    def test_list_all(self, graph):
        graph.create_run(run_id="r1", run_type="notebook")
        graph.create_run(run_id="r2", run_type="correlation_study")
        assert len(graph.list_runs()) == 2

    def test_list_by_report(self, graph, report):
        graph.create_run(run_id="r1", report_id=report["id"], run_type="notebook")
        graph.create_run(run_id="r2", run_type="notebook")
        results = graph.list_runs(report_id=report["id"])
        assert len(results) == 1

    def test_list_by_type(self, graph):
        graph.create_run(run_id="r1", run_type="notebook")
        graph.create_run(run_id="r2", run_type="correlation_study")
        results = graph.list_runs(run_type="notebook")
        assert len(results) == 1

    def test_list_by_status(self, graph):
        graph.create_run(run_id="r1", run_type="notebook", status="pending")
        graph.create_run(run_id="r2", run_type="notebook", status="completed")
        results = graph.list_runs(status="completed")
        assert len(results) == 1


class TestCompleteRun:
    def test_complete_sets_status_and_outputs(self, graph):
        graph.create_run(run_id="run_comp", run_type="notebook")
        result = graph.complete_run(
            "run_comp",
            outputs={"summary": "all good", "artifacts": ["chart.png"]},
            duration_ms=4500,
        )
        assert result["status"] == "completed"
        assert result["completed_at"] is not None
        assert result["duration_ms"] == 4500

    def test_complete_with_error(self, graph):
        graph.create_run(run_id="run_err", run_type="notebook")
        result = graph.complete_run("run_err", status="failed", error="Missing data source")
        assert result["status"] == "failed"
        assert result["error"] == "Missing data source"


class TestRunSchema:
    def test_topo_runs_table_exists(self, graph):
        conn = graph._conn
        row = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'topo_runs'").fetchone()
        assert row[0] == 1

    def test_topo_runs_has_expected_columns(self, graph):
        conn = graph._conn
        cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'topo_runs'").fetchall()}
        expected = {
            "id",
            "report_id",
            "node_id",
            "run_type",
            "status",
            "inputs",
            "outputs",
            "error",
            "duration_ms",
            "started_at",
            "completed_at",
            "created_by",
            "created_at",
            "metadata",
        }
        assert expected.issubset(cols)
