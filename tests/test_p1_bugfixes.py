"""Tests for OHM-twd2: 3 P1 bug fixes.

OHM-mzyc.1: INFLUENCES edge type has contradictory causal status
OHM-sbtz.2: task nodes accept invalid task_status
OHM-sbtz.1: sync-beads is not idempotent
"""

from __future__ import annotations

import json

import pytest

from ohm.server.nudges import CAUSAL_EDGE_TYPES, NON_CAUSAL_EDGE_TYPES
from ohm.graph.schema import VALID_TASK_STATUSES
from ohm.integrations.beads_sync import sync_beads_to_ohm_tasks


@pytest.fixture
def test_db():
    import duckdb

    from ohm.graph.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


def _make_issue(**overrides):
    base = {
        "id": "OHM-test1",
        "title": "Test Issue",
        "description": "A test issue",
        "status": "open",
        "priority": 1,
        "assignee": "metis@olympus.local",
        "issue_type": "task",
        "labels": ["test"],
    }
    base.update(overrides)
    return base


# ── OHM-mzyc.1: INFLUENCES causal status ────────────────────────────────────


class TestInfluencesCausalStatus:
    """INFLUENCES must NOT be in CAUSAL_EDGE_TYPES (it doesn't feed the
    Bayesian network). It should be in NON_CAUSAL_EDGE_TYPES."""

    def test_influences_not_in_causal_types(self):
        assert "INFLUENCES" not in CAUSAL_EDGE_TYPES

    def test_influences_in_non_causal_types(self):
        assert "INFLUENCES" in NON_CAUSAL_EDGE_TYPES

    def test_blocks_not_in_causal_types(self):
        assert "BLOCKS" not in CAUSAL_EDGE_TYPES

    def test_enables_not_in_causal_types(self):
        assert "ENABLES" not in CAUSAL_EDGE_TYPES

    def test_causes_in_causal_types(self):
        assert "CAUSES" in CAUSAL_EDGE_TYPES

    def test_depends_on_in_causal_types(self):
        assert "DEPENDS_ON" in CAUSAL_EDGE_TYPES

    def test_threatens_in_causal_types(self):
        assert "THREATENS" in CAUSAL_EDGE_TYPES

    def test_influences_nudge_says_non_causal(self):
        """The nudge for INFLUENCES should say it doesn't feed the Bayesian network."""
        from ohm.server.nudges import generate_nudges

        nudges = generate_nudges(action="edge", edge_type="INFLUENCES")
        causal_suggestions = [n for n in nudges if n["type"] == "causal_edge_suggestion"]
        assert len(causal_suggestions) == 1
        assert "Bayesian" in causal_suggestions[0]["message"]

    def test_causes_nudge_says_causal(self):
        from ohm.server.nudges import generate_nudges

        nudges = generate_nudges(action="edge", edge_type="CAUSES")
        causal_confirmed = [n for n in nudges if n["type"] == "causal_edge_confirmed"]
        assert len(causal_confirmed) == 1
        assert "Bayesian" in causal_confirmed[0]["message"]


# ── OHM-sbtz.2: task_status validation ──────────────────────────────────────


class TestTaskStatusValidation:
    """store.write_node must reject invalid task_status for task nodes."""

    def test_valid_task_status_accepted(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", schema=None)
        # Reuse the test_db connection
        store.conn = test_db
        for status in VALID_TASK_STATUSES:
            store.write_node(
                f"task_{status}",
                f"Task {status}",
                "task",
                task_status=status,
                agent_name="test",
            )
            row = test_db.execute("SELECT task_status FROM ohm_nodes WHERE id = ?", [f"task_{status}"]).fetchone()
            assert row[0] == status
        store.close()

    def test_invalid_task_status_rejected(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", schema=None)
        store.conn = test_db
        with pytest.raises(ValueError, match="Invalid task_status"):
            store.write_node(
                "task_bad",
                "Bad Task",
                "task",
                task_status="invalid_status",
                agent_name="test",
            )
        store.close()

    def test_invalid_task_status_closed_rejected(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", schema=None)
        store.conn = test_db
        with pytest.raises(ValueError, match="Invalid task_status"):
            store.write_node(
                "task_closed",
                "Closed Task",
                "task",
                task_status="closed",
                agent_name="test",
            )
        store.close()

    def test_non_task_node_accepts_any_task_status(self, test_db):
        from ohm.graph.store import OhmStore

        store = OhmStore(db_path=":memory:", schema=None)
        store.conn = test_db
        # Non-task nodes should not validate task_status
        store.write_node(
            "concept_1",
            "Concept",
            "concept",
            task_status="invalid_status",
            agent_name="test",
        )
        store.close()


# ── OHM-sbtz.1: sync-beads idempotency ──────────────────────────────────────


class TestSyncBeadsIdempotency:
    """sync_beads_to_ohm_tasks must not UPDATE when nothing changed."""

    def test_first_sync_creates(self, test_db):
        issues = [_make_issue()]
        report = sync_beads_to_ohm_tasks(test_db, issues)
        assert report["created"] == 1
        assert report["updated"] == 0
        assert report["skipped"] == 0

    def test_second_sync_with_same_data_skips(self, test_db):
        issues = [_make_issue()]
        sync_beads_to_ohm_tasks(test_db, issues)
        report = sync_beads_to_ohm_tasks(test_db, issues)
        assert report["created"] == 0
        assert report["updated"] == 0
        assert report["skipped"] == 1

    def test_third_sync_still_skips(self, test_db):
        issues = [_make_issue()]
        sync_beads_to_ohm_tasks(test_db, issues)
        sync_beads_to_ohm_tasks(test_db, issues)
        report = sync_beads_to_ohm_tasks(test_db, issues)
        assert report["skipped"] == 1

    def test_sync_with_changed_title_updates(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(title="Old")])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(title="New Title")])
        assert report["updated"] == 1
        assert report["skipped"] == 0

    def test_sync_with_changed_priority_updates(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(priority=1)])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(priority=3)])
        assert report["updated"] == 1

    def test_sync_with_changed_assignee_updates(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(assignee="metis@olympus.local")])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(assignee="clio@olympus.local")])
        assert report["updated"] == 1

    def test_sync_with_changed_labels_updates(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(labels=["a"])])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(labels=["a", "b"])])
        assert report["updated"] == 1

    def test_sync_with_same_labels_skips(self, test_db):
        sync_beads_to_ohm_tasks(test_db, [_make_issue(labels=["a", "b"])])
        report = sync_beads_to_ohm_tasks(test_db, [_make_issue(labels=["a", "b"])])
        assert report["skipped"] == 1
