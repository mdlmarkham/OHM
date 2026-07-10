"""Tests for OHM-765: ohm_belief MCP tool and /belief endpoint."""

from __future__ import annotations

import pytest

from ohm.mcp.tools import all_tools
from ohm.mcp.dispatch import build_request


class TestOhmBeliefTool:
    """Test ohm_belief tool definition and dispatch."""

    def test_ohm_belief_in_tool_list(self):
        """ohm_belief appears in all_tools()."""
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_belief" in names

    def test_ohm_belief_schema_has_target(self):
        """ohm_belief inputSchema requires target."""
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        assert "target" in tool.inputSchema["required"]
        assert "target" in tool.inputSchema["properties"]

    def test_ohm_belief_schema_has_optional_fields(self):
        """ohm_belief has optional evidence, layers, leak, format."""
        tool = next(t for t in all_tools() if t.name == "ohm_belief")
        props = tool.inputSchema["properties"]
        assert "evidence" in props
        assert "layers" in props
        assert "leak" in props
        assert "format" in props

    def test_ohm_belief_dispatch_builds_request(self):
        """build_request returns GET /belief with target."""
        method, path, body = build_request(
            "ohm_belief",
            {"target": "concept-hormuz-and-gate"},
            "test-agent",
        )
        assert method == "GET"
        assert "/belief" in path
        assert "target=concept-hormuz-and-gate" in path or "target=concept-hormuz" in path
        assert body is None

    def test_ohm_belief_dispatch_with_evidence(self):
        """build_request passes evidence as query param."""
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "evidence": "node-2:1,node-3:0.7"},
            "test-agent",
        )
        assert method == "GET"
        assert "/belief" in path
        assert "evidence=" in path

    def test_ohm_belief_dispatch_with_layers_and_leak(self):
        """build_request passes layers and leak as query params."""
        method, path, body = build_request(
            "ohm_belief",
            {"target": "node-1", "layers": "L3,L4", "leak": 0.2},
            "test-agent",
        )
        assert method == "GET"
        assert "/belief" in path
        assert "layers=" in path
        assert "leak=0.2" in path

    def test_ohm_belief_not_in_write_tools(self):
        """ohm_belief is a read-only tool (not in WRITE_TOOLS)."""
        from ohm.mcp.config import WRITE_TOOLS

        assert "ohm_belief" not in WRITE_TOOLS


class TestStatisticalTools:
    """Test ohm_pert, ohm_monte_carlo, ohm_markov, ohm_game tool definitions and dispatch (OHM-788)."""

    def test_ohm_pert_in_tool_list(self):
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_pert" in names

    def test_ohm_monte_carlo_in_tool_list(self):
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_monte_carlo" in names

    def test_ohm_markov_in_tool_list(self):
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_markov" in names

    def test_ohm_game_in_tool_list(self):
        tools = all_tools()
        names = [t.name for t in tools]
        assert "ohm_game" in names

    def test_ohm_pert_dispatch(self):
        method, path, body = build_request("ohm_pert", {"target": "node-1"}, "test")
        assert method == "GET"
        assert "/inference" in path
        assert "pert=1" in path
        assert body is None

    def test_ohm_monte_carlo_dispatch(self):
        method, path, body = build_request("ohm_monte_carlo", {"target": "node-1", "n_simulations": 500}, "test")
        assert method == "GET"
        assert "/monte-carlo/node-1" in path
        assert "n_simulations=500" in path

    def test_ohm_markov_dispatch_absorbing(self):
        method, path, body = build_request("ohm_markov", {"target": "node-1", "analysis": "absorbing"}, "test")
        assert method == "GET"
        assert "/markov/absorbing" in path

    def test_ohm_markov_dispatch_expected_steps(self):
        method, path, body = build_request("ohm_markov", {"target": "node-1", "analysis": "expected_steps"}, "test")
        assert method == "GET"
        assert "/markov/expected_steps" in path

    def test_ohm_game_dispatch(self):
        method, path, body = build_request("ohm_game", {"target": "node-1"}, "test")
        assert method == "GET"
        assert "/game" in path

    def test_statistical_tools_not_in_write_tools(self):
        from ohm.mcp.config import WRITE_TOOLS

        assert "ohm_pert" not in WRITE_TOOLS
        assert "ohm_monte_carlo" not in WRITE_TOOLS
        assert "ohm_markov" not in WRITE_TOOLS
        assert "ohm_game" not in WRITE_TOOLS


class TestBeliefEndpoint:
    """Test the /belief server endpoint."""

    def test_belief_returns_posterior_and_drivers(self, test_server):
        """GET /belief returns posterior, drivers, and VoI suggestions."""
        port, store = test_server

        # Create a simple causal graph: cause → target
        from ohm.graph.queries import create_node

        cause = create_node(store.conn, label="Cause Event", node_type="event", created_by="test")
        target = create_node(store.conn, label="Target Event", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.8, agent_name="test")

        from tests.conftest import _request

        status, data = _request("GET", port, f"/belief?target={target['id']}")
        assert status == 200, f"Expected 200, got {status}: {data}"
        assert data["target"] == target["id"]
        assert "P(bad)" in data["posterior"]
        assert "P(good)" in data["posterior"]
        assert "entropy_bits" in data["posterior"]
        assert "summary" in data
        assert "drivers" in data["why"]
        assert "suggested_observations" in data["what_to_do_next"]

    def test_belief_missing_target_returns_400(self, test_server):
        """GET /belief without target returns 400."""
        port, store = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/belief")
        assert status == 400
        assert "missing_parameter" in data.get("error", "")

    def test_belief_with_evidence(self, test_server):
        """GET /belief with evidence returns updated posterior."""
        port, store = test_server
        from ohm.graph.queries import create_node
        from tests.conftest import _request

        cause = create_node(store.conn, label="Evidence Event", node_type="event", created_by="test")
        target = create_node(store.conn, label="Effect Event", node_type="event", created_by="test")
        store.write_edge(cause["id"], target["id"], "CAUSES", "L3", confidence=0.9, agent_name="test")

        status, data = _request("GET", port, f"/belief?target={target['id']}&evidence={cause['id']}:1")
        assert status == 200
        assert data["target"] == target["id"]
        assert "P(bad)" in data["posterior"]
