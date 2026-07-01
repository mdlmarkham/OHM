"""Decision node recommendation logic.

Links decision nodes to hypotheses via DECISION_DEPENDS_ON edges and
re-evaluates the best action when supporting hypotheses change status.
"""

from __future__ import annotations

import json
from typing import Any


def evaluate_decision(conn, decision_id: str) -> dict[str, Any]:
    """Return a recommendation for a decision node.

    Args:
        conn: DuckDB connection.
        decision_id: The decision node ID.

    Returns:
        Dict with current_best_action, action_alternatives, confidence,
        and key_assumptions (linked hypotheses with statuses).
    """
    decision = conn.execute(
        "SELECT id, label, type, current_best_action, action_alternatives, utility_scale, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Decision node {decision_id} not found")

    _id, label, node_type, current_best_action, action_alternatives_json, utility_scale, decision_conf = decision
    if node_type != "decision":
        raise ValueError(f"Node {decision_id} is not a decision (type={node_type})")

    action_alternatives = json.loads(action_alternatives_json) if action_alternatives_json else []

    assumptions = conn.execute(
        """
        SELECT n.id, n.label, n.type, n.hypothesis_status, e.confidence
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        ORDER BY e.confidence DESC
        """,
        [decision_id],
    ).fetchall()

    assumptions_list = [
        {
            "id": row[0],
            "label": row[1],
            "type": row[2],
            "hypothesis_status": row[3],
            "confidence": row[4],
        }
        for row in assumptions
    ]

    # Confidence combines the node's own confidence with the average support of verified/tested assumptions.
    # Pruned assumptions reduce confidence; missing assumptions default to 0.5.
    if assumptions_list:
        scores = []
        for a in assumptions_list:
            status = a.get("hypothesis_status")
            if status == "verified":
                scores.append(1.0)
            elif status == "pruned":
                scores.append(0.0)
            elif status == "tested":
                scores.append(0.5)
            else:
                scores.append(0.5)
        avg_support = sum(scores) / len(scores)
        confidence = round((decision_conf or 1.0) * avg_support, 4)
    else:
        confidence = round(decision_conf or 1.0, 4)

    confidence = max(0.0, min(1.0, confidence))

    best_action = current_best_action
    if best_action is None and action_alternatives:
        best_action = action_alternatives[0]

    _utility_scale_map = {1.0: "best", 0.5: "neutral", 0.0: "worst"}
    utility_scale_str = _utility_scale_map.get(utility_scale, utility_scale)

    return {
        "decision_id": decision_id,
        "label": label,
        "current_best_action": best_action,
        "action_alternatives": action_alternatives,
        "confidence": confidence,
        "key_assumptions": assumptions_list,
        "utility_scale": utility_scale_str,
    }


def _choose_best_action(conn, decision_id: str, action_alternatives: list[str]) -> str | None:
    """Pick the best action alternative based on supporting assumption status.

    Heuristic mapping from hypothesis status to action:
      - verified  -> prefer "positive" actions (build/launch/go/yes/do)
      - pruned    -> prefer "negative" actions (kill/drop/abandon/stop/no/nothing)
      - otherwise -> keep current first alternative
    """
    if not action_alternatives:
        return None

    assumptions = conn.execute(
        """
        SELECT n.hypothesis_status
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchall()

    statuses = {status for (status,) in assumptions if status}
    [a.lower() for a in action_alternatives]

    if statuses == {"verified"}:
        for action in action_alternatives:
            if any(p in action.lower() for p in ("build", "launch", "go", "yes", "do", "feature")):
                return action
        return action_alternatives[0]

    if "pruned" in statuses:
        for action in action_alternatives:
            if any(n in action.lower() for n in ("kill", "drop", "abandon", "stop", "no", "nothing")):
                return action
        return action_alternatives[-1]

    return action_alternatives[0]


def recompute_linked_decisions(conn, hypothesis_id: str) -> list[dict[str, Any]]:
    """Re-evaluate all decision nodes that depend on the given hypothesis.

    Should be called after a hypothesis status changes (verified/pruned/tested)
    via verification outcome or decay.

    Returns:
        List of updated decisions with old and new current_best_action.
    """
    decisions = conn.execute(
        """
        SELECT DISTINCT n.id, n.current_best_action, n.action_alternatives
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [hypothesis_id],
    ).fetchall()

    updates = []
    for row in decisions:
        decision_id, old_action, alternatives_json = row
        alternatives = json.loads(alternatives_json) if alternatives_json else []
        new_action = _choose_best_action(conn, decision_id, alternatives)
        if new_action != old_action:
            conn.execute(
                "UPDATE ohm_nodes SET current_best_action = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [new_action, decision_id],
            )
            from ohm.queries import _log_change

            _log_change(conn, "ohm_nodes", decision_id, "UPDATE", "system")
        updates.append(
            {
                "decision_id": decision_id,
                "old_action": old_action,
                "new_action": new_action,
            }
        )
    return updates
