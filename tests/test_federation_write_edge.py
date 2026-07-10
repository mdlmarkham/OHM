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
