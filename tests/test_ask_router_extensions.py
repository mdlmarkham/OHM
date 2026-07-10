"""Tests for OHM-790: AskRouter statistical intent extensions."""

from __future__ import annotations

import pytest

from ohm.server.ask_router import AskRouter, detect_intent, INTENT_TOOLS, HANDLER_MAP


class TestNewIntentDetection:
    """Test that all 10 new intents are detected from natural language."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_belief_intent(self, router):
        assert detect_intent("how likely is this to happen?") == "belief"
        assert detect_intent("what is the probability of deal_closes?") == "belief"
        assert detect_intent("what are the odds of this outcome?") == "belief"

    def test_why_intent(self, router):
        assert detect_intent("why does it believe this is likely?") == "why"
        assert detect_intent("what drives the belief on target_node?") == "why"
        assert detect_intent("what evidence supports the probability?") == "why"

    def test_what_if_intent(self, router):
        assert detect_intent("counterfactual: what if we intervene on target_node?") == "what_if" or detect_intent("counterfactual analysis on target_node", declared_intent="what_if") == "what_if"
        assert detect_intent("do intervention on target_node") == "what_if"
        assert detect_intent("counterfactual on target_node") == "what_if"

    def test_voi_intent(self, router):
        assert detect_intent("what should I observe next?") == "voi"
        assert detect_intent("what should I look at to reduce uncertainty?") == "voi"
        assert detect_intent("what is the value of information?") == "voi"
        assert detect_intent("voi for target_node") == "voi"

    def test_simulate_intent(self, router):
        assert detect_intent("run simulations on this") == "simulate"
        assert detect_intent("monte carlo on target_node") == "simulate"
        assert detect_intent("run it many times") == "simulate"

    def test_scenario_intent(self, router):
        assert detect_intent("what is the range of outcomes?") == "scenario"
        assert detect_intent("pert estimate for target_node") == "scenario"
        assert detect_intent("best case worst case for target_node") == "scenario"

    def test_forecast_intent(self, router):
        assert detect_intent("markov analysis on target_node") == "forecast"
        assert detect_intent("steady state for target_node") == "forecast"
        assert detect_intent("expected steps for target_node") == "forecast"

    def test_calibrate_intent(self, router):
        assert detect_intent("do the methods agree?") == "calibrate"
        assert detect_intent("method divergence on target_node") == "calibrate"
        assert detect_intent("calibrate the belief") == "calibrate"

    def test_disagreement_intent(self, router):
        assert detect_intent("who disagrees on target_node?") == "disagreement"
        assert detect_intent("divergent views on target_node") == "disagreement"
        assert detect_intent("split belief on target_node", declared_intent="disagreement") == "disagreement"

    def test_decision_ready_intent(self, router):
        assert detect_intent("can we decide on this?") == "decision_ready"
        assert detect_intent("are we ready to decide?") == "decision_ready"
        assert detect_intent("is there enough evidence to decide?") == "decision_ready"


class TestToolCallsOutput:
    """Test that route() returns tool_calls for statistical intents."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_belief_tool_calls(self, router):
        result = router.route("how likely is target_node?", {"target": "target_node"})
        assert result["intent"] == "belief"
        assert "tool_calls" in result
        assert result["tool_calls"][0]["tool"] == "ohm_belief"
        assert result["tool_calls"][0]["arguments"]["target"] == "target_node"
        assert "reply_to_agent" in result

    def test_why_tool_calls(self, router):
        result = router.route("why does it believe target_node is likely?", {"target": "target_node"})
        assert result["intent"] == "why"
        assert result["tool_calls"][0]["tool"] == "ohm_belief"
        assert result["tool_calls"][0]["arguments"]["focus"] == "drivers"

    def test_what_if_tool_calls(self, router):
        result = router.route("counterfactual on target_node", {"target": "target_node", "state": 0, "intent": "what_if"})
        assert result["intent"] == "what_if"
        assert result["tool_calls"][0]["tool"] == "ohm_intervene"
        assert result["tool_calls"][0]["arguments"]["state"] == 0

    def test_voi_tool_calls(self, router):
        result = router.route("what should I observe for target_node?", {"target": "target_node"})
        assert result["intent"] == "voi"
        assert result["tool_calls"][0]["tool"] == "ohm_voi"

    def test_simulate_tool_calls(self, router):
        result = router.route("run monte carlo on target_node", {"target": "target_node", "n_simulations": 500})
        assert result["intent"] == "simulate"
        assert result["tool_calls"][0]["tool"] == "ohm_monte_carlo"
        assert result["tool_calls"][0]["arguments"]["n_simulations"] == 500

    def test_scenario_tool_calls(self, router):
        result = router.route("what is the range for target_node?", {"target": "target_node"})
        assert result["intent"] == "scenario"
        assert result["tool_calls"][0]["tool"] == "ohm_pert"

    def test_forecast_tool_calls(self, router):
        result = router.route("markov steady state for target_node", {"target": "target_node"})
        assert result["intent"] == "forecast"
        assert result["tool_calls"][0]["tool"] == "ohm_markov"

    def test_calibrate_tool_calls(self, router):
        result = router.route("do the methods agree on target_node?", {"target": "target_node"})
        assert result["intent"] == "calibrate"
        assert result["tool_calls"][0]["tool"] == "ohm_belief"

    def test_disagreement_tool_calls(self, router):
        result = router.route("who disagrees on target_node?", {"target": "target_node"})
        assert result["intent"] == "disagreement"
        assert result["tool_calls"][0]["tool"] == "ohm_belief"

    def test_decision_ready_tool_calls(self, router):
        result = router.route("can we decide on target_node?", {"target": "target_node"})
        assert result["intent"] == "decision_ready"
        assert result["tool_calls"][0]["tool"] == "ohm_conversation"


class TestConfidenceScoring:
    """Test that route() returns a confidence score."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_confidence_is_float(self, router):
        result = router.route("how likely is target_node?", {})
        assert "confidence" in result
        assert isinstance(result["confidence"], float)
        assert 0.0 <= result["confidence"] <= 1.0

    def test_confidence_high_for_strong_match(self, router):
        result = router.route("what is the probability of target_node?", {})
        assert result["confidence"] >= 0.5

    def test_confidence_low_for_default(self, router):
        result = router.route("gibberish xyz pdq", {})
        assert result["intent"] == "ask"
        assert result["confidence"] == 0.0


class TestLoopPrevention:
    """Test loop-prevention routing (OHM-790)."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_autonomy_nudge_when_no_claims(self, router):
        result = router.route(
            "how likely is target_node?",
            {"target": "target_node", "conversation_state": {"agent_contributions": {}}},
        )
        assert "loop_prevention" in result
        types = [n["type"] for n in result["loop_prevention"]]
        assert "autonomy" in types

    def test_no_autonomy_nudge_when_claims_exist(self, router):
        result = router.route(
            "how likely is target_node?",
            {
                "target": "target_node",
                "conversation_state": {"agent_contributions": {"a1": {"claims": 1}}},
            },
        )
        if "loop_prevention" in result:
            types = [n["type"] for n in result["loop_prevention"]]
            assert "autonomy" not in types

    def test_loop_detected_on_repeated_queries(self, router):
        conv_state = {
            "topics": [{"node_id": "target_node", "mentions": 5}],
        }
        result = router.route(
            "how likely is target_node?",
            {"target": "target_node", "conversation_state": conv_state},
        )
        assert "loop_prevention" in result
        types = [n["type"] for n in result["loop_prevention"]]
        assert "loop_detected" in types

    def test_no_loop_detected_on_few_mentions(self, router):
        conv_state = {
            "topics": [{"node_id": "target_node", "mentions": 2}],
        }
        result = router.route(
            "how likely is target_node?",
            {"target": "target_node", "conversation_state": conv_state},
        )
        loop_types = []
        if "loop_prevention" in result:
            loop_types = [n["type"] for n in result["loop_prevention"]]
        assert "loop_detected" not in loop_types

    def test_no_loop_prevention_without_conversation_state(self, router):
        result = router.route("how likely is target_node?", {"target": "target_node"})
        assert "loop_prevention" not in result


class TestBackwardCompat:
    """Verify existing intents still work after the extension."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_existing_intents_preserved(self, router):
        assert detect_intent("show me semantic metrics") == "analytics"
        assert detect_intent("explore around andgate") == "explore"
        assert detect_intent("challenge this claim") == "challenge"
        assert detect_intent("what if andgate fails") == "what-if"
        assert detect_intent("help with the API") == "help"

    def test_response_still_has_intent_handler_payload_links(self, router):
        result = router.route("show stats", {})
        assert "intent" in result
        assert "handler" in result
        assert "payload" in result
        assert "links" in result

    def test_default_intent_returns_zero_confidence(self, router):
        result = router.route("gibberish", {})
        assert result["intent"] == "ask"
        assert result["confidence"] == 0.0
        assert "tool_calls" not in result


class TestIntentToolsMap:
    def test_all_new_intents_have_tool_mappings(self):
        new_intents = ["belief", "why", "what_if", "voi", "simulate", "scenario", "forecast", "calibrate", "disagreement", "decision_ready"]
        for intent in new_intents:
            assert intent in INTENT_TOOLS, f"Missing tool for intent: {intent}"
            assert INTENT_TOOLS[intent].startswith("ohm_")

    def test_all_new_intents_have_handlers(self):
        new_intents = ["belief", "why", "what_if", "voi", "simulate", "scenario", "forecast", "calibrate", "disagreement", "decision_ready"]
        for intent in new_intents:
            assert intent in HANDLER_MAP, f"Missing handler for intent: {intent}"
