"""OHM-xdd4: Temporal decay for observations.

Observations have a half-life. A sentiment observation recorded yesterday
is more reliable than one from three months ago. A verified structural fact
(verification obs_type) is durable; a price quote (measurement) is perishable.

Design principles:
- Each obs_type has a default half_life_days; agents can override per-observation
- Negative half_life_days = appreciating (pattern recognition gets better with age)
- half_life_days = 0 = binary (valid until superseded, then 0)
- half_life_days is None = permanent (never decays; for verified structural facts)
- confidence_at(obs, t) is the single function callers need

Default half-lives by type (from OHM-xdd4 research synthesis):
  measurement  →  7 days   (perishable: prices, quotes, readings)
  sentiment    →  3 days   (fast-perishable: opinions, moods)
  verification → 180 days  (durable: structural facts, audits)
  outcome      →  None     (permanent: recorded results don't decay)
  source       →  30 days  (standard: source notes)
  pattern      → -30 days  (appreciating: patterns strengthen with confirmation)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# Default half-lives per obs_type (days). None = permanent, negative = appreciating.
DEFAULT_HALF_LIFE: dict[str, float | None] = {
    "measurement":  7.0,
    "sentiment":    3.0,
    "verification": 180.0,
    "outcome":      None,   # permanent
    "source":       30.0,
    "pattern":      -30.0,  # appreciating
    # Generic fallback for custom types
    "_default":     30.0,
}


def default_half_life(obs_type: str) -> float | None:
    """Return the default half_life_days for a given observation type."""
    return DEFAULT_HALF_LIFE.get(obs_type, DEFAULT_HALF_LIFE["_default"])


def confidence_at(
    obs: dict[str, Any],
    t: datetime | None = None,
) -> float:
    """Compute effective confidence of an observation at time t.

    Args:
        obs: Observation record dict (must have 'value', 'created_at',
             and optionally 'half_life_days', 'valid_to', 'valid_from').
        t: Point in time to evaluate at. Defaults to now (UTC).

    Returns:
        Effective confidence in [0.0, 1.0].
    """
    if t is None:
        t = datetime.now(timezone.utc)
    elif t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)

    # If superseded (valid_to is set and in the past), confidence = 0
    valid_to = obs.get("valid_to")
    if valid_to is not None:
        if isinstance(valid_to, str):
            valid_to = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
        if valid_to.tzinfo is None:
            valid_to = valid_to.replace(tzinfo=timezone.utc)
        if t >= valid_to:
            return 0.0

    base_value = float(obs.get("value") or 1.0)
    base_value = max(0.0, min(1.0, base_value))

    # Determine half_life_days: use stored value if present, else obs_type default
    half_life = obs.get("half_life_days")
    if half_life is None:
        half_life = default_half_life(obs.get("type", "_default"))

    # Permanent (half_life_days IS NULL in DB, default_half_life returned None)
    if half_life is None:
        return base_value

    # Compute age from valid_from (or created_at if not set)
    anchor = obs.get("valid_from") or obs.get("created_at")
    if anchor is None:
        return base_value
    if isinstance(anchor, str):
        anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)

    age_days = max(0.0, (t - anchor).total_seconds() / 86400.0)

    # Binary (half_life_days == 0): valid until superseded
    if half_life == 0.0:
        return base_value  # valid_to check above already handles supersession

    # Appreciating (negative half_life_days): confidence grows with age, capped at 1.0
    if half_life < 0.0:
        appreciation_rate = math.log(2) / abs(half_life)
        return min(1.0, base_value * (1.0 + appreciation_rate * age_days))

    # Standard exponential decay
    return base_value * math.exp(-math.log(2) * age_days / half_life)


def decay_profile(half_life_days: float | None) -> str:
    """Return a human-readable decay profile name for a half_life_days value."""
    if half_life_days is None:
        return "permanent"
    if half_life_days == 0.0:
        return "binary"
    if half_life_days < 0.0:
        return "appreciating"
    if half_life_days <= 5.0:
        return "fast-perishable"
    if half_life_days <= 14.0:
        return "perishable"
    if half_life_days <= 60.0:
        return "standard"
    return "durable"


# ── Supersession chain queries ────────────────────────────────────────────────


def supersede_observation(
    conn: "DuckDBPyConnection",
    *,
    new_obs_id: str,
    old_obs_id: str,
    agent: str,
) -> dict[str, Any]:
    """Link new_obs_id as the supersession of old_obs_id.

    Sets valid_to = now() on the old observation and records the chain link
    on the new observation. Both observations must exist and not be deleted.

    Returns a dict with old_obs and new_obs records, plus the supersession timestamp.

    Raises:
        ValueError: if either observation doesn't exist, or if old is already superseded.
    """
    now = datetime.now(timezone.utc)

    old = conn.execute(
        "SELECT * FROM ohm_observations WHERE id = ? AND deleted_at IS NULL",
        [old_obs_id],
    ).fetchone()
    if not old:
        raise ValueError(f"Old observation not found: {old_obs_id}")

    old_cols = [d[0] for d in conn.description]
    old_dict = dict(zip(old_cols, old))

    if old_dict.get("valid_to") is not None:
        raise ValueError(
            f"Observation {old_obs_id} is already superseded (valid_to={old_dict['valid_to']}). "
            "Chain to the most recent observation instead."
        )

    new = conn.execute(
        "SELECT * FROM ohm_observations WHERE id = ? AND deleted_at IS NULL",
        [new_obs_id],
    ).fetchone()
    if not new:
        raise ValueError(f"New observation not found: {new_obs_id}")

    new_cols = [d[0] for d in conn.description]
    new_dict = dict(zip(new_cols, new))

    # Mark old as no longer active
    conn.execute(
        "UPDATE ohm_observations SET valid_to = ? WHERE id = ?",
        [now, old_obs_id],
    )
    # Link new to old in the supersession chain
    conn.execute(
        "UPDATE ohm_observations SET supersedes_obs_id = ?, valid_from = ? WHERE id = ?",
        [old_obs_id, now, new_obs_id],
    )

    # Refetch both
    old_updated = conn.execute(
        "SELECT * FROM ohm_observations WHERE id = ?", [old_obs_id]
    ).fetchone()
    old_cols2 = [d[0] for d in conn.description]
    new_updated = conn.execute(
        "SELECT * FROM ohm_observations WHERE id = ?", [new_obs_id]
    ).fetchone()
    new_cols2 = [d[0] for d in conn.description]

    return {
        "superseded_at": now.isoformat(),
        "old_observation": dict(zip(old_cols2, old_updated)),
        "new_observation": dict(zip(new_cols2, new_updated)),
    }


def get_observation_chain(
    conn: "DuckDBPyConnection",
    obs_id: str,
    *,
    max_depth: int = 20,
) -> list[dict[str, Any]]:
    """Retrieve the full supersession chain for an observation.

    Walks backward via supersedes_obs_id from the given observation
    to the original. Returns oldest-first.

    Args:
        obs_id: Starting observation ID (typically the most recent one).
        max_depth: Maximum chain length to traverse.

    Returns:
        List of observation records, oldest first, each enriched with
        effective_confidence (at the time valid_to was set, or now if active).
    """
    now = datetime.now(timezone.utc)

    result = conn.execute(
        f"""
        WITH RECURSIVE chain AS (
            SELECT *, 1 AS depth
            FROM ohm_observations
            WHERE id = ? AND deleted_at IS NULL

            UNION ALL

            SELECT o.*, c.depth + 1
            FROM ohm_observations o
            JOIN chain c ON o.id = c.supersedes_obs_id
            WHERE c.depth < {max_depth}
              AND o.deleted_at IS NULL
        )
        SELECT * FROM chain ORDER BY depth DESC
        """,
        [obs_id],
    )
    cols = [d[0] for d in result.description]
    rows = [dict(zip(cols, row)) for row in result.fetchall()]

    # Enrich with effective_confidence
    for row in rows:
        eval_at = row.get("valid_to") or now
        if isinstance(eval_at, str):
            eval_at = datetime.fromisoformat(eval_at.replace("Z", "+00:00"))
        row["effective_confidence"] = round(confidence_at(row, t=eval_at), 4)
        row["decay_profile"] = decay_profile(row.get("half_life_days"))

    return rows


def get_active_observations(
    conn: "DuckDBPyConnection",
    node_id: str,
    *,
    min_validity: float = 0.0,
    at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return active observations for a node, filtered by effective confidence.

    'Active' means valid_to IS NULL (not superseded). effective_confidence
    is computed at time `at` (defaults to now) and rows below min_validity
    are excluded.

    Args:
        node_id: Node to query observations for.
        min_validity: Minimum effective confidence threshold (0.0 = return all active).
        at: Point in time to evaluate decay at. Defaults to now.

    Returns:
        List of observation records enriched with effective_confidence and decay_profile.
    """
    t = at or datetime.now(timezone.utc)

    result = conn.execute(
        """
        SELECT * FROM ohm_observations
        WHERE node_id = ?
          AND deleted_at IS NULL
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY created_at DESC
        """,
        [node_id, t],
    )
    cols = [d[0] for d in result.description]
    rows = [dict(zip(cols, row)) for row in result.fetchall()]

    enriched = []
    for row in rows:
        eff = confidence_at(row, t=t)
        if eff >= min_validity:
            row["effective_confidence"] = round(eff, 4)
            row["decay_profile"] = decay_profile(row.get("half_life_days"))
            enriched.append(row)

    return enriched


# ── Chain Validity (OHM-wuki) ─────────────────────────────────────────────────


def chain_validity(
    conn: "DuckDBPyConnection",
    synthesis_id: str,
    *,
    t: datetime | None = None,
    threshold: float = 0.1,
) -> dict[str, Any]:
    """Compute STL weakest-link chain validity for a synthesis node.

    A synthesis built on N supporting observations is only as valid as its
    weakest link. This implements the STL robustness metric:
        φ = G[0,n](v(s_i) ≥ γ)  — globally all observations meet threshold γ.

    Two complementary metrics:
    - **weakest_link**: min(confidence_at(obs)) — one bad link fails the chain.
    - **chain_validity**: ∏(confidence_at(obs)) — multiplicative; five obs at
      0.6 each = 0.6^5 = 0.078, far worse than any individual observation.

    Supporting observations are gathered from nodes connected by outgoing L3
    edges from synthesis_id (the cluster nodes the synthesis was built on).
    If a cluster node has no observations, it is represented by the edge
    confidence as a proxy (marked synthetic=True in the output).

    Args:
        synthesis_id: Node ID of the synthesis (concept) node.
        t: Point in time to evaluate at. Defaults to now.
        threshold: Validity threshold for STL guarantee (default 0.1).

    Returns:
        Dict with weakest_link, chain_validity, chain_product, n_observations,
        validity_threshold_met, robustness, and per-observation breakdown.
    """
    if t is None:
        t = datetime.now(timezone.utc)
    elif t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)

    # Find all outgoing L3 edges from the synthesis node
    edge_result = conn.execute(
        """
        SELECT id, to_node, confidence, edge_type
        FROM ohm_edges
        WHERE from_node = ?
          AND layer = 'L3'
          AND deleted_at IS NULL
        """,
        [synthesis_id],
    )
    edge_cols = [d[0] for d in edge_result.description]
    edges = [dict(zip(edge_cols, row)) for row in edge_result.fetchall()]

    # Also include the synthesis node's own observations (its self-assessment)
    own_obs = get_active_observations(conn, synthesis_id, at=t)

    obs_details: list[dict[str, Any]] = []

    # Supporting observations from cluster nodes
    cluster_nodes_with_obs: set[str] = set()
    for edge in edges:
        cluster_id = edge["to_node"]
        node_obs = get_active_observations(conn, cluster_id, at=t)
        if node_obs:
            cluster_nodes_with_obs.add(cluster_id)
            for obs in node_obs:
                obs_details.append({
                    "obs_id": obs["id"],
                    "node_id": cluster_id,
                    "effective_confidence": obs["effective_confidence"],
                    "decay_profile": obs["decay_profile"],
                    "half_life_days": obs.get("half_life_days"),
                    "synthetic": False,
                })
        else:
            # No observations — use edge confidence as proxy
            edge_conf = float(edge.get("confidence") or 0.7)
            obs_details.append({
                "obs_id": None,
                "node_id": cluster_id,
                "effective_confidence": round(edge_conf, 4),
                "decay_profile": "edge_proxy",
                "half_life_days": None,
                "synthetic": True,
            })

    # Include own observations if any (the synthesis self-assessment)
    for obs in own_obs:
        obs_details.append({
            "obs_id": obs["id"],
            "node_id": synthesis_id,
            "effective_confidence": obs["effective_confidence"],
            "decay_profile": obs["decay_profile"],
            "half_life_days": obs.get("half_life_days"),
            "synthetic": False,
            "self_assessment": True,
        })

    if not obs_details:
        return {
            "synthesis_id": synthesis_id,
            "weakest_link": 0.0,
            "chain_validity": 0.0,
            "n_observations": 0,
            "n_cluster_nodes": len(edges),
            "validity_threshold_met": False,
            "robustness": -threshold,
            "observations": [],
            "evaluated_at": t.isoformat(),
        }

    confidences = [o["effective_confidence"] for o in obs_details]
    weakest = min(confidences)
    product = 1.0
    for c in confidences:
        product *= c

    return {
        "synthesis_id": synthesis_id,
        "weakest_link": round(weakest, 4),
        "chain_validity": round(product, 6),
        "n_observations": len(obs_details),
        "n_cluster_nodes": len(edges),
        "validity_threshold_met": weakest >= threshold,
        "robustness": round(weakest - threshold, 4),
        "observations": sorted(obs_details, key=lambda o: o["effective_confidence"]),
        "evaluated_at": t.isoformat(),
    }
