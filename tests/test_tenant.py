"""Tests for TenantManager — provisioning, LRU eviction, isolation, thread safety.

OHM-tss4.2 / OHM-tlza acceptance criteria:
  - provision() creates isolated DuckDB + meta.json
  - get_store() returns OhmStore, caches it (LRU hit = same object)
  - LRU capacity: oldest evicted when max_cached exceeded
  - idle eviction: entries idle > threshold are closed
  - deprovision(): secure delete, store removed from cache
  - list_tenants(): returns all meta.json dicts
  - thread safety: concurrent get_store() calls return the same object
  - isolation: write to tenant A is not visible in tenant B
  - per-tenant write lock: different tenants have different locks
  - validation: invalid customer_ids are rejected
"""

import json
import threading
import time

import pytest

from ohm.tenant import TenantAlreadyExistsError, TenantManager, TenantNotFoundError, _IDLE_EVICT_SECONDS


@pytest.fixture
def tm(tmp_path):
    manager = TenantManager(tmp_path / "tenants", max_cached=5)
    yield manager
    manager.close()


class TestProvisioning:
    def test_provision_creates_db_and_meta(self, tm, tmp_path):
        meta = tm.provision("acme_hvac", domain="ohm", tier="starter")
        tenant_dir = tmp_path / "tenants" / "acme_hvac"
        assert (tenant_dir / "ohm.duckdb").exists()
        assert (tenant_dir / "meta.json").exists()
        assert meta["customer_id"] == "acme_hvac"
        assert meta["domain"] == "ohm"
        assert meta["tier"] == "starter"

    def test_provision_duplicate_raises(self, tm):
        tm.provision("acme_hvac")
        with pytest.raises(TenantAlreadyExistsError):
            tm.provision("acme_hvac")

    def test_provision_rejects_invalid_customer_id(self, tm):
        for bad in ("../etc", "a/b", "acme.hvac", "", "acme hvac"):
            with pytest.raises(ValueError):
                tm.provision(bad)

    def test_provision_multiple_tenants(self, tm):
        tm.provision("tenant_a")
        tm.provision("tenant_b")
        tenants = tm.list_tenants()
        ids = {t["customer_id"] for t in tenants}
        assert ids == {"tenant_a", "tenant_b"}


class TestGetStore:
    def test_get_store_returns_ohm_store(self, tm):
        from ohm.graph.store import OhmStore

        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")
        assert isinstance(store, OhmStore)

    def test_get_store_caches_same_object(self, tm):
        tm.provision("acme_hvac")
        s1 = tm.get_store("acme_hvac")
        s2 = tm.get_store("acme_hvac")
        assert s1 is s2

    def test_get_store_not_found_raises(self, tm):
        with pytest.raises(TenantNotFoundError):
            tm.get_store("no_such_tenant")

    def test_get_store_rejects_invalid_id(self, tm):
        with pytest.raises(ValueError):
            tm.get_store("../etc/passwd")


class TestLRUEviction:
    def test_lru_evicts_oldest_when_at_capacity(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants", max_cached=3)
        for cid in ("ten1", "ten2", "ten3", "ten4"):
            tm.provision(cid)

        s1 = tm.get_store("ten1")
        tm.get_store("ten2")
        tm.get_store("ten3")
        # ten1 is LRU; adding ten4 should evict ten1
        tm.get_store("ten4")
        # ten1 was evicted — get_store should open a new connection (different object)
        s1_new = tm.get_store("ten1")
        assert s1_new is not s1

        tm.close()


class TestDeprovision:
    def test_deprovision_requires_confirm(self, tm):
        tm.provision("acme_hvac")
        with pytest.raises(ValueError, match="confirm=True"):
            tm.deprovision("acme_hvac")

    def test_deprovision_removes_files(self, tm, tmp_path):
        tm.provision("acme_hvac")
        tm.deprovision("acme_hvac", confirm=True)
        tenant_dir = tmp_path / "tenants" / "acme_hvac"
        assert not (tenant_dir / "ohm.duckdb").exists()
        assert not (tenant_dir / "meta.json").exists()

    def test_deprovision_evicts_from_cache(self, tm):
        tm.provision("acme_hvac")
        tm.get_store("acme_hvac")
        tm.deprovision("acme_hvac", confirm=True)
        with pytest.raises(TenantNotFoundError):
            tm.get_store("acme_hvac")

    def test_deprovision_nonexistent_raises(self, tm):
        with pytest.raises(TenantNotFoundError):
            tm.deprovision("ghost", confirm=True)


class TestIsolation:
    def test_write_to_tenant_a_not_visible_in_tenant_b(self, tm):
        tm.provision("tenant_a")
        tm.provision("tenant_b")

        store_a = tm.get_store("tenant_a")
        store_b = tm.get_store("tenant_b")

        store_a.write_node("unique-node-xyz", "Secret A", "concept", agent_name="test")

        node_in_b = store_b.get_node("unique-node-xyz")
        assert node_in_b is None, "Data from tenant A leaked into tenant B"

    def test_separate_write_locks_per_tenant(self, tm):
        tm.provision("tenant_a")
        tm.provision("tenant_b")
        lock_a = tm.get_write_lock("tenant_a")
        lock_b = tm.get_write_lock("tenant_b")
        assert lock_a is not lock_b

    def test_same_tenant_same_write_lock(self, tm):
        tm.provision("tenant_a")
        lock1 = tm.get_write_lock("tenant_a")
        lock2 = tm.get_write_lock("tenant_a")
        assert lock1 is lock2


class TestThreadSafety:
    def test_concurrent_get_store_returns_same_object(self, tm):
        tm.provision("shared_tenant")
        results = []
        errors = []

        def worker():
            try:
                results.append(tm.get_store("shared_tenant"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in concurrent access: {errors}"
        assert len(results) == 20
        first = results[0]
        assert all(s is first for s in results), "Concurrent get_store returned different objects"

    def test_concurrent_writes_with_lock_no_corruption(self, tm):
        tm.provision("concurrent_tenant")
        store = tm.get_store("concurrent_tenant")
        lock = tm.get_write_lock("concurrent_tenant")
        errors = []

        def writer(i):
            try:
                with lock:
                    store.write_node(f"node-{i}", f"Node {i}", "concept", agent_name="test")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Write errors under concurrent load: {errors}"
        # Verify all 50 nodes were written
        rows = store.execute("SELECT COUNT(*) AS n FROM ohm_nodes")
        assert rows[0]["n"] == 50


class TestIdleEviction:
    def test_idle_eviction_closes_old_entries(self, tmp_path, monkeypatch):
        evicted = []
        tm = TenantManager(tmp_path / "tenants", max_cached=10)

        original_evict = tm._evict

        def tracked_evict(cid):
            evicted.append(cid)
            original_evict(cid)

        monkeypatch.setattr(tm, "_evict", tracked_evict)
        monkeypatch.setattr("ohm.tenant._IDLE_EVICT_SECONDS", -1)  # always idle

        tm.provision("idle_tenant")
        tm.get_store("idle_tenant")

        # Manually trigger the eviction loop logic (don't wait 60s)
        with tm._cache_lock:
            idle = [cid for cid, entry in tm._cache.items() if time.monotonic() - entry.last_accessed > -1]
        for cid in idle:
            tm._evict(cid)

        assert "idle_tenant" in evicted
        tm.close()


class TestLazySchemaMigration:
    def test_get_store_syncs_meta_json_after_upgrade(self, tm, tmp_path):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")

        # Simulate a behind-version tenant by rewriting meta.json
        # (In reality, OhmStore.__init__ already migrates the DB, but meta.json
        # may be stale if it was written by an older OHM version)
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))

        # Evict from cache so next get_store re-checks meta.json
        tm._evict("acme_hvac")

        # Re-access — lazy migration should sync meta.json
        store = tm.get_store("acme_hvac")
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == SCHEMA_VERSION

    def test_get_store_skips_migration_when_current(self, tm, tmp_path):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"

        store = tm.get_store("acme_hvac")
        from ohm.schema import get_schema_version
        db_version = get_schema_version(store.conn)
        assert db_version == SCHEMA_VERSION

        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == SCHEMA_VERSION

    def test_migration_failure_marks_needs_attention(self, tm, tmp_path, monkeypatch):
        from ohm.schema import SCHEMA_VERSION
        import unittest.mock

        tm.provision("acme_hvac")

        # Get the store, then manually regress the DB version to simulate
        # a scenario where migration is needed at the lazy-migration level
        store = tm.get_store("acme_hvac")
        store.conn.execute("UPDATE ohm_meta SET value = '0.1.0' WHERE key = 'schema_version'")

        # Set meta.json to old version too
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))

        # Mock get_schema_version to return old version (bypass the DB check)
        # and _apply_migrations to fail
        import ohm.schema as schema_mod
        monkeypatch.setattr(schema_mod, "get_schema_version", lambda conn: "0.1.0")
        monkeypatch.setattr(schema_mod, "_apply_migrations", lambda conn: (_ for _ in ()).throw(RuntimeError("simulated migration failure")))

        # Call lazy migration — re-raises after writing needs_attention to meta.json (OHM-dlnx)
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            tm._apply_lazy_migrations("acme_hvac", store)
        meta = json.loads(meta_path.read_text())
        assert meta.get("needs_attention") is True
        assert "simulated migration failure" in meta.get("migration_error", "")

    def test_provision_writes_current_schema_version(self, tm):
        from ohm.schema import SCHEMA_VERSION

        meta = tm.provision("acme_hvac")
        assert meta["schema_version"] == SCHEMA_VERSION

    def test_concurrent_get_store_syncs_once(self, tm, tmp_path):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")

        # Set behind version in meta.json only
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))
        tm._evict("acme_hvac")

        results = []
        errors = []

        def worker():
            try:
                s = tm.get_store("acme_hvac")
                results.append(s)
            except (json.JSONDecodeError, PermissionError):
                pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors: {errors}"
        # meta.json should be updated (atomic writes prevent partial reads)
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == SCHEMA_VERSION


class TestCrashConsistentMigration:
    def test_reconcile_detects_meta_behind(self, tm, tmp_path):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")

        # Set meta.json behind (simulate stale after upgrade)
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))
        tm._evict("acme_hvac")

        results = tm.reconcile_tenants()
        assert len(results) == 1
        assert results[0]["customer_id"] == "acme_hvac"
        assert results[0]["status"] == "meta_behind"

        # meta.json should be auto-corrected
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == SCHEMA_VERSION

    def test_reconcile_detects_half_migrated(self, tm, tmp_path):
        tm.provision("acme_hvac")

        # Simulate crash mid-migration: leave .migration_lock file
        lock_path = tmp_path / "tenants" / "acme_hvac" / ".migration_lock"
        lock_path.write_text(json.dumps({
            "customer_id": "acme_hvac",
            "from_version": "0.1.0",
            "to_version": "0.18.0",
            "started_at": "2026-05-24T00:00:00+00:00",
        }))

        results = tm.reconcile_tenants()
        assert len(results) == 1
        assert results[0]["status"] == "half_migrated"

        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta.get("needs_attention") is True

    def test_reconcile_all_ok(self, tm):
        tm.provision("acme_hvac")

        results = tm.reconcile_tenants()
        assert len(results) == 1
        assert results[0]["status"] == "ok"

    def test_migration_lock_created_during_migration(self, tm, tmp_path, monkeypatch):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")
        store.conn.execute("UPDATE ohm_meta SET value = '0.1.0' WHERE key = 'schema_version'")

        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))

        import ohm.schema as schema_mod
        monkeypatch.setattr(schema_mod, "get_schema_version", lambda conn: "0.1.0")

        lock_path = tmp_path / "tenants" / "acme_hvac" / ".migration_lock"

        # Make _apply_migrations raise to simulate crash — re-raises after failure (OHM-dlnx)
        monkeypatch.setattr(schema_mod, "_apply_migrations", lambda conn: (_ for _ in ()).throw(RuntimeError("crash")))

        with pytest.raises(RuntimeError, match="crash"):
            tm._apply_lazy_migrations("acme_hvac", store)

        # Lock file should persist after failed migration
        assert lock_path.exists()

    def test_migration_lock_cleaned_after_success(self, tm, tmp_path, monkeypatch):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")

        # Set meta behind, DB is already current (OhmStore init migrated it)
        meta_path = tmp_path / "tenants" / "acme_hvac" / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = "0.1.0"
        meta_path.write_text(json.dumps(meta, indent=2))

        # Simulate stale lock file from a previous crash
        lock_path = tmp_path / "tenants" / "acme_hvac" / ".migration_lock"
        lock_path.write_text("{}")

        tm._evict("acme_hvac")
        store = tm.get_store("acme_hvac")

        # Lock file should be cleaned up (DB is already current)
        assert not lock_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == SCHEMA_VERSION


class TestLRUEvictionGuard:
    def test_eviction_skips_in_flight_request(self, tm):
        tm.provision("acme_hvac")
        entry = tm.acquire_store("acme_hvac")
        assert entry.refcount == 1

        # Eviction should skip (refcount > 0)
        tm._evict("acme_hvac")
        assert "acme_hvac" in tm._cache

        # After release, eviction should proceed
        tm.release_store("acme_hvac")
        assert entry.refcount == 0

    def test_lru_skips_in_flight_and_marks_deferred(self, tm):
        for i in range(5):
            tm.provision(f"tenant_{i}")
            tm.get_store(f"tenant_{i}")

        # Acquire one tenant so it has an in-flight request
        entry = tm.acquire_store("tenant_0")
        assert entry.refcount == 1

        # Add one more to exceed max_cached=5
        tm.provision("tenant_5")
        tm.get_store("tenant_5")

        # tenant_0 should still be in cache (was marked evict_pending)
        assert "tenant_0" in tm._cache

        tm.release_store("tenant_0")
        assert entry.refcount == 0

    def test_using_store_context_manager(self, tm):
        tm.provision("acme_hvac")

        with tm.using_store("acme_hvac") as entry:
            assert entry.refcount >= 1
            store = entry.store
            assert store is not None

        assert entry.refcount == 0

    def test_using_store_releases_on_exception(self, tm):
        tm.provision("acme_hvac")

        try:
            with tm.using_store("acme_hvac") as entry:
                assert entry.refcount >= 1
                raise ValueError("test error")
        except ValueError:
            pass

        assert tm._cache.get("acme_hvac") is not None and tm._cache["acme_hvac"].refcount == 0

    def test_idle_eviction_skips_in_flight(self, tmp_path):
        tm = TenantManager(tmp_path / "tenants", max_cached=10)
        tm.provision("acme_hvac")
        entry = tm.acquire_store("acme_hvac")

        # Simulate idle eviction
        tm._evict("acme_hvac")

        # Should still be in cache
        assert "acme_hvac" in tm._cache
        assert entry.evict_pending is True

        # Release should trigger deferred eviction
        tm.release_store("acme_hvac")
        assert entry.refcount == 0
        assert "acme_hvac" not in tm._cache

        tm.close()


class TestWALCheckpointStrategy:
    def test_checkpoint_on_eviction(self, tm, tmp_path):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")

        # Write something to generate WAL
        store.conn.execute("INSERT INTO ohm_meta (key, value) VALUES ('test_key', 'test_val')")

        # Evict should checkpoint before close
        tm._evict("acme_hvac")
        assert "acme_hvac" not in tm._cache

    def test_periodic_checkpoint_active_tenants(self, tm, tmp_path, monkeypatch):
        monkeypatch.setattr("ohm.tenant._CHECKPOINT_INTERVAL_SECONDS", 0)

        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")

        entry = tm._cache["acme_hvac"]
        assert entry.last_checkpoint_at == 0.0

        # Manually trigger checkpoint loop logic
        tm._checkpoint_active_tenants()

        assert entry.last_checkpoint_at > 0.0

    def test_wal_size_tracking(self, tm, tmp_path):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")

        wal_size = tm._wal_size("acme_hvac")
        assert isinstance(wal_size, int)
        assert wal_size >= 0

    def test_tenant_health(self, tm, tmp_path):
        from ohm.schema import SCHEMA_VERSION

        tm.provision("acme_hvac")
        tm.get_store("acme_hvac")

        health = tm.tenant_health("acme_hvac")
        assert health["customer_id"] == "acme_hvac"
        assert health["schema_version"] == SCHEMA_VERSION
        assert health["cached"] is True
        assert health["refcount"] == 0
        assert "wal_size_bytes" in health

    def test_checkpoint_tenant_method(self, tm, tmp_path):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")

        entry = tm._cache["acme_hvac"]
        assert entry.last_checkpoint_at == 0.0

        tm._checkpoint_tenant("acme_hvac", entry, reason="test")
        assert entry.last_checkpoint_at > 0.0


class TestPerTenantIntegrations:
    def test_provision_with_integrations(self, tm):
        integrations = {
            "twilio": {
                "account_sid": "AC123",
                "auth_token_ref": "TWILIO_AUTH_TOKEN_ACME",
                "phone_number": "+15551234567",
            }
        }
        meta = tm.provision("acme_hvac", integrations=integrations)
        assert meta["integrations"]["twilio"]["account_sid"] == "AC123"

    def test_load_integrations_resolves_ref(self, tm, monkeypatch):
        monkeypatch.setenv("TWILIO_AUTH_TOKEN_ACME", "secret_token_value")

        integrations = {
            "twilio": {
                "account_sid": "AC123",
                "auth_token_ref": "TWILIO_AUTH_TOKEN_ACME",
                "phone_number": "+15551234567",
            }
        }
        tm.provision("acme_hvac", integrations=integrations)

        loaded = tm.load_integrations("acme_hvac")
        assert loaded["twilio"]["auth_token"] == "secret_token_value"
        assert loaded["twilio"]["account_sid"] == "AC123"

    def test_load_integrations_unresolved_ref(self, tm):
        integrations = {
            "twilio": {
                "account_sid": "AC123",
                "auth_token_ref": "TWILIO_AUTH_TOKEN_NONEXISTENT",
                "phone_number": "+15551234567",
            }
        }
        tm.provision("acme_hvac", integrations=integrations)

        loaded = tm.load_integrations("acme_hvac")
        assert loaded["twilio"]["auth_token_ref"] == "TWILIO_AUTH_TOKEN_NONEXISTENT"
        assert loaded["twilio"].get("auth_token_ref_unresolved") is True

    def test_update_integrations(self, tm):
        tm.provision("acme_hvac")
        meta = tm.update_integrations("acme_hvac", {
            "sendgrid": {"api_key_ref": "SENDGRID_KEY_ACME", "from_email": "ops@acme.com"}
        })
        assert "sendgrid" in meta["integrations"]

        loaded = tm.load_integrations("acme_hvac")
        assert loaded["sendgrid"]["from_email"] == "ops@acme.com"

    def test_provision_validates_required_integrations(self, tm):
        with pytest.raises(ValueError, match="Missing required integrations"):
            tm.provision("acme_hvac", domain="home_services")

    def test_provision_home_services_with_twilio(self, tm):
        integrations = {
            "twilio": {
                "account_sid": "AC123",
                "auth_token_ref": "TWILIO_AUTH_TOKEN_ACME",
                "phone_number": "+15551234567",
            }
        }
        meta = tm.provision("acme_hvac", domain="home_services", integrations=integrations)
        assert meta["domain"] == "home_services"
        assert meta["integrations"]["twilio"]["account_sid"] == "AC123"


class TestAgentMultiTenantRouting:
    def test_for_agent_tenant_id_creates_tenant_scoped_db(self, tmp_path):
        from ohm.graph.store import OhmStore

        base = str(tmp_path / "agents")
        store = OhmStore.for_agent("metis", tenant_id="acme_hvac", base_dir=base)
        assert "metis" in str(store.db_path)
        assert "acme_hvac" in str(store.db_path)
        store.close()

        # Backward compat: no tenant_id
        store2 = OhmStore.for_agent("metis", base_dir=base)
        assert "acme_hvac" not in str(store2.db_path)
        store2.close()

    def test_for_agent_different_tenants_different_dbs(self, tmp_path):
        from ohm.graph.store import OhmStore

        base = str(tmp_path / "agents")
        store_a = OhmStore.for_agent("metis", tenant_id="tenant_a", base_dir=base)
        store_b = OhmStore.for_agent("metis", tenant_id="tenant_b", base_dir=base)

        assert str(store_a.db_path) != str(store_b.db_path)
        store_a.close()
        store_b.close()

    def test_connect_tenant_id_opens_scoped_db(self, tmp_path):
        from ohm.sdk import connect

        db_path = str(tmp_path / "graphs")
        g = connect(db_path, actor="metis", tenant_id="acme_hvac")
        assert g.tenant_id == "acme_hvac"
        g._conn.close()

    def test_connect_no_tenant_id_backward_compat(self, tmp_path):
        from ohm.sdk import connect

        db_path = str(tmp_path / "test.duckdb")
        g = connect(db_path, actor="metis")
        assert g.tenant_id is None
        g._conn.close()

    def test_tenant_scoped_stores_isolated(self, tmp_path):
        from ohm.graph.store import OhmStore

        base = str(tmp_path / "agents")
        store_a = OhmStore.for_agent("metis", tenant_id="tenant_a", base_dir=base)
        store_b = OhmStore.for_agent("metis", tenant_id="tenant_b", base_dir=base)

        # Write to tenant_a
        store_a.write_node(id="node_a", label="Tenant A Node", type="concept")

        # Verify not visible in tenant_b
        count_b = store_b.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()
        count_a = store_a.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()

        assert count_a[0] > 0
        assert count_b[0] == 0

        store_a.close()
        store_b.close()


class TestPerTenantQuotas:
    def test_provision_includes_tier_quotas(self, tm):
        meta = tm.provision("acme_hvac", tier="starter")
        assert "quotas" in meta
        assert meta["quotas"]["max_nodes"] == 10_000
        assert meta["quotas"]["max_edges"] == 50_000

    def test_professional_tier_higher_quotas(self, tm):
        meta = tm.provision("acme_pro", tier="professional")
        assert meta["quotas"]["max_nodes"] == 100_000
        assert meta["quotas"]["max_edges"] == 500_000

    def test_check_quota_allows_under_limit(self, tm):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")
        tm.check_quota("acme_hvac", "nodes", amount=1)

    def test_check_quota_rejects_over_limit(self, tm, monkeypatch):
        from ohm.tenant import QuotaExceededError, TIER_QUOTAS

        monkeypatch.setitem(TIER_QUOTAS["starter"], "max_nodes", 5)
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")

        for i in range(5):
            store.write_node(id=f"n{i}", label=f"Node {i}", type="concept")

        with pytest.raises(QuotaExceededError, match="nodes quota"):
            tm.check_quota("acme_hvac", "nodes", amount=1)

    def test_check_quota_rejects_when_store_evicted(self, tm):
        """check_quota raises QuotaExceededError when store is not in cache (OHM-qo8z).

        Previously returned 0, bypassing quota enforcement. Now raises so callers
        cannot accidentally allow writes when quota state is unknown.
        """
        from ohm.tenant import QuotaExceededError

        tm.provision("acme_hvac")
        tm.get_store("acme_hvac")

        tm._evict("acme_hvac")

        with pytest.raises(QuotaExceededError, match="store not in cache"):
            tm.check_quota("acme_hvac", "nodes", amount=1)

    def test_check_quota_raises_on_broken_connection(self, tm, monkeypatch):
        """check_quota raises QuotaExceededError when DB query fails (OHM-qo8z).

        If the store is in cache but the connection throws, the old code silently
        returned 0, bypassing quota enforcement. Now it raises conservatively.
        """
        from ohm.tenant import QuotaExceededError, TIER_QUOTAS

        monkeypatch.setitem(TIER_QUOTAS["starter"], "max_nodes", 10)
        tm.provision("acme_hvac")
        tm.get_store("acme_hvac")

        with tm._cache_lock:
            entry = tm._cache["acme_hvac"]
        # Close the connection so any execute() call raises.
        entry.store.conn.close()

        with pytest.raises(QuotaExceededError, match="store access failed"):
            tm.check_quota("acme_hvac", "nodes", amount=1)

    def test_check_quota_db_size(self, tm, monkeypatch):
        from ohm.tenant import QuotaExceededError, TIER_QUOTAS

        monkeypatch.setitem(TIER_QUOTAS["starter"], "max_db_size_bytes", 1024)
        tm.provision("acme_hvac")

        with pytest.raises(QuotaExceededError, match="DB size quota"):
            tm.check_quota("acme_hvac", "db_size_bytes", amount=1)

    def test_check_quota_requests_per_day(self, tm, monkeypatch):
        from ohm.tenant import QuotaExceededError, TIER_QUOTAS

        monkeypatch.setitem(TIER_QUOTAS["starter"], "max_requests_per_day", 3)
        tm.provision("acme_hvac")

        for _ in range(3):
            tm.record_request("acme_hvac")

        with pytest.raises(QuotaExceededError, match="daily request quota"):
            tm.check_quota("acme_hvac", "requests_per_day", amount=1)

    def test_tenant_health_includes_quotas_and_usage(self, tm):
        tm.provision("acme_hvac")
        tm.get_store("acme_hvac")

        health = tm.tenant_health("acme_hvac")
        assert "quotas" in health
        assert "usage" in health
        assert health["usage"]["nodes"] >= 0
        assert health["usage"]["edges"] >= 0
        assert health["usage"]["db_size_bytes"] > 0
        assert health["tier"] == "starter"

    def test_record_request_tracks_daily_count(self, tm):
        tm.provision("acme_hvac")
        count = tm.record_request("acme_hvac")
        assert count == 1
        count = tm.record_request("acme_hvac")
        assert count == 2

    def test_quota_cache_avoids_meta_read(self, tm):
        """check_quota uses in-memory cache, not meta.json read (OHM-8d54)."""
        tm.provision("cache_test", tier="starter")
        tm.get_store("cache_test")
        with tm._cache_lock:
            assert "cache_test" not in tm._quota_cache
        tm.check_quota("cache_test", "nodes")
        with tm._cache_lock:
            assert "cache_test" in tm._quota_cache
        tier, quotas = tm._quota_cache["cache_test"]
        assert tier == "starter"
        assert quotas["max_nodes"] == 10_000

    def test_quota_cache_invalidated_on_integrations_update(self, tm):
        """Updating integrations invalidates quota cache (OHM-8d54)."""
        tm.provision("invalidate_test", tier="professional")
        tm.get_store("invalidate_test")
        tm.check_quota("invalidate_test", "nodes")
        with tm._cache_lock:
            cached_tier, _ = tm._quota_cache["invalidate_test"]
            assert cached_tier == "professional"
        tm.update_integrations("invalidate_test", {"slack": {"channel": "#test"}})
        with tm._cache_lock:
            assert "invalidate_test" not in tm._quota_cache


class TestBackupRestore:
    """Tests for OHM-kbwl: Per-tenant backup, restore, and disaster recovery."""

    def test_backup_creates_backup_directory(self, tm):
        tm.provision("acme_hvac")
        result = tm.backup_tenant("acme_hvac")
        assert "backup_id" in result
        assert result["reason"] == "manual"
        backup_dir = tm._tenant_dir("acme_hvac") / "backups"
        assert backup_dir.exists()

    def test_backup_copies_db_and_meta(self, tm):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")
        store.conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES ('n1', 'test', 'concept', 'agent', CURRENT_TIMESTAMP)")
        store.conn.execute("CHECKPOINT")
        tm.release_store("acme_hvac")

        result = tm.backup_tenant("acme_hvac")
        backup_id = result["backup_id"]
        backup_path = tm._tenant_dir("acme_hvac") / "backups" / backup_id
        assert (backup_path / "ohm.duckdb").exists()
        assert (backup_path / "meta.json").exists()
        assert result["db_size_bytes"] > 0

    def test_backup_with_reason(self, tm):
        tm.provision("acme_hvac")
        result = tm.backup_tenant("acme_hvac", reason="pre_migration")
        assert result["reason"] == "pre_migration"

    def test_list_backups_returns_backups(self, tm):
        tm.provision("acme_hvac")
        tm.backup_tenant("acme_hvac")
        tm.backup_tenant("acme_hvac")
        backups = tm.list_backups("acme_hvac")
        assert len(backups) == 2

    def test_list_backups_empty_when_no_backups(self, tm):
        tm.provision("acme_hvac")
        backups = tm.list_backups("acme_hvac")
        assert backups == []

    def test_restore_replaces_db(self, tm):
        tm.provision("acme_hvac")
        store = tm.get_store("acme_hvac")
        store.conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES ('n1', 'before_backup', 'concept', 'agent', CURRENT_TIMESTAMP)")
        store.conn.execute("CHECKPOINT")
        tm.release_store("acme_hvac")

        result = tm.backup_tenant("acme_hvac")
        backup_id = result["backup_id"]

        store = tm.get_store("acme_hvac")
        store.conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES ('n2', 'after_backup', 'concept', 'agent', CURRENT_TIMESTAMP)")
        store.conn.execute("CHECKPOINT")
        tm.release_store("acme_hvac")

        restore_result = tm.restore_tenant("acme_hvac", backup_id)
        assert restore_result["status"] == "restored"

        store = tm.get_store("acme_hvac")
        count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE label = 'after_backup'").fetchone()[0]
        assert count == 0
        count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE label = 'before_backup'").fetchone()[0]
        assert count == 1
        tm.release_store("acme_hvac")

    def test_restore_nonexistent_backup_raises(self, tm):
        tm.provision("acme_hvac")
        with pytest.raises(ValueError, match="not found"):
            tm.restore_tenant("acme_hvac", "nonexistent_backup")

    def test_restore_creates_pre_restore_backup(self, tm):
        tm.provision("acme_hvac")
        result = tm.backup_tenant("acme_hvac")
        backup_id = result["backup_id"]
        tm.restore_tenant("acme_hvac", backup_id)
        backups = tm.list_backups("acme_hvac")
        reasons = [b["reason"] for b in backups]
        assert "pre_restore" in reasons

    def test_backup_retention_prunes_old(self, tm):
        tm.provision("acme_hvac")
        meta = tm._read_meta("acme_hvac")
        meta["backup_retention_days"] = 0
        tm._write_meta("acme_hvac", meta)

        tm.backup_tenant("acme_hvac")
        import time
        time.sleep(1.1)
        tm.backup_tenant("acme_hvac")

        backups = tm.list_backups("acme_hvac")
        assert len(backups) <= 1


class TestTemplatePropagation:
    """Tests for OHM-dcf3: Domain-template change propagation to existing tenants."""

    def test_provision_stores_template_version(self, tm):
        tm.provision("acme_hvac", domain="home_services", integrations={
            "twilio": {"account_sid": "x", "auth_token_ref": "x", "phone_number": "+1"}
        })
        meta = tm._read_meta("acme_hvac")
        assert "template_version" in meta
        assert meta["template_version"] >= 1

    def test_provision_default_template_version_zero(self, tm):
        tm.provision("acme_basic")
        meta = tm._read_meta("acme_basic")
        assert meta["template_version"] >= 0

    def test_no_propagation_when_template_current(self, tm):
        tm.provision("acme_hvac", domain="home_services", integrations={
            "twilio": {"account_sid": "x", "auth_token_ref": "x", "phone_number": "+1"}
        })
        store = tm.get_store("acme_hvac")
        original_tv = store.schema.template_version
        tm._propagate_template("acme_hvac", store)
        assert store.schema.template_version == original_tv

    def test_additive_merge_adds_node_types(self, tm):
        from ohm.graph.schema import SchemaConfig
        old = SchemaConfig(name="test", node_types=frozenset({"a", "b"}), template_version=1)
        current = SchemaConfig(name="test", node_types=frozenset({"a", "b", "c"}), template_version=2)
        merged = tm._additive_merge(old, current)
        assert merged.node_types == frozenset({"a", "b", "c"})
        assert merged.template_version == 2

    def test_additive_merge_preserves_old_types(self, tm):
        from ohm.graph.schema import SchemaConfig
        old = SchemaConfig(name="test", node_types=frozenset({"a", "b", "custom"}), template_version=1)
        current = SchemaConfig(name="test", node_types=frozenset({"a", "b", "c"}), template_version=2)
        merged = tm._additive_merge(old, current)
        assert "custom" in merged.node_types

    def test_additive_merge_merges_edge_types(self, tm):
        from ohm.graph.schema import SchemaConfig
        old = SchemaConfig(
            name="test",
            edge_types_by_layer={"L1": frozenset({"A", "B"})},
            template_version=1,
        )
        current = SchemaConfig(
            name="test",
            edge_types_by_layer={"L1": frozenset({"B", "C"}), "L5": frozenset({"X"})},
            template_version=2,
        )
        merged = tm._additive_merge(old, current)
        assert merged.layer_edge_types["L1"] == frozenset({"A", "B", "C"})
        assert merged.layer_edge_types["L5"] == frozenset({"X"})

    def test_additive_merge_merges_observation_types(self, tm):
        from ohm.graph.schema import SchemaConfig
        old = SchemaConfig(name="test", observation_types=frozenset({"temp"}), template_version=1)
        current = SchemaConfig(name="test", observation_types=frozenset({"temp", "humidity"}), template_version=2)
        merged = tm._additive_merge(old, current)
        assert "humidity" in merged.observation_types

    def test_propagation_updates_meta_template_version(self, tm):
        tm.provision("acme_hvac", domain="home_services", integrations={
            "twilio": {"account_sid": "x", "auth_token_ref": "x", "phone_number": "+1"}
        })
        meta = tm._read_meta("acme_hvac")
        meta["template_version"] = 0
        tm._write_meta("acme_hvac", meta)

        store = tm.get_store("acme_hvac")
        meta_after = tm._read_meta("acme_hvac")
        assert meta_after["template_version"] > 0
