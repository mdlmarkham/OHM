"""Decision handler mixin — GET /decision/{id}/recommendation."""

from __future__ import annotations


class DecisionHandlerMixin:
    """Handler mixin for decision-node recommendation endpoints."""

    def _get_decision_recommendation(self, path: str, qs: dict) -> None:
        """GET /decision/{id}/recommendation — decision recommendation."""
        from ohm.validation import validate_identifier
        from ohm.decision import evaluate_decision

        # path is like /decision/decision-xxx/recommendation
        prefix = "/decision/"
        suffix = "/recommendation"
        if not path.startswith(prefix) or not path.endswith(suffix):
            self._json_response(400, {"error": "Invalid decision recommendation URL"})
            return
        decision_id = path[len(prefix) : -len(suffix)]
        decision_id = validate_identifier(decision_id, name="decision_id")
        result = evaluate_decision(self.current_store.conn, decision_id)
        self._json_response(200, result)
