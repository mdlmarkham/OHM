"""Tests for OHM #917 — agent-facing backend and storage introspection tools.

Covers three layers:
  - Query functions (``query_backend_status``, ``query_storage_efficiency``)
    tested directly against an in-memory DuckDB via the ``test_db`` fixture.
  - HTTP endpoints (``/backend/status``, ``/storage/efficiency``) tested via
    the ``test_server`` fixture.
  - MCP tool registration and dispatch mapping (unit tests).
"""

from __future__ import annotations

import pytest

from ohm.graph.schema import SCHEMA_VERSION
from ohm.mcp.config import WRITE_TOOLS
from ohm.mcp.dispatch import build_request
from ohm.mcp.tools import all_tools

from tests.conftest import create_sample_edge, create_sample_node, create_sample_observation


# ── Query-layer tests ───────────────────────────────────────────────────────


class TestQueryBackendStatus:
    """Tests for ``query_backend_status`` (queries layer)."""

    def test_returns_schema_version(self, test_db):
        from ohm.queries import query_backend_status

        result = query_backend_status(test_db)
        assert result["schema_version"] == SCHEMA_VERSION

    def test_pending_migrations_empty_on_fresh_db(self, test_db):
        from ohm.queries import query_backend_status

        result = query_backend_status(test_db)
        assert result["pending_migrations"] == []

    def test_graph_size_has_all_keys(self, test_db):
        from ohm.queries import query_backend_status

        result = query_backend_status(test_db)
        gs = result["graph_size"]
        assert set(gs.keys()) == {"nodes", "edges", "observations", "fragments"}

    def test_graph_size_empty_db(self, test_db):
        from ohm.queries import query_backend_status

        gs = query_backend_status(test_db)["graph_size"]
        assert gs == {"nodes": 0, "edges": 0, "observations": 0, "fragments": 0}

    def test_graph_size_after_seeding(self, test_db):
        from ohm.queries import query_backend_status

        na = create_sample_node(test_db, label="Alpha")
        nb = create_sample_node(test_db, label="Beta")
        nc = create_sample_node(test_db, label="Gamma")
        create_sample_edge(test_db, from_node=na, to_node=nb)
        create_sample_edge(test_db, from_node=nb, to_node=nc)
        create_sample_observation(test_db, node_id=na, value=0.5)
        create_sample_observation(test_db, node_id=nb, value=0.7)

        gs = query_backend_status(test_db)["graph_size"]
        assert gs["nodes"] == 3
        assert gs["edges"] == 2
        assert gs["observations"] == 2
        assert gs["fragments"] == 0

    def test_fragments_counted_separately(self, test_db):
        from ohm.queries import query_backend_status

        create_sample_node(test_db, label="Concept1", node_type="concept")
        create_sample_node(test_db, label="Frag1", node_type="fragment")
        create_sample_node(test_db, label="Frag2", node_type="fragment")

        gs = query_backend_status(test_db)["graph_size"]
        # Fragments excluded from the main node count (matches query_stats convention)
        assert gs["nodes"] == 1
        assert gs["fragments"] == 2

    def test_pending_migrations_when_version_old(self, test_db):
        """Manually lowering the schema version should surface pending migrations."""
        from ohm.queries import query_backend_status

        test_db.execute("UPDATE ohm_meta SET value = '0.1.0' WHERE key = 'schema_version'")
        result = query_backend_status(test_db)
        assert len(result["pending_migrations"]) > 0
        assert "0.56.0" in result["pending_migrations"]


class TestQueryStorageEfficiency:
    """Tests for ``query_storage_efficiency`` (queries layer)."""

    def test_empty_db(self, test_db):
        from ohm.queries import query_storage_efficiency

        result = query_storage_efficiency(test_db)
        assert result["deleted_rows_estimate"] == 0
        assert result["fragment_ratio"] == 0.0
        assert result["orphan_rate"] == 0.0
        assert result["embedding_coverage"] == 0.0
        assert isinstance(result["recommendation"], str)

    def test_has_all_keys(self, test_db):
        from ohm.queries import query_storage_efficiency

        result = query_storage_efficiency(test_db)
        assert set(result.keys()) == {
            "deleted_rows_estimate",
            "fragment_ratio",
            "orphan_rate",
            "embedding_coverage",
            "recommendation",
        }

    def test_deleted_rows_after_soft_delete(self, test_db):
        from ohm.queries import query_storage_efficiency

        na = create_sample_node(test_db, label="A")
        nb = create_sample_node(test_db, label="B")
        create_sample_edge(test_db, from_node=na, to_node=nb)

        before = query_storage_efficiency(test_db)["deleted_rows_estimate"]
        assert before == 0

        test_db.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [na])
        test_db.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ?", [na])

        after = query_storage_efficiency(test_db)["deleted_rows_estimate"]
        assert after == 2

    def test_orphan_rate_after_seeding_orphans(self, test_db):
        from ohm.queries import query_storage_efficiency

        # Two connected nodes (not orphans)
        na = create_sample_node(test_db, label="Connected A")
        nb = create_sample_node(test_db, label="Connected B")
        create_sample_edge(test_db, from_node=na, to_node=nb)
        # Two orphan nodes (no edges)
        create_sample_node(test_db, label="Orphan 1")
        create_sample_node(test_db, label="Orphan 2")

        result = query_storage_efficiency(test_db)
        # 2 orphans / 4 total non-fragment nodes = 0.5
        assert result["orphan_rate"] == 0.5

    def test_fragment_ratio(self, test_db):
        from ohm.queries import query_storage_efficiency

        create_sample_node(test_db, label="Concept1", node_type="concept")
        create_sample_node(test_db, label="Concept2", node_type="concept")
        create_sample_node(test_db, label="Frag1", node_type="fragment")
        create_sample_node(test_db, label="Frag2", node_type="fragment")

        result = query_storage_efficiency(test_db)
        # 2 fragments / 2 non-fragment nodes = 1.0
        assert result["fragment_ratio"] == 1.0

    def test_embedding_coverage(self, test_db):
        from ohm.queries import query_storage_efficiency

        na = create_sample_node(test_db, label="With Emb")
        nb = create_sample_node(test_db, label="No Emb")
        nc = create_sample_node(test_db, label="With Emb 2")

        embedding = [0.0] * 768
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [embedding, na],
        )
        test_db.execute(
            "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
            [embedding, nc],
        )

        result = query_storage_efficiency(test_db)
        # 2 of 3 active nodes have embeddings
        assert result["embedding_coverage"] == round(2 / 3, 4)

    def test_recommendation_compaction(self, test_db):
        from ohm.queries import query_storage_efficiency

        # Seed 10 active nodes + 10 edges = 20 active rows.
        ids = [create_sample_node(test_db, label=f"N{i}") for i in range(10)]
        for i in range(9):
            create_sample_edge(test_db, from_node=ids[i], to_node=ids[i + 1])
        # Soft-delete enough rows to exceed 5% of active rows (20 * 0.05 = 1).
        test_db.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [ids[0]])
        test_db.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ?", [ids[0]])

        result = query_storage_efficiency(test_db)
        assert "compaction" in result["recommendation"].lower()

    def test_recommendation_orphans(self, test_db):
        from ohm.queries import query_storage_efficiency

        # 10 orphan nodes, 0 edges → orphan_rate = 1.0 (> 0.10)
        for i in range(10):
            create_sample_node(test_db, label=f"Orphan {i}")

        result = query_storage_efficiency(test_db)
        assert "orphan" in result["recommendation"].lower()

    def test_recommendation_good_when_healthy(self, test_db):
        from ohm.queries import query_storage_efficiency

        # A fully connected graph with no deletes, no fragments, full embedding coverage
        na = create_sample_node(test_db, label="A")
        nb = create_sample_node(test_db, label="B")
        create_sample_edge(test_db, from_node=na, to_node=nb)
        embedding = [0.0] * 768
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [embedding, na])
        test_db.execute("UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?", [embedding, nb])

        result = query_storage_efficiency(test_db)
        assert "good" in result["recommendation"].lower()

    def test_recommendation_low_embedding(self, test_db):
        from ohm.queries import query_storage_efficiency

        # 4 connected nodes, no embeddings → coverage 0.0 (< 0.50)
        ids = [create_sample_node(test_db, label=f"N{i}") for i in range(4)]
        create_sample_edge(test_db, from_node=ids[0], to_node=ids[1])
        create_sample_edge(test_db, from_node=ids[1], to_node=ids[2])
        create_sample_edge(test_db, from_node=ids[2], to_node=ids[3])

        result = query_storage_efficiency(test_db)
        assert "embedding" in result["recommendation"].lower()


# ── HTTP endpoint tests ─────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestBackendStatusHTTP:
    """Tests for GET /backend/status via the HTTP daemon."""

    def test_returns_200_with_all_keys(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        expected_keys = {
            "store_type",
            "db_path",
            "catalog_path",
            "tenant_schema",
            "schema_version",
            "pending_migrations",
            "graph_size",
            "storage_bytes",
            "write_mode",
            "tenant_id",
            "agent_profile",
            "sync_status",
            "uptime_seconds",
        }
        assert expected_keys <= set(data.keys())

    def test_store_type_local_duckdb(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["store_type"] == "local_duckdb"

    def test_write_mode_read_write(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["write_mode"] == "read_write"

    def test_storage_bytes_positive(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["storage_bytes"] is not None
        assert data["storage_bytes"] > 0

    def test_schema_version_matches_constant(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["schema_version"] == SCHEMA_VERSION

    def test_pending_migrations_empty(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["pending_migrations"] == []

    def test_graph_size_empty(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        gs = data["graph_size"]
        assert gs["nodes"] == 0
        assert gs["edges"] == 0
        assert gs["observations"] == 0

    def test_graph_size_after_seeding(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence) "
            "VALUES ('n1', 'Node 1', 'concept', 'test', 'team', 'test', 1.0)"
        )
        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence) "
            "VALUES ('n2', 'Node 2', 'concept', 'test', 'team', 'test', 1.0)"
        )
        store.conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence) "
            "VALUES (gen_random_uuid(), 'n1', 'n2', 'L3', 'CAUSES', 'test', 0.9)"
        )

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["graph_size"]["nodes"] == 2
        assert data["graph_size"]["edges"] == 1

    def test_uptime_positive(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["uptime_seconds"] >= 0

    def test_sync_status_null_without_ducklake(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/backend/status")
        assert status == 200
        assert data["sync_status"] is None
        assert data["catalog_path"] is None


@pytest.mark.integration
@pytest.mark.xdist_group("server")
class TestStorageEfficiencyHTTP:
    """Tests for GET /storage/efficiency via the HTTP daemon."""

    def test_returns_200_with_all_keys(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/storage/efficiency")
        assert status == 200
        expected_keys = {
            "deleted_rows_estimate",
            "fragment_ratio",
            "orphan_rate",
            "embedding_coverage",
            "recommendation",
        }
        assert set(data.keys()) == expected_keys

    def test_empty_db_signals(self, test_server):
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("GET", port, "/storage/efficiency")
        assert status == 200
        assert data["deleted_rows_estimate"] == 0
        assert data["fragment_ratio"] == 0.0
        assert data["orphan_rate"] == 0.0
        assert data["embedding_coverage"] == 0.0

    def test_deleted_rows_after_soft_delete(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        store.conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence) "
            "VALUES ('n1', 'Node 1', 'concept', 'test', 'team', 'test', 1.0)"
        )
        store.conn.execute(
            "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence) "
            "VALUES (gen_random_uuid(), 'n1', 'n1', 'L3', 'RELATED_TO', 'test', 0.5)"
        )
        store.conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 'n1'")

        status, data = _request("GET", port, "/storage/efficiency")
        assert status == 200
        assert data["deleted_rows_estimate"] >= 1

    def test_orphan_rate_after_seeding_orphans(self, test_server):
        port, store = test_server
        from tests.conftest import _request

        for i in range(5):
            store.conn.execute(
                "INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence) "
                f"VALUES ('orphan_{i}', 'Orphan {i}', 'concept', 'test', 'team', 'test', 1.0)"
            )

        status, data = _request("GET", port, "/storage/efficiency")
        assert status == 200
        assert data["orphan_rate"] == 1.0
        assert "orphan" in data["recommendation"].lower()


# ── MCP tool registration & dispatch tests ─────────────────────────────────


class TestMCPTools:
    """Tests for MCP tool registration and dispatch mapping."""

    def test_backend_status_tool_registered(self):
        names = {t.name for t in all_tools()}
        assert "ohm_backend_status" in names

    def test_storage_efficiency_tool_registered(self):
        names = {t.name for t in all_tools()}
        assert "ohm_storage_efficiency" in names

    def test_backend_status_not_write_tool(self):
        assert "ohm_backend_status" not in WRITE_TOOLS

    def test_storage_efficiency_not_write_tool(self):
        assert "ohm_storage_efficiency" not in WRITE_TOOLS

    def test_dispatch_backend_status(self):
        method, path, body = build_request("ohm_backend_status", {}, "test-agent")
        assert method == "GET"
        assert path == "/backend/status"
        assert body is None

    def test_dispatch_storage_efficiency(self):
        method, path, body = build_request("ohm_storage_efficiency", {}, "test-agent")
        assert method == "GET"
        assert path == "/storage/efficiency"
        assert body is None

    def test_backend_status_tool_has_format_param(self):
        tool = next(t for t in all_tools() if t.name == "ohm_backend_status")
        assert "format" in tool.inputSchema["properties"]

    def test_storage_efficiency_tool_has_format_param(self):
        tool = next(t for t in all_tools() if t.name == "ohm_storage_efficiency")
        assert "format" in tool.inputSchema["properties"]
