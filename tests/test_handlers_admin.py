"""Tests for AdminHandlerMixin endpoints (OHM-brry extraction).

Covers the endpoints defined in src/ohm/server/handlers/admin.py.
"""

from __future__ import annotations

import pytest

from tests.conftest import http_request, start_test_server


@pytest.fixture(autouse=True)
def _restore_handler_state():
    """Save/restore OhmHandler class state around every test.

    Inline-server tests (e.g. test_get_checkpoint_requires_write_auth)
    overwrite OhmHandler class attributes; without this, subsequent
    module-fixture tests get a 500.
    """
    from ohm.server.server import OhmHandler

    _attrs = ["store", "tokens", "roles", "no_auth", "require_read_auth", "config", "schema_config", "multi_tenant", "customer_tokens"]
    saved = {a: getattr(OhmHandler, a, None) for a in _attrs}
    yield
    for a, v in saved.items():
        setattr(OhmHandler, a, v)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """No-auth server — shared across all tests in this module."""
    from ohm.store import OhmStore

    tmp_path = tmp_path_factory.mktemp("admin")
    store = OhmStore(db_path=str(tmp_path / "admin.duckdb"), agent_name="test_agent")
    port, srv, thread = start_test_server(store, no_auth=True)
    yield port, store
    srv.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.fixture(scope="module")
def auth_server(tmp_path_factory):
    """Authenticated server — shared across all tests in this module."""
    from ohm.store import OhmStore

    tmp_path = tmp_path_factory.mktemp("admin_auth")
    store = OhmStore(db_path=str(tmp_path / "admin_auth.duckdb"), agent_name="test_agent")
    tokens = {"admin-token": "admin_agent"}
    roles = {"admin_agent": "admin"}
    port, srv, thread = start_test_server(store, tokens=tokens, roles=roles, require_read_auth=True)
    yield port, store
    srv.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.mark.xdist_group("server_admin")
class TestAdminCheckpointGet:
    def test_get_checkpoint_returns_200(self, auth_server):
        """GET /admin/checkpoint with a write token flushes the WAL."""
        port, _ = auth_server
        status, data = http_request("GET", port, "/admin/checkpoint", token="admin-token")
        assert status == 200
        assert data.get("status") == "ok"
        assert "WAL" in data.get("message", "")

    def test_get_checkpoint_requires_write_auth(self, tmp_path):
        from ohm.store import OhmStore

        store = OhmStore(db_path=str(tmp_path / "chk.duckdb"), agent_name="test_agent")
        tokens = {"rw-token": "writer"}
        roles = {"writer": "read-write"}
        port, srv, thread = start_test_server(store, tokens=tokens, roles=roles)
        try:
            # No token → 401
            status, _ = http_request("GET", port, "/admin/checkpoint")
            assert status == 401
            # Write token → 200
            status, data = http_request("GET", port, "/admin/checkpoint", token="rw-token")
            assert status == 200
            assert data.get("status") == "ok"
        finally:
            srv.shutdown()
            thread.join(timeout=2)
            store.close()


@pytest.mark.xdist_group("server_admin")
class TestAdminCheckpointPost:
    def test_post_checkpoint_returns_200(self, server):
        port, _ = server
        status, data = http_request("POST", port, "/admin/checkpoint", body={})
        assert status == 200
        assert data.get("status") == "ok"

    def test_post_checkpoint_idempotent(self, server):
        port, _ = server
        for _ in range(3):
            status, data = http_request("POST", port, "/admin/checkpoint", body={})
            assert status == 200


@pytest.mark.xdist_group("server_admin")
class TestAdminEmbeddings:
    def test_get_embeddings_returns_200(self, server):
        """GET /admin/embeddings returns a valid response even with no Ollama available."""
        port, _ = server
        status, data = http_request("GET", port, "/admin/embeddings?batch_size=1")
        assert status == 200
        # Either all-already-embedded or partial/ok
        assert data.get("status") in ("ok", "partial")
        assert "updated" in data
        assert "failed" in data
        assert "total" in data

    def test_get_embeddings_clamps_batch_size(self, server):
        """batch_size is clamped to [1, 50]."""
        port, _ = server
        status, data = http_request("GET", port, "/admin/embeddings?batch_size=999")
        assert status == 200

    def test_get_embeddings_handles_zero_nodes(self, server):
        """No missing embeddings → returns status=ok with zero counts."""
        port, _ = server
        status, data = http_request("GET", port, "/admin/embeddings")
        assert status == 200
        # No nodes in the DB → total=0
        if data.get("total") == 0:
            assert data.get("updated") == 0
            assert data.get("status") == "ok"


@pytest.mark.xdist_group("server_admin")
class TestAdminSnapshots:
    def test_get_snapshots_returns_200(self, server):
        port, _ = server
        status, data = http_request("GET", port, "/admin/snapshots")
        assert status == 200
        assert "snapshots" in data
        assert "count" in data
        assert isinstance(data["snapshots"], list)
        assert data["count"] == len(data["snapshots"])
