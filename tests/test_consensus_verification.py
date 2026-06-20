"""Tests for OHM-2yq2 — consensus verification loops.

Exercises detect_consensus_only_support() and fire_verification_nudge() against
an in-memory DuckDB with the full OHM schema (test_db fixture). A CAUSES edge
is consensus-only when it has SUPPORTS edges but none of the supporters'
from_nodes have a recorded outcome.
"""

from __future__ import annotations

import uuid

import pytest

from ohm.framework.sdk import Graph
from ohm.graph.queries import (
    create_edge,
    create_node,
    detect_consensus_only_support,
    fire_verification_nudge,
)
from ohm.graph.schema import SOURCE_TIER_CEILINGS


# ── Helpers ──────────────────────────────────────────────────────────────────


def _node(conn, label="node", created_by="metis") -> str:
    return create_node(conn, label=label, node_type="concept", created_by=created_by)["id"]


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


def _support(conn, target_edge, frm, to, created_by="clio", source_tier="raw") -> str:
    sid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
             (id, from_node, to_node, layer, edge_type, created_by,
              confidence, challenge_of, challenge_type, source_tier)
           VALUES (?, ?, ?, 'L3', 'SUPPORTS', ?, 0.7, ?, 'SUPPORTS', ?)""",
        [sid, frm, to, created_by, target_edge, source_tier],
    )
    return sid


def _outcome(conn, claim_node, source_agent="clio", outcome=True, recorded_by="clio"):
    conn.execute(
        "INSERT INTO ohm_outcomes (source_agent, claim_node, outcome, recorded_by) VALUES (?, ?, ?, ?)",
        [source_agent, claim_node, outcome, recorded_by],
    )


def _setup_causes_with_support(conn, tiers=("raw",), agents=("clio",)):
    src = _node(conn, label="source")
    tgt = _node(conn, label="target")
    cause_id = _causes(conn, src, tgt)
    for tier, agent in zip(tiers, agents):
        _support(conn, cause_id, src, tgt, created_by=agent, source_tier=tier)
    return cause_id, src, tgt


# ── detect_consensus_only_support ─────────────────────────────────────────────


class TestDetectConsensusOnlySupport:
    def test_no_support_is_not_consensus(self, test_db):
        src = _node(test_db)
        tgt = _node(test_db)
        cause_id = _causes(test_db, src, tgt)
        result = detect_consensus_only_support(test_db, edge_id=cause_id)
        assert result["is_consensus_only"] is False
        assert result["supporting_edges"] == []
        assert result["recommended_ceiling"] is None

    def test_support_without_outcome_is_consensus(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        result = detect_consensus_only_support(test_db, edge_id=cause_id)
        assert result["is_consensus_only"] is True
        assert result["has_verified_outcome"] is False
        assert len(result["supporting_edges"]) == 1
        assert result["strongest_tier"] == "raw"
        assert result["recommended_ceiling"] == SOURCE_TIER_CEILINGS["raw"]

    def test_support_with_outcome_is_not_consensus(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        _outcome(test_db, src)  # outcome on the supporter's from_node
        result = detect_consensus_only_support(test_db, edge_id=cause_id)
        assert result["is_consensus_only"] is False
        assert result["has_verified_outcome"] is True
        assert result["recommended_ceiling"] is None

    def test_strongest_tier_is_highest_ceiling(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(
            test_db,
            tiers=("raw", "official"),
            agents=("clio", "metis"),
        )
        result = detect_consensus_only_support(test_db, edge_id=cause_id)
        assert result["is_consensus_only"] is True
        # 'official' has a higher ceiling than 'raw'
        assert result["strongest_tier"] == "official"
        assert result["recommended_ceiling"] == SOURCE_TIER_CEILINGS["official"]

    def test_supporter_outcome_on_unrelated_node_still_consensus(self, test_db):
        # Outcome on a node that is NOT a supporter's from_node → still consensus
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        other = _node(test_db, label="unrelated")
        _outcome(test_db, other)
        result = detect_consensus_only_support(test_db, edge_id=cause_id)
        assert result["is_consensus_only"] is True


# ── fire_verification_nudge ───────────────────────────────────────────────────


class TestFireVerificationNudge:
    def test_creates_consensus_flag_challenge(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        result = fire_verification_nudge(
            test_db,
            edge_id=cause_id,
            reason="consensus-only support detected",
        )
        assert result["edge_type"] == "CHALLENGED_BY"
        assert result["challenge_of"] == cause_id
        assert result["challenge_type"] == "CONSENSUS_FLAG"
        assert "consensus-only" in result["condition"]
        assert result["created_by"] == "system"

    def test_idempotent_does_not_duplicate(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        first = fire_verification_nudge(test_db, edge_id=cause_id, reason="r1")
        second = fire_verification_nudge(test_db, edge_id=cause_id, reason="r2")
        assert first["id"] == second["id"]
        count = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE challenge_of = ? AND challenge_type = 'CONSENSUS_FLAG'",
            [cause_id],
        ).fetchone()[0]
        assert count == 1

    def test_custom_created_by(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        result = fire_verification_nudge(
            test_db,
            edge_id=cause_id,
            reason="r",
            created_by="metis",
        )
        assert result["created_by"] == "metis"

    def test_edge_not_found_raises(self, test_db):
        with pytest.raises(ValueError):
            fire_verification_nudge(test_db, edge_id="nonexistent-edge", reason="r")


# ── SDK parity ────────────────────────────────────────────────────────────────


class TestSdkConsensus:
    def test_sdk_detect_consensus_only(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        g = Graph(test_db, actor="metis")
        result = g.detect_consensus_only(cause_id)
        assert result["is_consensus_only"] is True

    def test_sdk_fire_verification_nudge(self, test_db):
        cause_id, src, tgt = _setup_causes_with_support(test_db, tiers=("raw",))
        g = Graph(test_db, actor="metis")
        result = g.fire_verification_nudge(cause_id, reason="consensus-only")
        assert result["challenge_type"] == "CONSENSUS_FLAG"
        assert result["created_by"] == "metis"
