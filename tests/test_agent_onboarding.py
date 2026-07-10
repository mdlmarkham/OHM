"""Tests for OHM-798: Guided agent onboarding to the OHM agora."""

from __future__ import annotations

import pytest

from ohm.mcp.agent_onboarding import (
    get_onboarding_state,
    get_onboarding_prompt,
    record_calibration_prediction,
    record_practice_deliberation,
    is_onboarding_complete,
    is_in_practice_phase,
    ONBOARDING_STEPS,
)
from ohm.mcp.conversation_state import _reset_store


@pytest.fixture(autouse=True)
def _clean_store():
    _reset_store()
    yield
    _reset_store()


class TestOnboardingSteps:
    def test_has_6_steps(self):
        assert len(ONBOARDING_STEPS) == 6

    def test_step_names(self):
        names = [s["name"] for s in ONBOARDING_STEPS]
        assert "Capability Discovery" in names
        assert "Calibration Baseline" in names
        assert "Practice Deliberation" in names
        assert "First Real Contribution" in names


class TestInitialState:
    def test_new_agent_starts_at_step_0(self):
        state = get_onboarding_state("new-agent")
        assert state["step"] == 0
        assert state["completed"] is False
        assert state["started"] is False

    def test_initial_prompt(self):
        prompt = get_onboarding_prompt("new-agent")
        assert prompt["step"] == 0
        assert "welcome" in prompt["prompt"].lower() or "capability" in prompt["prompt"].lower()
        assert prompt["tool"] == "ohm_domain_onboarding"


class TestCalibrationBaseline:
    def test_records_prediction(self):
        result = record_calibration_prediction("agent-a", "target_node", 0.3)
        assert result["ok"] is True
        assert result["is_practice"] is True

    def test_prediction_stored_in_conversation_state(self):
        record_calibration_prediction("agent-a", "target_node", 0.3)
        state = get_onboarding_state("agent-a")
        # Should have progressed past step 0
        assert state["started"] is True
        assert state["has_calibration"] is True

    def test_prediction_does_not_create_observation(self):
        """CRITICAL: Practice predictions must NOT be durable ohm_observations."""
        record_calibration_prediction("agent-a", "target_node", 0.3)
        from ohm.mcp.conversation_state import get_store

        get_store().evict("onboarding:agent-a")
        state = get_onboarding_state("agent-a")
        # After eviction, state should show not started (data was ephemeral)
        assert state["started"] is False
        assert not state.get("has_calibration", False)


class TestPracticeDeliberation:
    def test_records_action(self):
        result = record_practice_deliberation("agent-a", "target_node", "propose")
        assert result["ok"] is True
        assert result["is_practice"] is True

    def test_practice_stored_in_conversation_state(self):
        record_practice_deliberation("agent-a", "target_node", "propose")
        state = get_onboarding_state("agent-a")
        assert state["started"] is True

    def test_practice_does_not_create_edges(self):
        """CRITICAL: Practice deliberations must NOT create durable ohm_edges."""
        record_practice_deliberation("agent-a", "target_node", "propose")
        from ohm.mcp.conversation_state import get_store

        get_store().evict("onboarding:agent-a")
        state = get_onboarding_state("agent-a")
        assert state["started"] is False
        assert not state.get("has_practice", False)


class TestPracticePhaseDetection:
    def test_in_practice_phase_during_calibration(self):
        record_calibration_prediction("agent-a", "target_node", 0.3)
        assert is_in_practice_phase("agent-a") is True

    def test_not_in_practice_phase_before_starting(self):
        assert is_in_practice_phase("new-agent") is False

    def test_not_in_practice_phase_after_completion(self):
        # Simulate completion by recording a real contribution
        from ohm.mcp.conversation_state import get_store

        store = get_store()
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "observations")
        assert is_in_practice_phase("agent-a") is False


class TestCompletionDetection:
    def test_not_complete_for_new_agent(self):
        assert is_onboarding_complete("new-agent") is False

    def test_complete_after_real_contribution(self):
        from ohm.mcp.conversation_state import get_store

        store = get_store()
        # Record enough contributions to trigger completion
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "observations")
        assert is_onboarding_complete("agent-a") is True


class TestPromptProgression:
    def test_step_0_prompt(self):
        prompt = get_onboarding_prompt("new-agent")
        assert prompt["step"] == 0
        assert "completed" in prompt

    def test_step_3_prompt_is_practice(self):
        record_calibration_prediction("agent-a", "target_node", 0.3)
        prompt = get_onboarding_prompt("agent-a")
        assert prompt["step"] >= 3
        assert prompt.get("is_practice") is True

    def test_step_5_prompt_is_not_practice(self):
        from ohm.mcp.conversation_state import get_store

        store = get_store()
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "claims")
        store.record_contribution("onboarding:agent-a", "agent-a", "observations")
        prompt = get_onboarding_prompt("agent-a")
        assert prompt["step"] == 5
        assert prompt.get("is_practice") is False


class TestCalibrationIsolation:
    """CRITICAL: Completing onboarding must NOT change any agent's real
    calibration/reputation baseline. Practice data is in conversation
    state, not in ohm_observations/ohm_edges."""

    def test_practice_prediction_not_in_compute_agent_profile(self, test_db):
        """Practice predictions must not appear in compute_agent_profile."""
        from ohm.graph.calibration import compute_agent_profile

        # Record practice predictions
        record_calibration_prediction("agent-a", "target_node", 0.3)
        record_calibration_prediction("agent-a", "target_node2", 0.7)

        # compute_agent_profile reads from ohm_observations/ohm_edges,
        # NOT from conversation state. So practice predictions should
        # have zero effect on the profile.
        profile = compute_agent_profile(test_db, "agent-a")
        assert profile["brier_score"] == 0.0  # no real observations
        assert profile["novelty_score"] == 0.0  # no real observations
        assert profile["point_estimate_bias"] == 0.0  # no real edges

    def test_practice_deliberation_not_in_loop_risk(self, test_db):
        """Practice deliberations must not affect loop_risk scoring."""
        from ohm.graph.calibration import compute_agent_profile

        record_practice_deliberation("agent-a", "target_node", "propose")
        record_practice_deliberation("agent-a", "target_node", "challenge")

        profile = compute_agent_profile(test_db, "agent-a")
        # loop_risk is computed from ohm_belief_calibration_log, not
        # conversation state. Practice deliberations don't write to
        # that table, so loop_risk should be empty.
        assert profile["loop_risk"] == {}
        assert profile["max_loop_risk"] == 0.0

    def test_onboarding_does_not_affect_other_agents(self, test_db):
        """Completing onboarding must not change other agents' profiles."""
        from ohm.graph.calibration import compute_agent_profile

        # Get baseline for agent-b
        baseline = compute_agent_profile(test_db, "agent-b")

        # Agent-a does onboarding (practice)
        record_calibration_prediction("agent-a", "target_node", 0.3)
        record_practice_deliberation("agent-a", "target_node", "propose")

        # Agent-b's profile should be unchanged
        after = compute_agent_profile(test_db, "agent-b")
        assert after == baseline
