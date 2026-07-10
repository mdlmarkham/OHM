"""loop_status queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from ohm.graph.queries import apply_decay_to_edges, compute_confidence_with_decay


def query_loop_status(
    conn: DuckDBPyConnection,
    *,
    agent_name: str | None = None,
    half_life_days: float = 30.0,
) -> dict[str, Any]:
    """Return the status of the autonomy loop — proposed/executed actions (OHM-446a).

    Summarizes the action lifecycle: how many actions are proposed,
    executing, executed, with what outcomes. Optionally filtered by agent.

    Extended with temporal section (OHM-2x2u): upcoming evaluations,
    stale feeds, compromised/stuck gates, and decay summary.

    Args:
        conn: Database connection.
        agent_name: Optional filter — only actions proposed by this agent.
        half_life_days: Half-life for confidence decay computation (default 30).

    Returns:
        Dict with:
          - proposed: list of action nodes with status 'proposed'
          - executed: list of action nodes with status 'executed'
          - summary: counts by status and outcome
          - recent_scenarios: scenario nodes linked to recent actions
          - temporal: dict with upcoming_evaluations, stale_feeds,
            compromised_gates, stuck_gates, decay_summary
    """
    conditions = ["type = 'action'", "deleted_at IS NULL"]
    params: list[Any] = []
    if agent_name:
        conditions.append("created_by = ?")
        params.append(agent_name)

    action_rows = conn.execute(
        f"""SELECT id, label, task_status, outcome, outcome_notes,
                   created_by, created_at, updated_at
           FROM ohm_nodes
           WHERE {" AND ".join(conditions)}
           ORDER BY COALESCE(updated_at, created_at) DESC
           LIMIT 100""",
        params,
    ).fetchall()

    proposed = []
    executed = []
    outcomes: dict[str, int] = {}

    for row in action_rows:
        action = {
            "id": row[0],
            "label": row[1],
            "status": row[2],
            "outcome": row[3],
            "outcome_notes": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "updated_at": str(row[7]) if row[7] else None,
        }
        if row[2] == "executed":
            executed.append(action)
            outcome_key = row[3] or "unrecorded"
            outcomes[outcome_key] = outcomes.get(outcome_key, 0) + 1
        else:
            proposed.append(action)

    # Fetch recent scenarios linked to actions
    scenario_rows = conn.execute(
        """SELECT DISTINCT s.id, s.label, s.created_by, s.created_at
           FROM ohm_nodes s
           JOIN ohm_edges e ON e.from_node = s.id AND e.edge_type = 'PROPOSES_ACTION'
           WHERE s.type = 'scenario' AND s.deleted_at IS NULL AND e.deleted_at IS NULL
           ORDER BY s.created_at DESC
           LIMIT 20""",
    ).fetchall()

    recent_scenarios = [
        {
            "id": row[0],
            "label": row[1],
            "created_by": row[2],
            "created_at": str(row[3]) if row[3] else None,
        }
        for row in scenario_rows
    ]

    # ── Temporal section (OHM-2x2u) ──

    # Upcoming evaluations: decision nodes with freshness thresholds
    upcoming_eval_rows = conn.execute(
        """SELECT d.id AS decision_id, d.label,
                  ft.metadata AS ft_metadata,
                  d.updated_at AS next_evaluation_due
           FROM ohm_nodes d
           JOIN ohm_edges e ON e.to_node = d.id AND e.edge_type = 'GOVERNS_FRESHNESS' AND e.deleted_at IS NULL
           JOIN ohm_nodes ft ON ft.id = e.from_node AND ft.deleted_at IS NULL
           WHERE d.type = 'decision' AND d.deleted_at IS NULL
           ORDER BY d.updated_at ASC
           LIMIT 50""",
    ).fetchall()

    upcoming_evaluations = []
    for row in upcoming_eval_rows:
        entry = {
            "decision_id": row[0],
            "label": row[1] or "",
            "next_evaluation_due": str(row[3]) if row[3] else None,
            "freshness_pressure": None,
        }
        if row[2]:
            import json as _json

            try:
                meta = _json.loads(row[2]) if isinstance(row[2], str) else row[2]
                entry["freshness_pressure"] = meta.get("max_age_seconds")
            except Exception:
                pass
        upcoming_evaluations.append(entry)

    # Stale feeds: nodes feeding decisions, ranked by decayed confidence
    # ascending (most-decayed-first = highest refresh priority). Each feed's
    # latest observation is decayed by its age so a feed that has gone silent
    # rises to the top of the actionable list.
    #
    # Subquery pattern: pick the latest non-deleted observation per feed node
    # using a window-function-free approach (DuckDB supports it, but a
    # correlated subquery is simpler and clearer here).
    stale_feed_rows = conn.execute(
        """SELECT n.id AS feed_node_id, n.label,
                  latest_o.value AS latest_value,
                  latest_o.created_at AS latest_observed_at,
                  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - COALESCE(latest_o.created_at, n.created_at)))::DOUBLE AS age_seconds
           FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL
           LEFT JOIN LATERAL (
               SELECT o.value, o.created_at
               FROM ohm_observations o
               WHERE o.node_id = n.id AND o.deleted_at IS NULL
               ORDER BY o.created_at DESC
               LIMIT 1
           ) latest_o ON true
           WHERE n.deleted_at IS NULL""",
    ).fetchall()

    # Build the list with decayed confidence; sort by it ascending.
    stale_feeds_intermediate = []
    for row in stale_feed_rows:
        feed_node_id = row[0]
        latest_value = row[2]
        latest_observed_at = row[3]
        age_seconds = row[4] if row[4] is not None else 0.0

        # Decay the latest observation's value by its age. When there is no
        # observation, treat as fully decayed (0.0) so the feed ranks high.
        if latest_value is None or latest_observed_at is None:
            decayed_confidence: float = 0.0
            decay_info = None
        else:
            decay_info = compute_confidence_with_decay(
                conn,
                base_confidence=float(latest_value),
                last_observed_at=latest_observed_at,
                half_life_days=half_life_days,
                floor=0.0,  # no floor — feeds can decay to 0
            )
            decayed_confidence = decay_info["decayed_confidence"]

        feeding_decision_rows = conn.execute(
            """SELECT DISTINCT e.to_node
               FROM ohm_edges e
               WHERE e.from_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL
               LIMIT 20""",
            [feed_node_id],
        ).fetchall()
        feeding_ids = [r[0] for r in feeding_decision_rows]
        stale_feeds_intermediate.append(
            {
                "feed_node_id": feed_node_id,
                "label": row[1] or "",
                "latest_value": float(latest_value) if latest_value is not None else None,
                "decayed_confidence": round(decayed_confidence, 6),
                "age_seconds": round(age_seconds, 2),
                "feeding_decision_ids": feeding_ids,
                "decay": decay_info,
            }
        )

    # Sort by decayed_confidence ASCENDING — most-decayed first.
    stale_feeds = sorted(
        stale_feeds_intermediate,
        key=lambda f: f["decayed_confidence"],
    )[:50]

    # Compromised gates: nodes with gate_status='compromised'
    compromised_rows = conn.execute(
        """SELECT id, label, gate_type, gate_status
           FROM ohm_nodes
           WHERE gate_status = 'compromised' AND deleted_at IS NULL
           ORDER BY updated_at DESC
           LIMIT 50""",
    ).fetchall()

    compromised_gates = [
        {
            "node_id": row[0],
            "label": row[1] or "",
            "gate_status": "compromised",
            "gate_type": row[2],
        }
        for row in compromised_rows
    ]

    # Stuck gates: nodes with gate_status='failed' AND old updated_at
    stuck_rows = conn.execute(
        """SELECT id, label, gate_type, updated_at
           FROM ohm_nodes
           WHERE gate_status = 'failed'
             AND deleted_at IS NULL
             AND updated_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
           ORDER BY updated_at ASC
           LIMIT 50""",
    ).fetchall()

    stuck_gates = [
        {
            "node_id": row[0],
            "label": row[1] or "",
            "gate_status": "failed",
            "gate_type": row[2],
            "since": str(row[3]) if row[3] else None,
        }
        for row in stuck_rows
    ]

    # Decay summary: call apply_decay_to_edges(dry_run=True)
    decay_result = apply_decay_to_edges(
        conn,
        half_life_days=half_life_days,
        floor=0.1,
        dry_run=True,
    )

    temporal = {
        "upcoming_evaluations": upcoming_evaluations,
        "stale_feeds": stale_feeds,
        "compromised_gates": compromised_gates,
        "stuck_gates": stuck_gates,
        "decay_summary": {
            "edges_examined": decay_result["edges_examined"],
            "edges_decayed": decay_result["edges_decayed"],
            "average_decay_factor": decay_result["average_decay_factor"],
        },
    }

    return {
        "proposed": proposed,
        "executed": executed,
        "recent_scenarios": recent_scenarios,
        "summary": {
            "total": len(action_rows),
            "proposed": len(proposed),
            "executed": len(executed),
            "outcomes": outcomes,
        },
        "temporal": temporal,
    }
