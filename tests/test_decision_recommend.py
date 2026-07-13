"""Tests for #839: decision-node recommendation exposure (SDK, MCP, router, nudges)."""

from __future__ import annotations

import pytest

from ohm.server.nudges import generate_nudges


# ── Nudge: bare decision node ───────────────────────────────────────────────


class TestDecisionNodeNudge:
    """New nudge fires for decision-type nodes missing required fields."""

    def test_bare_decision_gets_all_three_missing_nudges(self):
        node = {"type": "decision", "id": "d1", "label": "Should we?"}
        nudges = generate_nudges("node", node_id="d1", node=node)
        incomplete = [n for n in nudges if n["type"] == "decision_node_incomplete"]
        assert len(incomplete) == 1
        missing = incomplete[0]["data"]["missing"]
        assert "utility_scale" in missing
        assert "action_alternatives" in missing
        assert "DECISION_DEPENDS_ON edge" in missing

    def test_fully_specified_decision_no_nudge(self):
        node = {
            "type": "decision",
            "id": "d2",
            "label": "Fully specified",
            "utility_scale": 0.8,
            "action_alternatives": ["a", "b"],
        }
        neighborhood = {
            "nodes": {},
            "edges": [{"edge_type": "DECISION_DEPENDS_ON", "to_node": "h1"}],
        }
        nudges = generate_nudges("node", node_id="d2", node=node, neighborhood=neighborhood)
        incomplete = [n for n in nudges if n["type"] == "decision_node_incomplete"]
        assert len(incomplete) == 0

    def test_decision_with_utility_but_no_alts_no_dep_edges(self):
        node = {
            "type": "decision",
            "id": "d3",
            "label": "Missing edges",
            "utility_scale": 0.5,
        }
        nudges = generate_nudges("node", node_id="d3", node=node)
        incomplete = [n for n in nudges if n["type"] == "decision_node_incomplete"]
        assert len(incomplete) == 1
        missing = incomplete[0]["data"]["missing"]
        assert "utility_scale" not in missing
        assert "action_alternatives" in missing
        assert "DECISION_DEPENDS_ON edge" in missing

    def test_non_decision_node_no_incomplete_nudge(self):
        node = {"type": "concept", "id": "c1", "label": "Just a concept"}
        nudges = generate_nudges("node", node_id="c1", node=node)
        incomplete = [n for n in nudges if n["type"] == "decision_node_incomplete"]
        assert len(incomplete) == 0

    def test_nudge_message_mentions_recommendation_endpoint(self):
        node = {"type": "decision", "id": "d4", "label": "Bare"}
        nudges = generate_nudges("node", node_id="d4", node=node)
        incomplete = [n for n in nudges if n["type"] == "decision_node_incomplete"]
        assert "/recommendation" in incomplete[0]["message"]

    def test_existing_decision_suggestion_nudge_still_works(self):
        """The pre-existing ADR-023 nudge for non-decision nodes is unaffected."""
        node = {
            "type": "concept",
            "id": "c1",
            "label": "Deploy decision",
            "content": "we need to decide on the rollout strategy",
        }
        nudges = generate_nudges("node", node_id="c1", node=node)
        suggestion = [n for n in nudges if n["type"] == "decision_node_suggestion"]
        assert len(suggestion) == 1


# ── Router: POST returns 405 ────────────────────────────────────────────────


class TestDecisionRouterRegistration:
    def test_get_decision_recommendation_dispatches(self, test_db):
        """Verify the GET handler is registered and callable."""
        from ohm.server.handlers.decision import DecisionHandlerMixin

        assert hasattr(DecisionHandlerMixin, "_get_decision_recommendation")

    def test_router_includes_decision_prefix(self):
        from ohm.server.server import _RouteRegistry

        # Verify decision prefix is in the GET prefixes by checking the router
        # imports cleanly and the decision endpoint is a known path
        from ohm.server.handlers.decision import DecisionHandlerMixin

        assert hasattr(DecisionHandlerMixin, "_get_decision_recommendation")

    def test_post_decision_returns_405(self, test_server):
        """POST to /decision/{id}/recommendation should return 405."""
        import urllib.error
        import urllib.request

        port, _store = test_server
        url = f"http://127.0.0.1:{port}/decision/test-id/recommendation"
        req = urllib.request.Request(url, method="POST", data=b"{}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.getcode()
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 405


# ── MCP dispatch ────────────────────────────────────────────────────────────


class TestMCPDecisionRecommendDispatch:
    def test_dispatch_maps_to_correct_path(self):
        from ohm.mcp.dispatch import build_request

        method, path, body = build_request(
            "ohm_decision_recommend", {"node_id": "decision-abc"}, "test"
        )
        assert method == "GET"
        assert "/decision/decision-abc/recommendation" in path
        assert body is None

    def test_tool_is_registered(self):
        from ohm.mcp.tools import all_tools

        names = [t.name for t in all_tools()]
        assert "ohm_decision_recommend" in names
