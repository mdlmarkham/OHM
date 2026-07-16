"""Scenario persistence queries: list, get, rerun, diff (OHM-942 / Stage 5)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def get_scenario(
    conn: DuckDBPyConnection,
    scenario_id: str,
) -> dict[str, Any] | None:
    """Get a scenario node with its metadata and SCENARIO_FOR target."""
    from ohm.graph.queries._shared import _rows_to_dicts

    rows = _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND type = 'scenario'", [scenario_id])
    )
    if not rows:
        return None

    scenario = rows[0]
    meta = scenario.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    scenario["metadata"] = meta

    target_rows = _rows_to_dicts(
        conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'SCENARIO_FOR' LIMIT 1",
            [scenario_id],
        )
    )
    if target_rows:
        scenario["target_node_id"] = target_rows[0]["to_node"]

    obs_rows = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND type = 'experiment_result' "
            "ORDER BY created_at DESC LIMIT 1",
            [scenario_id],
        )
    )
    if obs_rows:
        scenario["latest_result"] = obs_rows[0]

    return scenario


def rerun_scenario(
    conn: DuckDBPyConnection,
    scenario_id: str,
    *,
    created_by: str = "system",
) -> dict[str, Any]:
    """Re-run a saved scenario against current graph state and return deltas."""
    from ohm.graph.queries.cascade_scenario import query_compare_scenarios
    from ohm.graph.queries import create_observation

    scenario = get_scenario(conn, scenario_id)
    if not scenario:
        raise ValueError(f"Scenario {scenario_id} not found")

    meta = scenario.get("metadata", {})
    target_node_id = scenario.get("target_node_id")
    if not target_node_id:
        raise ValueError("Scenario has no target node")

    new_result = query_compare_scenarios(
        conn,
        target_node_id,
        failure_probability=meta.get("failure_probability", 1.0),
        max_depth=meta.get("max_depth", 10),
        edge_overrides=meta.get("edge_overrides"),
        node_interventions=meta.get("node_interventions"),
        disabled_edges=set(meta.get("disabled_edges", [])),
        disabled_nodes=set(meta.get("disabled_nodes", [])),
    )

    old_summary = meta.get("result_summary", {})
    new_summary = new_result.get("summary", {})

    deltas = {
        "total_nodes_change": new_summary.get("total_nodes", 0) - old_summary.get("total_nodes", 0),
        "increased_change": new_summary.get("increased", 0) - old_summary.get("increased", 0),
        "decreased_change": new_summary.get("decreased", 0) - old_summary.get("decreased", 0),
        "unchanged_change": new_summary.get("unchanged", 0) - old_summary.get("unchanged", 0),
    }

    create_observation(
        conn,
        node_id=scenario_id,
        obs_type="experiment_result",
        created_by=created_by,
        metadata={"rerun": True, "new_summary": new_summary, "deltas": deltas},
    )

    return {
        "scenario_id": scenario_id,
        "new_result": new_result,
        "deltas": deltas,
    }


def diff_scenario(
    conn: DuckDBPyConnection,
    scenario_id: str,
) -> dict[str, Any]:
    """Compare a scenario snapshot to current graph state."""
    scenario = get_scenario(conn, scenario_id)
    if not scenario:
        raise ValueError(f"Scenario {scenario_id} not found")

    meta = scenario.get("metadata", {})
    target_node_id = scenario.get("target_node_id")

    result = {
        "scenario_id": scenario_id,
        "target_node_id": target_node_id,
        "edge_overrides": meta.get("edge_overrides", {}),
        "node_interventions": meta.get("node_interventions", {}),
        "disabled_edges": meta.get("disabled_edges", []),
        "disabled_nodes": meta.get("disabled_nodes", []),
    }

    return result