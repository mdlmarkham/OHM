"""design_sessions.py — extracted from queries/__init__.py (OHM-447 Phase 2).

Part of the twins/ML cluster decomposition. Re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile


def start_twin_design_session(
    conn: DuckDBPyConnection,
    *,
    goal: str,
    context: dict[str, Any] | None = None,
    created_by: str,
    label: str | None = None,
) -> dict[str, Any]:
    from ohm.exceptions import ValidationError
    from ohm.graph.schema import generate_node_id

    if not goal or not goal.strip():
        raise ValidationError("goal is required")

    session_label = label or f"Twin design: {goal[:80]}"
    generate_node_id(session_label, node_type="twin_design_session")

    metadata: dict[str, Any] = {
        "session_state": "init",
        "goal": goal,
        "observations": [],
        "calibration_records": [],
    }
    if context:
        metadata["context"] = context

    session = create_node(
        conn,
        label=session_label,
        node_type="twin_design_session",
        content=goal,
        created_by=created_by,
        metadata=metadata,
    )

    _log_change(conn, "ohm_nodes", session["id"], "START_SESSION", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session["id"]]))[0]


def transition_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    to_state: str,
    notes: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    if to_state not in VALID_SESSION_STATES:
        raise ValidationError(f"Invalid session state: '{to_state}' — must be one of: {sorted(VALID_SESSION_STATES)}")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")

    allowed = SESSION_TRANSITIONS.get(current_state, set())
    if to_state not in allowed:
        raise ValidationError(f"Invalid transition: '{current_state}' → '{to_state}'. Allowed from '{current_state}': {sorted(allowed) if allowed else ['(terminal)']}")

    current_meta["session_state"] = to_state

    transition_record = {
        "from_state": current_state,
        "to_state": to_state,
        "notes": notes,
        "actor": created_by,
    }
    history = current_meta.get("transition_history", [])
    history.append(transition_record)
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata=transition_record,
    )

    _log_change(conn, "ohm_nodes", session_id, f"TRANSITION_{current_state}_TO_{to_state}", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0]


def add_session_observation(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    observations: dict[str, Any],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "observe":
        raise ValidationError(f"Can only add observations in 'observe' state, current state is '{current_state}'")

    obs_list = current_meta.get("observations", [])
    obs_list.append(observations)
    current_meta["observations"] = obs_list

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    _log_change(conn, "ohm_nodes", session_id, "ADD_OBSERVATION", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0]


def propose_twin_config(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    decision_node_id: str | None = None,
    preferred_template_id: str | None = None,
    preferred_model_id: str | None = None,
    confidence_threshold: float = 0.6,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[2]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "propose":
        raise ValidationError(f"Can only propose in 'propose' state, current state is '{current_state}'")

    goal = current_meta.get("goal", "")

    twin_preview = None
    ranking = []
    reasoning = ""

    if decision_node_id:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")
        try:
            twin_preview = assemble_twin_for_decision(
                conn,
                decision_node_id=decision_node_id,
                goal=goal,
                preferred_template_id=preferred_template_id,
                preferred_model_id=preferred_model_id,
                created_by=created_by,
            )
            ranking = twin_preview.get("ranking", [])
            reasoning = twin_preview.get("reasoning", "")
        except (NodeNotFoundError, ValidationError):
            twin_preview = None
            ranking = []
            reasoning = "No suitable twin configuration found"

    if ranking and ranking[0].get("score", 0) < confidence_threshold:
        raise ValidationError(f"Best proposal score {ranking[0].get('score', 0):.2f} below confidence threshold {confidence_threshold}")

    from ohm.graph.schema import generate_node_id

    proposal_label = f"Proposal for: {goal[:60]}"
    generate_node_id(proposal_label, node_type="twin_design_proposal")

    proposal_metadata: dict[str, Any] = {
        "decision_node_id": decision_node_id,
        "preferred_template_id": preferred_template_id,
        "preferred_model_id": preferred_model_id,
        "confidence_threshold": confidence_threshold,
        "ranking": ranking,
        "reasoning": reasoning,
    }

    proposal = create_node(
        conn,
        label=proposal_label,
        node_type="twin_design_proposal",
        content=reasoning,
        created_by=created_by,
        metadata=proposal_metadata,
        connects_to=[session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=proposal["id"],
        edge_type="PROPOSES",
        layer="L3",
        created_by=created_by,
    )

    current_meta["session_state"] = "approve"
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "propose", "to_state": "approve", "notes": "auto-transition after proposal", "actor": created_by})
    current_meta["transition_history"] = history
    current_meta["current_proposal_id"] = proposal["id"]

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "propose", "to_state": "approve", "notes": "auto-transition after proposal", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "PROPOSE_TWIN_CONFIG", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "proposal": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [proposal["id"]]))[0],
        "twin_preview": twin_preview,
        "ranking": ranking,
        "reasoning": reasoning,
    }


def review_proposal(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    proposal_id: str,
    decision: str,
    approved_aspects: list[str] | None = None,
    declined_aspects: list[str] | None = None,
    modifications: dict[str, Any] | None = None,
    reason: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")
    proposal_id = validate_identifier(proposal_id, name="proposal_id")

    if decision not in ("approve", "decline", "modify"):
        raise ValidationError(f"Invalid review decision: '{decision}' — must be 'approve', 'decline', or 'modify'")

    session_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not session_row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    proposal_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_proposal' AND deleted_at IS NULL",
        [proposal_id],
    ).fetchone()
    if not proposal_row:
        raise NodeNotFoundError(f"Twin design proposal not found: {proposal_id}")

    meta_raw = session_row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "approve":
        raise ValidationError(f"Can only review proposals in 'approve' state, current state is '{current_state}'")

    edge_type_map = {"approve": "APPROVES", "decline": "DECLINES", "modify": "MODIFIES"}
    review_meta: dict[str, Any] = {
        "decision": decision,
        "approved_aspects": approved_aspects,
        "declined_aspects": declined_aspects,
        "modifications": modifications,
        "reason": reason,
    }

    create_edge(
        conn,
        from_node=session_id,
        to_node=proposal_id,
        edge_type=edge_type_map[decision],
        layer="L3",
        created_by=created_by,
        metadata=review_meta,
    )

    proposal_meta_raw = proposal_row[1]
    proposal_meta: dict[str, Any] = {}
    if proposal_meta_raw:
        try:
            proposal_meta = _json.loads(proposal_meta_raw) if isinstance(proposal_meta_raw, str) else proposal_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    reviews = proposal_meta.get("reviews", [])
    reviews.append(review_meta)
    proposal_meta["reviews"] = reviews
    if modifications:
        proposal_meta["modifications"] = modifications

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(proposal_meta), created_by, proposal_id],
    )

    new_state: str
    if decision == "approve":
        new_state = "instantiate"
    elif decision == "decline":
        new_state = "abandoned"
    else:
        new_state = "propose"

    current_meta["session_state"] = new_state
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "approve", "to_state": new_state, "notes": f"review decision: {decision}", "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "approve", "to_state": new_state, "notes": f"review decision: {decision}", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, f"REVIEW_{decision.upper()}", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "proposal": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [proposal_id]))[0],
        "decision": decision,
        "new_state": new_state,
    }


def instantiate_from_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "instantiate":
        raise ValidationError(f"Can only instantiate in 'instantiate' state, current state is '{current_state}'")

    proposal_id = current_meta.get("current_proposal_id")
    if not proposal_id:
        raise ValidationError("No current proposal to instantiate")

    proposal_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_proposal' AND deleted_at IS NULL",
        [proposal_id],
    ).fetchone()
    if not proposal_row:
        raise NodeNotFoundError(f"Current proposal not found: {proposal_id}")

    proposal_meta_raw = proposal_row[1]
    proposal_meta: dict[str, Any] = {}
    if proposal_meta_raw:
        try:
            proposal_meta = _json.loads(proposal_meta_raw) if isinstance(proposal_meta_raw, str) else proposal_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    decision_node_id = proposal_meta.get("decision_node_id")
    preferred_template_id = proposal_meta.get("preferred_template_id")
    preferred_model_id = proposal_meta.get("preferred_model_id")

    twin_result = None
    model_result = None

    if decision_node_id and preferred_template_id:
        try:
            twin_result = instantiate_twin_from_template(
                conn,
                template_id=preferred_template_id,
                target_node_id=decision_node_id,
                created_by=created_by,
            )
        except (NodeNotFoundError, ValidationError):
            twin_result = None

    if twin_result and preferred_model_id:
        try:
            model_result = register_model_candidate(
                conn,
                label=f"Model for session {session_id[:20]}",
                twin_id=twin_result["id"],
                created_by=created_by,
            )
        except (NodeNotFoundError, ValidationError):
            model_result = None

    if twin_result:
        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=session_id,
            edge_type="INSTANTIATED_FROM",
            layer="L3",
            created_by=created_by,
        )

    new_state = "calibrate"
    current_meta["session_state"] = new_state
    current_meta["instantiated_twin_id"] = twin_result["id"] if twin_result else None
    current_meta["instantiated_model_id"] = model_result["id"] if model_result else None
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "instantiate", "to_state": new_state, "notes": "twin instantiated", "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "instantiate", "to_state": new_state, "notes": "twin instantiated", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "INSTANTIATE_FROM_SESSION", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "twin": twin_result,
        "model_candidate": model_result,
        "calibration_plan": {"recommended_observations": 5, "drift_threshold": 0.15},
    }


def record_calibration(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    observations: dict[str, float],
    actuals: dict[str, float],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "calibrate":
        raise ValidationError(f"Can only record calibration in 'calibrate' state, current state is '{current_state}'")

    drift_metrics: dict[str, Any] = {}
    max_drift = 0.0
    for key in observations:
        if key in actuals:
            drift = abs(observations[key] - actuals[key])
            drift_metrics[key] = {"observed": observations[key], "actual": actuals[key], "drift": drift}
            max_drift = max(max_drift, drift)

    cal_records = current_meta.get("calibration_records", [])
    cal_record = {"observations": observations, "actuals": actuals, "drift_metrics": drift_metrics, "max_drift": max_drift}
    cal_records.append(cal_record)
    current_meta["calibration_records"] = cal_records

    DRIFT_THRESHOLD = 0.15
    MIN_CALIBRATIONS = 3

    recommended_next: str
    if max_drift > DRIFT_THRESHOLD:
        recommended_next = "evolve"
    elif len(cal_records) >= MIN_CALIBRATIONS:
        recommended_next = "operate"
    else:
        recommended_next = "calibrate"

    if recommended_next != "calibrate":
        current_meta["session_state"] = recommended_next
        history = current_meta.get("transition_history", [])
        history.append({"from_state": "calibrate", "to_state": recommended_next, "notes": f"max_drift={max_drift:.4f}", "actor": created_by})
        current_meta["transition_history"] = history

        create_edge(
            conn,
            from_node=session_id,
            to_node=session_id,
            edge_type="TRANSITIONS_TO",
            layer="L3",
            created_by=created_by,
            metadata={"from_state": "calibrate", "to_state": recommended_next, "notes": f"max_drift={max_drift:.4f}", "actor": created_by},
        )

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    _log_change(conn, "ohm_nodes", session_id, "RECORD_CALIBRATION", created_by)

    return {
        "calibration_metrics": drift_metrics,
        "max_drift": max_drift,
        "recommended_next_state": recommended_next,
        "calibration_count": len(cal_records),
    }


def evolve_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    reason: str,
    proposed_changes: dict[str, Any],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    if not reason or not reason.strip():
        raise ValidationError("reason is required")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state not in ("evolve", "operate"):
        raise ValidationError(f"Can only evolve in 'evolve' or 'operate' state, current state is '{current_state}'")

    evolution_record = {"reason": reason, "proposed_changes": proposed_changes, "actor": created_by}
    evolutions = current_meta.get("evolution_records", [])
    evolutions.append(evolution_record)
    current_meta["evolution_records"] = evolutions

    new_state = "propose"
    current_meta["session_state"] = new_state
    history = current_meta.get("transition_history", [])
    history.append({"from_state": current_state, "to_state": new_state, "notes": reason, "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": current_state, "to_state": new_state, "notes": reason, "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "EVOLVE_SESSION", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "evolution_record": evolution_record,
    }


def get_session_state(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    columns = [desc[0] for desc in conn.description]
    session = dict(zip(columns, row))

    meta_raw = session.get("metadata")
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    history = current_meta.get("transition_history", [])

    proposal_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'PROPOSES' AND e.deleted_at IS NULL
           ORDER BY e.created_at DESC""",
            [session_id],
        )
    )

    proposals = []
    for pe in proposal_edges:
        proposal_node = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [pe["to_node"]],
            )
        )
        if proposal_node:
            proposals.append(proposal_node[0])

    final_twin_id = current_meta.get("instantiated_twin_id")

    return {
        "session": session,
        "current_state": current_state,
        "history": history,
        "proposals": proposals,
        "final_twin_id": final_twin_id,
    }


def get_session_audit(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    columns = [desc[0] for desc in conn.description]
    session = dict(zip(columns, row))

    transitions = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.to_node = ? AND e.edge_type = 'TRANSITIONS_TO' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id, session_id],
        )
    )

    proposals = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'PROPOSES' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    approvals = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type IN ('APPROVES', 'DECLINES', 'MODIFIES') AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    instantiations = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.from_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.to_node = ? AND e.edge_type = 'INSTANTIATED_FROM' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    return {
        "session": session,
        "transitions": transitions,
        "proposals": proposals,
        "approvals": approvals,
        "instantiations": instantiations,
    }


def set_promotion_policy(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    policy: str,
    decision_node_id: str | None = None,
    min_improvement: float = 0.0,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    model_candidate_id = validate_identifier(model_candidate_id, name="model_candidate_id")

    valid_policies = {"accuracy", "decision_value"}
    if policy not in valid_policies:
        raise ValidationError(f"Invalid policy '{policy}' — must be one of {sorted(valid_policies)}")

    if policy == "decision_value" and not decision_node_id:
        raise ValidationError("decision_node_id is required when policy='decision_value'")

    if decision_node_id is not None:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    candidate_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_candidate_id],
    ).fetchone()
    if not candidate_row:
        raise NodeNotFoundError(f"Model candidate not found: {model_candidate_id}")

    current_meta_raw = candidate_row[1]
    current_meta: dict[str, Any] = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_meta["promotion_policy"] = policy
    if decision_node_id is not None:
        current_meta["decision_node_id"] = decision_node_id
    current_meta["min_improvement"] = min_improvement

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_candidate_id],
    )

    _log_change(conn, "ohm_nodes", model_candidate_id, "SET_PROMOTION_POLICY", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_candidate_id]))[0]


def auto_promote_best_model(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    decision_node_id: str | None = None,
    policy: str = "decision_value",
    min_improvement: float = 0.0,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL""",
            [twin_id],
        )
    )

    if not candidates:
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": [],
            "reason": "no_candidates",
            "detail": "No model candidates compete for this twin.",
        }

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        score = None
        scoring_error: str | None = None
        if policy == "decision_value" and decision_node_id is not None:
            try:
                dv = compute_decision_value(
                    conn,
                    model_id=c["id"],
                    decision_node_id=decision_node_id,
                    utility_scale=1.0,
                )
                score = dv["decision_value_score"]
            except (NodeNotFoundError, ValueError) as exc:
                scoring_error = str(exc)
        else:
            eval_nodes = _rows_to_dicts(
                conn.execute(
                    """SELECT n.metadata FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
                   WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
                   ORDER BY n.created_at DESC LIMIT 1""",
                    [c["id"]],
                )
            )
            if eval_nodes:
                ev_meta_raw = eval_nodes[0].get("metadata")
                if ev_meta_raw:
                    try:
                        ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                        score = ev_parsed.get("composite_score")
                    except (_json.JSONDecodeError, TypeError):
                        scoring_error = "failed to parse evaluation metadata"

        ranked.append(
            {
                "model_candidate_id": c["id"],
                "label": c.get("label", ""),
                "score": score,
                "scoring_error": scoring_error,
            }
        )

    # Both policy branches share the same sort key — higher score wins,
    # candidates with no score sink to the bottom. Previously duplicated
    # sort blocks masked the equivalence; now single sort handles both.
    ranked.sort(key=lambda r: r["score"] if r["score"] is not None else float("-inf"), reverse=True)

    best = ranked[0] if ranked else None

    if not best or best["score"] is None:
        # Either no candidates ranked, or the best candidate has no score
        # (missing evaluation, scoring error). Surface the reason.
        reason = "no_score"
        if best and best.get("scoring_error"):
            reason = "scoring_error"
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": reason,
            "detail": (f"Best candidate '{best['label']}' has no scorable evaluation: {best.get('scoring_error', 'no evaluation found')}." if best else "No candidates with scores."),
            "best_candidate": best,
        }

    try:
        promoted = promote_model(
            conn,
            model_candidate_id=best["model_candidate_id"],
            created_by=created_by,
            policy=policy,
            decision_node_id=decision_node_id,
            min_improvement=min_improvement,
        )
        return {
            "promoted": promoted,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": "promoted",
            "detail": f"Promoted '{best['label']}' with score {best['score']:.4f}.",
            "best_candidate": best,
        }
    except ValidationError as exc:
        # Promotion blocked — most commonly the best candidate did not
        # beat the active one by min_improvement. Surface this so the
        # caller can decide whether to lower the threshold, replace the
        # active model manually, or accept the status quo.
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": "below_min_improvement" if "min_improvement" in str(exc) else "promotion_blocked",
            "detail": str(exc),
            "best_candidate": best,
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

