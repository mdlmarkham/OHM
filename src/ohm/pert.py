"""PERT (Program Evaluation and Review Technique) elicitation module.

ADR-013: PERT three-point estimation for probability distributions.
Provides validation, computation, and expert aggregation utilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class PERTELError(ValueError):
    """Raised when PERT estimates are invalid."""


def validate_pert(p05: float, p50: float, p95: float, *, bounds: tuple[float, float] = (0.0, 1.0)) -> None:
    """Validate PERT three-point estimates.

    Args:
        p05: Optimistic estimate (5th percentile).
        p50: Most likely estimate (median).
        p95: Pessimistic estimate (95th percentile).
        bounds: Allowed value range (default [0, 1] for probabilities).

    Raises:
        PERTELError: If estimates violate ordering, bounds, or are degenerate.
    """
    lo, hi = bounds
    if not (lo <= p05 <= hi):
        raise PERTELError(f"p05={p05} outside bounds [{lo}, {hi}]")
    if not (lo <= p50 <= hi):
        raise PERTELError(f"p50={p50} outside bounds [{lo}, {hi}]")
    if not (lo <= p95 <= hi):
        raise PERTELError(f"p95={p95} outside bounds [{lo}, {hi}]")
    if p05 > p50:
        raise PERTELError(f"p05={p05} > p50={p50}: optimistic exceeds most-likely")
    if p50 > p95:
        raise PERTELError(f"p50={p50} > p95={p95}: most-likely exceeds pessimistic")
    if p05 == p95:
        raise PERTELError(f"p05={p05} == p95={p95}: degenerate (zero spread)")


def compute_pert_mean(p05: float, p50: float, p95: float) -> float:
    """Compute PERT mean estimate.

        μ = (O + 4M + P) / 6

    where O = optimistic (P05), M = most likely (P50), P = pessimistic (P95).

    Args:
        p05: Optimistic estimate (5th percentile).
        p50: Most likely estimate (median).
        p95: Pessimistic estimate (95th percentile).

    Returns:
        PERT mean estimate.
    """
    return (p05 + 4 * p50 + p95) / 6.0


def compute_pert_variance(p05: float, p95: float) -> float:
    """Compute PERT variance estimate.

        σ² = ((P - O) / 6)²

    High variance = high uncertainty = high VoI if downstream decision
    impact is also high.

    Args:
        p05: Optimistic estimate (5th percentile).
        p95: Pessimistic estimate (95th percentile).

    Returns:
        PERT variance estimate.
    """
    return ((p95 - p05) / 6.0) ** 2


def scale_pert_variance(spread: float) -> float:
    """Scale PERT spread to a [0, 1] uncertainty signal via sigmoid.

    The raw PERT variance σ² = ((p95-p05)/6)² is on a very narrow range
    (max ≈ 0.028 for [0,1] bounds), so linear scaling (×36) compressed
    meaningful differences at low spread and saturated at high spread.

    Sigmoid scaling provides better discrimination:
    - spread=0.1 (tight PERT) → ~0.12 (low uncertainty)
    - spread=0.3 (moderate) → 0.5
    - spread=0.5 (wide PERT) → ~0.88 (high uncertainty)
    - spread=1.0 (full range) → ~1.0

    Formula: 1 / (1 + exp(-10 * (spread - 0.3)))

    Args:
        spread: p95 - p05, the PERT range.

    Returns:
        Scaled uncertainty in [0, 1].
    """
    import math
    return 1.0 / (1.0 + math.exp(-10.0 * (spread - 0.3)))


def aggregate_mixture_of_experts(
    estimates: list[tuple[float, float, float]],
    weights: list[float] | None = None,
) -> dict[str, float]:
    """Aggregate multiple expert PERT estimates via weighted mixture.

    Each expert provides a (p05, p50, p95) triple. The aggregation
    computes the weighted mean and between-expert variance, producing
    a combined estimate that accounts for both within-expert uncertainty
    (PERT variance) and between-expert disagreement.

    Args:
        estimates: List of (p05, p50, p95) triples from each expert.
        weights: Optional weights per expert (uniform if None).
            Weights are normalized to sum to 1.

    Returns:
        Dict with 'mean', 'variance', 'between_variance', 'total_variance',
        'p05', 'p50', 'p95'.
    """
    if not estimates:
        return {"mean": 0.0, "variance": 0.0, "between_variance": 0.0, "total_variance": 0.0, "p05": 0.0, "p50": 0.0, "p95": 0.0}

    n = len(estimates)
    if weights is None:
        weights = [1.0 / n] * n
    else:
        total = sum(weights)
        if total <= 0:
            raise PERTELError("Weights must sum to a positive value")
        weights = [w / total for w in weights]

    expert_means = [compute_pert_mean(*e) for e in estimates]
    expert_vars = [compute_pert_variance(e[0], e[2]) for e in estimates]

    weighted_mean = sum(w * m for w, m in zip(weights, expert_means))
    within_variance = sum(w * v for w, v in zip(weights, expert_vars))
    between_variance = sum(w * (m - weighted_mean) ** 2 for w, m in zip(weights, expert_means))
    total_variance = within_variance + between_variance

    expert_p05s = [e[0] for e in estimates]
    expert_p50s = [e[1] for e in estimates]
    expert_p95s = [e[2] for e in estimates]
    agg_p05 = sum(w * p for w, p in zip(weights, expert_p05s))
    agg_p50 = sum(w * p for w, p in zip(weights, expert_p50s))
    agg_p95 = sum(w * p for w, p in zip(weights, expert_p95s))

    return {
        "mean": weighted_mean,
        "variance": within_variance,
        "between_variance": between_variance,
        "total_variance": total_variance,
        "p05": agg_p05,
        "p50": agg_p50,
        "p95": agg_p95,
    }


def anchored_pert(
    p05: float,
    p50: float,
    p95: float,
    reference_class: float,
    adjustment_factor: float = 0.5,
) -> dict[str, float]:
    """Adjust PERT estimates using reference class anchoring.

    Pulls the PERT estimate toward a reference class (base rate) value
    by the adjustment factor. A factor of 0 means no adjustment (use
    PERT as-is); 1 means fully use the reference class.

    Args:
        p05: Optimistic estimate (5th percentile).
        p50: Most likely estimate (median).
        p95: Pessimistic estimate (95th percentile).
        reference_class: Base rate / reference value to anchor toward.
        adjustment_factor: How much to shrink toward the reference
            (0 = no adjustment, 1 = full reference class).

    Returns:
        Dict with 'p05', 'p50', 'p95', 'mean', 'variance'.
    """
    if not (0.0 <= adjustment_factor <= 1.0):
        raise PERTELError(f"adjustment_factor={adjustment_factor} not in [0, 1]")

    pert_mean = compute_pert_mean(p05, p50, p95)
    adjusted_mean = pert_mean * (1 - adjustment_factor) + reference_class * adjustment_factor
    spread = (p95 - p05) / 2.0 * (1 - adjustment_factor * 0.5)
    adjusted_p50 = adjusted_mean
    adjusted_p05 = max(0.0, adjusted_mean - spread)
    adjusted_p95 = min(1.0, adjusted_mean + spread)

    return {
        "p05": adjusted_p05,
        "p50": adjusted_p50,
        "p95": adjusted_p95,
        "mean": adjusted_mean,
        "variance": compute_pert_variance(adjusted_p05, adjusted_p95),
    }
