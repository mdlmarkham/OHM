"""Tests for OHM-yiui: outcome claimed_by / verified_by columns.

When an agent records an outcome via /record-outcome, the outcome should
auto-derive ``claimed_by`` from the originating edge's ``created_by``
(the agent who made the claim), and ``verified_by`` from the recorder
(the agent who verified it). This fixes a data-model gap where
``source_agent`` was caller-supplied and could be wrong.

Coverage:
- query_record_outcome auto-populates claimed_by from the edge's created_by
- query_record_outcome sets verified_by = recorded_by
- query_record_outcome falls back to source_agent when no edge is found
- query_source_reliability uses claimed_by (via COALESCE)
- effective_reliability uses claimed_by (via COALESCE)
- Migration: existing outcomes get claimed_by backfilled
- Schema: fresh DBs include the new columns
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_db() -> duckdb.DuckDBPyConnection:
    """Fresh in-memory DuckDB with OHM schema (includes migration 0.41.0)."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from ohm.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


def _seed_node(conn, node_id: str, label: str = "Test", created_by: str = "seeder") -> None:
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES (?, ?, 'concept', ?, CURRENT_TIMESTAMP)",
        [node_id, label, created_by],
    )


def _seed_edge(conn, from_node: str, to_node: str, created_by: str = "claimer", edge_type: str = "CAUSES", layer: str = "L3") -> str:
    import uuid

    eid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, 0.7, ?, CURRENT_TIMESTAMP)",
        [eid, from_node, to_node, layer, edge_type, created_by],
    )
    return eid


class TestRecordOutcomeAutoPopulatesClaimedBy:
    """query_record_outcome should auto-derive claimed_by from the edge."""

    def test_claimed_by_set_from_edge_created_by(self):
        """When an edge exists for the claim_node, claimed_by should be
        the edge's created_by, not the caller-supplied source_agent."""
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "claim_1", "Claim 1", created_by="seeder")
            _seed_node(conn, "target_1", "Target 1", created_by="seeder")
            # Edge from claim_1 -> target_1, created by "claimer_agent"
            _seed_edge(conn, "claim_1", "target_1", created_by="claimer_agent")

            # Record an outcome with source_agent="verifier" (wrong agent)
            result = query_record_outcome(
                conn,
                source_agent="verifier_agent",
                claim_node="claim_1",
                outcome=True,
                recorded_by="verifier_agent",
            )
            assert result["claimed_by"] == "claimer_agent", f"Expected claimed_by='claimer_agent' (from edge), got '{result['claimed_by']}'"
            assert result["verified_by"] == "verifier_agent"
        finally:
            conn.close()

    def test_claimed_by_falls_back_to_source_agent_when_no_edge(self):
        """When no edge exists for the claim_node, claimed_by falls back
        to the caller-supplied source_agent (backward compat)."""
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "orphan_claim", "Orphan", created_by="seeder")
            # No edge from this node

            result = query_record_outcome(
                conn,
                source_agent="fallback_agent",
                claim_node="orphan_claim",
                outcome=True,
                recorded_by="recorder",
            )
            assert result["claimed_by"] == "fallback_agent", f"Expected claimed_by='fallback_agent' (from source_agent), got '{result['claimed_by']}'"
            assert result["verified_by"] == "recorder"
        finally:
            conn.close()

    def test_verified_by_always_equals_recorded_by(self):
        """verified_by must always match the recorded_by parameter."""
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "claim_2", "Claim 2", created_by="seeder")
            _seed_node(conn, "target_2", "Target 2", created_by="seeder")
            _seed_edge(conn, "claim_2", "target_2", created_by="claimer")

            result = query_record_outcome(
                conn,
                source_agent="src",
                claim_node="claim_2",
                outcome=False,
                recorded_by="the_verifier",
            )
            assert result["verified_by"] == "the_verifier"
        finally:
            conn.close()

    def test_different_verifier_credits_original_claimer(self):
        """The key scenario: agent A makes a claim, agent B records the
        outcome. The credit (claimed_by) should go to A, not B."""
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "claim_a", "Claim A", created_by="seeder")
            _seed_node(conn, "target_a", "Target A", created_by="seeder")
            _seed_edge(conn, "claim_a", "target_a", created_by="agent_a")

            # Agent B records the outcome
            result = query_record_outcome(
                conn,
                source_agent="agent_b",  # wrong -- B is the verifier, not the claimer
                claim_node="claim_a",
                outcome=True,
                recorded_by="agent_b",
            )
            assert result["claimed_by"] == "agent_a", "Credit should flow to agent_a (the claimer), not agent_b (the verifier)"
            assert result["verified_by"] == "agent_b"
        finally:
            conn.close()


class TestSourceReliabilityUsesClaimedBy:
    """query_source_reliability should use COALESCE(claimed_by, source_agent)
    so that credit flows to the original claimer."""

    def test_reliability_counts_claimed_by_not_source_agent(self):
        """If agent A made claims and agent B verified them, the reliability
        for agent A should reflect those outcomes, not agent B's."""
        from ohm.queries import query_record_outcome, query_source_reliability

        conn = _init_db()
        try:
            _seed_node(conn, "c1", "C1", created_by="seeder")
            _seed_node(conn, "t1", "T1", created_by="seeder")
            _seed_edge(conn, "c1", "t1", created_by="agent_a")

            # Agent B verifies agent A's claim
            query_record_outcome(
                conn,
                source_agent="agent_b",
                claim_node="c1",
                outcome=True,
                recorded_by="agent_b",
            )

            # Agent A's reliability should include this outcome
            rel_a = query_source_reliability(conn, "agent_a")
            assert rel_a["total_outcomes"] >= 1, f"agent_a should have outcomes (via claimed_by), got {rel_a['total_outcomes']}"

            # Agent B's reliability should NOT include this outcome
            # (B was the verifier, not the claimer)
            rel_b = query_source_reliability(conn, "agent_b")
            assert rel_b["total_outcomes"] == 0, f"agent_b should have 0 outcomes (B is verifier, not claimer), got {rel_b['total_outcomes']}"
        finally:
            conn.close()


class TestEffectiveReliabilityUsesClaimedBy:
    """calibration.effective_reliability should use COALESCE(claimed_by,
    source_agent) so authority decay tracks the claimer, not the verifier."""

    def test_effective_reliability_credits_claimer(self):
        from ohm.queries import query_record_outcome
        from ohm.graph.calibration import effective_reliability

        conn = _init_db()
        try:
            _seed_node(conn, "c1", "C1", created_by="seeder")
            _seed_node(conn, "t1", "T1", created_by="seeder")
            _seed_edge(conn, "c1", "t1", created_by="agent_a")

            query_record_outcome(
                conn,
                source_agent="agent_b",
                claim_node="c1",
                outcome=True,
                recorded_by="agent_b",
            )

            rel_a = effective_reliability(conn, "agent_a")
            assert rel_a["p_accurate"] is not None, "agent_a should have p_accurate via claimed_by"

            rel_b = effective_reliability(conn, "agent_b")
            assert rel_b["p_accurate"] is None, "agent_b should have no p_accurate (B is verifier)"
        finally:
            conn.close()


class TestSchemaMigration:
    """The migration adds claimed_by and verified_by columns and backfills."""

    def test_fresh_db_has_new_columns(self):
        """A fresh DB (initialize_schema) should include the new columns."""
        conn = _init_db()
        try:
            cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_outcomes' ORDER BY column_name").fetchall()
            col_names = {r[0] for r in cols}
            assert "claimed_by" in col_names
            assert "verified_by" in col_names
        finally:
            conn.close()

    def test_schema_version_bumped_to_0_41_0(self):
        from ohm.graph.schema import SCHEMA_VERSION

        # Monotonic check: SCHEMA_VERSION only ever increases, and pinning an
        # exact string breaks this test on every later, unrelated migration.
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 41, 0)

    def test_backfill_from_edge_created_by(self):
        """Existing outcomes (pre-migration) should get claimed_by
        backfilled from the edge's created_by."""
        conn = _init_db()
        try:
            # Seed a node + edge + outcome manually (simulating pre-migration data)
            _seed_node(conn, "old_claim", "Old Claim", created_by="seeder")
            _seed_node(conn, "old_target", "Old Target", created_by="seeder")
            _seed_edge(conn, "old_claim", "old_target", created_by="original_claimer")

            # Insert an outcome the old way (no claimed_by/verified_by)
            import uuid

            conn.execute(
                "INSERT INTO ohm_outcomes (id, source_agent, claim_node, outcome, recorded_by, notes) VALUES (?, 'old_verifier', 'old_claim', TRUE, 'old_verifier', 'manual')",
                [str(uuid.uuid4())],
            )

            # Run the backfill (simulating what the migration does)
            conn.execute("UPDATE ohm_outcomes SET claimed_by = (  SELECT e.created_by FROM ohm_edges e   WHERE e.from_node = ohm_outcomes.claim_node     AND e.deleted_at IS NULL   ORDER BY e.created_at ASC LIMIT 1) WHERE claimed_by IS NULL")
            conn.execute("UPDATE ohm_outcomes SET verified_by = recorded_by WHERE verified_by IS NULL")
            conn.execute("UPDATE ohm_outcomes SET claimed_by = source_agent WHERE claimed_by IS NULL")

            row = conn.execute("SELECT claimed_by, verified_by FROM ohm_outcomes WHERE claim_node = 'old_claim'").fetchone()
            assert row[0] == "original_claimer", f"Expected claimed_by='original_claimer', got '{row[0]}'"
            assert row[1] == "old_verifier"
        finally:
            conn.close()

    def test_index_on_claimed_by_exists(self):
        """The migration creates an index on claimed_by for fast lookups."""
        conn = _init_db()
        try:
            indexes = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'ohm_outcomes'").fetchall()
            index_names = {r[0] for r in indexes}
            assert "idx_outcomes_claimed_by" in index_names
        finally:
            conn.close()
