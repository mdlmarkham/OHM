"""Prospect handler mixin."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class ProspectHandlerMixin(OhmHandlerBase):
    """Handler mixin for prospect create/transition/list/detail endpoints."""

    def _post_prospect(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /prospect — create a prospect node (OHM-844).

        Body: {label, authority?, parent_scenario_id?, planned_start?,
               planned_end?, horizon_label?, tags?, content?, connects_to?}
        """
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.prospects import create_prospect

        label = body.get("label")
        if not label:
            raise ValidationError("label is required")

        result = create_prospect(
            self.current_store.conn,
            label=label,
            created_by=agent,
            authority=body.get("authority"),
            parent_scenario_id=body.get("parent_scenario_id"),
            planned_start=body.get("planned_start"),
            planned_end=body.get("planned_end"),
            horizon_label=body.get("horizon_label"),
            tags=body.get("tags"),
            content=body.get("content"),
            connects_to=body.get("connects_to"),
            confidence=body.get("confidence", 1.0),
        )
        self._json_response(201, result)

    def _post_prospect_transition(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /prospect/{id}/transition — transition prospect lifecycle (OHM-844).

        Body: {new_status, reason?}
        """
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.prospects import transition_prospect

        prospect_id = path.rstrip("/").split("/")[-1]
        new_status = body.get("new_status")
        if not new_status:
            raise ValidationError("new_status is required")

        try:
            result = transition_prospect(
                self.current_store.conn,
                prospect_id=prospect_id,
                new_status=new_status,
                agent=agent,
                reason=body.get("reason"),
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "invalid_transition", "message": str(e)})
        except PermissionError as e:
            self._json_response(403, {"error": "authority_mismatch", "message": str(e)})

    def _get_prospects(self, path: str, qs: dict) -> None:
        """GET /prospects — list prospects with optional filters (OHM-844).

        Query params: ?status=, ?tags= (multiple), ?created_by=, ?limit=
        """
        from ohm.graph.queries.prospects import list_prospects

        status = qs.get("status", [None])[0]
        tags = qs.get("tags", [])
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [20])[0])

        results = list_prospects(
            self.current_store.read_conn,
            status=status,
            tags=tags or None,
            created_by=created_by,
            limit=limit,
        )
        self._json_response(200, {"results": results, "count": len(results)})

    def _get_prospect_detail(self, path: str, qs: dict) -> None:
        """GET /prospect/{id} — prospect detail with children and observations (OHM-844)."""
        from ohm.graph.queries.prospects import prospect_detail

        prospect_id = path.rstrip("/").split("/")[-1]

        try:
            result = prospect_detail(
                self.current_store.read_conn,
                prospect_id=prospect_id,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})
