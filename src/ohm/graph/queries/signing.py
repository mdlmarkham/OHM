"""signing queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def sign_node_write(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    key: bytes,
    algorithm: str = "hmac-sha256",
    key_id: str = "default",
) -> dict[str, Any]:
    from ohm.exceptions import NodeNotFoundError
    from ohm.graph.crypto import sign_write
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    row = conn.execute(
        "SELECT id, label, type, content, created_by, confidence, visibility, provenance, source_tier FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Node {node_id} not found")
    record = {"id": row[0], "label": row[1], "type": row[2], "content": row[3], "created_by": row[4], "confidence": row[5], "visibility": row[6], "provenance": row[7], "source_tier": row[8]}
    sig_data = sign_write(record, kind="node", key=key, algorithm=algorithm, key_id=key_id)
    conn.execute(
        "UPDATE ohm_nodes SET write_signature = ?, signing_key_id = ?, signed_at = ? WHERE id = ?",
        [sig_data["write_signature"], sig_data["signing_key_id"], sig_data["signed_at"], node_id],
    )
    return {"node_id": node_id, **sig_data}


def sign_edge_write(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    key: bytes,
    algorithm: str = "hmac-sha256",
    key_id: str = "default",
) -> dict[str, Any]:
    from ohm.exceptions import EdgeNotFoundError
    from ohm.graph.crypto import sign_write
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, created_by, confidence, probability, source_tier FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if not row:
        raise EdgeNotFoundError(f"Edge {edge_id} not found")
    record = {"id": row[0], "from_node": row[1], "to_node": row[2], "layer": row[3], "edge_type": row[4], "created_by": row[5], "confidence": row[6], "probability": row[7], "source_tier": row[8]}
    sig_data = sign_write(record, kind="edge", key=key, algorithm=algorithm, key_id=key_id)
    conn.execute(
        "UPDATE ohm_edges SET write_signature = ?, signing_key_id = ?, signed_at = ? WHERE id = ?",
        [sig_data["write_signature"], sig_data["signing_key_id"], sig_data["signed_at"], edge_id],
    )
    return {"edge_id": edge_id, **sig_data}


def verify_node_write(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    key: bytes,
) -> dict[str, Any]:
    from ohm.graph.crypto import verify_write
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    row = conn.execute(
        "SELECT id, label, type, content, created_by, confidence, visibility, provenance, source_tier, write_signature FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not row:
        return {"node_id": node_id, "verified": False, "reason": "not_found"}
    record = {"id": row[0], "label": row[1], "type": row[2], "content": row[3], "created_by": row[4], "confidence": row[5], "visibility": row[6], "provenance": row[7], "source_tier": row[8], "write_signature": row[9]}
    verified = verify_write(record, kind="node", key=key)
    return {"node_id": node_id, "verified": verified}


def verify_edge_write(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    key: bytes,
) -> dict[str, Any]:
    from ohm.graph.crypto import verify_write
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, created_by, confidence, probability, source_tier, write_signature FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if not row:
        return {"edge_id": edge_id, "verified": False, "reason": "not_found"}
    record = {"id": row[0], "from_node": row[1], "to_node": row[2], "layer": row[3], "edge_type": row[4], "created_by": row[5], "confidence": row[6], "probability": row[7], "source_tier": row[8], "write_signature": row[9]}
    verified = verify_write(record, kind="edge", key=key)
    return {"edge_id": edge_id, "verified": verified}
