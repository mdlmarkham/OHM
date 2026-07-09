"""HTTP regression tests for read-scope enforcement on traversal/analysis
endpoints (OHM-oqyc, ADR-037) — the follow-up to test_read_scope_leakage.py.

That file covers /node, /search, /neighborhood, /edges, /edge. This file
covers the six remaining traversal/analysis endpoints found (during review)
to have NO read-scope enforcement at all: /path, /impact, /confidence,
/narrative, /lineage, /provenance ("trace").

As of this writing, every scoped-agent test below is expected to FAIL
against current `main` — that's the point: they prove the gap exists.
They should pass once each handler is wired to the boundary.py scope
helpers, the same way c859b6e did for /edges, /edge, /neighborhood.

Fixture note: /provenance and /lineage only traverse a specific edge-type
allowlist (DERIVES_FROM, REFERENCES, INFLUENCES, SUPPORTS, SUPPORTS_EVIDENCE,
TESTS for lineage; DERIVES_FROM, REFERENCES, INFLUENCES, SUPPORTS for
provenance) — CAUSES is NOT in either list. The shared fixture graph below
therefore uses SUPPORTS uniformly so every endpoint under test actually
traverses into the restricted branch, rather than silently short-circuiting
before it ever reaches the scope check.
"""

from __future__ import annotations

from tests.conftest import _request


def _create_node_direct(store, node_id, label, created_by="metis", source_tier=None, node_type="concept"):
    store.write_node(node_id, label, node_type, source_tier=source_tier, agent_name=created_by, confidence=0.2 if source_tier == "raw" else 1.0)


def _create_edge_direct(store, from_id, to_id, edge_type="SUPPORTS", layer="L3", created_by="metis"):
    from ohm.graph.queries import create_edge

    return create_edge(store.conn, from_node=from_id, to_node=to_id, edge_type=edge_type, layer=layer, created_by=created_by)


def _set_scope(store, agent, scope):
    from ohm.server.boundary import set_agent_read_scope

    set_agent_read_scope(store.conn, agent, scope)


def _build_two_branch_graph(store):
    """Shared fixture topology, reused by every test below.

    root (verified) --SUPPORTS--> mid_visible (verified)    --SUPPORTS--> leaf_visible (verified)
    root (verified) --SUPPORTS--> mid_restricted (raw)       --SUPPORTS--> leaf_restricted (raw)

    Returns (edge_visible, edge_restricted) — the two edges out of root,
    since several tests need the restricted edge's id directly.
    """
    _create_node_direct(store, "root", "Root Claim", source_tier="verified")
    _create_node_direct(store, "mid_visible", "Supporting Evidence", source_tier="verified")
    _create_node_direct(store, "mid_restricted", "Raw Sensor Reading", source_tier="raw")
    _create_node_direct(store, "leaf_visible", "Corroborating Detail", source_tier="verified")
    _create_node_direct(store, "leaf_restricted", "Raw Detail", source_tier="raw")

    edge_visible = _create_edge_direct(store, "root", "mid_visible")
    edge_restricted = _create_edge_direct(store, "root", "mid_restricted")
    _create_edge_direct(store, "mid_visible", "leaf_visible")
    _create_edge_direct(store, "mid_restricted", "leaf_restricted")

    return edge_visible, edge_restricted


def _ids_in(obj) -> set[str]:
    """Recursively collect every string value found under any of the common
    id-ish keys in a nested dict/list response, so a test can assert a
    restricted node/edge id doesn't leak anywhere in a deeply nested shape
    (why_it_matters[].path[], lineage[].children[], etc.) without hand-coding
    the exact nesting for each endpoint."""
    found: set[str] = set()
    id_keys = {"id", "node_id", "edge_id", "from_node", "to_node", "source_node_id"}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in id_keys and isinstance(v, str):
                    found.add(v)
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return found


class TestPathScopeLeakage:
    """GET /path/<from>/<to> — shortest path between two nodes."""

    def test_no_scope_finds_restricted_path(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        status, data = _request("GET", port, "/path/root/leaf_restricted", token="test-token-abc")
        assert status == 200
        assert "mid_restricted" in _ids_in(data)

    def test_scoped_agent_visible_path_still_works(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/path/root/leaf_visible", token="test-token-abc")
        assert status == 200
        assert "mid_visible" in _ids_in(data)

    def test_scoped_agent_restricted_path_hidden(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/path/root/leaf_restricted", token="test-token-abc")
        if status == 200:
            leaked = _ids_in(data) & {"mid_restricted", "leaf_restricted"}
            assert not leaked, f"restricted ids leaked into path response: {leaked}"
        else:
            assert status in (403, 404)


class TestImpactScopeLeakage:
    """GET /impact/<id> — downstream impact analysis."""

    def test_no_scope_sees_both_branches(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        status, data = _request("GET", port, "/impact/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        assert "mid_restricted" in ids

    def test_scoped_agent_impact_excludes_restricted_branch(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/impact/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        leaked = ids & {"mid_restricted", "leaf_restricted"}
        assert not leaked, f"restricted ids leaked into impact response: {leaked}"


class TestConfidenceScopeLeakage:
    """GET /confidence/<id> — challenge/support/refinement chain, node or edge form."""

    def test_no_scope_allows_restricted_edge(self, auth_server):
        port, store = auth_server
        _, edge_restricted = _build_two_branch_graph(store)
        status, data = _request("GET", port, f"/confidence/{edge_restricted['id']}", token="test-token-abc")
        assert status == 200

    def test_scoped_agent_restricted_edge_blocked(self, auth_server):
        port, store = auth_server
        _, edge_restricted = _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, f"/confidence/{edge_restricted['id']}", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"

    def test_scoped_agent_visible_edge_allowed(self, auth_server):
        port, store = auth_server
        edge_visible, _ = _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, f"/confidence/{edge_visible['id']}", token="test-token-abc")
        assert status == 200

    def test_scoped_agent_restricted_node_form_blocked(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/confidence/mid_restricted", token="test-token-abc")
        assert status == 403
        assert data.get("error") == "permission_denied"


class TestNarrativeScopeLeakage:
    """GET /narrative/<id> — "why it matters" reasoning chains."""

    def test_no_scope_sees_both_branches(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        status, data = _request("GET", port, "/narrative/root?depth=2", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        assert "mid_restricted" in ids

    def test_scoped_agent_narrative_excludes_restricted_branch(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/narrative/root?depth=2", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        leaked = ids & {"mid_restricted", "leaf_restricted"}
        assert not leaked, f"restricted ids leaked into narrative response: {leaked}"


class TestLineageScopeLeakage:
    """GET /lineage/<id> — supporting-evidence chain tree."""

    def test_no_scope_sees_both_branches(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        status, data = _request("GET", port, "/lineage/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        assert "mid_restricted" in ids

    def test_scoped_agent_lineage_prunes_restricted_subtree(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/lineage/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        leaked = ids & {"mid_restricted", "leaf_restricted"}
        assert not leaked, f"restricted ids leaked into lineage response: {leaked}"


class TestProvenanceScopeLeakage:
    """GET /provenance/<id> — provenance trace ("trace")."""

    def test_no_scope_sees_both_branches(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        status, data = _request("GET", port, "/provenance/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        assert "mid_restricted" in ids

    def test_scoped_agent_provenance_excludes_restricted_branch(self, auth_server):
        port, store = auth_server
        _build_two_branch_graph(store)
        _set_scope(store, "metis", {"source_tier": ["verified"]})
        status, data = _request("GET", port, "/provenance/root?depth=3", token="test-token-abc")
        assert status == 200
        ids = _ids_in(data)
        assert "mid_visible" in ids
        leaked = ids & {"mid_restricted", "leaf_restricted"}
        assert not leaked, f"restricted ids leaked into provenance response: {leaked}"
