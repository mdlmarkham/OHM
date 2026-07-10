"""plans_events queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts

# ── TOPO Temporal Domain Tables (OHM-dh9l.1) ────────────────────────────────


def create_plan(
    conn: DuckDBPyConnection,
    *,
    plan_id: str,
    node_id: str | None = None,
    plan_type: str,
    label: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    horizon: str | None = None,
    status: str = "active",
    created_by: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Insert a new plan into topo_plans and return the row as a dict."""
    import json

    from ohm.validation import validate_identifier

    plan_id = validate_identifier(plan_id, name="plan_id")
    if node_id is not None:
        node_id = validate_identifier(node_id, name="node_id")
    if not plan_type:
        raise ValueError("plan_type must be non-empty")

    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO topo_plans
           (id, node_id, plan_type, label, start_ts, end_ts, horizon, status, created_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [plan_id, node_id, plan_type, label, start_ts, end_ts, horizon, status, created_by, metadata_json],
    )
    _log_change(conn, "topo_plans", plan_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM topo_plans WHERE id = ?", [plan_id]))[0]


def get_plan(
    conn: DuckDBPyConnection,
    plan_id: str,
) -> dict[str, Any] | None:
    """Fetch a single plan by id. Returns dict or None."""
    from ohm.validation import validate_identifier

    plan_id = validate_identifier(plan_id, name="plan_id")
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_plans WHERE id = ?", [plan_id]))
    return rows[0] if rows else None


def list_plans(
    conn: DuckDBPyConnection,
    *,
    node_id: str | None = None,
    plan_type: str | None = None,
    status: str | None = None,
    horizon: str | None = None,
) -> list[dict[str, Any]]:
    """List plans with optional filters."""
    query = "SELECT * FROM topo_plans WHERE 1=1"
    params: list[Any] = []
    if node_id is not None:
        query += " AND node_id = ?"
        params.append(node_id)
    if plan_type is not None:
        query += " AND plan_type = ?"
        params.append(plan_type)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if horizon is not None:
        query += " AND horizon = ?"
        params.append(horizon)
    query += " ORDER BY start_ts NULLS LAST, created_at DESC"
    return _rows_to_dicts(conn.execute(query, params))


def create_event(
    conn: DuckDBPyConnection,
    *,
    event_id: str,
    plan_id: str | None = None,
    node_id: str,
    node_path: str | None = None,
    event_class: str,
    title: str | None = None,
    start_ts: str,
    end_ts: str | None = None,
    horizon: str | None = None,
    operating_state: str | None = None,
    description: str | None = None,
    confidence: float | None = None,
    authority: str | None = None,
    created_by: str,
    metadata: dict | None = None,
    **extra_kw: Any,
) -> dict[str, Any]:
    """Insert a new event. extra_kw captures optional JSON/advanced fields."""
    import json

    from ohm.validation import validate_identifier, validate_confidence

    event_id = validate_identifier(event_id, name="event_id")
    if plan_id is not None:
        plan_id = validate_identifier(plan_id, name="plan_id")
    node_id = validate_identifier(node_id, name="node_id")
    if not event_class:
        raise ValueError("event_class must be non-empty")
    if not start_ts:
        raise ValueError("start_ts must be non-empty")
    if confidence is not None:
        confidence = validate_confidence(confidence)

    metadata_json = json.dumps(metadata) if metadata else None

    json_columns = ("source_refs", "l3_context", "flow_impact", "forecast_basis", "decision_metadata")
    json_values: dict[str, str | None] = {}
    for col in json_columns:
        val = extra_kw.pop(col, None)
        json_values[col] = json.dumps(val) if val is not None else None

    extra_columns = tuple(extra_kw.keys())
    extra_values = tuple(extra_kw.values())

    columns = (
        (
            "id",
            "plan_id",
            "node_id",
            "node_path",
            "event_class",
            "title",
            "start_ts",
            "end_ts",
            "horizon",
            "operating_state",
            "description",
            "confidence",
            "authority",
            "created_by",
            "metadata",
        )
        + json_columns
        + extra_columns
    )

    placeholders = ",".join(["?"] * len(columns))
    values: list[Any] = [
        event_id,
        plan_id,
        node_id,
        node_path,
        event_class,
        title,
        start_ts,
        end_ts,
        horizon,
        operating_state,
        description,
        confidence,
        authority,
        created_by,
        metadata_json,
    ]
    for col in json_columns:
        values.append(json_values[col])
    values.extend(extra_values)

    conn.execute(
        f"INSERT INTO topo_events ({','.join(columns)}) VALUES ({placeholders})",
        values,
    )
    _log_change(conn, "topo_events", event_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM topo_events WHERE id = ?", [event_id]))[0]


def get_event(
    conn: DuckDBPyConnection,
    event_id: str,
) -> dict[str, Any] | None:
    """Fetch a single event by id. Returns dict or None."""
    from ohm.validation import validate_identifier

    event_id = validate_identifier(event_id, name="event_id")
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_events WHERE id = ?", [event_id]))
    return rows[0] if rows else None


def get_events_for_node(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    horizon: str | None = None,
    plan_id: str | None = None,
    event_class: str | None = None,
    start_after: str | None = None,
    end_before: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch events for a node with optional filters."""
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    query = "SELECT * FROM topo_events WHERE node_id = ?"
    params: list[Any] = [node_id]
    if horizon is not None:
        query += " AND horizon = ?"
        params.append(horizon)
    if plan_id is not None:
        query += " AND plan_id = ?"
        params.append(plan_id)
    if event_class is not None:
        query += " AND event_class = ?"
        params.append(event_class)
    if start_after is not None:
        query += " AND start_ts >= ?"
        params.append(start_after)
    if end_before is not None:
        query += " AND end_ts <= ?"
        params.append(end_before)
    query += " ORDER BY start_ts ASC LIMIT ?"
    params.append(limit)
    return _rows_to_dicts(conn.execute(query, params))


def get_events_for_plan(
    conn: DuckDBPyConnection,
    plan_id: str,
) -> list[dict[str, Any]]:
    """Fetch all events for a plan, ordered by start_ts."""
    from ohm.validation import validate_identifier

    plan_id = validate_identifier(plan_id, name="plan_id")
    return _rows_to_dicts(
        conn.execute(
            "SELECT * FROM topo_events WHERE plan_id = ? ORDER BY start_ts ASC",
            [plan_id],
        )
    )


def create_event_link(
    conn: DuckDBPyConnection,
    *,
    link_id: str,
    from_event_id: str,
    to_event_id: str,
    edge_type: str,
    layer: str = "L1",
    confidence: float = 1.0,
    created_by: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Link two events."""
    import json

    from ohm.validation import validate_identifier, validate_confidence

    link_id = validate_identifier(link_id, name="link_id")
    from_event_id = validate_identifier(from_event_id, name="from_event_id")
    to_event_id = validate_identifier(to_event_id, name="to_event_id")
    if not edge_type:
        raise ValueError("edge_type must be non-empty")
    confidence = validate_confidence(confidence)

    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO topo_event_links
           (id, from_event_id, to_event_id, edge_type, layer, confidence, created_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [link_id, from_event_id, to_event_id, edge_type, layer, confidence, created_by, metadata_json],
    )
    _log_change(conn, "topo_event_links", link_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM topo_event_links WHERE id = ?", [link_id]))[0]


def get_event_links(
    conn: DuckDBPyConnection,
    *,
    event_id: str | None = None,
    from_event_id: str | None = None,
    to_event_id: str | None = None,
    edge_type: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch event links with optional filters."""
    query = "SELECT * FROM topo_event_links WHERE 1=1"
    params: list[Any] = []
    if event_id is not None:
        query += " AND (from_event_id = ? OR to_event_id = ?)"
        params.append(event_id)
        params.append(event_id)
    if from_event_id is not None:
        query += " AND from_event_id = ?"
        params.append(from_event_id)
    if to_event_id is not None:
        query += " AND to_event_id = ?"
        params.append(to_event_id)
    if edge_type is not None:
        query += " AND edge_type = ?"
        params.append(edge_type)
    query += " ORDER BY created_at ASC"
    return _rows_to_dicts(conn.execute(query, params))


def timeline_rollup(
    conn: DuckDBPyConnection,
    ancestor_node_id: str,
    *,
    horizon: str | None = None,
    start_after: str | None = None,
    end_before: str | None = None,
    event_class: str | None = None,
    plan_id: str | None = None,
    include_plans: bool = True,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Roll up TOPO temporal events from a subtree rooted at *ancestor_node_id*.

    Traverses L1 ``CONTAINS`` edges downward from the ancestor to collect all
    descendant node ids, then joins those nodes against ``topo_events`` with
    optional horizon / date-range / event-class / plan filters.  When
    *include_plans* is true, the matching plans are returned alongside the
    events so callers can render a complete timeline (plan headers + child
    events).

    Args:
        conn: Active DuckDB connection.
        ancestor_node_id: Root of the L1 CONTAINS subtree to roll up.
        horizon: Optional horizon filter (HISTORICAL/CURRENT/PLANNED/FORECAST).
        start_after: Optional ISO timestamp; only events with start_ts >= this.
        end_before: Optional ISO timestamp; only events with end_ts <= this.
        event_class: Optional event_class filter (e.g. 'shutdown', 'outage').
        plan_id: Optional plan_id filter; restricts events to one plan.
        include_plans: If True (default), include matching topo_plans rows.
        max_depth: Maximum L1 traversal depth (default 10).

    Returns:
        Dict with ``ancestor``, ``events`` (list ordered by start_ts), and
        (when include_plans) ``plans`` (list of matching plan dicts).
    """
    from ohm.validation import validate_identifier, validate_depth

    ancestor_node_id = validate_identifier(ancestor_node_id, name="ancestor_node_id")
    max_depth = validate_depth(max_depth)

    descendant_params: list[Any] = [ancestor_node_id, max_depth]
    descendant_query = """
        WITH RECURSIVE descendants AS (
            SELECT ? AS node, 0 AS hop
            UNION
            SELECT DISTINCT e.to_node, d.hop + 1
            FROM descendants d
            JOIN ohm_edges e ON e.from_node = d.node
            WHERE d.hop < ?
              AND e.edge_type = 'CONTAINS'
              AND e.layer = 'L1'
              AND e.deleted_at IS NULL
        )
        SELECT node FROM descendants
    """
    descendant_rows = _rows_to_dicts(conn.execute(descendant_query, descendant_params))
    descendant_ids = [r["node"] for r in descendant_rows]

    if not descendant_ids:
        return {"ancestor": ancestor_node_id, "events": [], "plans": []}

    event_query = "SELECT e.*, n.label AS node_label FROM topo_events e LEFT JOIN ohm_nodes n ON n.id = e.node_id WHERE e.node_id IN (" + ",".join(["?"] * len(descendant_ids)) + ")"
    event_params: list[Any] = list(descendant_ids)
    if horizon is not None:
        event_query += " AND e.horizon = ?"
        event_params.append(horizon)
    if event_class is not None:
        event_query += " AND e.event_class = ?"
        event_params.append(event_class)
    if plan_id is not None:
        event_query += " AND e.plan_id = ?"
        event_params.append(plan_id)
    if start_after is not None:
        event_query += " AND e.start_ts >= ?"
        event_params.append(start_after)
    if end_before is not None:
        event_query += " AND e.end_ts <= ?"
        event_params.append(end_before)
    event_query += " ORDER BY e.start_ts ASC"
    events = _rows_to_dicts(conn.execute(event_query, event_params))

    result: dict[str, Any] = {"ancestor": ancestor_node_id, "events": events}

    if include_plans:
        plan_ids = {e["plan_id"] for e in events if e.get("plan_id")}
        if plan_id is not None:
            plan_ids = {plan_id}
        plans: list[dict[str, Any]] = []
        if plan_ids:
            plan_query = "SELECT * FROM topo_plans WHERE id IN (" + ",".join(["?"] * len(plan_ids)) + ") ORDER BY start_ts NULLS LAST, created_at DESC"
            plans = _rows_to_dicts(conn.execute(plan_query, list(plan_ids)))
        result["plans"] = plans

    return result
