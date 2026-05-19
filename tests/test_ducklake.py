"""Tests for DuckLake shared backend sync (OHM-xgm.1)."""

import pytest

from ohm.store import OhmStore


class TestDuckLakeSync:
    """Tests for DuckLake push/pull sync."""

    def test_sync_heartbeat_no_ducklake(self, tmp_path):
        """sync_heartbeat with no DuckLake path is a no-op."""
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        result = store.sync_heartbeat(ducklake_path=None)
        assert result["pushed"] == 0
        assert result["pulled"] == 0
        assert result["agent"] == "test_agent"
        assert result["last_sync"] is not None
        store.close()

    def test_sync_heartbeat_with_ducklake(self, tmp_path):
        """sync_heartbeat pushes and pulls with DuckLake."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake with schema
        ducklake_store = OhmStore(db_path=ducklake_path, agent_name="ducklake")
        ducklake_store.close()

        # Create local store and write some data
        store = OhmStore(db_path=local_path, agent_name="agent_a")
        store.write_node("node_1", "Test Node", "concept")
        store.write_node("node_2", "Another Node", "concept")

        # Sync to DuckLake
        result = store.sync_heartbeat(ducklake_path=ducklake_path)
        assert result["pushed"] >= 2  # At least the two nodes
        assert result["agent"] == "agent_a"
        store.close()

    def test_push_pull_roundtrip(self, tmp_path):
        """Agent A pushes, Agent B pulls — changes propagate."""
        local_a_path = str(tmp_path / "local_a.duckdb")
        local_b_path = str(tmp_path / "local_b.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        ducklake_store = OhmStore(db_path=ducklake_path, agent_name="ducklake")
        ducklake_store.close()

        # Agent A creates data and pushes
        store_a = OhmStore(db_path=local_a_path, agent_name="agent_a")
        store_a.write_node("shared_node", "Shared Knowledge", "concept")
        result_a = store_a.sync_heartbeat(ducklake_path=ducklake_path)
        assert result_a["pushed"] >= 1
        store_a.close()

        # Agent B pulls from DuckLake
        store_b = OhmStore(db_path=local_b_path, agent_name="agent_b")
        result_b = store_b.sync_heartbeat(ducklake_path=ducklake_path)
        assert result_b["pulled"] >= 1
        store_b.close()

    def test_sync_idempotent(self, tmp_path):
        """Syncing twice without new changes should push/pull zero."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        ducklake_store = OhmStore(db_path=ducklake_path, agent_name="ducklake")
        ducklake_store.close()

        store = OhmStore(db_path=local_path, agent_name="agent_a")
        store.write_node("node_x", "Node X", "concept")

        # First sync
        result1 = store.sync_heartbeat(ducklake_path=ducklake_path)
        assert result1["pushed"] >= 1

        # Second sync — no new changes
        result2 = store.sync_heartbeat(ducklake_path=ducklake_path)
        assert result2["pushed"] == 0
        store.close()

    def test_last_sync_updated(self, tmp_path):
        """sync_heartbeat updates last_sync timestamp."""
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        # Get initial state
        initial = store.get_agent_state("test_agent")
        assert initial is None or initial.get("last_sync") is None

        # Sync
        result = store.sync_heartbeat(ducklake_path=None)
        assert result["last_sync"] is not None

        # Verify state updated
        state = store.get_agent_state("test_agent")
        assert state is not None
        assert state["last_sync"] is not None
        store.close()

    def test_push_to_ducklake_preserves_data(self, tmp_path):
        """Pushed data should be readable from DuckLake."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        ducklake_store = OhmStore(db_path=ducklake_path, agent_name="ducklake")
        ducklake_store.close()

        store = OhmStore(db_path=local_path, agent_name="agent_a")
        store.write_node("preserved_node", "Preserved", "concept")
        store.sync_heartbeat(ducklake_path=ducklake_path)

        # Read from DuckLake directly
        import duckdb
        dl = duckdb.connect(ducklake_path, read_only=True)
        try:
            changes = dl.execute(
                "SELECT COUNT(*) FROM ohm_change_feed WHERE agent_name = 'agent_a'"
            ).fetchone()
            assert changes[0] >= 1
        finally:
            dl.close()
        store.close()

    def test_env_var_ducklake_path(self, tmp_path, monkeypatch):
        """OHM_DUCKLAKE_PATH env var is used when no path provided."""
        local_path = str(tmp_path / "local.duckdb")
        ducklake_path = str(tmp_path / "ducklake.duckdb")

        # Initialize DuckLake
        ducklake_store = OhmStore(db_path=ducklake_path, agent_name="ducklake")
        ducklake_store.close()

        monkeypatch.setenv("OHM_DUCKLAKE_PATH", ducklake_path)

        store = OhmStore(db_path=local_path, agent_name="agent_a")
        store.write_node("env_node", "Env Node", "concept")
        result = store.sync_heartbeat()  # Uses env var
        assert result["pushed"] >= 1
        store.close()


class TestDuckLakeExtension:
    """Tests for DuckLake extension loading and catalog setup (OHM-kdk.1)."""

    def test_ducklake_extension_loads(self, tmp_path):
        """DuckLake extension loads without error."""
        import duckdb
        from ohm.db import _load_extensions

        conn = duckdb.connect(str(tmp_path / "test.duckdb"))
        _load_extensions(conn)

        # Verify DuckLake extension is loaded
        result = conn.execute(
            "SELECT extension_name FROM duckdb_extensions() "
            "WHERE loaded = true AND extension_name = 'ducklake'"
        ).fetchone()
        conn.close()

        # DuckLake extension should be available in DuckDB 1.5+
        assert result is not None, "DuckLake extension should load"

    def test_attach_ducklake_creates_catalog(self, tmp_path):
        """attach_ducklake creates a DuckLake catalog with mirror tables."""
        import duckdb
        from ohm.db import connect, attach_ducklake

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        result = attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)
        assert result is True, "DuckLake should attach successfully"

        # Verify mirror tables exist in the ohm_lake schema
        tables = conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'ohm_lake'"
        ).fetchall()
        table_names = {t[0] for t in tables}

        assert "ohm_nodes" in table_names, "ohm_nodes mirror table should exist"
        assert "ohm_edges" in table_names, "ohm_edges mirror table should exist"
        assert "ohm_observations" in table_names, "ohm_observations mirror table should exist"
        assert "ohm_change_feed" in table_names, "ohm_change_feed mirror table should exist"

        conn.close()

    def test_attach_ducklake_no_pks(self, tmp_path):
        """DuckLake mirror tables have no PRIMARY KEY constraints."""
        import duckdb
        from ohm.db import connect, attach_ducklake

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)

        # Verify ohm_nodes has no PRIMARY KEY (DuckLake constraint)
        # DuckDB stores constraint info in duckdb_constraints()
        constraints = conn.execute(
            "SELECT constraint_type, table_name FROM duckdb_constraints() "
            "WHERE database_name = 'ohm_lake'"
        ).fetchall()

        pk_constraints = [c for c in constraints if c[0] == "PRIMARY KEY"]
        assert len(pk_constraints) == 0, (
            f"DuckLake tables should have no PRIMARY KEY, found: {pk_constraints}"
        )

        conn.close()

    def test_attach_ducklake_varchar_columns(self, tmp_path):
        """DuckLake mirror tables use VARCHAR for all columns."""
        import duckdb
        from ohm.db import connect, attach_ducklake

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)

        # Check ohm_nodes columns are all VARCHAR (except id which is VARCHAR too)
        columns = conn.execute(
            "SELECT column_name, data_type FROM duckdb_columns() "
            "WHERE database_name = 'ohm_lake' AND table_name = 'ohm_nodes'"
        ).fetchall()

        for col_name, col_type in columns:
            assert col_type == "VARCHAR", (
                f"Column {col_name} should be VARCHAR, got {col_type}"
            )

        conn.close()

    def test_attach_ducklake_idempotent(self, tmp_path):
        """Attaching DuckLake twice does not error (idempotent)."""
        import duckdb
        from ohm.db import connect, attach_ducklake

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        result1 = attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)
        assert result1 is True

        # Second attach should succeed (already attached)
        result2 = attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)
        assert result2 is True

        conn.close()

    def test_store_attach_ducklake_method(self, tmp_path):
        """OhmStore.attach_ducklake() works with catalog_path argument."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        result = store.attach_ducklake(catalog_path=catalog_path, data_path=data_path)
        assert result is True, "OhmStore.attach_ducklake should succeed"

        # Verify tables exist
        tables = store.conn.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'ohm_lake'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "ohm_nodes" in table_names

        store.close()

    def test_store_attach_ducklake_env_var(self, tmp_path, monkeypatch):
        """OhmStore.attach_ducklake() uses OHM_DUCKLAKE_PATH env var."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")
        monkeypatch.setenv("OHM_DUCKLAKE_PATH", catalog_path)

        result = store.attach_ducklake(data_path=data_path)
        assert result is True

        store.close()

    def test_store_attach_ducklake_no_path_returns_false(self, tmp_path):
        """OhmStore.attach_ducklake() returns False when no path configured."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")

        result = store.attach_ducklake()
        assert result is False, "Should return False when no DuckLake path configured"

        store.close()

    def test_ducklake_write_to_mirror_table(self, tmp_path):
        """Can write to DuckLake mirror tables after attachment."""
        import duckdb
        from ohm.db import connect, attach_ducklake

        db_path = str(tmp_path / "test.duckdb")
        conn = connect(db_path)

        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")

        attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)

        # Insert a row into the DuckLake mirror table
        conn.execute(
            "INSERT INTO ohm_lake.ohm_nodes "
            "(id, label, type, created_by, created_at) "
            "VALUES ('test-1', 'Test Node', 'concept', 'agent_a', '2026-01-01T00:00:00')"
        )

        # Read it back
        result = conn.execute(
            "SELECT id, label, type FROM ohm_lake.ohm_nodes WHERE id = 'test-1'"
        ).fetchone()

        assert result is not None
        assert result[0] == "test-1"
        assert result[1] == "Test Node"
        assert result[2] == "concept"

        conn.close()

    def test_ducklake_config_in_default_config(self):
        """DEFAULT_CONFIG includes ducklake configuration section."""
        from ohm.server import DEFAULT_CONFIG

        assert "ducklake" in DEFAULT_CONFIG
        assert "path" in DEFAULT_CONFIG["ducklake"]
        assert "data_path" in DEFAULT_CONFIG["ducklake"]
        assert "sync_interval_seconds" in DEFAULT_CONFIG["ducklake"]
        assert DEFAULT_CONFIG["ducklake"]["sync_interval_seconds"] == 60

    def test_load_config_ducklake_env_vars(self, tmp_path, monkeypatch):
        """load_config reads DuckLake env vars."""
        from ohm.server import load_config

        monkeypatch.setenv("OHM_DUCKLAKE_PATH", "/tmp/test_lake.ducklake")
        monkeypatch.setenv("OHM_DUCKLAKE_DATA", "/tmp/test_lake_data")

        config = load_config(config_path=str(tmp_path / "nonexistent.json"))

        assert config["ducklake"]["path"] == "/tmp/test_lake.ducklake"
        assert config["ducklake"]["data_path"] == "/tmp/test_lake_data"


class TestDuckLakeTimeTravel:
    """Tests for DuckLake time-travel store methods (OHM-kdk.3)."""

    def test_list_snapshots_without_ducklake(self, tmp_path):
        """list_snapshots returns empty list when DuckLake is not attached."""
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        result = store.list_snapshots()
        assert result == []
        store.close()

    def test_graph_at_version_without_ducklake_raises(self, tmp_path):
        """graph_at_version raises OHMError when DuckLake is not attached."""
        from ohm.exceptions import OHMError
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        with pytest.raises(OHMError, match="DuckLake is not attached"):
            store.graph_at_version(1)
        store.close()

    def test_graph_changes_without_ducklake_raises(self, tmp_path):
        """graph_changes raises OHMError when DuckLake is not attached."""
        from ohm.exceptions import OHMError
        db_path = str(tmp_path / "local.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        with pytest.raises(OHMError, match="DuckLake is not attached"):
            store.graph_changes(1, 2)
        store.close()

    def test_list_snapshots_with_ducklake(self, tmp_path):
        """list_snapshots returns snapshots when DuckLake is attached."""
        from ohm.db import connect, attach_ducklake
        db_path = str(tmp_path / "local.duckdb")
        conn = connect(db_path)
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")
        attached = attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)
        if not attached:
            pytest.skip("DuckLake extension not available")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        store.attach_ducklake(catalog_path=catalog_path, data_path=data_path)
        store.conn.execute(
            "INSERT INTO ohm_lake.ohm_nodes (id, label, type, created_by, created_at, updated_at) "
            "VALUES ('n1', 'Test', 'concept', 'test', '2026-01-01', '2026-01-01')"
        )
        snapshots = store.list_snapshots()
        assert isinstance(snapshots, list)
        assert len(snapshots) >= 1
        assert "snapshot_id" in snapshots[0]
        store.close()

    def test_graph_at_version_with_ducklake(self, tmp_path):
        """graph_at_version returns graph state at a specific snapshot."""
        from ohm.db import connect, attach_ducklake
        db_path = str(tmp_path / "local.duckdb")
        conn = connect(db_path)
        catalog_path = str(tmp_path / "ohm_lake.ducklake")
        data_path = str(tmp_path / "ohm_lake_data")
        attached = attach_ducklake(conn, catalog_path=catalog_path, data_path=data_path)
        if not attached:
            pytest.skip("DuckLake extension not available")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        store.attach_ducklake(catalog_path=catalog_path, data_path=data_path)
        store.conn.execute(
            "INSERT INTO ohm_lake.ohm_nodes (id, label, type, created_by, created_at, updated_at) "
            "VALUES ('n1', 'Test', 'concept', 'test', '2026-01-01', '2026-01-01')"
        )
        snapshots = store.list_snapshots()
        latest_version = snapshots[-1]["snapshot_id"]
        result = store.graph_at_version(latest_version)
        assert result["version"] == latest_version
        assert result["node_count"] >= 1
        assert len(result["nodes"]) >= 1
        assert result["edge_count"] >= 0
        store.close()
