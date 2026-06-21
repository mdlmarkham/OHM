"""Tests for /ask intent routing (AskRouter + POST /ask handler).

These tests are isolated: the unit tests exercise AskRouter directly without a
running server, and the handler integration tests use the in-memory test server
fixture from conftest.py (random port, no fixed network services).
"""

from __future__ import annotations

import pytest

from ohm.server.ask_router import AskRouter, detect_intent


class TestAskRouterUnit:
    """Pure unit tests for AskRouter — no DB, no HTTP server."""

    @pytest.fixture
    def router(self):
        return AskRouter()

    def test_detect_analytics_intent(self, router):
        result = router.route("show me semantic metrics for health score", {})
        assert result["intent"] == "analytics"
        assert result["handler"] == "semantic_layer_metrics"
        assert "/metrics/semantic" in result["links"]
        assert result["payload"]["target_endpoints"]

    def test_detect_explore_intent(self, router):
        result = router.route("explore the neighborhood around andgate", {})
        assert result["intent"] == "explore"
        assert result["handler"] == "graph_search_neighborhood"
        assert result["payload"]["candidate_target"] == "andgate"
        assert "/neighborhood/{id}" in result["links"]

    def test_detect_challenge_intent(self, router):
        result = router.route("challenge this causal claim", {"edge_id": "edge-123"})
        assert result["intent"] == "challenge"
        assert result["handler"] == "challenge_flow"
        assert result["payload"]["edge_id"] == "edge-123"
        assert result["payload"]["confidence"] == 0.5

    def test_detect_what_if_intent(self, router):
        result = router.route("what if andgate fails under pressure", {"target": "andgate"})
        assert result["intent"] == "what-if"
        assert result["handler"] == "inference_game_theory"
        assert result["payload"]["target"] == "andgate"
        assert "/inference" in result["links"]

    def test_detect_help_intent(self, router):
        result = router.route("how do I use the OHM API?", {})
        assert result["intent"] == "help"
        assert result["handler"] == "openapi_summary"
        assert "/openapi.json" in result["links"]

    def test_explicit_intent_overrides_keywords(self, router):
        # The word "metrics" would normally trigger analytics, but explicit help wins.
        result = router.route("what metrics are available?", {"intent": "help"})
        assert result["intent"] == "help"
        assert result["handler"] == "openapi_summary"

    def test_default_fallback_to_ask(self, router):
        result = router.route("something completely unrelated", {})
        assert result["intent"] == "ask"
        assert result["handler"] == "conversational_synthesis"

    def test_empty_question_defaults_to_ask(self, router):
        result = router.route("", {})
        assert result["intent"] == "ask"

    def test_detect_intent_standalone(self):
        assert detect_intent("run analytics") == "analytics"
        assert detect_intent("find related nodes") == "explore"
        assert detect_intent("refute this edge") == "challenge"
        assert detect_intent("what happens if X causes Y") == "what-if"
        assert detect_intent("openapi docs") == "help"
        assert detect_intent("gibberish xyz pdq") == "ask"

    def test_response_shape(self, router):
        result = router.route("show stats", {})
        assert set(result.keys()) == {"intent", "handler", "payload", "links"}
        assert isinstance(result["links"], list)
        assert isinstance(result["payload"], dict)


class TestAskEndpointRouting:
    """Integration tests for POST /ask via the in-memory test server."""

    @pytest.fixture(autouse=True)
    def setup_graph(self, test_server):
        """Create a small test graph for /ask routing tests."""
        port, store = test_server
        from tests.conftest import _request

        _request("POST", port, "/node", body={"id": "andgate", "label": "AND gate", "type": "concept", "confidence": 0.9})
        _request("POST", port, "/node", body={"id": "orgate", "label": "OR gate", "type": "concept", "confidence": 0.8})
        _request("POST", port, "/edge", body={"from": "andgate", "to": "orgate", "type": "CAUSES", "layer": "L3", "confidence": 0.85})

    def test_post_ask_routing_decision(self, test_server):
        """POST /ask with route_only=true returns a routing decision."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request(
            "POST",
            port,
            "/ask",
            body={"question": "show me semantic metrics", "route_only": True},
        )
        assert status == 200
        assert data["intent"] == "analytics"
        assert data["handler"] == "semantic_layer_metrics"
        assert "payload" in data
        assert "links" in data

    def test_post_ask_default_intent(self, test_server):
        """POST /ask with no strong keywords still returns a valid response."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request(
            "POST",
            port,
            "/ask",
            body={"question": "gibberish_unrelated_query_xyz"},
        )
        # Default intent is ask → falls back to legacy synthesis.
        assert status == 200
        assert "synthesis" in data

    def test_post_ask_help_dispatch(self, test_server):
        """POST /ask with help intent dispatches to /openapi.json content."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request(
            "POST",
            port,
            "/ask",
            body={"question": "what endpoints are available?", "intent": "help"},
        )
        assert status == 200
        # The help dispatch returns the OpenAPI JSON payload directly.
        assert data.get("openapi") == "3.0.3"

    def test_post_ask_explore_dispatch(self, test_server):
        """POST /ask with explore intent dispatches to graph search."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request(
            "POST",
            port,
            "/ask",
            body={"question": "explore around andgate", "intent": "explore", "route_only": True},
        )
        assert status == 200
        assert data["intent"] == "explore"
        assert data["payload"].get("candidate_target") == "andgate"

    def test_post_ask_what_if_dispatch(self, test_server):
        """POST /ask with what-if intent and target routes to inference."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request(
            "POST",
            port,
            "/ask",
            body={"question": "what if andgate fails", "intent": "what-if", "target": "andgate"},
        )
        assert status == 200
        # /inference on a real target returns either a posterior or an error
        # because the graph is tiny; either way the dispatch ran.
        assert "posterior" in data or "error" in data or "message" in data

    def test_post_ask_missing_question_returns_400(self, test_server):
        """POST /ask without a question returns 400."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("POST", port, "/ask", body={})
        assert status == 400
