"""Tests for OHM-943 / Stage 6: Lightweight time-series observations and helpers."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


@pytest.fixture
def series_server(tmp_path):
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "series_test.duckdb")
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
def series_with_data(series_server):
    """Create a node with 5 observations, last one anomalous."""
    from ohm.graph.queries import create_observation

    # Create node via HTTP
    _request("POST", series_server, "/node", {
        "id": "brent-price",
        "label": "Brent price",
        "type": "concept",
    })

    # Get the store's connection for direct observation writes
    from ohm.server.server import OhmHandler
    store = OhmHandler.store
    conn = store.conn

    for val in [90, 91, 92, 93, 150]:
        create_observation(
            conn,
            node_id="brent-price",
            obs_type="measurement",
            value=float(val),
            created_by="test",
            metadata={"series_id": "brent_test"},
        )
    return series_server


# ── Series query ──────────────────────────────────────────────────────────


class TestSeriesQuery:
    def test_query_series(self, series_with_data):
        status, data = _request("GET", series_with_data, "/series/query?series_id=brent_test")
        assert status == 200
        assert data["count"] == 5

    def test_query_series_requires_id(self, series_server):
        status, _ = _request("GET", series_server, "/series/query")
        assert status == 400

    def test_query_series_empty(self, series_server):
        status, data = _request("GET", series_server, "/series/query?series_id=nonexistent")
        assert status == 200
        assert data["count"] == 0


# ── Series baseline ──────────────────────────────────────────────────────


class TestSeriesBaseline:
    def test_baseline(self, series_with_data):
        status, data = _request("GET", series_with_data, "/series/baseline?series_id=brent_test&method=mean")
        assert status == 200
        assert data["mean"] is not None
        assert data["std"] is not None
        assert data["n_points"] == 5

    def test_baseline_empty(self, series_server):
        status, data = _request("GET", series_server, "/series/baseline?series_id=nonexistent")
        assert status == 200
        assert data["n_points"] == 0


# ── Series anomalies ──────────────────────────────────────────────────────


class TestSeriesAnomalies:
    def test_anomalies(self, series_with_data):
        status, data = _request("GET", series_with_data, "/series/anomalies?series_id=brent_test&method=mean&sigma=1.5")
        assert status == 200
        assert data["count"] >= 1

    def test_anomalies_requires_id(self, series_server):
        status, _ = _request("GET", series_server, "/series/anomalies")
        assert status == 400

    def test_anomalies_empty(self, series_server):
        status, data = _request("GET", series_server, "/series/anomalies?series_id=nonexistent")
        assert status == 200
        assert data["count"] == 0


# ── MCP tool schemas ──────────────────────────────────────────────────────


class TestMCPSchemas943:
    def test_series_tools_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        expected = {"ohm_series_query", "ohm_series_baseline", "ohm_series_anomalies"}
        assert expected.issubset(tool_names)

    def test_tool_count_72(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 72


# ── MCP dispatch ─────────────────────────────────────────────────────────


class TestMCPDispatch943:
    def test_dispatch_series_query(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_series_query", {"series_id": "s1"}, "test")
        assert m == "GET"
        assert "series_id=s1" in p

    def test_dispatch_series_baseline(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_series_baseline", {"series_id": "s1"}, "test")
        assert m == "GET"
        assert "series_id=s1" in p

    def test_dispatch_series_anomalies(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_series_anomalies", {"series_id": "s1", "sigma": 3.0}, "test")
        assert m == "GET"
        assert "series_id=s1" in p
        assert "sigma=3" in p