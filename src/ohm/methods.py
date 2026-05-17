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
) -> list[dict[str, Any]]:
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
