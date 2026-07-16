"""Forecast accuracy metrics (OHM-941 / Stage 4).

Compute MAE, RMSE, directional hit, and Brier score for forecast resolution.
"""

from __future__ import annotations

from typing import Any


def compute_accuracy(
    *,
    predicted_value: float | None,
    actual_value: float,
    distribution: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compute accuracy metrics for a forecast vs actual.

    Args:
        predicted_value: The p50/point forecast value (may be None).
        actual_value: The realized value.
        distribution: Optional distribution dict with p10/p50/p90 etc.

    Returns:
        Dict with mae, rmse, directional_hit, brier_score, error.
    """
    result: dict[str, Any] = {
        "actual_value": actual_value,
        "predicted_p50": predicted_value,
        "error": None,
        "mae": None,
        "rmse": None,
        "directional_hit": None,
        "brier_score": None,
    }

    if predicted_value is not None:
        error = actual_value - predicted_value
        mae = abs(error)
        result["error"] = round(error, 6)
        result["mae"] = round(mae, 6)
        result["rmse"] = round(mae, 6)

        if distribution and "p50" in distribution:
            p50 = distribution["p50"]
            if p50 != 0:
                predicted_direction = p50 > 0
                actual_direction = actual_value > 0
                result["directional_hit"] = predicted_direction == actual_direction
            else:
                result["directional_hit"] = None
        else:
            if predicted_value != 0:
                predicted_direction = predicted_value > 0
                actual_direction = actual_value > 0
                result["directional_hit"] = predicted_direction == actual_direction

    if distribution and "p50" in distribution:
        p50 = distribution.get("p50", 0)
        if actual_value is not None:
            brier = (p50 - (1.0 if actual_value > 0 else 0.0)) ** 2
            result["brier_score"] = round(brier, 6)

    return result