"""Temporal-domain-tables Graph mixin (OHM-dh9l.1): plans, events, reports, runs."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class TemporalGraphMixin(GraphMixinBase):
    """Plans, events, reports, RUL assessments, runs, path/observation propagation."""

    def create_plan(
        self,
        plan_id: str,
        *,
        node_id: str | None = None,
        plan_type: str,
        label: str | None = None,
        start_ts: str | None = None,
        end_ts: str | None = None,
        horizon: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a plan (time-bounded grouping of events).

        Args:
            plan_id: Unique plan identifier.
            node_id: Optional primary node this plan is anchored to.
            plan_type: Plan category (e.g., 'maintenance_window', 'annual_outage').
            label: Optional human-readable label.
            start_ts: Optional ISO timestamp for plan start.
            end_ts: Optional ISO timestamp for plan end.
            horizon: Optional horizon tag (e.g., 'short', 'medium', 'long').
            status: Plan status (default 'active').
            metadata: Optional structured key-value data (JSON dict).

        Returns:
            The created plan record as a dict.
        """
        from ohm.queries import create_plan as _create_plan

        return _create_plan(
            self._conn,
            plan_id=plan_id,
            node_id=node_id,
            plan_type=plan_type,
            label=label,
            start_ts=start_ts,
            end_ts=end_ts,
            horizon=horizon,
            status=status,
            created_by=self.actor,
            metadata=metadata,
            **kwargs,
        )

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Retrieve a single plan by ID.

        Returns the full plan record or None if not found.
        """
        from ohm.queries import get_plan as _get_plan

        return _get_plan(self._conn, plan_id)

    def list_plans(
        self,
        *,
        node_id: str | None = None,
        plan_type: str | None = None,
        status: str | None = None,
        horizon: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """List plans with optional filters.

        Args:
            node_id: Filter by primary node.
            plan_type: Filter by plan type.
            status: Filter by status.
            horizon: Filter by horizon tag.

        Returns:
            List of plan dicts ordered by start_ts.
        """
        from ohm.queries import list_plans as _list_plans

        return _list_plans(
            self._conn,
            node_id=node_id,
            plan_type=plan_type,
            status=status,
            horizon=horizon,
            **kwargs,
        )

    def create_event(
        self,
        event_id: str,
        *,
        node_id: str,
        event_class: str,
        start_ts: str,
        plan_id: str | None = None,
        node_path: str | None = None,
        title: str | None = None,
        end_ts: str | None = None,
        horizon: str | None = None,
        operating_state: str | None = None,
        description: str | None = None,
        confidence: float | None = None,
        authority: str | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a temporal event.

        Args:
            event_id: Unique event identifier.
            node_id: Node this event is anchored to (required).
            event_class: Event category (e.g., 'shutdown', 'restart', 'outage').
            start_ts: ISO timestamp for event start (required).
            plan_id: Optional parent plan ID.
            node_path: Optional hierarchical node path.
            title: Optional human-readable title.
            end_ts: Optional ISO timestamp for event end.
            horizon: Optional horizon tag.
            operating_state: Optional operating state (e.g., 'low', 'medium', 'high').
            description: Optional free-text description.
            confidence: Optional confidence score in [0, 1].
            authority: Optional authority attribution.
            metadata: Optional structured key-value data (JSON dict).
            **kwargs: Additional JSON columns (source_refs, l3_context, flow_impact,
                forecast_basis, decision_metadata) or future schema columns.

        Returns:
            The created event record as a dict.
        """
        from ohm.queries import create_event as _create_event

        result = _create_event(
            self._conn,
            event_id=event_id,
            plan_id=plan_id,
            node_id=node_id,
            node_path=node_path,
            event_class=event_class,
            title=title,
            start_ts=start_ts,
            end_ts=end_ts,
            horizon=horizon,
            operating_state=operating_state,
            description=description,
            confidence=confidence,
            authority=authority,
            created_by=self.actor,
            metadata=metadata,
            **kwargs,
        )

        # Fire post_event_create hooks for propagation (OHM-vatf)
        try:
            from ohm.hooks import HookRunner

            runner = HookRunner(self._conn)
            runner.run_hooks("post_event_create", {"event": result})
        except Exception:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning("post_event_create hook failed for event %s (non-fatal)", event_id)

        return result

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Retrieve a single event by ID.

        Returns the full event record or None if not found.
        """
        from ohm.queries import get_event as _get_event

        return _get_event(self._conn, event_id)

    def get_events_for_node(
        self,
        node_id: str,
        *,
        horizon: str | None = None,
        plan_id: str | None = None,
        event_class: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch events for a node with optional filters.

        Args:
            node_id: Node to fetch events for.
            horizon: Optional horizon filter.
            plan_id: Optional plan filter.
            event_class: Optional event class filter.
            start_after: Optional ISO timestamp; only events starting at or after.
            end_before: Optional ISO timestamp; only events ending at or before.
            limit: Maximum number of results (default 100).

        Returns:
            List of event dicts ordered by start_ts ascending.
        """
        from ohm.queries import get_events_for_node as _get_events_for_node

        return _get_events_for_node(
            self._conn,
            node_id,
            horizon=horizon,
            plan_id=plan_id,
            event_class=event_class,
            start_after=start_after,
            end_before=end_before,
            limit=limit,
            **kwargs,
        )

    def get_events_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        """Fetch all events for a plan, ordered by start_ts.

        Args:
            plan_id: Plan ID to fetch events for.

        Returns:
            List of event dicts ordered by start_ts ascending.
        """
        from ohm.queries import get_events_for_plan as _get_events_for_plan

        return _get_events_for_plan(self._conn, plan_id)

    def create_event_link(
        self,
        link_id: str,
        *,
        from_event_id: str,
        to_event_id: str,
        edge_type: str,
        layer: str = "L1",
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a directed link between two events.

        Args:
            link_id: Unique link identifier.
            from_event_id: Source event ID.
            to_event_id: Target event ID.
            edge_type: Link type (e.g., 'caused_by', 'followed_by', 'overlaps').
            layer: Layer tag (default 'L1').
            confidence: Confidence score in [0, 1] (default 1.0).
            metadata: Optional structured key-value data (JSON dict).

        Returns:
            The created event link record as a dict.
        """
        from ohm.queries import create_event_link as _create_event_link

        return _create_event_link(
            self._conn,
            link_id=link_id,
            from_event_id=from_event_id,
            to_event_id=to_event_id,
            edge_type=edge_type,
            layer=layer,
            confidence=confidence,
            created_by=self.actor,
            metadata=metadata,
            **kwargs,
        )

    def get_event_links(
        self,
        *,
        event_id: str | None = None,
        from_event_id: str | None = None,
        to_event_id: str | None = None,
        edge_type: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Fetch event links with optional filters.

        Args:
            event_id: Filter to links where this event is either from or to.
            from_event_id: Filter by source event.
            to_event_id: Filter by target event.
            edge_type: Filter by link type.

        Returns:
            List of event link dicts ordered by created_at ascending.
        """
        from ohm.queries import get_event_links as _get_event_links

        return _get_event_links(
            self._conn,
            event_id=event_id,
            from_event_id=from_event_id,
            to_event_id=to_event_id,
            edge_type=edge_type,
            **kwargs,
        )

    def timeline_rollup(
        self,
        ancestor_node_id: str,
        *,
        horizon: str | None = None,
        start_after: str | None = None,
        end_before: str | None = None,
        event_class: str | None = None,
        plan_id: str | None = None,
        include_plans: bool = True,
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """Roll up temporal events from a subtree rooted at an ancestor.

        Traverses L1 CONTAINS edges downward from *ancestor_node_id* to collect
        all descendant nodes, then returns the matching events (and, by
        default, the plans that own them) filtered by horizon, date range,
        event class, or plan.

        Args:
            ancestor_node_id: Root of the L1 CONTAINS subtree to roll up.
            horizon: Optional horizon filter (HISTORICAL/CURRENT/PLANNED/FORECAST).
            start_after: Optional ISO timestamp; only events starting at or after.
            end_before: Optional ISO timestamp; only events ending at or before.
            event_class: Optional event class filter (e.g. 'shutdown', 'outage').
            plan_id: Optional plan filter; restricts events to one plan.
            include_plans: If True (default), include matching plans rows.
            max_depth: Maximum L1 traversal depth (default 10).

        Returns:
            Dict with ``ancestor``, ``events`` (list ordered by start_ts), and
            (when include_plans) ``plans`` (list of matching plan dicts).
        """
        from ohm.queries import timeline_rollup as _timeline_rollup

        return _timeline_rollup(
            self._conn,
            ancestor_node_id,
            horizon=horizon,
            start_after=start_after,
            end_before=end_before,
            event_class=event_class,
            plan_id=plan_id,
            include_plans=include_plans,
            max_depth=max_depth,
        )

    def create_report(
        self,
        report_id: str,
        *,
        report_type: str,
        node_id: str | None = None,
        plan_id: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        findings: dict[str, Any] | None = None,
        recommendations: dict[str, Any] | None = None,
        confidence_adjustments: dict[str, Any] | None = None,
        status: str = "draft",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create an analytical report artifact (OHM-o3rd).

        Args:
            report_id: Unique report identifier.
            report_type: Type of report (e.g., 'sensitivity_analysis', 'rca_report', 'correlation_study').
            node_id: Optional OHM node the report applies to.
            plan_id: Optional plan the report is associated with.
            title: Human-readable report title.
            summary: Short summary of findings.
            findings: Structured findings as a JSON dict.
            recommendations: Structured recommendations as a JSON dict.
            confidence_adjustments: Edge ID → new confidence mapping (applied on finalize).
            status: Report status (draft, finalized, superseded).
            metadata: Optional extensible metadata.

        Returns:
            The created report record as a dict.
        """
        from ohm.queries import create_report as _create_report

        return _create_report(
            self._conn,
            report_id=report_id,
            report_type=report_type,
            node_id=node_id,
            plan_id=plan_id,
            title=title,
            summary=summary,
            findings=findings,
            recommendations=recommendations,
            confidence_adjustments=confidence_adjustments,
            status=status,
            created_by=self.actor,
            metadata=metadata,
        )

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        """Fetch a single report by id (OHM-o3rd)."""
        from ohm.queries import get_report as _get_report

        return _get_report(self._conn, report_id)

    def list_reports(
        self,
        *,
        report_type: str | None = None,
        node_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List reports with optional filters (OHM-o3rd)."""
        from ohm.queries import list_reports as _list_reports

        return _list_reports(
            self._conn,
            report_type=report_type,
            node_id=node_id,
            plan_id=plan_id,
            status=status,
        )

    def finalize_report(
        self,
        report_id: str,
        *,
        confidence_adjustments: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Finalize a report and optionally apply L3 edge confidence
        adjustments (OHM-o3rd feedback loop).

        Args:
            report_id: The report to finalize.
            confidence_adjustments: Optional mapping of edge ID → new confidence.
                Each edge is updated in place when provided.

        Returns:
            The updated report record as a dict.
        """
        from ohm.queries import finalize_report as _finalize_report

        return _finalize_report(
            self._conn,
            report_id=report_id,
            confidence_adjustments=confidence_adjustments,
            created_by=self.actor,
        )

    def supersede_report(
        self,
        old_report_id: str,
        new_report_id: str,
    ) -> None:
        """Mark an old report as superseded by a newer version (OHM-o3rd)."""
        from ohm.queries import supersede_report as _supersede_report

        _supersede_report(
            self._conn,
            old_report_id=old_report_id,
            new_report_id=new_report_id,
            created_by=self.actor,
        )

    def register_rul_assessment(
        self,
        equipment_node_id: str,
        *,
        rul_days: float,
        risk_class: str,
        model_version: str | None = None,
        site_id: str | None = None,
        node_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a RUL assessment and link it via L4 PREDICTS edge (OHM-q4ku).

        Args:
            equipment_node_id: OHM node id for the equipment being assessed.
            rul_days: Remaining useful life in days.
            risk_class: Risk classification (critical/high/medium/low).
            model_version: Optional model version string.
            site_id: Optional site identifier.
            node_path: Optional UNS address path (stored in metadata).
            metadata: Optional extensible metadata.

        Returns:
            Dict with 'prospect' (the stored assessment) and 'edge_id' (or None).
        """
        from ohm.queries import register_rul_assessment as _register

        return _register(
            self._conn,
            equipment_node_id=equipment_node_id,
            rul_days=rul_days,
            risk_class=risk_class,
            model_version=model_version,
            site_id=site_id,
            node_path=node_path,
            metadata=metadata,
            created_by=self.actor,
        )

    def get_rul_assessments(
        self,
        *,
        equipment_node_id: str | None = None,
        risk_class: str | None = None,
        site_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch RUL assessments with optional filters (OHM-q4ku)."""
        from ohm.queries import get_rul_assessments as _get

        return _get(
            self._conn,
            equipment_node_id=equipment_node_id,
            risk_class=risk_class,
            site_id=site_id,
            limit=limit,
        )

    def create_run(
        self,
        run_id: str,
        *,
        run_type: str,
        report_id: str | None = None,
        node_id: str | None = None,
        inputs: dict[str, Any] | None = None,
        status: str = "pending",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a DataProductRun execution record (OHM-08uk)."""
        from ohm.queries import create_run as _create_run

        return _create_run(
            self._conn,
            run_id=run_id,
            report_id=report_id,
            node_id=node_id,
            run_type=run_type,
            inputs=inputs,
            status=status,
            created_by=self.actor,
            metadata=metadata,
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a single run by id (OHM-08uk)."""
        from ohm.queries import get_run as _get_run

        return _get_run(self._conn, run_id)

    def list_runs(
        self,
        *,
        report_id: str | None = None,
        node_id: str | None = None,
        run_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List runs with optional filters (OHM-08uk)."""
        from ohm.queries import list_runs as _list_runs

        return _list_runs(
            self._conn,
            report_id=report_id,
            node_id=node_id,
            run_type=run_type,
            status=status,
        )

    def complete_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        outputs: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        """Mark a run as completed/failed and record outputs (OHM-08uk)."""
        from ohm.queries import complete_run as _complete_run

        return _complete_run(
            self._conn,
            run_id=run_id,
            status=status,
            outputs=outputs,
            error=error,
            duration_ms=duration_ms,
            created_by=self.actor,
        )

    def set_node_path(self, node_id: str, node_path: str) -> dict[str, Any]:
        """Set the UNS hierarchical path on a node (OHM-ivlt).

        Args:
            node_id: The node to update.
            node_path: UNS path string (e.g., 'pns.fm10.main_drive').

        Returns:
            The updated node record as a dict.
        """
        from ohm.queries import set_node_path as _set

        return _set(self._conn, node_id=node_id, node_path=node_path, created_by=self.actor)

    def get_nodes_by_path_prefix(self, path_prefix: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Find nodes whose node_path starts with a prefix (OHM-ivlt).

        Args:
            path_prefix: Path prefix to match (e.g., 'pns.fm10').
            limit: Maximum results.

        Returns:
            List of node dicts ordered by node_path.
        """
        from ohm.queries import get_nodes_by_path_prefix as _get

        return _get(self._conn, path_prefix, limit=limit)

    def propagate_observation(
        self,
        source_node_id: str,
        *,
        observation_weight: float = 1.0,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        max_depth: int = 10,
        edge_types: tuple[str, ...] | None = None,
        layers: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Propagate a Bayesian observation downstream through the causal graph.

        Walks the L3 causal graph from *source_node_id* and updates each
        reachable node's belief using a conjugate Beta-Binomial update.

        Args:
            source_node_id: Node where the observation originates.
            observation_weight: Strength of the observation in [0, 1]
                (default 1.0).
            prior_alpha: Alpha parameter of the Beta prior (default 1.0).
            prior_beta: Beta parameter of the Beta prior (default 1.0).
            max_depth: Maximum traversal depth (default 10).
            edge_types: Causal edge types to traverse. Defaults to
                ('CAUSES', 'DEPENDS_ON', 'THREATENS', 'EXPECTED_LIKELIHOOD').
            layers: Layer filter (e.g., ('L3', 'L4')). None = all layers.

        Returns:
            List of dicts with node_id, posterior_alpha/beta, posterior_mean,
            accumulated_weight, depth, path.
        """
        from ohm.queries import propagate_observation as _propagate

        return _propagate(
            self._conn,
            source_node_id,
            observation_weight=observation_weight,
            prior_alpha=prior_alpha,
            prior_beta=prior_beta,
            max_depth=max_depth,
            edge_types=edge_types,
            layers=layers,
        )
