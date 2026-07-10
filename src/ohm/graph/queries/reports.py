"""reports queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts

def create_report(
    conn: DuckDBPyConnection,
    *,
    report_id: str,
    report_type: str,
    node_id: str | None = None,
    plan_id: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    findings: dict | None = None,
    recommendations: dict | None = None,
    confidence_adjustments: dict | None = None,
    status: str = "draft",
    created_by: str,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Insert a new analytical report into topo_reports (OHM-o3rd)."""
    import json

    from ohm.validation import validate_identifier

    report_id = validate_identifier(report_id, name="report_id")
    if not report_type:
        raise ValueError("report_type must be non-empty")
    if node_id is not None:
        node_id = validate_identifier(node_id, name="node_id")
    if plan_id is not None:
        plan_id = validate_identifier(plan_id, name="plan_id")

    findings_json = json.dumps(findings) if findings else None
    recs_json = json.dumps(recommendations) if recommendations else None
    adj_json = json.dumps(confidence_adjustments) if confidence_adjustments else None
    meta_json = json.dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO topo_reports
           (id, report_type, node_id, plan_id, title, summary, findings,
            recommendations, confidence_adjustments, status, created_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [report_id, report_type, node_id, plan_id, title, summary, findings_json, recs_json, adj_json, status, created_by, meta_json],
    )
    _log_change(conn, "topo_reports", report_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM topo_reports WHERE id = ?", [report_id]))[0]


def get_report(
    conn: DuckDBPyConnection,
    report_id: str,
) -> dict[str, Any] | None:
    """Fetch a single report by id. Returns dict or None."""
    from ohm.validation import validate_identifier

    report_id = validate_identifier(report_id, name="report_id")
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_reports WHERE id = ?", [report_id]))
    return rows[0] if rows else None


def list_reports(
    conn: DuckDBPyConnection,
    *,
    report_type: str | None = None,
    node_id: str | None = None,
    plan_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List reports with optional filters."""
    query = "SELECT * FROM topo_reports WHERE 1=1"
    params: list[Any] = []
    if report_type is not None:
        query += " AND report_type = ?"
        params.append(report_type)
    if node_id is not None:
        query += " AND node_id = ?"
        params.append(node_id)
    if plan_id is not None:
        query += " AND plan_id = ?"
        params.append(plan_id)
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC, version DESC"
    return _rows_to_dicts(conn.execute(query, params))


def finalize_report(
    conn: DuckDBPyConnection,
    *,
    report_id: str,
    confidence_adjustments: dict | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Finalize a report: set status=finalized, stamp finalized_at, and
    optionally apply L3 edge confidence adjustments (OHM-o3rd feedback loop).

    When ``confidence_adjustments`` is provided, it should map edge IDs to
    new confidence values. Each edge is updated in place.
    """
    import json
    from datetime import datetime, timezone
    from ohm.validation import validate_identifier

    report_id = validate_identifier(report_id, name="report_id")
    now = datetime.now(timezone.utc).isoformat()

    adj_json = None
    if confidence_adjustments:
        adj_json = json.dumps(confidence_adjustments)
        for edge_id, new_conf in confidence_adjustments.items():
            try:
                new_conf_f = float(new_conf)
            except (TypeError, ValueError):
                continue
            # OHM-733: append to confidence log instead of direct UPDATE
            from ohm.graph.queries import log_confidence_change

            old_row = conn.execute(
                "SELECT confidence FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
                [edge_id],
            ).fetchone()
            old_val = old_row[0] if old_row else None
            log_confidence_change(
                conn,
                edge_id=edge_id,
                agent="topo_report",
                old_value=old_val,
                new_value=new_conf_f,
                reason="topo_report_adjustment",
                metadata={"report_id": report_id},
            )

    conn.execute(
        "UPDATE topo_reports SET status = 'finalized', finalized_at = ?::TIMESTAMP, confidence_adjustments = COALESCE(?, confidence_adjustments), updated_at = ?::TIMESTAMP WHERE id = ?",
        [now, adj_json, now, report_id],
    )
    _log_change(conn, "topo_reports", report_id, "UPDATE", created_by)
    rows = _rows_to_dicts(conn.execute("SELECT * FROM topo_reports WHERE id = ?", [report_id]))
    return rows[0] if rows else {}


def supersede_report(
    conn: DuckDBPyConnection,
    *,
    old_report_id: str,
    new_report_id: str,
    created_by: str,
) -> None:
    """Mark an old report as superseded by a newer version (OHM-o3rd)."""
    from datetime import datetime, timezone
    from ohm.validation import validate_identifier

    old_report_id = validate_identifier(old_report_id, name="old_report_id")
    new_report_id = validate_identifier(new_report_id, name="new_report_id")
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "UPDATE topo_reports SET status = 'superseded', superseded_by = ?, updated_at = ?::TIMESTAMP WHERE id = ?",
        [new_report_id, now, old_report_id],
    )
    _log_change(conn, "topo_reports", old_report_id, "UPDATE", created_by)
