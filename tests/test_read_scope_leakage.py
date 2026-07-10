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

    return create_edge(store.conn, from_node=from_id, to_node=to_id, edge_type=edge_type, layer=layer, created_by=created_by)


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

    def test_neighbor_nodes_filtered_for_scoped_agent(self, auth_server):
        """Edges pointing to nodes outside the agent's scope must not leak those nodes."""
        port, store = auth_server
        _set_scope(store, "metis", {"created_by": ["metis"]})
        _create_node_direct(store, "my_root", "My Root", created_by="metis")
        _create_node_direct(store, "my_child", "My Child", created_by="metis")
        _create_node_direct(store, "obs_child", "Observer Child", created_by="observer")
        _create_edge_direct(store, "my_root", "my_child", created_by="metis")
        _create_edge_direct(store, "my_root", "obs_child", created_by="metis")
        status, data = _request("GET", port, "/neighborhood/my_root", token="test-token-abc")
        assert status == 200
        node_ids = {n["id"] for n in data.get("nodes", [])}
        edge_pairs = {(e["from_node"], e["to_node"]) for e in data.get("edges", [])}
        assert "my_root" in node_ids
        assert "my_child" in node_ids
        assert "obs_child" not in node_ids
        assert ("my_root", "obs_child") not in edge_pairs


class TestEdgesScopeLeakage:
    """GET /edges and GET /edge/<id> enforce read scope on endpoints."""

    def test_edge_list_hides_cross_tier_edges(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        _create_node_direct(store, "ver_a", "Verified A", created_by="metis", source_tier="verified")
        _create_node_direct(store, "ver_b", "Verified B", created_by="metis", source_tier="verified")
        _create_node_direct(store, "raw_c", "Raw C", created_by="metis", source_tier="raw")
        _create_edge_direct(store, "ver_a", "ver_b", layer="L3", created_by="metis")
        _create_edge_direct(store, "ver_a", "raw_c", layer="L3", created_by="metis")
        status, data = _request("GET", port, "/edges?from_node=ver_a", token="test-token-abc")
        assert status == 200
        edges = data.get("edges", [])
        pairs = {(e["from_node"], e["to_node"]) for e in edges}
        assert ("ver_a", "ver_b") in pairs
        assert ("ver_a", "raw_c") not in pairs

    def test_single_edge_hides_inaccessible_endpoint(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        _create_node_direct(store, "ver_a", "Verified A", created_by="metis", source_tier="verified")
        _create_node_direct(store, "raw_c", "Raw C", created_by="metis", source_tier="raw")
        edge = _create_edge_direct(store, "ver_a", "raw_c", layer="L3", created_by="metis")
        status, data = _request("GET", port, f"/edge/{edge['id']}", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"

    def test_single_edge_allows_accessible_endpoints(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        _create_node_direct(store, "ver_a", "Verified A", created_by="metis", source_tier="verified")
        _create_node_direct(store, "ver_b", "Verified B", created_by="metis", source_tier="verified")
        edge = _create_edge_direct(store, "ver_a", "ver_b", layer="L3", created_by="metis")
        status, data = _request("GET", port, f"/edge/{edge['id']}", token="test-token-abc")
        assert status == 200
        assert data["id"] == edge["id"]


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


class TestTraversalScopeLeakage:
    """OHM-737: traversal endpoints enforce read scope on seeds and filter results.

    Covers /path, /impact, /confidence, /narrative, /lineage, /provenance.
    A scoped agent must not reach restricted nodes through traversal, and
    must not see restricted edges/nodes in the returned results.
    """

    def test_path_blocked_when_endpoint_out_of_scope(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "p_src", "Path Src", created_by="metis")
        _create_node_direct(store, "p_dst", "Path Dst", created_by="observer")
        _create_edge_direct(store, "p_src", "p_dst", created_by="metis")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/path/p_src/p_dst", token="test-token-abc")
        assert status == 403

    def test_path_works_when_endpoints_in_scope(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "p_a", "Path A", created_by="metis")
        _create_node_direct(store, "p_b", "Path B", created_by="metis")
        _create_edge_direct(store, "p_a", "p_b", created_by="metis")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, data = _request("GET", port, "/path/p_a/p_b", token="test-token-abc")
        assert status == 200

    def test_impact_blocked_for_out_of_scope_seed(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "imp_root", "Impact Root", created_by="observer")
        _create_node_direct(store, "imp_child", "Impact Child", created_by="observer")
        _create_edge_direct(store, "imp_root", "imp_child", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/impact/imp_root", token="test-token-abc")
        assert status == 403

    def test_impact_filters_cross_scope_edges(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"created_by": ["metis"]})
        _create_node_direct(store, "my_root", "My Root", created_by="metis")
        _create_node_direct(store, "my_child", "My Child", created_by="metis")
        _create_node_direct(store, "obs_child", "Obs Child", created_by="observer")
        _create_edge_direct(store, "my_root", "my_child", created_by="metis")
        _create_edge_direct(store, "my_root", "obs_child", created_by="metis")
        status, data = _request("GET", port, "/impact/my_root", token="test-token-abc")
        assert status == 200
        to_nodes = {e.get("to_node") for e in data} if isinstance(data, list) else set()
        assert "my_child" in to_nodes
        assert "obs_child" not in to_nodes

    def test_confidence_blocked_for_out_of_scope_node(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "conf_n", "Conf Node", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/confidence/conf_n", token="test-token-abc")
        assert status == 403

    def test_provenance_blocked_for_out_of_scope_seed(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "prov_n", "Prov Node", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/provenance/prov_n", token="test-token-abc")
        assert status == 403

    def test_provenance_filters_out_of_scope_sources(self, auth_server):
        port, store = auth_server
        _set_scope(store, "metis", {"created_by": ["metis"]})
        _create_node_direct(store, "prov_root", "Prov Root", created_by="metis")
        _create_node_direct(store, "prov_mine", "My Source", created_by="metis", node_type="source")
        _create_node_direct(store, "prov_obs", "Obs Source", created_by="observer", node_type="source")
        _create_edge_direct(store, "prov_root", "prov_mine", edge_type="DERIVES_FROM", layer="L2", created_by="metis")
        _create_edge_direct(store, "prov_root", "prov_obs", edge_type="DERIVES_FROM", layer="L2", created_by="metis")
        status, data = _request("GET", port, "/provenance/prov_root", token="test-token-abc")
        assert status == 200
        source_ids = {r.get("node_id") for r in data} if isinstance(data, list) else set()
        assert "prov_mine" in source_ids
        assert "prov_obs" not in source_ids

    def test_narrative_blocked_for_out_of_scope_seed(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "nar_n", "Narrative Node", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/narrative/nar_n", token="test-token-abc")
        assert status == 403

    def test_lineage_blocked_for_out_of_scope_seed(self, auth_server):
        port, store = auth_server
        _create_node_direct(store, "lin_n", "Lineage Node", created_by="observer")
        _set_scope(store, "metis", {"created_by": ["metis"]})
        status, _ = _request("GET", port, "/lineage/lin_n", token="test-token-abc")
        assert status == 403

    def test_traversal_unscoped_works(self, auth_server):
        """Without a scope, all traversal endpoints return normally (backward compat)."""
        port, store = auth_server
        _create_node_direct(store, "u_a", "U A", created_by="metis")
        _create_node_direct(store, "u_b", "U B", created_by="observer")
        _create_edge_direct(store, "u_a", "u_b", created_by="metis")
        for endpoint in ("/path/u_a/u_b", "/impact/u_a", "/confidence/u_a", "/provenance/u_a"):
            status, _ = _request("GET", port, endpoint, token="test-token-abc")
            assert status == 200, f"{endpoint} returned {status}"
