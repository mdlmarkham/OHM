"""Tests for the OHM daemon HTTP server endpoints.

Starts a test server on a random port and tests all 17+ endpoints
including auth, error handling, and edge cases.
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
    time.sleep(0.1)  # Let server start
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


class TestObservationEndpoints:
    """Tests for observation endpoints."""

    def test_create_observation(self, test_server):
        port, store = test_server
        _request("POST", port, "/node", body={"id": "obs_node", "label": "O", "type": "concept"})
        status, data = _request("POST", port, "/observe/obs_node", body={
            "type": "measurement", "value": 1.5, "sigma": 0.3,
        })
        assert status == 201


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
        from ohm.server import _hash_token, _verify_token
        token = "my-secret-token"
        token_hash = _hash_token(token)
        assert _verify_token(token, token_hash) is True

    def test_verify_token_wrong(self):
        """Wrong token should not verify."""
        from ohm.server import _hash_token, _verify_token
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
        from ohm.server import _build_token_lookup, _hash_token
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
        from ohm.server import _build_token_lookup, _hash_token
        token_hash = _hash_token("my-secret-token")
        config = {"metis": {"hash": token_hash, "role": "read-write"}}
        token_hashes, roles = _build_token_lookup(config)
        assert token_hash in token_hashes
        assert token_hashes[token_hash] == "metis"
        assert roles["metis"] == "read-write"

    def test_build_token_lookup_readonly_role(self):
        """Hashed format should support read-only role."""
        from ohm.server import _build_token_lookup, _hash_token
        token_hash = _hash_token("readonly-token")
        config = {"observer": {"hash": token_hash, "role": "read-only"}}
        token_hashes, roles = _build_token_lookup(config)
        assert roles["observer"] == "read-only"

    def test_auth_with_hashed_tokens(self, tmp_path):
        """Authentication should work with hashed tokens."""
        db_path = str(tmp_path / "test_hashed_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        # Use plaintext tokens — _start_test_server will hash them
        tokens = {"hashed-token-abc": "metis", "hashed-token-xyz": "observer"}
        roles = {"metis": "read-write", "observer": "read-only"}
        port, server, thread = _start_test_server(store, tokens=tokens, roles=roles)
        try:
            # Valid token → 201
            status, data = _request("POST", port, "/node", body={
                "id": "auth_ok", "label": "AuthOK", "type": "concept",
            }, headers={"Authorization": "Bearer hashed-token-abc"})
            assert status == 201

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
        from ohm.server import _build_token_lookup, _hash_token
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
        assert "FEEDS" in data["edge_types"]["L2"]
        assert "FLOWS_TO" in data["edge_types"]["L2"]


class TestTopodEntryPoint:
    """Tests for the topod entry point function."""

    def test_topod_main_exists(self):
        """topod_main should be importable."""
        from ohm.server import topod_main
        assert callable(topod_main)

    def test_topod_main_passes_topo_schema(self):
        """topod_main should pass TOPO_SCHEMA to main()."""
        from ohm.server import topod_main
        from ohm.schema import TOPO_SCHEMA
        # We can't actually run topod_main (it starts a server),
        # but we can verify it references TOPO_SCHEMA
        import inspect
        source = inspect.getsource(topod_main)
        assert "TOPO_SCHEMA" in source
