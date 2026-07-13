"""Decision-node hypothesis autoresearch loop (OHM-845).

Applies the generator→executor→evaluator→promotion pattern to decision
nodes so they can self-improve their linked hypotheses.

The loop:
1. **Generator**: Propose candidate hypothesis edges (tag-overlap and
   label-word-overlap strategies; semantic-similarity deferred to v2
   since it needs Ollama).
2. **Evaluator**: For each candidate, insert a temporary
   DECISION_DEPENDS_ON edge inside a transaction, call
   ``evaluate_decision()`` unmodified, then roll back. If the
   recommendation quality improves, the candidate is promoted.
3. **Promotion**: Persist the candidate edge and record it in
   ``ohm_autoresearch_history`` to prevent re-proposal.

Ollama chat-completion candidate generation is deferred to v2 (no
chat plumbing exists in this codebase yet).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _rows_to_dicts


def _get_tags(conn: "DuckDBPyConnection", node_id: str) -> set[str]:
    """Extract tags from a node as a set."""
    row = conn.execute(
        "SELECT tags FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]
    ).fetchone()
    if not row or not row[0]:
        return set()
    try:
        tags = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or [])
        if isinstance(tags, list):
            return set(tags)
    except (json.JSONDecodeError, TypeError):
        pass
    return set()


def _get_label_words(conn: "DuckDBPyConnection", node_id: str) -> set[str]:
    """Extract words from a node's label."""
    row = conn.execute(
        "SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]
    ).fetchone()
    if not row or not row[0]:
        return set()
    return set(row[0].lower().split())


def _would_create_cycle(
    conn: "DuckDBPyConnection",
    from_node: str,
    to_node: str,
    max_depth: int = 20,
) -> bool:
    """Check if adding from_node→to_node would create a cycle.

    Traverses outgoing edges from to_node; if from_node is reachable,
    adding the edge would close a cycle.
    """
    visited: set[str] = set()
    stack = [to_node]
    while stack and len(visited) < max_depth:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        if current == from_node:
            return True
        rows = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL",
            [current],
        ).fetchall()
        for (child,) in rows:
            if child == from_node:
                return True
            if child not in visited:
                stack.append(child)
    return False


def _has_higher_confidence_challenge(
    conn: "DuckDBPyConnection",
    node_id: str,
) -> bool:
    """Check if node has a CHALLENGED_BY edge with higher confidence than the node itself."""
    node_row = conn.execute(
        "SELECT confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]
    ).fetchone()
    node_conf = node_row[0] if node_row and node_row[0] is not None else 1.0

    challenge_rows = conn.execute(
        """SELECT e.confidence FROM ohm_edges e
           WHERE e.edge_type = 'CHALLENGED_BY' AND e.to_node = ? AND e.deleted_at IS NULL""",
        [node_id],
    ).fetchall()
    for (conf,) in challenge_rows:
        if conf is not None and conf > node_conf:
            return True
    return False


def _get_existing_edge_targets(
    conn: "DuckDBPyConnection",
    from_node: str,
    edge_type: str = "DECISION_DEPENDS_ON",
) -> set[str]:
    """Get the set of node ids already linked via a specific edge type."""
    rows = conn.execute(
        "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = ? AND deleted_at IS NULL",
        [from_node, edge_type],
    ).fetchall()
    return {r[0] for r in rows}


def _get_rejected_candidates(
    conn: "DuckDBPyConnection",
    decision_id: str,
) -> set[str]:
    """Get the set of hypothesis node ids previously rejected for a decision."""
    rows = _rows_to_dicts(conn.execute(
        "SELECT hypothesis_id FROM ohm_autoresearch_history WHERE decision_id = ? AND outcome = 'rejected'",
        [decision_id],
    ))
    return {r.get("hypothesis_id") for r in rows if r.get("hypothesis_id")}


def _record_history(
    conn: "DuckDBPyConnection",
    *,
    decision_id: str,
    hypothesis_id: str,
    outcome: str,
    reason: str | None = None,
) -> None:
    """Record an autoresearch evaluation outcome."""
    conn.execute(
        """INSERT INTO ohm_autoresearch_history (decision_id, hypothesis_id, outcome, reason)
           VALUES (?, ?, ?, ?)""",
        [decision_id, hypothesis_id, outcome, reason or ""],
    )


def propose_hypothesis_edges(
    conn: "DuckDBPyConnection",
    *,
    decision_id: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Propose candidate hypothesis edges for a decision node.

    Uses tag-overlap and label-word-overlap strategies. Excludes:
    - Nodes already linked via DECISION_DEPENDS_ON
    - Nodes previously rejected (ohm_autoresearch_history)
    - Nodes with higher-confidence CHALLENGED_BY edges (guard)
    - Edges that would create cycles

    Args:
        conn: Database connection.
        decision_id: The decision node to find hypotheses for.
        limit: Maximum candidates to return.

    Returns:
        List of candidate dicts with hypothesis_id, label, type, score,
        strategy, and shared_tags.
    """
    decision_row = conn.execute(
        "SELECT id, label, type, tags FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision_row:
        raise ValueError(f"Decision {decision_id!r} not found")
    if decision_row[2] != "decision":
        raise ValueError(f"Node {decision_id!r} is type {decision_row[2]!r}, not 'decision'")

    decision_tags = set()
    if decision_row[3]:
        try:
            parsed = json.loads(decision_row[3]) if isinstance(decision_row[3], str) else (decision_row[3] or [])
            if isinstance(parsed, list):
                decision_tags = set(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    decision_words = set(decision_row[1].lower().split()) if decision_row[1] else set()

    existing = _get_existing_edge_targets(conn, decision_id)
    rejected = _get_rejected_candidates(conn, decision_id)

    candidates_by_id: dict[str, dict[str, Any]] = {}

    candidate_rows = _rows_to_dicts(conn.execute(
        """SELECT id, label, type, tags, confidence FROM ohm_nodes
           WHERE type IN ('pattern', 'idea', 'concept', 'source', 'event')
             AND id != ? AND deleted_at IS NULL
           ORDER BY created_at DESC LIMIT 500""",
        [decision_id],
    ))

    for row in candidate_rows:
        nid = row["id"]
        if nid in existing or nid in rejected:
            continue
        if _has_higher_confidence_challenge(conn, nid):
            continue
        if _would_create_cycle(conn, decision_id, nid):
            continue

        cand_tags = set()
        raw_tags = row.get("tags")
        if raw_tags:
            try:
                parsed = json.loads(raw_tags) if isinstance(raw_tags, str) else (raw_tags or [])
                if isinstance(parsed, list):
                    cand_tags = set(parsed)
            except (json.JSONDecodeError, TypeError):
                pass

        tag_overlap = len(decision_tags & cand_tags)
        cand_words = set(row.get("label", "").lower().split()) if row.get("label") else set()
        word_overlap = len(decision_words & cand_words)

        tag_score = tag_overlap * 2.0
        word_score = word_overlap * 1.0
        total = tag_score + word_score
        if total == 0:
            continue

        strategy = "tag_overlap" if tag_overlap > 0 else "label_word_overlap"

        candidates_by_id[nid] = {
            "hypothesis_id": nid,
            "label": row.get("label", ""),
            "type": row.get("type", ""),
            "score": round(total, 2),
            "strategy": strategy,
            "shared_tags": sorted(decision_tags & cand_tags) if tag_overlap > 0 else [],
        }

    return sorted(candidates_by_id.values(), key=lambda c: c["score"], reverse=True)[:limit]


def evaluate_candidate_edge(
    conn: "DuckDBPyConnection",
    *,
    decision_id: str,
    hypothesis_id: str,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Evaluate a candidate hypothesis edge via transaction-insert-then-rollback.

    Inserts a temporary DECISION_DEPENDS_ON edge, calls evaluate_decision()
    unmodified, then rolls back. Compares the recommendation quality before
    and after.

    Args:
        conn: Database connection.
        decision_id: The decision node.
        hypothesis_id: The candidate hypothesis node.
        confidence: Edge confidence (default 0.7).

    Returns:
        Dict with before/after confidence, improved (bool), and the
        full before/after recommendation.
    """
    from ohm.decision.recommendation import evaluate_decision

    before = evaluate_decision(conn, decision_id)

    try:
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            """INSERT INTO ohm_edges (from_node, to_node, edge_type, layer, confidence, created_by, created_at)
               VALUES (?, ?, 'DECISION_DEPENDS_ON', 'L3', ?, 'autoresearch', CURRENT_TIMESTAMP)""",
            [decision_id, hypothesis_id, confidence],
        )
        after = evaluate_decision(conn, decision_id)
        conn.execute("ROLLBACK")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    before_conf = before.get("confidence", 0)
    after_conf = after.get("confidence", 0)
    improved = after_conf > before_conf

    return {
        "decision_id": decision_id,
        "hypothesis_id": hypothesis_id,
        "before": before,
        "after": after,
        "before_confidence": before_conf,
        "after_confidence": after_conf,
        "improved": improved,
    }


def promote_candidate_edge(
    conn: "DuckDBPyConnection",
    *,
    decision_id: str,
    hypothesis_id: str,
    confidence: float = 0.7,
    agent: str = "autoresearch",
) -> dict[str, Any]:
    """Promote a candidate hypothesis edge — persist it permanently.

    Args:
        conn: Database connection.
        decision_id: The decision node.
        hypothesis_id: The hypothesis node to link.
        confidence: Edge confidence.
        agent: Agent name for the edge.

    Returns:
        Dict with the created edge and updated recommendation.
    """
    from ohm.graph.queries import create_edge
    from ohm.decision.recommendation import evaluate_decision

    edge = create_edge(
        conn,
        from_node=decision_id,
        to_node=hypothesis_id,
        edge_type="DECISION_DEPENDS_ON",
        layer="L3",
        confidence=confidence,
        created_by=agent,
    )

    recommendation = evaluate_decision(conn, decision_id)

    _record_history(
        conn,
        decision_id=decision_id,
        hypothesis_id=hypothesis_id,
        outcome="promoted",
        reason=f"Confidence improved to {recommendation.get('confidence', 0)}",
    )

    return {
        "edge": edge,
        "recommendation": recommendation,
    }


def run_autoresearch_round(
    conn: "DuckDBPyConnection",
    *,
    decision_id: str,
    dry_run: bool = False,
    max_candidates: int = 5,
    agent: str = "autoresearch",
) -> dict[str, Any]:
    """Run one full autoresearch round for a decision node.

    Generates candidates, evaluates each, and promotes any that improve
    the recommendation. In dry_run mode, evaluates but never persists.

    Args:
        conn: Database connection.
        decision_id: The decision node to improve.
        dry_run: If True, evaluate candidates but don't persist edges.
        max_candidates: Maximum candidates to evaluate.
        agent: Agent name for created edges.

    Returns:
        Dict with candidates, evaluations, promotions, and rejections.
    """
    candidates = propose_hypothesis_edges(conn, decision_id=decision_id, limit=max_candidates)

    if not candidates:
        return {
            "decision_id": decision_id,
            "candidates": [],
            "evaluations": [],
            "promotions": [],
            "rejections": [],
            "dry_run": dry_run,
            "message": "No candidates found",
        }

    evaluations = []
    promotions = []
    rejections = []

    for cand in candidates:
        hid = cand["hypothesis_id"]
        eval_result = evaluate_candidate_edge(
            conn,
            decision_id=decision_id,
            hypothesis_id=hid,
        )
        evaluations.append(eval_result)

        if eval_result["improved"] and not dry_run:
            promo = promote_candidate_edge(
                conn,
                decision_id=decision_id,
                hypothesis_id=hid,
                agent=agent,
            )
            promotions.append({
                "hypothesis_id": hid,
                "label": cand["label"],
                "edge": promo["edge"],
                "new_confidence": promo["recommendation"].get("confidence", 0),
            })
        elif not eval_result["improved"]:
            if not dry_run:
                _record_history(
                    conn,
                    decision_id=decision_id,
                    hypothesis_id=hid,
                    outcome="rejected",
                    reason=f"Confidence did not improve ({eval_result['before_confidence']} → {eval_result['after_confidence']})",
                )
            rejections.append({
                "hypothesis_id": hid,
                "label": cand["label"],
                "before_confidence": eval_result["before_confidence"],
                "after_confidence": eval_result["after_confidence"],
            })

    return {
        "decision_id": decision_id,
        "candidates": candidates,
        "evaluations": evaluations,
        "promotions": promotions,
        "rejections": rejections,
        "dry_run": dry_run,
        "summary": {
            "candidates_evaluated": len(evaluations),
            "promoted": len(promotions),
            "rejected": len(rejections),
        },
    }