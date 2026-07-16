"""Tests for temporal planning MCP tools (OHM-937).

Covers plan/event/report/run/rul create, scenario run, verification
outcomes, drift observations, and MCP tool schema + dispatch for all 13
new tools.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import _start_test_server, _request

pytestmark = pytest.mark.integration


@pytest.fixture
def temporal_server(tmp_path):
    """Start a test server with TOPO schema (needed for topo_plans, topo_events, etc.)."""
    from ohm.graph.embeddings import NullBackend
    from ohm.schema import TOPO_SCHEMA
    from ohm.store import OhmStore

    db_path = str(tmp_path / "temporal_test.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
        schema=TOPO_SCHEMA,
    )
    port, server, thread = _start_test_server(store, no_auth=True, schema_config=TOPO_SCHEMA)
    yield port
    server.shutdown()
    thread.join(timeout=5)
    store.close()


@pytest.fixture
def sample_plan(temporal_server):
    """Create a sample plan and return its data."""
    status, data = _request("POST", temporal_server, "/plan/create", {
        "plan_type": "operational",
        "label": "Test maintenance plan",
        "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-06-30T00:00:00Z",
        "horizon": "180d",
    })
    assert status == 201
    return data


@pytest.fixture
def sample_event(temporal_server, sample_plan):
    """Create a sample event linked to a plan."""
    status, data = _request("POST", temporal_server, "/event/create", {
        "node_id": "test-equipment-1",
        "event_class": "milestone",
        "start_ts": "2026-03-15T00:00:00Z",
        "title": "Quarterly inspection",
        "plan_id": sample_plan["id"],
    })
    assert status == 201
    return data


@pytest.fixture
def sample_report(temporal_server):
    """Create a sample report."""
    status, data = _request("POST", temporal_server, "/report/create", {
        "report_type": "status",
        "title": "Q1 maintenance report",
        "summary": "All systems operational",
    })
    assert status == 201
    return data


@pytest.fixture
def sample_run(temporal_server):
    """Create a sample run."""
    status, data = _request("POST", temporal_server, "/run/create", {
        "run_type": "calibration",
        "inputs": {"model": "v1", "dataset": "q1"},
    })
    assert status == 201
    return data


# ── Plan endpoints ──────────────────────────────────────────────────────


class TestPlanCreate:
    def test_create_plan(self, temporal_server):
        status, data = _request("POST", temporal_server, "/plan/create", {
            "plan_type": "strategic",
            "label": "Annual review",
        })
        assert status == 201
        assert data["plan_type"] == "strategic"
        assert data["label"] == "Annual review"

    def test_create_plan_requires_type(self, temporal_server):
        status, data = _request("POST", temporal_server, "/plan/create", {})
        assert status == 400

    def test_list_plans(self, temporal_server, sample_plan):
        status, data = _request("GET", temporal_server, "/plans")
        assert status == 200
        assert data["count"] >= 1

    def test_list_plans_filter_type(self, temporal_server, sample_plan):
        status, data = _request("GET", temporal_server, "/plans?plan_type=operational")
        assert status == 200
        assert data["count"] >= 1

    def test_list_plans_filter_nonexistent(self, temporal_server, sample_plan):
        status, data = _request("GET", temporal_server, "/plans?plan_type=nonexistent")
        assert status == 200
        assert data["count"] == 0


# ── Event endpoints ─────────────────────────────────────────────────────


class TestEventCreate:
    def test_create_event(self, temporal_server, sample_plan):
        status, data = _request("POST", temporal_server, "/event/create", {
            "node_id": "test-equipment-2",
            "event_class": "incident",
            "start_ts": "2026-04-01T10:00:00Z",
            "title": "Unexpected shutdown",
            "plan_id": sample_plan["id"],
        })
        assert status == 201
        assert data["event_class"] == "incident"

    def test_create_event_requires_fields(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/event/create", {
            "node_id": "test-equipment-3",
        })
        assert status == 400

    def test_create_event_link(self, temporal_server, sample_event):
        status2, data2 = _request("POST", temporal_server, "/event/create", {
            "node_id": "test-equipment-4",
            "event_class": "assessment",
            "start_ts": "2026-04-02T00:00:00Z",
        })
        assert status2 == 201
        link_status, link_data = _request("POST", temporal_server, "/event/link", {
            "from_event_id": sample_event["id"],
            "to_event_id": data2["id"],
            "edge_type": "FOLLOWS",
        })
        assert link_status == 201

    def test_create_event_link_requires_fields(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/event/link", {})
        assert status == 400


# ── Report endpoints ────────────────────────────────────────────────────


class TestReportEndpoints:
    def test_create_report(self, temporal_server):
        status, data = _request("POST", temporal_server, "/report/create", {
            "report_type": "incident",
            "title": "Safety incident report",
            "summary": "Minor incident during maintenance",
        })
        assert status == 201
        assert data["report_type"] == "incident"

    def test_create_report_requires_type(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/report/create", {})
        assert status == 400

    def test_list_reports(self, temporal_server, sample_report):
        status, data = _request("GET", temporal_server, "/reports")
        assert status == 200
        assert data["count"] >= 1

    def test_finalize_report(self, temporal_server, sample_report):
        report_id = sample_report["id"]
        status, data = _request("POST", temporal_server, "/report/finalize", {
            "report_id": report_id,
        })
        assert status == 200
        assert data.get("status") == "finalized"

    def test_finalize_requires_report_id(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/report/finalize", {})
        assert status == 400


# ── Run endpoints ───────────────────────────────────────────────────────


class TestRunEndpoints:
    def test_create_run(self, temporal_server):
        status, data = _request("POST", temporal_server, "/run/create", {
            "run_type": "ingestion",
        })
        assert status == 201
        assert data["run_type"] == "ingestion"

    def test_create_run_requires_type(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/run/create", {})
        assert status == 400

    def test_list_runs(self, temporal_server, sample_run):
        status, data = _request("GET", temporal_server, "/runs")
        assert status == 200
        assert data["count"] >= 1

    def test_complete_run(self, temporal_server, sample_run):
        run_id = sample_run["id"]
        status, data = _request("POST", temporal_server, "/run/complete", {
            "run_id": run_id,
            "outputs": {"accuracy": 0.95},
            "duration_ms": 1234,
        })
        assert status == 200
        assert data.get("status") == "completed"

    def test_complete_requires_run_id(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/run/complete", {})
        assert status == 400


# ── RUL endpoints ───────────────────────────────────────────────────────


class TestRULEndpoints:
    def test_register_rul(self, temporal_server):
        status, data = _request("POST", temporal_server, "/rul/register", {
            "equipment_node_id": "pump-001",
            "rul_days": 45.0,
            "risk_class": "medium",
        })
        assert status == 201
        assert data["prospect"]["rul_days"] == 45.0

    def test_register_requires_fields(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/rul/register", {})
        assert status == 400

    def test_list_rul(self, temporal_server):
        _request("POST", temporal_server, "/rul/register", {
            "equipment_node_id": "pump-002",
            "rul_days": 30.0,
            "risk_class": "high",
        })
        status, data = _request("GET", temporal_server, "/rul")
        assert status == 200
        assert data["count"] >= 1

    def test_list_rul_filter(self, temporal_server):
        _request("POST", temporal_server, "/rul/register", {
            "equipment_node_id": "pump-003",
            "rul_days": 10.0,
            "risk_class": "critical",
        })
        status, data = _request("GET", temporal_server, "/rul?risk_class=critical")
        assert status == 200
        assert data["count"] >= 1


# ── Scenario endpoints ──────────────────────────────────────────────────


class TestScenarioEndpoints:
    def test_scenario_run_no_persist(self, temporal_server):
        """Scenario run without persistence returns cascade results."""
        status, data = _request("POST", temporal_server, "/scenario/run", {
            "node_id": "test-node-1",
            "compare": True,
            "persist": False,
        })
        assert status == 200
        assert "node_id" in data

    def test_scenario_run_requires_node_id(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/scenario/run", {})
        assert status == 400

    def test_list_scenarios_empty(self, temporal_server):
        status, data = _request("GET", temporal_server, "/scenarios")
        assert status == 200
        assert data["count"] == 0


# ── Verification endpoints ──────────────────────────────────────────────


class TestVerificationEndpoints:
    def test_verifiable_claims(self, temporal_server):
        status, data = _request("GET", temporal_server, "/verifiable-claims")
        assert status == 200
        assert "claims" in data

    def test_record_verification_outcome_requires_fields(self, temporal_server):
        status, _ = _request("POST", temporal_server, "/verification/outcome", {})
        assert status == 400


# ── Drift endpoints ─────────────────────────────────────────────────────


class TestDriftEndpoints:
    def test_drifts_empty(self, temporal_server):
        status, data = _request("GET", temporal_server, "/drifts")
        assert status == 200
        assert data["count"] == 0


# ── MCP tool schemas ────────────────────────────────────────────────────


class TestMCPSchemas:
    def test_all_temporal_tools_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        expected = {
            "ohm_plan_create", "ohm_event_create", "ohm_event_link",
            "ohm_report_create", "ohm_report_finalize",
            "ohm_run_create", "ohm_run_complete",
            "ohm_rul_register", "ohm_scenario_run", "ohm_scenarios",
            "ohm_verifiable_claims", "ohm_record_verification_outcome",
            "ohm_drifts",
        }
        assert expected.issubset(tool_names), f"Missing: {expected - tool_names}"

    def test_tool_count(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 69


# ── MCP dispatch ────────────────────────────────────────────────────────


class TestMCPDispatch:
    def test_dispatch_plan_create(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_plan_create", {"plan_type": "ops"}, "test")
        assert method == "POST"
        assert path == "/plan/create"
        assert body["plan_type"] == "ops"

    def test_dispatch_event_create(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_event_create", {
            "node_id": "n1", "event_class": "milestone", "start_ts": "2026-01-01T00:00:00Z",
        }, "test")
        assert method == "POST"
        assert path == "/event/create"

    def test_dispatch_event_link(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_event_link", {
            "from_event_id": "e1", "to_event_id": "e2", "edge_type": "CAUSES",
        }, "test")
        assert method == "POST"
        assert path == "/event/link"

    def test_dispatch_report_create(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_report_create", {"report_type": "status"}, "test")
        assert method == "POST"
        assert path == "/report/create"

    def test_dispatch_report_finalize(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_report_finalize", {"report_id": "r1"}, "test")
        assert method == "POST"
        assert path == "/report/finalize"

    def test_dispatch_run_create(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_run_create", {"run_type": "cascade"}, "test")
        assert method == "POST"
        assert path == "/run/create"

    def test_dispatch_run_complete(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_run_complete", {"run_id": "run1"}, "test")
        assert method == "POST"
        assert path == "/run/complete"

    def test_dispatch_rul_register(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_rul_register", {
            "equipment_node_id": "eq1", "rul_days": 30, "risk_class": "high",
        }, "test")
        assert method == "POST"
        assert path == "/rul/register"

    def test_dispatch_scenario_run(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_scenario_run", {"node_id": "n1"}, "test")
        assert method == "POST"
        assert path == "/scenario/run"

    def test_dispatch_scenarios(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_scenarios", {}, "test")
        assert method == "GET"
        assert path == "/scenarios"
        assert body is None

    def test_dispatch_scenarios_with_filter(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_scenarios", {"target_node_id": "n1"}, "test")
        assert method == "GET"
        assert "target_node_id=n1" in path

    def test_dispatch_verifiable_claims(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_verifiable_claims", {}, "test")
        assert method == "GET"
        assert path == "/verifiable-claims"

    def test_dispatch_record_verification_outcome(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_record_verification_outcome", {
            "edge_id": "e1", "outcome": True,
        }, "test")
        assert method == "POST"
        assert path == "/verification/outcome"
        assert body["outcome"] is True

    def test_dispatch_drifts(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_drifts", {}, "test")
        assert method == "GET"
        assert path == "/drifts"

    def test_dispatch_drifts_with_filter(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_drifts", {"severity": "high"}, "test")
        assert method == "GET"
        assert "severity=high" in path
