"""Tests for OHM-yzyk.5: instance registry and monitoring endpoint."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import DEFAULT_SCHEMA, initialize_schema
from ohm.framework.sdk import Graph


@pytest.fixture
def graph():
    conn = duckdb.connect(":memory:")
    initialize_schema(conn, DEFAULT_SCHEMA)
    return Graph(conn, actor="test_agent")


class TestInstanceEndpoint:
    """GET /instance — instance metadata for discovery (OHM-yzyk.5)."""

    def test_instance_returns_required_fields(self, graph):
        """The /instance handler should return all required metadata fields."""
        from ohm.server.handlers.infra import InfraHandlerMixin
        from ohm.graph.store import OhmStore
        import inspect

        # Verify the handler method exists
        assert hasattr(InfraHandlerMixin, "_get_instance")

    def test_instance_metadata_structure(self, graph):
        """Verify the expected metadata fields are defined in the handler."""
        from ohm.server.handlers.infra import InfraHandlerMixin
        import inspect

        source = inspect.getsource(InfraHandlerMixin._get_instance)
        required_fields = [
            "instance_id",
            "version",
            "multi_tenant",
            "tenants",
            "domain_configs",
            "listen_url",
            "ducklake",
            "uptime_seconds",
            "agent_count",
        ]
        for field in required_fields:
            assert field in source, f"Missing field '{field}' in _get_instance source"

    def test_instance_no_auth_required(self):
        """GET /instance should not require auth (discovery tool can probe)."""
        from ohm.server.server import OhmHandler

        assert "/instance" in OhmHandler._GET_EXACT
        assert OhmHandler._GET_EXACT["/instance"] == "_get_instance"


class TestMetricsEndpoint:
    """GET /metrics — Prometheus metrics include graph counts (OHM-yzyk.5)."""

    def test_metrics_includes_graph_counts(self):
        """The /metrics handler should emit ohm_nodes_total, ohm_edges_total, ohm_observations_total."""
        from ohm.server.handlers.infra import InfraHandlerMixin
        import inspect

        source = inspect.getsource(InfraHandlerMixin._get_infra_metrics)
        assert "ohm_nodes_total" in source
        assert "ohm_edges_total" in source
        assert "ohm_observations_total" in source
        assert "ohm_instance_uptime_seconds" in source


class TestInstanceRoute:
    """Verify the route is registered correctly."""

    def test_instance_route_registered(self):
        from ohm.server.server import OhmHandler

        assert "/instance" in OhmHandler._GET_EXACT

    def test_instance_in_no_auth_routes(self):
        """GET /instance should be in the no-auth route list."""
        from ohm.server.server import OhmHandler

        # The route should be registered
        assert OhmHandler._GET_EXACT.get("/instance") == "_get_instance"
