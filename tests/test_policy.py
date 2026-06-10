"""Tests for belief-state decision (Phase 1 POMDP) — observe vs. act."""

from __future__ import annotations

import pytest

from tests.conftest import create_sample_edge, create_sample_node


class TestBeliefStateDecision:
    def test_policy_recommends_act_when_no_ancestors(self, test_db):
        from ohm.methods import belief_state_decision

        node = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9)
        result = belief_state_decision(test_db, node)
        assert result["method"] == "belief_state_decision"
        assert result["action"] == "act"
        assert result["reason"] == "no_ancestors"
        assert result["evpi"] == 0.0

    def test_policy_with_ancestors(self, test_db):
        from ohm.methods import belief_state_decision

        ancestor = create_sample_node(test_db, label="cause", confidence=0.5)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9)
        create_sample_edge(test_db, from_node=ancestor, to_node=target, edge_type="CAUSES", probability=0.8, confidence=0.6)
        result = belief_state_decision(test_db, target)
        assert result["method"] == "belief_state_decision"
        assert result["action"] in ("observe", "act")
        assert result["evpi"] >= 0
        assert result["observation_cost"] > 0

    def test_policy_observe_when_high_evpi(self, test_db):
        from ohm.methods import belief_state_decision

        ancestor = create_sample_node(test_db, label="uncertain", confidence=0.3)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9)
        create_sample_edge(test_db, from_node=ancestor, to_node=target, edge_type="CAUSES", probability=0.9, confidence=0.8)
        result = belief_state_decision(test_db, target, observation_cost=0.0001)
        assert result["action"] == "observe"
        assert result["evpi"] > result["observation_cost"]

    def test_policy_act_when_low_evpi(self, test_db):
        from ohm.methods import belief_state_decision

        ancestor = create_sample_node(test_db, label="certain", confidence=0.99)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.01)
        create_sample_edge(test_db, from_node=ancestor, to_node=target, edge_type="CAUSES", probability=0.1, confidence=0.1)
        result = belief_state_decision(test_db, target, observation_cost=100.0)
        assert result["action"] == "act"
        assert result["evpi"] <= result["observation_cost"]

    def test_policy_custom_horizon(self, test_db):
        from ohm.methods import belief_state_decision

        a1 = create_sample_node(test_db, label="c1", confidence=0.5)
        a2 = create_sample_node(test_db, label="c2", confidence=0.4)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9)
        create_sample_edge(test_db, from_node=a1, to_node=target, edge_type="CAUSES", probability=0.7)
        create_sample_edge(test_db, from_node=a2, to_node=target, edge_type="CAUSES", probability=0.6)
        result = belief_state_decision(test_db, target, horizon=2)
        assert result["horizon"] == 2

    def test_policy_returns_top_target(self, test_db):
        from ohm.methods import belief_state_decision

        ancestor = create_sample_node(test_db, label="cause", confidence=0.4)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9)
        create_sample_edge(test_db, from_node=ancestor, to_node=target, edge_type="CAUSES", probability=0.8, confidence=0.6)
        result = belief_state_decision(test_db, target)
        if result.get("top_target"):
            assert "node_id" in result["top_target"]
            assert "voi_score" in result["top_target"]

    def test_policy_with_usd_utility(self, test_db):
        from ohm.methods import belief_state_decision

        ancestor = create_sample_node(test_db, label="cause", confidence=0.5)
        target = create_sample_node(test_db, label="dec", node_type="decision", utility_scale=0.9, utility_usd_per_day=1_000_000)
        create_sample_edge(test_db, from_node=ancestor, to_node=target, edge_type="CAUSES", probability=0.8, confidence=0.6)
        result = belief_state_decision(test_db, target)
        assert result["method"] == "belief_state_decision"
        assert result["voi_units"] in ("usd", "dimensionless", "mixed")
