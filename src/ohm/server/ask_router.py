"""AskRouter — intent-based routing for POST /ask.

Converts a natural-language question (and optional explicit intent) into a
structured routing decision.  The handler can either use the decision to
produce an immediate routing payload or dispatch to the matching internal
endpoint.

Intents:
    analytics       -> semantic-layer metrics (/metrics/semantic)
    explore         -> graph search / neighborhood (/search, /neighborhood, /deep)
    challenge       -> existing challenge flow (/challenge/{id})
    what-if         -> inference / game_theory / pomdp (/inference, /game, /policy)
    help            -> OpenAPI summary (/openapi.json)
    ask             -> conversational synthesis (existing POST /ask behaviour)

OHM-790: Statistical and deliberation intents:
    belief          -> ohm_belief tool
    why             -> ohm_belief (focus=drivers)
    what_if         -> ohm_intervene tool
    voi             -> ohm_voi tool
    simulate        -> ohm_monte_carlo tool
    scenario        -> ohm_pert tool
    forecast        -> ohm_markov tool
    calibrate       -> ohm_belief + method divergence
    disagreement    -> ohm_belief + conversation state
    decision_ready  -> conversation-state threshold check

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
        "decision",
        "best action",
        "expected value",
        "intervene",
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
    # ── OHM-790: Statistical and deliberation intents ──
    "belief": [
        "how likely",
        "what is the probability",
        "what's the probability",
        "what are the odds",
        "p of",
        "probability of",
        "chance of",
        "belief about",
        "posterior for",
        "how confident",
    ],
    "why": [
        "why does it believe",
        "why does the graph",
        "what drives the belief",
        "what evidence supports",
        "why is the probability",
        "what causes the belief",
        "why does ohm think",
        "belief drivers",
        "why is it likely",
    ],
    "what_if": [
        "what if i",
        "what if we",
        "intervene on",
        "set to true",
        "set to false",
        "force state",
        "do intervention",
        "counterfactual",
    ],
    "voi": [
        "what should i observe",
        "what should i look at",
        "value of information",
        "voi",
        "what to measure",
        "best observation",
        "most informative",
        "worth observing",
    ],
    "simulate": [
        "run simulations",
        "run it many times",
        "monte carlo",
        "simulate many",
        "cascade probability",
        "stochastic spread",
        "thousand scenarios",
    ],
    "scenario": [
        "what is the range",
        "pert",
        "best case worst case",
        "optimistic pessimistic",
        "projected timeline",
        "schedule estimate",
        "critical path",
    ],
    "forecast": [
        "where is this heading",
        "markov",
        "steady state",
        "long run",
        "expected steps",
        "absorbing state",
        "what is the trajectory",
    ],
    "calibrate": [
        "do the methods agree",
        "method divergence",
        "calibrate the belief",
        "how accurate",
        "reliability",
        "bias correction",
        "calibration",
    ],
    "disagreement": [
        "who disagrees",
        "where is the disagreement",
        "which agents disagree",
        "split belief",
        "divergent views",
    ],
    "decision_ready": [
        "can we decide",
        "are we ready to decide",
        "decision thresholds",
        "is there enough evidence",
        "can we commit",
        "decision quality",
    ],
}

# Default when no strong keyword match is detected.
DEFAULT_INTENT = "ask"

# OHM-790: Intents that use _build_statistical_payload
_STATISTICAL_INTENTS = frozenset(
    {
        "belief",
        "why",
        "what_if",
        "voi",
        "simulate",
        "scenario",
        "forecast",
        "calibrate",
        "disagreement",
        "decision_ready",
    }
)

# Canonical internal handler names (used in the response payload).
HANDLER_MAP: dict[str, str] = {
    "analytics": "semantic_layer_metrics",
    "explore": "graph_search_neighborhood",
    "challenge": "challenge_flow",
    "what-if": "inference_game_theory",
    "help": "openapi_summary",
    "ask": "conversational_synthesis",
    # OHM-790: Statistical intents
    "belief": "belief_tool",
    "why": "belief_drivers",
    "what_if": "intervene_tool",
    "voi": "voi_tool",
    "simulate": "monte_carlo_tool",
    "scenario": "pert_tool",
    "forecast": "markov_tool",
    "calibrate": "calibration_check",
    "disagreement": "disagreement_scan",
    "decision_ready": "decision_threshold_check",
}

# MCP tool mappings for each intent (OHM-790).
INTENT_TOOLS: dict[str, str] = {
    "belief": "ohm_belief",
    "why": "ohm_belief",
    "what_if": "ohm_intervene",
    "voi": "ohm_voi",
    "simulate": "ohm_monte_carlo",
    "scenario": "ohm_pert",
    "forecast": "ohm_markov",
    "calibrate": "ohm_belief",
    "disagreement": "ohm_belief",
    "decision_ready": "ohm_conversation",
}

# Endpoint paths for link generation.
ENDPOINT_LINKS: dict[str, list[str]] = {
    "analytics": ["/metrics/semantic", "/metrics/semantic/actions"],
    "explore": ["/search", "/semantic_search", "/neighborhood/{id}"],
    "challenge": ["/challenge/{id}"],
    "what-if": ["/inference", "/game", "/nash", "/policy"],
    "help": ["/openapi.json", "/layers", "/schema"],
    "ask": ["/ask"],
    # OHM-790
    "belief": ["/belief"],
    "why": ["/belief"],
    "what_if": ["/intervene"],
    "voi": ["/voi"],
    "simulate": ["/monte-carlo/{id}"],
    "scenario": ["/inference?pert=1"],
    "forecast": ["/markov/absorbing", "/markov/expected_steps"],
    "calibrate": ["/belief", "/calibration/{agent}"],
    "disagreement": ["/belief"],
    "decision_ready": ["/conversation"],
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
        elif intent in _STATISTICAL_INTENTS:
            payload = self._build_statistical_payload(question, intent, body, base)
        else:  # ask / default
            payload["target_endpoints"] = ["POST /ask"]
            payload["include_inference"] = base.get("include_inference", True)
            payload["depth"] = min(max(int(base.get("depth", 2)), 1), 3)

        return payload

    def _build_statistical_payload(
        self,
        question: str,
        intent: str,
        body: dict[str, Any] | None,
        base: dict[str, Any],
    ) -> dict[str, Any]:
        """Build payload for statistical/deliberation intents (OHM-790)."""
        target = base.get("target") or _extract_node_id(question) or ""
        tool = INTENT_TOOLS.get(intent, "")
        payload: dict[str, Any] = {
            "question": question,
            "params": base,
        }

        if intent == "belief":
            payload["target_endpoints"] = ["GET /belief"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target}}]
            payload["reply_to_agent"] = f"Checking the graph's belief on {target or 'the target'}..."
        elif intent == "why":
            payload["target_endpoints"] = ["GET /belief"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target, "focus": "drivers"}}]
            payload["reply_to_agent"] = f"Analyzing what drives the belief on {target or 'the target'}..."
        elif intent == "what_if":
            payload["target_endpoints"] = ["GET /intervene"]
            payload["target"] = target
            state = base.get("state", 0)
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target, "state": state}}]
            payload["reply_to_agent"] = f"Running counterfactual: {target} set to {state}..."
        elif intent == "voi":
            payload["target_endpoints"] = ["GET /voi"]
            decision = target or base.get("decision", "")
            payload["tool_calls"] = [{"tool": tool, "arguments": {"decision": decision}}]
            payload["reply_to_agent"] = "Ranking observations by value of information..."
        elif intent == "simulate":
            payload["target_endpoints"] = [f"GET /monte-carlo/{target or '{id}'}"]
            payload["target"] = target
            n = base.get("n_simulations", 1000)
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target, "n_simulations": n}}]
            payload["reply_to_agent"] = f"Running {n} Monte Carlo simulations on {target or 'the target'}..."
        elif intent == "scenario":
            payload["target_endpoints"] = ["GET /inference?pert=1"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target}}]
            payload["reply_to_agent"] = f"Computing PERT range estimates for {target or 'the target'}..."
        elif intent == "forecast":
            payload["target_endpoints"] = ["/markov/absorbing", "/markov/expected_steps"]
            payload["target"] = target
            analysis = base.get("analysis", "absorbing")
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target, "analysis": analysis}}]
            payload["reply_to_agent"] = f"Running Markov {analysis} analysis on {target or 'the target'}..."
        elif intent == "calibrate":
            payload["target_endpoints"] = ["GET /belief", "GET /calibration/{agent}"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target}}]
            payload["reply_to_agent"] = "Checking method agreement and calibration..."
        elif intent == "disagreement":
            payload["target_endpoints"] = ["GET /belief"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"target": target}}]
            payload["reply_to_agent"] = "Scanning for agent disagreement..."
        elif intent == "decision_ready":
            payload["target_endpoints"] = ["/conversation"]
            payload["target"] = target
            payload["tool_calls"] = [{"tool": tool, "arguments": {"action": "get"}}]
            payload["reply_to_agent"] = "Checking decision thresholds..."

        return payload

    def _compute_confidence(self, question: str, intent: str) -> float:
        """Compute a confidence score for the detected intent (OHM-790)."""
        if not question:
            return 0.0
        pattern = self._compiled.get(intent)
        if not pattern:
            return 0.0
        matches = len(pattern.findall(question.lower()))
        word_count = len(question.split())
        if word_count == 0:
            return 0.0
        density = matches / max(word_count, 1)
        confidence = min(0.5 + density * 2.0, 1.0)
        return round(confidence, 2)

    def _detect_loop_prevention(
        self,
        question: str,
        intent: str,
        conversation_state: dict[str, Any] | None = None,
        explicit_target: str | None = None,
    ) -> list[dict[str, Any]]:
        """Detect loop-prevention patterns and return nudges (OHM-790).

        - Agent asks for belief before stating their own → autonomy nudge
        - Agent repeatedly asks exact posterior → category nudge
        - Agent language strength mismatches graph belief → calibration nudge
        """
        nudges: list[dict[str, Any]] = []
        if not conversation_state:
            return nudges

        topics = conversation_state.get("topics", [])
        target = explicit_target or _extract_node_id(question) or ""

        # Pattern 1: Agent asks for belief before stating their own
        if intent in ("belief", "why", "what-if") and target:
            agent_contribs = conversation_state.get("agent_contributions", {})
            # Check if any agent has made claims about this target
            has_claims = any(contrib.get("claims", 0) > 0 for contrib in agent_contribs.values())
            if not has_claims:
                nudges.append(
                    {
                        "type": "autonomy",
                        "message": "Consider forming your own belief before reading the graph's.",
                        "severity": 2,
                    }
                )

        # Pattern 2: Repeated similar queries
        if target:
            for topic in topics:
                if topic.get("node_id") == target and topic.get("mentions", 0) >= 4:
                    nudges.append(
                        {
                            "type": "loop_detected",
                            "message": f"You've queried {target} {topic['mentions']} times. Consider acting on current information.",
                            "severity": 3,
                        }
                    )
                    break

        return nudges

    def route(
        self,
        question: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a structured routing decision.

        OHM-790: Extended with confidence, tool_calls, reply_to_agent,
        and loop-prevention nudges.

        The response shape is::

            {
                "intent": <canonical intent>,
                "confidence": <0.0-1.0>,
                "handler": <internal handler name>,
                "payload": <handler-specific payload>,
                "links": [<related endpoint paths>],
                "tool_calls": [<optional MCP tool calls>],
                "reply_to_agent": <optional human-readable reply>,
                "loop_prevention": [<optional nudges>],
            }
        """
        declared = (body or {}).get("intent")
        intent = self.detect_intent(question, declared_intent=declared)
        handler = HANDLER_MAP.get(intent, HANDLER_MAP[self.default_intent])
        payload = self.build_payload(question, intent, body)
        links = list(ENDPOINT_LINKS.get(intent, ENDPOINT_LINKS[self.default_intent]))

        result: dict[str, Any] = {
            "intent": intent,
            "confidence": self._compute_confidence(question, intent),
            "handler": handler,
            "payload": payload,
            "links": links,
        }

        # OHM-790: Extract tool_calls and reply_to_agent from payload
        if "tool_calls" in payload:
            result["tool_calls"] = payload["tool_calls"]
        if "reply_to_agent" in payload:
            result["reply_to_agent"] = payload["reply_to_agent"]
        if "target" in payload and payload["target"]:
            result["target"] = payload["target"]

        # OHM-790: Loop-prevention routing
        conv_state = (body or {}).get("conversation_state")
        explicit_target = (body or {}).get("target")
        loop_nudges = self._detect_loop_prevention(question, intent, conv_state, explicit_target)
        if loop_nudges:
            result["loop_prevention"] = loop_nudges

        return result


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
