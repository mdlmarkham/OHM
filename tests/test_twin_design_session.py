from __future__ import annotations

import pytest
from ohm.graph.schema import initialize_schema
from ohm.graph.queries import (
    start_twin_design_session,
    transition_session,
    add_session_observation,
    propose_twin_config,
    review_proposal,
    instantiate_from_session,
    record_calibration,
    evolve_session,
    get_session_state,
    get_session_audit,
    VALID_SESSION_STATES,
    SESSION_TRANSITIONS,
    create_node,
    create_edge,
)
from ohm.exceptions import ValidationError, NodeNotFoundError


@pytest.fixture
def test_db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


class TestSessionStateMachine:
    def test_valid_transitions(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test goal", created_by="test"
        )
        sid = session["id"]

        result = transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        assert result["id"] == sid

    def test_invalid_transition_raises(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test goal", created_by="test"
        )
        sid = session["id"]

        with pytest.raises(ValidationError, match="Invalid transition"):
            transition_session(
                test_db, session_id=sid, to_state="operate", created_by="test"
            )

    def test_completed_is_terminal(self, test_db):
        for state in ("completed", "abandoned"):
            assert len(SESSION_TRANSITIONS[state]) == 0

    def test_abandoned_is_terminal(self, test_db):
        assert len(SESSION_TRANSITIONS["abandoned"]) == 0

    def test_all_states_in_valid_set(self):
        for state in SESSION_TRANSITIONS:
            assert state in VALID_SESSION_STATES

    def test_all_targets_in_valid_set(self):
        for state, targets in SESSION_TRANSITIONS.items():
            for target in targets:
                assert target in VALID_SESSION_STATES


class TestStartSession:
    def test_start_creates_session_in_init_state(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Design a supply chain twin", created_by="test"
        )
        assert session["type"] == "twin_design_session"
        import json

        meta = json.loads(session["metadata"]) if isinstance(session["metadata"], str) else session["metadata"]
        assert meta["session_state"] == "init"
        assert meta["goal"] == "Design a supply chain twin"

    def test_start_missing_goal_raises(self, test_db):
        with pytest.raises(ValidationError, match="goal is required"):
            start_twin_design_session(test_db, goal="", created_by="test")

    def test_start_with_context(self, test_db):
        session = start_twin_design_session(
            test_db,
            goal="Test",
            context={"domain": "supply_chain"},
            created_by="test",
        )
        import json

        meta = json.loads(session["metadata"]) if isinstance(session["metadata"], str) else session["metadata"]
        assert meta["context"]["domain"] == "supply_chain"


class TestObserveFlow:
    def test_add_observations_stores_in_metadata(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="observe", created_by="test"
        )

        result = add_session_observation(
            test_db,
            session_id=sid,
            observations={"supplier_reliability": 0.85},
            created_by="test",
        )
        import json

        meta = json.loads(result["metadata"]) if isinstance(result["metadata"], str) else result["metadata"]
        assert len(meta["observations"]) == 1
        assert meta["observations"][0]["supplier_reliability"] == 0.85

    def test_add_observations_requires_observe_state(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]

        with pytest.raises(ValidationError, match="observe"):
            add_session_observation(
                test_db,
                session_id=sid,
                observations={"key": "val"},
                created_by="test",
            )


class TestProposeFlow:
    def test_propose_creates_proposal_node(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )

        result = propose_twin_config(
            test_db,
            session_id=sid,
            created_by="test",
        )
        assert "proposal" in result
        assert result["proposal"]["type"] == "twin_design_proposal"

    def test_propose_auto_transitions_to_approve(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )

        result = propose_twin_config(
            test_db,
            session_id=sid,
            created_by="test",
        )
        import json

        meta = json.loads(result["session"]["metadata"]) if isinstance(result["session"]["metadata"], str) else result["session"]["metadata"]
        assert meta["session_state"] == "approve"

    def test_propose_requires_propose_state(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]

        with pytest.raises(ValidationError, match="propose"):
            propose_twin_config(
                test_db,
                session_id=sid,
                created_by="test",
            )


class TestReviewFlow:
    def test_approve_creates_approves_edge(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )
        proposal_result = propose_twin_config(
            test_db, session_id=sid, created_by="test"
        )
        proposal_id = proposal_result["proposal"]["id"]

        result = review_proposal(
            test_db,
            session_id=sid,
            proposal_id=proposal_id,
            decision="approve",
            created_by="test",
        )
        assert result["decision"] == "approve"
        assert result["new_state"] == "instantiate"

    def test_decline_creates_declines_edge(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )
        proposal_result = propose_twin_config(
            test_db, session_id=sid, created_by="test"
        )
        proposal_id = proposal_result["proposal"]["id"]

        result = review_proposal(
            test_db,
            session_id=sid,
            proposal_id=proposal_id,
            decision="decline",
            created_by="test",
        )
        assert result["new_state"] == "abandoned"

    def test_modify_reenters_propose_state(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )
        proposal_result = propose_twin_config(
            test_db, session_id=sid, created_by="test"
        )
        proposal_id = proposal_result["proposal"]["id"]

        result = review_proposal(
            test_db,
            session_id=sid,
            proposal_id=proposal_id,
            decision="modify",
            modifications={"confidence_threshold": 0.8},
            created_by="test",
        )
        assert result["new_state"] == "propose"

    def test_modify_stores_modifications(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="propose", created_by="test"
        )
        proposal_result = propose_twin_config(
            test_db, session_id=sid, created_by="test"
        )
        proposal_id = proposal_result["proposal"]["id"]

        review_proposal(
            test_db,
            session_id=sid,
            proposal_id=proposal_id,
            decision="modify",
            modifications={"confidence_threshold": 0.8},
            created_by="test",
        )
        import json

        proposal = _get_node(test_db, proposal_id)
        meta = json.loads(proposal["metadata"]) if isinstance(proposal["metadata"], str) else proposal["metadata"]
        assert meta["modifications"]["confidence_threshold"] == 0.8


class TestInstantiateFlow:
    def test_instantiate_requires_approve_state(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]

        with pytest.raises(ValidationError, match="instantiate"):
            instantiate_from_session(
                test_db, session_id=sid, created_by="test"
            )


class TestCalibrationFlow:
    def test_calibrate_records_metrics(self, test_db):
        session = _create_session_to_calibrate(test_db)
        sid = session["id"]

        result = record_calibration(
            test_db,
            session_id=sid,
            observations={"pred_1": 0.8},
            actuals={"pred_1": 0.75},
            created_by="test",
        )
        assert "calibration_metrics" in result
        assert result["max_drift"] == pytest.approx(0.05)

    def test_high_drift_triggers_evolve(self, test_db):
        session = _create_session_to_calibrate(test_db)
        sid = session["id"]

        result = record_calibration(
            test_db,
            session_id=sid,
            observations={"pred_1": 0.9},
            actuals={"pred_1": 0.5},
            created_by="test",
        )
        assert result["recommended_next_state"] == "evolve"


class TestEvolveFlow:
    def test_evolve_transitions_to_propose(self, test_db):
        session = _create_session_to_operate(test_db)
        sid = session["id"]

        result = evolve_session(
            test_db,
            session_id=sid,
            reason="Drift detected in predictions",
            proposed_changes={"model": "updated"},
            created_by="test",
        )
        import json

        meta = json.loads(result["session"]["metadata"]) if isinstance(result["session"]["metadata"], str) else result["session"]["metadata"]
        assert meta["session_state"] == "propose"

    def test_evolve_requires_reason(self, test_db):
        session = _create_session_to_operate(test_db)
        sid = session["id"]

        with pytest.raises(ValidationError, match="reason"):
            evolve_session(
                test_db,
                session_id=sid,
                reason="",
                proposed_changes={},
                created_by="test",
            )


class TestAuditChain:
    def test_audit_returns_full_history(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )

        audit = get_session_audit(test_db, session_id=sid)
        assert "transitions" in audit
        assert len(audit["transitions"]) >= 1

    def test_audit_provenance_complete(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]
        transition_session(
            test_db, session_id=sid, to_state="discover", created_by="test"
        )
        transition_session(
            test_db, session_id=sid, to_state="observe", created_by="test"
        )

        audit = get_session_audit(test_db, session_id=sid)
        assert len(audit["transitions"]) == 2


class TestGetSessionState:
    def test_get_session_state_returns_current(self, test_db):
        session = start_twin_design_session(
            test_db, goal="Test", created_by="test"
        )
        sid = session["id"]

        state = get_session_state(test_db, session_id=sid)
        assert state["current_state"] == "init"
        assert "history" in state

    def test_get_session_state_not_found(self, test_db):
        with pytest.raises(NodeNotFoundError):
            get_session_state(test_db, session_id="nonexistent_id")


class TestSessionSDK:
    def test_full_session_lifecycle(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test_agent")

        session = g.start_twin_design_session("Design a supply chain twin")
        sid = session["id"]
        assert session["type"] == "twin_design_session"

        g.transition_session(sid, to_state="discover")
        g.transition_session(sid, to_state="observe")
        g.add_session_observation(sid, observations={"supplier_reliability": 0.85})

        g.transition_session(sid, to_state="propose")
        proposal_result = g.propose_twin_config(sid)
        proposal_id = proposal_result["proposal"]["id"]

        review_result = g.review_proposal(
            sid, proposal_id, decision="modify", modifications={"threshold": 0.8}
        )
        assert review_result["new_state"] == "propose"

        second_proposal = g.propose_twin_config(sid)
        second_pid = second_proposal["proposal"]["id"]

        approve_result = g.review_proposal(sid, second_pid, decision="approve")
        assert approve_result["new_state"] == "instantiate"

        state = g.get_session_state(sid)
        assert state["current_state"] == "instantiate"

    def test_sdk_get_session_state(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test_agent")
        session = g.start_twin_design_session("Test")
        sid = session["id"]

        state = g.get_session_state(sid)
        assert state["current_state"] == "init"
        assert "history" in state

    def test_sdk_get_session_audit(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test_agent")
        session = g.start_twin_design_session("Test")
        sid = session["id"]

        audit = g.get_session_audit(sid)
        assert "transitions" in audit
        assert "proposals" in audit


def _get_node(conn, node_id):
    import json

    row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    columns = [desc[0] for desc in conn.description]
    return dict(zip(columns, row))


def _create_session_to_calibrate(conn):
    session = start_twin_design_session(
        conn, goal="Test", created_by="test"
    )
    sid = session["id"]
    transition_session(conn, session_id=sid, to_state="discover", created_by="test")
    transition_session(conn, session_id=sid, to_state="observe", created_by="test")
    transition_session(conn, session_id=sid, to_state="propose", created_by="test")
    propose_twin_config(conn, session_id=sid, created_by="test")
    proposal_id = _get_current_proposal_id(conn, sid)
    review_proposal(conn, session_id=sid, proposal_id=proposal_id, decision="approve", created_by="test")
    transition_session(conn, session_id=sid, to_state="calibrate", created_by="test")
    return _get_node(conn, sid)


def _create_session_to_operate(conn):
    session = start_twin_design_session(
        conn, goal="Test", created_by="test"
    )
    sid = session["id"]
    transition_session(conn, session_id=sid, to_state="discover", created_by="test")
    transition_session(conn, session_id=sid, to_state="observe", created_by="test")
    transition_session(conn, session_id=sid, to_state="propose", created_by="test")
    propose_twin_config(conn, session_id=sid, created_by="test")
    proposal_id = _get_current_proposal_id(conn, sid)
    review_proposal(conn, session_id=sid, proposal_id=proposal_id, decision="approve", created_by="test")
    transition_session(conn, session_id=sid, to_state="calibrate", created_by="test")
    for _ in range(3):
        record_calibration(
            conn,
            session_id=sid,
            observations={"pred": 0.8},
            actuals={"pred": 0.79},
            created_by="test",
        )
    return _get_node(conn, sid)


def _get_current_proposal_id(conn, session_id):
    import json

    row = conn.execute(
        "SELECT metadata FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return meta.get("current_proposal_id")
