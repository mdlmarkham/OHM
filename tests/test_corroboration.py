"""Tests for OHM-m32a: cross-graph corroboration.

When two independent sources (different ``created_by`` agents) make the
same L3 claim (same ``to_node`` and same ``edge_type``), the edges are
corroborated. The ``corroboration_count`` column tracks how many
independent agents made the same claim, and the effective confidence
gets a small bump.
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


def _seed_node(conn, node_id: str, created_by: str = "seeder") -> None:
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES (?, ?, 'concept', ?, CURRENT_TIMESTAMP)",
        [node_id, node_id.replace("_", " ").title(), created_by],
    )


def _seed_edge(conn, from_node: str, to_node: str, created_by: str, edge_type: str = "CAUSES", layer: str = "L3", confidence: float = 0.7) -> str:
    import uuid

    eid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
        [eid, from_node, to_node, layer, edge_type, confidence, created_by],
    )
    return eid


class TestComputeEdgeCorroboration:
    """compute_edge_corroboration() populates the corroboration_count column."""

    def test_single_edge_has_zero_corroboration(self):
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src1")
            _seed_node(conn, "tgt1")
            _seed_edge(conn, "src1", "tgt1", created_by="agent_a")

            summary = compute_edge_corroboration(conn)
            assert summary["total_edges"] == 1
            assert summary["corroborated_edges"] == 0
            assert summary["max_count"] == 0

            count = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE from_node = 'src1'").fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_two_agents_same_claim_corroborate(self):
        """Two agents making the same CAUSES claim to the same target
        should each get corroboration_count=1."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "target")
            _seed_edge(conn, "src_a", "target", created_by="agent_a")
            _seed_edge(conn, "src_b", "target", created_by="agent_b")

            summary = compute_edge_corroboration(conn)
            assert summary["corroborated_edges"] == 2
            assert summary["max_count"] == 1

            counts = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE to_node = 'target' ORDER BY created_by").fetchall()
            assert all(r[0] == 1 for r in counts)
        finally:
            conn.close()

    def test_three_agents_give_count_2(self):
        """Three agents making the same claim → each gets count=2
        (two OTHER agents corroborating)."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "src_c")
            _seed_node(conn, "target")
            _seed_edge(conn, "src_a", "target", created_by="agent_a")
            _seed_edge(conn, "src_b", "target", created_by="agent_b")
            _seed_edge(conn, "src_c", "target", created_by="agent_c")

            compute_edge_corroboration(conn)
            counts = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE to_node = 'target'").fetchall()
            assert all(r[0] == 2 for r in counts)
        finally:
            conn.close()

    def test_same_agent_multiple_edges_no_corroboration(self):
        """The same agent making multiple edges to the same target does
        NOT count as corroboration — corroboration requires INDEPENDENT
        sources."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src1")
            _seed_node(conn, "src2")
            _seed_node(conn, "target")
            _seed_edge(conn, "src1", "target", created_by="agent_a")
            _seed_edge(conn, "src2", "target", created_by="agent_a")  # same agent

            compute_edge_corroboration(conn)
            counts = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE to_node = 'target'").fetchall()
            assert all(r[0] == 0 for r in counts), "Same agent making multiple edges should NOT corroborate"
        finally:
            conn.close()

    def test_different_edge_types_dont_corroborate(self):
        """A CAUSES edge and a SUPPORTS edge to the same target are
        different claims — they don't corroborate each other."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "target")
            _seed_edge(conn, "src_a", "target", created_by="agent_a", edge_type="CAUSES")
            _seed_edge(conn, "src_b", "target", created_by="agent_b", edge_type="SUPPORTS")

            compute_edge_corroboration(conn)
            counts = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE to_node = 'target'").fetchall()
            assert all(r[0] == 0 for r in counts)
        finally:
            conn.close()

    def test_non_l3_edges_reset_to_zero(self):
        """L2 edges don't get corroboration — only L3 knowledge claims."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "target")
            _seed_edge(conn, "src_a", "target", created_by="agent_a", layer="L2")
            _seed_edge(conn, "src_b", "target", created_by="agent_b", layer="L2")

            compute_edge_corroboration(conn)
            counts = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE to_node = 'target'").fetchall()
            assert all(r[0] == 0 for r in counts)
        finally:
            conn.close()

    def test_soft_deleted_edges_excluded(self):
        """Soft-deleted edges should not count as corroborators."""
        from ohm.graph.corroboration import compute_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "target")
            eid_a = _seed_edge(conn, "src_a", "target", created_by="agent_a")
            _seed_edge(conn, "src_b", "target", created_by="agent_b")
            # Soft-delete agent_a's edge
            conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [eid_a])

            compute_edge_corroboration(conn)
            # agent_b's edge should have 0 corroboration (agent_a's is deleted)
            count_b = conn.execute("SELECT corroboration_count FROM ohm_edges WHERE created_by = 'agent_b'").fetchone()[0]
            assert count_b == 0
        finally:
            conn.close()


class TestGetEdgeCorroboration:
    """get_edge_corroboration() returns detail for a single edge."""

    def test_returns_corroboration_detail(self):
        from ohm.graph.corroboration import get_edge_corroboration

        conn = _init_db()
        try:
            _seed_node(conn, "src_a")
            _seed_node(conn, "src_b")
            _seed_node(conn, "target")
            eid = _seed_edge(conn, "src_a", "target", created_by="agent_a", confidence=0.7)
            _seed_edge(conn, "src_b", "target", created_by="agent_b", confidence=0.8)

            from ohm.graph.corroboration import compute_edge_corroboration

            compute_edge_corroboration(conn)

            detail = get_edge_corroboration(conn, eid)
            assert detail["edge_id"] == eid
            assert detail["corroboration_count"] == 1
            assert detail["confidence"] == pytest.approx(0.7)
            # Effective confidence = 0.7 * (1 + 0.1*1) = 0.77
            assert detail["effective_confidence"] == pytest.approx(0.77, abs=0.01)
            assert len(detail["corroborating_edges"]) == 1
            assert detail["corroborating_edges"][0]["created_by"] == "agent_b"
        finally:
            conn.close()

    def test_raises_on_missing_edge(self):
        from ohm.graph.corroboration import get_edge_corroboration
        from ohm.exceptions import EdgeNotFoundError

        conn = _init_db()
        try:
            with pytest.raises(EdgeNotFoundError):
                get_edge_corroboration(conn, "nonexistent_edge")
        finally:
            conn.close()


class TestEffectiveConfidenceWithCorroboration:
    """The pure function for computing effective confidence."""

    def test_no_corroboration_returns_base(self):
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        assert effective_confidence_with_corroboration(0.7, 0) == 0.7

    def test_one_corroboration_bumps_10_percent(self):
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        result = effective_confidence_with_corroboration(0.7, 1)
        assert result == pytest.approx(0.77, abs=0.001)

    def test_five_corroboration_bumps_50_percent(self):
        """5 corroborators → 50% bump. 0.7 * 1.5 = 1.05, capped at 1.0."""
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        result = effective_confidence_with_corroboration(0.7, 5)
        assert result == 1.0  # capped at 1.0

    def test_five_corroboration_low_confidence_not_capped(self):
        """With lower base confidence, the bump is visible."""
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        result = effective_confidence_with_corroboration(0.5, 5)
        assert result == pytest.approx(0.75, abs=0.001)  # 0.5 * 1.5 = 0.75

    def test_capped_at_max_corroboration(self):
        """Beyond MAX_CORROBORATION (5), the bump doesn't increase."""
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        r5 = effective_confidence_with_corroboration(0.7, 5)
        r10 = effective_confidence_with_corroboration(0.7, 10)
        assert r5 == r10

    def test_capped_at_1_0(self):
        """Effective confidence never exceeds 1.0."""
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        result = effective_confidence_with_corroboration(0.9, 5)
        assert result == 1.0  # 0.9 * 1.5 = 1.35 → capped at 1.0

    def test_non_l3_no_bump(self):
        from ohm.graph.corroboration import effective_confidence_with_corroboration

        assert effective_confidence_with_corroboration(0.7, 5, layer="L2") == 0.7


class TestSchemaMigration:
    """The migration adds corroboration_count to ohm_edges."""

    def test_fresh_db_has_column(self):
        conn = _init_db()
        try:
            cols = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_edges' AND column_name = 'corroboration_count'").fetchall()
            assert len(cols) == 1
        finally:
            conn.close()

    def test_schema_version_is_042(self):
        from ohm.graph.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION == "0.44.0"
