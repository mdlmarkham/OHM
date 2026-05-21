"""Tests for ohm.utils.discretize_evidence (OHM-mk5v)."""

import pytest
from ohm.utils import discretize_evidence


class TestDiscretizeEvidence:
    """Batch alarm-threshold discretization."""

    def test_value_above_high_alarm_is_bad(self):
        result = discretize_evidence({"temp": 105.0}, {"temp": (0.0, 100.0)})
        assert result == {"temp": 0}

    def test_value_below_low_alarm_is_bad(self):
        result = discretize_evidence({"ph": 6.5}, {"ph": (7.0, 14.0)})
        assert result == {"ph": 0}

    def test_value_within_band_is_good(self):
        result = discretize_evidence({"rpm": 1800.0}, {"rpm": (1000.0, 2000.0)})
        assert result == {"rpm": 1}

    def test_no_threshold_returns_default_state(self):
        result = discretize_evidence({"unknown": 42.0}, {})
        assert result == {"unknown": 1}

    def test_no_threshold_respects_custom_default(self):
        result = discretize_evidence({"x": 0.5}, {}, default_state=0)
        assert result == {"x": 0}

    def test_one_sided_upper_limit_only(self):
        result = discretize_evidence({"pressure": 120.0}, {"pressure": (None, 100.0)})
        assert result["pressure"] == 0
        result2 = discretize_evidence({"pressure": 80.0}, {"pressure": (None, 100.0)})
        assert result2["pressure"] == 1

    def test_one_sided_lower_limit_only(self):
        result = discretize_evidence({"flow": 3.0}, {"flow": (5.0, None)})
        assert result["flow"] == 0
        result2 = discretize_evidence({"flow": 8.0}, {"flow": (5.0, None)})
        assert result2["flow"] == 1

    def test_value_at_boundary_high_is_good(self):
        # Alarms use strict inequalities: exactly at threshold is in-range (good)
        result = discretize_evidence({"v": 100.0}, {"v": (0.0, 100.0)})
        assert result["v"] == 1

    def test_value_just_above_boundary_is_bad(self):
        result = discretize_evidence({"v": 100.001}, {"v": (0.0, 100.0)})
        assert result["v"] == 0

    def test_value_at_boundary_low_is_good(self):
        result = discretize_evidence({"v": 0.0}, {"v": (0.0, 100.0)})
        assert result["v"] == 1

    def test_value_just_below_boundary_is_bad(self):
        result = discretize_evidence({"v": -0.001}, {"v": (0.0, 100.0)})
        assert result["v"] == 0

    def test_batch_multiple_keys(self):
        values = {"temp": 85.0, "pressure": 120.0, "flow": 8.0}
        thresholds = {
            "temp": (0.0, 100.0),
            "pressure": (None, 100.0),
            "flow": (5.0, None),
        }
        result = discretize_evidence(values, thresholds)
        assert result["temp"] == 1    # in band
        assert result["pressure"] == 0  # above upper limit
        assert result["flow"] == 1    # above lower limit

    def test_importable_from_ohm_evidence(self):
        from ohm.evidence import discretize_evidence as de
        assert callable(de)
        r = de({"x": 50.0}, {"x": (0.0, 100.0)})
        assert r == {"x": 1}

    def test_empty_values_returns_empty(self):
        result = discretize_evidence({}, {"temp": (0.0, 100.0)})
        assert result == {}
