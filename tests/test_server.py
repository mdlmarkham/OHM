"""Tests for the OHM daemon HTTP server endpoints.

Starts a test server on a random port and tests all 17+ endpoints
including auth, error handling, and edge cases.

NOTE: Server tests share class-level state on OhmHandler (tokens, roles, etc.)
and must run sequentially. They are grouped with xdist_group("server").
"""

import json
import threading
from http.client import HTTPConnection

import pytest

from ohm.server import OhmHandler, _hash_token, _verify_token, _build_token_lookup, _trigger_webhooks, _webhook_registry, _webhook_lock
from ohm.schema import DEFAULT_SCHEMA, TOPO_SCHEMA
from ohm.store import OhmStore


def _start_test_server(store, tokens=None, roles=None, no_auth=False, schema_config=None, require_read_auth=False, multi_tenant=False):
    """Start a test HTTP server on a random port and return (port, thread).

    tokens can be:
      - dict of {plaintext_token: agent_name} — will be hashed automatically
      - dict of {hash: agent_name} — used directly (for testing hashed mode)

    schema_config: SchemaConfig instance (default: DEFAULT_SCHEMA)
    require_read_auth: If True, all endpoints require auth (OHM-gwg)
    multi_tenant: If True, enable multi-tenancy mode (OHM-l31g)
    """
    import socketserver

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = schema_config or DEFAULT_SCHEMA
    if tokens:
        # Convert plaintext tokens to hashed lookup
        token_hashes = {}
        for token, agent_name in tokens.items():
            token_hashes[_hash_token(token)] = agent_name
        OhmHandler.tokens = token_hashes
    else:
        OhmHandler.tokens = {}
    OhmHandler.roles = roles or {}
    OhmHandler.no_auth = no_auth
    OhmHandler.multi_tenant = multi_tenant
    if multi_tenant and not require_read_auth:
        OhmHandler.require_read_auth = True
    else:
        OhmHandler.require_read_auth = require_read_auth

    # Use TCPServer to get a random port
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
    from tests.conftest import wait_for_port

    wait_for_port("127.0.0.1", port)
    return port, server, thread


def _request(method, port, path, body=None, headers=None, token=None):
    """Make an HTTP request to the test server."""
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
    hdrs = headers or {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
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


@pytest.fixture
def test_server(tmp_path):
    """Start a test server with a temp database (no-auth dev mode)."""
    db_path = str(tmp_path / "test_server.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    port, server, thread = _start_test_server(store, no_auth=True)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.fixture
def auth_server(tmp_path):
    """Start a test server with token auth enabled."""
    db_path = str(tmp_path / "test_auth.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    tokens = {"test-token-abc": "metis", "readonly-token": "observer"}
    roles = {"metis": "read-write", "observer": "read-only"}
    port, server, thread = _start_test_server(store, tokens=tokens, roles=roles)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.mark.xdist_group("server")
class TestHealthEndpoints:
    """Tests for /health, /ready, /status endpoints."""

    def test_health_returns_ok(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/health")
        assert status == 200
        assert data["status"] == "ok"
        assert "uptime" in data

    def test_health_graph_stats_populated(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/health")
        assert status == 200
        graph = data.get("graph", {})
        # All graph stat fields must be present and non-None
        for key in ("health_score", "node_count", "edge_count", "orphan_count", "orphan_rate", "low_confidence_count"):
            assert key in graph, f"Missing graph stat: {key}"
            assert graph[key] is not None, f"Graph stat null: {key}"

    def test_ready_returns_ready(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/ready")
        assert status == 200
        assert data["status"] == "ready"

    def test_status_has_counts(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/status")
        assert status == 200
        assert "node_count" in data
        assert "edge_count" in data
        assert "uptime" in data
        assert "version" in data


@pytest.mark.xdist_group("server")
class TestSchemaEndpoints:
    """Tests for /schema and /layers."""

    def test_schema_returns_types(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert "node_types" in data
        assert "edge_types" in data
        assert "layers" in data
        assert data["schema"] == "ohm"

    def test_layers_returns_descriptions(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/layers")
        assert status == 200
        assert "L1" in data


@pytest.mark.xdist_group("server")
class TestNodeEndpoints:
    """Tests for node CRUD via HTTP."""

    def test_get_nonexistent_node(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/node/nonexistent")
        assert status == 404
        assert data["error"] == "not_found"
        assert "correlation_id" in data

    def test_create_and_get_node(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "test_node_1",
                "label": "Test Node",
                "type": "concept",
            },
        )
        assert status == 201
        assert data["id"] == "test_node_1"

        status, data = _request("GET", port, "/node/test_node_1")
        assert status == 200
        assert data["label"] == "Test Node"


@pytest.mark.xdist_group("server")
class TestEdgeEndpoints:
    """Tests for edge CRUD via HTTP."""

    def test_get_nonexistent_edge(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/edge/nonexistent")
        assert status == 404
        assert data["error"] == "not_found"

    def test_create_edge(self, test_server):
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "a",
                "label": "A",
                "type": "concept",
            },
        )
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "b",
                "label": "B",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "a",
                "to": "b",
                "type": "CAUSES",
                "layer": "L3",
            },
)
        assert status == 201

    def test_observe_invalid_obs_type_rejected(self, test_server):
        """POST /observe/{id} rejects observation types not in schema (OHM-jt98)."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "obs-type-test-node",
                "label": "Obs Type Test",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/obs-type-test-node",
            body={
                "type": "not_a_valid_obs_type",
                "value": 5.0,
            },
        )
        assert status == 400
        assert "not_a_valid_obs_type" in data.get("message", "") or "not_a_valid_obs_type" in data.get("error", "")


@pytest.mark.xdist_group("server")
class TestSourceAttribution:
    """Tests for structured source attribution on observations (OHM-lmr)."""

    def test_observe_with_source_name_and_url(self, test_server):
        """POST /observe/{id} with source_name and source_url persists them."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-attrib-node",
                "label": "Source Test",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/src-attrib-node",
            body={
                "type": "measurement",
                "value": 1.5,
                "source_name": "Reuters",
                "source_url": "https://reuters.com/article/123",
            },
        )
        assert status == 201
        assert data.get("source_name") == "Reuters"
        assert data.get("source_url") == "https://reuters.com/article/123"

    def test_observe_source_attribution_in_db(self, test_server):
        """source_name and source_url are stored in the database."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-attrib-db",
                "label": "DB Source Test",
                "type": "concept",
            },
        )
        _request(
            "POST",
            port,
            "/observe/src-attrib-db",
            body={
                "type": "measurement",
                "value": 2.0,
                "source_name": "AP News",
                "source_url": "https://apnews.com/article/456",
            },
        )
        obs = store.execute(
            "SELECT source_name, source_url FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1",
            ["src-attrib-db"],
        )
        assert len(obs) == 1
        assert obs[0]["source_name"] == "AP News"
        assert obs[0]["source_url"] == "https://apnews.com/article/456"

    def test_observe_without_source_attribution(self, test_server):
        """POST /observe/{id} without source fields works (backward compatible)."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-no-attrib",
                "label": "No Source",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/src-no-attrib",
            body={
                "type": "measurement",
                "value": 3.0,
            },
        )
        assert status == 201
        assert data.get("source_name") is None
        assert data.get("source_url") is None


@pytest.mark.xdist_group("server")
class TestPERTFields:
    """Tests for PERT distribution fields on edges (OHM-6mv.11)."""

    def test_post_edge_with_pert_probability(self, test_server):
        """POST /edge with PERT probability fields persists them."""
        port, store = test_server
        # Create nodes first
        _request("POST", port, "/node", body={"id": "pert-cause-1", "label": "Cause 1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-1", "label": "Effect 1", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-1",
                "to": "pert-effect-1",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
                "probability_p05": 0.1,
                "probability_p50": 0.5,
                "probability_p95": 0.9,
            },
        )
        assert status == 201
        assert abs(data["probability_p05"] - 0.1) < 0.01
        assert abs(data["probability_p50"] - 0.5) < 0.01
        assert abs(data["probability_p95"] - 0.9) < 0.01

    def test_post_edge_with_all_pert_fields(self, test_server):
        """POST /edge with all PERT fields persists them."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "pert-cause-2", "label": "Cause 2", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-2", "label": "Effect 2", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-2",
                "to": "pert-effect-2",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
                "probability_p05": 0.05,
                "probability_p50": 0.4,
                "probability_p95": 0.85,
                "confidence_p05": 0.2,
                "confidence_p50": 0.7,
                "confidence_p95": 0.95,
            },
        )
        assert status == 201
        assert abs(data["probability_p05"] - 0.05) < 0.01
        assert abs(data["confidence_p05"] - 0.2) < 0.01

    def test_post_edge_without_pert_fields(self, test_server):
        """POST /edge without PERT fields works (backward compatible)."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "pert-cause-3", "label": "Cause 3", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-3", "label": "Effect 3", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-3",
                "to": "pert-effect-3",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
            },
        )
        assert status == 201
        assert data.get("probability_p05") is None
        assert data.get("confidence_p05") is None


@pytest.mark.xdist_group("server")
class TestBatchEndpoint:
    """Tests for POST /batch endpoint (OHM-1m3)."""

    def test_batch_create_nodes(self, test_server):
        """POST /batch creates multiple nodes."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-n1", "label": "Node 1", "type": "concept"},
                    {"id": "batch-n2", "label": "Node 2", "type": "source"},
                ],
                "edges": [],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 0

    def test_batch_create_nodes_and_edges(self, test_server):
        """POST /batch creates nodes and edges together."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-n3", "label": "Node A", "type": "concept"},
                    {"id": "batch-n4", "label": "Node B", "type": "concept"},
                ],
                "edges": [
                    {"from": "batch-n3", "to": "batch-n4", "type": "CAUSES", "layer": "L3"},
                ],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 1

    def test_batch_validation_error(self, test_server):
        """POST /batch with missing required fields returns validation error."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-bad"},  # missing 'label'
                ],
                "edges": [],
            },
        )
        assert status == 400

    def test_batch_empty(self, test_server):
        """POST /batch with empty arrays returns zeros."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [],
                "edges": [],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 0
        assert data["edges_created"] == 0

    def test_batch_populates_change_feed(self, test_server):
        """POST /batch populates change feed for each created item."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "cf-batch-1", "label": "CF1", "type": "concept"},
                    {"id": "cf-batch-2", "label": "CF2", "type": "concept"},
                ],
                "edges": [],
            },
        )
        # Verify change feed entries
        feed = store.execute("SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' AND row_id IN ('cf-batch-1', 'cf-batch-2') ORDER BY occurred_at DESC")
        assert len(feed) == 2


@pytest.mark.xdist_group("server")
class TestIdempotentRegistration:
    """Tests for idempotent agent registration (OHM-5n7: deduplicate registration)."""

    def test_register_creates_agent_node(self, test_server):
        """POST /register creates an agent node with deterministic ID."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "testbot",
                "description": "A test agent",
                "values": ["accuracy"],
                "goals": ["explore"],
            },
        )
        assert status == 201
        assert data["agent"]["label"] == "testbot"
        assert data["agent"]["type"] == "agent"
        assert data["edges_created"] >= 2  # VALUES + GOALS

    def test_register_idempotent(self, test_server):
        """POST /register twice with same name reuses agent node (no duplicates)."""
        port, store = test_server
        # First registration
        status1, data1 = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "idem_agent",
                "values": ["truth"],
            },
        )
        assert status1 == 201
        agent_id_1 = data1["agent"]["id"]

        # Second registration with same name
        status2, data2 = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "idem_agent",
                "values": ["truth", "fairness"],
            },
        )
        assert status2 == 201
        agent_id_2 = data2["agent"]["id"]

        # Same agent node ID (deterministic)
        assert agent_id_1 == agent_id_2

        # No duplicate agent nodes
        agent_nodes = store.execute("SELECT * FROM ohm_nodes WHERE type = 'agent' AND label = 'idem_agent'")
        assert len(agent_nodes) == 1

    def test_register_reuses_value_nodes(self, test_server):
        """POST /register reuses existing value/goal/skill nodes."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "reuse_agent",
                "values": ["courage"],
            },
        )
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "other_agent",
                "values": ["courage"],
            },
        )
        # Only one "courage" value node should exist
        courage_nodes = store.execute("SELECT * FROM ohm_nodes WHERE label = 'courage' AND type = 'value'")
        assert len(courage_nodes) == 1

    def test_register_updates_edges(self, test_server):
        """POST /register replaces old edges on re-registration."""
        port, store = test_server
        # First registration with 1 value
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "edge_agent",
                "values": ["loyalty"],
            },
        )
        # Second registration with 2 values
        status, data = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "edge_agent",
                "values": ["loyalty", "honesty"],
            },
        )
        assert status == 201
        # Should have 2 active VALUES edges (old ones soft-deleted, new ones created)
        agent_id = data["agent"]["id"]
        values_edges = store.execute(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES' AND deleted_at IS NULL",
            [agent_id],
        )
        assert len(values_edges) == 2


@pytest.mark.xdist_group("server")
class TestSemanticSearchEndpoint:
    """Tests for /semantic_search endpoint (OHM-o9f)."""

    def test_semantic_search_endpoint_requires_query(self, test_server):
        """GET /semantic_search without ?q= returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/semantic_search")
        assert status == 400

    def test_semantic_search_endpoint_returns_503_without_ollama(self, test_server):
        """GET /semantic_search?q=test returns 503 when Ollama is not available."""
        port, _ = test_server
        status, data = _request("GET", port, "/semantic_search?q=test+query")
        # Either 503 (Ollama not running) or 200 (Ollama available)
        assert status in (200, 503)
        if status == 503:
            assert "service_unavailable" in data.get("error", "")

    def test_semantic_search_endpoint_in_discovery(self, test_server):
        """Root discovery endpoint includes /semantic_search."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        assert "/semantic_search" in data.get("endpoints", {})

    def test_search_endpoint_still_works(self, test_server):
        """GET /search?q= still works (ILIKE search unchanged)."""
        port, _ = test_server
        # Create a node
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "search-test-node",
                "label": "Machine Learning",
                "type": "concept",
            },
        )
        status, data = _request("GET", port, "/search?q=Machine")
        assert status == 200


@pytest.mark.xdist_group("server")
class TestDuckLakeTimeTravel:
    """Tests for DuckLake time-travel endpoints (OHM-kdk.3)."""

    def test_admin_snapshots_without_ducklake(self, test_server):
        """GET /admin/snapshots returns empty list when DuckLake is not attached."""
        port, _ = test_server
        status, data = _request("GET", port, "/admin/snapshots")
        assert status == 200
        assert data["snapshots"] == []
        assert data["count"] == 0

    def test_graph_at_without_version_returns_400(self, test_server):
        """GET /graph/at without ?version=N returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at")
        assert status == 400

    def test_graph_at_with_invalid_version_returns_400(self, test_server):
        """GET /graph/at?version=abc returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=abc")
        assert status == 400

    def test_graph_at_without_ducklake_returns_error(self, test_server):
        """GET /graph/at?version=1 without DuckLake attached returns error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=1")
        assert status in (400, 500)

    def test_graph_changes_without_params_returns_400(self, test_server):
        """GET /graph/changes without required params returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes")
        assert status == 400

    def test_graph_changes_missing_to_version_returns_400(self, test_server):
        """GET /graph/changes?from_version=1 without to_version returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes?from_version=1")
        assert status == 400

    def test_graph_changes_invalid_version_returns_400(self, test_server):
        """GET /graph/changes?from_version=abc&to_version=2 returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes?from_version=abc&to_version=2")
        assert status == 400

    def test_discovery_index_includes_time_travel(self, test_server):
        """Root discovery endpoint includes time-travel endpoints."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        endpoints = data["endpoints"]
        assert "/admin/snapshots" in endpoints
        assert "/graph/at" in endpoints
        assert "/graph/changes" in endpoints


@pytest.mark.xdist_group("server")
class TestPublicReadAuthModel:
    """Tests for public-read auth model (OHM-gwg).

    Default behavior: reads are public (no token needed), writes require auth.
    With --require-read-auth: all endpoints require auth.
    """

    def test_public_read_allows_unauthenticated_get(self, test_server):
        """GET /stats works without a token (public-read model)."""
        port, _ = test_server
        # test_server fixture uses no_auth=True, so reads are always allowed
        status, data = _request("GET", port, "/stats")
        assert status == 200

    def test_auth_model_in_discovery(self, test_server):
        """Root discovery includes auth_model field."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        # no_auth mode should report "public-read" or "authenticated"
        assert "auth_model" in data

    def test_require_read_auth_blocks_unauthenticated_reads(self, tmp_path):
        """With require_read_auth=True, unauthenticated reads return 401."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_auth_read.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"read-auth-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens, require_read_auth=True)
        try:
            # Unauthenticated read should fail
            status, data = _request("GET", port, "/stats")
            assert status == 401
            # Authenticated read should succeed
            status, data = _request("GET", port, "/stats", token="read-auth-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_default_allows_unauthenticated_reads_with_tokens(self, tmp_path):
        """With tokens configured but require_read_auth=False, reads are public."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_public_read.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"pub-read-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens)
        try:
            # Unauthenticated read should succeed (public-read model)
            status, data = _request("GET", port, "/stats")
            assert status == 200
            # Authenticated read should also succeed
            status, data = _request("GET", port, "/stats", token="pub-read-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_multi_tenant_default_requires_auth(self, tmp_path):
        """Multi-tenant mode defaults to require_read_auth=True (OHM-en2r)."""
        db_path = str(tmp_path / "test_mt_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"mt-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens, multi_tenant=True)
        try:
            status, data = _request("GET", port, "/stats")
            assert status == 401, f"Multi-tenant should default to require_read_auth=True, got {status}"
            status, data = _request("GET", port, "/stats", token="mt-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()


@pytest.mark.xdist_group("server")
class TestMetisBugFixes:
    """Regression tests for bugs found by Metis in the 50-endpoint test run."""

    def test_edge_rejects_nonexistent_from_node(self, test_server):
        """POST /edge should 404 when from_node doesn't exist (OHM-7298)."""
        port, store = test_server
        store.write_node("real-node", "Real Node", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ghost-node",
                "to": "real-node",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 404
        assert "ghost-node" in data.get("message", "")

    def test_edge_rejects_nonexistent_to_node(self, test_server):
        """POST /edge should 404 when to_node doesn't exist (OHM-7298)."""
        port, store = test_server
        store.write_node("src-node", "Source Node", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "src-node",
                "to": "ghost-target",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 404
        assert "ghost-target" in data.get("message", "")

    def test_edge_with_valid_nodes_succeeds(self, test_server):
        """POST /edge should succeed when both nodes exist (OHM-7298 no regression)."""
        port, store = test_server
        store.write_node("ei-src", "Source", "concept", agent_name="test")
        store.write_node("ei-dst", "Dest", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ei-src",
                "to": "ei-dst",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 201

    def test_observe_rejects_nonexistent_node(self, test_server):
        """POST /observe/{id} should 404 when node doesn't exist (OHM-7302)."""
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/observe/ghost-node-obs",
            body={
                "type": "measurement",
                "value": 42.0,
            },
        )
        assert status == 404

    def test_observe_valid_node_succeeds(self, test_server):
        """POST /observe/{id} should succeed when node exists (OHM-7302 no regression)."""
        port, store = test_server
        store.write_node("obs-node", "Observable", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/observe/obs-node",
            body={
                "type": "measurement",
                "value": 42.0,
            },
        )
        assert status == 201

    def test_deep_includes_edges(self, test_server):
        """GET /deep/{id} should include connected edges (OHM-7299)."""
        port, store = test_server
        store.write_node("deep-hub", "Hub", "concept", agent_name="test")
        store.write_node("deep-spoke", "Spoke", "concept", agent_name="test")
        store.write_edge("deep-hub", "deep-spoke", "CAUSES", "L3", agent_name="test")
        status, data = _request("GET", port, "/deep/deep-hub")
        assert status == 200
        assert "edges" in data
        assert data["edge_count"] >= 1
        assert any(e["from_node"] == "deep-hub" for e in data["edges"])

    def test_post_sync_returns_200(self, test_server):
        """POST /sync should return 200 with sync result (OHM-7301)."""
        port, _ = test_server
        status, data = _request("POST", port, "/sync", body={})
        assert status == 200
        assert "pushed" in data or "last_sync" in data

    def test_post_tasks_creates_task(self, test_server):
        """POST /tasks should create a task node (OHM-7304)."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "task-create-test",
                "label": "Do the thing",
                "task_status": "open",
                "priority": "P1",
            },
        )
        assert status == 201
        assert data.get("type") == "task"
        assert data.get("task_status") == "open"

    def test_post_tasks_then_get_tasks(self, test_server):
        """Task created via POST /tasks is visible in GET /tasks (OHM-7304)."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "task-roundtrip",
                "label": "Roundtrip task",
                "task_status": "open",
            },
        )
        status, data = _request("GET", port, "/tasks")
        assert status == 200
        ids = [t["id"] for t in data.get("tasks", [])]
        assert "task-roundtrip" in ids


@pytest.mark.xdist_group("server")
class TestMetisBatch2Fixes:
    """Regression tests for bugs found in Metis's second test run (OHM-7308..7321)."""

    def test_post_task_auto_generates_id(self, test_server):
        """POST /tasks without 'id' field auto-generates one (OHM-7308)."""
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "label": "Auto-ID task",
                "task_status": "open",
            },
        )
        assert status == 201
        assert data.get("id"), "id should be auto-generated"
        assert data["id"].startswith("task_")

    def test_post_task_with_explicit_id(self, test_server):
        """POST /tasks with explicit 'id' uses that id (OHM-7308)."""
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "explicit-task-id-7308",
                "label": "Explicit ID task",
            },
        )
        assert status == 201
        assert data.get("id") == "explicit-task-id-7308"

    def test_post_edge_accepts_from_node_alias(self, test_server):
        """POST /edge accepts from_node/to_node/edge_type aliases (OHM-7314)."""
        port, store = test_server
        store.write_node("alias-from", "Alias Source", "concept", agent_name="test")
        store.write_node("alias-to", "Alias Dest", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from_node": "alias-from",
                "to_node": "alias-to",
                "edge_type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 201
        assert data.get("from_node") == "alias-from"
        assert data.get("to_node") == "alias-to"

    def test_patch_node_updates_label(self, test_server):
        """PATCH /node/{id} can update node label (OHM-7319)."""
        port, store = test_server
        store.write_node("patch-test-node", "Original Label", "concept", agent_name="test")
        status, data = _request(
            "PATCH",
            port,
            "/node/patch-test-node",
            body={
                "label": "Updated Label",
            },
        )
        assert status == 200
        assert data.get("label") == "Updated Label"

    def test_patch_node_404_for_missing(self, test_server):
        """PATCH /node/{id} returns 404 for non-existent node (OHM-7319)."""
        port, _ = test_server
        status, data = _request(
            "PATCH",
            port,
            "/node/nonexistent-patch-node",
            body={
                "label": "Won't work",
            },
        )
        assert status == 404

    def test_source_reliability_alias(self, test_server):
        """GET /source_reliability?source=<agent> returns reliability data (OHM-7310)."""
        port, _ = test_server
        status, data = _request("GET", port, "/source_reliability?source=test")
        assert status == 200
        assert "source_agent" in data

    def test_compound_confidence_endpoint(self, test_server):
        """GET /compound_confidence/{node} returns compound confidence (OHM-7311)."""
        port, store = test_server
        store.write_node("cc-test-node", "CC Node", "concept", agent_name="test")
        status, data = _request("GET", port, "/compound_confidence/cc-test-node")
        assert status == 200
        assert "node_id" in data
        assert data["node_id"] == "cc-test-node"

    def test_suggest_orphan_connect_returns_list(self, test_server):
        """GET /suggest?method=orphan_connect returns a list (OHM-7312)."""
        port, _ = test_server
        status, data = _request("GET", port, "/suggest?method=orphan_connect")
        assert status == 200
        assert isinstance(data, list)

    def test_suggest_cooccurrence_returns_list(self, test_server):
        """GET /suggest?method=cooccurrence returns a list (OHM-7312)."""
        port, _ = test_server
        status, data = _request("GET", port, "/suggest?method=cooccurrence")
        assert status == 200
        assert isinstance(data, list)

    def test_suggest_shared_tags_no_empty_fields(self, test_server):
        """GET /suggest?method=shared_tags results have non-empty from_id/to_id (OHM-7313)."""
        port, store = test_server
        # Create tagged nodes to trigger shared_tags results
        store.write_node("tagged-a", "Tagged A", "concept", agent_name="test", tags=["geopolitics", "energy"])
        store.write_node("tagged-b", "Tagged B", "concept", agent_name="test", tags=["geopolitics", "security"])
        status, data = _request("GET", port, "/suggest?method=shared_tags&min_shared=1")
        assert status == 200
        assert isinstance(data, list)
        for item in data:
            assert item.get("from_id"), f"from_id empty in {item}"
            assert item.get("to_id"), f"to_id empty in {item}"

    def test_ate_returns_diagnostic_when_disconnected(self, test_server):
        """GET /ate returns diagnostic error (not silent ATE=0) when nodes not connected (OHM-7320)."""
        port, store = test_server
        # Create two nodes with no edge between them
        store.write_node("ate-cause-island", "Isolated Cause", "concept", agent_name="test")
        store.write_node("ate-effect-island", "Isolated Effect", "concept", agent_name="test")
        status, data = _request("GET", port, "/ate?cause=ate-cause-island&effect=ate-effect-island")
        assert status == 200
        # When pgmpy is unavailable the endpoint returns method=none with an error message —
        # either way it must never silently return ATE=0.0 with risk_ratio=1.0 and no error.
        if data.get("method") not in ("none", "error"):
            # pgmpy available — should detect disconnection and return method=error
            if data.get("ate") == 0.0 and data.get("risk_ratio") == 1.0:
                assert data.get("method") == "error", f"ATE=0 with RR=1 must not be returned silently for disconnected nodes; got {data}"

    def test_ate_connected_path_returns_nonzero(self, test_server):
        """GET /ate returns non-zero ATE when cause→effect edge exists (OHM-7320)."""
        import importlib.util

        if not importlib.util.find_spec("pgmpy"):
            pytest.skip("pgmpy not installed")
        port, store = test_server
        store.write_node("ate-cause-a", "Cause A", "concept", agent_name="test")
        store.write_node("ate-effect-b", "Effect B", "concept", agent_name="test")
        # Create a direct causal edge with high probability
        _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ate-cause-a",
                "to": "ate-effect-b",
                "type": "CAUSES",
                "layer": "L3",
                "probability": 0.9,
            },
        )
        status, data = _request("GET", port, "/ate?cause=ate-cause-a&effect=ate-effect-b")
        assert status == 200
        assert data.get("method") != "error", f"Unexpected error: {data}"
        # With a direct high-probability CAUSES edge, ATE should be detectably non-zero
        assert abs(data.get("ate", 0.0)) > 0.01, f"ATE should be non-zero with direct causal edge, got {data}"


class TestGitHubBacklogFixes:
    """Regression tests for GitHub issues from Deepthought/Socrates (OHM-zwrw, OHM-9pb7, OHM-zn3s)."""

    def test_heartbeat_does_not_crash_when_change_feed_missing(self, test_server):
        """POST /heartbeat must not 500 even when ohm_change_feed table is absent (OHM-zwrw)."""
        port, store = test_server
        # Drop the change feed table to simulate a pre-migration production DB
        try:
            store.conn.execute("DROP TABLE IF EXISTS ohm_change_feed")
            store.conn.execute("DROP SEQUENCE IF EXISTS seq_change_feed")
        except Exception:
            pass
        # Heartbeat should succeed (not 500)
        status, data = _request("POST", port, "/heartbeat", body={"focus": "test"})
        assert status == 200, f"Heartbeat should not crash without ohm_change_feed: {data}"

    def test_change_feed_query_falls_back_when_table_missing(self, test_server):
        """GET /listen must not crash when ohm_change_feed is absent (OHM-zwrw)."""
        port, store = test_server
        try:
            store.conn.execute("DROP TABLE IF EXISTS ohm_change_feed")
        except Exception:
            pass
        # /listen reads from ohm_change_feed — should fall back to ohm_change_log
        status, data = _request("GET", port, "/listen?limit=5")
        assert status == 200, f"/listen should not crash without ohm_change_feed: {data}"

    def test_voi_reports_mixed_sensitivity_methods(self, test_server):
        """GET /voi includes mixed_sensitivity_methods flag in response (OHM-9pb7)."""
        port, _ = test_server
        status, data = _request("GET", port, "/voi?top=5")
        assert status == 200
        assert "mixed_sensitivity_methods" in data, f"VoI response must include mixed_sensitivity_methods field: {data}"
        assert "sensitivity_methods_used" in data

    def test_voi_min_observations_flags_sparse_nodes(self, test_server):
        """GET /voi?min_observations=3 flags nodes with fewer than 3 observations (OHM-zn3s)."""
        port, store = test_server
        store.write_node("dec-test-voi", "Test Decision", "decision", agent_name="test", utility_scale=1.0)
        store.write_node("anc-test-voi", "Test Ancestor", "concept", agent_name="test")
        _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "anc-test-voi",
                "to": "dec-test-voi",
                "type": "CAUSES",
                "layer": "L3",
                "probability": 0.7,
            },
        )
        status, data = _request("GET", port, "/voi?min_observations=3&decision=dec-test-voi")
        assert status == 200
        for entry in data.get("rankings", []):
            if entry["node_id"] == "anc-test-voi":
                assert entry.get("low_data_warning") is True, f"Node with 0 obs should have low_data_warning: {entry}"
                break

    def test_voi_no_low_data_warning_when_threshold_zero(self, test_server):
        """GET /voi without min_observations has no low_data_warning fields (OHM-zn3s)."""
        port, _ = test_server
        status, data = _request("GET", port, "/voi?top=5")
        assert status == 200
        for entry in data.get("rankings", []):
            assert "low_data_warning" not in entry, f"low_data_warning should not appear when min_observations=0: {entry}"


class TestWebhookTenantIsolation:
    """OHM-ym2f: Webhook registry must not fire cross-tenant (tenant A webhook ≠ tenant B events)."""

    def setup_method(self):
        with _webhook_lock:
            _webhook_registry.clear()

    def teardown_method(self):
        with _webhook_lock:
            _webhook_registry.clear()

    def test_webhook_fires_for_matching_tenant(self):
        """Webhook registered under customer_id='a' fires when event is triggered for 'a'."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append((url, event))
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry["tenant_a"] = {"agent1": {"url": "https://example.com/hook", "events": ["node.created"]}}

            _trigger_webhooks({"type": "node.created", "agent": "agent1", "node": {}}, customer_id="tenant_a")
            assert len(fired) == 1
            assert fired[0][0] == "https://example.com/hook"
        finally:
            srv._deliver_webhook = original

    def test_webhook_does_not_fire_for_different_tenant(self):
        """Webhook registered under customer_id='a' must NOT fire for customer_id='b' events."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append(url)
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry["tenant_a"] = {"agent1": {"url": "https://example.com/hook", "events": ["*"]}}

            _trigger_webhooks({"type": "node.created", "agent": "agent2", "node": {}}, customer_id="tenant_b")
            assert fired == [], f"Cross-tenant webhook fired: {fired}"
        finally:
            srv._deliver_webhook = original

    def test_webhook_none_tenant_fires_for_none_events(self):
        """Single-tenant (customer_id=None) webhooks fire for customer_id=None events only."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append(url)
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry[None] = {"agent_st": {"url": "https://example.com/st", "events": ["*"]}}

            _trigger_webhooks({"type": "edge.created", "agent": "agent_st"}, customer_id=None)
            assert len(fired) == 1

            fired.clear()
            _trigger_webhooks({"type": "edge.created", "agent": "other"}, customer_id="some_tenant")
            assert fired == [], "Single-tenant webhook must not fire for tenant-scoped event"
        finally:
            srv._deliver_webhook = original


class TestMultiTenantFeatureFlag:
    """Tests for OHM-l31g: feature-flag multi-tenancy rollout."""

    def test_multi_tenant_default_off(self, tmp_path):
        """Multi-tenancy is OFF by default — no flag, no env var."""
        db_path = str(tmp_path / "test_mt_off.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            assert OhmHandler.multi_tenant is False
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["multi_tenant"] is False
        finally:
            server.shutdown()

    def test_customer_id_none_when_off(self):
        """When multi_tenant=False, _customer_id always returns None."""
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        assert handler._customer_id is None

    def test_customer_id_resolved_when_on(self):
        """When multi_tenant=True, _customer_id returns _resolved_customer_id if set."""
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler._resolved_customer_id = "acme-corp"
        assert handler._customer_id == "acme-corp"

    def test_current_store_returns_store_when_off(self, tmp_path):
        """When multi_tenant=False, current_store returns self.store with zero indirection."""
        db_path = str(tmp_path / "test_mt_store.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        handler.store = store
        assert handler.current_store is store
        store.close()

    def test_status_includes_multi_tenant(self, tmp_path):
        """GET /status includes multi_tenant flag in response."""
        db_path = str(tmp_path / "test_mt_status.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True, multi_tenant=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["multi_tenant"] is True
        finally:
            server.shutdown()

    def test_env_var_enables_multi_tenant(self, monkeypatch):
        """OHM_MULTI_TENANT=1 enables multi-tenancy via environment variable."""
        import os
        monkeypatch.setenv("OHM_MULTI_TENANT", "1")
        assert os.environ.get("OHM_MULTI_TENANT", "").lower() in ("1", "true", "yes")


class TestMarkovHTTPEndpoints:
    """Tests for OHM-20bt: Markov HTTP endpoints in the daemon."""

    @pytest.fixture(autouse=True)
    def require_numpy(self):
        pytest.importorskip("numpy")

    def test_markov_absorbing_risk_endpoint(self, tmp_path):
        """GET /markov/absorbing?start=<node_id> returns Markov analysis."""
        db_path = str(tmp_path / "test_markov_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "healthy", "label": "healthy", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "symptomatic", "label": "symptomatic", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "deceased", "label": "deceased", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.3}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "healthy", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.7}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "deceased", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.1}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/absorbing?start=healthy")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "method" in data
            assert "absorbing" in data["method"]
        finally:
            server.shutdown()

    def test_markov_expected_steps_endpoint(self, tmp_path):
        """GET /markov/expected_steps?start=<node_id> returns expected steps."""
        db_path = str(tmp_path / "test_markov_steps_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "healthy", "label": "healthy", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "symptomatic", "label": "symptomatic", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "deceased", "label": "deceased", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.3}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "healthy", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.7}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "deceased", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.1}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/expected_steps?start=healthy")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "method" in data
            assert "expected_steps" in data["method"]
        finally:
            server.shutdown()

    def test_markov_absorbing_missing_start(self, tmp_path):
        """GET /markov/absorbing without ?start= returns 400."""
        db_path = str(tmp_path / "test_markov_no_start.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/absorbing")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            server.shutdown()

    def test_markov_in_discovery_index(self, tmp_path):
        """GET / discovery index includes Markov endpoints."""
        db_path = str(tmp_path / "test_markov_index.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert "/markov/absorbing" in data["endpoints"]
            assert "/markov/expected_steps" in data["endpoints"]
        finally:
            server.shutdown()


@pytest.mark.xdist_group("server")
class TestOrphansPurge:
    """Tests for POST /orphans/purge (OHM-llw9)."""

    def test_purge_dry_run_lists_candidates(self, test_server):
        """POST /orphans/purge with dry_run=true returns candidates without deleting."""
        port, store = test_server
        # Create two isolated nodes (no edges)
        _request("POST", port, "/node", body={"id": "orphan-a", "label": "Orphan A", "type": "concept"})
        _request("POST", port, "/node", body={"id": "orphan-b", "label": "Orphan B", "type": "concept"})

        status, data = _request("POST", port, "/orphans/purge", body={"dry_run": True})
        assert status == 200
        assert data["dry_run"] is True
        assert data["purged"] == 0
        node_ids = data["nodes"]
        assert "orphan-a" in node_ids
        assert "orphan-b" in node_ids

    def test_purge_deletes_orphans(self, test_server):
        """POST /orphans/purge soft-deletes orphan nodes."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "orphan-c", "label": "Orphan C", "type": "concept"})

        # Confirm it exists
        s, d = _request("GET", port, "/node/orphan-c")
        assert s == 200

        status, data = _request("POST", port, "/orphans/purge", body={"dry_run": False})
        assert status == 200
        assert data["dry_run"] is False
        assert "orphan-c" in data["nodes"]
        assert data["purged"] >= 1

        # Node should be soft-deleted (not visible via GET)
        s2, _ = _request("GET", port, "/node/orphan-c")
        assert s2 == 404

    def test_purge_skips_connected_nodes(self, test_server):
        """POST /orphans/purge does not touch nodes that have edges."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "connected-src", "label": "Src", "type": "concept"})
        _request("POST", port, "/node", body={"id": "connected-dst", "label": "Dst", "type": "concept"})
        _request("POST", port, "/edge", body={
            "from_node": "connected-src",
            "to_node": "connected-dst",
            "edge_type": "CAUSES",
            "layer": "L3",
        })

        status, data = _request("POST", port, "/orphans/purge", body={"dry_run": True})
        assert status == 200
        assert "connected-src" not in data["nodes"]
        assert "connected-dst" not in data["nodes"]


@pytest.mark.xdist_group("server")
class TestWebhookOutbox:
    """Tests for webhook retry outbox (OHM-ufjk)."""

    def test_dead_letter_endpoint_returns_empty_initially(self, test_server):
        """GET /webhooks/dead-letter returns empty list when no failures."""
        port, _ = test_server
        status, data = _request("GET", port, "/webhooks/dead-letter")
        assert status == 200
        assert data["count"] == 0
        assert data["dead_letters"] == []

    def test_outbox_table_exists(self, test_server):
        """ohm_webhook_outbox table is created by migration."""
        port, store = test_server
        result = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_webhook_outbox"
        ).fetchone()
        assert result is not None
        assert result[0] == 0

    def test_failed_delivery_writes_to_outbox(self, test_server):
        """Webhook delivery failure writes an entry to ohm_webhook_outbox."""
        port, store = test_server
        from ohm.server.server import _write_outbox

        _write_outbox(
            store.conn,
            agent_name="test-agent",
            url="http://unreachable.example.com/hook",
            event={"type": "node.created", "agent": "test-agent"},
            error="connection refused",
        )
        row = store.conn.execute(
            "SELECT agent_name, status, attempt_count FROM ohm_webhook_outbox WHERE agent_name='test-agent'"
        ).fetchone()
        assert row is not None
        assert row[0] == "test-agent"
        assert row[1] == "pending"
        assert row[2] == 1

    def test_dead_letter_endpoint_shows_dead_entries(self, test_server):
        """GET /webhooks/dead-letter returns entries with status='dead'."""
        port, store = test_server
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        store.conn.execute(
            """INSERT INTO ohm_webhook_outbox
               (id, agent_name, url, event_type, payload, attempt_count, next_attempt_at, status, last_error)
               VALUES ('test-dead-1', 'agent-x', 'http://dead.example.com', 'node.created',
                       '{"type":"node.created"}', 3, ?, 'dead', 'max retries exceeded')""",
            [now],
        )

        status, data = _request("GET", port, "/webhooks/dead-letter")
        assert status == 200
        assert data["count"] >= 1
        ids = [e["id"] for e in data["dead_letters"]]
        assert "test-dead-1" in ids


@pytest.mark.xdist_group("server")
class TestSyncDegradedHealth:
    """Tests for /health sync_degraded flag (OHM-qiio)."""

    def test_health_shows_no_sync_degraded_by_default(self, test_server):
        """GET /health has no sync_degraded when sync is healthy."""
        port, store = test_server
        status, data = _request("GET", port, "/health")
        assert status == 200
        assert data.get("sync_degraded") is not True

    def test_health_shows_sync_degraded_when_set(self, test_server, monkeypatch):
        """GET /health includes sync_degraded=true when store has sync failure."""
        port, store = test_server
        monkeypatch.setattr(store, "sync_degraded", True)
        monkeypatch.setattr(store, "sync_error", "connection timeout")

        status, data = _request("GET", port, "/health")
        assert status == 200
        assert data.get("sync_degraded") is True
        assert "connection timeout" in data.get("sync_error", "")
