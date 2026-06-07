"""Tests for OHM-xdd4 temporal decay — confidence_at(), decay profiles,
supersession chains, and half_life_days wiring through the write path."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.graph.decay import (
    DEFAULT_HALF_LIFE,
    confidence_at,
    decay_profile,
    default_half_life,
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
            supersedes_obs_id VARCHAR
        )
    """)
    yield conn
    conn.close()


def _insert_obs(conn, obs_id, node_id="n1", type_="measurement", value=0.9,
                half_life_days=7.0, supersedes_obs_id=None):
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
            valid_from TIMESTAMP,
            valid_to TIMESTAMP,
            supersedes_obs_id VARCHAR
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
            half_life_days FLOAT, valid_from TIMESTAMP,
            valid_to TIMESTAMP, supersedes_obs_id VARCHAR
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
