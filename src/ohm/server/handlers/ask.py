"""Ask handler — intent-routed POST /ask.

This module replaces the previous monolithic _post_ask in
ohm.server.handlers.graph with a thin wrapper that:

  1. Detects the user's intent (or uses an explicit `intent` field).
  2. Returns a structured routing payload for most intents.
  3. Continues to dispatch to the legacy conversational synthesis path
     for the default `ask` intent so existing /ask behaviour is preserved.
"""

from __future__ import annotations

from typing import Any

from ohm.server.ask_router import AskRouter, DEFAULT_INTENT


class AskHandlerMixin:
    """Handler mixin for POST /ask intent routing."""

    def _post_ask(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /ask — route by intent, then dispatch or return routing payload."""
        question = str(body.get("question", "")).strip()
        if not question:
            self._json_response(400, {"error": "missing_parameter", "message": "'question' is required"})
            return

        router = AskRouter()
        decision = router.route(question, body)
        intent = decision["intent"]

        # Explicit routing: callers who want only the routing decision can pass
        # "route_only": true.  This is useful for clients that dispatch internally.
        route_only = body.get("route_only", False)
        if route_only:
            decision["agent"] = agent
            self._json_response(200, decision)
            return

        # Dispatch to the matching internal handler.  The default `ask` intent
        # falls back to the legacy conversational synthesis implementation.
        if intent == DEFAULT_INTENT:
            self._post_ask_synthesis(path, qs, body, agent)
            return

        # The canonical intent name contains a hyphen; map to a valid Python
        # method name by replacing it with an underscore.
        dispatch_method = f"_post_ask_{intent.replace('-', '_')}"
        dispatch = getattr(self, dispatch_method, None)
        if dispatch and not body.get("route_only"):
            dispatch(path, qs, body, agent, decision)
            return

        # No internal dispatch implementation yet — return the routing decision.
        decision["agent"] = agent
        decision["note"] = "routing decision returned; internal handler not yet implemented"
        self._json_response(200, decision)

    # ── Internal dispatch shims ───────────────────────────────────────────

    def _post_ask_analytics(self, path: str, qs: dict, body: dict, agent: str, decision: dict[str, Any]) -> None:
        """analytics intent → semantic-layer metrics."""
        params = decision.get("payload", {}).get("params", {})
        actions = params.get("actions", False)
        # Reuse the existing GET /metrics/semantic handler, toggling actions.
        from urllib.parse import parse_qs, urlencode

        sub_qs: dict[str, list[str]] = {}
        if actions:
            sub_qs["actions"] = ["true"]
        self._get_metrics_semantic(path, sub_qs)

    def _post_ask_explore(self, path: str, qs: dict, body: dict, agent: str, decision: dict[str, Any]) -> None:
        """explore intent → graph search / neighborhood."""
        payload = decision.get("payload", {})
        candidate = payload.get("candidate_target")
        depth = payload.get("depth", 2)
        limit = payload.get("limit", 5)

        if candidate:
            # Prefer neighborhood if we extracted a candidate node id.
            from urllib.parse import parse_qs

            sub_qs = parse_qs(f"depth={depth}&limit={limit}")
            self._get_neighborhood(f"/neighborhood/{candidate}", sub_qs)
            return

        # Otherwise fall back to text + semantic search.
        from urllib.parse import parse_qs

        sub_qs = parse_qs(f"q={decision['payload']['question']}&limit={limit}")
        self._get_search(path, sub_qs)

    def _post_ask_challenge(self, path: str, qs: dict, body: dict, agent: str, decision: dict[str, Any]) -> None:
        """challenge intent → existing challenge flow."""
        edge_id = decision.get("payload", {}).get("edge_id")
        if edge_id:
            self._post_challenge(f"/challenge/{edge_id}", qs, body, agent)
            return
        # Without an edge_id, return a routing decision so the caller can pick.
        decision["agent"] = agent
        decision["note"] = "edge_id required to challenge; pass edge_id in body or use POST /challenge/{id}"
        self._json_response(200, decision)

    def _post_ask_what_if(self, path: str, qs: dict, body: dict, agent: str, decision: dict[str, Any]) -> None:
        """what-if intent → inference / game_theory / pomdp."""
        payload = decision.get("payload", {})
        target = payload.get("target") or body.get("target")
        if not target:
            # No target node id — return routing decision for caller to resolve.
            decision["agent"] = agent
            decision["note"] = "target node_id required for what-if inference"
            self._json_response(200, decision)
            return

        from urllib.parse import parse_qs

        sub_qs = parse_qs(f"target={target}")
        self._get_inference(path, sub_qs)

    def _post_ask_help(self, path: str, qs: dict, body: dict, agent: str, decision: dict[str, Any]) -> None:
        """help intent → OpenAPI summary."""
        self._get_infra_openapi(path, qs)
