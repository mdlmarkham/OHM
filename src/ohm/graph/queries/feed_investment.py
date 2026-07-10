"""feed_investment.py — extracted from queries/__init__.py (OHM-447 Phase 2).

Part of the twins/ML cluster decomposition. Re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile

def compute_feed_investment(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    created_by: str,
    observation_cost: float = 0.5,
    label: str | None = None,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, _label, _type, utility_scale, decision_conf = decision
    utility_scale = utility_scale if utility_scale is not None else 0.5
    decision_conf = decision_conf if decision_conf is not None else 0.5

    supporting_edges = conn.execute(
        """
        SELECT e.id, e.confidence
        FROM ohm_edges e
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchall()

    current_confidence = decision_conf
    if supporting_edges:
        edge_confs = [row[1] for row in supporting_edges if row[1] is not None]
        if edge_confs:
            current_confidence = sum(edge_confs) / len(edge_confs)

    expected_confidence_after = min(current_confidence + 0.15, 1.0)
    voi = round(utility_scale * (current_confidence - expected_confidence_after), 4)
    voi = abs(voi)

    invest = voi > observation_cost

    metadata_dict = {
        "voi": voi,
        "observation_cost": observation_cost,
        "current_confidence": round(current_confidence, 4),
        "expected_confidence_after": round(expected_confidence_after, 4),
        "utility_scale": round(utility_scale, 4),
        "recommendation": "invest" if invest else "defer",
    }

    fi_label = label or f"Feed investment analysis for {decision_id}"

    fi_node = create_node(
        conn,
        label=fi_label,
        node_type="feed_investment",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=fi_node["id"],
        to_node=decision_id,
        edge_type="INVESTS_IN",
        layer="L3",
        created_by=created_by,
    )

    return {
        "id": fi_node["id"],
        "decision_id": decision_id,
        "voi": voi,
        "observation_cost": observation_cost,
        "current_confidence": round(current_confidence, 4),
        "expected_confidence_after": round(expected_confidence_after, 4),
        "utility_scale": round(utility_scale, 4),
        "recommendation": "invest" if invest else "defer",
    }


def recommend_mode(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, _label, _type, utility_scale, decision_conf = decision
    utility_scale = utility_scale if utility_scale is not None else 0.5

    freshness = get_freshness_status(conn, decision_id=decision_id)
    freshness_pressure = freshness.get("freshness_pressure") or 0.0

    urgency_edges = conn.execute(
        """
        SELECT e.urgency
        FROM ohm_edges e
        WHERE e.from_node = ?
          AND e.deleted_at IS NULL
          AND e.urgency IS NOT NULL
        """,
        [decision_id],
    ).fetchall()

    max_urgency = 0.0
    for row in urgency_edges:
        try:
            u = float(row[0]) if row[0] else 0.0
            max_urgency = max(max_urgency, u)
        except (ValueError, TypeError):
            pass

    if max_urgency > 0.7 and freshness_pressure < 0.3:
        mode = "real_time"
    elif freshness_pressure > 0.5 or utility_scale > 0.8:
        mode = "deliberative"
    else:
        mode = "hybrid"

    return {
        "decision_id": decision_id,
        "mode": mode,
        "urgency": round(max_urgency, 4),
        "freshness_pressure": freshness_pressure,
        "utility_scale": round(utility_scale, 4),
        "reasoning": {
            "real_time": "urgency>0.7 AND freshness_pressure<0.3",
            "deliberative": "freshness_pressure>0.5 OR utility_scale>0.8",
            "hybrid": "default when neither real_time nor deliberative conditions met",
        }[mode],
    }


def record_mode_switch(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    from_mode: str,
    to_mode: str,
    created_by: str,
    reason: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    decision_id = validate_identifier(decision_id, name="decision_id")

    valid_modes = {"real_time", "deliberative", "hybrid"}
    if from_mode not in valid_modes:
        raise ValidationError(f"Invalid from_mode: {from_mode}. Must be one of {sorted(valid_modes)}")
    if to_mode not in valid_modes:
        raise ValidationError(f"Invalid to_mode: {to_mode}. Must be one of {sorted(valid_modes)}")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    ms_label = label or f"Mode switch {from_mode}→{to_mode} for {decision_id}"
    metadata_dict = {
        "from_mode": from_mode,
        "to_mode": to_mode,
        "reason": reason,
    }

    ms_node = create_node(
        conn,
        label=ms_label,
        node_type="mode_switch",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=ms_node["id"],
        to_node=decision_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
    )

    return ms_node


def get_current_mode(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any] | None:
    """Return the most recent mode for a decision, if any.

    Used by POST /temporal/mode-switch to make ``from_mode`` optional —
    the handler derives it from the most recent prior mode_switch
    node rather than requiring the caller to round-trip GET first.

    Returns a dict with ``to_mode``, ``from_mode``, ``switch_id``,
    ``switched_at`` and ``created_by`` for the most recent switch,
    or ``None`` if the decision has no prior mode_switch.

    The "current mode" is the to_mode of the most recent
    TRANSITIONS_TO edge from a mode_switch node to this decision
    (record_mode_switch stores from_mode + to_mode in the
    mode_switch node's metadata JSON).
    """
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    row = conn.execute(
        """
        SELECT ms.id, ms.metadata, ms.created_at, ms.created_by
        FROM ohm_nodes ms
        JOIN ohm_edges e
          ON e.from_node = ms.id
         AND e.to_node = ?
         AND e.edge_type = 'TRANSITIONS_TO'
         AND e.deleted_at IS NULL
        WHERE ms.type = 'mode_switch'
          AND ms.deleted_at IS NULL
        ORDER BY ms.created_at DESC
        LIMIT 1
        """,
        [decision_id],
    ).fetchone()

    if not row:
        return None

    ms_id, metadata_raw, switched_at, created_by = row
    metadata = _json.loads(metadata_raw) if metadata_raw else {}
    return {
        "switch_id": ms_id,
        "from_mode": metadata.get("from_mode"),
        "to_mode": metadata.get("to_mode"),
        "switched_at": str(switched_at) if switched_at else None,
        "created_by": created_by,
    }


def temporal_decision_summary(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence, current_best_action FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, label, _type, utility_scale, decision_conf, current_best_action = decision

    freshness = get_freshness_status(conn, decision_id=decision_id)
    mode = recommend_mode(conn, decision_id=decision_id)

    feed_investments = conn.execute(
        """
        SELECT n.id, n.label, n.metadata
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'INVESTS_IN'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        ORDER BY n.created_at DESC
        """,
        [decision_id],
    ).fetchall()

    fi_records = []
    for row in feed_investments:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        fi_records.append(
            {
                "id": row[0],
                "label": row[1],
                "voi": meta.get("voi"),
                "recommendation": meta.get("recommendation"),
            }
        )

    mode_switches = conn.execute(
        """
        SELECT n.id, n.label, n.metadata, n.created_at
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'TRANSITIONS_TO'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        ORDER BY n.created_at DESC
        """,
        [decision_id],
    ).fetchall()

    ms_records = []
    for row in mode_switches:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        ms_records.append(
            {
                "id": row[0],
                "label": row[1],
                "from_mode": meta.get("from_mode"),
                "to_mode": meta.get("to_mode"),
                "reason": meta.get("reason"),
                "created_at": str(row[3]) if row[3] else None,
            }
        )

    return {
        "decision_id": decision_id,
        "label": label,
        "utility_scale": utility_scale,
        "confidence": decision_conf,
        "current_best_action": current_best_action,
        "freshness": freshness,
        "mode": mode,
        "feed_investments": fi_records,
        "mode_switches": ms_records,
    }


# ── Twin Design Session State Machine (OHM-konq) ────────────────────────────

VALID_SESSION_STATES = frozenset(
    {
        "init",
        "discover",
        "observe",
        "propose",
        "approve",
        "instantiate",
        "calibrate",
        "operate",
        "evolve",
        "completed",
        "abandoned",
    }
)

SESSION_TRANSITIONS: dict[str, set[str]] = {
    "init": {"discover", "abandoned"},
    "discover": {"observe", "propose", "abandoned"},
    "observe": {"propose", "discover", "abandoned"},
    "propose": {"approve", "observe", "abandoned"},
    "approve": {"instantiate", "propose", "abandoned"},
    "instantiate": {"calibrate", "operate"},
    "calibrate": {"operate", "evolve"},
    "operate": {"evolve"},
    "evolve": {"propose", "abandoned"},
    "completed": set(),
    "abandoned": set(),
}


