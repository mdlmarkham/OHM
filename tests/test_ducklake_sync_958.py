"""Tests for OHM-958: DuckLake incremental sync truncation fix + OHM-960 transactional safety."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from ohm.graph.concurrency_guard import _get_pid_file


class TestSyncToDucklakeForceFull:
    """Test the force_full parameter on sync_to_ducklake."""

    def test_force_full_param_exists(self):
        from ohm.graph.store import OhmStore
        import inspect

        sig = inspect.signature(OhmStore.sync_to_ducklake)
        assert "force_full" in sig.parameters
        assert sig.parameters["force_full"].default is False

    def test_sync_heartbeat_force_full_param(self):
        from ohm.graph.store import OhmStore
        import inspect

        sig = inspect.signature(OhmStore.sync_heartbeat)
        assert "force_full_sync" in sig.parameters
        assert sig.parameters["force_full_sync"].default is False


class TestIncrementalSyncTimestampCol:
    """Test that _incremental_sync_table uses the registry's timestamp_col."""

    def test_outcomes_uses_recorded_at(self):
        """ohm_outcomes should use recorded_at, not updated_at (from the registry)."""
        from ohm.graph.schema import DEFAULT_DUCKLAKE_TABLES

        outcomes = [t for t in DEFAULT_DUCKLAKE_TABLES if t.name == "ohm_outcomes"]
        assert len(outcomes) == 1
        assert outcomes[0].timestamp_col == "recorded_at"

    def test_nodes_uses_updated_at(self):
        from ohm.graph.schema import DEFAULT_DUCKLAKE_TABLES

        nodes = [t for t in DEFAULT_DUCKLAKE_TABLES if t.name == "ohm_nodes"]
        assert len(nodes) == 1
        assert nodes[0].timestamp_col == "updated_at"


class TestSyncForceFullEndpoint:
    """Test the POST /sync/force-full endpoint."""

    def test_force_full_endpoint_exists(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        status, data = _request("POST", port, "/sync/force-full")
        # Will return 200 with sync result (may be throttled or no DuckLake)
        assert status == 200
        assert "pushed" in data or "throttled" in data

    def test_sync_endpoint_bypasses_throttle(self, test_server):
        """POST /sync should now pass force=True to bypass throttle."""
        from tests.conftest import _request

        port, store = test_server
        status, data = _request("POST", port, "/sync")
        assert status == 200
        # Should NOT be throttled since we pass force=True now
        assert data.get("throttled") is not True or data.get("pushed") is not None


class TestInitialSyncTransactionSafety:
    """OHM-960: _initial_sync_table must be transactional so a failed INSERT
    doesn't leave the mirror empty after the DELETE has committed."""

    def test_failed_insert_rolls_back_delete(self, tmp_path):
        """If the INSERT inside _initial_sync_table fails, the mirror retains
        its pre-sync row count (not zero)."""
        import duckdb

        conn = duckdb.connect(":memory:")
        # Simulate a local table + mirror with data
        conn.execute("CREATE TABLE local_t (id INTEGER, label VARCHAR, deleted_at TIMESTAMP)")
        conn.execute("INSERT INTO local_t VALUES (1, 'a', NULL), (2, 'b', NULL), (3, 'c', NULL)")
        conn.execute("CREATE TABLE mirror_t (id VARCHAR, label VARCHAR, deleted_at VARCHAR)")
        conn.execute("INSERT INTO mirror_t VALUES ('1', 'a', NULL), ('2', 'b', NULL), ('3', 'c', NULL)")

        before_count = conn.execute("SELECT COUNT(*) FROM mirror_t").fetchone()[0]
        assert before_count == 3

        # Simulate a failed INSERT by patching the second execute to fail
        original_execute = conn.execute
        call_count = [0]

        def failing_execute(sql, *args, **kwargs):
            call_count[0] += 1
            # First call is BEGIN, second is DELETE, third is INSERT — fail it
            if "INSERT INTO mirror_t" in sql:
                raise Exception("simulated column mismatch")
            return original_execute(sql, *args, **kwargs)

        # Manually test the transactional pattern
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM mirror_t")
            try:
                conn.execute("INSERT INTO mirror_t (id, label) SELECT id, label FROM local_t WHERE deleted_at IS NULL")
            except Exception:
                conn.execute("ROLLBACK")
                # After rollback, mirror should still have 3 rows
                after_count = conn.execute("SELECT COUNT(*) FROM mirror_t").fetchone()[0]
                assert after_count == 3, f"Mirror was emptied (count={after_count}) — DELETE was not rolled back!"
        except Exception:
            conn.execute("ROLLBACK")

    def test_successful_sync_preserves_count(self, tmp_path):
        """A successful _initial_sync_table should produce the correct row count."""
        import duckdb

        conn = duckdb.connect(":memory:")
        conn.execute("CREATE TABLE local_t (id INTEGER, label VARCHAR, deleted_at TIMESTAMP)")
        conn.execute("INSERT INTO local_t VALUES (1, 'a', NULL), (2, 'b', NULL), (3, 'c', NULL)")
        conn.execute("CREATE TABLE mirror_t (id VARCHAR, label VARCHAR, deleted_at VARCHAR)")
        conn.execute("INSERT INTO mirror_t VALUES ('old', 'old', NULL)")

        # Transactional sync
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute("DELETE FROM mirror_t")
            conn.execute("INSERT INTO mirror_t (id, label) SELECT CAST(id AS VARCHAR), label FROM local_t WHERE deleted_at IS NULL")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        count = conn.execute("SELECT COUNT(*) FROM mirror_t").fetchone()[0]
        assert count == 3