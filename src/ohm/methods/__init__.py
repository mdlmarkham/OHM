"""OHM Substrate Methods — validated computation in the cognition substrate.

These are pure functions that take graph state and return deterministic or
well-characterized probabilistic output. They produce the same result
regardless of which agent calls them.

Design principle: If a method produces the same output regardless of which
agent calls it, it belongs in the substrate. If it requires domain judgment,
it stays with the agent.

Methods:
    1. aggregate_observations — Combine multiple observations with configurable strategy
    2. detect_anomalies — Sigma-based anomaly flagging
    3. graph_health — Structural health metrics (orphans, stale edges, clusters)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Confidence Aggregation ──────────────────────────────────────────────────

def aggregate_observations(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    method: str = "weighted",
) -> dict[str, Any]:
    """Combine multiple observations on a node into a single value.

    Strategies:
        - weighted: Average weighted by 1/sigma² (inverse-variance weighting).
                    Falls back to simple mean if sigma is missing.
        - mean: Simple arithmetic mean of all values.
        - max_confidence: Take the observation with the highest sigma (lowest uncertainty).
        - consensus: Weighted average, but only if the coefficient of variation
                     is below 0.3 (observations agree). Returns None if they disagree.

    Args:
        conn: Database connection.
        node_id: The node to aggregate observations for.
        method: Aggregation strategy (weighted, mean, max_confidence, consensus).

    Returns:
        Dict with combined_value, combined_confidence, observation_count,
        method_used, and individual observations.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Fetch all observations with values for this node
    rows = conn.execute(
        """SELECT id, value, sigma, created_by, created_at
           FROM ohm_observations
           WHERE node_id = ? AND value IS NOT NULL
           ORDER BY created_at DESC""",
        [node_id],
    ).fetchall()

    if not rows:
        return {
            "combined_value": None,
            "combined_confidence": 0.0,
            "observation_count": 0,
            "method_used": method,
            "observations": [],
        }

    columns = ["id", "value", "sigma", "created_by", "created_at"]
    observations = [dict(zip(columns, row)) for row in rows]

    if method == "max_confidence":
        # Take the observation with highest sigma (lowest uncertainty)
        best = max(observations, key=lambda o: o["sigma"] or 0)
        return {
            "combined_value": best["value"],
            "combined_confidence": best["sigma"] or 0.0,
            "observation_count": len(observations),
            "method_used": method,
            "observations": observations,
        }

    if method == "mean":
        values = [o["value"] for o in observations if o["value"] is not None]
        if not values:
            return {
                "combined_value": None,
                "combined_confidence": 0.0,
                "observation_count": len(observations),
                "method_used": method,
                "observations": observations,
            }
        avg = sum(values) / len(values)
        return {
            "combined_value": round(avg, 6),
            "combined_confidence": 1.0 / len(values),  # simple: more obs = more confidence
            "observation_count": len(observations),
            "method_used": method,
            "observations": observations,
        }

    if method == "consensus":
        values = [o["value"] for o in observations if o["value"] is not None]
        if len(values) < 2:
            return aggregate_observations(conn, node_id, method="weighted")

        mean_val = sum(values) / len(values)
        if mean_val == 0:
            cv = 0.0
        else:
            variance = sum((v - mean_val) ** 2 for v in values) / len(values)
            cv = (variance ** 0.5) / abs(mean_val)

        if cv > 0.3:
            return {
                "combined_value": None,
                "combined_confidence": 0.0,
                "observation_count": len(observations),
                "method_used": method,
                "disagreement": True,
                "coefficient_of_variation": round(cv, 4),
                "observations": observations,
            }
        # Fall through to weighted if consensus exists
        return aggregate_observations(conn, node_id, method="weighted")

    # Default: weighted (inverse-variance)
    total_weight = 0.0
    weighted_sum = 0.0
    combined_sigma: float = 0.0
    for o in observations:
        if o["value"] is not None and o["sigma"] and o["sigma"] > 0:
            weight = 1.0 / (o["sigma"] ** 2)
            weighted_sum += o["value"] * weight
            total_weight += weight

    if total_weight > 0:
        combined_val: float | None = weighted_sum / total_weight
        combined_sigma = (1.0 / total_weight) ** 0.5
    else:
        # Fall back to simple mean
        values = [o["value"] for o in observations if o["value"] is not None]
        combined_val = sum(values) / len(values) if values else None
        combined_sigma = 0.0

    return {
        "combined_value": round(combined_val, 6) if combined_val is not None else None,
        "combined_confidence": round(combined_sigma, 6),
        "observation_count": len(observations),
        "method_used": method,
        "observations": observations,
    }


# ── Anomaly Detection ───────────────────────────────────────────────────────

def detect_anomalies(
    conn: DuckDBPyConnection,
    *,
    sigma_threshold: float = 2.0,
    layer: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Detect anomalous observations using sigma-based flagging.

    An observation is anomalous if |value - baseline| / sigma > threshold.
    Also flags nodes with high variance across observations and edges with
    unusually low confidence for their layer.

    Args:
        conn: Database connection.
        sigma_threshold: Number of sigmas for flagging (default 2.0).
        layer: Optional layer filter for edge confidence anomalies.
        limit: Maximum results to return.

    Returns:
        List of anomalous observations ranked by surprise (sigma distance).
    """
    from ohm.validation import validate_layer

    if layer:
        layer = validate_layer(layer)

    anomalies: list[dict[str, Any]] = []

    # 1. Sigma-based observation anomalies
    layer_filter = ""
    params: list = [sigma_threshold, limit]
    if layer:
        layer_filter = "AND e.layer = ?"
        params.insert(1, layer)

    obs_anomalies = conn.execute("""
        SELECT
            o.id AS observation_id,
            o.node_id,
            o.value,
            o.baseline,
            o.sigma,
            ABS(o.value - o.baseline) / NULLIF(o.sigma, 0) AS sigma_distance,
            o.created_by,
            o.created_at,
            n.label AS node_label,
            'observation' AS anomaly_type
        FROM ohm_observations o
        JOIN ohm_nodes n ON n.id = o.node_id
        WHERE o.value IS NOT NULL
          AND o.baseline IS NOT NULL
          AND o.sigma IS NOT NULL
          AND o.sigma > 0
          AND ABS(o.value - o.baseline) / o.sigma > ?
        ORDER BY sigma_distance DESC
        LIMIT ?
    """, params).fetchall()

    if obs_anomalies:
        cols = ["observation_id", "node_id", "value", "baseline", "sigma",
                "sigma_distance", "created_by", "created_at", "node_label", "anomaly_type"]
        anomalies.extend(dict(zip(cols, row)) for row in obs_anomalies)

    # 2. High-variance nodes (multiple observations with high spread)
    variance_anomalies = conn.execute("""
        SELECT
            o.node_id,
            n.label AS node_label,
            COUNT(*) AS observation_count,
            ROUND(STDDEV(o.value), 4) AS stddev,
            ROUND(AVG(o.value), 4) AS mean_value,
            'high_variance' AS anomaly_type
        FROM ohm_observations o
        JOIN ohm_nodes n ON n.id = o.node_id
        WHERE o.value IS NOT NULL
        GROUP BY o.node_id, n.label
        HAVING COUNT(*) >= 3 AND STDDEV(o.value) > AVG(ABS(o.value)) * 0.5
        ORDER BY stddev DESC
        LIMIT ?
    """, [limit]).fetchall()

    if variance_anomalies:
        cols = ["node_id", "node_label", "observation_count", "stddev",
                "mean_value", "anomaly_type"]
        anomalies.extend(dict(zip(cols, row)) for row in variance_anomalies)

    # 3. Low-confidence edges (unusually low for their layer)
    edge_anomalies = conn.execute(f"""
        SELECT
            e.id AS edge_id,
            e.from_node,
            e.to_node,
            e.layer,
            e.edge_type,
            e.confidence,
            e.created_by,
            'low_confidence' AS anomaly_type
        FROM ohm_edges e
        WHERE e.confidence < 0.3
          AND e.layer IN ('L3', 'L4')
          AND e.challenge_of IS NULL
          {layer_filter}
        ORDER BY e.confidence ASC
        LIMIT ?
    """, params).fetchall()

    if edge_anomalies:
        cols = ["edge_id", "from_node", "to_node", "layer", "edge_type",
                "confidence", "created_by", "anomaly_type"]
        anomalies.extend(dict(zip(cols, row)) for row in edge_anomalies)

    return anomalies


# ── Graph Health ────────────────────────────────────────────────────────────

def graph_health(conn: DuckDBPyConnection) -> dict[str, Any]:
    """Compute structural health metrics for the graph.

    Returns metrics on orphans, unchallenged low-confidence edges,
    dense clusters, and stale observations.

    Returns:
        Dict with health metrics and overall health score.
    """
    health: dict[str, Any] = {}

    # Orphans: nodes with < 2 edges
    orphan_result = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT n.id
            FROM ohm_nodes n
            LEFT JOIN ohm_edges e ON e.from_node = n.id OR e.to_node = n.id
            GROUP BY n.id
            HAVING COUNT(e.id) < 2
        )
    """).fetchone()
    health["orphan_nodes"] = orphan_result[0] if orphan_result else 0

    # Unchallenged low-confidence: L3/L4 edges with confidence < 0.5 and no challenges
    unchallenged_result = conn.execute("""
        SELECT COUNT(*)
        FROM ohm_edges e
        WHERE e.layer IN ('L3', 'L4')
          AND e.confidence < 0.5
          AND e.challenge_of IS NULL
          AND e.id NOT IN (
              SELECT DISTINCT challenge_of FROM ohm_edges WHERE challenge_of IS NOT NULL
          )
    """).fetchone()
    health["unchallenged_low_confidence"] = unchallenged_result[0] if unchallenged_result else 0

    # Dense clusters: nodes with > 5 edges (potential synthesis candidates)
    dense_result = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT n.id
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.from_node = n.id OR e.to_node = n.id
            GROUP BY n.id
            HAVING COUNT(e.id) > 5
        )
    """).fetchone()
    health["dense_cluster_nodes"] = dense_result[0] if dense_result else 0

    # Stale observations: not updated in > 30 days
    stale_result = conn.execute("""
        SELECT COUNT(*)
        FROM ohm_observations
        WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '30 days'
    """).fetchone()
    health["stale_observations"] = stale_result[0] if stale_result else 0

    # Total counts for context
    total_nodes = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()
    health["total_nodes"] = total_nodes[0] if total_nodes else 0
    total_edges = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()
    health["total_edges"] = total_edges[0] if total_edges else 0

    # Overall health score (0-100)
    # Penalize: orphans, unchallenged edges, staleness
    # Reward: connectivity
    score = 100.0
    if health["total_nodes"] > 0:
        score -= min(30, (health["orphan_nodes"] / health["total_nodes"]) * 100)
    if health["total_edges"] > 0:
        score -= min(20, (health["unchallenged_low_confidence"] / health["total_edges"]) * 100)
        score -= min(10, (health["stale_observations"] / max(1, health["total_edges"])) * 100)
    health["health_score"] = round(max(0, score), 1)

    return health
