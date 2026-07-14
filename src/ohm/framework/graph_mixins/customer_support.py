"""Customer-support Graph mixin: handoff, escalation, provenance."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class CustomerSupportGraphMixin(GraphMixinBase):
    """Handoff, escalation, and provenance workflow methods."""

    def handoff(
        self,
        *,
        from_agent: str,
        to_agent: str,
        ticket_node: str,
        reason: str,
        edge_type: str = "TRANSFERRED_TO",
        confidence: float = 0.8,
    ) -> dict[str, Any]:
        """Transfer a ticket between agents with full context tracking.

        Creates a TRANSFERRED_TO (default), ESCALATED_TO, or DELEGATED_TO
        edge from the from_agent to the to_agent, and returns the full
        handoff chain for the ticket.

        Example:
            g.handoff(from_agent=agent_a, to_agent=agent_b,
                      ticket_node=ticket, reason="Customer needs specialist")
            -> {edge: {...}, handoff_chain: [...]}

        Args:
            from_agent: Agent node ID transferring from.
            to_agent: Agent node ID transferring to.
            ticket_node: The ticket/case node being handed off.
            reason: Reason for the handoff.
            edge_type: TRANSFERRED_TO, ESCALATED_TO, or DELEGATED_TO.
            confidence: Confidence for the edge (default 0.8).

        Returns:
            Dict with the created edge and the full handoff chain.
        """
        from ohm.queries import query_handoff

        return query_handoff(
            self._conn,
            from_agent=from_agent,
            to_agent=to_agent,
            ticket_node=ticket_node,
            reason=reason,
            edge_type=edge_type,
            confidence=confidence,
            created_by=self.actor,
        )

    def escalate(
        self,
        *,
        ticket_node: str,
        to_tier: str,
        reason: str,
        from_agent: str | None = None,
        confidence: float = 0.9,
    ) -> dict[str, Any]:
        """Escalate a ticket to a higher tier with urgency.

        Creates an ESCALATED_TO edge and sets the ticket's urgency to 'high'.
        Returns the escalation edge and the updated ticket info.

        Example:
            g.escalate(ticket_node=ticket, to_tier=tier2,
                       reason="SLA breach imminent")
            -> {edge: {...}, ticket: {urgency: 'high', ...}}

        Args:
            ticket_node: The ticket/case node being escalated.
            to_tier: Agent node ID or tier identifier to escalate to.
            reason: Reason for the escalation.
            from_agent: Agent node ID escalating from (optional).
            confidence: Confidence for the edge (default 0.9).

        Returns:
            Dict with the created edge and updated ticket info.
        """
        from ohm.queries import query_escalate

        return query_escalate(
            self._conn,
            ticket_node=ticket_node,
            to_tier=to_tier,
            reason=reason,
            from_agent=from_agent,
            confidence=confidence,
            created_by=self.actor,
        )

    def ticket_provenance(
        self,
        ticket_node: str,
        *,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Show the complete handoff and state history for a ticket.

        Follows TRANSFERRED_TO, ESCALATED_TO, DELEGATED_TO edges and
        state machine edges (OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY,
        CLOSED_BY) to reconstruct the full provenance chain.

        Example:
            g.ticket_provenance(ticket_node=ticket)
            -> [{edge_type: 'OPENED_BY', from_label: 'agent_a', ...},
               {edge_type: 'TRANSFERRED_TO', from_label: 'agent_a', ...}]

        Args:
            ticket_node: The ticket/case node.
            max_depth: Maximum traversal depth.

        Returns:
            List of provenance records ordered chronologically.
        """
        from ohm.queries import query_ticket_provenance

        return query_ticket_provenance(
            self._conn,
            ticket_node,
            max_depth=max_depth,
        )

    def delete_node(self, node_id: str) -> dict[str, Any]:
        """Delete a node and all its associated edges and observations.

        Removes all edges referencing the node (both as source and target),
        all observations on the node, then the node itself.

        Args:
            node_id: The node to delete.

        Returns:
            Dict with deleted node_id, type, and counts of removed edges/observations.

        Raises:
            NodeNotFoundError: If the node doesn't exist.
        """
        from ohm.queries import delete_node

        return delete_node(self._conn, node_id=node_id, deleted_by=self.actor)

    def delete_edge(self, edge_id: str) -> dict[str, Any]:
        """Delete an edge by ID.

        Also removes any observations referencing the edge.

        Args:
            edge_id: The edge to delete.

        Returns:
            Dict with deleted edge_id and type.

        Raises:
            EdgeNotFoundError: If the edge doesn't exist.
        """
        from ohm.queries import delete_edge

        return delete_edge(self._conn, edge_id=edge_id, deleted_by=self.actor)

    def start_twin_design_session(
        self,
        goal: str,
        *,
        context: dict[str, Any] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import start_twin_design_session

        return start_twin_design_session(
            self._conn,
            goal=goal,
            context=context,
            created_by=self.actor,
            label=label,
        )

    def transition_session(
        self,
        session_id: str,
        *,
        to_state: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import transition_session

        return transition_session(
            self._conn,
            session_id=session_id,
            to_state=to_state,
            notes=notes,
            created_by=self.actor,
        )

    def add_session_observation(
        self,
        session_id: str,
        *,
        observations: dict[str, Any],
    ) -> dict[str, Any]:
        from ohm.queries import add_session_observation

        return add_session_observation(
            self._conn,
            session_id=session_id,
            observations=observations,
            created_by=self.actor,
        )

    def propose_twin_config(
        self,
        session_id: str,
        *,
        decision_node_id: str | None = None,
        preferred_template_id: str | None = None,
        preferred_model_id: str | None = None,
        confidence_threshold: float = 0.6,
    ) -> dict[str, Any]:
        from ohm.queries import propose_twin_config

        return propose_twin_config(
            self._conn,
            session_id=session_id,
            decision_node_id=decision_node_id,
            preferred_template_id=preferred_template_id,
            preferred_model_id=preferred_model_id,
            confidence_threshold=confidence_threshold,
            created_by=self.actor,
        )

    def review_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        decision: str,
        approved_aspects: list[str] | None = None,
        declined_aspects: list[str] | None = None,
        modifications: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import review_proposal

        return review_proposal(
            self._conn,
            session_id=session_id,
            proposal_id=proposal_id,
            decision=decision,
            approved_aspects=approved_aspects,
            declined_aspects=declined_aspects,
            modifications=modifications,
            reason=reason,
            created_by=self.actor,
        )

    def instantiate_from_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import instantiate_from_session

        return instantiate_from_session(
            self._conn,
            session_id=session_id,
            created_by=self.actor,
        )

    def create_skill(
        self,
        label: str,
        *,
        trigger: str,
        scope: str = "personal",
        required_tools: list[str] | None = None,
        boundaries: str | None = None,
        output_format: str | None = None,
        verification_evidence: list[str] | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a portable skill node (OHM-461f)."""
        from ohm.queries import create_skill

        return create_skill(
            self._conn,
            label=label,
            trigger=trigger,
            scope=scope,
            required_tools=required_tools,
            boundaries=boundaries,
            output_format=output_format,
            verification_evidence=verification_evidence,
            connects_to=connects_to,
            created_by=self.actor,
        )

    def create_runbook(
        self,
        label: str,
        *,
        skill_ids: list[str],
        description: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a runbook with ordered DEPENDS_ON chain of skills (OHM-461f)."""
        from ohm.queries import create_runbook

        return create_runbook(
            self._conn,
            label=label,
            skill_ids=skill_ids,
            description=description,
            connects_to=connects_to,
            created_by=self.actor,
        )

    def get_runbook_steps(self, runbook_id: str) -> dict[str, Any]:
        """Get the ordered skill chain for a runbook (OHM-461f)."""
        from ohm.queries import get_runbook_steps

        return get_runbook_steps(self._conn, runbook_id=runbook_id)

    def record_calibration(
        self,
        session_id: str,
        *,
        observations: dict[str, float],
        actuals: dict[str, float],
    ) -> dict[str, Any]:
        from ohm.queries import record_calibration

        return record_calibration(
            self._conn,
            session_id=session_id,
            observations=observations,
            actuals=actuals,
            created_by=self.actor,
        )

    def evolve_session(
        self,
        session_id: str,
        *,
        reason: str,
        proposed_changes: dict[str, Any],
    ) -> dict[str, Any]:
        from ohm.queries import evolve_session

        return evolve_session(
            self._conn,
            session_id=session_id,
            reason=reason,
            proposed_changes=proposed_changes,
            created_by=self.actor,
        )

    def get_session_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import get_session_state

        return get_session_state(self._conn, session_id=session_id)

    def get_session_audit(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import get_session_audit

        return get_session_audit(self._conn, session_id=session_id)

    def detect_verifiable_claims(
        self,
        *,
        agent: str | None = None,
        days_threshold: int = 14,
        confidence_threshold: float = 0.85,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Detect verifiable dated claims past their expected date with no outcome.

        Scans CAUSES, PREDICTS, EXPECTS, EXPECTS_FROM edges whose metadata
        contains an 'expected_by' or 'window_end' date that is now past,
        and for which no outcome has been recorded.

        Args:
            agent: If set, only scan edges created_by this agent.
            days_threshold: Minimum age in days for edges (default 14).
            confidence_threshold: Minimum confidence to flag (default 0.85).
            limit: Maximum number of results (default 100).

        Returns:
            List of dicts with edge info, claim node info, and expected_by date.
        """
        from ohm.queries import detect_verifiable_claims as _detect

        return _detect(
            self._conn,
            agent=agent,
            days_threshold=days_threshold,
            confidence_threshold=confidence_threshold,
            limit=limit,
        )

    def create_verification_nudge(
        self,
        *,
        edge_id: str,
        confidence: float = 0.5,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Create a NUDGES_FOR_VERIFICATION edge prompting verification of a claim.

        Creates a task node and links it to the claim node via a
        NUDGES_FOR_VERIFICATION edge in L3. Idempotent: returns existing
        nudge if one already exists for the edge.

        Args:
            edge_id: The edge whose claim needs verification.
            confidence: Confidence for the nudge edge (default 0.5).
            reason: Optional reason for the nudge.

        Returns:
            Dict with the created nudge task node and nudge edge.
        """
        from ohm.queries import create_verification_nudge as _create

        return _create(
            self._conn,
            edge_id=edge_id,
            created_by=self.actor,
            confidence=confidence,
            reason=reason,
        )

    def record_verification_outcome(
        self,
        *,
        edge_id: str,
        outcome: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record a verification outcome for a verifiable claim edge.

        Outcome mappings: "true" -> confidence=1.0, "false" -> 0.0,
        "ambiguous" -> 0.5, "deferred" -> metadata only.

        Also resolves any NUDGES_FOR_VERIFICATION edges linked to this edge.

        Args:
            edge_id: The edge being verified.
            outcome: One of "true", "false", "ambiguous", "deferred".
            reason: Optional context about the outcome.

        Returns:
            Dict with the outcome record and any nudge resolution info.
        """
        from ohm.queries import record_verification_outcome as _record

        return _record(
            self._conn,
            edge_id=edge_id,
            outcome=outcome,
            recorded_by=self.actor,
            reason=reason,
        )

    def list_pending_verifications(
        self,
        *,
        agent: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List pending NUDGES_FOR_VERIFICATION edges that haven't been resolved.

        Args:
            agent: If set, only list nudges created_by this agent.
            limit: Maximum number of results (default 100).

        Returns:
            List of dicts with nudge edge and associated claim node info.
        """
        from ohm.queries import list_pending_verifications as _list

        return _list(
            self._conn,
            agent=agent,
            limit=limit,
        )
