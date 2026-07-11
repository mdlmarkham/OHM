"""Tests for OHM-768: agent calibration scoring."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema
from ohm.graph.queries import query_source_reliability


class TestBeliefCalibrationLog:
    """Test the belief calibration log table and scoring."""

    def test_calibration_log_table_exists(self, test_db):
        """ohm_belief_calibration_log table exists after schema init."""
        result = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_name = 'ohm_belief_calibration_log'").fetchone()
        assert result is not None

    def test_reliability_includes_belief_calibration(self, test_db):
        """query_source_reliability includes belief_calibration field."""
        result = query_source_reliability(test_db, "test-agent")
        assert "belief_calibration" in result
        bc = result["belief_calibration"]
        assert "total_belief_statements" in bc
        assert "belief_calibration_error" in bc
        assert "directional_accuracy" in bc
        assert "brier_score" in bc
        assert "overconfidence_rate" in bc

    def test_empty_calibration_returns_zeros(self, test_db):
        """No belief statements → all metrics are None/0."""
        result = query_source_reliability(test_db, "new-agent")
        bc = result["belief_calibration"]
        assert bc["total_belief_statements"] == 0
        assert bc["belief_calibration_error"] is None
        assert bc["brier_score"] is None

    def test_calibration_with_logged_statements(self, test_db):
        """Log belief statements, verify metrics are computed."""
        # Insert calibration log entries
        for claimed, graph_p, actual in [
            (0.3, 0.35, None),  # close — low divergence
            (0.8, 0.3, None),  # far — high divergence, overconfident
            (0.5, 0.5, True),  # exact match, resolved true
            (0.4, 0.6, False),  # moderate divergence, resolved false
        ]:
            div = abs(claimed - graph_p)
            test_db.execute(
                "INSERT INTO ohm_belief_calibration_log (agent_name, claimed_probability, graph_posterior, divergence, actual_state) VALUES (?, ?, ?, ?, ?)",
                ["test-agent", claimed, graph_p, div, actual],
            )

        result = query_source_reliability(test_db, "test-agent")
        bc = result["belief_calibration"]
        assert bc["total_belief_statements"] == 4
        assert bc["belief_calibration_error"] is not None
        assert bc["directional_accuracy"] is not None
        assert bc["overconfidence_rate"] is not None
        # Brier score computed from 2 resolved entries
        assert bc["brier_score"] is not None

    def test_brier_score_calculation(self, test_db):
        """Brier score = avg((claimed - actual)^2) for resolved entries."""
        # claimed=0.3, actual=False(0) → (0.3-0)^2 = 0.09
        # claimed=0.7, actual=True(1)  → (0.7-1)^2 = 0.09
        # avg = 0.09
        for claimed, actual in [(0.3, False), (0.7, True)]:
            test_db.execute(
                "INSERT INTO ohm_belief_calibration_log (agent_name, claimed_probability, graph_posterior, divergence, actual_state) VALUES (?, ?, ?, ?, ?)",
                ["brier-agent", claimed, 0.5, abs(claimed - 0.5), actual],
            )

        result = query_source_reliability(test_db, "brier-agent")
        bc = result["belief_calibration"]
        assert bc["brier_score"] == 0.09

    def test_overconfidence_rate(self, test_db):
        """overconfidence_rate = fraction with divergence >= 0.25."""
        # 2 of 4 have divergence >= 0.25 → rate = 0.5
        entries = [
            (0.3, 0.35, abs(0.3 - 0.35)),  # div=0.05 — not overconfident
            (0.8, 0.3, abs(0.8 - 0.3)),  # div=0.50 — overconfident
            (0.5, 0.5, 0.0),  # div=0.00 — not overconfident
            (0.9, 0.6, abs(0.9 - 0.6)),  # div=0.30 — overconfident
        ]
        for claimed, graph_p, div in entries:
            test_db.execute(
                "INSERT INTO ohm_belief_calibration_log (agent_name, claimed_probability, graph_posterior, divergence, actual_state) VALUES (?, ?, ?, ?, ?)",
                ["overconf-agent", claimed, graph_p, div, None],
            )

        result = query_source_reliability(test_db, "overconf-agent")
        bc = result["belief_calibration"]
        assert bc["overconfidence_rate"] == 0.5
