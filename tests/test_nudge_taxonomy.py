"""Tests for OHM-769: nudge taxonomy, severity scoring, and throttling."""

from __future__ import annotations

import pytest

from ohm.server.nudge_taxonomy import (
    NudgeCategory,
    SeverityLevel,
    NUDGE_TYPE_MAP,
    classify_nudge,
    compute_severity,
    NudgeThrottle,
)


class TestNudgeTypeMap:
    """Test the reconciliation of 19 existing nudge types to categories."""

    def test_all_19_existing_types_mapped(self):
        """Every existing nudge type string has a category mapping."""
        existing_types = [
            "babel_insight",
            "batch_suggestion",
            "causal_edge_confirmed",
            "causal_edge_missing_mechanism",
            "causal_edge_suggestion",
            "challenge_reminder",
            "cluster_synthesis",
            "confidence_outlier",
            "contradiction_alert",
            "decision_node_suggestion",
            "fast_decaying_observation",
            "high_confidence_weak_source",
            "inference_delta",
            "mechanism_gate",
            "pattern_detection",
            "pattern_to_causal_warning",
            "pert_estimation",
            "semantic_edge_warning",
            "source_citation",
            "value_contradiction",
        ]
        for nudge_type in existing_types:
            assert nudge_type in NUDGE_TYPE_MAP, f"{nudge_type} not mapped"

    def test_new_types_mapped(self):
        """New types from the agora design are mapped."""
        new_types = [
            "belief_statement_suggestion",
            "autonomy_nudge",
            "novelty_nudge",
            "method_divergence",
            "socratic_question",
            "evidence_race",
            "threshold_not_met",
            "human_escalation",
        ]
        for nudge_type in new_types:
            assert nudge_type in NUDGE_TYPE_MAP

    def test_pert_maps_to_pert_category(self):
        assert NUDGE_TYPE_MAP["pert_estimation"] == NudgeCategory.PERT

    def test_inference_delta_maps_to_voi(self):
        assert NUDGE_TYPE_MAP["inference_delta"] == NudgeCategory.VOI

    def test_contradiction_alert_maps_to_contradiction(self):
        assert NUDGE_TYPE_MAP["contradiction_alert"] == NudgeCategory.CONTRADICTION


class TestClassifyNudge:
    """Test classify_nudge function."""

    def test_classify_by_type(self):
        """Classification by type string works."""
        nudge = {"type": "pert_estimation", "message": "Consider a range."}
        category, severity = classify_nudge(nudge)
        assert category == NudgeCategory.PERT
        assert severity == SeverityLevel.SOFT

    def test_classify_with_explicit_category(self):
        """Explicit category overrides type lookup."""
        nudge = {"type": "unknown_type", "category": "calibration"}
        category, _ = classify_nudge(nudge)
        assert category == NudgeCategory.CALIBRATION

    def test_classify_with_severity_string(self):
        """String severity (hint/soft/firm) is parsed."""
        nudge = {"type": "contradiction_alert", "severity": "firm"}
        _, severity = classify_nudge(nudge)
        assert severity == SeverityLevel.FIRM

    def test_classify_unknown_type_defaults_to_structure(self):
        """Unknown type defaults to STRUCTURE category."""
        nudge = {"type": "totally_new_type"}
        category, _ = classify_nudge(nudge)
        assert category == NudgeCategory.STRUCTURE

    def test_classify_no_type_defaults_to_structure(self):
        """Missing type defaults to STRUCTURE."""
        nudge = {"message": "something"}
        category, _ = classify_nudge(nudge)
        assert category == NudgeCategory.STRUCTURE


class TestComputeSeverity:
    """Test severity computation with contextual factors."""

    def test_high_divergence_escalates_to_firm(self):
        """divergence >= 0.35 → at least FIRM."""
        result = compute_severity(
            SeverityLevel.CONTEXT,
            divergence=0.40,
        )
        assert result == SeverityLevel.FIRM

    def test_moderate_divergence_escalates_to_soft(self):
        """0.25 <= divergence < 0.35 → at least SOFT."""
        result = compute_severity(
            SeverityLevel.CONTEXT,
            divergence=0.28,
        )
        assert result == SeverityLevel.SOFT

    def test_blast_radius_escalates_one_level(self):
        """blast_radius='high' escalates by one level."""
        result = compute_severity(
            SeverityLevel.SOFT,
            blast_radius="high",
        )
        assert result == SeverityLevel.FIRM

    def test_blast_radius_caps_at_firm(self):
        """Severity never exceeds FIRM (3)."""
        result = compute_severity(
            SeverityLevel.FIRM,
            blast_radius="high",
        )
        assert result == SeverityLevel.FIRM

    def test_calibration_error_escalates(self):
        """High calibration error escalates severity."""
        result = compute_severity(
            SeverityLevel.CONTEXT,
            calibration_error=0.30,
        )
        assert result == SeverityLevel.SOFT

    def test_no_factors_returns_base(self):
        """No contextual factors → base severity unchanged."""
        result = compute_severity(SeverityLevel.SOFT)
        assert result == SeverityLevel.SOFT


class TestNudgeThrottle:
    """Test nudge throttling."""

    def test_first_nudge_emits(self):
        """First nudge for a target/type always emits."""
        throttle = NudgeThrottle(min_turns=5)
        assert throttle.should_emit("node-1", "voi") is True

    def test_throttle_blocks_within_min_turns(self):
        """Same nudge type within min_turns is throttled."""
        throttle = NudgeThrottle(min_turns=5)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        assert throttle.should_emit("node-1", "voi", turn=2) is False

    def test_throttle_allows_after_min_turns(self):
        """Nudge emits again after min_turns turns."""
        throttle = NudgeThrottle(min_turns=5)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        assert throttle.should_emit("node-1", "voi", turn=7) is True

    def test_belief_movement_overrides_throttle(self):
        """Significant belief movement allows nudge regardless of turns."""
        throttle = NudgeThrottle(min_turns=5, belief_movement_threshold=0.15)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        # Belief moved from 0.3 to 0.5 — exceeds threshold
        assert throttle.should_emit("node-1", "voi", current_belief=0.5, turn=2) is True

    def test_small_belief_movement_does_not_override(self):
        """Small belief movement doesn't override throttle."""
        throttle = NudgeThrottle(min_turns=5, belief_movement_threshold=0.15)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        assert throttle.should_emit("node-1", "voi", current_belief=0.35, turn=2) is False

    def test_different_targets_not_throttled(self):
        """Different targets are independent."""
        throttle = NudgeThrottle(min_turns=5)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        assert throttle.should_emit("node-2", "voi", turn=2) is True

    def test_different_types_not_throttled(self):
        """Different nudge types for same target are independent."""
        throttle = NudgeThrottle(min_turns=5)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        assert throttle.should_emit("node-1", "pert", turn=2) is True

    def test_reset_clears_state(self):
        """reset() clears all throttle history."""
        throttle = NudgeThrottle(min_turns=5)
        throttle.record("node-1", "voi", belief=0.3, turn=1)
        throttle.reset()
        assert throttle.should_emit("node-1", "voi", turn=2) is True

    def test_history_bounded(self):
        """History doesn't grow unboundedly."""
        throttle = NudgeThrottle(min_turns=1, max_history=3)
        for i in range(10):
            throttle.record("node-1", "voi", belief=float(i) * 0.1, turn=i)
        # Internal history should be bounded to max_history
        key = "node-1:voi"
        assert len(throttle._history[key]) <= 3
