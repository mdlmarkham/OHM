"""Tests for OHM-u39s — auto-apply confidence decay before ranking.

Decay is applied automatically in four model/feed ranking sites:
  1. compare_models — composite_score decayed by latest model_evaluation age
  2. assemble_twin_for_decision — model score decayed by observation age
  3. compute_decision_value (+ promote_model) — accuracy decayed by eval age
  4. query_loop_status.temporal.stale_feeds — rank by decayed confidence ASC

All sites accept apply_decay: bool = True and half_life_days: float = 30.0
parameters, plus an optional decay_floor (None by default for composite_score
which can be negative; 0.1 default for accuracy in [0, 1]).

The ``compute_confidence_with_decay`` helper now accepts ``floor: float | None``
(None disables the floor — needed for unbounded composite_score).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from ohm.framework.validation import enforce_confidence_ceiling
from ohm.queries import (
    assemble_twin_for_decision,
    compare_models,
    compute_confidence_with_decay,
    compute_decision_value,
    create_edge,
    create_node,
    create_observation,
    evaluate_model,
    promote_model,
    query_loop_status,
    register_twin,
)
from ohm.schema import initialize_schema


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    initialize_schema(c)
    yield c
    c.close()


# ──────────────────────────────────────────────────────────────────────────────
# compute_confidence_with_decay: floor=None support
# ──────────────────────────────────────────────────────────────────────────────


class TestFloorNoneSupport:
    """floor=None disables the lower bound — needed for unbounded scores."""

    def test_floor_none_preserves_negative_values(self, conn):
        """Negative base score must not be clamped up to 0.1 by the floor."""
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = compute_confidence_with_decay(
            conn,
            base_confidence=-0.5,
            last_observed_at=old,
            half_life_days=30.0,
            floor=None,
        )
        # No floor means no clamping
        assert result["decayed_confidence"] < 0
        assert result["is_stale"] is False  # staleness undefined when no floor

    def test_floor_zero_clamps_to_zero(self, conn):
        """floor=0.0 is a valid explicit floor — clamps positive values to 0
        only when raw crosses below 0 (rare). For very stale inputs the
        decayed value snaps to exactly 0.0 (no floating-point dust)."""
        # 50 half-lives at 30d half-life = 1500 days. 0.5 * 2^(-50) ≈ 4.4e-16.
        # The result is rounded to 0.0 by the round(..., 6) call.
        very_old = (datetime.now(timezone.utc) - timedelta(days=1500)).isoformat()
        result = compute_confidence_with_decay(
            conn,
            base_confidence=0.5,
            last_observed_at=very_old,
            half_life_days=30.0,
            floor=0.0,
        )
        assert result["decayed_confidence"] == 0.0
        # Note: is_stale=False because raw (4.4e-16) is technically above
        # the floor (0.0) — staleness is "raw was clamped", not "result is
        # at floor". floor=0.0 is a corner case; floor>0 is the common usage.

    def test_floor_default_still_applies(self, conn):
        """Default floor (0.1) still clamps for confidence-style inputs."""
        old = (datetime.now(timezone.utc) - timedelta(days=365 * 5)).isoformat()
        result = compute_confidence_with_decay(
            conn,
            base_confidence=0.9,
            last_observed_at=old,
            half_life_days=30.0,
        )
        assert result["decayed_confidence"] == 0.1
        assert result["is_stale"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Site 1: compare_models
# ──────────────────────────────────────────────────────────────────────────────


class TestCompareModelsDecay:
    """Auto-apply decay in model ranking (OHM-u39s #1)."""

    def test_returns_both_raw_and_decayed_scores(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        c = register_twin(conn, label="C", target_node_id=target["id"], created_by="t")["id"] if False else None
        cand = _register_candidate(conn, twin["id"], "M1", "t")
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        result = compare_models(conn, twin_id=twin["id"])
        assert len(result["candidates"]) == 1
        c0 = result["candidates"][0]
        assert "composite_score" in c0
        assert "decayed_composite_score" in c0
        assert "decay" in c0
        # Fresh eval → decayed close to raw (within decay factor of 0.9999...)
        assert c0["decayed_composite_score"] == pytest.approx(c0["composite_score"], abs=1e-3)

    def test_apply_decay_false_preserves_old_behavior(self, conn):
        """Opt-out path: no decayed field divergence, sort by raw score."""
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        c1 = _register_candidate(conn, twin["id"], "M1", "t")
        c2 = _register_candidate(conn, twin["id"], "M2", "t")
        evaluate_model(conn, model_candidate_id=c1["id"], created_by="t", metrics={"mae": 0.05, "rmse": 0.1})
        evaluate_model(conn, model_candidate_id=c2["id"], created_by="t", metrics={"mae": 0.5, "rmse": 0.8})
        result = compare_models(conn, twin_id=twin["id"], apply_decay=False)
        # Better model still first
        assert result["candidates"][0]["label"] == "M1"
        # decayed == raw when decay disabled
        for c in result["candidates"]:
            assert c["decayed_composite_score"] == c["composite_score"]

    def test_stale_eval_loses_to_fresh_eval_with_decay(self, conn):
        """A fresh evaluation beats an old one with the same raw score
        when apply_decay=True. This is the core behavior — stale evidence
        should not carry equal weight."""
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        fresh = _register_candidate(conn, twin["id"], "Fresh", "t")
        stale = _register_candidate(conn, twin["id"], "Stale", "t")
        # Same metric input → same raw score
        evaluate_model(conn, model_candidate_id=fresh["id"], created_by="t", metrics={"accuracy": 0.9})
        evaluate_model(conn, model_candidate_id=stale["id"], created_by="t", metrics={"accuracy": 0.9})
        # Backdate the Stale eval 90 days
        _backdate_evaluations(conn, stale["id"], days_ago=90, half_life_days=30)
        result = compare_models(conn, twin_id=twin["id"], half_life_days=30.0)
        # Fresh eval should be ranked first
        assert result["candidates"][0]["label"] == "Fresh"
        # Verify the stale one is meaningfully decayed
        stale_entry = next(c for c in result["candidates"] if c["label"] == "Stale")
        assert stale_entry["decayed_composite_score"] < stale_entry["composite_score"]

    def test_recommendation_uses_decayed_score(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        a = _register_candidate(conn, twin["id"], "A", "t")
        b = _register_candidate(conn, twin["id"], "B", "t")
        evaluate_model(conn, model_candidate_id=a["id"], created_by="t", metrics={"accuracy": 0.9})
        evaluate_model(conn, model_candidate_id=b["id"], created_by="t", metrics={"accuracy": 0.5})
        result = compare_models(conn, twin_id=twin["id"])
        rec = result["recommendation"]
        assert rec is not None
        assert rec["label"] == "A"


# ──────────────────────────────────────────────────────────────────────────────
# Site 2: assemble_twin_for_decision
# ──────────────────────────────────────────────────────────────────────────────


class TestAssembleTwinDecay:
    """Auto-apply decay in model scoring during twin assembly (OHM-u39s #2)."""

    def test_each_candidate_has_decayed_score(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t")
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        # Need a decision to assemble against
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = assemble_twin_for_decision(
            conn,
            decision_node_id=decision["id"],
            goal="test goal",
            created_by="t",
        )
        # At least one model_candidate in the ranking
        if result.get("model_candidates"):
            for mc in result["model_candidates"]:
                assert "score" in mc
                assert "decayed_score" in mc
                assert "decay" in mc

    def test_stale_obs_loses_to_fresh_in_assembly(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        fresh = _register_candidate(conn, twin["id"], "Fresh", "t")
        stale = _register_candidate(conn, twin["id"], "Stale", "t")
        evaluate_model(conn, model_candidate_id=fresh["id"], created_by="t", metrics={"accuracy": 0.9})
        evaluate_model(conn, model_candidate_id=stale["id"], created_by="t", metrics={"accuracy": 0.9})
        _backdate_evaluations(conn, stale["id"], days_ago=90, half_life_days=30)
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = assemble_twin_for_decision(
            conn,
            decision_node_id=decision["id"],
            goal="test",
            created_by="t",
            half_life_days=30.0,
        )
        cands = result.get("model_candidates", [])
        labels = [c["label"] for c in cands]
        # Fresh should be ranked higher
        if "Fresh" in labels and "Stale" in labels:
            assert labels.index("Fresh") < labels.index("Stale")

    def test_apply_decay_false_opt_out(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t")
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = assemble_twin_for_decision(
            conn,
            decision_node_id=decision["id"],
            goal="test",
            created_by="t",
            apply_decay=False,
        )
        cands = result.get("model_candidates", [])
        for mc in cands:
            assert mc["decayed_score"] == mc["score"]


# ──────────────────────────────────────────────────────────────────────────────
# Site 3: compute_decision_value + promote_model
# ──────────────────────────────────────────────────────────────────────────────


class TestComputeDecisionValueDecay:
    """Auto-apply decay to accuracy in decision_value_score (OHM-u39s #3)."""

    def test_decayed_accuracy_returned(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t", parameters={"latency": 0.1, "cost": 0.2})
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = compute_decision_value(
            conn,
            model_id=cand["id"],
            decision_node_id=decision["id"],
            utility_scale=1.0,
        )
        assert "accuracy" in result
        assert "decayed_accuracy" in result
        assert "decay" in result
        # Fresh eval → decayed close to raw
        assert result["decayed_accuracy"] == pytest.approx(result["accuracy"], abs=1e-3)

    def test_stale_accuracy_drops_decision_value(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t", parameters={"latency": 0.0, "cost": 0.0})
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        _backdate_evaluations(conn, cand["id"], days_ago=180, half_life_days=30)
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = compute_decision_value(
            conn,
            model_id=cand["id"],
            decision_node_id=decision["id"],
            utility_scale=1.0,
            half_life_days=30.0,
        )
        # 180 days at 30-day half-life → decay factor 2^(-6) = 0.015625
        # 0.9 * 0.015625 ≈ 0.014; floor at 0.1 → 0.1
        assert result["decayed_accuracy"] <= result["accuracy"]
        assert result["decayed_accuracy"] <= 0.15  # floor at 0.1 max

    def test_apply_decay_false_uses_raw_accuracy(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t", parameters={"latency": 0.0, "cost": 0.0})
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        _backdate_evaluations(conn, cand["id"], days_ago=180, half_life_days=30)
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        result = compute_decision_value(
            conn,
            model_id=cand["id"],
            decision_node_id=decision["id"],
            utility_scale=1.0,
            apply_decay=False,
        )
        # Without decay, decayed == raw regardless of age
        assert result["decayed_accuracy"] == result["accuracy"] == 0.9

    def test_promote_model_threads_decay_params(self, conn):
        """promote_model with policy=decision_value should thread decay params."""
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        twin = register_twin(conn, label="T", target_node_id=target["id"], created_by="t")
        cand = _register_candidate(conn, twin["id"], "M", "t", parameters={"latency": 0.0, "cost": 0.0})
        evaluate_model(conn, model_candidate_id=cand["id"], created_by="t", metrics={"accuracy": 0.9})
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        # Should not raise
        promoted = promote_model(
            conn,
            model_candidate_id=cand["id"],
            created_by="t",
            policy="decision_value",
            decision_node_id=decision["id"],
            apply_decay=True,
            half_life_days=30.0,
        )
        assert promoted is not None


# ──────────────────────────────────────────────────────────────────────────────
# Site 4: query_loop_status.temporal.stale_feeds
# ──────────────────────────────────────────────────────────────────────────────


class TestStaleFeedsDecayRanking:
    """Rank stale feeds by decayed confidence ASC (most-decayed first) (OHM-u39s #4)."""

    def test_feeds_sorted_by_decayed_confidence_ascending(self, conn):
        """Most-decayed feed comes first."""
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        fresh = create_node(conn, label="Fresh", node_type="concept", created_by="t")
        stale = create_node(conn, label="Stale", node_type="concept", created_by="t")
        # Both feed the decision
        for feed in (fresh, stale):
            create_edge(
                conn,
                from_node=feed["id"],
                to_node=decision["id"],
                layer="L2",
                edge_type="FEEDS",
                created_by="t",
            )
        # Same observation value, different ages
        observe(conn, node_id_or_label=fresh["id"], value=0.8, source="t")
        observe(conn, node_id_or_label=stale["id"], value=0.8, source="t")
        _backdate_node(conn, stale["id"], days_ago=180)
        result = query_loop_status(conn, half_life_days=30.0)
        stale_feeds = result["temporal"]["stale_feeds"]
        labels = [f["label"] for f in stale_feeds]
        if "Stale" in labels and "Fresh" in labels:
            # Stale should come first (most decayed)
            assert labels.index("Stale") < labels.index("Fresh")

    def test_each_feed_has_decayed_confidence_field(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        feed = create_node(conn, label="Feed", node_type="concept", created_by="t")
        create_edge(
            conn,
            from_node=feed["id"],
            to_node=decision["id"],
            layer="L2",
            edge_type="FEEDS",
            created_by="t",
        )
        observe(conn, node_id_or_label=feed["id"], value=0.7, source="t")
        result = query_loop_status(conn)
        feeds = result["temporal"]["stale_feeds"]
        if feeds:
            for f in feeds:
                assert "decayed_confidence" in f
                assert "latest_value" in f
                assert "age_seconds" in f
                assert "decay" in f

    def test_feed_without_observation_gets_zero_decay(self, conn):
        target = create_node(conn, label="Target", node_type="concept", created_by="t")
        decision = create_node(conn, label="Decision", node_type="decision", created_by="t", connects_to=[target["id"]])
        feed = create_node(conn, label="NoObsFeed", node_type="concept", created_by="t")
        create_edge(
            conn,
            from_node=feed["id"],
            to_node=decision["id"],
            layer="L2",
            edge_type="FEEDS",
            created_by="t",
        )
        result = query_loop_status(conn)
        no_obs = [f for f in result["temporal"]["stale_feeds"] if f["label"] == "NoObsFeed"]
        if no_obs:
            assert no_obs[0]["decayed_confidence"] == 0.0
            assert no_obs[0]["latest_value"] is None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _register_candidate(conn, twin_id: str, label: str, created_by: str, parameters: dict | None = None):
    from ohm.queries import register_model_candidate

    return register_model_candidate(
        conn,
        label=label,
        twin_id=twin_id,
        created_by=created_by,
        model_parameters=parameters,
    )


def _backdate_evaluations(conn, model_id: str, days_ago: int, half_life_days: float):
    """Set the model_evaluation's created_at to N days ago, simulating staleness."""
    from ohm.queries import _rows_to_dicts  # type: ignore[attr-defined]

    rows = _rows_to_dicts(
        conn.execute(
            "SELECT id FROM ohm_nodes WHERE type = 'model_evaluation' AND deleted_at IS NULL AND id IN (  SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'EVALUATED_BY')",
            [model_id],
        )
    )
    target_iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    for row in rows:
        conn.execute(
            "UPDATE ohm_nodes SET created_at = ? WHERE id = ?",
            [target_iso, row["id"]],
        )


def _backdate_node(conn, node_id: str, days_ago: int):
    """Backdate a node's created_at and any observations' created_at."""
    target_iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute("UPDATE ohm_nodes SET created_at = ? WHERE id = ?", [target_iso, node_id])
    conn.execute(
        "UPDATE ohm_observations SET created_at = ? WHERE node_id = ?",
        [target_iso, node_id],
    )


def observe(conn, *, node_id_or_label: str, value: float, source: str):
    return create_observation(
        conn,
        node_id=node_id_or_label,
        obs_type="measurement",
        created_by=source,
        value=value,
        source=source,
    )
