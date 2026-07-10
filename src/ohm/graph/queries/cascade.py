"""cascade queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile

def query_deterministic_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Deterministic cascade through downstream graph from a node.

    Starting from *node_id* with *failure_probability*, walks downstream
    through CAUSES, EXPECTED_LIKELIHOOD, DEPENDS_ON, and THREATENS edges.
    Each downstream node's failure probability is computed as:

        P_downstream = P_upstream × edge.probability (or edge.confidence if no probability)

    Returns all downstream nodes with computed failure probabilities and
    the path chain that leads to each. This is a deterministic computation,
    not a Monte Carlo simulation — for probabilistic analysis with variance
    use `monte_carlo_cascade()`.

    Args:
        conn: Database connection.
        node_id: Starting node (e.g., supplier that might fail).
        failure_probability: Probability that the starting node fails (0.0-1.0).
        max_depth: Maximum traversal depth.

    Returns:
        List of dicts with node_id, label, failure_probability, depth, and path.
    """
    from ohm.validation import validate_confidence, validate_depth, validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    failure_probability = validate_confidence(failure_probability)
    max_depth = validate_depth(max_depth)

    # Use a recursive CTE to walk downstream
    query = """
        WITH RECURSIVE cascade AS (
            -- Anchor: the starting node
            SELECT
                ? AS node_id,
                CAST(? AS FLOAT) AS failure_probability,
                0 AS depth,
                list_value(?) AS path
            UNION ALL
            -- Recursive: follow downstream edges
            SELECT
                e.to_node AS node_id,
                CAST(
                    c.failure_probability *
                    COALESCE(e.probability, e.confidence, 0.5)
                    AS FLOAT
                ) AS failure_probability,
                c.depth + 1 AS depth,
                list_concat(c.path, list_value(e.to_node)) AS path
            FROM cascade c
            JOIN ohm_edges e ON e.from_node = c.node_id
            WHERE c.depth < ?
              AND e.edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
              AND NOT list_contains(c.path, e.to_node)
        )
        SELECT DISTINCT
            c.node_id,
            n.label AS node_label,
            n.type AS node_type,
            c.failure_probability,
            c.depth,
            c.path
        FROM cascade c
        JOIN ohm_nodes n ON c.node_id = n.id
        WHERE c.depth > 0
        ORDER BY c.depth, c.failure_probability DESC
    """
    result = conn.execute(query, [node_id, failure_probability, node_id, max_depth])
    return _rows_to_dicts(result)


# Backward-compatible alias (deprecated)
def query_cascade_scenario(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """[DEPRECATED] Use query_deterministic_cascade() instead.

    This function was renamed to clarify that it performs deterministic
    cascade propagation, not Monte Carlo simulation.
    """
    import warnings

    warnings.warn(
        "query_cascade_scenario is deprecated, use query_deterministic_cascade instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return query_deterministic_cascade(conn, node_id, failure_probability=failure_probability, max_depth=max_depth)


def monte_carlo_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    trials: int = 1000,
    max_depth: int = 10,
    seed: int | None = None,
    default_probability: float = 0.5,
) -> dict[str, Any]:
    """Monte Carlo simulation of cascade through downstream graph.

    Runs *trials* number of cascade trials with two-stage sampling per ADR-008:
    - Stage 1: Edge existence — sample random() < confidence
    - Stage 2: Effect propagation — sample random() < probability

    Returns distribution statistics (p5, p50, p95, mean) for each downstream
    node rather than a single point estimate.

    For a deterministic analysis use `query_deterministic_cascade()`.

    Args:
        conn: Database connection.
        node_id: Starting node for cascade simulation.
        trials: Number of Monte Carlo trials to run (default 1000).
        max_depth: Maximum traversal depth per trial.
        seed: Random seed for reproducibility. If None, results vary each run.
        default_probability: Default probability when edge has none set (default 0.5).

    Returns:
        Dict with:
        - node_id: the starting node
        - results: list of per-node statistics {node_id, p5, p50, p95, mean, activated_count, trials}
        - trials: number of trials run
        - seed: random seed used (None if no seed)
    """
    import random
    from ohm.validation import validate_identifier, validate_depth

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth)

    if seed is not None:
        random.seed(seed)

    # Get downstream edges - traverse from node_id following outgoing edges
    # and collect all edges encountered during traversal
    edges_query = """
        WITH RECURSIVE traverse AS (
            SELECT
                ? AS start_node,
                ? AS node_id,
                0 AS depth,
                list_value(?) AS path
            UNION ALL
            SELECT
                t.start_node,
                e.to_node AS node_id,
                t.depth + 1 AS depth,
                list_concat(t.path, list_value(e.to_node)) AS path
            FROM traverse t
            JOIN ohm_edges e ON e.from_node = t.node_id
            WHERE t.depth < ?
              AND e.edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
              AND e.deleted_at IS NULL
              AND NOT list_contains(t.path, e.to_node)
        )
        SELECT
            t.node_id AS from_node,
            e.to_node AS to_node,
            e.edge_type,
            e.probability,
            e.confidence
        FROM traverse t
        JOIN ohm_edges e ON e.from_node = t.node_id
        WHERE t.depth >= 0
          AND e.deleted_at IS NULL
        ORDER BY t.depth, t.node_id
    """
    edges_result = conn.execute(edges_query, [node_id, node_id, node_id, max_depth])
    edges = _rows_to_dicts(edges_result)

    # Build adjacency for simulation: from_node -> list of {to_node, confidence, probability}
    # ADR-008: two-stage sampling — confidence (existence) then probability (propagation)
    node_edges: dict[str, list[dict[str, Any]]] = {}
    all_nodes = {node_id}
    for edge in edges:
        from_node = edge["from_node"]
        to_node = edge["to_node"]
        if from_node not in node_edges:
            node_edges[from_node] = []
        effective_prob = float(edge["probability"]) if edge["probability"] is not None else default_probability
        node_edges[from_node].append(
            {
                "to_node": to_node,
                "confidence": float(edge["confidence"]) if edge["confidence"] is not None else 0.7,
                "probability": effective_prob,
            }
        )
        all_nodes.add(from_node)
        all_nodes.add(to_node)

    # Run trials — delegate to Rust extension if available (OHM-lqpk.4)
    from ohm.mc import monte_carlo_sim

    # Build the adjacency in the tuple format expected by mc.monte_carlo_sim
    adj_tuples: dict[str, list[tuple[str, float, float]]] = {}
    for from_node, edges in node_edges.items():
        adj_tuples[from_node] = [(e["to_node"], e["confidence"], e["probability"]) for e in edges]

    sim_counts, _ = monte_carlo_sim(adj_tuples, node_id, trials, max_depth, seed)

    # monte_carlo_cascade counts ALL visited nodes (including source), while
    # the sim function only counts targets that passed both sampling stages.
    # The source is always visited (trials times); add it to the counts.
    activated_counts: dict[str, int] = {n: 0 for n in all_nodes}
    activated_counts[node_id] = trials
    for nid, count in sim_counts.items():
        activated_counts[nid] = activated_counts.get(nid, 0) + count

    # Compute distribution statistics
    results = []
    for nid in sorted(all_nodes):
        count = activated_counts[nid]
        activated_pct = count / trials
        results.append(
            {
                "node_id": nid,
                "activated_count": count,
                "activated_pct": round(activated_pct, 4),
                "p5": round(_percentile(count, trials, 0.05), 4),
                "p50": round(_percentile(count, trials, 0.50), 4),
                "p95": round(_percentile(count, trials, 0.95), 4),
                "mean": round(activated_pct, 4),
            }
        )

    return {
        "node_id": node_id,  # starting node (not last in loop)
        "results": results,
        "trials": trials,
        "seed": seed,
    }


def query_what_if(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Dry-run: what happens downstream if this edge's event occurs?

    Treats the edge's to_node as the failure origin with probability
    equal to the edge's probability (or confidence). Returns the cascade
    analysis without modifying the graph.

    Args:
        conn: Database connection.
        edge_id: The edge whose event we're simulating.
        max_depth: Maximum traversal depth.

    Returns:
        Dict with edge details and downstream cascade results.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")

    # Get the edge details
    edge = conn.execute(
        """SELECT id, from_node, to_node, edge_type, layer,
                  confidence, probability, condition, created_by
           FROM ohm_edges WHERE id = ?""",
        [edge_id],
    ).fetchone()
    if edge is None:
        raise ValueError(f"Edge not found: {edge_id}")

    columns = ["id", "from_node", "to_node", "edge_type", "layer", "confidence", "probability", "condition", "created_by"]
    edge_dict = dict(zip(columns, edge))

    # Use edge's probability (or confidence) as the failure probability
    trigger_prob = edge_dict.get("probability") or edge_dict.get("confidence") or 1.0

    cascade = query_deterministic_cascade(
        conn,
        edge_dict["to_node"],
        failure_probability=float(trigger_prob),
        max_depth=max_depth,
    )

    return {
        "trigger_edge": edge_dict,
        "trigger_probability": trigger_prob,
        "downstream_impact": cascade,
        "affected_nodes": len(cascade),
    }


def propagate_observation(
    conn: DuckDBPyConnection,
    source_node_id: str,
    *,
    observation_weight: float = 1.0,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    max_depth: int = 10,
    edge_types: tuple[str, ...] | None = None,
    layers: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Propagate a Bayesian observation downstream through the causal graph.

    Walks the L3 causal graph downstream from *source_node_id* and updates
    each reachable node's belief using a conjugate Beta-Binomial update:

        posterior_alpha = prior_alpha + accumulated_weight
        posterior_beta  = prior_beta + (1 - accumulated_weight)

    The accumulated weight at each downstream node is the product of the
    *observation_weight* and all edge probabilities/confidences along the
    shortest path from the source.

    This is a deterministic Bayesian propagation (OHM-vatf). For Monte Carlo
    cascade simulation use :func:`monte_carlo_cascade`. For heuristic
    probability-product cascade use :func:`query_deterministic_cascade`.

    Args:
        conn: Database connection.
        source_node_id: Node where the observation originates.
        observation_weight: Strength of the observation in [0, 1]
            (default 1.0 = fully confident observation).
        prior_alpha: Alpha parameter of the Beta prior for all downstream
            nodes (default 1.0, i.e. uniform prior Beta(1,1)).
        prior_beta: Beta parameter of the Beta prior (default 1.0).
        max_depth: Maximum traversal depth (default 10).
        edge_types: Edge types to traverse. Defaults to causal types
            (CAUSES, DEPENDS_ON, THREATENS, EXPECTED_LIKELIHOOD).
        layers: Optional layer filter (e.g., ('L3', 'L4')). If None,
            all layers are included.

    Returns:
        List of dicts with keys:
            node_id: The downstream node.
            node_label: Human-readable label.
            node_type: Node type from ohm_nodes.
            depth: Distance from source in edges.
            path: List of node IDs along the shortest path.
            prior_alpha: The prior alpha used.
            prior_beta: The prior beta used.
            posterior_alpha: Updated alpha after propagation.
            posterior_beta: Updated beta after propagation.
            posterior_mean: posterior_alpha / (posterior_alpha + posterior_beta).
            accumulated_weight: Total weight that reached this node.
    """
    from ohm.validation import validate_confidence, validate_depth, validate_identifier

    source_node_id = validate_identifier(source_node_id, name="source_node_id")
    observation_weight = validate_confidence(observation_weight)
    max_depth = validate_depth(max_depth)

    if observation_weight <= 0.0:
        return []

    if edge_types is None:
        edge_types = ("CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD")

    edge_type_list = list(edge_types)
    placeholders = ", ".join(["?"] * len(edge_type_list))

    layer_filter = ""
    layer_params: list[str] = []
    if layers:
        layer_filter = f"AND e.layer IN ({', '.join(['?'] * len(layers))})"
        layer_params = list(layers)

    query = f"""
        WITH RECURSIVE propagation AS (
            SELECT
                ? AS source_node,
                ? AS node_id,
                0 AS depth,
                CAST(? AS FLOAT) AS accumulated_weight,
                list_value(?) AS path
            UNION ALL
            SELECT
                p.source_node,
                e.to_node AS node_id,
                p.depth + 1 AS depth,
                CAST(
                    p.accumulated_weight *
                    COALESCE(e.probability, e.confidence, 0.5)
                    AS FLOAT
                ) AS accumulated_weight,
                list_concat(p.path, list_value(e.to_node)) AS path
            FROM propagation p
            JOIN ohm_edges e ON e.from_node = p.node_id
            WHERE p.depth < ?
              AND e.edge_type IN ({placeholders})
              AND e.deleted_at IS NULL
              AND e.to_node != p.source_node
              AND NOT list_contains(p.path, e.to_node)
              {layer_filter}
        )
        SELECT DISTINCT
            p.node_id,
            n.label AS node_label,
            n.type AS node_type,
            p.depth,
            p.path,
            p.accumulated_weight
        FROM propagation p
        JOIN ohm_nodes n ON p.node_id = n.id
        WHERE p.depth > 0
        ORDER BY p.depth, p.accumulated_weight DESC
    """
    params: list[Any] = [source_node_id, source_node_id, observation_weight, source_node_id, max_depth]
    params.extend(edge_type_list)
    params.extend(layer_params)

    result = conn.execute(query, params)
    rows = _rows_to_dicts(result)

    output = []
    for row in rows:
        w = float(row["accumulated_weight"])
        post_alpha = prior_alpha + w
        post_beta = prior_beta + (1.0 - w)
        post_mean = post_alpha / (post_alpha + post_beta) if (post_alpha + post_beta) > 0 else 0.5
        output.append(
            {
                "node_id": row["node_id"],
                "node_label": row["node_label"],
                "node_type": row["node_type"],
                "depth": row["depth"],
                "path": row["path"],
                "prior_alpha": prior_alpha,
                "prior_beta": prior_beta,
                "posterior_alpha": round(post_alpha, 6),
                "posterior_beta": round(post_beta, 6),
                "posterior_mean": round(post_mean, 6),
                "accumulated_weight": round(w, 6),
            }
        )

    return output


# ── Customer Support: Handoff, Escalation, Provenance ───────────────────────

HANDOFF_EDGE_TYPES = frozenset(
    {
        "TRANSFERRED_TO",
        "ESCALATED_TO",
        "DELEGATED_TO",
    }
)

STATE_MACHINE_EDGE_TYPES = frozenset(
    {
        "OPENED_BY",
        "STARTED_BY",
        "AWAITING",
        "RESOLVED_BY",
        "CLOSED_BY",
    }
)


