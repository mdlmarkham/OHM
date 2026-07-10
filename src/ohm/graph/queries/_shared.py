"""Private helpers shared across all queries submodules (OHM-447).

These are intentionally not re-exported from ``queries/__init__.py`` —
they're internal utilities used by the domain-specific submodules.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _rows_to_dicts(result: Any) -> list[dict[str, Any]]:
    """Convert DuckDB query result to list of dicts using column descriptions."""
    if not result:
        return []
    columns = [desc[0] for desc in result.description]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for row in rows:
        if "from_node" in row:
            row["from"] = row["from_node"]
            row["to"] = row["to_node"]
            if "edge_type" in row:
                row["type"] = row["edge_type"]
        if "type" in row and "from_node" not in row and "node_type" not in row:
            row["node_type"] = row["type"]
    return rows


def _percentile(count: int, trials: int, pct: float) -> float:
    """Compute a percentile for a binomial activation count."""
    if trials == 0:
        return 0.0
    p = count / trials
    if p == 0.0 or p == 1.0:
        return p
    import math

    z = {0.05: -1.645, 0.50: 0.0, 0.95: 1.645}.get(pct, 0.0)
    se = math.sqrt(p * (1 - p) / trials)
    result = p + z * se
    return max(0.0, min(1.0, result))


def _log_change(
    conn: "DuckDBPyConnection",
    table_name: str,
    row_id: str,
    operation: str,
    agent_name: str,
) -> None:
    """Log a write operation to the change feed.

    This mirrors store.py._log_change() for the direct-connection
    path. Both paths must populate ohm_change_feed so that
    listen() works regardless of how agents connect.
    """
    try:
        conn.execute(
            """INSERT INTO ohm_change_feed
               (table_name, row_id, operation, agent_name, old_data)
               VALUES (?, ?, ?, ?, ?)""",
            [table_name, row_id, operation, agent_name, json.dumps({})],
        )
    except Exception:
        pass


def _existing_label(conn: "DuckDBPyConnection", node_id: str) -> str:
    """Look up the label of an existing node by id."""
    row = conn.execute("SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()
    return row[0] if row else node_id