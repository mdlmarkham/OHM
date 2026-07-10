"""End-to-end HTTP tests for #747 onboarding surfaces (OHM-755).

Tests the actual HTTP-level behavior of:
- GET /suggest returning domain_onboarding for zero-activity agents
- GET /schema surfacing onboarding_node_id
"""

from __future__ import annotations

import json
from http.client import HTTPConnection

import pytest

from tests.conftest import _start_test_server, _request

pytestmark = pytest.mark.integration


@pytest.fixture
def onboarding_server(tmp_path):
    """Start a test server with a schema config that has onboarding_node_id set."""
    from ohm.graph.embeddings import NullBackend
    from ohm.graph.schema import SchemaConfig
    from ohm.store import OhmStore

    schema = SchemaConfig(onboarding_node_id="welcome_onboarding_001")
    db_path = str(tmp_path / "test_onboarding.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
    )
    port, server, thread = _start_test_server(store, no_auth=True, schema_config=schema)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.fixture
def plain_server(tmp_path):
    """Start a test server with default schema (no onboarding_node_id)."""
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "test_plain.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
    )
    port, server, thread = _start_test_server(store, no_auth=True)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


class TestSuggestOnboarding:
    """GET /suggest onboarding behavior."""

    def test_zero_activity_agent_gets_onboarding(self, onboarding_server):
        """Agent with 0 nodes/edges gets domain_onboarding response."""
        port, store = onboarding_server
        status, data = _request("GET", port, "/suggest")
        assert status == 200
        assert data.get("domain_onboarding") is True
        assert data.get("onboarding_node_id") == "welcome_onboarding_001"

    def test_agent_with_nodes_gets_normal_suggest(self, onboarding_server):
        """Agent with existing nodes does NOT get onboarding nudge."""
        port, store = onboarding_server
        # Server in no-auth mode uses agent="ohm", so write as "ohm"
        store.write_node("n1", "Existing Node", "concept", agent_name="ohm")
        status, data = _request("GET", port, "/suggest")
        assert status == 200
        # Normal suggest returns a list (not the onboarding dict)
        if isinstance(data, dict):
            assert data.get("domain_onboarding") is not True
        else:
            # List response = normal suggest — onboarding was correctly skipped
            assert isinstance(data, list)

    def test_no_onboarding_when_schema_lacks_it(self, plain_server):
        """Server without onboarding_node_id returns normal suggest."""
        port, store = plain_server
        status, data = _request("GET", port, "/suggest")
        assert status == 200
        assert "domain_onboarding" not in data or data.get("domain_onboarding") is not True


class TestSchemaOnboarding:
    """GET /schema onboarding_node_id surfacing."""

    def test_schema_includes_onboarding_node_id(self, onboarding_server):
        """GET /schema includes onboarding_node_id when configured."""
        port, store = onboarding_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert data.get("onboarding_node_id") == "welcome_onboarding_001"

    def test_schema_omits_onboarding_node_id_when_not_set(self, plain_server):
        """GET /schema does not include onboarding_node_id when not configured."""
        port, store = plain_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert "onboarding_node_id" not in data

    def test_welcome_shows_onboarding_for_new_agent(self, onboarding_server):
        """GET /welcome for zero-activity agent includes domain_onboarding hint (OHM-774)."""
        port, store = onboarding_server
        status, data = _request("GET", port, "/welcome?agent=newagent")
        assert status == 200
        assert "domain_onboarding" in data
        assert data["domain_onboarding"]["node_id"] == "welcome_onboarding_001"

    def test_schema_includes_onboarding_hint(self, onboarding_server):
        """GET /schema includes onboarding_hint when configured (OHM-774)."""
        port, store = onboarding_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert "onboarding_hint" in data
        assert "welcome_onboarding_001" in data["onboarding_hint"]
