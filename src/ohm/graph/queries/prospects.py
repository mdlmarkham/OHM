"""prospect queries (OHM-844: MCP-first prospect lifecycle surfaces).

Provides create, transition, list, and detail operations for prospect nodes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts

# Valid prospect lifecycle transitions
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"committed", "superseded"},
    "committed": {"active", "failed", "superseded"},
    "active": {"completed", "failed", "superseded"},
    "completed": set(),
    "failed": set(),
    "superseded": set(),
}


def create_prospect(
    conn: DuckDBPyConnection,
    *,
    label: str,
    created_by: str,
    authority: str | None = None,
    parent_scenario_id: str | None = None,
    planned_start: str | None = None,
    planned_end: str | None = None,
    horizon_label: str | None = None,
    tags: list[str] | None = None,
    content: str | None = None,
    connects_to: list[str] | None = None,
    confidence: float = 1.0,
) -> dict[str, Any]:
    """Create a prospect node with initial status 'proposed' (OHM-844).

    Args:
        conn: Database connection.
        label: Human-readable prospect description.
        created_by: Agent creating the prospect.
        authority: Agent who can authorize transitions (plain field equality check).
        parent_scenario_id: Optional scenario this prospect is derived from.
        planned_start: ISO 8601 planned start date.
        planned_end: ISO 8601 planned end date.
        horizon_label: Human-readable horizon (e.g. 'Q3 2026', '6 months').
        tags: Optional tags for scope filtering.
        content: Optional description/rationale.
        connects_to: Additional nodes to cross-link (ADR-018).
        confidence: Initial confidence (0-1).

    Returns:
        The created prospect node record.
    """
    from ohm.graph.queries import create_node, create_edge

    all_connects = list(connects_to or [])
    if parent_scenario_id:
        all_connects.append(parent_scenario_id)

    metadata: dict[str, Any] = {}
    if planned_start:
        metadata["planned_start"] = planned_start
    if planned_end:
        metadata["planned_end"] = planned_end
    if horizon_label:
        metadata["horizon_label"] = horizon_label

    prospect = create_node(
        conn,
        label=label,
        node_type="prospect",
        content=content,
        created_by=created_by,
        tags=tags,
        metadata=metadata or None,
        confidence=confidence,
        connects_to=all_connects or None,
    )

    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'proposed', assigned_to = ? WHERE id = ?",
        [authority, prospect["id"]],
    )

    if parent_scenario_id:
        create_edge(
            conn,
            from_node=parent_scenario_id,
            to_node=prospect["id"],
            edge_type="PROPOSES_ACTION",
            layer="L3",
            created_by=created_by,
        )

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [prospect["id"]]))[0]


def transition_prospect(
    conn: DuckDBPyConnection,
    *,
    prospect_id: str,
    new_status: str,
    agent: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Transition a prospect to a new lifecycle status (OHM-844).

    Validates the transition is legal, checks authority matches the calling
    agent (plain field equality — no existing auth mechanism to reuse), and
    creates an assessment observation logging the transition.

    Args:
        conn: Database connection.
        prospect_id: The prospect node to transition.
        new_status: Target lifecycle status.
        agent: Agent requesting the transition.
        reason: Optional explanation for the transition.

    Returns:
        Updated prospect node record.

    Raises:
        ValueError: If transition is invalid, prospect not found, or authority mismatch.
    """
    from ohm.validation import validate_identifier
    from ohm.framework.validation import validate_task_status

    prospect_id = validate_identifier(prospect_id, name="prospect_id")

    rows = _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [prospect_id],
    ))
    if not rows:
        raise ValueError(f"Prospect {prospect_id!r} not found")
    prospect = rows[0]

    if prospect.get("type") != "prospect":
        raise ValueError(f"Node {prospect_id!r} is type {prospect.get('type')!r}, not 'prospect'")

    current_status = prospect.get("task_status") or "proposed"

    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition from {current_status!r} to {new_status!r}. "
            f"Allowed: {sorted(allowed) or '(none — terminal state)'}"
        )

    authority = prospect.get("assigned_to")
    if authority and authority != agent:
        raise PermissionError(
            f"Agent {agent!r} is not authorized. Prospect authority is {authority!r}."
        )

    conn.execute(
        "UPDATE ohm_nodes SET task_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [new_status, prospect_id],
    )

    from ohm.graph.queries import create_observation

    create_observation(
        conn,
        node_id=prospect_id,
        obs_type="assessment",
        created_by=agent,
        value=1.0,
        notes=f"Transitioned {current_status} → {new_status}" + (f": {reason}" if reason else ""),
        metadata={"from_status": current_status, "to_status": new_status, "reason": reason},
    )

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [prospect_id]))[0]


def list_prospects(
    conn: DuckDBPyConnection,
    *,
    status: str | None = None,
    tags: list[str] | None = None,
    created_by: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List prospects with optional status, tag, and creator filters (OHM-844).

    Expectation counts are computed via a single aggregate LEFT JOIN, not N+1.

    Args:
        conn: Database connection.
        status: Filter by task_status (e.g. 'proposed', 'active').
        tags: AND-semantics tag filter (all must be present).
        created_by: Filter by creating agent.
        limit: Maximum results.

    Returns:
        List of prospect records with expectation_count.
    """
    conditions = ["n.type = 'prospect'", "n.deleted_at IS NULL"]
    params: list[Any] = []

    if status:
        conditions.append("n.task_status = ?")
        params.append(status)
    if created_by:
        conditions.append("n.created_by = ?")
        params.append(created_by)
    for tag in (tags or []):
        conditions.append("json_contains(n.tags, ?)")
        params.append(f'"{tag}"')

    where = " AND ".join(conditions)

    sql = f"""
        SELECT n.*, COALESCE(ec.expectation_count, 0) AS expectation_count
        FROM ohm_nodes n
        LEFT JOIN (
            SELECT e.from_node, COUNT(child.id) AS expectation_count
            FROM ohm_edges e
            JOIN ohm_nodes child ON child.id = e.to_node AND child.type = 'expectation' AND child.deleted_at IS NULL
            WHERE e.edge_type = 'CONTAINS' AND e.deleted_at IS NULL
            GROUP BY e.from_node
        ) ec ON ec.from_node = n.id
        WHERE {where}
        ORDER BY n.created_at DESC
        LIMIT ?
    """
    params.append(limit)
    return _rows_to_dicts(conn.execute(sql, params))


def prospect_detail(
    conn: DuckDBPyConnection,
    *,
    prospect_id: str,
) -> dict[str, Any]:
    """Get a prospect with its CONTAINS children and latest experiment_result (OHM-844).

    Args:
        conn: Database connection.
        prospect_id: The prospect node ID.

    Returns:
        Dict with prospect, children (CONTAINS edges), and latest_observation.
    """
    from ohm.validation import validate_identifier

    prospect_id = validate_identifier(prospect_id, name="prospect_id")

    rows = _rows_to_dicts(conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [prospect_id],
    ))
    if not rows:
        raise ValueError(f"Prospect {prospect_id!r} not found")
    prospect = rows[0]

    children = _rows_to_dicts(conn.execute("""
        SELECT child.*, e.edge_type AS link_type
        FROM ohm_edges e
        JOIN ohm_nodes child ON child.id = e.to_node AND child.deleted_at IS NULL
        WHERE e.from_node = ? AND e.edge_type = 'CONTAINS' AND e.deleted_at IS NULL
        ORDER BY child.created_at
    """, [prospect_id]))

    observations = _rows_to_dicts(conn.execute("""
        SELECT * FROM ohm_observations
        WHERE node_id = ? AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
    """, [prospect_id]))

    return {
        "prospect": prospect,
        "children": children,
        "latest_observation": observations[0] if observations else None,
    }
