"""Unit tests for HttpGraph SDK parity wrappers.

These tests do not start a daemon. They instantiate connect_http() with a
dummy in-memory DuckDB connection and patch _http_request to record the
constructed URLs/bodies. This verifies that the SDK parity methods map to
the expected HTTP endpoints and query parameters.
"""

from __future__ import annotations

import pytest

from ohm.framework.sdk import connect_http

pytestmark = pytest.mark.integration


@pytest.fixture
def graph():
    """Return an HttpGraph instance with a recording HTTP transport."""
    g = connect_http("http://test.ohm", actor="tester", token="test-token")
    g._calls: list[tuple[str, str, dict | None]] = []

    def _record(method: str, path: str, body: dict | None = None) -> dict:
        g._calls.append((method, path, body))
        return {"ok": True}

    g._http_request = _record
    return g


def test_voi_url(graph):
    graph.voi(decision=["d1", "d2"], top=5, layers=["L2", "L3"])
    method, path, body = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/voi?")
    assert "decision=d1%2Cd2" in path
    assert "top=5" in path
    assert "layers=L2%2CL3" in path


def test_voi_tasks_url(graph):
    graph.voi_tasks(agent="metis", decision=["d1"])
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/voi/tasks?")
    assert "agent=metis" in path
    assert "decision=d1" in path


def test_regime_url(graph):
    graph.regime("target_a", evidence={"x": 1, "y": 0.7}, window_days=14)
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/regime?")
    assert "target=target_a" in path
    assert "evidence=x%3A1%2Cy%3A0.7" in path
    assert "window_days=14" in path


def test_game_url(graph):
    graph.game("target_a", players=["p1", "p2"], layers=["L3"])
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/game?")
    assert "target=target_a" in path
    assert "players=p1%2Cp2" in path


def test_nash_url(graph):
    graph.nash(["p1", "p2"], [[[[1, 0], [0, 1]], [[0, 1], [1, 0]]]])
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/nash?")
    assert "players=p1%2Cp2" in path
    assert "payoffs=" in path


def test_policy_url(graph):
    graph.policy("target_a", observation_cost=0.1, horizon=2)
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/policy?")
    assert "target=target_a" in path
    assert "observation_cost=0.1" in path
    assert "horizon=2" in path


def test_discover_url(graph):
    graph.discover(["a", "b"], method="pc", alpha=0.01, queue=True)
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/discover?")
    assert "nodes=a%2Cb" in path
    assert "method=pc" in path
    assert "alpha=0.01" in path
    assert "queue=true" in path


def test_discovery_queue_url(graph):
    graph.discovery_queue(status="pending", limit=50)
    method, path, _ = graph._calls[-1]
    assert method == "GET"
    assert path.startswith("/discover/queue?")
    assert "status=pending" in path
    assert "limit=50" in path


def test_review_discovery_body(graph):
    graph.review_discovery("q-1", "accept", reviewed_by="tester", review_notes="looks good")
    method, path, body = graph._calls[-1]
    assert method == "POST"
    assert path == "/discover/queue/review"
    assert body["queue_id"] == "q-1"
    assert body["action"] == "accept"
    assert body["reviewed_by"] == "tester"
    assert body["review_notes"] == "looks good"
    assert body["edge_layer"] == "L3"
