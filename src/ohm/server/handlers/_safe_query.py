"""Defensive query helpers for HTTP handler mixins (GH #896).

Centralizes the guards that keep admin/observability endpoints from crashing
with ``NoneType not subscriptable`` (or similar) when the DuckDB connection is
unavailable or a query returns empty/malformed rows. Handler mixins should
import these helpers rather than reinventing the defensive plumbing.

The helpers are deliberately broad: an observability endpoint that returns a
degraded but well-formed response is preferable to an unhandled 500 that
breaks routine heartbeat checks. Failures are logged via ``logger.exception``
so they remain visible without taking the endpoint down.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def safe_scalar(
    conn: Any,
    sql: str,
    params: list | None = None,
    default: int | float | None = 0,
) -> int | float | None:
    """Run a single-value SQL query and return the first column of the first row.

    Returns *default* when *conn* is None, the query raises, or no row is
    returned. This is the drop-in replacement for the bare
    ``conn.execute(sql, params).fetchone()[0]`` pattern that crashes with
    ``NoneType not subscriptable`` on empty result sets or a missing connection.
    """
    if conn is None:
        return default
    try:
        row = conn.execute(sql, params or []).fetchone()
    except Exception:
        logger.exception("safe_scalar query failed: %s", sql)
        return default
    if row is None or len(row) == 0:
        return default
    return row[0]


def safe_rows(
    conn: Any,
    sql: str,
    params: list | None = None,
) -> list:
    """Run a multi-row SQL query and return a list of row tuples.

    Returns an empty list when *conn* is None or the query raises, so callers
    can iterate without a separate guard.
    """
    if conn is None:
        return []
    try:
        return conn.execute(sql, params or []).fetchall()
    except Exception:
        logger.exception("safe_rows query failed: %s", sql)
        return []


def safe_unpack_type_rows(rows: list | None, expected_cols: int = 2) -> list[tuple]:
    """Validate that every row tuple has *expected_cols* columns.

    Returns a list of well-shaped row tuples, skipping any malformed row
    (wrong arity or non-tuple). Used to protect ``for a, b in rows`` unpacking
    from ``ValueError``/``TypeError`` on degraded or malformed result sets.
    """
    out: list[tuple] = []
    for row in rows or []:
        if isinstance(row, tuple) and len(row) == expected_cols:
            out.append(row)
    return out


def db_unavailable_response() -> tuple[int, dict]:
    """Return the canonical (status, body) pair for a missing DB connection."""
    return (503, {"error": "database_unavailable"})


__all__ = [
    "safe_scalar",
    "safe_rows",
    "safe_unpack_type_rows",
    "db_unavailable_response",
]
