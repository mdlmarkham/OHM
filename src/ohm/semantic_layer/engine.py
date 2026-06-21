"""OHM semantic layer — YAML-defined metrics over the knowledge graph.

Loads metric definitions from `metrics.yaml` and executes them against a
DuckDB connection. Uses Ibis when available, otherwise falls back to plain
DuckDB SQL.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PATH = Path(__file__).with_name("metrics.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def load_metrics(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load metric definitions from YAML.

    Args:
        path: Path to the metrics YAML file. Defaults to bundled metrics.yaml.

    Returns:
        Dict mapping metric name to its definition dict, including
        'description', 'sql', and 'thresholds'.
    """
    if path is None:
        path = DEFAULT_METRICS_PATH
    path = Path(path)
    raw = _load_yaml(path)
    metrics = raw.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError(f"Invalid metrics YAML: 'metrics' must be a mapping, got {type(metrics).__name__}")
    # Materialise descriptions and SQL strings, preserving thresholds.
    return {
        name: {
            "description": definition.get("description", ""),
            "sql": definition.get("sql", "").strip(),
            "thresholds": definition.get("thresholds", []),
        }
        for name, definition in metrics.items()
        if isinstance(definition, dict)
    }


def _execute_sql(conn: Any, sql: str) -> float | None:
    """Execute a SQL query that returns a single scalar and return it."""
    result = conn.execute(sql).fetchone()
    if result is None or result[0] is None:
        return None
    return float(result[0])


def _execute_with_ibis(conn: Any, sql: str) -> float | None:
    """Best-effort Ibis-backed execution. Falls back to raw SQL on any issue."""
    try:
        import ibis  # type: ignore[import-untyped]

        if not hasattr(ibis, "duckdb"):
            raise ImportError("Ibis DuckDB backend not available")

        # Ibis can wrap an existing DuckDB connection.
        con = ibis.duckdb.connect() if conn is None else ibis.duckdb.from_connection(conn)
        expr = con.sql(sql)
        df = expr.execute()
        if df.empty or df.shape[1] == 0:
            return None
        value = df.iloc[0, 0]
        if value is None:
            return None
        return float(value)
    except Exception as exc:  # pragma: no cover - Ibis is optional
        logger.debug("Ibis execution failed (%s), falling back to plain SQL", exc)
        return _execute_sql(conn, sql)


def execute_metric(conn: Any, metric: dict[str, Any], use_ibis: bool = True) -> float | None:
    """Run a single metric definition against a DuckDB connection.

    Args:
        conn: DuckDB connection.
        metric: Metric definition containing 'sql'.
        use_ibis: Whether to attempt Ibis execution. Defaults to True.

    Returns:
        Scalar metric value, or None if the metric SQL returns no rows / NULL.
    """
    sql = metric.get("sql", "").strip()
    if not sql:
        raise ValueError("Metric definition missing 'sql'")
    if use_ibis and os.environ.get("OHM_SEMANTIC_LAYER_IBIS", "1") != "0":
        return _execute_with_ibis(conn, sql)
    return _execute_sql(conn, sql)


def run_metrics(
    conn: Any,
    path: Path | str | None = None,
    use_ibis: bool = True,
) -> dict[str, float | None]:
    """Execute all metrics defined in the YAML file.

    Args:
        conn: DuckDB connection with OHM schema initialised.
        path: Optional override for the metrics YAML file.
        use_ibis: Whether to attempt Ibis execution. Defaults to True.

    Returns:
        Dict mapping metric name to computed scalar value.
    """
    metrics = load_metrics(path)
    results: dict[str, float | None] = {}
    for name, definition in metrics.items():
        try:
            results[name] = execute_metric(conn, definition, use_ibis=use_ibis)
        except Exception as exc:
            logger.warning("Metric %s failed: %s", name, exc)
            results[name] = None
    return results


def run_metrics_and_actions(
    conn: Any,
    repo_path: str | None = None,
    path: Path | str | None = None,
    use_ibis: bool = True,
    execute: bool = True,
    rate_limit_window_seconds: float = 24 * 60 * 60,
) -> dict[str, Any]:
    """Run all metrics, evaluate thresholds, and optionally execute actions.

    Args:
        conn: DuckDB connection with OHM schema initialised.
        repo_path: Optional Beads repo path for `create_task` actions.
        path: Optional override for the metrics YAML file.
        use_ibis: Whether to attempt Ibis execution. Defaults to True.
        execute: If True, run actions; if False, only evaluate and list them.
        rate_limit_window_seconds: Minimum seconds between creating the same
            (metric, threshold, action_type) task. Defaults to 24 hours.

    Returns:
        Dict with 'metrics', 'actions', and 'executed' (when execute=True).
    """
    from ohm.semantic_layer.actions import evaluate_thresholds, run_actions

    metrics = load_metrics(path)
    metric_values = run_metrics(conn, path=path, use_ibis=use_ibis)
    actions = evaluate_thresholds(metric_values, metrics)
    result: dict[str, Any] = {"metrics": metric_values, "actions": actions}
    if execute:
        repo = repo_path or "/root/olympus/OHM"
        executed = run_actions(conn, repo_path=repo, actions=actions, rate_limit_window_seconds=rate_limit_window_seconds)
        result["executed"] = executed
    return result


def list_metrics(path: Path | str | None = None) -> dict[str, str]:
    """Return metric names and descriptions without executing them."""
    metrics = load_metrics(path)
    return {name: definition.get("description", "") for name, definition in metrics.items()}
