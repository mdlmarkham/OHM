"""model_registry.py — extracted from queries/__init__.py (OHM-447 Phase 2).

Part of the twins/ML cluster decomposition. Re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from ohm.graph.queries import compute_confidence_with_decay, create_edge, create_node

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


# ── Model Marketplace (OHM-75tw) ──────────────────────────────────────────────


def register_model_candidate(
    conn: DuckDBPyConnection,
    *,
    label: str,
    twin_id: str,
    created_by: str,
    model_parameters: dict[str, Any] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Register a model candidate competing for a twin (OHM-75tw).

    Creates a ``model_candidate`` node linked via EVALUATES L3 to the twin.
    If other model_candidates already compete for the same twin, creates
    COMPETES_WITH L3 edges between them.

    Args:
        conn: Database connection.
        label: Human-readable model name.
        twin_id: The twin this model competes for.
        created_by: Agent registering the model.
        model_parameters: Optional dict of model hyperparameters (stored in metadata).
        description: Optional description of the model.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The registered model_candidate node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    metadata_dict = None
    if model_parameters is not None:
        metadata_dict = {"model_parameters": model_parameters, "gate_status": "candidate"}

    candidate = create_node(
        conn,
        label=label,
        node_type="model_candidate",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[twin_id] + (list(connects_to) if connects_to else []),
    )

    create_edge(
        conn,
        from_node=candidate["id"],
        to_node=twin_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != twin_id:
                create_edge(
                    conn,
                    from_node=candidate["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    existing_candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
            [twin_id, candidate["id"]],
        )
    )
    for ec in existing_candidates:
        create_edge(
            conn,
            from_node=candidate["id"],
            to_node=ec["id"],
            edge_type="COMPETES_WITH",
            layer="L3",
            created_by=created_by,
        )
        create_edge(
            conn,
            from_node=ec["id"],
            to_node=candidate["id"],
            edge_type="COMPETES_WITH",
            layer="L3",
            created_by=created_by,
        )

    _log_change(conn, "ohm_nodes", candidate["id"], "REGISTER_MODEL_CANDIDATE", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [candidate["id"]]))[0]


def evaluate_model(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    created_by: str,
    metrics: dict[str, float],
    dataset: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Evaluate a model candidate and store metrics (OHM-75tw).

    Creates a ``model_evaluation`` node linked via EVALUATED_BY L3 to the
    model candidate. Metrics (MAE, RMSE, log_loss, accuracy, etc.) are
    stored in the metadata column as JSON.

    Args:
        conn: Database connection.
        model_candidate_id: The model candidate being evaluated.
        created_by: Agent performing the evaluation.
        metrics: Dict of metric name → score (e.g., {"mae": 0.12, "rmse": 0.18}).
        dataset: Optional name of the evaluation dataset.
        description: Optional description of the evaluation.

    Returns:
        The created model_evaluation node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    model_candidate_id = validate_identifier(model_candidate_id, name="model_candidate_id")

    candidate = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_candidate_id],
    ).fetchone()
    if not candidate:
        raise NodeNotFoundError(f"Model candidate not found: {model_candidate_id}")

    if not metrics:
        raise ValidationError("metrics dict must not be empty")

    composite_score = 0.0
    weight_sum = 0.0
    metric_weights = {"accuracy": 1.0, "rmse": -1.0, "mae": -1.0, "log_loss": -1.0}
    for metric_name, value in metrics.items():
        weight = metric_weights.get(metric_name, 0.5)
        composite_score += weight * value
        weight_sum += abs(weight)
    if weight_sum > 0:
        composite_score = composite_score / weight_sum
    else:
        composite_score = 0.5

    metadata_dict: dict[str, Any] = {
        "metrics": metrics,
        "composite_score": round(composite_score, 6),
    }
    if dataset:
        metadata_dict["dataset"] = dataset

    eval_label = f"Evaluation of {model_candidate_id}"
    if dataset:
        eval_label = f"Evaluation on {dataset}"

    evaluation = create_node(
        conn,
        label=eval_label,
        node_type="model_evaluation",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[model_candidate_id],
    )

    create_edge(
        conn,
        from_node=model_candidate_id,
        to_node=evaluation["id"],
        edge_type="EVALUATED_BY",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", evaluation["id"], "EVALUATE_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [evaluation["id"]]))[0]


def compare_models(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float | None = None,
) -> dict[str, Any]:
    """Compare all model candidates competing for a twin (OHM-75tw).

    Returns a ranked list of model candidates with their latest evaluation
    metrics and composite scores. When ``apply_decay`` is True (default),
    each candidate's ``composite_score`` is decayed by the age of its latest
    evaluation before ranking — a stale evaluation loses influence vs. a
    fresh one. Both raw and decayed scores are returned so callers can audit.

    Args:
        conn: Database connection.
        twin_id: The twin whose competing models to compare.
        apply_decay: When True (default), decay each candidate's composite
            score by the age of its latest evaluation before sorting.
        half_life_days: Confidence half-life used for decay (default 30).
        decay_floor: Lower bound on decayed score (default 0.1).

    Returns:
        Dict with twin_id, candidates (ranked list), and recommendation.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata, n.created_at
           FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC""",
            [twin_id],
        )
    )

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        meta_raw = c.get("metadata")
        model_params = None
        gate_status = "candidate"
        if meta_raw:
            try:
                parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                model_params = parsed.get("model_parameters")
                gate_status = parsed.get("gate_status", "candidate")
            except (_json.JSONDecodeError, TypeError):
                pass

        eval_nodes = _rows_to_dicts(
            conn.execute(
                """SELECT n.id, n.label, n.metadata, n.created_at
               FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
               ORDER BY n.created_at DESC LIMIT 1""",
                [c["id"]],
            )
        )

        latest_eval = None
        composite_score = None
        decayed_composite_score = None
        decay_info = None
        metrics = None
        dataset = None
        if eval_nodes:
            ev = eval_nodes[0]
            ev_meta_raw = ev.get("metadata")
            if ev_meta_raw:
                try:
                    ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                    metrics = ev_parsed.get("metrics")
                    composite_score = ev_parsed.get("composite_score")
                    dataset = ev_parsed.get("dataset")
                except (_json.JSONDecodeError, TypeError):
                    pass
            latest_eval = {
                "evaluation_id": ev["id"],
                "label": ev.get("label", ""),
                "created_at": ev.get("created_at"),
                "metrics": metrics,
                "composite_score": composite_score,
                "dataset": dataset,
            }
            if composite_score is not None and apply_decay:
                # composite_score can be negative (error metrics weighted -1.0
                # dominate). A 0.1 floor would collapse all candidates to the
                # same value, so default to floor=None (decay is multiplicative
                # only). Callers can pass decay_floor explicitly if their
                # composite_score is always non-negative.
                decay_info = compute_confidence_with_decay(
                    conn,
                    base_confidence=composite_score,
                    last_observed_at=ev.get("created_at"),
                    half_life_days=half_life_days,
                    floor=decay_floor,
                )
                decayed_composite_score = decay_info["decayed_confidence"]
            elif composite_score is not None:
                decayed_composite_score = composite_score

        ranked.append(
            {
                "model_candidate_id": c["id"],
                "label": c.get("label", ""),
                "gate_status": gate_status,
                "model_parameters": model_params,
                "latest_evaluation": latest_eval,
                "composite_score": composite_score,
                "decayed_composite_score": decayed_composite_score,
                "decay": decay_info,
            }
        )

    sort_key = (lambda r: r["decayed_composite_score"] if r["decayed_composite_score"] is not None else float("-inf")) if apply_decay else (lambda r: r["composite_score"] if r["composite_score"] is not None else float("-inf"))
    ranked.sort(key=sort_key, reverse=True)

    recommendation = None
    if ranked:
        top = ranked[0]
        top_score = top["decayed_composite_score"] if apply_decay else top["composite_score"]
        if top_score is not None:
            recommendation = {
                "model_candidate_id": top["model_candidate_id"],
                "label": top["label"],
                "composite_score": top["composite_score"],
                "decayed_composite_score": top["decayed_composite_score"],
            }

    return {
        "twin_id": twin_id,
        "twin_label": twin[1],
        "candidates": ranked,
        "recommendation": recommendation,
        "apply_decay": apply_decay,
        "half_life_days": half_life_days if apply_decay else None,
    }


def promote_model(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    created_by: str,
    policy: str = "accuracy",
    decision_node_id: str | None = None,
    min_improvement: float = 0.0,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Promote a model candidate to active status for its twin (OHM-75tw).

    Sets the promoted candidate's gate_status to 'active' and archives all
    other competing candidates (gate_status → 'archived') for the same twin.

    Args:
        conn: Database connection.
        model_candidate_id: The model candidate to promote.
        created_by: Agent performing the promotion.
        policy: Promotion policy — "accuracy" (default) or "decision_value".
        decision_node_id: Required when policy="decision_value".
        min_improvement: Minimum decision_value improvement over active model
            (only used with policy="decision_value").

    Returns:
        The promoted model_candidate node record with promotion metadata.
    """
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

    twin_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           AND e.to_node IN (SELECT id FROM ohm_nodes WHERE type = 'twin' AND deleted_at IS NULL)""",
            [model_candidate_id],
        )
    )
    twin_ids = [e["to_node"] for e in twin_edges]

    candidate_decision_value = None
    active_model_id = None
    active_decision_value = None

    if policy == "decision_value" and decision_node_id is not None:
        candidate_dv = compute_decision_value(
            conn,
            model_id=model_candidate_id,
            decision_node_id=decision_node_id,
            utility_scale=1.0,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )
        candidate_decision_value = candidate_dv["decision_value_score"]

        for twin_id in twin_ids:
            active_candidates = _rows_to_dicts(
                conn.execute(
                    """SELECT n.id, n.metadata FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
                   WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
                    [twin_id, model_candidate_id],
                )
            )
            for ac in active_candidates:
                ac_meta_raw = ac.get("metadata")
                ac_meta = {}
                if ac_meta_raw:
                    try:
                        ac_meta = _json.loads(ac_meta_raw) if isinstance(ac_meta_raw, str) else ac_meta_raw
                    except (_json.JSONDecodeError, TypeError):
                        pass
                if ac_meta.get("gate_status") == "active":
                    active_model_id = ac["id"]
                    active_dv = compute_decision_value(
                        conn,
                        model_id=ac["id"],
                        decision_node_id=decision_node_id,
                        utility_scale=1.0,
                        apply_decay=apply_decay,
                        half_life_days=half_life_days,
                        decay_floor=decay_floor,
                    )
                    active_decision_value = active_dv["decision_value_score"]
                    break
            if active_model_id:
                break

        if active_model_id is not None and active_decision_value is not None:
            if candidate_decision_value < active_decision_value + min_improvement:
                raise ValidationError(f"Candidate decision_value ({candidate_decision_value}) does not exceed active model ({active_model_id}) decision_value ({active_decision_value}) + min_improvement ({min_improvement})")

    for twin_id in twin_ids:
        other_candidates = _rows_to_dicts(
            conn.execute(
                """SELECT n.id, n.metadata FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
               WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
                [twin_id, model_candidate_id],
            )
        )
        for oc in other_candidates:
            oc_meta_raw = oc.get("metadata")
            oc_meta = {}
            if oc_meta_raw:
                try:
                    oc_meta = _json.loads(oc_meta_raw) if isinstance(oc_meta_raw, str) else oc_meta_raw
                except (_json.JSONDecodeError, TypeError):
                    pass
            oc_meta["gate_status"] = "archived"
            conn.execute(
                "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
                [_json.dumps(oc_meta), oc["id"]],
            )

    current_meta_raw = candidate_row[1]
    current_meta = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass
    current_meta["gate_status"] = "active"
    current_meta["promotion_policy"] = policy
    if candidate_decision_value is not None:
        current_meta["promotion_decision_value"] = candidate_decision_value
    if active_model_id is not None:
        current_meta["previous_active_id"] = active_model_id
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_candidate_id],
    )

    _log_change(conn, "ohm_nodes", model_candidate_id, "PROMOTE_MODEL", created_by)

    result = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_candidate_id]))[0]
    result["promotion_policy"] = policy
    if candidate_decision_value is not None:
        result["promotion_decision_value"] = candidate_decision_value
    if active_model_id is not None:
        result["previous_active_id"] = active_model_id
    return result


# ── Operational Twin Models (OHM-bf45) ────────────────────────────────────────


def register_shadow_model(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    label: str,
    source_model_id: str,
    created_by: str,
    model_parameters: dict[str, Any] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")
    source_model_id = validate_identifier(source_model_id, name="source_model_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    source = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [source_model_id],
    ).fetchone()
    if not source:
        raise NodeNotFoundError(f"Source model not found: {source_model_id}")

    metadata_dict: dict[str, Any] = {
        "model_parameters": model_parameters or {},
        "gate_status": "shadow",
    }

    shadow = create_node(
        conn,
        label=label,
        node_type="model_candidate",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[twin_id, source_model_id] + (list(connects_to) if connects_to else []),
    )

    create_edge(
        conn,
        from_node=shadow["id"],
        to_node=twin_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    create_edge(
        conn,
        from_node=shadow["id"],
        to_node=source_model_id,
        edge_type="SHADOWS",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", shadow["id"], "REGISTER_SHADOW_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [shadow["id"]]))[0]


def detect_drift(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    window_size: int = 100,
    residual_threshold: float = 0.15,
    created_by: str | None = None,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    active_models = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL""",
            [twin_id],
        )
    )

    observations = _rows_to_dicts(
        conn.execute(
            """SELECT o.id, o.node_id, o.value, o.baseline, o.created_at
           FROM ohm_observations o
           WHERE o.node_id = ? AND o.deleted_at IS NULL
           ORDER BY o.created_at DESC LIMIT ?""",
            [twin_id, window_size],
        )
    )

    drift_score = 0.0
    drift_type = "none"
    drift_details: dict[str, Any] = {}

    if observations and active_models:
        values = [o["value"] for o in observations if o.get("value") is not None]
        baselines = [o.get("baseline") for o in observations if o.get("baseline") is not None]

        if values and baselines:
            residuals = [abs(v - b) for v, b in zip(values, baselines)]
            mae = sum(residuals) / len(residuals) if residuals else 0.0
            drift_details["residual_mae"] = round(mae, 6)
            if mae > residual_threshold:
                drift_score = min(mae / (residual_threshold * 2), 1.0)
                drift_type = "residual"

        if drift_type == "none" and len(values) >= 10:
            half = len(values) // 2
            recent_mean = sum(values[:half]) / half if half > 0 else 0.0
            older_mean = sum(values[half:]) / (len(values) - half) if (len(values) - half) > 0 else 0.0
            if older_mean != 0:
                feature_shift = abs(recent_mean - older_mean) / abs(older_mean)
                drift_details["feature_shift"] = round(feature_shift, 6)
                if feature_shift > 0.5:
                    drift_score = min(feature_shift, 1.0)
                    drift_type = "feature"

        if drift_type == "none" and len(active_models) > 1:
            scores = []
            for m in active_models:
                meta_raw = m.get("metadata")
                if meta_raw:
                    try:
                        _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                        eval_nodes = _rows_to_dicts(
                            conn.execute(
                                """SELECT n.metadata FROM ohm_nodes n
                               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
                               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
                               ORDER BY n.created_at DESC LIMIT 1""",
                                [m["id"]],
                            )
                        )
                        if eval_nodes:
                            ev_meta_raw = eval_nodes[0].get("metadata")
                            if ev_meta_raw:
                                ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                                cs = ev_parsed.get("composite_score")
                                if cs is not None:
                                    scores.append(cs)
                    except (_json.JSONDecodeError, TypeError):
                        pass
            if len(scores) >= 2:
                score_variance = sum((s - sum(scores) / len(scores)) ** 2 for s in scores) / len(scores)
                drift_details["ensemble_variance"] = round(score_variance, 6)
                if score_variance > 0.1:
                    drift_score = min(score_variance * 5, 1.0)
                    drift_type = "ensemble_disagreement"

    result: dict[str, Any] = {
        "twin_id": twin_id,
        "drift_score": round(drift_score, 6),
        "drift_type": drift_type,
        "drift_details": drift_details,
        "observation_count": len(observations),
        "active_model_count": len(active_models),
    }

    if drift_score > 0.0 and created_by:
        event = create_node(
            conn,
            label=f"Drift detected on {twin_id}",
            node_type="drift_event",
            created_by=created_by,
            metadata={
                "drift_score": round(drift_score, 6),
                "drift_type": drift_type,
                "window_size": window_size,
                "residual_threshold": residual_threshold,
                "drift_details": drift_details,
            },
            connects_to=[twin_id],
        )

        create_edge(
            conn,
            from_node=twin_id,
            to_node=event["id"],
            edge_type="DRIFT_SIGNAL",
            layer="L3",
            created_by=created_by,
        )

        _log_change(conn, "ohm_nodes", event["id"], "DETECT_DRIFT", created_by)
        result["drift_event_id"] = event["id"]

    return result


def run_walk_forward_validation(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    n_splits: int = 5,
    min_train_size: int = 50,
    created_by: str,
) -> dict[str, Any]:

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")

    model = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    twin_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           AND e.to_node IN (SELECT id FROM ohm_nodes WHERE type = 'twin' AND deleted_at IS NULL)""",
            [model_id],
        )
    )
    twin_id = twin_edges[0]["to_node"] if twin_edges else None

    observations = []
    if twin_id:
        observations = _rows_to_dicts(
            conn.execute(
                """SELECT o.id, o.value, o.baseline, o.created_at
               FROM ohm_observations o
               WHERE o.node_id = ? AND o.deleted_at IS NULL
               ORDER BY o.created_at ASC""",
                [twin_id],
            )
        )

    total_obs = len(observations)
    per_split_metrics: list[dict[str, Any]] = []
    overfitting_detected = False

    if total_obs >= min_train_size + n_splits:
        split_size = max((total_obs - min_train_size) // n_splits, 1)
        for i in range(n_splits):
            train_end = min_train_size + i * split_size
            test_start = train_end
            test_end = min(train_end + split_size, total_obs)

            train_obs = observations[:train_end]
            test_obs = observations[test_start:test_end]

            [o["value"] for o in train_obs if o.get("value") is not None]
            test_values = [o["value"] for o in test_obs if o.get("value") is not None]
            test_baselines = [o.get("baseline") for o in test_obs if o.get("baseline") is not None]

            split_mae = 0.0
            if test_values and test_baselines and len(test_values) == len(test_baselines):
                residuals = [abs(v - b) for v, b in zip(test_values, test_baselines)]
                split_mae = sum(residuals) / len(residuals)

            per_split_metrics.append(
                {
                    "split": i + 1,
                    "train_size": len(train_obs),
                    "test_size": len(test_obs),
                    "mae": round(split_mae, 6),
                }
            )

    if len(per_split_metrics) >= 3:
        early_mae = sum(s["mae"] for s in per_split_metrics[: len(per_split_metrics) // 2]) / max(len(per_split_metrics) // 2, 1)
        late_mae = sum(s["mae"] for s in per_split_metrics[len(per_split_metrics) // 2 :]) / max(len(per_split_metrics) - len(per_split_metrics) // 2, 1)
        if early_mae > 0 and late_mae > early_mae * 1.5:
            overfitting_detected = True

    mean_mae = sum(s["mae"] for s in per_split_metrics) / len(per_split_metrics) if per_split_metrics else 0.0
    std_mae = (sum((s["mae"] - mean_mae) ** 2 for s in per_split_metrics) / len(per_split_metrics)) ** 0.5 if per_split_metrics else 0.0

    metadata_dict: dict[str, Any] = {
        "model_id": model_id,
        "n_splits": n_splits,
        "min_train_size": min_train_size,
        "per_split_metrics": per_split_metrics,
        "mean_mae": round(mean_mae, 6),
        "std_mae": round(std_mae, 6),
        "overfitting_detected": overfitting_detected,
        "total_observations": total_obs,
    }

    validation = create_node(
        conn,
        label=f"Walk-forward validation of {model_id}",
        node_type="validation_run",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[model_id],
    )

    create_edge(
        conn,
        from_node=model_id,
        to_node=validation["id"],
        edge_type="EVALUATED_BY",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", validation["id"], "WALK_FORWARD_VALIDATION", created_by)

    return {
        "validation_id": validation["id"],
        "model_id": model_id,
        "n_splits": n_splits,
        "per_split_metrics": per_split_metrics,
        "mean_mae": round(mean_mae, 6),
        "std_mae": round(std_mae, 6),
        "overfitting_detected": overfitting_detected,
        "total_observations": total_obs,
    }


def ensemble_predict(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    observation_window: int = 50,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float | None = None,
) -> dict[str, Any]:
    """Weighted prediction across competing model candidates for a twin (OHM-75tw).

    Each candidate's weight is its latest ``composite_score`` (decayed by
    observation age when ``apply_decay`` is True). Negative decayed scores
    are clamped to 0 for weighting (a model with bad accuracy should not
    get a positive vote). Both raw and decayed scores are returned in the
    per-vote breakdown so callers can audit.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

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

    votes: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_prediction = 0.0

    for c in candidates:
        meta_raw = c.get("metadata")
        gate_status = "candidate"
        composite_score = None
        if meta_raw:
            try:
                parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                gate_status = parsed.get("gate_status", "candidate")
            except (_json.JSONDecodeError, TypeError):
                pass

        eval_nodes = _rows_to_dicts(
            conn.execute(
                """SELECT n.metadata, n.created_at FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
               ORDER BY n.created_at DESC LIMIT 1""",
                [c["id"]],
            )
        )
        decay_info = None
        if eval_nodes:
            ev_meta_raw = eval_nodes[0].get("metadata")
            if ev_meta_raw:
                try:
                    ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                    composite_score = ev_parsed.get("composite_score")
                except (_json.JSONDecodeError, TypeError):
                    pass

            if apply_decay and composite_score is not None:
                decay_info = compute_confidence_with_decay(
                    conn,
                    base_confidence=composite_score,
                    last_observed_at=eval_nodes[0].get("created_at"),
                    half_life_days=half_life_days,
                    floor=decay_floor,
                )
                decayed_score: float = decay_info["decayed_confidence"]
            else:
                decayed_score = float(composite_score) if composite_score is not None else 0.0
        else:
            decayed_score = 0.0

        # Weight = max(0, decayed) — negative scores do not vote positively.
        weight = decayed_score if decayed_score > 0 else 0.0
        votes.append(
            {
                "model_id": c["id"],
                "label": c.get("label", ""),
                "gate_status": gate_status,
                "composite_score": composite_score,
                "decayed_composite_score": round(decayed_score, 6),
                "weight": round(weight, 6),
                "decay": decay_info,
            }
        )
        total_weight += weight

    if total_weight > 0:
        for v in votes:
            v["normalized_weight"] = round(v["weight"] / total_weight, 6)
            weighted_prediction += v["decayed_composite_score"] * v["normalized_weight"]
    else:
        for v in votes:
            v["normalized_weight"] = 0.0

    disagreement = 0.0
    if len(votes) >= 2 and total_weight > 0:
        scores = [v["decayed_composite_score"] for v in votes if v["composite_score"] is not None]
        if scores:
            mean_s = sum(scores) / len(scores)
            disagreement = sum((s - mean_s) ** 2 for s in scores) / len(scores)

    return {
        "twin_id": twin_id,
        "weighted_prediction": round(weighted_prediction, 6),
        "votes": votes,
        "disagreement": round(disagreement, 6),
        "candidate_count": len(candidates),
    }


def compute_decision_value(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    decision_node_id: str,
    utility_scale: float,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Compute a model's decision value for a given decision (OHM-75tw).

    The decision value is a utility-weighted accuracy measure penalized by
    model cost, latency, and overfitting risk. When ``apply_decay`` is True
    (default), the model's accuracy is decayed by the age of its latest
    evaluation before scoring — a stale evaluation loses influence. Both raw
    and decayed accuracy are returned so callers can audit. ``decay_floor``
    defaults to 0.1 because accuracy is in [0, 1] (different from
    ``composite_score`` which can be negative).
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")
    decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    model = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_node_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    accuracy = 0.5
    latency = 0.0
    cost = 0.0
    overfitting_risk = 0.0
    decay_info: dict | None = None

    eval_nodes = _rows_to_dicts(
        conn.execute(
            """SELECT n.metadata, n.created_at FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
           WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC LIMIT 1""",
            [model_id],
        )
    )
    if eval_nodes:
        ev_meta_raw = eval_nodes[0].get("metadata")
        if ev_meta_raw:
            try:
                ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                metrics = ev_parsed.get("metrics", {})
                accuracy = metrics.get("accuracy", 0.5)
                composite_score = ev_parsed.get("composite_score", 0.5)
                if composite_score < 0.3:
                    overfitting_risk = 0.5
            except (_json.JSONDecodeError, TypeError):
                pass

        if apply_decay:
            decay_info = compute_confidence_with_decay(
                conn,
                base_confidence=accuracy,
                last_observed_at=eval_nodes[0].get("created_at"),
                half_life_days=half_life_days,
                floor=decay_floor,
            )
            decayed_accuracy: float = decay_info["decayed_confidence"]
        else:
            decayed_accuracy = accuracy
    else:
        decayed_accuracy = accuracy

    meta_raw = model[2]
    if meta_raw:
        try:
            parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            params = parsed.get("model_parameters", {})
            latency = params.get("latency", 0.0)
            cost = params.get("cost", 0.0)
            overfitting_risk = params.get("overfitting_risk", overfitting_risk)
        except (_json.JSONDecodeError, TypeError):
            pass

    decision_value_score = max(0.0, min(1.0, utility_scale * decayed_accuracy - latency * cost * overfitting_risk))

    return {
        "model_id": model_id,
        "decision_node_id": decision_node_id,
        "utility_scale": utility_scale,
        "accuracy": accuracy,
        "decayed_accuracy": round(decayed_accuracy, 6),
        "latency": latency,
        "cost": cost,
        "overfitting_risk": overfitting_risk,
        "decision_value_score": round(decision_value_score, 6),
        "decay": decay_info,
    }


def auto_retire_model(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    reason: str,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")

    model_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model_row:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    current_meta_raw = model_row[1]
    current_meta: dict[str, Any] = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_meta["gate_status"] = "retired"
    current_meta["retirement_reason"] = reason
    current_meta["retired_by"] = created_by

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_id],
    )

    _log_change(conn, "ohm_nodes", model_id, "AUTO_RETIRE_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_id]))[0]


def set_freshness_threshold(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    max_age_seconds: int,
    created_by: str,
    label: str | None = None,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    if max_age_seconds < 1:
        raise ValueError("max_age_seconds must be >= 1")

    ft_label = label or f"Freshness threshold for {decision_id}"
    metadata_dict = {"max_age_seconds": max_age_seconds}

    ft_node = create_node(
        conn,
        label=ft_label,
        node_type="freshness_threshold",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=ft_node["id"],
        to_node=decision_id,
        edge_type="GOVERNS_FRESHNESS",
        layer="L3",
        created_by=created_by,
    )

    return ft_node


def get_freshness_status(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    thresholds = conn.execute(
        """
        SELECT n.id, n.label, n.metadata
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'GOVERNS_FRESHNESS'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchall()

    threshold_records = []
    for row in thresholds:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        threshold_records.append(
            {
                "id": row[0],
                "label": row[1],
                "max_age_seconds": meta.get("max_age_seconds"),
            }
        )

    latest_obs = conn.execute(
        """
        SELECT MAX(o.created_at)
        FROM ohm_observations o
        JOIN ohm_edges e ON e.to_node = o.node_id AND e.deleted_at IS NULL
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND o.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchone()

    latest_obs_time = latest_obs[0] if latest_obs and latest_obs[0] else None

    now_result = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    now_ts = now_result[0] if now_result else None

    age_seconds = None
    if latest_obs_time and now_ts:
        try:
            age_seconds = (now_ts - latest_obs_time).total_seconds()
        except (AttributeError, TypeError):
            age_seconds = None

    max_age = None
    if threshold_records:
        max_age = min(t["max_age_seconds"] for t in threshold_records if t["max_age_seconds"] is not None)

    freshness_pressure = None
    if age_seconds is not None and max_age is not None and max_age > 0:
        freshness_pressure = round(min(age_seconds / max_age, 1.0), 4)

    return {
        "decision_id": decision_id,
        "thresholds": threshold_records,
        "latest_observation_at": str(latest_obs_time) if latest_obs_time else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "max_age_seconds": max_age,
        "freshness_pressure": freshness_pressure,
    }
