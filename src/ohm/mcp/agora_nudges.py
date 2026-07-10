"""Agora-aware nudge generation (OHM-791).

Generates the 9 new nudge types from the agora/deliberation design:

| Type                    | When                                           | Sev |
|-------------------------|------------------------------------------------|-----|
| socratic_falsifiability | Deliberation stalled or disagreement is high   | 2   |
| socratic_steel_man      | Agent makes a strong claim                     | 2   |
| evidence_race           | Challenge could be resolved by an observation  | 2   |
| threshold_not_met       | Collective decision thresholds not satisfied   | 2   |
| human_escalation        | High-blast-radius decision with unmet thresholds| 3   |
| autonomy                | Agent asks for graph belief before giving own   | 2   |
| loop_detected           | Repeated similar queries indicate belief-summing| 3   |
| dissent_rewarded        | Agent's evidence was fresh/surprising and confirmed | 1 |
| method_divergence       | Statistical methods disagree beyond threshold   | 2   |

This module operates at the gateway level (not the daemon) because it
needs conversation state and agent profile, which are gateway-level
concerns (OHM-789).
"""

from __future__ import annotations

from typing import Any

from ohm.server.nudge_taxonomy import (
    SeverityLevel,
    NudgeThrottle,
)

_throttle = NudgeThrottle(min_turns=3, belief_movement_threshold=0.15)


def _reset_throttle() -> None:
    _throttle.reset()


def generate_agora_nudges(
    *,
    thread_id: str,
    tool_name: str,
    kwargs: dict[str, Any],
    agent_id: str | None,
    response_data: dict[str, Any] | None,
    conversation_state: dict[str, Any] | None,
    agent_profile: dict[str, Any] | None = None,
    belief_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate agora-aware nudges based on conversation context.

    Args:
        thread_id: The conversation thread ID.
        tool_name: The MCP tool that was called.
        kwargs: The tool arguments.
        agent_id: The calling agent's ID.
        response_data: The tool response data (from the daemon).
        conversation_state: The per-thread conversation state dict.
        agent_profile: The calling agent's calibration profile.
        belief_data: Belief data for the target node (if available).

    Returns:
        List of nudge dicts with type, severity, message, optional data and action.
    """
    nudges: list[dict[str, Any]] = []
    conv = conversation_state or {}
    belief = belief_data or {}

    target = kwargs.get("target") or kwargs.get("node_id") or ""
    turn = _estimate_turn(conv)

    # ── 1. autonomy: Agent asks for belief before stating their own ──
    if tool_name in ("ohm_belief", "ohm_inference") and target:
        agent_contribs = conv.get("agent_contributions", {})
        my_contribs = agent_contribs.get(agent_id, {}) if agent_id else {}
        has_own_claim = my_contribs.get("claims", 0) > 0
        if not has_own_claim:
            nudge = _build_nudge(
                type="autonomy",
                message=(f"You asked for the graph's belief on {target} before stating your own. Consider forming your independent assessment first, then comparing it to the graph."),
                severity=SeverityLevel.SOFT,
                data={"target": target},
                action="State your own belief before reading the graph's.",
            )
            if _throttle.should_emit(target, "autonomy", _belief_p(belief), turn):
                nudges.append(nudge)
                _throttle.record(target, "autonomy", _belief_p(belief), turn)

    # ── 2. loop_detected: Repeated similar queries ──
    if tool_name in ("ohm_belief", "ohm_inference", "ohm_get_node") and target:
        topics = conv.get("topics", [])
        for topic in topics:
            if topic.get("node_id") == target and topic.get("mentions", 0) >= 4:
                loop_risk = 0.0
                if agent_id and agent_id in conv.get("agent_contributions", {}):
                    loop_risk = conv["agent_contributions"][agent_id].get("loop_risk_max", 0.0)
                if loop_risk >= 0.3 or topic.get("mentions", 0) >= 6:
                    nudge = _build_nudge(
                        type="loop_detected",
                        message=(f"You've queried {target} {topic['mentions']} times in this thread. Consider acting on your current belief rather than repeatedly seeking confirmation."),
                        severity=SeverityLevel.FIRM,
                        data={"target": target, "mentions": topic["mentions"], "loop_risk": loop_risk},
                        action="State a decision or provide new evidence instead of re-querying.",
                    )
                    if _throttle.should_emit(target, "loop_detected", _belief_p(belief), turn):
                        nudges.append(nudge)
                        _throttle.record(target, "loop_detected", _belief_p(belief), turn)
                break

    # ── 3. socratic_falsifiability: Deliberation stalled ──
    deliberations = conv.get("deliberations", [])
    for deliberation in deliberations:
        if deliberation.get("node_id") == target and deliberation.get("status") == "challenged":
            challengers = deliberation.get("challengers", [])
            if len(challengers) >= 2:
                nudge = _build_nudge(
                    type="socratic_falsifiability",
                    message=(f"Deliberation on {target} has {len(challengers)} challengers but no new evidence. What observation would resolve this?"),
                    severity=SeverityLevel.SOFT,
                    data={"target": target, "challengers": challengers},
                    action="Propose a specific observation that would change the outcome.",
                    response_optional=True,
                )
                if _throttle.should_emit(target, "socratic_falsifiability", _belief_p(belief), turn):
                    nudges.append(nudge)
                    _throttle.record(target, "socratic_falsifiability", _belief_p(belief), turn)
            break

    # ── 4. socratic_steel_man: Agent makes a strong claim ──
    if tool_name in WRITE_TOOL_NAMES and target and agent_id:
        confidence = kwargs.get("confidence")
        if confidence is not None and confidence >= 0.85:
            nudge = _build_nudge(
                type="socratic_steel_man",
                message=(f"You're creating a claim on {target} with {confidence:.0%} confidence. What is the strongest argument against this view?"),
                severity=SeverityLevel.SOFT,
                data={"target": target, "confidence": confidence},
                action="Consider the strongest counter-argument before committing.",
                response_optional=True,
            )
            if _throttle.should_emit(target, "socratic_steel_man", _belief_p(belief), turn):
                nudges.append(nudge)
                _throttle.record(target, "socratic_steel_man", _belief_p(belief), turn)

    # ── 5. evidence_race: Challenge resolvable by an observation ──
    if deliberations and target:
        for deliberation in deliberations:
            if deliberation.get("node_id") == target and deliberation.get("status") == "challenged":
                voi_candidates = belief.get("top_voi_candidates", [])
                if voi_candidates:
                    top_voi = voi_candidates[0]
                    nudge = _build_nudge(
                        type="evidence_race",
                        message=(f"Deliberation on {target} is challenged. Observing '{top_voi.get('node_id', 'the top candidate')}' (VOI={top_voi.get('voi_score', 0):.2f}) could resolve it."),
                        severity=SeverityLevel.SOFT,
                        data={
                            "target": target,
                            "observation_candidate": top_voi,
                        },
                        action=f"Use ohm_observe on {top_voi.get('node_id', 'the top candidate')}.",
                    )
                    if _throttle.should_emit(target, "evidence_race", _belief_p(belief), turn):
                        nudges.append(nudge)
                        _throttle.record(target, "evidence_race", _belief_p(belief), turn)
                break

    # ── 6. threshold_not_met: Decision thresholds not satisfied ──
    if deliberations and target:
        for deliberation in deliberations:
            if deliberation.get("node_id") == target:
                thresholds = deliberation.get("decision_thresholds", {})
                unmet = [k for k, v in thresholds.items() if v is False]
                if unmet and deliberation.get("status") not in ("resolved", "decided"):
                    nudge = _build_nudge(
                        type="threshold_not_met",
                        message=(f"Decision on {target} is not ready: {', '.join(unmet[:3])} not met."),
                        severity=SeverityLevel.SOFT,
                        data={"target": target, "unmet_thresholds": unmet},
                        action="Address the unmet thresholds before deciding.",
                    )
                    if _throttle.should_emit(target, "threshold_not_met", _belief_p(belief), turn):
                        nudges.append(nudge)
                        _throttle.record(target, "threshold_not_met", _belief_p(belief), turn)
                break

    # ── 7. human_escalation: High-blast-radius with unmet thresholds ──
    if deliberations and target:
        for deliberation in deliberations:
            if deliberation.get("node_id") == target:
                thresholds = deliberation.get("decision_thresholds", {})
                unmet = [k for k, v in thresholds.items() if v is False]
                blast_radius = deliberation.get("blast_radius", "normal")
                if unmet and blast_radius == "high":
                    nudge = _build_nudge(
                        type="human_escalation",
                        message=(f"High-blast-radius decision on {target} has unmet thresholds ({', '.join(unmet[:3])}). Escalate to human review."),
                        severity=SeverityLevel.FIRM,
                        data={"target": target, "unmet_thresholds": unmet, "blast_radius": "high"},
                        action="Escalate to human review before proceeding.",
                    )
                    if _throttle.should_emit(target, "human_escalation", _belief_p(belief), turn):
                        nudges.append(nudge)
                        _throttle.record(target, "human_escalation", _belief_p(belief), turn)
                break

    # ── 8. method_divergence: Statistical methods disagree ──
    if tool_name in ("ohm_belief", "ohm_inference") and target:
        method_divergence = belief.get("method_divergence")
        if method_divergence and method_divergence.get("max_divergence", 0) >= 0.25:
            methods = method_divergence.get("methods", [])
            nudge = _build_nudge(
                type="method_divergence",
                message=(f"Statistical methods disagree on {target} (divergence={method_divergence['max_divergence']:.2f}): {', '.join(methods[:3])}. Treat the posterior with caution."),
                severity=SeverityLevel.SOFT,
                data={"target": target, "divergence": method_divergence["max_divergence"], "methods": methods},
                action="Consider which method is most appropriate for this question.",
            )
            if _throttle.should_emit(target, "method_divergence", _belief_p(belief), turn):
                nudges.append(nudge)
                _throttle.record(target, "method_divergence", _belief_p(belief), turn)

    # ── 9. dissent_rewarded: Agent's evidence was fresh and confirmed ──
    if tool_name == "ohm_observe" and agent_id:
        agent_contribs = conv.get("agent_contributions", {})
        my_contribs = agent_contribs.get(agent_id, {}) if agent_id else {}
        novelty_score = my_contribs.get("novelty_score", 0.0)
        if novelty_score >= 0.5:
            nudge = _build_nudge(
                type="dissent_rewarded",
                message=(f"Your recent observations have high novelty (score={novelty_score:.2f}). Keep bringing fresh evidence."),
                severity=SeverityLevel.CONTEXT,
                data={"target": target, "novelty_score": novelty_score},
            )
            if _throttle.should_emit(target or "global", "dissent_rewarded", None, turn):
                nudges.append(nudge)
                _throttle.record(target or "global", "dissent_rewarded", None, turn)

    return nudges


def _build_nudge(
    *,
    type: str,
    message: str,
    severity: SeverityLevel = SeverityLevel.SOFT,
    data: dict[str, Any] | None = None,
    action: str | None = None,
    response_optional: bool = False,
) -> dict[str, Any]:
    """Build a standardized nudge dict.

    Args:
        response_optional: True for Socratic questions — agents may
            ignore them without penalty (OHM-791).
    """
    nudge: dict[str, Any] = {
        "type": type,
        "severity": int(severity),
        "message": message,
    }
    if data:
        nudge["data"] = data
    if action:
        nudge["action"] = action
    if response_optional:
        nudge["response_optional"] = True
    return nudge


def _belief_p(belief_data: dict[str, Any]) -> float | None:
    """Extract P(bad) from belief data for throttle comparison."""
    posterior = belief_data.get("posterior", {})
    return posterior.get("P(bad)")


def _estimate_turn(conv: dict[str, Any]) -> int:
    """Estimate the current turn from conversation state."""
    total_mentions = sum(t.get("mentions", 0) for t in conv.get("topics", []))
    total_nudges = len(conv.get("nudge_history", []))
    return total_mentions + total_nudges


WRITE_TOOL_NAMES = {"ohm_create_node", "ohm_create_edge", "ohm_batch", "ohm_observe", "ohm_challenge", "ohm_support"}
