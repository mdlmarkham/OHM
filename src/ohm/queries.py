"""
OHM Queries — High-level query and mutation functions.

These functions operate directly on a DuckDB connection (for use in tests
and direct-connection scenarios) and delegate to the graph module for
recursive CTE construction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .schema import (
    LAYER_EDGE_TYPES,
    VALID_LAYERS,
    VALID_NODE_TYPES,
    validate_edge_type,
    validate_node_type,
)
from .boundary import (
    enforce_challenge_boundary,
    enforce_support_boundary,
)
from .exceptions import (
    EdgeNotFoundError,
)
from . import graph as _graph


# ── Node operations ─────────────────────────────────────────

def create_node(
    conn,
    *,
    label: str,
    node_type: str = "concept",
    content: str | None = None,
    created_by: str = "ohm",
    visibility: str = "team",
    provenance: str | None = None,
    confidence: float = 1.0,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> str:
    """Create a node and return its ID."""
    if not validate_node_type(node_type):
        raise ValueError(f"Invalid node type: {node_type}. Valid: {sorted(VALID_NODE_TYPES)}")

    node_id = f"{label.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    tags_json = __import__("json").dumps(tags) if tags else None
    metadata_json = __import__("json").dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, content, created_by, created_at,
           updated_at, confidence, visibility, provenance, tags, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [node_id, label, node_type, content, created_by, now, now,
         confidence, visibility, provenance, tags_json, metadata_json],
    )

    _log_change(conn, "ohm_nodes", node_id, "INSERT", created_by, layer=None, change_data=None)
    return node_id


def node_exists(conn, node_id: str) -> bool:
    """Check if a node exists."""
    result = conn.execute("SELECT 1 FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
    return result is not None


def edge_exists(conn, edge_id: str) -> bool:
    """Check if an edge exists."""
    result = conn.execute("SELECT 1 FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
    return result is not None


# ── Edge operations ──────────────────────────────────────────

def create_edge(
    conn,
    *,
    from_node: str,
    to_node: str,
    layer: str,
    edge_type: str,
    created_by: str = "ohm",
    confidence: float | None = None,
    condition: str | None = None,
    provenance: str | None = None,
    challenge_of: str | None = None,
    challenge_type: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Create an edge with validation and return its ID."""
    if layer not in VALID_LAYERS:
        raise ValueError(f"Invalid layer: {layer}. Valid: {sorted(VALID_LAYERS)}")
    if not validate_edge_type(layer, edge_type):
        raise ValueError(
            f"Invalid edge type '{edge_type}' for layer '{layer}'. "
            f"Valid for {layer}: {sorted(LAYER_EDGE_TYPES[layer])}"
        )

    edge_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    metadata_json = __import__("json").dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type,
           confidence, condition, provenance, created_by, created_at, updated_at,
           challenge_of, challenge_type, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_node, to_node, layer, edge_type,
         confidence, condition, provenance, created_by, now, now,
         challenge_of, challenge_type, metadata_json],
    )

    _log_change(conn, "ohm_edges", edge_id, "INSERT", created_by, layer=layer, change_data=None)
    return edge_id


def create_challenge(
    conn,
    *,
    edge_id: str,
    reason: str,
    created_by: str = "ohm",
    confidence: float | None = None,
    challenge_type: str = "CHALLENGED_BY",
) -> str:
    """Create a challenge edge referencing an existing edge."""
    # Enforce boundary: can only challenge L3/L4
    enforce_challenge_boundary(conn, created_by, edge_id)

    # Get original edge details
    original = conn.execute(
        "SELECT from_node, to_node, layer FROM ohm_edges WHERE id = ?",
        [edge_id],
    ).fetchone()
    if original is None:
        raise EdgeNotFoundError(f"Edge '{edge_id}' not found")

    from_node, to_node, layer = original

    return create_edge(
        conn,
        from_node=from_node,
        to_node=to_node,
        layer=layer,
        edge_type=challenge_type,
        created_by=created_by,
        confidence=confidence,
        condition=reason,
        challenge_of=edge_id,
        challenge_type=challenge_type,
    )


def create_support(
    conn,
    *,
    edge_id: str,
    reason: str,
    created_by: str = "ohm",
    confidence: float | None = None,
) -> str:
    """Create a support edge referencing an existing edge."""
    # Enforce boundary: can only support L3/L4
    enforce_support_boundary(conn, created_by, edge_id)

    # Get original edge details
    original = conn.execute(
        "SELECT from_node, to_node, layer FROM ohm_edges WHERE id = ?",
        [edge_id],
    ).fetchone()
    if original is None:
        raise EdgeNotFoundError(f"Edge '{edge_id}' not found")

    from_node, to_node, layer = original

    return create_edge(
        conn,
        from_node=from_node,
        to_node=to_node,
        layer=layer,
        edge_type="SUPPORTS",
        created_by=created_by,
        confidence=confidence,
        condition=reason,
        challenge_of=edge_id,
        challenge_type="SUPPORTS",
    )


# ── Agent state ─────────────────────────────────────────────

def set_agent_state(
    conn,
    *,
    agent_name: str,
    focus: str | None = None,
    patterns: list[str] | None = None,
    services: list[str] | None = None,
    session_id: str | None = None,
) -> None:
    """Set or update agent state."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    patterns_json = __import__("json").dumps(patterns) if patterns else None
    services_json = __import__("json").dumps(services) if services else None

    # Upsert
    conn.execute(
        """INSERT INTO ohm_agent_state (agent_name, current_focus, active_patterns,
           available_services, current_session_id, last_sync, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (agent_name) DO UPDATE SET
               current_focus = COALESCE(EXCLUDED.current_focus, ohm_agent_state.current_focus),
               active_patterns = COALESCE(EXCLUDED.active_patterns, ohm_agent_state.active_patterns),
               available_services = COALESCE(EXCLUDED.available_services, ohm_agent_state.available_services),
               current_session_id = COALESCE(EXCLUDED.current_session_id, ohm_agent_state.current_session_id),
               last_sync = EXCLUDED.last_sync,
               updated_at = EXCLUDED.updated_at""",
        [agent_name, focus, patterns_json, services_json, session_id, now, now],
    )


def query_agent_state(conn, agent_name: str | None = None) -> list[dict]:
    """Query agent state. If agent_name is None, return all agents."""
    if agent_name:
        rows = conn.execute(
            "SELECT * FROM ohm_agent_state WHERE agent_name = ?",
            [agent_name],
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name").fetchall()

    columns = [desc[0] for desc in conn.description] if conn.description else []
    return [dict(zip(columns, row)) for row in rows]


# ── Graph query functions ───────────────────────────────────

def query_neighborhood(conn, node_id: str, depth: int = 3, layer: str | None = None,
                        direction: str | None = None) -> list[dict]:
    """Query neighborhood of a node using recursive CTE."""
    sql, params = _graph.build_neighborhood_query(node_id, depth, layer, direction)
    rows = conn.execute(sql, params).fetchall()
    columns = [desc[0] for desc in conn.description] if conn.description else []
    return [dict(zip(columns, row)) for row in rows]


def query_path(conn, from_node: str, to_node: str, max_depth: int = 5) -> list[dict]:
    """Find shortest path between two nodes."""
    sql, params = _graph.build_path_query(from_node, to_node, max_depth)
    rows = conn.execute(sql, params).fetchall()
    columns = [desc[0] for desc in conn.description] if conn.description else []
    return [dict(zip(columns, row)) for row in rows]


def query_impact(conn, node_id: str, depth: int = 5) -> list[dict]:
    """Analyze downstream impact of a node."""
    sql, params = _graph.build_impact_query(node_id, depth)
    rows = conn.execute(sql, params).fetchall()
    columns = [desc[0] for desc in conn.description] if conn.description else []
    return [dict(zip(columns, row)) for row in rows]


def query_confidence(conn, edge_id: str) -> dict:
    """Audit confidence for an edge: original, challenges, supports."""
    # Get original edge
    original_row = conn.execute(
        "SELECT * FROM ohm_edges WHERE id = ?", [edge_id],
    ).fetchone()
    if original_row is None:
        return {"original": None, "challenges": [], "supports": []}

    columns = [desc[0] for desc in conn.description]
    original = dict(zip(columns, original_row))

    # Get challenges
    challenge_rows = conn.execute(
        "SELECT * FROM ohm_edges WHERE challenge_of = ? AND challenge_type IN ('CHALLENGED_BY', 'CONTRADICTS', 'REFINES')",
        [edge_id],
    ).fetchall()
    challenges = [dict(zip(columns, row)) for row in challenge_rows]

    # Get supports
    support_rows = conn.execute(
        "SELECT * FROM ohm_edges WHERE challenge_of = ? AND challenge_type = 'SUPPORTS'",
        [edge_id],
    ).fetchall()
    supports = [dict(zip(columns, row)) for row in support_rows]

    return {"original": original, "challenges": challenges, "supports": supports}


def query_stats(conn) -> dict:
    """Return graph statistics."""
    node_count = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()[0]
    edge_count = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()[0]

    edges_by_layer = {}
    rows = conn.execute(
        "SELECT layer, COUNT(*) as cnt FROM ohm_edges GROUP BY layer ORDER BY layer"
    ).fetchall()
    for layer, cnt in rows:
        edges_by_layer[layer] = cnt

    challenge_count = conn.execute(
        "SELECT COUNT(*) FROM ohm_edges WHERE challenge_of IS NOT NULL"
    ).fetchone()[0]
    challenge_ratio = challenge_count / edge_count if edge_count > 0 else 0.0

    return {
        "total_nodes": node_count,
        "total_edges": edge_count,
        "edges_by_layer": edges_by_layer,
        "challenge_ratio": challenge_ratio,
    }


def query_change_feed(conn, since: str | None = None, agent_name: str | None = None) -> list[dict]:
    """Query change feed since a timestamp."""
    if since is None:
        rows = conn.execute(
            "SELECT * FROM ohm_change_log ORDER BY changed_at DESC LIMIT 100"
        ).fetchall()
    elif agent_name:
        rows = conn.execute(
            "SELECT * FROM ohm_change_log WHERE changed_at > ? AND agent_name = ? ORDER BY changed_at DESC LIMIT 100",
            [since, agent_name],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ohm_change_log WHERE changed_at > ? ORDER BY changed_at DESC LIMIT 100",
            [since],
        ).fetchall()

    columns = [desc[0] for desc in conn.description] if conn.description else []
    return [dict(zip(columns, row)) for row in rows]


# ── Internal ────────────────────────────────────────────────

def _log_change(conn, table_name: str, row_id: str, operation: str,
                agent_name: str, layer: str | None = None,
                change_data: dict | None = None) -> None:
    """Append a change log entry."""
    import json as _json
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    data_json = _json.dumps(change_data) if change_data else None
    log_id = str(uuid.uuid4())

    conn.execute(
        """INSERT INTO ohm_change_log (id, table_name, row_id, operation, agent_name,
           layer, changed_at, change_data)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [log_id, table_name, row_id, operation, agent_name, layer, now, data_json],
    )