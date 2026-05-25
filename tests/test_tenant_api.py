"""Tests for OHM-tss4.6 — Tenant Provisioning API endpoints.

Acceptance criteria:
  - POST /tenant/provision creates instance + returns API key (admin-only)
  - GET /tenants lists all tenants (admin-only)
  - GET /tenant/{id} returns tenant meta (admin-only)
  - GET /tenant/{id}/schema returns domain schema (admin-only)
  - DELETE /tenant/{id}?confirm=true deprovisions (admin-only)
  - POST /tenant/{id}/export returns graph data (admin-only)
  - Non-admin agents get 403
  - Customer keys get 403
  - Multi-tenancy disabled returns appropriate error
"""

import json
import threading
from http.client import HTTPConnection

import pytest

from ohm.server import OhmHandler, _hash_token, _build_customer_token_lookup
from ohm.server.server import _generate_customer_token
from ohm.store import OhmStore
from ohm.tenant import TenantManager


def _start_mt_server(tmp_path, admin_token="admin-secret", no_auth=False):
    """Start a multi-tenant test server with TenantManager."""
    import socketserver
    from tests.conftest import wait_for_port

    db_path = str(tmp_path / "core.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test")
    tenants_dir = tmp_path / "tenants"

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.multi_tenant = True
    OhmHandler.no_auth = no_auth
    OhmHandler.require_read_auth = False

    if no_auth:
        OhmHandler.tokens = {}
        OhmHandler.roles = {}
    else:
        OhmHandler.tokens = {_hash_token(admin_token): "admin"}
        OhmHandler.roles = {"admin": "admin"}

    OhmHandler.customer_tokens = {}
    tm = TenantManager(tenants_dir, max_cached=10)
    OhmHandler.tenant_manager = tm

    server = socketserver.TCPServer(("127.0.0.1", 0), OhmHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port("127.0.0.1", port)
    return port, server, store, tm


def _req(method, port, path, body=None, token=None):
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
    hdrs = {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body).encode(), headers=hdrs)
    else:
        conn.request(method, path, headers=hdrs)
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


# ── POST /tenant/provision ────────────────────────────────────────────────────


class TestProvisionEndpoint:
    def test_provision_creates_tenant_and_returns_token(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, data = _req(
                "POST", port, "/tenant/provision",
                body={"customer_id": "acme_hvac", "domain": "ohm", "tier": "starter"},
                token="admin-secret",
            )
            assert status == 201, data
            assert data["customer_id"] == "acme_hvac"
            assert data["token"].startswith("twai_live_")
            assert "warning" in data
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_token_is_functional(self, tmp_path):
        """Generated customer token can authenticate (stored in OhmHandler.customer_tokens)."""
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            _, data = _req(
                "POST", port, "/tenant/provision",
                body={"customer_id": "acme_hvac"},
                token="admin-secret",
            )
            customer_token = data["token"]
            # Token should now work for auth (stored in OhmHandler.customer_tokens)
            token_hash = _hash_token(customer_token)
            assert token_hash in OhmHandler.customer_tokens
            assert OhmHandler.customer_tokens[token_hash] == "acme_hvac"
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_duplicate_returns_409(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            _req("POST", port, "/tenant/provision",
                 body={"customer_id": "acme_hvac"}, token="admin-secret")
            status, data = _req("POST", port, "/tenant/provision",
                                body={"customer_id": "acme_hvac"}, token="admin-secret")
            assert status == 409
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_missing_customer_id_returns_400(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, _ = _req("POST", port, "/tenant/provision",
                              body={}, token="admin-secret")
            assert status == 400
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_non_admin_returns_403(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            # Add a non-admin agent token
            OhmHandler.tokens[_hash_token("regular-agent")] = "regular"
            OhmHandler.roles["regular"] = "read-write"
            status, _ = _req("POST", port, "/tenant/provision",
                              body={"customer_id": "acme_hvac"}, token="regular-agent")
            assert status == 403
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_customer_key_returns_403(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            # Pre-provision a tenant and add their token
            tm.provision("existing_tenant")
            ctoken, chash = _generate_customer_token("existing_tenant")
            OhmHandler.customer_tokens[chash] = "existing_tenant"

            status, _ = _req("POST", port, "/tenant/provision",
                              body={"customer_id": "new_tenant"}, token=ctoken)
            assert status == 403
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_provision_no_auth_mode(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path, no_auth=True)
        try:
            status, data = _req("POST", port, "/tenant/provision",
                                 body={"customer_id": "free_tenant"})
            assert status == 201, data
        finally:
            server.shutdown()
            store.close()
            tm.close()


# ── GET /tenants ──────────────────────────────────────────────────────────────


class TestListTenantsEndpoint:
    def test_list_tenants_returns_all(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("tenant_a")
            tm.provision("tenant_b")
            status, data = _req("GET", port, "/tenants", token="admin-secret")
            assert status == 200
            ids = {t["customer_id"] for t in data["tenants"]}
            assert ids == {"tenant_a", "tenant_b"}
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_list_tenants_empty(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, data = _req("GET", port, "/tenants", token="admin-secret")
            assert status == 200
            assert data["tenants"] == []
            assert data["count"] == 0
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_list_tenants_non_admin_returns_403(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            OhmHandler.tokens[_hash_token("rw-agent")] = "rw"
            OhmHandler.roles["rw"] = "read-write"
            status, _ = _req("GET", port, "/tenants", token="rw-agent")
            assert status == 403
        finally:
            server.shutdown()
            store.close()
            tm.close()


# ── GET /tenant/{id} ──────────────────────────────────────────────────────────


class TestGetTenantEndpoint:
    def test_get_tenant_returns_meta(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("acme_hvac", domain="ohm", tier="starter")
            status, data = _req("GET", port, "/tenant/acme_hvac", token="admin-secret")
            assert status == 200
            assert data["tenant"]["customer_id"] == "acme_hvac"
            assert data["tenant"]["domain"] == "ohm"
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_get_tenant_not_found_returns_404(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, _ = _req("GET", port, "/tenant/ghost", token="admin-secret")
            assert status == 404
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_get_tenant_schema(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("acme_hvac")
            status, data = _req("GET", port, "/tenant/acme_hvac/schema", token="admin-secret")
            assert status == 200
            assert data["customer_id"] == "acme_hvac"
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_get_tenant_health(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("acme_hvac", tier="starter")
            tm.get_store("acme_hvac")  # warm cache so health has full data
            status, data = _req("GET", port, "/tenant/acme_hvac/health", token="admin-secret")
            assert status == 200
            assert data["customer_id"] == "acme_hvac"
            assert data["tier"] == "starter"
            assert "wal_size_bytes" in data
            assert "quotas" in data
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_get_tenant_unknown_sub_resource_returns_404(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("acme_hvac")
            status, _ = _req("GET", port, "/tenant/acme_hvac/bogus", token="admin-secret")
            assert status == 404
        finally:
            server.shutdown()
            store.close()
            tm.close()


# ── DELETE /tenant/{id} ───────────────────────────────────────────────────────


class TestDeleteTenantEndpoint:
    def test_delete_deprovisions_tenant(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("doomed_tenant")
            status, data = _req("DELETE", port, "/tenant/doomed_tenant?confirm=true",
                                token="admin-secret")
            assert status == 200
            assert data["status"] == "deprovisioned"
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_delete_without_confirm_returns_400(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("live_tenant")
            status, _ = _req("DELETE", port, "/tenant/live_tenant", token="admin-secret")
            assert status == 400
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_delete_revokes_customer_tokens(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("revoked_tenant")
            ctoken, chash = _generate_customer_token("revoked_tenant")
            OhmHandler.customer_tokens[chash] = "revoked_tenant"

            _req("DELETE", port, "/tenant/revoked_tenant?confirm=true",
                 token="admin-secret")
            assert chash not in OhmHandler.customer_tokens
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_delete_not_found_returns_404(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, _ = _req("DELETE", port, "/tenant/ghost?confirm=true",
                             token="admin-secret")
            assert status == 404
        finally:
            server.shutdown()
            store.close()
            tm.close()


# ── POST /tenant/{id}/export ──────────────────────────────────────────────────


class TestExportTenantEndpoint:
    def test_export_returns_graph_data(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            tm.provision("export_tenant")
            ts = tm.get_store("export_tenant")
            ts.write_node("n1", "Node 1", "concept", agent_name="test")

            status, data = _req("POST", port, "/tenant/export_tenant/export",
                                body={}, token="admin-secret")
            assert status == 200
            assert data["customer_id"] == "export_tenant"
            assert data["node_count"] >= 1
        finally:
            server.shutdown()
            store.close()
            tm.close()

    def test_export_not_found_returns_404(self, tmp_path):
        port, server, store, tm = _start_mt_server(tmp_path)
        try:
            status, _ = _req("POST", port, "/tenant/ghost/export",
                             body={}, token="admin-secret")
            assert status == 404
        finally:
            server.shutdown()
            store.close()
            tm.close()
