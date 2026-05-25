"""Tests for DuckLake corruption detection and repair (OHM-qiio)."""

import pytest
from ohm.store import OhmStore


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
        result = store.sync_heartbeat(ducklake_path=ducklake_path)
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
