"""Tests for OHM-791: Agora-aware nudge taxonomy extensions."""

from __future__ import annotations

import pytest

from ohm.mcp.agora_nudges import (
    generate_agora_nudges,
    _build_nudge,
    _reset_throttle,
    _belief_p,
    _estimate_turn,
)
from ohm.server.nudge_taxonomy import (
    NudgeCategory,
    SeverityLevel,
    NUDGE_TYPE_MAP,
    classify_nudge,
    compute_severity,
    NudgeThrottle,
)


@pytest.fixture(autouse=True)
def _clean_throttle():
    _reset_throttle()
    yield
    _reset_throttle()


class TestNudgeTypeMap:
    """Verify all 9 new types are in NUDGE_TYPE_MAP."""

    def test_all_new_types_classified(self):
        new_types = [
            "socratic_falsifiability",
            "socratic_steel_man",
            "evidence_race",
            "threshold_not_met",
            "human_escalation",
            "autonomy",
            "loop_detected",
            "dissent_rewarded",
            "method_divergence",
        ]
        for t in new_types:
            assert t in NUDGE_TYPE_MAP, f"Missing type: {t}"

    def test_socratic_types_in_socratic_category(self):
        assert NUDGE_TYPE_MAP["socratic_falsifiability"] == NudgeCategory.SOCRATIC
        assert NUDGE_TYPE_MAP["socratic_steel_man"] == NudgeCategory.SOCRATIC

    def test_autonomy_types_in_autonomy_category(self):
        assert NUDGE_TYPE_MAP["autonomy"] == NudgeCategory.AUTONOMY
        assert NUDGE_TYPE_MAP["loop_detected"] == NudgeCategory.AUTONOMY

    def test_dissent_rewarded_in_novelty_category(self):
        assert NUDGE_TYPE_MAP["dissent_rewarded"] == NudgeCategory.NOVELTY


class TestBuildNudge:
    def test_basic_nudge_structure(self):
        n = _build_nudge(type="test", message="hello", severity=SeverityLevel.SOFT)
        assert n["type"] == "test"
        assert n["severity"] == 2
        assert n["message"] == "hello"
        assert "data" not in n
        assert "action" not in n
        assert "response_optional" not in n

    def test_nudge_with_data_and_action(self):
        n = _build_nudge(
            type="test",
            message="hello",
            data={"target": "n1"},
            action="do something",
        )
        assert n["data"] == {"target": "n1"}
        assert n["action"] == "do something"

    def test_response_optional_flag(self):
        n = _build_nudge(type="test", message="hello", response_optional=True)
        assert n["response_optional"] is True


class TestAutonomyNudge:
    def test_fires_when_agent_asks_belief_without_own_claim(self):
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state={"agent_contributions": {}},
        )
        types = [n["type"] for n in nudges]
        assert "autonomy" in types

    def test_suppressed_when_agent_has_own_claim(self):
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state={
                "agent_contributions": {"a1": {"claims": 1}},
            },
        )
        types = [n["type"] for n in nudges]
        assert "autonomy" not in types

    def test_not_fired_for_non_belief_tools(self):
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_search",
            kwargs={"q": "test"},
            agent_id="a1",
            response_data=None,
            conversation_state={"agent_contributions": {}},
        )
        types = [n["type"] for n in nudges]
        assert "autonomy" not in types


class TestLoopDetectedNudge:
    def test_fires_on_repeated_queries(self):
        conv_state = {
            "topics": [{"node_id": "n1", "mentions": 6}],
            "agent_contributions": {"a1": {"loop_risk_max": 0.5}},
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        loop_nudges = [n for n in nudges if n["type"] == "loop_detected"]
        assert len(loop_nudges) == 1
        assert loop_nudges[0]["severity"] == 3  # FIRM

    def test_not_fires_on_few_mentions(self):
        conv_state = {
            "topics": [{"node_id": "n1", "mentions": 2}],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "loop_detected" not in types


class TestSocraticFalsifiabilityNudge:
    def test_fires_on_stalled_deliberation(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "challengers": ["a2", "a3"],
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "socratic_falsifiability" in types
        socratic = [n for n in nudges if n["type"] == "socratic_falsifiability"][0]
        assert socratic.get("response_optional") is True

    def test_not_fires_on_single_challenger(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "challengers": ["a2"],
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "socratic_falsifiability" not in types


class TestSocraticSteelManNudge:
    def test_fires_on_high_confidence_claim(self):
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_create_node",
            kwargs={"target": "n1", "confidence": 0.9},
            agent_id="a1",
            response_data=None,
            conversation_state={},
        )
        types = [n["type"] for n in nudges]
        assert "socratic_steel_man" in types
        steel_man = [n for n in nudges if n["type"] == "socratic_steel_man"][0]
        assert steel_man.get("response_optional") is True

    def test_not_fires_on_low_confidence(self):
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_create_node",
            kwargs={"target": "n1", "confidence": 0.5},
            agent_id="a1",
            response_data=None,
            conversation_state={},
        )
        types = [n["type"] for n in nudges]
        assert "socratic_steel_man" not in types


class TestEvidenceRaceNudge:
    def test_fires_when_challenge_resolvable_by_observation(self):
        conv_state = {
            "deliberations": [
                {"node_id": "n1", "status": "challenged"},
            ],
        }
        belief_data = {
            "top_voi_candidates": [
                {"node_id": "obs_candidate", "voi_score": 0.42},
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
            belief_data=belief_data,
        )
        types = [n["type"] for n in nudges]
        assert "evidence_race" in types

    def test_not_fires_without_voi_candidates(self):
        conv_state = {
            "deliberations": [
                {"node_id": "n1", "status": "challenged"},
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
            belief_data={},
        )
        types = [n["type"] for n in nudges]
        assert "evidence_race" not in types


class TestThresholdNotMetNudge:
    def test_fires_when_thresholds_unmet(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "decision_thresholds": {
                        "min_agents_met": True,
                        "disagreement_bound_met": False,
                        "evidence_freshness_met": False,
                    },
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "threshold_not_met" in types

    def test_not_fires_when_all_met(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "decision_thresholds": {
                        "min_agents_met": True,
                        "disagreement_bound_met": True,
                    },
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "threshold_not_met" not in types


class TestHumanEscalationNudge:
    def test_fires_on_high_blast_radius_with_unmet_thresholds(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "blast_radius": "high",
                    "decision_thresholds": {
                        "min_agents_met": True,
                        "disagreement_bound_met": False,
                    },
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "human_escalation" in types
        escalation = [n for n in nudges if n["type"] == "human_escalation"][0]
        assert escalation["severity"] == 3  # FIRM

    def test_not_fires_on_normal_blast_radius(self):
        conv_state = {
            "deliberations": [
                {
                    "node_id": "n1",
                    "status": "challenged",
                    "blast_radius": "normal",
                    "decision_thresholds": {
                        "disagreement_bound_met": False,
                    },
                }
            ],
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "human_escalation" not in types


class TestMethodDivergenceNudge:
    def test_fires_when_methods_disagree(self):
        belief_data = {
            "method_divergence": {
                "max_divergence": 0.35,
                "methods": ["bayesian", "pert", "markov"],
            },
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state={},
            belief_data=belief_data,
        )
        types = [n["type"] for n in nudges]
        assert "method_divergence" in types

    def test_not_fires_on_small_divergence(self):
        belief_data = {
            "method_divergence": {
                "max_divergence": 0.1,
                "methods": ["bayesian", "pert"],
            },
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state={},
            belief_data=belief_data,
        )
        types = [n["type"] for n in nudges]
        assert "method_divergence" not in types


class TestDissentRewardedNudge:
    def test_fires_on_high_novelty_observation(self):
        conv_state = {
            "agent_contributions": {
                "a1": {"novelty_score": 0.6, "claims": 1, "observations": 2},
            },
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_observe",
            kwargs={"node_id": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "dissent_rewarded" in types

    def test_not_fires_on_low_novelty(self):
        conv_state = {
            "agent_contributions": {
                "a1": {"novelty_score": 0.2, "claims": 1, "observations": 2},
            },
        }
        nudges = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_observe",
            kwargs={"node_id": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        types = [n["type"] for n in nudges]
        assert "dissent_rewarded" not in types


class TestThrottling:
    def test_same_nudge_throttled(self):
        conv_state = {
            "topics": [{"node_id": "n1", "mentions": 6}],
            "agent_contributions": {"a1": {"loop_risk_max": 0.5}},
        }
        # First call emits
        nudges1 = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        assert any(n["type"] == "loop_detected" for n in nudges1)

        # Second call (same turn, same target) is throttled
        nudges2 = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        assert not any(n["type"] == "loop_detected" for n in nudges2)

    def test_different_targets_not_throttled(self):
        conv_state = {
            "agent_contributions": {},
        }
        nudges1 = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n1"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        nudges2 = generate_agora_nudges(
            thread_id="t1",
            tool_name="ohm_belief",
            kwargs={"target": "n2"},
            agent_id="a1",
            response_data=None,
            conversation_state=conv_state,
        )
        assert any(n["type"] == "autonomy" for n in nudges1)
        assert any(n["type"] == "autonomy" for n in nudges2)


class TestNudgeStructure:
    """Verify all nudges have required fields."""

    def test_all_nudges_have_type_severity_message(self):
        scenarios = [
            {"tool_name": "ohm_belief", "kwargs": {"target": "n1"}, "conv": {"agent_contributions": {}}},
            {"tool_name": "ohm_create_node", "kwargs": {"target": "n1", "confidence": 0.9}, "conv": {}},
            {"tool_name": "ohm_observe", "kwargs": {"node_id": "n1"}, "conv": {"agent_contributions": {"a1": {"novelty_score": 0.6}}}},
        ]
        for scenario in scenarios:
            _reset_throttle()
            nudges = generate_agora_nudges(
                thread_id="t1",
                tool_name=scenario["tool_name"],
                kwargs=scenario["kwargs"],
                agent_id="a1",
                response_data=None,
                conversation_state=scenario["conv"],
            )
            for n in nudges:
                assert "type" in n, f"Missing type in nudge: {n}"
                assert "severity" in n, f"Missing severity in nudge: {n}"
                assert "message" in n, f"Missing message in nudge: {n}"
                assert isinstance(n["severity"], int)
                assert 0 <= n["severity"] <= 3


class TestHelperFunctions:
    def test_belief_p_extracts_posterior(self):
        assert _belief_p({"posterior": {"P(bad)": 0.3}}) == 0.3
        assert _belief_p({}) is None

    def test_estimate_turn_from_state(self):
        conv = {"topics": [{"mentions": 3}, {"mentions": 2}], "nudge_history": [{}, {}]}
        assert _estimate_turn(conv) == 7

    def test_estimate_turn_empty(self):
        assert _estimate_turn({}) == 0


class TestClassifyNudgeIntegration:
    """Verify classify_nudge works with all new types."""

    def test_classify_all_new_types(self):
        for nudge_type, expected_category in [
            ("socratic_falsifiability", NudgeCategory.SOCRATIC),
            ("socratic_steel_man", NudgeCategory.SOCRATIC),
            ("evidence_race", NudgeCategory.EVIDENCE_RACE),
            ("threshold_not_met", NudgeCategory.THRESHOLD_NOT_MET),
            ("human_escalation", NudgeCategory.HUMAN_ESCALATION),
            ("autonomy", NudgeCategory.AUTONOMY),
            ("loop_detected", NudgeCategory.AUTONOMY),
            ("dissent_rewarded", NudgeCategory.NOVELTY),
            ("method_divergence", NudgeCategory.METHOD_DIVERGENCE),
        ]:
            nudge = {"type": nudge_type, "message": "test"}
            category, severity = classify_nudge(nudge)
            assert category == expected_category, f"Wrong category for {nudge_type}"
