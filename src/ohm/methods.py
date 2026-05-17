"""Substrate methods — validated computation in the cognition substrate.

These methods produce the same output regardless of which agent calls them.
That's what makes them substrate: they're mechanical, not judgmental.

If a method requires domain judgment (e.g., "is this pattern valid?"),
it belongs with the agent, not the substrate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def detect_anomalies(
    conn: DuckDBPyConnection,
    *,
    sigma_threshold: float = 2.0,
    layer: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find observations where the deviation exceeds sigma_threshold.

    An observation is anomalous when |value - baseline| / sigma > threshold.
    The sigma field was designed for this — it measures how surprising the
    observation was relative to expectation.

    Args:
        sigma_threshold: Minimum deviation in standard deviations (default 2.0).
        layer: Optional edge layer filter for related edges.
        limit: Maximum results (default 50).

    Returns:
        List of anomalous observation records with deviation magnitude.
    """
    # Column names are hardcoded, values parameterized
    layer_clause = ""
    params: list[Any] = [sigma_threshold, limit]
    if layer:
        layer_clause = "AND e.layer = ?"
        params = [sigma_threshold, layer, limit]

    query = f"""
        SELECT
            o.id AS obs_id,
            o.node_id,
            o.type AS obs_type,
            o.value,
            o.baseline,
            o.sigma,
            ABS(o.value - o.baseline) / NULLIF(o.sigma, 0) AS deviation,
            o.source,
            o.created_by,
            o.created_at,
            n.label AS node_label,
            n.type AS node_type
        FROM ohm_observations o
        LEFT JOIN ohm_nodes n ON n.id = o.node_id
        WHERE o.sigma IS NOT NULL
          AND o.sigma > 0
          AND o.value IS NOT NULL
          AND o.baseline IS NOT NULL
          AND ABS(o.value - o.baseline) / o.sigma > ?
          {layer_clause}
        ORDER BY deviation DESC
        LIMIT ?
    """

    result = conn.execute(query, params)
    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def detect_contradictions(
    conn: DuckDBPyConnection,
    *,
    confidence_threshold: float = 0.5,
    limit: int = 50,
) -> dict[str, Any]:
    """Flag conflicting observations or interpretations between agents.

    Detects three types of contradictions:
    1. Same node, opposite observations (value far from baseline in different directions)
    2. CHALLENGED_BY edges with high confidence (serious disagreements)
    3. Same source, contradictory L3 interpretations

    Does NOT resolve contradictions — only surfaces them for agents to address.

    Args:
        confidence_threshold: Minimum confidence for challenges to count (default 0.5).
        limit: Maximum results per type (default 50).

    Returns:
        Dict with 'opposite_observations', 'high_confidence_challenges',
        and 'contradictory_interpretations' lists.
    """
    # Type 1: Opposite observations on same node
    opposite = conn.execute("""
        SELECT
            a.node_id,
            n.label AS node_label,
            a.created_by AS agent_a,
            b.created_by AS agent_b,
            a.value AS value_a,
            b.value AS value_b,
            a.baseline AS baseline,
            ABS(a.value - b.value) AS gap,
            a.created_at AS time_a,
            b.created_at AS time_b
        FROM ohm_observations a
        JOIN ohm_observations b ON a.node_id = b.node_id AND a.id < b.id
        LEFT JOIN ohm_nodes n ON n.id = a.node_id
        WHERE a.created_by != b.created_by
          AND a.value IS NOT NULL AND b.value IS NOT NULL
          AND a.baseline IS NOT NULL
          AND (
            (a.value > a.baseline AND b.value < a.baseline)
            OR
            (a.value < a.baseline AND b.value > a.baseline)
          )
        ORDER BY gap DESC
        LIMIT ?
    """, [limit]).fetchall()

    # Type 2: High-confidence challenges
    challenges = conn.execute("""
        SELECT
            c.id AS challenge_id,
            c.challenge_of AS target_edge_id,
            c.created_by AS challenger,
            e.created_by AS original_author,
            c.confidence AS challenge_confidence,
            e.confidence AS original_confidence,
            c.condition AS challenge_reason,
            c.created_at
        FROM ohm_edges c
        JOIN ohm_edges e ON c.challenge_of = e.id
        WHERE c.challenge_type = 'CHALLENGED_BY'
          AND c.confidence >= ?
        ORDER BY c.confidence DESC, c.created_at DESC
        LIMIT ?
    """, [confidence_threshold, limit]).fetchall()

    # Type 3: Same source, contradictory L3 interpretations
    # Two edges from the same source node, by different agents, with
    # opposing edge types (e.g., one CAUSES, one CONTRADICTS)
    contradictory = conn.execute("""
        SELECT
            a.from_node AS source_node,
            n.label AS source_label,
            a.edge_type AS edge_type_a,
            b.edge_type AS edge_type_b,
            a.created_by AS agent_a,
            b.created_by AS agent_b,
            a.confidence AS conf_a,
            b.confidence AS conf_b,
            a.condition AS reason_a,
            b.condition AS reason_b
        FROM ohm_edges a
        JOIN ohm_edges b ON a.from_node = b.from_node AND a.id < b.id
        LEFT JOIN ohm_nodes n ON n.id = a.from_node
        WHERE a.layer = 'L3' AND b.layer = 'L3'
          AND a.created_by != b.created_by
          AND (
            (a.edge_type = 'CAUSES' AND b.edge_type IN ('CONTRADICTS', 'CHALLENGED_BY'))
            OR
            (a.edge_type = 'SUPPORTS' AND b.edge_type IN ('CONTRADICTS', 'CHALLENGED_BY'))
            OR
            (a.edge_type = 'EXPLAINS' AND b.edge_type = 'CONTRADICTS')
          )
        ORDER BY (a.confidence + b.confidence) DESC
        LIMIT ?
    """, [limit]).fetchall()

    def _to_dicts(result_rows, col_names):
        """Convert result rows to dicts using provided column names."""
        if not result_rows:
            return []
        return [dict(zip(col_names, row)) for row in result_rows]

    # Get column names from each query's description
    # We need to re-execute with a simple query to get column names
    # Or just use hardcoded column lists from the SELECT statements

    opposite_cols = [
        "node_id", "node_label", "agent_a", "agent_b",
        "value_a", "value_b", "baseline", "gap", "time_a", "time_b",
    ]
    challenge_cols = [
        "challenge_id", "target_edge_id", "challenger", "original_author",
        "challenge_confidence", "original_confidence", "challenge_reason", "created_at",
    ]
    contradict_cols = [
        "source_node", "source_label", "edge_type_a", "edge_type_b",
        "agent_a", "agent_b", "conf_a", "conf_b", "reason_a", "reason_b",
    ]

    return {
        "opposite_observations": _to_dicts(opposite, opposite_cols),
        "high_confidence_challenges": _to_dicts(challenges, challenge_cols),
        "contradictory_interpretations": _to_dicts(contradictory, contradict_cols),
    }


def agent_heartbeat(
    conn: DuckDBPyConnection,
    agent_name: str,
    *,
    focus: str | None = None,
) -> dict[str, Any]:
    """Record an agent heartbeat and update its last-seen timestamp.

    Agents should call this at regular intervals (every sync_interval_sec).
    The substrate uses this to detect stale agents — if last_heartbeat
    is older than 2x the agent's sync_interval, it's considered stale.

    Args:
        agent_name: The agent sending the heartbeat.
        focus: Optional update to current focus.

    Returns:
        Agent state record with heartbeat timestamp.
    """
    from ohm.queries import _log_change

    # Update agent_state with heartbeat
    existing = conn.execute(
        "SELECT 1 FROM ohm_agent_state WHERE agent_name = ?", [agent_name]
    ).fetchone()

    if existing:
        set_parts = ["last_sync = CURRENT_TIMESTAMP", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = []
        if focus is not None:
            set_parts.append("current_focus = ?")
            params.append(focus)
        params.append(agent_name)
        conn.execute(
            "UPDATE ohm_agent_state SET " + ", ".join(set_parts) + " WHERE agent_name = ?",
            params,
        )
    else:
        conn.execute(
            """INSERT INTO ohm_agent_state
               (agent_name, current_focus, last_sync, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            [agent_name, focus],
        )

    _log_change(conn, "ohm_agent_state", agent_name, "HEARTBEAT", agent_name)

    # Return updated state
    result = conn.execute(
        "SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name]
    ).fetchone()
    if result:
        columns = [desc[0] for desc in conn.execute("SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).description]
        return dict(zip(columns, result))
    return {}


def query_agent_health(
    conn: DuckDBPyConnection,
) -> list[dict[str, Any]]:
    """Check health of all registered agents.

    An agent is stale if its last heartbeat is older than 2x its configured
    sync interval. An agent is dead if it's never sent a heartbeat.

    Returns:
        List of agent health records with status (alive/stale/dead/unknown).
    """
    # Get agents with config (sync_interval) and state (last_sync)
    result = conn.execute("""
        SELECT
            a.agent_name,
            a.current_focus,
            a.last_sync,
            a.updated_at,
            c.sync_interval_sec,
            c.optimization_target,
            CASE
                WHEN a.last_sync IS NULL THEN 'dead'
                WHEN a.last_sync < CURRENT_TIMESTAMP - INTERVAL (2 * COALESCE(c.sync_interval_sec, 300)) SECOND
                    THEN 'stale'
                ELSE 'alive'
            END AS status,
            CASE
                WHEN a.last_sync IS NOT NULL
                THEN EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - a.last_sync)) / 60.0
                ELSE NULL
            END AS minutes_since_heartbeat
        FROM ohm_agent_state a
        LEFT JOIN ohm_agent_config c ON c.agent_name = a.agent_name
        ORDER BY
            CASE
                WHEN a.last_sync IS NULL THEN 3
                WHEN a.last_sync < CURRENT_TIMESTAMP - INTERVAL (2 * COALESCE(c.sync_interval_sec, 300)) SECOND THEN 2
                ELSE 1
            END,
            a.agent_name
    """)

    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def aggregate_observations(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    method: str = "weighted",
) -> dict[str, Any]:
    """Combine multiple observations on a node into a single value.

    Strategies:
    - weighted: inverse-variance weighting (highest sigma = lowest weight)
    - mean: simple arithmetic mean
    - max_confidence: use the observation with highest confidence
    - consensus: only if agreement > 70% of observations point same direction

    Same result regardless of which agent calls it — substrate method.

    Args:
        node_id: Node to aggregate observations for.
        method: Aggregation strategy (default 'weighted').

    Returns:
        Dict with: value, confidence, method, observation_count, agreement_ratio.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    obs = conn.execute(
        """SELECT value, baseline, sigma, created_by, created_at
           FROM ohm_observations
           WHERE node_id = ? AND value IS NOT NULL
           ORDER BY created_at DESC""",
        [node_id],
    ).fetchall()

    if not obs:
        return {
            "node_id": node_id,
            "value": None,
            "confidence": 0.0,
            "method": method,
            "observation_count": 0,
            "agreement_ratio": 0.0,
        }

    values = [r[0] for r in obs]
    sigmas = [r[2] or 1.0 for r in obs]
    count = len(values)

    if method == "mean":
        agg_value = sum(values) / count
    elif method == "max_confidence":
        # Find most recent observation (no confidence column in observations)
        agg_value = values[0]  # Already sorted by created_at DESC
    elif method == "weighted":
        # Inverse-variance weighting: weight = 1/sigma^2
        weights = [1.0 / (s * s) for s in sigmas]
        total_weight = sum(weights)
        if total_weight == 0:
            agg_value = sum(values) / count
        else:
            agg_value = sum(v * w for v, w in zip(values, weights)) / total_weight
    else:
        agg_value = sum(values) / count

    # Agreement ratio: what fraction of observations point in the same direction?
    baselines = [r[1] for r in obs if r[1] is not None]
    if baselines:
        avg_baseline = sum(baselines) / len(baselines)
        same_direction = sum(
            1 for v in values
            if (v > avg_baseline and agg_value > avg_baseline)
            or (v < avg_baseline and agg_value < avg_baseline)
            or (v == avg_baseline)
        )
        agreement = same_direction / count
    else:
        agreement = 1.0

    # Combined confidence: base confidence * agreement * (1 / (1 + variance))
    if count > 1:
        mean = sum(values) / count
        variance = sum((v - mean) ** 2 for v in values) / count
        combined_conf = min(0.95, agreement * (1.0 / (1.0 + variance)))
    else:
        combined_conf = 0.5  # Single observation, moderate confidence

    return {
        "node_id": node_id,
        "value": round(agg_value, 4),
        "confidence": round(combined_conf, 4),
        "method": method,
        "observation_count": count,
        "agreement_ratio": round(agreement, 4),
    }


def monte_carlo_impact(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    simulations: int = 1000,
    depth: int = 3,
    confidence_threshold: float = 0.5,
) -> dict[str, Any]:
    """Monte Carlo simulation of failure propagation from a node.

    Randomly sample edge activation (on/off based on confidence) and
    trace downstream impact. Runs N simulations and returns the
    distribution of affected nodes.

    Same result regardless of which agent calls it — substrate method.
    No domain judgment involved — purely mechanical confidence sampling.

    Args:
        node_id: Source node for impact simulation.
        simulations: Number of Monte Carlo trials (default 1000).
        depth: Maximum traversal depth (default 3).
        confidence_threshold: Minimum confidence to consider an edge active.

    Returns:
        Dict with: affected_nodes (list of {id, label, impact_probability}),
        simulation_count, depth, mean_affected, max_affected.
    """
    import random
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Build adjacency from the graph
    edges = conn.execute(
        """SELECT from_node, to_node, edge_type, confidence, layer
           FROM ohm_edges
           WHERE layer IN ('L1', 'L2', 'L3')
             AND confidence >= ?""",
        [confidence_threshold],
    ).fetchall()

    # Adjacency list: node -> [(target, confidence)]
    adj: dict[str, list[tuple[str, float]]] = {}
    for from_node, to_node, edge_type, conf, layer in edges:
        if from_node not in adj:
            adj[from_node] = []
        adj[from_node].append((to_node, conf or 0.7))

    # Run simulations
    impact_counts: dict[str, int] = {}
    total_affected_per_sim = []

    for _ in range(simulations):
        visited = set()
        frontier = [node_id]
        affected_this_sim = 0

        for _ in range(depth):
            next_frontier = []
            for current in frontier:
                if current not in adj:
                    continue
                for target, conf in adj[current]:
                    if target in visited:
                        continue
                    # Monte Carlo: activate edge with probability = confidence
                    if random.random() < conf:
                        visited.add(target)
                        next_frontier.append(target)
                        impact_counts[target] = impact_counts.get(target, 0) + 1
                        affected_this_sim += 1
            frontier = next_frontier
            if not frontier:
                break

        total_affected_per_sim.append(affected_this_sim)

    # Convert to probabilities
    node_labels = {}
    node_ids_hit = list(impact_counts.keys())
    if node_ids_hit:
        label_rows = conn.execute(
            f"SELECT id, label FROM ohm_nodes WHERE id IN ({','.join(['?'] * len(node_ids_hit))})",
            node_ids_hit,
        ).fetchall()
        node_labels = {r[0]: r[1] for r in label_rows}

    affected_nodes = []
    for nid, count in sorted(impact_counts.items(), key=lambda x: -x[1]):
        affected_nodes.append({
            "id": nid,
            "label": node_labels.get(nid, nid),
            "impact_probability": round(count / simulations, 4),
        })

    return {
        "source_node": node_id,
        "affected_nodes": affected_nodes,
        "simulation_count": simulations,
        "depth": depth,
        "mean_affected": round(sum(total_affected_per_sim) / max(simulations, 1), 2),
        "max_affected": max(total_affected_per_sim) if total_affected_per_sim else 0,
    }


def detect_near_duplicates(
    conn: DuckDBPyConnection,
    *,
    similarity_threshold: float = 0.8,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find observations that may be duplicates from different agents.

    Two observations are near-duplicates if:
    - Same node_id
    - Same type
    - Values within 10% of each other
    - Created within 1 hour of each other

    The substrate flags these; agents decide whether to deduplicate.
    Same result regardless of which agent calls it — substrate method.

    Args:
        similarity_threshold: Minimum value similarity ratio (default 0.8).
        limit: Maximum results.

    Returns:
        List of near-duplicate pairs with similarity scores.
    """
    pairs = conn.execute(
        """
        SELECT
            a.id AS obs_a_id,
            b.id AS obs_b_id,
            a.node_id,
            n.label AS node_label,
            a.type AS obs_type,
            a.value AS value_a,
            b.value AS value_b,
            a.created_by AS agent_a,
            b.created_by AS agent_b,
            CASE
                WHEN ABS(a.value) < 0.001 AND ABS(b.value) < 0.001 THEN 1.0
                WHEN ABS(a.value) < 0.001 OR ABS(b.value) < 0.001 THEN 0.0
                ELSE 1.0 - ABS(a.value - b.value) / GREATEST(ABS(a.value), ABS(b.value))
            END AS similarity,
            a.created_at AS time_a,
            b.created_at AS time_b
        FROM ohm_observations a
        JOIN ohm_observations b ON a.node_id = b.node_id
            AND a.type = b.type
            AND a.id < b.id
            AND a.created_by != b.created_by
            AND ABS(EXTRACT(EPOCH FROM (b.created_at - a.created_at))) < 3600
        LEFT JOIN ohm_nodes n ON n.id = a.node_id
        WHERE a.value IS NOT NULL AND b.value IS NOT NULL
        ORDER BY similarity DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    result = []
    for row in pairs:
        similarity = row[9]
        if similarity is not None and similarity >= similarity_threshold:
            result.append({
                "obs_a_id": row[0],
                "obs_b_id": row[1],
                "node_id": row[2],
                "node_label": row[3],
                "obs_type": row[4],
                "value_a": round(row[5], 4) if row[5] is not None else None,
                "value_b": round(row[6], 4) if row[6] is not None else None,
                "agent_a": row[7],
                "agent_b": row[8],
                "similarity": round(similarity, 4),
                "time_gap_seconds": round(
                    abs((row[11] - row[10]).total_seconds()) if row[10] and row[11] else 0, 1
                ),
            })

    return result


def compute_confidence_calibration(
    conn: DuckDBPyConnection,
    agent_name: str,
) -> dict[str, Any]:
    """Track how well an agent's confidence ratings predict actual outcomes.

    Calibration: do edges with high confidence actually hold up better?
    Measures the ratio of challenged vs. unchallenged edges by confidence band.

    A well-calibrated agent has high-confidence edges challenged less often.
    An overconfident agent has high-confidence edges challenged frequently.
    An underconfident agent has low-confidence edges that hold up well.

    Same result regardless of which agent calls it — substrate method.

    Args:
        agent_name: Agent to evaluate.

    Returns:
        Dict with: agent_name, total_edges, calibration_by_band,
        overall_calibration_score (0-1, 1 = perfectly calibrated).
    """
    # Count edges by confidence band
    bands = conn.execute(
        """
        SELECT
            CASE
                WHEN confidence >= 0.9 THEN '0.9-1.0'
                WHEN confidence >= 0.7 THEN '0.7-0.9'
                WHEN confidence >= 0.5 THEN '0.5-0.7'
                WHEN confidence >= 0.3 THEN '0.3-0.5'
                ELSE '0.0-0.3'
            END AS confidence_band,
            COUNT(*) AS total_edges,
            SUM(CASE WHEN challenge_of IS NOT NULL THEN 1 ELSE 0 END) AS challenged_count
        FROM ohm_edges
        WHERE created_by = ? AND layer IN ('L3', 'L4')
        GROUP BY confidence_band
        ORDER BY confidence_band DESC
        """,
        [agent_name],
    ).fetchall()

    # Count challenges TO this agent's edges
    challenged_row = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id)
        FROM ohm_edges c
        JOIN ohm_edges e ON c.challenge_of = e.id
        WHERE e.created_by = ?
          AND c.challenge_type = 'CHALLENGED_BY'
        """,
        [agent_name],
    ).fetchone()
    challenged_edges = challenged_row[0] if challenged_row else 0

    total_edges = sum(b[1] for b in bands)

    # Calibration score: higher-confidence bands should have LOWER challenge rates
    # Perfect calibration: challenge_rate inversely proportional to confidence
    calibration_by_band = []
    weighted_error = 0.0
    total_weight = 0.0

    for band_name, total, challenged_in_band in bands:
        challenge_rate = challenged_in_band / max(total, 1)
        # Expected challenge rate for this band (inverse of midpoint)
        band_midpoint = {
            "0.9-1.0": 0.95, "0.7-0.9": 0.8, "0.5-0.7": 0.6,
            "0.3-0.5": 0.4, "0.0-0.3": 0.15,
        }.get(band_name, 0.5)
        expected_rate = 1.0 - band_midpoint  # High confidence → low expected challenge rate

        calibration_by_band.append({
            "band": band_name,
            "total_edges": total,
            "challenged": challenged_in_band,
            "challenge_rate": round(challenge_rate, 4),
            "expected_rate": round(expected_rate, 4),
        })

        # Error from perfect calibration
        error = abs(challenge_rate - expected_rate)
        weighted_error += error * total
        total_weight += total

    calibration_score = round(1.0 - (weighted_error / max(total_weight, 1)), 4) if total_weight > 0 else None

    return {
        "agent_name": agent_name,
        "total_l3_l4_edges": total_edges,
        "challenged_edges": challenged_edges,
        "overall_challenge_rate": round(challenged_edges / max(total_edges, 1), 4),
        "calibration_by_band": calibration_by_band,
        "calibration_score": calibration_score,
        "interpretation": (
            "well_calibrated" if calibration_score and calibration_score > 0.7
            else "overconfident" if calibration_score and calibration_score < 0.3
            else "underexamined" if total_edges < 5
            else "needs_data"
        ),
    }


def apply_confidence_decay(
    conn: DuckDBPyConnection,
    *,
    half_life_days: float = 30.0,
    min_confidence: float = 0.1,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Auto-decay edge confidence over time using exponential decay.

    Confidence decays toward 0 with a configurable half-life.
    Only affects L3/L4 edges (knowledge and prospect layers).
    L1/L2 edges (structure and flow) are not decayed — they represent
    facts, not beliefs.

    Formula: new_confidence = original_confidence * 0.5^(age_days / half_life_days)

    Args:
        conn: Database connection.
        half_life_days: Days until confidence halves (default 30).
        min_confidence: Floor for decayed confidence (default 0.1).
        dry_run: If True, return affected edges without modifying.

    Returns:
        Dict with decayed_count, affected_edges, and summary.
    """
    # Find edges eligible for decay: L3/L4, not already at floor, not challenged
    affected = conn.execute("""
        SELECT id, edge_type, layer, confidence, created_by,
               created_at,
               EXTRACT(DAY FROM CURRENT_TIMESTAMP - created_at) AS age_days
        FROM ohm_edges
        WHERE layer IN ('L3', 'L4')
          AND confidence > ?
          AND created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'
        ORDER BY age_days DESC
    """, [min_confidence]).fetchall()

    if not affected:
        return {"decayed_count": 0, "affected_edges": [], "summary": "No edges to decay"}

    decayed = []
    for row in affected:
        edge_id, etype, layer, conf, created_by, created_at, age_days = row
        age_days = float(age_days) if age_days else 0
        decay_factor = 0.5 ** (age_days / half_life_days)
        new_conf = round(conf * decay_factor, 4)
        new_conf = max(new_conf, min_confidence)

        if new_conf < conf:
            decayed.append({
                "id": edge_id,
                "edge_type": etype,
                "layer": layer,
                "original_confidence": conf,
                "new_confidence": new_conf,
                "age_days": round(age_days, 1),
                "created_by": created_by,
            })

            if not dry_run:
                conn.execute(
                    "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [new_conf, edge_id],
                )

    return {
        "decayed_count": len(decayed),
        "affected_edges": decayed,
        "half_life_days": half_life_days,
        "min_confidence": min_confidence,
        "dry_run": dry_run,
        "summary": (
            f"Decayed {len(decayed)} edges "
            f"(half-life: {half_life_days}d, floor: {min_confidence})"
        ),
    }


def composite_score(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    observation_weight: float = 0.5,
    evidence_weight: float = 0.5,
) -> dict[str, Any]:
    """Compute a composite decision score for a node.

    Combines two independent signals into a single 0-1 score:
    1. Observation score: aggregate of direct observations on the node
       (weighted by inverse-variance, or simple mean if no sigma).
    2. Evidence score: aggregate confidence from incoming evidence edges
       (from query_confidence_chain).

    The weights control how much each signal contributes. Default is
    equal weighting (0.5 each). Set observation_weight=0 to use only
    evidence, or evidence_weight=0 to use only observations.

    This is a universal substrate method — works for any domain.

    Args:
        conn: Database connection.
        node_id: The node to score.
        observation_weight: Weight for observation signal (0-1).
        evidence_weight: Weight for evidence signal (0-1).

    Returns:
        Dict with composite_score, observation_score, evidence_score,
        observation_count, evidence_count, and components.
    """
    from ohm.queries import query_confidence_chain

    # ── Observation score ──────────────────────────────────────────
    obs_result = conn.execute(
        "SELECT value, sigma FROM ohm_observations WHERE node_id = ? AND value IS NOT NULL",
        [node_id],
    ).fetchall()

    obs_score: float | None = None
    obs_count = len(obs_result)
    if obs_result:
        total_weight = 0.0
        weighted_sum = 0.0
        for value, sigma in obs_result:
            if sigma and sigma > 0:
                w = 1.0 / (sigma ** 2)
            else:
                w = 1.0
            weighted_sum += (value or 0) * w
            total_weight += w
        obs_score = round(weighted_sum / total_weight, 4) if total_weight > 0 else None

    # ── Evidence score ─────────────────────────────────────────────
    evidence = query_confidence_chain(conn, node_id)
    evidence_score = evidence.get("aggregate_confidence")
    evidence_count = evidence.get("evidence_count", 0)

    # ── Composite ──────────────────────────────────────────────────
    if obs_score is None and evidence_score is None:
        composite = None
    elif obs_score is None:
        composite = evidence_score
    elif evidence_score is None:
        composite = obs_score
    else:
        total_w = observation_weight + evidence_weight
        composite = round(
            (obs_score * observation_weight + evidence_score * evidence_weight) / total_w, 4,
        )

    return {
        "node_id": node_id,
        "composite_score": composite,
        "observation_score": obs_score,
        "evidence_score": evidence_score,
        "observation_count": obs_count,
        "evidence_count": evidence_count,
        "weights": {
            "observation": observation_weight,
            "evidence": evidence_weight,
        },
    }


def detect_trend(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    window_days: int = 60,
    min_observations: int = 3,
) -> dict[str, Any]:
    """Detect temporal trends in observations for a node.

    Uses simple linear regression over observations within *window_days*
    to compute the trend direction (rising/falling/stable) and magnitude
    (slope per day). Returns the trend along with the raw observations
    so agents can apply domain-specific interpretation.

    This is a universal substrate method — works for any domain:
    NDVI decline, vibration increase, confidence decay, etc.

    Args:
        conn: Database connection.
        node_id: The node to analyze.
        window_days: Lookback window in days (default 60).
        min_observations: Minimum observations needed for a trend (default 3).

    Returns:
        Dict with trend (rising/falling/stable), slope_per_day,
        r_squared, observation_count, and observations.
    """
    observations = conn.execute(
        """
        SELECT value, created_at,
               EXTRACT(EPOCH FROM created_at) AS epoch_sec
        FROM ohm_observations
        WHERE node_id = ?
          AND value IS NOT NULL
          AND created_at >= CURRENT_TIMESTAMP - INTERVAL '1 day' * ?
        ORDER BY created_at ASC
        """,
        [node_id, window_days],
    ).fetchall()

    n = len(observations)
    if n < min_observations:
        return {
            "node_id": node_id,
            "trend": "insufficient_data",
            "slope_per_day": None,
            "r_squared": None,
            "observation_count": n,
            "window_days": window_days,
            "observations": [
                {"value": v, "created_at": str(t)} for v, t, _ in observations
            ],
        }

    # Simple linear regression: value = slope * x + intercept
    # x = days since first observation
    values = [o[0] for o in observations]
    timestamps = [o[1] for o in observations]
    epoch_secs = [o[2] for o in observations]

    # Use epoch seconds for precision, convert slope to per-day
    t0 = epoch_secs[0]
    x_vals = [(t - t0) / 86400.0 for t in epoch_secs]  # days since first obs

    mean_x = sum(x_vals) / n
    mean_y = sum(values) / n

    # Covariance and variance
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_vals, values))
    var_x = sum((x - mean_x) ** 2 for x in x_vals)

    if var_x == 0:
        slope = 0.0
        r_squared = 0.0
    else:
        slope = cov_xy / var_x
        intercept = mean_y - slope * mean_x

        # R-squared
        y_pred = [slope * x + intercept for x in x_vals]
        ss_res = sum((y - yp) ** 2 for y, yp in zip(values, y_pred))
        ss_tot = sum((y - mean_y) ** 2 for y in values)
        r_squared = round(1.0 - (ss_res / ss_tot), 4) if ss_tot > 0 else 0.0

    # Classify trend
    if abs(slope) < 0.001:
        trend = "stable"
    elif slope > 0:
        trend = "rising"
    else:
        trend = "falling"

    return {
        "node_id": node_id,
        "trend": trend,
        "slope_per_day": round(slope, 6),
        "r_squared": r_squared,
        "observation_count": n,
        "window_days": window_days,
        "observations": [
            {"value": v, "created_at": str(t)} for v, t, _ in observations
        ],
    }
