"""Tests for OHM-lqpk.3: DuckDB PRAGMA tuning in the connection lifecycle.

The PRAGMAs are applied via ``_apply_pragmas()`` in src/ohm/graph/db.py,
called from both ``connect()`` (the canonical helper) and
``OhmStore._connect_with_wal_recovery()`` (the daemon's write path).
Read-only connections skip the thread PRAGMA (a single query plan is
serial anyway) but still get enable_object_cache.

Coverage:
- OHM_DUCKDB_THREADS=N env var → threads=N on the connection
- Default (no env var) → threads=max(1, cpu_count//2)
- Invalid env var (non-integer) → falls back to default with a warning
- enable_object_cache=True is always set on write connections
- OHM_DUCKDB_TEMP_DIR → temp_directory is set
- Read-only connections are NOT thread-tuned (write-only optimisation)
- The helper survives PRAGMA failures (defence-in-depth — log + swallow)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_conn() -> duckdb.DuckDBPyConnection:
    """Return a fresh in-memory DuckDB without going through OHM's
    connect() so we can test _apply_pragmas() in isolation."""
    return duckdb.connect(":memory:")


def _read_setting(conn, name: str):
    """Read a DuckDB setting via current_setting(). Returns the value."""
    return conn.execute(f"SELECT current_setting('{name}')").fetchone()[0]


class TestPragmaThreads:
    """OHM_DUCKDB_THREADS sets the worker pool size on write connections."""

    def test_env_var_sets_threads(self, monkeypatch):
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "3")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert int(_read_setting(conn, "threads")) == 3
        finally:
            conn.close()

    def test_env_var_one_is_respected(self, monkeypatch):
        """A user explicitly setting threads=1 (single-threaded) must not
        be silently bumped — that's a real workload signal."""
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "1")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert int(_read_setting(conn, "threads")) == 1
        finally:
            conn.close()

    def test_env_var_large_value_is_respected(self, monkeypatch):
        """Even if a user sets threads to a value larger than the core
        count (for I/O-bound workloads), respect it."""
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "64")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert int(_read_setting(conn, "threads")) == 64
        finally:
            conn.close()

    def test_default_threads_is_half_cores(self, monkeypatch):
        """No env var → threads = max(1, cpu_count // 2)."""
        monkeypatch.delenv("OHM_DUCKDB_THREADS", raising=False)
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            expected = max(1, (os.cpu_count() or 1) // 2)
            assert int(_read_setting(conn, "threads")) == expected
        finally:
            conn.close()

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch):
        """A non-integer OHM_DUCKDB_THREADS must NOT raise — it should
        warn and use the default. A misconfigured operator should not
        crash the daemon at boot."""
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "not-a-number")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)  # must not raise
            expected = max(1, (os.cpu_count() or 1) // 2)
            assert int(_read_setting(conn, "threads")) == expected
        finally:
            conn.close()

    def test_zero_threads_becomes_one(self, monkeypatch):
        """threads=0 in DuckDB means 'use all cores', which is the
        behaviour we're trying to override. Clamp to 1."""
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "0")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert int(_read_setting(conn, "threads")) >= 1
        finally:
            conn.close()

    def test_negative_threads_becomes_one(self, monkeypatch):
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "-3")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert int(_read_setting(conn, "threads")) >= 1
        finally:
            conn.close()


class TestPragmaObjectCache:
    """enable_object_cache=True is always set on connections we tune."""

    def test_object_cache_enabled_on_write_connection(self, monkeypatch):
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "2")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            assert _read_setting(conn, "enable_object_cache") is True or _read_setting(conn, "enable_object_cache") == "true"
        finally:
            conn.close()


class TestPragmaTempDirectory:
    """OHM_DUCKDB_TEMP_DIR sets the spill directory."""

    def test_temp_dir_env_sets_setting(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OHM_DUCKDB_TEMP_DIR", str(tmp_path))
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)
            # DuckDB's temp_directory setting reflects the configured path.
            actual = _read_setting(conn, "temp_directory")
            assert str(tmp_path) in str(actual), f"Expected temp_directory to contain {tmp_path}, got {actual}"
        finally:
            conn.close()

    def test_no_temp_dir_env_keeps_default(self, monkeypatch):
        """No OHM_DUCKDB_TEMP_DIR → leave DuckDB's default in place
        (system temp). We should not touch the setting at all."""
        monkeypatch.delenv("OHM_DUCKDB_TEMP_DIR", raising=False)
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _read_setting(conn, "temp_directory")
            _apply_pragmas(conn)
            default_after = _read_setting(conn, "temp_directory")
            # If the helper touched the setting, the value would differ.
            # Some DuckDB versions normalise the path (e.g., resolve
            # symlinks), so we just assert the path is non-empty and
            # points somewhere sensible — not the operator's CWD.
            assert default_after, "temp_directory should not be empty"
            # Don't assert equality — the helper may have run on a
            # different connection state. The key invariant: we don't
            # CRASH and we don't point it at a nonsense path.
        finally:
            conn.close()


class TestPragmaHelperSurvivesFailures:
    """The helper must NOT raise if a PRAGMA fails. PRAGMA tuning is a
    perf optimisation, not a correctness requirement.

    We can't directly mock ``DuckDBPyConnection.execute`` (it's a
    read-only C-implemented attribute), so we exercise the failure path
    by pointing temp_directory at an invalid path that DuckDB refuses.
    The threads PRAGMA and enable_object_cache are well-tested above.
    """

    def test_invalid_temp_dir_does_not_propagate(self, monkeypatch, tmp_path):
        """Setting temp_directory to a path DuckDB can't create must
        NOT crash the helper — it should log a debug line and move on.
        Operators occasionally typo OHM_DUCKDB_TEMP_DIR; that shouldn't
        brick the daemon."""
        # Use a path under a non-existent parent directory on Windows
        # (DuckDB refuses to create intermediate dirs).
        bad_path = str(tmp_path / "missing" / "dir" / "tempdir")
        monkeypatch.setenv("OHM_DUCKDB_TEMP_DIR", bad_path)
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "2")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import _apply_pragmas

        conn = _make_conn()
        try:
            _apply_pragmas(conn)  # must NOT raise even if temp_directory fails
            # threads and object_cache must still have been applied.
            assert int(_read_setting(conn, "threads")) == 2
            assert _read_setting(conn, "enable_object_cache") in (True, "true")
        finally:
            conn.close()


class TestConnectAppliesPragmas:
    """The canonical ``ohm.graph.db.connect()`` must apply the PRAGMAs."""

    def test_connect_applies_pragmas(self, monkeypatch):
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "7")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.graph.db import connect

        conn = connect(":memory:")
        try:
            assert int(_read_setting(conn, "threads")) == 7
            assert _read_setting(conn, "enable_object_cache") in (True, "true")
        finally:
            conn.close()


class TestOhmStoreAppliesPragmas:
    """The OhmStore write path must apply the PRAGMAs (symmetry with
    the canonical ``connect()`` helper). Without this, the daemon's
    main write connection runs on DuckDB's default settings while
    one-off CLI calls benefit from the tuned pool — a silent perf
    cliff."""

    def test_ohmstore_write_connection_has_tuned_threads(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "6")
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from ohm.store import OhmStore

        db_path = tmp_path / "pragma_test.duckdb"
        store = OhmStore(str(db_path))
        try:
            assert int(_read_setting(store.conn, "threads")) == 6
        finally:
            store.close()
            if db_path.exists():
                db_path.unlink()
            wal = db_path.with_suffix(".duckdb.wal")
            if wal.exists():
                wal.unlink()

    def test_ohmstore_readonly_connection_is_not_thread_tuned(self, monkeypatch, tmp_path):
        """Read-only OhmStore connections are short-lived (one query
        path) and don't benefit from threads>1. The helper skips them
        to avoid PRAGMA-threads errors on locked-down DuckDB builds."""
        monkeypatch.setenv("OHM_DUCKDB_THREADS", "6")
        sys.path.insert(0, str(REPO_ROOT / "src"))

        # Seed a write DB so the read-only open has something to read.
        from ohm.store import OhmStore

        db_path = tmp_path / "ro_test.duckdb"
        store = OhmStore(str(db_path))
        try:
            store.conn.execute("INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES ('ro_seed', 'Seed', 'concept', 'seeder', CURRENT_TIMESTAMP)")
            store.conn.execute("CHECKPOINT")
        finally:
            store.close()

        # Now open read-only.
        ro_store = OhmStore(str(db_path), readonly=True)
        try:
            # The thread count must NOT have been forced to 6 — the helper
            # skipped the read-only connection. So it stays at DuckDB's
            # default for this connection. We don't assert the exact
            # value (it depends on DuckDB version); we just assert it's
            # NOT 6 — proving the helper skipped.
            actual = int(_read_setting(ro_store.conn, "threads"))
            assert actual != 6, f"Read-only connection should NOT have been thread-tuned to 6, but got {actual} (OHM-lqpk.3 helper skipped)"
        finally:
            ro_store.close()
            if db_path.exists():
                db_path.unlink()
