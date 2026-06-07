"""Layer promotion and edge-level constraint validation (ADR-022).

SHACL-like write gates for L0→L1→L1→L3→L4 layer transitions.
Constraint satisfaction is computed from existing graph data, not stored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Layer Promotion Constraints ────────────────────────────────────────────

PROMOTION_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "L0_to_L1": {
        "min_context_links": 1,
        "require_promotion_action": True,
    },
    "L1_to_L2": {
        "min_sources": 1,
        "source_must_have_url": True,
        "min_observations": 1,
    },
    "L2_to_L3": {
        "min_sources": 2,
        "min_independent_agents": 1,
        "min_observations": 2,
        "min_outcomes": 1,
        "min_chain_validity": 0.3,
        "require_references_edge": True,
    },
    "L3_to_L4": {
        "min_L3_support": 3,
        "min_verified_outcomes": 2,
        "min_chain_validity": 0.5,
        "no_open_challenges": True,
    },
}

EDGE_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "CAUSES": {
        "min_layer": "L2",
        "require_references": True,
        "require_outcome": False,
    },
    "PREDICTS": {
        "min_layer": "L3",
        "require_references": True,
        "require_outcome": True,
    },
    "CHALLENGED_BY": {
        "require_confidence": True,
        "require_reasoning": True,
    },
    "SUPPORTS": {
        "min_layer": "L1",
        "require_references": False,
    },
}


# ── Helper functions ───────────────────────────────────────────────────────


def count_context_links(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(*) FROM ohm_edges
           WHERE (from_node = ? OR to_node = ?)
             AND layer = 'L0'
             AND deleted_at IS NULL""",
        [node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def count_sources(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(DISTINCT n.id) FROM ohm_nodes n
           JOIN ohm_edges e ON (e.from_node = ? OR e.to_node = ?)
             AND (n.id = e.from_node OR n.id = e.to_node)
             AND n.id != ?
           WHERE n.type = 'source'
             AND n.deleted_at IS NULL
             AND e.deleted_at IS NULL""",
        [node_id, node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def count_sources_with_url(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(DISTINCT n.id) FROM ohm_nodes n
           JOIN ohm_edges e ON (e.from_node = ? OR e.to_node = ?)
             AND (n.id = e.from_node OR n.id = e.to_node)
             AND n.id != ?
           WHERE n.type = 'source'
             AND n.deleted_at IS NULL
             AND e.deleted_at IS NULL
             AND n.url IS NOT NULL
             AND n.url != ''""",
        [node_id, node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def count_independent_sources(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(DISTINCT e.created_by) FROM ohm_edges e
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.deleted_at IS NULL
             AND e.created_by != ''""",
        [node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def count_observations(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        "SELECT COUNT(*) FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    return result[0] if result else 0


def count_outcomes(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        "SELECT COUNT(*) FROM ohm_outcomes WHERE claim_node = ?",
        [node_id],
    ).fetchone()
    return result[0] if result else 0


def count_verified_outcomes(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        "SELECT COUNT(*) FROM ohm_outcomes WHERE claim_node = ? AND outcome = TRUE",
        [node_id],
    ).fetchone()
    return result[0] if result else 0


def count_open_challenges(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(*) FROM ohm_edges
           WHERE (from_node = ? OR to_node = ?)
             AND edge_type = 'CHALLENGED_BY'
             AND deleted_at IS NULL
             AND challenge_type IS NULL""",
        [node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def count_L3_supporting_nodes(conn: DuckDBPyConnection, node_id: str) -> int:
    result = conn.execute(
        """SELECT COUNT(DISTINCT CASE WHEN e.from_node = ? THEN e.to_node ELSE e.from_node END)
           FROM ohm_edges e
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.from_node != ?
             AND e.deleted_at IS NULL
             AND (e.layer = 'L3' OR e.layer = 'L2')""",
        [node_id, node_id, node_id, node_id],
    ).fetchone()
    return result[0] if result else 0


def check_requires_references_edge(conn: DuckDBPyConnection, node_id: str) -> bool:
    result = conn.execute(
        """SELECT COUNT(*) FROM ohm_edges
           WHERE (from_node = ? OR to_node = ?)
             AND edge_type = 'REFERENCES'
             AND layer = 'L2'
             AND deleted_at IS NULL""",
        [node_id, node_id],
    ).fetchone()
    return (result[0] if result else 0) > 0


def chain_validity(conn: DuckDBPyConnection, node_id: str, t: str | None = None) -> float:
    if t:
        rows = conn.execute(
            """SELECT COALESCE(o.value, 0.5) as conf
               FROM ohm_observations o
               WHERE o.node_id = ? AND o.deleted_at IS NULL
                 AND (o.valid_to IS NULL OR o.valid_to > ?::TIMESTAMP)""",
            [node_id, t],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT COALESCE(o.value, 0.5) as conf FROM ohm_observations o WHERE o.node_id = ? AND o.deleted_at IS NULL",
            [node_id],
        ).fetchall()
    if not rows:
        return 0.0
    return min(r[0] for r in rows)


# ── Constraint dispatch ────────────────────────────────────────────────────


CONSTRAINT_DISPATCH: dict[str, Any] = {
    "min_context_links": count_context_links,
    "min_sources": count_sources,
    "source_must_have_url": lambda conn, nid: count_sources_with_url(conn, nid) > 0,
    "min_observations": count_observations,
    "min_independent_agents": count_independent_sources,
    "min_outcomes": count_outcomes,
    "min_verified_outcomes": count_verified_outcomes,
    "min_chain_validity": chain_validity,
    "require_references_edge": check_requires_references_edge,
    "min_L3_support": count_L3_supporting_nodes,
    "no_open_challenges": lambda conn, nid: count_open_challenges(conn, nid) == 0,
    "require_promotion_action": lambda conn, nid: True,
}


def compute_constraint(
    conn: DuckDBPyConnection, node_id: str, constraint_name: str
) -> Any:
    handler = CONSTRAINT_DISPATCH.get(constraint_name)
    if handler is None:
        return None
    return handler(conn, node_id)


# ── Validation functions ──────────────────────────────────────────────────


def validate_layer_promotion(
    node_id: str,
    current_layer: str,
    target_layer: str,
    conn: DuckDBPyConnection,
    enforce: bool = False,
) -> tuple[bool, list[str], list[str]]:
    transition_key = f"{current_layer}_to_{target_layer}"
    constraints = PROMOTION_CONSTRAINTS.get(transition_key, {})
    if not constraints:
        return True, [], []

    warnings: list[str] = []
    errors: list[str] = []

    for constraint_name, threshold in constraints.items():
        value = compute_constraint(conn, node_id, constraint_name)
        if value is None:
            continue

        if isinstance(threshold, bool):
            satisfied = bool(value) == threshold
        elif isinstance(threshold, (int, float)):
            satisfied = bool(value is not None and value >= threshold)
        else:
            satisfied = bool(value == threshold)

        if not satisfied:
            msg = f"{constraint_name}: {value} < {threshold} (required for {transition_key})"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

    valid = len(errors) == 0
    return valid, warnings, errors


def validate_edge_constraints(
    edge_type: str,
    layer: str,
    conn: DuckDBPyConnection,
    from_node: str | None = None,
    confidence: float | None = None,
    enforce: bool = False,
) -> tuple[bool, list[str], list[str]]:
    constraints = EDGE_CONSTRAINTS.get(edge_type, {})
    if not constraints:
        return True, [], []

    warnings: list[str] = []
    errors: list[str] = []

    min_layer = constraints.get("min_layer")
    if min_layer:
        layer_order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
        if layer_order.get(layer, 0) < layer_order.get(min_layer, 0):
            msg = f"Edge type '{edge_type}' requires layer >= {min_layer}, got {layer}"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

    if constraints.get("require_references") and from_node:
        has_ref = check_requires_references_edge(conn, from_node)
        if not has_ref:
            msg = f"Edge type '{edge_type}' requires at least one L2 REFERENCES edge on the source node"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

    if constraints.get("require_outcome") and from_node:
        outcomes = count_outcomes(conn, from_node)
        if outcomes < 1:
            msg = f"Edge type '{edge_type}' requires at least one outcome on the source node"
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

    if constraints.get("require_confidence") and confidence is None:
        msg = f"Edge type '{edge_type}' requires a confidence value"
        if enforce:
            errors.append(msg)
        else:
            warnings.append(msg)

    valid = len(errors) == 0
    return valid, warnings, errors


# ── Effective Layer ───────────────────────────────────────────────────────


def effective_layer(
    conn: DuckDBPyConnection, node_id: str, t: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Compute the effective layer of a node based on its edges and type.

    Nodes don't have a stored layer column — the layer is inferred from
    the maximum layer of incident edges, or from node type for special cases.

    Returns:
        Tuple of (effective_layer, constraint_status dict).
    """
    node = conn.execute(
        "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node:
        return "unknown", {}

    node_type = node[0]

    # Fragments are always L0
    if node_type == "fragment":
        return "L0", _build_constraint_status(conn, node_id, "L0", t)

    # Source nodes are always L1 (foundational references)
    if node_type == "source":
        return "L1", _build_constraint_status(conn, node_id, "L1", t)

    # Infer layer from the maximum layer of incident edges
    result = conn.execute(
        """SELECT COALESCE(MAX(
            CASE layer
                WHEN 'L0' THEN 0 WHEN 'L1' THEN 1
                WHEN 'L2' THEN 2 WHEN 'L3' THEN 3
                WHEN 'L4' THEN 4
                ELSE 0 END
        ), 0) FROM ohm_edges
           WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL""",
        [node_id, node_id],
    ).fetchone()
    inferred_level = result[0] if result else 0
    level_to_layer = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4"}
    original_layer = level_to_layer.get(inferred_level, "L1")

    if original_layer in ("L0", "L1"):
        return original_layer, _build_constraint_status(conn, node_id, original_layer, t)

    if original_layer == "L2":
        return original_layer, _build_constraint_status(conn, node_id, original_layer, t)

    cv = chain_validity(conn, node_id, t)

    if original_layer == "L3":
        sources = count_sources(conn, node_id)
        outcomes = count_verified_outcomes(conn, node_id)
        challenges = count_open_challenges(conn, node_id)

        if cv >= 0.3 and sources >= 2 and outcomes >= 1 and challenges == 0:
            effective = "L3"
        elif cv >= 0.1 and sources >= 1:
            effective = "L2"
        else:
            effective = "L1"

        return effective, _build_constraint_status(conn, node_id, original_layer, t)

    if original_layer == "L4":
        support = count_L3_supporting_nodes(conn, node_id)
        outcomes = count_verified_outcomes(conn, node_id)
        challenges = count_open_challenges(conn, node_id)

        if cv >= 0.5 and support >= 3 and outcomes >= 2 and challenges == 0:
            effective = "L4"
        elif cv >= 0.3 and support >= 2 and outcomes >= 1:
            effective = "L3"
        elif cv >= 0.1:
            effective = "L2"
        else:
            effective = "L1"

        return effective, _build_constraint_status(conn, node_id, original_layer, t)

    return original_layer, {}


def _build_constraint_status(
    conn: DuckDBPyConnection, node_id: str, layer: str, t: str | None = None
) -> dict[str, Any]:
    status: dict[str, Any] = {}
    layer_display = layer.replace("L", "L")

    transitions = {
        "L0": "L0_to_L1",
        "L1": "L1_to_L2",
        "L2": "L2_to_L3",
        "L3": "L3_to_L4",
    }

    for check_layer, trans_key in transitions.items():
        constraints = PROMOTION_CONSTRAINTS.get(trans_key, {})
        skip = False
        if layer == "L0" and check_layer != "L0":
            skip = True
        if layer == "L1" and check_layer not in ("L0", "L1"):
            skip = True
        if layer == "L2" and check_layer == "L3":
            skip = True
        if layer == "L3" and check_layer == "L4":
            skip = True

        if skip:
            continue

        group_key = f"{check_layer}_requirements"
        group: dict[str, Any] = {}
        for cname, threshold in constraints.items():
            value = compute_constraint(conn, node_id, cname)
            if isinstance(threshold, bool):
                satisfied = bool(value) == threshold
            elif isinstance(threshold, (int, float)):
                satisfied = bool(value is not None and value >= threshold)
            else:
                satisfied = bool(value == threshold)
            group[cname] = {
                "required": threshold,
                "actual": value,
                "satisfied": satisfied,
            }
        if group:
            status[group_key] = group

    return status
