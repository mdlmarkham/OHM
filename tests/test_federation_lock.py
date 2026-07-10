"""Test OHM-735: cross-process migration lock for federated mode."""

import tempfile
import os
import time
from pathlib import Path

import duckdb
import pytest

from ohm.tenant import TenantManager
from ohm.graph.schema import initialize_schema_ducklake, get_schema_version, SCHEMA_VERSION


@pytest.fixture
def fed_env():
    """Create a temp directory with a DuckLake catalog for federated mode."""
    tmp = tempfile.mkdtemp()
    catalog = os.path.join(tmp, "test_lake.ducklake")
    tenants_dir = os.path.join(tmp, "tenants")
    os.makedirs(tenants_dir)
    tm = TenantManager(
        tenants_dir=tenants_dir,
        shared_catalog_url=catalog,
    )
    yield tm, catalog, tmp
    # cleanup
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _make_conn(catalog_url, schema_name="acme_corp"):
    """Create a DuckDB connection attached to the shared catalog."""
    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL ducklake FROM core")
    conn.execute("LOAD ducklake")
    conn.execute(f"ATTACH IF NOT EXISTS '{catalog_url}' AS ohm_lake (TYPE ducklake)")
    conn.execute(f"CREATE SCHEMA IF NOT EXISTS ohm_lake.{schema_name}")
    conn.execute(f"SET schema TO ohm_lake.{schema_name}")
    return conn


class TestSchemaLockTable:
    """Test the lock table creation and row management."""

    def test_ensure_lock_table_creates_table(self, fed_env):
        """_ensure_schema_lock_table creates the table in the shared catalog."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)
        tm._ensure_schema_lock_table(conn)
        result = conn.execute("SELECT COUNT(*) FROM ohm_lake.ohm_system.ohm_schema_lock").fetchone()
        assert result[0] == 0
        conn.close()

    def test_ensure_lock_row_inserts_one_row(self, fed_env):
        """_ensure_lock_row inserts exactly one row per schema."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)
        tm._ensure_schema_lock_table(conn)
        tm._ensure_lock_row(conn, "acme_corp")
        tm._ensure_lock_row(conn, "acme_corp")  # idempotent
        result = conn.execute("SELECT COUNT(*) FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert result[0] == 1
        conn.close()

    def test_ensure_lock_row_different_schemas(self, fed_env):
        """Different schemas get different lock rows."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)
        tm._ensure_schema_lock_table(conn)
        tm._ensure_lock_row(conn, "acme_corp")
        tm._ensure_lock_row(conn, "beta_inc")
        result = conn.execute("SELECT schema_name FROM ohm_lake.ohm_system.ohm_schema_lock ORDER BY schema_name").fetchall()
        assert len(result) == 2
        assert result[0][0] == "acme_corp"
        assert result[1][0] == "beta_inc"
        conn.close()


class TestLockAcquireRelease:
    """Test lock acquisition and release semantics."""

    def test_acquire_and_release(self, fed_env):
        """Acquire lock, verify it's held, then release."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)
        acquired = tm._acquire_migration_lock(conn, "acme_corp", timeout=5.0)
        assert acquired is True

        # Verify lock is held by our daemon
        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row[0] is not None
        assert row[0] == tm._daemon_id()

        # Release
        tm._release_migration_lock(conn, "acme_corp")

        # Verify lock is released
        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row[0] is None
        conn.close()

    def test_second_acquire_fails_while_held(self, fed_env):
        """A second acquire attempt fails while the first holds the lock."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)

        # Acquire with real daemon ID
        acquired1 = tm._acquire_migration_lock(conn, "acme_corp", timeout=5.0)
        assert acquired1 is True

        # Manually try to acquire as a different "daemon"
        daemon2 = "99999@other-host"
        conn.execute("BEGIN TRANSACTION")
        conn.execute(
            "UPDATE ohm_lake.ohm_system.ohm_schema_lock "
            "SET locked_by = ?, locked_at = CURRENT_TIMESTAMP, "
            "lock_expires_at = CURRENT_TIMESTAMP + INTERVAL '300 seconds' "
            "WHERE schema_name = 'acme_corp' "
            "AND (locked_by IS NULL OR lock_expires_at < CURRENT_TIMESTAMP)",
            [daemon2],
        )
        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        conn.execute("COMMIT")
        # Second daemon should NOT have acquired — first daemon still holds
        assert row[0] == tm._daemon_id(), "Second daemon should not have acquired the lock"

        tm._release_migration_lock(conn, "acme_corp")
        conn.close()

    def test_release_only_if_holder(self, fed_env):
        """Release only clears the lock if we are the holder."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)

        # Manually set lock to a different daemon
        tm._ensure_schema_lock_table(conn)
        tm._ensure_lock_row(conn, "acme_corp")
        conn.execute("UPDATE ohm_lake.ohm_system.ohm_schema_lock SET locked_by = 'other-daemon', locked_at = CURRENT_TIMESTAMP, lock_expires_at = CURRENT_TIMESTAMP + INTERVAL '300 seconds' WHERE schema_name = 'acme_corp'")

        # Try to release — should NOT clear (we're not the holder)
        tm._release_migration_lock(conn, "acme_corp")
        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row[0] == "other-daemon", "Release should not clear another daemon's lock"
        conn.close()


class TestStaleLockRecovery:
    """Test stale-lock recovery when a daemon crashes mid-migration."""

    def test_stale_lock_can_be_reclaimed(self, fed_env):
        """A daemon can reclaim a lock whose expiry has passed."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)

        # Simulate a crashed daemon: set lock with expired timestamp
        tm._ensure_schema_lock_table(conn)
        tm._ensure_lock_row(conn, "acme_corp")
        conn.execute("UPDATE ohm_lake.ohm_system.ohm_schema_lock SET locked_by = 'crashed-daemon', locked_at = CURRENT_TIMESTAMP - INTERVAL '600 seconds', lock_expires_at = CURRENT_TIMESTAMP - INTERVAL '300 seconds' WHERE schema_name = 'acme_corp'")

        # A new daemon should be able to acquire (stale lock recovery)
        acquired = tm._acquire_migration_lock(conn, "acme_corp", timeout=5.0)
        assert acquired is True

        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row[0] == tm._daemon_id(), "New daemon should have reclaimed the stale lock"

        tm._release_migration_lock(conn, "acme_corp")
        conn.close()


class TestFederatedStoreWithLock:
    """Test that _create_federated_store uses the lock correctly."""

    def test_provision_creates_schema_under_lock(self, fed_env):
        """Provisioning a tenant creates the schema and acquires/releases the lock."""
        tm, catalog, tmp = fed_env
        meta = tm.provision("acme_corp", domain="ohm")
        assert meta["customer_id"] == "acme_corp"

        # Lock should be released after provisioning
        conn = _make_conn(catalog)
        row = conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row is None or row[0] is None, "Lock should be released after provisioning"
        conn.close()

    def test_get_store_acquires_and_releases_lock(self, fed_env):
        """get_store acquires the lock, initializes schema, releases."""
        tm, catalog, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        assert store is not None
        assert store._federated is True

        # Verify schema was initialized (tables exist)
        table_exists = store.conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'ohm_nodes'").fetchone()[0]
        assert table_exists > 0  # Table exists (domain seeding may have added nodes)

        # Lock should be released
        row = store.conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row is None or row[0] is None, "Lock should be released after get_store"
        store.close()

    def test_write_and_read_after_provisioning(self, fed_env):
        """End-to-end: provision, write, read from federated store."""
        tm, catalog, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")

        store.write_node("n1", "Test Node", "concept", agent_name="test")
        node = store.get_node("n1")
        assert node is not None
        assert node["label"] == "Test Node"

        # Verify lock is not held after normal operation
        row = store.conn.execute("SELECT locked_by FROM ohm_lake.ohm_system.ohm_schema_lock WHERE schema_name = 'acme_corp'").fetchone()
        assert row is None or row[0] is None
        store.close()


class TestNonFederatedUnchanged:
    """Verify non-federated mode is completely unaffected."""

    def test_non_federated_uses_file_lock(self):
        """Non-federated mode still uses file-based .migration_lock, not the shared lock."""
        tmp = tempfile.mkdtemp()
        import shutil

        try:
            tenants_dir = os.path.join(tmp, "tenants")
            os.makedirs(tenants_dir)
            tm = TenantManager(tenants_dir=tenants_dir)
            assert tm.federated is False

            # Provision should create a local .duckdb file, no shared lock table
            meta = tm.provision("acme_corp", domain="ohm")
            assert meta["customer_id"] == "acme_corp"

            # No DuckLake catalog should exist
            assert not any(f.endswith(".ducklake") for f in os.listdir(tmp))

            store = tm.get_store("acme_corp")
            assert store._federated is False
            store.write_node("n1", "Test", "concept", agent_name="test")
            node = store.get_node("n1")
            assert node["label"] == "Test"
            store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestWaitForMigration:
    """Test _wait_for_migration when another daemon holds the lock."""

    def test_wait_returns_true_when_version_current(self, fed_env):
        """_wait_for_migration returns True when schema version is already current."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)

        # Initialize schema (version will be current)
        initialize_schema_ducklake(conn)
        result = tm._wait_for_migration(conn, "acme_corp", timeout=2.0)
        assert result is True
        conn.close()

    def test_wait_returns_false_on_timeout(self, fed_env):
        """_wait_for_migration returns False when version stays behind and lock is held."""
        tm, catalog, tmp = fed_env
        conn = _make_conn(catalog)

        # Create the schema but with an old version
        initialize_schema_ducklake(conn)
        conn.execute("UPDATE ohm_meta SET value = '0.1.0' WHERE key = 'schema_version'")

        # Set lock as held by another daemon (with future expiry)
        tm._ensure_schema_lock_table(conn)
        tm._ensure_lock_row(conn, "acme_corp")
        conn.execute("UPDATE ohm_lake.ohm_system.ohm_schema_lock SET locked_by = 'other-daemon', locked_at = CURRENT_TIMESTAMP, lock_expires_at = CURRENT_TIMESTAMP + INTERVAL '300 seconds' WHERE schema_name = 'acme_corp'")

        # Should time out (version is behind, lock is held)
        result = tm._wait_for_migration(conn, "acme_corp", timeout=3.0)
        assert result is False
        conn.close()
