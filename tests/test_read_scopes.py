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


class TestNonDictReadScopeGuard:
    """Defensive guard: a non-dict read_scope JSON value degrades to full-access
    (None) instead of raising AttributeError (OHM #921).

    A non-dict scope can only enter via raw SQL, a migration, or a DuckLake sync
    from a stale replica; set_agent_read_scope validates dict shape on the writer
    side. The guard mirrors the existing fallback semantics for unparseable
    scopes (boundary.py:172-173).
    """

    def test_scalar_number_scope_returns_none(self, test_db):
        conn = test_db
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) "
            "VALUES ('agent1', 'default', '0')"
        )
        assert get_agent_read_scope(conn, "agent1") is None

    def test_scalar_number_scope_enforce_does_not_raise(self, test_db):
        conn = test_db
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) "
            "VALUES ('agent1', 'default', '0')"
        )
        enforce_read_scope(conn, "agent1", layer="L3")
        enforce_read_scope(conn, "agent1", source_tier="peer_reviewed")
        enforce_read_scope(conn, "agent1", created_by="agent2")
        enforce_read_scope(conn, "agent1", node_id="n1")

    def test_scalar_string_scope_returns_none(self, test_db):
        conn = test_db
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) "
            "VALUES ('agent1', 'default', '\"oops\"')"
        )
        assert get_agent_read_scope(conn, "agent1") is None

    def test_list_scope_returns_none(self, test_db):
        conn = test_db
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) "
            "VALUES ('agent1', 'default', '[1, 2, 3]')"
        )
        assert get_agent_read_scope(conn, "agent1") is None

    def test_scalar_scope_enforce_allows_all_layers(self, test_db):
        conn = test_db
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) "
            "VALUES ('agent1', 'default', '1')"
        )
        enforce_read_scope(conn, "agent1", layer="L1")
        enforce_read_scope(conn, "agent1", layer="L2")
        enforce_read_scope(conn, "agent1", layer="L3")
        enforce_read_scope(conn, "agent1", layer="L4")
