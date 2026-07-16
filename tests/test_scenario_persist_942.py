"""Tests for OHM-942 / Stage 5: Persist scenarios as graph objects with snapshots."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

from tests.conftest import _request, _start_test_server  # noqa: E402


@pytest.fixture
def scenario_server(tmp_path):
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "scenario_test.duckdb")
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
def target_node(scenario_server):
    status, data = _request("POST", scenario_server, "/node", {
        "id": "hormuz-and-gate",
        "label": "Hormuz AND-gate",
        "type": "concept",
    })
    assert status == 201
    return data


@pytest.fixture
def sample_scenario(scenario_server, target_node):
    """Create a persisted scenario."""
    status, data = _request("POST", scenario_server, "/scenario/run", {
        "node_id": target_node["id"],
        "failure_probability": 1.0,
        "max_depth": 5,
        "compare": True,
        "persist": True,
        "label": "Hormuz partial closure scenario",
    })
    assert status == 200
    assert "scenario_node_id" in data
    return data


# ── Scenario run with persistence ────────────────────────────────────────


class TestScenarioPersist:
    def test_scenario_run_persist_creates_node(self, scenario_server, target_node):
        status, data = _request("POST", scenario_server, "/scenario/run", {
            "node_id": target_node["id"],
            "failure_probability": 1.0,
            "max_depth": 5,
            "compare": True,
            "persist": True,
            "label": "Test scenario",
        })
        assert status == 200
        assert "scenario_node_id" in data

    def test_scenario_run_no_persist(self, scenario_server, target_node):
        status, data = _request("POST", scenario_server, "/scenario/run", {
            "node_id": target_node["id"],
            "persist": False,
        })
        assert status == 200
        assert "scenario_node_id" not in data


# ── Scenario list ────────────────────────────────────────────────────────


class TestScenarioList:
    def test_list_scenarios_empty(self, scenario_server):
        status, data = _request("GET", scenario_server, "/scenarios")
        assert status == 200
        assert "scenarios" in data

    def test_list_scenarios_after_persist(self, scenario_server, sample_scenario):
        status, data = _request("GET", scenario_server, "/scenarios")
        assert status == 200
        assert data["count"] >= 1


# ── Scenario get ─────────────────────────────────────────────────────────


class TestScenarioGet:
    def test_get_scenario(self, scenario_server, sample_scenario):
        sid = sample_scenario["scenario_node_id"]
        status, data = _request("GET", scenario_server, f"/scenario/{sid}")
        assert status == 200
        assert data["id"] == sid
        assert data["type"] == "scenario"

    def test_get_nonexistent(self, scenario_server):
        status, _ = _request("GET", scenario_server, "/scenario/nonexistent-xyz")
        assert status == 404


# ── Scenario rerun ───────────────────────────────────────────────────────


class TestScenarioRerun:
    def test_rerun_scenario(self, scenario_server, sample_scenario):
        sid = sample_scenario["scenario_node_id"]
        status, data = _request("POST", scenario_server, "/scenario/rerun", {
            "scenario_id": sid,
        })
        assert status == 200
        assert "deltas" in data

    def test_rerun_requires_scenario_id(self, scenario_server):
        status, _ = _request("POST", scenario_server, "/scenario/rerun", {})
        assert status == 400


# ── Scenario diff ─────────────────────────────────────────────────────────


class TestScenarioDiff:
    def test_diff_scenario(self, scenario_server, sample_scenario):
        sid = sample_scenario["scenario_node_id"]
        status, data = _request("GET", scenario_server, f"/scenario/{sid}/diff")
        assert status == 200
        assert "scenario_id" in data


# ── MCP tool schemas ─────────────────────────────────────────────────────


class TestMCPSchemas942:
    def test_scenario_tools_present(self):
        from ohm.mcp.tools import all_tools
        tool_names = {t.name for t in all_tools()}
        expected = {"ohm_scenario_get", "ohm_scenario_rerun", "ohm_scenario_diff"}
        assert expected.issubset(tool_names)

    def test_tool_count_72(self):
        from ohm.mcp.tools import all_tools
        assert len(all_tools()) == 72


# ── MCP dispatch ─────────────────────────────────────────────────────────


class TestMCPDispatch942:
    def test_dispatch_scenario_get(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_scenario_get", {"scenario_id": "s1"}, "test")
        assert m == "GET"
        assert p == "/scenario/s1"

    def test_dispatch_scenario_rerun(self):
        from ohm.mcp.dispatch import build_request
        m, p, b = build_request("ohm_scenario_rerun", {"scenario_id": "s1"}, "test")
        assert m == "POST"
        assert p == "/scenario/rerun"

    def test_dispatch_scenario_diff(self):
        from ohm.mcp.dispatch import build_request
        m, p, _ = build_request("ohm_scenario_diff", {"scenario_id": "s1"}, "test")
        assert m == "GET"
        assert "s1" in p
        assert "diff" in p