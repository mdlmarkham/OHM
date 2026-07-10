"""Test OHM-744: bounded backoff-retry for node existence in federated write_edge."""

import tempfile
import os
import shutil

import pytest

from ohm.tenant import TenantManager


@pytest.fixture
def fed_env():
    """Create a federated TenantManager with a DuckLake catalog."""
    tmp = tempfile.mkdtemp()
    catalog = os.path.join(tmp, "test_lake.ducklake")
    tenants_dir = os.path.join(tmp, "tenants")
    os.makedirs(tenants_dir)
    tm = TenantManager(
        tenants_dir=tenants_dir,
        shared_catalog_url=catalog,
    )
    yield tm, tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def local_env():
    """Create a local-file TenantManager."""
    tmp = tempfile.mkdtemp()
    tenants_dir = os.path.join(tmp, "tenants")
    os.makedirs(tenants_dir)
    tm = TenantManager(tenants_dir=tenants_dir)
    yield tm, tmp
    shutil.rmtree(tmp, ignore_errors=True)


class TestFederatedWriteEdgeRetry:
    """Test that write_edge uses bounded retry in federated mode."""

    def test_edge_to_existing_node_succeeds(self, fed_env):
        """write_edge succeeds immediately when the node exists (no retry needed)."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")
        store.write_node("n2", "Target", "concept", agent_name="test")
        edge = store.write_edge("n1", "n2", "CAUSES", "L3", agent_name="test")
        assert edge is not None
        store.close()

    def test_edge_to_nonexistent_node_raises_quickly(self, fed_env):
        """write_edge to a nonexistent node raises NodeNotFoundError without hanging."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")

        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError, match="to_node does not exist"):
            store.write_edge("n1", "nonexistent", "CAUSES", "L3", agent_name="test")
        store.close()

    def test_federated_flag_is_set(self, fed_env):
        """The _federated flag is True in federated mode, enabling the retry path."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        assert store._federated is True
        store.close()

    def test_edge_creation_e2e(self, fed_env):
        """End-to-end: create nodes and edge, verify edge exists."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")
        store.write_node("n2", "Target", "concept", agent_name="test")
        edge = store.write_edge("n1", "n2", "CAUSES", "L3", agent_name="test")
        assert edge is not None
        assert edge["from_node"] == "n1"
        assert edge["to_node"] == "n2"
        assert edge["edge_type"] == "CAUSES"
        store.close()


class TestLocalWriteEdgeNoChange:
    """Test that local-file mode keeps the existing single-shot behavior."""

    def test_local_flag_is_false(self, local_env):
        """The _federated flag is False in local mode, using the single-shot path."""
        tm, tmp = local_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        assert store._federated is False
        store.close()

    def test_local_edge_to_existing_node(self, local_env):
        """write_edge works in local mode."""
        tm, tmp = local_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")
        store.write_node("n2", "Target", "concept", agent_name="test")
        edge = store.write_edge("n1", "n2", "CAUSES", "L3", agent_name="test")
        assert edge is not None
        store.close()

    def test_local_edge_to_nonexistent_raises(self, local_env):
        """write_edge to nonexistent node raises NodeNotFoundError in local mode."""
        tm, tmp = local_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")

        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError, match="to_node does not exist"):
            store.write_edge("n1", "nonexistent", "CAUSES", "L3", agent_name="test")
        store.close()

    def test_local_edge_from_nonexistent_raises(self, local_env):
        """write_edge from nonexistent node raises NodeNotFoundError."""
        tm, tmp = local_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Target", "concept", agent_name="test")

        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError, match="from_node does not exist"):
            store.write_edge("nonexistent", "n1", "CAUSES", "L3", agent_name="test")
        store.close()


class TestChallengeEdgeExempt:
    """Challenge edges can reference nonexistent nodes — retry doesn't apply."""

    def test_challenge_edge_to_nonexistent_succeeds(self, fed_env):
        """Challenge edges bypass the existence check entirely."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")
        # challenge_of is not None — existence check is skipped
        edge = store.write_edge(
            "n1",
            "nonexistent",
            "CHALLENGED_BY",
            "L3",
            challenge_of="some-edge-id",
            agent_name="test",
        )
        assert edge is not None
        store.close()


class TestHyphenatedTenantId:
    """Test OHM-734 blocker 1: hyphenated tenant IDs work in federated mode."""

    def test_hyphenated_tenant_provisions_and_writes(self, fed_env):
        """A tenant with hyphens in its ID can be provisioned and written to."""
        tm, tmp = fed_env
        tm.provision("acme-corp", domain="ohm")
        store = tm.get_store("acme-corp")
        store.write_node("n1", "Test", "concept", agent_name="test")
        node = store.get_node("n1")
        assert node is not None
        assert node["label"] == "Test"
        store.close()

    def test_hyphenated_tenant_schema_name_normalize(self):
        """_tenant_schema_name replaces hyphens with underscores."""
        import tempfile
        import os
        import shutil

        tmp = tempfile.mkdtemp()
        try:
            tenants_dir = os.path.join(tmp, "tenants")
            os.makedirs(tenants_dir)
            catalog = os.path.join(tmp, "test_lake.ducklake")
            tm = TenantManager(
                tenants_dir=tenants_dir,
                shared_catalog_url=catalog,
            )
            assert tm._tenant_schema_name("acme-corp") == "acme_corp"
            assert tm._tenant_schema_name("my-tenant-123") == "my_tenant_123"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFederatedMigrationFidelity:
    """Test OHM-734 blocker 3: migrations create tables correctly in DuckLake."""

    def test_ohm_confidence_log_exists_in_federated_schema(self, fed_env):
        """ohm_confidence_log table is created in federated mode (not silently dropped)."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")

        # Verify ohm_confidence_log exists and has the right columns
        cols = store.conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_confidence_log' ORDER BY column_name").fetchall()
        col_names = [c[0] for c in cols]
        assert "id" in col_names, "ohm_confidence_log.id column should exist"
        assert "edge_id" in col_names, "ohm_confidence_log.edge_id column should exist"
        assert "agent" in col_names, "ohm_confidence_log.agent column should exist"
        assert "new_value" in col_names, "ohm_confidence_log.new_value column should exist"
        store.close()

    def test_log_confidence_change_works_in_federated_mode(self, fed_env):
        """log_confidence_change works end-to-end in federated mode."""
        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")
        store.write_node("n1", "Source", "concept", agent_name="test")
        store.write_node("n2", "Target", "concept", agent_name="test")
        edge = store.write_edge("n1", "n2", "CAUSES", "L3", confidence=0.8, agent_name="test")
        assert edge is not None

        from ohm.graph.queries import log_confidence_change

        log_confidence_change(
            store.conn,
            edge_id=edge["id"],
            agent="test",
            old_value=0.8,
            new_value=0.9,
            reason="updated confidence",
        )

        # Verify the log entry exists
        count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_confidence_log WHERE edge_id = ?",
            [edge["id"]],
        ).fetchone()[0]
        assert count == 1, "Confidence log entry should exist"
        store.close()

    def test_node_ids_are_generated_in_federated_mode(self, fed_env):
        """Node IDs are auto-generated (not NULL) in federated mode via create_node."""
        from ohm.graph.queries import create_node

        tm, tmp = fed_env
        tm.provision("acme_corp", domain="ohm")
        store = tm.get_store("acme_corp")

        node = create_node(
            store.conn,
            label="Auto ID Node",
            node_type="concept",
            created_by="test",
        )

        assert node is not None
        assert node["id"] is not None, "Node ID should be auto-generated, not NULL"
        assert len(node["id"]) > 0, "Node ID should be a non-empty string"

        # Verify it's readable
        fetched = store.get_node(node["id"])
        assert fetched is not None
        assert fetched["label"] == "Auto ID Node"
        store.close()
