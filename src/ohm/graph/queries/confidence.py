"""Confidence change log and decay queries (OHM-447 Phase 4).

Contains the confidence change log API (log_confidence_change, recompute,
history) and the decay application functions (apply_confidence_decay,
compute_confidence_with_decay, apply_decay_to_edges).

These functions use the decay machinery from ohm.graph.decay (confidence_at,
chain_validity, decay_profile) and the append-only ohm_confidence_log table
(OHM-733).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

    from duckdb import DuckDBPyConnection

    from ohm.graph.queries import query_stale_edges

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


def apply_confidence_decay(
    conn: DuckDBPyConnection,
    *,
    stale_threshold: float = 0.1,
    layer: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply confidence decay to stale edges.

    Reads effective confidence using the decay formula, then updates the
    stored confidence for edges whose effective_confidence < stale_threshold.

    L1/L2 edges are never decayed (permanent).
    L3 edges decay with 90-day half-life.
    L4 edges decay with 30-day half-life.

    effective_confidence = confidence * 0.5 ^ (age_days / half_life)

    Args:
        stale_threshold: Effective confidence below this is decayed (default 0.1).
        layer: If set, only decay edges in this layer.
        dry_run: If True, compute decay but don't update database.

    Returns:
        dict with 'updated' (int), 'skipped' (int), and 'decayed' (list of dicts).
    """
    # Get stale edges (reuse existing logic)
    stale = query_stale_edges(conn, stale_threshold=stale_threshold)

    # Filter by layer if specified
    if layer:
        stale = [e for e in stale if e.get("layer") == layer]

    decayed = []
    skipped = 0
    updated = 0

    for edge in stale:
        # L1/L2 never decay (but they won't appear in stale due to infinite half-life)
        if edge.get("layer") in ("L1", "L2"):
            skipped += 1
            continue

        original_conf = edge.get("confidence", 1.0) or 1.0
        effective_conf = edge.get("effective_confidence", original_conf)

        # Compute what the new confidence should be
        # effective = original * decay_factor, so decay_factor = effective / original
        if original_conf > 0:
            decay_factor = effective_conf / original_conf
            new_confidence = round(effective_conf, 4)
        else:
            continue

        decayed.append(
            {
                "id": edge["id"],
                "confidence": original_conf,
                "new_confidence": new_confidence,
                "decay_factor": round(decay_factor, 4),
                "age_days": edge.get("age_days", 0),
                "layer": edge.get("layer"),
                "edge_type": edge.get("edge_type"),
            }
        )

        if not dry_run:
            # OHM-733: append to confidence log instead of direct UPDATE
            log_confidence_change(
                conn,
                edge_id=edge["id"],
                agent="system",
                old_value=original_conf,
                new_value=new_confidence,
                reason="decay",
            )
            _log_change(conn, "ohm_edges", edge["id"], "UPDATE", "decay")
            updated += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "decayed": decayed,
    }


def compute_confidence_with_decay(
    conn: DuckDBPyConnection,
    *,
    base_confidence: float,
    last_observed_at: datetime | str | None,
    half_life_days: float = 30.0,
    floor: float | None = 0.1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute decayed confidence based on observation age (OHM-2x2u).

    Decay model: confidence(t) = base * 2^(-age_days / half_life), optionally
    floored from below by ``floor`` (default 0.1). Pass ``floor=None`` to
    disable the floor — useful when the input is an unbounded quality score
    (e.g., model composite_score, which can be negative when error metrics
    dominate) rather than a confidence in [0, 1]. When the floor is
    disabled, ``is_stale`` is always False because there is no defined
    staleness threshold.

    Time source: by default we read ``now`` from DuckDB (CURRENT_TIMESTAMP)
    so both timestamps share the same clock + timezone. The fallback to
    ``datetime.now(timezone.utc)`` is only used when the caller passes
    ``now`` explicitly or the DB read fails.
    """
    from datetime import datetime as _dt, timezone as _tz

    if last_observed_at is None:
        return {
            "decayed_confidence": base_confidence,
            "age_days": None,
            "decay_factor": 1.0,
            "is_stale": False,
        }

    if now is None:
        # Read "now" from DuckDB so it shares the same timezone as
        # CURRENT_TIMESTAMP used by create_node's default values. Without
        # this, naive datetimes from the DB are interpreted as UTC while
        # CURRENT_TIMESTAMP carries the session TZ (e.g. EDT), causing
        # spurious ~4h "staleness" on fresh writes.
        try:
            now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        except Exception:
            now = _dt.now(_tz.utc)

    if isinstance(last_observed_at, str):
        last_observed_at = _dt.fromisoformat(last_observed_at.replace("Z", "+00:00"))
    # If the timestamp is naive, attach the same tzinfo as `now` so the
    # subtraction is TZ-correct. If `now` is also naive, treat both as UTC.
    if last_observed_at.tzinfo is None:
        ref_tz = now.tzinfo if now.tzinfo is not None else _tz.utc
        last_observed_at = last_observed_at.replace(tzinfo=ref_tz)
    if now.tzinfo is None and last_observed_at.tzinfo is not None:
        now = now.replace(tzinfo=last_observed_at.tzinfo)

    age_seconds = max(0.0, (now - last_observed_at).total_seconds())
    age_days = age_seconds / 86400.0

    if half_life_days <= 0:
        decay_factor = 1.0
    else:
        decay_factor = 2.0 ** (-age_days / half_life_days)

    raw = base_confidence * decay_factor
    if floor is not None:
        # Explicit clamp: when raw drops below floor, snap to floor exactly
        # (avoids floating-point underflow like 4.4e-16 being treated as
        # non-stale). is_stale is True iff raw was clamped (decayed == floor).
        if raw < floor:
            decayed = floor
            is_stale = True
        else:
            decayed = raw
            is_stale = False
    else:
        decayed = raw
        is_stale = False

    return {
        "decayed_confidence": round(decayed, 6),
        "age_days": round(age_days, 4),
        "decay_factor": round(decay_factor, 6),
        "is_stale": is_stale,
    }


def apply_decay_to_edges(
    conn: DuckDBPyConnection,
    *,
    half_life_days: float = 30.0,
    floor: float = 0.1,
    dry_run: bool = True,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Apply decay to all edges' effective confidence based on their last observation (OHM-2x2u).

    If dry_run=True (default), returns what would change without modifying.
    If dry_run=False, UPDATE ohm_edges SET confidence = decayed_value and store
    original confidence in metadata.confidence_original.
    """
    import json

    defaults = {"L1": float("inf"), "L2": float("inf"), "L3": 90.0, "L4": 30.0}
    when_clauses = " ".join(f"WHEN '{k}' THEN {999999.0 if v == float('inf') or v <= 0 else float(v)}" for k, v in defaults.items())
    hl_case = f"CASE layer {when_clauses} ELSE 90.0 END"

    rows = conn.execute(
        f"""
        SELECT
            id, confidence, layer, created_at, metadata,
            {hl_case} AS half_life,
            GREATEST(date_diff('second', created_at, CURRENT_TIMESTAMP) / 86400.0, 0.0) AS age_days
        FROM ohm_edges
        WHERE deleted_at IS NULL
          AND confidence IS NOT NULL
          AND layer IN ('L3', 'L4')
        """,
    ).fetchall()

    edges_examined = len(rows)
    edges_decayed = 0
    summary: list[dict[str, Any]] = []
    total_decay_factor = 0.0

    for row in rows:
        edge_id, original_conf, layer, created_at, metadata_json, hl, age = row
        hl = float(hl)
        age = float(age)
        original_conf = float(original_conf) if original_conf is not None else original_conf
        if original_conf is None or original_conf <= 0:
            continue

        if hl <= 0 or hl >= 999999:
            continue

        decay_factor = 2.0 ** (-age / hl)
        decayed_conf = max(floor, original_conf * decay_factor)

        if decayed_conf < original_conf:
            edges_decayed += 1
            total_decay_factor += decay_factor

            entry = {
                "id": edge_id,
                "layer": layer,
                "original_confidence": round(original_conf, 6),
                "decayed_confidence": round(decayed_conf, 6),
                "decay_factor": round(decay_factor, 6),
                "age_days": round(age, 4),
            }
            summary.append(entry)

            if not dry_run:
                existing_meta = {}
                if metadata_json:
                    try:
                        existing_meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing_meta["confidence_original"] = original_conf
                meta_str = json.dumps(existing_meta)

                conn.execute(
                    "UPDATE ohm_edges SET metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [meta_str, edge_id],
                )
                # OHM-733: append to confidence log instead of direct UPDATE
                log_confidence_change(
                    conn,
                    edge_id=edge_id,
                    agent=created_by or "decay",
                    old_value=original_conf,
                    new_value=round(decayed_conf, 6),
                    reason="decay",
                )
                agent = created_by or "decay"
                _log_change(conn, "ohm_edges", edge_id, "UPDATE", agent)

    avg_decay = round(total_decay_factor / edges_decayed, 6) if edges_decayed > 0 else 1.0

    return {
        "edges_examined": edges_examined,
        "edges_decayed": edges_decayed,
        "average_decay_factor": avg_decay,
        "summary": summary[:100],
    }


# OHM-447: Lazy cross-domain imports resolved at access time
_LAZY_IMPORTS = {
    "create_node",
    "create_edge",
}


def log_confidence_change(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    agent: str,
    new_value: float,
    reason: str,
    old_value: float | None = None,
    challenge_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a row to the confidence change log (OHM-733).

    This is the single entry point for recording a confidence-affecting
    event on an edge. The log is append-only — every change is attributed
    to an agent with a reason. ``ohm_edges.confidence`` is refreshed from
    the log via :func:`recompute_confidence_from_log` (idempotent).
    """
    import json as _json
    import uuid as _uuid

    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    log_id = str(_uuid.uuid4())
    meta_json = _json.dumps(metadata) if metadata else None

    conn.execute(
        """INSERT INTO ohm_confidence_log
           (id, edge_id, agent, old_value, new_value, reason, challenge_id, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [log_id, edge_id, agent, old_value, new_value, reason, challenge_id, meta_json],
    )

    # Refresh the cached column from the log (idempotent recompute)
    conn.execute(
        "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
        [new_value, edge_id],
    )

    return {
        "id": log_id,
        "edge_id": edge_id,
        "agent": agent,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason,
    }


def recompute_confidence_from_log(
    conn: DuckDBPyConnection,
    edge_id: str,
) -> float | None:
    """Recompute an edge's confidence from the append-only log (OHM-733).

    Takes the ``new_value`` from the most recent log row for this edge
    and writes it to ``ohm_edges.confidence``. Idempotent — safe to call
    from multiple daemons concurrently; the result is the same regardless
    of ordering because "most recent by created_at" is deterministic.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        """SELECT new_value FROM ohm_confidence_log
           WHERE edge_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        [edge_id],
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    conn.execute(
        "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
        [value, edge_id],
    )
    return value


def get_confidence_history(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return the full confidence change history for an edge (OHM-733)."""
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    result = conn.execute(
        """SELECT id, edge_id, agent, old_value, new_value, reason,
                  challenge_id, created_at
           FROM ohm_confidence_log
           WHERE edge_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        [edge_id, limit],
    )
    return _rows_to_dicts(result)
