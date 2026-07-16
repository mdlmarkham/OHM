"""Plans-vs-actuals reconciliation loop (OHM-940 / Stage 3).

Compare planned temporal artifacts against actual events/observations and
surface drift as first-class observations with ``DRIFT_FROM`` edges.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def reconcile_plan_actuals(
    conn: DuckDBPyConnection,
    plan_id: str | None = None,
    *,
    horizon: str | None = None,
    dry_run: bool = False,
    tolerance: dict[str, float] | None = None,
    created_by: str = "system",
) -> dict[str, Any]:
    """Compare plan/prospect/forecast to actuals and return/write drift records.

    Args:
        conn: DuckDB connection (must have TOPO schema tables).
        plan_id: Optional plan ID to reconcile. If None, all active plans are checked.
        horizon: Optional horizon filter.
        dry_run: If True, return drift records without writing observations or edges.
        tolerance: Optional dict overriding default tolerances:
            - ``timing_seconds``: max timing drift (default 3600 = 1 hour)
            - ``value``: max value drift (default 0.15 = 15%)
            - ``duration_seconds``: max duration drift (default 7200 = 2 hours)
        created_by: Agent name for drift observation attribution.

    Returns:
        Dict with ``plan_id``, ``drifts`` (list of drift dicts), ``count``, ``dry_run``.
    """
    from ohm.graph.queries.plans_events import get_plan, get_events_for_plan, list_plans
    from ohm.graph.queries import create_observation, create_edge, create_node

    tol = {
        "timing_seconds": 3600.0,
        "value": 0.15,
        "duration_seconds": 7200.0,
    }
    if tolerance:
        tol.update(tolerance)

    if plan_id:
        plan = get_plan(conn, plan_id)
        if not plan:
            return {"plan_id": plan_id, "drifts": [], "count": 0, "dry_run": dry_run}
        plans = [plan]
    else:
        plans = list_plans(conn, status="active", horizon=horizon)

    all_drifts: list[dict[str, Any]] = []

    for plan in plans:
        pid = plan["id"]
        plan_start = plan.get("start_ts")
        plan_end = plan.get("end_ts")

        actual_events = get_events_for_plan(conn, pid)

        if not actual_events:
            drift = {
                "drift_type": "missing_event",
                "plan_id": pid,
                "planned_value": plan_start,
                "actual_value": None,
                "tolerance": tol["timing_seconds"],
                "delta": None,
                "severity": "high",
                "expected_event_class": None,
                "actual_event_id": None,
                "window_start": plan_start,
                "window_end": plan_end,
            }
            all_drifts.append(drift)
            if not dry_run:
                _write_drift_observation(conn, drift, created_by)
            continue

        for event in actual_events:
            event_start = event.get("start_ts")
            event_end = event.get("end_ts")
            event_class = event.get("event_class")

            if plan_start and event_start:
                try:
                    ps = _parse_ts(plan_start)
                    es = _parse_ts(event_start)
                    timing_delta = abs((es - ps).total_seconds())
                    if timing_delta > tol["timing_seconds"]:
                        severity = "high" if timing_delta > 2 * tol["timing_seconds"] else "medium"
                        drift = {
                            "drift_type": "timing_drift",
                            "plan_id": pid,
                            "planned_value": plan_start,
                            "actual_value": event_start,
                            "tolerance": tol["timing_seconds"],
                            "delta": timing_delta,
                            "severity": severity,
                            "expected_event_class": event_class,
                            "actual_event_id": event.get("id"),
                            "window_start": plan_start,
                            "window_end": plan_end,
                        }
                        all_drifts.append(drift)
                        if not dry_run:
                            _write_drift_observation(conn, drift, created_by)
                except (ValueError, TypeError):
                    pass

            if plan_end and event_end and plan_start and event_start:
                try:
                    planned_duration = (_parse_ts(plan_end) - _parse_ts(plan_start)).total_seconds()
                    actual_duration = (_parse_ts(event_end) - _parse_ts(event_start)).total_seconds()
                    duration_delta = abs(planned_duration - actual_duration)
                    if duration_delta > tol["duration_seconds"]:
                        severity = "high" if duration_delta > 2 * tol["duration_seconds"] else "medium"
                        drift = {
                            "drift_type": "duration_drift",
                            "plan_id": pid,
                            "planned_value": planned_duration,
                            "actual_value": actual_duration,
                            "tolerance": tol["duration_seconds"],
                            "delta": duration_delta,
                            "severity": severity,
                            "expected_event_class": event_class,
                            "actual_event_id": event.get("id"),
                            "window_start": plan_start,
                            "window_end": plan_end,
                        }
                        all_drifts.append(drift)
                        if not dry_run:
                            _write_drift_observation(conn, drift, created_by)
                except (ValueError, TypeError):
                    pass

    return {
        "plan_id": plan_id,
        "drifts": all_drifts,
        "count": len(all_drifts),
        "dry_run": dry_run,
    }


def list_drifts(
    conn: DuckDBPyConnection,
    *,
    plan_id: str | None = None,
    severity: str | None = None,
    drift_type: str | None = None,
    horizon: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List drift observations from the observation store.

    Drift observations are identified by ``obs_type = 'anomaly'`` with
    ``metadata.drift_type`` set.
    """
    query = (
        "SELECT * FROM ohm_observations WHERE type = 'anomaly' "
        "AND metadata IS NOT NULL "
        "AND json_extract_string(metadata, 'drift_type') IS NOT NULL"
    )
    params: list[Any] = []
    if plan_id:
        query += " AND json_extract_string(metadata, 'plan_id') = ?"
        params.append(plan_id)
    if severity:
        query += " AND json_extract_string(metadata, 'severity') = ?"
        params.append(severity)
    if drift_type:
        query += " AND json_extract_string(metadata, 'drift_type') = ?"
        params.append(drift_type)
    query += " ORDER BY valid_from DESC LIMIT ?"
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


def explain_drift(
    conn: DuckDBPyConnection,
    drift_observation_id: str,
    *,
    top: int = 10,
) -> dict[str, Any]:
    """Run VoI analysis to explain which assumptions/edges would change the plan.

    Uses ``compute_voi`` from the inference layer to rank which observations
    would most reduce uncertainty about the drifted plan.
    """
    from ohm.inference.bayesian import compute_voi

    obs_rows = conn.execute(
        "SELECT * FROM ohm_observations WHERE id = ?", [drift_observation_id]
    ).fetchall()
    if not obs_rows:
        return {"error": "drift observation not found", "drift_observation_id": drift_observation_id}

    from ohm.graph.queries._shared import _rows_to_dicts

    obs = _rows_to_dicts(obs_rows)[0]
    meta = obs.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}

    plan_id = meta.get("plan_id")
    node_id = obs.get("node_id")

    if not node_id:
        return {
            "drift_observation_id": drift_observation_id,
            "plan_id": plan_id,
            "voi": {"rankings": [], "n_candidates": 0},
            "message": "No target node for drift observation; cannot run VoI.",
        }

    voi_result = compute_voi(conn, decision_nodes=[node_id], top=top)

    return {
        "drift_observation_id": drift_observation_id,
        "plan_id": plan_id,
        "node_id": node_id,
        "drift_type": meta.get("drift_type"),
        "severity": meta.get("severity"),
        "voi": voi_result,
    }


def _write_drift_observation(
    conn: DuckDBPyConnection,
    drift: dict[str, Any],
    created_by: str,
) -> None:
    """Write a drift observation and create a DRIFT_FROM edge to the plan node."""
    from ohm.graph.queries import create_observation, create_edge, create_node

    plan_id = drift.get("plan_id")
    if not plan_id:
        return

    plan_node_id = None
    try:
        from ohm.graph.queries.plans_events import get_plan

        plan = get_plan(conn, plan_id)
        if plan:
            plan_node_id = plan.get("node_id")
    except Exception:
        pass

    target_node_id = plan_node_id or plan_id

    idempotency_key = f"drift_{plan_id}_{drift['drift_type']}_{drift.get('actual_event_id', 'none')}"

    existing = conn.execute(
        "SELECT COUNT(*) FROM ohm_observations WHERE idempotency_key = ?",
        [idempotency_key],
    ).fetchone()
    if existing and existing[0] > 0:
        return

    try:
        clean_drift = {k: (str(v) if hasattr(v, 'isoformat') else v) for k, v in drift.items()}
        obs = create_observation(
            conn,
            node_id=target_node_id,
            obs_type="anomaly",
            created_by=created_by,
            notes=f"Drift detected: {drift['drift_type']} on plan {plan_id}",
            metadata={
                **clean_drift,
                "idempotency_key": idempotency_key,
            },
        )

        if plan_node_id and obs:
            try:
                create_edge(
                    conn,
                    from_node=obs.get("node_id", target_node_id),
                    to_node=plan_node_id,
                    layer="L3",
                    edge_type="DRIFT_FROM",
                    created_by=created_by,
                    metadata={"drift_type": drift["drift_type"], "severity": drift["severity"]},
                )
            except Exception:
                pass
    except Exception:
        pass
    except Exception:
        pass


def _parse_ts(ts: str) -> datetime:
    """Parse a timestamp string into a timezone-aware datetime."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts[:26 if len(ts) > 26 else len(ts)], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts}")