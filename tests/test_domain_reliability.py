"""Tests for OHM-avkj: domain-aware source reliability.

An agent reliable about cattle health may be unreliable about stock
prices. The ``domain`` column on ``ohm_outcomes`` (derived from the
claim node's ``provenance``) enables per-domain reliability scoring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_db() -> duckdb.DuckDBPyConnection:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from ohm.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


def _seed_node(conn, node_id: str, provenance: str | None = None, created_by: str = "seeder") -> None:
    if provenance:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, provenance, created_at) VALUES (?, ?, 'concept', ?, ?, CURRENT_TIMESTAMP)",
            [node_id, node_id.replace("_", " ").title(), created_by, provenance],
        )
    else:
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES (?, ?, 'concept', ?, CURRENT_TIMESTAMP)",
            [node_id, node_id.replace("_", " ").title(), created_by],
        )


def _seed_edge(conn, from_node: str, to_node: str, created_by: str) -> None:
    import uuid

    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by, created_at) VALUES (?, ?, ?, 'L3', 'CAUSES', 0.7, ?, CURRENT_TIMESTAMP)",
        [str(uuid.uuid4()), from_node, to_node, created_by],
    )


class TestDomainAutoPopulation:
    """query_record_outcome auto-derives domain from the claim node's provenance."""

    def test_domain_set_from_node_provenance(self):
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "cattle_claim", provenance="cattle-health")
            _seed_node(conn, "target", provenance="cattle-health")
            _seed_edge(conn, "cattle_claim", "target", created_by="agent_a")

            result = query_record_outcome(
                conn,
                source_agent="agent_b",
                claim_node="cattle_claim",
                outcome=True,
                recorded_by="agent_b",
            )
            assert result["domain"] == "cattle-health"
        finally:
            conn.close()

    def test_domain_defaults_to_star_when_no_provenance(self):
        from ohm.queries import query_record_outcome

        conn = _init_db()
        try:
            _seed_node(conn, "no_prov_claim")  # no provenance
            _seed_node(conn, "target2")
            _seed_edge(conn, "no_prov_claim", "target2", created_by="agent_a")

            result = query_record_outcome(
                conn,
                source_agent="agent_b",
                claim_node="no_prov_claim",
                outcome=True,
                recorded_by="agent_b",
            )
            assert result["domain"] == "*"
        finally:
            conn.close()


class TestDomainAwareReliability:
    """query_source_reliability with domain filter counts only matching outcomes."""

    def test_domain_filter_counts_only_matching(self):
        """Agent A has outcomes in two domains. Filtering by one domain
        should only count that domain's outcomes."""
        from ohm.queries import query_record_outcome, query_source_reliability

        conn = _init_db()
        try:
            # Domain 1: cattle-health (2 correct, 0 wrong)
            _seed_node(conn, "c1", provenance="cattle-health")
            _seed_node(conn, "t1", provenance="cattle-health")
            _seed_edge(conn, "c1", "t1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="c1", outcome=True, recorded_by="agent_b")
            _seed_node(conn, "c2", provenance="cattle-health")
            _seed_node(conn, "t2", provenance="cattle-health")
            _seed_edge(conn, "c2", "t2", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="c2", outcome=True, recorded_by="agent_b")

            # Domain 2: finance (1 wrong, 0 correct)
            _seed_node(conn, "f1", provenance="finance")
            _seed_node(conn, "ft1", provenance="finance")
            _seed_edge(conn, "f1", "ft1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="f1", outcome=False, recorded_by="agent_b")

            # No filter: total = 3 (2 correct + 1 wrong)
            rel_all = query_source_reliability(conn, "agent_a")
            assert rel_all["total_outcomes"] == 3

            # Filter cattle-health: total = 2 (both correct)
            rel_cattle = query_source_reliability(conn, "agent_a", domain="cattle-health")
            assert rel_cattle["total_outcomes"] == 2
            assert rel_cattle["p_accurate"] == 1.0

            # Filter finance: total = 1 (wrong)
            rel_finance = query_source_reliability(conn, "agent_a", domain="finance")
            assert rel_finance["total_outcomes"] == 1
            assert rel_finance["p_accurate"] == 0.0
        finally:
            conn.close()

    def test_unscoped_outcomes_count_in_all_domains(self):
        """Outcomes with domain='*' (no provenance) count toward every
        domain filter."""
        from ohm.queries import query_record_outcome, query_source_reliability

        conn = _init_db()
        try:
            _seed_node(conn, "unscoped_claim")  # no provenance → domain='*'
            _seed_node(conn, "tgt")
            _seed_edge(conn, "unscoped_claim", "tgt", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="unscoped_claim", outcome=True, recorded_by="agent_b")

            # Filtering by any domain should still find this outcome
            rel = query_source_reliability(conn, "agent_a", domain="cattle-health")
            assert rel["total_outcomes"] == 1, "Unscoped outcomes should count in all domain filters"
        finally:
            conn.close()

    def test_no_domain_filter_backward_compat(self):
        """Without domain=, all outcomes are counted (backward compat)."""
        from ohm.queries import query_record_outcome, query_source_reliability

        conn = _init_db()
        try:
            _seed_node(conn, "c1", provenance="cattle-health")
            _seed_node(conn, "t1", provenance="cattle-health")
            _seed_edge(conn, "c1", "t1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="c1", outcome=True, recorded_by="agent_b")

            _seed_node(conn, "f1", provenance="finance")
            _seed_node(conn, "ft1", provenance="finance")
            _seed_edge(conn, "f1", "ft1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="f1", outcome=False, recorded_by="agent_b")

            # No filter → both domains counted
            rel = query_source_reliability(conn, "agent_a")
            assert rel["total_outcomes"] == 2
        finally:
            conn.close()


class TestDomainAwareEffectiveReliability:
    """calibration.effective_reliability with domain filter."""

    def test_domain_filter_affects_p_accurate(self):
        from ohm.queries import query_record_outcome
        from ohm.graph.calibration import effective_reliability

        conn = _init_db()
        try:
            _seed_node(conn, "c1", provenance="cattle-health")
            _seed_node(conn, "t1", provenance="cattle-health")
            _seed_edge(conn, "c1", "t1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="c1", outcome=True, recorded_by="agent_b")

            _seed_node(conn, "f1", provenance="finance")
            _seed_node(conn, "ft1", provenance="finance")
            _seed_edge(conn, "f1", "ft1", created_by="agent_a")
            query_record_outcome(conn, source_agent="agent_b", claim_node="f1", outcome=False, recorded_by="agent_b")

            rel_cattle = effective_reliability(conn, "agent_a", domain="cattle-health")
            assert rel_cattle["p_accurate"] == 1.0

            rel_finance = effective_reliability(conn, "agent_a", domain="finance")
            assert rel_finance["p_accurate"] == 0.0
        finally:
            conn.close()


class TestSchemaMigration:
    """Migration 0.43.0 adds domain column to ohm_outcomes."""

    def test_fresh_db_has_domain_column(self):
        conn = _init_db()
        try:
            cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_outcomes' AND column_name = 'domain'").fetchall()
            assert len(cols) == 1
        finally:
            conn.close()

    def test_schema_version_is_043(self):
        from ohm.graph.schema import SCHEMA_VERSION

        # Monotonic check: SCHEMA_VERSION only ever increases, and pinning an
        # exact string breaks this test on every later, unrelated migration.
        assert tuple(int(x) for x in SCHEMA_VERSION.split(".")) >= (0, 43, 0)

    def test_domain_index_exists(self):
        conn = _init_db()
        try:
            indexes = conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name = 'ohm_outcomes'").fetchall()
            index_names = {r[0] for r in indexes}
            assert "idx_outcomes_domain" in index_names
        finally:
            conn.close()

    def test_backfill_from_node_provenance(self):
        """Existing outcomes (pre-migration) should get domain backfilled
        from the claim node's provenance."""
        conn = _init_db()
        try:
            _seed_node(conn, "old_claim", provenance="cybersecurity")
            # Insert outcome manually without domain
            import uuid

            conn.execute(
                "INSERT INTO ohm_outcomes (id, source_agent, claim_node, outcome, recorded_by) VALUES (?, 'agent', 'old_claim', TRUE, 'agent')",
                [str(uuid.uuid4())],
            )
            # Run backfill
            conn.execute("UPDATE ohm_outcomes SET domain = (  SELECT n.provenance FROM ohm_nodes n   WHERE n.id = ohm_outcomes.claim_node AND n.deleted_at IS NULL) WHERE domain = '*' OR domain IS NULL")
            conn.execute("UPDATE ohm_outcomes SET domain = '*' WHERE domain IS NULL")

            row = conn.execute("SELECT domain FROM ohm_outcomes WHERE claim_node = 'old_claim'").fetchone()
            assert row[0] == "cybersecurity"
        finally:
            conn.close()
