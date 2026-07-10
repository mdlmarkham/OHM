"""OHM-8fdb: Self-Calibration — learned half-lives and authority decay.

When observations are superseded, the age at supersession is a training signal
for that observation type's half-life. When sources go unverified, their
reliability decays toward a community prior.

Feature 5: Learned Half-Lives
  - empirical_half_life(): compute from supersession history per obs_type
  - /admin/learned-half-lives: API endpoint returning comparison table
  - confidence_at() uses learned half-life when n_samples >= 5

Feature 6: Authority Decay
  - effective_reliability(): decay source p_accurate toward community prior
  - /source-reliability/{agent_id}: includes effective_reliability
  - last_verified_at and decay_lambda columns on source_reliability tracking
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.decay import default_half_life, default_weibull_shape


# ── Feature 5: Learned Half-Lives ──────────────────────────────────────────


MIN_SAMPLES = 5  # Minimum supersession events before using learned half-life


def empirical_half_life(
    conn: "DuckDBPyConnection",
    obs_type: str,
) -> dict[str, Any]:
    """Compute empirical half-life from supersession history for an obs_type.

    Walks all observations of the given type that have been superseded
    (valid_to IS NOT NULL) and computes:
    - median age at supersession
    - learned half-life (median / log(2))
    - sample count

    If fewer than MIN_SAMPLES superseded observations exist, returns the
    default half-life with a note explaining why.

    Returns:
        Dict with learned_half_life, n_samples, median_age_at_supersession,
        default_half_life, using_default (bool), and note.
    """
    result = conn.execute(
        """
        SELECT
            o.id,
            o.valid_from,
            o.valid_to,
            o.half_life_days,
            o.type
        FROM ohm_observations o
        WHERE o.type = ?
          AND o.deleted_at IS NULL
          AND o.valid_to IS NOT NULL
          AND o.valid_from IS NOT NULL
        """,
        [obs_type],
    ).fetchall()

    n_samples = len(result)

    if n_samples < MIN_SAMPLES:
        fallback = default_half_life(obs_type)
        return {
            "obs_type": obs_type,
            "learned_half_life": fallback,
            "n_samples": n_samples,
            "median_age_at_supersession": None,
            "default_half_life": fallback,
            "weibull_shape": default_weibull_shape(obs_type),
            "using_default": True,
            "note": f"Only {n_samples} superseded observations; need {MIN_SAMPLES} for learned half-life. Using default.",
        }

    # Compute ages at supersession
    ages = []
    for row in result:
        valid_from = row[1]
        valid_to = row[2]
        if isinstance(valid_from, str):
            valid_from = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        if isinstance(valid_to, str):
            valid_to = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))

        # Ensure timezone-aware
        if valid_from.tzinfo is None:
            valid_from = valid_from.replace(tzinfo=timezone.utc)
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=timezone.utc)

        age_days = (valid_to - valid_from).total_seconds() / 86400.0
        if age_days > 0:
            ages.append(age_days)

    if not ages:
        fallback = default_half_life(obs_type)
        return {
            "obs_type": obs_type,
            "learned_half_life": fallback,
            "n_samples": 0,
            "median_age_at_supersession": None,
            "default_half_life": fallback,
            "weibull_shape": default_weibull_shape(obs_type),
            "using_default": True,
            "note": "No positive ages at supersession. Using default.",
        }

    ages.sort()
    median_age = ages[len(ages) // 2]
    learned_hl = median_age / math.log(2) if median_age > 0 else default_half_life(obs_type)

    # For permanent types (outcome), learned half_life should be None
    fallback = default_half_life(obs_type)
    if fallback is None:
        learned_hl = None

    return {
        "obs_type": obs_type,
        "learned_half_life": round(learned_hl, 2) if learned_hl is not None else None,
        "n_samples": n_samples,
        "median_age_at_supersession": round(median_age, 2),
        "default_half_life": fallback,
        "weibull_shape": default_weibull_shape(obs_type),
        "using_default": False,
        "note": None,
    }


def all_learned_half_lives(conn: "DuckDBPyConnection") -> dict[str, dict[str, Any]]:
    """Compute learned half-lives for all obs_types with supersession data.

    Returns:
        Dict mapping obs_type to the result of empirical_half_life() for that type.
    """
    # Get all distinct obs_types that have at least one superseded observation
    conn.execute(
        """
        SELECT DISTINCT type
        FROM ohm_observations
        WHERE valid_to IS NOT NULL
          AND deleted_at IS NULL
          AND type IS NOT NULL
        """
    ).fetchall()

    # Also get all types that exist in the observations table (for default comparison)
    all_types = conn.execute(
        """
        SELECT DISTINCT type
        FROM ohm_observations
        WHERE deleted_at IS NULL
          AND type IS NOT NULL
        """
    ).fetchall()

    result = {}
    seen = set()

    for (obs_type,) in all_types:
        if obs_type in seen:
            continue
        seen.add(obs_type)
        result[obs_type] = empirical_half_life(conn, obs_type)

    return result


def effective_half_life(
    conn: "DuckDBPyConnection",
    obs_type: str,
) -> float | None:
    """Return the effective half-life for an obs_type.

    Uses the learned half-life if n_samples >= MIN_SAMPLES, otherwise
    falls back to the default.

    This is the function that confidence_at() should call to get the
    half-life for decay computation.

    Returns:
        Half-life in days (float), or None for permanent types.
    """
    result = empirical_half_life(conn, obs_type)
    if result["using_default"]:
        return result["default_half_life"]
    return result["learned_half_life"]


# ── Feature 6: Authority Decay ─────────────────────────────────────────────

# Default decay rate: reliability half-life of 70 days
DEFAULT_AUTHORITY_DECAY_LAMBDA = 0.01


def community_prior(conn: "DuckDBPyConnection") -> float:
    """Compute the community prior for source reliability.

    The community prior is the median p_accurate across all agents with
    recorded outcomes. This provides a reasonable baseline for untested
    sources and the decay target for stale sources.

    Returns:
        Median p_accurate across all agents, defaulting to 0.5 if no data.
    """
    result = conn.execute(
        """
        SELECT COALESCE(claimed_by, source_agent) AS source_agent,
               CASE WHEN COUNT(*) > 0
                    THEN CAST(SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS DOUBLE) / COUNT(*)
                    ELSE NULL END AS p_accurate
        FROM ohm_outcomes
        GROUP BY COALESCE(claimed_by, source_agent)
        HAVING COUNT(*) >= 2
        """
    ).fetchall()

    if not result:
        return 0.5

    accuracies = sorted([row[1] for row in result if row[1] is not None])
    if not accuracies:
        return 0.5

    mid = len(accuracies) // 2
    if len(accuracies) % 2 == 0:
        return (accuracies[mid - 1] + accuracies[mid]) / 2
    return accuracies[mid]


def effective_reliability(
    conn: "DuckDBPyConnection",
    agent_id: str,
    t: datetime | None = None,
    decay_lambda: float = DEFAULT_AUTHORITY_DECAY_LAMBDA,
    *,
    domain: str | None = None,
) -> dict[str, Any]:
    """Compute effective reliability for a source agent with temporal decay.

    Source reliability decays toward the community prior without recent
    verification. An agent verified at 97% accuracy in May shouldn't be
    assumed 97% in June without fresh verification.

    Formula:
        effective = prior + (observed - prior) * exp(-lambda * days_stale)

    Where:
        prior = community_prior() (median reliability across all agents)
        observed = p_accurate for the agent
        days_stale = days since last outcome
        lambda = decay rate (default 0.01, ~70 day half-life)

    Args:
        conn: DuckDB connection
        agent_id: Source agent identifier (e.g., "agent-metis")
        t: Point in time to evaluate at. Defaults to now.
        decay_lambda: Decay rate (higher = faster decay toward prior)
        domain: Optional domain filter (OHM-avkj). When set, only
            outcomes with matching ``domain`` (or ``'*'`` for unscoped)
            are counted.

    Returns:
        Dict with agent_id, p_accurate, effective_reliability,
        days_since_verification, community_prior, and decay_lambda.
    """
    if t is None:
        t = datetime.now(timezone.utc)

    domain_clause = ""
    params: list = [agent_id]
    if domain is not None:
        domain_clause = " AND (domain = ? OR domain = '*')"
        params.append(domain)

    # Get agent's p_accurate and last outcome date
    agent_stats = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_outcomes,
            SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS accurate,
            MAX(recorded_at) AS last_outcome_at
        FROM ohm_outcomes
        WHERE COALESCE(claimed_by, source_agent) = ?{domain_clause}
        """,
        params,
    ).fetchone()

    total, accurate, last_outcome_at = agent_stats
    p_accurate = (accurate / total) if total and total > 0 else None

    # Compute community prior
    prior = community_prior(conn)

    # Compute days since last verification
    if last_outcome_at is not None:
        if isinstance(last_outcome_at, str):
            last_outcome_at = datetime.fromisoformat(last_outcome_at.replace("Z", "+00:00"))
        if last_outcome_at.tzinfo is None:
            last_outcome_at = last_outcome_at.replace(tzinfo=timezone.utc)
        days_stale = (t - last_outcome_at).total_seconds() / 86400.0
    else:
        days_stale = float("inf")

    # Compute effective reliability
    if p_accurate is not None:
        # Exponential decay toward community prior
        effective = prior + (p_accurate - prior) * math.exp(-decay_lambda * days_stale)
    else:
        # No outcomes recorded — use community prior
        effective = prior

    return {
        "agent_id": agent_id,
        "p_accurate": round(p_accurate, 4) if p_accurate is not None else None,
        "effective_reliability": round(effective, 4),
        "days_since_verification": round(days_stale, 1) if days_stale != float("inf") else None,
        "community_prior": round(prior, 4),
        "decay_lambda": decay_lambda,
        "total_outcomes": int(total) if total else 0,
        "last_outcome_at": last_outcome_at.isoformat() if last_outcome_at is not None else None,
    }


def all_effective_reliabilities(
    conn: "DuckDBPyConnection",
    t: datetime | None = None,
    decay_lambda: float = DEFAULT_AUTHORITY_DECAY_LAMBDA,
) -> list[dict[str, Any]]:
    """Compute effective reliability for all agents with recorded outcomes.

    Returns:
        List of effective_reliability() result dicts, sorted by
        effective_reliability descending.
    """
    if t is None:
        t = datetime.now(timezone.utc)

    # Get all distinct agents
    agents = conn.execute("SELECT DISTINCT COALESCE(claimed_by, source_agent) FROM ohm_outcomes ORDER BY 1").fetchall()

    results = []
    for (agent_id,) in agents:
        results.append(effective_reliability(conn, agent_id, t=t, decay_lambda=decay_lambda))

    # Sort by effective_reliability descending
    results.sort(key=lambda r: r["effective_reliability"], reverse=True)
    return results


# ── OHM-792: Extended Agent Profile ─────────────────────────────────────────


def compute_agent_profile(
    conn: "DuckDBPyConnection",
    agent_name: str,
) -> dict[str, Any]:
    """Compute the extended agent calibration profile (OHM-792).

    Extends compute_confidence_calibration with agora-aware fields:
    loop-risk, novelty, contrarian value, evidence quality, language-
    confidence bias, and blast-radius awareness.

    Returns a dict with all fields from compute_confidence_calibration
    PLUS the new agora profile fields.
    """
    from ohm.graph.methods import compute_confidence_calibration

    base = compute_confidence_calibration(conn, agent_name)

    total_edges = base.get("total_l3_l4_edges", 0)

    # ── point_estimate_bias ──
    # Average difference between agent's confidence and actual outcome rate
    point_bias = _compute_point_estimate_bias(conn, agent_name, total_edges)

    # ── overconfidence_rate ──
    # Fraction of high-confidence (>=0.8) edges that were challenged
    overconfidence = _compute_overconfidence_rate(conn, agent_name)

    # ── language_confidence_bias ──
    # Difference between verbal strength (from belief_statements) and
    # stated probability. Requires ohm_belief_calibration_log.
    lang_bias = _compute_language_confidence_bias(conn, agent_name)

    # ── brier_score ──
    # Mean squared error between stated confidence and actual outcome
    brier = _compute_brier_score(conn, agent_name)

    # ── novelty_score ──
    # Expected information gain: fraction of agent's observations that
    # were surprises (high sigma relative to baseline)
    novelty = _compute_novelty_score(conn, agent_name)

    # ── contrarian_value ──
    # Outcome-weighted score for agents who disagree with the graph and
    # turn out correct (their challenged edges held up)
    contrarian = _compute_contrarian_value(conn, agent_name)

    # ── evidence_quality ──
    # Source reliability × source_url presence × sigma plausibility
    evidence_quality = _compute_evidence_quality(conn, agent_name)

    # ── evidence_freshness ──
    # Average age of agent's observations relative to now (0 = stale, 1 = fresh)
    freshness = _compute_evidence_freshness(conn, agent_name)

    # ── mechanism_quality ──
    # Fraction of agent's CAUSES edges that have a mechanism specified
    mechanism = _compute_mechanism_quality(conn, agent_name)

    # ── information_contribution ──
    # Fraction of agent's nodes that are unique (not created by other agents)
    info_contrib = _compute_information_contribution(conn, agent_name)

    # ── loop_risk ──
    # Per-target risk: recency of belief requests vs. independent evidence
    loop_risk = _compute_loop_risk(conn, agent_name)

    # ── blast_radius_awareness ──
    # How well the agent calibrates confidence to actual stakes
    blast_awareness = _compute_blast_radius_awareness(conn, agent_name, total_edges)

    # ── Intervention ladder ──
    max_loop_risk = max(loop_risk.values()) if loop_risk else 0.0
    intervention = _intervention_ladder(max_loop_risk)

    return {
        **base,
        "point_estimate_bias": point_bias,
        "overconfidence_rate": overconfidence,
        "language_confidence_bias": lang_bias,
        "brier_score": brier,
        "novelty_score": novelty,
        "contrarian_value": contrarian,
        "evidence_quality": evidence_quality,
        "evidence_freshness": freshness,
        "mechanism_quality": mechanism,
        "information_contribution": info_contrib,
        "loop_risk": loop_risk,
        "max_loop_risk": round(max_loop_risk, 4),
        "blast_radius_awareness": blast_awareness,
        "intervention": intervention,
    }


def _compute_point_estimate_bias(conn, agent_name, total_edges):
    try:
        row = conn.execute(
            """
            SELECT AVG(ABS(e.confidence - COALESCE(o.outcome, 0.5)))
            FROM ohm_edges e
            LEFT JOIN ohm_outcomes o ON o.claim_node = e."from"
            WHERE e.created_by = ? AND e.layer IN ('L3','L4')
            """,
            [agent_name],
        ).fetchone()
        return round(row[0] or 0.0, 4) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0


def _compute_overconfidence_rate(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END) AS challenged
            FROM ohm_edges e
            LEFT JOIN ohm_edges c ON c.challenge_of = e.id AND c.challenge_type = 'CHALLENGED_BY'
            WHERE e.created_by = ? AND e.confidence >= 0.8 AND e.layer IN ('L3','L4')
            """,
            [agent_name],
        ).fetchone()
        total = row[0] or 0
        challenged = row[1] or 0
        return round(challenged / max(total, 1), 4) if total > 0 else 0.0
    except Exception:
        return 0.0


def _compute_language_confidence_bias(conn, agent_name):
    try:
        rows = conn.execute(
            """
            SELECT claimed_probability, graph_probability
            FROM ohm_belief_calibration_log
            WHERE agent_id = ?
            """,
            [agent_name],
        ).fetchall()
        if not rows:
            return 0.0
        diffs = [abs(r[0] - r[1]) for r in rows if r[0] is not None and r[1] is not None]
        return round(sum(diffs) / len(diffs), 4) if diffs else 0.0
    except Exception:
        return 0.0


def _compute_brier_score(conn, agent_name):
    try:
        rows = conn.execute(
            """
            SELECT e.confidence, COALESCE(o.outcome, 0.5)
            FROM ohm_edges e
            LEFT JOIN ohm_outcomes o ON o.claim_node = e."from"
            WHERE e.created_by = ? AND e.layer IN ('L3','L4')
            """,
            [agent_name],
        ).fetchall()
        if not rows:
            return 0.0
        total = sum((c - o) ** 2 for c, o in rows if c is not None and o is not None)
        return round(total / len(rows), 4) if rows else 0.0
    except Exception:
        return 0.0


def _compute_novelty_score(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN sigma > 1.5 THEN 1 ELSE 0 END) AS surprises
            FROM ohm_observations
            WHERE created_by = ? AND sigma IS NOT NULL
            """,
            [agent_name],
        ).fetchone()
        total = row[0] or 0
        surprises = row[1] or 0
        return round(surprises / max(total, 1), 4) if total > 0 else 0.0
    except Exception:
        return 0.0


def _compute_contrarian_value(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(DISTINCT e.id) AS challenged_count,
                SUM(CASE WHEN e.challenge_of IS NULL THEN 1 ELSE 0 END) AS survived
            FROM ohm_edges c
            JOIN ohm_edges e ON c.challenge_of = e.id
            WHERE c.challenge_type = 'CHALLENGED_BY'
              AND e.created_by = ?
              AND e.layer IN ('L3','L4')
            """,
            [agent_name],
        ).fetchone()
        challenged = row[0] or 0
        survived = row[1] or 0
        if challenged == 0:
            return 0.0
        return round(survived / challenged, 4)
    except Exception:
        return 0.0


def _compute_evidence_quality(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN n.source_url IS NOT NULL THEN 1 ELSE 0 END) AS with_url
            FROM ohm_nodes n
            WHERE n.created_by = ? AND n.deleted_at IS NULL
            """,
            [agent_name],
        ).fetchone()
        total = row[0] or 0
        with_url = row[1] or 0
        if total == 0:
            return 0.0
        url_fraction = with_url / total
        reliability = effective_reliability(conn, agent_name).get("effective_reliability", 0.5)
        return round(reliability * 0.5 + url_fraction * 0.5, 4)
    except Exception:
        return 0.0


def _compute_evidence_freshness(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                AVG(EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) / 86400) AS avg_age_days
            FROM ohm_observations
            WHERE created_by = ?
            """,
            [agent_name],
        ).fetchone()
        avg_age = row[0] if row and row[0] is not None else 999
        # 0 days = 1.0 (fresh), 30+ days = 0.0 (stale)
        return round(max(0.0, 1.0 - avg_age / 30.0), 4) if avg_age is not None else 0.0
    except Exception:
        return 0.0


def _compute_mechanism_quality(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN condition IS NOT NULL AND condition != '' THEN 1 ELSE 0 END) AS with_mechanism
            FROM ohm_edges
            WHERE created_by = ? AND edge_type = 'CAUSES' AND layer IN ('L3','L4')
            """,
            [agent_name],
        ).fetchone()
        total = row[0] or 0
        with_mech = row[1] or 0
        return round(with_mech / max(total, 1), 4) if total > 0 else 0.0
    except Exception:
        return 0.0


def _compute_information_contribution(conn, agent_name):
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_nodes,
                COUNT(DISTINCT id) AS unique_ids
            FROM ohm_nodes
            WHERE created_by = ? AND deleted_at IS NULL
            """,
            [agent_name],
        ).fetchone()
        total = row[0] or 0
        if total == 0:
            return 0.0
        # Fraction of nodes that are unique to this agent (not created by others)
        multi_row = conn.execute(
            """
            SELECT COUNT(DISTINCT id) FROM ohm_nodes
            WHERE deleted_at IS NULL GROUP BY id HAVING COUNT(DISTINCT created_by) > 1
            """,
        ).fetchone()
        shared = multi_row[0] if multi_row else 0
        return round((total - shared) / total, 4) if total > 0 else 0.0
    except Exception:
        return 0.0


def _compute_loop_risk(conn, agent_name):
    """Per-target loop risk based on belief calibration log.

    High loop risk = many belief requests on the same target without
    new independent evidence.
    """
    try:
        rows = conn.execute(
            """
            SELECT target_node, COUNT(*) AS n_requests
            FROM ohm_belief_calibration_log
            WHERE agent_id = ?
            GROUP BY target_node
            ORDER BY n_requests DESC
            LIMIT 10
            """,
            [agent_name],
        ).fetchall()
        if not rows:
            return {}
        result = {}
        max_requests = max(r[1] for r in rows) or 1
        for target, n in rows:
            # Normalize: 0 = no risk, 1 = max risk (most repeated requests)
            risk = min(n / max_requests, 1.0) * 0.5
            # Add language-graph divergence component
            try:
                div_row = conn.execute(
                    """
                    SELECT AVG(ABS(claimed_probability - graph_probability))
                    FROM ohm_belief_calibration_log
                    WHERE agent_id = ? AND target_node = ?
                    """,
                    [agent_name, target],
                ).fetchone()
                divergence = div_row[0] if div_row and div_row[0] is not None else 0.0
                risk = min(risk + divergence * 0.5, 1.0)
            except Exception:
                pass
            result[target] = round(risk, 4)
        return result
    except Exception:
        return {}


def _compute_blast_radius_awareness(conn, agent_name, total_edges):
    """How well the agent calibrates confidence to actual stakes.

    Measures whether the agent uses lower confidence for high-blast-radius
    decisions (nodes with many downstream dependencies).
    """
    try:
        rows = conn.execute(
            """
            SELECT
                e.confidence,
                (SELECT COUNT(*) FROM ohm_edges d WHERE d."from" = e."to" AND d.layer IN ('L3','L4') AND d.deleted_at IS NULL) AS downstream
            FROM ohm_edges e
            WHERE e.created_by = ? AND e.layer IN ('L3','L4') AND e.deleted_at IS NULL
            """,
            [agent_name],
        ).fetchall()
        if not rows:
            return 0.0
        # Good awareness = negative correlation between confidence and downstream count
        # High downstream should → lower confidence
        confidences = [r[0] or 0.5 for r in rows]
        downstreams = [r[1] or 0 for r in rows]
        avg_conf = sum(confidences) / len(confidences)
        avg_down = sum(downstreams) / len(downstreams)
        if avg_down == 0:
            return 0.5  # neutral — no downstream dependencies
        # Simple proxy: if agent's avg confidence on high-downstream nodes
        # is lower than on low-downstream nodes, awareness is good
        high_stakes = [(c, d) for c, d in zip(confidences, downstreams) if d > avg_down]
        low_stakes = [(c, d) for c, d in zip(confidences, downstreams) if d <= avg_down]
        high_avg = sum(c for c, _ in high_stakes) / len(high_stakes) if high_stakes else avg_conf
        low_avg = sum(c for c, _ in low_stakes) / len(low_stakes) if low_stakes else avg_conf
        # Awareness = 1 when high_avg < low_avg (good), 0 when equal, negative when reversed
        diff = low_avg - high_avg
        return round(max(0.0, min(1.0, 0.5 + diff)), 4)
    except Exception:
        return 0.5


def _intervention_ladder(max_loop_risk):
    """Determine intervention level from max loop risk (OHM-792)."""
    if max_loop_risk > 0.85:
        return "observation_quarantine"
    elif max_loop_risk > 0.6:
        return "category_only_answer"
    elif max_loop_risk > 0.3:
        return "autonomy_prompt"
    elif max_loop_risk > 0.0:
        return "soft_nudge"
    return "none"
