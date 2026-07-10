"""Guided agent onboarding to the OHM agora (OHM-798).

Participant-level onboarding: how a new agent joins the agora after the
instance already has a domain (#797).

6-step flow:
1. Capability discovery (ohm_domain_onboarding)
2. Identity registration (ohm_update_state)
3. Domain vocabulary orientation
4. Calibration baseline (3-5 lightweight predictions)
5. Practice deliberation (synthetic, low-stakes)
6. First real contribution

Critical design decision (from issue comment): Steps 4-5 (practice) must
NOT pollute compute_agent_profile. Practice data is stored in conversation
state (#789), NOT as durable ohm_observations/ohm_edges rows. Step 6 is
the first thing that counts toward real calibration.
"""

from __future__ import annotations

from typing import Any

from ohm.mcp.conversation_state import get_store


ONBOARDING_STEPS = [
    {"id": "capability_discovery", "name": "Capability Discovery"},
    {"id": "identity_registration", "name": "Identity Registration"},
    {"id": "vocabulary_orientation", "name": "Domain Vocabulary Orientation"},
    {"id": "calibration_baseline", "name": "Calibration Baseline"},
    {"id": "practice_deliberation", "name": "Practice Deliberation"},
    {"id": "first_contribution", "name": "First Real Contribution"},
]

_ONBOARDING_THREAD_PREFIX = "onboarding"


def get_onboarding_state(agent_id: str) -> dict[str, Any]:
    """Get the current onboarding state for an agent.

    Returns:
        Dict with 'step' (int), 'completed' (bool), and 'answers'.
    """
    thread_id = f"{_ONBOARDING_THREAD_PREFIX}:{agent_id}"
    store = get_store()
    state = store.get_state(thread_id)

    if state is None:
        return {"step": 0, "completed": False, "started": False}

    # Extract onboarding progress from conversation state
    pending = state.get("pending_questions", [])
    contribs = state.get("agent_contributions", {}).get(agent_id, {})

    # Determine step from contributions and state
    has_identity = bool(contribs.get("claims", 0) > 0 or state.get("participants"))
    has_calibration = any(q.get("type") == "calibration_prediction" for q in pending) or any(q.get("text", "").startswith("calibration:") for q in state.get("nudge_history", []))
    has_practice = any(d.get("status") in ("resolved", "decided") for d in state.get("deliberations", []))
    has_real_contribution = contribs.get("claims", 0) > 1 or contribs.get("observations", 0) > 0

    if has_real_contribution:
        step = 5
        completed = True
    elif has_practice:
        step = 5
        completed = False
    elif has_calibration:
        step = 4
        completed = False
    elif has_identity:
        step = 2
        completed = False
    else:
        step = 0
        completed = False

    return {
        "step": step,
        "completed": completed,
        "started": True,
        "step_name": ONBOARDING_STEPS[min(step, len(ONBOARDING_STEPS) - 1)]["name"],
        "total_steps": len(ONBOARDING_STEPS),
        "has_identity": has_identity,
        "has_calibration": has_calibration,
        "has_practice": has_practice,
        "has_real_contribution": has_real_contribution,
    }


def get_onboarding_prompt(agent_id: str) -> dict[str, Any]:
    """Get the prompt for the current onboarding step.

    Returns:
        Dict with 'step', 'prompt', and instructions for the agent.
    """
    state = get_onboarding_state(agent_id)
    step = state["step"]

    prompts = {
        0: {
            "step": 0,
            "prompt": "Welcome to the OHM agora! Start by discovering what capabilities are available.",
            "action": "Call ohm_domain_onboarding to see node/edge types, layers, and available statistical methods.",
            "tool": "ohm_domain_onboarding",
        },
        1: {
            "step": 1,
            "prompt": "Register your identity. What is your focus area and what services can you provide?",
            "action": "Call ohm_update_state with your focus and services.",
            "tool": "ohm_update_state",
        },
        2: {
            "step": 2,
            "prompt": "Learn the domain vocabulary. Which standard identifiers and mapping edges are available?",
            "action": "Review the schema from ohm_domain_onboarding. Note standard IDs you can reference.",
            "tool": "ohm_domain_onboarding",
        },
        3: {
            "step": 3,
            "prompt": "Complete a quick calibration baseline. Make 3-5 predictions in your focus area with confidence levels.",
            "action": "State predictions as belief_statements. These are PRACTICE — they will NOT count toward your real calibration.",
            "tool": "ohm_belief",
            "is_practice": True,
        },
        4: {
            "step": 4,
            "prompt": "Practice a deliberation: propose a claim, see it challenged, submit evidence, and resolve.",
            "action": "Use ohm_deliberation to propose, challenge, evidence, and resolve. This is PRACTICE — it will NOT count toward your real reputation.",
            "tool": "ohm_deliberation",
            "is_practice": True,
        },
        5: {
            "step": 5,
            "prompt": "Make your first real contribution to the agora. Propose a claim, add an observation, or challenge an existing edge.",
            "action": "Use ohm_create_node, ohm_observe, or ohm_challenge. This IS your first real contribution — it will count toward your calibration.",
            "tool": "ohm_create_node",
            "is_practice": False,
        },
    }

    result = prompts.get(step, {"step": step, "prompt": "Onboarding complete.", "action": "You're ready to participate in the agora."})
    result["completed"] = state["completed"]
    result["total_steps"] = len(ONBOARDING_STEPS)
    result["step_name"] = ONBOARDING_STEPS[min(step, len(ONBOARDING_STEPS) - 1)]["name"]
    return result


def record_calibration_prediction(
    agent_id: str,
    target_node: str,
    predicted_probability: float,
) -> dict[str, Any]:
    """Record a calibration baseline prediction (OHM-798, step 4).

    IMPORTANT: Predictions are stored in conversation state as pending
    questions — NOT as durable ohm_observations rows. This ensures they
    do NOT appear in compute_agent_profile's output for the agent.

    Returns:
        Dict with recording confirmation.
    """
    thread_id = f"{_ONBOARDING_THREAD_PREFIX}:{agent_id}"
    store = get_store()

    store.add_pending_question(
        thread_id,
        {
            "type": "calibration_prediction",
            "text": f"calibration:{target_node}:P={predicted_probability:.2f}",
            "target_node": target_node,
            "predicted_probability": predicted_probability,
            "answered": False,
        },
    )

    return {
        "ok": True,
        "message": f"Practice prediction recorded for {target_node}. This will NOT count toward your real calibration.",
        "is_practice": True,
    }


def record_practice_deliberation(
    agent_id: str,
    target_node: str,
    action: str,
) -> dict[str, Any]:
    """Record a practice deliberation action (OHM-798, step 5).

    IMPORTANT: Practice deliberations use the conversation state's
    deliberation tracking — NOT the real deliberation state machine.
    This ensures they do NOT appear in the agent's real loop_risk or
    reputation scores.

    Returns:
        Dict with recording confirmation.
    """
    thread_id = f"{_ONBOARDING_THREAD_PREFIX}:{agent_id}"
    store = get_store()

    # Record in conversation state as a practice deliberation
    store.update_state(
        thread_id,
        {
            "deliberations": [
                {
                    "node_id": target_node,
                    "status": "practice",
                    "practice_action": action,
                    "is_practice": True,
                }
            ],
        },
    )

    return {
        "ok": True,
        "message": f"Practice deliberation action '{action}' recorded for {target_node}. This will NOT count toward your real reputation.",
        "is_practice": True,
    }


def is_onboarding_complete(agent_id: str) -> bool:
    """Check if an agent has completed onboarding."""
    return get_onboarding_state(agent_id).get("completed", False)


def is_in_practice_phase(agent_id: str) -> bool:
    """Check if an agent is in the practice phase (steps 3-4).

    During practice, contributions should NOT count toward real calibration.
    """
    state = get_onboarding_state(agent_id)
    return state["step"] in (3, 4) and not state["completed"]
