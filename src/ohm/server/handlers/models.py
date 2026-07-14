"""Model handler mixin: registration, evaluation, promotion, drift detection."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class ModelHandlerMixin(OhmHandlerBase):
    """Handler mixin for model registration/evaluation/promotion endpoints."""

    def _post_register_model_candidate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/register — register a model candidate for a twin (OHM-75tw).

        Body: {label, twin_id, model_parameters?, description?, connects_to?}
        """
        from ohm.queries import register_model_candidate
        from ohm.exceptions import ValidationError, NodeNotFoundError

        label = body.get("label")
        twin_id = body.get("twin_id")
        if not label:
            raise ValidationError("label is required")
        if not twin_id:
            raise ValidationError("twin_id is required")

        try:
            result = register_model_candidate(
                self.current_store.conn,
                label=label,
                twin_id=twin_id,
                created_by=agent,
                model_parameters=body.get("model_parameters"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_evaluate_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/evaluate — evaluate a model candidate (OHM-75tw).

        Body: {metrics, dataset?, description?}
        """
        from ohm.queries import evaluate_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        metrics = body.get("metrics")
        if not metrics:
            raise ValidationError("metrics is required")

        try:
            result = evaluate_model(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                created_by=agent,
                metrics=metrics,
                dataset=body.get("dataset"),
                description=body.get("description"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_promote_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/promote — promote a model candidate to active (OHM-75tw)."""
        from ohm.queries import promote_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        try:
            result = promote_model(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                created_by=agent,
                policy=body.get("policy", "accuracy"),
                decision_node_id=body.get("decision_node_id"),
                min_improvement=float(body.get("min_improvement", 0.0)),
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _get_compare_models(self, path: str, qs: dict) -> None:
        """GET /model/compare — compare model candidates for a twin (OHM-75tw).

        Query params: ?twin_id=
        """
        from ohm.queries import compare_models
        from ohm.exceptions import ValidationError, NodeNotFoundError

        twin_id = qs.get("twin_id", [None])[0]
        if not twin_id:
            raise ValidationError("twin_id query parameter is required")

        try:
            result = compare_models(
                self.current_store.read_conn,
                twin_id=twin_id,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _route_model_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /model/{id}/{evaluate|promote|validate|retire|promotion-policy} to the right handler (OHM-75tw, OHM-bf45)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /model/{id}/{evaluate|promote|validate|retire|promotion-policy} required")
        action = parts[2]
        if action == "evaluate":
            self._post_evaluate_model(path, qs, body, agent)
        elif action == "promote":
            self._post_promote_model(path, qs, body, agent)
        elif action == "validate":
            self._post_validate_model(path, qs, body, agent)
        elif action == "retire":
            self._post_auto_retire_model(path, qs, body, agent)
        elif action == "promotion-policy":
            self._post_set_promotion_policy(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown model action: {action}")

    def _post_register_shadow_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import register_shadow_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        twin_id = body.get("twin_id")
        label = body.get("label")
        source_model_id = body.get("source_model_id")
        if not twin_id:
            raise ValidationError("twin_id is required")
        if not label:
            raise ValidationError("label is required")
        if not source_model_id:
            raise ValidationError("source_model_id is required")

        try:
            result = register_shadow_model(
                self.current_store.conn,
                twin_id=twin_id,
                label=label,
                source_model_id=source_model_id,
                created_by=agent,
                model_parameters=body.get("model_parameters"),
                description=body.get("description"),
                connects_to=body.get("connects_to"),
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_detect_drift(self, path: str, qs: dict) -> None:
        from ohm.queries import detect_drift
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        window_size = int(qs.get("window_size", [100])[0])
        residual_threshold = float(qs.get("residual_threshold", [0.15])[0])

        try:
            result = detect_drift(
                self.current_store.read_conn,
                twin_id=twin_id,
                window_size=window_size,
                residual_threshold=residual_threshold,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_validate_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import run_walk_forward_validation
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        n_splits = int(body.get("n_splits", 5))
        min_train_size = int(body.get("min_train_size", 50))

        try:
            result = run_walk_forward_validation(
                self.current_store.conn,
                model_id=model_id,
                n_splits=n_splits,
                min_train_size=min_train_size,
                created_by=agent,
            )
            self._json_response(201, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_ensemble_predict(self, path: str, qs: dict) -> None:
        from ohm.queries import ensemble_predict
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        observation_window = int(qs.get("observation_window", [50])[0])

        try:
            result = ensemble_predict(
                self.current_store.read_conn,
                twin_id=twin_id,
                observation_window=observation_window,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _get_decision_value(self, path: str, qs: dict) -> None:
        from ohm.queries import compute_decision_value
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        decision_node_id = qs.get("decision_node_id", [None])[0]
        utility_scale_str = qs.get("utility_scale", [None])[0]
        if not decision_node_id:
            raise ValidationError("decision_node_id query parameter is required")
        if not utility_scale_str:
            raise ValidationError("utility_scale query parameter is required")

        utility_scale = float(utility_scale_str)

        try:
            result = compute_decision_value(
                self.current_store.read_conn,
                model_id=model_id,
                decision_node_id=decision_node_id,
                utility_scale=utility_scale,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_auto_retire_model(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.queries import auto_retire_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_id = parts[1] if len(parts) >= 2 else None
        if not model_id:
            raise ValidationError("model_id is required in path")

        reason = body.get("reason")
        if not reason:
            raise ValidationError("reason is required")

        try:
            result = auto_retire_model(
                self.current_store.conn,
                model_id=model_id,
                reason=reason,
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})

    def _post_set_promotion_policy(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /model/{id}/promotion-policy — set promotion policy on a model candidate (OHM-75tw)."""
        from ohm.queries import set_promotion_policy
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        model_candidate_id = parts[1] if len(parts) >= 2 else None
        if not model_candidate_id:
            raise ValidationError("model_candidate_id is required in path")

        policy = body.get("policy")
        if not policy:
            raise ValidationError("policy is required")

        try:
            result = set_promotion_policy(
                self.current_store.conn,
                model_candidate_id=model_candidate_id,
                policy=policy,
                decision_node_id=body.get("decision_node_id"),
                min_improvement=float(body.get("min_improvement", 0.0)),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _post_auto_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /twin/{id}/auto-promote — auto-promote the best model for a twin (OHM-75tw)."""
        from ohm.queries import auto_promote_best_model
        from ohm.exceptions import ValidationError, NodeNotFoundError

        parts = path.strip("/").split("/")
        twin_id = parts[1] if len(parts) >= 2 else None
        if not twin_id:
            raise ValidationError("twin_id is required in path")

        try:
            result = auto_promote_best_model(
                self.current_store.conn,
                twin_id=twin_id,
                decision_node_id=body.get("decision_node_id"),
                policy=body.get("policy", "decision_value"),
                min_improvement=float(body.get("min_improvement", 0.0)),
                created_by=agent,
            )
            self._json_response(200, {"ok": True, "data": result})
        except NodeNotFoundError as e:
            self._json_response(404, {"ok": False, "error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(422, {"ok": False, "error": "validation_error", "message": str(e)})

    def _route_model_get(self, path: str, qs: dict) -> None:
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("GET /model/{id}/{decision-value} required")
        action = parts[2]
        if action == "decision-value":
            self._get_decision_value(path, qs)
        else:
            raise ValidationError(f"unknown model GET action: {action}")
