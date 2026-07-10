"""verification queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile

def detect_verifiable_claims(
    conn: DuckDBPyConnection,
    *,
    agent: str | None = None,
    days_threshold: int = 14,
    confidence_threshold: float = 0.85,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Detect verifiable dated claims that are past their expected date with no outcome recorded.

    Scans edges of type CAUSES, PREDICTS, EXPECTS, EXPECTS_FROM whose metadata
    contains an 'expected_by' or 'window_end' ISO-8601 date that is now past,
    and for which no outcome has been recorded against the from_node (claim node).

    Args:
        conn: Database connection.
        agent: If set, only scan edges created_by this agent.
        days_threshold: Minimum age in days for edges to be considered (default 14).
        confidence_threshold: Minimum confidence to flag (default 0.85).
        limit: Maximum number of results (default 100).

    Returns:
        List of dicts with edge info, claim node info, and expected_by date.
    """
    import json as _json
    from datetime import datetime, timezone

    from ohm.validation import validate_identifier

    if agent is not None:
        agent = validate_identifier(agent, name="agent")

    verifiable_types = ["CAUSES", "PREDICTS", "EXPECTS", "EXPECTS_FROM"]
    placeholders = ",".join(["?"] * len(verifiable_types))

    query = f"""
        SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
               e.created_by, e.created_at, e.metadata,
               fn.label AS from_label, fn.type AS from_type,
               tn.label AS to_label, tn.type AS to_type
        FROM ohm_edges e
        LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
        LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
        WHERE e.deleted_at IS NULL
          AND e.edge_type IN ({placeholders})
          AND e.confidence >= ?
          AND NOT EXISTS (
              SELECT 1 FROM ohm_outcomes oc
              WHERE oc.claim_node = e.from_node
          )
          AND fn.id IS NOT NULL
        ORDER BY e.confidence DESC, e.created_at ASC
        LIMIT ?
    """

    params: list[Any] = verifiable_types + [confidence_threshold, limit]
    if agent is not None:
        query = query.replace(
            "AND fn.id IS NOT NULL",
            "AND fn.id IS NOT NULL\n          AND e.created_by = ?",
        )
        params = verifiable_types + [confidence_threshold, agent, limit]

    rows = conn.execute(query, params).fetchall()

    results = []
    now = datetime.now(timezone.utc)
    for row in rows:
        d = dict(
            zip(
                ["id", "from_node", "to_node", "edge_type", "confidence", "created_by", "created_at", "metadata", "from_label", "from_type", "to_label", "to_type"],
                row,
            )
        )
        meta_raw = d.get("metadata")
        expected_by = None
        if meta_raw:
            try:
                meta = _json.loads(str(meta_raw)) if isinstance(meta_raw, str) else meta_raw
                expected_by = meta.get("expected_by") or meta.get("window_end")
            except (ValueError, TypeError):
                pass
        if not expected_by:
            continue
        try:
            expected_dt = datetime.fromisoformat(str(expected_by).replace("Z", "+00:00"))
            if expected_dt.tzinfo is None:
                expected_dt = expected_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if expected_dt > now:
            continue
        age_days = (now - expected_dt).days
        if age_days < 0:
            continue
        d["expected_by"] = str(expected_by)
        d["days_overdue"] = age_days
        results.append(d)

    return results


def create_verification_nudge(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    created_by: str = "system",
    confidence: float = 0.5,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a NUDGES_FOR_VERIFICATION edge from a nudge task node to the claim node.

    Creates a task node representing the verification nudge, then links it to the
    claim node (from_node of the original edge) via a NUDGES_FOR_VERIFICATION edge
    in L3. Idempotent: if a nudge already exists for the edge, returns the existing one.

    Args:
        conn: Database connection.
        edge_id: The edge whose claim needs verification.
        created_by: Agent creating the nudge.
        confidence: Confidence for the nudge edge (default 0.5).
        reason: Optional reason for the nudge.

    Returns:
        Dict with the created nudge task node and nudge edge.
    """
    import uuid
    import json as _json

    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)

    existing = conn.execute(
        """SELECT id FROM ohm_edges
           WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchone()
    if existing:
        nudge_edge = _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [existing[0]]))[0]
        nudge_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [nudge_edge["from_node"]]))[0] if nudge_edge else {}
        return {"nudge_node": nudge_node, "nudge_edge": nudge_edge}

    target = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, metadata FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    claim_node_id = target[1]
    claim_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [claim_node_id]))
    if not claim_node:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Claim node not found: {claim_node_id}")

    nudge_task_id = str(uuid.uuid4())
    nudge_label = f"Verify: {claim_node[0].get('label', claim_node_id)}"
    nudge_metadata = _json.dumps({"nudge_for_edge": edge_id, "edge_type": target[4], "reason": reason})
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence, visibility, provenance, metadata, created_at, updated_at)
           VALUES (?, ?, 'task', ?, ?, ?, 'team', 'system', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        [nudge_task_id, nudge_label, reason or f"Verification nudge for edge {edge_id}", created_by, 0.5, nudge_metadata],
    )
    _log_change(conn, "ohm_nodes", nudge_task_id, "INSERT", created_by)

    nudge_edge_id = str(uuid.uuid4())
    edge_metadata = _json.dumps({"nudge_for_edge": edge_id, "reason": reason})
    conn.execute(
        """INSERT INTO ohm_edges
             (id, from_node, to_node, layer, edge_type, created_by,
              confidence, condition, challenge_of, metadata)
           VALUES (?, ?, ?, 'L3', 'NUDGES_FOR_VERIFICATION', ?, ?, ?, ?, ?)""",
        [nudge_edge_id, nudge_task_id, claim_node_id, created_by, confidence, reason, edge_id, edge_metadata],
    )
    _log_change(conn, "ohm_edges", nudge_edge_id, "INSERT", created_by)

    nudge_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [nudge_task_id]))[0]
    nudge_edge = _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [nudge_edge_id]))[0]
    return {"nudge_node": nudge_node, "nudge_edge": nudge_edge}


def record_verification_outcome(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    outcome: str,
    recorded_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record a verification outcome for a verifiable claim edge.

    Maps string outcome to boolean and confidence:
    - "true" → outcome=True, confidence=1.0
    - "false" → outcome=False, confidence=0.0
    - "ambiguous" → outcome=True, confidence=0.5
    - "deferred" → no outcome recorded, metadata only

    Also resolves any NUDGES_FOR_VERIFICATION edges linked to this edge by
    setting their resolved metadata.

    Args:
        conn: Database connection.
        edge_id: The edge being verified.
        outcome: One of "true", "false", "ambiguous", "deferred".
        recorded_by: Agent recording the outcome.
        reason: Optional context about the outcome.

    Returns:
        Dict with the outcome record and any nudge resolution info.
    """
    import json as _json

    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    if outcome not in ("true", "false", "ambiguous", "deferred"):
        from ohm.exceptions import ValidationError

        raise ValidationError(f"outcome must be one of 'true', 'false', 'ambiguous', 'deferred', got '{outcome}'")

    target = conn.execute(
        "SELECT id, from_node, edge_type FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    claim_node = target[1]
    result: dict[str, Any] = {"edge_id": edge_id, "outcome": outcome, "recorded_by": recorded_by}

    if outcome == "deferred":
        nudge_edges = conn.execute(
            """SELECT id, metadata FROM ohm_edges
               WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
            [edge_id],
        ).fetchall()
        resolved = []
        for ne_id, ne_meta in nudge_edges:
            meta = {}
            if ne_meta:
                try:
                    meta = _json.loads(str(ne_meta)) if isinstance(ne_meta, str) else ne_meta
                except (ValueError, TypeError):
                    pass
            meta["resolved"] = True
            meta["resolution"] = "deferred"
            meta["resolved_by"] = recorded_by
            conn.execute(
                "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
                [_json.dumps(meta), ne_id],
            )
            resolved.append(ne_id)
        result["nudges_resolved"] = resolved
        result["deferred"] = True
        return result

    bool_outcome = outcome in ("true", "ambiguous")
    confidence_map = {"true": 1.0, "false": 0.0, "ambiguous": 0.5}
    outcome_confidence = confidence_map[outcome]

    outcome_result = query_record_outcome(
        conn,
        source_agent=target[2] or "unknown",
        claim_node=claim_node,
        outcome=bool_outcome,
        recorded_by=recorded_by,
        notes=reason,
    )
    result["outcome_record"] = outcome_result
    result["confidence"] = outcome_confidence

    nudge_edges = conn.execute(
        """SELECT id, metadata FROM ohm_edges
           WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchall()
    resolved = []
    for ne_id, ne_meta in nudge_edges:
        meta = {}
        if ne_meta:
            try:
                meta = _json.loads(str(ne_meta)) if isinstance(ne_meta, str) else ne_meta
            except (ValueError, TypeError):
                pass
        meta["resolved"] = True
        meta["resolution"] = outcome
        meta["resolved_by"] = recorded_by
        conn.execute(
            "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
            [_json.dumps(meta), ne_id],
        )
        resolved.append(ne_id)
    result["nudges_resolved"] = resolved

    return result


def list_pending_verifications(
    conn: DuckDBPyConnection,
    *,
    agent: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List pending NUDGES_FOR_VERIFICATION edges that haven't been resolved.

    A nudge is pending if its metadata does not contain 'resolved': True.

    Args:
        conn: Database connection.
        agent: If set, only list nudges created_by this agent.
        limit: Maximum number of results (default 100).

    Returns:
        List of dicts with nudge edge and associated claim node info.
    """
    import json as _json

    from ohm.validation import validate_identifier

    if agent is not None:
        agent = validate_identifier(agent, name="agent")

    query = """
        SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
               e.created_by, e.created_at, e.metadata, e.challenge_of,
               fn.label AS from_label, fn.type AS from_type,
               tn.label AS to_label, tn.type AS to_type
        FROM ohm_edges e
        LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
        LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
        WHERE e.deleted_at IS NULL
          AND e.edge_type = 'NUDGES_FOR_VERIFICATION'
          AND fn.id IS NOT NULL
    """

    params: list[Any] = []
    if agent is not None:
        query += "\n          AND e.created_by = ?"
        params.append(agent)

    query += "\n        ORDER BY e.created_at ASC\n        LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        d = dict(
            zip(
                ["id", "from_node", "to_node", "edge_type", "confidence", "created_by", "created_at", "metadata", "challenge_of", "from_label", "from_type", "to_label", "to_type"],
                row,
            )
        )
        meta_raw = d.get("metadata")
        if meta_raw:
            try:
                meta = _json.loads(str(meta_raw)) if isinstance(meta_raw, str) else meta_raw
                if meta.get("resolved"):
                    continue
                d["reason"] = meta.get("reason")
                d["nudge_for_edge"] = meta.get("nudge_for_edge")
            except (ValueError, TypeError):
                pass
        results.append(d)

    return results
