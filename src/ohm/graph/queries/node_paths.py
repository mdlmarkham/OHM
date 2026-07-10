"""Node path / UNS hierarchical address queries (OHM-447 domain 29)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


def set_node_path(
    conn: "DuckDBPyConnection",
    *,
    node_id: str,
    node_path: str,
    created_by: str,
) -> dict[str, Any]:
    """Set the UNS hierarchical path on a node (OHM-ivlt)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    node_id = validate_identifier(node_id, name="node_id")
    if not node_path:
        raise ValueError("node_path must be non-empty")

    row = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()
    if not row or row[0] == 0:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    conn.execute(
        "UPDATE ohm_nodes SET node_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [node_path, node_id],
    )
    _log_change(conn, "ohm_nodes", node_id, "UPDATE", created_by)
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [node_id]))
    return rows[0] if rows else {}


def get_nodes_by_path_prefix(
    conn: "DuckDBPyConnection",
    path_prefix: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Find nodes whose node_path starts with a given prefix (OHM-ivlt)."""
    if not path_prefix:
        raise ValueError("path_prefix must be non-empty")
    prefix = path_prefix + "%"
    rows = conn.execute(
        "SELECT * FROM ohm_nodes WHERE node_path LIKE ? AND deleted_at IS NULL ORDER BY node_path LIMIT ?",
        [prefix, limit],
    )
    return _rows_to_dicts(rows)
