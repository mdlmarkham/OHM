"""Tests for OHM-2x2u loop-status temporal section + confidence decay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ohm.graph.queries import (
    apply_decay_to_edges,
    compute_confidence_with_decay,
    create_edge,
    create_node,
    query_loop_status,
)


class TestComputeConfidenceWithDecay:
    def test_reduces_with_age(self, test_db):
        old_obs = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        result = compute_confidence_with_decay(test_db, base_confidence=0.8, last_observed_at=old_obs, half_life_days=30.0)
        assert 0.3 < result["decayed_confidence"] < 0.5
        assert result["age_days"] > 29
        assert result["is_stale"] is False

    def test_floor_when_very_old(self, test_db):
        very_old = (datetime.now(timezone.utc) - timedelta(days=365 * 5)).isoformat()
        result = compute_confidence_with_decay(test_db, base_confidence=0.9, last_observed_at=very_old, half_life_days=30.0, floor=0.1)
        assert result["decayed_confidence"] == 0.1
        assert result["is_stale"] is True

    def test_no_observation_returns_base(self, test_db):
        result = compute_confidence_with_decay(test_db, base_confidence=0.7, last_observed_at=None)
        assert result["decayed_confidence"] == 0.7
        assert result["age_days"] is None
        assert result["is_stale"] is False

    def test_half_life_zero_disables_decay(self, test_db):
        old_obs = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        result = compute_confidence_with_decay(test_db, base_confidence=0.6, last_observed_at=old_obs, half_life_days=0.0)
        assert result["decayed_confidence"] == 0.6

    def test_accepts_string_isoformat(self, test_db):
        result = compute_confidence_with_decay(
            test_db,
            base_confidence=0.5,
            last_observed_at="2020-01-01T00:00:00+00:00",
            half_life_days=30.0,
        )
        assert result["decayed_confidence"] == 0.1
        assert result["is_stale"] is True


class TestApplyDecayToEdges:
    def test_dry_run_does_not_modify(self, test_db):
        target = create_node(test_db, label="T", node_type="concept", created_by="tester")
        edge = create_edge(
            test_db,
            from_node=target["id"],
            to_node=target["id"],
            edge_type="SUPPORTS",
            layer="L3",
            created_by="tester",
            confidence=0.9,
        )
        result = apply_decay_to_edges(test_db, half_life_days=30.0, dry_run=True)
        assert result["edges_examined"] >= 1
        row = test_db.execute("SELECT confidence FROM ohm_edges WHERE id = ?", [edge["id"]]).fetchone()
        assert row[0] == pytest.approx(0.9)

    def test_live_updates_confidence(self, test_db):
        target = create_node(test_db, label="T", node_type="concept", created_by="tester")
        edge = create_edge(
            test_db,
            from_node=target["id"],
            to_node=target["id"],
            edge_type="SUPPORTS",
            layer="L3",
            created_by="tester",
            confidence=0.9,
        )
        apply_decay_to_edges(test_db, half_life_days=1.0, dry_run=False)
        row = test_db.execute("SELECT confidence, metadata FROM ohm_edges WHERE id = ?", [edge["id"]]).fetchone()
        assert row[0] < 0.9
        meta = row[1]
        if meta:
            import json

            parsed = json.loads(meta) if isinstance(meta, str) else meta
            assert parsed.get("confidence_original") == 0.9

    def test_returns_summary_counts(self, test_db):
        result = apply_decay_to_edges(test_db, half_life_days=30.0, dry_run=True)
        assert "edges_examined" in result
        assert "edges_decayed" in result
        assert "average_decay_factor" in result
        assert "summary" in result


class TestLoopStatusTemporal:
    def test_loop_status_includes_temporal_section(self, test_db):
        result = query_loop_status(test_db)
        assert "temporal" in result

    def test_loop_status_temporal_has_expected_keys(self, test_db):
        result = query_loop_status(test_db)
        temporal = result["temporal"]
        assert "upcoming_evaluations" in temporal
        assert "stale_feeds" in temporal
        assert "compromised_gates" in temporal
        assert "stuck_gates" in temporal
        assert "decay_summary" in temporal

    def test_empty_temporal_when_no_data(self, test_db):
        result = query_loop_status(test_db)
        temporal = result["temporal"]
        assert temporal["upcoming_evaluations"] == []
        assert temporal["stale_feeds"] == []
        assert temporal["compromised_gates"] == []
        assert temporal["stuck_gates"] == []

    def test_decay_summary_present(self, test_db):
        result = query_loop_status(test_db)
        ds = result["temporal"]["decay_summary"]
        assert "edges_examined" in ds
        assert "edges_decayed" in ds
        assert "average_decay_factor" in ds

    def test_compromised_gates_lists_node(self, test_db):
        n = create_node(test_db, label="Bad Gate", node_type="concept", created_by="tester")
        test_db.execute(
            "UPDATE ohm_nodes SET gate_type = 'twin', gate_status = 'compromised' WHERE id = ?",
            [n["id"]],
        )
        result = query_loop_status(test_db)
        ids = [g["node_id"] for g in result["temporal"]["compromised_gates"]]
        assert n["id"] in ids

    def test_stale_feeds_detected(self, test_db):
        target = create_node(test_db, label="Feed", node_type="concept", created_by="tester")
        decision = create_node(test_db, label="D", node_type="decision", created_by="tester")
        create_edge(
            test_db,
            from_node=target["id"],
            to_node=decision["id"],
            edge_type="FEEDS",
            layer="L2",
            created_by="tester",
        )
        result = query_loop_status(test_db)
        stale_ids = [s.get("feed_node_id") for s in result["temporal"]["stale_feeds"]]
        assert target["id"] in stale_ids

    def test_accepts_agent_name_filter(self, test_db):
        result = query_loop_status(test_db, agent_name="nonexistent_agent_xyz")
        assert "temporal" in result

    def test_accepts_half_life_days(self, test_db):
        result = query_loop_status(test_db, half_life_days=7.0)
        assert "temporal" in result
        assert result["temporal"]["decay_summary"] is not None


class TestTemporalSDK:
    def test_sdk_loop_status(self, test_db):
        from ohm.framework.sdk import Graph

        with Graph(test_db, actor="sdk-tester") as g:
            status = g.loop_status()
            assert "temporal" in status

    def test_sdk_compute_decay(self, test_db):
        from ohm.framework.sdk import Graph

        result = compute_confidence_with_decay(
            test_db,
            base_confidence=0.8,
            last_observed_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            half_life_days=20.0,
        )
        assert result["decayed_confidence"] < 0.8

    def test_sdk_apply_decay_dry_run(self, test_db):
        from ohm.framework.sdk import Graph

        target = create_node(test_db, label="T", node_type="concept", created_by="tester")
        create_edge(
            test_db,
            from_node=target["id"],
            to_node=target["id"],
            edge_type="SUPPORTS",
            layer="L3",
            created_by="tester",
            confidence=0.9,
        )
        result = apply_decay_to_edges(test_db, half_life_days=30.0, dry_run=True)
        assert result["edges_examined"] >= 1
