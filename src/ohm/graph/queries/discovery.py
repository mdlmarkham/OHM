"""discovery queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile, _existing_label


def queue_discovery_candidates(
    conn: "DuckDBPyConnection",
    candidate_edges: list[dict[str, Any]],
    *,
    created_by: str = "system",
) -> list[str]:
    """Insert candidate edges from structure learning into the discovery queue.

    Returns list of queue entry IDs.
    """
    from ohm.validation import validate_identifier

    ids = []
    for edge in candidate_edges:
        from_node = validate_identifier(edge["from"], name="from_node")
        to_node = validate_identifier(edge["to"], name="to_node")
        edge_type = edge.get("edge_type", "undirected")
        if edge_type not in ("directed", "undirected"):
            edge_type = "undirected"
        layer = edge.get("layer", "L3")
        confidence = edge.get("confidence")
        provenance = edge.get("provenance", "structure_learning")
        method = edge.get("method", "unknown")

        row_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO ohm_discovery_queue
               (id, from_node, to_node, edge_type, layer, confidence, provenance, method, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [row_id, from_node, to_node, edge_type, layer, confidence, provenance, method, created_by],
        )
        ids.append(row_id)
    return ids


def query_discovery_queue(
    conn: "DuckDBPyConnection",
    *,
    status: str | None = None,
    method: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return pending discovery queue entries for agent review."""
    conditions = ["1=1"]
    params: list[Any] = []

    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if method is not None:
        conditions.append("method = ?")
        params.append(method)

    where = " AND ".join(conditions)
    params.append(limit)

    result = conn.execute(
        f"""SELECT id, from_node, to_node, edge_type, layer, confidence,
                  provenance, method, status, reviewed_by, reviewed_at,
                  review_notes, created_by, created_at
           FROM ohm_discovery_queue
           WHERE {where}
           ORDER BY created_at DESC
           LIMIT ?""",
        params,
    )
    return _rows_to_dicts(result)


def review_discovery_candidate(
    conn: "DuckDBPyConnection",
    queue_id: str,
    *,
    action: str,
    reviewed_by: str,
    review_notes: str | None = None,
    edge_layer: str = "L3",
) -> dict[str, Any]:
    """Accept or reject a discovery queue entry.

    Accept: creates the edge in ohm_edges, marks queue entry as accepted.
    Reject: marks queue entry as rejected with optional notes.
    """
    from ohm.validation import validate_identifier

    queue_id = validate_identifier(queue_id, name="queue_id")

    row = conn.execute(
        "SELECT id, from_node, to_node, edge_type, layer, confidence, provenance, method, status FROM ohm_discovery_queue WHERE id = ?",
        [queue_id],
    ).fetchone()
    if row is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Discovery queue entry {queue_id} not found")

    if row[8] != "pending":
        return {"error": "already_reviewed", "status": row[8], "queue_id": queue_id}

    now_row = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    now = now_row[0] if now_row else None

    if action == "accept":
        from_node = row[1]
        to_node = row[2]
        edge_type = row[3]
        confidence = row[5]
        provenance = row[6]

        ohm_edge_type = edge_type
        if edge_type == "directed":
            ohm_edge_type = "CAUSES"
        elif edge_type == "undirected":
            ohm_edge_type = "CORRELATES_WITH"
        elif edge_type == "SUGGESTED_CAUSES":
            ohm_edge_type = "CAUSES"
        elif edge_type == "SUGGESTED_CORRELATES_WITH":
            ohm_edge_type = "CORRELATES_WITH"
        elif edge_type.startswith("SUGGESTED_"):
            # Strip SUGGESTED_ prefix for any other types
            ohm_edge_type = edge_type[len("SUGGESTED_") :]

        edge_id = create_edge(
            conn,
            from_node=from_node,
            to_node=to_node,
            edge_type=ohm_edge_type,
            layer=edge_layer,
            confidence=confidence if confidence is not None else 0.5,
            provenance=provenance,
            created_by=reviewed_by,
        )

        conn.execute(
            """UPDATE ohm_discovery_queue
               SET status = 'accepted', reviewed_by = ?, reviewed_at = ?, review_notes = ?
               WHERE id = ?""",
            [reviewed_by, now, review_notes, queue_id],
        )

        return {"action": "accepted", "queue_id": queue_id, "edge_id": edge_id}

    elif action == "reject":
        conn.execute(
            """UPDATE ohm_discovery_queue
               SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, review_notes = ?
               WHERE id = ?""",
            [reviewed_by, now, review_notes, queue_id],
        )
        return {"action": "rejected", "queue_id": queue_id}

    else:
        return {"error": "invalid_action", "message": "action must be 'accept' or 'reject'"}



def _existing_label(conn: DuckDBPyConnection, node_id: str) -> str:
    """Look up the label of an existing node by id."""
    row = conn.execute("SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()
    return row[0] if row else node_id

# OHM-447: Lazy cross-domain imports resolved at access time
_LAZY_IMPORTS = {
    "create_edge",
}

def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import ohm.graph.queries as _q
        return getattr(_q, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

