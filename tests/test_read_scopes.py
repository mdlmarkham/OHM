"""Tests for per-agent read scopes and temporal pinning (OHM-ybyb, ADR-037)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from ohm.boundary import enforce_read_scope, get_agent_read_scope, set_agent_read_scope
from ohm.exceptions import PermissionDeniedError
from ohm.graph.queries import query_snapshot
from ohm.framework.sdk import Graph


class TestEnforceReadScope:
    def test_null_scope_allows_all(self, test_db):
        conn = test_db
        enforce_read_scope(conn, "agent1", layer="L3")
        enforce_read_scope(conn, "agent1", source_tier="peer_reviewed")

    def test_layer_scope_denies(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"layer": ["L1", "L2"]})
        with pytest.raises(PermissionDeniedError, match="layer"):
            enforce_read_scope(conn, "agent1", layer="L3")

    def test_layer_scope_allows(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"layer": ["L1", "L2", "L3"]})
        enforce_read_scope(conn, "agent1", layer="L3")

    def test_source_tier_scope(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"source_tier": ["peer_reviewed", "expert"]})
        enforce_read_scope(conn, "agent1", source_tier="peer_reviewed")
        with pytest.raises(PermissionDeniedError, match="source_tier"):
            enforce_read_scope(conn, "agent1", source_tier="ugc")

    def test_created_by_scope(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"created_by": ["agent1", "agent2"]})
        enforce_read_scope(conn, "agent1", created_by="agent1")
        with pytest.raises(PermissionDeniedError):
            enforce_read_scope(conn, "agent1", created_by="agent3")


class TestGetAgentReadScope:
    def test_no_config_returns_none(self, test_db):
        assert get_agent_read_scope(test_db, "unknown") is None

    def test_returns_scope(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"layer": ["L3"]})
        scope = get_agent_read_scope(conn, "agent1")
        assert scope == {"layer": ["L3"]}

    def test_clear_scope(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"layer": ["L3"]})
        set_agent_read_scope(conn, "agent1", None)
        assert get_agent_read_scope(conn, "agent1") is None


class TestSetAgentReadScope:
    def test_set_and_update(self, test_db):
        conn = test_db
        set_agent_read_scope(conn, "agent1", {"layer": ["L1"]})
        set_agent_read_scope(conn, "agent1", {"layer": ["L1", "L2"]})
        scope = get_agent_read_scope(conn, "agent1")
        assert scope == {"layer": ["L1", "L2"]}


class TestQuerySnapshotDeletedAt:
    def test_snapshot_excludes_soft_deleted(self, test_db):
        conn = test_db
        conn.execute(
            """INSERT INTO ohm_nodes (id, label, type, created_by, visibility, confidence, created_at)
               VALUES ('n1', 'test', 'concept', 'a1', 'team', 0.9, '2024-01-01 10:00:00')"""
        )
        conn.execute("""UPDATE ohm_nodes SET deleted_at = '2024-06-01 10:00:00' WHERE id = 'n1'""")
        result = query_snapshot(conn, "2024-03-01 10:00:00")
        node_ids = [n["id"] for n in result["nodes"]]
        assert "n1" in node_ids

        result = query_snapshot(conn, "2024-07-01 10:00:00")
        node_ids = [n["id"] for n in result["nodes"]]
        assert "n1" not in node_ids


class TestSDKReadScope:
    def test_sdk_set_and_get(self, test_db):
        conn = test_db
        with Graph(conn, actor="agent1") as g:
            g.set_read_scope({"layer": ["L3"]})
            scope = g.get_read_scope()
            assert scope == {"layer": ["L3"]}

    def test_sdk_clear(self, test_db):
        conn = test_db
        with Graph(conn, actor="agent1") as g:
            g.set_read_scope({"layer": ["L3"]})
            g.set_read_scope(None)
            assert g.get_read_scope() is None
