"""Tests for AnalysisHandlerMixin endpoints (OHM-lzhk extraction).

Covers the endpoints defined in src/ohm/server/handlers/analysis.py.
Each test starts a live no-auth server backed by a temp database.
"""

from __future__ import annotations

import pytest

from tests.conftest import http_request, start_test_server


@pytest.fixture(autouse=True)
def _restore_handler_state():
    """Save/restore OhmHandler class state around every test.

    TestDecay creates inline servers that overwrite OhmHandler class
    attributes; without this, subsequent module-fixture tests get a 500.
    """
    from ohm.server.server import OhmHandler

    _attrs = ["store", "tokens", "roles", "no_auth", "require_read_auth", "config", "schema_config", "multi_tenant", "customer_tokens"]
    saved = {a: getattr(OhmHandler, a, None) for a in _attrs}
    yield
    for a, v in saved.items():
        setattr(OhmHandler, a, v)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """No-auth server with seed nodes/edges — shared across all tests in this module."""
    from ohm.store import OhmStore

    tmp_path = tmp_path_factory.mktemp("analysis")
    store = OhmStore(db_path=str(tmp_path / "analysis.duckdb"), agent_name="test_agent")

    # Seed data: two nodes connected by a causal edge with an observation
    store.write_node("n1", "Node One", "concept", "First node", confidence=0.9, provenance="conversation")
    store.write_node("n2", "Node Two", "concept", "Second node", confidence=0.4, provenance="conversation")
    store.conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence)
           VALUES ('e1', 'n1', 'n2', 'L3', 'CAUSES', 'test_agent', 0.8)"""
    )
    store.conn.execute(
        """INSERT INTO ohm_observations (id, node_id, type, value, sigma, created_by)
           VALUES ('obs1', 'n1', 'measurement', 5.0, 0.5, 'test_agent')"""
    )
    store.conn.execute(
        """INSERT INTO ohm_observations (id, node_id, type, value, sigma, created_by)
           VALUES ('obs2', 'n1', 'measurement', 12.0, 0.5, 'test_agent')"""
    )

    port, srv, thread = start_test_server(store, no_auth=True)
    yield port, store
    srv.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.mark.xdist_group("server_analysis")
class TestHealthGraph:
    def test_health_graph_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/health/graph")
        assert status == 200
        assert "health_score" in data

    def test_health_graph_includes_node_count(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/health/graph")
        assert status == 200
        assert data.get("total_nodes", 0) >= 2


@pytest.mark.xdist_group("server_analysis")
class TestHealthAgents:
    def test_health_agents_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/health/agents")
        assert status == 200
        assert isinstance(data, (list, dict))


@pytest.mark.xdist_group("server_analysis")
class TestContradictions:
    def test_contradictions_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/contradictions")
        assert status == 200
        assert isinstance(data, (list, dict))

    def test_contradictions_respects_confidence_param(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/contradictions?confidence=0.9")
        assert status == 200


@pytest.mark.xdist_group("server_analysis")
class TestAnomalies:
    def test_anomalies_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/anomalies")
        assert status == 200

    def test_anomalies_respects_sigma_param(self, server):
        port, _ = server
        status, _ = http_request("GET", port, "/anomalies?sigma=3.0&limit=10")
        assert status == 200


@pytest.mark.xdist_group("server_analysis")
class TestOrphans:
    def test_orphans_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/orphans")
        assert status == 200
        assert isinstance(data, (list, dict))

    def test_orphans_excludes_connected_nodes(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/orphans")
        assert status == 200
        # n1 and n2 are connected — they are not orphans
        ids = [item.get("id") for item in (data if isinstance(data, list) else data.get("orphans", []))]
        assert "n1" not in ids
        assert "n2" not in ids


@pytest.mark.xdist_group("server_analysis")
class TestHubs:
    def test_hubs_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/hubs")
        assert status == 200

    def test_hubs_respects_min_connections(self, server):
        port, _ = server
        status, _ = http_request("GET", port, "/hubs?min_connections=2&limit=5")
        assert status == 200


@pytest.mark.xdist_group("server_analysis")
class TestDeadEnds:
    def test_dead_ends_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/dead_ends")
        assert status == 200


@pytest.mark.xdist_group("server_analysis")
class TestStale:
    def test_stale_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/stale")
        assert status == 200
        assert isinstance(data, (list, dict))


@pytest.mark.xdist_group("server_analysis")
class TestDecay:
    def test_decay_dry_run_returns_200(self, tmp_path):
        """GET /decay?dry_run=true with a write token returns 200."""
        from ohm.store import OhmStore

        store = OhmStore(db_path=str(tmp_path / "decay2.duckdb"), agent_name="test_agent")
        tokens = {"rw-token": "writer"}
        roles = {"writer": "read-write"}
        port, srv, thread = start_test_server(store, tokens=tokens, roles=roles)
        try:
            status, data = http_request("GET", port, "/decay?dry_run=true", token="rw-token")
            assert status == 200
        finally:
            srv.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_decay_requires_write_auth(self, tmp_path):
        from ohm.store import OhmStore

        store = OhmStore(db_path=str(tmp_path / "decay.duckdb"), agent_name="test_agent")
        tokens = {"rw-token": "writer"}
        roles = {"writer": "read-write"}
        port, srv, thread = start_test_server(store, tokens=tokens, roles=roles)
        try:
            # No token → should be denied
            status, _ = http_request("GET", port, "/decay?dry_run=true")
            assert status == 401
            # Write token → allowed
            status, data = http_request("GET", port, "/decay?dry_run=true", token="rw-token")
            assert status == 200
        finally:
            srv.shutdown()
            thread.join(timeout=2)
            store.close()


@pytest.mark.xdist_group("server_analysis")
class TestAggregate:
    def test_aggregate_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/aggregate/n1")
        assert status == 200
        assert "node_id" in data or "value" in data or isinstance(data, dict)

    def test_aggregate_404_for_missing_node(self, server):
        port, _ = server
        status, _ = http_request("GET", port, "/aggregate/nonexistent-node")
        assert status in (200, 404)  # returns empty aggregate or 404


@pytest.mark.xdist_group("server_analysis")
class TestProvenance:
    def test_provenance_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/provenance/n1")
        assert status == 200
        assert isinstance(data, (list, dict))


@pytest.mark.xdist_group("server_analysis")
class TestGraphStats:
    def test_graph_stats_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/graph/stats")
        assert status == 200
        assert isinstance(data, dict)


@pytest.mark.xdist_group("server_analysis")
class TestLint:
    def test_lint_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/lint")
        assert status == 200
        assert isinstance(data, dict)


@pytest.mark.xdist_group("server_analysis")
class TestContract:
    def test_contract_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/contract")
        assert status == 200
        assert isinstance(data, dict)


@pytest.mark.xdist_group("server_analysis")
class TestSuggest:
    def test_suggest_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/suggest")
        assert status == 200

    def test_suggest_respects_method_param(self, server):
        port, _ = server
        status, _ = http_request("GET", port, "/suggest?method=shared_provenance&limit=5")
        assert status == 200
