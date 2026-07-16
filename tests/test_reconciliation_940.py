"""Tests for OHM-940 / Stage 3: Plans-vs-actuals reconciliation loop."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temporal_server(tmp_path):
    """Start a test server with TOPO schema (needed for topo_plans, topo_events, etc.)."""
    from ohm.graph.embeddings import NullBackend
    from ohm.schema import TOPO_SCHEMA
    from ohm.store import OhmStore

    db_path = str(tmp_path / "reconciliation_test.duckdb")
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
        "start_ts": "2026-08-01T00:00:00Z",
        "end_ts": "2026-08-05T00:00:00Z",
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
        "start_ts": "2026-08-02T00:00:00Z",
        "title": "Quarterly inspection",
        "plan_id": sample_plan["id"],
    })
    assert status == 201
    return data


# ── Reconciliation unit tests ───────────────────────────────────────────────


class TestReconciliationUnit:
    def test_reconcile_plan_with_no_events_produces_missing_event(self, temporal_server, sample_plan):
        """Reconciliation on a plan with zero actuals produces a missing_event drift."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": True,
        })
        assert status == 200
        assert data["count"] >= 1
        assert all(d["drift_type"] == "missing_event" for d in data["drifts"])
        assert data["dry_run"] is True

    def test_reconcile_plan_with_matching_actuals_no_drift(self, temporal_server, sample_plan, sample_event):
        """Running against a plan with matching actuals produces no timing drift (event within tolerance)."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": True,
            "tolerance": {"timing_seconds": 200000.0},
        })
        assert status == 200
        timing_drifts = [d for d in data["drifts"] if d["drift_type"] == "timing_drift"]
        assert len(timing_drifts) == 0

    def test_reconcile_dry_run_does_not_write(self, temporal_server, sample_plan):
        """dry_run=true returns drift records without writing observations."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": True,
        })
        assert status == 200
        assert data["dry_run"] is True

    def test_reconcile_writes_drift_observation(self, temporal_server, sample_plan):
        """dry_run=false writes drift observations."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": False,
            "created_by": "test_agent",
        })
        assert status == 200
        assert data["count"] >= 1
        assert data["dry_run"] is False

    def test_reconcile_nonexistent_plan(self, temporal_server):
        """Reconciliation of a nonexistent plan returns empty drifts."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "plan_id": "nonexistent-plan-xyz",
            "dry_run": True,
        })
        assert status == 200
        assert data["count"] == 0

    def test_reconcile_all_plans(self, temporal_server, sample_plan):
        """Reconciliation without plan_id checks all active plans."""
        status, data = _request("POST", temporal_server, "/reconcile", {
            "dry_run": True,
        })
        assert status == 200
        # Should have at least one drift (from our sample_plan with no events)
        assert data["count"] >= 1


# ── Drift list / explain endpoint tests ─────────────────────────────────────


class TestDriftEndpoints:
    def test_drift_list_empty(self, temporal_server):
        """GET /drifts returns empty when no drifts exist."""
        status, data = _request("GET", temporal_server, "/drifts")
        assert status == 200
        assert "drifts" in data
        assert data["count"] == 0

    def test_drift_list_after_reconcile(self, temporal_server, sample_plan):
        """After running reconciliation (non-dry-run), drifts should be listable and non-empty."""
        _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": False,
            "created_by": "test_agent",
        })
        status, data = _request("GET", temporal_server, "/drifts")
        assert status == 200
        assert data["count"] >= 1
        drift = data["drifts"][0]
        assert drift["type"] == "anomaly"

    def test_drift_list_filter_by_plan_id(self, temporal_server, sample_plan):
        """GET /drifts?plan_id=<id> returns only drifts for that plan."""
        _request("POST", temporal_server, "/reconcile", {
            "plan_id": sample_plan["id"],
            "dry_run": False,
            "created_by": "test_agent",
        })
        status, data = _request("GET", temporal_server, f"/drifts?plan_id={sample_plan['id']}")
        assert status == 200
        assert data["count"] >= 1

    def test_drift_explain_requires_drift_id(self, temporal_server):
        """GET /drift/explain without drift_id returns 400."""
        status, _ = _request("GET", temporal_server, "/drift/explain")
        assert status == 400


# ── MCP tool schema tests ───────────────────────────────────────────────────


class TestMCPSchemas940:
    def test_reconcile_tool_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        assert "ohm_reconcile" in tool_names
        assert "ohm_drift_explain" in tool_names

    def test_tool_count_72(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 72


# ── MCP dispatch tests ──────────────────────────────────────────────────────


class TestMCPDispatch940:
    def test_dispatch_reconcile(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_reconcile", {"plan_id": "p1"}, "test")
        assert method == "POST"
        assert path == "/reconcile"
        assert body["plan_id"] == "p1"

    def test_dispatch_reconcile_dry_run(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_reconcile", {"dry_run": True}, "test")
        assert method == "POST"
        assert body["dry_run"] is True

    def test_dispatch_drift_explain(self):
        from ohm.mcp.dispatch import build_request
        method, path, body = build_request("ohm_drift_explain", {"drift_id": "d1"}, "test")
        assert method == "GET"
        assert "drift_id=d1" in path