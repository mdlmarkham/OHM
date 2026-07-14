"""Twin handler mixin: registration, bindings, prediction, templates."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class TwinHandlerMixin(OhmHandlerBase):
    """Handler mixin for twin core + template endpoints."""

    def _post_register_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/register — register an external domain twin (OHM-josq).

        Body: {label, target_node_id, endpoint_url?, description?, connects_to?}
        """
        from ohm.queries import register_twin
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = register_twin(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                created_by=agent,
                endpoint_url=body.get("endpoint_url"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_register_twin_with_bindings(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/register-with-bindings — register twin with bindings (OHM-f7tl).

        Body: {label, target_node_id, decision_node_id?, feed_node_ids?,
               model_candidate_ids?, description?, endpoint_url?}
        """
        from ohm.queries import register_twin_with_bindings
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = register_twin_with_bindings(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                decision_node_id=body.get("decision_node_id"),
                feed_node_ids=body.get("feed_node_ids"),
                model_candidate_ids=body.get("model_candidate_ids"),
                created_by=agent,
                description=body.get("description"),
                endpoint_url=body.get("endpoint_url"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_add_twin_bindings(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/add-bindings — add/remove feed bindings (OHM-f7tl).

        Body: {feed_node_ids?, feed_node_ids_remove?}
        """
        from ohm.queries import add_twin_bindings
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = add_twin_bindings(
                self.current_store.conn,
                twin_id=twin_id,
                feed_node_ids=body.get("feed_node_ids"),
                feed_node_ids_remove=body.get("feed_node_ids_remove"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_attach_twin_models(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/attach-models — attach/detach model candidates (OHM-f7tl).

        Body: {model_candidate_ids?, model_candidate_ids_remove?}
        """
        from ohm.queries import attach_twin_models
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = attach_twin_models(
                self.current_store.conn,
                twin_id=twin_id,
                model_candidate_ids=body.get("model_candidate_ids"),
                model_candidate_ids_remove=body.get("model_candidate_ids_remove"),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_predict(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/predict — twin predictions as edge_overrides (OHM-josq)."""
        from ohm.queries import twin_predict
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = twin_predict(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_constraints(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/constraints — twin constraints (OHM-josq)."""
        from ohm.queries import twin_constraints
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = twin_constraints(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_validate_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /twin/{id}/{validate-action|auto-promote|add-bindings|attach-models} to the right handler (OHM-josq, OHM-75tw, OHM-f7tl)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /twin/{id}/{validate-action|auto-promote|add-bindings|attach-models} required")
        action = parts[2]
        if action == "validate-action":
            self._post_twin_validate_action(path, qs, body, agent)
        elif action == "auto-promote":
            self._post_auto_promote(path, qs, body, agent)
        elif action == "add-bindings":
            self._post_add_twin_bindings(path, qs, body, agent)
        elif action == "attach-models":
            self._post_attach_twin_models(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown twin POST action: {action}")

    def _post_twin_validate_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/validate-action — validate action against twin constraints (OHM-josq).

        Body: {action_id}
        """
        from ohm.queries import validate_action_against_twin
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        action_id = body.get("action_id")
        if not action_id:
            raise ValidationError("action_id is required")

        try:
            result = validate_action_against_twin(
                self.current_store.conn,
                twin_id=twin_id,
                action_id=action_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_explain(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/explain — explain what the twin models (OHM-josq)."""
        from ohm.queries import explain_twin
        from ohm.exceptions import NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("twin_id is required in path")

        try:
            result = explain_twin(self.current_store.read_conn, twin_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_readiness(self, path: str, qs: dict) -> None:
        """GET /twin/{id}/readiness — check twin readiness gates (OHM-f7tl).

        Optional query params:
          - freshness_days (int): override the default 7-day feed
            freshness window. When set, the response distinguishes
            "no threshold set" from "threshold exceeded" (kg16 item 4).
        """
        from ohm.queries import get_twin_readiness
        from ohm.exceptions import NodeNotFoundError, ValidationError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        freshness_days: int | None = None
        raw_days = qs.get("freshness_days", [None])[0]
        if raw_days is not None:
            try:
                freshness_days = int(raw_days)
            except (TypeError, ValueError) as e:
                raise ValidationError(f"freshness_days must be an integer, got {raw_days!r}") from e

        try:
            result = get_twin_readiness(
                self.current_store.read_conn,
                twin_id=twin_id,
                freshness_days=freshness_days,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_twin_get(self, path: str, qs: dict) -> None:
        """Dispatch /twin/{id}/{predict|constraints|explain|drift|ensemble} to the right handler (OHM-josq, OHM-bf45)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /twin/{id}/{predict|constraints|explain|drift|ensemble} required")
        parts[1]
        action = parts[2]
        if action == "predict":
            self._get_twin_predict(path, qs)
        elif action == "constraints":
            self._get_twin_constraints(path, qs)
        elif action == "explain":
            self._get_twin_explain(path, qs)
        elif action == "drift":
            self._get_detect_drift(path, qs)
        elif action == "ensemble":
            self._get_ensemble_predict(path, qs)
        elif action == "readiness":
            self._get_twin_readiness(path, qs)
        else:
            raise ValidationError(f"unknown twin action: {action}")

    def _post_create_twin_template(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin-template — create a twin template (OHM-hl61).

        Body: {label, target_node_id, constraint_schema?, required_edges?, description?, connects_to?}
        """
        from ohm.queries import create_twin_template
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        if not label:
            raise ValidationError("label is required")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = create_twin_template(
                self.current_store.conn,
                label=label,
                target_node_id=target_node_id,
                created_by=agent,
                constraint_schema=body.get("constraint_schema"),
                required_edges=body.get("required_edges"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_twin_templates(self, path: str, qs: dict) -> None:
        """GET /twin-templates — list twin templates (OHM-hl61).

        Filters: ?target_node_id=, ?created_by=, ?limit=
        """
        from ohm.queries import list_twin_templates

        target_node_id = qs.get("target_node_id", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [50])[0])

        result = list_twin_templates(
            self.current_store.read_conn,
            target_node_id=target_node_id,
            created_by=created_by,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": result})

    def _get_twin_template(self, path: str, qs: dict) -> None:
        """GET /twin-template/{id} — get a twin template (OHM-hl61)."""
        from ohm.queries import get_twin_template
        from ohm.exceptions import NodeNotFoundError, ValidationError

        parts = path.strip("/").split("/")
        template_id = parts[1] if len(parts) >= 2 else None
        if not template_id:
            raise ValidationError("template_id is required in path")

        try:
            result = get_twin_template(self.current_store.read_conn, template_id)
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_instantiate_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin-template/{id}/instantiate — instantiate a twin from template (OHM-hl61).

        Body: {target_node_id, label?, connects_to?}
        """
        from ohm.queries import instantiate_twin_from_template
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        template_id = parts[1] if len(parts) >= 2 else None
        if not template_id:
            raise ValidationError("template_id is required in path")

        target_node_id = body.get("target_node_id")
        if not target_node_id:
            raise ValidationError("target_node_id is required")

        try:
            result = instantiate_twin_from_template(
                self.current_store.conn,
                template_id=template_id,
                target_node_id=target_node_id,
                created_by=agent,
                label=body.get("label"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_assemble_twin(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/assemble — assemble a decision-specific twin (OHM-f7tl).

        Body: {decision_node_id, goal, horizon?, preferred_template_id?, preferred_model_id?}
        """
        from ohm.queries import assemble_twin_for_decision
        from ohm.exceptions import ValidationError, NodeNotFoundError

        decision_node_id = body.get("decision_node_id")
        goal = body.get("goal")
        if not decision_node_id:
            raise ValidationError("decision_node_id is required")
        if not goal:
            raise ValidationError("goal is required")

        try:
            result = assemble_twin_for_decision(
                self.current_store.conn,
                decision_node_id=decision_node_id,
                goal=goal,
                horizon=body.get("horizon", 7),
                preferred_template_id=body.get("preferred_template_id"),
                preferred_model_id=body.get("preferred_model_id"),
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_twin_template_get(self, path: str, qs: dict) -> None:
        """Dispatch /twin-template/{id}/{action} to the right handler (OHM-hl61)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 2:
            raise ValidationError("GET /twin-template/{id} required")
        parts[1]
        if len(parts) >= 3 and parts[2]:
            raise ValidationError(f"unknown twin-template action: {parts[2]}")
        self._get_twin_template(path, qs)
