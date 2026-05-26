"""Tests for POMDP Phase 1: Belief-State Policy (OHM-od01.5)."""

from __future__ import annotations

import pytest

from ohm.inference.pomdp import compute_policy


class TestComputePolicy:
    def test_policy_returns_recommendation(self, db):
        from tests.conftest import create_sample_node, create_sample_edge

        target = create_sample_node(db, label="test_decision", node_type="decision")
        cause = create_sample_node(db, label="cause_node")
        create_sample_edge(db, from_node=cause, to_node=target, edge_type="CAUSES", layer="L3", confidence=0.8)

        result = compute_policy(db, target)
        assert "recommendation" in result
        assert result["recommendation"] in ("observe", "act")
        assert "evpi" in result
        assert "reasoning" in result

    def test_policy_horizon_not_implemented(self, db):
        from tests.conftest import create_sample_node

        target = create_sample_node(db, label="test_decision", node_type="decision")

        result = compute_policy(db, target, horizon=5)
        assert result["horizon"] == 5
        assert result["recommendation"] == "act"
        assert "not yet implemented" in result["reasoning"]

    def test_policy_with_voi_candidates(self, db):
        from tests.conftest import create_sample_node, create_sample_edge

        target = create_sample_node(db, label="test_decision", node_type="decision")
        cause_a = create_sample_node(db, label="cause_a")
        cause_b = create_sample_node(db, label="cause_b")
        create_sample_edge(db, from_node=cause_a, to_node=target, edge_type="CAUSES", layer="L3", confidence=0.8)
        create_sample_edge(db, from_node=cause_b, to_node=target, edge_type="CAUSES", layer="L3", confidence=0.6)

        result = compute_policy(db, target, cost_of_observation=0.1)
        assert result["voi_rankings_used"] >= 0
        assert "top_voi_candidates" in result

    def test_policy_returns_current_belief(self, db):
        from tests.conftest import create_sample_node, create_sample_edge

        target = create_sample_node(db, label="test_decision", node_type="decision")
        cause = create_sample_node(db, label="cause_node")
        create_sample_edge(db, from_node=cause, to_node=target, edge_type="CAUSES", layer="L3", confidence=0.9)

        result = compute_policy(db, target)
        assert "current_belief" in result
        assert "good" in result["current_belief"]
        assert "bad" in result["current_belief"]

    def test_policy_act_when_no_causal_ancestors(self, db):
        from tests.conftest import create_sample_node

        target = create_sample_node(db, label="test_decision", node_type="decision")

        result = compute_policy(db, target, cost_of_observation=0.1)
        assert result["recommendation"] == "act"

    def test_policy_returns_method(self, db):
        from tests.conftest import create_sample_node

        target = create_sample_node(db, label="test_decision", node_type="decision")

        result = compute_policy(db, target)
        assert result["method"] == "belief_state_policy"
        assert result["target"] == target
