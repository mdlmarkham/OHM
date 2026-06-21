"""Integration tests for OHM multi-tenancy (OHM-tss4.9).

Tests the full stack: HTTP auth → current_store routing → TenantManager →
isolated DuckDB instances. Each test starts a live server with multi_tenant=True.

Acceptance criteria:
  - provision → write → read → verify isolation
  - two tenants on same domain, no cross-contamination
  - deprovision removes data, revoked token returns 401, unprovisioned returns 404
  - customer key cannot read another tenant's data
  - agent token routes to core store, not tenant stores
  - concurrent writes to same tenant are consistent
  - LRU eviction + re-access reconnects correctly
  - end-to-end: write via customer token, read back same node

Marks: integration, slow.
"""

import json
import threading
from http.client import HTTPConnection

import pytest

from ohm.server import OhmHandler, _hash_token
from ohm.server.server import _generate_customer_token
from ohm.store import OhmStore
from ohm.tenant import TenantManager

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ── Server fixture ────────────────────────────────────────────────────────────


def _start_mt_server(tmp_path, admin_token="admin-secret", no_auth=False):
    """Start a multi-tenant test server. Returns (port, server, core_store, tm)."""
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

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), OhmHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.daemon_threads = True
    server.request_queue_size = 128  # OHM-yv35: avoid connection resets under burst load
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port("127.0.0.1", port)
    return port, server, store, tm


def _register_customer(port, tm, customer_id, admin_token="admin-secret"):
    """Provision a tenant via HTTP and register the returned customer token."""
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
    conn.request(
        "POST",
        "/tenant/provision",
        body=json.dumps({"customer_id": customer_id}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin_token}"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read())
    assert resp.status == 201, f"provision failed: {data}"
    return data["token"]


def _req(method, port, path, body=None, token=None):
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=30)
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


# ── Isolation tests ───────────────────────────────────────────────────────────


class TestTenantIsolation:
    def test_provision_write_read_isolated(self, tmp_path):
        """Full lifecycle: provision → write via customer token → read back → verify not in other tenant."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")
            token_b = _register_customer(port, tm, "tenant_b")

            # Write a node to tenant_a
            status, data = _req(
                "POST",
                port,
                "/node",
                body={"id": "secret-node-A", "label": "Tenant A secret", "type": "concept"},
                token=token_a,
            )
            assert status in (200, 201), f"write to tenant_a failed: {data}"

            # Read it back via tenant_a — should exist
            status, data = _req("GET", port, "/node/secret-node-A", token=token_a)
            assert status == 200, f"read from tenant_a failed: {data}"
            assert data["id"] == "secret-node-A"

            # Read it via tenant_b — should NOT exist
            status, data = _req("GET", port, "/node/secret-node-A", token=token_b)
            assert status == 404, f"Data from tenant_a leaked into tenant_b: {data}"
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_two_tenants_same_domain_no_cross_contamination(self, tmp_path):
        """Nodes written to tenant_a on 'ohm' domain are invisible to tenant_b on 'ohm' domain."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")
            token_b = _register_customer(port, tm, "tenant_b")

            for i in range(5):
                _req("POST", port, "/node", body={"id": f"a-node-{i}", "label": f"A {i}", "type": "concept"}, token=token_a)
                _req("POST", port, "/node", body={"id": f"b-node-{i}", "label": f"B {i}", "type": "concept"}, token=token_b)

            # tenant_a can see its own nodes
            status, data = _req("GET", port, "/nodes", token=token_a)
            assert status == 200
            a_ids = {n["id"] for n in data.get("nodes", data.get("items", []))}
            for i in range(5):
                assert f"a-node-{i}" in a_ids, f"a-node-{i} missing from tenant_a"
                assert f"b-node-{i}" not in a_ids, f"b-node-{i} leaked into tenant_a"

            # tenant_b can see its own nodes
            status, data = _req("GET", port, "/nodes", token=token_b)
            assert status == 200
            b_ids = {n["id"] for n in data.get("nodes", data.get("items", []))}
            for i in range(5):
                assert f"b-node-{i}" in b_ids, f"b-node-{i} missing from tenant_b"
                assert f"a-node-{i}" not in b_ids, f"a-node-{i} leaked into tenant_b"
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_same_node_id_in_two_tenants(self, tmp_path):
        """The same node ID can exist in two tenants with different data — no conflict."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")
            token_b = _register_customer(port, tm, "tenant_b")

            _req("POST", port, "/node", body={"id": "shared-id", "label": "Version A", "type": "concept"}, token=token_a)
            _req("POST", port, "/node", body={"id": "shared-id", "label": "Version B", "type": "concept"}, token=token_b)

            _, data_a = _req("GET", port, "/node/shared-id", token=token_a)
            _, data_b = _req("GET", port, "/node/shared-id", token=token_b)
            assert data_a["label"] == "Version A"
            assert data_b["label"] == "Version B"
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_customer_token_blocked_from_other_tenants(self, tmp_path):
        """Customer token for tenant_a cannot read tenant_b's nodes by any means."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")
            token_b = _register_customer(port, tm, "tenant_b")

            # Write exclusive node to tenant_b
            _req("POST", port, "/node", body={"id": "b-exclusive", "label": "Only in B", "type": "concept"}, token=token_b)

            # tenant_a token cannot see it — 404 not 403 (it's isolated, not forbidden)
            status, _ = _req("GET", port, "/node/b-exclusive", token=token_a)
            assert status == 404

            # tenant_a list also doesn't show it
            _, data = _req("GET", port, "/nodes", token=token_a)
            ids = {n["id"] for n in data.get("nodes", data.get("items", []))}
            assert "b-exclusive" not in ids
        finally:
            server.shutdown()
            core.close()
            tm.close()


# ── Agent token vs customer token routing ─────────────────────────────────────


class TestTokenRouting:
    def test_agent_token_routes_to_core_store(self, tmp_path):
        """Agent token (no _resolved_customer_id) reads/writes the core OhmStore."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")

            # Write a node via customer token → goes into tenant_a's store
            _req("POST", port, "/node", body={"id": "tenant-only-node", "label": "Tenant data", "type": "concept"}, token=token_a)

            # Agent token (admin) cannot see it — it's in a different DuckDB
            status, _ = _req("GET", port, "/node/tenant-only-node", token="admin-secret")
            assert status == 404, "Agent token should not see tenant nodes in core store"
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_agent_writes_to_core_store_not_visible_in_tenant(self, tmp_path):
        """Agent writes go to the core store and are not visible inside a tenant."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token_a = _register_customer(port, tm, "tenant_a")

            # Agent writes a node to the core store
            _req("POST", port, "/node", body={"id": "core-node", "label": "Core data", "type": "concept"}, token="admin-secret")

            # Customer token for tenant_a cannot see it
            status, _ = _req("GET", port, "/node/core-node", token=token_a)
            assert status == 404, "Tenant customer should not see core store nodes"
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_unprovisioned_customer_token_returns_404(self, tmp_path):
        """A valid customer token for an unprovisioned tenant returns 404 on data access.

        404 (not 403) is intentional: unprovisioned = resource doesn't exist from
        the caller's view. 403 would leak existence info. See OHM-uahx.
        """
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            # Manually add a token hash for a non-existent tenant
            orphan_token, orphan_hash = _generate_customer_token("ghost_tenant")
            OhmHandler.customer_tokens[orphan_hash] = "ghost_tenant"

            status, data = _req("GET", port, "/stats", token=orphan_token)
            assert status == 404, f"Expected 404 for unprovisioned tenant, got {status}: {data}"
        finally:
            OhmHandler.customer_tokens.pop(orphan_hash, None)
            server.shutdown()
            core.close()
            tm.close()


# ── Deprovision ───────────────────────────────────────────────────────────────


class TestDeprovision:
    def test_deprovision_removes_data_and_blocks_access(self, tmp_path):
        """After deprovision, the customer token returns 403 (unprovisioned tenant)."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token = _register_customer(port, tm, "doomed")
            _req("POST", port, "/node", body={"id": "doomed-node", "label": "Will be gone", "type": "concept"}, token=token)

            # Deprovision via admin
            status, _ = _req("DELETE", port, "/tenant/doomed?confirm=true", token="admin-secret")
            assert status == 200

            # Subsequent write with old (revoked) token → 401 (token no longer recognized)
            status, _ = _req("POST", port, "/node", body={"id": "post-deprovision", "label": "x", "type": "concept"}, token=token)
            assert status == 401, f"Revoked token should get 401 on write, got {status}"

            # Tenant directory is gone
            assert not (tmp_path / "tenants" / "doomed" / "meta.json").exists()
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_deprovision_and_reprovision(self, tmp_path):
        """A deprovisioned tenant can be reprovisioned and starts fresh."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            # Provision, write, deprovision
            token1 = _register_customer(port, tm, "recycled")
            _req("POST", port, "/node", body={"id": "old-node", "label": "Old", "type": "concept"}, token=token1)
            _req("DELETE", port, "/tenant/recycled?confirm=true", token="admin-secret")

            # Reprovision — should be a clean slate
            token2 = _register_customer(port, tm, "recycled")
            status, _ = _req("GET", port, "/node/old-node", token=token2)
            assert status == 404, "Reprovisioned tenant should not have old data"
        finally:
            server.shutdown()
            core.close()
            tm.close()


# ── Concurrency ───────────────────────────────────────────────────────────────


class TestConcurrentAccess:
    @pytest.mark.skipif("sys.platform == 'win32'", reason="DuckDB write serialization causes timeouts on Windows")
    def test_concurrent_writes_same_tenant_no_corruption(self, tmp_path):
        """50 concurrent writes to the same tenant complete without errors or data loss."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            token = _register_customer(port, tm, "concurrent_tenant")
            errors = []
            written = []

            def writer(i):
                try:
                    s, d = _req(
                        "POST",
                        port,
                        "/node",
                        body={"id": f"cn-{i}", "label": f"Node {i}", "type": "concept"},
                        token=token,
                    )
                    if s not in (200, 201):
                        errors.append((i, s, d))
                    else:
                        written.append(i)
                except Exception as e:
                    errors.append((i, "exc", str(e)))

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Write errors: {errors[:5]}"
            assert len(written) == 50

            # Verify all 50 test nodes are in the tenant store.
            # Filter by id prefix to exclude pre-seeded agent nodes from provisioning.
            tenant_store = tm.get_store("concurrent_tenant")
            rows = tenant_store.execute("SELECT COUNT(*) AS n FROM ohm_nodes WHERE deleted_at IS NULL AND id LIKE 'cn-%'")
            assert rows[0]["n"] == 50
        finally:
            server.shutdown()
            core.close()
            tm.close()

    def test_concurrent_reads_different_tenants(self, tmp_path):
        """20 concurrent threads each read from their own isolated tenant."""
        port, server, core, tm = _start_mt_server(tmp_path)
        try:
            tokens = {}
            for i in range(4):
                cid = f"reader_{i}"
                tok = _register_customer(port, tm, cid)
                tokens[cid] = tok
                _req("POST", port, "/node", body={"id": f"unique-{cid}", "label": f"Node in {cid}", "type": "concept"}, token=tok)

            errors = []

            def reader(cid, tok):
                for _ in range(5):
                    s, d = _req("GET", port, f"/node/unique-{cid}", token=tok)
                    if s != 200:
                        errors.append((cid, s))

            threads = [threading.Thread(target=reader, args=(cid, tok)) for cid, tok in tokens.items()]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"Read errors: {errors}"
        finally:
            server.shutdown()
            core.close()
            tm.close()


# ── LRU eviction ─────────────────────────────────────────────────────────────


class TestLRUEviction:
    def test_evicted_tenant_reconnects_on_access(self, tmp_path):
        """A tenant evicted from the LRU cache can be re-accessed transparently."""
        port, server, core, tm = _start_mt_server(tmp_path)
        # Use a small-cache TenantManager so we can force evictions
        tm.close()
        tm2 = TenantManager(tmp_path / "tenants", max_cached=2)
        OhmHandler.tenant_manager = tm2
        try:
            tok_a = _register_customer(port, tm2, "evict_a")
            tok_b = _register_customer(port, tm2, "evict_b")
            tok_c = _register_customer(port, tm2, "evict_c")

            # Write a distinctive node to evict_a before it gets evicted
            _req("POST", port, "/node", body={"id": "pre-evict", "label": "Before eviction", "type": "concept"}, token=tok_a)

            # Access b and c to fill the cache (evicts a)
            _req("GET", port, "/stats", token=tok_b)
            _req("GET", port, "/stats", token=tok_c)
            assert "evict_a" not in tm2._cache  # evict_a should be gone from cache

            # Re-access evict_a — store should reopen and data should still be there
            status, data = _req("GET", port, "/node/pre-evict", token=tok_a)
            assert status == 200, f"Node lost after LRU eviction: {data}"
            assert data["id"] == "pre-evict"
        finally:
            server.shutdown()
            core.close()
            tm2.close()

    def test_lru_eviction_no_data_loss(self, tmp_path):
        """Writing to a tenant before it's evicted and reading after reconnect preserves data."""
        port, server, core, tm = _start_mt_server(tmp_path)
        tm.close()
        tm2 = TenantManager(tmp_path / "tenants", max_cached=3)
        OhmHandler.tenant_manager = tm2
        try:
            tokens = {}
            for cid in ("lru_a", "lru_b", "lru_c", "lru_d"):
                tokens[cid] = _register_customer(port, tm2, cid)

            # Write 3 nodes to lru_a
            for i in range(3):
                _req("POST", port, "/node", body={"id": f"lru-a-{i}", "label": f"A{i}", "type": "concept"}, token=tokens["lru_a"])

            # Fill cache with b, c, d (pushes lru_a out)
            for cid in ("lru_b", "lru_c", "lru_d"):
                _req("GET", port, "/stats", token=tokens[cid])

            # lru_a should be evicted
            assert "lru_a" not in tm2._cache

            # All 3 nodes still accessible after reconnect
            for i in range(3):
                status, data = _req("GET", port, f"/node/lru-a-{i}", token=tokens["lru_a"])
                assert status == 200, f"lru-a-{i} lost after eviction: {data}"
        finally:
            server.shutdown()
            core.close()
            tm2.close()


# ── Lazy migration (stub — OHM-tss4.5 not yet implemented) ───────────────────


@pytest.mark.skip(reason="OHM-tss4.5 (lazy schema migration) not yet implemented")
class TestLazyMigration:
    def test_old_schema_version_auto_migrates(self, tmp_path):
        """A tenant provisioned on an older schema version is migrated on get_store()."""
        pass
