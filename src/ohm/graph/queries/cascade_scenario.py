"""cascade_scenario queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Scenario Engine (OHM-xagx) ──────────────────────────────────────────────


def query_counterfactual_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
    edge_overrides: dict[str, float] | None = None,
    node_interventions: dict[str, float] | None = None,
    disabled_edges: set[str] | None = None,
    disabled_nodes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Counterfactual cascade with edge overrides and node interventions (OHM-xagx).

    Like :func:`query_deterministic_cascade` but accepts modifications to the
    graph without persisting them. This enables "what if" scenario analysis:
    "What if this supplier's reliability dropped to 0.3?" or "What if we
    removed this dependency?"

    Args:
        conn: Database connection (read-only — no modifications persisted).
        node_id: Starting node for the cascade.
        failure_probability: Initial failure probability (0.0-1.0).
        max_depth: Maximum traversal depth.
        edge_overrides: Dict of ``{edge_id: new_probability}`` to use instead
            of the stored probability/confidence. The cascade uses the override
            value when traversing that edge.
        node_interventions: Dict of ``{node_id: failure_probability}`` to set
            a node's failure probability directly, bypassing its upstream
            propagation. This is the do-operator: "force this node to this
            state regardless of its inputs."
        disabled_edges: Set of edge IDs to skip (as if the edge doesn't exist).
        disabled_nodes: Set of node IDs to skip (as if the node is removed).

    Returns:
        List of dicts with node_id, node_label, node_type, failure_probability,
        depth, path, and ``intervened`` (bool) / ``overridden`` (bool) flags.
    """
    from ohm.validation import validate_identifier, validate_depth, validate_confidence

    node_id = validate_identifier(node_id, name="node_id")
    failure_probability = validate_confidence(failure_probability)
    max_depth = validate_depth(max_depth)

    edge_overrides = edge_overrides or {}
    node_interventions = node_interventions or {}
    disabled_edges = disabled_edges or set()
    disabled_nodes = disabled_nodes or set()

    # Fetch all relevant edges (same edge types as deterministic cascade)
    edge_rows = conn.execute(
        """SELECT id, from_node, to_node, edge_type, probability, confidence
           FROM ohm_edges
           WHERE edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
             AND deleted_at IS NULL""",
    ).fetchall()

    # Build adjacency: from_node → list of (edge_id, to_node, effective_probability)
    adjacency: dict[str, list[tuple[str, str, float]]] = {}
    for row in edge_rows:
        eid, from_n, to_n, etype, prob, conf = row
        if eid in disabled_edges or from_n in disabled_nodes or to_n in disabled_nodes:
            continue
        eff_prob = edge_overrides.get(eid, prob if prob is not None else (conf if conf is not None else 0.5))
        if from_n not in adjacency:
            adjacency[from_n] = []
        adjacency[from_n].append((eid, to_n, eff_prob))

    # BFS cascade with override support
    results: list[dict[str, Any]] = []
    visited: dict[str, float] = {}  # node_id → best failure_probability
    queue: list[tuple[str, float, int, list[str], bool]] = [(node_id, failure_probability, 0, [node_id], False)]

    while queue:
        current, current_prob, depth, path, intervened = queue.pop(0)

        # Check if this node has a direct intervention
        if current in node_interventions:
            current_prob = node_interventions[current]
            intervened = True

        # Track best probability for this node
        if current in visited and visited[current] >= current_prob:
            continue  # Already visited with higher probability
        visited[current] = current_prob

        if depth > 0:
            # Look up node info
            node_info = conn.execute(
                "SELECT label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [current],
            ).fetchone()
            results.append(
                {
                    "node_id": current,
                    "node_label": node_info[0] if node_info else current,
                    "node_type": node_info[1] if node_info else "unknown",
                    "failure_probability": round(current_prob, 6),
                    "depth": depth,
                    "path": path,
                    "intervened": intervened,
                }
            )

        if depth >= max_depth:
            continue

        # Propagate to downstream nodes
        for eid, to_n, edge_prob in adjacency.get(current, []):
            if to_n in path:
                continue  # Cycle detection
            downstream_prob = current_prob * edge_prob
            # Skip negligible propagation UNLESS the downstream node has a
            # direct intervention (which will override the probability anyway)
            if downstream_prob <= 0.001 and to_n not in node_interventions:
                continue
            queue.append((to_n, downstream_prob, depth + 1, path + [to_n], intervened))

    # Sort by depth then probability descending
    results.sort(key=lambda r: (r["depth"], -r["failure_probability"]))
    return results


def query_compare_scenarios(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
    edge_overrides: dict[str, float] | None = None,
    node_interventions: dict[str, float] | None = None,
    disabled_edges: set[str] | None = None,
    disabled_nodes: set[str] | None = None,
) -> dict[str, Any]:
    """Compare baseline vs counterfactual cascade scenarios (OHM-xagx).

    Runs two cascades:
    1. **Baseline**: the current graph state (no overrides).
    2. **Counterfactual**: with the provided edge overrides, node interventions,
       disabled edges, and disabled nodes.

    Returns both results plus a delta analysis showing which nodes changed
    and by how much.

    Args:
        conn: Database connection.
        node_id: Starting node.
        failure_probability: Initial failure probability.
        max_depth: Maximum traversal depth.
        edge_overrides: Edge probability modifications.
        node_interventions: Forced node states.
        disabled_edges: Edges to remove.
        disabled_nodes: Nodes to remove.

    Returns:
        Dict with:
          - baseline: list of cascade results (unmodified)
          - counterfactual: list of cascade results (with overrides)
          - deltas: list of per-node changes (node_id, baseline_prob,
            counterfactual_prob, delta, direction)
          - summary: counts of increased/decreased/unchanged/new/removed nodes
    """
    # Run baseline
    baseline = query_counterfactual_cascade(
        conn,
        node_id,
        failure_probability=failure_probability,
        max_depth=max_depth,
    )

    # Run counterfactual
    counterfactual = query_counterfactual_cascade(
        conn,
        node_id,
        failure_probability=failure_probability,
        max_depth=max_depth,
        edge_overrides=edge_overrides,
        node_interventions=node_interventions,
        disabled_edges=disabled_edges,
        disabled_nodes=disabled_nodes,
    )

    # Build lookup maps
    baseline_map = {r["node_id"]: r["failure_probability"] for r in baseline}
    cf_map = {r["node_id"]: r["failure_probability"] for r in counterfactual}

    all_nodes = set(baseline_map.keys()) | set(cf_map.keys())

    deltas = []
    increased = 0
    decreased = 0
    unchanged = 0
    new_nodes = 0
    removed_nodes = 0

    for nid in all_nodes:
        b_prob = baseline_map.get(nid)
        c_prob = cf_map.get(nid)

        if b_prob is None and c_prob is not None:
            new_nodes += 1
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": None,
                    "counterfactual_prob": c_prob,
                    "delta": c_prob,
                    "direction": "new",
                }
            )
        elif b_prob is not None and c_prob is None:
            removed_nodes += 1
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": b_prob,
                    "counterfactual_prob": None,
                    "delta": -b_prob,
                    "direction": "removed",
                }
            )
        else:
            delta = (c_prob or 0.0) - (b_prob or 0.0)
            if abs(delta) < 0.001:
                unchanged += 1
                direction = "unchanged"
            elif delta > 0:
                increased += 1
                direction = "increased"
            else:
                decreased += 1
                direction = "decreased"
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": b_prob,
                    "counterfactual_prob": c_prob,
                    "delta": round(delta, 6),
                    "direction": direction,
                }
            )

    # Sort deltas by absolute delta descending
    deltas.sort(key=lambda d: abs(d["delta"]) if d["delta"] is not None else 0, reverse=True)

    return {
        "node_id": node_id,
        "baseline": baseline,
        "counterfactual": counterfactual,
        "deltas": deltas,
        "summary": {
            "total_nodes": len(all_nodes),
            "increased": increased,
            "decreased": decreased,
            "unchanged": unchanged,
            "new": new_nodes,
            "removed": removed_nodes,
        },
    }


# ── Autonomy Loop: Proposed/Executed Actions (OHM-446a) ─────────────────────
