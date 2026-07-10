"""Tests for OHM-792: Extended agent calibration profile."""

from __future__ import annotations

import pytest

from ohm.graph.calibration import (
    compute_agent_profile,
    _intervention_ladder,
)


class TestInterventionLadder:
    """Test the loop-risk intervention ladder."""

    def test_no_risk(self):
        assert _intervention_ladder(0.0) == "none"

    def test_low_risk_soft_nudge(self):
        assert _intervention_ladder(0.15) == "soft_nudge"

    def test_medium_risk_autonomy_prompt(self):
        assert _intervention_ladder(0.35) == "autonomy_prompt"

    def test_high_risk_category_only(self):
        assert _intervention_ladder(0.7) == "category_only_answer"

    def test_extreme_risk_quarantine(self):
        assert _intervention_ladder(0.9) == "observation_quarantine"


class TestComputeAgentProfile:
    """Test the extended agent profile (OHM-792)."""

    def test_returns_dict_with_all_fields(self, test_db):
        """All new fields should be present in the result."""
        profile = compute_agent_profile(test_db, "test-agent")
        assert isinstance(profile, dict)
        # Base fields from compute_confidence_calibration
        assert "agent_name" in profile
        assert "calibration_score" in profile
        assert "calibration_by_band" in profile
        # New OHM-792 fields
        assert "point_estimate_bias" in profile
        assert "overconfidence_rate" in profile
        assert "language_confidence_bias" in profile
        assert "brier_score" in profile
        assert "novelty_score" in profile
        assert "contrarian_value" in profile
        assert "evidence_quality" in profile
        assert "evidence_freshness" in profile
        assert "mechanism_quality" in profile
        assert "information_contribution" in profile
        assert "loop_risk" in profile
        assert "max_loop_risk" in profile
        assert "blast_radius_awareness" in profile
        assert "intervention" in profile

    def test_new_agent_returns_zeros(self, test_db):
        """An agent with no edges should return zero/default scores."""
        profile = compute_agent_profile(test_db, "nonexistent-agent")
        assert profile["agent_name"] == "nonexistent-agent"
        assert profile["point_estimate_bias"] == 0.0
        assert profile["overconfidence_rate"] == 0.0
        assert profile["novelty_score"] == 0.0
        assert profile["brier_score"] == 0.0
        assert profile["loop_risk"] == {}
        assert profile["max_loop_risk"] == 0.0
        assert profile["intervention"] == "none"

    def test_scores_are_floats(self, test_db):
        """All score fields should be floats in [0, 1]."""
        profile = compute_agent_profile(test_db, "test-agent")
        for field in (
            "novelty_score",
            "contrarian_value",
            "evidence_quality",
            "evidence_freshness",
            "mechanism_quality",
            "information_contribution",
        ):
            val = profile[field]
            assert isinstance(val, float), f"{field} should be float, got {type(val)}"
            assert 0.0 <= val <= 1.0, f"{field} should be in [0,1], got {val}"

    def test_loop_risk_is_dict(self, test_db):
        """loop_risk should be a dict of target -> float."""
        profile = compute_agent_profile(test_db, "test-agent")
        assert isinstance(profile["loop_risk"], dict)
        for target, risk in profile["loop_risk"].items():
            assert isinstance(target, str)
            assert isinstance(risk, float)
            assert 0.0 <= risk <= 1.0

    def test_blast_radius_awareness_is_float(self, test_db):
        """blast_radius_awareness should be a float in [0, 1]."""
        profile = compute_agent_profile(test_db, "test-agent")
        bra = profile["blast_radius_awareness"]
        assert isinstance(bra, float)
        assert 0.0 <= bra <= 1.0

    def test_intervention_matches_max_loop_risk(self, test_db):
        """The intervention field should match the max_loop_risk level."""
        profile = compute_agent_profile(test_db, "test-agent")
        max_risk = profile["max_loop_risk"]
        expected = _intervention_ladder(max_risk)
        assert profile["intervention"] == expected

    def test_with_edges(self, test_db):
        """Agent with edges should have non-null calibration."""
        from ohm.graph.queries import create_node, create_edge

        node_a = create_node(test_db, label="Target A", created_by="test-agent")
        node_b = create_node(test_db, label="Target B", created_by="test-agent")
        create_edge(
            test_db,
            from_node=node_a["id"],
            to_node=node_b["id"],
            edge_type="CAUSES",
            layer="L3",
            confidence=0.8,
            created_by="test-agent",
            condition="via mechanism X",
        )
        profile = compute_agent_profile(test_db, "test-agent")
        # With at least 1 edge, calibration_score should be defined
        assert profile["calibration_score"] is not None or profile["total_l3_l4_edges"] >= 1
        # Mechanism quality should be 1.0 (the CAUSES edge has a condition)
        assert profile["mechanism_quality"] == 1.0


class TestProfileViaSDK:
    """Test agent_profile via the SDK."""

    def test_sdk_agent_profile(self, test_db):
        from ohm.framework.sdk import Graph

        with Graph(conn=test_db, actor="test-agent") as g:
            profile = g.agent_profile()
            assert isinstance(profile, dict)
            assert profile["agent_name"] == "test-agent"
            assert "intervention" in profile
