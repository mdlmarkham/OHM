"""Tests for OHM-767: thread-aware belief context using L0 fragments."""

from __future__ import annotations

import pytest

from ohm.graph.schema import initialize_schema
from ohm.graph.queries import create_node
from ohm.mcp.thread_belief import store_thread_belief, get_thread_beliefs, promote_thread_belief


class TestThreadBeliefStorage:
    """Test storing and retrieving thread beliefs as L0 fragments."""

    def test_store_thread_belief_creates_fragment(self, test_db):
        """store_thread_belief creates an L0 fragment with posterior in metadata."""
        target = create_node(test_db, label="Target", node_type="concept", created_by="test")
        fragment = store_thread_belief(
            test_db,
            thread_id="thread-1",
            target_node=target["id"],
            posterior={"P(bad)": 0.34, "P(good)": 0.66},
            created_by="test-agent",
        )
        assert fragment is not None
        assert fragment["type"] == "fragment"
        assert fragment["id"] is not None

    def test_get_thread_beliefs_retrieves_by_thread(self, test_db):
        """get_thread_beliefs returns fragments for a given thread."""
        target = create_node(test_db, label="Target", node_type="concept", created_by="test")
        store_thread_belief(
            test_db,
            thread_id="thread-1",
            target_node=target["id"],
            posterior={"P(bad)": 0.3, "P(good)": 0.7},
            created_by="agent-1",
        )
        store_thread_belief(
            test_db,
            thread_id="thread-1",
            target_node=target["id"],
            posterior={"P(bad)": 0.5, "P(good)": 0.5},
            created_by="agent-1",
        )

        beliefs = get_thread_beliefs(test_db, "thread-1")
        assert len(beliefs) == 2
        # Most recent first
        assert beliefs[0]["posterior"]["P(bad)"] == 0.5

    def test_get_thread_beliefs_filters_by_target(self, test_db):
        """get_thread_beliefs with target_node filters correctly."""
        target_a = create_node(test_db, label="Target A", node_type="concept", created_by="test")
        target_b = create_node(test_db, label="Target B", node_type="concept", created_by="test")

        store_thread_belief(test_db, thread_id="t1", target_node=target_a["id"], posterior={"P(bad)": 0.3}, created_by="a")
        store_thread_belief(test_db, thread_id="t1", target_node=target_b["id"], posterior={"P(bad)": 0.6}, created_by="a")

        beliefs_a = get_thread_beliefs(test_db, "t1", target_node=target_a["id"])
        assert len(beliefs_a) == 1
        assert beliefs_a[0]["target_node"] == target_a["id"]

    def test_different_threads_are_isolated(self, test_db):
        """Thread beliefs for different threads don't cross-contaminate."""
        target = create_node(test_db, label="Target", node_type="concept", created_by="test")
        store_thread_belief(test_db, thread_id="thread-a", target_node=target["id"], posterior={"P(bad)": 0.3}, created_by="a")
        store_thread_belief(test_db, thread_id="thread-b", target_node=target["id"], posterior={"P(bad)": 0.6}, created_by="b")

        beliefs_a = get_thread_beliefs(test_db, "thread-a")
        beliefs_b = get_thread_beliefs(test_db, "thread-b")
        assert len(beliefs_a) == 1
        assert len(beliefs_b) == 1
        assert beliefs_a[0]["posterior"]["P(bad)"] == 0.3
        assert beliefs_b[0]["posterior"]["P(bad)"] == 0.6

    def test_empty_thread_returns_empty_list(self, test_db):
        """No beliefs for unknown thread returns empty list."""
        beliefs = get_thread_beliefs(test_db, "nonexistent-thread")
        assert beliefs == []

    def test_belief_has_posterior_in_metadata(self, test_db):
        """Retrieved belief includes posterior from metadata."""
        target = create_node(test_db, label="T", node_type="concept", created_by="test")
        store_thread_belief(
            test_db,
            thread_id="t1",
            target_node=target["id"],
            posterior={"P(bad)": 0.42, "P(good)": 0.58},
            created_by="agent",
        )
        beliefs = get_thread_beliefs(test_db, "t1")
        assert beliefs[0]["posterior"]["P(bad)"] == 0.42
        assert beliefs[0]["posterior"]["P(good)"] == 0.58
        assert beliefs[0]["thread_id"] == "t1"
        assert beliefs[0]["target_node"] == target["id"]


class TestThreadBeliefPromotion:
    """Test promoting thread beliefs to persistent nodes."""

    def test_promote_thread_belief(self, test_db):
        """promote_thread_belief promotes L0 fragment to L1 concept."""
        target = create_node(test_db, label="Target", node_type="concept", created_by="test")
        fragment = store_thread_belief(
            test_db,
            thread_id="t1",
            target_node=target["id"],
            posterior={"P(bad)": 0.4, "P(good)": 0.6},
            created_by="agent",
        )

        result = promote_thread_belief(test_db, fragment["id"], created_by="agent")
        assert result is not None
        # promote_fragment returns a dict with 'concept' and 'edge' sub-dicts
        assert "concept" in result or "id" in result
