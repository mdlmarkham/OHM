"""Startup concurrency guard — prevents double-open of DuckDB files (OHM-955).

DuckDB is single-writer. When two processes open the same file, the second
gets a raw ``IOException`` and the WAL can corrupt, leading to total data
loss. This module provides a PID-file-based guard that:

1. Checks for an existing PID file before ``duckdb.connect``.
2. If a PID file exists and the process is alive, raises ``DaemonAlreadyRunningError``.
3. If the PID file is stale (process dead), removes it and proceeds.
4. Writes a new PID file with the current process's PID.
5. On ``close()``, removes the PID file.

The guard is bypassed when:
- ``OHM_DISABLE_CONCURRENCY_GUARD=1`` is set (tests, dev).
- ``readonly=True`` (read-only connections don't need exclusive access).
- ``db_path`` is ``:memory:`` or ``None`` (in-memory DBs).
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import Optional


def _get_pid_file(db_path: str | os.PathLike) -> Path:
    """Return the PID file path for a given DB path."""
    db = Path(db_path)
    state_dir = Path(os.environ.get("OHM_STATE_DIR", str(db.parent)))
    return state_dir / f"{db.stem}.pid"


def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running (cross-platform)."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def acquire_lock(db_path: str | os.PathLike) -> Path:
    """Acquire a PID-file lock for the given DB path.

    Returns the PID file path (for later release).

    Raises:
        DaemonAlreadyRunningError: If another live process holds the lock.
    """
    from ohm.exceptions import DaemonAlreadyRunningError

    pid_file = _get_pid_file(db_path)

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            old_pid = 0

        if old_pid > 0 and _is_process_running(old_pid):
            raise DaemonAlreadyRunningError(
                f"Another ohmd process (PID {old_pid}) is already using "
                f"database '{db_path}'. Refusing to start to prevent "
                f"WAL corruption (OHM-955)."
            )

        pid_file.unlink(missing_ok=True)

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))
    return pid_file


def release_lock(pid_file: Path | None) -> None:
    """Release the PID-file lock."""
    if pid_file is None:
        return
    try:
        if pid_file.exists():
            current_pid = int(pid_file.read_text().strip())
            if current_pid == os.getpid():
                pid_file.unlink()
    except (ValueError, OSError):
        pass


def is_guard_enabled(readonly: bool = False, db_path: str | None = None) -> bool:
    """Check if the concurrency guard should be active."""
    if os.environ.get("OHM_DISABLE_CONCURRENCY_GUARD", "").strip() in ("1", "true", "yes"):
        return False
    if readonly:
        return False
    if db_path is None or db_path == ":memory:":
        return False
    return True