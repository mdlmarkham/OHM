"""RUL (Remaining Useful Life) assessment queries (OHM-447 domain 27)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts



def register_rul_assessment(
    conn: "DuckDBPyConnection",
    *,
    equipment_node_id: str,
    rul_days: float,
    risk_class: str,
    model_version: str | None = None,
    site_id: str | None = None,
    node_path: str | None = None,
    metadata: dict | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Store a RUL assessment in topo_prospects and link it via L4 PREDICTS (OHM-q4ku)."""
    import json
    import uuid

    from ohm.validation import validate_identifier, validate_confidence

    equipment_node_id = validate_identifier(equipment_node_id, name="equipment_node_id")
    if rul_days < 0:
        raise ValueError("rul_days must be non-negative")
    if not risk_class:
        raise ValueError("risk_class must be non-empty")

    prospect_id = f"rul_{equipment_node_id}_{uuid.uuid4().hex[:8]}"
    meta = metadata or {}
    if node_path:
        meta["node_path"] = node_path
    meta_json = json.dumps(meta) if meta else None

    conn.execute(
        """INSERT INTO topo_prospects
           (id, equipment_id, site_id, rul_days, risk_class, model_version, created_by, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [prospect_id, equipment_node_id, site_id, rul_days, risk_class, model_version, created_by, meta_json],
    )
    _log_change(conn, "topo_prospects", prospect_id, "INSERT", created_by)

    prospect = _rows_to_dicts(conn.execute("SELECT * FROM topo_prospects WHERE id = ?", [prospect_id]))[0]

    edge_id = None
    node_exists = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [equipment_node_id],
    ).fetchone()
    if node_exists and node_exists[0] > 0:
        edge_id = f"e_rul_{uuid.uuid4().hex[:12]}"
        rul_confidence = max(0.0, min(1.0, 1.0 - (rul_days / 365.0))) if rul_days > 0 else 0.5
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, confidence, created_by, metadata)
               VALUES (?, ?, ?, 'PREDICTS', 'L4', ?, ?, ?)""",
            [edge_id, equipment_node_id, equipment_node_id, rul_confidence, created_by, meta_json],
        )
        _log_change(conn, "ohm_edges", edge_id, "INSERT", created_by)

    return {"prospect": prospect, "edge_id": edge_id}


def get_rul_assessments(
    conn: "DuckDBPyConnection",
    *,
    equipment_node_id: str | None = None,
    risk_class: str | None = None,
    site_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch RUL assessments from topo_prospects with optional filters (OHM-q4ku)."""
    from ohm.validation import validate_identifier

    query = "SELECT * FROM topo_prospects WHERE 1=1"
    params: list[Any] = []
    if equipment_node_id is not None:
        equipment_node_id = validate_identifier(equipment_node_id, name="equipment_node_id")
        query += " AND equipment_id = ?"
        params.append(equipment_node_id)
    if risk_class is not None:
        query += " AND risk_class = ?"
        params.append(risk_class)
    if site_id is not None:
        query += " AND site_id = ?"
        params.append(site_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return _rows_to_dicts(conn.execute(query, params))


