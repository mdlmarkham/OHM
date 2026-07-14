"""Type-proposal handler mixin (OHM-846)."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class TypeProposalHandlerMixin(OhmHandlerBase):
    """Handler mixin for type-proposal evaluate/promote/demote endpoints (OHM-846)."""

    def _post_type_proposal_evaluate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/evaluate — evaluate a type proposal (OHM-846)."""
        from ohm.graph.queries.type_proposals import evaluate_type_proposal

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "evaluate":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = evaluate_type_proposal(
                self.current_store.conn,
                proposal_id=proposal_id,
                min_distinct_agents=int(body.get("min_distinct_agents", 2)),
                min_evidence_nodes=int(body.get("min_evidence_nodes", 3)),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "evaluation_failed", "message": str(e)})

    def _post_type_proposal_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/promote — promote a type to canonical schema (OHM-846)."""
        from ohm.graph.queries.type_proposals import promote_type

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "promote":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = promote_type(
                self.current_store.conn,
                proposal_id=proposal_id,
                agent=agent,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "promotion_failed", "message": str(e)})

    def _post_type_proposal_demote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /type-proposal/{id}/demote — reject/demote a type proposal (OHM-846)."""
        from ohm.graph.queries.type_proposals import demote_type

        proposal_id = path.rstrip("/").split("/")[-1]
        if proposal_id == "demote":
            proposal_id = path.rstrip("/").split("/")[-2]

        try:
            result = demote_type(
                self.current_store.conn,
                proposal_id=proposal_id,
                agent=agent,
                reason=body.get("reason"),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "demotion_failed", "message": str(e)})

    def _route_type_proposal_post(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """Dispatch /type-proposal/{id}/{evaluate|promote|demote} to the right handler (OHM-846)."""
        from ohm.exceptions import ValidationError

        parts = path.strip("/").split("/")
        if len(parts) < 3:
            raise ValidationError("POST /type-proposal/{id}/{evaluate|promote|demote} required")
        action = parts[2]
        if action == "evaluate":
            self._post_type_proposal_evaluate(path, qs, body, agent)
        elif action == "promote":
            self._post_type_proposal_promote(path, qs, body, agent)
        elif action == "demote":
            self._post_type_proposal_demote(path, qs, body, agent)
        else:
            raise ValidationError(f"unknown type-proposal action: {action}")

    def _get_type_proposals(self, path: str, qs: dict) -> None:
        """GET /type-proposals — list type proposals (OHM-846)."""
        from ohm.graph.queries.type_proposals import list_type_proposals

        status = qs.get("status", [None])[0]
        results = list_type_proposals(
            self.current_store.read_conn,
            status=status,
        )
        self._json_response(200, {"results": results, "count": len(results)})
