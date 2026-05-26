"""Phase 1 POMDP: Belief-State Decision (Observe vs. Act).

OHM-od01.5 Phase 1: For a given decision, should we gather more information (observe)
or take action now (act)? Compares expected value of perfect information (EVPI)
against the cost of observation.

EVPI = expected improvement in decision quality from reducing uncertainty.
If EVPI > cost_of_observation → observe; else → act.

This is a 1-step POMDP where:
- Belief state = Bayesian posterior on target node
- Actions = {observe, act}
- observe: gain information (reduce uncertainty), cost = time/opportunity
- act: take action now, payoff = expected utility based on current belief
"""

from __future__ import annotations

from typing import Any


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
) -> dict[str, Any]:
    """Recommend observe vs. act for a decision target.

    Phase 1 POMDP: Compare expected value of perfect information (EVPI)
    against cost of observation. If EVPI > cost → observe; else → act.

    Args:
        conn: DuckDB connection.
        target: Target node ID for the decision.
        decision_nodes: Optional list of decision node IDs to consider.
            If None, auto-detected from nodes with type='decision'.
        horizon: Planning horizon (1 = single step, N = N steps ahead).
            Currently only horizon=1 is implemented.
        cost_of_observation: Cost of gathering information before acting.
            In USD/day (if decision has utility_usd_per_day) or dimensionless.
            Default 1.0.
        edge_types: Edge types to include in causal analysis.
        layers: Optional layer filter.
        leak_probability: Baseline probability for Bayesian inference.
        root_prior: Prior probability for root nodes.
        observation_window_days: Only use observations within this window.

    Returns:
        Dict with:
        - method: "belief_state_policy"
        - target: decision target node
        - recommendation: "observe" or "act"
        - confidence: confidence in recommendation (0-1)
        - reasoning: human-readable explanation
        - evpi: expected value of perfect information (in utility units)
        - cost_of_observation: cost threshold used
        - current_belief: P(good|evidence) for target
        - utility_available: whether utility_usd_per_day is populated
        - horizon: planning horizon (always 1 for Phase 1)
    """
    from ohm.bayesian import bayesian_inference, compute_voi
    from ohm.graph_reader import coerce_reader

    reader = coerce_reader(conn)

    if horizon != 1:
        return {
            "method": "belief_state_policy",
            "target": target,
            "recommendation": "act",
            "confidence": 0.0,
            "reasoning": f"Horizon {horizon} not yet implemented. Phase 1 only supports horizon=1.",
            "evpi": 0.0,
            "cost_of_observation": cost_of_observation,
            "current_belief": {"good": 0.5, "bad": 0.5},
            "utility_available": False,
            "horizon": horizon,
        }

    if decision_nodes is None:
        _dec_nodes = reader.get_nodes(node_type="decision")
        decision_nodes = [n.id for n in _dec_nodes if n.utility_scale is None or n.utility_scale > 0]

    if target not in decision_nodes:
        decision_nodes = [target] + [d for d in decision_nodes if d != target]

    voi_result = compute_voi(
        conn,
        decision_nodes=decision_nodes,
        edge_types=edge_types,
        layers=layers,
        leak_probability=leak_probability,
        root_prior=root_prior,
    )

    rankings = voi_result.get("rankings", [])
    top_candidates = rankings[:3] if rankings else []
    evpi = sum(r.get("voi_score", 0.0) for r in top_candidates)

    target_voi = next((r for r in rankings if r["node_id"] == target), None)
    if target_voi is None:
        for r in rankings:
            if target in r.get("downstream_decisions", []):
                target_voi = r
                break

    if target_voi:
        evpi = target_voi.get("voi_score", 0.0)

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

    target_node = reader.get_node(target)
    utility_usd = target_node.utility_usd_per_day if target_node else None
    utility_scale = target_node.utility_scale if target_node else None
    utility_available = utility_usd is not None

    if not utility_available and utility_scale is not None:
        evpi = evpi * utility_scale

    recommend_observe = evpi > cost_of_observation

    if recommend_observe:
        recommendation = "observe"
        confidence = min(1.0, evpi / (cost_of_observation * 2)) if cost_of_observation > 0 else 0.5
        reasoning = (
            f"EVPI ({evpi:.4f}) exceeds observation cost ({cost_of_observation:.4f}). "
            f"Expected improvement from reducing uncertainty exceeds cost of gathering information."
        )
    else:
        recommendation = "act"
        confidence = min(1.0, cost_of_observation / (evpi + 0.01)) if evpi > 0 else 0.5
        reasoning = (
            f"EVPI ({evpi:.4f}) does not exceed observation cost ({cost_of_observation:.4f}). "
            f"Take action now rather than spend time gathering more information."
        )

    if not rankings:
        reasoning = "No causal ancestors found. Insufficient information to compute VoI. Recommend act based on current belief."
        recommendation = "act"
        confidence = 0.3

    return {
        "method": "belief_state_policy",
        "target": target,
        "recommendation": recommendation,
        "confidence": round(confidence, 4),
        "reasoning": reasoning,
        "evpi": round(evpi, 4),
        "cost_of_observation": cost_of_observation,
        "current_belief": {
            "good": round(p_good, 4),
            "bad": round(p_bad, 4),
        },
        "utility_available": utility_available,
        "utility_usd_per_day": utility_usd,
        "utility_scale": utility_scale,
        "horizon": horizon,
        "voi_rankings_used": len(rankings),
        "top_voi_candidates": [
            {"node_id": r["node_id"], "voi_score": round(r.get("voi_score", 0), 4)}
            for r in top_candidates[:5]
        ],
    }
