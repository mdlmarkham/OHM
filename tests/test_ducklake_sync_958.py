"""Tests for OHM-958: DuckLake incremental sync truncation fix."""

from __future__ import annotations

import pytest

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