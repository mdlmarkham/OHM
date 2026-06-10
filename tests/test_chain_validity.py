"""Tests for OHM-wuki — chain_validity() STL weakest-link computation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.graph.decay import chain_validity, confidence_at


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mem_conn():
    """In-memory DuckDB with minimal OHM schema for decay tests."""
    conn = duckdb.connect(":memory:")
    conn.execute("""
        CREATE TABLE ohm_nodes (
            id VARCHAR PRIMARY KEY,
            label VARCHAR,
            type VARCHAR DEFAULT 'concept',
            deleted_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE ohm_edges (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR,
            from_node VARCHAR,
            to_node VARCHAR,
            layer VARCHAR,
            edge_type VARCHAR,
            confidence FLOAT,
            deleted_at TIMESTAMP
        )
    """)
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


def _node(conn, nid, label=None, node_type="concept"):
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type) VALUES (?, ?, ?)",
        [nid, label or nid, node_type],
    )


def _edge(conn, from_node, to_node, layer="L3", edge_type="SUPPORTS", confidence=0.8):
    conn.execute(
        "INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence) VALUES (?, ?, ?, ?, ?)",
        [from_node, to_node, layer, edge_type, confidence],
    )


def _obs(conn, node_id, value=0.9, half_life_days=30.0, created_at=None):
    now = created_at or datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO ohm_observations
           (node_id, type, value, created_by, created_at, half_life_days, valid_from)
           VALUES (?, 'measurement', ?, 'test', ?, ?, ?)""",
        [node_id, value, now, half_life_days, now],
    )


# ── chain_validity ────────────────────────────────────────────────────────────


def test_chain_validity_no_edges_returns_zero(mem_conn):
    _node(mem_conn, "synth-1")
    result = chain_validity(mem_conn, "synth-1")
    assert result["n_cluster_nodes"] == 0
    assert result["chain_validity"] == 0.0
    assert result["weakest_link"] == 0.0
    assert result["n_observations"] == 0


def test_chain_validity_single_cluster_fresh_obs(mem_conn):
    _node(mem_conn, "synth-2")
    _node(mem_conn, "cluster-a")
    _edge(mem_conn, "synth-2", "cluster-a")
    _obs(mem_conn, "cluster-a", value=0.8, half_life_days=30.0)

    result = chain_validity(mem_conn, "synth-2")
    assert result["n_cluster_nodes"] == 1
    assert result["n_observations"] >= 1
    # Fresh observation — weakest_link ≈ 0.8
    assert result["weakest_link"] == pytest.approx(0.8, abs=0.05)
    assert result["chain_validity"] == pytest.approx(0.8, abs=0.05)
    assert result["validity_threshold_met"] is True
    assert result["robustness"] > 0


def test_chain_validity_five_clusters_multiplicative(mem_conn):
    """Five clusters each with value=0.6 → chain_validity ≈ 0.6^5 = 0.0778."""
    _node(mem_conn, "synth-5")
    for i in range(5):
        nid = f"cluster-{i}"
        _node(mem_conn, nid)
        _edge(mem_conn, "synth-5", nid)
        _obs(mem_conn, nid, value=0.6, half_life_days=30.0)

    result = chain_validity(mem_conn, "synth-5")
    assert result["n_cluster_nodes"] == 5
    assert result["weakest_link"] == pytest.approx(0.6, abs=0.05)
    # 0.6^5 ≈ 0.0778
    assert result["chain_validity"] == pytest.approx(0.6**5, abs=0.01)


def test_chain_validity_weakest_link_is_minimum(mem_conn):
    """Weakest link should be the minimum confidence, not the product."""
    _node(mem_conn, "synth-wl")
    confidences = [0.9, 0.3, 0.8, 0.7, 0.95]
    for i, c in enumerate(confidences):
        nid = f"wl-node-{i}"
        _node(mem_conn, nid)
        _edge(mem_conn, "synth-wl", nid)
        _obs(mem_conn, nid, value=c, half_life_days=30.0)

    result = chain_validity(mem_conn, "synth-wl")
    assert result["weakest_link"] == pytest.approx(0.3, abs=0.05)


def test_chain_validity_threshold_not_met(mem_conn):
    """A stale observation should cause validity_threshold_met=False."""
    _node(mem_conn, "synth-stale")
    _node(mem_conn, "stale-cluster")
    _edge(mem_conn, "synth-stale", "stale-cluster")
    # Very old observation — will have decayed near zero
    old = datetime.now(timezone.utc) - timedelta(days=200)
    _obs(mem_conn, "stale-cluster", value=1.0, half_life_days=7.0, created_at=old)

    result = chain_validity(mem_conn, "synth-stale", threshold=0.1)
    assert result["validity_threshold_met"] is False
    assert result["robustness"] < 0


def test_chain_validity_uses_edge_proxy_when_no_obs(mem_conn):
    """Cluster node with no observations → edge confidence used as proxy."""
    _node(mem_conn, "synth-proxy")
    _node(mem_conn, "cluster-no-obs")
    _edge(mem_conn, "synth-proxy", "cluster-no-obs", confidence=0.75)
    # No observation on cluster-no-obs

    result = chain_validity(mem_conn, "synth-proxy")
    assert result["n_cluster_nodes"] == 1
    assert result["n_observations"] == 1  # the proxy counts
    assert result["weakest_link"] == pytest.approx(0.75, abs=0.05)
    # Proxy marked as synthetic=True
    assert any(o["synthetic"] for o in result["observations"])


def test_chain_validity_excludes_non_l3_edges(mem_conn):
    """Only L3 edges contribute to chain validity."""
    _node(mem_conn, "synth-l1")
    _node(mem_conn, "l1-target")
    _node(mem_conn, "l3-target")
    _edge(mem_conn, "synth-l1", "l1-target", layer="L1")
    _edge(mem_conn, "synth-l1", "l3-target", layer="L3")
    _obs(mem_conn, "l3-target", value=0.7)

    result = chain_validity(mem_conn, "synth-l1")
    # Only 1 cluster node (the L3 target)
    assert result["n_cluster_nodes"] == 1


def test_chain_validity_observations_sorted_by_confidence(mem_conn):
    """Observations in result should be sorted weakest-first."""
    _node(mem_conn, "synth-sort")
    for i, c in enumerate([0.9, 0.2, 0.6]):
        nid = f"sort-node-{i}"
        _node(mem_conn, nid)
        _edge(mem_conn, "synth-sort", nid)
        _obs(mem_conn, nid, value=c, half_life_days=30.0)

    result = chain_validity(mem_conn, "synth-sort")
    effs = [o["effective_confidence"] for o in result["observations"]]
    assert effs == sorted(effs)


def test_chain_validity_at_parameter(mem_conn):
    """chain_validity with t= should evaluate at that time."""
    _node(mem_conn, "synth-time")
    _node(mem_conn, "time-cluster")
    _edge(mem_conn, "synth-time", "time-cluster")
    _obs(mem_conn, "time-cluster", value=1.0, half_life_days=7.0)

    now = datetime.now(timezone.utc)
    # At t=now+14days, obs should be at ~0.25 (2 half-lives)
    future_t = now + timedelta(days=14)
    result = chain_validity(mem_conn, "synth-time", t=future_t)
    assert result["weakest_link"] == pytest.approx(0.25, abs=0.05)


def test_chain_validity_includes_synthesis_self_assessment(mem_conn):
    """The synthesis node's own observations are included in chain."""
    _node(mem_conn, "synth-self")
    _node(mem_conn, "self-cluster")
    _edge(mem_conn, "synth-self", "self-cluster")
    _obs(mem_conn, "self-cluster", value=0.8)
    # Own observation (self-assessment)
    _obs(mem_conn, "synth-self", value=0.4, half_life_days=30.0)

    result = chain_validity(mem_conn, "synth-self")
    # Should include both the cluster obs and the self-assessment
    assert result["n_observations"] >= 2
    # Weakest link is the self-assessment at 0.4
    assert result["weakest_link"] == pytest.approx(0.4, abs=0.05)
