"""DataProductRun queries (OHM-447 domain 28)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


def create_run(
    conn: "DuckDBPyConnection",
    *,
    run_id: str,
    report_id: str | None = None,
    node_id: str | None = None,
    run_type: str,
    inputs: dict | None = None,
    status: str = "pending",
    created_by: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Insert a new DataProductRun into topo_runs (OHM-08uk)."""
    import json

    from ohm.validation import validate_identifier

    run_id = validate_identifier(run_id, name="run_id")
    if not run_type:
        raise ValueError("run_type must be non-empty")
    if report_id is not None:
        report_id = validate_identifier(report_id, name="report_id")
    if node_id is not None:
        node_id = validate_identifier(node_id, name="node_id")

    inputs_json = json.dumps(inputs) if inputs else None
    meta_json = json.dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO topo_runs
           (id, report_id, node_id, run_type, status, inputs, created_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [run_id, report_id, node_id, run_type, status, inputs_json, created_by, meta_json],
    )
    _log_change(conn, "topo_runs", run_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM topo_runs WHERE id = ?", [run_id]))[0]


def get_run(
    conn: "DuckDBPyConnection",
    run_id: str,
) -> dict[str, Any] | None:
    """Fetch a single run by id (OHM-08uk)."""
    from ohm.validation import validate_identifier

    run_id = validate_identifier(run_id, name="run_id")
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_runs WHERE id = ?", [run_id]))
    return rows[0] if rows else None


def list_runs(
    conn: "DuckDBPyConnection",
    *,
    report_id: str | None = None,
    node_id: str | None = None,
    run_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List runs with optional filters (OHM-08uk)."""
    query = "SELECT * FROM topo_runs WHERE 1=1"
    params: list[Any] = []
    if report_id is not None:
        query += " AND report_id = ?"
        params.append(report_id)
    if node_id is not None:
        query += " AND node_id = ?"
        params.append(node_id)
    if run_type is not None:
        query += " AND run_type = ?"
        params.append(run_type)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    return _rows_to_dicts(conn.execute(query, params))


def complete_run(
    conn: "DuckDBPyConnection",
    *,
    run_id: str,
    status: str = "completed",
    outputs: dict | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Mark a run as completed (or failed) and record outputs (OHM-08uk)."""
    import json
    from datetime import datetime, timezone
    from ohm.validation import validate_identifier

    run_id = validate_identifier(run_id, name="run_id")
    now = datetime.now(timezone.utc).isoformat()
    outputs_json = json.dumps(outputs) if outputs else None

    conn.execute(
        "UPDATE topo_runs SET status = ?, outputs = ?, error = ?, duration_ms = ?, completed_at = ?::TIMESTAMP WHERE id = ?",
        [status, outputs_json, error, duration_ms, now, run_id],
    )
    _log_change(conn, "topo_runs", run_id, "UPDATE", created_by)
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_runs WHERE id = ?", [run_id]))
    return rows[0] if rows else {}
