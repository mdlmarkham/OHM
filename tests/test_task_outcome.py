"""Tests for OHM-f5iq: outcome feedback loop — closing tasks updates beliefs.

Covers:
- query_close_task_with_outcome (direct query tests)
- POST /tasks/{id}/outcome (HTTP integration tests)
- PATCH /node expected_claim / success_criteria fields
"""

import pytest

from ohm.graph.schema import initialize_schema, VALID_TASK_OUTCOMES
from ohm.graph.queries import query_close_task_with_outcome
from ohm.exceptions import NodeNotFoundError, ValidationError


# ── Direct query tests ─────────────────────────────────────────────────────


class TestTaskOutcomeQuery:
    """Unit tests for query_close_task_with_outcome via direct DB connection."""

    def _seed_task_and_claim(self, conn, *, task_id="task1", claim_id="claim1", expected_claim=True):
        # create_node auto-generates ids; use direct INSERT to control ids.
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, confidence) VALUES (?, ?, ?, ?, ?)",
            [claim_id, "Claim 1", "hypothesis", "metis", 0.6],
        )
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, confidence, task_status) VALUES (?, ?, ?, ?, ?, ?)",
            [task_id, "Task 1", "task", "metis", 1.0, "open"],
        )
        if expected_claim:
            conn.execute("UPDATE ohm_nodes SET expected_claim = ? WHERE id = ?", [claim_id, task_id])

    def test_close_task_with_true_outcome_records_outcome(self, test_db):
        self._seed_task_and_claim(test_db)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="TRUE", recorded_by="recorder")
        assert result["outcome"] == "TRUE"
        assert result["task"]["task_status"] == "done"
        assert result["task"]["outcome"] == "TRUE"
        assert result["outcome_record"] is not None
        rows = test_db.execute("SELECT source_agent, claim_node, outcome FROM ohm_outcomes").fetchall()
        assert rows == [("metis", "claim1", True)]

    def test_close_task_with_false_outcome(self, test_db):
        self._seed_task_and_claim(test_db)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="FALSE", recorded_by="recorder")
        assert result["outcome"] == "FALSE"
        rows = test_db.execute("SELECT outcome FROM ohm_outcomes").fetchall()
        assert rows == [(False,)]

    def test_close_task_ambiguous_no_outcome_record(self, test_db):
        self._seed_task_and_claim(test_db)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="AMBIGUOUS", recorded_by="recorder", notes="Inconclusive")
        assert result["outcome"] == "AMBIGUOUS"
        assert result["task"]["outcome"] == "AMBIGUOUS"
        assert result["outcome_record"] is None
        assert test_db.execute("SELECT COUNT(*) FROM ohm_outcomes").fetchone()[0] == 0

    def test_close_task_no_expected_claim_no_outcome(self, test_db):
        self._seed_task_and_claim(test_db, expected_claim=False)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="TRUE", recorded_by="recorder")
        assert result["outcome_record"] is None

    def test_close_task_with_explicit_claim_node_arg(self, test_db):
        self._seed_task_and_claim(test_db, expected_claim=False)
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["claim_x", "Claim X", "hypothesis", "metis"],
        )
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="TRUE", recorded_by="recorder", claim_node="claim_x")
        assert result["outcome_record"] is not None
        rows = test_db.execute("SELECT claim_node FROM ohm_outcomes").fetchall()
        assert rows == [("claim_x",)]

    def test_close_nonexistent_task_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            query_close_task_with_outcome(test_db, task_id="ghost", outcome="TRUE", recorded_by="r")

    def test_close_non_task_node_raises(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by) VALUES (?, ?, ?, ?)",
            ["concept1", "Concept", "concept", "metis"],
        )
        with pytest.raises(ValidationError):
            query_close_task_with_outcome(test_db, task_id="concept1", outcome="TRUE", recorded_by="r")

    def test_invalid_outcome_raises(self, test_db):
        self._seed_task_and_claim(test_db)
        with pytest.raises(ValidationError):
            query_close_task_with_outcome(test_db, task_id="task1", outcome="MAYBE", recorded_by="r")

    def test_outcome_case_insensitive(self, test_db):
        self._seed_task_and_claim(test_db)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="true", recorded_by="r")
        assert result["outcome"] == "TRUE"

    def test_notes_stored_on_task(self, test_db):
        self._seed_task_and_claim(test_db)
        result = query_close_task_with_outcome(test_db, task_id="task1", outcome="TRUE", recorded_by="r", notes="Confirmed by experiment")
        assert result["task"]["outcome_notes"] == "Confirmed by experiment"


# ── HTTP integration tests ─────────────────────────────────────────────────

pytestmark = pytest.mark.integration

from tests.conftest import _request  # noqa: E402


@pytest.mark.xdist_group("server")
class TestTaskOutcomeHTTP:
    """HTTP integration tests for POST /tasks/{id}/outcome."""

    @staticmethod
    def _seed(server_fixture, *, expected_claim=True):
        port, store = server_fixture
        store.write_node("anchor1", "Anchor", "concept", agent_name="test")
        store.write_node("claim1", "Claim 1", "hypothesis", agent_name="metis")
        store.write_edge("anchor1", "claim1", "REFERENCES", layer="L2", agent_name="test")
        store.write_node("task1", "Task 1", "task", agent_name="metis", task_status="open")
        store.write_edge("anchor1", "task1", "REFERENCES", layer="L2", agent_name="test")
        if expected_claim:
            store.conn.execute(
                "UPDATE ohm_nodes SET expected_claim = ? WHERE id = ?",
                ["claim1", "task1"],
            )
        return port, store

    def test_post_task_outcome_true(self, test_server):
        port, _ = self._seed(test_server)
        status, data = _request("POST", port, "/tasks/task1/outcome", body={"outcome": "TRUE", "notes": "Confirmed"})
        assert status == 200, data
        assert data["task"]["task_status"] == "done"
        assert data["task"]["outcome"] == "TRUE"
        assert data["outcome_record"] is not None

    def test_post_task_outcome_false(self, test_server):
        port, store = self._seed(test_server)
        # Reset to open for a fresh close
        store.conn.execute("UPDATE ohm_nodes SET task_status='open', outcome=NULL WHERE id='task1'")
        status, data = _request("POST", port, "/tasks/task1/outcome", body={"outcome": "FALSE"})
        assert status == 200, data
        assert data["outcome"] == "FALSE"
        assert data["outcome_record"] is not None

    def test_post_task_outcome_ambiguous(self, test_server):
        port, store = self._seed(test_server)
        store.conn.execute("UPDATE ohm_nodes SET task_status='open', outcome=NULL WHERE id='task1'")
        status, data = _request("POST", port, "/tasks/task1/outcome", body={"outcome": "AMBIGUOUS"})
        assert status == 200, data
        assert data["outcome"] == "AMBIGUOUS"
        assert data["outcome_record"] is None

    def test_post_task_outcome_nonexistent_404(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/tasks/nonexistent/outcome", body={"outcome": "TRUE"})
        assert status == 404

    def test_post_task_outcome_non_task_400(self, test_server):
        port, store = test_server
        store.write_node("anchor2", "Anchor 2", "concept", agent_name="test")
        store.write_node("concept1", "A concept", "concept", agent_name="test")
        store.write_edge("anchor2", "concept1", "REFERENCES", layer="L2", agent_name="test")
        status, data = _request("POST", port, "/tasks/concept1/outcome", body={"outcome": "TRUE"})
        assert status == 400

    def test_post_task_outcome_missing_outcome_field_400(self, test_server):
        port, store = self._seed(test_server)
        store.conn.execute("UPDATE ohm_nodes SET task_status='open', outcome=NULL WHERE id='task1'")
        status, data = _request("POST", port, "/tasks/task1/outcome", body={})
        assert status == 400

    def test_post_task_outcome_invalid_value_400(self, test_server):
        port, store = self._seed(test_server)
        store.conn.execute("UPDATE ohm_nodes SET task_status='open', outcome=NULL WHERE id='task1'")
        status, data = _request("POST", port, "/tasks/task1/outcome", body={"outcome": "MAYBE"})
        assert status == 400

    def test_post_task_outcome_with_claim_node_override(self, test_server):
        port, store = self._seed(test_server, expected_claim=False)
        store.write_node("anchor3", "Anchor 3", "concept", agent_name="test")
        store.write_node("claim_x", "Claim X", "hypothesis", agent_name="metis")
        store.write_edge("anchor3", "claim_x", "REFERENCES", layer="L2", agent_name="test")
        status, data = _request("POST", port, "/tasks/task1/outcome", body={"outcome": "TRUE", "claim_node": "claim_x"})
        assert status == 200, data
        assert data["outcome_record"] is not None

    def test_unknown_task_subpath_404(self, test_server):
        port, store = self._seed(test_server)
        store.conn.execute("UPDATE ohm_nodes SET task_status='open', outcome=NULL WHERE id='task1'")
        status, data = _request("POST", port, "/tasks/task1/unknown", body={"outcome": "TRUE"})
        assert status == 404

    def test_patch_node_sets_expected_claim(self, test_server):
        port, store = test_server
        store.write_node("anchor4", "Anchor 4", "concept", agent_name="test")
        store.write_node("task_patch", "Patch Task", "task", agent_name="test")
        store.write_edge("anchor4", "task_patch", "REFERENCES", layer="L2", agent_name="test")
        status, data = _request("PATCH", port, "/tasks/task_patch", body={"expected_claim": "some_claim_id"})
        assert status == 200, data
        # Verify via direct DB read
        row = store.conn.execute("SELECT expected_claim FROM ohm_nodes WHERE id = ?", ["task_patch"]).fetchone()
        assert row[0] == "some_claim_id"
