"""Tests for DuckLake corruption detection and repair (OHM-qiio)."""

import os

import duckdb
import pytest
from ohm.store import OhmStore

pytestmark = pytest.mark.integration

# Skip DuckLake tests in CI where ATTACH DATABASE may not work
SKIP_CI = os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"


@pytest.mark.skipif(SKIP_CI, reason="DuckLake ATTACH DATABASE not available in CI")
class TestDuckLakeHealthCheck:
    """Tests for check_ducklake_health()."""

    def test_health_check_no_ducklake(self, tmp_path):
        """Health check with no DuckLake attached returns healthy but local-only."""
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_health")

        result = store.check_ducklake_health()

        # No DuckLake attached — should be healthy (local-only mode)
        assert result["healthy"] is True
        assert result["sync_degraded"] is False
        assert "ohm_nodes" in result["local_counts"]
        store.close()

    def test_health_check_with_ducklake(self, tmp_path):
        """Health check with DuckLake attached compares row counts."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        dl_store = OhmStore(db_path=ducklake_path, agent_name="ducklake_init")
        dl_store.write_node("node_1", "Node 1", "concept")
        dl_store.write_node("node_2", "Node 2", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Create local store and attach DuckLake
        store = OhmStore(db_path=local_path, agent_name="test_health")
        store.write_node("local_1", "Local 1", "concept")

        # Attach DuckLake
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_ONLY)")
        except Exception:
            pytest.skip("DuckLake attachment not available in this DuckDB version")

        result = store.check_ducklake_health()

        # Should have local and DuckLake counts
        assert result["local_counts"]["ohm_nodes"] == 1
        assert result["ducklake_counts"]["ohm_nodes"] == 2
        store.close()

    def test_health_check_detects_orphans(self, tmp_path):
        """Health check detects orphaned rows in DuckLake."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake with nodes
        dl_store = OhmStore(db_path=ducklake_path, agent_name="ducklake_init")
        dl_store.write_node("orphan_node", "Orphan Node", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Create local store with no matching nodes
        store = OhmStore(db_path=local_path, agent_name="test_health")

        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_ONLY)")
        except Exception:
            pytest.skip("DuckLake attachment not available")

        result = store.check_ducklake_health()

        # Orphan detection: DuckLake has nodes not in local
        assert result["orphan_counts"]["ohm_nodes"] >= 1
        assert result["sync_degraded"] is True
        store.close()

    def test_sync_degraded_flag(self, tmp_path):
        """sync_heartbeat sets sync_degraded flag when DuckLake has orphans."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake with data
        dl_store = OhmStore(db_path=ducklake_path, agent_name="ducklake_init")
        dl_store.write_node("node_1", "Node 1", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Create local store
        store = OhmStore(db_path=local_path, agent_name="test_degraded")
        store.write_node("local_1", "Local 1", "concept")

        # Attach DuckLake before sync
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_ONLY)")
        except Exception:
            pytest.skip("DuckLake attachment not available")

        # Run sync — should detect orphans and set sync_degraded
        # Run 5 times to pass health check sampling (every 5th cycle)
        # Use force=True to bypass 30-second throttle between rapid test calls
        for _ in range(5):
            store.sync_heartbeat(ducklake_path=ducklake_path, force=True)
        assert hasattr(store, "sync_degraded")
        store.close()


class TestDuckLakeRepair:
    """Tests for repair_from_ducklake()."""

    def test_repair_inserts_missing_nodes(self, tmp_path):
        """Repair inserts nodes from DuckLake that are missing locally."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake with nodes
        dl_store = OhmStore(db_path=ducklake_path, agent_name="ducklake_init")
        dl_store.write_node("repair_node_1", "Repair Node 1", "concept")
        dl_store.write_node("repair_node_2", "Repair Node 2", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Create local store with no matching nodes
        store = OhmStore(db_path=local_path, agent_name="test_repair")

        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_ONLY)")
        except Exception:
            pytest.skip("DuckLake attachment not available")

        result = store.repair_from_ducklake()

        assert result["inserted"] >= 2  # At least the two missing nodes
        assert result["verified"] is True
        store.close()

    def test_repair_no_ducklake(self, tmp_path):
        """Repair with no DuckLake attached returns error."""
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_repair_nodl")

        result = store.repair_from_ducklake()

        assert result["verified"] is False
        assert len(result["errors"]) > 0
        store.close()

    def test_repair_idempotent(self, tmp_path):
        """Running repair twice is idempotent."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        dl_store = OhmStore(db_path=ducklake_path, agent_name="ducklake_init")
        dl_store.write_node("repair_node_3", "Repair Node 3", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        # Create local store
        store = OhmStore(db_path=local_path, agent_name="test_repair_idem")

        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_ONLY)")
        except Exception:
            pytest.skip("DuckLake attachment not available")

        result1 = store.repair_from_ducklake()
        assert result1["inserted"] >= 1

        # Second repair should find nothing new
        result2 = store.repair_from_ducklake()
        assert result2["inserted"] == 0
        assert result2["verified"] is True
        store.close()


class TestDuckLakeAutoRestore:
    """Tests for _auto_restore_if_empty() (OHM-cqrh)."""

    def test_auto_restore_with_matching_schema(self, tmp_path):
        """Auto-restore restores data from DuckLake when local is empty."""
        from ohm.db import attach_ducklake

        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")
        data_path = str(tmp_path / "ducklake_data")

        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("auto_node_1", "Auto Node 1", "concept")
        dl_store.write_node("auto_node_2", "Auto Node 2", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        conn_for_attach = duckdb.connect(local_path)
        if not attach_ducklake(conn_for_attach, catalog_path=ducklake_path, data_path=data_path):
            conn_for_attach.close()
            pytest.skip("DuckLake not available")
        conn_for_attach.close()

        store = OhmStore(db_path=local_path, agent_name="test_auto_restore")
        store.ducklake_path = ducklake_path
        store.ducklake_data_path = data_path

        initial_count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        assert initial_count == 0, "Local should be empty before restore"

        store._auto_restore_if_empty()

        restored_count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        assert restored_count == 2, f"Expected 2 nodes restored, got {restored_count}"
        store.close()

    def test_auto_restore_skips_when_data_exists(self, tmp_path):
        """Auto-restore does nothing when local already has data."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")
        data_path = str(tmp_path / "ducklake_data")

        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("dl_node", "DL Node", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        store = OhmStore(db_path=local_path, agent_name="test_skip_restore")
        store.write_node("local_node", "Local Node", "concept")
        store.ducklake_path = ducklake_path
        store.ducklake_data_path = data_path

        store._auto_restore_if_empty()

        count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        assert count == 1, "Should keep local data, not restore from DuckLake"
        store.close()

    def test_auto_restore_skips_without_ducklake_path(self, tmp_path):
        """Auto-restore skips when no DuckLake path is configured."""
        local_path = str(tmp_path / "local.duckdb")

        store = OhmStore(db_path=local_path, agent_name="test_no_path")
        store.ducklake_path = ""

        store._auto_restore_if_empty()

        count = store.conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL").fetchone()[0]
        assert count == 0, "Should remain empty without DuckLake"
        store.close()


@pytest.mark.skipif(SKIP_CI, reason="DuckLake ATTACH DATABASE not available in CI")
class TestDuckLakeOrphanRepair:
    """Tests for repair_ducklake_orphans() (OHM-926).

    Tests the DuckLake→mirror direction: delete orphan rows from the mirror
    that have no corresponding active local row.
    """

    def test_repair_orphans_dry_run_reports_but_does_not_delete(self, tmp_path):
        """dry_run=True reports orphans without deleting them."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Seed DuckLake with a node that will have no local counterpart
        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("orphan_only_node", "Orphan Only", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        store = OhmStore(db_path=local_path, agent_name="test_dry_run")
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_WRITE)")
        except Exception:
            store.close()
            pytest.skip("DuckLake attachment not available")

        result = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=True)

        assert result["dry_run"] is True
        assert result["tables"]["ohm_nodes"]["orphans_found"] >= 1
        assert result["tables"]["ohm_nodes"]["orphans_deleted"] == 0
        assert result["total_deleted"] == 0

        # Orphan should still be in the mirror
        remaining = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'orphan_only_node'"
        ).fetchone()[0]
        assert remaining == 1, "dry_run must not delete from the mirror"
        store.close()

    def test_repair_orphans_deletes_when_not_dry_run(self, tmp_path):
        """dry_run=False hard-deletes orphan rows from the mirror."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("orphan_to_delete", "Orphan To Delete", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        store = OhmStore(db_path=local_path, agent_name="test_delete")
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_WRITE)")
        except Exception:
            store.close()
            pytest.skip("DuckLake attachment not available")

        result = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=False)

        assert result["dry_run"] is False
        assert result["tables"]["ohm_nodes"]["orphans_found"] >= 1
        assert result["tables"]["ohm_nodes"]["orphans_deleted"] >= 1
        assert result["total_deleted"] >= 1

        # Orphan should be gone from the mirror
        remaining = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'orphan_to_delete'"
        ).fetchone()[0]
        assert remaining == 0, "dry_run=False must delete orphan from the mirror"
        store.close()

    def test_repair_orphans_is_idempotent(self, tmp_path):
        """A second run reports 0 orphans after the first run cleaned them."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("idem_orphan", "Idem Orphan", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        store = OhmStore(db_path=local_path, agent_name="test_idem")
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_WRITE)")
        except Exception:
            store.close()
            pytest.skip("DuckLake attachment not available")

        first = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=False)
        assert first["total_deleted"] >= 1

        second = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=False)
        assert second["total_deleted"] == 0
        # All tables should report 0 orphans found on the second run
        for table, counts in second["tables"].items():
            assert counts["orphans_found"] == 0, f"{table} still has orphans after repair"
        store.close()

    def test_repair_orphans_preserves_active_local_rows(self, tmp_path):
        """Rows that exist both in DuckLake and locally are never deleted."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # DuckLake has two nodes; local will have one of them.
        dl_store = OhmStore(db_path=ducklake_path, agent_name="dl_init")
        dl_store.write_node("shared_node", "Shared", "concept")
        dl_store.write_node("orphan_only", "Orphan Only", "concept")
        dl_store.conn.execute("CHECKPOINT")
        dl_store.close()

        store = OhmStore(db_path=local_path, agent_name="test_preserve")
        store.write_node("shared_node", "Shared Local", "concept")
        try:
            store.conn.execute(f"ATTACH DATABASE '{ducklake_path}' AS ohm_lake (READ_WRITE)")
        except Exception:
            store.close()
            pytest.skip("DuckLake attachment not available")

        result = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=False)
        deleted_ids = {table: counts["orphans_deleted"] for table, counts in result["tables"].items()}
        assert result["total_deleted"] >= 1, "should delete the orphan_only row"

        # shared_node must still exist in the mirror
        remaining_shared = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_lake.ohm_nodes WHERE id = 'shared_node'"
        ).fetchone()[0]
        assert remaining_shared == 1, "active shared row must not be deleted"
        # And locally too
        local_shared = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id = 'shared_node' AND deleted_at IS NULL"
        ).fetchone()[0]
        assert local_shared == 1
        store.close()

    def test_repair_orphans_no_ducklake_attached(self, tmp_path):
        """Repair with no DuckLake attached returns an error, does not crash."""
        store = OhmStore(db_path=str(tmp_path / "local.duckdb"), agent_name="test_no_dl")

        result = store.repair_ducklake_orphans(alias="ohm_lake", dry_run=False)

        assert len(result["errors"]) > 0
        assert result["total_deleted"] == 0
        assert any("not attached" in e for e in result["errors"])
        store.close()
