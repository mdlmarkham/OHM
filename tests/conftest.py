"""Test helpers and fixtures for OHM tests.

Provides a temporary DuckDB in-memory database with the OHM schema
pre-initialized, plus sample data factories.
"""

from __future__ import annotations

import pathlib
import sys
import uuid
from io import StringIO
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import duckdb


@pytest.fixture(scope="session", autouse=True)
def _cleanup_duckdb_tmp_files():
    """Remove orphaned .tmp- files from the DuckDB extensions directory.

    DuckDB's INSTALL command writes to a temp file with a UUID suffix, then
    renames it on success. If the process is interrupted (common in tests),
    these temp files are left behind and accumulate over time, consuming
    significant disk space. This fixture cleans them up after the test session.
    """
    yield  # Run after all tests complete

    ext_dir = pathlib.Path.home() / ".duckdb" / "extensions"
    if not ext_dir.exists():
        return

    count = 0
    for tmp_file in ext_dir.rglob("*.tmp-*"):
        try:
            tmp_file.unlink()
            count += 1
        except OSError:
            pass  # File may be in use or already deleted

    if count > 0:
        print(f"\nCleaned up {count} orphaned DuckDB extension temp file(s)")


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    """Clear the rate limit store between tests to prevent flaky failures."""
    import ohm.server as srv

    srv._rate_limit_store.clear()


@pytest.fixture(autouse=True)
def _clear_bayesian_cache():
    """Clear the Bayesian network cache between tests to prevent cross-test pollution (OHM-omr)."""
    import ohm.bayesian as bay

    bay._bayesian_network_cache.clear()


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll until a TCP port is accepting connections (replaces blind sleep)."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.05)
            s.close()
            return
        except OSError:
            time.sleep(0.005)
    raise TimeoutError(f"Server at {host}:{port} did not start within {timeout}s")


def create_test_db() -> "duckdb.DuckDBPyConnection":
    """Create an in-memory DuckDB with the OHM schema initialized."""
    import duckdb

    conn = duckdb.connect(":memory:")
    from ohm.schema import initialize_schema

    initialize_schema(conn)
    return conn


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI with given args and capture stdout/stderr + exit code."""
    from ohm.cli import main

    stdout = StringIO()
    stderr = StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    exit_code = 0
    try:
        main(argv)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    except Exception:
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return exit_code, stdout.getvalue(), stderr.getvalue()


def create_sample_node(
    conn: "duckdb.DuckDBPyConnection",
    *,
    label: str = "test_node",
    node_type: str = "concept",
    created_by: str = "test_agent",
    visibility: str = "team",
    provenance: str = "conversation",
    confidence: float = 1.0,
    utility_scale: float | None = None,
    utility_usd_per_day: float | None = None,
    utility_currency: str | None = None,
) -> str:
    """Insert a sample node and return its ID."""
    node_id = f"{label.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
    conn.execute(
        """
        INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence,
                               utility_scale, utility_usd_per_day, utility_currency)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [node_id, label, node_type, created_by, visibility, provenance, confidence, utility_scale, utility_usd_per_day, utility_currency],
    )
    return node_id


def create_sample_edge(
    conn: "duckdb.DuckDBPyConnection",
    *,
    from_node: str,
    to_node: str,
    layer: str = "L3",
    edge_type: str = "CAUSES",
    created_by: str = "test_agent",
    confidence: float = 0.9,
    challenge_of: str | None = None,
    challenge_type: str | None = None,
    probability: float | None = None,
    probability_p05: float | None = None,
    probability_p50: float | None = None,
    probability_p95: float | None = None,
    confidence_p05: float | None = None,
    confidence_p50: float | None = None,
    confidence_p95: float | None = None,
) -> str:
    """Insert a sample edge and return its ID."""
    edge_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type,
                               created_by, confidence, challenge_of, challenge_type,
                               probability, probability_p05, probability_p50, probability_p95,
                               confidence_p05, confidence_p50, confidence_p95)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [edge_id, from_node, to_node, layer, edge_type, created_by, confidence, challenge_of, challenge_type, probability, probability_p05, probability_p50, probability_p95, confidence_p05, confidence_p50, confidence_p95],
    )
    return edge_id


def create_sample_observation(
    conn: "duckdb.DuckDBPyConnection",
    *,
    node_id: str,
    obs_type: str = "measurement",
    value: float = 1.0,
    source: str = "analysis",
    created_by: str = "test_agent",
    scale: str = "probability",
    created_at: str | None = None,
) -> str:
    """Insert a sample observation and return its ID."""
    obs_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO ohm_observations (id, node_id, type, value, source, created_by, scale, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [obs_id, node_id, obs_type, value, source, created_by, scale, created_at],
    )
    return obs_id


# ── Pytest Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def test_db():
    """Provide a fresh in-memory DuckDB with OHM schema for each test."""
    conn = create_test_db()
    yield conn
    conn.close()


@pytest.fixture
def db():
    """Create an in-memory test database with OHM schema for Bayesian tests."""
    conn = create_test_db()
    yield conn
    conn.close()


@pytest.fixture
def sample_graph_small(test_db):
    """Provide a small sample graph (3 nodes, 2 edges)."""
    return create_sample_graph(test_db, size="small")


@pytest.fixture
def sample_graph_medium(test_db):
    """Provide a medium sample graph (6 nodes, 8 edges with challenges)."""
    return create_sample_graph(test_db, size="medium")


@pytest.fixture
def sample_graph_large(test_db):
    """Provide a large sample graph (10 nodes, 13 edges)."""
    return create_sample_graph(test_db, size="large")


def create_sample_graph(conn: "duckdb.DuckDBPyConnection", size: str = "small") -> dict[str, Any]:
    """Create a sample graph for testing.

    Args:
        conn: Database connection.
        size: 'small' (3 nodes, 2 edges), 'medium' (6 nodes, 8 edges),
              or 'large' (10 nodes, 15 edges).

    Returns:
        Dict with 'nodes' and 'edges' lists of IDs.
    """
    nodes = {}
    edges = {}

    if size == "small":
        nodes["a"] = create_sample_node(conn, label="Node A")
        nodes["b"] = create_sample_node(conn, label="Node B")
        nodes["c"] = create_sample_node(conn, label="Node C")
        edges["ab"] = create_sample_edge(conn, from_node=nodes["a"], to_node=nodes["b"], edge_type="CAUSES", layer="L3")
        edges["bc"] = create_sample_edge(conn, from_node=nodes["b"], to_node=nodes["c"], edge_type="INFLUENCES", layer="L2")

    elif size == "medium":
        for name in ["A", "B", "C", "D", "E", "F"]:
            nodes[name] = create_sample_node(conn, label=f"Node {name}")
        edges["ab"] = create_sample_edge(conn, from_node=nodes["A"], to_node=nodes["B"], edge_type="CAUSES", layer="L3")
        edges["bc"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["C"], edge_type="CAUSES", layer="L3")
        edges["bd"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["D"], edge_type="INFLUENCES", layer="L2")
        edges["ce"] = create_sample_edge(conn, from_node=nodes["C"], to_node=nodes["E"], edge_type="PREDICTS", layer="L3")
        edges["cf"] = create_sample_edge(conn, from_node=nodes["C"], to_node=nodes["F"], edge_type="DERIVES_FROM", layer="L2")
        edges["de"] = create_sample_edge(conn, from_node=nodes["D"], to_node=nodes["E"], edge_type="REFERENCES", layer="L2")
        edges["challenge"] = create_sample_edge(
            conn,
            from_node=nodes["F"],
            to_node=nodes["B"],
            edge_type="CHALLENGED_BY",
            layer="L3",
            challenge_of=edges["ab"],
            challenge_type="CHALLENGED_BY",
            created_by="critic_agent",
            confidence=0.4,
        )
        edges["support"] = create_sample_edge(
            conn,
            from_node=nodes["E"],
            to_node=nodes["B"],
            edge_type="SUPPORTS",
            layer="L3",
            challenge_of=edges["ab"],
            challenge_type="SUPPORTS",
            created_by="supporter_agent",
            confidence=0.8,
        )

    else:  # large
        import string

        for i, name in enumerate(string.ascii_uppercase[:10]):
            nodes[name] = create_sample_node(conn, label=f"Node {name}")
        # Chain: A→B→C→D→E→F→G→H→I→J
        for i in range(9):
            keys = list(string.ascii_uppercase[:10])
            edges[f"chain_{i}"] = create_sample_edge(
                conn,
                from_node=nodes[keys[i]],
                to_node=nodes[keys[i + 1]],
                edge_type="CAUSES",
                layer="L3",
            )
        # Cross edges: A→C, B→E, D→F, G→J
        edges["cross_1"] = create_sample_edge(conn, from_node=nodes["A"], to_node=nodes["C"], edge_type="INFLUENCES", layer="L2")
        edges["cross_2"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["E"], edge_type="REFERENCES", layer="L2")
        edges["cross_3"] = create_sample_edge(conn, from_node=nodes["D"], to_node=nodes["F"], edge_type="DERIVES_FROM", layer="L2")
        edges["cross_4"] = create_sample_edge(conn, from_node=nodes["G"], to_node=nodes["J"], edge_type="PREDICTS", layer="L3")

    return {"nodes": nodes, "edges": edges}


# ── Shared HTTP server fixtures ───────────────────────────────────────────

import json
import threading
from http.client import HTTPConnection


def _start_test_server(store, tokens=None, roles=None, no_auth=False, schema_config=None, require_read_auth=False, multi_tenant=False):
    """Start a test HTTP server on a random port and return (port, server, thread)."""
    import socketserver

    from ohm.server import OhmHandler, _hash_token
    from ohm.schema import DEFAULT_SCHEMA
    from ohm.server.server import _register_builtin_hooks

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = schema_config or DEFAULT_SCHEMA
    if tokens:
        token_hashes = {}
        for token, agent_name in tokens.items():
            token_hashes[_hash_token(token)] = agent_name
        OhmHandler.tokens = token_hashes
    else:
        OhmHandler.tokens = {}
    OhmHandler.roles = roles or {}
    OhmHandler.no_auth = no_auth
    OhmHandler.multi_tenant = multi_tenant
    if multi_tenant and not require_read_auth:
        OhmHandler.require_read_auth = True
    else:
        OhmHandler.require_read_auth = require_read_auth

    _register_builtin_hooks(store)

    # Pre-warm pgmpy imports to avoid 15s cold-import penalty on first request
    try:
        import ohm.inference.bayesian  # noqa: F401 — triggers PGMPY_AVAILABLE at module level
    except Exception:
        pass

    server = socketserver.TCPServer(
        ("127.0.0.1", 0),
        OhmHandler,
        bind_and_activate=False,
    )
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_port("127.0.0.1", port)
    return port, server, thread


def _request(method, port, path, body=None, headers=None, token=None):
    """Make an HTTP request to the test server."""
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=15)
    hdrs = headers or {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"
        body_bytes = json.dumps(body).encode() if not isinstance(body, bytes) else body
    elif body is not None and isinstance(body, bytes):
        body_bytes = body
    elif body is not None:
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = None
    conn.request(method, path, body=body_bytes, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


@pytest.fixture
def test_server(tmp_path):
    """Start a test server with a temp database (no-auth dev mode)."""
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "test_server.duckdb")
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


@pytest.fixture
def auth_server(tmp_path):
    """Start a test server with token auth enabled."""
    from ohm.graph.embeddings import NullBackend
    from ohm.store import OhmStore

    db_path = str(tmp_path / "test_auth.duckdb")
    store = OhmStore(
        db_path=db_path,
        agent_name="test_agent",
        embedding_backend=NullBackend(dimensions=768),
    )
    tokens = {"test-token-abc": "metis", "readonly-token": "observer"}
    roles = {"metis": "read-write", "observer": "read-only"}
    port, server, thread = _start_test_server(store, tokens=tokens, roles=roles)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()
