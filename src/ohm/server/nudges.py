"""Cognitive nudges — enrich write responses with guidance for agents.

When an agent writes to OHM, the system responds not just with the created object
but with contextual nudges that encourage deeper thinking: pattern detection,
challenge prompts, source citation reminders, PERT estimation suggestions,
cluster synthesis opportunities, causal edge guidance, and inference deltas.

ADR-017: Cognitive Nudge Architecture
ADR-023: Causal Nudge Architecture — steer agents toward Bayesian-tractable writes
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Edge types that flow through the Bayesian network (causal direction)
CAUSAL_EDGE_TYPES = {"CAUSES", "DEPENDS_ON", "BLOCKS", "ENABLES", "INFLUENCES", "THREATENS"}

# Edge types that do NOT flow through the Bayesian network
NON_CAUSAL_EDGE_TYPES = {
    "SUPPORTS",
    "REFERENCES",
    "RELATED_TO",
    "APPLIES_TO",
    "CORRELATES_WITH",
    "CHALLENGED_BY",
    "SYNTHESIZES",
    "CONTEXT_OF",
    "PART_OF",
    "HAS_COMPONENT",
    "CONTAINS",
    "BELONGS_TO",
    "VALUES",
    "GOALS",
    "CAPABLE_OF",
    "DERIVES_FROM",
    "CONVERGES_WITH",
    "PARALLELS",
    "INTERESTED_IN",
    "USES",
    "SERVES",
    "REFINES",
    "EXPLAINS",
    "FEEDS",
    "COMPOUNDS",
}

# Node types that represent decision points for game theory
DECISION_CANDIDATE_TYPES = {"event", "concept"}

# Keywords that suggest a node is a decision/action point
DECISION_KEYWORDS = [
    "strike",
    "attack",
    "respond",
    "retaliate",
    "invade",
    "defend",
    "withdraw",
    "negotiate",
    "concede",
    "escalate",
    "deescalate",
    "sanction",
    "authorize",
    "deploy",
    "launch",
    "halt",
    "resume",
    "impose",
    "lift",
    "ban",
    "approve",
    "reject",
    "veto",
    "sign",
    "cancel",
    "commit",
    "invest",
    "sell",
    "buy",
    "hold",
    "cut",
    "hike",
    "pause",
    "pivot",
    "exercise",
    "option",
]


def _looks_like_decision(node: dict) -> bool:
    """Heuristic: does this node look like a decision point for game theory?"""
    content = (node.get("content") or "").lower()
    label = (node.get("label") or "").lower()
    node_id = (node.get("id") or "").lower()
    node_type = node.get("type", "")

    if node_type == "decision":
        return False  # Already a decision node

    if node_type not in DECISION_CANDIDATE_TYPES:
        return False

    combined = f"{label} {content} {node_id}"
    return any(kw in combined for kw in DECISION_KEYWORDS)


def _get_inference_delta(store: Any, node_id: str, action: str) -> dict | None:
    """Return a lightweight nudge pointing to the inference endpoint.

    ADR-023: Synchronous Bayesian inference in the write path caused 5-6s
    blocking (bayesian_inference() on 50+ node network). DuckDB connections
    are not thread-safe, so offloading to a ThreadPoolExecutor crashes the
    server. Instead, we return a prompt to check /inference manually.

    Returns a dict suggesting the user check /inference for updated probabilities,
    or None if the node_id is not set.
    """
    if store is None or not node_id:
        return None

    # Lightweight: just point the user to the inference endpoint.
    # The full Bayesian computation runs asynchronously when the user
    # queries /inference?target=<node_id>.
    return {
        "node_id": node_id,
        "suggestion": f"Check /inference?target={node_id} for updated Bayesian probabilities after this write.",
    }


def generate_nudges(
    action: str,
    node_id: str | None = None,
    edge_type: str | None = None,
    confidence: float | None = None,
    tags: list[str] | None = None,
    provenance: str | None = None,
    source_url: str | None = None,
    neighborhood: dict | None = None,
    store: Any = None,
    node: dict | None = None,
    from_node_id: str | None = None,
    to_node_id: str | None = None,
    challenge_ratio: float | None = None,
) -> list[dict]:
    """Generate cognitive nudges based on what the agent just wrote.

    Args:
        action: What was written — "node", "edge", "observation", "synthesis"
        node_id: The node ID that was created/updated
        edge_type: The edge type (CAUSES, SUPPORTS, etc.)
        confidence: Confidence value of the edge/observation
        tags: Tags on the node
        provenance: Provenance of the edge
        source_url: Source URL (if any)
        neighborhood: Neighborhood query result for the node
        store: Graph store for additional queries
        node: Full node dict (for decision detection heuristics)
        from_node_id: Source node ID of the edge
        to_node_id: Target node ID of the edge
        challenge_ratio: Current graph challenge ratio (for dynamic nudges)

    Returns:
        List of nudge dicts with "type", "message", and optional "data"
    """
    nudges = []

    # ── ADR-023: Causal edge type nudge ──────────────────────────────────
    if action == "edge" and edge_type and edge_type in NON_CAUSAL_EDGE_TYPES:
        # Check if either endpoint has causal edges already — if so, this
        # non-causal edge won't feed the Bayesian network
        causal_hint = f"{edge_type} edges don't flow through the Bayesian inference network. For causal reasoning (inference, intervention, game theory), use CAUSES, DEPENDS_ON, BLOCKS, ENABLES, or INFLUENCES. Is this a causal relationship?"
        nudges.append(
            {
                "type": "causal_edge_suggestion",
                "message": causal_hint,
                "severity": "info",
                "data": {
                    "edge_type_used": edge_type,
                    "suggested_types": sorted(CAUSAL_EDGE_TYPES),
                    "current_edge_non_causal": True,
                },
            }
        )

    if action == "edge" and edge_type and edge_type in CAUSAL_EDGE_TYPES:
        nudges.append(
            {
                "type": "causal_edge_confirmed",
                "message": f"{edge_type} edge feeds the Bayesian network. This edge will be used by /inference, /intervene, /sensitivity, /voi, and /game.",
                "severity": "info",
            }
        )

    # ── ADR-023: Decision node nudge ──────────────────────────────────────
    if action == "node" and node and _looks_like_decision(node):
        nudges.append(
            {
                "type": "decision_node_suggestion",
                "message": "This node looks like a decision or action point. Consider adding "
                "utility_scale and action_alternatives to enable game theory analysis "
                "(/game, /nash, /policy). Decision nodes with utility_scale are the "
                "entry point for strategic reasoning about the graph.",
                "severity": "suggestion",
                "data": {
                    "suggested_type": "decision",
                    "required_fields": ["utility_scale", "current_best_action", "action_alternatives"],
                    "enabled_endpoints": ["/game", "/nash", "/policy", "/voi"],
                },
            }
        )

    # ── Source citation nudge ────────────────────────────────────────────
    if action == "observation" and not source_url and provenance != "pattern_analysis":
        nudges.append(
            {
                "type": "source_citation",
                "message": "No source_url on this observation. If this is based on external information, add source_url to enable L2 citation tracking and source reliability scoring.",
                "severity": "hint",
            }
        )

    # ── PERT estimation nudge ─────────────────────────────────────────────
    if action == "edge" and confidence is not None:
        nudges.append(
            {
                "type": "pert_estimation",
                "message": f"Confidence {confidence:.2f} recorded. Consider providing PERT estimates (p05/p50/p95) for calibrated inference. What range of probabilities would surprise you?",
                "severity": "hint",
            }
        )

    # ── Challenge nudge (dynamic) ────────────────────────────────────────
    if action == "edge" and edge_type in ("CAUSES", "SUPPORTS", "INFLUENCES"):
        ratio_str = f"{challenge_ratio:.1%}" if challenge_ratio is not None else "low"
        nudges.append(
            {
                "type": "challenge_reminder",
                "message": f"{edge_type} edge created. Challenge ratio is {ratio_str} — most L3 interpretations are unchallenged. Consider: does this causal claim have evidence against it? Is the direction correct? Could this be an AND→OR conversion?",
                "severity": "hint",
            }
        )

    # ── AND→OR pattern nudge ─────────────────────────────────────────────
    if tags and any(t in ("and-or", "AND-OR", "governance", "security", "ontology") for t in (tags or [])):
        nudges.append(
            {
                "type": "pattern_detection",
                "message": "AND→OR pattern tag detected. Ask: Is this an AND-gate or OR-gate? Which direction does it operate? AND-gates trap when enabling contradictory truths; AND-gates escape when constraining anti-democratic processing.",
                "severity": "info",
            }
        )

    # ── Cluster synthesis nudge ───────────────────────────────────────────
    if neighborhood and action == "node":
        nodes_in_hood = neighborhood.get("nodes", {})
        edges_in_hood = neighborhood.get("edges", [])
        unchallenged = [e for e in edges_in_hood if e.get("edge_type") in ("CAUSES", "SUPPORTS", "INFLUENCES") and e.get("challenge_of") is None]
        if len(nodes_in_hood) >= 3 and len(unchallenged) >= 2:
            nudges.append(
                {
                    "type": "cluster_synthesis",
                    "message": f"This node has {len(nodes_in_hood)} neighbors with {len(unchallenged)} "
                    f"unchallenged causal edges. Consider writing a synthesis connecting these "
                    f"into a pattern. When 3+ nodes share a theme, the pattern is often more "
                    f"important than any single node.",
                    "severity": "suggestion",
                    "data": {"neighbor_count": len(nodes_in_hood), "unchallenged_causal_edges": len(unchallenged)},
                }
            )

    # ── Confidence outlier nudge ─────────────────────────────────────────
    if confidence is not None and neighborhood:
        edges_in_hood = neighborhood.get("edges", [])
        similar_edges = [e for e in edges_in_hood if e.get("edge_type") == edge_type and e.get("confidence") is not None]
        if len(similar_edges) >= 2:
            avg_conf = sum(e["confidence"] for e in similar_edges) / len(similar_edges)
            if abs(confidence - avg_conf) > 0.15:
                direction = "higher" if confidence > avg_conf else "lower"
                nudges.append(
                    {
                        "type": "confidence_outlier",
                        "message": f"Your confidence ({confidence:.2f}) is {direction} than the neighborhood average ({avg_conf:.2f}) for {edge_type} edges. What evidence supports this {direction} confidence?",
                        "severity": "info",
                        "data": {"your_confidence": confidence, "neighborhood_avg": round(avg_conf, 3), "direction": direction},
                    }
                )

    # ── Babel insight nudge (rare, for L3 interpretations) ──────────────
    if provenance == "pattern_analysis" and action == "edge":
        nudges.append(
            {
                "type": "babel_insight",
                "message": "You're writing an L3 interpretation. Remember: the graph values navigable plurality, not convergence. If another agent might see this differently, that divergence is data, not error.",
                "severity": "info",
            }
        )

    # ── ADR-023: Inference delta nudge ───────────────────────────────────
    # After writing to a node, show how the Bayesian network shifted
    if action in ("observation", "edge") and node_id and store:
        delta = _get_inference_delta(store, node_id, action)
        if delta and delta.get("probabilities"):
            nudges.append(
                {
                    "type": "inference_delta",
                    "message": f"Bayesian network update for {node_id}: {delta['probabilities']}. Use /inference?target={node_id} for full analysis.",
                    "severity": "info",
                    "data": delta,
                }
            )

    # ── ADR-023: Batch write nudge ────────────────────────────────────────
    if action in ("node", "edge") and store is not None:
        # If this is a single write, suggest batch for multi-write sessions
        nudges.append(
            {
                "type": "batch_suggestion",
                "message": "For multi-write sessions, use POST /batch to create nodes and edges in a single transaction. Faster and atomic — no partial writes.",
                "severity": "hint",
            }
        )

    # ── ADR-023: Contradiction awareness nudge ────────────────────────────
    # After writing an observation, check if it contradicts existing ones
    if action == "observation" and node_id and store:
        try:
            # Quick check: does this node have any CHALLENGED_BY edges?
            rows = store.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE to_node = ? AND edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL",
                [node_id],
            ).fetchone()
            challenge_count = rows[0] if rows else 0
            if challenge_count > 0:
                nudges.append(
                    {
                        "type": "contradiction_alert",
                        "message": f"This node has {challenge_count} challenge(s) from other agents. Your observation may contradict existing interpretations. Consider: does your evidence resolve or deepen the disagreement? Use /contradictions to review.",
                        "severity": "info",
                        "data": {"challenge_count": challenge_count, "node_id": node_id},
                    }
                )
        except Exception:
            pass  # Never fail the write for a nudge

    # ── ADR-026: Semantic edge validation nudge ───────────────────────────
    # OrionBelt pattern: refuse wrong aggregates loudly.
    # Advisory first — warn when edge semantics don't match node types.
    if action == "edge" and edge_type and store is not None:
        # Rule 1: Sources don't cause things — they support or reference them
        if edge_type in ("CAUSES", "DEPENDS_ON", "BLOCKS", "ENABLES", "INFLUENCES", "THREATENS"):
            for nid_param, direction in [("from", "source"), ("to", "target")]:
                nid = from_node_id if nid_param == "from" else to_node_id
                if nid:
                    try:
                        node_row = store.conn.execute(
                            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                            [nid],
                        ).fetchone()
                        if node_row and node_row[0] == "source":
                            direction_label = "Source" if direction == "source" else "Target"
                            nudges.append(
                                {
                                    "type": "semantic_edge_warning",
                                    "message": f"{direction_label} node '{nid}' is type 'source'. Sources {('support' if edge_type == 'DEPENDS_ON' else 'reference')} claims — they don't {edge_type.lower()} outcomes. Consider SUPPORTS or REFERENCES instead.",
                                    "severity": "warning",
                                    "data": {
                                        "edge_type": edge_type,
                                        "node_id": nid,
                                        "node_type": "source",
                                        "suggested_types": ["SUPPORTS", "REFERENCES"],
                                    },
                                }
                            )
                    except Exception:
                        pass  # Never fail the write for a nudge

        # Rule 2: REFERENCES edges should have a URL on the source node (ADR-013)
        # Nodes use 'url' column, not 'source_url' (that's on observations)
        if edge_type == "REFERENCES" and from_node_id:
            try:
                source_row = store.conn.execute(
                    "SELECT url FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [from_node_id],
                ).fetchone()
                if source_row and not source_row[0]:
                    nudges.append(
                        {
                            "type": "semantic_edge_warning",
                            "message": f"REFERENCES edge created, but source node '{from_node_id}' has no URL. L2 citation edges should trace back to an external source (ADR-013). Add url to the source node for proper provenance tracking.",
                            "severity": "hint",
                            "data": {
                                "edge_type": "REFERENCES",
                                "node_id": from_node_id,
                                "missing_field": "url",
                            },
                        }
                    )
            except Exception:
                pass

    return nudges


def enrich_response(response: dict, nudges: list[dict]) -> dict:
    """Add nudges to an API response without breaking existing clients."""
    if nudges:
        response["nudges"] = nudges
    return response
