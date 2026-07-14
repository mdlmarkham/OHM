"""Temporal/twin-design handler mixin: freshness thresholds, mode switching."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class TemporalHandlerMixin(OhmHandlerBase):
    """Handler mixin for temporal freshness/mode-switch/twin-design endpoints."""

    def _post_set_freshness_threshold(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import set_freshness_threshold
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        max_age_seconds = body.get("max_age_seconds")
        if not decision_id:
            raise ValidationError("decision_id is required")
        if max_age_seconds is None:
            raise ValidationError("max_age_seconds is required")

        try:
            max_age_seconds = int(max_age_seconds)
        except (ValueError, TypeError):
            raise ValidationError("max_age_seconds must be an integer")

        try:
            result = set_freshness_threshold(
                self.current_store.conn,
                decision_id=decision_id,
                max_age_seconds=max_age_seconds,
                created_by=agent,
                label=body.get("label"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_compute_feed_investment(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import compute_feed_investment
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        if not decision_id:
            raise ValidationError("decision_id is required")

        try:
            observation_cost = float(body.get("observation_cost", 0.5))
        except (ValueError, TypeError):
            raise ValidationError("observation_cost must be a number")

        try:
            result = compute_feed_investment(
                self.current_store.conn,
                decision_id=decision_id,
                created_by=agent,
                observation_cost=observation_cost,
                label=body.get("label"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_record_mode_switch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import get_current_mode, record_mode_switch
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_id = body.get("decision_id")
        from_mode = body.get("from_mode")
        to_mode = body.get("to_mode")
        if not decision_id:
            raise ValidationError("decision_id is required")
        if not to_mode:
            raise ValidationError("to_mode is required")

        # from_mode is optional (OHM-kg16 item 5): if not provided,
        # derive it from the most recent mode_switch node for this
        # decision. The caller can always override by passing it
        # explicitly.
        from_mode_source = "explicit"
        if not from_mode:
            current = get_current_mode(self.current_store.conn, decision_id=decision_id)
            if current is None or not current.get("to_mode"):
                raise ValidationError("from_mode is required for the first mode switch on a decision — no prior mode_switch node exists. Pass from_mode explicitly or call GET /temporal/{decision_id}/mode first.")
            from_mode = current["to_mode"]
            from_mode_source = "derived"

        try:
            result = record_mode_switch(
                self.current_store.conn,
                decision_id=decision_id,
                from_mode=from_mode,
                to_mode=to_mode,
                created_by=agent,
                reason=body.get("reason"),
                label=body.get("label"),
            )
            self._json_response(
                201,
                {
                    "ok": True,
                    "data": result,
                    "from_mode_source": from_mode_source,
                },
            )
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation", "message": str(e)})

    def _route_temporal_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /temporal/{decision_id}/{action} required")
        decision_id = parts[1]
        action = parts[2]
        if action == "freshness":
            self._get_freshness_status(decision_id, qs)
        elif action == "mode":
            self._get_recommend_mode(decision_id, qs)
        elif action == "summary":
            self._get_temporal_summary(decision_id, qs)
        else:
            raise ValidationError(f"unknown temporal GET action: {action}")

    def _get_freshness_status(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import get_freshness_status
        from ohm.exceptions import NodeNotFoundError

        try:
            result = get_freshness_status(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_recommend_mode(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import recommend_mode
        from ohm.exceptions import NodeNotFoundError

        try:
            result = recommend_mode(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_temporal_summary(self, decision_id: str, qs: dict) -> None:
        from ohm.queries import temporal_decision_summary
        from ohm.exceptions import NodeNotFoundError

        try:
            result = temporal_decision_summary(
                self.current_store.conn,
                decision_id=decision_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_twin_design_start(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import start_twin_design_session
        from ohm.exceptions import ValidationError

        goal = body.get("goal")
        if not goal:
            raise ValidationError("goal is required")

        result = start_twin_design_session(
            self.current_store.conn,
            goal=goal,
            context=body.get("context"),
            created_by=agent,
            label=body.get("label"),
        )
        self._json_response(201, {"ok": True, "data": result})

    def _route_twin_design_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /twin/design/{session_id}/{action} required")

        session_id = parts[2]
        action = parts[3] if len(parts) >= 4 else ""

        if action == "transition":
            to_state = body.get("to_state")
            if not to_state:
                raise ValidationError("to_state is required")
            from ohm.queries import transition_session

            result = transition_session(
                self.current_store.conn,
                session_id=session_id,
                to_state=to_state,
                notes=body.get("notes"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "observe":
            observations = body.get("observations")
            if not observations:
                raise ValidationError("observations is required")
            from ohm.queries import add_session_observation

            result = add_session_observation(
                self.current_store.conn,
                session_id=session_id,
                observations=observations,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "propose":
            from ohm.queries import propose_twin_config

            result = propose_twin_config(
                self.current_store.conn,
                session_id=session_id,
                decision_node_id=body.get("decision_node_id"),
                preferred_template_id=body.get("preferred_template_id"),
                preferred_model_id=body.get("preferred_model_id"),
                confidence_threshold=body.get("confidence_threshold", 0.6),
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        elif action == "review":
            proposal_id = body.get("proposal_id")
            decision = body.get("decision")
            if not proposal_id:
                raise ValidationError("proposal_id is required")
            if not decision:
                raise ValidationError("decision is required")
            from ohm.queries import review_proposal

            result = review_proposal(
                self.current_store.conn,
                session_id=session_id,
                proposal_id=proposal_id,
                decision=decision,
                approved_aspects=body.get("approved_aspects"),
                declined_aspects=body.get("declined_aspects"),
                modifications=body.get("modifications"),
                reason=body.get("reason"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "instantiate":
            from ohm.queries import instantiate_from_session

            result = instantiate_from_session(
                self.current_store.conn,
                session_id=session_id,
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        elif action == "calibrate":
            observations = body.get("observations")
            actuals = body.get("actuals")
            if not observations or not actuals:
                raise ValidationError("observations and actuals are required")
            from ohm.queries import record_calibration

            result = record_calibration(
                self.current_store.conn,
                session_id=session_id,
                observations=observations,
                actuals=actuals,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "evolve":
            reason = body.get("reason")
            proposed_changes = body.get("proposed_changes")
            if not reason:
                raise ValidationError("reason is required")
            from ohm.queries import evolve_session

            result = evolve_session(
                self.current_store.conn,
                session_id=session_id,
                reason=reason,
                proposed_changes=proposed_changes or {},
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        else:
            raise ValidationError(f"unknown twin design action: {action}")

    def _route_twin_design_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /twin/design/{session_id}/{state|audit} required")

        session_id = parts[2]
        action = parts[3] if len(parts) >= 4 else "state"

        if action == "state":
            from ohm.queries import get_session_state

            result = get_session_state(
                self.current_store.read_conn,
                session_id=session_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        elif action == "audit":
            from ohm.queries import get_session_audit

            result = get_session_audit(
                self.current_store.read_conn,
                session_id=session_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        else:
            raise ValidationError(f"unknown twin design GET action: {action}")
