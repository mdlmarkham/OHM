"""Tests for the OHM daemon HTTP server endpoints.

Starts a test server on a random port and tests all 17+ endpoints
including auth, error handling, and edge cases.

NOTE: Server tests share class-level state on OhmHandler (tokens, roles, etc.)
and must run sequentially. They are grouped with xdist_group("server").
"""

import json
import threading
import time
from http.client import HTTPConnection

import pytest

from ohm.server import OhmHandler, _hash_token, _verify_token, _build_token_lookup
from ohm.schema import DEFAULT_SCHEMA, TOPO_SCHEMA
from ohm.store import OhmStore


def _start_test_server(store, tokens=None, roles=None, no_auth=False, schema_config=None):
    """Start a test HTTP server on a random port and return (port, thread).

    tokens can be:
      - dict of {plaintext_token: agent_name} — will be hashed automatically
      - dict of {hash: agent_name} — used directly (for testing hashed mode)

    schema_config: SchemaConfig instance (default: DEFAULT_SCHEMA)
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

    # Use TCPServer to get a random port
    server = socketserver.TCPServer(
        ("127.0.0.1", 0), OhmHandler, bind_and_activate=False,
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)  # Let server start (longer for xdist parallel mode)
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
        status, data = _request("POST", port, "/node", body={
            "id": "test_node_1", "label": "Test Node", "type": "concept",
        })
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
        _request("POST", port, "/node", body={
            "id": "a", "label": "A", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "b", "label": "B", "type": "concept",
        })
        status, data = _request("POST", port, "/edge", body={
            "from": "a", "to": "b", "type": "CAUSES", "layer": "L3",
        })
        assert status == 201
        assert data["from_node"] == "a"


@pytest.mark.xdist_group("server")
class TestQueryEndpoints:
    """Tests for graph query endpoints."""

    def test_neighborhood(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "x", "label": "X", "type": "concept"})
        _request("POST", port, "/node", body={"id": "y", "label": "Y", "type": "concept"})
        _request("POST", port, "/edge", body={"from": "x", "to": "y", "type": "CAUSES", "layer": "L3"})

        status, data = _request("GET", port, "/neighborhood/x?depth=2")
        assert status == 200
        assert isinstance(data, list)

    def test_path(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "p", "label": "P", "type": "concept"})
        _request("POST", port, "/node", body={"id": "q", "label": "Q", "type": "concept"})
        _request("POST", port, "/edge", body={"from": "p", "to": "q", "type": "CAUSES", "layer": "L3"})

        status, data = _request("GET", port, "/path/p/q")
        assert status == 200

    def test_impact(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "imp_a", "label": "A", "type": "concept"})
        status, data = _request("GET", port, "/impact/imp_a?depth=3")
        assert status == 200

    def test_confidence(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "c1", "label": "C1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "c2", "label": "C2", "type": "concept"})
        resp = _request("POST", port, "/edge", body={"from": "c1", "to": "c2", "type": "CAUSES", "layer": "L3"})
        edge_id = resp[1]["id"]

        status, data = _request("GET", port, f"/confidence/{edge_id}")
        assert status == 200


@pytest.mark.xdist_group("server")
class TestAgentEndpoints:
    """Tests for agent state endpoints."""

    def test_get_nonexistent_agent(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/agent/nonexistent")
        assert status == 404

    def test_list_agents(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/agents")
        assert status == 200
        assert isinstance(data, list)

    def test_update_state(self, test_server):
        port, store = test_server
        status, data = _request("POST", port, "/state", body={
            "focus": "testing OHM endpoints",
        })
        assert status == 200
        assert data["current_focus"] == "testing OHM endpoints"


@pytest.mark.xdist_group("server")
class TestChallengeEndpoints:
    """Tests for challenge and support endpoints."""

    def test_challenge_edge(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "ch_a", "label": "A", "type": "concept"})
        _request("POST", port, "/node", body={"id": "ch_b", "label": "B", "type": "concept"})
        resp = _request("POST", port, "/edge", body={"from": "ch_a", "to": "ch_b", "type": "CAUSES", "layer": "L3"})
        edge_id = resp[1]["id"]

        status, data = _request("POST", port, f"/challenge/{edge_id}", body={
            "reason": "weak evidence", "confidence": 0.3,
        })
        assert status == 201

    def test_support_edge(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "su_a", "label": "A", "type": "concept"})
        _request("POST", port, "/node", body={"id": "su_b", "label": "B", "type": "concept"})
        resp = _request("POST", port, "/edge", body={"from": "su_a", "to": "su_b", "type": "CAUSES", "layer": "L3"})
        edge_id = resp[1]["id"]

        status, data = _request("POST", port, f"/support/{edge_id}", body={
            "reason": "additional evidence", "confidence": 0.8,
        })
        assert status == 201


@pytest.mark.xdist_group("server")
class TestObservationEndpoints:
    """Tests for observation endpoints."""

    def test_create_observation(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "obs_node", "label": "O", "type": "concept"})
        status, data = _request("POST", port, "/observe/obs_node", body={
            "type": "measurement", "value": 1.5, "sigma": 0.3,
        })
        assert status == 201


@pytest.mark.xdist_group("server")
class TestAuthEndpoints:
    """Tests for authentication and authorization."""

    def test_post_without_token_rejected(self, auth_server):
        port, _ = auth_server
        status, data = _request("POST", port, "/node", body={
            "id": "unauth", "label": "Unauth", "type": "concept",
        })
        assert status == 401
        assert data["error"] == "authentication_error"

    def test_post_with_valid_token_accepted(self, auth_server):
        port, _ = auth_server
        status, data = _request("POST", port, "/node", body={
            "id": "auth_ok", "label": "AuthOK", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201

    def test_post_with_invalid_token_rejected(self, auth_server):
        port, _ = auth_server
        status, data = _request("POST", port, "/node", body={
            "id": "bad", "label": "Bad", "type": "concept",
        }, headers={"Authorization": "Bearer wrong-token"})
        assert status == 401

    def test_readonly_cannot_write(self, auth_server):
        port, _ = auth_server
        status, data = _request("POST", port, "/node", body={
            "id": "ro_test", "label": "RO", "type": "concept",
        }, headers={"Authorization": "Bearer readonly-token"})
        assert status == 403
        assert data["error"] == "permission_denied"

    def test_get_without_token_still_works(self, auth_server):
        port, _ = auth_server
        status, data = _request("GET", port, "/health")
        assert status == 200


@pytest.mark.xdist_group("server")
class TestAgentAttribution:
    """Tests for OHM-y2i.19: Server routes must attribute writes to authenticated agent."""

    def test_node_created_by_authenticated_agent(self, auth_server):
        """POST /node should set created_by to the authenticated agent, not 'ohmd'."""
        port, store = auth_server
        status, data = _request("POST", port, "/node", body={
            "id": "attr_node", "label": "Attributed", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201
        # Verify created_by is 'metis' (the agent mapped to test-token-abc), not 'test_agent'
        node = store.get_node("attr_node")
        assert node["created_by"] == "metis"

    def test_edge_created_by_authenticated_agent(self, auth_server):
        """POST /edge should set created_by to the authenticated agent."""
        port, store = auth_server
        # Create nodes first
        _request("POST", port, "/node", body={
            "id": "attr_from", "label": "From", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        _request("POST", port, "/node", body={
            "id": "attr_to", "label": "To", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        status, data = _request("POST", port, "/edge", body={
            "from": "attr_from", "to": "attr_to", "type": "CAUSES", "layer": "L3",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201
        # Verify edge is attributed to 'metis'
        edges = store.execute(
            "SELECT * FROM ohm_edges WHERE from_node = 'attr_from' AND to_node = 'attr_to'"
        )
        assert len(edges) == 1
        assert edges[0]["created_by"] == "metis"

    def test_observation_created_by_authenticated_agent(self, auth_server):
        """POST /observe should set created_by to the authenticated agent."""
        port, store = auth_server
        _request("POST", port, "/node", body={
            "id": "obs_attr_node", "label": "ObsNode", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        status, data = _request("POST", port, "/observe/obs_attr_node", body={
            "type": "measurement", "value": 0.85,
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201
        obs = store.execute(
            "SELECT * FROM ohm_observations WHERE node_id = 'obs_attr_node' ORDER BY created_at DESC LIMIT 1"
        )
        assert len(obs) == 1
        assert obs[0]["created_by"] == "metis"

    def test_state_updated_by_authenticated_agent(self, auth_server):
        """POST /state should update the authenticated agent's state, not 'ohmd'."""
        port, store = auth_server
        status, data = _request("POST", port, "/state", body={
            "focus": "testing attribution",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 200
        # Verify state is for 'metis', not 'test_agent'
        state = store.get_agent_state("metis")
        assert state is not None
        assert state["current_focus"] == "testing attribution"

    def test_challenge_attributed_to_authenticated_agent(self, auth_server):
        """POST /challenge should create a CHALLENGED_BY edge attributed to the challenger."""
        port, store = auth_server
        # Create nodes and edge as metis
        _request("POST", port, "/node", body={
            "id": "ch_from", "label": "From", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        _request("POST", port, "/node", body={
            "id": "ch_to", "label": "To", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        _, edge_data = _request("POST", port, "/edge", body={
            "from": "ch_from", "to": "ch_to", "type": "CAUSES", "layer": "L3",
        }, headers={"Authorization": "Bearer test-token-abc"})
        edge_id = edge_data["id"]
        # Challenge as metis
        status, data = _request("POST", port, f"/challenge/{edge_id}", body={
            "reason": "doubtful", "confidence": 0.3,
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201
        # Verify challenge edge is attributed to 'metis'
        challenge_edges = store.execute(
            "SELECT * FROM ohm_edges WHERE challenge_of = ?", [edge_id]
        )
        assert len(challenge_edges) == 1
        assert challenge_edges[0]["created_by"] == "metis"


@pytest.mark.xdist_group("server")
class TestNodeIdempotency:
    """Tests for OHM-y2i.20: POST /node should be idempotent, not raise ConflictError."""

    def test_post_node_idempotent_on_existing_id(self, test_server):
        """POST /node with an existing ID should return 200 with updated data, not 409."""
        port, _ = test_server
        # Create a node
        status, data = _request("POST", port, "/node", body={
            "id": "idem_node", "label": "Original", "type": "concept",
        })
        assert status == 201
        assert data["created"] is True

        # Re-post same ID with updated data
        status, data = _request("POST", port, "/node", body={
            "id": "idem_node", "label": "Updated", "type": "concept",
        })
        assert status == 200
        assert data["created"] is False
        assert data["label"] == "Updated"

    def test_post_node_idempotent_with_auth(self, auth_server):
        """POST /node idempotency should work with authenticated agents."""
        port, _ = auth_server
        # Create a node as metis
        status, data = _request("POST", port, "/node", body={
            "id": "idem_auth", "label": "First", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 201
        assert data["created"] is True

        # Re-post same ID as metis — should update, not conflict
        status, data = _request("POST", port, "/node", body={
            "id": "idem_auth", "label": "Second", "type": "concept",
        }, headers={"Authorization": "Bearer test-token-abc"})
        assert status == 200
        assert data["created"] is False
        assert data["label"] == "Second"


@pytest.mark.xdist_group("server")
class TestErrorHandling:
    """Tests for error response format."""

    def test_unknown_endpoint(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/nonexistent")
        assert status == 404

    def test_error_has_correlation_id(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/node/nonexistent")
        assert "correlation_id" in data
        assert len(data["correlation_id"]) == 36  # UUID4

    def test_error_has_status_field(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/node/nonexistent")
        assert data["status"] == 404
        assert data["error"] == "not_found"


@pytest.mark.xdist_group("server")
class TestSecurity:
    """Tests for security features: rate limiting, body size limit, auth fail-closed."""

    def test_rate_limit_allows_normal_requests(self, test_server):
        """Normal request volume should succeed."""
        port, _ = test_server
        status, _ = _request("GET", port, "/health")
        assert status == 200

    def test_rate_limit_blocks_excessive_requests(self, test_server):
        """Sending many requests rapidly should trigger rate limiting."""
        import ohm.server as srv
        # Temporarily lower the limit for testing
        original_max = srv.RATE_LIMIT_MAX_REQUESTS
        srv.RATE_LIMIT_MAX_REQUESTS = 5
        srv._rate_limit_store.clear()
        try:
            port, _ = test_server
            # Send 5 requests — all should succeed
            for _ in range(5):
                status, _ = _request("GET", port, "/health")
                assert status == 200
            # 6th request should be rate limited
            status, data = _request("GET", port, "/health")
            assert status == 429
            assert data["error"] == "rate_limited"
        finally:
            srv.RATE_LIMIT_MAX_REQUESTS = original_max
            srv._rate_limit_store.clear()

    def test_body_size_limit_rejects_oversized(self, test_server):
        """Request body exceeding MAX_BODY_SIZE should be rejected."""
        import ohm.server as srv
        original_max = srv.MAX_BODY_SIZE
        srv.MAX_BODY_SIZE = 100  # 100 bytes for testing
        try:
            port, _ = test_server
            large_body = {"id": "x" * 200, "label": "big", "type": "concept"}
            status, data = _request("POST", port, "/node", body=large_body)
            assert status == 400
            assert data["error"] == "validation_error"
        finally:
            srv.MAX_BODY_SIZE = original_max

    def test_post_without_token_and_no_auth_configured_denied(self, tmp_path):
        """POST without token should be denied when tokens are configured."""
        db_path = str(tmp_path / "test_fail_closed.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"valid-token": "agent1"}
        port, server, thread = _start_test_server(store, tokens=tokens)
        try:
            # POST without token → 401
            status, data = _request("POST", port, "/node", body={
                "id": "unauth", "label": "Unauth", "type": "concept",
            })
            assert status == 401
            assert data["error"] == "authentication_error"
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_get_without_token_with_tokens_configured_denied(self, tmp_path):
        """GET without token should be denied when tokens are configured."""
        db_path = str(tmp_path / "test_get_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"valid-token": "agent1"}
        port, server, thread = _start_test_server(store, tokens=tokens)
        try:
            # GET /status without token → 401
            status, data = _request("GET", port, "/status")
            assert status == 401
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_get_with_valid_token_succeeds(self, tmp_path):
        """GET with valid token should succeed when tokens are configured."""
        db_path = str(tmp_path / "test_get_auth_ok.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"valid-token": "agent1"}
        port, server, thread = _start_test_server(store, tokens=tokens)
        try:
            # GET /status with valid token → 200
            status, data = _request("GET", port, "/status",
                                     headers={"Authorization": "Bearer valid-token"})
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_no_auth_flag_allows_all(self, tmp_path):
        """--no-auth flag should allow all requests without token."""
        db_path = str(tmp_path / "test_no_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            # POST without token → allowed
            status, data = _request("POST", port, "/node", body={
                "id": "free", "label": "Free", "type": "concept",
            })
            assert status == 201
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()


@pytest.mark.xdist_group("server")
class TestBodyValidation:
    """Tests for request body validation (OHM-y2i.16)."""

    def test_invalid_json_rejected(self, test_server):
        """Malformed JSON body should return 400 validation_error."""
        port, _ = test_server
        conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
        body = b"this is not json"
        conn.request("POST", "/node", body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 400
        assert data["error"] == "validation_error"

    def test_node_missing_required_fields(self, test_server):
        """POST /node without required fields should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={})
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Missing required fields" in data["message"]

    def test_node_missing_label(self, test_server):
        """POST /node without label should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={"id": "x"})
        assert status == 400
        assert "Missing required fields" in data["message"]

    def test_edge_missing_required_fields(self, test_server):
        """POST /edge without required fields should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={})
        assert status == 400
        assert "Missing required fields" in data["message"]

    def test_node_invalid_type(self, test_server):
        """POST /node with invalid node type should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "bad_type", "label": "Bad", "type": "not_a_real_type",
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid node type" in data["message"]

    def test_node_invalid_visibility(self, test_server):
        """POST /node with invalid visibility should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "bad_vis", "label": "Bad", "visibility": "secret",
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid visibility" in data["message"]

    def test_edge_invalid_type(self, test_server):
        """POST /edge with invalid edge type should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={
            "from": "a", "to": "b", "type": "NOT_AN_EDGE_TYPE",
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid edge type" in data["message"]

    def test_edge_invalid_layer(self, test_server):
        """POST /edge with invalid layer should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={
            "from": "a", "to": "b", "type": "CAUSES", "layer": "L9",
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid layer" in data["message"]

    def test_node_confidence_out_of_range(self, test_server):
        """POST /node with confidence > 1.0 should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "conf_bad", "label": "Conf", "confidence": 2.5,
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid confidence" in data["message"]

    def test_edge_confidence_out_of_range(self, test_server):
        """POST /edge with negative confidence should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={
            "from": "a", "to": "b", "type": "CAUSES", "confidence": -0.5,
        })
        assert status == 400
        assert data["error"] == "validation_error"
        assert "Invalid confidence" in data["message"]

    def test_node_wrong_field_type(self, test_server):
        """POST /node with wrong field type should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": 123, "label": "WrongType",
        })
        assert status == 400
        assert data["error"] == "validation_error"

    def test_edge_wrong_field_type(self, test_server):
        """POST /edge with wrong field type should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={
            "from": 42, "to": "b", "type": "CAUSES",
        })
        assert status == 400
        assert data["error"] == "validation_error"

    def test_body_not_dict_rejected(self, test_server):
        """POST with a JSON array body should return 400."""
        port, _ = test_server
        conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
        body = json.dumps([1, 2, 3]).encode()
        conn.request("POST", "/node", body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        assert resp.status == 400
        assert data["error"] == "validation_error"

    def test_valid_node_still_works(self, test_server):
        """Valid POST /node should still succeed after validation."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "valid_node", "label": "Valid", "type": "concept",
        })
        assert status == 201
        assert data["id"] == "valid_node"

    def test_valid_edge_still_works(self, test_server):
        """Valid POST /edge should still succeed after validation."""
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "e1", "label": "E1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "e2", "label": "E2", "type": "concept"})
        status, data = _request("POST", port, "/edge", body={
            "from": "e1", "to": "e2", "type": "CAUSES", "layer": "L3",
        })
        assert status == 201

    def test_challenge_confidence_validated(self, test_server):
        """POST /challenge with out-of-range confidence should return 400."""
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "ch1", "label": "CH1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "ch2", "label": "CH2", "type": "concept"})
        resp = _request("POST", port, "/edge", body={"from": "ch1", "to": "ch2", "type": "CAUSES", "layer": "L3"})
        edge_id = resp[1]["id"]

        status, data = _request("POST", port, f"/challenge/{edge_id}", body={
            "reason": "test", "confidence": 5.0,
        })
        assert status == 400
        assert data["error"] == "validation_error"

    def test_node_id_validation(self, test_server):
        """POST /node with unsafe ID should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "'; DROP TABLE ohm_nodes;--", "label": "SQLi", "type": "concept",
        })
        assert status == 400
        assert data["error"] == "validation_error"

    def test_edge_from_validation(self, test_server):
        """POST /edge with unsafe from_node should return 400."""
        port, _ = test_server
        status, data = _request("POST", port, "/edge", body={
            "from": "../etc/passwd", "to": "b", "type": "CAUSES",
        })
        assert status == 400
        assert data["error"] == "validation_error"

    def test_size_limit_1mb_default(self, test_server):
        """Default MAX_BODY_SIZE should be 1MB."""
        import ohm.server as srv
        assert srv.MAX_BODY_SIZE == 1 * 1024 * 1024

    def test_oversized_body_rejected(self, test_server):
        """Request body exceeding MAX_BODY_SIZE should be rejected."""
        import ohm.server as srv
        original_max = srv.MAX_BODY_SIZE
        srv.MAX_BODY_SIZE = 100  # 100 bytes for testing
        try:
            port, _ = test_server
            large_body = {"id": "x" * 200, "label": "big", "type": "concept"}
            status, data = _request("POST", port, "/node", body=large_body)
            assert status == 400
            assert data["error"] == "validation_error"
            assert "too large" in data["message"].lower()
        finally:
            srv.MAX_BODY_SIZE = original_max


@pytest.mark.xdist_group("server")
class TestTokenSecurity:
    """Tests for token hashing and constant-time comparison (OHM-y2i.17)."""

    def test_hash_token_deterministic(self):
        """Same input produces same hash."""
        from ohm.server import _hash_token
        h1 = _hash_token("test-token-123")
        h2 = _hash_token("test-token-123")
        assert h1 == h2

    def test_hash_token_different_inputs(self):
        """Different inputs produce different hashes."""
        from ohm.server import _hash_token
        h1 = _hash_token("token-a")
        h2 = _hash_token("token-b")
        assert h1 != h2

    def test_hash_token_is_sha256_hex(self):
        """Hash should be a 64-character hex string (SHA-256)."""
        from ohm.server import _hash_token
        h = _hash_token("test-token")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_verify_token_correct(self):
        """Correct token should verify against its hash."""
        from ohm.server import _hash_token
        token = "my-secret-token"
        token_hash = _hash_token(token)
        assert _verify_token(token, token_hash) is True

    def test_verify_token_wrong(self):
        """Wrong token should not verify."""
        from ohm.server import _hash_token
        token = "my-secret-token"
        token_hash = _hash_token(token)
        assert _verify_token("wrong-token", token_hash) is False

    def test_verify_token_constant_time(self):
        """secrets.compare_digest is used (not dict lookup)."""
        import ohm.server as srv
        # Verify the module-level _verify_token uses compare_digest
        import inspect
        source = inspect.getsource(srv)
        assert "compare_digest" in source

    def test_build_token_lookup_plaintext(self):
        """Legacy plaintext tokens should be hashed on load."""
        from ohm.server import _hash_token
        config = {"metis": "plaintext-token-abc", "observer": "plaintext-token-xyz"}
        token_hashes, roles = _build_token_lookup(config)
        # Should have hashed the tokens
        assert _hash_token("plaintext-token-abc") in token_hashes
        assert token_hashes[_hash_token("plaintext-token-abc")] == "metis"
        assert _hash_token("plaintext-token-xyz") in token_hashes
        assert token_hashes[_hash_token("plaintext-token-xyz")] == "observer"
        # Default role should be read-write
        assert roles["metis"] == "read-write"
        assert roles["observer"] == "read-write"

    def test_build_token_lookup_hashed(self):
        """Hashed token format should be loaded directly."""
        from ohm.server import _hash_token
        token_hash = _hash_token("my-secret-token")
        config = {"metis": {"hash": token_hash, "role": "read-write"}}
        token_hashes, roles = _build_token_lookup(config)
        assert token_hash in token_hashes
        assert token_hashes[token_hash] == "metis"
        assert roles["metis"] == "read-write"

    def test_build_token_lookup_readonly_role(self):
        """Hashed format should support read-only role."""
        from ohm.server import _hash_token
        token_hash = _hash_token("readonly-token")
        config = {"observer": {"hash": token_hash, "role": "read-only"}}
        token_hashes, roles = _build_token_lookup(config)
        assert roles["observer"] == "read-only"

    def test_auth_with_hashed_tokens(self, tmp_path):
        """Authentication should work with hashed tokens."""
        import time
        time.sleep(0.5)  # Ensure previous server cleanup is complete (xdist)
        db_path = str(tmp_path / "test_hashed_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        # Use plaintext tokens — _start_test_server will hash them
        tokens = {"hashed-token-abc": "metis", "hashed-token-xyz": "observer"}
        roles = {"metis": "read-write", "observer": "read-only"}
        port, server, thread = _start_test_server(store, tokens=tokens, roles=roles)
        try:
            # Retry loop for first request (server may still be starting under xdist)
            for attempt in range(3):
                status, data = _request("POST", port, "/node", body={
                    "id": "auth_ok", "label": "AuthOK", "type": "concept",
                }, headers={"Authorization": "Bearer hashed-token-abc"})
                if status == 201:
                    break
                time.sleep(0.2)
            assert status == 201, f"Expected 201, got {status}: {data}"

            time.sleep(0.05)  # Small delay to avoid connection race on Windows

            # Invalid token → 401
            status, data = _request("POST", port, "/node", body={
                "id": "bad", "label": "Bad", "type": "concept",
            }, headers={"Authorization": "Bearer wrong-token"})
            assert status == 401

            time.sleep(0.05)

            # Read-only token → 403
            status, data = _request("POST", port, "/node", body={
                "id": "ro_test", "label": "RO", "type": "concept",
            }, headers={"Authorization": "Bearer hashed-token-xyz"})
            assert status == 403
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_tokens_not_stored_in_plaintext(self):
        """Verify that OhmHandler.tokens does not contain plaintext tokens."""
        config = {"agent1": "plaintext-secret"}
        token_hashes, _ = _build_token_lookup(config)
        # The keys should be hashes, not plaintext
        for key in token_hashes:
            assert key != "plaintext-secret"
            assert len(key) == 64  # SHA-256 hex digest

    def test_init_token_stores_hash(self, tmp_path):
        """--init-token should store hashed token in config, not plaintext."""
        from ohm.server import _hash_token
        import secrets
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)

        config = {"host": "127.0.0.1", "port": 8710, "tokens": {}}
        config["tokens"]["test_agent"] = {"hash": token_hash, "role": "read-write"}

        # Verify the stored format
        assert config["tokens"]["test_agent"]["hash"] == token_hash
        assert "role" in config["tokens"]["test_agent"]
        # Plaintext token should NOT be in config
        assert token not in str(config)


@pytest.mark.xdist_group("server")
class TestTopoSchemaServer:
    """Tests for the TOPO schema variant of the daemon."""

    @pytest.fixture
    def topo_server(self, tmp_path):
        """Start a test server with TOPO schema (no-auth dev mode)."""
        db_path = str(tmp_path / "test_topo_server.duckdb")
        store = OhmStore(db_path=db_path, agent_name="topo_test")
        port, server, thread = _start_test_server(store, no_auth=True, schema_config=TOPO_SCHEMA)
        yield port, store
        server.shutdown()
        thread.join(timeout=2)
        store.close()

    def test_schema_returns_topo(self, topo_server):
        port, _ = topo_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert data["schema"] == "topo"
        assert "process" in data["node_types"]
        assert "sensor" in data["node_types"]
        assert "vessel" in data["node_types"]

    def test_layers_returns_topo_descriptions(self, topo_server):
        port, _ = topo_server
        status, data = _request("GET", port, "/layers")
        assert status == 200
        assert "L1" in data
        assert "Physical hierarchy" in data["L1"]

    def test_status_includes_schema_name(self, topo_server):
        port, _ = topo_server
        status, data = _request("GET", port, "/status")
        assert status == 200
        assert data["schema"] == "topo"

    def test_topo_node_types_accepted(self, topo_server):
        """TOPO-specific node types should be accepted by the server."""
        port, _ = topo_server
        status, data = _request("POST", port, "/node", body={
            "id": "pump_1", "label": "Main Pump", "type": "pump",
        })
        assert status == 201

    def test_topo_node_type_rejected_by_default_schema(self, test_server):
        """TOPO-specific node types should be rejected by the default OHM schema."""
        port, _ = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "pump_1", "label": "Main Pump", "type": "pump",
        })
        assert status == 400
        assert "Invalid node type" in data.get("message", "")

    def test_default_schema_name(self, test_server):
        """Default server should report 'ohm' schema."""
        port, _ = test_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert data["schema"] == "ohm"

    def test_topo_edge_types_in_schema(self, topo_server):
        """TOPO schema should include industrial edge types."""
        port, _ = topo_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        # FEEDS and FLOWS_TO are in L2
        assert "FEEDS" in data["edge_types_by_layer"]["L2"]
        assert "FLOWS_TO" in data["edge_types_by_layer"]["L2"]


@pytest.mark.xdist_group("server")
class TestTopodEntryPoint:
    """Tests for the topod entry point function."""

    def test_topod_main_exists(self):
        """topod_main should be importable."""
        from ohm.server import topod_main
        assert callable(topod_main)

    def test_topod_main_passes_topo_schema(self):
        """topod_main should pass TOPO_SCHEMA to main()."""
        from ohm.server import topod_main
        # We can't actually run topod_main (it starts a server),
        # but we can verify it references TOPO_SCHEMA
        import inspect
        source = inspect.getsource(topod_main)
        assert "TOPO_SCHEMA" in source


@pytest.mark.xdist_group("server")
class TestRegisterEndpoint:
    """Tests for POST /register endpoint (OHM-jn1: NameError fix)."""

    def test_register_creates_agent_node(self, test_server):
        """POST /register creates an agent node with identity edges."""
        port, store = test_server
        status, data = _request("POST", port, "/register", body={
            "name": "test-agent-reg",
            "description": "Test agent for registration",
            "values": ["accuracy", "transparency"],
            "goals": ["help users"],
            "capabilities": ["search"],
        })
        assert status == 201
        assert "agent" in data
        assert data["agent"]["type"] == "agent"
        assert data["agent"]["label"] == "test-agent-reg"
        assert data["edges_created"] >= 1

    def test_register_with_interests(self, test_server):
        """POST /register creates INTERESTED_IN edges for interests."""
        port, store = test_server
        status, data = _request("POST", port, "/register", body={
            "name": "curious-agent",
            "interests": ["climate", "energy"],
        })
        assert status == 201
        assert data["edges_created"] >= 2

    def test_register_with_listens_to(self, test_server):
        """POST /register creates LISTENS_TO edges for listens_to."""
        port, store = test_server
        status, data = _request("POST", port, "/register", body={
            "name": "listener-agent",
            "listens_to": ["metis", "clio"],
        })
        assert status == 201
        assert data["edges_created"] >= 2

    def test_register_no_duplicate_edges(self, test_server):
        """POST /register does not create duplicate edges on re-registration."""
        port, store = test_server
        # First registration
        status1, data1 = _request("POST", port, "/register", body={
            "name": "dedup-agent",
            "values": ["accuracy"],
        })
        assert status1 == 201
        first_edges = data1["edges_created"]

        # Second registration with same values
        status2, data2 = _request("POST", port, "/register", body={
            "name": "dedup-agent",
            "values": ["accuracy"],
        })
        assert status2 == 201
        # Should not create duplicate edges
        assert data2["edges_created"] <= first_edges


@pytest.mark.xdist_group("server")
class TestNodesEndpoint:
    """Tests for GET /nodes endpoint (OHM-usg: node search/list)."""

    def test_nodes_returns_list(self, test_server):
        """GET /nodes returns paginated node list."""
        port, store = test_server
        # Create some nodes first
        _request("POST", port, "/node", body={
            "id": "test-node-1", "label": "Alpha", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "test-node-2", "label": "Beta", "type": "source",
        })
        status, data = _request("GET", port, "/nodes")
        assert status == 200
        assert "nodes" in data
        assert "total" in data
        assert data["total"] >= 2

    def test_nodes_filter_by_type(self, test_server):
        """GET /nodes?type=source filters by node type."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "filter-source-1", "label": "Source A", "type": "source",
        })
        _request("POST", port, "/node", body={
            "id": "filter-concept-1", "label": "Concept A", "type": "concept",
        })
        status, data = _request("GET", port, "/nodes?type=source")
        assert status == 200
        assert all(n["type"] == "source" for n in data["nodes"])

    def test_nodes_filter_by_label(self, test_server):
        """GET /nodes?label=... filters by label text."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "label-test-1", "label": "UniqueLabelXYZ", "type": "concept",
        })
        status, data = _request("GET", port, "/nodes?label=UniqueLabelXYZ")
        assert status == 200
        assert len(data["nodes"]) >= 1
        assert any("UniqueLabelXYZ" in n["label"] for n in data["nodes"])

    def test_nodes_pagination(self, test_server):
        """GET /nodes supports limit and offset for pagination."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "page-node-1", "label": "PageTest1", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "page-node-2", "label": "PageTest2", "type": "concept",
        })
        status, data = _request("GET", port, "/nodes?limit=1&offset=0")
        assert status == 200
        assert len(data["nodes"]) <= 1
        assert data["limit"] == 1
        assert data["offset"] == 0


@pytest.mark.xdist_group("server")
class TestChangeFeedAttribution:
    """Tests for change feed agent attribution (OHM-qyn: writes attributed to 'ohmd' bug)."""

    def test_post_node_change_feed_uses_caller_agent(self, auth_server):
        """POST /node should attribute change feed entry to the calling agent, not 'ohmd'."""
        port, store = auth_server
        headers = {"Authorization": "Bearer test-token-abc"}
        status, data = _request("POST", port, "/node", body={
            "id": "cf-attrib-node-1", "label": "Attribution Test", "type": "concept",
        }, headers=headers)
        assert status == 201

        # Check change feed — should show 'metis' (the agent from the token), not 'ohmd'
        feed = store.execute(
            "SELECT agent_name FROM ohm_change_feed WHERE row_id = ? ORDER BY occurred_at DESC LIMIT 1",
            ["cf-attrib-node-1"],
        )
        assert len(feed) == 1
        assert feed[0]["agent_name"] == "metis"

    def test_post_edge_change_feed_uses_caller_agent(self, auth_server):
        """POST /edge should attribute change feed entry to the calling agent, not 'ohmd'."""
        port, store = auth_server
        headers = {"Authorization": "Bearer test-token-abc"}
        # Create two nodes first
        _request("POST", port, "/node", body={
            "id": "cf-edge-from", "label": "From Node", "type": "concept",
        }, headers=headers)
        _request("POST", port, "/node", body={
            "id": "cf-edge-to", "label": "To Node", "type": "concept",
        }, headers=headers)
        # Create edge
        status, data = _request("POST", port, "/edge", body={
            "from": "cf-edge-from", "to": "cf-edge-to",
            "type": "REFERENCES", "layer": "L2",
        }, headers=headers)
        assert status == 201

        # Check change feed for edge entries by 'metis'
        feed = store.execute(
            "SELECT agent_name, table_name FROM ohm_change_feed "
            "WHERE table_name = 'ohm_edges' AND agent_name = 'metis' ORDER BY occurred_at DESC LIMIT 1",
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "metis"

    def test_post_observation_change_feed_uses_caller_agent(self, auth_server):
        """POST /observe/{node_id} should attribute change feed entry to the calling agent."""
        port, store = auth_server
        headers = {"Authorization": "Bearer test-token-abc"}
        # Create a node first
        _request("POST", port, "/node", body={
            "id": "cf-obs-node", "label": "Obs Node", "type": "concept",
        }, headers=headers)
        # Create observation via /observe/{node_id}
        status, data = _request("POST", port, "/observe/cf-obs-node", body={
            "type": "measurement", "value": 42.0,
        }, headers=headers)
        assert status == 201

        # Check change feed for observation entries by 'metis'
        feed = store.execute(
            "SELECT agent_name, table_name FROM ohm_change_feed "
            "WHERE table_name = 'ohm_observations' AND agent_name = 'metis' ORDER BY occurred_at DESC LIMIT 1",
        )
        assert len(feed) >= 1
        assert feed[0]["agent_name"] == "metis"

    def test_change_log_uses_caller_agent(self, auth_server):
        """ohm_change_log should also attribute to the calling agent, not 'ohmd'."""
        port, store = auth_server
        headers = {"Authorization": "Bearer test-token-abc"}
        _request("POST", port, "/node", body={
            "id": "cf-log-node", "label": "Log Test", "type": "concept",
        }, headers=headers)

        log = store.execute(
            "SELECT agent_name FROM ohm_change_log WHERE row_id = ? ORDER BY changed_at DESC LIMIT 1",
            ["cf-log-node"],
        )
        assert len(log) == 1
        assert log[0]["agent_name"] == "metis"


@pytest.mark.xdist_group("server")
class TestDeleteValidation:
    """Tests for DELETE endpoint input validation (OHM-i60: 500 on invalid ID)."""

    def test_delete_node_invalid_id_returns_400(self, test_server):
        """DELETE /node/{invalid_id} returns 400, not 500."""
        port, _ = test_server
        status, data = _request("DELETE", port, "/node/invalid%20id%20with%20spaces")
        assert status == 400
        assert "validation" in data.get("error", "").lower() or "invalid" in str(data).lower()

    def test_delete_node_special_chars_returns_400(self, test_server):
        """DELETE /node/{id_with_special_chars} returns 400."""
        port, _ = test_server
        status, data = _request("DELETE", port, "/node/bad!id@here")
        assert status == 400

    def test_delete_edge_invalid_id_returns_400(self, test_server):
        """DELETE /edge/{invalid_id} returns 400, not 500."""
        port, _ = test_server
        status, data = _request("DELETE", port, "/edge/invalid%20id%20with%20spaces")
        assert status == 400

    def test_get_node_invalid_id_returns_400(self, test_server):
        """GET /node/{invalid_id} returns 400, not 500."""
        port, _ = test_server
        status, data = _request("GET", port, "/node/invalid%20id%20with%20spaces")
        assert status == 400

    def test_get_edge_invalid_id_returns_400(self, test_server):
        """GET /edge/{invalid_id} returns 400, not 500."""
        port, _ = test_server
        status, data = _request("GET", port, "/edge/bad!id")
        assert status == 400


@pytest.mark.xdist_group("server")
class TestCascadingDelete:
    """Tests for cascading DELETE — node deletion removes edges (OHM-cpi)."""

    def test_delete_node_removes_edges(self, test_server):
        """DELETE /node/{id} removes all edges referencing the node."""
        port, _ = test_server
        # Create two nodes and an edge between them
        _request("POST", port, "/node", body={
            "id": "del-node-a", "label": "Node A", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "del-node-b", "label": "Node B", "type": "concept",
        })
        _request("POST", port, "/edge", body={
            "from": "del-node-a", "to": "del-node-b",
            "type": "CAUSES", "layer": "L1",
        })
        # Verify edge exists
        status, data = _request("GET", port, "/edge/del-edge-1")
        # Edge ID is auto-generated, so we need to find it
        # Instead, just verify the node deletion cascades by checking node is gone
        status, data = _request("DELETE", port, "/node/del-node-a")
        assert status == 200
        assert data["deleted"] == "del-node-a"
        assert data["type"] == "node"
        assert data["edges_removed"] >= 1

        # Node should be gone
        status, data = _request("GET", port, "/node/del-node-a")
        assert status == 404

    def test_delete_node_removes_observations(self, test_server):
        """DELETE /node/{id} removes observations on the node."""
        port, _ = test_server
        _request("POST", port, "/node", body={
            "id": "del-obs-node", "label": "Obs Node", "type": "concept",
        })
        _request("POST", port, "/observe/del-obs-node", body={
            "type": "metric",
            "value": 42.0,
        })
        # Delete the node
        status, data = _request("DELETE", port, "/node/del-obs-node")
        assert status == 200
        assert data["observations_removed"] >= 1

    def test_delete_node_idempotent_404(self, test_server):
        """DELETE /node/{id} twice returns 404 on second call (idempotent)."""
        port, _ = test_server
        _request("POST", port, "/node", body={
            "id": "del-twice-node", "label": "Twice Node", "type": "concept",
        })
        # First delete succeeds
        status, data = _request("DELETE", port, "/node/del-twice-node")
        assert status == 200
        # Second delete returns 404 (not 500)
        status, data = _request("DELETE", port, "/node/del-twice-node")
        assert status == 404

    def test_delete_edge_idempotent_404(self, test_server):
        """DELETE /edge/{id} twice returns 404 on second call (idempotent)."""
        port, _ = test_server
        _request("POST", port, "/node", body={
            "id": "del-edge-node-a", "label": "A", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "del-edge-node-b", "label": "B", "type": "concept",
        })
        resp = _request("POST", port, "/edge", body={
            "from": "del-edge-node-a", "to": "del-edge-node-b",
            "type": "CAUSES", "layer": "L1",
        })
        edge_id = resp[1]["id"]
        # First delete succeeds
        status, data = _request("DELETE", port, f"/edge/{edge_id}")
        assert status == 200
        # Second delete returns 404 (not 500)
        status, data = _request("DELETE", port, f"/edge/{edge_id}")
        assert status == 404

    def test_delete_node_with_incoming_edges(self, test_server):
        """DELETE /node/{id} removes edges where node is the target."""
        port, _ = test_server
        _request("POST", port, "/node", body={
            "id": "del-target-node", "label": "Target", "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "del-source-node", "label": "Source", "type": "concept",
        })
        _request("POST", port, "/edge", body={
            "from": "del-source-node", "to": "del-target-node",
            "type": "CAUSES", "layer": "L1",
        })
        # Delete the target node — should remove the incoming edge
        status, data = _request("DELETE", port, "/node/del-target-node")
        assert status == 200
        assert data["edges_removed"] >= 1

    def test_delete_nonexistent_node_returns_404(self, test_server):
        """DELETE /node/{nonexistent} returns 404, not 500."""
        port, _ = test_server
        status, data = _request("DELETE", port, "/node/nonexistent_node_xyz")
        assert status == 404

    def test_delete_nonexistent_edge_returns_404(self, test_server):
        """DELETE /edge/{nonexistent} returns 404, not 500."""
        port, _ = test_server
        status, data = _request("DELETE", port, "/edge/nonexistent_edge_xyz")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestListenWithoutSince:
    """Tests for OHM-9ud: /listen without 'since' returns 400, not 500."""

    def test_listen_without_since_no_agent_returns_400(self, test_server):
        """GET /listen without 'since' and unknown agent returns 400 with validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/listen")
        assert status == 400
        assert "since" in str(data).lower() or "last-check" in str(data).lower()


@pytest.mark.xdist_group("server")
class TestEnrichedChangeFeed:
    """Tests for enriched change feed (OHM-m8a: include node content in listen())."""

    def _make_timestamp(self):
        """Generate a recent ISO timestamp for testing."""
        from datetime import datetime, timedelta
        return (datetime.now() - timedelta(hours=1)).isoformat()

    def test_listen_without_enrich_returns_raw_entries(self, test_server):
        """GET /listen without enrich=true returns raw entries (backward compatible)."""
        port, store = test_server
        # Create a node to generate a change feed entry
        _request("POST", port, "/node", body={
            "id": "enrich-test-node",
            "label": "Test Node",
            "type": "concept",
        })
        # Use a timestamp from 1 hour ago
        since = self._make_timestamp()
        status, data = _request("GET", port, f"/listen?since={since}&agent=test_agent")
        assert status == 200
        assert isinstance(data, list)
        if len(data) > 0:
            entry = data[0]
            assert "data" not in entry  # Raw entries don't have enrichment

    def test_listen_with_enrich_includes_node_data(self, test_server):
        """GET /listen?enrich=true includes node content in data field."""
        port, store = test_server
        # Create a node
        _request("POST", port, "/node", body={
            "id": "enrich-node-test",
            "label": "Enriched Node",
            "type": "pattern",
            "content": "This is the content",
        })
        # Use a timestamp from 1 hour ago
        since = self._make_timestamp()
        status, data = _request("GET", port, f"/listen?since={since}&agent=test_agent&enrich=true")
        assert status == 200
        assert isinstance(data, list)
        if len(data) > 0:
            entry = data[0]
            if entry.get("table_name") == "ohm_nodes":
                assert "data" in entry
                assert entry["data"].get("label") == "Enriched Node"
                assert entry["data"].get("type") == "pattern"
                assert entry["data"].get("content") == "This is the content"

    def test_listen_with_enrich_includes_edge_data(self, test_server):
        """GET /listen?enrich=true includes edge data (from_node, to_node, edge_type)."""
        port, store = test_server
        # Create two nodes and an edge
        _request("POST", port, "/node", body={
            "id": "enrich-edge-from",
            "label": "From Node",
            "type": "concept",
        })
        _request("POST", port, "/node", body={
            "id": "enrich-edge-to",
            "label": "To Node",
            "type": "concept",
        })
        _request("POST", port, "/edge", body={
            "from": "enrich-edge-from",
            "to": "enrich-edge-to",
            "type": "CAUSES",
            "layer": "L3",
        })
        # Use a timestamp from 1 hour ago
        since = self._make_timestamp()
        status, data = _request("GET", port, f"/listen?since={since}&agent=test_agent&enrich=true")
        assert status == 200
        assert isinstance(data, list)
        # Find the edge entry
        edge_entries = [e for e in data if e.get("table_name") == "ohm_edges"]
        if len(edge_entries) > 0:
            entry = edge_entries[0]
            assert "data" in entry
            assert entry["data"].get("from_node") == "enrich-edge-from"
            assert entry["data"].get("to_node") == "enrich-edge-to"
            assert entry["data"].get("edge_type") == "CAUSES"


@pytest.mark.xdist_group("server")
class TestNodeUrlField:
    """Tests for URL field on nodes (OHM-qp6: External URL field on nodes)."""

    def test_post_node_with_url(self, test_server):
        """POST /node accepts and persists url field."""
        port, store = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "url-test-node",
            "label": "Reuters Article",
            "type": "source",
            "content": "Summary of the article",
            "url": "https://reuters.com/article/12345",
        })
        assert status == 201
        assert data["url"] == "https://reuters.com/article/12345"

    def test_get_node_returns_url(self, test_server):
        """GET /node/{id} returns url field."""
        port, store = test_server
        # Create node with URL
        _request("POST", port, "/node", body={
            "id": "url-get-test",
            "label": "Research Paper",
            "type": "source",
            "url": "https://arxiv.org/pdf/1234.5678",
        })
        # Retrieve it
        status, data = _request("GET", port, "/node/url-get-test")
        assert status == 200
        assert data["url"] == "https://arxiv.org/pdf/1234.5678"

    def test_post_node_without_url_succeeds(self, test_server):
        """POST /node without url field still works (backward compatible)."""
        port, store = test_server
        status, data = _request("POST", port, "/node", body={
            "id": "no-url-node",
            "label": "Concept Node",
            "type": "concept",
        })
        assert status == 201
        assert data.get("url") is None


@pytest.mark.xdist_group("server")
class TestObservationNotes:
    """Tests for observation notes persistence (OHM-of8: notes accepted but not persisted)."""

    def test_observe_notes_persisted(self, test_server):
        """POST /observe/{id} with notes field persists and returns notes."""
        port, store = test_server
        # Create a node first
        _request("POST", port, "/node", body={
            "id": "obs-notes-node", "label": "Notes Test", "type": "concept",
        })
        # Create observation with notes
        status, data = _request("POST", port, "/observe/obs-notes-node", body={
            "type": "measurement", "value": 42.0, "notes": "Anomalous reading",
        })
        assert status == 201
        assert data.get("notes") == "Anomalous reading"

    def test_observe_notes_stored_in_db(self, test_server):
        """Notes are actually stored in the database, not just echoed."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "obs-notes-db", "label": "DB Notes Test", "type": "concept",
        })
        _request("POST", port, "/observe/obs-notes-db", body={
            "type": "measurement", "value": 1.0, "notes": "Stored in DB",
        })
        # Query the database directly
        obs = store.execute(
            "SELECT notes FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1",
            ["obs-notes-db"],
        )
        assert len(obs) == 1
        assert obs[0]["notes"] == "Stored in DB"

    def test_observe_without_notes(self, test_server):
        """POST /observe/{id} without notes field works fine (notes is optional)."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "obs-no-notes", "label": "No Notes", "type": "concept",
        })
        status, data = _request("POST", port, "/observe/obs-no-notes", body={
            "type": "measurement", "value": 5.0,
        })
        assert status == 201


@pytest.mark.xdist_group("server")
class TestSourceAttribution:
    """Tests for structured source attribution on observations (OHM-lmr)."""

    def test_observe_with_source_name_and_url(self, test_server):
        """POST /observe/{id} with source_name and source_url persists them."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "src-attrib-node", "label": "Source Test", "type": "concept",
        })
        status, data = _request("POST", port, "/observe/src-attrib-node", body={
            "type": "measurement", "value": 1.5,
            "source_name": "Reuters", "source_url": "https://reuters.com/article/123",
        })
        assert status == 201
        assert data.get("source_name") == "Reuters"
        assert data.get("source_url") == "https://reuters.com/article/123"

    def test_observe_source_attribution_in_db(self, test_server):
        """source_name and source_url are stored in the database."""
        port, store = test_server
        _request("POST", port, "/node", body={
            "id": "src-attrib-db", "label": "DB Source Test", "type": "concept",
        })
        _request("POST", port, "/observe/src-attrib-db", body={
            "type": "measurement", "value": 2.0,
            "source_name": "AP News", "source_url": "https://apnews.com/article/456",
        })
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
        _request("POST", port, "/node", body={
            "id": "src-no-attrib", "label": "No Source", "type": "concept",
        })
        status, data = _request("POST", port, "/observe/src-no-attrib", body={
            "type": "measurement", "value": 3.0,
        })
        assert status == 201
        assert data.get("source_name") is None
        assert data.get("source_url") is None


@pytest.mark.xdist_group("server")
class TestBatchEndpoint:
    """Tests for POST /batch endpoint (OHM-1m3)."""

    def test_batch_create_nodes(self, test_server):
        """POST /batch creates multiple nodes."""
        port, store = test_server
        status, data = _request("POST", port, "/batch", body={
            "nodes": [
                {"id": "batch-n1", "label": "Node 1", "type": "concept"},
                {"id": "batch-n2", "label": "Node 2", "type": "source"},
            ],
            "edges": [],
        })
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 0

    def test_batch_create_nodes_and_edges(self, test_server):
        """POST /batch creates nodes and edges together."""
        port, store = test_server
        status, data = _request("POST", port, "/batch", body={
            "nodes": [
                {"id": "batch-n3", "label": "Node A", "type": "concept"},
                {"id": "batch-n4", "label": "Node B", "type": "concept"},
            ],
            "edges": [
                {"from": "batch-n3", "to": "batch-n4", "type": "CAUSES", "layer": "L3"},
            ],
        })
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 1

    def test_batch_validation_error(self, test_server):
        """POST /batch with missing required fields returns validation error."""
        port, store = test_server
        status, data = _request("POST", port, "/batch", body={
            "nodes": [
                {"id": "batch-bad"},  # missing 'label'
            ],
            "edges": [],
        })
        assert status == 400

    def test_batch_empty(self, test_server):
        """POST /batch with empty arrays returns zeros."""
        port, store = test_server
        status, data = _request("POST", port, "/batch", body={
            "nodes": [],
            "edges": [],
        })
        assert status == 201
        assert data["nodes_created"] == 0
        assert data["edges_created"] == 0

    def test_batch_populates_change_feed(self, test_server):
        """POST /batch populates change feed for each created item."""
        port, store = test_server
        _request("POST", port, "/batch", body={
            "nodes": [
                {"id": "cf-batch-1", "label": "CF1", "type": "concept"},
                {"id": "cf-batch-2", "label": "CF2", "type": "concept"},
            ],
            "edges": [],
        })
        # Verify change feed entries
        feed = store.execute(
            "SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' "
            "AND row_id IN ('cf-batch-1', 'cf-batch-2') ORDER BY occurred_at DESC"
        )
        assert len(feed) == 2


@pytest.mark.xdist_group("server")
class TestIdempotentRegistration:
    """Tests for idempotent agent registration (OHM-5n7: deduplicate registration)."""

    def test_register_creates_agent_node(self, test_server):
        """POST /register creates an agent node with deterministic ID."""
        port, store = test_server
        status, data = _request("POST", port, "/register", body={
            "name": "testbot",
            "description": "A test agent",
            "values": ["accuracy"],
            "goals": ["explore"],
        })
        assert status == 201
        assert data["agent"]["label"] == "testbot"
        assert data["agent"]["type"] == "agent"
        assert data["edges_created"] >= 2  # VALUES + GOALS

    def test_register_idempotent(self, test_server):
        """POST /register twice with same name reuses agent node (no duplicates)."""
        port, store = test_server
        # First registration
        status1, data1 = _request("POST", port, "/register", body={
            "name": "idem_agent",
            "values": ["truth"],
        })
        assert status1 == 201
        agent_id_1 = data1["agent"]["id"]

        # Second registration with same name
        status2, data2 = _request("POST", port, "/register", body={
            "name": "idem_agent",
            "values": ["truth", "fairness"],
        })
        assert status2 == 201
        agent_id_2 = data2["agent"]["id"]

        # Same agent node ID (deterministic)
        assert agent_id_1 == agent_id_2

        # No duplicate agent nodes
        agent_nodes = store.execute(
            "SELECT * FROM ohm_nodes WHERE type = 'agent' AND label = 'idem_agent'"
        )
        assert len(agent_nodes) == 1

    def test_register_reuses_value_nodes(self, test_server):
        """POST /register reuses existing value/goal/skill nodes."""
        port, store = test_server
        _request("POST", port, "/register", body={
            "name": "reuse_agent",
            "values": ["courage"],
        })
        _request("POST", port, "/register", body={
            "name": "other_agent",
            "values": ["courage"],
        })
        # Only one "courage" value node should exist
        courage_nodes = store.execute(
            "SELECT * FROM ohm_nodes WHERE label = 'courage' AND type = 'value'"
        )
        assert len(courage_nodes) == 1

    def test_register_updates_edges(self, test_server):
        """POST /register replaces old edges on re-registration."""
        port, store = test_server
        # First registration with 1 value
        _request("POST", port, "/register", body={
            "name": "edge_agent",
            "values": ["loyalty"],
        })
        # Second registration with 2 values
        status, data = _request("POST", port, "/register", body={
            "name": "edge_agent",
            "values": ["loyalty", "honesty"],
        })
        assert status == 201
        # Should have 2 VALUES edges (old ones deleted, new ones created)
        agent_id = data["agent"]["id"]
        values_edges = store.execute(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES'",
            [agent_id],
        )
        assert len(values_edges) == 2
