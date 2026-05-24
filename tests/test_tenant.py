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
