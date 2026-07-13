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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logger = logging.getLogger(__name__)

# Edge types that flow through the Bayesian network (causal direction)
# OHM-mzyc.1: aligned with actual Bayesian network default edge_types
# in src/ohm/inference/bayesian.py (CAUSES, DEPENDS_ON, THREATENS,
# EXPECTED_LIKELIHOOD, NEGATES). INFLUENCES/BLOCKS/ENABLES are L2/L4
# edges that are semantically causal but NOT used by the inference
# engine by default — the nudge must not claim they feed the network.
CAUSAL_EDGE_TYPES = {"CAUSES", "DEPENDS_ON", "THREATENS", "EXPECTED_LIKELIHOOD", "NEGATES"}

# Edge types that are semantically causal and should have a mechanism.
# This is broader than CAUSAL_EDGE_TYPES (which is only the Bayesian
# network's default edge types) — it includes INFLUENCES, BLOCKS,
# ENABLES which are causal in meaning even if not in the inference
# engine by default (OHM-mzyc.1).
SEMANTICALLY_CAUSAL_EDGE_TYPES = CAUSAL_EDGE_TYPES | {"INFLUENCES", "BLOCKS", "ENABLES"}

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
    # OHM-mzyc.1: these are semantically causal but NOT in the default
    # Bayesian network edge_types. The nudge must not claim they feed
    # /inference, /intervene, etc.
    "INFLUENCES",
    "BLOCKS",
    "ENABLES",
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
    source_tier: str | None = None,
    condition: str | None = None,
    metadata: dict | None = None,
    obs_type: str | None = None,
    half_life_days: float | None = None,
    value: float | None = None,
    value_contradiction_threshold: float = 0.3,
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
        source_tier: Source quality tier (raw, unverified, preliminary, official, verified)
        condition: Edge condition field (if set, indicates a mechanism was specified)
        metadata: Edge or node metadata dict (may contain 'mechanism' key)
        obs_type: Observation type (for decay-relevant types like 'measurement')
        half_life_days: Observation decay half-life (if set, indicates perishable data)
        value: The numeric value of the new observation (for contradiction detection)
        value_contradiction_threshold: Minimum |gap| between new and existing observation
            values to fire the value_contradiction nudge (default 0.3)

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

    # ── OHM-tsxk: Pattern-to-case causal edge nudge ─────────────────────
    # When a pattern/concept/idea node is linked to a decision/task/event/case
    # with a CAUSES edge, warn that REFINES or EXPLAINS is more appropriate.
    if action == "edge" and edge_type == "CAUSES" and from_node_id and to_node_id and store:
        try:
            from_row = store.conn.execute(
                "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [from_node_id],
            ).fetchone()
            to_row = store.conn.execute(
                "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [to_node_id],
            ).fetchone()
            if from_row and to_row:
                from_type = from_row[0]
                to_type = to_row[0]
                PATTERN_TYPES = {"pattern", "idea", "synthesis", "interpretation", "concept"}
                CASE_TYPES = {"decision", "task", "event", "action", "intervention"}
                if from_type in PATTERN_TYPES and to_type in CASE_TYPES:
                    nudges.append(
                        {
                            "type": "pattern_to_causal_warning",
                            "message": (
                                f"CAUSES edge from {from_type}→{to_type} may be mistyped. "
                                f"A pattern refines or explains a {to_type}, it doesn't cause it. "
                                f"Consider using REFINES or EXPLAINS instead. "
                                f"Use GET /edge/suggest-type?from={from_node_id}&to={to_node_id} for guidance."
                            ),
                            "severity": "warning",
                            "data": {
                                "from_type": from_type,
                                "to_type": to_type,
                                "edge_type_used": "CAUSES",
                                "suggested_types": ["REFINES", "EXPLAINS"],
                            },
                        }
                    )
                elif from_type in PATTERN_TYPES and to_type in {"concept", "source", "fragment"}:
                    nudges.append(
                        {
                            "type": "pattern_to_causal_warning",
                            "message": (
                                f"CAUSES edge from {from_type}→{to_type} may be mistyped. A pattern explains a {to_type}, it doesn't cause it. Consider using EXPLAINS instead. Use GET /edge/suggest-type?from={from_node_id}&to={to_node_id} for guidance."
                            ),
                            "severity": "warning",
                            "data": {
                                "from_type": from_type,
                                "to_type": to_type,
                                "edge_type_used": "CAUSES",
                                "suggested_types": ["EXPLAINS", "RELATED_TO"],
                            },
                        }
                    )
        except Exception:
            pass  # nudge is advisory; never block the write

    # ── OHM-bm5r: Mechanism gate for causal edges ───────────────────────
    # When a CAUSES/INFLUENCES/DEPENDS_ON edge is created without a condition
    # (mediating mechanism), warn the agent to provide one.
    if action == "edge" and edge_type and edge_type in SEMANTICALLY_CAUSAL_EDGE_TYPES and not condition and not (metadata and metadata.get("mechanism")):
        nudges.append(
            {
                "type": "mechanism_gate",
                "message": (
                    f"{edge_type} edge created without a stated mechanism. "
                    f"Causal claims without a mediating pathway are not falsifiable — "
                    f"they cannot be verified or refuted. Provide a 'condition' field "
                    f"describing the causal pathway (e.g., 'via increased insulin resistance') "
                    f"or set metadata.mechanism. This enables future verification and "
                    f"prevents the edge from compounding into unearned certainty."
                ),
                "severity": "warning",
                "data": {
                    "edge_type": edge_type,
                    "has_condition": False,
                    "has_mechanism_metadata": False,
                },
            }
        )

    if action == "edge" and edge_type and edge_type in SEMANTICALLY_CAUSAL_EDGE_TYPES:
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

    # ── Bare decision node nudge (OHM-839) ────────────────────────────────
    if action == "node" and node and node.get("type") == "decision":
        missing_fields = []
        if not node.get("utility_scale"):
            missing_fields.append("utility_scale")
        if not node.get("action_alternatives"):
            missing_fields.append("action_alternatives")
        has_dep_edges = False
        if neighborhood:
            for e in neighborhood.get("edges", []):
                if e.get("edge_type") == "DECISION_DEPENDS_ON":
                    has_dep_edges = True
                    break
        if not has_dep_edges:
            missing_fields.append("DECISION_DEPENDS_ON edge")
        if missing_fields:
            nudges.append(
                {
                    "type": "decision_node_incomplete",
                    "message": f"Decision node is missing: {', '.join(missing_fields)}. "
                    "Add these to enable /recommendation, /voi, and /game analysis. "
                    "Link hypothesis nodes via DECISION_DEPENDS_ON (L3) to let "
                    "evaluate_decision() score your alternatives.",
                    "severity": "suggestion",
                    "skill_uri": "skill://ohm/decision-node/SKILL.md",
                    "data": {
                        "missing": missing_fields,
                        "enabled_endpoints": ["/decision/{id}/recommendation", "/voi", "/game"],
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

        # ── OHM-jdfq: Value-contradiction nudge (OHM-ag92) ──────────────────
        # When a new observation's value disagrees with existing observations
        # on the same node by more than the threshold, fire a nudge. This
        # is more actionable than contradiction_alert (which only counts
        # CHALLENGED_BY edges) — it tells the agent WHICH prior obs conflicts.
        # Skip if a recent CHALLENGED_BY edge exists for the most recent
        # prior observation (already addressed).
        if value is not None:
            try:
                # Pull the most recent 3 prior observations on this node
                prior_rows = store.conn.execute(
                    """SELECT o.id, o.value, o.created_by, o.created_at, o.sigma
                       FROM ohm_observations o
                       WHERE o.node_id = ? AND o.deleted_at IS NULL
                       ORDER BY o.created_at DESC LIMIT 3""",
                    [node_id],
                ).fetchall()
                for prior_id, prior_value, prior_agent, prior_at, prior_sigma in prior_rows:
                    if prior_value is None:
                        continue
                    gap = abs(value - float(prior_value))
                    if gap < value_contradiction_threshold:
                        continue
                    # Check if there's a recent CHALLENGED_BY edge on this prior obs
                    has_challenge = store.conn.execute(
                        """SELECT COUNT(*) FROM ohm_edges
                           WHERE (to_node = ? OR to_node = ?)
                             AND edge_type = 'CHALLENGED_BY'
                             AND deleted_at IS NULL
                             AND created_at >= ?""",
                        [node_id, prior_id, prior_at],
                    ).fetchone()
                    has_challenge_count = has_challenge[0] if has_challenge else 0
                    if has_challenge_count > 0:
                        # Already being addressed — don't double-nudge
                        continue
                    nudges.append(
                        {
                            "type": "value_contradiction",
                            "message": (
                                f"Your new observation value ({value:.4g}) differs from "
                                f"{prior_agent}'s observation ({float(prior_value):.4g}) on the "
                                f"same node by {gap:.4g}. Consider recording a reconciliation "
                                f"or challenging the older claim if your evidence is stronger."
                            ),
                            "severity": "warning",
                            "data": {
                                "node_id": node_id,
                                "new_value": value,
                                "prior_value": float(prior_value),
                                "prior_agent": prior_agent,
                                "prior_observation_id": prior_id,
                                "prior_at": str(prior_at) if prior_at else None,
                                "gap": round(gap, 4),
                                "threshold": value_contradiction_threshold,
                            },
                        }
                    )
                    # Only fire once per write — the most recent disagreement is
                    # the most actionable.
                    break
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

    # ── OHM-jdfq: High confidence + weak source nudge ─────────────────────
    # ADR-028: source_tier ceilings exist but agents may still write high
    # confidence with a weak tier. Nudge them to reconcile.
    if confidence is not None and source_tier is not None:
        weak_tiers = {"raw", "unverified"}
        if confidence >= 0.8 and source_tier in weak_tiers:
            nudges.append(
                {
                    "type": "high_confidence_weak_source",
                    "message": f"Confidence is {confidence:.2f} but source_tier is '{source_tier}'. Weak sources (raw, unverified) have confidence ceilings of 0.3-0.5. Consider downgrading confidence or adding stronger evidence.",
                    "severity": "warning",
                    "data": {
                        "confidence": confidence,
                        "source_tier": source_tier,
                        "ceiling": 0.3 if source_tier == "raw" else 0.5,
                    },
                }
            )

    # ── OHM-jdfq: Causal edge without mechanism nudge ─────────────────────
    # ADR-023: Causal edges feed the Bayesian network. A mechanism helps
    # other agents understand WHY the causation holds, not just THAT it does.
    if action == "edge" and edge_type and edge_type in SEMANTICALLY_CAUSAL_EDGE_TYPES:
        has_mechanism = bool(condition and condition.strip()) or bool(metadata and metadata.get("mechanism"))
        if not has_mechanism:
            nudges.append(
                {
                    "type": "causal_edge_missing_mechanism",
                    "message": f"You wrote {edge_type} but didn't specify a mediating mechanism. What's the causal pathway? Add a 'condition' field or metadata.mechanism to help other agents understand why this causation holds.",
                    "severity": "suggestion",
                    "data": {
                        "edge_type": edge_type,
                        "suggested_fields": ["condition", "metadata.mechanism"],
                    },
                }
            )

    # ── OHM-jdfq: Fast-decaying observation nudge ─────────────────────────
    # If the observation has a half_life_days set (perishable data), check
    # whether existing observations on this node have already decayed.
    if action == "observation" and node_id and store and half_life_days is not None:
        try:
            from ohm.graph.queries import compute_confidence_with_decay

            rows = store.conn.execute(
                """SELECT o.value, o.created_at
                   FROM ohm_observations o
                   WHERE o.node_id = ? AND o.deleted_at IS NULL
                   ORDER BY o.created_at DESC LIMIT 5""",
                [node_id],
            ).fetchall()
            stale_count = 0
            max_decayed = 1.0
            for row in rows:
                val, created_at = row[0], row[1]
                if val is not None and created_at is not None:
                    decay = compute_confidence_with_decay(
                        store.conn,
                        base_confidence=float(val),
                        last_observed_at=created_at,
                        half_life_days=half_life_days,
                        floor=0.1,
                    )
                    if decay["decayed_confidence"] <= 0.2:
                        stale_count += 1
                        max_decayed = min(max_decayed, decay["decayed_confidence"])
            if stale_count >= 2:
                nudges.append(
                    {
                        "type": "fast_decaying_observation",
                        "message": f"This node has {stale_count} observations that have decayed below 0.2 confidence (half-life {half_life_days}d). Refresh with new measurements to maintain signal quality.",
                        "severity": "hint",
                        "data": {
                            "stale_count": stale_count,
                            "half_life_days": half_life_days,
                            "max_decayed_confidence": round(max_decayed, 4),
                        },
                    }
                )
        except Exception:
            pass  # Never fail the write for a nudge

    # ── OHM-848: Proposed-type tag nudge ─────────────────────────────────
    if action == "node" and tags:
        from ohm.graph.queries.type_proposals import detect_proposed_types
        proposed = detect_proposed_types(tags)
        for pt in proposed:
            nudges.append({
                "type": "proposed_type_trial",
                "message": f"Type '{pt}' is in trial mode. It will be promoted if it proves load-bearing across agents and outcomes.",
                "severity": "info",
                "data": {"proposed_type": pt},
            })

    return nudges


def enrich_response(response: dict, nudges: list[dict], store: Any = None, agent: str = "unknown", action: str = "unknown", target_id: str | None = None) -> dict:
    """Add nudges to an API response without breaking existing clients.

    When a store is provided, each nudge is also persisted to ohm_nudge_log
    for quality analytics (OHM-jdfq). The log entry's `accepted` field is
    initially NULL — it's filled in later if the agent acts on the nudge.
    """
    if nudges:
        response["nudges"] = nudges
        if store is not None:
            _persist_nudge_log(store, agent, action, target_id, nudges)
    return response


def _persist_nudge_log(store: Any, agent: str, action: str, target_id: str | None, nudges: list[dict]) -> None:
    """Write nudge entries to ohm_nudge_log (OHM-jdfq).

    Best-effort: never fails the write if logging fails. Each nudge gets
    its own row so analytics can group by nudge_type.
    """
    import json as _json
    import uuid as _uuid

    try:
        for n in nudges:
            nudge_id = f"nudge_{_uuid.uuid4().hex[:12]}"
            store.conn.execute(
                """INSERT INTO ohm_nudge_log (id, agent, action, nudge_type, severity, target_id, message, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    nudge_id,
                    agent,
                    action,
                    n.get("type", "unknown"),
                    n.get("severity", "info"),
                    target_id,
                    n.get("message", ""),
                    _json.dumps(n.get("data", {})) if n.get("data") else None,
                ],
            )
    except Exception:
        logger.debug("nudge log persistence failed", exc_info=True)


def accept_nudge(
    conn: DuckDBPyConnection,
    *,
    nudge_id: str,
    agent: str | None = None,
    helpful: bool = True,
    notes: str | None = None,
) -> dict[str, Any]:
    """Mark a nudge as accepted (or rejected) by the agent (OHM-jdfq tranche 3).

    The agent that received a nudge can opt-in/opt-out to provide quality
    feedback. Acceptance rate by nudge_type is the primary signal for
    "is this nudge useful?" — high rejection rates mean we should retire
    or refine the nudge.

    Args:
        conn: Database connection.
        nudge_id: The ohm_nudge_log.id to accept.
        agent: Optional agent name. When set, the agent must match the
            nudge's recorded agent (prevents cross-agent acceptance
            tampering). When None, the check is skipped.
        helpful: True for accepted, False for rejected.
        notes: Optional free-text reason.

    Returns:
        The updated nudge log row.

    Raises:
        ValueError: If nudge_id does not exist or agent doesn't match.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError

    nudge_id = validate_identifier(nudge_id, name="nudge_id")

    row = conn.execute(
        "SELECT id, agent, accepted, accepted_at FROM ohm_nudge_log WHERE id = ?",
        [nudge_id],
    ).fetchone()
    if not row:
        raise ValidationError(f"Nudge {nudge_id} not found")
    existing_id, existing_agent, existing_accepted, existing_accepted_at = row

    if agent is not None and existing_agent != agent:
        raise ValidationError(f"Nudge {nudge_id} was issued to '{existing_agent}', not '{agent}'")

    conn.execute(
        """UPDATE ohm_nudge_log
           SET accepted = ?, accepted_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        [bool(helpful), nudge_id],
    )

    result = conn.execute(
        "SELECT * FROM ohm_nudge_log WHERE id = ?",
        [nudge_id],
    ).fetchone()
    cols = [d[0] for d in conn.description]
    return dict(zip(cols, result))


def list_nudges(
    conn: DuckDBPyConnection,
    *,
    agent: str | None = None,
    nudge_type: str | None = None,
    target_id: str | None = None,
    accepted: bool | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List nudge log entries with optional filters (OHM-jdfq tranche 3).

    Args:
        conn: Database connection.
        agent: Filter by agent who received the nudge.
        nudge_type: Filter by nudge type (e.g. 'high_confidence_weak_source').
        target_id: Filter by target node/edge ID.
        accepted: True for accepted nudges, False for rejected, None for
            un-responded (or any state when None).
        since: ISO timestamp — only return nudges created after this time.
        limit: Max rows to return (default 50).

    Returns:
        List of nudge log row dicts, newest first.
    """
    where: list[str] = []
    params: list[Any] = []
    if agent is not None:
        where.append("agent = ?")
        params.append(agent)
    if nudge_type is not None:
        where.append("nudge_type = ?")
        params.append(nudge_type)
    if target_id is not None:
        where.append("target_id = ?")
        params.append(target_id)
    if accepted is not None:
        where.append("accepted = ?")
        params.append(bool(accepted))
    if since is not None:
        where.append("created_at >= ?")
        params.append(since)

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM ohm_nudge_log{where_clause} ORDER BY created_at DESC LIMIT ?",
        [*params, limit],
    ).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, row)) for row in rows]


def nudge_acceptance_stats(
    conn: DuckDBPyConnection,
    *,
    since: str | None = None,
    agent: str | None = None,
) -> dict[str, Any]:
    """Aggregate nudge acceptance rates by nudge_type (OHM-jdfq tranche 3).

    Returns:
        Dict with:
        - total: total nudges
        - responded: nudges with accepted IS NOT NULL
        - acceptance_rate: responded / total
        - by_type: dict of nudge_type → {total, accepted, rejected, rate}
        - by_agent: dict of agent → {total, accepted, rejected, rate}
    """
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("created_at >= ?")
        params.append(since)
    if agent is not None:
        where.append("agent = ?")
        params.append(agent)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    totals = conn.execute(
        f"SELECT COUNT(*), SUM(CASE WHEN accepted IS NOT NULL THEN 1 ELSE 0 END) FROM ohm_nudge_log{where_clause}",
        params,
    ).fetchone()
    total_nudges = totals[0] or 0
    responded = totals[1] or 0

    by_type_rows = conn.execute(
        f"""SELECT nudge_type,
                  COUNT(*) AS total,
                  SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS accepted_count,
                  SUM(CASE WHEN accepted = false THEN 1 ELSE 0 END) AS rejected_count
           FROM ohm_nudge_log{where_clause}
           GROUP BY nudge_type
           ORDER BY total DESC""",
        params,
    ).fetchall()
    by_type = {}
    for ntype, total, accepted_count, rejected_count in by_type_rows:
        total = total or 0
        accepted_count = accepted_count or 0
        rejected_count = rejected_count or 0
        responded_count = accepted_count + rejected_count
        by_type[ntype] = {
            "total": total,
            "accepted": accepted_count,
            "rejected": rejected_count,
            "acceptance_rate": round(accepted_count / responded_count, 4) if responded_count > 0 else None,
        }

    by_agent_rows = conn.execute(
        f"""SELECT agent,
                  COUNT(*) AS total,
                  SUM(CASE WHEN accepted = true THEN 1 ELSE 0 END) AS accepted_count,
                  SUM(CASE WHEN accepted = false THEN 1 ELSE 0 END) AS rejected_count
           FROM ohm_nudge_log{where_clause}
           GROUP BY agent
           ORDER BY total DESC""",
        params,
    ).fetchall()
    by_agent = {}
    for ag, total, accepted_count, rejected_count in by_agent_rows:
        total = total or 0
        accepted_count = accepted_count or 0
        rejected_count = rejected_count or 0
        responded_count = accepted_count + rejected_count
        by_agent[ag] = {
            "total": total,
            "accepted": accepted_count,
            "rejected": rejected_count,
            "acceptance_rate": round(accepted_count / responded_count, 4) if responded_count > 0 else None,
        }

    return {
        "total": total_nudges,
        "responded": responded,
        "acceptance_rate": round(responded / total_nudges, 4) if total_nudges > 0 else None,
        "by_type": by_type,
        "by_agent": by_agent,
    }
