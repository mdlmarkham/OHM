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

    Returns:
        Dict with agent_id, p_accurate, effective_reliability,
        days_since_verification, community_prior, and decay_lambda.
    """
    if t is None:
        t = datetime.now(timezone.utc)

    # Get agent's p_accurate and last outcome date
    agent_stats = conn.execute(
        """
        SELECT
            COUNT(*) AS total_outcomes,
            SUM(CASE WHEN outcome = TRUE THEN 1 ELSE 0 END) AS accurate,
            MAX(recorded_at) AS last_outcome_at
        FROM ohm_outcomes
        WHERE COALESCE(claimed_by, source_agent) = ?
        """,
        [agent_id],
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
