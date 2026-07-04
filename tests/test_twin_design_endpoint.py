"""HTTP endpoint tests for the conversational /twin/design session state machine (OHM-konq)."""

from __future__ import annotations

import pytest

from tests.conftest import _request


def _start(port, goal="Test goal", **kwargs):
    body = {"goal": goal, **kwargs}
    return _request("POST", port, "/twin/design/start", body)


def _transition(port, sid, to_state, **kwargs):
    body = {"to_state": to_state, **kwargs}
    return _request("POST", port, f"/twin/design/{sid}/transition", body)


def _observe(port, sid, observations):
    return _request("POST", port, f"/twin/design/{sid}/observe", {"observations": observations})


def _propose(port, sid, **kwargs):
    return _request("POST", port, f"/twin/design/{sid}/propose", kwargs)


def _review(port, sid, proposal_id, decision, **kwargs):
    body = {"proposal_id": proposal_id, "decision": decision, **kwargs}
    return _request("POST", port, f"/twin/design/{sid}/review", body)


def _instantiate(port, sid):
    return _request("POST", port, f"/twin/design/{sid}/instantiate", {})


def _calibrate(port, sid, observations, actuals):
    return _request("POST", port, f"/twin/design/{sid}/calibrate", {"observations": observations, "actuals": actuals})


def _evolve(port, sid, reason, proposed_changes=None):
    return _request("POST", port, f"/twin/design/{sid}/evolve", {"reason": reason, "proposed_changes": proposed_changes or {}})


def _get_state(port, sid):
    return _request("GET", port, f"/twin/design/{sid}/state")


def _get_audit(port, sid):
    return _request("GET", port, f"/twin/design/{sid}/audit")


def _advance_to_observe(port, sid):
    _transition(port, sid, "discover")
    _transition(port, sid, "observe")


def _advance_to_propose(port, sid):
    _advance_to_observe(port, sid)
    _transition(port, sid, "propose")


def _advance_to_approve(port, sid):
    _advance_to_propose(port, sid)
    result = _propose(port, sid)
    return result[1]["data"]["proposal"]["id"]


def _advance_to_instantiate(port, sid):
    pid = _advance_to_approve(port, sid)
    _review(port, sid, pid, "approve")


def _advance_to_calibrate(port, sid):
    _advance_to_instantiate(port, sid)
    _instantiate(port, sid)


def _advance_to_operate(port, sid):
    _advance_to_calibrate(port, sid)
    for _ in range(3):
        _calibrate(port, sid, {"pred": 0.8}, {"pred": 0.79})


class TestStartEndpoint:
    def test_start_creates_session(self, test_server):
        port, _ = test_server
        status, data = _start(port, goal="Design a supply chain twin")
        assert status == 201
        assert data["ok"] is True
        session = data["data"]
        assert session["type"] == "twin_design_session"
        assert session["id"]

    def test_start_with_context(self, test_server):
        port, _ = test_server
        status, data = _start(port, goal="Test", context={"domain": "finance"})
        assert status == 201
        assert data["ok"] is True

    def test_start_missing_goal(self, test_server):
        port, _ = test_server
        status, data = _request("POST", port, "/twin/design/start", {})
        assert status in (400, 422)
        assert data.get("error")

    def test_start_empty_goal(self, test_server):
        port, _ = test_server
        status, data = _start(port, goal="")
        assert status in (400, 422)
        assert data.get("error")


class TestTransitionEndpoint:
    def test_valid_transition(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _transition(port, sid, "discover")
        assert status == 200
        assert data["ok"] is True

    def test_invalid_transition_raises(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _transition(port, sid, "operate")
        assert status in (400, 422)
        assert data.get("error")

    def test_missing_to_state(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _request("POST", port, f"/twin/design/{sid}/transition", {})
        assert status in (400, 422)
        assert data.get("error")

    def test_transition_with_notes(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _transition(port, sid, "discover", notes="starting discovery")
        assert status == 200
        assert data["ok"] is True

    def test_transition_to_abandoned(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _transition(port, sid, "abandoned")
        assert status == 200
        assert data["ok"] is True

    def test_transition_nonexistent_session(self, test_server):
        port, _ = test_server
        status, data = _transition(port, "nonexistent_id", "discover")
        assert status == 404
        assert data.get("error")


class TestObserveEndpoint:
    def test_add_observations(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_observe(port, sid)
        status, data = _observe(port, sid, {"supplier_reliability": 0.85})
        assert status == 200
        assert data["ok"] is True

    def test_observe_wrong_state(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _observe(port, sid, {"key": "val"})
        assert status in (400, 422)
        assert data.get("error")

    def test_observe_missing_observations(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_observe(port, sid)
        status, data = _request("POST", port, f"/twin/design/{sid}/observe", {})
        assert status in (400, 422)
        assert data.get("error")


class TestProposeEndpoint:
    def test_propose_creates_proposal(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_propose(port, sid)
        status, data = _propose(port, sid)
        assert status == 201
        assert data["ok"] is True
        assert data["data"]["proposal"]["type"] == "twin_design_proposal"

    def test_propose_wrong_state(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _propose(port, sid)
        assert status in (400, 422)
        assert data.get("error")

    def test_propose_auto_transitions_to_approve(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_propose(port, sid)
        _, data = _propose(port, sid)
        import json

        meta = json.loads(data["data"]["session"]["metadata"])
        assert meta["session_state"] == "approve"


class TestReviewEndpoint:
    def test_approve(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        status, data = _review(port, sid, pid, "approve")
        assert status == 200
        assert data["ok"] is True
        assert data["data"]["decision"] == "approve"
        assert data["data"]["new_state"] == "instantiate"

    def test_decline(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        status, data = _review(port, sid, pid, "decline")
        assert status == 200
        assert data["ok"] is True
        assert data["data"]["new_state"] == "abandoned"

    def test_modify(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        status, data = _review(port, sid, pid, "modify", modifications={"threshold": 0.8})
        assert status == 200
        assert data["ok"] is True
        assert data["data"]["new_state"] == "propose"

    def test_review_missing_proposal_id(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_approve(port, sid)
        status, data = _request("POST", port, f"/twin/design/{sid}/review", {"decision": "approve"})
        assert status in (400, 422)
        assert data.get("error")

    def test_review_missing_decision(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        status, data = _request("POST", port, f"/twin/design/{sid}/review", {"proposal_id": pid})
        assert status in (400, 422)
        assert data.get("error")

    def test_review_with_aspects(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        status, data = _review(
            port,
            sid,
            pid,
            "approve",
            approved_aspects=["model_selection"],
            declined_aspects=["calibration_plan"],
            reason="looks good overall",
        )
        assert status == 200
        assert data["ok"] is True


class TestInstantiateEndpoint:
    def test_instantiate_wrong_state(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _instantiate(port, sid)
        assert status in (400, 422)
        assert data.get("error")

    def test_instantiate_after_approve(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_instantiate(port, sid)
        status, data = _instantiate(port, sid)
        assert status == 201
        assert data["ok"] is True
        assert "calibration_plan" in data["data"]


class TestCalibrateEndpoint:
    def test_calibrate_records_metrics(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_calibrate(port, sid)
        status, data = _calibrate(port, sid, {"pred_1": 0.8}, {"pred_1": 0.75})
        assert status == 200
        assert data["ok"] is True
        assert "calibration_metrics" in data["data"]
        assert data["data"]["max_drift"] == pytest.approx(0.05)

    def test_calibrate_high_drift_recommends_evolve(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_calibrate(port, sid)
        status, data = _calibrate(port, sid, {"pred_1": 0.9}, {"pred_1": 0.5})
        assert status == 200
        assert data["data"]["recommended_next_state"] == "evolve"

    def test_calibrate_missing_fields(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_calibrate(port, sid)
        status, data = _request("POST", port, f"/twin/design/{sid}/calibrate", {"observations": {"a": 1}})
        assert status in (400, 422)
        assert data.get("error")


class TestEvolveEndpoint:
    def test_evolve_from_operate(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_operate(port, sid)
        status, data = _evolve(port, sid, "Drift detected", {"model": "updated"})
        assert status == 200
        assert data["ok"] is True
        import json

        meta = json.loads(data["data"]["session"]["metadata"])
        assert meta["session_state"] == "propose"

    def test_evolve_missing_reason(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_operate(port, sid)
        status, data = _request("POST", port, f"/twin/design/{sid}/evolve", {"proposed_changes": {}})
        assert status in (400, 422)
        assert data.get("error")


class TestStateEndpoint:
    def test_get_state_returns_current(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _get_state(port, sid)
        assert status == 200
        assert data["ok"] is True
        assert data["data"]["current_state"] == "init"
        assert "history" in data["data"]

    def test_get_state_after_transition(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _transition(port, sid, "discover")
        status, data = _get_state(port, sid)
        assert status == 200
        assert data["data"]["current_state"] == "discover"

    def test_get_state_not_found(self, test_server):
        port, _ = test_server
        status, data = _get_state(port, "nonexistent_id")
        assert status == 404
        assert data.get("error")


class TestAuditEndpoint:
    def test_get_audit_returns_history(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _transition(port, sid, "discover")
        status, data = _get_audit(port, sid)
        assert status == 200
        assert data["ok"] is True
        assert "transitions" in data["data"]
        assert len(data["data"]["transitions"]) >= 1

    def test_get_audit_provenance_complete(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _transition(port, sid, "discover")
        _transition(port, sid, "observe")
        status, data = _get_audit(port, sid)
        assert status == 200
        assert len(data["data"]["transitions"]) == 2

    def test_get_audit_not_found(self, test_server):
        port, _ = test_server
        status, data = _get_audit(port, "nonexistent_id")
        assert status == 404
        assert data.get("error")

    def test_audit_includes_proposals_after_propose(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        _advance_to_approve(port, sid)
        status, data = _get_audit(port, sid)
        assert status == 200
        assert len(data["data"]["proposals"]) >= 1

    def test_audit_includes_approvals_after_review(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        pid = _advance_to_approve(port, sid)
        _review(port, sid, pid, "approve")
        status, data = _get_audit(port, sid)
        assert status == 200
        assert len(data["data"]["approvals"]) >= 1


class TestFullLifecycleEndpoint:
    def test_full_session_lifecycle(self, test_server):
        port, _ = test_server

        # start
        _, data = _start(port, goal="Design a supply chain twin")
        sid = data["data"]["id"]

        # discover
        _transition(port, sid, "discover")

        # observe
        _transition(port, sid, "observe")
        _observe(port, sid, {"supplier_reliability": 0.85})

        # propose
        _transition(port, sid, "propose")
        _, data = _propose(port, sid)
        pid = data["data"]["proposal"]["id"]

        # review — modify first time
        _, data = _review(port, sid, pid, "modify", modifications={"threshold": 0.8})
        assert data["data"]["new_state"] == "propose"

        # propose again
        _, data = _propose(port, sid)
        pid2 = data["data"]["proposal"]["id"]

        # review — approve
        _, data = _review(port, sid, pid2, "approve")
        assert data["data"]["new_state"] == "instantiate"

        # instantiate
        _, data = _instantiate(port, sid)
        assert "calibration_plan" in data["data"]

        # calibrate 3 times (low drift → operate)
        for _ in range(3):
            _calibrate(port, sid, {"pred": 0.8}, {"pred": 0.79})

        # verify state is operate
        _, data = _get_state(port, sid)
        assert data["data"]["current_state"] == "operate"

        # evolve
        _, data = _evolve(port, sid, "Periodic retraining", {"model": "v2"})
        import json

        meta = json.loads(data["data"]["session"]["metadata"])
        assert meta["session_state"] == "propose"

        # audit should show full history
        _, data = _get_audit(port, sid)
        assert len(data["data"]["transitions"]) >= 8
        assert len(data["data"]["proposals"]) >= 2


class TestUnknownAction:
    def test_unknown_post_action(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _request("POST", port, f"/twin/design/{sid}/bogus", {})
        assert status in (400, 422)
        assert data.get("error")

    def test_unknown_get_action(self, test_server):
        port, _ = test_server
        _, data = _start(port)
        sid = data["data"]["id"]
        status, data = _request("GET", port, f"/twin/design/{sid}/bogus")
        assert status in (400, 422)
        assert data.get("error")
