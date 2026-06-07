"""Tests for OHM-vj3i — compute_trajectory() temporal regression detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.graph.methods import compute_trajectory


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mem_conn():
    """In-memory DuckDB with minimal ohm_observations schema."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            node_id VARCHAR,
            edge_id VARCHAR,
            type VARCHAR,
            value FLOAT,
            baseline FLOAT,
            sigma FLOAT,
            source VARCHAR,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP,
            notes VARCHAR,
            source_name VARCHAR,
            source_url VARCHAR,
            scale VARCHAR,
            half_life_days FLOAT,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            supersedes_obs_id VARCHAR
        )
    """)
    yield conn
    conn.close()


def _obs(conn, node_id, value, created_at, source="test_src", created_by="agent", sigma=None):
    conn.execute(
        """INSERT INTO ohm_observations
           (node_id, type, value, source, created_by, sigma, created_at, valid_from)
           VALUES (?, 'measurement', ?, ?, ?, ?, ?, ?)""",
        [node_id, value, source, created_by, sigma, created_at, created_at],
    )


def _ts(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


# ── Insufficient data ─────────────────────────────────────────────────────────


def test_insufficient_data_returns_marker(mem_conn):
    _obs(mem_conn, "n1", 0.5, _ts(10))
    _obs(mem_conn, "n1", 0.6, _ts(5))
    result = compute_trajectory(mem_conn, "n1", min_observations=3)
    assert result["trend"] == "insufficient_data"
    assert result["observations"] == 2
    assert result["regressions"] == []
    assert result["consistency"] is None


def test_no_observations_returns_insufficient(mem_conn):
    result = compute_trajectory(mem_conn, "no-node")
    assert result["trend"] == "insufficient_data"
    assert result["observations"] == 0


def test_exact_minimum_observations_proceeds(mem_conn):
    for i, v in enumerate([0.3, 0.5, 0.7]):
        _obs(mem_conn, "n-min", v, _ts(10 - i))
    result = compute_trajectory(mem_conn, "n-min", min_observations=3)
    assert result["trend"] != "insufficient_data"


# ── Trend direction ───────────────────────────────────────────────────────────


def test_rising_trend_detected(mem_conn):
    for i, v in enumerate([0.2, 0.4, 0.6, 0.8, 0.9]):
        _obs(mem_conn, "n-up", v, _ts(20 - i * 4))
    result = compute_trajectory(mem_conn, "n-up")
    assert result["trend"] == "rising"
    assert result["trend_slope"] > 0


def test_falling_trend_detected(mem_conn):
    for i, v in enumerate([0.9, 0.7, 0.5, 0.3, 0.1]):
        _obs(mem_conn, "n-dn", v, _ts(20 - i * 4))
    result = compute_trajectory(mem_conn, "n-dn")
    assert result["trend"] == "falling"
    assert result["trend_slope"] < 0


def test_flat_trend_detected(mem_conn):
    for i in range(6):
        _obs(mem_conn, "n-flat", 0.5, _ts(20 - i * 3))
    result = compute_trajectory(mem_conn, "n-flat")
    assert result["trend"] == "flat"


# ── Regression detection ──────────────────────────────────────────────────────


def test_no_regressions_in_monotone_series(mem_conn):
    for i, v in enumerate([0.1, 0.3, 0.5, 0.7, 0.9]):
        _obs(mem_conn, "n-mono", v, _ts(20 - i * 3))
    result = compute_trajectory(mem_conn, "n-mono")
    assert result["regressions"] == []
    assert result["regression_count"] == 0


def test_single_reversal_detected(mem_conn):
    # Rising then falling: 0.2, 0.5, 0.8, 0.4, 0.1
    for i, v in enumerate([0.2, 0.5, 0.8, 0.4, 0.1]):
        _obs(mem_conn, "n-rev", v, _ts(20 - i * 3))
    result = compute_trajectory(mem_conn, "n-rev")
    assert result["regression_count"] >= 1
    first_reg = result["regressions"][0]
    assert first_reg["previous_trend"] == "rising"
    assert first_reg["new_trend"] == "falling"


def test_multiple_regressions_detected(mem_conn):
    # Oscillating: 0.1, 0.9, 0.1, 0.9, 0.1
    for i, v in enumerate([0.1, 0.9, 0.1, 0.9, 0.1]):
        _obs(mem_conn, "n-osc", v, _ts(20 - i * 3))
    result = compute_trajectory(mem_conn, "n-osc")
    assert result["regression_count"] >= 2


def test_regression_has_required_fields(mem_conn):
    for i, v in enumerate([0.2, 0.7, 0.3, 0.8, 0.1]):
        _obs(mem_conn, "n-rf", v, _ts(20 - i * 3))
    result = compute_trajectory(mem_conn, "n-rf")
    assert result["regressions"]
    reg = result["regressions"][0]
    for key in ("index", "at", "previous_trend", "new_trend", "from_value", "to_value", "magnitude"):
        assert key in reg, f"Missing regression field: {key}"
    assert reg["magnitude"] >= 0


# ── Acceleration ──────────────────────────────────────────────────────────────


def test_acceleration_present_for_sufficient_data(mem_conn):
    for i, v in enumerate([0.1, 0.2, 0.35, 0.55, 0.8, 1.0]):
        _obs(mem_conn, "n-acc", v, _ts(30 - i * 5))
    result = compute_trajectory(mem_conn, "n-acc")
    assert result["acceleration"] is not None


def test_acceleration_none_for_small_window(mem_conn):
    for i, v in enumerate([0.1, 0.5, 0.9]):
        _obs(mem_conn, "n-acc-sm", v, _ts(10 - i * 3))
    result = compute_trajectory(mem_conn, "n-acc-sm")
    # With only 3 points, mid=1 which is < 2 so acceleration should be None
    assert result["acceleration"] is None


# ── Consistency ───────────────────────────────────────────────────────────────


def test_single_source_consistency_is_none(mem_conn):
    for i, v in enumerate([0.3, 0.5, 0.7, 0.8]):
        _obs(mem_conn, "n-cons-s", v, _ts(20 - i * 4), source="only_src")
    result = compute_trajectory(mem_conn, "n-cons-s")
    assert result["consistency"] is None
    assert result["consistency_detail"] == "single_source"


def test_agreeing_sources_high_consistency(mem_conn):
    # Two sources both trending up
    for i in range(4):
        _obs(mem_conn, "n-agree", 0.2 + i * 0.2, _ts(20 - i * 2), source="src_a")
    for i in range(4):
        _obs(mem_conn, "n-agree", 0.25 + i * 0.2, _ts(19 - i * 2), source="src_b")
    result = compute_trajectory(mem_conn, "n-agree")
    assert result["consistency"] is not None
    assert result["consistency"] >= 0.5


def test_disagreeing_sources_low_consistency(mem_conn):
    # Source A: rising. Source B: falling.
    for i in range(4):
        _obs(mem_conn, "n-disagree", 0.1 + i * 0.2, _ts(20 - i * 2), source="src_up")
    for i in range(4):
        _obs(mem_conn, "n-disagree", 0.9 - i * 0.2, _ts(19 - i * 2), source="src_dn")
    result = compute_trajectory(mem_conn, "n-disagree")
    assert result["consistency"] is not None
    assert result["consistency"] < 0.5


# ── since= filter ─────────────────────────────────────────────────────────────


def test_since_filter_excludes_old_observations(mem_conn):
    # 3 old observations (50–40 days ago) + 5 recent (10–0 days ago)
    for i in range(3):
        _obs(mem_conn, "n-since", 0.1, _ts(50 - i * 5))
    for i in range(5):
        _obs(mem_conn, "n-since", 0.5 + i * 0.1, _ts(10 - i * 2))

    since_str = _ts(15).isoformat()
    result = compute_trajectory(mem_conn, "n-since", since=since_str)
    assert result["observations"] == 5


def test_since_filter_too_restrictive_returns_insufficient(mem_conn):
    for i in range(5):
        _obs(mem_conn, "n-since2", 0.5, _ts(20 - i * 3))
    # since = yesterday → only obs from last 24h included
    since_str = _ts(1).isoformat()
    result = compute_trajectory(mem_conn, "n-since2", since=since_str)
    assert result["trend"] == "insufficient_data"


# ── Response shape ────────────────────────────────────────────────────────────


def test_response_has_required_top_level_keys(mem_conn):
    for i, v in enumerate([0.3, 0.5, 0.7, 0.6, 0.8]):
        _obs(mem_conn, "n-shape", v, _ts(20 - i * 4))
    result = compute_trajectory(mem_conn, "n-shape")
    for key in ("node_id", "observations", "data_points", "trend", "regressions",
                "regression_count", "acceleration", "consistency", "consistency_detail",
                "mean_value", "trend_slope", "window_since"):
        assert key in result, f"Missing top-level key: {key}"


def test_data_points_capped_at_20(mem_conn):
    for i in range(25):
        _obs(mem_conn, "n-cap", 0.5 + i * 0.01, _ts(100 - i * 3))
    result = compute_trajectory(mem_conn, "n-cap")
    assert len(result["data_points"]) <= 20


def test_data_points_contain_required_fields(mem_conn):
    for i, v in enumerate([0.3, 0.5, 0.7, 0.9]):
        _obs(mem_conn, "n-dp", v, _ts(15 - i * 3), sigma=0.05)
    result = compute_trajectory(mem_conn, "n-dp")
    assert result["data_points"]
    dp = result["data_points"][0]
    for key in ("value", "source", "created_by", "created_at"):
        assert key in dp, f"Missing data_point field: {key}"


def test_deleted_observations_excluded(mem_conn):
    for i, v in enumerate([0.3, 0.5, 0.7]):
        _obs(mem_conn, "n-del", v, _ts(15 - i * 3))
    # Mark one as deleted (DuckDB doesn't support LIMIT in UPDATE; use subquery)
    mem_conn.execute(
        """UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP
           WHERE id = (SELECT id FROM ohm_observations WHERE node_id = 'n-del' LIMIT 1)"""
    )
    result = compute_trajectory(mem_conn, "n-del")
    assert result["observations"] == 2
    assert result["trend"] == "insufficient_data"
