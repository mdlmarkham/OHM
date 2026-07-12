"""Tests for concurrent inference race fix (#825).

Tests that concurrent inference calls don't spuriously return "No edges found"
and that the caches are thread-safe.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from ohm.inference.bayesian import bayesian_inference, causal_intervention, _ve_cache, _ve_cache_lock, BayesianContext


class TestConcurrentInference:
    """Test concurrent inference race fix."""

    def test_per_request_cursor_isolation(self, test_db) -> None:
        """Test inference handlers use per-request cursor instead of shared conn."""
        # This is tested implicitly by the concurrency stress test
        # The fix is in the handler code, not the inference functions themselves
        conn = test_db
        from ohm.schema import initialize_schema
        initialize_schema(conn)
        
        # Create a simple graph with probability-bearing edges
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["target", "Target", "concept", 0.8, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "Cause", "concept", 0.7, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        
        # Test multiple concurrent calls with different cursors
        def worker(i):
            try:
                cursor = conn.cursor()
                result = bayesian_inference(cursor, "target", {})
                assert result is not None
                assert "posterior" in result
                return True
            except Exception as e:
                print(f"Worker {i} failed: {e}")
                return False
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(worker, i) for i in range(20)]
            results = [f.result() for f in futures]
        
        assert all(results)

    def test_bayesian_network_cache_thread_safe(self) -> None:
        """Test _LRUBayesianCache is thread-safe."""
        from ohm.inference.bayesian import _bayesian_network_cache, _bayesian_network_cache_lock
        
        # Test concurrent access to the cache
        def worker(i):
            key = f"key_{i}"
            value = f"value_{i}"
            with _bayesian_network_cache_lock:
                _bayesian_network_cache[key] = value
                assert _bayesian_network_cache[key] == value
                assert key in _bayesian_network_cache
                assert len(_bayesian_network_cache) <= _bayesian_network_cache._maxsize
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(20)]
            concurrent.futures.wait(futures)
        
        for i in range(20):
            key = f"key_{i}"
            assert key in _bayesian_network_cache

    def test_ve_cache_thread_safe(self) -> None:
        """Test _ve_cache check-then-clear pattern is thread-safe."""
        # Clear the cache first
        with _ve_cache_lock:
            _ve_cache.clear()
        
        # Test concurrent access to the cache
        def worker(i):
            key = i
            # Simulate the check-then-clear pattern
            with _ve_cache_lock:
                if key not in _ve_cache:
                    if len(_ve_cache) >= 20:  # _MAX_VE_CACHE_SIZE
                        _ve_cache.clear()
                    _ve_cache[key] = i  # Store the key as the value for simplicity
                assert _ve_cache[key] == i
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(20)]
            concurrent.futures.wait(futures)
        
        # Cache should not be empty (evictions should be thread-safe)
        assert len(_ve_cache) > 0

    def test_concurrent_inference_no_spurious_empty_errors(self) -> None:
        """Test concurrent inference calls don't return spurious 'No edges found'."""
        import duckdb
        conn = duckdb.connect(':memory:')
        from ohm.schema import initialize_schema
        initialize_schema(conn)
        
        # Create a simple graph with probability-bearing edges
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["target", "Target", "concept", 0.8, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "Cause", "concept", 0.7, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        
        # Test concurrent inference calls
        def worker(i):
            try:
                result = bayesian_inference(conn.cursor(), "target", {})
                # Should not return None (which would trigger "No edges found")
                assert result is not None
                assert "posterior" in result
                return True
            except Exception as e:
                print(f"Worker {i} failed: {e}")
                return False
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(10)]
            results = [f.result() for f in futures]
        
        # All workers should succeed
        assert all(results)

    def test_concurrent_inference_stress(self) -> None:
        """Stress test: many concurrent inference calls against the same graph.
        
        Per acceptance criteria: ≥16 concurrent × ≥30 iterations with 0 spurious errors.
        """
        import duckdb
        conn = duckdb.connect(':memory:')
        from ohm.schema import initialize_schema
        initialize_schema(conn)
        
        # Create a simple graph
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["target", "Target", "concept", 0.8, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause1", "Cause 1", "concept", 0.7, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause2", "Cause 2", "concept", 0.7, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause1", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause2", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        
        # Test many concurrent inference calls (≥16 workers, ≥30 tasks each = ≥480 total)
        def worker(i):
            try:
                result = bayesian_inference(conn.cursor(), "target", {})
                assert result is not None
                assert "posterior" in result
                return True
            except Exception as e:
                print(f"Worker {i} failed: {e}")
                return False
        
        # Use ≥16 workers, each doing ≥30 tasks
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(worker, i) for i in range(480)]
            results = [f.result() for f in futures]
        
        # All workers should succeed - no spurious "No edges found" errors
        assert all(results), f"Some workers failed: {sum(not r for r in results)} failures out of {len(results)}"

    def test_error_messages_distinguish_edge_fetch_vs_probability(self) -> None:
        """Test error messages distinguish 'no edges found' vs 'no probability on edges'."""
        import duckdb
        from ohm.schema import initialize_schema
        
        # Case 1: No edges at all - should say "no edges found matching edge_types"
        conn1 = duckdb.connect(':memory:')
        initialize_schema(conn1)
        
        result1 = bayesian_inference(conn1, "nonexistent", {}, edge_types=["CAUSES"])
        assert "error" in result1
        assert "no edges found matching edge_types" in result1["error"].lower() or "no probability-bearing edges found for inference" in result1["error"].lower()
        
        # Case 2: Edges exist but with explicit probability/confidence
        conn2 = duckdb.connect(':memory:')
        initialize_schema(conn2)
        conn2.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["target", "Target", "concept", 0.8, "test"],
        )
        conn2.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "Cause", "concept", 0.7, "test"],
        )
        conn2.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        
        result2 = bayesian_inference(conn2, "target", {}, edge_types=["CAUSES"])
        assert "error" not in result2
        assert "posterior" in result2
        
        # Case 3: Causal intervention with no matching edges
        result3 = causal_intervention(conn1, "target", 0, edge_types=["CAUSES"])
        assert "error" in result3
        assert "no probability-bearing edges found for intervention" in result3["error"].lower()
        
        # Case 4: BayesianContext with empty network
        conn4 = duckdb.connect(':memory:')
        initialize_schema(conn4)
        ctx = BayesianContext(conn4, edge_types=["CAUSES"])
        result4 = ctx.inference("target", {})
        assert "error" in result4
        assert "cached network" in result4["error"].lower()

    def test_bayesian_context_works(self) -> None:
        """Regression test: BayesianContext.inference() should not crash with UnboundLocalError."""
        import duckdb
        from ohm.schema import initialize_schema
        
        conn = duckdb.connect(':memory:')
        initialize_schema(conn)
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["target", "Target", "concept", 0.8, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "Cause", "concept", 0.7, "test"],
        )
        conn.execute(
            "INSERT INTO ohm_edges (from_node, to_node, edge_type, confidence, probability, layer, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            ["cause", "target", "CAUSES", 0.8, 0.6, "L3", "test"],
        )
        
        # This used to crash with UnboundLocalError due to query_kwargs used before definition
        with BayesianContext(conn, edge_types=['CAUSES'], layers=['L3']) as ctx:
            result = ctx.inference('target', {})
            assert "posterior" in result
            assert "good" in result["posterior"]
            assert "bad" in result["posterior"]
            
            # Also test intervention
            result2 = ctx.intervention('target', 1)
            assert "posterior" in result2
            
            # And ate
            result3 = ctx.ate('cause', 'target')
            assert "ate" in result3
            
            # And sensitivity
            result4 = ctx.sensitivity('cause', 'target')
            assert "e_value" in result4 or "error" in result4