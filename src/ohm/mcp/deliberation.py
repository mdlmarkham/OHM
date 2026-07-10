"""Deliberation lifecycle state machine (OHM-793).

Implements the deliberation lifecycle:
    PROPOSE → CHALLENGE → EVIDENCE → SYNTHESIZE → DECIDE → RESOLVE

A deliberation is associated with a target node and tracked in conversation
state (#789). Key events (challenge, evidence, decision) are promoted to
durable graph state via the existing edge/observation system.

Collective decision thresholds (scaled by blast radius):
- minimum number of independent agents
- inter-agent disagreement below a bound
- evidence is fresh enough
- statistical methods agree within a bound
- posterior has stabilized
- no active high-quality challenge remains

Integration:
- Conversation state (#789): deliberations stored in conv state
- Nudge taxonomy (#791): Socratic and threshold nudges
- Calibration profile (#792): reputation and loop-risk scoring
"""

from __future__ import annotations

from typing import Any

from ohm.mcp.conversation_state import get_store


# ── Deliberation status constants ──

PROPOSED = "proposed"
CHALLENGED = "challenged"
EVIDENCE_PHASE = "evidence_phase"
SYNTHESIZED = "synthesized"
DECIDED = "decided"
RESOLVED = "resolved"

_VALID_TRANSITIONS = {
    PROPOSED: {CHALLENGED, SYNTHESIZED, DECIDED, RESOLVED},
    CHALLENGED: {EVIDENCE_PHASE, SYNTHESIZED, DECIDED, RESOLVED},
    EVIDENCE_PHASE: {SYNTHESIZED, DECIDED, RESOLVED},
    SYNTHESIZED: {DECIDED, RESOLVED},
    DECIDED: {RESOLVED},
    RESOLVED: set(),
}


# ── Default thresholds (scaled by blast radius) ──

_DEFAULT_THRESHOLDS = {
    "normal": {
        "min_agents": 2,
        "max_disagreement": 0.35,
        "min_evidence_freshness": 0.3,
        "max_method_divergence": 0.25,
        "min_belief_stability": 0.7,
        "require_no_active_challenges": True,
    },
    "high": {
        "min_agents": 3,
        "max_disagreement": 0.20,
        "min_evidence_freshness": 0.5,
        "max_method_divergence": 0.15,
        "min_belief_stability": 0.8,
        "require_no_active_challenges": True,
    },
}


def propose_claim(
    thread_id: str,
    node_id: str,
    proposed_by: str,
    claim_text: str | None = None,
    confidence: float = 0.5,
) -> dict[str, Any]:
    """Propose a new claim for deliberation (OHM-793).

    Creates a deliberation entry in conversation state with status 'proposed'.
    """
    store = get_store()
    state = store.get_or_create(thread_id)

    for deliberation in state.deliberations:
        if deliberation.get("node_id") == node_id:
            return {
                "error": "deliberation_exists",
                "message": f"Deliberation already exists for {node_id} with status {deliberation.get('status')}",
                "node_id": node_id,
            }

    deliberation: dict[str, Any] = {
        "node_id": node_id,
        "status": PROPOSED,
        "proposed_by": proposed_by,
        "claim_text": claim_text,
        "confidence": confidence,
        "challengers": [],
        "evidence_count": 0,
        "last_status_change": _now_iso(),
        "decision_thresholds": {},
        "blast_radius": "normal",
    }

    store.update_state(thread_id, {"deliberations": [deliberation]})

    if proposed_by:
        store.record_contribution(thread_id, proposed_by, "claims")

    return {
        "node_id": node_id,
        "status": PROPOSED,
        "proposed_by": proposed_by,
        "message": f"Claim proposed for {node_id}",
    }


def challenge_claim(
    thread_id: str,
    node_id: str,
    challenged_by: str,
    reason: str,
    confidence: float = 0.6,
) -> dict[str, Any]:
    """Challenge a proposed claim (OHM-793).

    Transitions the deliberation to 'challenged' status and records the challenger.
    """
    store = get_store()
    state = store.get_or_create(thread_id)

    for deliberation in state.deliberations:
        if deliberation.get("node_id") == node_id:
            current_status = deliberation["status"]
            # Allow adding more challengers if already challenged
            if current_status != CHALLENGED and not _can_transition(current_status, CHALLENGED):
                return {
                    "error": "invalid_transition",
                    "message": f"Cannot transition from {current_status} to {CHALLENGED}",
                    "node_id": node_id,
                }

            if current_status == PROPOSED:
                deliberation["status"] = CHALLENGED
                deliberation["last_status_change"] = _now_iso()
            if challenged_by not in deliberation["challengers"]:
                deliberation["challengers"].append(challenged_by)
            deliberation["challenge_reason"] = reason
            deliberation["challenge_confidence"] = confidence

            if challenged_by:
                store.record_contribution(thread_id, challenged_by, "challenges")

            return {
                "node_id": node_id,
                "status": CHALLENGED,
                "challengers": deliberation["challengers"],
                "message": f"Claim challenged by {challenged_by}",
            }

    return {
        "error": "deliberation_not_found",
        "message": f"No deliberation found for {node_id}",
        "node_id": node_id,
    }


def submit_evidence(
    thread_id: str,
    node_id: str,
    submitted_by: str,
    evidence_type: str = "observation",
    evidence_summary: str | None = None,
) -> dict[str, Any]:
    """Submit evidence for a deliberation (OHM-793).

    Transitions to 'evidence_phase' if currently 'challenged'.
    Increments the evidence count.
    """
    store = get_store()
    state = store.get_or_create(thread_id)

    for deliberation in state.deliberations:
        if deliberation.get("node_id") == node_id:
            if deliberation["status"] == CHALLENGED:
                if _can_transition(CHALLENGED, EVIDENCE_PHASE):
                    deliberation["status"] = EVIDENCE_PHASE
                    deliberation["last_status_change"] = _now_iso()

            deliberation["evidence_count"] = deliberation.get("evidence_count", 0) + 1
            if "evidence_submissions" not in deliberation:
                deliberation["evidence_submissions"] = []
            deliberation["evidence_submissions"].append(
                {
                    "submitted_by": submitted_by,
                    "type": evidence_type,
                    "summary": evidence_summary,
                    "submitted_at": _now_iso(),
                }
            )

            if submitted_by:
                store.record_contribution(thread_id, submitted_by, "observations")

            return {
                "node_id": node_id,
                "status": deliberation["status"],
                "evidence_count": deliberation["evidence_count"],
                "message": f"Evidence submitted by {submitted_by}",
            }

    return {
        "error": "deliberation_not_found",
        "message": f"No deliberation found for {node_id}",
        "node_id": node_id,
    }


def check_decision_ready(
    thread_id: str,
    node_id: str,
    belief_data: dict[str, Any] | None = None,
    agent_profile: dict[str, Any] | None = None,
    blast_radius: str = "normal",
) -> dict[str, Any]:
    """Check if collective decision thresholds are met (OHM-793).

    Thresholds scale with blast radius. Returns the threshold check results
    and whether the decision is ready.
    """
    store = get_store()
    state = store.get_or_create(thread_id)

    thresholds = _DEFAULT_THRESHOLDS.get(blast_radius, _DEFAULT_THRESHOLDS["normal"])

    deliberation = None
    for d in state.deliberations:
        if d.get("node_id") == node_id:
            deliberation = d
            break

    if not deliberation:
        return {
            "error": "deliberation_not_found",
            "message": f"No deliberation found for {node_id}",
            "node_id": node_id,
        }

    if deliberation["status"] in (RESOLVED, DECIDED):
        return {
            "node_id": node_id,
            "status": deliberation["status"],
            "ready": True,
            "message": f"Deliberation already {deliberation['status']}",
        }

    # Count independent agents (participants who contributed to this deliberation)
    participants = set(deliberation.get("challengers", []))
    if deliberation.get("proposed_by"):
        participants.add(deliberation["proposed_by"])
    contribs = state.agent_contributions
    for agent, data in contribs.items():
        if data.get("claims", 0) > 0 or data.get("observations", 0) > 0 or data.get("challenges", 0) > 0:
            participants.add(agent)
    n_agents = len(participants)

    # Inter-agent disagreement: number of challengers / total participants
    n_challengers = len(deliberation.get("challengers", []))
    disagreement = n_challengers / max(n_agents, 1)

    # Evidence freshness from conversation state (or belief data)
    evidence_freshness = 0.5  # default neutral
    if belief_data and "evidence_freshness" in belief_data:
        evidence_freshness = belief_data["evidence_freshness"]

    # Method divergence from belief data
    method_divergence = 0.0
    if belief_data and "method_divergence" in belief_data:
        method_divergence = belief_data["method_divergence"].get("max_divergence", 0.0)

    # Belief stability: posterior confidence (from belief data)
    belief_stability = 0.5
    if belief_data and "posterior" in belief_data:
        p = belief_data["posterior"].get("P(bad)", 0.5)
        belief_stability = max(p, 1.0 - p)  # higher = more certain

    # Active challenges
    active_challenges = n_challengers > 0

    # Evaluate thresholds
    threshold_results = {
        "min_agents_met": n_agents >= thresholds["min_agents"],
        "disagreement_bound_met": disagreement <= thresholds["max_disagreement"],
        "evidence_freshness_met": evidence_freshness >= thresholds["min_evidence_freshness"],
        "method_consensus_met": method_divergence <= thresholds["max_method_divergence"],
        "belief_stability_met": belief_stability >= thresholds["min_belief_stability"],
        "no_active_challenges_met": not active_challenges or not thresholds["require_no_active_challenges"],
    }

    ready = all(threshold_results.values())

    deliberation["decision_thresholds"] = threshold_results
    deliberation["blast_radius"] = blast_radius

    if ready and _can_transition(deliberation["status"], DECIDED):
        deliberation["status"] = DECIDED
        deliberation["last_status_change"] = _now_iso()

    return {
        "node_id": node_id,
        "status": deliberation["status"],
        "ready": ready,
        "thresholds": threshold_results,
        "n_agents": n_agents,
        "n_challengers": n_challengers,
        "disagreement": round(disagreement, 4),
        "evidence_freshness": round(evidence_freshness, 4),
        "method_divergence": round(method_divergence, 4),
        "belief_stability": round(belief_stability, 4),
        "blast_radius": blast_radius,
        "message": "Decision ready" if ready else "Thresholds not met",
    }


def resolve_deliberation(
    thread_id: str,
    node_id: str,
    resolved_by: str,
    outcome: str = "resolved",
    notes: str | None = None,
) -> dict[str, Any]:
    """Resolve a deliberation (OHM-793).

    Transitions to 'resolved' status — the terminal state.
    """
    store = get_store()
    state = store.get_or_create(thread_id)

    for deliberation in state.deliberations:
        if deliberation.get("node_id") == node_id:
            if not _can_transition(deliberation["status"], RESOLVED):
                return {
                    "error": "invalid_transition",
                    "message": f"Cannot transition from {deliberation['status']} to {RESOLVED}",
                    "node_id": node_id,
                }

            deliberation["status"] = RESOLVED
            deliberation["last_status_change"] = _now_iso()
            deliberation["resolved_by"] = resolved_by
            deliberation["outcome"] = outcome
            deliberation["resolution_notes"] = notes

            return {
                "node_id": node_id,
                "status": RESOLVED,
                "outcome": outcome,
                "resolved_by": resolved_by,
                "message": f"Deliberation resolved: {outcome}",
            }

    return {
        "error": "deliberation_not_found",
        "message": f"No deliberation found for {node_id}",
        "node_id": node_id,
    }


def get_deliberation(thread_id: str, node_id: str) -> dict[str, Any] | None:
    """Get the current state of a deliberation."""
    store = get_store()
    state = store.get_state(thread_id)
    if not state:
        return None
    for d in state.get("deliberations", []):
        if d.get("node_id") == node_id:
            return d
    return None


def _can_transition(from_status: str, to_status: str) -> bool:
    return to_status in _VALID_TRANSITIONS.get(from_status, set())


def _now_iso() -> str:
    import time as _time

    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


# ── MCP tool dispatch ──


def handle_deliberation_action(
    thread_id: str,
    action: str,
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> dict[str, Any]:
    """Handle ohm_deliberation MCP tool actions (OHM-793).

    Actions: propose, challenge, evidence, check, resolve, get
    """
    node_id = kwargs.get("node_id") or kwargs.get("target") or ""
    if not node_id and action != "get":
        return {"error": "missing_node_id", "message": "node_id or target is required"}

    if action == "propose":
        return propose_claim(
            thread_id=thread_id,
            node_id=node_id,
            proposed_by=agent_id or "unknown",
            claim_text=kwargs.get("claim_text"),
            confidence=kwargs.get("confidence", 0.5),
        )
    elif action == "challenge":
        return challenge_claim(
            thread_id=thread_id,
            node_id=node_id,
            challenged_by=agent_id or "unknown",
            reason=kwargs.get("reason", ""),
            confidence=kwargs.get("confidence", 0.6),
        )
    elif action == "evidence":
        return submit_evidence(
            thread_id=thread_id,
            node_id=node_id,
            submitted_by=agent_id or "unknown",
            evidence_type=kwargs.get("evidence_type", "observation"),
            evidence_summary=kwargs.get("evidence_summary"),
        )
    elif action == "check":
        return check_decision_ready(
            thread_id=thread_id,
            node_id=node_id,
            belief_data=kwargs.get("belief_data"),
            agent_profile=kwargs.get("agent_profile"),
            blast_radius=kwargs.get("blast_radius", "normal"),
        )
    elif action == "resolve":
        return resolve_deliberation(
            thread_id=thread_id,
            node_id=node_id,
            resolved_by=agent_id or "unknown",
            outcome=kwargs.get("outcome", "resolved"),
            notes=kwargs.get("notes"),
        )
    elif action == "get":
        result = get_deliberation(thread_id, node_id)
        if result is None:
            return {"message": "No deliberation found", "node_id": node_id}
        return result
    else:
        return {"error": "unknown_action", "message": f"Unknown action: {action}"}
