"""Narrative handler mixin."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class NarrativeHandlerMixin(OhmHandlerBase):
    """Handler mixin for narrative handler mixin."""

    def _get_narrative(self, path: str, qs: dict) -> None:
        """GET /narrative/{node_id}?agent=NAME — neighborhood narrative (OHM-q9rt.1).

        Returns a contextualized explanation of WHY an agent should care about
        a node, including reasoning chains, evidence, and a human-readable
        connections summary.
        """
        from ohm.queries import query_neighborhood_narrative

        prefix = "/narrative/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid narrative path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        agent = qs.get("agent", [None])[0]
        if not agent:
            agent = getattr(self, "_current_agent", None)
            if agent and agent == "ohm":
                agent = None

        depth = int(qs.get("depth", [2])[0])

        # OHM-737: enforce read scope on the seed node before traversal
        from ohm.server.boundary import enforce_read_scope

        scope_agent = getattr(self, "_current_agent", "ohm")
        node = self.current_store.get_node(node_id)
        if node:
            enforce_read_scope(
                self.current_store.conn,
                scope_agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
        result = query_neighborhood_narrative(
            self.current_store.read_conn,
            node_id,
            agent_name=agent,
            depth=depth,
        )
        self._json_response(200, result)

    def _get_lineage(self, path: str, qs: dict) -> None:
        """GET /lineage/{node_id} — claim lineage (OHM-q9rt.2).

        Explodes a synthesis/pattern/decision node into its supporting
        evidence chain: tree of supporting nodes with observations, source
        leaves, confidence products, and gap detection.
        """
        from ohm.queries import query_claim_lineage

        prefix = "/lineage/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid lineage path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        max_depth = int(qs.get("depth", [10])[0])

        # OHM-737: enforce read scope on the seed node before traversal
        from ohm.server.boundary import enforce_read_scope

        scope_agent = getattr(self, "_current_agent", "ohm")
        node = self.current_store.get_node(node_id)
        if node:
            enforce_read_scope(
                self.current_store.conn,
                scope_agent,
                node_id=node_id,
                source_tier=node.get("source_tier"),
                created_by=node.get("created_by"),
            )
        result = query_claim_lineage(
            self.current_store.read_conn,
            node_id,
            max_depth=max_depth,
        )
        self._json_response(200, result)

    def _get_contradiction_summary(self, path: str, qs: dict) -> None:
        """GET /contradiction/{node_id} — contradiction summary (OHM-q9rt.3).

        Returns a structured "both sides" view of contradictions involving
        a node: groups of conflicting observations, their agents, effective
        confidence (with decay), existing challenges, and a recommendation.
        """
        from ohm.queries import query_contradiction_summary

        prefix = "/contradiction/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid contradiction path")
        node_id = path[len(prefix) :]
        if not node_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing node id")

        result = query_contradiction_summary(
            self.current_store.read_conn,
            node_id,
        )
        self._json_response(200, result)

    def _get_confidence_report(self, path: str, qs: dict) -> None:
        """GET /confidence-report?agent=NAME&since=ISO8601 — confidence report (OHM-q9rt.5).

        Returns a per-agent report showing which of their edges had confidence
        changes since a timestamp, with the reason for each shift.
        """
        from ohm.queries import query_confidence_report
        from ohm.exceptions import ValidationError

        agent = qs.get("agent", [None])[0]
        if not agent:
            agent = getattr(self, "_current_agent", None)
            if not agent or agent == "ohm":
                raise ValidationError("agent parameter is required")

        since = qs.get("since", [None])[0]

        result = query_confidence_report(
            self.current_store.read_conn,
            agent_name=agent,
            since=since,
        )
        self._json_response(200, result)

