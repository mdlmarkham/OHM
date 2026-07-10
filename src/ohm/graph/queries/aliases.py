"""aliases queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts

# ── Alias Resolution & Content Hashing (OHM-g0kv) ────────────────────────


def register_alias(
    conn: DuckDBPyConnection,
    *,
    alias_norm: str,
    node_id: str,
) -> dict[str, Any]:
    """Register a normalized alias for a node.

    Allows multiple alias_norm entries for different node_ids (collision
    detection). Skips if this exact (alias_norm, node_id) pair already exists.

    Args:
        alias_norm: The normalized alias string.
        node_id: The node this alias points to.

    Returns:
        Dict with the alias id and node_id.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    existing = conn.execute(
        "SELECT id FROM ohm_aliases WHERE alias_norm = ? AND node_id = ?",
        [alias_norm, node_id],
    ).fetchone()
    if existing:
        return {"id": existing[0], "alias_norm": alias_norm, "node_id": node_id, "created": False}

    import uuid

    alias_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_aliases (id, alias_norm, node_id) VALUES (?, ?, ?)",
        [alias_id, alias_norm, node_id],
    )
    return {"id": alias_id, "alias_norm": alias_norm, "node_id": node_id, "created": True}


def resolve_alias(
    conn: DuckDBPyConnection,
    *,
    alias_norm: str,
) -> list[dict[str, Any]]:
    """Look up a normalized alias. Returns list of matching alias records."""
    result = conn.execute(
        "SELECT id, alias_norm, node_id, created_at FROM ohm_aliases WHERE alias_norm = ?",
        [alias_norm],
    )
    return _rows_to_dicts(result)


def query_aliases(
    conn: DuckDBPyConnection,
    *,
    node_id: str | None = None,
    prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Query aliases, optionally filtered by node_id or prefix."""
    from ohm.validation import validate_identifier

    conditions = []
    params: list[Any] = []

    if node_id is not None:
        node_id = validate_identifier(node_id, name="node_id")
        conditions.append("node_id = ?")
        params.append(node_id)

    if prefix is not None:
        conditions.append("alias_norm LIKE ?")
        params.append(f"{prefix}%")

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    result = conn.execute(
        f"SELECT id, alias_norm, node_id, created_at FROM ohm_aliases{where} ORDER BY alias_norm",
        params,
    )
    return _rows_to_dicts(result)


def register_content_hash(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    content_hash: str,
) -> dict[str, Any]:
    """Register a content hash for a node. Upsert semantics."""
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    existing = conn.execute(
        "SELECT id FROM ohm_content_hashes WHERE node_id = ?",
        [node_id],
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE ohm_content_hashes SET content_hash = ? WHERE node_id = ?",
            [content_hash, node_id],
        )
        return {"id": existing[0], "node_id": node_id, "content_hash": content_hash, "created": False}

    import uuid

    hash_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_content_hashes (id, node_id, content_hash) VALUES (?, ?, ?)",
        [hash_id, node_id, content_hash],
    )
    return {"id": hash_id, "node_id": node_id, "content_hash": content_hash, "created": True}


def lookup_content_hash(
    conn: DuckDBPyConnection,
    *,
    content_hash: str,
) -> list[dict[str, Any]]:
    """Find nodes with a given content hash (for dedup detection)."""
    result = conn.execute(
        "SELECT id, node_id, content_hash, created_at FROM ohm_content_hashes WHERE content_hash = ?",
        [content_hash],
    )
    return _rows_to_dicts(result)


# ── Confidence Change Log (OHM-733) ─────────────────────────────────────────


def log_confidence_change(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    agent: str,
    new_value: float,
    reason: str,
    old_value: float | None = None,
    challenge_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a row to the confidence change log (OHM-733).

    This is the single entry point for recording a confidence-affecting
    event on an edge. The log is append-only — every change is attributed
    to an agent with a reason. ``ohm_edges.confidence`` is refreshed from
    the log via :func:`recompute_confidence_from_log` (idempotent).
    """
    import json as _json
    import uuid as _uuid

    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    log_id = str(_uuid.uuid4())
    meta_json = _json.dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO ohm_confidence_log
           (id, edge_id, agent, old_value, new_value, reason, challenge_id, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [log_id, edge_id, agent, old_value, new_value, reason, challenge_id, meta_json],
    )

    # Refresh the cached column from the log (idempotent recompute)
    conn.execute(
        "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
        [new_value, edge_id],
    )

    return {
        "id": log_id,
        "edge_id": edge_id,
        "agent": agent,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    }


def recompute_confidence_from_log(
    conn: DuckDBPyConnection,
    edge_id: str,
) -> float | None:
    """Recompute an edge's confidence from the append-only log (OHM-733).

    Takes the ``new_value`` from the most recent log row for this edge
    and writes it to ``ohm_edges.confidence``. Idempotent — safe to call
    from multiple daemons concurrently; the result is the same regardless
    of ordering because "most recent by created_at" is deterministic.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        """SELECT new_value FROM ohm_confidence_log
           WHERE edge_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        [edge_id],
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    conn.execute(
        "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
        [value, edge_id],
    )
    return value


def get_confidence_history(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the full confidence change history for an edge (OHM-733)."""
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    result = conn.execute(
        """SELECT id, edge_id, agent, old_value, new_value, reason,
                  challenge_id, created_at
           FROM ohm_confidence_log
           WHERE edge_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        [edge_id, limit],
    )
    return _rows_to_dicts(result)


def resolve_node_by_alias(
    conn: DuckDBPyConnection,
    *,
    query: str,
) -> dict[str, Any] | None:
    """Resolve a query string to a node via alias matching.

    Normalizes the query, checks ohm_aliases, returns the first
    matching node record (or None if no match found).
    """
    from ohm.validation import normalize_alias

    norm = normalize_alias(query)
    if not norm:
        return None

    alias_row = conn.execute(
        "SELECT node_id FROM ohm_aliases WHERE alias_norm = ? LIMIT 1",
        [norm],
    ).fetchone()
    if not alias_row:
        return None

    node_id = alias_row[0]
    node = conn.execute(
        "SELECT id, label, type, confidence, visibility FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node:
        return None

    return {"id": node[0], "label": node[1], "type": node[2], "confidence": node[3], "visibility": node[4]}


