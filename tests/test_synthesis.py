"""Tests for the /agent/synthesis endpoint and SDK write_synthesis method."""

import json
import socketserver
import threading
import pytest
from http.client import HTTPConnection

from ohm.server import OhmHandler, _hash_token
from ohm.schema import DEFAULT_SCHEMA
from ohm.store import OhmStore
from ohm.sdk import Graph


def _start_test_server(store, tokens=None, no_auth=True):
    """Start a test HTTP server on a random port (mirrors test_server.py pattern)."""
    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = DEFAULT_SCHEMA
    if tokens:
        token_hashes = {}
        for token, agent_name in tokens.items():
            token_hashes[_hash_token(token)] = agent_name
        OhmHandler.tokens = token_hashes
    else:
        OhmHandler.tokens = {}
    OhmHandler.roles = {}
    OhmHandler.no_auth = no_auth
    OhmHandler.require_read_auth = False

    server = socketserver.TCPServer(
        ("127.0.0.1", 0), OhmHandler, bind_and_activate=False,
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import time
    time.sleep(0.3)
    return port, server, thread


def _request(method, port, path, body=None, token=None):
    """Send HTTP request and return (status, json_data)."""
    conn = HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    return resp.status, data


@pytest.fixture
def synthesis_server(tmp_path):
    """Start a test server for synthesis endpoint tests."""
    db_path = str(tmp_path / "test_synthesis.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    tokens = {"test-token": "metis"}
    port, server, thread = _start_test_server(store, tokens=tokens, no_auth=True)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


class TestSynthesisHTTPEndpoint:
    """Test /agent/synthesis HTTP endpoint."""

    def test_create_synthesis_basic(self, synthesis_server):
        """Create a synthesis with cluster_ids and verify node + edges + observation."""
        port, store = synthesis_server

        # First create some cluster nodes
        _, n1 = _request("POST", port, "/node", {"id": "concept-a", "label": "Concept A", "type": "concept"}, token="test-token")
        _, n2 = _request("POST", port, "/node", {"id": "concept-b", "label": "Concept B", "type": "concept"}, token="test-token")

        # Create synthesis
        status, data = _request("POST", port, "/agent/synthesis", {
            "label": "A and B are connected",
            "content": "The pattern shows that A causes B through mechanism C",
            "cluster_ids": ["concept-a", "concept-b"],
            "edge_type": "SUPPORTS",
            "confidence": 0.85,
            "tags": ["pattern", "synthesis"]
        }, token="test-token")

        assert status == 201
        assert "node" in data
        assert data["edges_created"] == 2
        assert "observation" in data

    def test_synthesis_requires_fields(self, synthesis_server):
        """Synthesis requires label, content, and cluster_ids."""
        port, _ = synthesis_server

        # Missing content
        status, data = _request("POST", port, "/agent/synthesis", {
            "label": "Test",
            "cluster_ids": ["some-id"]
        }, token="test-token")
        assert status == 400

        # Missing cluster_ids
        status, data = _request("POST", port, "/agent/synthesis", {
            "label": "Test",
            "content": "Test content"
        }, token="test-token")
        assert status == 400

    def test_synthesis_skips_invalid_cluster_ids(self, synthesis_server):
        """Synthesis gracefully handles nonexistent cluster nodes.
        
        Note: DuckDB has no foreign key constraints, so edges to nonexistent
        nodes ARE created. This test verifies the endpoint doesn't crash."""
        port, _ = synthesis_server

        # Create one valid node
        _ = _request("POST", port, "/node", {"id": "valid-node", "label": "Valid Node", "type": "concept"}, token="test-token")

        status, data = _request("POST", port, "/agent/synthesis", {
            "label": "Partial synthesis",
            "content": "Only one valid connection",
            "cluster_ids": ["valid-node", "nonexistent-node-12345"],
            "confidence": 0.7
        }, token="test-token")

        assert status == 201
        # Only valid-node edge is created; nonexistent-node edge is rejected by referential integrity (OHM-7298)
        assert data["edges_created"] == 1

    def test_synthesis_default_edge_type(self, synthesis_server):
        """Default edge type is SUPPORTS."""
        port, _ = synthesis_server

        _ = _request("POST", port, "/node", {"id": "test-node", "label": "Test Node", "type": "concept"}, token="test-token")

        status, data = _request("POST", port, "/agent/synthesis", {
            "label": "Default synthesis",
            "content": "Testing defaults",
            "cluster_ids": ["test-node"],
        }, token="test-token")

        # Print error details if not 201
        if status != 201:
            print(f"SYNTHESIS RESPONSE: status={status}, data={data}")
        assert status == 201
        assert data["edges_created"] == 1


class TestSynthesisSDK:
    """Test SDK write_synthesis method."""

    def test_write_synthesis_direct(self, tmp_path):
        """Test write_synthesis via SDK with direct DuckDB connection."""
        db_path = str(tmp_path / "test_synthesis_sdk.duckdb")
        import duckdb
        conn = duckdb.connect(db_path)

        from ohm.schema import initialize_schema
        initialize_schema(conn)

        g = Graph(conn, actor="metis")

        # Create cluster nodes
        n1 = g.create_node(label="Hormuz AND-Gate", node_type="concept")
        n2 = g.create_node(label="Demand Rationing", node_type="concept")

        # Write synthesis
        result = g.write_synthesis(
            cluster_ids=[n1["id"], n2["id"]],
            label="AND-OR Inversion Pattern",
            content="The AND-gate in Hormuz is inverting into an OR-gate through demand rationing",
            edge_type="CAUSES",
            confidence=0.85,
            sigma=0.08,
            provenance="pattern_analysis",
            tags=["AND-OR", "governance", "hormuz"]
        )

        assert "node" in result
        assert result["edges_created"] == 2
        assert "observation" in result

        # Verify the node was created
        fetched = g.get_node(result["node"]["id"])
        assert fetched is not None
        assert fetched["label"] == "AND-OR Inversion Pattern"

        # Verify edges were created
        neighbors = g.neighborhood(result["node"]["id"], depth=1)
        assert len(neighbors) >= 2

        conn.close()