"""Tests for OHM-955: DuckDB concurrency guard — prevents double-open."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from ohm.graph.concurrency_guard import (
    _get_pid_file,
    _is_process_running,
    acquire_lock,
    is_guard_enabled,
    release_lock,
)


class TestIsProcessRunning:
    def test_current_process_is_running(self):
        assert _is_process_running(os.getpid()) is True

    def test_nonexistent_pid(self):
        # PID 0 is never a real process
        assert _is_process_running(0) is False

    def test_negative_pid(self):
        assert _is_process_running(-1) is False


class TestIsGuardEnabled:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("OHM_DISABLE_CONCURRENCY_GUARD", "1")
        assert is_guard_enabled(db_path="/tmp/test.duckdb") is False

    def test_readonly_skips(self):
        assert is_guard_enabled(readonly=True, db_path="/tmp/test.duckdb") is False

    def test_in_memory_skips(self):
        assert is_guard_enabled(db_path=":memory:") is False

    def test_none_skips(self):
        assert is_guard_enabled(db_path=None) is False

    def test_enabled_for_real_path(self, monkeypatch):
        monkeypatch.delenv("OHM_DISABLE_CONCURRENCY_GUARD", raising=False)
        assert is_guard_enabled(db_path="/tmp/test.duckdb") is True


class TestAcquireRelease:
    def test_acquire_creates_pid_file(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        pid_file = acquire_lock(str(db_path))
        assert pid_file.exists()
        assert int(pid_file.read_text().strip()) == os.getpid()

    def test_release_removes_pid_file(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        pid_file = acquire_lock(str(db_path))
        assert pid_file.exists()
        release_lock(pid_file)
        assert not pid_file.exists()

    def test_release_none_is_noop(self):
        release_lock(None)

    def test_release_wrong_pid_does_not_remove(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        pid_file = acquire_lock(str(db_path))
        # Overwrite with a different PID
        pid_file.write_text("999999")
        release_lock(pid_file)
        assert pid_file.exists()
        pid_file.unlink()

    def test_acquire_raises_on_existing_live_process(self, tmp_path):
        from ohm.exceptions import DaemonAlreadyRunningError

        db_path = tmp_path / "test.duckdb"
        pid_file = _get_pid_file(str(db_path))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # Write our own PID (which is "alive")
        pid_file.write_text(str(os.getpid()))

        with pytest.raises(DaemonAlreadyRunningError):
            acquire_lock(str(db_path))

    def test_acquire_removes_stale_pid(self, tmp_path):
        db_path = tmp_path / "test.duckdb"
        pid_file = _get_pid_file(str(db_path))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        # Write a stale PID (process that doesn't exist)
        pid_file.write_text("999999")
        # 999999 is likely not running on a test machine
        if _is_process_running(999999):
            pytest.skip("PID 999999 happens to be running")

        new_pid_file = acquire_lock(str(db_path))
        assert new_pid_file.exists()
        assert int(new_pid_file.read_text().strip()) == os.getpid()
        release_lock(new_pid_file)


class TestOhmStoreIntegration:
    def test_store_acquire_and_release(self, tmp_path):
        """OhmStore acquires lock on init and releases on close."""
        from ohm.graph.embeddings import NullBackend
        from ohm.store import OhmStore

        db_path = str(tmp_path / "guard_test.duckdb")
        store = OhmStore(
            db_path=db_path,
            agent_name="test",
            embedding_backend=NullBackend(dimensions=768),
        )
        pid_file = _get_pid_file(db_path)
        assert pid_file.exists()
        store.close()
        assert not pid_file.exists()

    def test_store_with_disabled_guard(self, tmp_path, monkeypatch):
        """When OHM_DISABLE_CONCURRENCY_GUARD=1, no PID file is created."""
        from ohm.graph.embeddings import NullBackend
        from ohm.store import OhmStore

        monkeypatch.setenv("OHM_DISABLE_CONCURRENCY_GUARD", "1")
        db_path = str(tmp_path / "no_guard_test.duckdb")
        store = OhmStore(
            db_path=db_path,
            agent_name="test",
            embedding_backend=NullBackend(dimensions=768),
        )
        pid_file = _get_pid_file(db_path)
        assert not pid_file.exists()
        store.close()

    def test_store_releases_lock_on_init_failure(self, tmp_path):
        """If OhmStore.__init__ fails after acquiring the lock, the lock is
        released so a same-process retry can succeed (OHM-956)."""
        from unittest.mock import patch

        from ohm.graph.embeddings import NullBackend
        from ohm.store import OhmStore

        db_path = str(tmp_path / "fail_test.duckdb")
        pid_file = _get_pid_file(db_path)

        # First attempt: simulate a failure after lock acquisition
        with patch.object(OhmStore, "_init_schema", side_effect=RuntimeError("simulated failure")):
            try:
                OhmStore(
                    db_path=db_path,
                    agent_name="test",
                    embedding_backend=NullBackend(dimensions=768),
                )
            except RuntimeError:
                pass

        # The PID file must have been cleaned up by the except handler
        assert not pid_file.exists(), "PID file leaked after __init__ failure — self-deadlock!"

        # Second attempt: should succeed normally (no self-deadlock)
        store = OhmStore(
            db_path=db_path,
            agent_name="test",
            embedding_backend=NullBackend(dimensions=768),
        )
        assert pid_file.exists()
        store.close()
        assert not pid_file.exists()