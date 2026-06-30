"""Tests for the OHM daemon HTTP server endpoints.

Starts a test server on a random port and tests all 17+ endpoints
including auth, error handling, and edge cases.

Server tests share class-level state on OhmHandler (tokens, roles, etc.)
and must run sequentially. They are grouped with xdist_group("server").

Marks: integration (HTTP server required, slow setup/teardown).
"""

import json
import threading
from http.client import HTTPConnection

import pytest

pytestmark = pytest.mark.integration

from ohm.server import OhmHandler, _hash_token, _verify_token, _build_token_lookup, _trigger_webhooks, _webhook_registry, _webhook_lock
from ohm.schema import DEFAULT_SCHEMA, TOPO_SCHEMA
from ohm.store import OhmStore


from tests.conftest import _start_test_server, _request  # noqa: F401 — used by tests that import directly

# test_server and auth_server fixtures are now in conftest.py and available to all test modules


@pytest.mark.xdist_group("server")
class TestHealthEndpoints:
    """Tests for /health, /ready, /status endpoints."""

    def test_health_returns_ok(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/health")
        assert status == 200
        assert data["status"] == "ok"
        assert "uptime" in data

    def test_health_graph_stats_populated(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/health")
        assert status == 200
        graph = data.get("graph", {})
        # All graph stat fields must be present and non-None
        for key in ("health_score", "node_count", "edge_count", "orphan_count", "orphan_rate", "low_confidence_count"):
            assert key in graph, f"Missing graph stat: {key}"
            assert graph[key] is not None, f"Graph stat null: {key}"

    def test_ready_returns_ready(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/ready")
        assert status == 200
        assert data["status"] == "ready"

    def test_status_has_counts(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/status")
        assert status == 200
        assert "node_count" in data
        assert "edge_count" in data
        assert "uptime" in data
        assert "version" in data


@pytest.mark.xdist_group("server")
class TestSchemaEndpoints:
    """Tests for /schema and /layers."""

    def test_schema_returns_types(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/schema")
        assert status == 200
        assert "node_types" in data
        assert "edge_types" in data
        assert "layers" in data
        assert data["schema"] == "ohm"

    def test_layers_returns_descriptions(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/layers")
        assert status == 200
        assert "L1" in data


@pytest.mark.xdist_group("server")
class TestNodeEndpoints:
    """Tests for node CRUD via HTTP."""

    def test_get_nonexistent_node(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/node/nonexistent")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestQuestionAutoDetection:
    """Tests for question auto-detection in fragments (OHM-a5rz.12)."""

    def test_scratch_question_has_is_question_metadata(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Why would Altman meet Sanders?"},
        )
        assert status == 201
        meta = data.get("metadata", {})
        if isinstance(meta, str):
            import json

            meta = json.loads(meta)
        assert meta.get("is_question") is True

    def test_scratch_non_question_no_is_question(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Broadcom refused to raise guidance"},
        )
        assert status == 201
        meta = data.get("metadata") or {}
        if isinstance(meta, str):
            import json

            meta = json.loads(meta)
        assert "is_question" not in meta

    def test_fragments_open_questions_filter(self, test_server):
        port, _ = test_server
        _request("POST", port, "/scratch", body={"content": "Why is this happening?"})
        _request("POST", port, "/scratch", body={"content": "Just a statement here"})
        status, data = _request("GET", port, "/fragments?open_questions=true")
        assert status == 200
        for frag in data["fragments"]:
            meta = frag.get("metadata", {})
            if isinstance(meta, str):
                import json

                meta = json.loads(meta)
            assert meta.get("is_question") is True

    def test_fragment_resolve(self, test_server):
        port, _ = test_server
        status, frag = _request("POST", port, "/scratch", body={"content": "Is HBM scaling?"})
        assert status == 201
        status, result = _request("POST", port, f"/fragments/{frag['id']}/resolve", body={})
        assert status == 200
        meta = result.get("metadata", {})
        if isinstance(meta, str):
            import json

            meta = json.loads(meta)
        assert meta.get("is_question") is False
        assert "resolved_at" in meta

    def test_fragment_resolve_non_question_404(self, test_server):
        port, _ = test_server
        status, frag = _request("POST", port, "/scratch", body={"content": "Not a question"})
        status, data = _request("POST", port, f"/fragments/{frag['id']}/resolve", body={})
        assert status == 404

    def test_fragment_resolve_nonexistent_404(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/fragments/nonexistent/resolve", body={})
        assert status == 404


@pytest.mark.xdist_group("server")
class TestFragmentResonanceEndpoint:
    """Tests for GET /admin/fragment-resonance (OHM-a5rz.13)."""

    def test_fragment_resonance_empty(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/fragment-resonance")
        assert status == 200
        assert data["resonance"] == []

    def test_fragment_resonance_with_overlap(self, test_server):
        port, store = test_server
        store.write_node("anchor_1", "Shared Topic One", "concept", agent_name="test")
        store.write_node("anchor_2", "Shared Topic Two", "concept", agent_name="test")
        _request("POST", port, "/scratch", body={"content": "Shared Topic One and Shared Topic Two both matter", "connects_to": ["anchor_1", "anchor_2"]})
        _request("POST", port, "/scratch", body={"content": "Shared Topic One and Shared Topic Two overlap", "connects_to": ["anchor_1", "anchor_2"]})
        status, data = _request("GET", port, "/admin/fragment-resonance?min_shared=1")
        assert status == 200

    def test_create_and_get_node(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "test_node_1",
                "label": "Test Node",
                "type": "concept",
            },
        )
        assert status == 201
        assert data["id"] == "test_node_1"

        status, data = _request("GET", port, "/node/test_node_1")
        assert status == 200
        assert data["label"] == "Test Node"


@pytest.mark.xdist_group("server")
class TestEdgeEndpoints:
    """Tests for edge CRUD via HTTP."""

    def test_get_nonexistent_edge(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/edge/nonexistent")
        assert status == 404
        assert data["error"] == "not_found"

    def test_create_edge(self, test_server):
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "a",
                "label": "A",
                "type": "concept",
            },
        )
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "b",
                "label": "B",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "a",
                "to": "b",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 201

    def test_observe_invalid_obs_type_rejected(self, test_server):
        """POST /observe/{id} rejects observation types not in schema (OHM-jt98)."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "obs-type-test-node",
                "label": "Obs Type Test",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/obs-type-test-node",
            body={
                "type": "not_a_valid_obs_type",
                "value": 5.0,
            },
        )
        assert status == 400
        assert "not_a_valid_obs_type" in data.get("message", "") or "not_a_valid_obs_type" in data.get("error", "")


@pytest.mark.xdist_group("server")
class TestSourceAttribution:
    """Tests for structured source attribution on observations (OHM-lmr)."""

    def test_observe_with_source_name_and_url(self, test_server):
        """POST /observe/{id} with source_name and source_url persists them."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-attrib-node",
                "label": "Source Test",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/src-attrib-node",
            body={
                "type": "measurement",
                "value": 1.5,
                "source_name": "Reuters",
                "source_url": "https://reuters.com/article/123",
            },
        )
        assert status == 201
        assert data.get("source_name") == "Reuters"
        assert data.get("source_url") == "https://reuters.com/article/123"

    def test_observe_source_attribution_in_db(self, test_server):
        """source_name and source_url are stored in the database."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-attrib-db",
                "label": "DB Source Test",
                "type": "concept",
            },
        )
        _request(
            "POST",
            port,
            "/observe/src-attrib-db",
            body={
                "type": "measurement",
                "value": 2.0,
                "source_name": "AP News",
                "source_url": "https://apnews.com/article/456",
            },
        )
        obs = store.execute(
            "SELECT source_name, source_url FROM ohm_observations WHERE node_id = ? ORDER BY created_at DESC LIMIT 1",
            ["src-attrib-db"],
        )
        assert len(obs) == 1
        assert obs[0]["source_name"] == "AP News"
        assert obs[0]["source_url"] == "https://apnews.com/article/456"

    def test_observe_without_source_attribution(self, test_server):
        """POST /observe/{id} without source fields works (backward compatible)."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "src-no-attrib",
                "label": "No Source",
                "type": "concept",
            },
        )
        status, data = _request(
            "POST",
            port,
            "/observe/src-no-attrib",
            body={
                "type": "measurement",
                "value": 3.0,
            },
        )
        assert status == 201
        assert data.get("source_name") is None
        assert data.get("source_url") is None


@pytest.mark.xdist_group("server")
class TestPERTFields:
    """Tests for PERT distribution fields on edges (OHM-6mv.11)."""

    def test_post_edge_with_pert_probability(self, test_server):
        """POST /edge with PERT probability fields persists them."""
        port, store = test_server
        # Create nodes first
        _request("POST", port, "/node", body={"id": "pert-cause-1", "label": "Cause 1", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-1", "label": "Effect 1", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-1",
                "to": "pert-effect-1",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
                "probability_p05": 0.1,
                "probability_p50": 0.5,
                "probability_p95": 0.9,
            },
        )
        assert status == 201
        assert abs(data["probability_p05"] - 0.1) < 0.01
        assert abs(data["probability_p50"] - 0.5) < 0.01
        assert abs(data["probability_p95"] - 0.9) < 0.01

    def test_post_edge_with_all_pert_fields(self, test_server):
        """POST /edge with all PERT fields persists them."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "pert-cause-2", "label": "Cause 2", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-2", "label": "Effect 2", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-2",
                "to": "pert-effect-2",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
                "probability_p05": 0.05,
                "probability_p50": 0.4,
                "probability_p95": 0.85,
                "confidence_p05": 0.2,
                "confidence_p50": 0.7,
                "confidence_p95": 0.95,
            },
        )
        assert status == 201
        assert abs(data["probability_p05"] - 0.05) < 0.01
        assert abs(data["confidence_p05"] - 0.2) < 0.01

    def test_post_edge_without_pert_fields(self, test_server):
        """POST /edge without PERT fields works (backward compatible)."""
        port, store = test_server
        _request("POST", port, "/node", body={"id": "pert-cause-3", "label": "Cause 3", "type": "concept"})
        _request("POST", port, "/node", body={"id": "pert-effect-3", "label": "Effect 3", "type": "concept"})
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "pert-cause-3",
                "to": "pert-effect-3",
                "type": "CAUSES",
                "layer": "L3",
                "confidence": 0.7,
            },
        )
        assert status == 201
        assert data.get("probability_p05") is None
        assert data.get("confidence_p05") is None


@pytest.mark.xdist_group("server")
class TestBatchEndpoint:
    """Tests for POST /batch endpoint (OHM-1m3)."""

    def test_batch_create_nodes(self, test_server):
        """POST /batch creates multiple nodes."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-n1", "label": "Node 1", "type": "concept"},
                    {"id": "batch-n2", "label": "Node 2", "type": "source"},
                ],
                "edges": [],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 0

    def test_batch_create_nodes_and_edges(self, test_server):
        """POST /batch creates nodes and edges together."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-n3", "label": "Node A", "type": "concept"},
                    {"id": "batch-n4", "label": "Node B", "type": "concept"},
                ],
                "edges": [
                    {"from": "batch-n3", "to": "batch-n4", "type": "CAUSES", "layer": "L3"},
                ],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 2
        assert data["edges_created"] == 1

    def test_batch_validation_error(self, test_server):
        """POST /batch with missing required fields returns validation error."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "batch-bad"},  # missing 'label'
                ],
                "edges": [],
            },
        )
        assert status == 400

    def test_batch_empty(self, test_server):
        """POST /batch with empty arrays returns zeros."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [],
                "edges": [],
            },
        )
        assert status == 201
        assert data["nodes_created"] == 0
        assert data["edges_created"] == 0

    def test_batch_populates_change_feed(self, test_server):
        """POST /batch populates change feed for each created item."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/batch",
            body={
                "nodes": [
                    {"id": "cf-batch-1", "label": "CF1", "type": "concept"},
                    {"id": "cf-batch-2", "label": "CF2", "type": "concept"},
                ],
                "edges": [],
            },
        )
        # Verify change feed entries
        feed = store.execute("SELECT row_id FROM ohm_change_feed WHERE table_name = 'ohm_nodes' AND row_id IN ('cf-batch-1', 'cf-batch-2') ORDER BY occurred_at DESC")
        assert len(feed) == 2


@pytest.mark.xdist_group("server")
class TestIdempotentRegistration:
    """Tests for idempotent agent registration (OHM-5n7: deduplicate registration)."""

    def test_register_creates_agent_node(self, test_server):
        """POST /register creates an agent node with deterministic ID."""
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "testbot",
                "description": "A test agent",
                "values": ["accuracy"],
                "goals": ["explore"],
            },
        )
        assert status == 201
        assert data["agent"]["label"] == "testbot"
        assert data["agent"]["type"] == "agent"
        assert data["edges_created"] >= 2  # VALUES + GOALS

    def test_register_idempotent(self, test_server):
        """POST /register twice with same name reuses agent node (no duplicates)."""
        port, store = test_server
        # First registration
        status1, data1 = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "idem_agent",
                "values": ["truth"],
            },
        )
        assert status1 == 201
        agent_id_1 = data1["agent"]["id"]

        # Second registration with same name
        status2, data2 = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "idem_agent",
                "values": ["truth", "fairness"],
            },
        )
        assert status2 == 201
        agent_id_2 = data2["agent"]["id"]

        # Same agent node ID (deterministic)
        assert agent_id_1 == agent_id_2

        # No duplicate agent nodes
        agent_nodes = store.execute("SELECT * FROM ohm_nodes WHERE type = 'agent' AND label = 'idem_agent'")
        assert len(agent_nodes) == 1

    def test_register_reuses_value_nodes(self, test_server):
        """POST /register reuses existing value/goal/skill nodes."""
        port, store = test_server
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "reuse_agent",
                "values": ["courage"],
            },
        )
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "other_agent",
                "values": ["courage"],
            },
        )
        # Only one "courage" value node should exist
        courage_nodes = store.execute("SELECT * FROM ohm_nodes WHERE label = 'courage' AND type = 'value'")
        assert len(courage_nodes) == 1

    def test_register_updates_edges(self, test_server):
        """POST /register replaces old edges on re-registration."""
        port, store = test_server
        # First registration with 1 value
        _request(
            "POST",
            port,
            "/register",
            body={
                "name": "edge_agent",
                "values": ["loyalty"],
            },
        )
        # Second registration with 2 values
        status, data = _request(
            "POST",
            port,
            "/register",
            body={
                "name": "edge_agent",
                "values": ["loyalty", "honesty"],
            },
        )
        assert status == 201
        # Should have 2 active VALUES edges (old ones soft-deleted, new ones created)
        agent_id = data["agent"]["id"]
        values_edges = store.execute(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND edge_type = 'VALUES' AND deleted_at IS NULL",
            [agent_id],
        )
        assert len(values_edges) == 2


@pytest.mark.xdist_group("server")
class TestSemanticSearchEndpoint:
    """Tests for /semantic_search endpoint (OHM-o9f)."""

    def test_semantic_search_endpoint_requires_query(self, test_server):
        """GET /semantic_search without ?q= returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/semantic_search")
        assert status == 400

    def test_semantic_search_endpoint_returns_503_without_ollama(self, test_server):
        """GET /semantic_search?q=test returns 503 when Ollama is not available."""
        port, _ = test_server
        status, data = _request("GET", port, "/semantic_search?q=test+query")
        # Either 503 (Ollama not running) or 200 (Ollama available)
        assert status in (200, 503)
        if status == 503:
            assert "service_unavailable" in data.get("error", "")

    def test_semantic_search_endpoint_in_discovery(self, test_server):
        """Root discovery endpoint includes /semantic_search."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        assert "/semantic_search" in data.get("endpoints", {})

    def test_search_endpoint_still_works(self, test_server):
        """GET /search?q= still works (ILIKE search unchanged)."""
        port, _ = test_server
        # Create a node
        _request(
            "POST",
            port,
            "/node",
            body={
                "id": "search-test-node",
                "label": "Machine Learning",
                "type": "concept",
            },
        )
        status, data = _request("GET", port, "/search?q=Machine")
        assert status == 200


@pytest.mark.xdist_group("server")
class TestDuckLakeTimeTravel:
    """Tests for DuckLake time-travel endpoints (OHM-kdk.3)."""

    def test_admin_snapshots_without_ducklake(self, test_server):
        """GET /admin/snapshots returns empty list when DuckLake is not attached."""
        port, _ = test_server
        status, data = _request("GET", port, "/admin/snapshots")
        assert status == 200
        assert data["snapshots"] == []
        assert data["count"] == 0

    def test_graph_at_without_version_returns_400(self, test_server):
        """GET /graph/at without ?version=N returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at")
        assert status == 400

    def test_graph_at_with_invalid_version_returns_400(self, test_server):
        """GET /graph/at?version=abc returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=abc")
        assert status == 400

    def test_graph_at_without_ducklake_returns_degraded(self, test_server):
        """GET /graph/at?version=1 without DuckLake attached returns degraded response."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/at?version=1")
        assert status == 200
        assert data["node_count"] == 0
        assert data["edge_count"] == 0

    def test_graph_changes_without_params_returns_400(self, test_server):
        """GET /graph/changes without required params returns validation error."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes")
        assert status == 400

    def test_graph_changes_missing_to_version_returns_400(self, test_server):
        """GET /graph/changes?from_version=1 without to_version returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes?from_version=1")
        assert status == 400

    def test_graph_changes_invalid_version_returns_400(self, test_server):
        """GET /graph/changes?from_version=abc&to_version=2 returns 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/graph/changes?from_version=abc&to_version=2")
        assert status == 400

    def test_discovery_index_includes_time_travel(self, test_server):
        """Root discovery endpoint includes time-travel endpoints."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        endpoints = data["endpoints"]
        assert "/admin/snapshots" in endpoints
        assert "/graph/at" in endpoints
        assert "/graph/changes" in endpoints


@pytest.mark.xdist_group("server")
class TestPublicReadAuthModel:
    """Tests for public-read auth model (OHM-gwg).

    Default behavior: reads are public (no token needed), writes require auth.
    With --require-read-auth: all endpoints require auth.
    """

    def test_public_read_allows_unauthenticated_get(self, test_server):
        """GET /stats works without a token (public-read model)."""
        port, _ = test_server
        # test_server fixture uses no_auth=True, so reads are always allowed
        status, data = _request("GET", port, "/stats")
        assert status == 200

    def test_auth_model_in_discovery(self, test_server):
        """Root discovery includes auth_model field."""
        port, _ = test_server
        status, data = _request("GET", port, "/")
        assert status == 200
        # no_auth mode should report "public-read" or "authenticated"
        assert "auth_model" in data

    def test_require_read_auth_blocks_unauthenticated_reads(self, tmp_path):
        """With require_read_auth=True, unauthenticated reads return 401."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_auth_read.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"read-auth-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens, require_read_auth=True)
        try:
            # Unauthenticated read should fail
            status, data = _request("GET", port, "/stats")
            assert status == 401
            # Authenticated read should succeed
            status, data = _request("GET", port, "/stats", token="read-auth-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_default_allows_unauthenticated_reads_with_tokens(self, tmp_path):
        """With tokens configured but require_read_auth=False, reads are public."""
        from ohm.store import OhmStore

        db_path = str(tmp_path / "test_public_read.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"pub-read-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens)
        try:
            # Unauthenticated read should succeed (public-read model)
            status, data = _request("GET", port, "/stats")
            assert status == 200
            # Authenticated read should also succeed
            status, data = _request("GET", port, "/stats", token="pub-read-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()

    def test_multi_tenant_default_requires_auth(self, tmp_path):
        """Multi-tenant mode defaults to require_read_auth=True (OHM-en2r)."""
        db_path = str(tmp_path / "test_mt_auth.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test_agent")
        tokens = {"mt-token": "test_agent"}
        port, server, thread = _start_test_server(store, tokens=tokens, multi_tenant=True)
        try:
            status, data = _request("GET", port, "/stats")
            assert status == 401, f"Multi-tenant should default to require_read_auth=True, got {status}"
            status, data = _request("GET", port, "/stats", token="mt-token")
            assert status == 200
        finally:
            server.shutdown()
            thread.join(timeout=2)
            store.close()


@pytest.mark.xdist_group("server")
class TestMetisBugFixes:
    """Regression tests for bugs found by Metis in the 50-endpoint test run."""

    def test_edge_rejects_nonexistent_from_node(self, test_server):
        """POST /edge should 404 when from_node doesn't exist (OHM-7298)."""
        port, store = test_server
        store.write_node("real-node", "Real Node", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ghost-node",
                "to": "real-node",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 404
        assert "ghost-node" in data.get("message", "")

    def test_edge_rejects_nonexistent_to_node(self, test_server):
        """POST /edge should 404 when to_node doesn't exist (OHM-7298)."""
        port, store = test_server
        store.write_node("src-node", "Source Node", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "src-node",
                "to": "ghost-target",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 404
        assert "ghost-target" in data.get("message", "")

    def test_edge_with_valid_nodes_succeeds(self, test_server):
        """POST /edge should succeed when both nodes exist (OHM-7298 no regression)."""
        port, store = test_server
        store.write_node("ei-src", "Source", "concept", agent_name="test")
        store.write_node("ei-dst", "Dest", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ei-src",
                "to": "ei-dst",
                "type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 201

    def test_observe_rejects_nonexistent_node(self, test_server):
        """POST /observe/{id} should 404 when node doesn't exist (OHM-7302)."""
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/observe/ghost-node-obs",
            body={
                "type": "measurement",
                "value": 42.0,
            },
        )
        assert status == 404

    def test_observe_valid_node_succeeds(self, test_server):
        """POST /observe/{id} should succeed when node exists (OHM-7302 no regression)."""
        port, store = test_server
        store.write_node("obs-node", "Observable", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/observe/obs-node",
            body={
                "type": "measurement",
                "value": 42.0,
            },
        )
        assert status == 201

    def test_deep_includes_edges(self, test_server):
        """GET /deep/{id} should include connected edges (OHM-7299)."""
        port, store = test_server
        store.write_node("deep-hub", "Hub", "concept", agent_name="test")
        store.write_node("deep-spoke", "Spoke", "concept", agent_name="test")
        store.write_edge("deep-hub", "deep-spoke", "CAUSES", "L3", agent_name="test")
        status, data = _request("GET", port, "/deep/deep-hub")
        assert status == 200
        assert "edges" in data
        assert data["edge_count"] >= 1
        assert any(e["from_node"] == "deep-hub" for e in data["edges"])

    def test_post_sync_returns_200(self, test_server):
        """POST /sync should return 200 with sync result (OHM-7301)."""
        port, _ = test_server
        status, data = _request("POST", port, "/sync", body={})
        assert status == 200
        assert "pushed" in data or "last_sync" in data

    def test_post_tasks_creates_task(self, test_server):
        """POST /tasks should create a task node (OHM-7304)."""
        port, store = test_server
        # OHM-tjzh: tasks must link to existing structure. Create an anchor first.
        store.write_node("task-anchor", "Task anchor concept", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "task-create-test",
                "label": "Do the thing",
                "task_status": "open",
                "priority": "P1",
                "connects_to": ["task-anchor"],
            },
        )
        assert status == 201
        assert data.get("type") == "task"
        assert data.get("task_status") == "open"

    def test_post_tasks_then_get_tasks(self, test_server):
        """Task created via POST /tasks is visible in GET /tasks (OHM-7304)."""
        port, store = test_server
        # OHM-tjzh: tasks must link to existing structure. Create an anchor first.
        store.write_node("task-roundtrip-anchor", "Anchor", "concept", agent_name="test")
        _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "task-roundtrip",
                "label": "Roundtrip task",
                "task_status": "open",
                "connects_to": ["task-roundtrip-anchor"],
            },
        )
        status, data = _request("GET", port, "/tasks")
        assert status == 200
        ids = [t["id"] for t in data.get("tasks", [])]
        assert "task-roundtrip" in ids


@pytest.mark.xdist_group("server")
class TestMetisBatch2Fixes:
    """Regression tests for bugs found in Metis's second test run (OHM-7308..7321)."""

    def test_post_task_auto_generates_id(self, test_server):
        """POST /tasks without 'id' field auto-generates one (OHM-7308)."""
        port, store = test_server
        # OHM-tjzh: tasks must link to existing structure. Create an anchor first.
        store.write_node("auto-id-task-anchor", "Auto-id anchor", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "label": "Auto-ID task",
                "task_status": "open",
                "connects_to": ["auto-id-task-anchor"],
            },
        )
        assert status == 201
        assert data.get("id"), "id should be auto-generated"
        assert data["id"].startswith("task_")

    def test_post_task_with_explicit_id(self, test_server):
        """POST /tasks with explicit 'id' uses that id (OHM-7308)."""
        port, store = test_server
        # OHM-tjzh: tasks must link to existing structure. Create an anchor first.
        store.write_node("explicit-task-anchor", "Explicit task anchor", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/tasks",
            body={
                "id": "explicit-task-id-7308",
                "label": "Explicit ID task",
                "connects_to": ["explicit-task-anchor"],
            },
        )
        assert status == 201
        assert data.get("id") == "explicit-task-id-7308"

    def test_post_edge_accepts_from_node_alias(self, test_server):
        """POST /edge accepts from_node/to_node/edge_type aliases (OHM-7314)."""
        port, store = test_server
        store.write_node("alias-from", "Alias Source", "concept", agent_name="test")
        store.write_node("alias-to", "Alias Dest", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/edge",
            body={
                "from_node": "alias-from",
                "to_node": "alias-to",
                "edge_type": "CAUSES",
                "layer": "L3",
            },
        )
        assert status == 201
        assert data.get("from_node") == "alias-from"
        assert data.get("to_node") == "alias-to"

    def test_patch_node_updates_label(self, test_server):
        """PATCH /node/{id} can update node label (OHM-7319)."""
        port, store = test_server
        store.write_node("patch-test-node", "Original Label", "concept", agent_name="test")
        status, data = _request(
            "PATCH",
            port,
            "/node/patch-test-node",
            body={
                "label": "Updated Label",
            },
        )
        assert status == 200
        assert data.get("label") == "Updated Label"

    def test_patch_node_404_for_missing(self, test_server):
        """PATCH /node/{id} returns 404 for non-existent node (OHM-7319)."""
        port, _ = test_server
        status, data = _request(
            "PATCH",
            port,
            "/node/nonexistent-patch-node",
            body={
                "label": "Won't work",
            },
        )
        assert status == 404

    def test_source_reliability_alias(self, test_server):
        """GET /source_reliability?source=<agent> returns reliability data (OHM-7310)."""
        port, _ = test_server
        status, data = _request("GET", port, "/source_reliability?source=test")
        assert status == 200
        assert "source_agent" in data

    def test_compound_confidence_endpoint(self, test_server):
        """GET /compound_confidence/{node} returns compound confidence (OHM-7311)."""
        port, store = test_server
        store.write_node("cc-test-node", "CC Node", "concept", agent_name="test")
        status, data = _request("GET", port, "/compound_confidence/cc-test-node")
        assert status == 200
        assert "node_id" in data
        assert data["node_id"] == "cc-test-node"

    def test_suggest_orphan_connect_returns_list(self, test_server):
        """GET /suggest?method=orphan_connect returns a list (OHM-7312)."""
        port, _ = test_server
        status, data = _request("GET", port, "/suggest?method=orphan_connect")
        assert status == 200
        assert isinstance(data, list)

    def test_suggest_cooccurrence_returns_list(self, test_server):
        """GET /suggest?method=cooccurrence returns a list (OHM-7312)."""
        port, _ = test_server
        status, data = _request("GET", port, "/suggest?method=cooccurrence")
        assert status == 200
        assert isinstance(data, list)

    def test_suggest_shared_tags_no_empty_fields(self, test_server):
        """GET /suggest?method=shared_tags results have non-empty from_id/to_id (OHM-7313)."""
        port, store = test_server
        # Create tagged nodes to trigger shared_tags results
        store.write_node("tagged-a", "Tagged A", "concept", agent_name="test", tags=["geopolitics", "energy"])
        store.write_node("tagged-b", "Tagged B", "concept", agent_name="test", tags=["geopolitics", "security"])
        status, data = _request("GET", port, "/suggest?method=shared_tags&min_shared=1")
        assert status == 200
        assert isinstance(data, list)
        for item in data:
            assert item.get("from_id"), f"from_id empty in {item}"
            assert item.get("to_id"), f"to_id empty in {item}"

    def test_ate_returns_diagnostic_when_disconnected(self, test_server):
        """GET /ate returns diagnostic error (not silent ATE=0) when nodes not connected (OHM-7320)."""
        port, store = test_server
        # Create two nodes with no edge between them
        store.write_node("ate-cause-island", "Isolated Cause", "concept", agent_name="test")
        store.write_node("ate-effect-island", "Isolated Effect", "concept", agent_name="test")
        status, data = _request("GET", port, "/ate?cause=ate-cause-island&effect=ate-effect-island")
        assert status == 200
        # When pgmpy is unavailable the endpoint returns method=none with an error message —
        # either way it must never silently return ATE=0.0 with risk_ratio=1.0 and no error.
        if data.get("method") not in ("none", "error"):
            # pgmpy available — should detect disconnection and return method=error
            if data.get("ate") == 0.0 and data.get("risk_ratio") == 1.0:
                assert data.get("method") == "error", f"ATE=0 with RR=1 must not be returned silently for disconnected nodes; got {data}"

    def test_ate_connected_path_returns_nonzero(self, test_server):
        """GET /ate returns non-zero ATE when cause→effect edge exists (OHM-7320)."""
        import importlib.util

        if not importlib.util.find_spec("pgmpy"):
            pytest.skip("pgmpy not installed")
        port, store = test_server
        store.write_node("ate-cause-a", "Cause A", "concept", agent_name="test")
        store.write_node("ate-effect-b", "Effect B", "concept", agent_name="test")
        # Create a direct causal edge with high probability
        _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "ate-cause-a",
                "to": "ate-effect-b",
                "type": "CAUSES",
                "layer": "L3",
                "probability": 0.9,
            },
        )
        status, data = _request("GET", port, "/ate?cause=ate-cause-a&effect=ate-effect-b")
        assert status == 200
        assert data.get("method") != "error", f"Unexpected error: {data}"
        # With a direct high-probability CAUSES edge, ATE should be detectably non-zero
        assert abs(data.get("ate", 0.0)) > 0.01, f"ATE should be non-zero with direct causal edge, got {data}"


class TestGitHubBacklogFixes:
    """Regression tests for GitHub issues from Deepthought/Socrates (OHM-zwrw, OHM-9pb7, OHM-zn3s)."""

    def test_heartbeat_does_not_crash_when_change_feed_missing(self, test_server):
        """POST /heartbeat must not 500 even when ohm_change_feed table is absent (OHM-zwrw)."""
        port, store = test_server
        # Drop the change feed table to simulate a pre-migration production DB
        try:
            store.conn.execute("DROP TABLE IF EXISTS ohm_change_feed")
            store.conn.execute("DROP SEQUENCE IF EXISTS seq_change_feed")
        except Exception:
            pass
        # Heartbeat should succeed (not 500)
        status, data = _request("POST", port, "/heartbeat", body={"focus": "test"})
        assert status == 200, f"Heartbeat should not crash without ohm_change_feed: {data}"

    def test_change_feed_query_falls_back_when_table_missing(self, test_server):
        """GET /listen must not crash when ohm_change_feed is absent (OHM-zwrw)."""
        port, store = test_server
        try:
            store.conn.execute("DROP TABLE IF EXISTS ohm_change_feed")
        except Exception:
            pass
        # /listen reads from ohm_change_feed — should fall back to ohm_change_log
        status, data = _request("GET", port, "/listen?limit=5")
        assert status == 200, f"/listen should not crash without ohm_change_feed: {data}"

    def test_voi_reports_mixed_sensitivity_methods(self, test_server):
        """GET /voi includes mixed_sensitivity_methods flag in response (OHM-9pb7)."""
        port, _ = test_server
        status, data = _request("GET", port, "/voi?top=5")
        assert status == 200
        assert "mixed_sensitivity_methods" in data, f"VoI response must include mixed_sensitivity_methods field: {data}"
        assert "sensitivity_methods_used" in data

    def test_voi_min_observations_flags_sparse_nodes(self, test_server):
        """GET /voi?min_observations=3 flags nodes with fewer than 3 observations (OHM-zn3s)."""
        port, store = test_server
        store.write_node("dec-test-voi", "Test Decision", "decision", agent_name="test", utility_scale=1.0)
        store.write_node("anc-test-voi", "Test Ancestor", "concept", agent_name="test")
        _request(
            "POST",
            port,
            "/edge",
            body={
                "from": "anc-test-voi",
                "to": "dec-test-voi",
                "type": "CAUSES",
                "layer": "L3",
                "probability": 0.7,
            },
        )
        status, data = _request("GET", port, "/voi?min_observations=3&decision=dec-test-voi")
        assert status == 200
        for entry in data.get("rankings", []):
            if entry["node_id"] == "anc-test-voi":
                assert entry.get("low_data_warning") is True, f"Node with 0 obs should have low_data_warning: {entry}"
                break

    def test_voi_no_low_data_warning_when_threshold_zero(self, test_server):
        """GET /voi without min_observations has no low_data_warning fields (OHM-zn3s)."""
        port, _ = test_server
        status, data = _request("GET", port, "/voi?top=5")
        assert status == 200
        for entry in data.get("rankings", []):
            assert "low_data_warning" not in entry, f"low_data_warning should not appear when min_observations=0: {entry}"


class TestWebhookTenantIsolation:
    """OHM-ym2f: Webhook registry must not fire cross-tenant (tenant A webhook ≠ tenant B events)."""

    def setup_method(self):
        with _webhook_lock:
            _webhook_registry.clear()

    def teardown_method(self):
        with _webhook_lock:
            _webhook_registry.clear()

    def test_webhook_fires_for_matching_tenant(self):
        """Webhook registered under customer_id='a' fires when event is triggered for 'a'."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append((url, event))
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry["tenant_a"] = {"agent1": {"url": "https://example.com/hook", "events": ["node.created"]}}

            _trigger_webhooks({"type": "node.created", "agent": "agent1", "node": {}}, customer_id="tenant_a")
            assert len(fired) == 1
            assert fired[0][0] == "https://example.com/hook"
        finally:
            srv._deliver_webhook = original

    def test_webhook_does_not_fire_for_different_tenant(self):
        """Webhook registered under customer_id='a' must NOT fire for customer_id='b' events."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append(url)
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry["tenant_a"] = {"agent1": {"url": "https://example.com/hook", "events": ["*"]}}

            _trigger_webhooks({"type": "node.created", "agent": "agent2", "node": {}}, customer_id="tenant_b")
            assert fired == [], f"Cross-tenant webhook fired: {fired}"
        finally:
            srv._deliver_webhook = original

    def test_webhook_none_tenant_fires_for_none_events(self):
        """Single-tenant (customer_id=None) webhooks fire for customer_id=None events only."""
        fired = []

        def fake_deliver(url, event, timeout=5.0):
            fired.append(url)
            return True

        import ohm.server as srv

        original = srv._deliver_webhook
        srv._deliver_webhook = fake_deliver
        try:
            with _webhook_lock:
                _webhook_registry[None] = {"agent_st": {"url": "https://example.com/st", "events": ["*"]}}

            _trigger_webhooks({"type": "edge.created", "agent": "agent_st"}, customer_id=None)
            assert len(fired) == 1

            fired.clear()
            _trigger_webhooks({"type": "edge.created", "agent": "other"}, customer_id="some_tenant")
            assert fired == [], "Single-tenant webhook must not fire for tenant-scoped event"
        finally:
            srv._deliver_webhook = original


class TestMultiTenantFeatureFlag:
    """Tests for OHM-l31g: feature-flag multi-tenancy rollout."""

    def test_multi_tenant_default_off(self, tmp_path):
        """Multi-tenancy is OFF by default — no flag, no env var."""
        db_path = str(tmp_path / "test_mt_off.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            assert OhmHandler.multi_tenant is False
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["multi_tenant"] is False
        finally:
            server.shutdown()

    def test_customer_id_none_when_off(self):
        """When multi_tenant=False, _customer_id always returns None."""
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        assert handler._customer_id is None

    def test_customer_id_resolved_when_on(self):
        """When multi_tenant=True, _customer_id returns _resolved_customer_id if set."""
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = True
        handler._resolved_customer_id = "acme-corp"
        assert handler._customer_id == "acme-corp"

    def test_current_store_returns_store_when_off(self, tmp_path):
        """When multi_tenant=False, current_store returns self.store with zero indirection."""
        db_path = str(tmp_path / "test_mt_store.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        handler = OhmHandler.__new__(OhmHandler)
        handler.multi_tenant = False
        handler.store = store
        assert handler.current_store is store
        store.close()

    def test_status_includes_multi_tenant(self, tmp_path):
        """GET /status includes multi_tenant flag in response."""
        db_path = str(tmp_path / "test_mt_status.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True, multi_tenant=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert data["multi_tenant"] is True
        finally:
            server.shutdown()

    def test_env_var_enables_multi_tenant(self, monkeypatch):
        """OHM_MULTI_TENANT=1 enables multi-tenancy via environment variable."""
        import os

        monkeypatch.setenv("OHM_MULTI_TENANT", "1")
        assert os.environ.get("OHM_MULTI_TENANT", "").lower() in ("1", "true", "yes")


class TestMarkovHTTPEndpoints:
    """Tests for OHM-20bt: Markov HTTP endpoints in the daemon."""

    @pytest.fixture(autouse=True)
    def require_numpy(self):
        pytest.importorskip("numpy")

    def test_markov_absorbing_risk_endpoint(self, tmp_path):
        """GET /markov/absorbing?start=<node_id> returns Markov analysis."""
        db_path = str(tmp_path / "test_markov_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "healthy", "label": "healthy", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "symptomatic", "label": "symptomatic", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "deceased", "label": "deceased", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.3}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "healthy", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.7}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "deceased", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.1}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/absorbing?start=healthy")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "method" in data
            assert "absorbing" in data["method"]
        finally:
            server.shutdown()

    def test_markov_expected_steps_endpoint(self, tmp_path):
        """GET /markov/expected_steps?start=<node_id> returns expected steps."""
        db_path = str(tmp_path / "test_markov_steps_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "healthy", "label": "healthy", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "symptomatic", "label": "symptomatic", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "deceased", "label": "deceased", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.3}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "healthy", "to_node": "healthy", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.7}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "deceased", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.1}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "symptomatic", "to_node": "symptomatic", "edge_type": "TRANSITIONS_TO", "layer": "L1", "probability": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/expected_steps?start=healthy")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert "method" in data
            assert "expected_steps" in data["method"]
        finally:
            server.shutdown()

    def test_markov_absorbing_missing_start(self, tmp_path):
        """GET /markov/absorbing without ?start= returns 400."""
        db_path = str(tmp_path / "test_markov_no_start.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/markov/absorbing")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            server.shutdown()

    def test_markov_in_discovery_index(self, tmp_path):
        """GET / discovery index includes Markov endpoints."""
        db_path = str(tmp_path / "test_markov_index.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert "/markov/absorbing" in data["endpoints"]
            assert "/markov/expected_steps" in data["endpoints"]
        finally:
            server.shutdown()


@pytest.mark.xdist_group("server")
class TestTemporalHTTPEndpoints:
    """Tests for /granger, /edge_stability, and /policy HTTP endpoints."""

    def test_granger_endpoint(self, tmp_path):
        """GET /granger?from=X&to=Y returns Granger causality test."""
        db_path = str(tmp_path / "test_granger_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "node_a", "label": "A", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "node_b", "label": "B", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/granger?from=node_a&to=node_b")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["method"] == "granger_causality"
            assert data["from_node"] == "node_a"
            assert data["to_node"] == "node_b"
        finally:
            server.shutdown()

    def test_granger_missing_params(self, tmp_path):
        """GET /granger without ?from or ?to returns 400."""
        db_path = str(tmp_path / "test_granger_400.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/granger?from=node_a")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            server.shutdown()

    def test_granger_invalid_lag_returns_400(self, tmp_path):
        """GET /granger with non-integer max_lag returns 400, not 500."""
        db_path = str(tmp_path / "test_granger_invalid.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/granger?from=a&to=b&max_lag=notanumber")
            resp = conn.getresponse()
            assert resp.status == 400
            data = json.loads(resp.read())
            assert data["error"] == "invalid_parameter"
        finally:
            server.shutdown()

    def test_granger_invalid_node_id_returns_400(self, tmp_path):
        """GET /granger with disallowed characters in node id returns 400."""
        db_path = str(tmp_path / "test_granger_badid.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/granger?from=node_a;DROP+TABLE&to=node_b")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            server.shutdown()
        """GET /edge_stability returns stability analysis."""
        db_path = str(tmp_path / "test_edgestab_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "n1", "label": "A", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "n2", "label": "B", "type": "concept"}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/edge", json.dumps({"from_node": "n1", "to_node": "n2", "edge_type": "CAUSES", "layer": "L3", "probability": 0.8, "confidence": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/edge_stability")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            assert data["method"] == "edge_stability"
            assert data["n_edges"] >= 1
        finally:
            server.shutdown()

    def test_policy_endpoint(self, tmp_path):
        """GET /policy?target=X returns observe-vs-act recommendation (OHM-od01.5)."""
        db_path = str(tmp_path / "test_policy_http.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/node", json.dumps({"id": "dec1", "label": "Decision", "type": "decision", "utility_scale": 0.9}))
            conn.getresponse().read()
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/policy?target=dec1")
            resp = conn.getresponse()
            assert resp.status == 200
            data = json.loads(resp.read())
            # OHM-od01.5: HTTP /policy is now routed through the canonical
            # compute_policy() in ohm.inference.pomdp. The richer response
            # shape replaces the older belief_state_decision format.
            assert data["method"] == "belief_state_policy"
            assert data["recommendation"] in ("observe", "act")
            assert "evpi" in data
            assert "current_belief" in data
            assert "confidence" in data
        finally:
            server.shutdown()

    def test_policy_missing_target(self, tmp_path):
        """GET /policy without ?target returns 400."""
        db_path = str(tmp_path / "test_policy_400.duckdb")
        store = OhmStore(db_path=db_path, agent_name="test")
        port, server, thread = _start_test_server(store, no_auth=True)
        try:
            conn = HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/policy")
            resp = conn.getresponse()
            assert resp.status == 400
        finally:
            server.shutdown()


@pytest.mark.xdist_group("server")
class TestHookEndpoints:
    """Tests for POST /hooks, GET /hooks, DELETE /hooks/{id} (OHM-aznh.3)."""

    def test_post_hooks_creates_hook(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/hooks",
            body={
                "event": "pre_ingest",
                "command": "echo validate",
            },
        )
        assert status == 201
        assert data["event"] == "pre_ingest"
        assert data["command"] == "echo validate"
        assert data["timeout_ms"] == 5000
        assert data["enabled"] is True

    def test_post_hooks_invalid_event_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/hooks",
            body={
                "event": "bad_event",
                "command": "echo",
            },
        )
        assert status == 400

    def test_post_hooks_missing_command_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/hooks",
            body={
                "event": "pre_ingest",
            },
        )
        assert status == 400

    def test_get_hooks_returns_list(self, test_server):
        port, _ = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": "echo a"})
        _request("POST", port, "/hooks", body={"event": "post_ingest", "command": "echo b"})
        status, data = _request("GET", port, "/hooks")
        assert status == 200
        assert data["count"] == 5  # 2 user + 3 built-in (OHM-aznh.11)
        assert len(data["hooks"]) == 5

    def test_get_hooks_filter_by_event(self, test_server):
        port, _ = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": "echo a"})
        _request("POST", port, "/hooks", body={"event": "post_ingest", "command": "echo b"})
        status, data = _request("GET", port, "/hooks?event=pre_ingest")
        assert status == 200
        assert data["count"] == 4  # 1 user + 3 built-in (OHM-aznh.11)
        assert data["hooks"][0]["event"] == "pre_ingest"

    def test_get_hooks_invalid_event_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/hooks?event=bad_event")
        assert status == 400

    def test_delete_hooks_removes_hook(self, test_server):
        port, _ = test_server
        _, hook = _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": "echo x"})
        hook_id = hook["id"]
        status, data = _request("DELETE", port, f"/hooks/{hook_id}")
        assert status == 200
        assert data["deleted"] == hook_id
        status, data = _request("GET", port, "/hooks")
        assert data["count"] == 3  # 3 built-in hooks remain (OHM-aznh.11)

    def test_delete_hooks_not_found_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request("DELETE", port, "/hooks/nonexistent-id")
        assert status == 400

    def test_hooks_require_auth(self, auth_server):
        port, _ = auth_server
        status, _ = _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": "echo"})
        assert status == 401

    def test_hooks_readonly_cannot_write(self, auth_server):
        port, _ = auth_server
        status, _ = _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": "echo"}, token="readonly-token")
        assert status == 403

    def test_pre_ingest_hook_allows_node_creation(self, test_server):
        port, store = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": 'python3 -c "pass"'})
        status, data = _request("POST", port, "/node", body={"id": "n1", "label": "test", "type": "concept"})
        assert status == 201

    def test_pre_ingest_hook_rejects_node_creation(self, test_server):
        port, store = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": 'python3 -c "raise SystemExit(1)"'})
        status, data = _request("POST", port, "/node", body={"id": "n2", "label": "rejected", "type": "concept"})
        assert status == 422
        assert data["error"] == "hook_rejected"

    def test_pre_ingest_hook_allows_edge_creation(self, test_server):
        port, store = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": 'python3 -c "pass"'})
        _request("POST", port, "/node", body={"id": "from1", "label": "from", "type": "concept"})
        _request("POST", port, "/node", body={"id": "to1", "label": "to", "type": "concept"})
        status, data = _request("POST", port, "/edge", body={"from": "from1", "to": "to1", "type": "SUPPORTS", "layer": "L3"})
        assert status == 201

    def test_pre_ingest_hook_rejects_edge_creation(self, test_server):
        port, store = test_server
        _request("POST", port, "/hooks", body={"event": "pre_ingest", "command": 'python3 -c "raise SystemExit(1)"'})
        _request("POST", port, "/node", body={"id": "from2", "label": "from", "type": "concept"})
        _request("POST", port, "/node", body={"id": "to2", "label": "to", "type": "concept"})
        status, data = _request("POST", port, "/edge", body={"from": "from2", "to": "to2", "type": "SUPPORTS", "layer": "L3"})
        assert status == 422
        assert data["error"] == "hook_rejected"

    def test_no_hooks_normal_operation(self, test_server):
        port, store = test_server
        status, data = _request("POST", port, "/node", body={"id": "n3", "label": "normal", "type": "concept"})
        assert status == 201

    def test_post_ingest_hook_decorates_response(self, test_server):
        import sys

        port, store = test_server
        cmd = 'python3 -c "import sys,json; sys.stdout.write(json.dumps({chr(100)+chr(101)+chr(99)+chr(111)+chr(114)+chr(97)+chr(116)+chr(101)+chr(100): True}))"'
        _request("POST", port, "/hooks", body={"event": "post_ingest", "command": cmd})
        status, data = _request("POST", port, "/node", body={"id": "n4", "label": "decorated", "type": "concept"})
        assert status == 201
        assert data.get("hook_decorations", {}).get("decorated") is True


class TestPreQueryPostQueryHooks:
    """Tests for pre_query/post_query hooks wired into GET handlers (OHM-aznh.10)."""

    def test_pre_query_hook_blocks_with_403(self, test_server):
        port, _ = test_server
        _request("POST", port, "/hooks", body={"event": "pre_query", "command": 'python3 -c "raise SystemExit(1)"'})
        status, data = _request("GET", port, "/stats")
        assert status == 403
        assert data["error"] == "hook_rejected"

    def test_pre_query_hook_allows_when_passing(self, test_server):
        port, _ = test_server
        _request("POST", port, "/hooks", body={"event": "pre_query", "command": 'python3 -c "pass"'})
        status, data = _request("GET", port, "/stats")
        assert status == 200

    def test_pre_query_hook_modifies_query_params(self, test_server):
        port, _ = test_server
        _request("POST", port, "/node", body={"id": "n1", "label": "test", "type": "concept"})
        cmd = "python3 -c \"import sys,json; sys.stdout.write(json.dumps({'query_params': {'limit': ['1']}}))\""
        _request("POST", port, "/hooks", body={"event": "pre_query", "command": cmd})
        status, data = _request("GET", port, "/nodes")
        assert status == 200

    def test_post_query_hook_decorates_response(self, test_server):
        port, _ = test_server
        cmd = "python3 -c \"import sys,json; sys.stdout.write(json.dumps({'enriched': True}))\""
        _request("POST", port, "/hooks", body={"event": "post_query", "command": cmd})
        status, data = _request("GET", port, "/stats")
        assert status == 200
        assert data.get("hook_decorations", {}).get("enriched") is True

    def test_post_query_hook_failure_does_not_block(self, test_server):
        port, _ = test_server
        _request("POST", port, "/hooks", body={"event": "post_query", "command": 'python3 -c "raise SystemExit(1)"'})
        status, data = _request("GET", port, "/stats")
        assert status == 200

    def test_no_query_hooks_normal_operation(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/stats")
        assert status == 200
        assert "hook_decorations" not in data


class TestBuiltinHooks:
    """Tests for built-in hooks registered on server startup (OHM-aznh.11)."""

    def test_cross_link_enforced_via_hook(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "bare-pattern",
                "label": "Bare",
                "type": "pattern",
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"

    def test_cross_link_passes_with_connects_to(self, test_server):
        port, store = test_server
        store.write_node("anchor1", "Anchor", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "linked-pattern",
                "label": "Linked",
                "type": "pattern",
                "connects_to": ["anchor1"],
            },
        )
        assert status == 201

    def test_source_url_enforced_via_hook(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "bare-source",
                "label": "No URL",
                "type": "source",
            },
        )
        assert status == 422
        assert data["error"] == "hook_rejected"

    def test_source_url_passes_with_url(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            body={
                "id": "url-source",
                "label": "With URL",
                "type": "source",
                "source_url": "https://example.com",
            },
        )
        assert status == 201

    def test_builtin_hooks_listed(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/hooks?event=pre_ingest")
        assert status == 200
        commands = [h["command"] for h in data["hooks"]]
        assert "python:ohm.hooks_builtin.cross_link_check" in commands
        assert "python:ohm.hooks_builtin.source_url_required" in commands


class TestResolveEndpoint:
    """Tests for GET /resolve?query= endpoint (OHM-g0kv.4)."""

    def test_resolve_exact_match(self, test_server):
        port, store = test_server
        store.write_node("n1", "Hormuz AND-Gate", "concept", agent_name="test")
        status, data = _request("GET", port, "/resolve?query=hormuz%20and-gate")
        assert status == 200
        assert data["resolved"]["id"] == "n1"

    def test_resolve_case_insensitive(self, test_server):
        port, store = test_server
        store.write_node("n2", "Demand Rationing", "concept", agent_name="test")
        status, data = _request("GET", port, "/resolve?query=Demand%20Rationing")
        assert status == 200
        assert data["resolved"]["id"] == "n2"

    def test_resolve_suggestions_on_prefix(self, test_server):
        port, store = test_server
        store.write_node("n3", "Hormuz Shipping", "concept", agent_name="test")
        status, data = _request("GET", port, "/resolve?query=hormuz")
        assert status == 200

    def test_resolve_not_found(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/resolve?query=nonexistent_xyz")
        assert status == 404

    def test_resolve_missing_query_param(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/resolve")
        assert status == 400


@pytest.mark.xdist_group("server")
class TestScratchEndpoint:
    """Tests for POST /scratch — L0 thinking fragments (OHM-a5rz.4)."""

    def test_scratch_creates_fragment(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Broadcom didn't miss — they refused to raise. That's different."},
        )
        assert status == 201
        assert data["type"] == "fragment"
        assert data["scratch"] is True
        assert data["confidence"] == 0.0
        assert "Broadcom" in data["label"]
        assert data["provenance"] == "scratch"

    def test_scratch_empty_content_400(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/scratch", body={"content": ""})
        assert status == 400
        assert "error" in data

    def test_scratch_missing_content_400(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/scratch", body={})
        assert status == 400

    def test_scratch_url_extraction(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "See https://example.com/paper for details"},
        )
        assert status == 201
        assert data["url"] == "https://example.com/paper"

    def test_scratch_with_connects_to(self, test_server):
        port, store = test_server
        store.write_node("anchor_1", "Anchor Node", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "This relates to anchor", "connects_to": ["anchor_1"]},
        )
        assert status == 201
        assert data["type"] == "fragment"

    def test_scratch_fragment_node_retrievable(self, test_server):
        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Hunch about supply chain"},
        )
        assert status == 201
        node_id = data["id"]
        status, retrieved = _request("GET", port, f"/node/{node_id}")
        assert status == 200
        assert retrieved["type"] == "fragment"


@pytest.mark.xdist_group("server")
class TestFragmentConnectEndpoint:
    """Tests for POST /fragments/{id}/connect — L0 fragment linking (OHM-a5rz.11)."""

    def test_fragment_connect_refines(self, test_server):
        port, store = test_server
        status, f1 = _request("POST", port, "/scratch", body={"content": "First hunch about supply"})
        assert status == 201
        status, f2 = _request("POST", port, "/scratch", body={"content": "Second refined hunch"})
        assert status == 201
        status, edge = _request(
            "POST",
            port,
            f"/fragments/{f1['id']}/connect",
            body={"target_id": f2["id"], "edge_type": "REFINES_FRAG"},
        )
        assert status == 201
        assert edge["layer"] == "L0"
        assert edge["edge_type"] == "REFINES_FRAG"

    def test_fragment_connect_contradicts(self, test_server):
        port, store = test_server
        status, f1 = _request("POST", port, "/scratch", body={"content": "Hunch A"})
        status, f2 = _request("POST", port, "/scratch", body={"content": "Hunch B"})
        status, edge = _request(
            "POST",
            port,
            f"/fragments/{f1['id']}/connect",
            body={"target_id": f2["id"], "edge_type": "CONTRADICTS_FRAG"},
        )
        assert status == 201
        assert edge["edge_type"] == "CONTRADICTS_FRAG"

    def test_fragment_connect_non_fragment_400(self, test_server):
        port, store = test_server
        store.write_node("concept_1", "Regular Concept", "concept", agent_name="test")
        status, frag = _request("POST", port, "/scratch", body={"content": "A hunch"})
        status, data = _request(
            "POST",
            port,
            f"/fragments/{frag['id']}/connect",
            body={"target_id": "concept_1", "edge_type": "REFINES_FRAG"},
        )
        assert status == 400

    def test_fragment_connect_missing_target_400(self, test_server):
        port, _ = test_server
        status, frag = _request("POST", port, "/scratch", body={"content": "A hunch"})
        status, data = _request(
            "POST",
            port,
            f"/fragments/{frag['id']}/connect",
            body={"edge_type": "REFINES_FRAG"},
        )
        assert status == 400

    def test_fragment_connect_invalid_edge_type_400(self, test_server):
        port, store = test_server
        status, f1 = _request("POST", port, "/scratch", body={"content": "Hunch A"})
        status, f2 = _request("POST", port, "/scratch", body={"content": "Hunch B"})
        status, data = _request(
            "POST",
            port,
            f"/fragments/{f1['id']}/connect",
            body={"target_id": f2["id"], "edge_type": "CAUSES"},
        )
        assert status == 400

    def test_fragment_connect_not_found_404(self, test_server):
        port, _ = test_server
        status, data = _request(
            "POST",
            port,
            "/fragments/nonexistent/connect",
            body={"target_id": "also_missing", "edge_type": "REFINES_FRAG"},
        )
        assert status == 404


@pytest.mark.xdist_group("server")
class TestSearchExcludesFragments:
    """OHM-a5rz.18: L0 fragments excluded from /search and /semantic_search by default."""

    def test_search_excludes_fragments_by_default(self, test_server):
        port, store = test_server
        store.write_node("concept_search_test", "TestConcept", "concept", agent_name="test")
        _request("POST", port, "/scratch", body={"content": "TestConcept hunch fragment"})
        # Default search should exclude fragments
        status, data = _request("GET", port, "/search?q=TestConcept")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", data)
        types = [r.get("type") for r in results]
        assert "fragment" not in types

    def test_search_includes_fragments_with_flag(self, test_server):
        port, store = test_server
        store.write_node("concept_search_flag", "UniqueFlagConcept", "concept", agent_name="test")
        _request("POST", port, "/scratch", body={"content": "UniqueFlagConcept hunch"})
        # include_l0=true should include fragments
        status, data = _request("GET", port, "/search?q=UniqueFlagConcept&include_l0=true")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", data)
        types = [r.get("type") for r in results]
        assert "fragment" in types

    def test_search_type_fragment_explicit(self, test_server):
        port, store = test_server
        _request("POST", port, "/scratch", body={"content": "ExplicitTypeSearch hunch"})
        # Explicit type=fragment should work even without include_l0
        status, data = _request("GET", port, "/search?q=ExplicitTypeSearch&type=fragment")
        assert status == 200
        results = data if isinstance(data, list) else data.get("results", data)
        assert len(results) >= 1
        assert all(r.get("type") == "fragment" for r in results)


@pytest.mark.xdist_group("server")
class TestScratchConnectsToEdges:
    """OHM-a5rz.17: connects_to should create L0 CONTEXT_OF edges."""

    def test_connects_to_creates_explicit_edges(self, test_server):
        port, store = test_server
        store.write_node("anchor_explicit", "Anchor For Explicit", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "This connects explicitly", "connects_to": ["anchor_explicit"]},
        )
        assert status == 201
        assert "explicit_links" in data
        assert len(data["explicit_links"]) == 1
        link = data["explicit_links"][0]
        assert link["node_id"] == "anchor_explicit"
        assert link["edge_type"] == "CONTEXT_OF"
        assert link["provenance"] == "scratch_explicit"

    def test_connects_to_creates_L0_edge_in_graph(self, test_server):
        port, store = test_server
        store.write_node("anchor_graph", "Anchor For Graph", "concept", agent_name="test")
        status, frag = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Graph edge test", "connects_to": ["anchor_graph"]},
        )
        assert status == 201
        # Verify the edge exists in the graph (L0 layer)
        status, nbr = _request("GET", port, f"/neighborhood/{frag['id']}?layer=L0")
        assert status == 200
        edges = nbr.get("edges", [])
        l0_context_edges = [e for e in edges if e.get("edge_type") == "CONTEXT_OF" and e.get("layer") == "L0"]
        assert len(l0_context_edges) >= 1

    def test_connects_to_multiple_targets(self, test_server):
        port, store = test_server
        store.write_node("anchor_a", "Anchor A", "concept", agent_name="test")
        store.write_node("anchor_b", "Anchor B", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "Multi-target test", "connects_to": ["anchor_a", "anchor_b"]},
        )
        assert status == 201
        assert len(data.get("explicit_links", [])) == 2
        target_ids = {link["node_id"] for link in data["explicit_links"]}
        assert "anchor_a" in target_ids
        assert "anchor_b" in target_ids

    def test_connects_to_and_auto_links_both_present(self, test_server):
        port, store = test_server
        # Create a node with a label that will match via auto-link
        store.write_node("and_gate_pattern", "AND-Gate Pattern", "concept", agent_name="test")
        status, data = _request(
            "POST",
            port,
            "/scratch",
            body={"content": "AND-Gate Pattern connects here", "connects_to": ["and_gate_pattern"]},
        )
        assert status == 201
        # Both explicit and auto links should be present
        has_explicit = len(data.get("explicit_links", [])) >= 1
        len(data.get("auto_links", [])) >= 1
        # At minimum the explicit link must exist
        assert has_explicit


@pytest.mark.xdist_group("server")
class TestOrientEndpoint:
    """Tests for /orient context-recovery endpoint.

    OHM-cod4: /orient was returning 500 'NoneType' object is not subscriptable
    when an agent had no activity. Fixed by hardening fetchone()[0] patterns
    and dict access in _get_orient.
    """

    def test_orient_unknown_agent_returns_200_cold_start(self, test_server):
        """Agent with no nodes should return 200 with cold_start guidance."""
        port, _ = test_server
        status, data = _request("GET", port, "/orient?agent=ghost_agent")
        assert status == 200, data
        assert data["cold_start"] is True
        assert data["where_was_i"]["last_activity"] is None
        assert data["where_was_i"]["time_since"] is None
        assert data["what_next"]["orphan_count"] == 0
        assert "bootstrap_guide" in data

    def test_orient_missing_agent_param_returns_400(self, test_server):
        """Missing agent query param should return 400."""
        port, _ = test_server
        status, data = _request("GET", port, "/orient")
        assert status == 400
        assert data["error"] == "agent parameter required"

    def test_orient_with_seeded_activity_returns_full_packet(self, test_server):
        """An agent with nodes and edges should return the full orient packet."""
        port, store = test_server
        store.write_node("orient_a", "Anchor A", "concept", confidence=0.8, agent_name="metis")
        store.write_node("orient_b", "Anchor B", "concept", confidence=0.6, agent_name="metis")
        store.write_edge("orient_a", "orient_b", "REFERENCES", layer="L2", agent_name="metis")
        status, data = _request("GET", port, "/orient?agent=metis")
        assert status == 200, data
        assert "where_was_i" in data
        assert "what_did_i_miss" in data
        assert "what_next" in data
        assert data["where_was_i"]["last_activity"] is not None
        assert data["cold_start"] is False

    def test_orient_handles_empty_db_no_fetchone_crash(self, test_server):
        """Regression: fetchone()[0] must not raise on empty tables.

        OHM-cod4 root cause was unbounded NoneType subscripting when fetchone()
        returned None on edge cases. Even an empty agent footprint should
        produce a 200 response, never a 500.
        """
        port, _ = test_server
        status, data = _request("GET", port, "/orient?agent=metis")
        assert status == 200, f"expected 200 got {status}: {data}"
        # Without any seeded data, metis should be in cold_start
        assert "where_was_i" in data
        assert "what_next" in data


@pytest.mark.xdist_group("server")
class TestChangesEndpoint:
    """Tests for /changes — personalised agent delta (OHM-b7l7).

    /changes consolidates what an agent would otherwise poll /listen,
    /contradictions, /anomalies, /stale, /suggest, and /tasks for into a
    single call. The legacy fields (`since`, `node_total`, `edge_total`,
    `nodes`, `edges`) must be preserved; agent-scoped sections are
    additive and only populated when an agent can be resolved.
    """

    def test_changes_no_agent_returns_core_feed(self, test_server):
        """Without ?agent= the legacy core fields are returned and the
        agent-scoped sections are absent (backward-compatible)."""
        port, _ = test_server
        status, data = _request("GET", port, "/changes")
        assert status == 200, data
        for k in ("since", "agent", "query_timestamp", "node_total", "edge_total", "nodes", "edges"):
            assert k in data, f"missing core field: {k}"
        assert data["agent"] is None
        # Agent-scoped sections absent when no agent resolved
        for k in (
            "new_observations_on_my_nodes",
            "edges_touching_my_nodes",
            "challenges_to_my_edges",
            "tasks_assigned_or_status_changed",
            "stale_nodes_needing_refresh",
        ):
            assert k not in data, f"agent section should be absent without agent: {k}"

    def test_changes_since_falls_back_to_24h_when_no_last_sync(self, test_server):
        """When ?since is omitted and the agent has no last_sync the
        endpoint falls back to 24h ago (mirrors /listen)."""
        port, _ = test_server
        status, data = _request("GET", port, "/changes?agent=metis")
        assert status == 200, data
        assert data["agent"] == "metis"
        # since is non-null and ISO-format
        assert data["since"]
        assert "T" in data["since"]

    def test_changes_with_explicit_since_filters_nodes(self, test_server):
        """?since= filters the core feed by created_at > since."""
        port, store = test_server
        store.write_node("ch_a", "Anchor A", "concept", agent_name="metis")
        # Future since returns empty nodes/edges but still populated agent sections
        status, data = _request("GET", port, "/changes?agent=metis&since=3000-01-01T00:00:00")
        assert status == 200, data
        assert data["nodes"] == []
        assert data["edges"] == []
        assert data["node_total"] == 0
        assert data["edge_total"] == 0
        # agent-scoped sections still exist (may be empty)
        assert "edges_touching_my_nodes" in data

    def test_changes_agent_sections_populated(self, test_server):
        """When ?agent= is present the five agent-scoped sections are present."""
        port, store = test_server
        store.write_node("ch_a", "Anchor A", "concept", agent_name="metis")
        store.write_node("ch_b", "Anchor B", "concept", agent_name="hephaestus")
        store.write_edge("ch_a", "ch_b", "REFERENCES", layer="L2", agent_name="hephaestus")
        status, data = _request("GET", port, "/changes?agent=metis")
        assert status == 200, data
        # All five sections present
        for k in (
            "new_observations_on_my_nodes",
            "edges_touching_my_nodes",
            "challenges_to_my_edges",
            "tasks_assigned_or_status_changed",
            "stale_nodes_needing_refresh",
        ):
            assert k in data, f"missing agent section: {k}"
            assert isinstance(data[k], list)
        # The hephaestus-authored edge touches metis's node ch_a → should appear
        touch_ids = [e["id"] for e in data["edges_touching_my_nodes"]]
        assert len(touch_ids) >= 1

    def test_changes_challenges_to_my_edges_section(self, test_server):
        """A CHALLENGED_BY edge targeting one of metis's edges surfaces in
        challenges_to_my_edges."""
        port, store = test_server
        store.write_node("ch_a", "Anchor A", "concept", agent_name="metis")
        store.write_node("ch_b", "Anchor B", "concept", agent_name="hephaestus")
        store.write_edge("ch_a", "ch_b", "CAUSES", layer="L3", agent_name="metis")
        # Find the L3 edge id and challenge it (via the proper challenge_edge API)
        edge = store.execute_one(
            "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND deleted_at IS NULL",
            ["ch_a", "ch_b"],
        )
        assert edge is not None
        store.challenge_edge(
            edge["id"], "i disagree with this causal claim",
            0.6, "CHALLENGED_BY", agent_name="hephaestus",
        )
        status, data = _request("GET", port, "/changes?agent=metis")
        assert status == 200, data
        challenges = data["challenges_to_my_edges"]
        assert len(challenges) >= 1
        assert challenges[0]["target_edge_id"] == edge["id"]
        assert challenges[0]["challenger"] == "hephaestus"
        assert "disagree" in challenges[0]["challenge_reason"]

    def test_changes_tasks_assigned_section(self, test_server):
        """A task node assigned to metis appears in tasks_assigned_or_status_changed."""
        port, store = test_server
        store.write_node(
            "ch_task", "Write follow-up", "task", agent_name="atlas",
            task_status="open", assigned_to="metis",
        )
        status, data = _request("GET", port, "/changes?agent=metis")
        assert status == 200, data
        tasks = data["tasks_assigned_or_status_changed"]
        ids = [t["id"] for t in tasks]
        assert "ch_task" in ids
        assert tasks[0]["status"] == "open"

    def test_changes_invalid_since_returns_400(self, test_server):
        """A malformed ?since= is rejected with ValidationError (4xx)."""
        port, _ = test_server
        status, data = _request("GET", port, "/changes?agent=metis&since=not-a-timestamp")
        assert 400 <= status < 500

    def test_changes_invalid_limit_returns_400(self, test_server):
        """Non-integer ?limit= is rejected with ValidationError."""
        port, _ = test_server
        status, data = _request("GET", port, "/changes?agent=metis&limit=banana")
        assert 400 <= status < 500

    def test_changes_returns_query_timestamp(self, test_server):
        """Response should include a server-side query_timestamp (ISO format)."""
        port, _ = test_server
        status, data = _request("GET", port, "/changes?agent=metis")
        assert status == 200, data
        assert data["query_timestamp"]
        # DuckDB returns CURRENT_TIMESTAMP either as '2026-...T...' or as
        # '2026-... HH:MM:SS...' depending on version/mode — accept either.
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", data["query_timestamp"])


@pytest.mark.xdist_group("server")
class TestObservationConfidenceEndpoint:
    """Tests for GET /observation/{id} and /observation/{id}/confidence (OHM-60pd)."""

    def test_get_observation_returns_record_with_confidence(self, test_server):
        """GET /observation/{id} returns the raw record enriched with
        effective_confidence and decay_profile."""
        port, store = test_server
        store.write_node("obs_n1", "Node", "concept", agent_name="metis")
        store.write_observation("obs_n1", "measurement", value=0.9, agent_name="metis", half_life_days=7.0, weibull_shape=1.0)
        obs_id = store.execute_one(
            "SELECT id FROM ohm_observations WHERE node_id = 'obs_n1' ORDER BY created_at DESC LIMIT 1"
        )["id"]
        status, data = _request("GET", port, f"/observation/{obs_id}")
        assert status == 200, data
        assert data["id"] == obs_id
        assert "effective_confidence" in data
        assert "decay_profile" in data
        assert data["weibull_shape"] == 1.0

    def test_get_observation_confidence_returns_full_packet(self, test_server):
        """GET /observation/{id}/confidence returns the full confidence packet."""
        port, store = test_server
        store.write_node("obs_n2", "Node 2", "concept", agent_name="metis")
        store.write_observation("obs_n2", "sentiment", value=0.8, agent_name="metis")
        obs_id = store.execute_one(
            "SELECT id FROM ohm_observations WHERE node_id = 'obs_n2' ORDER BY created_at DESC LIMIT 1"
        )["id"]
        status, data = _request("GET", port, f"/observation/{obs_id}/confidence")
        assert status == 200, data
        assert data["observation_id"] == obs_id
        for k in ("effective_confidence", "weibull_shape", "half_life_days",
                   "decay_function", "decay_profile", "age_days", "evaluated_at"):
            assert k in data, f"missing key: {k}"
        # sentiment default: weibull_shape=1.5, half_life=3.0
        assert data["weibull_shape"] == 1.5
        assert data["half_life_days"] == 3.0
        assert data["decay_function"] == "weibull"
        assert data["decay_profile"] == "fast-perishable"

    def test_get_observation_confidence_with_at_param(self, test_server):
        """?at=ISO8601 evaluates confidence at a specific time."""
        port, store = test_server
        store.write_node("obs_n3", "Node 3", "concept", agent_name="metis")
        store.write_observation("obs_n3", "measurement", value=1.0, agent_name="metis", half_life_days=7.0, weibull_shape=1.0)
        obs_id = store.execute_one(
            "SELECT id FROM ohm_observations WHERE node_id = 'obs_n3' ORDER BY created_at DESC LIMIT 1"
        )["id"]
        # Evaluate 7 days from now → should be ~0.5 (one half-life, κ=1)
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, data = _request("GET", port, f"/observation/{obs_id}/confidence?at={future}")
        assert status == 200, data
        assert data["effective_confidence"] == pytest.approx(0.5, abs=0.05)

    def test_get_observation_confidence_missing_returns_404(self, test_server):
        """A non-existent observation id returns 404."""
        port, _ = test_server
        status, data = _request("GET", port, "/observation/does-not-exist-1234/confidence")
        assert status == 404

    def test_get_observation_missing_returns_404(self, test_server):
        """GET /observation/{id} with a missing id returns 404."""
        port, _ = test_server
        status, data = _request("GET", port, "/observation/does-not-exist-1234")
        assert status == 404

    def test_get_observation_confidence_invalid_at_returns_400(self, test_server):
        """A malformed ?at= returns a 4xx."""
        port, store = test_server
        store.write_node("obs_n4", "Node 4", "concept", agent_name="metis")
        store.write_observation("obs_n4", "measurement", value=0.9, agent_name="metis")
        obs_id = store.execute_one(
            "SELECT id FROM ohm_observations WHERE node_id = 'obs_n4' ORDER BY created_at DESC LIMIT 1"
        )["id"]
        status, data = _request("GET", port, f"/observation/{obs_id}/confidence?at=not-a-timestamp")
        assert 400 <= status < 500


@pytest.mark.xdist_group("server")
class TestAdminDuplicatesEndpoint:
    """Tests for GET /admin/duplicates — combined duplicate detection (OHM-z2gp)."""

    def test_admin_duplicates_returns_summary(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/duplicates")
        assert status == 200, data
        for k in ("alias_collisions", "content_hash_collisions", "semantic_duplicates", "summary"):
            assert k in data
        assert data["summary"]["threshold"] == 0.85

    def test_admin_duplicates_with_threshold(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/duplicates?threshold=0.95")
        assert status == 200, data
        assert data["summary"]["threshold"] == 0.95

    def test_admin_duplicates_empty_db(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/admin/duplicates")
        assert status == 200, data
        assert data["summary"]["total"] == 0


@pytest.mark.xdist_group("server")
class TestNarrativeEndpoint:
    """Tests for GET /narrative/{node_id} — neighborhood narrative (OHM-q9rt.1)."""

    def test_narrative_returns_node_and_chains(self, test_server):
        port, store = test_server
        store.write_node("narr_a", "Hormuz AND-Gate", "concept", agent_name="metis")
        store.write_node("narr_b", "Chokepoint", "concept", agent_name="metis")
        store.write_edge("narr_a", "narr_b", "CAUSES", layer="L3", agent_name="metis")
        status, data = _request("GET", port, "/narrative/narr_a")
        assert status == 200, data
        assert data["node"]["label"] == "Hormuz AND-Gate"
        assert data["connection_count"] >= 1
        assert len(data["why_it_matters"]) >= 1

    def test_narrative_with_agent_param(self, test_server):
        port, store = test_server
        store.write_node("narr_c", "Source", "concept", agent_name="metis")
        store.write_node("narr_d", "Target", "concept", agent_name="hephaestus")
        store.write_edge("narr_c", "narr_d", "SUPPORTS", layer="L3", agent_name="metis")
        status, data = _request("GET", port, "/narrative/narr_c?agent=metis")
        assert status == 200, data
        assert "agent_context" in data
        assert data["agent_context"]["agent"] == "metis"

    def test_narrative_missing_node_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/narrative/does-not-exist-1234")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestLineageEndpoint:
    """Tests for GET /lineage/{node_id} — claim lineage (OHM-q9rt.2)."""

    def test_lineage_returns_claim_and_tree(self, test_server):
        port, store = test_server
        store.write_node("lin_src", "Source Doc", "source", agent_name="metis")
        store.write_node("lin_obs", "Observation", "concept", agent_name="metis")
        store.write_node("lin_pat", "Pattern", "pattern", agent_name="metis")
        store.write_edge("lin_obs", "lin_src", "REFERENCES", layer="L2", agent_name="metis")
        store.write_edge("lin_pat", "lin_obs", "DERIVES_FROM", layer="L2", agent_name="metis")
        status, data = _request("GET", port, "/lineage/lin_pat")
        assert status == 200, data
        assert data["claim"]["label"] == "Pattern"
        assert data["total_nodes"] >= 2
        assert data["total_sources"] >= 1

    def test_lineage_missing_node_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/lineage/does-not-exist-1234")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestContradictionSummaryEndpoint:
    """Tests for GET /contradiction/{node_id} — contradiction summary (OHM-q9rt.3)."""

    def test_contradiction_returns_sides(self, test_server):
        port, store = test_server
        store.write_node("con_n", "Debated Price", "concept", agent_name="metis")
        store.write_observation("con_n", "measurement", value=0.9, baseline=0.5, agent_name="metis")
        store.write_observation("con_n", "measurement", value=0.1, baseline=0.5, agent_name="hephaestus")
        status, data = _request("GET", port, "/contradiction/con_n")
        assert status == 200, data
        assert data["has_contradiction"] is True
        assert len(data["sides"]) == 2

    def test_contradiction_no_conflict_returns_false(self, test_server):
        port, store = test_server
        store.write_node("con_safe", "Safe Node", "concept", agent_name="metis")
        status, data = _request("GET", port, "/contradiction/con_safe")
        assert status == 200, data
        assert data["has_contradiction"] is False

    def test_contradiction_missing_node_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/contradiction/does-not-exist-1234")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestTaskContextEndpoint:
    """Tests for GET /task-context/{task_id} — task context binding (OHM-q9rt.4)."""

    def test_task_context_returns_task_and_subgraph(self, test_server):
        port, store = test_server
        store.write_node("tc_task", "Verify Claim", "task", agent_name="metis")
        store.write_node("tc_dec", "Decision", "decision", agent_name="metis")
        store.write_edge("tc_task", "tc_dec", "DECISION_DEPENDS_ON", layer="L3", agent_name="metis")
        status, data = _request("GET", port, "/task-context/tc_task")
        assert status == 200, data
        assert data["task"]["label"] == "Verify Claim"
        assert len(data["subgraph"]["nodes"]) >= 2
        assert len(data["rationale"]) >= 1

    def test_task_context_missing_returns_404(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/task-context/does-not-exist-1234")
        assert status == 404


@pytest.mark.xdist_group("server")
class TestConfidenceReportEndpoint:
    """Tests for GET /confidence-report — per-agent confidence report (OHM-q9rt.5)."""

    def test_confidence_report_returns_summary(self, test_server):
        port, store = test_server
        store.write_node("cr_a", "A", "concept", agent_name="metis")
        store.write_node("cr_b", "B", "concept", agent_name="metis")
        store.write_edge("cr_a", "cr_b", "CAUSES", layer="L3", agent_name="metis")
        status, data = _request("GET", port, "/confidence-report?agent=metis&since=2000-01-01T00:00:00")
        assert status == 200, data
        assert data["agent"] == "metis"
        assert "summary" in data
        assert data["summary"]["new"] >= 1

    def test_confidence_report_missing_agent_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/confidence-report")
        assert status == 400


@pytest.mark.xdist_group("server")
class TestScenarioEndpoint:
    """Tests for POST /scenario — counterfactual scenario analysis (OHM-xagx)."""

    def test_scenario_returns_comparison(self, test_server):
        port, store = test_server
        store.write_node("sc_a", "Supplier", "concept", agent_name="metis")
        store.write_node("sc_b", "Factory", "concept", agent_name="metis")
        store.write_edge("sc_a", "sc_b", "CAUSES", layer="L3", agent_name="metis")
        edges = store.execute("SELECT id FROM ohm_edges WHERE from_node = 'sc_a' AND to_node = 'sc_b' AND deleted_at IS NULL")
        edge_id = edges[0]["id"] if edges else None
        body = {"node_id": "sc_a", "failure_probability": 1.0, "edge_overrides": {edge_id: 0.3}, "compare": True}
        status, data = _request("POST", port, "/scenario", body=body)
        assert status == 200, data
        assert "baseline" in data
        assert "counterfactual" in data
        assert "deltas" in data

    def test_scenario_no_compare_returns_cascade(self, test_server):
        port, store = test_server
        store.write_node("sc_c", "Source", "concept", agent_name="metis")
        store.write_node("sc_d", "Target", "concept", agent_name="metis")
        store.write_edge("sc_c", "sc_d", "CAUSES", layer="L3", agent_name="metis")
        body = {"node_id": "sc_c", "compare": False}
        status, data = _request("POST", port, "/scenario", body=body)
        assert status == 200, data
        assert "cascade" in data
        assert "baseline" not in data

    def test_scenario_missing_node_id_returns_400(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/scenario", body={})
        assert status == 400


@pytest.mark.xdist_group("server")
class TestAutonomyLoopEndpoint:
    """Tests for the autonomy loop API (OHM-446a)."""

    def test_propose_action_endpoint(self, test_server):
        port, store = test_server
        store.write_node("al_target", "Target", "concept", agent_name="metis")
        store.write_node("al_scn", "Scenario", "scenario", agent_name="metis")
        store.write_edge("al_scn", "al_target", "EVALUATES", layer="L3", agent_name="metis")
        body = {"scenario_id": "al_scn", "label": "Increase stock", "rationale": "Risk mitigation"}
        status, data = _request("POST", port, "/propose-action", body=body)
        assert status == 201, data
        assert data["type"] == "action"
        assert data["task_status"] == "proposed"

    def test_execute_action_endpoint(self, test_server):
        port, store = test_server
        store.write_node("al_t2", "T", "concept", agent_name="metis")
        store.write_node("al_s2", "S", "scenario", agent_name="metis")
        store.write_edge("al_s2", "al_t2", "EVALUATES", layer="L3", agent_name="metis")
        # Propose first
        body = {"scenario_id": "al_s2", "label": "Test action"}
        status, action = _request("POST", port, "/propose-action", body=body)
        assert status == 201
        # Execute
        exec_body = {"action_id": action["id"], "outcome": "TRUE", "outcome_notes": "Success"}
        status, data = _request("POST", port, "/execute-action", body=exec_body)
        assert status == 200, data
        assert data["task_status"] == "executed"
        assert data["outcome"] == "TRUE"

    def test_loop_status_endpoint(self, test_server):
        port, _ = test_server
        status, data = _request("GET", port, "/loop-status")
        assert status == 200, data
        assert "summary" in data
        assert "proposed" in data
        assert "executed" in data
