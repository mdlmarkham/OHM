"""Lightweight time-series helpers (OHM-943 / Stage 6).

Query, baseline, and anomaly detection on observations with ``series_id``
metadata — no new tables needed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def query_series(
    conn: DuckDBPyConnection,
    series_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return observations for a series, ordered by ``created_at``.

    Observations are identified by ``metadata.series_id == series_id``.
    """
    query = (
        "SELECT * FROM ohm_observations "
        "WHERE metadata IS NOT NULL "
        "AND json_extract_string(metadata, 'series_id') = ?"
    )
    params: list[Any] = [series_id]
    if start:
        query += " AND created_at >= ?::TIMESTAMP"
        params.append(start)
    if end:
        query += " AND created_at <= ?::TIMESTAMP"
        params.append(end)
    query += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)

    from ohm.graph.queries._shared import _rows_to_dicts

    rows = _rows_to_dicts(conn.execute(query, params))
    for row in rows:
        meta = row.get("metadata")
        if isinstance(meta, str):
            try:
                row["metadata"] = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                pass
    return rows


def compute_baseline(
    conn: DuckDBPyConnection,
    series_id: str,
    *,
    method: str = "rolling_30d",
    window: int | None = None,
) -> dict[str, Any]:
    """Compute baseline mean and std for a series.

    Args:
        method: ``"rolling_30d"`` (default, 30-point rolling window),
                ``"rolling_7d"`` (7-point), ``"mean"`` (global mean/std).
        window: Override window size (points). If None, derived from method.

    Returns:
        Dict with ``series_id``, ``method``, ``mean``, ``std``, ``n_points``, ``window``.
    """
    obs = query_series(conn, series_id)
    values = [float(o["value"]) for o in obs if o.get("value") is not None]
    n = len(values)
    if n == 0:
        return {
            "series_id": series_id,
            "method": method,
            "mean": None,
            "std": None,
            "n_points": 0,
            "window": window or _window_for_method(method),
        }

    if window is None:
        window = _window_for_method(method)

    if method == "mean" or n < (window or 2):
        mean_val = sum(values) / n
        if n > 1:
            var = sum((v - mean_val) ** 2 for v in values) / (n - 1)
            std_val = var ** 0.5
        else:
            std_val = 0.0
        return {
            "series_id": series_id,
            "method": method,
            "mean": round(mean_val, 6),
            "std": round(std_val, 6),
            "n_points": n,
            "window": window,
        }

    w = min(window, n)
    recent = values[-w:]
    mean_val = sum(recent) / w
    if w > 1:
        var = sum((v - mean_val) ** 2 for v in recent) / (w - 1)
        std_val = var ** 0.5
    else:
        std_val = 0.0

    return {
        "series_id": series_id,
        "method": method,
        "mean": round(mean_val, 6),
        "std": round(std_val, 6),
        "n_points": n,
        "window": w,
    }


def detect_series_anomalies(
    conn: DuckDBPyConnection,
    series_id: str,
    *,
    method: str = "rolling_30d",
    sigma: float = 2.0,
) -> list[dict[str, Any]]:
    """Return observations exceeding baseline +/- sigma*std.

    Uses a rolling baseline computed from the most recent window of points.
    Each observation is flagged if |value - baseline_mean| > sigma * baseline_std.
    """
    obs = query_series(conn, series_id)
    values = [float(o["value"]) for o in obs if o.get("value") is not None]
    n = len(values)
    if n < 2:
        return []

    window = _window_for_method(method)
    w = min(window, n)

    if method == "mean":
        mean_val = sum(values) / n
        var = sum((v - mean_val) ** 2 for v in values) / (n - 1)
        std_val = var ** 0.5
    else:
        recent = values[-w:]
        mean_val = sum(recent) / w
        if w > 1:
            var = sum((v - mean_val) ** 2 for v in recent) / (w - 1)
            std_val = var ** 0.5
        else:
            std_val = 0.0

    if std_val == 0:
        return []

    threshold = sigma * std_val
    anomalies: list[dict[str, Any]] = []
    for o, v in zip(obs, values):
        deviation = abs(v - mean_val)
        if deviation > threshold:
            anomalies.append({
                "id": o.get("id"),
                "node_id": o.get("node_id"),
                "value": v,
                "mean": round(mean_val, 6),
                "std": round(std_val, 6),
                "deviation": round(deviation, 6),
                "threshold": round(threshold, 6),
                "sigma": sigma,
                "created_at": o.get("created_at"),
            })
    return anomalies


def _window_for_method(method: str) -> int:
    """Derive window size from method name."""
    if method == "rolling_30d":
        return 30
    if method == "rolling_7d":
        return 7
    if method == "rolling_14d":
        return 14
    if method == "mean":
        return 0
    return 30