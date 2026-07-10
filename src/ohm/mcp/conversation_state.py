"""Conversation-state MVP with TTL and budget eviction (OHM-789).

Per-thread conversation state tracks what is "in play" in a conversation:
active topics, open deliberations, nudge history, agent contributions,
pending Socratic questions, and thread-level uncertainty. This is a
transient overlay on the durable graph — it lives in the gateway process
memory and is evicted by TTL or budget.

Storage is in-memory (consistent with _SESSION_NUDGES_SEEN and
_SESSION_PROFILES). This is "hot" state that needs to be accessible on
every tool response to populate ohm_context. Full multi-worker
consistency requires shared storage (Redis, DB) — tracked as a follow-up.

TTL and budget defaults (from OHM-789):
    Hot thread state:     64 KB, 24h active / 7d dormant
    Nudge history:       last 50 per thread
    Pending questions:   20 open, 30 min unanswered
    Agent contribution:  thread lifetime, 1 KB per agent
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── TTL and budget constants ──

_ACTIVE_TTL = 24 * 3600  # 24 hours since last access → dormant
_DORMANT_TTL = 7 * 24 * 3600  # 7 days since last access → evict
_MAX_NUDGE_HISTORY = 50
_MAX_PENDING_QUESTIONS = 20
_MAX_PARTICIPANTS = 50
_MAX_TOPICS = 30
_MAX_DELIBERATIONS = 20
_MAX_BYTES = 64 * 1024  # 64 KB per thread state
_MAX_THREADS = 5000  # global cap to prevent unbounded growth
_AGENT_CONTRIBUTION_BYTES = 1024  # 1 KB per agent


@dataclass
class ConversationState:
    """Per-thread conversation state (OHM-789)."""

    thread_id: str
    participants: list[str] = field(default_factory=list)
    topics: list[dict[str, Any]] = field(default_factory=list)
    deliberations: list[dict[str, Any]] = field(default_factory=list)
    nudge_history: list[dict[str, Any]] = field(default_factory=list)
    agent_contributions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_questions: list[dict[str, Any]] = field(default_factory=list)
    thread_entropy: float = 0.0
    needs_attention: bool = False
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_access = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "participants": list(self.participants),
            "topics": list(self.topics),
            "deliberations": list(self.deliberations),
            "nudge_history": list(self.nudge_history),
            "agent_contributions": dict(self.agent_contributions),
            "pending_questions": list(self.pending_questions),
            "thread_entropy": self.thread_entropy,
            "needs_attention": self.needs_attention,
        }

    def estimate_bytes(self) -> int:
        import json

        try:
            return len(json.dumps(self.to_dict()).encode())
        except Exception:
            return 0


class ConversationStateStore:
    """In-memory store for per-thread conversation state with TTL/budget.

    Keyed by thread_id. Entries are evicted by:
    - Active TTL: 24h since last access → marked dormant
    - Dormant TTL: 7d since last access → evicted
    - Budget: per-thread size cap (64 KB), per-field caps
    - Global cap: 5000 threads max
    """

    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}

    def get_state(self, thread_id: str) -> dict[str, Any] | None:
        state = self._states.get(thread_id)
        if state is None:
            return None
        state.touch()
        return state.to_dict()

    def get_or_create(self, thread_id: str) -> ConversationState:
        state = self._states.get(thread_id)
        if state is None:
            state = ConversationState(thread_id=thread_id)
            self._states[thread_id] = state
            self._enforce_global_cap()
        state.touch()
        return state

    def update_state(self, thread_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        state = self.get_or_create(thread_id)

        if "participants" in updates:
            self._merge_participants(state, updates["participants"])
        if "topics" in updates:
            self._merge_topics(state, updates["topics"])
        if "deliberations" in updates:
            self._merge_deliberations(state, updates["deliberations"])
        if "nudge_history" in updates:
            self._append_nudges(state, updates["nudge_history"])
        if "agent_contributions" in updates:
            self._merge_contributions(state, updates["agent_contributions"])
        if "pending_questions" in updates:
            self._merge_questions(state, updates["pending_questions"])
        if "thread_entropy" in updates:
            state.thread_entropy = float(updates["thread_entropy"])
        if "needs_attention" in updates:
            state.needs_attention = bool(updates["needs_attention"])

        self._enforce_budget(state)
        return state.to_dict()

    def record_mention(
        self,
        thread_id: str,
        node_id: str,
        agent_id: str,
        belief: dict[str, float] | None = None,
    ) -> None:
        """Record a node mention in the conversation (implicit topic tracking)."""
        state = self.get_or_create(thread_id)

        if agent_id and agent_id not in state.participants:
            state.participants.append(agent_id)
            if len(state.participants) > _MAX_PARTICIPANTS:
                state.participants = state.participants[-_MAX_PARTICIPANTS:]

        for topic in state.topics:
            if topic.get("node_id") == node_id:
                topic["mentions"] = topic.get("mentions", 0) + 1
                if belief:
                    topic["thread_belief"] = belief
                break
        else:
            entry: dict[str, Any] = {"node_id": node_id, "mentions": 1}
            if belief:
                entry["thread_belief"] = belief
            state.topics.append(entry)
            if len(state.topics) > _MAX_TOPICS:
                state.topics = state.topics[-_MAX_TOPICS:]

    def record_contribution(
        self,
        thread_id: str,
        agent_id: str,
        contribution_type: str,
    ) -> None:
        """Record an agent contribution (claim, challenge, observation, etc.)."""
        state = self.get_or_create(thread_id)

        if agent_id not in state.agent_contributions:
            state.agent_contributions[agent_id] = {
                "claims": 0,
                "challenges": 0,
                "observations": 0,
                "supports": 0,
                "loop_risk_max": 0.0,
            }

        contribs = state.agent_contributions[agent_id]
        if contribution_type in contribs:
            contribs[contribution_type] = contribs[contribution_type] + 1

    def add_pending_question(
        self,
        thread_id: str,
        question: dict[str, Any],
    ) -> None:
        """Add a pending Socratic question."""
        state = self.get_or_create(thread_id)
        question.setdefault("asked_at", time.time())
        question.setdefault("answered", False)
        state.pending_questions.append(question)

        self._prune_questions(state)

    def answer_question(self, thread_id: str, question_text: str) -> bool:
        """Mark a pending question as answered. Returns True if found."""
        state = self._states.get(thread_id)
        if state is None:
            return False
        for q in state.pending_questions:
            if q.get("text") == question_text and not q.get("answered"):
                q["answered"] = True
                q["answered_at"] = time.time()
                return True
        return False

    def evict(self, thread_id: str) -> None:
        """Explicitly evict a thread's state."""
        self._states.pop(thread_id, None)

    def evict_expired(self) -> int:
        """Evict all expired entries. Returns count evicted."""
        now = time.time()
        evicted = 0
        for tid in list(self._states.keys()):
            state = self._states[tid]
            idle = now - state.last_access
            if idle > _DORMANT_TTL:
                del self._states[tid]
                evicted += 1
        return evicted

    def get_ohm_context_extras(self, thread_id: str) -> dict[str, Any]:
        """Extract conversation-state fields for the ohm_context envelope.

        Returns only the fields that are non-empty/non-default, so the
        envelope stays lean when there's no active conversation.
        """
        state = self._states.get(thread_id)
        if state is None:
            return {}

        extras: dict[str, Any] = {}

        open_questions = [q for q in state.pending_questions if not q.get("answered")]
        if open_questions:
            extras["pending_questions"] = open_questions[-5:]

        if state.deliberations:
            active = [d for d in state.deliberations if d.get("status") not in ("resolved", "decided")]
            if active:
                extras["deliberation_status"] = {
                    "active_count": len(active),
                    "statuses": [d.get("status") for d in active],
                }

        if state.topics:
            top_topic = max(state.topics, key=lambda t: t.get("mentions", 0))
            if top_topic.get("thread_belief"):
                extras["belief_summary"] = {
                    "node_id": top_topic["node_id"],
                    "belief": top_topic["thread_belief"],
                    "mentions": top_topic.get("mentions", 0),
                }

        if state.needs_attention:
            extras["needs_attention"] = True

        return extras

    def all_thread_ids(self) -> list[str]:
        return list(self._states.keys())

    def clear(self) -> None:
        self._states.clear()

    # ── Internal budget enforcement ──

    def _enforce_budget(self, state: ConversationState) -> None:
        self._prune_nudges(state)
        self._prune_questions(state)
        self._trim_contributions(state)

        if state.estimate_bytes() > _MAX_BYTES:
            state.nudge_history = state.nudge_history[-20:]
            state.topics = state.topics[-10:]
            if state.estimate_bytes() > _MAX_BYTES:
                state.nudge_history = state.nudge_history[-10:]
                state.topics = state.topics[-5:]

    def _enforce_global_cap(self) -> None:
        if len(self._states) <= _MAX_THREADS:
            return
        sorted_tids = sorted(self._states.keys(), key=lambda t: self._states[t].last_access)
        excess = len(self._states) - _MAX_THREADS
        for tid in sorted_tids[:excess]:
            del self._states[tid]

    def _merge_participants(self, state: ConversationState, participants: list[str]) -> None:
        for p in participants:
            if p and p not in state.participants:
                state.participants.append(p)
        if len(state.participants) > _MAX_PARTICIPANTS:
            state.participants = state.participants[-_MAX_PARTICIPANTS:]

    def _merge_topics(self, state: ConversationState, topics: list[dict[str, Any]]) -> None:
        for new_topic in topics:
            nid = new_topic.get("node_id")
            if not nid:
                continue
            for existing in state.topics:
                if existing.get("node_id") == nid:
                    existing.update(new_topic)
                    break
            else:
                state.topics.append(new_topic)
        if len(state.topics) > _MAX_TOPICS:
            state.topics = state.topics[-_MAX_TOPICS:]

    def _merge_deliberations(self, state: ConversationState, deliberations: list[dict[str, Any]]) -> None:
        for new_del in deliberations:
            nid = new_del.get("node_id")
            if not nid:
                continue
            for existing in state.deliberations:
                if existing.get("node_id") == nid:
                    existing.update(new_del)
                    break
            else:
                state.deliberations.append(new_del)
        if len(state.deliberations) > _MAX_DELIBERATIONS:
            state.deliberations = state.deliberations[-_MAX_DELIBERATIONS:]

    def _append_nudges(self, state: ConversationState, nudges: list[dict[str, Any]]) -> None:
        for nudge in nudges:
            nudge.setdefault("acknowledged", False)
            nudge.setdefault("logged_at", time.time())
            state.nudge_history.append(nudge)
        self._prune_nudges(state)

    def _merge_contributions(self, state: ConversationState, contribs: dict[str, dict[str, Any]]) -> None:
        for agent, data in contribs.items():
            if agent not in state.agent_contributions:
                state.agent_contributions[agent] = {
                    "claims": 0,
                    "challenges": 0,
                    "observations": 0,
                    "supports": 0,
                    "loop_risk_max": 0.0,
                }
            state.agent_contributions[agent].update(data)
        self._trim_contributions(state)

    def _merge_questions(self, state: ConversationState, questions: list[dict[str, Any]]) -> None:
        for q in questions:
            q.setdefault("asked_at", time.time())
            q.setdefault("answered", False)
            state.pending_questions.append(q)
        self._prune_questions(state)

    def _prune_nudges(self, state: ConversationState) -> None:
        if len(state.nudge_history) > _MAX_NUDGE_HISTORY:
            state.nudge_history = state.nudge_history[-_MAX_NUDGE_HISTORY:]

    def _prune_questions(self, state: ConversationState) -> None:
        now = time.time()
        open_qs = [q for q in state.pending_questions if not q.get("answered")]
        for q in open_qs:
            if now - q.get("asked_at", now) > 1800:
                q["expired"] = True
        if len(state.pending_questions) > _MAX_PENDING_QUESTIONS:
            state.pending_questions = state.pending_questions[-_MAX_PENDING_QUESTIONS:]

    def _trim_contributions(self, state: ConversationState) -> None:
        import json

        for agent, data in state.agent_contributions.items():
            try:
                if len(json.dumps(data).encode()) > _AGENT_CONTRIBUTION_BYTES:
                    data.pop("loop_risk_max", None)
            except Exception:
                pass


_store = ConversationStateStore()


def get_store() -> ConversationStateStore:
    return _store


def _reset_store() -> None:
    _store.clear()


def resolve_thread_id(
    kwargs: dict[str, Any],
    headers: dict[str, str],
    agent_id: str | None,
) -> str:
    """Resolve the thread_id from tool kwargs, headers, or session fallback.

    Priority:
    1. thread_id kwarg (if the tool accepts it)
    2. X-OHM-Thread header
    3. mcp-session-id header
    4. Fallback: f"session:{agent_id}"
    """
    tid = kwargs.get("thread_id")
    if tid:
        return str(tid)

    tid = headers.get("x-ohm-thread", headers.get("X-OHM-Thread", ""))
    if tid:
        return tid

    tid = headers.get("mcp-session-id", headers.get("Mcp-Session-Id", ""))
    if tid:
        return tid

    if agent_id:
        return f"session:{agent_id}"

    return "session:default"


_NODE_ID_KWARGS = {"node_id", "target", "cause", "effect", "decision", "from_id", "to_id", "from_node", "to_node"}
_CONTRIBUTION_MAP = {
    "ohm_create_node": "claims",
    "ohm_create_edge": "claims",
    "ohm_batch": "claims",
    "ohm_observe": "observations",
    "ohm_challenge": "challenges",
    "ohm_support": "supports",
}


def auto_update_from_tool(
    thread_id: str,
    tool_name: str,
    kwargs: dict[str, Any],
    agent_id: str | None,
    response_data: dict[str, Any] | None,
) -> None:
    """Automatically update conversation state from a tool call.

    - Records node mentions from node_id-bearing kwargs
    - Records agent contributions for write tools
    - Logs nudges from the response into nudge_history
    """
    store = get_store()

    for key in _NODE_ID_KWARGS:
        val = kwargs.get(key)
        if val and isinstance(val, str):
            store.record_mention(thread_id, val, agent_id or "unknown")

    contrib_type = _CONTRIBUTION_MAP.get(tool_name)
    if contrib_type and agent_id:
        store.record_contribution(thread_id, agent_id, contrib_type)

    if response_data and isinstance(response_data, dict):
        nudges = response_data.get("nudges")
        if nudges and isinstance(nudges, list):
            store.update_state(thread_id, {"nudge_history": nudges})
