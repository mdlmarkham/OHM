"""POMDP Decision Intelligence — Belief-State Policy (Observe vs. Act).

OHM-od01.5: For a given decision, should we gather more information (observe)
or take action now (act)?

Phase 1 (EVPI): Compares expected value of perfect information against
observation cost. If EVPI > cost → observe; else → act.

Phase 1.5 (EVSI + Causal Intervention): Adds Expected Value of Sample
Information (more realistic than EVPI since observations are imperfect),
causal intervention comparison (what happens if we act vs. don't act), and
action alternatives from decision node metadata.

State convention (per ADR-008):
  0 = "bad" (failure, closed, negative, threat active)
  1 = "good" (normal, open, positive)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def compute_policy(
    conn,
    target: str,
    *,
    decision_nodes: list[str] | None = None,
    horizon: int = 1,
    cost_of_observation: float = 1.0,
    edge_types: list[str] | None = None,
    layers: list[str] | None = None,
    leak_probability: float = 0.15,
    root_prior: float = 0.3,
    observation_window_days: float | None = None,
    include_intervention: bool = True,
    sample_quality: float | None = None,
) -> dict[str, Any]:
    """Recommend observe vs. act for a decision target.

    Phase 1 POMDP: Compare EVPI/EVSI against cost of observation.
    Phase 1.5: Add causal intervention comparison and action alternatives.

    Args:
        conn: DuckDB connection.
        target: Target node ID for the decision.
        decision_nodes: Optional list of decision node IDs to consider.
        horizon: Planning horizon (1 = single step). Multi-step not yet implemented.
        cost_of_observation: Cost of gathering information (default 1.0).
        edge_types: Edge types to include in causal analysis.
        layers: Optional layer filter.
        leak_probability: Baseline probability for Bayesian inference.
        root_prior: Prior probability for root nodes.
        observation_window_days: Only use observations within this window.
        include_intervention: Compute do(bad) vs do(good) comparison (default True).
        sample_quality: Observation quality 0-1 (1 = perfect, = EVPI).
            If None, derived from mean edge confidence in causal neighborhood.
            EVSI = sample_quality * EVPI. Default EVPI when sample_quality=1.

    Returns:
        Dict with recommendation, EVPI, EVSI, belief, intervention comparison,
        action alternatives, and reasoning.
    """
    from ohm.bayesian import bayesian_inference, compute_voi, causal_intervention
    from ohm.graph_reader import coerce_reader

    reader = coerce_reader(conn)

    # Multi-step horizon not yet implemented
    if horizon != 1:
        return {
            "method": "belief_state_policy",
            "target": target,
            "recommendation": "act",
            "confidence": 0.0,
            "reasoning": f"Horizon {horizon} not yet implemented. Phase 1 only supports horizon=1.",
            "evpi": 0.0,
            "evsi": 0.0,
            "cost_of_observation": cost_of_observation,
            "current_belief": {"good": 0.5, "bad": 0.5},
            "utility_available": False,
            "horizon": horizon,
        }

    # Auto-detect decision nodes
    if decision_nodes is None:
        _dec_nodes = reader.get_nodes(node_type="decision")
        decision_nodes = [n.id for n in _dec_nodes if n.utility_scale is None or n.utility_scale > 0]

    if target not in decision_nodes:
        decision_nodes = [target] + [d for d in decision_nodes if d != target]

    # Compute VoI rankings
    voi_result = compute_voi(
        conn,
        decision_nodes=decision_nodes,
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
    )

    rankings = voi_result.get("rankings", [])
    top_candidates = rankings[:5] if rankings else []

    # EVPI = sum of top VoI scores (value of reducing ALL uncertainty)
    evpi = sum(r.get("voi_score", 0.0) for r in top_candidates[:3])

    # Check if target itself is in VoI rankings
    target_voi = next((r for r in rankings if r["node_id"] == target), None)
    if target_voi is None:
        for r in rankings:
            if target in r.get("downstream_decisions", []):
                target_voi = r
                break

    if target_voi:
        evpi = target_voi.get("voi_score", 0.0)

    # EVSI = Expected Value of Sample Information
    # Real observations are imperfect. EVSI = observation_quality * EVPI.
    # When sample_quality not given, derive from mean edge confidence in
    # the target's causal neighborhood (observations are only as good as
    # the causal model that interprets them).
    if sample_quality is None:
        # Derive from causal neighborhood confidence
        try:
            edges_from = reader.get_edges(from_node=target, edge_types=edge_types, layers=layers)
            edges_to = reader.get_edges(to_node=target, edge_types=edge_types, layers=layers)
            all_confs = []
            for e in list(edges_from) + list(edges_to):
                if e.confidence is not None:
                    all_confs.append(float(e.confidence))
            sample_quality = sum(all_confs) / len(all_confs) if all_confs else 0.7
        except Exception:
            sample_quality = 0.7  # Default: observations are 70% as informative as perfect info

    sample_quality = max(0.0, min(1.0, sample_quality))
    evsi = evpi * sample_quality

    # Bayesian posterior (current belief state)
    inference_result = bayesian_inference(
        conn,
        target,
        evidence={},
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
        observation_window_days=observation_window_days,
    )
    current_posterior = inference_result.get("posterior", {}).get(target, {})
    p_good = current_posterior.get("good", 0.5)
    p_bad = current_posterior.get("bad", 0.5)

    # Target node metadata
    target_node = reader.get_node(target)
    utility_usd = target_node.utility_usd_per_day if target_node else None
    utility_scale = target_node.utility_scale if target_node else None
    current_best_action = target_node.current_best_action if target_node else None
    action_alternatives = target_node.action_alternatives if target_node else None
    utility_available = utility_usd is not None

    if not utility_available and utility_scale is not None:
        evpi = evpi * utility_scale
        evsi = evsi * utility_scale

    # Causal intervention comparison (Phase 1.5)
    # What happens if we force target to good vs. bad state?
    intervention_comparison = None
    if include_intervention and p_bad > 0.05:
        try:
            do_bad = causal_intervention(
                conn,
                target,
                0,  # force bad
                edge_types=edge_types,
                layers=layers,
                leak_probability=leak_probability,
                root_prior=root_prior,
            )
            do_good = causal_intervention(
                conn,
                target,
                1,  # force good
                edge_types=edge_types,
                layers=layers,
                leak_probability=leak_probability,
                root_prior=root_prior,
            )

            # Key downstream effects
            bad_posteriors = do_bad.get("posterior", {})
            good_posteriors = do_good.get("posterior", {})

            downstream_diff = []
            for node_id in bad_posteriors:
                if node_id == target:
                    continue
                if isinstance(bad_posteriors[node_id], dict) and isinstance(good_posteriors.get(node_id), dict):
                    bad_p = bad_posteriors[node_id].get("bad", 0)
                    good_p = good_posteriors.get(node_id, {}).get("bad", 0)
                    diff = round(bad_p - good_p, 4)
                    if abs(diff) >= 0.05:  # Only report meaningful differences
                        downstream_diff.append(
                            {
                                "node_id": node_id,
                                "p_bad_if_bad": bad_p,
                                "p_bad_if_good": good_p,
                                "impact": diff,
                            }
                        )

            downstream_diff.sort(key=lambda x: abs(x["impact"]), reverse=True)

            intervention_comparison = {
                "downstream_impacts": downstream_diff[:10],
                "n_downstream_affected": len(downstream_diff),
                "incoming_edges_severed": do_bad.get("incoming_edges_severed", 0),
            }
        except Exception as e:
            logger.warning(f"Intervention comparison failed: {e}")
            intervention_comparison = {"error": str(e)}

    # Decision: observe or act?
    # Use EVSI (more realistic) for the primary recommendation
    # but show EVPI for reference
    recommend_observe = evsi > cost_of_observation

    if recommend_observe:
        recommendation = "observe"
        confidence = min(1.0, evsi / (cost_of_observation * 2)) if cost_of_observation > 0 else 0.5
        reasoning = f"EVSI ({evsi:.4f}) exceeds observation cost ({cost_of_observation:.4f}). Expected improvement from a single observation (quality {sample_quality:.0%}) exceeds the cost of gathering it."
    else:
        recommendation = "act"
        confidence = min(1.0, cost_of_observation / (evsi + 0.01)) if evsi > 0 else 0.5
        reasoning = f"EVSI ({evsi:.4f}) does not exceed observation cost ({cost_of_observation:.4f}). Take action now — a single observation would not improve decision quality enough to justify its cost."

    if not rankings:
        reasoning = "No causal ancestors found. Insufficient graph structure to compute VoI. Recommend act based on current belief."
        recommendation = "act"
        confidence = 0.3

    # Add intervention context to reasoning
    if intervention_comparison and not isinstance(intervention_comparison, dict) or "downstream_impacts" in (intervention_comparison or {}):
        impacts = (intervention_comparison or {}).get("downstream_impacts", [])
        if impacts:
            top_impact = impacts[0]
            reasoning += f" The largest downstream impact: {top_impact['node_id']} shifts {top_impact['impact']:+.2%} in P(bad) depending on action."

    # Observation recommendation: which node to observe?
    observe_target = None
    if recommend_observe and top_candidates:
        observe_target = {
            "node_id": top_candidates[0]["node_id"],
            "voi_score": top_candidates[0].get("voi_score", 0),
            "reason": f"Reducing uncertainty on {top_candidates[0]['node_id']} has the highest VoI score",
        }

    return {
        "method": "belief_state_policy",
        "target": target,
        "recommendation": recommendation,
        "confidence": round(confidence, 4),
        "reasoning": reasoning,
        "evpi": round(evpi, 4),
        "evsi": round(evsi, 4),
        "sample_quality": round(sample_quality, 4),
        "cost_of_observation": cost_of_observation,
        "current_belief": {
            "good": round(p_good, 4),
            "bad": round(p_bad, 4),
        },
        "utility_available": utility_available,
        "utility_usd_per_day": utility_usd,
        "utility_scale": utility_scale,
        "current_best_action": current_best_action,
        "action_alternatives": action_alternatives,
        "observe_target": observe_target,
        "intervention_comparison": intervention_comparison,
        "horizon": horizon,
        "voi_rankings_used": len(rankings),
        "top_voi_candidates": [
            {
                "node_id": r["node_id"],
                "voi_score": round(r.get("voi_score", 0), 4),
                "uncertainty": round(r.get("uncertainty", 0), 4),
                "sensitivity": round(r.get("sensitivity", 0), 4),
            }
            for r in top_candidates[:5]
        ],
    }
