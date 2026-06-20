"""Tests for suggestions lifecycle (OHM-xtzk, ADR-036)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from ohm.graph.queries import create_suggestion, query_suggestions, promote_suggestion, reject_suggestion
from ohm.graph.methods import compute_ripeness, ripen_then_decide
from ohm.framework.sdk import Graph


class TestCreateSuggestion:
    def test_create_returns_suggestion(self, test_db):
        conn = test_db
        result = create_suggestion(
            conn,
            suggestion_type="edge",
            from_node="n1",
            to_node="n2",
            target_node="n1",
            suggested_edge_type="CAUSES",
            created_by="test",
        )
        assert result["id"].startswith("sug_")
        assert result["status"] == "ripe"
        assert result["suggestion_type"] == "edge"

    def test_duplicate_increments_evidence(self, test_db):
        conn = test_db
        create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", target_node="n1", created_by="test")
        result = create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", target_node="n1", created_by="test")
        assert result["evidence_count"] == 2

    def test_invalid_type_raises(self, test_db):
        with pytest.raises(Exception):
            create_suggestion(test_db, suggestion_type="invalid", created_by="test")


class TestQuerySuggestions:
    def test_filter_by_status(self, test_db):
        conn = test_db
        create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", created_by="test")
        results = query_suggestions(conn, status="ripe")
        assert len(results) == 1
        results = query_suggestions(conn, status="promoted")
        assert len(results) == 0

    def test_filter_by_target(self, test_db):
        conn = test_db
        create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", target_node="target1", created_by="test")
        create_suggestion(conn, suggestion_type="edge", from_node="n3", to_node="n4", target_node="target2", created_by="test")
        results = query_suggestions(conn, target_node="target1")
        assert len(results) == 1


class TestPromoteSuggestion:
    def test_promote_creates_edge(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n1', 'from', 'concept', 'test', 'team', 0.9)"""
        )
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence)
               VALUES ('n2', 'to', 'concept', 'test', 'team', 0.9)"""
        )
        sug = create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", suggested_edge_type="CAUSES", created_by="test")
        result = promote_suggestion(conn, sug["id"], promoted_by="test")
        assert result["status"] == "promoted"
        edges = conn.execute("SELECT * FROM ohm_edges WHERE from_node = 'n1' AND to_node = 'n2'").fetchall()
        assert len(edges) == 1

    def test_promote_non_ripe_raises(self, test_db):
        conn = test_db
        sug = create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", created_by="test")
        reject_suggestion(conn, sug["id"], rejected_by="test")
        with pytest.raises(Exception, match="status"):
            promote_suggestion(conn, sug["id"], promoted_by="test")


class TestRejectSuggestion:
    def test_reject(self, test_db):
        conn = test_db
        sug = create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", created_by="test")
        result = reject_suggestion(conn, sug["id"], rejected_by="test", notes="not relevant")
        assert result["status"] == "rejected"


class TestComputeRipeness:
    def test_fresh_suggestion_low_ripeness(self):
        sug = {"suggested_at": datetime.now(), "evidence_count": 1, "confidence": 0.5}
        ripeness = compute_ripeness(sug)
        assert ripeness < 0.3

    def test_old_suggestion_high_ripeness(self):
        sug = {"suggested_at": datetime.now() - timedelta(days=30), "evidence_count": 3, "confidence": 0.8}
        ripeness = compute_ripeness(sug)
        assert ripeness > 0.5

    def test_no_suggested_at(self):
        sug = {"suggested_at": None}
        assert compute_ripeness(sug) == 0.0


class TestRipenThenDecide:
    def test_dry_run_no_modify(self, test_db):
        conn = test_db
        create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", created_by="test")
        result = ripen_then_decide(conn, dry_run=True)
        assert result["dry_run"] is True
        rows = conn.execute("SELECT ripeness_score FROM ohm_suggestions WHERE status = 'ripe'").fetchall()
        assert all(r[0] == 0.0 for r in rows)

    def test_ripen_updates_score(self, test_db):
        conn = test_db
        from datetime import datetime, timedelta

        old_date = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        sug = create_suggestion(conn, suggestion_type="edge", from_node="n1", to_node="n2", created_by="test")
        conn.execute(
            "UPDATE ohm_suggestions SET suggested_at = ? WHERE id = ?",
            [old_date, sug["id"]],
        )
        result = ripen_then_decide(conn, dry_run=False, max_age_days=30)
        assert result["ripened"] == 1
        row = conn.execute("SELECT ripeness_score FROM ohm_suggestions WHERE id = ?", [sug["id"]]).fetchone()
        assert row is not None
        assert row[0] > 0.0

    def test_empty_returns_zero(self, test_db):
        result = ripen_then_decide(test_db)
        assert result["ripened"] == 0


class TestSDKSuggestions:
    def test_sdk_create_and_query(self, test_db):
        conn = test_db
        with Graph(conn, actor="test") as g:
            g.create_suggestion(suggestion_type="edge", from_node="n1", to_node="n2")
            results = g.query_suggestions(status="ripe")
            assert len(results) == 1

    def test_sdk_reject(self, test_db):
        conn = test_db
        with Graph(conn, actor="test") as g:
            sug = g.create_suggestion(suggestion_type="edge", from_node="n1", to_node="n2")
            result = g.reject_suggestion(sug["id"])
            assert result["status"] == "rejected"
