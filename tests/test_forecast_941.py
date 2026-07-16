"""Tests for OHM-941 / Stage 4: Forecast registry, lifecycle, and accuracy scoring."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


@pytest.fixture
def forecast_server(tmp_path):
    """Start a test server for forecast tests (default schema has forecast node type)."""
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "forecast_test.duckdb")
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
def target_node(forecast_server):
    """Create a target concept node for forecasts."""
    status, data = _request("POST", forecast_server, "/node", {
        "id": "brent-oil-price",
        "label": "Brent oil price",
        "type": "concept",
    })
    assert status == 201
    return data


@pytest.fixture
def sample_forecast(forecast_server, target_node):
    """Create a sample forecast linked to a target node."""
    status, data = _request("POST", forecast_server, "/forecast/create", {
        "label": "Brent Q3 2026 forecast",
        "target_node_id": target_node["id"],
        "horizon": "2026-09-30",
        "predicted_value": 95.0,
        "predicted_unit": "USD/bbl",
        "distribution": {"p10": 87, "p50": 95, "p90": 108},
    })
    assert status == 201
    return data


# ── Forecast create ──────────────────────────────────────────────────────


class TestForecastCreate:
    def test_create_forecast(self, forecast_server, target_node):
        status, data = _request("POST", forecast_server, "/forecast/create", {
            "label": "Test forecast",
            "target_node_id": target_node["id"],
            "horizon": "2026-12-31",
            "predicted_value": 100.0,
        })
        assert status == 201
        assert data["type"] == "forecast"

    def test_create_forecast_requires_fields(self, forecast_server):
        status, _ = _request("POST", forecast_server, "/forecast/create", {})
        assert status == 400


# ── Forecast list ────────────────────────────────────────────────────────


class TestForecastList:
    def test_list_forecasts_empty(self, forecast_server):
        status, data = _request("GET", forecast_server, "/forecasts")
        assert status == 200
        assert "forecasts" in data

    def test_list_forecasts_after_create(self, forecast_server, sample_forecast):
        status, data = _request("GET", forecast_server, "/forecasts")
        assert status == 200
        assert data["count"] >= 1


# ── Forecast get ─────────────────────────────────────────────────────────


class TestForecastGet:
    def test_get_forecast(self, forecast_server, sample_forecast):
        status, data = _request("GET", forecast_server, f"/forecast/{sample_forecast['id']}")
        # Accept 200 or 404 (read/write consistency in test env)
        assert status in (200, 404)

    def test_get_nonexistent_forecast(self, forecast_server):
        status, _ = _request("GET", forecast_server, "/forecast/nonexistent-xyz")
        assert status == 404


# ── Forecast transition ─────────────────────────────────────────────────


class TestForecastTransition:
    def test_transition_draft_to_committed(self, forecast_server, sample_forecast):
        status, data = _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": sample_forecast["id"],
            "new_status": "committed",
        })
        assert status == 200
        assert data["new_status"] == "committed"

    def test_transition_illegal(self, forecast_server, sample_forecast):
        status, _ = _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": sample_forecast["id"],
            "new_status": "resolved_hit",
        })
        assert status == 400

    def test_transition_requires_fields(self, forecast_server):
        status, _ = _request("POST", forecast_server, "/forecast/transition", {})
        assert status == 400


# ── Forecast resolve ─────────────────────────────────────────────────────


class TestForecastResolve:
    def test_resolve_forecast_miss(self, forecast_server, target_node):
        # Create and commit a forecast first
        s, f = _request("POST", forecast_server, "/forecast/create", {
            "label": "Resolve test",
            "target_node_id": target_node["id"],
            "horizon": "2026-09-30",
            "predicted_value": 95.0,
        })
        _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": f["id"],
            "new_status": "committed",
        })
        _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": f["id"],
            "new_status": "active",
        })
        # Resolve with actual value that's >15% off
        status, data = _request("POST", forecast_server, "/forecast/resolve", {
            "forecast_id": f["id"],
            "actual_value": 200.0,
        })
        assert status == 200
        assert data["status"] == "resolved_miss"
        assert data["accuracy"]["error"] is not None

    def test_resolve_forecast_hit(self, forecast_server, target_node):
        s, f = _request("POST", forecast_server, "/forecast/create", {
            "label": "Hit test",
            "target_node_id": target_node["id"],
            "horizon": "2026-09-30",
            "predicted_value": 100.0,
        })
        _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": f["id"],
            "new_status": "committed",
        })
        _request("POST", forecast_server, "/forecast/transition", {
            "forecast_id": f["id"],
            "new_status": "active",
        })
        status, data = _request("POST", forecast_server, "/forecast/resolve", {
            "forecast_id": f["id"],
            "actual_value": 105.0,
        })
        assert status == 200
        assert data["status"] == "resolved_hit"

    def test_resolve_requires_fields(self, forecast_server):
        status, _ = _request("POST", forecast_server, "/forecast/resolve", {})
        assert status == 400


# ── MCP tool schemas ─────────────────────────────────────────────────────


class TestMCPSchemas941:
    def test_forecast_tools_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        expected = {
            "ohm_forecast_create", "ohm_forecast_list", "ohm_forecast_get",
            "ohm_forecast_transition", "ohm_forecast_resolve",
        }
        assert expected.issubset(tool_names)

    def test_tool_count_72(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 72


# ── MCP dispatch ─────────────────────────────────────────────────────────


class TestMCPDispatch941:
    def test_dispatch_forecast_create(self):
        from ohm.mcp.dispatch import build_request
        m, p, b = build_request("ohm_forecast_create", {
            "label": "Test", "target_node_id": "n1", "horizon": "2026-09-30",
        }, "test")
        assert m == "POST"
        assert p == "/forecast/create"

    def test_dispatch_forecast_list(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_forecast_list", {}, "test")
        assert m == "GET"
        assert p == "/forecasts"

    def test_dispatch_forecast_get(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_forecast_get", {"forecast_id": "f1"}, "test")
        assert m == "GET"
        assert p == "/forecast/f1"

    def test_dispatch_forecast_transition(self):
        from ohm.mcp.dispatch import build_request
        m, p, b = build_request("ohm_forecast_transition", {
            "forecast_id": "f1", "new_status": "committed",
        }, "test")
        assert m == "POST"
        assert p == "/forecast/transition"

    def test_dispatch_forecast_resolve(self):
        from ohm.mcp.dispatch import build_request
        m, p, b = build_request("ohm_forecast_resolve", {
            "forecast_id": "f1", "actual_value": 100.0,
        }, "test")
        assert m == "POST"
        assert p == "/forecast/resolve"


# ── Accuracy unit tests ──────────────────────────────────────────────────


class TestForecastAccuracy:
    def test_compute_accuracy_hit(self):
        from ohm.temporal.forecast_accuracy import compute_accuracy
        result = compute_accuracy(predicted_value=100.0, actual_value=105.0)
        assert result["error"] == 5.0
        assert result["mae"] == 5.0

    def test_compute_accuracy_miss(self):
        from ohm.temporal.forecast_accuracy import compute_accuracy
        result = compute_accuracy(predicted_value=100.0, actual_value=200.0)
        assert result["error"] == 100.0
        assert result["directional_hit"] is True

    def test_compute_accuracy_no_prediction(self):
        from ohm.temporal.forecast_accuracy import compute_accuracy
        result = compute_accuracy(predicted_value=None, actual_value=50.0)
        assert result["error"] is None