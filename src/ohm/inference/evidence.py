"""Evidence discretization utilities for OHM Bayesian inference.

OHM's Bayesian network represents node states as binary (good / bad).
These utilities convert continuous domain observations — sensor readings,
KPIs, rates, percentages — into the probability format expected by the
inference engine: a p_bad ∈ [0, 1] expressing belief that the node is
currently in its "bad" state.

Three methods are supported:

  threshold      Hard cutoff: p_bad ∈ {0, 1}. Use when the domain has a
                 crisp pass/fail boundary (e.g. pH < 7.0 is bad).

  soft_threshold Sigmoid around the threshold. Produces a smooth transition
                 that reflects measurement uncertainty near the boundary.
                 Requires sigma (or uses 10 % of threshold as default scale).

  zscore         Directional z-score from a normal baseline distribution.
                 p_bad increases as the value moves away from baseline in the
                 bad direction. Requires baseline and sigma.

Usage::

    from ohm.evidence import discretize_evidence

    # Hard threshold: kiln exit temp > 1450 °C is bad
    ev = discretize_evidence(1480, threshold=1450, direction="above_is_bad")
    # → {"state": "bad", "p_bad": 1.0, ...}

    # Soft threshold: same boundary, smooth transition within ±30 °C
    ev = discretize_evidence(1460, threshold=1450, sigma=30,
                              direction="above_is_bad", method="soft_threshold")
    # → {"state": "bad", "p_bad": 0.63, ...}

    # Z-score: kiln feed rate baseline 120 t/h ±10 t/h; high is bad
    ev = discretize_evidence(145, baseline=120, sigma=10,
                              direction="above_is_bad")
    # → {"state": "bad", "p_bad": 0.994, ...}
"""

from __future__ import annotations

import math
from typing import Any


def _norm_cdf(z: float) -> float:
    """Standard normal CDF — no scipy required."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _sigmoid(x: float) -> float:
    """Logistic sigmoid, numerically stable for large |x|."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def discretize_evidence(
    value: float,
    *,
    baseline: float | None = None,
    sigma: float | None = None,
    threshold: float | None = None,
    direction: str = "above_is_bad",
    method: str = "auto",
) -> dict[str, Any]:
    """Convert a continuous measurement into binary evidence for Bayesian inference.

    Parameters
    ----------
    value:
        The observed measurement.
    baseline:
        Expected value under normal operating conditions (mean of the "good"
        distribution). Required for method="zscore".
    sigma:
        Standard deviation or scale parameter.
        - zscore: std-dev of the baseline distribution
        - soft_threshold: transition width around the threshold (defaults to
          ``max(1e-9, abs(threshold) * 0.1)`` when not provided)
    threshold:
        Crisp boundary between good and bad.
        Required for method="threshold" or method="soft_threshold".
    direction:
        ``"above_is_bad"`` (default) — values above baseline/threshold are bad.
        ``"below_is_bad"``           — values below baseline/threshold are bad.
    method:
        ``"auto"``           — infer from inputs: zscore if baseline+sigma given,
                               soft_threshold if threshold+sigma given,
                               threshold if only threshold given.
        ``"threshold"``      — hard cutoff at threshold; p_bad ∈ {0, 1}.
        ``"soft_threshold"`` — sigmoid around threshold with sigma as scale.
        ``"zscore"``         — directional CDF from N(baseline, sigma²).

    Returns
    -------
    dict with keys:
        state      "good" or "bad" (majority vote from p_bad threshold 0.5)
        p_bad      float in [0, 1] — P(node is in bad state)
        p_good     float in [0, 1] — 1 - p_bad
        method     the method actually used
        raw_value  the input value echoed back
    """
    if direction not in ("above_is_bad", "below_is_bad"):
        raise ValueError(
            f"direction must be 'above_is_bad' or 'below_is_bad', got {direction!r}"
        )

    # Auto-select method from inputs
    if method == "auto":
        if baseline is not None and sigma is not None:
            method = "zscore"
        elif threshold is not None and sigma is not None:
            method = "soft_threshold"
        elif threshold is not None:
            method = "threshold"
        else:
            raise ValueError(
                "discretize_evidence requires at least one of: "
                "(baseline + sigma), (threshold + sigma), or threshold alone. "
                "Provide the appropriate parameters for your method."
            )

    if method == "threshold":
        if threshold is None:
            raise ValueError("method='threshold' requires threshold parameter")
        if direction == "above_is_bad":
            p_bad = 1.0 if value >= threshold else 0.0
        else:
            p_bad = 1.0 if value <= threshold else 0.0

    elif method == "soft_threshold":
        if threshold is None:
            raise ValueError("method='soft_threshold' requires threshold parameter")
        scale = sigma if sigma is not None else max(1e-9, abs(threshold) * 0.1)
        z = (value - threshold) / scale
        if direction == "above_is_bad":
            p_bad = _sigmoid(z)
        else:
            p_bad = _sigmoid(-z)

    elif method == "zscore":
        if baseline is None or sigma is None:
            raise ValueError("method='zscore' requires both baseline and sigma parameters")
        if sigma <= 0:
            raise ValueError("sigma must be positive")
        z = (value - baseline) / sigma
        if direction == "above_is_bad":
            p_bad = _norm_cdf(z)
        else:
            p_bad = _norm_cdf(-z)

    else:
        raise ValueError(
            f"Unknown method: {method!r}. "
            "Choose from: 'auto', 'threshold', 'soft_threshold', 'zscore'"
        )

    p_bad = max(0.0, min(1.0, p_bad))
    p_good = 1.0 - p_bad

    return {
        "state": "bad" if p_bad >= 0.5 else "good",
        "p_bad": round(p_bad, 6),
        "p_good": round(p_good, 6),
        "method": method,
        "direction": direction,
        "raw_value": value,
    }


def discretize_alarms(
    values: "dict[str, float]",
    thresholds: "dict[str, tuple[float | None, float | None]]",
    default_state: int = 1,
) -> "dict[str, int]":
    """Map a batch of sensor/metric values to binary states via alarm bounds (OHM-mk5v).

    Each key in *values* is classified as 0 (bad) if its reading falls outside
    the ``(low_alarm, high_alarm)`` interval, or 1 (good) if it's inside.
    Keys with no entry in *thresholds* receive *default_state*.

    Args:
        values: ``{name: reading}`` dict of continuous measurements.
        thresholds: ``{name: (low_alarm, high_alarm)}`` — either bound may be
            ``None`` for one-sided alarm (e.g. ``(None, 100.0)`` = only an
            upper limit).
        default_state: State to assign when a key has no threshold (default 1 = good).

    Returns:
        ``{name: 0_or_1}`` binary state dict (0 = bad, 1 = good).

    Examples::

        discretize_alarms({"temp": 105}, {"temp": (0.0, 100.0)})
        # → {"temp": 0}   (above high_alarm)

        discretize_alarms({"ph": 6.5}, {"ph": (7.0, 14.0)})
        # → {"ph": 0}     (below low_alarm)

        discretize_alarms({"rpm": 1800}, {"rpm": (1000.0, 2000.0)})
        # → {"rpm": 1}    (within alarm band)

        discretize_alarms({"unknown": 42}, {})
        # → {"unknown": 1}  (no threshold → default_state)
    """
    result: "dict[str, int]" = {}
    for key, reading in values.items():
        bounds = thresholds.get(key)
        if bounds is None:
            result[key] = default_state
            continue
        low, high = bounds
        bad = (low is not None and reading < low) or (high is not None and reading > high)
        result[key] = 0 if bad else 1
    return result
