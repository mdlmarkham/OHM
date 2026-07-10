"""actions queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile


def propose_action(
    conn: DuckDBPyConnection,
    *,
    scenario_id: str,
    label: str,
    created_by: str,
    rationale: str | None = None,
    connects_to: list[str] | None = None,
) -> dict[str, Any]:
    """Propose an action linked to a scenario (OHM-446a).

    Creates an ``action`` node and links it to the scenario via a
    ``PROPOSES_ACTION`` L3 edge. The action starts in 'proposed' status
    (stored in task_status column for compatibility).

    Args:
        conn: Database connection.
        scenario_id: The scenario node that suggests this action.
        label: Human-readable action description.
        created_by: Agent proposing the action.
        rationale: Optional explanation of why this action.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The created action node record.
    """
    from ohm.validation import validate_identifier

    scenario_id = validate_identifier(scenario_id, name="scenario_id")

    # Create the action node, linked to the scenario
    all_connects = [scenario_id] + (connects_to or [])
    action = create_node(
        conn,
        label=label,
        node_type="action",
        content=rationale,
        created_by=created_by,
        connects_to=all_connects,
    )

    # Set task_status to 'proposed' (reusing the existing column)
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'proposed' WHERE id = ?",
        [action["id"]],
    )

    # Create the PROPOSES_ACTION edge: scenario → action
    create_edge(
        conn,
        from_node=scenario_id,
        to_node=action["id"],
        edge_type="PROPOSES_ACTION",
        layer="L3",
        created_by=created_by,
    )

    # Return updated record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action["id"]]))[0]


def execute_action(
    conn: DuckDBPyConnection,
    *,
    action_id: str,
    executed_by: str,
    outcome: str | None = None,
    outcome_notes: str | None = None,
) -> dict[str, Any]:
    """Mark an action as executed and record the outcome (OHM-446a).

    Sets the action's task_status to 'executed', records the outcome
    (TRUE/FALSE/AMBIGUOUS), and creates an EXECUTED_BY L4 edge from
    the action to the executing agent.

    Args:
        conn: Database connection.
        action_id: The action node to execute.
        executed_by: Agent executing the action.
        outcome: TRUE/FALSE/AMBIGUOUS/DEFERRED.
        outcome_notes: Free-text notes on the execution result.

    Returns:
        The updated action node record.
    """
    from ohm.validation import validate_identifier

    action_id = validate_identifier(action_id, name="action_id")

    # Verify the action exists
    row = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'action' AND deleted_at IS NULL",
        [action_id],
    ).fetchone()
    if not row:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Action not found: {action_id}")

    # Update the action status
    now_sql = "CURRENT_TIMESTAMP"
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'executed', outcome = ?, outcome_notes = ?, updated_at = " + now_sql + ", updated_by = ? WHERE id = ?",
        [outcome, outcome_notes, executed_by, action_id],
    )

    # Find or create the agent node for EXECUTED_BY edge
    agent_row = conn.execute(
        "SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent' AND deleted_at IS NULL LIMIT 1",
        [executed_by],
    ).fetchone()

    if agent_row:
        agent_id = agent_row[0]
    else:
        # Create a minimal agent node
        agent = create_node(
            conn,
            label=executed_by,
            node_type="agent",
            created_by=executed_by,
        )
        agent_id = agent["id"]

    # Create the EXECUTED_BY edge: action → agent
    create_edge(
        conn,
        from_node=action_id,
        to_node=agent_id,
        edge_type="EXECUTED_BY",
        layer="L4",
        created_by=executed_by,
    )

    _log_change(conn, "ohm_nodes", action_id, "EXECUTE", executed_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action_id]))[0]


def compute_confidence_with_decay(
    conn: DuckDBPyConnection,
    *,
    base_confidence: float,
    last_observed_at: datetime | str | None,
    half_life_days: float = 30.0,
    floor: float | None = 0.1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute decayed confidence based on observation age (OHM-2x2u).

    Decay model: confidence(t) = base * 2^(-age_days / half_life), optionally
    floored from below by ``floor`` (default 0.1). Pass ``floor=None`` to
    disable the floor — useful when the input is an unbounded quality score
    (e.g., model composite_score, which can be negative when error metrics
    dominate) rather than a confidence in [0, 1]. When the floor is
    disabled, ``is_stale`` is always False because there is no defined
    staleness threshold.

    Time source: by default we read ``now`` from DuckDB (CURRENT_TIMESTAMP)
    so both timestamps share the same clock + timezone. The fallback to
    ``datetime.now(timezone.utc)`` is only used when the caller passes
    ``now`` explicitly or the DB read fails.
    """
    import math
    from datetime import datetime as _dt, timezone as _tz

    if last_observed_at is None:
        return {
            "decayed_confidence": base_confidence,
            "age_days": None,
            "decay_factor": 1.0,
            "is_stale": False,
        }

    if now is None:
        # Read "now" from DuckDB so it shares the same timezone as
        # CURRENT_TIMESTAMP used by create_node's default values. Without
        # this, naive datetimes from the DB are interpreted as UTC while
        # CURRENT_TIMESTAMP carries the session TZ (e.g. EDT), causing
        # spurious ~4h "staleness" on fresh writes.
        try:
            now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        except Exception:
            now = _dt.now(_tz.utc)

    if isinstance(last_observed_at, str):
        last_observed_at = _dt.fromisoformat(last_observed_at.replace("Z", "+00:00"))
    # If the timestamp is naive, attach the same tzinfo as `now` so the
    # subtraction is TZ-correct. If `now` is also naive, treat both as UTC.
    if last_observed_at.tzinfo is None:
        ref_tz = now.tzinfo if now.tzinfo is not None else _tz.utc
        last_observed_at = last_observed_at.replace(tzinfo=ref_tz)
    if now.tzinfo is None and last_observed_at.tzinfo is not None:
        now = now.replace(tzinfo=last_observed_at.tzinfo)

    age_seconds = max(0.0, (now - last_observed_at).total_seconds())
    age_days = age_seconds / 86400.0

    if half_life_days <= 0:
        decay_factor = 1.0
    else:
        decay_factor = 2.0 ** (-age_days / half_life_days)

    raw = base_confidence * decay_factor
    if floor is not None:
        # Explicit clamp: when raw drops below floor, snap to floor exactly
        # (avoids floating-point underflow like 4.4e-16 being treated as
        # non-stale). is_stale is True iff raw was clamped (decayed == floor).
        if raw < floor:
            decayed = floor
            is_stale = True
        else:
            decayed = raw
            is_stale = False
    else:
        decayed = raw
        is_stale = False

    return {
        "decayed_confidence": round(decayed, 6),
        "age_days": round(age_days, 4),
        "decay_factor": round(decay_factor, 6),
        "is_stale": is_stale,
    }


def apply_decay_to_edges(
    conn: DuckDBPyConnection,
    *,
    half_life_days: float = 30.0,
    floor: float = 0.1,
    dry_run: bool = True,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Apply decay to all edges' effective confidence based on their last observation (OHM-2x2u).

    If dry_run=True (default), returns what would change without modifying.
    If dry_run=False, UPDATE ohm_edges SET confidence = decayed_value and store
    original confidence in metadata.confidence_original.
    """
    import json
    import math

    defaults = {"L1": float("inf"), "L2": float("inf"), "L3": 90.0, "L4": 30.0}
    when_clauses = " ".join(f"WHEN '{k}' THEN {999999.0 if v == float('inf') or v <= 0 else float(v)}" for k, v in defaults.items())
    hl_case = f"CASE layer {when_clauses} ELSE 90.0 END"

    rows = conn.execute(
        f"""
        SELECT
            id, confidence, layer, created_at, metadata,
            {hl_case} AS half_life,
            GREATEST(date_diff('day', created_at, CURRENT_TIMESTAMP), 0)::DOUBLE AS age_days
        FROM ohm_edges
        WHERE deleted_at IS NULL
          AND confidence IS NOT NULL
          AND layer IN ('L3', 'L4')
        """,
    ).fetchall()

    edges_examined = len(rows)
    edges_decayed = 0
    summary: list[dict[str, Any]] = []
    total_decay_factor = 0.0

    for row in rows:
        edge_id, original_conf, layer, created_at, metadata_json, hl, age = row
        hl = float(hl)
        age = float(age)
        original_conf = float(original_conf) if original_conf is not None else original_conf
        if original_conf is None or original_conf <= 0:
            continue

        if hl <= 0 or hl >= 999999:
            continue

        decay_factor = 2.0 ** (-age / hl)
        decayed_conf = max(floor, original_conf * decay_factor)

        if decayed_conf < original_conf:
            edges_decayed += 1
            total_decay_factor += decay_factor

            entry = {
                "id": edge_id,
                "layer": layer,
                "original_confidence": round(original_conf, 6),
                "decayed_confidence": round(decayed_conf, 6),
                "decay_factor": round(decay_factor, 6),
                "age_days": round(age, 4),
            }
            summary.append(entry)

            if not dry_run:
                existing_meta = {}
                if metadata_json:
                    try:
                        existing_meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing_meta["confidence_original"] = original_conf
                meta_str = json.dumps(existing_meta)

                conn.execute(
                    "UPDATE ohm_edges SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [meta_str, edge_id],
                )
                # OHM-733: append to confidence log instead of direct UPDATE
                log_confidence_change(
                    conn,
                    edge_id=edge_id,
                    agent=created_by or "decay",
                    old_value=original_conf,
                    new_value=round(decayed_conf, 6),
                    reason="decay",
                )
                agent = created_by or "decay"
                _log_change(conn, "ohm_edges", edge_id, "UPDATE", agent)

    avg_decay = round(total_decay_factor / edges_decayed, 6) if edges_decayed > 0 else 1.0

    return {
        "edges_examined": edges_examined,
        "edges_decayed": edges_decayed,
        "average_decay_factor": avg_decay,
        "summary": summary[:100],
    }

# OHM-447: Lazy cross-domain imports resolved at access time
_LAZY_IMPORTS = {
    "create_node",
    "create_edge",
}

def __getattr__(name):
    if name in _LAZY_IMPORTS:
        import ohm.graph.queries as _q
        return getattr(_q, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

