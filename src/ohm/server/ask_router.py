"""AskRouter — intent-based routing for POST /ask.

Converts a natural-language question (and optional explicit intent) into a
structured routing decision.  The handler can either use the decision to
produce an immediate routing payload or dispatch to the matching internal
endpoint.

Intents:
    analytics  -> semantic-layer metrics (/metrics/semantic)
    explore    -> graph search / neighborhood (/search, /neighborhood, /deep)
    challenge  -> existing challenge flow (/challenge/{id})
    what-if    -> inference / game_theory / pomdp (/inference, /game, /policy)
    help       -> OpenAPI summary (/openapi.json)
    ask        -> conversational synthesis (existing POST /ask behaviour)

The router is intentionally stateless and does not touch the graph.  It is
unit-testable without a running DuckDB or HTTP server.
"""

from __future__ import annotations

import re
from typing import Any


INTENT_KEYWORDS: dict[str, list[str]] = {
    "analytics": [
        "metrics",
        "analytics",
        "kpi",
        "dashboard",
        "measure",
        "measurement",
        "statistic",
        "stats",
        "semantic-layer metrics",
        "semantic metrics",
        "metric",
        "how many",
        "count of",
        "trend",
        "health score",
    ],
    "explore": [
        "explore",
        "search",
        "find",
        "lookup",
        "look up",
        "neighborhood",
        "around",
        "connected to",
        "related to",
        "what is linked",
        "what relates to",
        "graph",
        "show me",
        "where is",
        "path from",
        "path to",
        "similar to",
        "semantic",
    ],
    "challenge": [
        "challenge",
        "contradict",
        "disagree",
        "objection",
        "refute",
        "rebuttal",
        "flaw",
        "what is wrong",
        "why is this wrong",
        "critique",
        "criticism",
    ],
    "what-if": [
        "what if",
        "what-if",
        "scenario",
        "simulate",
        "game theory",
        "nash",
        "policy",
        "pomdp",
        "belief",
        "decision",
        "best action",
        "expected value",
        "intervene",
        "inference",
        "would happen",
        "predict",
        "what happens",
    ],
    "help": [
        "help",
        "openapi",
        "api docs",
        "documentation",
        "what can you do",
        "endpoints",
        "how do i",
        "how to use",
        "schema",
        "layers",
    ],
}

# Default when no strong keyword match is detected.
DEFAULT_INTENT = "ask"

# Canonical internal handler names (used in the response payload).
HANDLER_MAP: dict[str, str] = {
    "analytics": "semantic_layer_metrics",
    "explore": "graph_search_neighborhood",
    "challenge": "challenge_flow",
    "what-if": "inference_game_theory",
    "help": "openapi_summary",
    "ask": "conversational_synthesis",
}

# Endpoint paths for link generation.
ENDPOINT_LINKS: dict[str, list[str]] = {
    "analytics": ["/metrics/semantic", "/metrics/semantic/actions"],
    "explore": ["/search", "/semantic_search", "/neighborhood/{id}"],
    "challenge": ["/challenge/{id}"],
    "what-if": ["/inference", "/game", "/nash", "/policy"],
    "help": ["/openapi.json", "/layers", "/schema"],
    "ask": ["/ask"],
}


class AskRouter:
    """Route an /ask request to the right internal handler based on intent."""

    def __init__(self, default_intent: str = DEFAULT_INTENT) -> None:
        self.default_intent = default_intent
        self._compiled = self._compile_keywords()

    def _compile_keywords(self) -> dict[str, re.Pattern]:
        compiled: dict[str, re.Pattern] = {}
        for intent, words in INTENT_KEYWORDS.items():
            # Sort by length descending so longer multi-word phrases match first.
            patterns = sorted((re.escape(w) for w in words), key=len, reverse=True)
            # Word boundaries that treat underscores as part of words so node IDs
            # like 'andgate' are not mistaken for the keyword 'and' inside them.
            boundary_prefix = r"(?:^|(?<![a-z0-9_]))"
            boundary_suffix = r"(?:$|(?![a-z0-9_]))"
            compiled[intent] = re.compile(
                boundary_prefix + r"(?:" + "|".join(patterns) + r")" + boundary_suffix,
                re.IGNORECASE,
            )
        return compiled

    def detect_intent(self, question: str, declared_intent: str | None = None) -> str:
        """Resolve intent from explicit field or keyword heuristics.

        Args:
            question: The natural-language question text.
            declared_intent: Optional explicit intent supplied by the caller.

        Returns:
            One of the canonical intent strings.
        """
        if declared_intent:
            normalized = declared_intent.strip().lower()
            if normalized in HANDLER_MAP:
                return normalized
            # Treat legacy aliases as ask.
            if normalized in ("question", "query"):
                return "ask"

        if not question:
            return self.default_intent

        question_norm = question.lower()
        scores: dict[str, int] = {}
        for intent, pattern in self._compiled.items():
            scores[intent] = len(pattern.findall(question_norm))

        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        if scores[best] > 0:
            # If analytics and explore tie, prefer analytics when "metrics" appears.
            if scores.get("analytics", 0) > 0 and scores.get("explore", 0) > 0:
                if "metrics" in question_norm:
                    return "analytics"
            return best
        return self.default_intent

    def build_payload(
        self,
        question: str,
        intent: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a handler-specific payload from the original request body.

        The payload preserves the caller's parameters while adding intent-
        specific hints (e.g. target node, depth, include_inference).
        """
        base = dict(body) if body else {}
        # Avoid echoing the raw question twice if it is already present.
        base.setdefault("question", question)
        base.setdefault("intent", intent)

        payload: dict[str, Any] = {"question": question, "params": base}

        if intent == "analytics":
            payload["target_endpoints"] = ["GET /metrics/semantic"]
            payload["actions"] = base.get("actions", False)
        elif intent == "explore":
            payload["target_endpoints"] = ["GET /search", "GET /semantic_search", "GET /neighborhood/{id}"]
            payload["depth"] = min(max(int(base.get("depth", 2)), 1), 3)
            payload["limit"] = min(max(int(base.get("limit", 5)), 1), 20)
            # If the question looks like a node id, surface it as a candidate target.
            candidate = _extract_node_id(question)
            # Ignore words that are also strong intent keywords.
            if candidate in INTENT_KEYWORDS.get("explore", []) + INTENT_KEYWORDS.get("what-if", []):
                candidate = None
            if candidate:
                payload["candidate_target"] = candidate
        elif intent == "challenge":
            payload["target_endpoints"] = ["POST /challenge/{id}"]
            payload["edge_id"] = base.get("edge_id")
            payload["reason"] = base.get("reason", question)
            payload["confidence"] = base.get("confidence", 0.5)
        elif intent == "what-if":
            payload["target_endpoints"] = ["GET /inference", "GET /game", "GET /policy"]
            payload["include_inference"] = base.get("include_inference", True)
            candidate = _extract_node_id(question)
            # Ignore words that are also strong intent keywords.
            if candidate in INTENT_KEYWORDS.get("what-if", []):
                candidate = None
            # Explicit target always wins.
            if base.get("target"):
                candidate = str(base["target"])
            if candidate:
                payload["target"] = candidate
            else:
                payload.pop("target", None)
        elif intent == "help":
            payload["target_endpoints"] = ["GET /openapi.json", "GET /layers"]
        else:  # ask / default
            payload["target_endpoints"] = ["POST /ask"]
            payload["include_inference"] = base.get("include_inference", True)
            payload["depth"] = min(max(int(base.get("depth", 2)), 1), 3)

        return payload

    def route(
        self,
        question: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a structured routing decision.

        The response shape is::

            {
                "intent": <canonical intent>,
                "handler": <internal handler name>,
                "payload": <handler-specific payload>,
                "links": [<related endpoint paths>],
            }
        """
        declared = (body or {}).get("intent")
        intent = self.detect_intent(question, declared_intent=declared)
        handler = HANDLER_MAP.get(intent, HANDLER_MAP[self.default_intent])
        payload = self.build_payload(question, intent, body)
        links = list(ENDPOINT_LINKS.get(intent, ENDPOINT_LINKS[self.default_intent]))

        return {
            "intent": intent,
            "handler": handler,
            "payload": payload,
            "links": links,
        }


def _extract_node_id(text: str) -> str | None:
    """Crude node-id candidate extractor.

    OHM identifiers are lowercase with underscores.  This heuristic pulls
    the first token that looks like an id (alphanumeric + underscores),
    skipping single letters and common stop-words.  It intentionally avoids
    over-committing; callers still validate via the schema/validation layer.
    """
    if not text:
        return None
    stop_words = {
        "what",
        "if",
        "the",
        "and",
        "edge",
        "node",
        "for",
        "how",
        "are",
        "this",
        "that",
        "with",
        "from",
        "causes",
        "fails",
        "under",
        "pressure",
        "around",
        "explore",
        "search",
        "find",
        "lookup",
        "look",
        "up",
        "neighborhood",
        "challenge",
        "metrics",
        "semantic",
        "what-if",
        "whatif",
    }
    # Remove common punctuation around identifiers.
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    for token in cleaned.split():
        token = token.strip()
        if token in stop_words:
            continue
        if len(token) >= 3 and re.fullmatch(r"[a-z0-9_]+", token):
            return token
    return None


# Convenience function for callers that only need the intent.
def detect_intent(question: str, declared_intent: str | None = None) -> str:
    """Standalone intent detection."""
    return AskRouter().detect_intent(question, declared_intent=declared_intent)
