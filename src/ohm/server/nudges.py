"""Cognitive nudges — enrich write responses with guidance for agents.

When an agent writes to OHM, the system responds not just with the created object
but with contextual nudges that encourage deeper thinking: pattern detection,
challenge prompts, source citation reminders, PERT estimation suggestions,
and cluster synthesis opportunities.

ADR-017: Cognitive Nudge Architecture
"""

from __future__ import annotations

from typing import Any


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

    Returns:
        List of nudge dicts with "type", "message", and optional "data"
    """
    nudges = []

    # ── Source citation nudge ────────────────────────────────────────────
    if action == "observation" and not source_url and provenance != "pattern_analysis":
        nudges.append({
            "type": "source_citation",
            "message": "No source_url on this observation. If this is based on external information, "
                        "add source_url to enable L2 citation tracking and source reliability scoring.",
            "severity": "hint",
        })

    # ── PERT estimation nudge ─────────────────────────────────────────────
    if action == "edge" and confidence is not None:
        nudges.append({
            "type": "pert_estimation",
            "message": f"Confidence {confidence:.2f} recorded. Consider providing PERT estimates "
                        f"(p05/p50/p95) for calibrated inference. What range of probabilities "
                        f"would surprise you?",
            "severity": "hint",
        })

    # ── Challenge nudge ──────────────────────────────────────────────────
    if action == "edge" and edge_type in ("CAUSES", "SUPPORTS", "INFLUENCES"):
        nudges.append({
            "type": "challenge_reminder",
            "message": f"{edge_type} edge created. Challenge ratio is 2.4% — most L3 interpretations "
                        f"are unchallenged. Consider: does this causal claim have evidence against it? "
                        f"Is the direction correct? Could this be an AND→OR conversion?",
            "severity": "hint",
        })

    # ── AND→OR pattern nudge ─────────────────────────────────────────────
    if tags and any(t in ("and-or", "AND-OR", "governance", "security", "ontology")
                    for t in (tags or [])):
        nudges.append({
            "type": "pattern_detection",
            "message": "AND→OR pattern tag detected. Ask: Is this an AND-gate or OR-gate? "
                        "Which direction does it operate? AND-gates trap when enabling contradictory "
                        "truths; AND-gates escape when constraining anti-democratic processing.",
            "severity": "info",
        })

    # ── Cluster synthesis nudge ───────────────────────────────────────────
    if neighborhood and action == "node":
        nodes_in_hood = neighborhood.get("nodes", {})
        edges_in_hood = neighborhood.get("edges", [])
        unchallenged = [e for e in edges_in_hood
                        if e.get("edge_type") in ("CAUSES", "SUPPORTS", "INFLUENCES")
                        and e.get("challenge_of") is None]
        if len(nodes_in_hood) >= 3 and len(unchallenged) >= 2:
            nudges.append({
                "type": "cluster_synthesis",
                "message": f"This node has {len(nodes_in_hood)} neighbors with {len(unchallenged)} "
                            f"unchallenged causal edges. Consider writing a synthesis connecting these "
                            f"into a pattern. When 3+ nodes share a theme, the pattern is often more "
                            f"important than any single node.",
                "severity": "suggestion",
                "data": {"neighbor_count": len(nodes_in_hood),
                         "unchallenged_causal_edges": len(unchallenged)},
            })

    # ── Confidence outlier nudge ─────────────────────────────────────────
    if confidence is not None and neighborhood:
        edges_in_hood = neighborhood.get("edges", [])
        similar_edges = [e for e in edges_in_hood
                         if e.get("edge_type") == edge_type
                         and e.get("confidence") is not None]
        if len(similar_edges) >= 2:
            avg_conf = sum(e["confidence"] for e in similar_edges) / len(similar_edges)
            if abs(confidence - avg_conf) > 0.15:
                direction = "higher" if confidence > avg_conf else "lower"
                nudges.append({
                    "type": "confidence_outlier",
                    "message": f"Your confidence ({confidence:.2f}) is {direction} than the "
                                f"neighborhood average ({avg_conf:.2f}) for {edge_type} edges. "
                                f"What evidence supports this {direction} confidence?",
                    "severity": "info",
                    "data": {"your_confidence": confidence,
                             "neighborhood_avg": round(avg_conf, 3),
                             "direction": direction},
                })

    # ── Babel insight nudge (rare, for L3 interpretations) ──────────────
    if provenance == "pattern_analysis" and action == "edge":
        nudges.append({
            "type": "babel_insight",
            "message": "You're writing an L3 interpretation. Remember: the graph values navigable "
                        "plurality, not convergence. If another agent might see this differently, "
                        "that divergence is data, not error.",
            "severity": "info",
        })

    return nudges


def enrich_response(response: dict, nudges: list[dict]) -> dict:
    """Add nudges to an API response without breaking existing clients."""
    if nudges:
        response["nudges"] = nudges
    return response