"""Tests for OHM-766: belief_statement parsing and comparison."""

from __future__ import annotations

import pytest

from ohm.mcp.belief import (
    parse_belief_statement,
    compare_belief_to_posterior,
    build_belief_log_entry,
)


class TestParseBeliefStatement:
    """Test belief statement parsing."""

    def test_parse_standard_format(self):
        """P(target=bad) = 0.3"""
        result = parse_belief_statement("P(hormuz=bad) = 0.3")
        assert result is not None
        assert result["target"] == "hormuz"
        assert result["state"] == "bad"
        assert result["claimed_probability"] == 0.3

    def test_parse_approx_format(self):
        """P(target=bad) ≈ 0.3"""
        result = parse_belief_statement("P(target=bad) ≈ 0.3")
        assert result is not None
        assert result["claimed_probability"] == 0.3

    def test_parse_good_state(self):
        """P(target=good) = 0.8"""
        result = parse_belief_statement("P(target=good) = 0.8")
        assert result is not None
        assert result["state"] == "good"
        assert result["claimed_probability"] == 0.8

    def test_parse_arbitrary_state(self):
        """P(target=high) = 0.7 — arbitrary state identifier (OHM-778/779)"""
        result = parse_belief_statement("P(risk=critical) = 0.4")
        assert result is not None
        assert result["target"] == "risk"
        assert result["state"] == "critical"
        assert result["claimed_probability"] == 0.4

    def test_parse_hyphenated_state(self):
        """P(target=high-risk) = 0.6"""
        result = parse_belief_statement("P(system=high-risk) = 0.6")
        assert result is not None
        assert result["target"] == "system"
        assert result["state"] == "high-risk"

    def test_parse_bare_probability(self):
        """Just a number."""
        result = parse_belief_statement("0.3")
        assert result is not None
        assert result["claimed_probability"] == 0.3
        assert result["target"] is None

    def test_parse_natural_language(self):
        """I believe P(node-id=bad) is about 0.3"""
        result = parse_belief_statement("I believe P(node-id=bad) is about 0.3")
        assert result is not None
        assert result["target"] == "node-id"
        assert result["claimed_probability"] == 0.3

    def test_parse_empty_returns_none(self):
        """Empty string returns None."""
        assert parse_belief_statement("") is None
        assert parse_belief_statement(None) is None

    def test_parse_no_probability_returns_none(self):
        """String without a probability returns None."""
        assert parse_belief_statement("I think it's bad") is None

    def test_parse_out_of_range_returns_none(self):
        """Probability > 1 returns None."""
        assert parse_belief_statement("P(target=bad) = 1.5") is None


class TestCompareBeliefToPosterior:
    """Test belief-to-posterior comparison."""

    def test_close_belief_is_silent(self):
        """|diff| < 0.15 → severity 0."""
        result = compare_belief_to_posterior(0.3, {"P(bad)": 0.35}, "bad")
        assert result["severity"] == 0
        assert result["agree"] is True
        assert result["divergence"] < 0.15

    def test_moderate_divergence_is_soft_nudge(self):
        """0.15 <= |diff| < 0.25 → severity 1."""
        result = compare_belief_to_posterior(0.2, {"P(bad)": 0.4}, "bad")
        assert result["severity"] == 1
        assert result["agree"] is False

    def test_large_divergence_is_firm_flag(self):
        """|diff| >= 0.35 → severity 3."""
        result = compare_belief_to_posterior(0.1, {"P(bad)": 0.6}, "bad")
        assert result["severity"] == 3
        assert result["agree"] is False

    def test_overconfident_direction(self):
        """Claimed > graph → overconfident."""
        result = compare_belief_to_posterior(0.8, {"P(bad)": 0.3}, "bad")
        assert result["direction"] == "overconfident"

    def test_underconfident_direction(self):
        """Claimed < graph → underconfident."""
        result = compare_belief_to_posterior(0.1, {"P(bad)": 0.5}, "bad")
        assert result["direction"] == "underconfident"


class TestBuildBeliefLogEntry:
    """Test calibration log entry building."""

    def test_log_entry_has_required_fields(self):
        result = build_belief_log_entry(
            agent_name="metis",
            target="concept-hormuz",
            claimed=0.3,
            graph_p=0.5,
            tool_name="ohm_create_edge",
            edge_or_node_id="edge-123",
        )
        assert result["agent_name"] == "metis"
        assert result["target_node"] == "concept-hormuz"
        assert result["claimed_probability"] == 0.3
        assert result["graph_posterior"] == 0.5
        assert result["divergence"] == 0.2
        assert result["tool"] == "ohm_create_edge"
        assert result["actual_state"] is None
        assert "timestamp" in result

    def test_log_entry_divergence_calculation(self):
        result = build_belief_log_entry(
            agent_name="test",
            target=None,
            claimed=0.7,
            graph_p=0.4,
            tool_name="ohm_observe",
        )
        assert result["divergence"] == 0.3
