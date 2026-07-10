"""Tests for OHM-789: Conversation-state MVP with TTL/budget."""

from __future__ import annotations

import json
import time
import pytest

from ohm.mcp.conversation_state import (
    ConversationState,
    ConversationStateStore,
    auto_update_from_tool,
    get_store,
    resolve_thread_id,
    _reset_store,
    _ACTIVE_TTL,
    _DORMANT_TTL,
    _MAX_NUDGE_HISTORY,
    _MAX_PENDING_QUESTIONS,
    _MAX_TOPICS,
    _MAX_PARTICIPANTS,
)


@pytest.fixture(autouse=True)
def _clean_store():
    _reset_store()
    yield
    _reset_store()


class TestConversationStateModel:
    def test_creation_defaults(self):
        state = ConversationState(thread_id="t1")
        assert state.thread_id == "t1"
        assert state.participants == []
        assert state.topics == []
        assert state.deliberations == []
        assert state.nudge_history == []
        assert state.agent_contributions == {}
        assert state.pending_questions == []
        assert state.thread_entropy == 0.0
        assert state.needs_attention is False

    def test_to_dict_roundtrip(self):
        state = ConversationState(thread_id="t1")
        state.participants = ["a1", "a2"]
        state.topics = [{"node_id": "n1", "mentions": 3}]
        d = state.to_dict()
        assert d["thread_id"] == "t1"
        assert d["participants"] == ["a1", "a2"]
        assert d["topics"][0]["node_id"] == "n1"

    def test_touch_updates_last_access(self):
        state = ConversationState(thread_id="t1")
        old = state.last_access
        time.sleep(0.01)
        state.touch()
        assert state.last_access > old

    def test_estimate_bytes_positive(self):
        state = ConversationState(thread_id="t1")
        state.participants = ["a1"]
        assert state.estimate_bytes() > 0


class TestConversationStateStore:
    def test_get_state_returns_none_for_unknown(self):
        store = ConversationStateStore()
        assert store.get_state("unknown") is None

    def test_get_or_create_creates_lazy(self):
        store = ConversationStateStore()
        state = store.get_or_create("t1")
        assert state.thread_id == "t1"
        # Second call returns same object
        state2 = store.get_or_create("t1")
        assert state2 is state

    def test_update_state_creates_if_missing(self):
        store = ConversationStateStore()
        result = store.update_state("t1", {"thread_entropy": 1.5})
        assert result["thread_entropy"] == 1.5
        assert result["thread_id"] == "t1"

    def test_update_state_merges_participants(self):
        store = ConversationStateStore()
        store.update_state("t1", {"participants": ["a1", "a2"]})
        store.update_state("t1", {"participants": ["a2", "a3"]})
        state = store.get_state("t1")
        assert set(state["participants"]) == {"a1", "a2", "a3"}

    def test_update_state_merges_topics(self):
        store = ConversationStateStore()
        store.update_state("t1", {"topics": [{"node_id": "n1", "mentions": 1}]})
        store.update_state("t1", {"topics": [{"node_id": "n1", "mentions": 5}]})
        state = store.get_state("t1")
        assert state["topics"][0]["mentions"] == 5

    def test_update_state_merges_deliberations(self):
        store = ConversationStateStore()
        store.update_state("t1", {"deliberations": [{"node_id": "n1", "status": "proposed"}]})
        store.update_state("t1", {"deliberations": [{"node_id": "n1", "status": "challenged"}]})
        state = store.get_state("t1")
        assert state["deliberations"][0]["status"] == "challenged"

    def test_update_state_appends_nudges(self):
        store = ConversationStateStore()
        store.update_state("t1", {"nudge_history": [{"type": "hint"}]})
        store.update_state("t1", {"nudge_history": [{"type": "calibration"}]})
        state = store.get_state("t1")
        assert len(state["nudge_history"]) == 2

    def test_update_state_merges_contributions(self):
        store = ConversationStateStore()
        store.update_state("t1", {"agent_contributions": {"a1": {"claims": 3}}})
        store.update_state("t1", {"agent_contributions": {"a1": {"challenges": 1}}})
        state = store.get_state("t1")
        assert state["agent_contributions"]["a1"]["claims"] == 3
        assert state["agent_contributions"]["a1"]["challenges"] == 1

    def test_update_state_sets_entropy_and_attention(self):
        store = ConversationStateStore()
        store.update_state("t1", {"thread_entropy": 2.3, "needs_attention": True})
        state = store.get_state("t1")
        assert state["thread_entropy"] == 2.3
        assert state["needs_attention"] is True


class TestRecordMention:
    def test_creates_topic_on_first_mention(self):
        store = ConversationStateStore()
        store.record_mention("t1", "n1", "a1")
        state = store.get_state("t1")
        assert state["topics"][0]["node_id"] == "n1"
        assert state["topics"][0]["mentions"] == 1
        assert "a1" in state["participants"]

    def test_increments_mention_count(self):
        store = ConversationStateStore()
        store.record_mention("t1", "n1", "a1")
        store.record_mention("t1", "n1", "a1")
        store.record_mention("t1", "n1", "a2")
        state = store.get_state("t1")
        assert state["topics"][0]["mentions"] == 3
        assert set(state["participants"]) == {"a1", "a2"}

    def test_updates_belief_when_provided(self):
        store = ConversationStateStore()
        store.record_mention("t1", "n1", "a1", belief={"P(bad)": 0.3})
        store.record_mention("t1", "n1", "a1", belief={"P(bad)": 0.5})
        state = store.get_state("t1")
        assert state["topics"][0]["thread_belief"] == {"P(bad)": 0.5}

    def test_caps_topics(self):
        store = ConversationStateStore()
        for i in range(_MAX_TOPICS + 5):
            store.record_mention("t1", f"n{i}", "a1")
        state = store.get_state("t1")
        assert len(state["topics"]) <= _MAX_TOPICS


class TestRecordContribution:
    def test_records_claim(self):
        store = ConversationStateStore()
        store.record_contribution("t1", "a1", "claims")
        state = store.get_state("t1")
        assert state["agent_contributions"]["a1"]["claims"] == 1

    def test_records_multiple_types(self):
        store = ConversationStateStore()
        store.record_contribution("t1", "a1", "claims")
        store.record_contribution("t1", "a1", "challenges")
        store.record_contribution("t1", "a1", "observations")
        state = store.get_state("t1")
        c = state["agent_contributions"]["a1"]
        assert c["claims"] == 1
        assert c["challenges"] == 1
        assert c["observations"] == 1

    def test_unknown_type_ignored(self):
        store = ConversationStateStore()
        store.record_contribution("t1", "a1", "unknown_type")
        state = store.get_state("t1")
        assert "unknown_type" not in state["agent_contributions"].get("a1", {})


class TestPendingQuestions:
    def test_add_question(self):
        store = ConversationStateStore()
        store.add_pending_question("t1", {"type": "socratic", "text": "What would change your mind?"})
        state = store.get_state("t1")
        assert len(state["pending_questions"]) == 1
        assert state["pending_questions"][0]["answered"] is False
        assert "asked_at" in state["pending_questions"][0]

    def test_answer_question(self):
        store = ConversationStateStore()
        store.add_pending_question("t1", {"type": "socratic", "text": "Why?"})
        found = store.answer_question("t1", "Why?")
        assert found is True
        state = store.get_state("t1")
        assert state["pending_questions"][0]["answered"] is True
        assert "answered_at" in state["pending_questions"][0]

    def test_answer_question_not_found(self):
        store = ConversationStateStore()
        found = store.answer_question("t1", "nonexistent")
        assert found is False

    def test_answer_question_already_answered(self):
        store = ConversationStateStore()
        store.add_pending_question("t1", {"type": "socratic", "text": "Why?"})
        assert store.answer_question("t1", "Why?") is True
        assert store.answer_question("t1", "Why?") is False

    def test_caps_pending_questions(self):
        store = ConversationStateStore()
        for i in range(_MAX_PENDING_QUESTIONS + 5):
            store.add_pending_question("t1", {"text": f"q{i}"})
        state = store.get_state("t1")
        assert len(state["pending_questions"]) <= _MAX_PENDING_QUESTIONS


class TestEviction:
    def test_explicit_evict(self):
        store = ConversationStateStore()
        store.update_state("t1", {"thread_entropy": 1.0})
        store.evict("t1")
        assert store.get_state("t1") is None

    def test_ttl_eviction_dormant(self):
        store = ConversationStateStore()
        state = store.get_or_create("t1")
        state.last_access = time.time() - _DORMANT_TTL - 1
        evicted = store.evict_expired()
        assert evicted == 1
        assert store.get_state("t1") is None

    def test_ttl_not_yet_expired(self):
        store = ConversationStateStore()
        store.update_state("t1", {"thread_entropy": 1.0})
        evicted = store.evict_expired()
        assert evicted == 0
        assert store.get_state("t1") is not None

    def test_global_cap_eviction(self):
        store = ConversationStateStore()
        from ohm.mcp.conversation_state import _MAX_THREADS

        for i in range(_MAX_THREADS + 10):
            store.get_or_create(f"t{i}")
        assert len(store.all_thread_ids()) <= _MAX_THREADS


class TestBudgetEnforcement:
    def test_nudge_history_cap(self):
        store = ConversationStateStore()
        for i in range(_MAX_NUDGE_HISTORY + 10):
            store.update_state("t1", {"nudge_history": [{"type": f"n{i}"}]})
        state = store.get_state("t1")
        assert len(state["nudge_history"]) <= _MAX_NUDGE_HISTORY

    def test_participants_cap(self):
        store = ConversationStateStore()
        participants = [f"agent-{i}" for i in range(_MAX_PARTICIPANTS + 10)]
        store.update_state("t1", {"participants": participants})
        state = store.get_state("t1")
        assert len(state["participants"]) <= _MAX_PARTICIPANTS


class TestOhmContextExtras:
    def test_empty_state_returns_empty(self):
        store = ConversationStateStore()
        assert store.get_ohm_context_extras("unknown") == {}

    def test_pending_questions_in_extras(self):
        store = ConversationStateStore()
        store.add_pending_question("t1", {"type": "socratic", "text": "Why?"})
        extras = store.get_ohm_context_extras("t1")
        assert "pending_questions" in extras
        assert len(extras["pending_questions"]) == 1

    def test_deliberation_status_in_extras(self):
        store = ConversationStateStore()
        store.update_state("t1", {"deliberations": [{"node_id": "n1", "status": "challenged"}]})
        extras = store.get_ohm_context_extras("t1")
        assert "deliberation_status" in extras
        assert extras["deliberation_status"]["active_count"] == 1

    def test_resolved_deliberation_not_in_extras(self):
        store = ConversationStateStore()
        store.update_state("t1", {"deliberations": [{"node_id": "n1", "status": "resolved"}]})
        extras = store.get_ohm_context_extras("t1")
        assert "deliberation_status" not in extras

    def test_belief_summary_in_extras(self):
        store = ConversationStateStore()
        store.record_mention("t1", "n1", "a1", belief={"P(bad)": 0.3})
        store.record_mention("t1", "n2", "a1", belief={"P(bad)": 0.5})
        store.record_mention("t1", "n1", "a1")
        extras = store.get_ohm_context_extras("t1")
        assert "belief_summary" in extras
        assert extras["belief_summary"]["node_id"] == "n1"
        assert extras["belief_summary"]["belief"] == {"P(bad)": 0.3}

    def test_needs_attention_in_extras(self):
        store = ConversationStateStore()
        store.update_state("t1", {"needs_attention": True})
        extras = store.get_ohm_context_extras("t1")
        assert extras["needs_attention"] is True


class TestResolveThreadId:
    def test_from_kwargs(self):
        tid = resolve_thread_id({"thread_id": "t1"}, {}, None)
        assert tid == "t1"

    def test_from_x_ohm_thread_header(self):
        tid = resolve_thread_id({}, {"x-ohm-thread": "t2"}, None)
        assert tid == "t2"

    def test_from_mcp_session_id(self):
        tid = resolve_thread_id({}, {"mcp-session-id": "s3"}, None)
        assert tid == "s3"

    def test_fallback_to_agent_id(self):
        tid = resolve_thread_id({}, {}, "agent-atlas")
        assert tid == "session:agent-atlas"

    def test_fallback_to_default(self):
        tid = resolve_thread_id({}, {}, None)
        assert tid == "session:default"

    def test_kwargs_priority_over_headers(self):
        tid = resolve_thread_id({"thread_id": "t1"}, {"x-ohm-thread": "t2"}, "a1")
        assert tid == "t1"


class TestAutoUpdateFromTool:
    def test_records_node_mention(self):
        auto_update_from_tool("t1", "ohm_get_node", {"node_id": "n1"}, "a1", None)
        state = get_store().get_state("t1")
        assert state["topics"][0]["node_id"] == "n1"
        assert "a1" in state["participants"]

    def test_records_target_mention(self):
        auto_update_from_tool("t1", "ohm_inference", {"target": "n2"}, "a1", None)
        state = get_store().get_state("t1")
        assert state["topics"][0]["node_id"] == "n2"

    def test_records_contribution_for_write_tool(self):
        auto_update_from_tool("t1", "ohm_create_node", {"id": "n3"}, "a1", None)
        state = get_store().get_state("t1")
        assert state["agent_contributions"]["a1"]["claims"] == 1

    def test_no_contribution_for_read_tool(self):
        auto_update_from_tool("t1", "ohm_search", {"q": "test"}, "a1", None)
        state = get_store().get_state("t1")
        # Read tools with no node_id kwargs don't create state
        assert state is None

    def test_logs_nudges_from_response(self):
        auto_update_from_tool("t1", "ohm_create_node", {"id": "n3"}, "a1", {"nudges": [{"type": "hint"}]})
        state = get_store().get_state("t1")
        assert len(state["nudge_history"]) == 1
        assert state["nudge_history"][0]["type"] == "hint"

    def test_records_multiple_node_mentions(self):
        auto_update_from_tool("t1", "ohm_refute", {"cause": "c1", "effect": "e1"}, "a1", None)
        state = get_store().get_state("t1")
        node_ids = {t["node_id"] for t in state["topics"]}
        assert "c1" in node_ids
        assert "e1" in node_ids
