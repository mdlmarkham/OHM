"""Tests for OHM-jbsr — oppositional review in the synthesis pipeline (Phase 1).

Exercises find_homogeneous_causes() and oppositional_review() against an
in-memory DuckDB with the full OHM schema (test_db fixture). Phase 1 uses
source_tier + agent-authorship homogeneity only.
"""

from __future__ import annotations

import uuid

from ohm.graph.methods import oppositional_review
from ohm.graph.queries import create_edge, create_node, find_homogeneous_causes
from ohm.framework.sdk import Graph


# ── Helpers ──────────────────────────────────────────────────────────────────


def _node(conn, label="node", created_by="metis", confidence=0.5) -> str:
    return create_node(
        conn,
        label=label,
        node_type="concept",
        created_by=created_by,
        confidence=confidence,
    )["id"]


def _causes(conn, frm, to, created_by="metis", confidence=0.8) -> str:
    return create_edge(
        conn,
        from_node=frm,
        to_node=to,
        layer="L3",
        edge_type="CAUSES",
        created_by=created_by,
        confidence=confidence,
    )["id"]


def _support(conn, target_edge, target_from, target_to,
             created_by="clio", source_tier="raw", confidence=0.7) -> str:
    """Insert a SUPPORTS edge backing `target_edge` with a controlled source_tier.

    create_support() does not accept source_tier, so we insert directly to
    exercise the homogeneity logic across tiers.
    """
    sid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
             (id, from_node, to_node, layer, edge_type, created_by,
              confidence, challenge_of, challenge_type, source_tier)
           VALUES (?, ?, ?, 'L3', 'SUPPORTS', ?, ?, ?, 'SUPPORTS', ?)""",
        [sid, target_from, target_to, created_by, confidence, target_edge, source_tier],
    )
    return sid


def _homogeneous_setup(conn, *, tiers=("raw", "raw"), agents=("clio", "clio"),
                       cause_confidence=0.8) -> str:
    """Build a CAUSES edge with two SUPPORTS edges and return the causes edge id."""
    src = _node(conn, label="source")
    tgt = _node(conn, label="target")
    cause_id = _causes(conn, src, tgt, confidence=cause_confidence)
    _support(conn, cause_id, src, tgt, created_by=agents[0], source_tier=tiers[0])
    _support(conn, cause_id, src, tgt, created_by=agents[1], source_tier=tiers[1])
    return cause_id


# ── find_homogeneous_causes ───────────────────────────────────────────────────


class TestFindHomogeneousCauses:
    def test_homogeneous_same_tier_flagged(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "raw"))
        results = find_homogeneous_causes(test_db)
        entry = next((r for r in results if r["edge_id"] == cause_id), None)
        assert entry is not None
        assert entry["homogeneity_score"] == 1.0
        assert entry["support_count"] == 2
        assert entry["distinct_tiers"] == 1

    def test_mixed_tiers_not_flagged(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "official"))
        results = find_homogeneous_causes(test_db)
        assert cause_id not in [r["edge_id"] for r in results]

    def test_min_support_count_respected(self, test_db):
        src = _node(test_db, label="s")
        tgt = _node(test_db, label="t")
        cause_id = _causes(test_db, src, tgt)
        _support(test_db, cause_id, src, tgt, source_tier="raw")
        results = find_homogeneous_causes(test_db)
        assert cause_id not in [r["edge_id"] for r in results]

    def test_null_tiers_are_homogeneous(self, test_db):
        src = _node(test_db, label="s")
        tgt = _node(test_db, label="t")
        cause_id = _causes(test_db, src, tgt)
        for _ in range(2):
            sid = str(uuid.uuid4())
            test_db.execute(
                """INSERT INTO ohm_edges
                     (id, from_node, to_node, layer, edge_type, created_by,
                      confidence, challenge_of, challenge_type)
                   VALUES (?, ?, ?, 'L3', 'SUPPORTS', 'clio', 0.7, ?, 'SUPPORTS')""",
                [sid, src, tgt, cause_id],
            )
        results = find_homogeneous_causes(test_db)
        entry = next((r for r in results if r["edge_id"] == cause_id), None)
        assert entry is not None
        assert entry["homogeneity_score"] == 1.0

    def test_below_min_confidence_excluded(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "raw"), cause_confidence=0.3)
        results = find_homogeneous_causes(test_db, min_confidence=0.5)
        assert cause_id not in [r["edge_id"] for r in results]

    def test_non_causes_edges_not_considered(self, test_db):
        src = _node(test_db, label="s")
        tgt = _node(test_db, label="t")
        edge_id = create_edge(
            test_db, from_node=src, to_node=tgt, layer="L3",
            edge_type="SUPPORTS", created_by="metis", confidence=0.8,
        )["id"]
        _support(test_db, edge_id, src, tgt, source_tier="raw")
        _support(test_db, edge_id, src, tgt, source_tier="raw")
        results = find_homogeneous_causes(test_db)
        assert edge_id not in [r["edge_id"] for r in results]

    def test_target_node_filter_scopes_results(self, test_db):
        cause1 = _homogeneous_setup(test_db, tiers=("raw", "raw"))
        src2 = _node(test_db, label="s2")
        tgt2 = _node(test_db, label="t2")
        cause2 = _causes(test_db, src2, tgt2)
        _support(test_db, cause2, src2, tgt2, source_tier="raw")
        _support(test_db, cause2, src2, tgt2, source_tier="raw")
        results = find_homogeneous_causes(test_db, target_node_id=tgt2)
        ids = [r["edge_id"] for r in results]
        assert cause2 in ids
        assert cause1 not in ids

    def test_reason_string_mentions_tier(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "raw"))
        results = find_homogeneous_causes(test_db)
        entry = next(r for r in results if r["edge_id"] == cause_id)
        assert "raw" in entry["reason"]
        assert "homogeneous" in entry["reason"]


# ── oppositional_review ───────────────────────────────────────────────────────


class TestOppositionalReview:
    def test_flag_only_does_not_create_challenges(self, test_db):
        _homogeneous_setup(test_db, tiers=("raw", "raw"))
        before = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY'"
        ).fetchone()[0]
        result = oppositional_review(test_db, auto_challenge=False)
        after = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY'"
        ).fetchone()[0]
        assert result["challenged_edges"] == []
        assert before == after
        assert result["flagged_edges"]

    def test_auto_challenge_creates_challenged_by(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "raw"))
        result = oppositional_review(
            test_db, auto_challenge=True, reviewer_agent="system_oppositional",
        )
        assert result["challenged_edges"]
        assert result["challenged_edges"][0]["edge_id"] == cause_id
        challenge_count = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND challenge_of = ?",
            [cause_id],
        ).fetchone()[0]
        assert challenge_count == 1

    def test_review_summary_has_required_fields(self, test_db):
        _homogeneous_setup(test_db, tiers=("raw", "raw"))
        result = oppositional_review(test_db)
        summary = result["review_summary"]
        for key in ("total_flagged", "total_challenged", "dimensions_used",
                    "homogeneity_threshold", "auto_challenge"):
            assert key in summary, f"Missing summary field: {key}"
        assert "source_tier" in summary["dimensions_used"]
        assert summary["auto_challenge"] is False

    def test_respects_limit(self, test_db):
        for _ in range(3):
            src = _node(test_db)
            tgt = _node(test_db)
            cid = _causes(test_db, src, tgt)
            _support(test_db, cid, src, tgt, source_tier="raw")
            _support(test_db, cid, src, tgt, source_tier="raw")
        result = oppositional_review(test_db, limit=2)
        assert len(result["flagged_edges"]) <= 2

    def test_empty_graph_returns_empty(self, test_db):
        result = oppositional_review(test_db)
        assert result["flagged_edges"] == []
        assert result["review_summary"]["total_flagged"] == 0


# ── SDK parity ────────────────────────────────────────────────────────────────


class TestSdkOppositionalReview:
    def test_sdk_run_oppositional_review(self, test_db):
        _homogeneous_setup(test_db, tiers=("raw", "raw"))
        g = Graph(test_db, actor="metis")
        result = g.run_oppositional_review(auto_challenge=False)
        assert result["flagged_edges"]
        assert result["review_summary"]["auto_challenge"] is False

    def test_sdk_auto_challenge_uses_actor(self, test_db):
        cause_id = _homogeneous_setup(test_db, tiers=("raw", "raw"))
        g = Graph(test_db, actor="metis")
        result = g.run_oppositional_review(auto_challenge=True)
        assert result["challenged_edges"]
        challenger = test_db.execute(
            "SELECT created_by FROM ohm_edges WHERE edge_type = 'CHALLENGED_BY' AND challenge_of = ?",
            [cause_id],
        ).fetchone()
        assert challenger is not None
        assert challenger[0] == "metis"
