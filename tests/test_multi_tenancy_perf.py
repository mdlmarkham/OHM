"""Multi-tenancy performance benchmarks (OHM-s2ao).

Measures TenantManager performance under concurrent load:
- Store resolution latency (warm vs cold LRU)
- LRU eviction overhead at capacity boundary
- Concurrent tenant access patterns
- Memory usage at various max_cached levels

Usage:
    pytest tests/test_multi_tenancy_perf.py -v --benchmark-only

Marks: slow (HNSW index creation for 100 tenants is expensive).
"""

from __future__ import annotations

import threading

import pytest

from ohm.tenant import TenantManager

pytestmark = pytest.mark.slow


@pytest.fixture
def tm_10(tmp_path):
    manager = TenantManager(tmp_path / "tenants", max_cached=10)
    yield manager
    manager.close()


@pytest.fixture
def tm_50(tmp_path):
    manager = TenantManager(tmp_path / "tenants", max_cached=50)
    yield manager
    manager.close()


@pytest.fixture
def tm_100(tmp_path):
    manager = TenantManager(tmp_path / "tenants", max_cached=100)
    yield manager
    manager.close()


def _provision_n(tm, n, prefix="t"):
    for i in range(n):
        tm.provision(f"{prefix}_{i}")


class TestStoreResolutionLatency:
    """Benchmark store resolution (get_store) latency."""

    @pytest.mark.benchmark(group="store_resolution")
    def test_cold_resolution(self, benchmark, tm_10):
        _provision_n(tm_10, 5)
        tenant_ids = [f"t_{i}" for i in range(5)]
        idx = [0]

        def run():
            tid = tenant_ids[idx[0] % 5]
            idx[0] += 1
            tm_10.release_store(tid)
            return tm_10.get_store(tid)

        benchmark(run)

    @pytest.mark.benchmark(group="store_resolution")
    def test_warm_resolution(self, benchmark, tm_10):
        _provision_n(tm_10, 5)
        for i in range(5):
            tm_10.get_store(f"t_{i}")
        tenant_ids = [f"t_{i}" for i in range(5)]
        idx = [0]

        def run():
            tid = tenant_ids[idx[0] % 5]
            idx[0] += 1
            return tm_10.get_store(tid)

        benchmark(run)

    @pytest.mark.benchmark(group="store_resolution")
    def test_resolution_100_tenants(self, benchmark, tm_100):
        _provision_n(tm_100, 100)
        tenant_ids = [f"t_{i}" for i in range(100)]
        for tid in tenant_ids:
            tm_100.get_store(tid)
        idx = [0]

        def run():
            tid = tenant_ids[idx[0] % 100]
            idx[0] += 1
            return tm_100.get_store(tid)

        benchmark(run)


class TestLRUEviction:
    """Benchmark LRU eviction at capacity boundary."""

    @pytest.mark.benchmark(group="lru_eviction")
    def test_eviction_at_capacity_10(self, benchmark, tm_10):
        _provision_n(tm_10, 10)
        for i in range(10):
            tm_10.get_store(f"t_{i}")
        extra_idx = [0]

        def run():
            extra_idx[0] += 1
            tm_10.provision(f"extra_{extra_idx[0]}")
            store = tm_10.get_store(f"extra_{extra_idx[0]}")
            return store

        benchmark(run)

    @pytest.mark.benchmark(group="lru_eviction")
    def test_provision_100_tenants(self, benchmark, tm_100):
        _provision_n(tm_100, 99)
        idx = [99]

        def run():
            idx[0] += 1
            return tm_100.provision(f"prov_{idx[0]}")

        benchmark(run)


class TestConcurrentTenantAccess:
    """Benchmark concurrent access across tenants."""

    @pytest.mark.benchmark(group="concurrent")
    def test_concurrent_get_store_10_tenants(self, benchmark, tm_10):
        _provision_n(tm_10, 10)
        tenant_ids = [f"t_{i}" for i in range(10)]
        idx = [0]
        lock = threading.Lock()

        def run():
            with lock:
                i = idx[0] % 10
                idx[0] += 1
            tid = tenant_ids[i]
            return tm_10.get_store(tid)

        benchmark(run)

    @pytest.mark.benchmark(group="concurrent")
    def test_concurrent_get_store_100_tenants(self, benchmark, tm_100):
        _provision_n(tm_100, 100)
        tenant_ids = [f"t_{i}" for i in range(100)]
        idx = [0]
        lock = threading.Lock()

        def run():
            with lock:
                i = idx[0] % 100
                idx[0] += 1
            tid = tenant_ids[i]
            return tm_100.get_store(tid)

        benchmark(run)


class TestMemoryUsage:
    """Memory usage at various max_cached levels.

    These are not strict benchmarks but provide baseline numbers
    for memory regression detection.
    """

    def test_memory_10_tenants(self, tm_10):
        import tracemalloc

        tracemalloc.start()
        _provision_n(tm_10, 10)
        for i in range(10):
            tm_10.get_store(f"t_{i}")
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 500 * 1024 * 1024  # < 500 MB

    def test_memory_50_tenants(self, tm_50):
        import tracemalloc

        tracemalloc.start()
        _provision_n(tm_50, 50)
        for i in range(50):
            tm_50.get_store(f"t_{i}")
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert peak < 2 * 1024 * 1024 * 1024  # < 2 GB

    def test_cache_hit_rate_repeated_access(self, tm_10):
        _provision_n(tm_10, 5)
        hits = 0
        misses = 0
        for _ in range(100):
            for i in range(5):
                store = tm_10.get_store(f"t_{i}")
                entry = tm_10._cache.get(f"t_{i}")
                if entry is not None and entry.store is store:
                    hits += 1
                else:
                    misses += 1
        total = hits + misses
        hit_rate = hits / total if total > 0 else 0
        assert hit_rate > 0.9
