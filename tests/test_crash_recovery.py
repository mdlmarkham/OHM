"""Tests for crash recovery divergence fix (OHM-822).

Verifies that soft-deleted nodes are not resurrected after crash recovery
via DuckLake. Tests three fixes:

1. Incremental sync propagates soft-deletes to DuckLake mirror
   (``_incremental_sync_table`` in store.py)
2. Rebuild paths filter ``WHERE deleted_at IS NULL`` from mirror
   (``_recover_from_ducklake`` and ``_auto_restore_if_empty`` in store.py,
    ``rebuild_from_ducklake.py``, ``_try_ducklake_recovery`` in db.py)
3. ``node_cols``/``edge_cols`` bug in ``_try_ducklake_recovery`` (db.py)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from ohm.store import OhmStore

pytestmark = pytest.mark.integration


def _attach_or_skip(store: OhmStore, catalog_path: str, data_path: str) -> None:
    """Attach DuckLake to *store* or skip the test if unavailable."""
    if not store.attach_ducklake(catalog_path=catalog_path, data_path=data_path):
        store.close()
        pytest.skip("DuckLake extension not available in this environment")


# ── Fix 1: incremental sync propagates soft-deletes ─────────────────────


class TestIncrementalSyncSoftDeletePropagation:
    """Fix 1 — ``_incremental_sync_table`` must propagate soft-deletes to the mirror."""

    def test_incremental_sync_removes_soft_deleted_from_mirror(self, tmp_path):
        """A soft-deleted node is removed from the DuckLake mirror on the next sync."""
        store = OhmStore(db_path=str(tmp_path / "local.duckdb"), agent_name="test_agent")
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")
        _attach_or_skip(store, catalog_path, data_path)

        # Create two nodes and do an initial sync.
        store.write_node("ghost_node", "Ghost", "concept")
        store.write_node("alive_node", "Alive", "concept")
        store.sync_to_ducklake(alias="ohm_lake")

        mirror_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id IN ('ghost_node', 'alive_node')"
        ).fetchone()[0]
        assert mirror_count == 2

        # Set last-push so the next sync uses the incremental path.
        store._set_last_push_timestamp(datetime.now(timezone.utc) - timedelta(seconds=5))

        # Soft-delete ghost_node (bumps updated_at, sets deleted_at).
        store.delete_node("ghost_node", "test_agent")
        assert store.get_node("ghost_node") is None
        assert store.get_node("alive_node") is not None

        # Incremental sync should propagate the tombstone.
        store.sync_to_ducklake(alias="ohm_lake")

        ghost_in_mirror = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'ghost_node'"
        ).fetchone()[0]
        assert ghost_in_mirror == 0, "Soft-deleted node must be removed from mirror"

        alive_in_mirror = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'alive_node'"
        ).fetchone()[0]
        assert alive_in_mirror == 1, "Active node must remain in mirror"

        store.close()

    def test_incremental_sync_removes_soft_deleted_edges_from_mirror(self, tmp_path):
        """Soft-deleted edges are also removed from the mirror on sync."""
        store = OhmStore(db_path=str(tmp_path / "local.duckdb"), agent_name="test_agent")
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")
        _attach_or_skip(store, catalog_path, data_path)

        store.write_node("node_a", "A", "concept")
        store.write_node("node_b", "B", "concept")
        store.write_edge("node_a", "node_b", "CAUSES", "L3")
        store.sync_to_ducklake(alias="ohm_lake")

        edge_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_edges WHERE from_node = 'node_a'"
        ).fetchone()[0]
        assert edge_count >= 1

        store._set_last_push_timestamp(datetime.now(timezone.utc) - timedelta(seconds=5))

        # Deleting node_a cascades to its edges.
        store.delete_node("node_a", "test_agent")
        store.sync_to_ducklake(alias="ohm_lake")

        node_in_mirror = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'node_a'"
        ).fetchone()[0]
        assert node_in_mirror == 0

        edge_in_mirror = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_edges WHERE from_node = 'node_a'"
        ).fetchone()[0]
        assert edge_in_mirror == 0

        store.close()


# ── Fix 2: rebuild paths filter WHERE deleted_at IS NULL ──────────────


class TestRebuildFiltersSoftDeleted:
    """Fix 2 — rebuild / auto-restore paths must not resurrect soft-deleted rows."""

    def test_auto_restore_filters_soft_deleted(self, tmp_path, monkeypatch):
        """``_auto_restore_if_empty`` does not pull rows with ``deleted_at`` set."""
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        store_a = OhmStore(db_path=str(tmp_path / "store_a.duckdb"), agent_name="agent_a")
        _attach_or_skip(store_a, catalog_path, data_path)

        store_a.write_node("active_node", "Active", "concept")
        store_a.write_node("dead_node", "Dead", "concept")
        store_a.sync_to_ducklake(alias="ohm_lake")

        # Simulate a mirror that correctly recorded the soft-delete
        # (deleted_at set on the mirror row).
        store_a.conn.execute(
            "UPDATE ohm_lake.ohm_nodes SET deleted_at = ? WHERE id = ?",
            ["2026-01-01T00:00:00", "dead_node"],
        )

        # Sanity: mirror has dead_node with deleted_at set.
        dead_mirror = store_a.conn.execute(
            "SELECT deleted_at FROM ohm_lake.ohm_nodes WHERE id = 'dead_node'"
        ).fetchone()
        assert dead_mirror is not None and dead_mirror[0] is not None
        store_a.close()

        # store_b starts with an empty DB and auto-restores from DuckLake.
        monkeypatch.setenv("OHM_DUCKLAKE_PATH", catalog_path)
        store_b = OhmStore(db_path=str(tmp_path / "store_b.duckdb"), agent_name="agent_b")

        assert store_b.get_node("active_node") is not None, "Active node should be restored"
        assert store_b.get_node("dead_node") is None, "Soft-deleted node must not be restored"

        store_b.close()

    def test_rebuild_insert_select_filters_soft_deleted(self, tmp_path):
        """The INSERT...SELECT FROM mirror with WHERE deleted_at IS NULL works."""
        import duckdb

        from ohm.schema import initialize_schema

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        store = OhmStore(db_path=str(tmp_path / "setup.duckdb"), agent_name="setup")
        _attach_or_skip(store, catalog_path, data_path)

        store.write_node("keep_node", "Keep", "concept")
        store.write_node("drop_node", "Drop", "concept")
        store.sync_to_ducklake(alias="ohm_lake")

        store.conn.execute(
            "UPDATE ohm_lake.ohm_nodes SET deleted_at = ? WHERE id = ?",
            ["2026-01-01T00:00:00", "drop_node"],
        )
        store.close()

        conn = duckdb.connect(str(tmp_path / "rebuild.duckdb"))
        initialize_schema(conn)
        conn.execute("INSTALL ducklake FROM core")
        conn.execute("LOAD ducklake")
        conn.execute(f"ATTACH 'ducklake:{catalog_path}' AS ohm_lake")

        local_cols = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'ohm_nodes' ORDER BY ordinal_position"
        ).fetchall()
        local_col_names = list(dict.fromkeys(c[0] for c in local_cols))

        mirror_cols = conn.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE database_name = 'ohm_lake' AND table_name = 'ohm_nodes'"
        ).fetchall()
        mirror_col_names = [c[0] for c in mirror_cols]

        common = [c for c in local_col_names if c in mirror_col_names and c != "deleted_at"]
        common_str = ", ".join(common)

        conn.execute(f"""
            INSERT INTO ohm_nodes ({common_str}, deleted_at)
            SELECT {common_str}, NULL::TIMESTAMP FROM ohm_lake.ohm_nodes
            WHERE deleted_at IS NULL
        """)

        keep = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = 'keep_node' AND deleted_at IS NULL"
        ).fetchone()
        drop = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = 'drop_node' AND deleted_at IS NULL"
        ).fetchone()

        assert keep is not None
        assert drop is None

        conn.close()


# ── Fix 3: node_cols/edge_cols bug + WHERE deleted_at IS NULL in db.py ─


class TestTryDucklakeRecoveryFix:
    """Fix 3 — ``_try_ducklake_recovery`` captures columns correctly and filters soft-deleted."""

    def test_recovery_filters_soft_deleted_nodes_and_edges(self, tmp_path, monkeypatch):
        """Soft-deleted rows are excluded from the recovered DB."""
        from ohm.graph.db import _try_ducklake_recovery

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        store = OhmStore(db_path=str(tmp_path / "setup.duckdb"), agent_name="setup")
        _attach_or_skip(store, catalog_path, data_path)

        store.write_node("survivor", "Survivor", "concept", content="Survives")
        store.write_node("casualty", "Casualty", "concept")
        store.write_edge("survivor", "casualty", "CAUSES", "L3")
        store.sync_to_ducklake(alias="ohm_lake")

        # Mark casualty and its edge as soft-deleted in the mirror.
        store.conn.execute(
            "UPDATE ohm_lake.ohm_nodes SET deleted_at = ? WHERE id = ?",
            ["2026-01-01T00:00:00", "casualty"],
        )
        store.conn.execute(
            "UPDATE ohm_lake.ohm_edges SET deleted_at = ? WHERE from_node = ?",
            ["2026-01-01T00:00:00", "survivor"],
        )
        store.close()

        monkeypatch.setenv("OHM_DUCKLAKE_PATH", catalog_path)

        corrupted_db = str(tmp_path / "corrupted.duckdb")
        with open(corrupted_db, "w") as f:
            f.write("corrupted")

        result = _try_ducklake_recovery(corrupted_db)
        if not result:
            pytest.skip("DuckLake snapshots not available in this environment")

        import duckdb

        from ohm.schema import initialize_schema

        conn = duckdb.connect(corrupted_db)
        initialize_schema(conn)

        survivor = conn.execute(
            "SELECT id, label FROM ohm_nodes WHERE id = 'survivor' AND deleted_at IS NULL"
        ).fetchone()
        casualty = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = 'casualty' AND deleted_at IS NULL"
        ).fetchone()

        assert survivor is not None, "Active node must be recovered"
        assert survivor[1] == "Survivor"
        assert casualty is None, "Soft-deleted node must NOT be recovered"

        edge = conn.execute(
            "SELECT id FROM ohm_edges WHERE from_node = 'survivor' AND deleted_at IS NULL"
        ).fetchone()
        assert edge is None, "Soft-deleted edge must NOT be recovered"

        conn.close()

    def test_recovery_uses_correct_column_names(self, tmp_path, monkeypatch):
        """node_cols and edge_cols are captured from their respective queries."""
        from ohm.graph.db import _try_ducklake_recovery

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        store = OhmStore(db_path=str(tmp_path / "setup.duckdb"), agent_name="setup")
        _attach_or_skip(store, catalog_path, data_path)

        store.write_node("n1", "Node One", "concept", content="Content here")
        store.write_node("n2", "Node Two", "concept")
        store.write_edge("n1", "n2", "SUPPORTS", "L3")
        store.sync_to_ducklake(alias="ohm_lake")
        store.close()

        monkeypatch.setenv("OHM_DUCKLAKE_PATH", catalog_path)

        corrupted_db = str(tmp_path / "corrupted2.duckdb")
        with open(corrupted_db, "w") as f:
            f.write("corrupted")

        result = _try_ducklake_recovery(corrupted_db)
        if not result:
            pytest.skip("DuckLake snapshots not available in this environment")

        import duckdb

        from ohm.schema import initialize_schema

        conn = duckdb.connect(corrupted_db)
        initialize_schema(conn)

        # If node_cols were wrongly set from edges' description, the node
        # insert would use edge column names (from_node, to_node, edge_type)
        # and the label/content would be missing or misplaced.
        node = conn.execute(
            "SELECT id, label, type, content FROM ohm_nodes WHERE id = 'n1' AND deleted_at IS NULL"
        ).fetchone()
        assert node is not None
        assert node[0] == "n1"
        assert node[1] == "Node One"
        assert node[2] == "concept"
        assert node[3] == "Content here"

        edge = conn.execute(
            "SELECT from_node, to_node, edge_type FROM ohm_edges WHERE deleted_at IS NULL"
        ).fetchone()
        assert edge is not None
        assert edge[0] == "n1"
        assert edge[1] == "n2"
        assert edge[2] == "SUPPORTS"

        conn.close()


# ── Full cycle: sync → soft-delete → sync → rebuild ────────────────────


class TestConsistencyInvariant:
    """End-to-end: a soft-deleted node must not reappear after rebuild."""

    def test_no_resurrection_after_sync_and_rebuild(self, tmp_path, monkeypatch):
        """A soft-deleted node stays dead through the sync + rebuild cycle."""
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        store_a = OhmStore(db_path=str(tmp_path / "store_a.duckdb"), agent_name="agent_a")
        _attach_or_skip(store_a, catalog_path, data_path)

        store_a.write_node("anchor", "Anchor", "concept")
        store_a.write_node("ghost", "Ghost", "concept")
        store_a.write_edge("anchor", "ghost", "CAUSES", "L3")
        store_a.sync_to_ducklake(alias="ohm_lake")

        # Soft-delete ghost and propagate to mirror.
        store_a._set_last_push_timestamp(datetime.now(timezone.utc) - timedelta(seconds=5))
        store_a.delete_node("ghost", "agent_a")
        store_a.sync_to_ducklake(alias="ohm_lake")

        ghost_mirror = store_a.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'ghost'"
        ).fetchone()[0]
        assert ghost_mirror == 0, "Ghost must be gone from mirror after sync"
        store_a.close()

        # Rebuild from DuckLake.
        monkeypatch.setenv("OHM_DUCKLAKE_PATH", catalog_path)
        store_b = OhmStore(db_path=str(tmp_path / "store_b.duckdb"), agent_name="agent_b")

        assert store_b.get_node("anchor") is not None, "Anchor should be restored"
        assert store_b.get_node("ghost") is None, "Ghost must not be resurrected"

        edges_to_ghost = store_b.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE to_node = 'ghost' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert edges_to_ghost == 0, "No active edges should reference the ghost"

        store_b.close()
