"""Test helpers and fixtures for OHM tests.

Provides a temporary DuckDB in-memory database with the OHM schema
pre-initialized, plus sample data factories.
"""

from __future__ import annotations

import pathlib
import uuid
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


def create_test_db() -> "duckdb.DuckDBPyConnection":
    """Create an in-memory DuckDB with the OHM schema initialized."""
    import duckdb

    conn = duckdb.connect(":memory:")
    from ohm.schema import initialize_schema

    initialize_schema(conn)
    return conn


def create_sample_node(
    conn: "duckdb.DuckDBPyConnection",
    *,
    label: str = "test_node",
    node_type: str = "concept",
    created_by: str = "test_agent",
    visibility: str = "team",
    provenance: str = "conversation",
    confidence: float = 1.0,
) -> str:
    """Insert a sample node and return its ID."""
    node_id = f"{label.lower().replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
    conn.execute(
        """
        INSERT INTO ohm_nodes (id, label, type, created_by, visibility, provenance, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [node_id, label, node_type, created_by, visibility, provenance, confidence],
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
) -> str:
    """Insert a sample edge and return its ID."""
    edge_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type,
                               created_by, confidence, challenge_of, challenge_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [edge_id, from_node, to_node, layer, edge_type,
         created_by, confidence, challenge_of, challenge_type],
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
) -> str:
    """Insert a sample observation and return its ID."""
    obs_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO ohm_observations (id, node_id, type, value, source, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [obs_id, node_id, obs_type, value, source, created_by],
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
        edges["ab"] = create_sample_edge(conn, from_node=nodes["a"], to_node=nodes["b"],
                                          edge_type="CAUSES", layer="L3")
        edges["bc"] = create_sample_edge(conn, from_node=nodes["b"], to_node=nodes["c"],
                                          edge_type="INFLUENCES", layer="L2")

    elif size == "medium":
        for name in ["A", "B", "C", "D", "E", "F"]:
            nodes[name] = create_sample_node(conn, label=f"Node {name}")
        edges["ab"] = create_sample_edge(conn, from_node=nodes["A"], to_node=nodes["B"],
                                          edge_type="CAUSES", layer="L3")
        edges["bc"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["C"],
                                          edge_type="CAUSES", layer="L3")
        edges["bd"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["D"],
                                          edge_type="INFLUENCES", layer="L2")
        edges["ce"] = create_sample_edge(conn, from_node=nodes["C"], to_node=nodes["E"],
                                          edge_type="PREDICTS", layer="L3")
        edges["cf"] = create_sample_edge(conn, from_node=nodes["C"], to_node=nodes["F"],
                                          edge_type="DERIVES_FROM", layer="L2")
        edges["de"] = create_sample_edge(conn, from_node=nodes["D"], to_node=nodes["E"],
                                          edge_type="REFERENCES", layer="L2")
        edges["challenge"] = create_sample_edge(
            conn, from_node=nodes["F"], to_node=nodes["B"],
            edge_type="CHALLENGED_BY", layer="L3",
            challenge_of=edges["ab"], challenge_type="CHALLENGED_BY",
            created_by="critic_agent", confidence=0.4,
        )
        edges["support"] = create_sample_edge(
            conn, from_node=nodes["E"], to_node=nodes["B"],
            edge_type="SUPPORTS", layer="L3",
            challenge_of=edges["ab"], challenge_type="SUPPORTS",
            created_by="supporter_agent", confidence=0.8,
        )

    else:  # large
        import string
        for i, name in enumerate(string.ascii_uppercase[:10]):
            nodes[name] = create_sample_node(conn, label=f"Node {name}")
        # Chain: A→B→C→D→E→F→G→H→I→J
        for i in range(9):
            keys = list(string.ascii_uppercase[:10])
            edges[f"chain_{i}"] = create_sample_edge(
                conn, from_node=nodes[keys[i]], to_node=nodes[keys[i + 1]],
                edge_type="CAUSES", layer="L3",
            )
        # Cross edges: A→C, B→E, D→F, G→J
        edges["cross_1"] = create_sample_edge(conn, from_node=nodes["A"], to_node=nodes["C"],
                                               edge_type="INFLUENCES", layer="L2")
        edges["cross_2"] = create_sample_edge(conn, from_node=nodes["B"], to_node=nodes["E"],
                                               edge_type="REFERENCES", layer="L2")
        edges["cross_3"] = create_sample_edge(conn, from_node=nodes["D"], to_node=nodes["F"],
                                               edge_type="DERIVES_FROM", layer="L2")
        edges["cross_4"] = create_sample_edge(conn, from_node=nodes["G"], to_node=nodes["J"],
                                               edge_type="PREDICTS", layer="L3")

    return {"nodes": nodes, "edges": edges}
