"""bindings.py — extracted from queries/__init__.py (OHM-447 Phase 2).

Part of the twins/ML cluster decomposition. Re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile


def register_twin_with_bindings(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    decision_node_id: str | None = None,
    feed_node_ids: Sequence[str] | None = None,
    model_candidate_ids: Sequence[str] | None = None,
    created_by: str,
    description: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    if decision_node_id is not None:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")
        decision = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [decision_node_id],
        ).fetchone()
        if not decision:
            raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    validated_feeds: list[str] = []
    if feed_node_ids is not None:
        for fid in feed_node_ids:
            fid = validate_identifier(fid, name="feed_node_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [fid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Feed node not found: {fid}")
            validated_feeds.append(fid)

    validated_models: list[str] = []
    if model_candidate_ids is not None:
        for mid in model_candidate_ids:
            mid = validate_identifier(mid, name="model_candidate_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [mid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Model candidate node not found: {mid}")
            validated_models.append(mid)

    twin = create_node(
        conn,
        label=label,
        node_type="twin",
        content=description,
        created_by=created_by,
        url=endpoint_url,
        connects_to=[target_node_id] + validated_feeds + validated_models,
    )

    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'external' WHERE id = ?",
        [twin["id"]],
    )

    create_edge(
        conn,
        from_node=twin["id"],
        to_node=target_node_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if decision_node_id is not None:
        create_edge(
            conn,
            from_node=twin["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
        )

    for fid in validated_feeds:
        create_edge(
            conn,
            from_node=fid,
            to_node=twin["id"],
            edge_type="FEEDS",
            layer="L2",
            created_by=created_by,
        )

    for mid in validated_models:
        create_edge(
            conn,
            from_node=mid,
            to_node=twin["id"],
            edge_type="APPLIES_TO",
            layer="L3",
            created_by=created_by,
        )

    _log_change(conn, "ohm_nodes", twin["id"], "REGISTER_TWIN_WITH_BINDINGS", created_by)

    twin = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]

    return {
        "twin": twin,
        "target_node_id": target_node_id,
        "decision_bound": decision_node_id is not None,
        "feeds_bound": len(validated_feeds),
        "models_bound": len(validated_models),
    }


def add_twin_bindings(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    feed_node_ids: Sequence[str] | None = None,
    feed_node_ids_remove: Sequence[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    added: list[str] = []
    if feed_node_ids is not None:
        for fid in feed_node_ids:
            fid = validate_identifier(fid, name="feed_node_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [fid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Feed node not found: {fid}")
            existing_edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
                [fid, twin_id],
            ).fetchone()
            if not existing_edge:
                create_edge(
                    conn,
                    from_node=fid,
                    to_node=twin_id,
                    edge_type="FEEDS",
                    layer="L2",
                    created_by=created_by,
                )
                added.append(fid)

    removed: list[str] = []
    if feed_node_ids_remove is not None:
        for fid in feed_node_ids_remove:
            fid = validate_identifier(fid, name="feed_node_ids_remove entry")
            edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
                [fid, twin_id],
            ).fetchone()
            if edge:
                conn.execute(
                    "UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [edge[0]],
                )
                _log_change(conn, "ohm_edges", edge[0], "SOFT_DELETE", created_by)
                removed.append(fid)

    current_feeds = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS feed_id, n.label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    return {
        "twin_id": twin_id,
        "added": added,
        "removed": removed,
        "current_feeds": current_feeds,
    }


def attach_twin_models(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    model_candidate_ids: Sequence[str] | None = None,
    model_candidate_ids_remove: Sequence[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    added: list[str] = []
    if model_candidate_ids is not None:
        for mid in model_candidate_ids:
            mid = validate_identifier(mid, name="model_candidate_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [mid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Model candidate node not found: {mid}")
            existing_edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'APPLIES_TO' AND deleted_at IS NULL",
                [mid, twin_id],
            ).fetchone()
            if not existing_edge:
                create_edge(
                    conn,
                    from_node=mid,
                    to_node=twin_id,
                    edge_type="APPLIES_TO",
                    layer="L3",
                    created_by=created_by,
                )
                added.append(mid)

    removed: list[str] = []
    if model_candidate_ids_remove is not None:
        for mid in model_candidate_ids_remove:
            mid = validate_identifier(mid, name="model_candidate_ids_remove entry")
            edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'APPLIES_TO' AND deleted_at IS NULL",
                [mid, twin_id],
            ).fetchone()
            if edge:
                conn.execute(
                    "UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [edge[0]],
                )
                _log_change(conn, "ohm_edges", edge[0], "SOFT_DELETE", created_by)
                removed.append(mid)

    current_models = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS model_id, n.label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'APPLIES_TO' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    return {
        "twin_id": twin_id,
        "added": added,
        "removed": removed,
        "current_models": current_models,
    }


def get_twin_readiness(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    freshness_days: int | None = None,
) -> dict[str, Any]:
    """Check whether a twin is ready to make decisions.

    Args:
        twin_id: ID of the twin node.
        freshness_days: Max age (in days) for a feed to count as fresh.
            Defaults to 7 days when not provided. The caller can pass
            a stricter window (e.g. 1) to surface "threshold exceeded"
            states in dashboards.

    Returns a dict with:
        - twin_id, gates, ready, missing, blocking (as before)
        - threshold: {days, configured, source} so callers can tell
          "no threshold set (default)" from "threshold set + exceeded".
          Resolves the OHM-kg16 item 4 UX concern.

    Threshold semantics (kg16 item 4):
        - "no_threshold_set": no caller has asked for a specific
          window; the default 7d is applied. If feeds_fresh is false
          in this state, the caller is seeing the default
          interpretation, not a user-set threshold.
        - "threshold_exceeded": a caller passed freshness_days (or
          a future persistent threshold is set), and feeds are
          older than that window. The failing feeds_fresh gate is
          binding the configured threshold, not the default.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    twin_id = validate_identifier(twin_id, name="twin_id")

    threshold_configured = freshness_days is not None
    effective_days = int(freshness_days) if threshold_configured else 7
    if effective_days <= 0:
        raise ValidationError(f"freshness_days must be a positive integer, got {freshness_days}")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    target_bound = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    decision_bound = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE from_node = ? AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    feeds_present = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    feeds_fresh = False
    if feeds_present:
        feed_edges = _rows_to_dicts(
            conn.execute(
                """SELECT e.from_node AS feed_id
               FROM ohm_edges e
               WHERE e.to_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL""",
                [twin_id],
            )
        )
        feed_ids = [fe["feed_id"] for fe in feed_edges]
        if feed_ids:
            placeholders = ",".join(["?"] * len(feed_ids))
            fresh_count = conn.execute(
                f"""SELECT COUNT(DISTINCT o.node_id)
                    FROM ohm_observations o
                    WHERE o.node_id IN ({placeholders})
                      AND o.deleted_at IS NULL
                      AND o.created_at > CURRENT_TIMESTAMP - INTERVAL '{effective_days} days'""",
                feed_ids,
            ).fetchone()[0]
            feeds_fresh = fresh_count == len(feed_ids)

    models_available = bool(
        conn.execute(
            """SELECT 1 FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'APPLIES_TO' AND e.deleted_at IS NULL
             AND (n.metadata IS NULL OR n.metadata NOT LIKE '%archived%')""",
            [twin_id],
        ).fetchone()
    )

    models_evaluated = bool(
        conn.execute(
            """SELECT 1 FROM ohm_edges e
           WHERE e.to_node = ? AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL""",
            [twin_id],
        ).fetchone()
    )

    gates = {
        "target_bound": target_bound,
        "decision_bound": decision_bound,
        "feeds_present": feeds_present,
        "feeds_fresh": feeds_fresh,
        "models_available": models_available,
        "models_evaluated": models_evaluated,
    }

    critical_gates = ["target_bound", "feeds_present", "models_available"]
    ready = all(gates[g] for g in critical_gates)
    missing = [g for g, v in gates.items() if not v]
    blocking = [g for g in critical_gates if not gates[g]]

    # Threshold state — the UX distinction called out in OHM-kg16
    # item 4. Three states surface the relationship between the
    # feeds_fresh gate and whether anyone asked for a specific
    # window.
    if not threshold_configured and feeds_present and not feeds_fresh:
        threshold_state = "no_threshold_set"  # default 7d, no caller override
    elif threshold_configured and feeds_present and not feeds_fresh:
        threshold_state = "threshold_exceeded"  # caller-set window violated
    else:
        threshold_state = "within_threshold"  # feeds fresh or no feeds

    return {
        "twin_id": twin_id,
        "gates": gates,
        "ready": ready,
        "missing": missing,
        "blocking": blocking,
        "threshold": {
            "days": effective_days,
            "configured": threshold_configured,
            "source": "configured" if threshold_configured else "default",
        },
        "threshold_state": threshold_state,
    }


