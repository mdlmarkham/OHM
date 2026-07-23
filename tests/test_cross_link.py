"""Tests for OHM-tjzh mandatory cross-link constraint (ADR-018).

Verifies that synthesis-like node types (pattern, idea, task, decision, and
the forward-compat synthesis/observation/interpretation/challenge types)
require a `connects_to` field referencing an existing node id before they
can be created. Bare claims of these types become dead-ends that cannot
be navigated to, challenged, or used in Bayesian inference.

The constraint is enforced:
  1. At the queries layer via create_node(conn, ..., connects_to=[...]).
  2. At the HTTP boundary in POST /node and POST /tasks (returns 422).
  3. Skipped for exempt types (source, concept, entity).
  4. Skipped for updates of pre-existing nodes.

The graph health response includes a `dead_end_count` metric for tracking
the legacy tail of pre-existing dead-end nodes.
"""

from __future__ import annotations

import json
import socket
import threading
from http.client import HTTPConnection
from typing import TYPE_CHECKING, Any

import pytest

from ohm.schema import (
    DEFAULT_SCHEMA,
    EXEMPT_CROSS_LINK_NODE_TYPES,
    MUST_HAVE_EDGE_NODE_TYPES,
    requires_cross_link,
)
from ohm.server import OhmHandler
from ohm.server.handlers.graph import GraphHandlerMixin
from ohm.store import OhmStore
from tests.conftest import wait_for_port

if TYPE_CHECKING:
    import duckdb


# ── Schema helper tests (no DB needed) ──────────────────────────────────────


class TestRequiresCrossLinkHelper:
    """Unit tests for the requires_cross_link schema helper."""

    def test_pattern_requires_cross_link(self):
        assert requires_cross_link("pattern") is True

    def test_idea_requires_cross_link(self):
        assert requires_cross_link("idea") is True

    def test_task_requires_cross_link(self):
        assert requires_cross_link("task") is True

    def test_decision_requires_cross_link(self):
        assert requires_cross_link("decision") is True

    def test_concept_is_exempt(self):
        assert requires_cross_link("concept") is False

    def test_source_is_exempt(self):
        assert requires_cross_link("source") is False

    def test_person_is_exempt_by_default(self):
        """Person is not in MUST_HAVE_EDGE_NODE_TYPES and not in EXEMPT — defaults to not required."""
        # Person/equipment/etc. are foundational nodes; not derived claims.
        assert requires_cross_link("person") is False

    def test_forward_compat_types_in_set(self):
        """The OHM-tjzh spec types are in MUST_HAVE_EDGE_NODE_TYPES for future-compat."""
        for t in ("synthesis", "observation", "interpretation", "challenge"):
            assert t in MUST_HAVE_EDGE_NODE_TYPES
            assert requires_cross_link(t) is True

    def test_exempt_set_is_disjoint(self):
        """Exempt and required sets must not overlap."""
        assert MUST_HAVE_EDGE_NODE_TYPES.isdisjoint(EXEMPT_CROSS_LINK_NODE_TYPES)


# ── Queries-layer tests (uses in-memory DuckDB) ─────────────────────────────


class TestCreateNodeConnectsTo:
    """Tests for create_node() accepts and validates connects_to."""

    def test_concept_without_connects_to_succeeds(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        node = create_node(test_db, label="Hello world", node_type="concept", created_by="test")
        assert node["id"]

    def test_source_without_connects_to_succeeds(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        node = create_node(test_db, label="Source stub", node_type="source", created_by="test")
        assert node["id"]

    def test_pattern_with_valid_connects_to_succeeds(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        hub = create_node(test_db, label="Hub", node_type="concept", created_by="test")
        pattern = create_node(
            test_db,
            label="My pattern",
            node_type="pattern",
            created_by="test",
            connects_to=[hub["id"]],
        )
        assert pattern["id"]

    def test_pattern_with_unknown_connects_to_rejected(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        with pytest.raises(ValueError, match="unknown node id"):
            create_node(
                test_db,
                label="Bad pattern",
                node_type="pattern",
                created_by="test",
                connects_to=["nonexistent_id"],
            )

    def test_pattern_with_mixed_known_unknown_rejected(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        hub = create_node(test_db, label="Hub", node_type="concept", created_by="test")
        with pytest.raises(ValueError, match="nonexistent_id"):
            create_node(
                test_db,
                label="Mixed",
                node_type="pattern",
                created_by="test",
                connects_to=[hub["id"], "nonexistent_id"],
            )

    def test_pattern_with_empty_connects_to_rejected(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        with pytest.raises(ValueError, match="at least one node id"):
            create_node(
                test_db,
                label="Empty list",
                node_type="pattern",
                created_by="test",
                connects_to=[],
            )

    def test_decision_with_connects_to_succeeds(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node

        hub = create_node(test_db, label="Hub", node_type="concept", created_by="test")
        decision = create_node(
            test_db,
            label="My decision",
            node_type="decision",
            created_by="test",
            utility_scale=0.7,
            utility_usd_per_day=100.0,
            utility_currency="USD",
            current_best_action="act",
            action_alternatives=["act", "wait"],
            connects_to=[hub["id"]],
        )
        assert decision["id"]


# ── HTTP-level enforcement tests (uses live test server) ───────────────────


def _start_server(store):
    """Start a no-auth test server on a random port, return (port, server, thread)."""
    import socketserver

    OhmHandler.store = store
    OhmHandler.config = {"host": "127.0.0.1", "port": 0}
    OhmHandler.schema_config = DEFAULT_SCHEMA
    OhmHandler.tokens = {}
    OhmHandler.roles = {}
    OhmHandler.no_auth = True
    OhmHandler.multi_tenant = False
    OhmHandler.require_read_auth = False

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


def _http(method: str, port: int, path: str, body: dict | None = None) -> tuple[int, Any]:
    conn = HTTPConnection(f"127.0.0.1:{port}", timeout=5)
    body_bytes = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=body_bytes, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except json.JSONDecodeError:
        return resp.status, raw


@pytest.fixture
def cross_link_server(tmp_path):
    """Live test server for cross-link enforcement tests."""
    db_path = str(tmp_path / "cross_link.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    from ohm.server.server import _register_builtin_hooks

    _register_builtin_hooks(store)
    port, server, thread = _start_server(store)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


class TestHttpCrossLinkEnforcement:
    """HTTP-level enforcement of cross-link requirement."""

    def test_post_node_concept_succeeds_without_connects_to(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {"id": "n_concept", "label": "Plain concept", "type": "concept"},
        )
        assert status == 200 or status == 201, data
        assert data["id"] == "n_concept"

    def test_post_node_source_succeeds_without_connects_to(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_source",
                "label": "Plain source",
                "type": "source",
                "source_url": "https://example.com",
            },
        )
        assert status == 200 or status == 201, data

    def test_post_node_pattern_without_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {"id": "n_pattern", "label": "Bare pattern", "type": "pattern"},
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "cross_link_required" in data.get("message", "")
        assert "pattern" in data.get("message", "").lower()

    def test_post_node_idea_without_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {"id": "n_idea", "label": "Bare idea", "type": "idea"},
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "cross_link_required" in data.get("message", "")

    def test_post_node_decision_without_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_decision",
                "label": "Bare decision",
                "type": "decision",
                "utility_scale": 0.5,
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "cross_link_required" in data.get("message", "")

    def test_post_node_pattern_with_valid_connects_to_succeeds(self, cross_link_server):
        port, _ = cross_link_server
        # First create an anchor node (exempt type)
        status, _ = _http(
            "POST",
            port,
            "/node",
            {"id": "n_anchor", "label": "Anchor concept", "type": "concept"},
        )
        assert status in (200, 201)

        # Now create a pattern that links to the anchor
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_pattern_linked",
                "label": "Linked pattern",
                "type": "pattern",
                "connects_to": ["n_anchor"],
            },
        )
        assert status in (200, 201), data
        assert data["id"] == "n_pattern_linked"

    def test_post_node_pattern_with_unknown_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_pattern_bad_link",
                "label": "Pattern with bad link",
                "type": "pattern",
                "connects_to": ["nonexistent_target"],
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "cross_link_unknown_target" in data.get("message", "")
        assert "nonexistent_target" in data.get("message", "")

    def test_post_node_pattern_with_empty_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_pattern_empty_link",
                "label": "Pattern with empty link",
                "type": "pattern",
                "connects_to": [],
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "cross_link_required" in data.get("message", "")

    def test_post_node_pattern_with_nonlist_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/node",
            {
                "id": "n_pattern_str_link",
                "label": "Pattern with string link",
                "type": "pattern",
                "connects_to": "not_a_list",
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"
        assert "connects_to must be a list" in data.get("message", "")

    def test_post_node_update_existing_pattern_skips_check(self, cross_link_server):
        """Updating an existing pattern node is exempt — you cannot fix a historical dead-end by refusing to update."""
        port, _ = cross_link_server
        # First create with a valid connects_to
        _http(
            "POST",
            port,
            "/node",
            {"id": "n_pattern_existing", "label": "Anchor", "type": "concept"},
        )
        _http(
            "POST",
            port,
            "/node?create_only=false",
            {
                "id": "n_pattern_existing",
                "label": "Existing pattern",
                "type": "pattern",
                "connects_to": ["n_pattern_existing"],
            },
        )

        # Update without connects_to — should succeed (update path is exempt)
        status, data = _http(
            "POST",
            port,
            "/node?create_only=false",
            {
                "id": "n_pattern_existing",
                "label": "Updated pattern",
                "type": "pattern",
                "content": "new content",
            },
        )
        assert status in (200, 201), data
        assert data.get("label") == "Updated pattern"

    def test_post_task_without_connects_to_returns_422(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http(
            "POST",
            port,
            "/tasks",
            {"id": "t_bare", "label": "Bare task"},
        )
        assert status == 422
        assert data["error"] == "cross_link_required"
        assert data.get("node_type") == "task"

    def test_post_task_with_valid_connects_to_succeeds(self, cross_link_server):
        port, _ = cross_link_server
        _http(
            "POST",
            port,
            "/node",
            {"id": "n_task_anchor", "label": "Task anchor", "type": "concept"},
        )
        status, data = _http(
            "POST",
            port,
            "/tasks",
            {
                "id": "t_linked",
                "label": "Linked task",
                "connects_to": ["n_task_anchor"],
            },
        )
        assert status in (200, 201), data
        assert data.get("type") == "task"


# ── /health metric test ─────────────────────────────────────────────────────


class TestHealthDeadEndMetric:
    """The /health endpoint must report dead_end_count."""

    def test_health_includes_dead_end_count(self, cross_link_server):
        port, _ = cross_link_server
        status, data = _http("GET", port, "/health")
        assert status == 200
        graph = data.get("graph", {})
        assert "dead_end_count" in graph, "missing dead_end_count metric"
        assert "dead_end_rate" in graph, "missing dead_end_rate metric"
        assert graph["dead_end_count"] is not None

    def test_query_graph_health_returns_dead_end_count(self, test_db: "duckdb.DuckDBPyConnection"):
        from ohm.queries import create_node, query_graph_health

        # Empty graph: 0 dead ends
        h = query_graph_health(test_db)
        assert h.get("dead_end_count") == 0

        # Create a sink: A -> B (B has incoming but no outgoing)
        a = create_node(test_db, label="A", node_type="concept", created_by="test")
        b = create_node(test_db, label="B", node_type="concept", created_by="test")
        from ohm.queries import create_edge

        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test")

        h = query_graph_health(test_db)
        assert h.get("dead_end_count") == 1, f"expected 1 dead end, got {h}"

    def test_graph_health_orphan_count_with_soft_deleted_edge(self, test_db):
        """#968: orphan count must agree with reality when edges are soft-deleted."""
        from ohm.queries import create_node, create_edge, query_graph_health

        a = create_node(test_db, label="A", node_type="concept", created_by="test")
        b = create_node(test_db, label="B", node_type="concept", created_by="test")
        create_edge(test_db, from_node=a["id"], to_node=b["id"], layer="L3", edge_type="CAUSES", created_by="test")

        h = query_graph_health(test_db)
        assert h["orphan_nodes"] == 0

        test_db.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND to_node = ?", [a["id"], b["id"]])

        h = query_graph_health(test_db)
        assert h["orphan_nodes"] == 2, f"both nodes should be orphans after soft-delete, got {h['orphan_nodes']}"
        assert h["orphan_type_breakdown"].get("concept", 0) == 2


# ── SDK path test ───────────────────────────────────────────────────────────


class TestSdkCreateNodeConnectsTo:
    """The SDK create_node must forward connects_to to the queries layer."""

    def test_sdk_create_pattern_with_valid_link_succeeds(self, tmp_path):
        """The SDK forwards connects_to to the queries layer and accepts valid links."""
        import duckdb

        from ohm.schema import initialize_schema
        from ohm.sdk import Graph

        db_path = str(tmp_path / "sdk_test_link.duckdb")
        conn = duckdb.connect(db_path)
        initialize_schema(conn)

        with Graph(conn, actor="test_agent") as g:
            anchor = g.create_node("Anchor concept", node_type="concept")
            pattern = g.create_node(
                "Linked pattern",
                node_type="pattern",
                connects_to=[anchor["id"]],
            )
            assert pattern["id"]

    def test_sdk_create_pattern_with_unknown_link_raises(self):
        """The SDK's create_node raises ValueError when connects_to references a missing node."""
        import duckdb

        from ohm.schema import initialize_schema
        from ohm.sdk import Graph

        conn = duckdb.connect(":memory:")
        initialize_schema(conn)

        with Graph(conn, actor="test_agent") as g:
            with pytest.raises(ValueError, match="unknown node id"):
                g.create_node(
                    "Bad pattern",
                    node_type="pattern",
                    connects_to=["definitely_not_a_node"],
                )

    def test_sdk_create_pattern_with_empty_connects_to_raises(self):
        """An empty connects_to list is treated as a policy violation."""
        import duckdb

        from ohm.schema import initialize_schema
        from ohm.sdk import Graph

        conn = duckdb.connect(":memory:")
        initialize_schema(conn)

        with Graph(conn, actor="test_agent") as g:
            with pytest.raises(ValueError, match="at least one node id"):
                g.create_node(
                    "Empty link",
                    node_type="pattern",
                    connects_to=[],
                )
