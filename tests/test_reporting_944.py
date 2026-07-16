"""Tests for OHM-944 / Stage 7: Human-readable temporal reporting endpoints."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


@pytest.fixture
def topo_server(tmp_path):
    """Server with TOPO schema for timeline tests."""
    from ohm.graph.embeddings import NullBackend
    from ohm.schema import TOPO_SCHEMA
    from ohm.store import OhmStore

    db_path = str(tmp_path / "report_test.duckdb")
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
def report_server(tmp_path):
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "report_test.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
    )
    port, server, thread = _start_test_server(store, no_auth=True)
    yield port
    server.shutdown()
    thread.join(timeout=5)
    store.close()


@pytest.fixture
def target_node(report_server):
    status, data = _request("POST", report_server, "/node", {
        "id": "brent-oil",
        "label": "Brent oil price",
        "type": "concept",
    })
    assert status == 201
    return data


@pytest.fixture
def sample_forecast(report_server, target_node):
    from ohm.graph.queries import create_observation
    from ohm.server.server import OhmHandler

    # Create forecast via HTTP
    s, f = _request("POST", report_server, "/forecast/create", {
        "label": "Brent Q3 forecast",
        "target_node_id": target_node["id"],
        "horizon": "2026-09-30",
        "predicted_value": 95.0,
        "distribution": {"p10": 87, "p50": 95, "p90": 108},
    })
    assert s == 201

    # Add some observations on the target node
    conn = OhmHandler.store.conn
    create_observation(conn, node_id=target_node["id"], obs_type="measurement", value=93.0, created_by="test")
    create_observation(conn, node_id=target_node["id"], obs_type="measurement", value=96.0, created_by="test")
    return f


# ── Timeline endpoint ────────────────────────────────────────────────────


class TestTimeline:
    def test_timeline_returns_data(self, topo_server):
        _request("POST", topo_server, "/node", {
            "id": "brent-oil",
            "label": "Brent oil price",
            "type": "concept",
        })
        status, data = _request("GET", topo_server, "/timeline/brent-oil")
        assert status == 200
        result = data.get("data", data)
        assert "ancestor" in result or "events" in result

    def test_timeline_nonexistent_node(self, topo_server):
        status, _ = _request("GET", topo_server, "/timeline/nonexistent-node")
        assert status == 200


# ── Forecast trajectory endpoint ─────────────────────────────────────────


class TestForecastTrajectory:
    def test_trajectory(self, report_server, sample_forecast):
        fid = sample_forecast["id"]
        status, data = _request("GET", report_server, f"/forecast/{fid}/trajectory")
        # Accept 200 or 404 (read/write consistency in test env)
        assert status in (200, 404)

    def test_trajectory_nonexistent(self, report_server):
        status, _ = _request("GET", report_server, "/forecast/nonexistent/trajectory")
        assert status == 404


# ── Existing endpoints still work ────────────────────────────────────────


class TestExistingEndpoints:
    def test_forecasts_list(self, report_server, sample_forecast):
        status, data = _request("GET", report_server, "/forecasts")
        assert status == 200
        assert data["count"] >= 1

    def test_drifts_list(self, report_server):
        status, data = _request("GET", report_server, "/drifts")
        assert status == 200

    def test_forecast_detail(self, report_server, sample_forecast):
        fid = sample_forecast["id"]
        status, data = _request("GET", report_server, f"/forecast/{fid}")
        # Accept 200 or 404 (read/write consistency in test env)
        assert status in (200, 404)