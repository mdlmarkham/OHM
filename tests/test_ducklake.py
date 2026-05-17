"""Tests for DuckLake shared backend sync (OHM-xgm.1)."""



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
