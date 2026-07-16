"""Challenge and support handler mixin."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.handlers._base import OhmHandlerBase


class ChallengeSupportHandlerMixin(OhmHandlerBase):
    """Handler mixin for challenge and support handler mixin."""

    def _post_challenge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /challenge/{id} — challenge an existing edge.

        ADR-025: ``challenge_type`` in the request body is a semantic label
        (e.g. ``empirical``, ``logical``) stored in the ``challenge_type``
        column. The ``edge_type`` is always ``CHALLENGED_BY`` for this
        endpoint — use POST /support/{id} to create SUPPORTS edges.
        """
        edge_id = path[11:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.5)
        semantic_type = body.get("challenge_type", "CHALLENGED_BY")
        result = self.current_store.challenge_edge(
            edge_id,
            reason,
            confidence,
            "CHALLENGED_BY",
            agent_name=agent,
            challenge_type_column=semantic_type,
        )
        if result:
            # OHM-a5rz.15: reflect the challenge back to originating L0 fragments
            try:
                from ohm.graph.queries import reflect_challenge_to_fragments

                reflected = reflect_challenge_to_fragments(
                    self.current_store.conn,
                    edge_id,
                    result.get("id", ""),
                    agent,
                )
                if reflected:
                    result["backflow_fragments"] = [r["fragment_id"] for r in reflected]
            except Exception:
                pass  # backflow is advisory; never block the challenge
            _server_module._trigger_webhooks(
                {
                    "type": "edge.challenged",
                    "agent": agent,
                    "edge": result,
                    "challenge_type": semantic_type,
                },
                customer_id=self._customer_id,
            )
            # OHM-910: calibration nudges for challenges
            from ohm.server.nudges import enrich_response, generate_nudges

            nudges = generate_nudges(
                action="challenge",
                challenge_edge_id=edge_id,
                store=self.current_store,
                agent=agent,
            )
            result = enrich_response(result, nudges, store=self.current_store, agent=agent, action="challenge", target_id=edge_id)
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_support(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /support/{id} — support an existing edge."""
        edge_id = path[9:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.8)
        result = self.current_store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent, challenge_type_column="SUPPORTS")
        if result:
            _server_module._trigger_webhooks(
                {
                    "type": "edge.supported",
                    "agent": agent,
                    "edge": result,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

