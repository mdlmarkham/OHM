"""Tests for OHM-2x2u temporal decision layer."""

from __future__ import annotations

import json

import pytest

from ohm.exceptions import NodeNotFoundError, ValidationError
from ohm.graph.queries import (
    compute_feed_investment,
    create_node,
    create_edge,
    get_freshness_status,
    recommend_mode,
    record_mode_switch,
    set_freshness_threshold,
    temporal_decision_summary,
)


def _make_decision(test_db, label: str = "Decision", utility_scale: float | None = None, confidence: float = 0.5) -> str:
    n = create_node(
        test_db,
        label=label,
        node_type="decision",
        created_by="tester",
        utility_scale=utility_scale,
        confidence=confidence,
    )
    return n["id"]


class TestTemporalDecisionSchema:
    def test_node_types_in_valid_set(self, test_db):
        from ohm.graph.schema import VALID_NODE_TYPES, LAYER_EDGE_TYPES

        assert "freshness_threshold" in VALID_NODE_TYPES
        assert "feed_investment" in VALID_NODE_TYPES
        assert "mode_switch" in VALID_NODE_TYPES
        assert "GOVERNS_FRESHNESS" in LAYER_EDGE_TYPES["L3"]
        assert "INVESTS_IN" in LAYER_EDGE_TYPES["L3"]


class TestSetFreshnessThreshold:
    def test_creates_threshold_node(self, test_db):
        d = _make_decision(test_db)
        ft = set_freshness_threshold(
            test_db,
            decision_id=d,
            max_age_seconds=300,
            created_by="tester",
        )
        assert ft["type"] == "freshness_threshold"

    def test_creates_governs_freshness_edge(self, test_db):
        d = _make_decision(test_db)
        ft = set_freshness_threshold(test_db, decision_id=d, max_age_seconds=300, created_by="tester")
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'GOVERNS_FRESHNESS' AND deleted_at IS NULL""",
            [ft["id"]],
        ).fetchall()
        assert any(e[0] == d for e in edges)

    def test_stores_max_age_in_metadata(self, test_db):
        d = _make_decision(test_db)
        ft = set_freshness_threshold(test_db, decision_id=d, max_age_seconds=600, created_by="tester")
        meta = ft.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["max_age_seconds"] == 600

    def test_invalid_max_age_raises(self, test_db):
        d = _make_decision(test_db)
        with pytest.raises(ValueError):
            set_freshness_threshold(test_db, decision_id=d, max_age_seconds=0, created_by="tester")

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            set_freshness_threshold(
                test_db,
                decision_id="nonexistent_decision_xyz",
                max_age_seconds=300,
                created_by="tester",
            )


class TestGetFreshnessStatus:
    def test_returns_status_dict(self, test_db):
        d = _make_decision(test_db)
        set_freshness_threshold(test_db, decision_id=d, max_age_seconds=300, created_by="tester")
        status = get_freshness_status(test_db, decision_id=d)
        assert status["decision_id"] == d
        assert status["max_age_seconds"] == 300
        assert len(status["thresholds"]) == 1

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            get_freshness_status(test_db, decision_id="nonexistent_decision_xyz")

    def test_multiple_thresholds_takes_min(self, test_db):
        d = _make_decision(test_db)
        set_freshness_threshold(test_db, decision_id=d, max_age_seconds=600, created_by="tester")
        set_freshness_threshold(test_db, decision_id=d, max_age_seconds=120, created_by="tester")
        status = get_freshness_status(test_db, decision_id=d)
        assert status["max_age_seconds"] == 120
        assert len(status["thresholds"]) == 2


class TestComputeFeedInvestment:
    def test_creates_investment_node(self, test_db):
        d = _make_decision(test_db, utility_scale=0.8)
        fi = compute_feed_investment(test_db, decision_id=d, created_by="tester", observation_cost=0.5)
        assert "voi" in fi
        assert "recommendation" in fi

    def test_creates_invests_in_edge(self, test_db):
        d = _make_decision(test_db, utility_scale=0.7)
        fi = compute_feed_investment(test_db, decision_id=d, created_by="tester")
        nodes = test_db.execute(
            """SELECT n.id FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ?
               WHERE e.edge_type = 'INVESTS_IN' AND e.deleted_at IS NULL""",
            [d],
        ).fetchall()
        assert any(n[0] == fi["id"] for n in nodes)

    def test_high_voi_recommends_invest(self, test_db):
        d = _make_decision(test_db, utility_scale=0.95, confidence=0.3)
        fi = compute_feed_investment(test_db, decision_id=d, created_by="tester", observation_cost=0.01)
        assert fi["recommendation"] == "invest"

    def test_low_voi_recommends_defer(self, test_db):
        d = _make_decision(test_db, utility_scale=0.1, confidence=0.3)
        fi = compute_feed_investment(test_db, decision_id=d, created_by="tester", observation_cost=1.0)
        assert fi["recommendation"] == "defer"

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            compute_feed_investment(
                test_db,
                decision_id="nonexistent_decision_xyz",
                created_by="tester",
            )


class TestRecommendMode:
    def test_returns_mode_dict(self, test_db):
        d = _make_decision(test_db)
        result = recommend_mode(test_db, decision_id=d)
        assert result["decision_id"] == d
        assert result["mode"] in {"real_time", "deliberative", "hybrid"}
        assert "reasoning" in result

    def test_high_utility_triggers_deliberative(self, test_db):
        d = _make_decision(test_db, utility_scale=0.95)
        result = recommend_mode(test_db, decision_id=d)
        assert result["mode"] == "deliberative"

    def test_high_urgency_with_fresh_data_triggers_real_time(self, test_db):
        d = _make_decision(test_db, utility_scale=0.3)
        target = create_node(test_db, label="Target", node_type="concept", created_by="tester")
        edge = create_edge(
            test_db,
            from_node=d,
            to_node=target["id"],
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by="tester",
        )
        test_db.execute(
            "UPDATE ohm_edges SET urgency = ? WHERE id = ?",
            ["0.95", edge["id"]],
        )
        set_freshness_threshold(test_db, decision_id=d, max_age_seconds=99999, created_by="tester")
        result = recommend_mode(test_db, decision_id=d)
        assert result["mode"] == "real_time"

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            recommend_mode(test_db, decision_id="nonexistent_decision_xyz")


class TestRecordModeSwitch:
    def test_creates_switch_node(self, test_db):
        d = _make_decision(test_db)
        ms = record_mode_switch(
            test_db,
            decision_id=d,
            from_mode="real_time",
            to_mode="deliberative",
            reason="Stale data",
            created_by="tester",
        )
        assert ms["type"] == "mode_switch"

    def test_creates_transitions_to_edge(self, test_db):
        d = _make_decision(test_db)
        ms = record_mode_switch(
            test_db,
            decision_id=d,
            from_mode="real_time",
            to_mode="deliberative",
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'TRANSITIONS_TO' AND deleted_at IS NULL""",
            [ms["id"]],
        ).fetchall()
        assert any(e[0] == d for e in edges)

    def test_invalid_from_mode_raises(self, test_db):
        d = _make_decision(test_db)
        with pytest.raises(ValidationError):
            record_mode_switch(
                test_db,
                decision_id=d,
                from_mode="invalid_mode",
                to_mode="real_time",
                created_by="tester",
            )

    def test_invalid_to_mode_raises(self, test_db):
        d = _make_decision(test_db)
        with pytest.raises(ValidationError):
            record_mode_switch(
                test_db,
                decision_id=d,
                from_mode="real_time",
                to_mode="invalid_mode",
                created_by="tester",
            )

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            record_mode_switch(
                test_db,
                decision_id="nonexistent_decision_xyz",
                from_mode="real_time",
                to_mode="deliberative",
                created_by="tester",
            )


class TestTemporalDecisionSummary:
    def test_returns_summary_dict(self, test_db):
        d = _make_decision(test_db)
        set_freshness_threshold(test_db, decision_id=d, max_age_seconds=300, created_by="tester")
        compute_feed_investment(test_db, decision_id=d, created_by="tester")
        record_mode_switch(
            test_db,
            decision_id=d,
            from_mode="real_time",
            to_mode="deliberative",
            created_by="tester",
        )
        summary = temporal_decision_summary(test_db, decision_id=d)
        assert summary["decision_id"] == d
        assert "freshness" in summary or "freshness_status" in summary
        assert "feed_investments" in summary or "feed" in summary
        assert "mode_switches" in summary or "switches" in summary
        assert "current_mode" in summary or "mode" in summary

    def test_missing_decision_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            temporal_decision_summary(test_db, decision_id="nonexistent_decision_xyz")


class TestTemporalDecisionSDK:
    def test_sdk_set_and_get_freshness(self, test_db):
        from ohm.framework.sdk import Graph

        d = _make_decision(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            ft = g.set_freshness_threshold(d, max_age_seconds=300)
            assert ft["type"] == "freshness_threshold"
            status = g.get_freshness_status(d)
            assert status["max_age_seconds"] == 300

    def test_sdk_compute_feed_investment(self, test_db):
        from ohm.framework.sdk import Graph

        d = _make_decision(test_db, utility_scale=0.7)
        with Graph(test_db, actor="sdk-tester") as g:
            fi = g.compute_feed_investment(d)
            assert "voi" in fi

    def test_sdk_recommend_mode(self, test_db):
        from ohm.framework.sdk import Graph

        d = _make_decision(test_db, utility_scale=0.95)
        with Graph(test_db, actor="sdk-tester") as g:
            result = g.recommend_mode(d)
            assert result["mode"] == "deliberative"

    def test_sdk_record_mode_switch(self, test_db):
        from ohm.framework.sdk import Graph

        d = _make_decision(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            ms = g.record_mode_switch(d, from_mode="real_time", to_mode="hybrid", reason="test")
            assert ms["type"] == "mode_switch"

    def test_sdk_temporal_summary(self, test_db):
        from ohm.framework.sdk import Graph

        d = _make_decision(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            g.set_freshness_threshold(d, max_age_seconds=300)
            summary = g.temporal_decision_summary(d)
            assert summary["decision_id"] == d
