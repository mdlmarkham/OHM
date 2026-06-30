"""Tests for OHM-0abu HTTP type-field aliases.

The HTTP body uses the generic key ``type`` for node/edge/observation
types, which collides with natural-language naming. Per the live daemon
test report (2026-06-30), clients reasonably send ``node_type``,
``edge_type``, or ``obs_type`` instead — and the handler was silently
falling back to the default ("concept" / "" / "measurement"), creating
the wrong node type. This test verifies both spellings work and the
correct precedence.

Acceptance:
- POST /node accepts both 'type' and 'node_type' (canonical)
- POST /edge accepts both 'type' and 'edge_type'
- POST /observation accepts both 'type' and 'obs_type'
- Descriptive name wins when both are present
- Neither present falls back to the documented default
- Bug reproduction (POST /node with node_type=decision) now creates
  type='decision', not type='concept'
"""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from http.client import HTTPConnection

import pytest

from ohm.schema import DEFAULT_SCHEMA
from ohm.server import OhmHandler
from ohm.server.handlers.graph import _resolve_type_field
from ohm.store import OhmStore
from tests.conftest import wait_for_port


# ── Unit tests for the resolver helper ───────────────────────────────────────


class TestResolveTypeField:
    def test_descriptive_name_wins(self):
        assert _resolve_type_field(
            {"node_type": "decision", "type": "concept"},
            "node_type", "type", default="concept",
        ) == "decision"

    def test_legacy_type_falls_through(self):
        assert _resolve_type_field(
            {"type": "decision"},
            "node_type", "type", default="concept",
        ) == "decision"

    def test_missing_both_uses_default(self):
        assert _resolve_type_field(
            {}, "node_type", "type", default="concept",
        ) == "concept"

    def test_empty_body_returns_default(self):
        assert _resolve_type_field(
            {"node_type": None, "type": None},
            "node_type", "type", default="concept",
        ) == "concept"

    def test_descriptive_name_present_even_if_none(self):
        # node_type key present with None value: should fall through to 'type'
        assert _resolve_type_field(
            {"node_type": None, "type": "decision"},
            "node_type", "type", default="concept",
        ) == "decision"

    def test_observation_aliases(self):
        assert _resolve_type_field(
            {"obs_type": "anomaly"}, "obs_type", "type", default="measurement",
        ) == "anomaly"

    def test_edge_aliases(self):
        assert _resolve_type_field(
            {"edge_type": "CAUSES"}, "edge_type", "type", default="",
        ) == "CAUSES"

    def test_empty_string_falls_through(self):
        # Empty string treated as 'not provided' — fall through to next alias.
        assert _resolve_type_field(
            {"node_type": "", "type": "decision"},
            "node_type", "type", default="concept",
        ) == "decision"

    def test_default_none_when_all_missing(self):
        assert _resolve_type_field({}, "node_type", "type") is None


# ── HTTP integration tests (live test server) ───────────────────────────────


def _start_server(store):
    """Start a no-auth test server on a random port, return (port, server, thread)."""
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


def _http(method: str, port: int, path: str, body: dict | None = None) -> tuple[int, object]:
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
def http_server(tmp_path):
    db_path = str(tmp_path / "alias.duckdb")
    store = OhmStore(db_path=db_path, agent_name="test_agent")
    port, server, thread = _start_server(store)
    yield port, store
    server.shutdown()
    thread.join(timeout=2)
    store.close()


@pytest.mark.xdist_group("server")
class TestNodeTypeAlias:
    """POST /node must accept both 'type' and 'node_type'."""

    def test_node_type_creates_decision_node(self, http_server):
        """Live daemon bug repro: POST /node with node_type=decision must
        create a node with type='decision', not the default 'concept'."""
        port, store = http_server
        # First create an anchor so decision (a must-have-edge type) can cross-link.
        _http("POST", port, "/node", {
            "id": "anchor_for_decision",
            "label": "Anchor",
            "node_type": "concept",
        })
        status, data = _http("POST", port, "/node", {
            "id": "decision_via_alias",
            "label": "Decision via node_type alias",
            "node_type": "decision",
            "utility_scale": 0.5,
            "connects_to": ["anchor_for_decision"],
        })
        assert status in (200, 201), data
        # Verify the stored type is 'decision', not 'concept'
        row = store.read_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            ["decision_via_alias"],
        ).fetchone()
        assert row[0] == "decision"

    def test_legacy_type_still_works(self, http_server):
        """Backward compat: clients sending 'type' keep working."""
        port, store = http_server
        status, data = _http("POST", port, "/node", {
            "id": "concept_legacy",
            "label": "Concept via type",
            "type": "concept",
        })
        assert status in (200, 201), data
        row = store.read_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            ["concept_legacy"],
        ).fetchone()
        assert row[0] == "concept"

    def test_descriptive_wins_when_both_present(self, http_server):
        port, store = http_server
        status, _ = _http("POST", port, "/node", {
            "id": "node_with_both",
            "label": "Both fields",
            "type": "concept",
            "node_type": "decision",
            "connects_to": ["anchor_for_decision"],
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            ["node_with_both"],
        ).fetchone()
        assert row[0] == "decision"

    def test_default_when_neither_present(self, http_server):
        port, store = http_server
        status, _ = _http("POST", port, "/node", {
            "id": "node_default",
            "label": "Default",
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            ["node_default"],
        ).fetchone()
        assert row[0] == "concept"

    def test_find_or_create_accepts_node_type(self, http_server):
        """The find_or_create endpoint must also accept the alias."""
        port, store = http_server
        status, data = _http("POST", port, "/node/find_or_create", {
            "label": "Find or create via alias",
            "node_type": "concept",
        })
        assert status in (200, 201), data
        # find_or_create auto-generates id from label+type
        node_id = data["id"]
        row = store.read_conn.execute(
            "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        assert row[0] == "concept"


@pytest.mark.xdist_group("server")
class TestEdgeTypeAlias:
    """POST /edge must accept both 'type' and 'edge_type'."""

    def test_edge_type_creates_causes_edge(self, http_server):
        port, store = http_server
        # Two anchor concepts first
        _http("POST", port, "/node", {"id": "src_for_edge", "label": "Src", "type": "concept"})
        _http("POST", port, "/node", {"id": "dst_for_edge", "label": "Dst", "type": "concept"})
        status, data = _http("POST", port, "/edge", {
            "from": "src_for_edge",
            "to": "dst_for_edge",
            "layer": "L3",
            "edge_type": "CAUSES",
            "created_by": "test",
        })
        assert status in (200, 201), data
        row = store.read_conn.execute(
            "SELECT edge_type FROM ohm_edges WHERE from_node = ? AND to_node = ? AND deleted_at IS NULL",
            ["src_for_edge", "dst_for_edge"],
        ).fetchone()
        assert row[0] == "CAUSES"

    def test_legacy_type_still_works(self, http_server):
        port, store = http_server
        _http("POST", port, "/node", {"id": "src2", "label": "Src2", "type": "concept"})
        _http("POST", port, "/node", {"id": "dst2", "label": "Dst2", "type": "concept"})
        status, _ = _http("POST", port, "/edge", {
            "from": "src2",
            "to": "dst2",
            "layer": "L3",
            "type": "CAUSES",
            "created_by": "test",
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT edge_type FROM ohm_edges WHERE from_node = ? AND to_node = ? AND deleted_at IS NULL",
            ["src2", "dst2"],
        ).fetchone()
        assert row[0] == "CAUSES"


@pytest.mark.xdist_group("server")
class TestObsTypeAlias:
    """POST /observe/{id} must accept both 'type' and 'obs_type'."""

    def test_obs_type_creates_anomaly_observation(self, http_server):
        port, store = http_server
        _http("POST", port, "/node", {"id": "obs_target", "label": "Target", "type": "concept"})
        status, _ = _http("POST", port, "/observe/obs_target", {
            "obs_type": "anomaly",
            "value": 0.95,
            "sigma": 0.05,
            "created_by": "test",
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT type FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            ["obs_target"],
        ).fetchone()
        assert row[0] == "anomaly"

    def test_legacy_type_still_works(self, http_server):
        port, store = http_server
        _http("POST", port, "/node", {"id": "obs_target2", "label": "T2", "type": "concept"})
        status, _ = _http("POST", port, "/observe/obs_target2", {
            "type": "anomaly",
            "value": 0.95,
            "sigma": 0.05,
            "created_by": "test",
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT type FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            ["obs_target2"],
        ).fetchone()
        assert row[0] == "anomaly"

    def test_default_when_neither_present(self, http_server):
        port, store = http_server
        _http("POST", port, "/node", {"id": "obs_target3", "label": "T3", "type": "concept"})
        status, _ = _http("POST", port, "/observe/obs_target3", {
            "value": 0.5,
            "created_by": "test",
        })
        assert status in (200, 201)
        row = store.read_conn.execute(
            "SELECT type FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
            ["obs_target3"],
        ).fetchone()
        assert row[0] == "measurement"
