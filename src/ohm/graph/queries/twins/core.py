"""core.py — extracted from queries/__init__.py (OHM-447 Phase 2).

Part of the twins/ML cluster decomposition. Re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile

def register_twin(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    created_by: str,
    endpoint_url: str | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Register an external domain twin as a ``twin`` node linked via EVALUATES L3 (OHM-josq).

    Creates a ``twin`` node with ``gate_type='external'`` and an EVALUATES L3
    edge from the twin to *target_node_id*. If *connects_to* is provided,
    additional EVALUATES L3 edges are created from the twin to each referenced
    node.

    Args:
        conn: Database connection.
        label: Human-readable twin name.
        target_node_id: The node this twin models.
        created_by: Agent registering the twin.
        endpoint_url: Optional URL of the external twin service.
        description: Optional description of what the twin models.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The registered twin node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    twin = create_node(
        conn,
        label=label,
        node_type="twin",
        content=description,
        created_by=created_by,
        url=endpoint_url,
        connects_to=[target_node_id] + (list(connects_to) if connects_to else []),
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

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=twin["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    _log_change(conn, "ohm_nodes", twin["id"], "REGISTER_TWIN", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]


def twin_predict(
    conn: DuckDBPyConnection,
    twin_id: str,
    *,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return twin prediction as edge_overrides-compatible dict (OHM-josq).

    Returns the target node + connected nodes with their current observation
    probabilities, formatted as ``{node_id: probability}`` for use with
    ``query_counterfactual_cascade``'s ``edge_overrides`` parameter.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label, url FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node, e.probability, e.confidence, n.label AS target_label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    edge_overrides: dict[str, float] = {}
    nodes: list[dict[str, Any]] = []
    for edge in eval_edges:
        prob = edge.get("probability")
        if prob is None:
            prob = edge.get("confidence", 0.5)
        edge_overrides[edge["to_node"]] = prob
        nodes.append(
            {
                "node_id": edge["to_node"],
                "label": edge.get("target_label", ""),
                "probability": prob,
            }
        )

    return {
        "twin_id": twin_id,
        "edge_overrides": edge_overrides,
        "nodes": nodes,
    }


def twin_constraints(
    conn: DuckDBPyConnection,
    twin_id: str,
) -> dict[str, Any]:
    """Return edges with constraint_expr touching twin's target, plus gate_type/gate_status (OHM-josq)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin_row = conn.execute(
        "SELECT id, label, gate_type, gate_status FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    twin_data = {
        "twin_id": twin_row[0],
        "label": twin_row[1],
        "gate_type": twin_row[2],
        "gate_status": twin_row[3],
    }

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.constraint_expr, e.edge_type, e.confidence, e.probability
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    target_ids = [e["to_node"] for e in eval_edges]

    constraints: list[dict[str, Any]] = []
    if target_ids:
        placeholders = ",".join(["?"] * len(target_ids))
        constraint_edges = _rows_to_dicts(
            conn.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.constraint_expr, e.edge_type,
                       e.confidence, e.probability, e.layer
                FROM ohm_edges e
                WHERE (e.from_node IN ({placeholders}) OR e.to_node IN ({placeholders}))
                  AND e.constraint_expr IS NOT NULL
                  AND e.deleted_at IS NULL""",
                target_ids + target_ids,
            )
        )
        constraints = constraint_edges

    return {
        "twin": twin_data,
        "evaluates_edges": eval_edges,
        "constraints": constraints,
    }


def validate_action_against_twin(
    conn: DuckDBPyConnection,
    twin_id: str,
    action_id: str,
) -> dict[str, Any]:
    """Check whether an action violates any twin constraints (OHM-josq).

    Finds the twin's target node(s) via EVALUATES edges, then checks
    constraints on edges incident to those targets against the action's
    confidence. Returns ``{valid, violations}`` where *violations* is a
    list of dicts describing each constraint that the action breaches.
    """
    import json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")
    action_id = validate_identifier(action_id, name="action_id")

    twin_row = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    action_row = conn.execute(
        "SELECT id, label, confidence FROM ohm_nodes WHERE id = ? AND type = 'action' AND deleted_at IS NULL",
        [action_id],
    ).fetchone()
    if not action_row:
        raise NodeNotFoundError(f"Action not found: {action_id}")

    action_confidence = action_row[2] if action_row[2] is not None else 0.5

    action_edge_rows = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.confidence, e.probability
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.deleted_at IS NULL""",
            [action_id],
        )
    )
    action_probability = None
    for ae in action_edge_rows:
        if ae.get("probability") is not None:
            action_probability = ae["probability"]
            break

    twin_targets = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )
    target_ids = [t["to_node"] for t in twin_targets]

    violations: list[dict[str, Any]] = []

    if target_ids:
        placeholders = ",".join(["?"] * len(target_ids))
        constraint_edges = _rows_to_dicts(
            conn.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.constraint_expr, e.confidence, e.probability
                FROM ohm_edges e
                WHERE (e.from_node IN ({placeholders}) OR e.to_node IN ({placeholders}))
                  AND e.constraint_expr IS NOT NULL
                  AND e.deleted_at IS NULL""",
                target_ids + target_ids,
            )
        )

        for ce in constraint_edges:
            expr = ce.get("constraint_expr", "")
            if not expr:
                continue
            try:
                parsed = json.loads(expr) if expr.startswith("{") else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}

            max_confidence = parsed.get("max_confidence")
            probability_range = parsed.get("probability_range")

            if max_confidence is not None and action_confidence > float(max_confidence):
                violations.append(
                    {
                        "constraint_edge_id": ce["id"],
                        "constraint_expr": expr,
                        "violation_type": "max_confidence_exceeded",
                        "action_confidence": action_confidence,
                        "max_confidence": float(max_confidence),
                    }
                )

            if probability_range is not None and action_probability is not None:
                p_min = probability_range.get("min")
                p_max = probability_range.get("max")
                if p_min is not None and action_probability < float(p_min):
                    violations.append(
                        {
                            "constraint_edge_id": ce["id"],
                            "constraint_expr": expr,
                            "violation_type": "probability_below_min",
                            "action_probability": action_probability,
                            "min_probability": float(p_min),
                        }
                    )
                if p_max is not None and action_probability > float(p_max):
                    violations.append(
                        {
                            "constraint_edge_id": ce["id"],
                            "constraint_expr": expr,
                            "violation_type": "probability_above_max",
                            "action_probability": action_probability,
                            "max_probability": float(p_max),
                        }
                    )

    return {
        "valid": len(violations) == 0,
        "violations": violations,
    }


def explain_twin(
    conn: DuckDBPyConnection,
    twin_id: str,
) -> dict[str, Any]:
    """Return human-readable explanation of what the twin models (OHM-josq)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin_row = conn.execute(
        "SELECT id, label, url FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node, n.label AS target_label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    target_node_id = eval_edges[0]["to_node"] if eval_edges else None
    target_label = eval_edges[0].get("target_label", "") if eval_edges else ""

    constraint_count = 0
    if target_node_id:
        row = conn.execute(
            """SELECT COUNT(*) FROM ohm_edges
               WHERE (from_node = ? OR to_node = ?)
                 AND constraint_expr IS NOT NULL
                 AND deleted_at IS NULL""",
            [target_node_id, target_node_id],
        ).fetchone()
        constraint_count = row[0] if row else 0

    edge_count = len(eval_edges)

    summary = f"Twin '{twin_row[1]}' models node '{target_label}'"
    if edge_count > 1:
        summary += f" and {edge_count - 1} other node(s)"
    if twin_row[2]:
        summary += f" via {twin_row[2]}"
    if constraint_count:
        summary += f" with {constraint_count} constraint(s)"

    return {
        "twin_id": twin_id,
        "label": twin_row[1],
        "target_node_id": target_node_id,
        "target_label": target_label,
        "endpoint_url": twin_row[2],
        "constraint_count": constraint_count,
        "edge_count": edge_count,
        "summary": summary,
    }


# ── Twin Template Catalog (OHM-hl61) ──────────────────────────────────────────


def create_twin_template(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    created_by: str,
    constraint_schema: dict[str, Any] | None = None,
    required_edges: list[str] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a twin_template node linked via EVALUATES L3 to target (OHM-hl61).

    A twin template is a reusable primitive that agents can instantiate to
    create twin nodes. The constraint_schema (stored as JSON in metadata)
    defines the constraints that instantiated twins must satisfy. The
    required_edges list specifies which edge types must exist on the target
    for the template to be applicable.

    Args:
        conn: Database connection.
        label: Human-readable template name.
        target_node_id: The node this template models.
        created_by: Agent creating the template.
        constraint_schema: Optional dict of constraints for instantiated twins.
        required_edges: Optional list of edge types required on the target.
        description: Optional description of what the template models.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The created twin_template node record.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    template_metadata: dict[str, Any] = {}
    if constraint_schema is not None:
        template_metadata["constraint_schema"] = constraint_schema
    if required_edges is not None:
        template_metadata["required_edges"] = required_edges

    all_connects = [target_node_id] + (list(connects_to) if connects_to else [])

    template = create_node(
        conn,
        label=label,
        node_type="twin_template",
        content=description,
        created_by=created_by,
        metadata=template_metadata if template_metadata else None,
        connects_to=all_connects,
    )

    create_edge(
        conn,
        from_node=template["id"],
        to_node=target_node_id,
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
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=template["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    _log_change(conn, "ohm_nodes", template["id"], "CREATE_TWIN_TEMPLATE", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [template["id"]]))[0]


def list_twin_templates(
    conn: DuckDBPyConnection,
    *,
    target_node_id: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List twin_template nodes with optional filters (OHM-hl61).

    Args:
        conn: Database connection.
        target_node_id: Optional filter — only templates evaluating this node.
        created_by: Optional filter — only templates by this agent.
        limit: Maximum number of templates to return.

    Returns:
        List of twin_template node records.
    """
    from ohm.validation import validate_identifier

    conditions = ["n.type = 'twin_template'", "n.deleted_at IS NULL"]
    params: list[Any] = []

    if target_node_id is not None:
        target_node_id = validate_identifier(target_node_id, name="target_node_id")
        conditions.append(
            """EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES'
                  AND e.deleted_at IS NULL
            )"""
        )
        params.append(target_node_id)

    if created_by is not None:
        created_by = validate_identifier(created_by, name="created_by")
        conditions.append("n.created_by = ?")
        params.append(created_by)

    params.append(limit)

    where = " AND ".join(conditions)
    rows = _rows_to_dicts(
        conn.execute(
            f"""SELECT n.* FROM ohm_nodes n
            WHERE {where}
            ORDER BY n.created_at DESC
            LIMIT ?""",
            params,
        )
    )

    return rows


def get_twin_template(
    conn: DuckDBPyConnection,
    template_id: str,
) -> dict[str, Any]:
    """Get a twin_template node with its EVALUATES edges and metadata (OHM-hl61).

    Args:
        conn: Database connection.
        template_id: The twin_template node ID.

    Returns:
        Dict with template node, evaluates_edges, constraint_schema, required_edges.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    template_id = validate_identifier(template_id, name="template_id")

    template_row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
        [template_id],
    ).fetchone()
    if not template_row:
        raise NodeNotFoundError(f"Twin template not found: {template_id}")

    columns = [desc[0] for desc in conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [template_id]).description]
    template = dict(zip(columns, template_row))

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.confidence, e.probability,
                  n.label AS target_label, n.type AS target_type
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [template_id],
        )
    )

    constraint_schema = None
    required_edges = None
    metadata_raw = template.get("metadata")
    if metadata_raw:
        try:
            parsed = _json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
            constraint_schema = parsed.get("constraint_schema")
            required_edges = parsed.get("required_edges")
        except (_json.JSONDecodeError, TypeError):
            pass

    return {
        "template": template,
        "evaluates_edges": eval_edges,
        "constraint_schema": constraint_schema,
        "required_edges": required_edges,
    }


def instantiate_twin_from_template(
    conn: DuckDBPyConnection,
    *,
    template_id: str,
    target_node_id: str,
    created_by: str,
    label: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Instantiate a twin from a twin_template (OHM-hl61).

    Creates a ``twin`` node with gate_type='template' and an EVALUATES L3
    edge to target_node_id. Copies constraint_schema from the template
    metadata into the twin's metadata. If the template has required_edges,
    looks up edges of those types touching the target and creates
    corresponding EVALUATES edges from the twin to those connected nodes.

    Args:
        conn: Database connection.
        template_id: The twin_template to instantiate.
        target_node_id: The node the new twin will model.
        created_by: Agent instantiating the twin.
        label: Optional label for the twin (defaults to template label + " instance").
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The instantiated twin node record.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    template_id = validate_identifier(template_id, name="template_id")
    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    template_row = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
        [template_id],
    ).fetchone()
    if not template_row:
        raise NodeNotFoundError(f"Twin template not found: {template_id}")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    template_label = template_row[1]
    twin_label = label or f"{template_label} instance"

    twin_metadata: dict[str, Any] = {"source_template_id": template_id}
    template_metadata_raw = template_row[2]
    if template_metadata_raw:
        try:
            parsed = _json.loads(template_metadata_raw) if isinstance(template_metadata_raw, str) else template_metadata_raw
            if parsed.get("constraint_schema"):
                twin_metadata["constraint_schema"] = parsed["constraint_schema"]
        except (_json.JSONDecodeError, TypeError):
            pass

    all_connects = [target_node_id] + (list(connects_to) if connects_to else [])

    twin = create_node(
        conn,
        label=twin_label,
        node_type="twin",
        created_by=created_by,
        metadata=twin_metadata,
        connects_to=all_connects,
    )

    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'template' WHERE id = ?",
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

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=twin["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    if template_metadata_raw:
        try:
            parsed = _json.loads(template_metadata_raw) if isinstance(template_metadata_raw, str) else template_metadata_raw
            required = parsed.get("required_edges")
            if required and isinstance(required, list):
                for edge_type in required:
                    target_edges = _rows_to_dicts(
                        conn.execute(
                            """SELECT e.to_node FROM ohm_edges e
                           WHERE e.from_node = ? AND e.edge_type = ? AND e.deleted_at IS NULL""",
                            [target_node_id, edge_type],
                        )
                    )
                    for te in target_edges:
                        connected_node = te["to_node"]
                        already = conn.execute(
                            """SELECT 1 FROM ohm_edges
                               WHERE from_node = ? AND to_node = ? AND edge_type = 'EVALUATES'
                               AND deleted_at IS NULL""",
                            [twin["id"], connected_node],
                        ).fetchone()
                        if not already:
                            create_edge(
                                conn,
                                from_node=twin["id"],
                                to_node=connected_node,
                                edge_type="EVALUATES",
                                layer="L3",
                                created_by=created_by,
                            )
                    target_edges_in = _rows_to_dicts(
                        conn.execute(
                            """SELECT e.from_node FROM ohm_edges e
                           WHERE e.to_node = ? AND e.edge_type = ? AND e.deleted_at IS NULL""",
                            [target_node_id, edge_type],
                        )
                    )
                    for te in target_edges_in:
                        connected_node = te["from_node"]
                        already = conn.execute(
                            """SELECT 1 FROM ohm_edges
                               WHERE from_node = ? AND to_node = ? AND edge_type = 'EVALUATES'
                               AND deleted_at IS NULL""",
                            [twin["id"], connected_node],
                        ).fetchone()
                        if not already:
                            create_edge(
                                conn,
                                from_node=twin["id"],
                                to_node=connected_node,
                                edge_type="EVALUATES",
                                layer="L3",
                                created_by=created_by,
                            )
        except (_json.JSONDecodeError, TypeError):
            pass

    _log_change(conn, "ohm_nodes", twin["id"], "INSTANTIATE_TWIN", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]


# ── Twin Construction Engine (OHM-f7tl) ──────────────────────────────────────────


def _jaccard_similarity(a: str, b: str) -> float:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def assemble_twin_for_decision(
    conn: DuckDBPyConnection,
    *,
    decision_node_id: str,
    goal: str,
    horizon: int = 7,
    preferred_template_id: str | None = None,
    preferred_model_id: str | None = None,
    created_by: str,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Assemble a decision-specific twin from templates + primitives (OHM-f7tl).

    Algorithm:
    1. Resolve decision_node_id (the decision this twin will support).
    2. Find candidate twin_templates relevant to the decision's domain
       (match on goal keywords vs template.description / template.label).
    3. If preferred_template_id provided, use it; else pick highest-ranked.
    4. Find candidate models for that template's twin (from marketplace).
    5. If preferred_model_id provided, use it; else pick highest-scoring.
    6. Instantiate the twin from the chosen template.
    7. Register the chosen model as a model_candidate linked to the new twin.
    8. Return {twin, template, model, ranking, reasoning}.

    Ranking:
    - Template relevance: keyword overlap between goal and template metadata
      (description, label, required_edges), normalized 0-1.
    - Model score: latest model_evaluation.value (a probability in [0, 1])
      decayed by observation age when apply_decay=True. Default 0.5 (neutral)
      if no observation exists. A stale evaluation loses influence vs. a fresh
      one — both raw and decayed scores are returned so callers can audit.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    decision = conn.execute(
        "SELECT id, label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_node_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    decision_label = decision[1] or ""

    template_rows = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.content, n.metadata, n.created_by
           FROM ohm_nodes n
           WHERE n.type = 'twin_template' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC""",
        )
    )

    template_candidates: list[dict[str, Any]] = []
    for t in template_rows:
        desc = t.get("content") or ""
        label = t.get("label") or ""
        required_edges_raw = t.get("metadata")
        required_edges_str = ""
        if required_edges_raw:
            try:
                parsed = _json.loads(required_edges_raw) if isinstance(required_edges_raw, str) else required_edges_raw
                required_edges_str = " ".join(parsed.get("required_edges", []))
            except (_json.JSONDecodeError, TypeError):
                pass
        corpus = f"{label} {desc} {required_edges_str}"
        relevance = _jaccard_similarity(goal, corpus)
        boost = _jaccard_similarity(goal, f"{decision_label} {label}") * 0.25
        score = min(relevance + boost, 1.0)
        template_candidates.append(
            {
                "template_id": t["id"],
                "label": label,
                "relevance_score": round(score, 4),
            }
        )

    template_candidates.sort(key=lambda c: c["relevance_score"], reverse=True)

    chosen_template_id = preferred_template_id
    if chosen_template_id:
        chosen_template_id = validate_identifier(chosen_template_id, name="preferred_template_id")
        template_exists = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
            [chosen_template_id],
        ).fetchone()
        if not template_exists:
            raise NodeNotFoundError(f"Preferred template not found: {chosen_template_id}")
    elif template_candidates:
        chosen_template_id = template_candidates[0]["template_id"]

    model_candidates: list[dict[str, Any]] = []
    if chosen_template_id:
        template_eval_edges = _rows_to_dicts(
            conn.execute(
                """SELECT e.to_node, n.label, n.metadata
               FROM ohm_edges e
               JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
               WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
                [chosen_template_id],
            )
        )
        target_ids = [ee["to_node"] for ee in template_eval_edges]
        for tid in target_ids:
            twin_nodes = _rows_to_dicts(
                conn.execute(
                    """SELECT n.id, n.label, n.metadata
                   FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
                   WHERE n.type = 'twin' AND n.deleted_at IS NULL""",
                    [tid],
                )
            )
            for twin_node in twin_nodes:
                obs_rows = _rows_to_dicts(
                    conn.execute(
                        """SELECT o.value, o.created_at
                       FROM ohm_observations o
                       WHERE o.node_id = ? AND o.type = 'model_evaluation' AND o.deleted_at IS NULL
                       ORDER BY o.created_at DESC LIMIT 1""",
                        [twin_node["id"]],
                    )
                )
                score = 0.5
                decayed_score: float | None = None
                decay_info: dict | None = None
                if obs_rows:
                    val = obs_rows[0].get("value")
                    if val is not None:
                        score = float(val)
                        if apply_decay:
                            decay_info = compute_confidence_with_decay(
                                conn,
                                base_confidence=score,
                                last_observed_at=obs_rows[0].get("created_at"),
                                half_life_days=half_life_days,
                                floor=decay_floor,
                            )
                            decayed_score = decay_info["decayed_confidence"]
                if decayed_score is None:
                    decayed_score = score
                model_candidates.append(
                    {
                        "model_id": twin_node["id"],
                        "label": twin_node.get("label", ""),
                        "score": round(score, 4),
                        "decayed_score": round(decayed_score, 4) if decayed_score is not None else None,
                        "decay": decay_info,
                    }
                )

    # When apply_decay=True, rank by decayed_score (fresher observations win).
    # When False, rank by raw score (backward-compat).
    sort_key = (lambda c: c["decayed_score"] if c["decayed_score"] is not None else c["score"]) if apply_decay else (lambda c: c["score"])
    model_candidates.sort(key=sort_key, reverse=True)

    chosen_model_id = preferred_model_id
    if chosen_model_id:
        chosen_model_id = validate_identifier(chosen_model_id, name="preferred_model_id")
        model_exists = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
            [chosen_model_id],
        ).fetchone()
        if not model_exists:
            raise NodeNotFoundError(f"Preferred model not found: {chosen_model_id}")
    elif model_candidates:
        chosen_model_id = model_candidates[0]["model_id"]

    twin_result: dict[str, Any] | None = None
    template_result: dict[str, Any] | None = None
    model_result: dict[str, Any] | None = None

    if chosen_template_id:
        template_result = (
            _rows_to_dicts(
                conn.execute(
                    "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [chosen_template_id],
                )
            )[0]
            if _rows_to_dicts(
                conn.execute(
                    "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [chosen_template_id],
                )
            )
            else None
        )

        twin_result = instantiate_twin_from_template(
            conn,
            template_id=chosen_template_id,
            target_node_id=decision_node_id,
            created_by=created_by,
            label=f"Twin for {decision_label[:50]}",
            connects_to=[decision_node_id],
        )

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
            confidence=0.7,
        )

        if chosen_model_id:
            model_result = (
                _rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [chosen_model_id],
                    )
                )[0]
                if _rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [chosen_model_id],
                    )
                )
                else None
            )

            create_edge(
                conn,
                from_node=twin_result["id"],
                to_node=chosen_model_id,
                edge_type="APPLIES_TO",
                layer="L3",
                created_by=created_by,
                confidence=0.7,
            )
    else:
        twin_result = create_node(
            conn,
            label=f"Ad-hoc twin for {decision_label[:50]}",
            node_type="twin",
            content=f"Ad-hoc twin assembled for decision '{decision_label}' with goal: {goal}",
            created_by=created_by,
            connects_to=[decision_node_id],
        )

        conn.execute(
            "UPDATE ohm_nodes SET gate_type = 'ad_hoc' WHERE id = ?",
            [twin_result["id"]],
        )

        twin_result = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [twin_result["id"]],
            )
        )[0]

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="EVALUATES",
            layer="L3",
            created_by=created_by,
        )

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
            confidence=0.5,
        )

    template_desc = ""
    if chosen_template_id:
        t_row = conn.execute(
            "SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [chosen_template_id],
        ).fetchone()
        if t_row:
            template_desc = t_row[0]

    model_desc = ""
    if chosen_model_id:
        m_row = conn.execute(
            "SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [chosen_model_id],
        ).fetchone()
        if m_row:
            model_desc = m_row[0]

    if chosen_template_id and chosen_model_id:
        tpl_score = template_candidates[0]["relevance_score"] if template_candidates else "N/A"
        model_score = model_candidates[0]["score"] if model_candidates else "N/A"
        reasoning = f"Selected template '{template_desc}' (relevance {tpl_score}) and model '{model_desc}' (score {model_score}) for decision '{decision_label}' with goal: {goal}"
    elif chosen_template_id:
        tpl_score = template_candidates[0]["relevance_score"] if template_candidates else "N/A"
        reasoning = f"Selected template '{template_desc}' (relevance {tpl_score}) for decision '{decision_label}' with goal: {goal}. No model candidates available."
    else:
        reasoning = f"No templates available for decision '{decision_label}' with goal: {goal}. Created ad-hoc twin."

    _log_change(conn, "ohm_nodes", twin_result["id"], "ASSEMBLE_TWIN", created_by)

    return {
        "twin": twin_result,
        "template": template_result,
        "model": model_result,
        "ranking": {
            "template_candidates": template_candidates,
            "model_candidates": model_candidates,
        },
        "reasoning": reasoning,
    }


