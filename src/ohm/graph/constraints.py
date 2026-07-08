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
        "participates_in_inference": True,
    },
    "PREDICTS": {
        "min_layer": "L3",
        "require_references": True,
        "require_outcome": True,
        "participates_in_inference": True,
    },
    "EXPECTS": {
        "min_layer": "L3",
        "require_references": False,
        "require_outcome": False,
        "participates_in_inference": True,
    },
    "INFLUENCES": {
        "min_layer": "L2",
        "require_references": False,
        "require_outcome": False,
        "participates_in_inference": True,
    },
    "CORRELATES_WITH": {
        "min_layer": "L3",
        "require_references": False,
        "require_outcome": False,
        "participates_in_inference": True,
    },
    "CHALLENGED_BY": {
        "require_confidence": True,
        "require_reasoning": True,
        "participates_in_inference": False,
    },
    "SUPPORTS": {
        "min_layer": "L1",
        "require_references": False,
        "participates_in_inference": False,
    },
    "REFINES": {
        "min_layer": "L3",
        "require_references": False,
        "participates_in_inference": False,
    },
    "EXPLAINS": {
        "min_layer": "L3",
        "require_references": False,
        "participates_in_inference": False,
    },
    "REFERENCES": {
        "min_layer": "L2",
        "require_references": False,
        "participates_in_inference": False,
    },
    "RELATED_TO": {
        "min_layer": "L3",
        "require_references": False,
        "participates_in_inference": False,
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


def compute_constraint(conn: DuckDBPyConnection, node_id: str, constraint_name: str) -> Any:
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
    condition: str | None = None,
    provenance: str | None = None,
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

    # OHM-e0t1: require_reasoning was declared but never enforced.
    # Now it is: the lint guard at write time (require_challenge_reason)
    # handles the case for CHALLENGED_BY writes; this check covers the
    # constraint when validate_edge_constraints is called with enforce=True
    # for any edge type that declares require_reasoning. A reason is
    # present if either condition or provenance is non-empty.
    if constraints.get("require_reasoning"):
        cond = (condition or "").strip()
        prov = (provenance or "").strip()
        if not cond and not prov:
            msg = f"Edge type '{edge_type}' requires a reason (condition or provenance must be non-empty) — see ADR-018 / OHM-e0t1."
            if enforce:
                errors.append(msg)
            else:
                warnings.append(msg)

    valid = len(errors) == 0
    return valid, warnings, errors


# ── Effective Layer ───────────────────────────────────────────────────────


def effective_layer(conn: DuckDBPyConnection, node_id: str, t: str | None = None) -> tuple[str, dict[str, Any]]:
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


def effective_layers(conn: DuckDBPyConnection, node_ids: list[str], t: str | None = None) -> dict[str, str]:
    """Batch version of effective_layer() for a list of node IDs.

    Returns only the effective layer string for each node (not the full
    constraint_status dict). This is intended for callers such as
    /neighborhood that need effective_layer on many nodes and can avoid
    the N+1 query cost of calling effective_layer() individually.

    The algorithm mirrors effective_layer():
      - fragment -> L0
      - source -> L1
      - L0/L1/L2 -> original layer
      - L3/L4 -> original layer gated by chain_validity, sources,
        verified outcomes, open challenges, and L3 support counts.
    """
    if not node_ids:
        return {}

    placeholders = ", ".join("?" for _ in node_ids)

    # 1. Node types
    node_types = {}
    rows = conn.execute(
        f"SELECT id, type FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        node_ids,
    ).fetchall()
    for nid, ntype in rows:
        node_types[nid] = ntype

    # 2. Max incident edge layer per node
    max_levels: dict[str, int] = {}
    rows = conn.execute(
        f"""
        SELECT node_id, MAX(level) AS max_level FROM (
            SELECT from_node AS node_id,
                   CASE layer
                       WHEN 'L0' THEN 0 WHEN 'L1' THEN 1
                       WHEN 'L2' THEN 2 WHEN 'L3' THEN 3
                       WHEN 'L4' THEN 4 ELSE 0 END AS level
            FROM ohm_edges
            WHERE from_node IN ({placeholders}) AND deleted_at IS NULL
            UNION ALL
            SELECT to_node AS node_id,
                   CASE layer
                       WHEN 'L0' THEN 0 WHEN 'L1' THEN 1
                       WHEN 'L2' THEN 2 WHEN 'L3' THEN 3
                       WHEN 'L4' THEN 4 ELSE 0 END AS level
            FROM ohm_edges
            WHERE to_node IN ({placeholders}) AND deleted_at IS NULL
        ) GROUP BY node_id
        """,
        node_ids + node_ids,
    ).fetchall()
    for nid, lvl in rows:
        max_levels[nid] = lvl

    # 3. Sources per node
    sources: dict[str, int] = {nid: 0 for nid in node_ids}
    rows = conn.execute(
        f"""
        SELECT target_id, COUNT(DISTINCT source_id) AS cnt FROM (
            SELECT e.from_node AS target_id, e.to_node AS source_id
            FROM ohm_edges e
            JOIN ohm_nodes n ON n.id = e.to_node AND n.type = 'source' AND n.deleted_at IS NULL
            WHERE e.from_node IN ({placeholders}) AND e.deleted_at IS NULL AND n.id != e.from_node
            UNION ALL
            SELECT e.to_node AS target_id, e.from_node AS source_id
            FROM ohm_edges e
            JOIN ohm_nodes n ON n.id = e.from_node AND n.type = 'source' AND n.deleted_at IS NULL
            WHERE e.to_node IN ({placeholders}) AND e.deleted_at IS NULL AND n.id != e.to_node
        ) GROUP BY target_id
        """,
        node_ids + node_ids,
    ).fetchall()
    for nid, cnt in rows:
        sources[nid] = cnt

    # 4. Verified outcomes per node
    verified_outcomes: dict[str, int] = {nid: 0 for nid in node_ids}
    rows = conn.execute(
        f"""
        SELECT claim_node, COUNT(*) AS cnt
        FROM ohm_outcomes
        WHERE claim_node IN ({placeholders}) AND outcome = TRUE
        GROUP BY claim_node
        """,
        node_ids,
    ).fetchall()
    for nid, cnt in rows:
        verified_outcomes[nid] = cnt

    # 5. Open challenges per node
    challenges: dict[str, int] = {nid: 0 for nid in node_ids}
    rows = conn.execute(
        f"""
        SELECT target_id, COUNT(*) AS cnt FROM (
            SELECT from_node AS target_id FROM ohm_edges
            WHERE from_node IN ({placeholders}) AND edge_type = 'CHALLENGED_BY'
              AND deleted_at IS NULL AND challenge_type IS NULL
            UNION ALL
            SELECT to_node AS target_id FROM ohm_edges
            WHERE to_node IN ({placeholders}) AND edge_type = 'CHALLENGED_BY'
              AND deleted_at IS NULL AND challenge_type IS NULL
        ) GROUP BY target_id
        """,
        node_ids + node_ids,
    ).fetchall()
    for nid, cnt in rows:
        challenges[nid] = cnt

    # 6. L2/L3 supporting nodes per node
    support: dict[str, int] = {nid: 0 for nid in node_ids}
    rows = conn.execute(
        f"""
        SELECT target_id, COUNT(DISTINCT peer_id) AS cnt FROM (
            SELECT e.from_node AS target_id, e.to_node AS peer_id
            FROM ohm_edges e
            WHERE e.from_node IN ({placeholders})
              AND e.deleted_at IS NULL
              AND (e.layer = 'L3' OR e.layer = 'L2')
              AND e.to_node != e.from_node
            UNION ALL
            SELECT e.to_node AS target_id, e.from_node AS peer_id
            FROM ohm_edges e
            WHERE e.to_node IN ({placeholders})
              AND e.deleted_at IS NULL
              AND (e.layer = 'L3' OR e.layer = 'L2')
              AND e.from_node != e.to_node
        ) GROUP BY target_id
        """,
        node_ids + node_ids,
    ).fetchall()
    for nid, cnt in rows:
        support[nid] = cnt

    # 7. Chain validity (min observation confidence) per node
    chain_validities: dict[str, float] = {nid: 0.0 for nid in node_ids}
    if t:
        rows = conn.execute(
            f"""
            SELECT node_id, MIN(COALESCE(value, 0.5)) AS cv
            FROM ohm_observations
            WHERE node_id IN ({placeholders}) AND deleted_at IS NULL
              AND (valid_to IS NULL OR valid_to > ?::TIMESTAMP)
            GROUP BY node_id
            """,
            node_ids + [t],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT node_id, MIN(COALESCE(value, 0.5)) AS cv
            FROM ohm_observations
            WHERE node_id IN ({placeholders}) AND deleted_at IS NULL
            GROUP BY node_id
            """,
            node_ids,
        ).fetchall()
    for nid, cv in rows:
        chain_validities[nid] = cv if cv is not None else 0.0

    level_map = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4"}
    result: dict[str, str] = {}
    for nid in node_ids:
        ntype = node_types.get(nid)
        if ntype is None:
            result[nid] = "unknown"
            continue
        if ntype == "fragment":
            result[nid] = "L0"
            continue
        if ntype == "source":
            result[nid] = "L1"
            continue

        original_layer = level_map.get(max_levels.get(nid, 0), "L1")
        if original_layer in ("L0", "L1", "L2"):
            result[nid] = original_layer
            continue

        cv = chain_validities.get(nid, 0.0)
        src = sources.get(nid, 0)
        out = verified_outcomes.get(nid, 0)
        ch = challenges.get(nid, 0)

        if original_layer == "L3":
            if cv >= 0.3 and src >= 2 and out >= 1 and ch == 0:
                result[nid] = "L3"
            elif cv >= 0.1 and src >= 1:
                result[nid] = "L2"
            else:
                result[nid] = "L1"
        elif original_layer == "L4":
            sup = support.get(nid, 0)
            if cv >= 0.5 and sup >= 3 and out >= 2 and ch == 0:
                result[nid] = "L4"
            elif cv >= 0.3 and sup >= 2 and out >= 1:
                result[nid] = "L3"
            elif cv >= 0.1:
                result[nid] = "L2"
            else:
                result[nid] = "L1"
        else:
            result[nid] = original_layer

    return result


def _build_constraint_status(conn: DuckDBPyConnection, node_id: str, layer: str, t: str | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {}
    layer.replace("L", "L")

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


# ── Batch Constraint Computation (OHM-3ngi optimization) ──────────────────


def batch_constraint_report(
    conn: DuckDBPyConnection,
) -> dict[str, Any]:
    """Compute constraint satisfaction for ALL nodes in a single batch.

    Instead of calling effective_layer() + compute_constraint() per node
    (O(N*M) queries for N nodes and M constraints), this computes all
    metrics in a handful of aggregate SQL queries and then assembles
    the constraint report.

    Returns the same structure as _get_admin_constraint_report but ~100x faster.
    """
    # 1. Batch compute per-node metrics via aggregate queries
    #
    # For each node, we need:
    #   - type (for fragment/source shortcuts)
    #   - max layer from incident edges (original_layer)
    #   - count of context links (L0 edges)
    #   - count of sources (connected source-type nodes)
    #   - count of sources with URL
    #   - count of independent agents (distinct created_by on edges)
    #   - count of observations
    #   - count of outcomes
    #   - count of verified outcomes
    #   - count of open challenges
    #   - count of L3/L2 supporting nodes
    #   - count of REFERENCES edges at L2

    # Base node types
    nodes = conn.execute("SELECT id, type FROM ohm_nodes WHERE deleted_at IS NULL").fetchall()
    node_types = {n[0]: n[1] for n in nodes}

    # Max layer from incident edges
    edge_layers = conn.execute("""
        SELECT n.id AS node_id,
               COALESCE(MAX(
                   CASE e.layer
                       WHEN 'L0' THEN 0 WHEN 'L1' THEN 1
                       WHEN 'L2' THEN 2 WHEN 'L3' THEN 3
                       WHEN 'L4' THEN 4 ELSE 0 END
               ), 0) AS max_level
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_max_levels = {row[0]: row[1] for row in edge_layers}

    # Context links (L0 edges)
    context_links = conn.execute("""
        SELECT n.id AS node_id, COUNT(e.id) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.layer = 'L0'
            AND e.deleted_at IS NULL
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_context_links = {row[0]: row[1] for row in context_links}

    # Observations per node
    obs_counts = conn.execute("""
        SELECT node_id, COUNT(*) AS cnt
        FROM ohm_observations
        WHERE deleted_at IS NULL
        GROUP BY node_id
    """).fetchall()
    node_obs = {row[0]: row[1] for row in obs_counts}

    # Outcomes per node
    outcome_counts = conn.execute("""
        SELECT claim_node, COUNT(*) AS total,
               SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS verified
        FROM ohm_outcomes
        GROUP BY claim_node
    """).fetchall()
    node_outcomes = {row[0]: row[1] for row in outcome_counts}
    node_verified_outcomes = {row[0]: row[2] for row in outcome_counts}

    # Open challenges per node
    challenge_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(e.id) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.edge_type = 'CHALLENGED_BY'
            AND e.deleted_at IS NULL
            AND e.challenge_type IS NULL
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_challenges = {row[0]: row[1] for row in challenge_counts}

    # REFERENCES edges at L2 per node
    ref_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(e.id) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.edge_type = 'REFERENCES'
            AND e.layer = 'L2'
            AND e.deleted_at IS NULL
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_refs = {row[0]: row[1] for row in ref_counts}

    # Sources per node (connected source-type nodes)
    source_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(DISTINCT n2.id) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
        LEFT JOIN ohm_nodes n2 ON (n2.id = CASE WHEN e.from_node = n.id THEN e.to_node ELSE e.from_node END)
            AND n2.type = 'source'
            AND n2.deleted_at IS NULL
        WHERE n.deleted_at IS NULL
          AND n2.id IS NOT NULL
          AND n2.id != n.id
        GROUP BY n.id
    """).fetchall()
    node_sources = {row[0]: row[1] for row in source_counts}

    # Sources with URL
    source_url_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(DISTINCT n2.id) AS cnt
        FROM ohm_nodes n
        JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
        JOIN ohm_nodes n2 ON (n2.id = CASE WHEN e.from_node = n.id THEN e.to_node ELSE e.from_node END)
            AND n2.type = 'source'
            AND n2.deleted_at IS NULL
            AND n2.url IS NOT NULL AND n2.url != ''
        WHERE n.deleted_at IS NULL
          AND n.id != n2.id
        GROUP BY n.id
    """).fetchall()
    node_sources_with_url = {row[0]: row[1] for row in source_url_counts}

    # Independent agents per node
    agent_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(DISTINCT e.created_by) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
            AND e.created_by != ''
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_agents = {row[0]: row[1] for row in agent_counts}

    # L3/L2 supporting nodes per node
    support_counts = conn.execute("""
        SELECT n.id AS node_id, COUNT(DISTINCT CASE WHEN e.from_node = n.id THEN e.to_node ELSE e.from_node END) AS cnt
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
            AND (e.layer = 'L3' OR e.layer = 'L2')
            AND e.from_node != n.id
        WHERE n.deleted_at IS NULL
        GROUP BY n.id
    """).fetchall()
    node_support = {row[0]: row[1] for row in support_counts}

    # 2. Determine effective layer for each node
    level_map = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4"}
    node_effective_layers = {}
    for node_id, node_type in node_types.items():
        if node_type == "fragment":
            node_effective_layers[node_id] = "L0"
            continue
        if node_type == "source":
            node_effective_layers[node_id] = "L1"
            continue

        max_level = node_max_levels.get(node_id, 0)
        original_layer = level_map.get(max_level, "L1")

        # For L0/L1/L2, use original layer
        if original_layer in ("L0", "L1", "L2"):
            node_effective_layers[node_id] = original_layer
            continue

        # For L3/L4, compute effective layer based on constraints
        # (simplified — doesn't compute chain_validity per node in batch)
        sources = node_sources.get(node_id, 0)
        outcomes = node_verified_outcomes.get(node_id, 0)
        challenges = node_challenges.get(node_id, 0)

        if original_layer == "L3":
            if sources >= 2 and outcomes >= 1 and challenges == 0:
                node_effective_layers[node_id] = "L3"
            elif sources >= 1:
                node_effective_layers[node_id] = "L2"
            else:
                node_effective_layers[node_id] = "L1"
        elif original_layer == "L4":
            support = node_support.get(node_id, 0)
            if support >= 3 and outcomes >= 2 and challenges == 0:
                node_effective_layers[node_id] = "L4"
            elif support >= 2 and outcomes >= 1:
                node_effective_layers[node_id] = "L3"
            elif node_obs.get(node_id, 0) >= 1:
                node_effective_layers[node_id] = "L2"
            else:
                node_effective_layers[node_id] = "L1"
        else:
            node_effective_layers[node_id] = original_layer

    # 3. Build the constraint report
    layers = {
        "L0": {"total": 0, "satisfied": {}, "violations": {}},
        "L1": {"total": 0, "satisfied": {}, "violations": {}},
        "L2": {"total": 0, "satisfied": {}, "violations": {}},
        "L3": {"total": 0, "satisfied": {}, "violations": {}},
        "L4": {"total": 0, "satisfied": {}, "violations": {}},
    }

    for node_id, eff in node_effective_layers.items():
        layers[eff]["total"] += 1

    # Compute constraint satisfaction per transition
    transition_map = {
        "L0_to_L1": "L0",
        "L1_to_L2": "L1",
        "L2_to_L3": "L2",
        "L3_to_L4": "L3",
    }

    # Constraint value lookup per node (pre-computed)
    def get_metric(nid, cname):
        if cname == "min_context_links":
            return node_context_links.get(nid, 0)
        elif cname == "min_sources":
            return node_sources.get(nid, 0)
        elif cname == "min_sources_with_url":
            return node_sources_with_url.get(nid, 0)
        elif cname == "min_observations":
            return node_obs.get(nid, 0)
        elif cname == "min_outcomes":
            return node_outcomes.get(nid, 0)
        elif cname == "min_verified_outcomes":
            return node_verified_outcomes.get(nid, 0)
        elif cname == "min_independent_agents":
            return node_agents.get(nid, 0)
        elif cname == "no_open_challenges":
            return node_challenges.get(nid, 0) == 0
        elif cname == "requires_references_edge":
            return node_refs.get(nid, 0) > 0
        elif cname == "min_chain_validity":
            # Batch report doesn't compute chain_validity per node — too expensive
            # Return None to indicate it was skipped
            return None
        elif cname == "min_L3_support":
            return node_support.get(nid, 0)
        else:
            return None

    for trans_key, src_layer in transition_map.items():
        constraints = PROMOTION_CONSTRAINTS.get(trans_key, {})
        if not constraints:
            continue
        for cname, threshold in constraints.items():
            total = 0
            satisfied = 0
            for node_id, eff in node_effective_layers.items():
                if eff != src_layer:
                    continue
                total += 1
                value = get_metric(node_id, cname)
                if value is None:
                    continue
                if isinstance(threshold, bool):
                    if bool(value) == threshold:
                        satisfied += 1
                elif isinstance(threshold, (int, float)):
                    if value is not None and value >= threshold:
                        satisfied += 1
                else:
                    if value == threshold:
                        satisfied += 1
            if total > 0:
                rate = round(satisfied / total * 100, 1)
                layers[src_layer]["satisfied"][cname] = {
                    "satisfied": satisfied,
                    "total": total,
                    "rate_pct": rate,
                }
                layers[src_layer]["violations"][cname] = total - satisfied

    total_nodes = sum(layer["total"] for layer in layers.values())
    total_violations = sum(sum(layer["violations"].values()) for layer in layers.values())

    return {
        "constraint_report": layers,
        "summary": {
            "total_nodes": total_nodes,
            "total_violations": total_violations,
            "enforcement_mode": "advisory",
            "note": "Run with enforce_layer_gates=true in config for strict enforcement",
            "batch_computed": True,
        },
    }
