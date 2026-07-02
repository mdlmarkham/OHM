"""Tests for OHM-lqpk.5: per-endpoint perf logging and /perf endpoint."""

from __future__ import annotations

import pytest

from tests.conftest import _request


class TestPerfEndpoint:
    """GET /perf returns per-endpoint latency breakdown."""

    def test_perf_returns_empty_initially(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/perf")
        assert status == 200
        assert "endpoints" in data
        assert isinstance(data["endpoints"], list)

    def test_perf_records_get_request(self, test_server):
        port, _ = test_server
        _request("GET", port, "/stats")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        endpoints = data["endpoints"]
        ep_keys = [e["endpoint"] for e in endpoints]
        assert any("/stats" in k for k in ep_keys)

    def test_perf_records_post_request(self, test_server):
        port, _ = test_server
        # Use a second GET endpoint to test multiple distinct endpoints
        _request("GET", port, "/health")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        endpoints = data["endpoints"]
        ep_keys = [e["endpoint"] for e in endpoints]
        assert any("/health" in k for k in ep_keys)

    def test_perf_has_percentiles(self, test_server):
        port, _ = test_server
        _request("GET", port, "/stats")
        _request("GET", port, "/stats")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        stats_ep = [e for e in data["endpoints"] if "/stats" in e["endpoint"]]
        if stats_ep:
            ep = stats_ep[0]
            assert "p50_ms" in ep
            assert "p95_ms" in ep
            assert "p99_ms" in ep
            assert "count" in ep
            assert "mean_ms" in ep
            assert ep["count"] >= 2

    def test_perf_sorted_by_total_time(self, test_server):
        port, _ = test_server
        for _ in range(5):
            _request("GET", port, "/stats")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        if len(data["endpoints"]) > 1:
            totals = [e["total_time_ms"] for e in data["endpoints"]]
            assert totals == sorted(totals, reverse=True)

    def test_perf_reports_perf_log_status(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/perf")
        assert status == 200
        assert "perf_log_enabled" in data
        assert isinstance(data["perf_log_enabled"], bool)

    def test_perf_endpoint_count(self, test_server):
        port, _ = test_server
        _request("GET", port, "/stats")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        assert data["endpoint_count"] >= 1

    def test_perf_multiple_different_endpoints(self, test_server):
        port, _ = test_server
        _request("GET", port, "/stats")
        _request("GET", port, "/health")
        status, data = _request("GET", port, "/perf")
        assert status == 200
        ep_keys = [e["endpoint"] for e in data["endpoints"]]
        assert any("/stats" in k for k in ep_keys)
        assert any("/health" in k for k in ep_keys)
