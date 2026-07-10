"""suggestions queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

    from ohm.graph.queries import query_neighborhood

from ohm.graph.queries._shared import _rows_to_dicts


def create_suggestion(
    conn: DuckDBPyConnection,
    *,
    suggestion_type: str,
    from_node: str | None = None,
    to_node: str | None = None,
    target_node: str | None = None,
    suggested_edge_type: str | None = None,
    suggested_layer: str | None = None,
    confidence: float = 0.5,
    source_method: str = "manual",
    source_agent: str = "system",
    metadata: dict | None = None,
    created_by: str = "system",
) -> dict[str, Any]:
    import json
    import uuid

    from ohm.validation import validate_suggestion_type

    suggestion_type = validate_suggestion_type(suggestion_type)

    existing = conn.execute(
        """SELECT id, evidence_count FROM ohm_suggestions
           WHERE from_node IS NOT DISTINCT FROM ?
             AND to_node IS NOT DISTINCT FROM ?
             AND target_node IS NOT DISTINCT FROM ?
             AND status = 'ripe' AND deleted_at IS NULL""",
        [from_node, to_node, target_node],
    ).fetchone()

    if existing:
        new_count = (existing[1] or 1) + 1
        conn.execute(
            "UPDATE ohm_suggestions SET evidence_count = ?, last_ripened_at = CURRENT_TIMESTAMP WHERE id = ?",
            [new_count, existing[0]],
        )
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [existing[0]]))[0]

    sid = f"sug_{uuid.uuid4().hex[:12]}"
    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO ohm_suggestions
           (id, suggestion_type, from_node, to_node, target_node, suggested_edge_type, suggested_layer,
            confidence, status, source_method, source_agent, metadata, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ripe', ?, ?, ?, ?)""",
        [sid, suggestion_type, from_node, to_node, target_node, suggested_edge_type, suggested_layer, confidence, source_method, source_agent, metadata_json, created_by],
    )
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [sid]))[0]


def query_suggestions(
    conn: DuckDBPyConnection,
    *,
    status: str | None = None,
    source_method: str | None = None,
    target_node: str | None = None,
    min_ripeness: float = 0.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if source_method is not None:
        conditions.append("source_method = ?")
        params.append(source_method)
    if target_node is not None:
        conditions.append("target_node = ?")
        params.append(target_node)
    if min_ripeness > 0:
        conditions.append("ripeness_score >= ?")
        params.append(min_ripeness)
    where = " WHERE " + " AND ".join(conditions)
    params.append(limit)
    return _rows_to_dicts(conn.execute(f"SELECT * FROM ohm_suggestions{where} ORDER BY ripeness_score DESC, suggested_at DESC LIMIT ?", params))


def promote_suggestion(
    conn: DuckDBPyConnection,
    suggestion_id: str,
    *,
    promoted_by: str,
    edge_layer: str = "L3",
) -> dict[str, Any]:
    from ohm.exceptions import OHMError
    from ohm.validation import validate_identifier

    suggestion_id = validate_identifier(suggestion_id, name="suggestion_id")
    row = conn.execute(
        "SELECT * FROM ohm_suggestions WHERE id = ? AND deleted_at IS NULL",
        [suggestion_id],
    ).fetchone()
    if not row:
        raise OHMError(f"Suggestion {suggestion_id} not found")

    cols = [d[0] for d in conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [suggestion_id]).description]
    sug = dict(zip(cols, row))

    if sug["status"] != "ripe":
        raise OHMError(f"Suggestion {suggestion_id} status is '{sug['status']}', must be 'ripe' to promote")

    if sug["suggestion_type"] == "edge" and sug["from_node"] and sug["to_node"] and sug["suggested_edge_type"]:
        import uuid

        edge_id = f"edge_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [edge_id, sug["from_node"], sug["to_node"], sug["suggested_edge_type"], edge_layer, promoted_by, sug["confidence"]],
        )

    conn.execute(
        "UPDATE ohm_suggestions SET status = 'promoted', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        [promoted_by, suggestion_id],
    )
    return {"suggestion_id": suggestion_id, "status": "promoted", "promoted_by": promoted_by}


def reject_suggestion(
    conn: DuckDBPyConnection,
    suggestion_id: str,
    *,
    rejected_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    from ohm.exceptions import OHMError
    from ohm.validation import validate_identifier

    suggestion_id = validate_identifier(suggestion_id, name="suggestion_id")
    row = conn.execute(
        "SELECT status FROM ohm_suggestions WHERE id = ? AND deleted_at IS NULL",
        [suggestion_id],
    ).fetchone()
    if not row:
        raise OHMError(f"Suggestion {suggestion_id} not found")

    conn.execute(
        "UPDATE ohm_suggestions SET status = 'rejected', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP, review_notes = ? WHERE id = ?",
        [rejected_by, notes, suggestion_id],
    )
    return {"suggestion_id": suggestion_id, "status": "rejected"}


def expire_suggestions(
    conn: DuckDBPyConnection,
    *,
    max_age_days: int = 30,
) -> dict[str, Any]:
    conn.execute(
        """UPDATE ohm_suggestions
           SET status = 'expired'
           WHERE status = 'ripe'
             AND deleted_at IS NULL
             AND suggested_at < CURRENT_TIMESTAMP - INTERVAL '?' DAY""",
        [max_age_days],
    )
    count = conn.execute("SELECT changes()").fetchone()[0]
    return {"expired_count": count}


def batch_orphan_triage(
    conn: DuckDBPyConnection,
    *,
    limit: int = 50,
    exclude_types: frozenset[str] | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """Batch triage orphan nodes, producing link suggestions.

    Scans orphan nodes (zero edges) and generates suggestions for connecting
    them to the graph. Uses two heuristics:
    1. Same-type matching: orphan shares type with a connected node.
    2. Label similarity: orphan label overlaps with connected node labels.

    Returns a triage report with per-node suggestions and summary stats.

    Args:
        conn: DuckDB connection.
        limit: Max orphans to process (default 50).
        exclude_types: Node types to skip (default: fragment, agent, skill, value, goal).
        min_confidence: Only triage orphans with confidence >= this value.
    """

    if exclude_types is None:
        exclude_types = frozenset({"fragment", "agent", "skill", "value", "goal"})

    exclude_clause = ""
    params: list[Any] = []
    if exclude_types:
        placeholders = ", ".join(["?"] * len(exclude_types))
        exclude_clause = f"AND n.type NOT IN ({placeholders})"
        params.extend(list(exclude_types))

    confidence_clause = ""
    if min_confidence is not None:
        confidence_clause = "AND n.confidence >= ?"
        params.append(min_confidence)

    orphans = conn.execute(
        f"""
        SELECT n.id, n.label, n.type, n.confidence, n.created_by, n.created_at
        FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE (e.from_node = n.id OR e.to_node = n.id)
                AND e.deleted_at IS NULL
          )
          {exclude_clause}
          {confidence_clause}
        ORDER BY n.confidence DESC NULLS LAST
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()

    if not orphans:
        return {
            "triaged_count": 0,
            "total_orphans": 0,
            "with_suggestions": 0,
            "without_suggestions": 0,
            "suggestions": [],
            "types_seen": {},
            "method": "batch_orphan_triage",
        }

    total_orphan_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE (e.from_node = n.id OR e.to_node = n.id)
                AND e.deleted_at IS NULL
          )
    """).fetchone()
    total_orphans = total_orphan_row[0] if total_orphan_row else 0

    types_seen: dict[str, int] = {}
    suggestions: list[dict[str, Any]] = []

    for row in orphans:
        orphan_id, label, node_type, confidence, created_by, created_at = row
        types_seen[node_type] = types_seen.get(node_type, 0) + 1

        same_type_matches = conn.execute(
            """
            SELECT c.id, c.label, c.type
            FROM ohm_nodes c
            WHERE c.deleted_at IS NULL
              AND c.type = ?
              AND c.id != ?
              AND EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE (e.from_node = c.id OR e.to_node = c.id)
                    AND e.deleted_at IS NULL
              )
            ORDER BY c.confidence DESC NULLS LAST
            LIMIT 3
            """,
            [node_type, orphan_id],
        ).fetchall()

        label_words = set(label.lower().split()) if label else set()
        label_matches: list[tuple[str, str, str, int]] = []
        if label_words and len(label_words) <= 20:
            label_match_rows = conn.execute(
                """
                SELECT c.id, c.label, c.type
                FROM ohm_nodes c
                WHERE c.deleted_at IS NULL
                  AND c.id != ?
                  AND c.label IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM ohm_edges e
                      WHERE (e.from_node = c.id OR e.to_node = c.id)
                        AND e.deleted_at IS NULL
                  )
                LIMIT 200
                """,
                [orphan_id],
            ).fetchall()
            for lm in label_match_rows:
                target_words = set(lm[1].lower().split()) if lm[1] else set()
                overlap = len(label_words & target_words)
                if overlap >= 2:
                    label_matches.append((lm[0], lm[1], lm[2], overlap))
            label_matches.sort(key=lambda x: x[3], reverse=True)
            label_matches = label_matches[:3]

        node_suggestions = []
        for m in same_type_matches:
            node_suggestions.append(
                {
                    "target_id": m[0],
                    "target_label": m[1],
                    "reason": f"Same type '{node_type}'",
                    "score": 0.6,
                    "edge_type": "APPLIES_TO",
                }
            )
        for m in label_matches:
            node_suggestions.append(
                {
                    "target_id": m[0],
                    "target_label": m[1],
                    "reason": f"Label overlap ({m[3]} words)",
                    "score": 0.4 + 0.1 * m[3],
                    "edge_type": "APPLIES_TO",
                }
            )
        node_suggestions.sort(key=lambda s: s["score"], reverse=True)
        node_suggestions = node_suggestions[:3]

        suggestions.append(
            {
                "orphan_id": orphan_id,
                "orphan_label": label,
                "orphan_type": node_type,
                "confidence": confidence,
                "created_by": created_by,
                "suggestions": node_suggestions,
            }
        )

    has_suggestions = sum(1 for s in suggestions if s["suggestions"])
    return {
        "triaged_count": len(orphans),
        "total_orphans": total_orphans,
        "with_suggestions": has_suggestions,
        "without_suggestions": len(orphans) - has_suggestions,
        "suggestions": suggestions,
        "types_seen": types_seen,
        "method": "batch_orphan_triage",
    }


# ── Neighborhood Narrative (OHM-q9rt.1) ─────────────────────────────────────


def query_neighborhood_narrative(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    agent_name: str | None = None,
    depth: int = 2,
) -> dict[str, Any]:
    """Build a contextualized narrative for a node — "You care about X because of Y and Z".

    Walks the edges touching *node_id* and builds reasoning chains that explain
    WHY an agent should care about this node. When *agent_name* is provided,
    the narrative is personalized: it highlights edges authored by that agent
    and traces paths from the agent's claims to this node.

    Args:
        conn: Database connection.
        node_id: Target node to build the narrative for.
        agent_name: Optional agent to personalize the narrative for.
        depth: How many hops to walk (default 2 — immediate neighbors + their neighbors).

    Returns:
        Dict with:
          - node: {id, label, type, confidence}
          - why_it_matters: list of reasoning chains, each:
              {path: [{node_id, label, type}, ...], edges: [{edge_type, confidence, layer, created_by}], summary: str}
          - evidence: observations on this node and its immediate neighbors
          - connections_summary: human-readable string
          - agent_context: when agent_name is set, the agent's edges touching this node
    """
    from ohm.validation import validate_identifier, validate_depth

    node_id = validate_identifier(node_id, name="node_id")
    depth = validate_depth(depth)

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_info = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
        "created_by": node_row[4],
    }

    # Fetch immediate neighbors (1-hop edges touching this node)
    edges = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_by, e.created_at,
                  nf.label AS from_label, nf.type AS from_type,
                  nt.label AS to_label, nt.type AS to_type
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node AND nf.deleted_at IS NULL
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node AND nt.deleted_at IS NULL
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.deleted_at IS NULL
             AND e.layer != 'L0'
           ORDER BY e.confidence DESC
           LIMIT 50""",
        [node_id, node_id],
    ).fetchall()

    # Build reasoning chains
    why_it_matters: list[dict[str, Any]] = []
    agent_edges: list[dict[str, Any]] = []

    for row in edges:
        eid, from_node, to_node, edge_type, layer, conf, created_by, created_at, from_label, from_type, to_label, to_type = row

        # Determine the "other" node (the one that's not the target)
        if from_node == node_id:
            other_id, other_label, other_type = to_node, to_label, to_type
            direction = "outgoing"
        else:
            other_id, other_label, other_type = from_node, from_label, from_type
            direction = "incoming"

        edge_info = {
            "edge_id": eid,
            "edge_type": edge_type,
            "layer": layer,
            "confidence": conf,
            "created_by": created_by,
            "direction": direction,
        }

        chain = {
            "path": [
                {"node_id": other_id, "label": other_label, "type": other_type},
                {"node_id": node_id, "label": node_info["label"], "type": node_info["type"]},
            ],
            "edges": [edge_info],
            "summary": f"{other_label} {edge_type} {node_info['label']}",
        }
        why_it_matters.append(chain)

        if agent_name and created_by == agent_name:
            agent_edges.append(
                {
                    "edge_id": eid,
                    "other_node": {"id": other_id, "label": other_label, "type": other_type},
                    "edge_type": edge_type,
                    "confidence": conf,
                    "direction": direction,
                }
            )

    # Fetch observations on this node and its immediate neighbors
    neighbor_ids = [node_id]
    for row in edges:
        other = row[1] if row[2] == node_id else row[2]
        if other and other not in neighbor_ids:
            neighbor_ids.append(other)

    # Limit to avoid huge queries
    neighbor_ids = neighbor_ids[:20]
    placeholders = ",".join(["?"] * len(neighbor_ids))
    obs_rows = conn.execute(
        f"""SELECT o.id, o.node_id, o.type, o.value, o.baseline, o.created_by,
                   o.created_at, n.label AS node_label
            FROM ohm_observations o
            LEFT JOIN ohm_nodes n ON n.id = o.node_id
            WHERE o.node_id IN ({placeholders})
              AND o.deleted_at IS NULL
            ORDER BY o.created_at DESC
            LIMIT 50""",
        neighbor_ids,
    ).fetchall()

    evidence = []
    for row in obs_rows:
        obs = {
            "obs_id": row[0],
            "node_id": row[1],
            "obs_type": row[2],
            "value": row[3],
            "baseline": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "node_label": row[7],
        }
        evidence.append(obs)

    # Build connections summary
    edge_types = [c["edges"][0]["edge_type"] for c in why_it_matters]
    if not edge_types:
        summary = f"{node_info['label']} has no connections yet."
    elif len(edge_types) == 1:
        summary = f"You care about {node_info['label']} because it {edge_types[0]} something."
    else:
        type_counts: dict[str, int] = {}
        for et in edge_types:
            type_counts[et] = type_counts.get(et, 0) + 1
        parts = [f"{count} {et}" for et, count in sorted(type_counts.items(), key=lambda x: -x[1])]
        summary = f"{node_info['label']} is connected via {', '.join(parts)}."

    result: dict[str, Any] = {
        "node": node_info,
        "why_it_matters": why_it_matters,
        "evidence": evidence,
        "connections_summary": summary,
        "connection_count": len(why_it_matters),
        "evidence_count": len(evidence),
    }

    if agent_name:
        result["agent_context"] = {
            "agent": agent_name,
            "my_edges": agent_edges,
            "my_edge_count": len(agent_edges),
        }

    return result


# ── Claim Lineage (OHM-q9rt.2) ──────────────────────────────────────────────


def query_claim_lineage(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Explode a synthesis/pattern/decision node into its supporting evidence chain.

    Traces backward through provenance edges (DERIVES_FROM, REFERENCES,
    INFLUENCES, SUPPORTS, SUPPORTS_EVIDENCE, TESTS) to find all supporting
    observations and source nodes. Returns a tree structure with confidence
    products, gap detection (nodes with no observations), and source leaves.

    Args:
        conn: Database connection.
        node_id: The claim/synthesis/pattern node to trace from.
        max_depth: Maximum chain depth (default 10).

    Returns:
        Dict with:
          - claim: the target node {id, label, type, confidence}
          - lineage: tree of supporting nodes, each with:
              {node_id, label, type, depth, edge_type, edge_confidence,
               confidence_chain, observations, children: [...]}
          - sources: list of source nodes at the leaves (type='source')
          - gaps: nodes in the chain with NO observations (weak links)
          - max_confidence: highest confidence_chain across all leaves
          - min_confidence: lowest confidence_chain (weakest link)
    """
    from ohm.validation import validate_identifier, validate_depth
    from ohm.exceptions import NodeNotFoundError

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth)

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    claim = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
    }

    # Walk provenance edges (L2 + L3 evidence edges)
    provenance_types = (
        "DERIVES_FROM",
        "REFERENCES",
        "INFLUENCES",
        "SUPPORTS",
        "SUPPORTS_EVIDENCE",
        "CONTRADICTS_EVIDENCE",
        "TESTS",
    )
    placeholders = ",".join(["?"] * len(provenance_types))

    chain_rows = conn.execute(
        f"""
        WITH RECURSIVE lineage AS (
            SELECT
                ? AS node_id,
                '' AS from_node,
                '' AS edge_id,
                '' AS edge_type,
                1.0 AS conf_product,
                0 AS depth,
                []::VARCHAR[] AS path
            UNION ALL
            SELECT
                e.to_node AS node_id,
                e.from_node AS from_node,
                e.id AS edge_id,
                e.edge_type AS edge_type,
                lc.conf_product * COALESCE(e.confidence, 1.0) AS conf_product,
                lc.depth + 1 AS depth,
                array_append(lc.path, e.id) AS path
            FROM lineage lc
            JOIN ohm_edges e ON e.from_node = lc.node_id
            WHERE lc.depth < ?
              AND e.edge_type IN ({placeholders})
              AND e.deleted_at IS NULL
              AND NOT array_contains(lc.path, e.id)
        )
        SELECT
            lc.node_id,
            lc.from_node,
            lc.edge_id,
            lc.edge_type,
            ROUND(lc.conf_product, 6) AS confidence_chain,
            lc.depth,
            n.label AS node_label,
            n.type AS node_type,
            n.confidence AS node_confidence,
            n.created_by AS node_created_by
        FROM lineage lc
        LEFT JOIN ohm_nodes n ON n.id = lc.node_id AND n.deleted_at IS NULL
        WHERE lc.depth > 0
        ORDER BY lc.depth, lc.conf_product DESC
        """,
        [node_id, max_depth, *provenance_types],
    ).fetchall()

    # Fetch observations for all nodes in the chain (plus the claim itself)
    all_node_ids = {node_id}
    for row in chain_rows:
        if row[0]:
            all_node_ids.add(row[0])

    obs_map: dict[str, list[dict[str, Any]]] = {}
    if all_node_ids:
        obs_placeholders = ",".join(["?"] * len(all_node_ids))
        obs_rows = conn.execute(
            f"""SELECT o.id, o.node_id, o.type, o.value, o.baseline,
                      o.created_by, o.created_at, o.source
               FROM ohm_observations o
               WHERE o.node_id IN ({obs_placeholders})
                 AND o.deleted_at IS NULL
               ORDER BY o.created_at DESC""",
            list(all_node_ids),
        ).fetchall()
        for row in obs_rows:
            nid = row[1]
            if nid not in obs_map:
                obs_map[nid] = []
            obs_map[nid].append(
                {
                    "obs_id": row[0],
                    "obs_type": row[2],
                    "value": row[3],
                    "baseline": row[4],
                    "created_by": row[5],
                    "created_at": str(row[6]) if row[6] else None,
                    "source": row[7],
                }
            )

    # Build tree: each chain row is a parent→child relationship
    # from_node is the parent (closer to claim), node_id is the child (further)
    tree_nodes: dict[str, dict[str, Any]] = {}

    def _get_tree_node(nid: str, label: str, ntype: str, depth: int, edge_type: str, edge_conf: float, conf_chain: float, created_by: str) -> dict[str, Any]:
        if nid not in tree_nodes:
            tree_nodes[nid] = {
                "node_id": nid,
                "label": label,
                "type": ntype,
                "depth": depth,
                "edge_type": edge_type,
                "edge_confidence": edge_conf,
                "confidence_chain": conf_chain,
                "created_by": created_by,
                "observations": obs_map.get(nid, []),
                "children": [],
            }
        return tree_nodes[nid]

    sources: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    all_confidences: list[float] = []

    for row in chain_rows:
        child_id, parent_id, edge_id, edge_type, conf_chain, depth, child_label, child_type, child_conf, child_created_by = row

        if not child_id:
            continue

        child_node = _get_tree_node(
            child_id,
            child_label or child_id,
            child_type or "unknown",
            depth,
            edge_type,
            child_conf or 1.0,
            conf_chain,
            child_created_by or "unknown",
        )

        # Track source nodes (leaves)
        if child_type == "source":
            sources.append(
                {
                    "node_id": child_id,
                    "label": child_label,
                    "depth": depth,
                    "confidence_chain": conf_chain,
                }
            )

        # Track gaps (nodes with no observations)
        if not obs_map.get(child_id):
            gaps.append(
                {
                    "node_id": child_id,
                    "label": child_label,
                    "type": child_type,
                    "depth": depth,
                    "edge_type": edge_type,
                }
            )

        all_confidences.append(conf_chain)

        # Attach to parent in tree
        if parent_id and parent_id in tree_nodes:
            tree_nodes[parent_id]["children"].append(child_node)

    # Also add claim's own observations to the tree root
    claim_obs = obs_map.get(node_id, [])

    return {
        "claim": claim,
        "claim_observations": claim_obs,
        "lineage": list(tree_nodes.values()),
        "sources": sources,
        "gaps": gaps,
        "max_confidence": max(all_confidences) if all_confidences else None,
        "min_confidence": min(all_confidences) if all_confidences else None,
        "chain_depth": max((r[5] for r in chain_rows), default=0),
        "total_nodes": len(tree_nodes),
        "total_sources": len(sources),
        "total_gaps": len(gaps),
    }


# ── Contradiction Summary (OHM-q9rt.3) ──────────────────────────────────────


def query_contradiction_summary(
    conn: DuckDBPyConnection,
    node_id: str,
) -> dict[str, Any]:
    """Build a structured summary of contradictions involving a node (OHM-q9rt.3).

    Given a node with contradictory observations or challenged edges, returns
    a "both sides" view: groups of conflicting observations, their supporting
    agents, effective confidence (with decay), existing reconciliation attempts
    (challenges/NEGATES), and a recommendation for which side has stronger evidence.

    Args:
        conn: Database connection.
        node_id: The node to analyze for contradictions.

    Returns:
        Dict with:
          - node: {id, label, type, confidence}
          - sides: list of conflicting observation groups, each with:
              {agent, observations, effective_confidence, supporting_edges}
          - challenges: CHALLENGED_BY edges targeting edges that touch this node
          - recommendation: which side has stronger evidence (or 'unresolved')
          - has_contradiction: bool
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError
    from ohm.graph.decay import confidence_at

    node_id = validate_identifier(node_id, name="node_id")

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_info = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
    }

    # 1. Find opposing observations on this node (different agents, opposite directions from baseline)
    obs_rows = conn.execute(
        """SELECT o.id, o.type, o.value, o.baseline, o.sigma, o.created_by,
                  o.created_at, o.source, o.half_life_days, o.weibull_shape,
                  o.valid_from, o.valid_to
           FROM ohm_observations o
           WHERE o.node_id = ?
             AND o.deleted_at IS NULL
             AND o.value IS NOT NULL
           ORDER BY o.created_at DESC
           LIMIT 100""",
        [node_id],
    ).fetchall()

    # Group observations by direction relative to baseline
    above: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    neutral: list[dict[str, Any]] = []

    for row in obs_rows:
        obs = {
            "obs_id": row[0],
            "obs_type": row[1],
            "value": row[2],
            "baseline": row[3],
            "sigma": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "source": row[7],
            "effective_confidence": round(
                confidence_at(
                    {
                        "value": row[2],
                        "half_life_days": row[8],
                        "weibull_shape": row[9],
                        "valid_from": row[10],
                        "valid_to": row[11],
                        "type": row[1],
                        "created_at": str(row[6]) if row[6] else None,
                    }
                ),
                4,
            ),
        }
        if row[3] is not None and row[2] is not None:
            if row[2] > row[3]:
                above.append(obs)
            elif row[2] < row[3]:
                below.append(obs)
            else:
                neutral.append(obs)
        else:
            neutral.append(obs)

    # Build sides: each side is a group of observations + the agents who made them
    sides: list[dict[str, Any]] = []

    if above:
        agents_above = list({o["created_by"] for o in above if o["created_by"]})
        avg_conf_above = sum(o["effective_confidence"] for o in above) / len(above)
        sides.append(
            {
                "direction": "above_baseline",
                "agents": agents_above,
                "observations": above,
                "effective_confidence": round(avg_conf_above, 4),
                "observation_count": len(above),
            }
        )

    if below:
        agents_below = list({o["created_by"] for o in below if o["created_by"]})
        avg_conf_below = sum(o["effective_confidence"] for o in below) / len(below)
        sides.append(
            {
                "direction": "below_baseline",
                "agents": agents_below,
                "observations": below,
                "effective_confidence": round(avg_conf_below, 4),
                "observation_count": len(below),
            }
        )

    if neutral and not above and not below:
        sides.append(
            {
                "direction": "neutral",
                "agents": list({o["created_by"] for o in neutral if o["created_by"]}),
                "observations": neutral,
                "effective_confidence": round(sum(o["effective_confidence"] for o in neutral) / len(neutral), 4) if neutral else 0.0,
                "observation_count": len(neutral),
            }
        )

    # 2. Find CHALLENGED_BY edges targeting edges that touch this node
    challenge_rows = conn.execute(
        """SELECT c.id AS challenge_id, c.challenge_of AS target_edge_id,
                  c.created_by AS challenger, c.confidence AS challenge_confidence,
                  c.provenance AS reason, c.created_at,
                  target.edge_type AS target_edge_type,
                  target.created_by AS target_author,
                  target.confidence AS target_confidence
           FROM ohm_edges c
           JOIN ohm_edges target ON target.id = c.challenge_of
           WHERE c.challenge_type = 'CHALLENGED_BY'
             AND (target.from_node = ? OR target.to_node = ?)
             AND target.deleted_at IS NULL
           ORDER BY c.confidence DESC, c.created_at DESC
           LIMIT 50""",
        [node_id, node_id],
    ).fetchall()

    challenges = [
        {
            "challenge_id": row[0],
            "target_edge_id": row[1],
            "challenger": row[2],
            "challenge_confidence": row[3],
            "reason": row[4],
            "created_at": str(row[5]) if row[5] else None,
            "target_edge_type": row[6],
            "target_author": row[7],
            "target_confidence": row[8],
        }
        for row in challenge_rows
    ]

    # 3. Recommendation: which side has stronger evidence?
    has_contradiction = len(sides) >= 2 or len(challenges) > 0
    recommendation = "no_contradiction"

    if len(sides) >= 2:
        # Compare effective confidence × observation count
        side0_weight = sides[0]["effective_confidence"] * sides[0]["observation_count"]
        side1_weight = sides[1]["effective_confidence"] * sides[1]["observation_count"]
        if side0_weight > side1_weight * 1.2:
            recommendation = f"Side '{sides[0]['direction']}' has stronger evidence (conf={sides[0]['effective_confidence']}, count={sides[0]['observation_count']})"
        elif side1_weight > side0_weight * 1.2:
            recommendation = f"Side '{sides[1]['direction']}' has stronger evidence (conf={sides[1]['effective_confidence']}, count={sides[1]['observation_count']})"
        else:
            recommendation = "unresolved — both sides have comparable evidence"

    if challenges and not has_contradiction:
        recommendation = f"{len(challenges)} challenge(s) registered — review required"

    return {
        "node": node_info,
        "sides": sides,
        "challenges": challenges,
        "recommendation": recommendation,
        "has_contradiction": has_contradiction,
        "total_observations": len(obs_rows),
        "total_challenges": len(challenges),
    }


# ── Task Context (OHM-q9rt.4) ───────────────────────────────────────────────


def query_task_context(
    conn: DuckDBPyConnection,
    task_id: str,
) -> dict[str, Any]:
    """Bundle a task node with its relevant subgraph and expected outcome (OHM-q9rt.4).

    Given a task node, returns the task with its 2-hop subgraph, rationale
    chain (decisions/observations that led to it), expected outcome, and any
    blocking tasks.

    Args:
        conn: Database connection.
        task_id: The task node ID.

    Returns:
        Dict with:
          - task: {id, label, type, status, assigned_to, expected_claim,
                   success_criteria, outcome, outcome_notes, created_by, created_at}
          - subgraph: {nodes: [...], edges: [...]} within 2 hops
          - rationale: list of reasoning chain entries (nodes connected via
            DECISION_DEPENDS_ON, DERIVES_FROM, REFERENCES edges)
          - expected_outcome: success_criteria text or a derived summary
          - blocking: list of other task nodes that block this one
          - blocked_by_count: int
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    task_id = validate_identifier(task_id, name="task_id")

    # Fetch the task node with all task-specific fields
    task_row = conn.execute(
        """SELECT id, label, type, task_status, assigned_to, expected_claim,
                  success_criteria, outcome, outcome_notes, created_by, created_at,
                  due_date, priority, confidence
           FROM ohm_nodes
           WHERE id = ? AND deleted_at IS NULL""",
        [task_id],
    ).fetchone()
    if not task_row:
        raise NodeNotFoundError(f"Task not found: {task_id}")

    task = {
        "id": task_row[0],
        "label": task_row[1],
        "type": task_row[2],
        "status": task_row[3],
        "assigned_to": task_row[4],
        "expected_claim": task_row[5],
        "success_criteria": task_row[6],
        "outcome": task_row[7],
        "outcome_notes": task_row[8],
        "created_by": task_row[9],
        "created_at": str(task_row[10]) if task_row[10] else None,
        "due_date": str(task_row[11]) if task_row[11] else None,
        "priority": task_row[12],
        "confidence": task_row[13],
    }

    # Fetch 2-hop subgraph using the existing neighborhood query
    subgraph_edges = query_neighborhood(conn, task_id, depth=2)

    # Extract unique node IDs from the subgraph edges
    subgraph_node_ids = {task_id}
    for e in subgraph_edges:
        fn = e.get("from_node")
        tn = e.get("to_node")
        if fn:
            subgraph_node_ids.add(fn)
        if tn:
            subgraph_node_ids.add(tn)

    # Fetch node info for all nodes in the subgraph
    subgraph_nodes = []
    if subgraph_node_ids:
        placeholders = ",".join(["?"] * len(subgraph_node_ids))
        node_rows = conn.execute(
            f"""SELECT id, label, type, confidence, created_by
                FROM ohm_nodes
                WHERE id IN ({placeholders}) AND deleted_at IS NULL""",
            list(subgraph_node_ids),
        ).fetchall()
        subgraph_nodes = [{"id": r[0], "label": r[1], "type": r[2], "confidence": r[3], "created_by": r[4]} for r in node_rows]

    # Rationale: trace back through DECISION_DEPENDS_ON, DERIVES_FROM,
    # REFERENCES, SUPPORTS edges to find the reasoning chain
    rationale_types = ("DECISION_DEPENDS_ON", "DERIVES_FROM", "REFERENCES", "SUPPORTS", "TESTS")
    rationale_rows = conn.execute(
        f"""SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_by,
                  nf.label AS from_label, nf.type AS from_type,
                  nt.label AS to_label, nt.type AS to_type
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node AND nf.deleted_at IS NULL
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node AND nt.deleted_at IS NULL
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.edge_type IN ({",".join(["?"] * len(rationale_types))})
             AND e.deleted_at IS NULL
           ORDER BY e.confidence DESC
           LIMIT 20""",
        [task_id, task_id, *rationale_types],
    ).fetchall()

    rationale = [
        {
            "edge_id": row[0],
            "from_node": {"id": row[1], "label": row[7], "type": row[8]},
            "to_node": {"id": row[2], "label": row[9], "type": row[10]},
            "edge_type": row[3],
            "layer": row[4],
            "confidence": row[5],
            "created_by": row[6],
        }
        for row in rationale_rows
    ]

    # Blocking: find other task nodes that block this one via DEPENDS_ON or
    # BLOCKED_BY edges, or tasks that this task DEPENDS_ON
    blocking_rows = conn.execute(
        """SELECT b.id, b.label, b.task_status, b.assigned_to,
                  e.edge_type, e.confidence
           FROM ohm_edges e
           JOIN ohm_nodes b ON b.id = CASE WHEN e.from_node = ? THEN e.to_node ELSE e.from_node END
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND b.type = 'task'
             AND b.id != ?
             AND b.deleted_at IS NULL
             AND e.deleted_at IS NULL
             AND e.edge_type IN ('DEPENDS_ON', 'BLOCKED_BY', 'BLOCKS')
           ORDER BY b.task_status""",
        [task_id, task_id, task_id, task_id],
    ).fetchall()

    blocking = [
        {
            "task_id": row[0],
            "label": row[1],
            "status": row[2],
            "assigned_to": row[3],
            "edge_type": row[4],
            "confidence": row[5],
        }
        for row in blocking_rows
    ]

    # Expected outcome: use success_criteria if available, otherwise derive
    expected_outcome = task["success_criteria"]
    if not expected_outcome and task["expected_claim"]:
        expected_outcome = f"Verify claim: {task['expected_claim']}"

    return {
        "task": task,
        "subgraph": {
            "nodes": subgraph_nodes,
            "edges": subgraph_edges,
        },
        "rationale": rationale,
        "expected_outcome": expected_outcome,
        "blocking": blocking,
        "blocked_by_count": len(blocking),
    }


# ── Confidence Report (OHM-q9rt.5) ──────────────────────────────────────────


def query_confidence_report(
    conn: DuckDBPyConnection,
    *,
    agent_name: str,
    since: str | None = None,
) -> dict[str, Any]:
    """Per-agent confidence report — which beliefs have shifted and why (OHM-q9rt.5).

    Shows which of the agent's edges had confidence changes since a timestamp,
    with the reason for each shift. Complements /changes (what's new) by
    showing what CHANGED in the agent's existing portfolio.

    Args:
        conn: Database connection.
        agent_name: Agent whose beliefs to report on.
        since: ISO 8601 timestamp. Falls back to agent's last_sync then 30d ago.

    Returns:
        Dict with:
          - agent, since, query_timestamp
          - shifted_beliefs: edges whose confidence changed (updated_at > since),
              each with edge_id, from/to, edge_type, confidence, delta (vs
              original created_at confidence), and reason
          - new_beliefs: edges the agent created since `since`
          - stale_beliefs: agent's edges with confidence < 0.15 (approaching zero)
          - summary: counts
    """
    from ohm.validation import validate_identifier, validate_timestamp
    from datetime import datetime, timedelta, timezone

    agent_name = validate_identifier(agent_name, name="agent_name")

    # Resolve since
    since_clean = since
    if since_clean:
        since_clean = validate_timestamp(since_clean)
    else:
        try:
            row = conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                [agent_name],
            ).fetchone()
            if row and row[0]:
                since_clean = str(row[0])
        except Exception:
            pass
    if not since_clean:
        since_clean = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    now_str = None
    try:
        now_str = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
    except Exception:
        now_str = datetime.now(timezone.utc).isoformat()

    # 1. Shifted beliefs: agent's edges whose updated_at > since AND
    #    updated_at != created_at (i.e., confidence was modified after creation)
    shifted_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence AS current_confidence,
                  e.created_at, e.updated_at, e.updated_by,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.updated_at IS NOT NULL
             AND e.updated_at > ?::TIMESTAMP
             AND e.updated_at != e.created_at
           ORDER BY e.updated_at DESC
           LIMIT 100""",
        [agent_name, since_clean],
    ).fetchall()

    # Determine reason for each shift
    # Check if a CHALLENGED_BY edge was created targeting this edge since `since`
    challenge_map: dict[str, str] = {}
    if shifted_rows:
        edge_ids = [r[0] for r in shifted_rows]
        placeholders = ",".join(["?"] * len(edge_ids))
        chal_rows = conn.execute(
            f"""SELECT c.challenge_of, c.created_by, c.created_at
               FROM ohm_edges c
               WHERE c.challenge_type = 'CHALLENGED_BY'
                 AND c.challenge_of IN ({placeholders})
                 AND c.created_at > ?::TIMESTAMP
                 AND c.deleted_at IS NULL""",
            [*edge_ids, since_clean],
        ).fetchall()
        for row in chal_rows:
            challenge_map[row[0]] = f"challenged by {row[1]}"

    # Check if an outcome was recorded for the edge's source node since `since`
    outcome_map: dict[str, str] = {}
    if shifted_rows:
        outcome_rows = conn.execute(
            """SELECT DISTINCT o.claim_node
               FROM ohm_outcomes o
               WHERE o.recorded_at > ?::TIMESTAMP""",
            [since_clean],
        ).fetchall()
        outcome_nodes = {r[0] for r in outcome_rows if r[0]}
        for row in shifted_rows:
            if row[1] in outcome_nodes:
                outcome_map[row[0]] = "outcome recorded"

    shifted_beliefs = []
    for row in shifted_rows:
        eid, from_node, to_node, edge_type, layer, current_conf, created_at, updated_at, updated_by, from_label, to_label = row

        # Delta: compare current to created_at confidence (approximate)
        # We don't store old confidence, so we note the updated_by and time
        reason = challenge_map.get(eid) or outcome_map.get(eid) or "confidence updated"

        shifted_beliefs.append(
            {
                "edge_id": eid,
                "from_node": from_node,
                "from_label": from_label,
                "to_node": to_node,
                "to_label": to_label,
                "edge_type": edge_type,
                "layer": layer,
                "current_confidence": current_conf,
                "updated_at": str(updated_at) if updated_at else None,
                "updated_by": updated_by,
                "reason": reason,
            }
        )

    # 2. New beliefs: edges the agent created since `since`
    new_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_at,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.created_at > ?::TIMESTAMP
           ORDER BY e.created_at DESC
           LIMIT 100""",
        [agent_name, since_clean],
    ).fetchall()

    new_beliefs = [
        {
            "edge_id": row[0],
            "from_node": row[1],
            "from_label": row[7],
            "to_node": row[2],
            "to_label": row[8],
            "edge_type": row[3],
            "layer": row[4],
            "confidence": row[5],
            "created_at": str(row[6]) if row[6] else None,
        }
        for row in new_rows
    ]

    # 3. Stale beliefs: agent's edges with confidence < 0.15
    stale_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.confidence < 0.15
           ORDER BY e.confidence ASC
           LIMIT 50""",
        [agent_name],
    ).fetchall()

    stale_beliefs = [
        {
            "edge_id": row[0],
            "from_node": row[1],
            "from_label": row[5],
            "to_node": row[2],
            "to_label": row[6],
            "edge_type": row[3],
            "confidence": row[4],
        }
        for row in stale_rows
    ]

    return {
        "agent": agent_name,
        "since": since_clean,
        "query_timestamp": now_str,
        "shifted_beliefs": shifted_beliefs,
        "new_beliefs": new_beliefs,
        "stale_beliefs": stale_beliefs,
        "summary": {
            "shifted": len(shifted_beliefs),
            "new": len(new_beliefs),
            "stale": len(stale_beliefs),
        },
    }
