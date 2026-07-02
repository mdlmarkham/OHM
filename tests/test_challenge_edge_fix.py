"""Tests for OHM-7el6: store.challenge_edge() creates correctly-shaped edges.

Verifies that CHALLENGED_BY and SUPPORTS edges created via OhmStore
match the canonical shape from queries.create_challenge() and
queries.create_support():
- Same from_node/to_node direction as the original edge (NOT reversed)
- Correct edge_type (CHALLENGED_BY for challenges, SUPPORTS for supports)
"""

from __future__ import annotations

import pytest

from ohm.graph.store import OhmStore


@pytest.fixture
def store(tmp_path):
    s = OhmStore(db_path=str(tmp_path / "test.duckdb"), agent_name="metis")
    s.write_node(id="source_node", label="Source", type="concept")
    s.write_node(id="target_node", label="Target", type="concept")
    yield s
    s.close()


def _create_l3_edge(store):
    return store.write_edge(
        from_node="source_node",
        to_node="target_node",
        edge_type="CAUSES",
        layer="L3",
        confidence=0.9,
    )


class TestChallengeEdgeDirection:
    """CHALLENGED_BY edges must have same from_node/to_node as original."""

    def test_challenge_same_direction_as_original(self, store):
        original = _create_l3_edge(store)
        challenge = store.challenge_edge(original["id"], "I disagree", 0.4)
        assert challenge is not None
        assert challenge["from_node"] == "source_node"
        assert challenge["to_node"] == "target_node"

    def test_challenge_does_not_reverse(self, store):
        original = _create_l3_edge(store)
        challenge = store.challenge_edge(original["id"], "I disagree", 0.4)
        assert challenge is not None
        assert challenge["from_node"] != "target_node"
        assert challenge["to_node"] != "source_node"


class TestChallengeEdgeType:
    """CHALLENGED_BY edges must have edge_type=CHALLENGED_BY."""

    def test_challenge_edge_type_is_challenged_by(self, store):
        original = _create_l3_edge(store)
        challenge = store.challenge_edge(original["id"], "I disagree", 0.4, "CHALLENGED_BY")
        assert challenge is not None
        assert challenge["edge_type"] == "CHALLENGED_BY"
        assert challenge["challenge_type"] == "CHALLENGED_BY"


class TestSupportEdgeType:
    """SUPPORTS edges must have edge_type=SUPPORTS (not CHALLENGED_BY)."""

    def test_support_edge_type_is_supports(self, store):
        original = _create_l3_edge(store)
        support = store.challenge_edge(original["id"], "I agree", 0.8, "SUPPORTS")
        assert support is not None
        assert support["edge_type"] == "SUPPORTS"
        assert support["challenge_type"] == "SUPPORTS"

    def test_support_edge_type_not_challenged_by(self, store):
        original = _create_l3_edge(store)
        support = store.challenge_edge(original["id"], "I agree", 0.8, "SUPPORTS")
        assert support is not None
        assert support["edge_type"] != "CHALLENGED_BY"

    def test_support_same_direction_as_original(self, store):
        original = _create_l3_edge(store)
        support = store.challenge_edge(original["id"], "I agree", 0.8, "SUPPORTS")
        assert support is not None
        assert support["from_node"] == "source_node"
        assert support["to_node"] == "target_node"


class TestCanonicalConsistency:
    """store.challenge_edge() must match queries.create_challenge() shape."""

    def test_challenge_matches_queries_create_challenge(self, store):
        from ohm.graph.queries import create_challenge

        original = _create_l3_edge(store)
        store_challenge = store.challenge_edge(original["id"], "store reason", 0.4)

        original2 = store.write_edge(
            from_node="source_node",
            to_node="target_node",
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
        )
        queries_challenge = create_challenge(
            store.conn,
            edge_id=original2["id"],
            reason="queries reason",
            created_by="metis",
            confidence=0.4,
        )

        assert store_challenge["edge_type"] == queries_challenge["edge_type"]
        assert store_challenge["challenge_type"] == queries_challenge["challenge_type"]
        assert store_challenge["from_node"] == queries_challenge["from_node"]
        assert store_challenge["to_node"] == queries_challenge["to_node"]

    def test_support_matches_queries_create_support(self, store):
        from ohm.graph.queries import create_support

        original = _create_l3_edge(store)
        store_support = store.challenge_edge(original["id"], "store reason", 0.8, "SUPPORTS")

        original2 = store.write_edge(
            from_node="source_node",
            to_node="target_node",
            edge_type="PREDICTS",
            layer="L3",
            confidence=0.9,
        )
        queries_support = create_support(
            store.conn,
            edge_id=original2["id"],
            reason="queries reason",
            created_by="metis",
            confidence=0.8,
        )

        assert store_support["edge_type"] == queries_support["edge_type"]
        assert store_support["challenge_type"] == queries_support["challenge_type"]
        assert store_support["from_node"] == queries_support["from_node"]
        assert store_support["to_node"] == queries_support["to_node"]


class TestChallengeOfLink:
    """Both challenge and support edges must reference the original via challenge_of."""

    def test_challenge_of_set(self, store):
        original = _create_l3_edge(store)
        challenge = store.challenge_edge(original["id"], "I disagree", 0.4)
        assert challenge["challenge_of"] == original["id"]

    def test_support_challenge_of_set(self, store):
        original = _create_l3_edge(store)
        support = store.challenge_edge(original["id"], "I agree", 0.8, "SUPPORTS")
        assert support["challenge_of"] == original["id"]
