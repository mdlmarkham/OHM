"""Tests for OHM-jx4q: orphan rate reporting + heartbeat nudge.

Covers:
- graph_health() distinguishes L0 fragment orphans from L1-L3 orphans
- The new fields are backward-compatible (orphan_nodes still returns total)
- orphan_rate_non_fragments respects the 10% threshold signal
- agent_heartbeat() emits orphan_rate_nudge when the rate exceeds 10%
- The nudge lists non-fragment orphans (fragments are NOT nudged)
- Under threshold: no nudge but the rate is still reported
- Edge case: empty graph
- Edge case: only fragments, no non-fragment orphans
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _init_db() -> duckdb.DuckDBPyConnection:
    """Fresh in-memory DuckDB with OHM schema."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from ohm.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


def _insert_node(conn, node_id: str, label: str, ntype: str = "concept", created_by: str = "test", confidence: float = 0.5) -> None:
    """Insert a node with the minimum required columns."""
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, created_by, created_at, confidence) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)",
        [node_id, label, ntype, created_by, confidence],
    )


def _insert_edge(conn, from_id: str, to_id: str, layer: str = "L3", edge_type: str = "CAUSES", confidence: float = 0.5) -> None:
    """Insert an edge with the minimum required columns."""
    edge_id = f"edge_{from_id}_{to_id}"
    conn.execute(
        "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, 'test', CURRENT_TIMESTAMP)",
        [edge_id, from_id, to_id, layer, edge_type, confidence],
    )


class TestGraphHealthOrphanBreakdown:
    """OHM-jx4q: graph_health() must distinguish L0 fragment orphans
    from L1-L3 orphans so triage has an actionable signal."""

    def test_orphan_breakdown_when_no_orphans(self):
        from ohm.queries import query_graph_health

        conn = _init_db()
        try:
            h = query_graph_health(conn)
            assert h["orphan_nodes"] == 0
            assert h["orphan_nodes_total"] == 0
            assert h["orphan_nodes_fragments"] == 0
            assert h["orphan_nodes_non_fragments"] == 0
            assert h["orphan_rate_non_fragments"] == 0.0
            assert h["orphan_threshold"] == 0.10
            assert h["orphan_threshold_exceeded"] is False
        finally:
            conn.close()

    def test_orphan_breakdown_separates_fragments(self):
        from ohm.queries import query_graph_health

        conn = _init_db()
        try:
            # 3 non-fragment orphans, no edges
            for i in range(3):
                _insert_node(conn, f"orphan_{i}", f"Orphan {i}", "concept")
            # 5 fragment orphans (L0)
            for i in range(5):
                _insert_node(conn, f"frag_{i}", f"Frag {i}", "fragment")
            # 2 connected concept nodes (NOT orphans)
            _insert_node(conn, "c_a", "Connected A", "concept")
            _insert_node(conn, "c_b", "Connected B", "concept")
            _insert_edge(conn, "c_a", "c_b")

            h = query_graph_health(conn)
            assert h["orphan_nodes_total"] == 8
            assert h["orphan_nodes_fragments"] == 5
            assert h["orphan_nodes_non_fragments"] == 3
            # total_nodes excludes fragments: 3 (orphans) + 2 (connected) = 5
            assert h["total_nodes"] == 5
            # 3 / 5 = 0.6
            assert h["orphan_rate_non_fragments"] == 0.6
            assert h["orphan_threshold_exceeded"] is True
        finally:
            conn.close()

    def test_orphan_rate_below_threshold(self):
        from ohm.queries import query_graph_health

        conn = _init_db()
        try:
            # 1 orphan, 19 connected -> 1/20 = 0.05 (below 10% threshold)
            _insert_node(conn, "lonely", "Lonely", "concept")
            for i in range(19):
                _insert_node(conn, f"c_{i}", f"C{i}", "concept")
            # Connect them all in a chain
            for i in range(18):
                _insert_edge(conn, f"c_{i}", f"c_{i + 1}")

            h = query_graph_health(conn)
            assert h["orphan_nodes_non_fragments"] == 1
            assert h["total_nodes"] == 20
            assert h["orphan_rate_non_fragments"] == 0.05
            assert h["orphan_threshold_exceeded"] is False
        finally:
            conn.close()

    def test_orphan_rate_only_fragments_is_zero(self):
        from ohm.queries import query_graph_health

        conn = _init_db()
        try:
            # Only fragments exist. All are orphans (fragments never get
            # edges), but the non-fragment rate is 0/0 = 0.
            for i in range(5):
                _insert_node(conn, f"frag_{i}", f"Frag {i}", "fragment")

            h = query_graph_health(conn)
            assert h["orphan_nodes_total"] == 5
            assert h["orphan_nodes_fragments"] == 5
            assert h["orphan_nodes_non_fragments"] == 0
            assert h["total_nodes"] == 0
            assert h["orphan_rate_non_fragments"] == 0.0
            # 0% is not > 10%, so threshold NOT exceeded
            assert h["orphan_threshold_exceeded"] is False
        finally:
            conn.close()

    def test_backward_compatibility_orphan_nodes_key_preserved(self):
        """The old ``orphan_nodes`` key is kept for backward compatibility
        and equals the total. New code should migrate to
        ``orphan_nodes_total`` / ``orphan_nodes_non_fragments``."""
        from ohm.queries import query_graph_health

        conn = _init_db()
        try:
            _insert_node(conn, "c1", "C1", "concept")
            _insert_node(conn, "c2", "C2", "concept")
            _insert_node(conn, "f1", "F1", "fragment")
            h = query_graph_health(conn)
            # Both keys return the same value
            assert h["orphan_nodes"] == h["orphan_nodes_total"]
        finally:
            conn.close()


class TestAgentHeartbeatOrphanNudge:
    """OHM-jx4q: agent_heartbeat() emits orphan_rate_nudge when
    non-fragment orphan rate exceeds 10%."""

    def test_no_nudge_below_threshold(self):
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            # 1 orphan, 19 connected = 5% rate (below 10%)
            _insert_node(conn, "lonely", "Lonely", "concept")
            for i in range(19):
                _insert_node(conn, f"c_{i}", f"C{i}", "concept")
            for i in range(18):
                _insert_edge(conn, f"c_{i}", f"c_{i + 1}")

            h = agent_heartbeat(conn, "test_agent")
            assert "orphan_rate" in h
            assert h["orphan_rate"] == 0.05
            assert h["orphan_threshold_exceeded"] is False
            assert h["orphan_rate_nudge"] == []
            assert h["orphan_rate_nudge_count"] == 0
        finally:
            conn.close()

    def test_nudge_above_threshold(self):
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            # 3 orphans, 2 connected = 60% rate (above 10%)
            for i in range(3):
                _insert_node(conn, f"orphan_{i}", f"Orphan {i}", "concept", confidence=0.5 + i * 0.1)
            _insert_node(conn, "c_a", "A", "concept")
            _insert_node(conn, "c_b", "B", "concept")
            _insert_edge(conn, "c_a", "c_b")

            h = agent_heartbeat(conn, "test_agent")
            assert h["orphan_rate"] == 0.6
            assert h["orphan_threshold_exceeded"] is True
            assert h["orphan_rate_nudge_count"] == 3
            # The nudge is sorted by confidence DESC
            confidences = [n["confidence"] for n in h["orphan_rate_nudge"]]
            assert confidences == sorted(confidences, reverse=True)
        finally:
            conn.close()

    def test_nudge_excludes_fragments(self):
        """L0 fragments are NOT nudged even when they have zero edges --
        they're expected to be ephemeral and the agent shouldn't have to
        triage them manually."""
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            # 1 non-fragment orphan + 10 fragment orphans + 1 connected
            _insert_node(conn, "real_orphan", "Real orphan", "concept", confidence=0.9)
            for i in range(10):
                _insert_node(conn, f"frag_{i}", f"Frag {i}", "fragment")
            _insert_node(conn, "c_a", "A", "concept")
            _insert_node(conn, "c_b", "B", "concept")
            _insert_edge(conn, "c_a", "c_b")
            # 1 / 2 non-fragment nodes = 50% rate, above 10% threshold
            h = agent_heartbeat(conn, "test_agent")
            assert h["orphan_threshold_exceeded"] is True
            # Only the real orphan is nudged, not any fragments
            assert h["orphan_rate_nudge_count"] == 1
            assert h["orphan_rate_nudge"][0]["node_id"] == "real_orphan"
            assert h["orphan_rate_nudge"][0]["type"] == "concept"
        finally:
            conn.close()

    def test_nudge_excludes_zero_node_graph(self):
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            h = agent_heartbeat(conn, "test_agent")
            assert h["orphan_rate"] == 0.0
            assert h["orphan_threshold_exceeded"] is False
            assert h["orphan_rate_nudge"] == []
            assert h["orphan_rate_nudge_count"] == 0
        finally:
            conn.close()

    def test_nudge_caps_at_5_results(self):
        """The nudge returns at most 5 orphans -- if there are more, the
        agent runs /suggest or /islands for the rest."""
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            # 10 non-fragment orphans, 1 connected = 10/11 = 90.9%
            for i in range(10):
                _insert_node(conn, f"orphan_{i}", f"O{i}", "concept", confidence=0.5)
            _insert_node(conn, "c_a", "A", "concept")
            _insert_node(conn, "c_b", "B", "concept")
            _insert_edge(conn, "c_a", "c_b")

            h = agent_heartbeat(conn, "test_agent")
            assert h["orphan_threshold_exceeded"] is True
            # Capped at 5 even though 10 orphans exist
            assert h["orphan_rate_nudge_count"] == 5
            assert len(h["orphan_rate_nudge"]) == 5
        finally:
            conn.close()

    def test_nudge_does_not_include_deleted_orphans(self):
        """Soft-deleted orphans (deleted_at IS NOT NULL) are excluded."""
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            _insert_node(conn, "real_orphan", "Real", "concept", confidence=0.9)
            _insert_node(conn, "deleted_orphan", "Deleted", "concept", confidence=0.9)
            # Soft-delete one
            conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'deleted_orphan'")
            # 0/1 = 0% (the only non-deleted non-fragment node has no edges... wait, "real_orphan" has no edges)
            h = agent_heartbeat(conn, "test_agent")
            # Both inserted nodes have no edges, so both are orphans
            # After soft-delete, only "real_orphan" counts
            assert h["orphan_rate_orphans"] == 1
            assert h["orphan_rate_total"] == 1
            assert h["orphan_rate"] == 1.0  # 1/1 = 100%
            assert h["orphan_threshold_exceeded"] is True
            assert h["orphan_rate_nudge_count"] == 1
            assert h["orphan_rate_nudge"][0]["node_id"] == "real_orphan"
        finally:
            conn.close()

    def test_nudge_keys_present_even_when_empty(self):
        """The state dict must always have orphan_rate_nudge and
        orphan_rate_nudge_count keys, even when no nudge is needed.
        Other code paths and dashboards key off these."""
        from ohm.methods import agent_heartbeat

        conn = _init_db()
        try:
            # Low-orphan scenario
            _insert_node(conn, "a", "A", "concept")
            _insert_node(conn, "b", "B", "concept")
            _insert_edge(conn, "a", "b")
            h = agent_heartbeat(conn, "test_agent")
            assert "orphan_rate_nudge" in h
            assert "orphan_rate_nudge_count" in h
            assert h["orphan_rate_nudge_count"] == 0
            assert h["orphan_rate_nudge"] == []
        finally:
            conn.close()
