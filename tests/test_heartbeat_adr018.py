"""Tests for OHM-hflx — sacred_references + challenge_nudge in agent heartbeat.

Exercises agent_heartbeat() directly against an in-memory DuckDB with the
minimal schema needed (ohm_nodes, ohm_edges, ohm_observations, ohm_agent_state,
ohm_change_log).
"""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.methods import agent_heartbeat


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    """Minimal in-memory DuckDB with required OHM tables."""
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE ohm_nodes (
            id VARCHAR PRIMARY KEY,
            label VARCHAR,
            type VARCHAR DEFAULT 'concept',
            confidence FLOAT DEFAULT 0.5,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohm_edges (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR PRIMARY KEY,
            from_node VARCHAR,
            to_node VARCHAR,
            layer VARCHAR,
            edge_type VARCHAR,
            confidence FLOAT DEFAULT 0.7,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohm_observations (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR PRIMARY KEY,
            node_id VARCHAR,
            type VARCHAR,
            value FLOAT,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohm_agent_state (
            agent_name VARCHAR PRIMARY KEY,
            current_focus VARCHAR,
            last_sync TIMESTAMP,
            updated_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohm_change_log (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR PRIMARY KEY,
            table_name VARCHAR,
            record_id VARCHAR,
            operation VARCHAR,
            actor VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE ohm_outcomes (
            id VARCHAR DEFAULT gen_random_uuid()::VARCHAR PRIMARY KEY,
            claim_node VARCHAR,
            created_by VARCHAR,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    yield c
    c.close()


def _node(c, node_id, label="Node", confidence=0.5, created_by="agent_a"):
    c.execute(
        "INSERT INTO ohm_nodes (id, label, confidence, created_by) VALUES (?, ?, ?, ?)",
        [node_id, label, confidence, created_by],
    )


def _obs(c, node_id, created_by="agent_a"):
    c.execute(
        "INSERT INTO ohm_observations (node_id, type, value, created_by) VALUES (?, 'measurement', 0.5, ?)",
        [node_id, created_by],
    )


def _edge(c, from_node, to_node, layer="L3", edge_type="CAUSES", confidence=0.8, created_by="agent_b"):
    c.execute(
        """INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [from_node, to_node, layer, edge_type, confidence, created_by],
    )


# ── sacred_references ─────────────────────────────────────────────────────────


class TestSacredReferences:
    def test_empty_graph_returns_empty_list(self, conn):
        result = agent_heartbeat(conn, "agent_a")
        assert result["sacred_references"] == []
        assert result["sacred_references_count"] == 0

    def test_high_confidence_node_with_no_obs_is_flagged(self, conn):
        _node(conn, "n1", confidence=0.97, created_by="agent_a")
        result = agent_heartbeat(conn, "agent_a")
        assert result["sacred_references_count"] == 1
        assert result["sacred_references"][0]["node_id"] == "n1"

    def test_threshold_is_0_95_not_lower(self, conn):
        # 0.94 should NOT be flagged (below threshold)
        _node(conn, "below", confidence=0.94, created_by="agent_a")
        # 0.95 SHOULD be flagged
        _node(conn, "at_threshold", confidence=0.95, created_by="agent_a")
        result = agent_heartbeat(conn, "agent_a")
        ids = [r["node_id"] for r in result["sacred_references"]]
        assert "at_threshold" in ids
        assert "below" not in ids

    def test_node_with_observation_is_not_flagged(self, conn):
        _node(conn, "n-obs", confidence=0.99, created_by="agent_a")
        _obs(conn, "n-obs", created_by="agent_a")
        result = agent_heartbeat(conn, "agent_a")
        ids = [r["node_id"] for r in result["sacred_references"]]
        assert "n-obs" not in ids

    def test_other_agents_nodes_not_flagged_for_this_agent(self, conn):
        _node(conn, "other-node", confidence=0.99, created_by="agent_b")
        result = agent_heartbeat(conn, "agent_a")
        assert result["sacred_references_count"] == 0

    def test_deleted_nodes_not_flagged(self, conn):
        _node(conn, "deleted", confidence=0.99, created_by="agent_a")
        conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'deleted'")
        result = agent_heartbeat(conn, "agent_a")
        assert result["sacred_references_count"] == 0

    def test_sacred_reference_record_has_required_fields(self, conn):
        _node(conn, "n-fields", label="My Node", confidence=0.98, created_by="agent_a")
        result = agent_heartbeat(conn, "agent_a")
        assert result["sacred_references"]
        ref = result["sacred_references"][0]
        for key in ("node_id", "label", "type", "confidence", "created_at", "age_days"):
            assert key in ref, f"Missing field: {key}"
        assert ref["confidence"] == pytest.approx(0.98, abs=0.001)

    def test_multiple_sacred_refs_sorted_by_confidence_desc(self, conn):
        for conf in [0.96, 0.99, 0.97]:
            node_id = f"n-{conf}"
            _node(conn, node_id, confidence=conf, created_by="agent_a")
        result = agent_heartbeat(conn, "agent_a")
        confs = [r["confidence"] for r in result["sacred_references"]]
        assert confs == sorted(confs, reverse=True)


# ── challenge_nudge + challenge_ratio ─────────────────────────────────────────


class TestChallengeNudge:
    def test_challenge_ratio_zero_with_no_challenges(self, conn):
        _node(conn, "from1", created_by="agent_b")
        _node(conn, "to1", created_by="agent_b")
        _edge(conn, "from1", "to1", created_by="agent_b")
        result = agent_heartbeat(conn, "agent_a")
        assert result["challenge_ratio"] == 0.0

    def test_challenge_ratio_present_in_response(self, conn):
        result = agent_heartbeat(conn, "agent_a")
        assert "challenge_ratio" in result
        assert isinstance(result["challenge_ratio"], float)

    def test_nudge_when_no_challenges(self, conn):
        _node(conn, "from2", created_by="agent_b")
        _node(conn, "to2", created_by="agent_b")
        _edge(conn, "from2", "to2", created_by="agent_b", edge_type="CAUSES")
        result = agent_heartbeat(conn, "agent_a")
        # challenge_ratio = 0/1 = 0.0 < 0.05, nudge expected
        assert len(result["challenge_nudge"]) >= 1

    def test_nudge_candidates_are_from_other_agents(self, conn):
        _node(conn, "my-from", created_by="agent_a")
        _node(conn, "my-to", created_by="agent_a")
        _edge(conn, "my-from", "my-to", created_by="agent_a", edge_type="CAUSES")
        _node(conn, "other-from", created_by="agent_b")
        _node(conn, "other-to", created_by="agent_b")
        _edge(conn, "other-from", "other-to", created_by="agent_b", edge_type="CAUSES")
        result = agent_heartbeat(conn, "agent_a")
        # Only agent_b's edge should appear in nudge
        for nudge in result["challenge_nudge"]:
            assert nudge["edge_author"] != "agent_a"

    def test_no_nudge_when_ratio_meets_threshold(self, conn):
        # Create 5 L3 edges from agent_b; create 1 CHALLENGED_BY from agent_a
        # → ratio = 1/5 = 0.20 ≥ 0.05 → no nudge
        for i in range(5):
            _node(conn, f"from-r{i}", created_by="agent_b")
            _node(conn, f"to-r{i}", created_by="agent_b")
            eid = f"edge-r{i}"
            conn.execute(
                """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by)
                   VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 'agent_b')""",
                [eid, f"from-r{i}", f"to-r{i}"],
            )
        # Record one challenge from agent_a
        conn.execute(
            """INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence, created_by)
               VALUES ('edge-r0', 'edge-r1', 'L3', 'CHALLENGED_BY', 0.9, 'agent_a')"""
        )
        result = agent_heartbeat(conn, "agent_a")
        assert result["challenge_ratio"] == pytest.approx(0.2, abs=0.01)
        assert result["challenge_nudge"] == []

    def test_nudge_below_5_percent_threshold(self, conn):
        # 40 L3 edges from agent_b, 1 challenge from agent_a
        # → ratio = 1/40 = 0.025 < 0.05 → nudge expected
        for i in range(40):
            _node(conn, f"from-t{i}", created_by="agent_b")
            _node(conn, f"to-t{i}", created_by="agent_b")
            eid = f"edge-t{i}"
            conn.execute(
                """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by)
                   VALUES (?, ?, ?, 'L3', 'CAUSES', 0.8, 'agent_b')""",
                [eid, f"from-t{i}", f"to-t{i}"],
            )
        conn.execute(
            """INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence, created_by)
               VALUES ('edge-t0', 'edge-t1', 'L3', 'CHALLENGED_BY', 0.9, 'agent_a')"""
        )
        result = agent_heartbeat(conn, "agent_a")
        assert result["challenge_ratio"] < 0.05
        assert len(result["challenge_nudge"]) >= 1

    def test_already_challenged_edges_not_in_nudge(self, conn):
        _node(conn, "from-c", created_by="agent_b")
        _node(conn, "to-c", created_by="agent_b")
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by)
               VALUES ('edge-challenged', 'from-c', 'to-c', 'L3', 'CAUSES', 0.85, 'agent_b')"""
        )
        # agent_a already challenged this edge
        conn.execute(
            """INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence, created_by)
               VALUES ('edge-challenged', 'to-c', 'L3', 'CHALLENGED_BY', 0.9, 'agent_a')"""
        )
        result = agent_heartbeat(conn, "agent_a")
        nudge_ids = [n["edge_id"] for n in result["challenge_nudge"]]
        assert "edge-challenged" not in nudge_ids

    def test_nudge_candidate_has_required_fields(self, conn):
        _node(conn, "from-f", label="Source Node", created_by="agent_b")
        _node(conn, "to-f", label="Target Node", created_by="agent_b")
        _edge(conn, "from-f", "to-f", created_by="agent_b", edge_type="CAUSES")
        result = agent_heartbeat(conn, "agent_a")
        if result["challenge_nudge"]:
            nudge = result["challenge_nudge"][0]
            for key in ("edge_id", "from_node", "to_node", "edge_type", "confidence", "edge_author", "from_label", "to_label"):
                assert key in nudge, f"Missing nudge field: {key}"

    def test_empty_graph_no_nudge(self, conn):
        result = agent_heartbeat(conn, "agent_a")
        # 0 L3 edges from others → ratio = 0/1 = 0.0 < 0.05 but nothing to nudge
        assert result["challenge_nudge"] == []


# ── Combined response shape ───────────────────────────────────────────────────


class TestHeartbeatResponseShape:
    def test_all_new_fields_always_present(self, conn):
        result = agent_heartbeat(conn, "agent_a")
        for key in ("sacred_references", "sacred_references_count", "challenge_ratio", "challenge_nudge"):
            assert key in result, f"Missing heartbeat field: {key}"

    def test_heartbeat_creates_agent_state_on_first_call(self, conn):
        result = agent_heartbeat(conn, "brand_new_agent")
        assert result.get("agent_name") == "brand_new_agent" or "last_sync" in result
