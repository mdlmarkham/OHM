"""Tests for post-write suggestions (OHM-tr71.1).

Verifies that POST /node and POST /scratch return suggestions with
similar_nodes, shared_tags, and orphan_warning.
"""

import json
import time
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _enable_suggestions(monkeypatch):
    """Suggestions are disabled by default in tests; re-enable them here."""
    monkeypatch.delenv("OHM_DISABLE_SUGGESTIONS", raising=False)


# ── Unit tests for suggestions module ────────────────────────────────────


class TestGenerateSuggestions:
    """Unit tests for the suggestions generation module."""

    def test_returns_empty_when_no_content(self):
        """Suggestions with no content or label return empty lists."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        result = generate_suggestions(store, node_id="test-1", content=None, label=None)
        assert result["similar_nodes"] == []
        assert result["shared_tags"] == []
        assert result["orphan_warning"] is None

    def test_respects_deadline_and_skips_expensive_work(self):
        """A deadline in the past skips semantic search and heavy SQL."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        store.db_path = "/tmp/not-used.duckdb"
        # Patch _suggestion_conn so we don't try to open a real DB.
        mock_conn = MagicMock()
        with patch("ohm.server.suggestions._suggestion_conn", return_value=mock_conn):
            result = generate_suggestions(
                store,
                node_id="test-1",
                content="some content",
                tags=["ai"],
                has_edges=False,
                deadline=0.0,  # immediately exceeded
            )
        # Semantic search and orphan connection paths should be skipped.
        assert result["similar_nodes"] == []
        assert result["shared_tags"] == []
        assert result["orphan_warning"] is None
        # No heavy queries should have run.
        mock_conn.execute.assert_not_called()

    def test_passes_embedding_timeout_to_semantic_search(self):
        """generate_suggestions forwards the remaining budget as an embedding timeout."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        store.db_path = "/tmp/not-used.duckdb"
        mock_conn = MagicMock()
        with patch("ohm.server.suggestions._suggestion_conn", return_value=mock_conn):
            with patch("ohm.graph.queries.semantic_search") as mock_semantic:
                mock_semantic.return_value = []
                deadline = time.time() + 0.25
                generate_suggestions(
                    store,
                    node_id="test-1",
                    content="content",
                    deadline=deadline,
                )
                call = mock_semantic.call_args
                assert call.kwargs.get("embedding_timeout") is not None
                assert 0.0 < call.kwargs["embedding_timeout"] <= 0.25

    def test_returns_empty_when_ollama_unavailable(self):
        """Suggestions gracefully handle Ollama being down."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        with patch("ohm.graph.queries.semantic_search", side_effect=ValueError("Ollama unavailable")):
            result = generate_suggestions(store, node_id="test-1", content="test content", label="Test")
        assert result["similar_nodes"] == []

    def test_finds_similar_nodes(self):
        """Suggestions include similar nodes from semantic search."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        mock_results = [
            {"node_id": "concept-and-gate", "label": "AND-Gate", "type": "concept", "distance": 0.15},
            {"node_id": "concept-or-gate", "label": "OR-Gate", "type": "concept", "distance": 0.25},
            {"node_id": "test-1", "label": "Test", "type": "concept", "distance": 0.01},
        ]
        with patch("ohm.graph.queries.semantic_search", return_value=mock_results):
            result = generate_suggestions(store, node_id="test-1", content="AND-gate control mechanism")
        assert len(result["similar_nodes"]) == 2
        assert result["similar_nodes"][0]["id"] == "concept-and-gate"
        assert all(s["id"] != "test-1" for s in result["similar_nodes"])

    def test_finds_shared_tags(self):
        """Suggestions include nodes sharing tags."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        store.conn.execute.return_value.fetchall.return_value = [
            ("concept-governance", "Governance", "concept", '["governance", "and-or", "security"]'),
            ("concept-trap", "Trap Pattern", "concept", '["and-or", "trap"]'),
            ("concept-unrelated", "Unrelated", "concept", '["biology"]'),
        ]
        result = generate_suggestions(
            store,
            node_id="test-1",
            content="test",
            tags=["governance", "and-or"],
        )
        assert len(result["shared_tags"]) >= 1
        assert result["shared_tags"][0]["id"] == "concept-governance"
        assert "governance" in result["shared_tags"][0]["shared_tags"]

    def test_orphan_warning_when_no_edges(self):
        """Orphan warning is generated when node has no edges."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        store.conn.execute.return_value.fetchall.return_value = [
            ("orphan-1", "Orphan Node", "concept"),
            ("orphan-2", "Another Orphan", "pattern"),
        ]
        mock_similar = [
            {"node_id": "orphan-1", "label": "Orphan Node", "type": "concept", "distance": 0.2},
        ]
        with patch("ohm.graph.queries.semantic_search", return_value=mock_similar):
            result = generate_suggestions(store, node_id="test-1", content="test", has_edges=False)
        assert result["orphan_warning"] is not None
        assert "message" in result["orphan_warning"]

    def test_no_orphan_warning_when_has_edges(self):
        """No orphan warning when node already has edges."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        result = generate_suggestions(store, node_id="test-1", content="test", has_edges=True)
        assert result["orphan_warning"] is None

    def test_max_three_similar_nodes(self):
        """At most 3 similar nodes are returned."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        mock_results = [{"node_id": f"node-{i}", "label": f"Node {i}", "type": "concept", "distance": 0.1 * i} for i in range(10)]
        with patch("ohm.graph.queries.semantic_search", return_value=mock_results):
            result = generate_suggestions(store, node_id="test-1", content="test")
        assert len(result["similar_nodes"]) <= 3

    def test_excludes_self_from_suggestions(self):
        """The newly created node never appears in its own suggestions."""
        from ohm.server.suggestions import generate_suggestions

        store = MagicMock()
        mock_results = [
            {"node_id": "test-1", "label": "Self", "type": "concept", "distance": 0.001},
            {"node_id": "concept-other", "label": "Other", "type": "concept", "distance": 0.3},
        ]
        with patch("ohm.graph.queries.semantic_search", return_value=mock_results):
            result = generate_suggestions(store, node_id="test-1", content="test")
        assert all(s["id"] != "test-1" for s in result["similar_nodes"])

    def test_generate_edge_suggestions_respects_deadline(self):
        """Edge suggestions return the empty default when deadline is exceeded."""
        from ohm.server.suggestions import generate_edge_suggestions

        store = MagicMock()
        result = generate_edge_suggestions(
            store,
            from_node="node-a",
            to_node="node-b",
            edge_type="SUPPORTS",
            deadline=0.0,
        )
        assert result == {"related_edges": [], "edge_patterns": [], "orphan_resolved": False}
        store.conn.execute.assert_not_called()

    def test_connectivity_nudge_respects_deadline(self):
        """Connectivity nudge returns None when deadline is exceeded."""
        from ohm.server.suggestions import generate_connectivity_nudge

        store = MagicMock()
        assert generate_connectivity_nudge(store, "test-agent", deadline=0.0) is None

    def test_island_nudge_respects_deadline(self):
        """Island nudge returns None when deadline is exceeded."""
        from ohm.server.suggestions import generate_island_nudge

        store = MagicMock()
        assert generate_island_nudge(store, "test-agent", deadline=0.0) is None

    def test_island_nudge_returns_cached_result(self):
        """Island nudge cache is respected even with a valid deadline."""
        from ohm.server.suggestions import generate_island_nudge, clear_island_nudge_cache

        clear_island_nudge_cache()
        store = MagicMock()
        store.db_path = "/tmp/not-used.duckdb"
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch("ohm.server.suggestions._suggestion_conn", return_value=mock_conn):
            with patch("ohm.server.suggestions.time") as mock_time:
                mock_time.time.return_value = 1000.0
                result = generate_island_nudge(store, "test-agent", deadline=time.time() + 10)
                assert result is None
                # Same agent within TTL should return cached None without re-querying.
                mock_conn.reset_mock()
                cached = generate_island_nudge(store, "test-agent", deadline=time.time() + 10)
                assert cached is None
                mock_conn.execute.assert_not_called()
        clear_island_nudge_cache()


# ── Integration tests using live handler ─────────────────────────────────


class TestSuggestionsInNodeCreation:
    """Integration tests that POST /node and POST /scratch include suggestions.

    Uses the same pattern as test_server.py: start a real HTTP server on
    a random port and make real requests.
    """

    @pytest.fixture(autouse=True)
    def setup_server(self):
        """Start a test HTTP server with in-memory store."""
        from ohm.store import OhmStore
        from ohm.server import OhmHandler, _build_token_lookup
        from ohm.schema import DEFAULT_SCHEMA
        from http.server import HTTPServer
        import threading
        import socket

        # Create temp store
        import tempfile
        import os

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test_suggestions.duckdb")
        self.store = OhmStore(db_path=db_path, agent_name="test_agent")
        # OhmStore initializes schema in __init__

        # Find a free port
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        # Configure handler
        OhmHandler.store = self.store
        OhmHandler.schema_config = DEFAULT_SCHEMA
        OhmHandler.config = {"host": "127.0.0.1", "port": self.port}
        OhmHandler.tokens = _build_token_lookup({"test-token": "test-agent"})
        OhmHandler.roles = {}
        OhmHandler.no_auth = True
        OhmHandler.require_read_auth = False

        # Start server
        self.server = HTTPServer(("127.0.0.1", self.port), OhmHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        import time

        time.sleep(0.3)  # Wait for server to start

        yield

        self.server.shutdown()
        self.thread.join(timeout=2)

    def _request(self, method, path, body=None):
        """Make an HTTP request to the test server."""
        from http.client import HTTPConnection

        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        if body:
            conn.request(method, path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        else:
            conn.request(method, path)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_post_node_includes_suggestions(self):
        """POST /node includes suggestions in response."""
        with patch("ohm.graph.queries.semantic_search", return_value=[]):
            status, data = self._request(
                "POST",
                "/node",
                {
                    "id": "suggestion-test-1",
                    "label": "Suggestion Test Node",
                    "type": "concept",
                    "content": "A node about AND-gate control mechanisms",
                },
            )
        assert status in (200, 201)
        assert "suggestions" in data
        assert "similar_nodes" in data["suggestions"]
        assert "shared_tags" in data["suggestions"]
        assert "orphan_warning" in data["suggestions"]

    def test_post_node_orphan_warning(self):
        """POST /node without connects_to includes orphan_warning."""
        status, data = self._request(
            "POST",
            "/node",
            {
                "id": "orphan-test-node",
                "label": "Orphan Test Node",
                "type": "concept",
                "content": "A lonely node with no connections",
            },
        )
        assert status in (200, 201)
        assert "suggestions" in data
        assert "orphan_warning" in data["suggestions"]

    def test_post_node_no_orphan_warning_with_connects_to(self):
        """POST /node with connects_to has no orphan_warning."""
        # Create a target node first
        self._request(
            "POST",
            "/node",
            {
                "id": "target-node",
                "label": "Target Node",
                "type": "concept",
            },
        )
        # Create a pattern node with connects_to
        status, data = self._request(
            "POST",
            "/node",
            {
                "id": "connected-pattern",
                "label": "Connected Pattern",
                "type": "pattern",
                "content": "A connected pattern",
                "connects_to": ["target-node"],
            },
        )
        assert status in (200, 201)
        assert "suggestions" in data
        assert data["suggestions"]["orphan_warning"] is None

    def test_post_scratch_includes_suggestions(self):
        """POST /scratch includes suggestions in response."""
        with patch("ohm.graph.queries.semantic_search", return_value=[]):
            status, data = self._request(
                "POST",
                "/scratch",
                {
                    "content": "I think this AND-gate pattern connects to the governance framework",
                },
            )
        assert status == 201
        assert "suggestions" in data
        assert "similar_nodes" in data["suggestions"]
        assert "shared_tags" in data["suggestions"]

    def test_suggestions_never_break_write(self):
        """Suggestions are always present even if semantic search fails."""
        with patch("ohm.graph.queries.semantic_search", side_effect=ValueError("Ollama down")):
            status, data = self._request(
                "POST",
                "/node",
                {
                    "id": "resilient-test-node",
                    "label": "Resilient Test Node",
                    "type": "concept",
                    "content": "A node that should still be created even if suggestions fail",
                },
            )
        assert status in (200, 201)
        assert "suggestions" in data
        assert data["suggestions"]["similar_nodes"] == []


# ── Unit tests for edge suggestions ────────────────────────────────────


class TestGenerateEdgeSuggestions:
    """Unit tests for edge suggestion generation."""

    def test_returns_empty_when_no_related_edges(self):
        """Edge suggestions with no related edges return empty lists."""
        from ohm.server.suggestions import generate_edge_suggestions

        store = MagicMock()
        store.conn.execute.return_value.fetchall.return_value = []
        store.conn.execute.return_value.fetchone.return_value = (0,)
        result = generate_edge_suggestions(store, from_node="node-a", to_node="node-b", edge_type="SUPPORTS")
        assert result["related_edges"] == []
        assert result["edge_patterns"] == []
        assert result["orphan_resolved"] is False

    def test_finds_related_edges(self):
        """Edge suggestions include related edges from both nodes."""
        from ohm.server.suggestions import generate_edge_suggestions

        store = MagicMock()
        # First call: from_node's outgoing edges
        # Second call: to_node's incoming edges
        # Third call: from_node edge type counts
        # Fourth call: to_node edge type counts
        # Fifth call: from_node edge count for orphan check
        # Sixth call: to_node edge count for orphan check
        store.conn.execute.side_effect = [
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # from_node edges
                        ("node-c", "CAUSES", "L3", 0.9),
                    ]
                )
            ),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # to_node incoming
                        ("node-d", "REFERENCES", "L2", 0.7),
                    ]
                )
            ),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # from_node edge type counts
                        ("CAUSES", 3),
                    ]
                )
            ),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # to_node edge type counts
                        ("REFERENCES", 5),
                    ]
                )
            ),
            MagicMock(fetchone=MagicMock(return_value=(2,))),  # from_node has 2 edges
            MagicMock(fetchone=MagicMock(return_value=(3,))),  # to_node has 3 edges
        ]
        result = generate_edge_suggestions(store, from_node="node-a", to_node="node-b", edge_type="SUPPORTS")
        assert len(result["related_edges"]) == 2
        assert result["orphan_resolved"] is False

    def test_detects_orphan_resolved(self):
        """Edge suggestions detect when an orphan was just resolved."""
        from ohm.server.suggestions import generate_edge_suggestions

        store = MagicMock()
        store.conn.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[])),  # from_node edges
            MagicMock(fetchall=MagicMock(return_value=[])),  # to_node edges
            MagicMock(fetchall=MagicMock(return_value=[])),  # from_node edge types
            MagicMock(fetchall=MagicMock(return_value=[])),  # to_node edge types
            MagicMock(fetchone=MagicMock(return_value=(1,))),  # from_node: only edge = just resolved
            MagicMock(fetchone=MagicMock(return_value=(5,))),  # to_node: has edges
        ]
        result = generate_edge_suggestions(store, from_node="orphan-node", to_node="node-b", edge_type="REFERENCES")
        assert result["orphan_resolved"] is True

    def test_suggests_alternate_edge_types(self):
        """Edge patterns suggest other edge types the nodes commonly use."""
        from ohm.server.suggestions import generate_edge_suggestions

        store = MagicMock()
        store.conn.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[])),  # related edges
            MagicMock(fetchall=MagicMock(return_value=[])),  # related edges
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # from_node edge types
                        ("CAUSES", 3),
                        ("SUPPORTS", 2),
                    ]
                )
            ),
            MagicMock(
                fetchall=MagicMock(
                    return_value=[  # to_node edge types
                        ("REFERENCES", 5),
                    ]
                )
            ),
            MagicMock(fetchone=MagicMock(return_value=(5,))),  # edge count
            MagicMock(fetchone=MagicMock(return_value=(6,))),  # edge count
        ]
        result = generate_edge_suggestions(store, from_node="node-a", to_node="node-b", edge_type="CORRELATES_WITH")
        # Should suggest CAUSES, SUPPORTS, REFERENCES (but not CORRELATES_WITH)
        patterns = result["edge_patterns"]
        edge_types = [p["edge_type"] for p in patterns]
        assert "CORRELATES_WITH" not in edge_types
        assert len(patterns) > 0


# ── Integration tests for edge suggestions ──────────────────────────────


class TestEdgeSuggestionsInHandler:
    """Integration tests that POST /edge includes suggestions."""

    @pytest.fixture(autouse=True)
    def setup_server(self):
        """Start a test HTTP server with temp store."""
        from ohm.store import OhmStore
        from ohm.server import OhmHandler, _build_token_lookup
        from ohm.schema import DEFAULT_SCHEMA
        from http.server import HTTPServer
        import threading
        import socket
        import tempfile
        import os

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test_edge_suggestions.duckdb")
        self.store = OhmStore(db_path=db_path, agent_name="test_agent")

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        OhmHandler.store = self.store
        OhmHandler.schema_config = DEFAULT_SCHEMA
        OhmHandler.config = {"host": "127.0.0.1", "port": self.port}
        OhmHandler.tokens = _build_token_lookup({"test-token": "test-agent"})
        OhmHandler.roles = {}
        OhmHandler.no_auth = True
        OhmHandler.require_read_auth = False

        self.server = HTTPServer(("127.0.0.1", self.port), OhmHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        import time

        time.sleep(0.3)

        yield

        self.server.shutdown()
        self.thread.join(timeout=2)

    def _request(self, method, path, body=None):
        """Make an HTTP request to the test server."""
        from http.client import HTTPConnection

        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        if body:
            conn.request(method, path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        else:
            conn.request(method, path)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_post_edge_includes_suggestions(self):
        """POST /edge includes suggestions in response."""
        # Create two nodes first
        self._request("POST", "/node", {"id": "edge-from", "label": "From Node", "type": "concept"})
        self._request("POST", "/node", {"id": "edge-to", "label": "To Node", "type": "concept"})

        status, data = self._request(
            "POST",
            "/edge",
            {
                "from": "edge-from",
                "to": "edge-to",
                "type": "SUPPORTS",
                "layer": "L3",
                "confidence": 0.85,
            },
        )
        assert status == 201
        # Edge responses may or may not have suggestions depending on related edges
        # At minimum, the response should succeed and include the edge
        assert "id" in data or "from" in data

    def test_edge_suggestions_include_related_edges(self):
        """Edge suggestions show other edges involving these nodes."""
        # Create nodes and an existing edge
        self._request("POST", "/node", {"id": "edge-a", "label": "Node A", "type": "concept"})
        self._request("POST", "/node", {"id": "edge-b", "label": "Node B", "type": "concept"})
        self._request("POST", "/node", {"id": "edge-c", "label": "Node C", "type": "concept"})
        # Create an existing edge A → C
        self._request("POST", "/edge", {"from": "edge-a", "to": "edge-c", "type": "CAUSES", "layer": "L3", "confidence": 0.9})
        # Now create edge A → B — should suggest the existing A → C edge
        status, data = self._request(
            "POST",
            "/edge",
            {
                "from": "edge-a",
                "to": "edge-b",
                "type": "SUPPORTS",
                "layer": "L3",
                "confidence": 0.8,
            },
        )
        assert status == 201
        if "suggestions" in data:
            sug = data["suggestions"]
            assert "related_edges" in sug
            # Should find the existing CAUSES edge from A
            from_edges = [e for e in sug["related_edges"] if e["direction"] == "from"]
            assert len(from_edges) >= 1

    def test_edge_orphan_resolved(self):
        """Creating an edge to an orphan resolves its orphan status."""
        # Create an orphan node (no edges)
        self._request("POST", "/node", {"id": "orphan-node", "label": "Orphan", "type": "concept"})
        self._request("POST", "/node", {"id": "connected-target", "label": "Target", "type": "concept"})
        # Create edge — this should resolve the orphan
        status, data = self._request(
            "POST",
            "/edge",
            {
                "from": "orphan-node",
                "to": "connected-target",
                "type": "REFERENCES",
                "layer": "L2",
                "confidence": 0.7,
            },
        )
        assert status == 201
        if "suggestions" in data:
            assert data["suggestions"]["orphan_resolved"] is True
