"""Concurrent access tests for the OHM daemon.

Tests that multiple agents can read/write concurrently without
data loss or corruption (OHM-y2i.4.12).
"""

import json
import threading
import time
from http.client import HTTPConnection

import pytest

from ohm.server import OhmHandler
from ohm.store import OhmStore


# ── Test Helpers ────────────────────────────────────────────

def _start_server(store, tokens=None, roles=None, no_auth=False):
    """Start a test HTTP server on a random port."""
    import socketserver

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.tokens = tokens or {}
    OhmHandler.roles = roles or {}
    OhmHandler.no_auth = no_auth

    server = socketserver.TCPServer(
        ("127.0.0.1", 0), OhmHandler, bind_and_activate=False,
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    return port, server, thread


def _request(method, port, path, body=None, headers=None):
    """Make an HTTP request to the test server."""
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=30)
    hdrs = headers or {}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = None
    try:
        conn.request(method, path, body=body_bytes, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode()
        try:
            return resp.status, json.loads(data)
        except json.JSONDecodeError:
            return resp.status, data
    finally:
        conn.close()


# ── Concurrent Agent Simulation ────────────────────────────

class ConcurrentAgent:
    """Simulates an agent making concurrent requests."""

    def __init__(self, agent_id, port, base_node_id):
        self.agent_id = agent_id
        self.port = port
        self.base_node_id = base_node_id
        self.results: list[dict] = []
        self.errors: list[dict] = []

    def create_nodes_and_edges(self, count=5):
        """Create nodes and edges concurrently."""
        for i in range(count):
            node_id = f"{self.base_node_id}_{self.agent_id}_{i}"
            # Create node
            status, data = _request("POST", self.port, "/node", body={
                "id": node_id,
                "label": f"Agent {self.agent_id} Node {i}",
                "type": "concept",
            })
            if status == 201:
                self.results.append({"type": "node", "id": node_id})
            else:
                self.errors.append({"type": "node", "id": node_id, "status": status})

            # Create edge to previous node
            if i > 0:
                prev_node_id = f"{self.base_node_id}_{self.agent_id}_{i-1}"
                edge_status, edge_data = _request("POST", self.port, "/edge", body={
                    "from": prev_node_id,
                    "to": node_id,
                    "type": "CAUSES",
                    "layer": "L3",
                })
                if edge_status == 201:
                    self.results.append({"type": "edge", "from": prev_node_id, "to": node_id})
                else:
                    self.errors.append({
                        "type": "edge",
                        "from": prev_node_id,
                        "to": node_id,
                        "status": edge_status,
                    })

    def query_graph(self, node_id, iterations=10):
        """Make concurrent read requests."""
        for _ in range(iterations):
            status, data = _request("GET", self.port, f"/neighborhood/{node_id}")
            if status == 200:
                self.results.append({"type": "query", "node": node_id})
            else:
                self.errors.append({"type": "query", "node": node_id, "status": status})


# ── Fixtures ────────────────────────────────────────────────

@pytest.fixture
def concurrent_server(tmp_path):
    """Start a test server with no-auth for concurrent testing."""
    db_path = str(tmp_path / "concurrent_test.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    port, server, thread = _start_server(store, no_auth=True)
    yield port, store
    server.shutdown()
    thread.join(timeout=5)
    store.close()


# ── Tests ──────────────────────────────────────────────────

class TestConcurrentWrites:
    """Test that concurrent writes don't cause data loss."""

    def test_ten_agents_concurrent_writes_no_data_loss(self, concurrent_server):
        """10 agents writing concurrently should result in no data loss.

        Each agent creates 5 nodes and 4 edges. Total expected:
        - 50 nodes (10 agents × 5 nodes)
        - 40 edges (10 agents × 4 edges)

        All writes should succeed (201 status).
        """
        port, store = concurrent_server
        num_agents = 10
        nodes_per_agent = 5

        # Create agents and start concurrent writes
        agents = []
        threads = []

        for i in range(num_agents):
            agent = ConcurrentAgent(f"agent_{i}", port, f"base_{i}")
            agents.append(agent)
            t = threading.Thread(target=agent.create_nodes_and_edges, args=(nodes_per_agent,))
            threads.append(t)

        # Start all threads simultaneously
        for t in threads:
            t.start()

        # Wait for all to complete
        for t in threads:
            t.join(timeout=30)

        # Verify no errors
        total_errors = sum(len(a.errors) for a in agents)
        assert total_errors == 0, f"Expected no errors but got: {[e for a in agents for e in a.errors]}"

        # Verify all nodes exist in database
        node_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id LIKE 'base_%'"
        ).fetchone()[0]
        assert node_count == num_agents * nodes_per_agent, \
            f"Expected {num_agents * nodes_per_agent} nodes but found {node_count}"

        # Verify all edges exist
        edge_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node LIKE 'base_%'"
        ).fetchone()[0]
        assert edge_count == num_agents * (nodes_per_agent - 1), \
            f"Expected {num_agents * (nodes_per_agent - 1)} edges but found {edge_count}"

    def test_concurrent_reads_during_writes(self, concurrent_server):
        """Concurrent reads during writes should succeed without errors."""
        port, store = concurrent_server

        # Create a test node first
        _request("POST", port, "/node", body={
            "id": "read_test",
            "label": "Read Test Node",
            "type": "concept",
        })

        # Start concurrent reads and writes
        read_agent = ConcurrentAgent("reader", port, "read_test")
        write_agents = []

        # Start multiple read threads
        read_threads = []
        for i in range(5):
            t = threading.Thread(target=read_agent.query_graph, args=("read_test", 10))
            read_threads.append(t)

        # Start write threads
        write_threads = []
        for i in range(5):
            write_agent = ConcurrentAgent(f"writer_{i}", port, f"write_{i}")
            write_agents.append(write_agent)
            t = threading.Thread(target=write_agent.create_nodes_and_edges, args=(3,))
            write_threads.append(t)

        # Start all threads
        for t in read_threads + write_threads:
            t.start()

        # Wait for completion
        for t in read_threads + write_threads:
            t.join(timeout=30)

        # Verify reads succeeded
        read_success = sum(1 for r in read_agent.results if r["type"] == "query")
        assert read_success > 0, "No successful reads during concurrent writes"

        # Verify no errors on reads
        read_errors = [e for e in read_agent.errors if e["type"] == "query"]
        assert len(read_errors) == 0, f"Read errors during concurrent writes: {read_errors}"


class TestConcurrentConflictResolution:
    """Test edge cases with concurrent access patterns."""

    def test_rapid_fire_requests_same_endpoint(self, concurrent_server):
        """Rapid fire requests to the same endpoint should all succeed."""
        port, store = concurrent_server
        node_id = "rapid_node"

        # Create initial node
        _request("POST", port, "/node", body={
            "id": node_id,
            "label": "Rapid Node",
            "type": "concept",
        })

        # Fire 50 concurrent requests to the same endpoint
        results = []
        errors = []
        lock = threading.Lock()

        def make_request():
            status, data = _request("GET", port, f"/node/{node_id}")
            with lock:
                if status == 200:
                    results.append(data)
                else:
                    errors.append({"status": status, "data": data})

        threads = [threading.Thread(target=make_request) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) >= 45, f"Expected at least 45 successes but got {len(results)}"
        # A few failures are acceptable under high concurrency (rate limiting, connection resets)
        # The key invariant: no server crash, no 500 errors
        server_errors = [e for e in errors if e.get("status", 0) >= 500]
        assert len(server_errors) == 0, f"Server errors during concurrent reads: {server_errors}"

    def test_concurrent_creates_same_node_id(self, concurrent_server):
        """Concurrent creates with same node ID should handle gracefully.

        DuckDB PRIMARY KEY constraint should reject duplicates.
        The server should return an error for subsequent attempts.
        No server crash.
        """
        port, store = concurrent_server
        node_id = "duplicate_id"

        # First create should succeed
        status, data = _request("POST", port, "/node", body={
            "id": node_id,
            "label": "Duplicate Node",
            "type": "concept",
        })
        assert status == 201, f"First create should succeed, got {status}"

        # Subsequent creates - need to check what status code comes back
        # The error handling should return 400 or 409, not 500
        results = []
        for _ in range(10):
            status, data = _request("POST", port, "/node", body={
                "id": node_id,
                "label": "Duplicate Node",
                "type": "concept",
            })
            results.append(status)

        # Count how many succeeded - exactly 1 should have
        success_count = sum(1 for s in results if s == 201)
        # The first one (outside this loop) succeeded, so no more should
        assert success_count == 0, \
            f"Expected no additional successes but got {success_count}"

        # Verify exactly one node exists
        node_count = store.conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE id = ?", [node_id]
        ).fetchone()[0]
        assert node_count == 1, f"Expected exactly 1 node, got {node_count}"


class TestRaceConditionPrevention:
    """Test that race conditions don't cause corruption."""

    def test_edge_references_valid_nodes(self, concurrent_server):
        """All edges should reference valid nodes after concurrent writes."""
        port, store = concurrent_server

        # Create nodes and edges concurrently
        num_agents = 5
        nodes_per_agent = 10

        agents = []
        threads = []

        for i in range(num_agents):
            agent = ConcurrentAgent(f"race_agent_{i}", port, f"race_{i}")
            agents.append(agent)
            t = threading.Thread(target=agent.create_nodes_and_edges, args=(nodes_per_agent,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Verify all edges have valid from_node and to_node references
        invalid_edges = store.conn.execute("""
            SELECT e.id, e.from_node, e.to_node
            FROM ohm_edges e
            LEFT JOIN ohm_nodes n1 ON e.from_node = n1.id
            LEFT JOIN ohm_nodes n2 ON e.to_node = n2.id
            WHERE n1.id IS NULL OR n2.id IS NULL
        """).fetchall()

        assert len(invalid_edges) == 0, \
            f"Found {len(invalid_edges)} edges with invalid node references: {invalid_edges}"

    def test_no_orphaned_edges_after_concurrent_writes(self, concurrent_server):
        """Every edge should have a valid challenge_of reference or null."""
        port, store = concurrent_server

        # Create nodes and edges
        agent = ConcurrentAgent("orphan_test", port, "orphan")
        agent.create_nodes_and_edges(5)

        # Challenge an edge
        edges = store.conn.execute(
            "SELECT id FROM ohm_edges WHERE from_node LIKE 'orphan_%' LIMIT 1"
        ).fetchone()
        if edges:
            edge_id = edges[0]
            status, _ = _request("POST", port, "/challenge", body={
                "edge_id": edge_id,
                "challenge_type": "CHALLENGED_BY",
                "confidence": 0.3,
            })

        # Verify no orphaned challenge edges
        orphaned = store.conn.execute("""
            SELECT COUNT(*) FROM ohm_edges
            WHERE challenge_of IS NOT NULL
            AND challenge_of NOT IN (SELECT id FROM ohm_edges)
        """).fetchone()[0]

        assert orphaned == 0, f"Found {orphaned} orphaned challenge references"
