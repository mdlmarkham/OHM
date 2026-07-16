"""Forecast registry queries: create, list, get, transition, resolve (OHM-941 / Stage 4)."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def create_forecast(
    conn: DuckDBPyConnection,
    *,
    forecast_id: str | None = None,
    label: str,
    target_node_id: str,
    horizon: str,
    predicted_value: float | None = None,
    predicted_unit: str | None = None,
    distribution: dict[str, float] | None = None,
    assumptions: list[str] | None = None,
    model_id: str | None = None,
    created_by: str,
    connects_to: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a forecast node with FORECAST_FOR edge to target.

    Returns the created node dict.
    """
    from ohm.graph.queries import create_edge, create_node

    if not label:
        raise ValueError("label is required")
    if not target_node_id:
        raise ValueError("target_node_id is required")
    if not horizon:
        raise ValueError("horizon is required")

    node_metadata: dict[str, Any] = {
        "horizon": horizon,
        "predicted_value": predicted_value,
        "predicted_unit": predicted_unit,
                "distribution": distribution,
        "assumptions": assumptions,
        "model_id": model_id,
    }
    if metadata:
        node_metadata.update(metadata)

    links = list(set((connects_to or []) + [target_node_id]))

    node = create_node(
        conn,
        label=label,
        node_type="forecast",
        created_by=created_by,
        metadata=node_metadata,
        connects_to=links,
    )

    create_edge(
        conn,
        from_node=node["id"],
        to_node=target_node_id,
        layer="L3",
        edge_type="FORECAST_FOR",
        created_by=created_by,
        metadata={"horizon": horizon, "predicted_value": predicted_value},
    )

    return node


def list_forecasts(
    conn: DuckDBPyConnection,
    *,
    target_node_id: str | None = None,
    horizon: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List forecast nodes with optional filters."""
    query = "SELECT * FROM ohm_nodes WHERE type = 'forecast' AND deleted_at IS NULL"
    params: list[Any] = []
    if target_node_id:
        query += " AND id IN (SELECT from_node FROM ohm_edges WHERE to_node = ? AND edge_type = 'FORECAST_FOR')"
        params.append(target_node_id)
    if status:
        query += " AND task_status = ?"
        params.append(status)
    if created_by:
        query += " AND created_by = ?"
        params.append(created_by)
    if horizon:
        query += " AND json_extract_string(metadata, '$.horizon') = ?"
        params.append(horizon)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    from ohm.graph.queries._shared import _rows_to_dicts

    return _rows_to_dicts(conn.execute(query, params))


def get_forecast(
    conn: DuckDBPyConnection,
    forecast_id: str,
) -> dict[str, Any] | None:
    """Get a full forecast: node + FORECAST_FOR target + latest accuracy observation."""
    from ohm.graph.queries._shared import _rows_to_dicts

    rows = _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND type = 'forecast'", [forecast_id])
    )
    if not rows:
        return None

    forecast = rows[0]

    target_rows = _rows_to_dicts(
        conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'FORECAST_FOR' LIMIT 1",
            [forecast_id],
        )
    )
    if target_rows:
        forecast["target_node_id"] = target_rows[0]["to_node"]

    obs_rows = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND type = 'experiment_result' "
            "ORDER BY created_at DESC LIMIT 1",
            [forecast_id],
        )
    )
    if obs_rows:
        forecast["latest_accuracy"] = obs_rows[0]

    return forecast


def transition_forecast(
    conn: DuckDBPyConnection,
    *,
    forecast_id: str,
    new_status: str,
    created_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Transition a forecast through its lifecycle.

    Legal transitions:
        draft -> committed -> active -> resolved_hit|resolved_miss|resolved_ambiguous -> superseded
    """
    from ohm.exceptions import ValidationError
    from ohm.graph.queries._shared import _rows_to_dicts

    legal = {
        "draft": {"committed"},
        "committed": {"active"},
        "active": {"resolved_hit", "resolved_miss", "resolved_ambiguous", "superseded"},
        "resolved_hit": {"superseded"},
        "resolved_miss": {"superseded"},
        "resolved_ambiguous": {"superseded"},
    }

    rows = _rows_to_dicts(
        conn.execute("SELECT task_status FROM ohm_nodes WHERE id = ?", [forecast_id])
    )
    if not rows:
        raise ValidationError(f"Forecast {forecast_id} not found")

    current = rows[0].get("task_status") or "draft"
    if new_status not in legal.get(current, set()):
        raise ValidationError(f"Illegal transition: {current} -> {new_status}")

    conn.execute(
        "UPDATE ohm_nodes SET task_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [new_status, forecast_id],
    )

    return {"forecast_id": forecast_id, "previous_status": current, "new_status": new_status}


def resolve_forecast(
    conn: DuckDBPyConnection,
    *,
    forecast_id: str,
    actual_value: float,
    created_by: str,
) -> dict[str, Any]:
    """Compute error metrics and resolve the forecast.

    Writes an experiment_result observation and transitions the forecast status.
    """
    from ohm.graph.queries import create_observation
    from ohm.temporal.forecast_accuracy import compute_accuracy

    forecast = get_forecast(conn, forecast_id)
    if not forecast:
        raise ValueError(f"Forecast {forecast_id} not found")

    meta = forecast.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    predicted_value = meta.get("predicted_value")
    distribution = meta.get("distribution")

    accuracy = compute_accuracy(
        predicted_value=predicted_value,
        actual_value=actual_value,
        distribution=distribution,
    )

    obs = create_observation(
        conn,
        node_id=forecast_id,
        obs_type="experiment_result",
        created_by=created_by,
        value=actual_value,
        metadata=accuracy,
    )

    error = accuracy.get("error")
    if error is not None:
        if predicted_value is not None:
            tolerance = abs(predicted_value) * 0.15 if predicted_value != 0 else 1.0
            if abs(error) <= tolerance:
                new_status = "resolved_hit"
            else:
                new_status = "resolved_miss"
        else:
            new_status = "resolved_ambiguous"
    else:
        new_status = "resolved_ambiguous"

    try:
        transition_forecast(conn, forecast_id=forecast_id, new_status=new_status, created_by=created_by)
    except Exception:
        pass

    return {
        "forecast_id": forecast_id,
        "actual_value": actual_value,
        "accuracy": accuracy,
        "status": new_status,
        "observation_id": obs.get("id"),
    }