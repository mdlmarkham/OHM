"""Tests for temporal regression detection (OHM-vj3i / TRAJ)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def _create_node(conn: DuckDBPyConnection, **kw) -> str:
    node_id = f"traj_node_{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
        [node_id, kw.get("label", "test"), kw.get("node_type", "concept"), kw.get("created_by", "agent")],
    )
    return node_id


def _create_obs(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    value: float,
    created_at: str | None = None,
    source: str = "test_source",
) -> str:
    obs_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_observations (id, node_id, type, value, source, created_by, scale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [obs_id, node_id, "measurement", value, source, "agent", "probability", created_at],
    )
    return obs_id


@pytest.fixture
def db(test_db):
    return test_db


class TestTrajectory:
    def test_insufficient_data_returns_insufficient_trend(self, db):
        node_id = _create_node(db)
        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["trend"] == "insufficient_data"
        assert result["observations"] == 0

    def test_rising_trend_detected(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.1, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.9, created_at="2026-01-03T00:00:00")
        _create_obs(db, node_id=node_id, value=0.95, created_at="2026-01-04T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["trend"] == "rising"
        assert result["trend_slope"] > 0

    def test_falling_trend_detected(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.9, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.6, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.3, created_at="2026-01-03T00:00:00")
        _create_obs(db, node_id=node_id, value=0.1, created_at="2026-01-04T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["trend"] == "falling"
        assert result["trend_slope"] < 0

    def test_flat_trend_detected(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.51, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.49, created_at="2026-01-03T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-04T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["trend"] == "flat"

    def test_regression_detected_on_direction_reversal(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.2, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.8, created_at="2026-01-03T00:00:00")
        _create_obs(db, node_id=node_id, value=0.4, created_at="2026-01-04T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert len(result["regressions"]) >= 1
        first_reg = result["regressions"][0]
        assert "previous_trend" in first_reg
        assert "new_trend" in first_reg
        assert first_reg["previous_trend"] != first_reg["new_trend"]

    def test_no_false_regression_on_flat_plateau(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-03T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert len(result["regressions"]) == 0

    def test_since_filter_limits_window(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.1, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.2, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.9, created_at="2026-01-10T00:00:00")
        _create_obs(db, node_id=node_id, value=0.95, created_at="2026-01-11T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, since="2026-01-09T00:00:00", min_observations=2)
        assert result["observations"] == 2
        assert result["trend"] == "rising"

    def test_consistency_with_multiple_sources(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.3, source="src_a", created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.4, source="src_a", created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.25, source="src_b", created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.35, source="src_b", created_at="2026-01-02T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["consistency"] is not None
        assert result["consistency_detail"]["sources"] == 2

    def test_data_points_returned_in_order(self, db):
        node_id = _create_node(db)
        _create_obs(db, node_id=node_id, value=0.1, created_at="2026-01-01T00:00:00")
        _create_obs(db, node_id=node_id, value=0.5, created_at="2026-01-02T00:00:00")
        _create_obs(db, node_id=node_id, value=0.9, created_at="2026-01-03T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=2)
        assert len(result["data_points"]) == 3
        values = [dp["value"] for dp in result["data_points"]]
        assert values[0] == pytest.approx(0.1, abs=0.01)
        assert values[1] == pytest.approx(0.5, abs=0.01)
        assert values[2] == pytest.approx(0.9, abs=0.01)

    def test_regression_count_matches(self, db):
        node_id = _create_node(db)
        values = [0.1, 0.9, 0.2, 0.8, 0.3]
        for i, v in enumerate(values):
            day = i + 1
            _create_obs(db, node_id=node_id, value=v, created_at=f"2026-01-{day:02d}T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["regression_count"] == len(result["regressions"])
        assert result["regression_count"] >= 2

    def test_acceleration_is_not_none(self, db):
        node_id = _create_node(db)
        for i in range(6):
            _create_obs(db, node_id=node_id, value=float(i) * 0.1, created_at=f"2026-01-{i+1:02d}T00:00:00")

        from ohm.methods import compute_trajectory

        result = compute_trajectory(db, node_id, min_observations=3)
        assert result["acceleration"] is not None
