"""Fast batch insert for ohm_nodes and ohm_edges (OHM-aadc).

The original ``batch_create_nodes`` and ``batch_create_edges`` in
``ohm.graph.queries`` called ``create_node`` / ``create_edge`` once
per item, doing N round-trips (each: validate, existence SELECT,
INSERT, _log_change). For 1000 nodes this was ~6.2 seconds; for 1000
edges ~3.1 seconds.

This module provides drop-in fast paths that use ``executemany`` (a
single SQL statement with N parameter sets) for the INSERT and for
the change-feed entries. Validation still happens per-row in Python
so the existing error messages and acceptance criteria are preserved.

The fast path activates only for the common case:
- All node ids are new (no soft-deleted reactivations)
- No ``connects_to`` references (the cross-link pre-check is
  cheap but the bulk path skips the per-row Python loop)

If either condition is false, the caller falls back to the slow
per-row path. The slow path remains the source of truth for
semantics; the fast path is an optimisation, not a rewrite.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# Columns written by create_node() for the INSERT path. Keep in sync
# with ohm.graph.queries.create_node. The order matters: it matches
# the VALUES clause built below.
_NODE_COLS = (
    "id", "label", "type", "content", "created_by", "visibility", "provenance",
    "confidence", "priority", "url",
    "tags", "metadata",
    "utility_scale", "utility_usd_per_day", "utility_currency",
    "current_best_action", "action_alternatives",
    "source_tier",
    "source_author", "source_institution", "data_origin",
)

# Same for create_edge().
_EDGE_COLS = (
    "id", "from_node", "to_node", "layer", "edge_type", "created_by",
    "confidence", "probability", "urgency", "condition", "provenance", "metadata",
    "probability_p05", "probability_p50", "probability_p95",
    "confidence_p05", "confidence_p50", "confidence_p95", "source_tier",
)

# Change-feed columns. Matches ohm.graph.queries._log_change.
_FEED_COLS = ("table_name", "row_id", "operation", "agent_name", "old_data")


def fast_batch_create_nodes(
    conn: "DuckDBPyConnection",
    *,
    nodes: list[dict[str, Any]],
    created_by: str,
) -> list[dict[str, Any]] | None:
    """Fast multi-row INSERT for batch_create_nodes.

    Returns the list of created node records in input order, or
    ``None`` if the fast path does not apply (caller should fall back
    to the slow per-row path).

    Fast-path conditions:
    - No node has ``connects_to`` (would need a bulk existence check
      which is a separate code path; the rare case isn't worth the
      complexity here).
    - No generated node id collides with a soft-deleted row (the
      reactivation path needs a per-row UPDATE).

    Semantics preserved:
    - Per-row validation (label, node_type, confidence, source_tier,
      priority, utility_scale, source_url alias for url)
    - ``generate_node_id`` produces deterministic ids
    - JSON serialization for tags/metadata/action_alternatives
    - One ohm_change_feed entry per node (INSERT, attributed to
      ``created_by``)
    - Alias registration via ``ohm_aliases`` (one row per new node)
    - Returns full node records via SELECT after the INSERT
    """
    if not nodes:
        return []

    from ohm.schema import generate_node_id, validate_node_type, VALID_PRIORITY
    from ohm.validation import (
        validate_confidence,
        validate_data_origin,
        validate_identifier,
        validate_source_tier,
        enforce_confidence_ceiling,
    )

    # --- 1. Pre-validate every entry. Same rules as create_node().
    parsed: list[dict[str, Any]] = []
    for idx, nd in enumerate(nodes):
        label = nd.get("label")
        if not label or len(label) > 500:
            return None  # fall back; the slow path gives a clearer error
        node_type = nd.get("node_type", "concept")
        if not validate_node_type(node_type):
            return None
        confidence = validate_confidence(nd.get("confidence", 1.0))
        source_tier = validate_source_tier(nd.get("source_tier"))
        data_origin = validate_data_origin(nd.get("data_origin"))
        enforce_confidence_ceiling(confidence, source_tier)
        priority = nd.get("priority")
        if priority is not None and priority not in VALID_PRIORITY:
            return None
        utility_scale = nd.get("utility_scale")
        if utility_scale is not None:
            if isinstance(utility_scale, str):
                if utility_scale not in ("best", "neutral", "worst"):
                    return None
                utility_scale = {"best": 1.0, "neutral": 0.5, "worst": 0.0}[utility_scale]
            elif not isinstance(utility_scale, (int, float)):
                return None
            if not (0 <= utility_scale <= 1):
                return None

        url = nd.get("url")
        source_url = nd.get("source_url")
        if source_url is not None and url is None:
            url = source_url

        connects_to = nd.get("connects_to")
        if connects_to is not None:
            # Fast path doesn't handle connects_to; the slow path does.
            return None

        action_alternatives = nd.get("action_alternatives")
        alternatives_json = (
            json.dumps(action_alternatives)
            if action_alternatives is not None
            else None
        )
        tags_json = json.dumps(nd.get("tags")) if nd.get("tags") else None
        metadata_json = json.dumps(nd.get("metadata")) if nd.get("metadata") else None

        node_id = generate_node_id(label, node_type)
        parsed.append({
            "id": node_id,
            "label": label,
            "node_type": node_type,
            "content": nd.get("content"),
            "visibility": nd.get("visibility", "team"),
            "provenance": nd.get("provenance"),
            "confidence": confidence,
            "priority": priority,
            "url": url,
            "tags_json": tags_json,
            "metadata_json": metadata_json,
            "utility_scale": utility_scale,
            "utility_usd_per_day": nd.get("utility_usd_per_day"),
            "utility_currency": nd.get("utility_currency"),
            "current_best_action": nd.get("current_best_action"),
            "alternatives_json": alternatives_json,
            "source_tier": source_tier,
            "source_author": nd.get("source_author"),
            "source_institution": nd.get("source_institution"),
            "data_origin": data_origin,
        })

    # --- 2. Bulk check for soft-deleted collisions.
    all_ids = [p["id"] for p in parsed]
    placeholders = ",".join(["?"] * len(all_ids))
    soft_deleted = conn.execute(
        f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NOT NULL",
        all_ids,
    ).fetchall()
    if soft_deleted:
        # Reactivation path is per-row; fall back to the slow path.
        return None

    # Also check for primary-key collisions with live rows (would
    # otherwise raise on the executemany). If any collide, fall back.
    existing = conn.execute(
        f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        all_ids,
    ).fetchall()
    if existing:
        return None

    # --- 3. Single executemany INSERT for the new nodes.
    cols_sql = ",".join(_NODE_COLS)
    values_sql = "(" + ",".join(["?"] * len(_NODE_COLS)) + ")"
    insert_sql = f"INSERT INTO ohm_nodes ({cols_sql}) VALUES {values_sql}"

    rows_to_insert: list[tuple] = []
    for p in parsed:
        rows_to_insert.append((
            p["id"], p["label"], p["node_type"], p["content"], created_by,
            p["visibility"], p["provenance"], p["confidence"], p["priority"], p["url"],
            p["tags_json"], p["metadata_json"],
            p["utility_scale"], p["utility_usd_per_day"], p["utility_currency"],
            p["current_best_action"], p["alternatives_json"],
            p["source_tier"],
            p["source_author"], p["source_institution"], p["data_origin"],
        ))
    conn.executemany(insert_sql, rows_to_insert)

    # --- 4. Single executemany INSERT into ohm_change_feed.
    empty_old = json.dumps({})
    feed_sql = (
        f"INSERT INTO ohm_change_feed ({','.join(_FEED_COLS)}) "
        f"VALUES (" + ",".join(["?"] * len(_FEED_COLS)) + ")"
    )
    feed_rows: list[tuple] = []
    for p in parsed:
        feed_rows.append(("ohm_nodes", p["id"], "INSERT", created_by, empty_old))
    try:
        conn.executemany(feed_sql, feed_rows)
    except Exception:
        # ohm_change_feed may be missing on pre-migration DBs; non-fatal.
        pass

    # --- 5. Bulk alias registration (OHM-z2gp).
    try:
        from ohm.validation import normalize_alias

        alias_rows: list[tuple] = []
        for p in parsed:
            norm = normalize_alias(p["label"])
            if norm:
                alias_rows.append((str(uuid.uuid4()), norm, p["id"]))

        if alias_rows:
            # Skip aliases already registered for these node ids.
            new_pids = [r[2] for r in alias_rows]
            ph = ",".join(["?"] * len(new_pids))
            already = {
                r[0]
                for r in conn.execute(
                    f"SELECT node_id FROM ohm_aliases WHERE node_id IN ({ph})",
                    new_pids,
                ).fetchall()
            }
            rows_to_register = [r for r in alias_rows if r[2] not in already]
            if rows_to_register:
                alias_sql = (
                    "INSERT INTO ohm_aliases (id, alias_norm, node_id) "
                    "VALUES (?, ?, ?)"
                )
                conn.executemany(alias_sql, rows_to_register)
    except Exception:
        # Alias registration is best-effort.
        pass

    # --- 6. SELECT all the new records and return in input order.
    ph = ",".join(["?"] * len(all_ids))
    rows = conn.execute(
        f"SELECT * FROM ohm_nodes WHERE id IN ({ph}) AND deleted_at IS NULL",
        all_ids,
    ).fetchall()
    col_names = [desc[0] for desc in conn.description]
    by_id = {dict(zip(col_names, r))["id"]: dict(zip(col_names, r)) for r in rows}
    return [by_id[nid] for nid in all_ids if nid in by_id]


def fast_batch_create_edges(
    conn: "DuckDBPyConnection",
    *,
    edges: list[dict[str, Any]],
    created_by: str,
) -> list[dict[str, Any]] | None:
    """Fast multi-row INSERT for batch_create_edges.

    Returns the list of created edge records in input order, or
    ``None`` if the fast path does not apply.

    Fast-path conditions:
    - All edges use simple types/layers that pass validation.

    Semantics preserved:
    - Per-row validation (edge_type/layer compatibility, confidence,
      PERT triples, urgency, source_tier ceiling)
    - UUID generation per edge
    - JSON serialization for metadata
    - One ohm_change_feed entry per edge (INSERT, attributed to
      ``created_by``)
    - Returns full edge records via SELECT after the INSERT
    """
    if not edges:
        return []

    from ohm.schema import validate_edge_type, VALID_URGENCY
    from ohm.validation import (
        validate_confidence,
        validate_pert_triple,
        validate_source_tier,
        enforce_confidence_ceiling,
    )

    parsed: list[dict[str, Any]] = []
    for idx, ed in enumerate(edges):
        layer = ed.get("layer", "L3")
        edge_type = ed["edge_type"]
        if not validate_edge_type(layer, edge_type):
            return None
        confidence = validate_confidence(ed.get("confidence", 0.7))
        source_tier = validate_source_tier(ed.get("source_tier"))
        enforce_confidence_ceiling(confidence, source_tier)
        urgency = ed.get("urgency")
        if urgency is not None and urgency not in VALID_URGENCY:
            return None
        try:
            validate_pert_triple(
                ed.get("probability_p05"), ed.get("probability_p50"), ed.get("probability_p95"),
                name=f"item {idx} probability PERT",
            )
            validate_pert_triple(
                ed.get("confidence_p05"), ed.get("confidence_p50"), ed.get("confidence_p95"),
                name=f"item {idx} confidence PERT",
            )
        except ValueError:
            return None

        metadata_json = json.dumps(ed.get("metadata")) if ed.get("metadata") else None
        parsed.append({
            "from_node": ed["from_node"],
            "to_node": ed["to_node"],
            "layer": layer,
            "edge_type": edge_type,
            "confidence": confidence,
            "probability": ed.get("probability"),
            "urgency": urgency,
            "condition": ed.get("condition"),
            "provenance": ed.get("provenance"),
            "metadata_json": metadata_json,
            "probability_p05": ed.get("probability_p05"),
            "probability_p50": ed.get("probability_p50"),
            "probability_p95": ed.get("probability_p95"),
            "confidence_p05": ed.get("confidence_p05"),
            "confidence_p50": ed.get("confidence_p50"),
            "confidence_p95": ed.get("confidence_p95"),
            "source_tier": source_tier,
        })

    edge_ids = [str(uuid.uuid4()) for _ in parsed]

    cols_sql = ",".join(_EDGE_COLS)
    values_sql = "(" + ",".join(["?"] * len(_EDGE_COLS)) + ")"
    insert_sql = f"INSERT INTO ohm_edges ({cols_sql}) VALUES {values_sql}"

    rows_to_insert: list[tuple] = []
    for eid, p in zip(edge_ids, parsed):
        rows_to_insert.append((
            eid, p["from_node"], p["to_node"], p["layer"], p["edge_type"], created_by,
            p["confidence"], p["probability"], p["urgency"], p["condition"],
            p["provenance"], p["metadata_json"],
            p["probability_p05"], p["probability_p50"], p["probability_p95"],
            p["confidence_p05"], p["confidence_p50"], p["confidence_p95"],
            p["source_tier"],
        ))
    conn.executemany(insert_sql, rows_to_insert)

    empty_old = json.dumps({})
    feed_sql = (
        f"INSERT INTO ohm_change_feed ({','.join(_FEED_COLS)}) "
        f"VALUES (" + ",".join(["?"] * len(_FEED_COLS)) + ")"
    )
    feed_rows: list[tuple] = []
    for eid in edge_ids:
        feed_rows.append(("ohm_edges", eid, "INSERT", created_by, empty_old))
    try:
        conn.executemany(feed_sql, feed_rows)
    except Exception:
        pass

    ph = ",".join(["?"] * len(edge_ids))
    rows = conn.execute(
        f"SELECT * FROM ohm_edges WHERE id IN ({ph}) AND deleted_at IS NULL",
        edge_ids,
    ).fetchall()
    col_names = [desc[0] for desc in conn.description]
    by_id = {dict(zip(col_names, r))["id"]: dict(zip(col_names, r)) for r in rows}
    return [by_id[eid] for eid in edge_ids if eid in by_id]