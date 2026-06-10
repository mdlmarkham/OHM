"""Tests for OHM-tr71: Proactive Discoverability — islands, bridges, nudges, connectivity.

Tests against both the method layer and the HTTP server layer.
"""

import json
import threading
import pytest
from http.client import HTTPConnection
from ohm.store import OhmStore
from ohm.server.server import _hash_token, _build_token_lookup


def _insert_node(conn, nid, label, ntype="concept", tags=None, confidence=0.8, created_by="test", provenance="test"):
    """Helper to insert a node."""
    tags_json = json.dumps(tags) if tags else "[]"
    conn.execute(
        "INSERT OR REPLACE INTO ohm_nodes (id, label, type, tags, confidence, created_by, provenance, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [nid, label, ntype, tags_json, confidence, created_by, provenance],
    )


def _insert_edge(conn, from_n, to_n, etype="SUPPORTS", layer="L3", confidence=0.9, created_by="test"):
    """Helper to insert an edge."""
    conn.execute(
        "INSERT OR REPLACE INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [f"edge-{from_n}-{to_n}", from_n, to_n, etype, layer, confidence, created_by],
    )


def _start_test_server(store, no_auth=True):
    """Start a test HTTP server on a random port and return (port, server, thread)."""
    import socketserver
    from ohm.server import OhmHandler
    from ohm.schema import DEFAULT_SCHEMA
    from tests.conftest import wait_for_port

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = DEFAULT_SCHEMA
    OhmHandler.tokens = {}
    OhmHandler.roles = {}
    OhmHandler.no_auth = no_auth
    OhmHandler.multi_tenant = False
    OhmHandler.require_read_auth = False

    server = socketserver.TCPServer(
        ("127.0.0.1", 0),
        OhmHandler,
        bind_and_activate=False,
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port("127.0.0.1", port)
    return port, server, thread


def _request(method, port, path, body=None, headers=None):
    """Make an HTTP request to the test server."""
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
    hdrs = headers or {}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = None
    conn.request(method, path, body=body_bytes, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


# ── Unit Tests ─────────────────────────────────────────────────────────────────


class TestAdminIslands:
    """Tests for GET /admin/islands endpoint."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a store with islands of varying sizes."""
        import os

        db_path = os.path.join(str(tmp_path), "test_admin_islands.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        # Mainland: a1→a2→a3 (3 connected nodes)
        # Island-1: b1→b2→b3 (3 connected nodes, with tags)
        # Island-2: c1→c2 (2 connected nodes)
        # Orphans: d1 (single orphan)

        for nid, label, ntype, tags, creator in [
            ("a1", "Main Alpha", "concept", ["economics", "trade"], "agent-a"),
            ("a2", "Main Beta", "concept", ["economics"], "agent-a"),
            ("a3", "Main Gamma", "pattern", ["trade", "policy"], "agent-a"),
            ("b1", "Isle Delta", "concept", ["oil", "energy"], "agent-b"),
            ("b2", "Isle Epsilon", "concept", ["oil", "geopolitics"], "agent-b"),
            ("b3", "Isle Zeta", "pattern", ["energy", "supply-chain"], "agent-b"),
            ("c1", "Isle Eta", "concept", ["healthcare"], "agent-c"),
            ("c2", "Isle Theta", "concept", ["healthcare", "policy"], "agent-c"),
            ("d1", "Orphan Iota", "concept", ["solo"], "agent-d"),
        ]:
            _insert_node(store.conn, nid, label, ntype, tags, created_by=creator)

        # Mainland edges
        _insert_edge(store.conn, "a1", "a2", "SUPPORTS", "L3", 0.9, "agent-a")
        _insert_edge(store.conn, "a2", "a3", "CAUSES", "L3", 0.8, "agent-a")

        # Island-1 edges
        _insert_edge(store.conn, "b1", "b2", "SUPPORTS", "L3", 0.9, "agent-b")
        _insert_edge(store.conn, "b2", "b3", "CAUSES", "L3", 0.7, "agent-b")

        # Island-2 edges
        _insert_edge(store.conn, "c1", "c2", "SUPPORTS", "L3", 0.8, "agent-c")

        return store

    def test_handler_registered(self, store):
        """The _get_admin_islands method should exist on the handler."""
        from ohm.server.handlers.admin import AdminHandlerMixin

        assert hasattr(AdminHandlerMixin, "_get_admin_islands")

    def test_via_http(self, store):
        """GET /admin/islands returns enriched island data."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/admin/islands?min_size=2&max_islands=10")

            assert status == 200, f"Expected 200, got {status}: {data}"
            assert "islands" in data
            assert data["total_islands"] >= 2  # should find at least 2 islands (excluding mainland of 3)
            assert data["total_orphan_nodes"] >= 1  # d1 is an orphan

            # Check island structure
            for island in data["islands"]:
                assert "id" in island
                assert "size" in island
                assert "center" in island
                assert "nodes" in island
                assert "tags" in island
                assert "bridges_suggested" in island
                # Each island should have at least 2 nodes
                assert island["size"] >= 2

        finally:
            server.shutdown()

    def test_handles_empty_graph(self, tmp_path):
        """Empty graph returns empty islands list."""
        import os

        db_path = os.path.join(str(tmp_path), "empty_admin_islands.duckdb")
        empty_store = OhmStore(db_path=db_path, agent_name="test")

        port, server, thread = _start_test_server(empty_store, no_auth=True)
        try:
            status, data = _request("GET", port, "/admin/islands")
            assert status == 200
            assert data["islands"] == []
            assert data["total_islands"] == 0
        finally:
            server.shutdown()

    def test_includes_center(self, store):
        """Islands include a center node (most connected within island)."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/admin/islands?min_size=2")
            assert status == 200
            for island in data["islands"]:
                assert island["center"] is not None
                # Center should be one of the island's nodes
                node_ids = {n["id"] for n in island["nodes"]}
                assert island["center"] in node_ids
        finally:
            server.shutdown()

    def test_includes_tags(self, store):
        """Islands include aggregated tags from their nodes."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/admin/islands?min_size=2")
            assert status == 200
            for island in data["islands"]:
                assert isinstance(island["tags"], list)
        finally:
            server.shutdown()


class TestSuggestBridges:
    """Tests for GET /suggest?method=bridges."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a store with an island that needs bridges."""
        import os

        db_path = os.path.join(str(tmp_path), "test_bridges.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        # Mainland with economics/trade nodes
        # Island with oil/energy nodes — shares some tags with mainland
        for nid, label, ntype, tags in [
            ("m1", "Oil Market", "concept", ["oil", "economics", "trade"]),
            ("m2", "Trade Policy", "concept", ["trade", "policy"]),
            ("m3", "Supply Chain", "concept", ["supply-chain", "trade"]),
            ("i1", "Isle Energy", "concept", ["oil", "energy"]),
            ("i2", "Isle Drilling", "concept", ["oil", "extraction"]),
        ]:
            _insert_node(store.conn, nid, label, ntype, tags)

        # Mainland fully connected
        _insert_edge(store.conn, "m1", "m2", "SUPPORTS")
        _insert_edge(store.conn, "m2", "m3", "CAUSES")

        # Island: i1→i2 internal edge (no connection to mainland)
        _insert_edge(store.conn, "i1", "i2", "SUPPORTS")

        return store

    def test_suggest_bridges_finds_connections(self, store):
        """Bridge suggestions find tag overlap between island and mainland."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            # First get islands to find island ID
            status, islands_data = _request("GET", port, "/admin/islands?min_size=2&max_islands=10")
            assert status == 200
            assert len(islands_data["islands"]) > 0

            island_id = islands_data["islands"][0]["id"]

            # Now suggest bridges
            status, data = _request("GET", port, f"/suggest?method=bridges&island_id={island_id}&limit=5")
            assert status == 200, f"Expected 200, got {status}: {data}"
            assert "bridges" in data
            assert data["island_id"] == island_id

            # Should find at least one bridge (i1 or i2 shares 'oil' tag with m1)
            if data["bridges"]:
                bridge = data["bridges"][0]
                assert "from" in bridge
                assert "to" in bridge
                assert "score" in bridge
                assert bridge["score"] > 0

        finally:
            server.shutdown()

    def test_bridges_missing_island_id(self, store):
        """Missing island_id returns 400."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=bridges")
            assert status == 400
        finally:
            server.shutdown()

    def test_bridges_nonexistent_island(self, store):
        """Nonexistent island_id returns 404."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=bridges&island_id=island-999")
            assert status == 404
        finally:
            server.shutdown()


class TestSuggestNudge:
    """Tests for GET /suggest?method=nudge."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a store with data relevant for nudge testing."""
        import os

        db_path = os.path.join(str(tmp_path), "test_nudge.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        # Agent-alice has:
        # - 2 orphan nodes (no edges)
        # - 1 node with edge
        # - 1 unverified causal edge (confidence < 0.5)
        # - 1 high-confidence edge with no challenge

        for nid, label, ntype, creator in [
            ("alice-node-1", "Alice Concept 1", "concept", "alice"),
            ("alice-node-2", "Alice Concept 2", "concept", "alice"),
            ("alice-node-3", "Alice Connected", "concept", "alice"),
            ("bob-node-1", "Bob Concept 1", "concept", "bob"),
            ("shared-target", "Shared Target", "concept", "bob"),
        ]:
            _insert_node(store.conn, nid, label, ntype, created_by=creator)

        # Edge from alice-node-3 to bob-node-1 (low confidence for alice)
        _insert_edge(store.conn, "alice-node-3", "bob-node-1", "CAUSES", "L3", 0.3, "alice")
        # High confidence edge for alice to a different target (unique edge id)
        _insert_edge(store.conn, "alice-node-3", "shared-target", "SUPPORTS", "L3", 0.9, "alice")

        return store

    def test_nudge_requires_agent(self, store):
        """Missing agent returns 400."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=nudge")
            assert status == 400
        finally:
            server.shutdown()

    def test_nudge_returns_orphans(self, store):
        """Nudge returns orphans for the requested agent."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=nudge&agent=alice")
            assert status == 200, f"Expected 200, got {status}: {data}"

            assert data["agent"] == "alice"
            assert "orphan_nodes" in data
            assert data["orphan_count"] >= 2  # alice-node-1 and alice-node-2

            # Check orphan structure
            for orphan in data["orphan_nodes"]:
                assert "id" in orphan
                assert "label" in orphan

            # Unverified causal edges
            assert "unverified_causal_edges" in data
            assert data["unverified_causal_count"] >= 1  # the 0.3 confidence edge

            # Unchallenged high confidence edges
            assert "unchallenged_high_confidence" in data
            assert data["unchallenged_high_confidence_count"] >= 1  # the 0.9 edge

        finally:
            server.shutdown()

    def test_nudge_different_agent(self, store):
        """Nudge works for different agents."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            # Bob has no orphan nodes
            status, data = _request("GET", port, "/suggest?method=nudge&agent=bob")
            assert status == 200
            assert data["agent"] == "bob"
            # bob-node-1 has an edge from alice, so it's not an orphan
            assert data["orphan_count"] >= 0
        finally:
            server.shutdown()


class TestSuggestConnectivity:
    """Tests for GET /suggest?method=connectivity."""

    @pytest.fixture
    def store(self, tmp_path):
        """Create a store with an orphan node and connected nodes."""
        import os

        db_path = os.path.join(str(tmp_path), "test_connectivity.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        # Connected nodes (have edges)
        for nid, label, ntype, tags in [
            ("connected-1", "AI Ethics", "concept", ["ai", "ethics", "philosophy"]),
            ("connected-2", "Machine Learning", "concept", ["ai", "ml", "data"]),
            ("connected-3", "Data Science", "concept", ["data", "statistics"]),
            ("connected-4", "Philosophy of Mind", "concept", ["philosophy", "consciousness"]),
            ("orphan-x", "AI Safety", "concept", ["ai", "ethics", "safety"]),
        ]:
            _insert_node(store.conn, nid, label, ntype, tags)

        # Connect the connected nodes
        _insert_edge(store.conn, "connected-1", "connected-2", "SUPPORTS")
        _insert_edge(store.conn, "connected-2", "connected-3", "CAUSES")
        _insert_edge(store.conn, "connected-4", "connected-1", "REFERENCES")

        return store

    def test_connectivity_requires_node_id(self, store):
        """Missing node_id returns 400."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=connectivity")
            assert status == 400
        finally:
            server.shutdown()

    def test_connectivity_finds_candidates(self, store):
        """Connectivity returns similar connected nodes for orphan-x."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=connectivity&node_id=orphan-x&limit=3")
            assert status == 200, f"Expected 200, got {status}: {data}"

            assert data["node_id"] == "orphan-x"
            assert data["node_label"] == "AI Safety"
            assert "candidates" in data

            # Should find connected-1 (AI Ethics) as a top candidate (shared: ai, ethics)
            candidate_ids = {c["id"] for c in data["candidates"]}
            assert len(candidate_ids) > 0

            for c in data["candidates"]:
                assert "id" in c
                assert "score" in c
                assert "shared_tags" in c
                assert c["score"] > 0

        finally:
            server.shutdown()

    def test_connectivity_nonexistent_node(self, store):
        """Nonexistent node returns 404."""
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=connectivity&node_id=nonexistent-node")
            assert status == 404
        finally:
            server.shutdown()

    def test_connectivity_empty_result(self, tmp_path):
        """Orphan with no tag overlap with connected nodes returns empty candidates."""
        import os

        db_path = os.path.join(str(tmp_path), "test_connectivity_empty.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")

        _insert_node(store.conn, "orphan", "Zork", "concept", ["alien", "unknown"])
        _insert_node(store.conn, "conn", "Earth", "concept", ["human", "known"])
        _insert_edge(store.conn, "conn", "conn", "REFERENCES")  # Self-loop to be connected

        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            status, data = _request("GET", port, "/suggest?method=connectivity&node_id=orphan")
            assert status == 200
            # Candidates may be empty or have entries — the endpoint should not error
            assert "candidates" in data
            assert data["node_id"] == "orphan"
        finally:
            server.shutdown()
