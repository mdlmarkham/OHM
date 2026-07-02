"""HTTP regression tests for per-agent read scope enforcement (OHM-oqyc, ADR-037).

Verifies that agents with restricted read scopes cannot access data
through HTTP endpoints that they are not permitted to see. Tests scope
leakage through search, neighborhood, and single-node fetches.
"""

from __future__ import annotations

import pytest

from tests.conftest import _request


def _create_node_direct(store, node_id, label, created_by="metis", source_tier=None, node_type="concept"):
    store.write_node(node_id, label, node_type, source_tier=source_tier, agent_name=created_by, confidence=0.2 if source_tier == "raw" else 1.0)


def _create_edge_direct(store, from_id, to_id, edge_type="CAUSES", layer="L3", created_by="metis"):
    from ohm.graph.queries import create_edge

    create_edge(store.conn, from_node=from_id, to_node=to_id, edge_type=edge_type, layer=layer, created_by=created_by)


def _set_scope(store, agent, scope):
    from ohm.server.boundary import set_agent_read_scope

    set_agent_read_scope(store.conn, agent, scope)


class TestSingleNodeScope:
    """GET /node/<id> enforces read scope."""

    def test_no_scope_sees_all(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "n1", "Public", created_by="metis")
        _create_node_direct(store, "n2", "Other", created_by="observer")
        status, _ = _request("GET", port, "/node/n1", token="test-token-abc")
        assert status == 200
        status, _ = _request("GET", port, "/node/n2", token="test-token-abc")
        assert status == 200

    def test_created_by_scope_blocks_others(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "obs_node", "Observer Node", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, data = _request("GET", port, "/node/obs_node", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"

    def test_created_by_scope_allows_own(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"created_by": ["metis"]})
        _create_node_direct(store, "metis_node", "My Node", created_by="metis")
        status, data = _request("GET", port, "/node/metis_node", token="test-token-abc")
        assert status == 200
        assert data["id"] == "metis_node"

    def test_scope_cleared_restores_access(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "other", "Other", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/node/other", token="test-token-abc")
        assert status == 403
        _set_scope(store, "metis", None)
        status, _ = _request("GET", port, "/node/other", token="test-token-abc")
        assert status == 200

    def test_source_tier_scope_blocks(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "raw_n", "Raw Node", created_by="metis", source_tier="raw")
        _set_scope(store, "metis", {"source_tier": ["verified", "official"]})
        status, data = _request("GET", port, "/node/raw_n", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"

    def test_source_tier_scope_allows(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "ver_n", "Verified Node", created_by="metis", source_tier="verified")
        _set_scope(store, "metis", {"source_tier": ["verified", "official"]})
        status, _ = _request("GET", port, "/node/ver_n", token="test-token-abc")
        assert status == 200


class TestSearchScopeLeakage:
    """GET /search enforces read scope at the SQL level."""

    def test_no_scope_returns_all(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "n1", "alpha metis", created_by="metis")
        _create_node_direct(store, "n2", "alpha observer", created_by="observer")
        status, data = _request("GET", port, "/search?q=alpha", token="test-token-abc")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", [])
        ids = {r.get("id") for r in results}
        assert "n1" in ids
        assert "n2" in ids

    def test_created_by_scope_filters_search(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "metis_a", "alpha metis", created_by="metis")
        _create_node_direct(store, "observer_b", "alpha observer", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, data = _request("GET", port, "/search?q=alpha", token="test-token-abc")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", [])
        ids = {r.get("id") for r in results}
        assert "metis_a" in ids
        assert "observer_b" not in ids

    def test_source_tier_scope_filters_search(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "verified_n", "alpha verified", created_by="metis", source_tier="verified")
        _create_node_direct(store, "raw_n", "alpha raw", created_by="metis", source_tier="raw")
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/search?q=alpha", token="test-token-abc")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", [])
        ids = {r.get("id") for r in results}
        assert "verified_n" in ids
        assert "raw_n" not in ids

    def test_scope_removed_restores_search(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "n_a", "alpha public", created_by="metis")
        _create_node_direct(store, "n_b", "alpha private", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, data = _request("GET", port, "/search?q=alpha", token="test-token-abc")
        results = data if isinstance(data, list) else data.get("results", [])
        assert len(results) == 1
        _set_scope(store, "metis", None)
        status, data = _request("GET", port, "/search?q=alpha", token="test-token-abc")
        results = data if isinstance(data, list) else data.get("results", [])
        assert len(results) >= 2


class TestNeighborhoodScopeLeakage:
    """GET /neighborhood/<id> enforces read scope on the root node."""

    def test_no_scope_works(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "root", "Root", created_by="metis")
        _create_node_direct(store, "child", "Child", created_by="metis")
        _create_edge_direct(store, "root", "child")
        status, data = _request("GET", port, "/neighborhood/root", token="test-token-abc")
        assert status == 200
        assert "nodes" in data
        assert "edges" in data

    def test_blocked_for_scoped_agent(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "other_root", "Other Root", created_by="observer")
        _create_node_direct(store, "other_child", "Other Child", created_by="observer")
        _create_edge_direct(store, "other_root", "other_child", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, data = _request("GET", port, "/neighborhood/other_root", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"

    def test_allowed_for_own_root(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"created_by": ["metis"]})
        _create_node_direct(store, "my_root", "My Root", created_by="metis")
        _create_node_direct(store, "my_child", "My Child", created_by="metis")
        _create_edge_direct(store, "my_root", "my_child")
        status, data = _request("GET", port, "/neighborhood/my_root", token="test-token-abc")
        assert status == 200


class TestNoScopeFullAccess:
    """Agents without a read scope have full access (backward compat)."""

    def test_no_scope_sees_all_nodes(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "n1", "Node 1", created_by="metis")
        _create_node_direct(store, "n2", "Node 2", created_by="observer")
        status, _ = _request("GET", port, "/node/n1", token="test-token-abc")
        assert status == 200
        status, _ = _request("GET", port, "/node/n2", token="test-token-abc")
        assert status == 200

    def test_no_scope_search_returns_all(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "n1", "shared word", created_by="metis")
        _create_node_direct(store, "n2", "shared word", created_by="observer")
        status, data = _request("GET", port, "/search?q=shared", token="test-token-abc")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", [])
        assert len(results) >= 2
