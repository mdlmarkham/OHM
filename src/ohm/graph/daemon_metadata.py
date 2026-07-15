"""Startup node-count assertion helpers (OHM #919).

ohmd writes to a DuckDB file. If the process is killed (SIGKILL, OOM, host
reboot) without a final CHECKPOINT, writes buffered in the WAL can be lost
on the next start. These helpers detect unexplained node-count drops at
daemon startup by comparing the current live node count to a baseline
persisted in ``ohm_meta`` (key ``last_node_count``) on the previous graceful
shutdown.

The baseline is a stringified integer. First-ever startup (no row in
``ohm_meta``) is NOT a drop — the baseline is persisted silently so the next
startup has something to compare against even if the daemon is hard-killed
before its first graceful shutdown.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

_META_KEY = "last_node_count"

logger = logging.getLogger(__name__)


def read_node_count_baseline(conn: "DuckDBPyConnection") -> int | None:
    """Return the persisted last-known node count, or None if no baseline.

    Returns None when the ``ohm_meta`` row is absent (first startup) or
    holds a non-integer value (corrupt/legacy). Never raises.
    """
    try:
        row = conn.execute(
            "SELECT value FROM ohm_meta WHERE key = ?", [_META_KEY]
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def count_live_nodes(conn: "DuckDBPyConnection") -> int:
    """Count non-soft-deleted nodes (the regression-guard baseline)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def persist_node_count_baseline(conn: "DuckDBPyConnection") -> int:
    """Write the current live node count to ``ohm_meta`` and return it.

    Idempotent — uses ``INSERT OR REPLACE``. Called on graceful shutdown
    (after CHECKPOINT, before ``store.close()``) and on first-ever startup.
    """
    current = count_live_nodes(conn)
    conn.execute(
        "INSERT OR REPLACE INTO ohm_meta (key, value) VALUES (?, ?)",
        [_META_KEY, str(current)],
    )
    return current


def check_node_count_baseline(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Compare the current live node count to the persisted baseline.

    Returns a dict with keys:
        last: int | None  — persisted baseline (None on first startup)
        current: int      — current live node count
        delta: int        — current - last (0 when last is None)
        dropped: bool     — True iff last is not None and current < last
    """
    last = read_node_count_baseline(conn)
    current = count_live_nodes(conn)
    delta = (current - last) if last is not None else 0
    return {
        "last": last,
        "current": current,
        "delta": delta,
        "dropped": last is not None and current < last,
    }


def assert_node_count_at_startup(
    conn: "DuckDBPyConnection",
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run the startup node-count assertion (OHM #919).

    - First-ever startup (no baseline): persist the baseline silently and
      return a non-dropped check with ``last`` set to ``current``.
    - Drop detected (``current < last``): log a WARNING with the delta.
    - Growth / equal: no warning.

    Returns the check dict (with ``last`` always populated after this call).
    The caller is expected to stash the result on the store so ``GET /health``
    can surface it.
    """
    if log is None:
        log = logger
    check = check_node_count_baseline(conn)
    if check["last"] is None:
        persist_node_count_baseline(conn)
        check["last"] = check["current"]
        check["delta"] = 0
        check["dropped"] = False
    elif check["dropped"]:
        log.warning(
            "Startup node-count drop detected: last=%s current=%s delta=%s "
            "(possible WAL loss from hard shutdown — see OHM #919)",
            check["last"],
            check["current"],
            check["delta"],
        )
    return check
