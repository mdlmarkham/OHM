"""Tests for OHM-xdd4 temporal decay — confidence_at(), decay profiles,
supersession chains, and half_life_days wiring through the write path."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.graph.decay import (
    DEFAULT_HALF_LIFE,
    DEFAULT_WEIBULL_SHAPE,
    confidence_at,
    decay_profile,
    default_half_life,
    default_weibull_shape,
    get_active_observations,
    get_observation_chain,
    supersede_observation,
)


# ── confidence_at ────────────────────────────────────────────────────────────


def _obs(
    value=1.0,
    half_life_days=None,
    obs_type="measurement",
    created_at=None,
    valid_from=None,
    valid_to=None,
    weibull_shape=None,
    **kw,
):
    now = datetime.now(timezone.utc)
    return {
        "value": value,
        "half_life_days": half_life_days,
        "type": obs_type,
        "created_at": (created_at or now).isoformat(),
        "valid_from": (valid_from or created_at or now).isoformat(),
        "valid_to": valid_to.isoformat() if valid_to else None,
        "weibull_shape": weibull_shape,
        **kw,
    }


def test_permanent_observation_no_decay():
    obs = _obs(value=0.8, half_life_days=None, obs_type="outcome")
    assert confidence_at(obs) == pytest.approx(0.8)


def test_standard_half_life_at_creation():
    # At age=0, confidence == base_value
    obs = _obs(value=0.9, half_life_days=7.0)
    assert confidence_at(obs) == pytest.approx(0.9, abs=1e-6)


def test_standard_half_life_at_one_half_life():
    created = datetime.now(timezone.utc) - timedelta(days=7)
    obs = _obs(value=1.0, half_life_days=7.0, created_at=created)
    t = datetime.now(timezone.utc)
    result = confidence_at(obs, t=t)
    # After exactly one half-life, confidence should be ~0.5
    assert result == pytest.approx(0.5, abs=0.01)


def test_standard_half_life_at_two_half_lives():
    created = datetime.now(timezone.utc) - timedelta(days=14)
    obs = _obs(value=1.0, half_life_days=7.0, created_at=created)
    result = confidence_at(obs, t=datetime.now(timezone.utc))
    assert result == pytest.approx(0.25, abs=0.01)


def test_superseded_observation_returns_zero():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    obs = _obs(value=0.9, half_life_days=7.0, valid_to=past)
    assert confidence_at(obs) == 0.0


def test_valid_to_in_future_not_superseded():
    future = datetime.now(timezone.utc) + timedelta(days=10)
    obs = _obs(value=0.9, half_life_days=7.0, valid_to=future)
    # Not yet superseded
    assert confidence_at(obs) > 0.0


def test_binary_observation_active():
    # half_life_days=0 → binary; returns base_value if not superseded
    obs = _obs(value=0.75, half_life_days=0.0)
    assert confidence_at(obs) == pytest.approx(0.75)


def test_binary_observation_superseded():
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    obs = _obs(value=0.75, half_life_days=0.0, valid_to=past)
    assert confidence_at(obs) == 0.0


def test_appreciating_observation_grows():
    created = datetime.now(timezone.utc) - timedelta(days=30)
    obs = _obs(value=0.5, half_life_days=-30.0, created_at=created)
    result = confidence_at(obs, t=datetime.now(timezone.utc))
    # After one "appreciation period" it should be > base_value and <= 1.0
    assert result > 0.5
    assert result <= 1.0


def test_appreciating_observation_capped_at_1():
    # Very old appreciating obs should not exceed 1.0
    created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
    obs = _obs(value=0.5, half_life_days=-30.0, created_at=created)
    result = confidence_at(obs, t=datetime.now(timezone.utc))
    assert result <= 1.0


def test_confidence_at_uses_type_default_when_no_half_life():
    # half_life_days is missing → falls back to type default
    created = datetime.now(timezone.utc) - timedelta(days=7)
    obs = _obs(value=1.0, obs_type="measurement", created_at=created)
    obs.pop("half_life_days")  # simulate DB row with NULL half_life_days
    result = confidence_at(obs, t=datetime.now(timezone.utc))
    # measurement default is 7 days → should be ~0.5
    assert result == pytest.approx(0.5, abs=0.05)


def test_confidence_at_clamps_base_value_to_1():
    obs = _obs(value=1.5, half_life_days=7.0)  # value > 1.0
    result = confidence_at(obs)
    assert result <= 1.0


def test_confidence_at_clamps_base_value_to_0():
    obs = _obs(value=-0.5, half_life_days=7.0)  # value < 0.0
    result = confidence_at(obs)
    assert result >= 0.0


# ── default_half_life ────────────────────────────────────────────────────────


def test_default_half_life_known_types():
    assert default_half_life("measurement") == 7.0
    assert default_half_life("sentiment") == 3.0
    assert default_half_life("verification") == 180.0
    assert default_half_life("outcome") is None
    assert default_half_life("source") == 30.0
    assert default_half_life("pattern") == -30.0


def test_default_half_life_unknown_type_fallback():
    assert default_half_life("custom_type") == DEFAULT_HALF_LIFE["_default"]


# ── decay_profile ────────────────────────────────────────────────────────────


def test_decay_profile_names():
    assert decay_profile(None) == "permanent"
    assert decay_profile(0.0) == "binary"
    assert decay_profile(-30.0) == "appreciating"
    assert decay_profile(3.0) == "fast-perishable"
    assert decay_profile(7.0) == "perishable"
    assert decay_profile(30.0) == "standard"
    assert decay_profile(180.0) == "durable"


# ── supersession + chain (integration) ──────────────────────────────────────


@pytest.fixture
def mem_conn():
    """In-memory DuckDB with ohm_observations table (OHM-xdd4 schema)."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR PRIMARY KEY,
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
            supersedes_obs_id VARCHAR,
            metadata JSON,
            worktree_ref VARCHAR,
            evaluation_script VARCHAR,
            held_out BOOLEAN DEFAULT FALSE
        )
    """)
    yield conn
    conn.close()


def _insert_obs(conn, obs_id, node_id="n1", type_="measurement", value=0.9, half_life_days=7.0, supersedes_obs_id=None):
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO ohm_observations
           (id, node_id, type, value, created_by, created_at, half_life_days, valid_from, supersedes_obs_id)
           VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?)""",
        [obs_id, node_id, type_, value, now, half_life_days, now, supersedes_obs_id],
    )


def test_supersede_observation_sets_valid_to(mem_conn):
    _insert_obs(mem_conn, "obs-old")
    _insert_obs(mem_conn, "obs-new")

    result = supersede_observation(mem_conn, new_obs_id="obs-new", old_obs_id="obs-old", agent="test")

    assert result["old_observation"]["valid_to"] is not None
    assert result["new_observation"]["supersedes_obs_id"] == "obs-old"


def test_supersede_already_superseded_raises(mem_conn):
    _insert_obs(mem_conn, "obs-a")
    _insert_obs(mem_conn, "obs-b")
    supersede_observation(mem_conn, new_obs_id="obs-b", old_obs_id="obs-a", agent="test")

    _insert_obs(mem_conn, "obs-c")
    with pytest.raises(ValueError, match="already superseded"):
        supersede_observation(mem_conn, new_obs_id="obs-c", old_obs_id="obs-a", agent="test")


def test_supersede_missing_obs_raises(mem_conn):
    _insert_obs(mem_conn, "obs-real")
    with pytest.raises(ValueError, match="not found"):
        supersede_observation(mem_conn, new_obs_id="obs-real", old_obs_id="obs-ghost", agent="test")


def test_get_observation_chain_single(mem_conn):
    _insert_obs(mem_conn, "obs-only")
    chain = get_observation_chain(mem_conn, "obs-only")
    assert len(chain) == 1
    assert chain[0]["id"] == "obs-only"
    assert "effective_confidence" in chain[0]
    assert "decay_profile" in chain[0]


def test_get_observation_chain_two_deep(mem_conn):
    _insert_obs(mem_conn, "obs-v1")
    _insert_obs(mem_conn, "obs-v2", supersedes_obs_id="obs-v1")
    supersede_observation(mem_conn, new_obs_id="obs-v2", old_obs_id="obs-v1", agent="test")

    chain = get_observation_chain(mem_conn, "obs-v2")
    assert len(chain) == 2
    ids = [r["id"] for r in chain]
    assert ids[0] == "obs-v1"  # oldest first
    assert ids[1] == "obs-v2"


# ── get_active_observations ─────────────────────────────────────────────────


def test_get_active_observations_excludes_superseded(mem_conn):
    _insert_obs(mem_conn, "obs-live", node_id="n2")
    _insert_obs(mem_conn, "obs-dead", node_id="n2")
    # Mark obs-dead as superseded
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    mem_conn.execute("UPDATE ohm_observations SET valid_to = ? WHERE id = 'obs-dead'", [past])

    rows = get_active_observations(mem_conn, "n2")
    ids = [r["id"] for r in rows]
    assert "obs-live" in ids
    assert "obs-dead" not in ids


def test_get_active_observations_min_validity_filter(mem_conn):
    # Create an obs that has decayed a lot (old enough to be below 0.5)
    old_created = datetime.now(timezone.utc) - timedelta(days=100)
    mem_conn.execute(
        """INSERT INTO ohm_observations
           (id, node_id, type, value, created_by, created_at, half_life_days, valid_from)
           VALUES ('obs-stale', 'n3', 'measurement', 1.0, 'test', ?, 7.0, ?)""",
        [old_created, old_created],
    )
    # Fresh obs
    _insert_obs(mem_conn, "obs-fresh", node_id="n3", half_life_days=7.0)

    rows = get_active_observations(mem_conn, "n3", min_validity=0.5)
    ids = [r["id"] for r in rows]
    assert "obs-fresh" in ids
    assert "obs-stale" not in ids


# ── write path integration ───────────────────────────────────────────────────


def test_write_observation_sets_half_life_days():
    """create_observation() should persist half_life_days via type default."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR PRIMARY KEY,
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
            weibull_shape FLOAT,
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            supersedes_obs_id VARCHAR,
            metadata JSON,
            worktree_ref VARCHAR,
            evaluation_script VARCHAR,
            held_out BOOLEAN DEFAULT FALSE
        )
    """)
    # Also need change feed tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohm_change_log (
            id BIGINT, table_name VARCHAR, row_id VARCHAR,
            operation VARCHAR, agent_name VARCHAR, layer VARCHAR, changed_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohm_change_feed (
            id BIGINT, table_name VARCHAR, row_id VARCHAR,
            operation VARCHAR, agent_name VARCHAR, old_data VARCHAR,
            new_data VARCHAR, occurred_at TIMESTAMP
        )
    """)

    from ohm.graph.queries import create_observation

    obs = create_observation(
        conn,
        node_id="n1",
        obs_type="sentiment",
        created_by="test",
        value=0.8,
    )
    # sentiment default half_life is 3.0
    assert obs["half_life_days"] == pytest.approx(3.0)
    assert obs["valid_from"] is not None

    conn.close()


def test_write_observation_explicit_half_life_override():
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR PRIMARY KEY, node_id VARCHAR, edge_id VARCHAR,
            type VARCHAR, value FLOAT, baseline FLOAT, sigma FLOAT,
            source VARCHAR, created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP, notes VARCHAR, source_name VARCHAR,
            source_url VARCHAR, scale VARCHAR,
            half_life_days FLOAT, weibull_shape FLOAT,
            valid_from TIMESTAMP, valid_to TIMESTAMP, supersedes_obs_id VARCHAR,
            metadata JSON, worktree_ref VARCHAR,
            evaluation_script VARCHAR, held_out BOOLEAN DEFAULT FALSE
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_log (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, layer VARCHAR, changed_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_feed (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, old_data VARCHAR, new_data VARCHAR, occurred_at TIMESTAMP)")

    from ohm.graph.queries import create_observation

    obs = create_observation(
        conn,
        node_id="n1",
        obs_type="measurement",
        created_by="test",
        value=0.9,
        half_life_days=99.0,  # explicit override
    )
    assert obs["half_life_days"] == pytest.approx(99.0)
    conn.close()


# ── Weibull decay (OHM-60pd / OHM-24g9) ──────────────────────────────────────


class TestWeibullDecay:
    """Tests for the Weibull generalization of temporal decay (OHM-60pd).

    The Weibull formula: confidence = value * exp(-(age/scale)^shape)
    where scale = half_life / ln(2).

    Special cases:
      shape = 0  → binary (step function, valid until superseded)
      shape < 0  → appreciating (confidence grows with age)
      shape = 1  → exponential (identical to Phase 1 standard decay)
      shape > 1  → accelerating decay (fast-perishable)
      0 < shape < 1 → decelerating decay (durable)
    """

    # ── Formula correctness ──

    def test_shape_1_matches_exponential(self):
        """κ=1 should produce the same result as Phase 1 exponential decay."""
        created = datetime.now(timezone.utc) - timedelta(days=7)
        t = datetime.now(timezone.utc)
        # Weibull path
        obs_w = _obs(value=1.0, half_life_days=7.0, weibull_shape=1.0, created_at=created)
        weibull_result = confidence_at(obs_w, t=t)
        # Phase 1 path
        obs_p1 = _obs(value=1.0, half_life_days=7.0, created_at=created)
        p1_result = confidence_at(obs_p1, t=t, use_weibull=False)
        assert weibull_result == pytest.approx(p1_result, abs=1e-10)

    def test_shape_1_at_one_half_life_is_0_5(self):
        """After exactly one half-life with κ=1, confidence ≈ 0.5."""
        created = datetime.now(timezone.utc) - timedelta(days=7)
        obs = _obs(value=1.0, half_life_days=7.0, weibull_shape=1.0, created_at=created)
        result = confidence_at(obs, t=datetime.now(timezone.utc))
        assert result == pytest.approx(0.5, abs=0.01)

    def test_shape_0_is_binary(self):
        """κ=0 should return base_value (valid until superseded)."""
        created = datetime.now(timezone.utc) - timedelta(days=365)
        obs = _obs(value=0.7, half_life_days=30.0, weibull_shape=0.0, created_at=created)
        result = confidence_at(obs, t=datetime.now(timezone.utc))
        assert result == pytest.approx(0.7)

    def test_shape_negative_is_appreciating(self):
        """κ<0 should produce appreciating behavior (confidence grows)."""
        created = datetime.now(timezone.utc) - timedelta(days=30)
        obs = _obs(value=0.5, half_life_days=-30.0, weibull_shape=-1.0, created_at=created)
        result = confidence_at(obs, t=datetime.now(timezone.utc))
        assert result > 0.5
        assert result <= 1.0

    def test_shape_gt_1_accelerating_decay(self):
        """κ>1 has an S-shaped curve: initially decays slower than exponential,
        then faster. At one half-life, κ=1.5 retains MORE than κ=1.0.
        But at 2× half-life (6 days for half_life=3), κ=1.5 should be lower."""
        created = datetime.now(timezone.utc) - timedelta(days=6)
        obs_fast = _obs(value=1.0, half_life_days=3.0, weibull_shape=1.5, created_at=created)
        obs_std = _obs(value=1.0, half_life_days=3.0, weibull_shape=1.0, created_at=created)
        t = datetime.now(timezone.utc)
        fast_result = confidence_at(obs_fast, t=t)
        std_result = confidence_at(obs_std, t=t)
        # At 2× half-life, the S-curve has crossed over — κ=1.5 < κ=1.0
        assert fast_result < std_result

    def test_shape_lt_1_decelerating_decay(self):
        """0<κ<1 has a concave curve: initially decays faster than exponential,
        then slower. At 90 days with half_life=180 (half a half-life), κ=0.7
        should be BELOW κ=1.0 (faster initial drop). But at 360 days (2× half-life),
        κ=0.7 should retain MORE (slower long-tail decay)."""
        created = datetime.now(timezone.utc) - timedelta(days=360)
        obs_dur = _obs(value=1.0, half_life_days=180.0, weibull_shape=0.7, created_at=created)
        obs_std = _obs(value=1.0, half_life_days=180.0, weibull_shape=1.0, created_at=created)
        t = datetime.now(timezone.utc)
        dur_result = confidence_at(obs_dur, t=t)
        std_result = confidence_at(obs_std, t=t)
        # At 2× half-life, the concave curve has crossed — κ=0.7 > κ=1.0
        assert dur_result > std_result

    # ── Phase 1 equivalence ──

    def test_perishable_equivalence(self):
        """measurement: κ=1, half_life=7 → identical to Phase 1 exponential."""
        created = datetime.now(timezone.utc) - timedelta(days=3.5)
        t = datetime.now(timezone.utc)
        obs_w = _obs(value=0.8, half_life_days=7.0, weibull_shape=1.0, created_at=created)
        obs_p1 = _obs(value=0.8, half_life_days=7.0, created_at=created)
        assert confidence_at(obs_w, t=t) == pytest.approx(
            confidence_at(obs_p1, t=t, use_weibull=False), abs=1e-10
        )

    def test_fast_perishable_equivalence(self):
        """sentiment: κ=1.5, half_life=3 → Weibull path, Phase 1 has no direct
        equivalent (was also exponential with half_life=3). At age=0 they match."""
        now = datetime.now(timezone.utc)
        obs_w = _obs(value=0.9, half_life_days=3.0, weibull_shape=1.5, created_at=now)
        obs_p1 = _obs(value=0.9, half_life_days=3.0, created_at=now)
        # At age=0 both return base_value
        assert confidence_at(obs_w, t=now) == pytest.approx(
            confidence_at(obs_p1, t=now, use_weibull=False), abs=1e-10
        )

    def test_durable_equivalence_at_creation(self):
        """verification: κ=0.7, half_life=180 → at age=0, both paths return base."""
        now = datetime.now(timezone.utc)
        obs_w = _obs(value=0.8, half_life_days=180.0, weibull_shape=0.7, created_at=now)
        obs_p1 = _obs(value=0.8, half_life_days=180.0, created_at=now)
        assert confidence_at(obs_w, t=now) == pytest.approx(
            confidence_at(obs_p1, t=now, use_weibull=False), abs=1e-10
        )

    def test_binary_equivalence(self):
        """outcome: κ=0 → binary. Phase 1 half_life=0 → also binary."""
        created = datetime.now(timezone.utc) - timedelta(days=365)
        t = datetime.now(timezone.utc)
        obs_w = _obs(value=0.7, half_life_days=30.0, weibull_shape=0.0, created_at=created)
        obs_p1 = _obs(value=0.7, half_life_days=0.0, created_at=created)
        assert confidence_at(obs_w, t=t) == pytest.approx(
            confidence_at(obs_p1, t=t, use_weibull=False)
        )

    def test_appreciating_equivalence(self):
        """pattern: κ=-1, half_life=-30 → both use the same linear formula."""
        created = datetime.now(timezone.utc) - timedelta(days=30)
        t = datetime.now(timezone.utc)
        obs_w = _obs(value=0.5, half_life_days=-30.0, weibull_shape=-1.0, created_at=created)
        obs_p1 = _obs(value=0.5, half_life_days=-30.0, created_at=created)
        assert confidence_at(obs_w, t=t) == pytest.approx(
            confidence_at(obs_p1, t=t, use_weibull=False), abs=1e-10
        )

    # ── Type defaults ──

    def test_default_weibull_shape_known_types(self):
        assert default_weibull_shape("measurement") == 1.0
        assert default_weibull_shape("sentiment") == 1.5
        assert default_weibull_shape("verification") == 0.7
        assert default_weibull_shape("outcome") == 0.0
        assert default_weibull_shape("source") == 1.0
        assert default_weibull_shape("pattern") == -1.0

    def test_default_weibull_shape_unknown_fallback(self):
        assert default_weibull_shape("custom_type") == DEFAULT_WEIBULL_SHAPE["_default"]

    # ── decay_profile with weibull_shape ──

    def test_decay_profile_with_weibull_shape(self):
        assert decay_profile(None, 0.0) == "binary"
        assert decay_profile(None, -1.0) == "appreciating"
        assert decay_profile(None, 0.7) == "durable"
        assert decay_profile(None, 1.0) == "perishable"
        assert decay_profile(None, 1.5) == "fast-perishable"

    def test_decay_profile_weibull_takes_priority(self):
        """When both half_life and weibull_shape are provided, shape wins."""
        # half_life=7 would say "perishable" in Phase 1, but shape=0 says "binary"
        assert decay_profile(7.0, 0.0) == "binary"
        # half_life=3 would say "fast-perishable", but shape=0.7 says "durable"
        assert decay_profile(3.0, 0.7) == "durable"

    # ── Fallback behavior ──

    def test_falls_back_to_exponential_when_no_shape(self):
        """When weibull_shape is None and use_weibull=True, falls back to
        Phase 1 exponential."""
        created = datetime.now(timezone.utc) - timedelta(days=7)
        obs = _obs(value=1.0, half_life_days=7.0, created_at=created)
        obs["weibull_shape"] = None  # explicitly None
        result = confidence_at(obs, t=datetime.now(timezone.utc))
        # Phase 1 exponential at one half-life ≈ 0.5
        assert result == pytest.approx(0.5, abs=0.01)

    def test_use_weibull_false_ignores_shape(self):
        """use_weibull=False should ignore weibull_shape entirely."""
        created = datetime.now(timezone.utc) - timedelta(days=7)
        obs = _obs(value=1.0, half_life_days=7.0, weibull_shape=1.5, created_at=created)
        result_weibull = confidence_at(obs, t=datetime.now(timezone.utc), use_weibull=False)
        # Phase 1 exponential at one half-life ≈ 0.5 regardless of shape
        assert result_weibull == pytest.approx(0.5, abs=0.01)


# ── create_observation weibull_shape wiring (OHM-60pd) ──────────────────────


def test_create_observation_sets_weibull_shape_from_type_default():
    """create_observation() should auto-resolve weibull_shape from obs_type."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR PRIMARY KEY, node_id VARCHAR, edge_id VARCHAR,
            type VARCHAR, value FLOAT, baseline FLOAT, sigma FLOAT,
            source VARCHAR, created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP, notes VARCHAR, source_name VARCHAR,
            source_url VARCHAR, scale VARCHAR,
            half_life_days FLOAT, weibull_shape FLOAT,
            valid_from TIMESTAMP, valid_to TIMESTAMP, supersedes_obs_id VARCHAR,
            metadata JSON, worktree_ref VARCHAR,
            evaluation_script VARCHAR, held_out BOOLEAN DEFAULT FALSE
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_log (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, layer VARCHAR, changed_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_feed (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, old_data VARCHAR, new_data VARCHAR, occurred_at TIMESTAMP)")

    from ohm.graph.queries import create_observation

    obs = create_observation(
        conn,
        node_id="n1",
        obs_type="sentiment",
        created_by="test",
        value=0.8,
    )
    # sentiment default weibull_shape is 1.5
    assert obs["weibull_shape"] == pytest.approx(1.5)
    conn.close()


def test_create_observation_explicit_weibull_shape_override():
    """create_observation() should accept explicit weibull_shape override."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR PRIMARY KEY, node_id VARCHAR, edge_id VARCHAR,
            type VARCHAR, value FLOAT, baseline FLOAT, sigma FLOAT,
            source VARCHAR, created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP, notes VARCHAR, source_name VARCHAR,
            source_url VARCHAR, scale VARCHAR,
            half_life_days FLOAT, weibull_shape FLOAT,
            valid_from TIMESTAMP, valid_to TIMESTAMP, supersedes_obs_id VARCHAR,
            metadata JSON, worktree_ref VARCHAR,
            evaluation_script VARCHAR, held_out BOOLEAN DEFAULT FALSE
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_log (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, layer VARCHAR, changed_at TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS ohm_change_feed (id BIGINT, table_name VARCHAR, row_id VARCHAR, operation VARCHAR, agent_name VARCHAR, old_data VARCHAR, new_data VARCHAR, occurred_at TIMESTAMP)")

    from ohm.graph.queries import create_observation

    obs = create_observation(
        conn,
        node_id="n1",
        obs_type="measurement",
        created_by="test",
        value=0.9,
        weibull_shape=0.5,  # explicit override
    )
    assert obs["weibull_shape"] == pytest.approx(0.5)
    conn.close()
