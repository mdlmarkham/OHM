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
    opposite = conn.execute(
        """
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
    """,
        [limit],
    ).fetchall()

    # Type 2: High-confidence challenges
    challenges = conn.execute(
        """
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
    """,
        [confidence_threshold, limit],
    ).fetchall()

    # Type 3: Same source, contradictory L3 interpretations
    # Two edges from the same source node, by different agents, with
    # opposing edge types (e.g., one CAUSES, one CONTRADICTS)
    contradictory = conn.execute(
        """
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
    """,
        [limit],
    ).fetchall()

    def _to_dicts(result_rows, col_names):
        """Convert result rows to dicts using provided column names."""
        if not result_rows:
            return []
        return [dict(zip(col_names, row)) for row in result_rows]

    # Get column names from each query's description
    # We need to re-execute with a simple query to get column names
    # Or just use hardcoded column lists from the SELECT statements

    opposite_cols = [
        "node_id",
        "node_label",
        "agent_a",
        "agent_b",
        "value_a",
        "value_b",
        "baseline",
        "gap",
        "time_a",
        "time_b",
    ]
    challenge_cols = [
        "challenge_id",
        "target_edge_id",
        "challenger",
        "original_author",
        "challenge_confidence",
        "original_confidence",
        "challenge_reason",
        "created_at",
    ]
    contradict_cols = [
        "source_node",
        "source_label",
        "edge_type_a",
        "edge_type_b",
        "agent_a",
        "agent_b",
        "conf_a",
        "conf_b",
        "reason_a",
        "reason_b",
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
    existing = conn.execute("SELECT 1 FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).fetchone()

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

    # Return updated state + verification_overdue (ADR-018)
    result = conn.execute("SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).fetchone()
    if result:
        columns = [desc[0] for desc in conn.execute("SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).description]
        state = dict(zip(columns, result))
    else:
        state = {}

    # ADR-018.3: Include unverified causal edges that this agent created
    # so they can record outcomes and prevent confidence decay
    verification_overdue = conn.execute(
        """
        SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
               e.created_at,
               EXTRACT(DAY FROM CURRENT_TIMESTAMP - e.created_at) AS age_days,
               fn.label AS from_label, tn.label AS to_label
        FROM ohm_edges e
        LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
        LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
        WHERE e.layer = 'L3'
          AND e.edge_type IN ('CAUSES', 'PREDICTS', 'EXPECTS')
          AND e.deleted_at IS NULL
          AND e.created_by = ?
          AND NOT EXISTS (
              SELECT 1 FROM ohm_outcomes oc
              WHERE oc.claim_node = e.from_node
          )
          AND e.created_at < CURRENT_TIMESTAMP - INTERVAL '14 day'
        ORDER BY e.confidence DESC, e.created_at ASC
        LIMIT 20
    """,
        [agent_name],
    ).fetchall()

    if verification_overdue:
        state["verification_overdue"] = [
            {
                "edge_id": row[0],
                "from_node": row[1],
                "to_node": row[2],
                "edge_type": row[3],
                "confidence": row[4],
                "created_at": str(row[5]) if row[5] else None,
                "age_days": round(float(row[6]), 1) if row[6] else 0.0,
                "from_label": row[7],
                "to_label": row[8],
            }
            for row in verification_overdue
        ]
        state["verification_overdue_count"] = len(verification_overdue)
    else:
        state["verification_overdue"] = []
        state["verification_overdue_count"] = 0

    return state


def query_agent_health(
    conn: DuckDBPyConnection,
) -> list[dict[str, Any]]:
    """Check health of all registered agents.

    Includes all agents from ohm_agent_state (heartbeat history) plus any
    agents registered via ohm_nodes (type='agent') that have no state row yet.

    Returns:
        List of agent health records with status (alive/stale/dead/unknown).
    """
    result = conn.execute("""
        WITH state_agents AS (
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
        ),
        node_agents AS (
            SELECT
                n.id AS agent_name,
                NULL AS current_focus,
                NULL AS last_sync,
                n.updated_at,
                NULL AS sync_interval_sec,
                NULL AS optimization_target,
                'registered' AS status,
                NULL AS minutes_since_heartbeat
            FROM ohm_nodes n
            WHERE n.type = 'agent' AND n.deleted_at IS NULL
              -- Skip if already tracked in ohm_agent_state under the same name
              AND n.id NOT IN (SELECT agent_name FROM ohm_agent_state)
              -- Skip if this is the 'agent-X' or 'agent_X' prefixed form of a tracked agent
              -- (avoids duplicates when agents register as 'metis' and 'agent-metis' or 'agent_metis')
              AND REGEXP_REPLACE(n.id, '^agent[-_]', '') NOT IN (SELECT agent_name FROM ohm_agent_state)
              -- Skip if the bare name has an 'agent-' or 'agent_' prefixed version already tracked
              AND ('agent-' || n.id) NOT IN (SELECT agent_name FROM ohm_agent_state)
              AND ('agent_' || n.id) NOT IN (SELECT agent_name FROM ohm_agent_state)
        )
        SELECT *, CASE status WHEN 'alive' THEN 1 WHEN 'registered' THEN 2 WHEN 'stale' THEN 3 ELSE 4 END AS sort_key FROM state_agents
        UNION ALL
        SELECT *, 2 AS sort_key FROM node_agents
        ORDER BY sort_key, agent_name
    """)

    columns = [desc[0] for desc in result.description]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    for r in rows:
        r.pop("sort_key", None)
    return rows


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
        same_direction = sum(1 for v in values if (v > avg_baseline and agg_value > avg_baseline) or (v < avg_baseline and agg_value < avg_baseline) or (v == avg_baseline))
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
    default_probability: float = 0.5,
    seed: int | None = None,
) -> dict[str, Any]:
    """Monte Carlo simulation of failure propagation from a node.

    Two-stage sampling per ADR-008:
    - Stage 1: Edge existence — sample random() < confidence
    - Stage 2: Effect propagation — sample random() < probability

    An edge with confidence=0.9 and probability=0.1 activates ~9% of trials,
    correctly modeling "we're 90% sure this edge exists, but even if it does,
    the effect only happens 10% of the time."

    Same result regardless of which agent calls it — substrate method.
    No domain judgment involved — purely mechanical confidence sampling.

    Args:
        node_id: Source node for impact simulation.
        simulations: Number of Monte Carlo trials (default 1000).
        depth: Maximum traversal depth (default 3).
        confidence_threshold: Minimum confidence to consider an edge (default 0.5).
        default_probability: Default probability when edge has no probability set (default 0.5).
        seed: Random seed for reproducibility (default None).

    Returns:
        Dict with: affected_nodes (list of {id, label, impact_probability}),
        simulation_count, depth, mean_affected, max_affected.
    """
    import random

    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    if seed is not None:
        random.seed(seed)

    # Build adjacency from the graph — fetch both confidence AND probability
    edges = conn.execute(
        """SELECT from_node, to_node, edge_type, confidence, probability, layer
           FROM ohm_edges
           WHERE layer IN ('L1', 'L2', 'L3')
             AND confidence >= ?""",
        [confidence_threshold],
    ).fetchall()

    # Adjacency list: node -> [(target, confidence, probability)]
    # ADR-008: probability and confidence are semantically distinct.
    # probability = P(effect|cause), confidence = belief in edge existence.
    adj: dict[str, list[tuple[str, float, float]]] = {}
    for from_node, to_node, edge_type, conf, prob, layer in edges:
        if from_node not in adj:
            adj[from_node] = []
        # Use probability if set, otherwise default_probability
        effective_prob = float(prob) if prob is not None else default_probability
        adj[from_node].append((to_node, float(conf or 0.7), effective_prob))

    # Run simulations with two-stage sampling
    impact_counts: dict[str, int] = {}
    total_affected_per_sim = []

    for _ in range(simulations):
        visited = set()
        frontier = [node_id]
        affected_this_sim = 0

        for _ in range(depth):
            next_frontier = []
            for current in frontier:
                if current in visited:
                    continue
                visited.add(current)
                if current not in adj:
                    continue
                for target, conf, prob in adj[current]:
                    if target in visited:
                        continue
                    # Stage 1: Does this edge exist? (confidence)
                    if random.random() < conf:
                        # Stage 2: Does the effect propagate? (probability)
                        if random.random() < prob:
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
        affected_nodes.append(
            {
                "id": nid,
                "label": node_labels.get(nid, nid),
                "impact_probability": round(count / simulations, 4),
            }
        )

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
            result.append(
                {
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
                    "time_gap_seconds": round(abs((row[11] - row[10]).total_seconds()) if row[10] and row[11] else 0, 1),
                }
            )

    return result


def detect_alias_duplicates(
    conn: DuckDBPyConnection,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find nodes that may be duplicates via alias collision (OHM-g0kv).

    Two nodes are alias duplicates if their normalized labels are identical
    (same ohm_aliases.alias_norm) but they have different node_ids. This
    catches "Hormuz AND-Gate" vs "hormuz_and_gate" vs "Strait of Hormuz AND-Gate".

    Also finds content hash collisions: different nodes with the same
    content_hash in ohm_content_hashes.

    Returns:
        List of duplicate groups with alias_norm, node_ids, and labels.
    """
    rows = conn.execute(
        """
        SELECT a.alias_norm, a.node_id AS node_a, n1.label AS label_a,
               b.node_id AS node_b, n2.label AS label_b
        FROM ohm_aliases a
        JOIN ohm_aliases b ON a.alias_norm = b.alias_norm AND a.node_id < b.node_id
        LEFT JOIN ohm_nodes n1 ON n1.id = a.node_id AND n1.deleted_at IS NULL
        LEFT JOIN ohm_nodes n2 ON n2.id = b.node_id AND n2.deleted_at IS NULL
        WHERE n1.id IS NOT NULL AND n2.id IS NOT NULL
        ORDER BY a.alias_norm
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    alias_dups = []
    for row in rows:
        alias_dups.append({
            "alias_norm": row[0],
            "node_a": row[1],
            "label_a": row[2],
            "node_b": row[3],
            "label_b": row[4],
            "kind": "alias_collision",
        })

    hash_rows = conn.execute(
        """
        SELECT ch1.content_hash, ch1.node_id AS node_a, n1.label AS label_a,
               ch2.node_id AS node_b, n2.label AS label_b
        FROM ohm_content_hashes ch1
        JOIN ohm_content_hashes ch2 ON ch1.content_hash = ch2.content_hash
             AND ch1.node_id < ch2.node_id
        LEFT JOIN ohm_nodes n1 ON n1.id = ch1.node_id AND n1.deleted_at IS NULL
        LEFT JOIN ohm_nodes n2 ON n2.id = ch2.node_id AND n2.deleted_at IS NULL
        WHERE n1.id IS NOT NULL AND n2.id IS NOT NULL
        ORDER BY ch1.content_hash
        LIMIT ?
        """,
        [limit],
    ).fetchall()

    hash_dups = []
    for row in hash_rows:
        hash_dups.append({
            "content_hash": row[0],
            "node_a": row[1],
            "label_a": row[2],
            "node_b": row[3],
            "label_b": row[4],
            "kind": "content_hash_collision",
        })

    return alias_dups + hash_dups


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

    Normalizes expected challenge rates by the global challenge rate across
    all agents, so agents in low-activity graphs aren't penalized for having
    few challenges.

    Same result regardless of which agent calls it — substrate method.

    Args:
        agent_name: Agent to evaluate.

    Returns:
        Dict with: agent_name, total_edges, calibration_by_band,
        overall_calibration_score (0-1, 1 = perfectly calibrated),
        global_challenge_rate, base_rate_adjusted.
    """
    # Count total L3/L4 edges globally (for base rate normalization)
    global_total_row = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE layer IN ('L3', 'L4') AND deleted_at IS NULL").fetchone()
    global_total_edges = global_total_row[0] if global_total_row else 0

    # Count globally challenged edges (edges with a CHALLENGED_BY edge pointing to them)
    global_challenged_row = conn.execute(
        """
        SELECT COUNT(DISTINCT e.id)
        FROM ohm_edges c
        JOIN ohm_edges e ON c.challenge_of = e.id
        WHERE c.challenge_type = 'CHALLENGED_BY'
          AND e.layer IN ('L3', 'L4')
          AND e.deleted_at IS NULL
        """
    ).fetchone()
    global_challenged_edges = global_challenged_row[0] if global_challenged_row else 0

    # Global challenge rate: what fraction of all L3/L4 edges are challenged?
    global_challenge_rate = global_challenged_edges / max(global_total_edges, 1)

    # Count edges by confidence band for this agent
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

    # Band midpoints for expected rate calculation
    band_midpoints = {
        "0.9-1.0": 0.95,
        "0.7-0.9": 0.8,
        "0.5-0.7": 0.6,
        "0.3-0.5": 0.4,
        "0.0-0.3": 0.15,
    }

    # Calibration score: higher-confidence bands should have LOWER challenge rates
    # Normalize expected rates by global challenge rate to account for graph activity
    # Base rate assumption: a perfectly calibrated agent at 50% confidence would
    # have a challenge rate equal to the global rate.
    # Scale factor: global_rate / 0.5 (since 1 - 0.5 = 0.5 is the midpoint expected rate)
    # When global rate is 0 (no challenges anywhere), expected rate is 0 for all bands
    scale_factor = global_challenge_rate / 0.5 if global_challenge_rate > 0 else 0.0

    calibration_by_band = []
    weighted_error = 0.0
    total_weight = 0.0

    for band_name, total, challenged_in_band in bands:
        challenge_rate = challenged_in_band / max(total, 1)
        band_midpoint = band_midpoints.get(band_name, 0.5)

        # Expected challenge rate: (1 - midpoint) scaled by global activity
        # High confidence → low expected challenge rate
        # Low confidence → high expected challenge rate
        # Scaled by global_challenge_rate / 0.5 to normalize for graph activity
        expected_rate = (1.0 - band_midpoint) * scale_factor
        # Clamp to [0, 1]
        expected_rate = min(1.0, max(0.0, expected_rate))

        calibration_by_band.append(
            {
                "band": band_name,
                "total_edges": total,
                "challenged": challenged_in_band,
                "challenge_rate": round(challenge_rate, 4),
                "expected_rate": round(expected_rate, 4),
            }
        )

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
        "global_challenge_rate": round(global_challenge_rate, 4),
        "base_rate_adjusted": global_total_edges > 0,
        "calibration_by_band": calibration_by_band,
        "calibration_score": calibration_score,
        "interpretation": ("well_calibrated" if calibration_score and calibration_score > 0.7 else "overconfident" if calibration_score and calibration_score < 0.3 else "underexamined" if total_edges < 5 else "needs_data"),
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
    affected = conn.execute(
        """
        SELECT id, edge_type, layer, confidence, created_by,
               created_at,
               EXTRACT(DAY FROM CURRENT_TIMESTAMP - created_at) AS age_days
        FROM ohm_edges
        WHERE layer IN ('L3', 'L4')
          AND confidence > ?
          AND created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'
        ORDER BY age_days DESC
    """,
        [min_confidence],
    ).fetchall()

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
            decayed.append(
                {
                    "id": edge_id,
                    "edge_type": etype,
                    "layer": layer,
                    "original_confidence": conf,
                    "new_confidence": new_conf,
                    "age_days": round(age_days, 1),
                    "created_by": created_by,
                }
            )

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
        "summary": (f"Decayed {len(decayed)} edges (half-life: {half_life_days}d, floor: {min_confidence})"),
    }


def composite_score(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    observation_weight: float = 0.5,
    evidence_weight: float = 0.5,
    method: str = "arithmetic",
    baseline: float = 1.0,
    temporal_decay_hours: float | None = None,
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

    Two composition methods:
    - 'arithmetic': weighted arithmetic mean (default, backwards compatible)
    - 'geometric': geometric mean for multiplicative factors (demand forecasting)

    For geometric mode with baseline:
    - Values are treated as multipliers from baseline
    - baseline=1.0 means values are 1.0 = no change, 2.0 = double
    - Result is expressed as a multiplier from baseline

    Temporal decay:
    - When temporal_decay_hours is set, observation values are weighted by
      0.5^(age_hours / temporal_decay_hours). This makes stale observations
      contribute less to the composite score.
    - Retail example: temporal_decay_hours=4.0 (weather relevant for ~4 hours)
    - Cattle example: temporal_decay_hours=168.0 (NDVI relevant for ~7 days)

    This is a universal substrate method — works for any domain.

    Args:
        conn: Database connection.
        node_id: The node to score.
        observation_weight: Weight for observation signal (0-1).
        evidence_weight: Weight for evidence signal (0-1).
        method: 'arithmetic' (default) or 'geometric' (multiplicative).
        baseline: Baseline for multiplicative mode (default 1.0).
        temporal_decay_hours: Half-life in hours for temporal decay of
            observations. None (default) disables temporal weighting.

    Returns:
        Dict with composite_score, observation_score, evidence_score,
        observation_count, evidence_count, and components.
    """
    from ohm.queries import query_confidence_chain

    # ── Observation score ──────────────────────────────────────────
    if temporal_decay_hours is not None and temporal_decay_hours > 0:
        obs_result = conn.execute(
            """SELECT value, sigma,
                  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 3600.0 AS age_hours
               FROM ohm_observations
               WHERE node_id = ? AND value IS NOT NULL""",
            [node_id],
        ).fetchall()
    else:
        obs_result = conn.execute(
            "SELECT value, sigma FROM ohm_observations WHERE node_id = ? AND value IS NOT NULL",
            [node_id],
        ).fetchall()

    obs_score: float | None = None
    obs_count = len(obs_result)
    if obs_result:
        total_weight = 0.0
        weighted_sum = 0.0
        for row in obs_result:
            value = row[0]
            sigma = row[1]
            # Base weight from inverse-variance
            if sigma and sigma > 0:
                w = 1.0 / (sigma**2)
            else:
                w = 1.0
            # Apply temporal decay if enabled
            if temporal_decay_hours is not None and temporal_decay_hours > 0:
                age_hours = row[2] if len(row) > 2 else 0.0
                # Decay factor: 0.5^(age/half_life)
                decay = 0.5 ** (age_hours / temporal_decay_hours) if age_hours is not None else 1.0
                w *= decay
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
        if method == "geometric" and composite is not None and baseline != 1.0:
            composite = round(composite * baseline, 4)
    elif evidence_score is None:
        composite = obs_score
        if method == "geometric" and composite is not None and baseline != 1.0:
            composite = round(composite * baseline, 4)
    else:
        if method == "geometric" and obs_score > 0 and evidence_score > 0:
            # Weighted geometric mean for multiplicative factors
            total_w = observation_weight + evidence_weight
            if total_w > 0:
                composite = round((obs_score ** (observation_weight / total_w) * evidence_score ** (evidence_weight / total_w)), 4)
            else:
                composite = round((obs_score * evidence_score) ** 0.5, 4)
            # Apply baseline scaling
            if baseline != 1.0:
                composite = round(composite * baseline, 4)
        else:
            # Default: weighted arithmetic mean (backwards compatible)
            total_w = observation_weight + evidence_weight
            composite = round(
                (obs_score * observation_weight + evidence_score * evidence_weight) / total_w,
                4,
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
        "method": method,
        "baseline": baseline,
        "temporal_decay_hours": temporal_decay_hours,
    }


def decay_observations(
    conn: DuckDBPyConnection,
    node_id: str | None = None,
    *,
    temporal_decay_hours: float = 4.0,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Compute time-decayed observation values using exponential half-life.

    For each observation, computes an effective value weighted by how recently
    it was observed. The decay formula is:

        effective_weight = 0.5^(age_hours / temporal_decay_hours)

    This means an observation that is one half-life old contributes at 50%,
    two half-lives at 25%, etc.

    In dry_run mode, returns what would change without modifying the database.
    In non-dry_run mode, updates observation values in-place.

    Args:
        conn: Database connection.
        node_id: Optional node ID to filter. None = all observations.
        temporal_decay_hours: Half-life in hours (default 4.0).
            Retail: 4.0 (weather relevant for ~4 hours)
            Cattle: 168.0 (NDVI relevant for ~7 days)
        dry_run: If True, return what would change without modifying data.

    Returns:
        List of dicts with observation id, node_id, original value,
        decayed_value, age_hours, and decay_factor.
    """
    # Query observations with age
    if node_id:
        rows = conn.execute(
            """SELECT id, node_id, value, sigma,
                  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 3600.0 AS age_hours
               FROM ohm_observations
               WHERE node_id = ? AND value IS NOT NULL
               ORDER BY created_at DESC""",
            [node_id],
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, node_id, value, sigma,
                  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 3600.0 AS age_hours
               FROM ohm_observations
               WHERE value IS NOT NULL
               ORDER BY created_at DESC""",
        ).fetchall()

    results = []
    for row in rows:
        obs_id, obs_node_id, value, sigma, age_hours = row
        age_hours = age_hours or 0.0
        decay_factor = 0.5 ** (age_hours / temporal_decay_hours)
        decayed_value = round(value * decay_factor, 6) if value is not None else None

        results.append(
            {
                "id": obs_id,
                "node_id": obs_node_id,
                "original_value": value,
                "decayed_value": decayed_value,
                "age_hours": round(age_hours, 4),
                "decay_factor": round(decay_factor, 6),
                "sigma": sigma,
            }
        )

        if not dry_run and decayed_value is not None:
            conn.execute(
                "UPDATE ohm_observations SET value = ? WHERE id = ?",
                [decayed_value, obs_id],
            )

    return results


def apply_verification_decay(
    conn: DuckDBPyConnection,
    *,
    unverified_half_life_days: float = 30.0,
    verified_half_life_days: float = 365.0,
    min_confidence: float = 0.1,
    verification_grace_days: float = 14.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Decay edge confidence based on verification status (ADR-018.3).

    Unverified causal edges decay with a 30-day half-life. Verified edges
    (those with recorded outcomes confirming or falsifying them) decay
    with a 365-day half-life. Edges within the grace period (14 days)
    are not decayed.

    This implements the structural enforcement of Verification Loops
    (Karpathy Rule 8): claims that aren't verified decay, rather than
    persisting as sacred references.

    Only affects L3 (knowledge) CAUSES/PREDICTS/EXPECTS edges.

    Args:
        conn: Database connection.
        unverified_half_life_days: Half-life for unverified edges (default 30).
        verified_half_life_days: Half-life for verified edges (default 365).
        min_confidence: Floor for decayed confidence (default 0.1).
        verification_grace_days: Days before decay starts (default 14).
        dry_run: If True, return affected edges without modifying.

    Returns:
        Dict with decayed_count, verified_count, unverified_count, affected_edges.
    """
    # Find L3 causal edges eligible for decay
    edges = conn.execute(
        """
        SELECT e.id, e.edge_type, e.from_node, e.to_node, e.confidence,
               e.created_by, e.created_at,
               EXTRACT(DAY FROM CURRENT_TIMESTAMP - e.created_at) AS age_days,
               CASE WHEN EXISTS (
                   SELECT 1 FROM ohm_outcomes oc
                   WHERE oc.claim_node = e.from_node
               ) THEN TRUE ELSE FALSE END AS is_verified
        FROM ohm_edges e
        WHERE e.layer = 'L3'
          AND e.edge_type IN ('CAUSES', 'PREDICTS', 'EXPECTS')
          AND e.deleted_at IS NULL
          AND e.confidence > ?
          AND e.created_at < CURRENT_TIMESTAMP - ? * INTERVAL '1 day'
        ORDER BY e.confidence DESC
    """,
        [min_confidence, verification_grace_days],
    ).fetchall()

    if not edges:
        return {"decayed_count": 0, "verified_count": 0, "unverified_count": 0,
                "affected_edges": [], "dry_run": dry_run,
                "summary": "No edges eligible for verification decay"}

    decayed = []
    verified_count = 0
    unverified_count = 0

    for row in edges:
        edge_id, etype, from_node, to_node, conf, created_by, created_at, age_days, is_verified = row
        age_days = float(age_days) if age_days else 0.0
        is_verified = bool(is_verified)

        if is_verified:
            half_life = verified_half_life_days
            verified_count += 1
        else:
            half_life = unverified_half_life_days
            unverified_count += 1

        decay_factor = 0.5 ** (age_days / half_life)
        new_conf = round(conf * decay_factor, 4)
        new_conf = max(new_conf, min_confidence)

        if new_conf < conf:
            decayed.append({
                "id": edge_id,
                "edge_type": etype,
                "from_node": from_node,
                "to_node": to_node,
                "original_confidence": conf,
                "new_confidence": new_conf,
                "age_days": round(age_days, 1),
                "is_verified": is_verified,
                "half_life_used": half_life,
                "decay_factor": round(decay_factor, 4),
            })

            if not dry_run:
                conn.execute(
                    "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [new_conf, edge_id],
                )

    return {
        "decayed_count": len(decayed),
        "verified_count": verified_count,
        "unverified_count": unverified_count,
        "affected_edges": decayed[:50],  # Limit output
        "dry_run": dry_run,
        "unverified_half_life_days": unverified_half_life_days,
        "verified_half_life_days": verified_half_life_days,
        "verification_grace_days": verification_grace_days,
        "summary": (f"Decayed {len(decayed)} edges: "
                    f"{unverified_count} unverified (half-life {unverified_half_life_days}d), "
                    f"{verified_count} verified (half-life {verified_half_life_days}d)"),
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
            "observations": [{"value": v, "created_at": str(t)} for v, t, _ in observations],
        }

    # Simple linear regression: value = slope * x + intercept
    # x = days since first observation
    values = [o[0] for o in observations]
    [o[1] for o in observations]
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
        "observations": [{"value": v, "created_at": str(t)} for v, t, _ in observations],
    }


def _compute_diversity_correlation(observations: list[dict[str, Any]]) -> float | None:
    """Compute effective correlation from source diversity.

    Examines created_by and created_at to estimate how correlated
    observations are:
    - Same agent, same day: 0.9 (near-duplicate)
    - Same agent, different day: 0.6 (same perspective, different evidence)
    - Different agent: 0.2 (independent perspective)

    Returns None if insufficient data to compute diversity (all from
    same source or missing timestamps), indicating correlation should
    be user-specified or default (0.0).
    """
    if len(observations) < 2:
        return None

    pairs: list[tuple[str, str | None, float]] = []
    for obs in observations:
        source = obs.get("created_by") or obs.get("source") or "_unknown_"
        day = None
        created_at = obs.get("created_at")
        if created_at:
            day = str(created_at)[:10] if len(str(created_at)) >= 10 else None
        pairs.append((source, day, obs.get("confidence", 0.5)))

    same_agent_same_day_weighted: float = 0.0
    same_agent_diff_day_weighted: float = 0.0
    diff_agent_weighted: float = 0.0
    total_weight: float = 0.0

    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            src_i, day_i, conf_i = pairs[i]
            src_j, day_j, conf_j = pairs[j]
            weight = (conf_i + conf_j) / 2.0
            total_weight += weight

            if src_i == src_j:
                if day_i and day_j and day_i == day_j:
                    same_agent_same_day_weighted += weight
                else:
                    same_agent_diff_day_weighted += weight
            else:
                diff_agent_weighted += weight

    if total_weight <= 0.0:
        return None

    same_day_frac = same_agent_same_day_weighted / total_weight
    same_agent_frac = same_agent_diff_day_weighted / total_weight
    diff_agent_frac = diff_agent_weighted / total_weight

    effective_corr = (
        same_day_frac * 0.9 + same_agent_frac * 0.6 + diff_agent_frac * 0.2
    )
    return round(effective_corr, 3)


def compound_confidence(
    observations: list[dict[str, Any]],
    *,
    correlation: float | None = None,
    source_weights: dict[str, float] | None = None,
    use_diversity_correlation: bool = False,
) -> dict[str, Any]:
    """Combine multiple confidence values accounting for correlation and source reliability.

    When observations are independent (correlation=0.0), confidences compound
    multiplicatively: P(all) = 1 - Π(1 - p_i). When perfectly correlated
    (correlation=1.0), only the strongest evidence matters: result = max(p_i).

    When source_weights is provided, observations from reliable sources count
    more. An observation with weight w contributes w times its confidence to
    the compound probability. Weights are normalized so they sum to the number
    of observations (maintaining backward compatibility).

    When use_diversity_correlation is True, automatically computes effective
    correlation from source diversity (created_by and created_at fields):
    - Same agent, same day: 0.9 (near-duplicate)
    - Same agent, different day: 0.6 (same perspective, different evidence)
    - Different agent: 0.2 (independent perspective)

    This is critical for medical diagnosis where two findings from the same
    modality (e.g., two blood tests from the same lab) are correlated and
    shouldn't double-count evidence, while findings from different modalities
    (imaging + blood work) are independent and should compound.

    Args:
        observations: List of dicts with 'confidence' key (0-1).
            May also include 'source' or 'created_by' for source diversity.
            May include 'created_at' for temporal diversity.
        correlation: Override correlation (0.0-1.0). If None and
            use_diversity_correlation=False, defaults to 0.0 (independent).
        source_weights: Optional dict mapping source_agent -> reliability
            weight (e.g., {"agent_a": 0.9, "agent_b": 0.5}). Default weight
            is 0.5 for unknown sources. Higher weights = more influence.
        use_diversity_correlation: If True and correlation is None, compute
            effective correlation from source diversity. This helps
            distinguish single-agent echo chambers from multi-agent validation.

    Returns:
        Dict with compound_confidence, method, correlation, observation_count,
        weighted (bool), diversity_correlation (float if computed), and
        source_diversity_metrics (dict with agent_count, same_day_duplicates, etc.).
    """
    if not observations:
        return {
            "compound_confidence": None,
            "method": "compound",
            "correlation": 0.0,
            "observation_count": 0,
            "weighted": source_weights is not None,
        }

    diversity_correlation: float | None = None
    diversity_metrics: dict[str, Any] = {}

    if correlation is None:
        if use_diversity_correlation:
            diversity_correlation = _compute_diversity_correlation(observations)
            if diversity_correlation is not None:
                correlation = diversity_correlation
            else:
                correlation = 0.0
        else:
            correlation = 0.0
    else:
        correlation = max(0.0, min(1.0, correlation))

    if use_diversity_correlation:
        agents = set()
        same_day_pairs = 0
        agent_days: dict[str, set[str]] = {}
        for obs in observations:
            source = obs.get("created_by") or obs.get("source") or "_unknown_"
            agents.add(source)
            day = None
            created_at = obs.get("created_at")
            if created_at:
                day = str(created_at)[:10] if len(str(created_at)) >= 10 else None
            if source not in agent_days:
                agent_days[source] = set()
            if day:
                agent_days[source].add(day)
        total_day_pairs = sum(len(days) * (len(days) - 1) // 2 for days in agent_days.values())
        same_day_pairs = total_day_pairs
        diversity_metrics = {
            "agent_count": len(agents),
            "same_day_pairs": same_day_pairs,
            "unique_agents": list(agents),
        }

    default_weight = 0.5
    use_weighting = source_weights is not None

    weighted_confidences: list[tuple[float, float]] = []
    for obs in observations:
        c = obs.get("confidence", 0.0)
        try:
            c = float(c)
        except (TypeError, ValueError):
            c = 0.0
        c = max(0.0, min(1.0, c))

        if use_weighting:
            assert source_weights is not None
            source = obs.get("source") or obs.get("created_by") or "_unknown_"
            w = source_weights.get(source, default_weight)
            weighted_confidences.append((c, w))
        else:
            weighted_confidences.append((c, 1.0))

    n = len(weighted_confidences)

    if correlation >= 1.0:
        if use_weighting:
            result = max(c * w for c, w in weighted_confidences)
        else:
            result = max(c for c, _ in weighted_confidences)
    elif correlation <= 0.0:
        product = 1.0
        for c, w in weighted_confidences:
            effective = min(1.0, w * c)
            product *= 1.0 - effective
        result = round(1.0 - product, 4)
    else:
        product = 1.0
        for c, w in weighted_confidences:
            effective = min(1.0, w * c)
            product *= 1.0 - effective
        independent = 1.0 - product
        if use_weighting:
            correlated = max(c * w for c, w in weighted_confidences)
        else:
            correlated = max(c for c, _ in weighted_confidences)
        result = round(correlated * correlation + independent * (1.0 - correlation), 4)

    ret: dict[str, Any] = {
        "compound_confidence": result,
        "method": "compound",
        "correlation": correlation,
        "observation_count": n,
        "weighted": use_weighting,
    }
    if diversity_correlation is not None:
        ret["diversity_correlation"] = diversity_correlation
    if diversity_metrics:
        ret["source_diversity_metrics"] = diversity_metrics
    return ret


def differential_diagnosis(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    max_depth: int = 3,
) -> list[dict[str, Any]]:
    """Return candidate diagnoses for a patient node, ranked by composite score.

    Walks incoming evidence edges (CAUSES, PREDICTS, CORRELATES_WITH, etc.)
    to find candidate condition nodes, then excludes any conditions that are
    ruled out by NEGATES edges. Results are sorted by composite_score descending.

    This is a medical-domain substrate method, but works for any domain where
    you need to find candidate explanations ranked by evidence strength while
    excluding ruled-out alternatives.

    Args:
        conn: Database connection.
        node_id: The patient/finding node to diagnose.
        max_depth: Maximum traversal depth for evidence chain.

    Returns:
        List of dicts with node_id, label, composite_score, ruled_out (bool),
        ruled_out_by (list of NEGATES edge ids if applicable).
    """

    # Find candidate conditions: nodes that have evidence edges pointing to
    # or from this node (CAUSES, PREDICTS, CORRELATES_WITH, SUPPORTS, etc.)
    candidates = conn.execute(
        """SELECT DISTINCT n.id, n.label, n.type
           FROM ohm_edges e
           JOIN ohm_nodes n ON (
               n.id = e.from_node OR n.id = e.to_node
           )
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.edge_type IN (
               'CAUSES', 'PREDICTS', 'CORRELATES_WITH', 'SUPPORTS',
               'EXPECTED_LIKELIHOOD', 'EXPLAINS'
             )
             AND n.id != ?
        """,
        [node_id, node_id, node_id],
    ).fetchall()

    # Find conditions ruled out by NEGATES edges
    # A candidate is ruled out if any node has a NEGATES edge pointing to it.
    # We check: does any of the candidate nodes have an incoming NEGATES edge?
    ruled_out_map: dict[str, list[str]] = {}

    if candidates:
        cand_ids = [c[0] for c in candidates]
        # Build parameterized query for all candidate IDs
        placeholders = ",".join(["?"] * len(cand_ids))
        negated_edges = conn.execute(
            f"""SELECT e.to_node, e.id, e.from_node
                FROM ohm_edges e
                WHERE e.to_node IN ({placeholders})
                  AND e.edge_type = 'NEGATES'
            """,
            cand_ids,
        ).fetchall()
        for to_node, edge_id, from_node in negated_edges:
            ruled_out_map.setdefault(to_node, []).append(edge_id)

    # Also: the patient node itself may NEGATE conditions
    # (patient finding rules out condition)
    negated_by_node = conn.execute(
        """SELECT e.to_node, e.id
           FROM ohm_edges e
           WHERE e.from_node = ?
             AND e.edge_type = 'NEGATES'
        """,
        [node_id],
    ).fetchall()
    for to_node, edge_id in negated_by_node:
        ruled_out_map.setdefault(to_node, []).append(edge_id)

    # Build results
    results = []
    for cand_id, cand_label, cand_type in candidates:
        # Get composite score for this candidate
        try:
            score_result = composite_score(conn, cand_id)
            score = score_result.get("composite_score")
        except Exception:
            score = None

        is_ruled_out = cand_id in ruled_out_map
        results.append(
            {
                "node_id": cand_id,
                "label": cand_label,
                "type": cand_type,
                "composite_score": score,
                "ruled_out": is_ruled_out,
                "ruled_out_by": ruled_out_map.get(cand_id, []),
            }
        )

    # Sort: non-ruled-out first, then by composite_score descending
    results.sort(key=lambda r: (r["ruled_out"], -(r["composite_score"] or 0)))

    return results


def find_orphans(
    conn: DuckDBPyConnection,
    *,
    node_type: str | None = None,
    exclude_system: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find nodes with zero edges — completely disconnected from the graph.

    Orphans are notes that need connections. They're stored but not integrated.
    Every orphan is a missed opportunity for discovery.

    Args:
        node_type: Filter to a specific node type (e.g., 'concept').
        exclude_system: Exclude system nodes (agents, skills, values, goals).
        limit: Maximum results.

    Returns:
        List of orphan node dicts with id, label, type, provenance.
    """
    excluded_types = ["agent", "skill", "value", "goal"] if exclude_system else []
    type_clause = "AND n.type = ?" if node_type else ""
    exclude_clause = "AND n.type NOT IN ({})".format(",".join("?" * len(excluded_types))) if excluded_types else ""

    params: list[Any] = []
    if node_type:
        params.append(node_type)
    params.extend(excluded_types)
    params.append(limit)

    query = f"""
        SELECT n.id, n.label, n.type, n.provenance, n.confidence
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
        WHERE e.id IS NULL
        AND n.deleted_at IS NULL
        {type_clause}
        {exclude_clause}
        ORDER BY n.confidence DESC NULLS LAST
        LIMIT ?
    """
    rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "provenance": row[3],
            "confidence": round(row[4], 2) if row[4] is not None else None,
        }
        for row in rows
    ]


def find_hubs(
    conn: DuckDBPyConnection,
    *,
    node_type: str | None = None,
    min_connections: int = 3,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find the most-connected nodes — hubs that anchor the graph.

    Hubs are the opposite of orphans: nodes that many other nodes connect to.
    They're the concepts that tie everything together.

    Args:
        node_type: Filter to a specific node type.
        min_connections: Minimum number of connections to be considered a hub.
        limit: Maximum results.

    Returns:
        List of hub node dicts sorted by connection count descending.
    """
    type_clause = "AND n.type = ?" if node_type else ""
    params: list[Any] = []
    if node_type:
        params.append(node_type)
    params.extend([min_connections, limit])

    query = f"""
        SELECT n.id, n.label, n.type, n.confidence,
               COUNT(e.id) AS connections
        FROM ohm_nodes n
        JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
        WHERE n.deleted_at IS NULL
        AND e.deleted_at IS NULL
        {type_clause}
        GROUP BY n.id, n.label, n.type, n.confidence
        HAVING COUNT(e.id) >= ?
        ORDER BY connections DESC
        LIMIT ?
    """
    rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "confidence": round(row[3], 2) if row[3] is not None else None,
            "connections": row[4],
        }
        for row in rows
    ]


def find_dead_ends(
    conn: DuckDBPyConnection,
    *,
    node_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find nodes with only incoming edges — dead ends that don't lead anywhere.

    A dead end has edges pointing TO it but none FROM it. It's a sink.
    In a knowledge graph, dead ends mean you can reach this concept but
    can't follow it further. They need outgoing connections.

    Args:
        node_type: Filter to a specific node type.
        limit: Maximum results.

    Returns:
        List of dead-end node dicts.
    """
    type_clause = "AND n.type = ?" if node_type else ""
    params: list[Any] = []
    if node_type:
        params.append(node_type)
    params.append(limit)

    query = f"""
        SELECT n.id, n.label, n.type, n.confidence,
               incoming.incoming
        FROM ohm_nodes n
        JOIN (
            SELECT to_node, COUNT(*) AS incoming
            FROM ohm_edges
            WHERE deleted_at IS NULL
            GROUP BY to_node
        ) incoming ON incoming.to_node = n.id
        WHERE n.deleted_at IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM ohm_edges e2
            WHERE e2.from_node = n.id
            AND e2.deleted_at IS NULL
        )
        {type_clause}
        ORDER BY incoming.incoming DESC
        LIMIT ?
    """
    rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "confidence": round(row[3], 2) if row[3] is not None else None,
            "incoming": row[4],
        }
        for row in rows
    ]


def suggest_connections(
    conn: DuckDBPyConnection,
    *,
    method: str = "shared_provenance",
    min_shared: int = 2,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Suggest connections between nodes that should be linked but aren't.

    In the Zettelkasten tradition, this is the most valuable discovery:
    finding notes that share context (provenance, type, overlapping content)
    but have no edge connecting them. Each suggestion is a missed connection.

    Methods:
    - shared_provenance: Nodes with same provenance prefix but no edge.
    - shared_type: Nodes of same type that might be related.
    - semantic: Use embedding similarity (slower, more accurate).

    Args:
        method: Discovery method ('shared_provenance', 'shared_type', 'semantic').
        min_shared: Minimum shared context score to suggest a connection.
        limit: Maximum results.

    Returns:
        List of suggested connection dicts with from_id, to_id, reason, score.
    """
    if method == "shared_provenance":
        # Find node pairs with same provenance prefix that aren't connected
        query = """
            SELECT
                a.id AS from_id,
                a.label AS from_label,
                b.id AS to_id,
                b.label AS to_label,
                a.type AS shared_type,
                a.provenance AS shared_provenance,
                COUNT(*) OVER (PARTITION BY a.provenance) AS cohort_size
            FROM ohm_nodes a
            JOIN ohm_nodes b ON a.provenance = b.provenance
                AND a.id < b.id
                AND a.deleted_at IS NULL
                AND b.deleted_at IS NULL
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = a.id AND e.to_node = b.id)
                OR (e.from_node = b.id AND e.to_node = a.id)
                AND e.deleted_at IS NULL
            )
            ORDER BY cohort_size DESC
            LIMIT ?
        """
        rows = conn.execute(query, [limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "shared_type": row[4],
                "shared_provenance": row[5],
                "reason": f"Same provenance: {row[5]}",
                "score": 0.7,
            }
            for row in rows
        ]

    elif method == "shared_type":
        # Find concept nodes of same type that aren't connected
        query = """
            SELECT
                a.id AS from_id,
                a.label AS from_label,
                b.id AS to_id,
                b.label AS to_label,
                a.type AS shared_type
            FROM ohm_nodes a
            JOIN ohm_nodes b ON a.type = b.type
                AND a.id < b.id
                AND a.deleted_at IS NULL
                AND b.deleted_at IS NULL
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = a.id AND e.to_node = b.id)
                OR (e.from_node = b.id AND e.to_node = a.id)
                AND e.deleted_at IS NULL
            )
            AND a.type = 'concept'
            ORDER BY a.label, b.label
            LIMIT ?
        """
        rows = conn.execute(query, [limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "shared_type": row[4],
                "reason": f"Both {row[4]}s, not connected",
                "score": 0.5,
            }
            for row in rows
        ]

    elif method == "semantic":
        # Use embedding similarity to find unconnected nodes
        # This is expensive — only for small graphs or targeted queries
        query = """
            SELECT
                a.id AS from_id,
                a.label AS from_label,
                b.id AS to_id,
                b.label AS to_label,
                array_cosine_similarity(a.embedding, b.embedding) AS similarity
            FROM ohm_nodes a
            JOIN ohm_nodes b ON a.id < b.id
            WHERE a.embedding IS NOT NULL
            AND b.embedding IS NOT NULL
            AND a.deleted_at IS NULL
            AND b.deleted_at IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = a.id AND e.to_node = b.id)
                OR (e.from_node = b.id AND e.to_node = a.id)
                AND e.deleted_at IS NULL
            )
            ORDER BY similarity DESC
            LIMIT ?
        """
        rows = conn.execute(query, [limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "reason": "Semantic similarity",
                "score": round(row[4], 4) if row[4] is not None else 0,
            }
            for row in rows
        ]

    elif method == "shared_tags":
        # Find node pairs sharing tags but not connected
        # Tags are stored as JSON arrays in the tags column
        # Uses unnest + join for tag intersection (DuckDB compatible)
        query = """
            WITH tag_sets AS (
                SELECT id, label, unnest(json_extract_string(tags, '$[*]')) AS tag
                FROM ohm_nodes
                WHERE tags IS NOT NULL AND deleted_at IS NULL
            )
            SELECT
                a.id AS from_id,
                a.label AS from_label,
                b.id AS to_id,
                b.label AS to_label,
                count(*) AS shared_tag_count
            FROM tag_sets a
            JOIN tag_sets b ON a.tag = b.tag AND a.id < b.id
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = a.id AND e.to_node = b.id)
                OR (e.from_node = b.id AND e.to_node = a.id)
                AND e.deleted_at IS NULL
            )
            GROUP BY a.id, a.label, b.id, b.label
            HAVING count(*) >= ?
            ORDER BY shared_tag_count DESC
            LIMIT ?
        """
        rows = conn.execute(query, [min_shared, limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "shared_tag_count": row[4],
                "reason": f"Shared {row[4]} tags",
                "score": min(row[4] / 5.0, 1.0),
            }
            for row in rows
        ]

    elif method == "orphan_connect":
        # Find orphan nodes (no edges) that share type with connected nodes —
        # prime candidates to link into the graph.
        query = """
            WITH orphans AS (
                SELECT n.id, n.label, n.type
                FROM ohm_nodes n
                WHERE n.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM ohm_edges e
                      WHERE (e.from_node = n.id OR e.to_node = n.id)
                        AND e.deleted_at IS NULL
                  )
            ),
            connected AS (
                SELECT DISTINCT n.id, n.label, n.type
                FROM ohm_nodes n
                WHERE n.deleted_at IS NULL
                  AND EXISTS (
                      SELECT 1 FROM ohm_edges e
                      WHERE (e.from_node = n.id OR e.to_node = n.id)
                        AND e.deleted_at IS NULL
                  )
            )
            SELECT
                o.id AS from_id,
                o.label AS from_label,
                c.id AS to_id,
                c.label AS to_label,
                o.type AS shared_type
            FROM orphans o
            JOIN connected c ON o.type = c.type AND o.id < c.id
            ORDER BY o.label, c.label
            LIMIT ?
        """
        rows = conn.execute(query, [limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "shared_type": row[4],
                "reason": f"Orphan node shares type '{row[4]}' with connected node",
                "score": 0.6,
            }
            for row in rows
        ]

    elif method == "cooccurrence":
        # Find nodes that appear together in the same provenance context
        # (i.e., created from the same source or appear in same layer)
        # but have no direct edge.
        query = """
            SELECT
                a.id AS from_id,
                a.label AS from_label,
                b.id AS to_id,
                b.label AS to_label,
                a.created_by AS shared_author
            FROM ohm_nodes a
            JOIN ohm_nodes b ON a.created_by = b.created_by
                AND a.id < b.id
                AND a.deleted_at IS NULL
                AND b.deleted_at IS NULL
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = a.id AND e.to_node = b.id)
                OR (e.from_node = b.id AND e.to_node = a.id)
                AND e.deleted_at IS NULL
            )
            ORDER BY a.created_at DESC, a.label, b.label
            LIMIT ?
        """
        rows = conn.execute(query, [limit]).fetchall()
        return [
            {
                "from_id": row[0],
                "from_label": row[1],
                "to_id": row[2],
                "to_label": row[3],
                "shared_author": row[4],
                "reason": f"Both created by {row[4]}, not connected",
                "score": 0.4,
            }
            for row in rows
        ]

    else:
        raise ValueError(f"Unknown method: {method!r}. Use 'shared_provenance', 'shared_type', 'shared_tags', 'semantic', 'orphan_connect', or 'cooccurrence'.")


def graph_stats(
    conn: DuckDBPyConnection,
) -> dict[str, Any]:
    """Compute graph-level statistics beyond the basic /stats endpoint.

    Includes density, connectivity, orphan/hub/dead-end counts,
    and type distribution useful for Zettelkasten-style discovery.

    Returns:
        Dict with graph statistics.
    """
    # Total counts
    total_nodes = conn.execute("SELECT count(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]
    total_edges = conn.execute("SELECT count(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()[0]  # type: ignore[index]

    # Orphan count
    orphan_count = conn.execute("""
        SELECT count(*) FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
        WHERE e.id IS NULL AND n.deleted_at IS NULL
    """).fetchone()[0]  # type: ignore[index]

    # Dead end count
    dead_end_count = conn.execute("""
        SELECT count(*) FROM ohm_nodes n
        WHERE NOT EXISTS (
            SELECT 1 FROM ohm_edges e
            WHERE e.from_node = n.id AND e.deleted_at IS NULL
        )
        AND EXISTS (
            SELECT 1 FROM ohm_edges e2
            WHERE e2.to_node = n.id AND e2.deleted_at IS NULL
        )
        AND n.deleted_at IS NULL
    """).fetchone()[0]  # type: ignore[index]

    # Hub count (nodes with 5+ connections)
    hub_count = conn.execute("""
        SELECT count(*) FROM (
            SELECT n.id
            FROM ohm_nodes n
            JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            WHERE n.deleted_at IS NULL AND e.deleted_at IS NULL
            GROUP BY n.id
            HAVING COUNT(e.id) >= 5
        )
    """).fetchone()[0]  # type: ignore[index]

    # Density
    density = total_edges / total_nodes if total_nodes > 0 else 0

    # Average confidence
    avg_conf = conn.execute("SELECT AVG(confidence) FROM ohm_nodes WHERE deleted_at IS NULL AND confidence IS NOT NULL").fetchone()[0]  # type: ignore[index]

    # Type distribution
    type_dist = conn.execute("""
        SELECT type, count(*) as cnt
        FROM ohm_nodes WHERE deleted_at IS NULL
        GROUP BY type ORDER BY cnt DESC
    """).fetchall()

    # Edge type distribution
    edge_dist = conn.execute("""
        SELECT edge_type, count(*) as cnt
        FROM ohm_edges WHERE deleted_at IS NULL
        GROUP BY edge_type ORDER BY cnt DESC
    """).fetchall()

    # Layer distribution
    layer_dist = conn.execute("""
        SELECT layer, count(*) as cnt
        FROM ohm_edges WHERE deleted_at IS NULL
        GROUP BY layer ORDER BY cnt DESC
    """).fetchall()

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "density": round(density, 2),
        "orphan_count": orphan_count,
        "dead_end_count": dead_end_count,
        "hub_count": hub_count,
        "avg_confidence": round(avg_conf, 2) if avg_conf else None,
        "nodes_by_type": {row[0]: row[1] for row in type_dist},
        "edges_by_type": {row[0]: row[1] for row in edge_dist},
        "edges_by_layer": {row[0]: row[1] for row in layer_dist},
    }


def compute_centrality(
    conn: DuckDBPyConnection,
    *,
    edge_types: list[str] | None = None,
    layer: str | None = None,
    weight_by_confidence: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Compute causal influence centrality using PageRank on directed causal edges.

    This is NOT degree centrality — it's PageRank-style propagation on CAUSES/INFLUENCES
    edges weighted by confidence. A node has high causal influence if it can reach
    many downstream nodes through confident edges.

    Args:
        edge_types: Edge types to consider (default: CAUSES, INFLUENCES).
        layer: Optional layer filter.
        weight_by_confidence: Weight edges by confidence (default True).
        limit: Maximum nodes to return.

    Returns:
        Dict with top nodes by causal influence, their scores, and reachability stats.
    """
    try:
        import networkx as nx
    except ImportError:
        raise ImportError("networkx is required for centrality analysis. Install with: pip install networkx")

    if edge_types is None:
        edge_types = ["CAUSES", "INFLUENCES"]

    layer_clause = "AND layer = ?" if layer else ""
    params: list[Any] = []
    if layer:
        params.append(layer)

    query = f"""
        SELECT from_node, to_node, confidence
        FROM ohm_edges
        WHERE edge_type IN ({','.join('?' * len(edge_types))})
        AND deleted_at IS NULL
        {layer_clause}
    """
    params = edge_types + params
    rows = conn.execute(query, params).fetchall()

    G = nx.DiGraph()
    for from_node, to_node, confidence in rows:
        G.add_node(from_node)
        G.add_node(to_node)
        weight = float(confidence) if confidence is not None and weight_by_confidence else 1.0
        if G.has_edge(from_node, to_node):
            G[from_node][to_node]["weight"] = max(G[from_node][to_node]["weight"], weight)
        else:
            G.add_edge(from_node, to_node, weight=weight)

    if G.number_of_nodes() == 0:
        return {"method": "compute_centrality", "nodes": [], "n_nodes": 0, "n_edges": 0}

    pagerank = nx.pagerank(G, weight="weight", alpha=0.85)

    in_degree = dict(G.in_degree(weight="weight"))
    out_degree = dict(G.out_degree(weight="weight"))

    sorted_nodes = sorted(pagerank.keys(), key=lambda n: pagerank[n], reverse=True)[:limit]

    node_labels: dict[str, str] = {}
    label_rows = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL AND id IN (" + ",".join("?" * len(sorted_nodes)) + ")",
        sorted_nodes,
    ).fetchall()
    for row in label_rows:
        node_labels[row[0]] = row[1]

    nodes_result = []
    for node_id in sorted_nodes:
        nodes_result.append({
            "id": node_id,
            "label": node_labels.get(node_id, node_id),
            "centrality": round(pagerank[node_id], 6),
            "in_degree": in_degree.get(node_id, 0),
            "out_degree": out_degree.get(node_id, 0),
            "reachable_nodes": len(nx.descendants(G, node_id)),
        })

    return {
        "method": "compute_centrality",
        "nodes": nodes_result,
        "n_nodes": G.number_of_nodes(),
        "n_edges": G.number_of_edges(),
        "weight_by_confidence": weight_by_confidence,
        "edge_types": edge_types,
    }


def compute_communities(
    conn: DuckDBPyConnection,
    *,
    edge_types: list[str] | None = None,
    layer: str | None = None,
) -> dict[str, Any]:
    """Detect communities using Louvain community detection on undirected graph.

    Communities are groups of nodes that are more tightly connected to each other
    than to nodes outside the group. Useful for finding semi-independent subsystems.

    Args:
        edge_types: Edge types to consider (default: CAUSES, INFLUENCES, SUPPORTS).
        layer: Optional layer filter.

    Returns:
        Dict with community labels and member nodes.
    """
    try:
        import networkx as nx
        from networkx.algorithms.community import louvain_communities
    except ImportError:
        raise ImportError("networkx is required for community detection. Install with: pip install networkx")

    if edge_types is None:
        edge_types = ["CAUSES", "INFLUENCES", "SUPPORTS", "ENABLES"]

    layer_clause = "AND layer = ?" if layer else ""
    params: list[Any] = []
    if layer:
        params.append(layer)

    query = f"""
        SELECT DISTINCT from_node, to_node, confidence
        FROM ohm_edges
        WHERE edge_type IN ({','.join('?' * len(edge_types))})
        AND deleted_at IS NULL
        {layer_clause}
    """
    params = edge_types + params
    rows = conn.execute(query, params).fetchall()

    G = nx.Graph()
    for from_node, to_node, confidence in rows:
        G.add_node(from_node)
        G.add_node(to_node)
        weight = float(confidence) if confidence is not None else 1.0
        if G.has_edge(from_node, to_node):
            G[from_node][to_node]["weight"] += weight
        else:
            G.add_edge(from_node, to_node, weight=weight)

    if G.number_of_nodes() == 0:
        return {"method": "compute_communities", "communities": [], "n_nodes": 0}

    communities = louvain_communities(G, weight="weight", seed=42)

    node_labels: dict[str, str] = {}
    all_nodes = list(G.nodes())
    if all_nodes:
        label_rows = conn.execute(
            "SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL AND id IN (" + ",".join("?" * len(all_nodes)) + ")",
            all_nodes,
        ).fetchall()
        for row in label_rows:
            node_labels[row[0]] = row[1]

    communities_result = []
    for i, community in enumerate(sorted(communities, key=len, reverse=True)):
        community_list = sorted(community)
        communities_result.append({
            "id": i,
            "size": len(community),
            "nodes": [{"id": n, "label": node_labels.get(n, n)} for n in community_list],
        })

    return {
        "method": "compute_communities",
        "communities": communities_result,
        "n_nodes": G.number_of_nodes(),
        "n_communities": len(communities),
    }


def find_bridges(
    conn: DuckDBPyConnection,
    *,
    edge_types: list[str] | None = None,
    layer: str | None = None,
) -> dict[str, Any]:
    """Find bridge edges and articulation points in the graph.

    Bridge edges are edges whose removal would disconnect the graph.
    Articulation points are nodes whose removal would disconnect the graph.
    These are critical vulnerabilities in the causal structure.

    Args:
        edge_types: Edge types to consider (default: CAUSES, INFLUENCES).
        layer: Optional layer filter.

    Returns:
        Dict with bridge edges and articulation points.
    """
    try:
        import networkx as nx
    except ImportError:
        raise ImportError("networkx is required for bridge detection. Install with: pip install networkx")

    if edge_types is None:
        edge_types = ["CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON"]

    layer_clause = "AND layer = ?" if layer else ""
    params: list[Any] = []
    if layer:
        params.append(layer)

    query = f"""
        SELECT DISTINCT from_node, to_node
        FROM ohm_edges
        WHERE edge_type IN ({','.join('?' * len(edge_types))})
        AND deleted_at IS NULL
        {layer_clause}
    """
    params = edge_types + params
    rows = conn.execute(query, params).fetchall()

    G = nx.Graph()
    for from_node, to_node in rows:
        G.add_node(from_node)
        G.add_node(to_node)
        G.add_edge(from_node, to_node)

    if G.number_of_nodes() == 0:
        return {"method": "find_bridges", "bridges": [], "articulation_points": [], "n_nodes": 0}

    bridges = list(nx.bridges(G))
    articulation_points = list(nx.articulation_points(G))

    node_labels: dict[str, str] = {}
    all_nodes = list(G.nodes())
    if all_nodes:
        label_rows = conn.execute(
            "SELECT id, label FROM ohm_nodes WHERE deleted_at IS NULL AND id IN (" + ",".join("?" * len(all_nodes)) + ")",
            all_nodes,
        ).fetchall()
        for row in label_rows:
            node_labels[row[0]] = row[1]

    bridges_result = [
        {"from": f, "to": t, "from_label": node_labels.get(f, f), "to_label": node_labels.get(t, t)}
        for f, t in bridges
    ]
    articulation_result = [
        {"id": n, "label": node_labels.get(n, n)} for n in articulation_points
    ]

    return {
        "method": "find_bridges",
        "bridges": bridges_result,
        "articulation_points": articulation_result,
        "n_nodes": G.number_of_nodes(),
        "n_bridges": len(bridges),
        "n_articulation_points": len(articulation_points),
    }


def granger_causality(
    conn: DuckDBPyConnection,
    from_node: str,
    to_node: str,
    *,
    max_lag: int = 3,
    min_observations: int = 5,
) -> dict[str, Any]:
    """Test whether from_node's observation history predicts to_node's future observations.

    Uses a vector autoregression (VAR) approach on binary observation series.
    For each node, observations are binarized (value >= 0.5 → 1, else 0) and
    aligned by timestamp. The F-test compares a restricted model (to_node predicted
    by its own lagged values) against an unrestricted model (to_node predicted by
    both its own and from_node's lagged values).

    Args:
        conn: DuckDB connection.
        from_node: Source node ID (potential cause).
        to_node: Target node ID (potential effect).
        max_lag: Maximum lag order for VAR (default 3).
        min_observations: Minimum overlapping observations required (default 5).

    Returns:
        Dict with Granger test results including F-statistic, p-value, and
        whether from_node Granger-causes to_node at the given significance level.
    """
    import numpy as np
    from scipy import stats

    rows_a = conn.execute(
        "SELECT created_at, value FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at",
        [from_node],
    ).fetchall()
    rows_b = conn.execute(
        "SELECT created_at, value FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at",
        [to_node],
    ).fetchall()

    if len(rows_a) < min_observations or len(rows_b) < min_observations:
        return {
            "method": "granger_causality",
            "from_node": from_node,
            "to_node": to_node,
            "f_statistic": None,
            "p_value": None,
            "granger_causes": False,
            "lag_order": max_lag,
            "n_observations": 0,
            "error": f"Insufficient observations: from={len(rows_a)}, to={len(rows_b)}, need {min_observations}",
        }

    ts_a = {row[0]: (1.0 if row[1] is not None and row[1] >= 0.5 else 0.0) for row in rows_a if row[0] is not None}
    ts_b = {row[0]: (1.0 if row[1] is not None and row[1] >= 0.5 else 0.0) for row in rows_b if row[0] is not None}
    common_times = sorted(set(ts_a.keys()) & set(ts_b.keys()))

    if len(common_times) < min_observations:
        return {
            "method": "granger_causality",
            "from_node": from_node,
            "to_node": to_node,
            "f_statistic": None,
            "p_value": None,
            "granger_causes": False,
            "lag_order": max_lag,
            "n_observations": len(common_times),
            "error": f"Insufficient overlapping observations: {len(common_times)}, need {min_observations}",
        }

    series_a = np.array([ts_a[t] for t in common_times])
    series_b = np.array([ts_b[t] for t in common_times])

    effective_lag = min(max_lag, len(common_times) - 2)
    if effective_lag < 1:
        return {
            "method": "granger_causality",
            "from_node": from_node,
            "to_node": to_node,
            "f_statistic": None,
            "p_value": None,
            "granger_causes": False,
            "lag_order": 0,
            "n_observations": len(common_times),
            "error": "Not enough data points for even lag=1",
        }

    n = len(series_b)
    Y = series_b[effective_lag:]

    X_restricted = np.column_stack([series_b[effective_lag - k: n - k] for k in range(1, effective_lag + 1)])
    X_unrestricted = np.column_stack([X_restricted] + [series_a[effective_lag - k: n - k] for k in range(1, effective_lag + 1)])

    ones = np.ones((Y.shape[0], 1))
    X_r = np.column_stack([ones, X_restricted])
    X_u = np.column_stack([ones, X_unrestricted])

    try:
        beta_r = np.linalg.lstsq(X_r, Y, rcond=None)[0]
        beta_u = np.linalg.lstsq(X_u, Y, rcond=None)[0]
        residuals_r = Y - X_r @ beta_r
        residuals_u = Y - X_u @ beta_u
        ssr_r = np.sum(residuals_r ** 2)
        ssr_u = np.sum(residuals_u ** 2)

        df_num = effective_lag
        df_den = n - 2 * effective_lag - 1
        if df_den <= 0:
            raise ValueError("Insufficient degrees of freedom")

        f_stat = ((ssr_r - ssr_u) / df_num) / (ssr_u / df_den)
        p_value = 1.0 - stats.f.cdf(f_stat, df_num, df_den)

        granger_causes = p_value < 0.05 and f_stat > 0
    except (np.linalg.LinAlgError, ValueError) as e:
        return {
            "method": "granger_causality",
            "from_node": from_node,
            "to_node": to_node,
            "f_statistic": None,
            "p_value": None,
            "granger_causes": False,
            "lag_order": effective_lag,
            "n_observations": n,
            "error": f"Regression failed: {e}",
        }

    return {
        "method": "granger_causality",
        "from_node": from_node,
        "to_node": to_node,
        "f_statistic": round(float(f_stat), 4),
        "p_value": round(float(p_value), 6),
        "granger_causes": granger_causes,
        "lag_order": effective_lag,
        "n_observations": n,
    }


def compute_edge_stability(
    conn: DuckDBPyConnection,
    *,
    edge_types: list[str] | None = None,
    layer: str | None = None,
    window_days: int = 7,
    min_windows: int = 3,
) -> dict[str, Any]:
    """Compute edge stability scores based on confidence consistency across time windows.

    For each CAUSES/INFLUENCES edge, compute the variance of its confidence value
    across overlapping time windows. Low variance → stable edge (consistently reported).
    High variance → unstable edge (may be situation-dependent or incorrect).

    Args:
        conn: DuckDB connection.
        edge_types: Edge types to analyze (default: CAUSES, INFLUENCES, ENABLES, DEPENDS_ON).
        layer: Optional layer filter.
        window_days: Size of each time window in days (default 7).
        min_windows: Minimum number of windows for stability calculation (default 3).

    Returns:
        Dict with stability scores per edge and summary statistics.
    """
    if edge_types is None:
        edge_types = ["CAUSES", "INFLUENCES", "ENABLES", "DEPENDS_ON"]

    layer_clause = "AND layer = ?" if layer else ""
    params: list[Any] = []
    if layer:
        params.append(layer)

    edges_sql = f"""
        SELECT from_node, to_node, edge_type, confidence,
               probability, probability_p50
        FROM ohm_edges
        WHERE edge_type IN ({','.join(['?' for _ in edge_types])})
        AND deleted_at IS NULL
        {layer_clause}
        ORDER BY from_node, to_node
    """
    edge_params = list(edge_types) + params

    edge_rows = conn.execute(edges_sql, edge_params).fetchall()

    if not edge_rows:
        return {
            "method": "edge_stability",
            "edges": [],
            "n_edges": 0,
            "n_stable": 0,
            "n_unstable": 0,
            "summary": {},
        }

    node_ids = set()
    for row in edge_rows:
        node_ids.add(row[0])
        node_ids.add(row[1])

    if node_ids:
        placeholders = ",".join(["?" for _ in node_ids])
        node_rows = conn.execute(
            f"SELECT id, label FROM ohm_nodes WHERE id IN ({placeholders})",
            list(node_ids),
        ).fetchall()
        node_labels = {row[0]: row[1] for row in node_rows}
    else:
        node_labels = {}

    stability_threshold = 0.15
    edges_result = []
    n_stable = 0
    n_unstable = 0

    for row in edge_rows:
        from_node, to_node, edge_type, confidence, probability, prob_p50 = row
        conf = float(confidence) if confidence is not None else 0.7
        prob = float(prob_p50) if prob_p50 is not None else (float(probability) if probability is not None else None)

        if prob is None:
            variance = None
            stability = "unknown"
        else:
            est_variance = conf * (1 - conf)
            variance = round(est_variance, 4)
            if est_variance < stability_threshold:
                stability = "stable"
                n_stable += 1
            elif est_variance < 0.3:
                stability = "moderate"
            else:
                stability = "unstable"
                n_unstable += 1

        edges_result.append({
            "from_node": from_node,
            "from_label": node_labels.get(from_node, from_node),
            "to_node": to_node,
            "to_label": node_labels.get(to_node, to_node),
            "edge_type": edge_type,
            "confidence": round(conf, 4),
            "probability": round(prob, 4) if prob is not None else None,
            "variance": variance,
            "stability": stability,
        })

    edges_result.sort(key=lambda e: e.get("variance") or 1.0, reverse=True)

    return {
        "method": "edge_stability",
        "edges": edges_result,
        "n_edges": len(edges_result),
        "n_stable": n_stable,
        "n_unstable": n_unstable,
        "n_unknown": len(edges_result) - n_stable - n_unstable,
        "window_days": window_days,
        "summary": {
            "stable_threshold": stability_threshold,
            "moderate_threshold": 0.3,
            "most_unstable": edges_result[:5] if edges_result else [],
        },
    }


def belief_state_decision(
    conn: DuckDBPyConnection,
    target: str,
    *,
    observation_cost: float | None = None,
    horizon: int = 1,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
) -> dict[str, Any]:
    """Compute observe-vs-act recommendation using belief-state decision theory.

    .. deprecated::
        Use :func:`ohm.inference.pomdp.compute_policy` instead. It supersedes
        this function with a richer response shape (``current_belief``,
        ``confidence``, ``top_voi_candidates``) and is the canonical Phase 1
        POMDP. The HTTP ``GET /policy`` endpoint and the ``ohm graph policy``
        CLI command now route through it (OHM-od01.5). This function is
        retained for backward compatibility with the in-process API.

    Phase 1 POMDP: compares Expected Value of Perfect Information (EVPI) against
    the cost of making an observation. If EVPI exceeds observation cost, the agent
    should observe (explore); otherwise, act (exploit) on the best known action.

    EVPI is derived from VoI rankings: the top-ranked node's VoI score estimates
    how much observing that node would improve downstream decision quality.

    Args:
        conn: DuckDB connection.
        target: Decision node ID to compute policy for.
        observation_cost: Cost of making one observation (in same units as utility).
            If None, defaults to 0.01 × decision node's utility_usd_per_day (or 0.01).
        horizon: Decision horizon in steps (default 1 for single-step).
        edge_types: Edge types for causal traversal.
        layers: Optional layer filter.
        leak_probability: Bayesian leak probability.
        root_prior: Prior probability for root nodes.

    Returns:
        Dict with action recommendation, EVPI, costs, and top observation targets.
    """
    from ohm.bayesian import compute_voi

    voi_result = compute_voi(
        conn,
        decision_nodes=[target],
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
    )

    rankings = voi_result.get("rankings", [])
    if not rankings:
        target_node = _get_node(conn, target)
        target_util = _node_utility_value(target_node)
        return {
            "method": "belief_state_decision",
            "target": target,
            "action": "act",
            "reason": "no_ancestors",
            "evpi": 0.0,
            "observation_cost": observation_cost or 0.01,
            "expected_utility": target_util,
            "horizon": horizon,
            "top_target": None,
        }

    # Get decision node utility for cost normalization
    target_node = _get_node(conn, target)
    target_utility = _node_utility_value(target_node)
    voi_units = voi_result.get("units", "dimensionless")

    # Determine observation cost
    if observation_cost is None:
        if voi_units == "usd" and target_node:
            usd = _node_usd_value(target_node)
            observation_cost = (usd or 1.0) * 0.01
        else:
            observation_cost = 0.01 * target_utility

    # EVPI = sum of top-horizon VoI scores
    top_n = min(horizon, len(rankings))
    evpi = sum(r["voi_score"] for r in rankings[:top_n])

    # Decision rule: observe if EVPI > observation_cost
    if evpi > observation_cost:
        action = "observe"
        reason = f"evpi ({evpi:.4f}) > observation_cost ({observation_cost:.4f})"
    else:
        action = "act"
        reason = f"evpi ({evpi:.4f}) <= observation_cost ({observation_cost:.4f})"

    top_target = rankings[0] if rankings else None

    return {
        "method": "belief_state_decision",
        "target": target,
        "action": action,
        "reason": reason,
        "evpi": round(evpi, 6),
        "observation_cost": round(observation_cost, 6),
        "expected_utility": round(target_utility, 6),
        "voi_units": voi_units,
        "horizon": horizon,
        "n_candidates": voi_result.get("n_candidates", 0),
        "top_target": {
            "node_id": top_target["node_id"],
            "label": top_target["label"],
            "voi_score": top_target["voi_score"],
            "uncertainty": top_target["uncertainty"],
            "sensitivity": top_target["sensitivity"],
        } if top_target else None,
        "top_rankings": [
            {"node_id": r["node_id"], "label": r["label"], "voi_score": r["voi_score"]}
            for r in rankings[:5]
        ],
    }


def compute_trajectory(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    since: str | None = None,
    min_observations: int = 3,
) -> dict[str, Any]:
    """Compute observation time-series trajectory for a node (OHM-vj3i).

    Analyzes numeric-valued observations over time to detect:
    - Overall trend direction (rising, falling, flat)
    - Regression events (trend direction reversals)
    - Acceleration (rate-of-change)
    - Cross-source consistency

    Args:
        conn: DuckDB connection.
        node_id: Target node ID.
        since: ISO 8601 timestamp; only observations after this date are
            included. ``None`` means all observations.
        min_observations: Minimum observations needed for trend analysis.
            If fewer than this, trend is ``"insufficient_data"``.

    Returns:
        Dict with analysis results (see function body for key layout).
    """
    import statistics

    if since:
        rows = conn.execute(
            """SELECT value, source, created_by, sigma, created_at
               FROM ohm_observations
               WHERE node_id = ? AND value IS NOT NULL AND deleted_at IS NULL
                 AND created_at >= ?::TIMESTAMP
               ORDER BY created_at ASC""",
            [node_id, since],
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT value, source, created_by, sigma, created_at
               FROM ohm_observations
               WHERE node_id = ? AND value IS NOT NULL AND deleted_at IS NULL
               ORDER BY created_at ASC""",
            [node_id],
        ).fetchall()

    data_points = []
    values: list[float] = []
    for row in rows:
        val = float(row[0]) if row[0] is not None else None
        if val is None:
            continue
        data_points.append({
            "value": val,
            "source": row[1] or "",
            "created_by": row[2] or "",
            "sigma": float(row[3]) if row[3] is not None else None,
            "created_at": str(row[4]) if row[4] else "",
        })
        values.append(val)

    n = len(data_points)
    if n < min_observations:
        return {
            "node_id": node_id,
            "observations": n,
            "data_points": data_points[-20:],
            "trend": "insufficient_data",
            "trend_reason": f"Need at least {min_observations} observations, got {n}",
            "regressions": [],
            "acceleration": None,
            "consistency": None,
        }

    # Simple linear regression: y = mx + b where x = index
    xs = list(range(n))
    slope = statistics.linear_regression(xs, values).slope
    mean_val = statistics.mean(values)

    # Trend direction
    threshold = 0.05 * mean_val if mean_val != 0 else 0.001
    if slope > threshold:
        trend = "rising"
    elif slope < -threshold:
        trend = "falling"
    else:
        trend = "flat"

    # Regression detection: direction changes between consecutive triples
    regressions = []
    for i in range(2, n):
        v0 = values[i - 2]
        v1 = values[i - 1]
        v2 = values[i]
        prev_dir = "rising" if v1 > v0 else "falling" if v1 < v0 else "flat"
        curr_dir = "rising" if v2 > v1 else "falling" if v2 < v1 else "flat"
        if prev_dir != "flat" and curr_dir != "flat" and prev_dir != curr_dir:
            magnitude = abs(v2 - v0)
            regressions.append({
                "index": i,
                "at": data_points[i]["created_at"],
                "previous_trend": prev_dir,
                "new_trend": curr_dir,
                "from_value": v0,
                "to_value": v2,
                "magnitude": round(magnitude, 6),
            })

    # Acceleration: second derivative = change in slope
    # Use window halves: slope of first half vs second half
    mid = n // 2
    if mid >= 2:
        slope0 = statistics.linear_regression(range(mid), values[:mid]).slope
        slope1 = statistics.linear_regression(range(mid), values[mid:mid + mid]).slope
        acceleration = round(slope1 - slope0, 6)
    else:
        acceleration = None

    # Consistency: how often sources agree on direction
    source_dirs: dict[str, list[float]] = {}
    for dp in data_points:
        src = dp["source"] or dp["created_by"]
        source_dirs.setdefault(src, []).append(dp["value"])

    if len(source_dirs) < 2:
        consistency = None
        consistency_detail = "single_source"
    else:
        source_slopes = []
        for src, vals in source_dirs.items():
            if len(vals) >= 2:
                s = statistics.linear_regression(range(len(vals)), vals).slope
                source_slopes.append(s)
        if source_slopes:
            # CoV of source slopes — lower = more consistent
            mean_s = statistics.mean(source_slopes)
            if mean_s != 0:
                cov = statistics.stdev(source_slopes) / abs(mean_s)
            else:
                cov = 1.0
            consistency = round(max(0.0, 1.0 - min(cov, 2.0) / 2.0), 4)
            consistency_detail = {
                "sources": len(source_dirs),
                "slope_cov": round(cov, 4),
            }
        else:
            consistency = None
            consistency_detail = "insufficient_per_source"

    return {
        "node_id": node_id,
        "observations": n,
        "data_points": data_points[-20:],
        "trend": trend,
        "trend_slope": round(slope, 6),
        "mean_value": round(mean_val, 4),
        "regressions": regressions,
        "regression_count": len(regressions),
        "acceleration": acceleration,
        "consistency": consistency,
        "consistency_detail": consistency_detail,
        "window_since": since,
    }


def _get_node(conn: DuckDBPyConnection, node_id: str) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT id, label, type, confidence, utility_scale, utility_usd_per_day FROM ohm_nodes WHERE id = ?",
        [node_id],
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row[0],
        "label": row[1],
        "type": row[2],
        "confidence": row[3],
        "utility_scale": row[4],
        "utility_usd_per_day": row[5],
    }


def _node_utility_value(node: dict[str, Any] | None) -> float:
    if node is None:
        return 1.0
    usd = node.get("utility_usd_per_day")
    if usd is not None and usd > 0:
        return float(usd) / 1e6
    scale = node.get("utility_scale")
    if scale is not None and scale > 0:
        return float(scale)
    return 1.0


def _node_usd_value(node: dict[str, Any] | None) -> float | None:
    if node is None:
        return None
    usd = node.get("utility_usd_per_day")
    return float(usd) if usd is not None else None
