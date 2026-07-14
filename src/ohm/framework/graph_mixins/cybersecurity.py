"""Cybersecurity Graph mixin: threat clustering, source reliability, neighborhood."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class CybersecurityGraphMixin(GraphMixinBase):
    """Threat clustering, source-trust/reliability scoring, neighborhood traversal."""

    def record_outcome(
        self,
        *,
        source_agent: str,
        claim_node: str,
        outcome: bool,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Record whether a source agent's claim was correct or incorrect.

        Stores an outcome observation on the claim node. Use this to build
        a reliability history for each source, enabling source_reliability()
        to compute P(accurate) and false_positive_rate.

        Example:
            g.record_outcome(source_agent=edr_node, claim_node=alert_node, outcome=False)
            # EDR was wrong about this alert (false positive)

            g.record_outcome(source_agent=siem_node, claim_node=alert_node, outcome=True)
            # SIEM was correct about this alert

        Args:
            source_agent: Agent node ID that made the claim.
            claim_node: Node ID of the claim being evaluated.
            outcome: True if the claim was correct, False if incorrect.
            notes: Optional context about the outcome.

        Returns:
            Dict with source_agent, claim_node, outcome, and recorded_by.
        """
        from ohm.queries import query_record_outcome

        return query_record_outcome(
            self._conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=outcome,
            recorded_by=self.actor,
            notes=notes,
        )

    def source_reliability(
        self,
        source_agent: str,
    ) -> dict[str, Any]:
        """Compute source reliability metrics from historical outcomes.

        Returns P(accurate), false_positive_rate, and outcome counts for the
        given source agent. Sources with high false_positive_rate should be
        downweighted in composite scores.

        Example:
            g.source_reliability(edr_node)
            → {p_accurate: 0.7, false_positive_rate: 0.3, total_outcomes: 100, ...}

        Args:
            source_agent: Agent node ID to evaluate.

        Returns:
            Dict with P(accurate), false_positive_rate, total_outcomes,
            accurate_count, false_positive_count.
        """
        from ohm.queries import query_source_reliability

        return query_source_reliability(self._conn, source_agent)

    def task_complete(
        self,
        task_node_id_or_label: str,
        *,
        completion_confidence: float = 1.0,
        derived_pattern_ids: list[str] | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Record task completion and link to any discovered patterns.

        When an agent marks a task as done, this automatically:
        1. Writes an observation on the task node with the completion confidence
        2. Creates DERIVES_FROM edges from the task to any pattern nodes discovered

        Args:
            task_node_id_or_label: Node ID or label of the completed task.
            completion_confidence: How confident we are in the task result (0-1).
            derived_pattern_ids: Optional list of pattern node IDs discovered during this task.
                DERIVES_FROM edges will be created from task -> pattern.
            notes: Optional notes about the completion.

        Returns:
            Dict with task_id, observation_id, and any derived edges created.
        """
        task_id = self._resolve_label_or_id(task_node_id_or_label, create_if_missing=False)

        result = {
            "task_id": task_id,
            "observation_id": None,
            "derived_edges_created": 0,
        }

        obs = self.observe(
            task_id,
            obs_type="task_completion",
            value=completion_confidence,
            sigma=0.1 * (1.0 - completion_confidence),
            notes=notes or f"Task completed by {self.actor}",
        )
        result["observation_id"] = obs.get("id")

        if derived_pattern_ids:
            from ohm.queries import create_edge

            for pattern_id in derived_pattern_ids:
                try:
                    create_edge(
                        self._conn,
                        from_node=task_id,
                        to_node=pattern_id,
                        edge_type="DERIVES_FROM",
                        layer="L2",
                        created_by=self.actor,
                        confidence=completion_confidence,
                    )
                    result["derived_edges_created"] += 1
                except Exception:
                    continue

        return result

    def complete_task_with_outcome(
        self,
        task_node_id_or_label: str,
        outcome: str,
        *,
        notes: str | None = None,
        claim_node: str | None = None,
    ) -> dict[str, Any]:
        """Close a task with a recorded outcome against its expected_claim (OHM-f5iq).

        Wraps :func:`ohm.graph.queries.query_close_task_with_outcome`. Sets
        ``task_status='done'``, stores the outcome on the task node, and
        records an ``ohm_outcomes`` row against the claim (using the task's
        ``created_by`` as the source agent being evaluated).

        Args:
            task_node_id_or_label: Task node id or label.
            outcome: ``TRUE``, ``FALSE``, or ``AMBIGUOUS``.
            notes: Optional justification.
            claim_node: Optional explicit claim node id (defaults to the
                task's ``expected_claim`` column).

        Returns:
            Dict with ``task``, ``outcome``, and ``outcome_record``.
        """
        from ohm.graph.queries import query_close_task_with_outcome

        task_id = self._resolve_label_or_id(task_node_id_or_label, create_if_missing=False)
        return query_close_task_with_outcome(
            self._conn,
            task_id=task_id,
            outcome=outcome,
            recorded_by=self.actor,
            notes=notes,
            claim_node=claim_node,
        )

    def neighborhood(
        self,
        node_id: str,
        *,
        depth: int = 3,
        layer: str | None = None,
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        """Bounded-depth traversal from a node."""
        from ohm.queries import query_neighborhood

        return query_neighborhood(
            self._conn,
            node_id,
            depth=depth,
            layer=layer,
            direction=direction,
        )

    def narrative(
        self,
        node_id: str,
        *,
        depth: int = 2,
    ) -> dict[str, Any]:
        """Neighborhood narrative — "You care about X because of Y and Z" (OHM-q9rt.1).

        Returns a contextualized explanation of why this node matters, including
        reasoning chains (edge paths from connected nodes), evidence (observations),
        and a human-readable connections summary.

        Args:
            node_id: Target node to narrate.
            depth: How many hops to walk (default 2).

        Returns:
            Dict with node, why_it_matters (list of reasoning chains),
            evidence (list of observations), connections_summary (str),
            connection_count, evidence_count, and agent_context.
        """
        from ohm.queries import query_neighborhood_narrative

        return query_neighborhood_narrative(
            self._conn,
            node_id,
            agent_name=self.actor,
            depth=depth,
        )

    def lineage(
        self,
        node_id: str,
        *,
        max_depth: int = 10,
    ) -> dict[str, Any]:
        """Claim lineage — explode a synthesis into supporting evidence (OHM-q9rt.2).

        Traces backward through provenance edges (DERIVES_FROM, REFERENCES,
        SUPPORTS, etc.) to find all supporting observations and source nodes.
        Returns a tree with confidence products, gap detection, and source leaves.

        Args:
            node_id: The claim/synthesis/pattern node to trace from.
            max_depth: Maximum chain depth (default 10).

        Returns:
            Dict with claim, lineage (tree), sources (leaves), gaps (weak links),
            max_confidence, min_confidence, chain_depth, total_nodes/sources/gaps.
        """
        from ohm.queries import query_claim_lineage

        return query_claim_lineage(
            self._conn,
            node_id,
            max_depth=max_depth,
        )

    def contradiction_summary(self, node_id: str) -> dict[str, Any]:
        """Contradiction summary — "these two observations disagree" (OHM-q9rt.3).

        Returns a structured "both sides" view of contradictions involving a
        node: groups of conflicting observations, their agents, effective
        confidence (with decay), existing challenges, and a recommendation.

        Args:
            node_id: The node to analyze for contradictions.

        Returns:
            Dict with node, sides (list of conflicting groups), challenges,
            recommendation, has_contradiction (bool), totals.
        """
        from ohm.queries import query_contradiction_summary

        return query_contradiction_summary(self._conn, node_id)

    def task_context(self, task_id: str) -> dict[str, Any]:
        """Task context binding — task + subgraph + rationale (OHM-q9rt.4).

        Returns a task node bundled with its 2-hop subgraph, rationale chain
        (decisions/observations that led to it), expected outcome, and any
        blocking tasks.

        Args:
            task_id: The task node ID.

        Returns:
            Dict with task, subgraph (nodes + edges), rationale,
            expected_outcome, blocking, blocked_by_count.
        """
        from ohm.queries import query_task_context

        return query_task_context(self._conn, task_id)

    def confidence_report(self, *, since: str | None = None) -> dict[str, Any]:
        """Per-agent confidence report — which beliefs have shifted (OHM-q9rt.5).

        Shows which of this agent's edges had confidence changes since a
        timestamp, with the reason for each shift. Complements changes() by
        showing what CHANGED in the agent's existing portfolio.

        Args:
            since: ISO 8601 timestamp. Falls back to last_sync then 30d ago.

        Returns:
            Dict with agent, since, shifted_beliefs (with reason), new_beliefs,
            stale_beliefs, and summary counts.
        """
        from ohm.queries import query_confidence_report

        return query_confidence_report(self._conn, agent_name=self.actor, since=since)

    def scenario(
        self,
        node_id: str,
        *,
        failure_probability: float = 1.0,
        max_depth: int = 10,
        edge_overrides: dict[str, float] | None = None,
        node_interventions: dict[str, float] | None = None,
        disabled_edges: set[str] | None = None,
        disabled_nodes: set[str] | None = None,
        compare: bool = True,
    ) -> dict[str, Any]:
        """Counterfactual scenario analysis (OHM-xagx).

        Runs a counterfactual cascade with edge overrides, node interventions,
        and disabled edges/nodes — without modifying the live graph. When
        ``compare=True`` (default), also runs the baseline and returns a
        delta comparison.

        Args:
            node_id: Starting node for the cascade.
            failure_probability: Initial failure probability (0.0-1.0).
            max_depth: Maximum traversal depth.
            edge_overrides: ``{edge_id: new_probability}`` to override.
            node_interventions: ``{node_id: failure_prob}`` to force (do-operator).
            disabled_edges: Edge IDs to remove for this scenario.
            disabled_nodes: Node IDs to remove for this scenario.
            compare: If True, run baseline + counterfactual + deltas.

        Returns:
            When compare=True: dict with baseline, counterfactual, deltas, summary.
            When compare=False: dict with node_id and cascade (list of results).
        """
        from ohm.queries import query_counterfactual_cascade, query_compare_scenarios

        if compare:
            return query_compare_scenarios(
                self._conn,
                node_id,
                failure_probability=failure_probability,
                max_depth=max_depth,
                edge_overrides=edge_overrides,
                node_interventions=node_interventions,
                disabled_edges=disabled_edges,
                disabled_nodes=disabled_nodes,
            )
        cascade = query_counterfactual_cascade(
            self._conn,
            node_id,
            failure_probability=failure_probability,
            max_depth=max_depth,
            edge_overrides=edge_overrides,
            node_interventions=node_interventions,
            disabled_edges=disabled_edges,
            disabled_nodes=disabled_nodes,
        )
        return {"node_id": node_id, "cascade": cascade}

    def propose_action(
        self,
        scenario_id: str,
        label: str,
        *,
        rationale: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Propose an action linked to a scenario (OHM-446a).

        Creates an ``action`` node and links it to the scenario via
        ``PROPOSES_ACTION`` L3 edge.

        Args:
            scenario_id: The scenario node that suggests this action.
            label: Human-readable action description.
            rationale: Optional explanation.
            connects_to: Additional nodes to cross-link.

        Returns:
            The created action node record.
        """
        from ohm.queries import propose_action

        return propose_action(
            self._conn,
            scenario_id=scenario_id,
            label=label,
            created_by=self.actor,
            rationale=rationale,
            connects_to=connects_to,
        )

    def execute_action(
        self,
        action_id: str,
        *,
        outcome: str | None = None,
        outcome_notes: str | None = None,
    ) -> dict[str, Any]:
        """Mark an action as executed and record the outcome (OHM-446a).

        Sets the action's status to 'executed', records the outcome,
        and creates an EXECUTED_BY L4 edge.

        Args:
            action_id: The action node to execute.
            outcome: TRUE/FALSE/AMBIGUOUS/DEFERRED.
            outcome_notes: Free-text notes on the execution.

        Returns:
            The updated action node record.
        """
        from ohm.queries import execute_action

        return execute_action(
            self._conn,
            action_id=action_id,
            executed_by=self.actor,
            outcome=outcome,
            outcome_notes=outcome_notes,
        )

    def loop_status(self, *, half_life_days: float = 30.0) -> dict[str, Any]:
        """Return the autonomy loop status — proposed/executed actions (OHM-446a).

        Extended with temporal section (OHM-2x2u): upcoming evaluations,
        stale feeds, compromised/stuck gates, and decay summary.

        Args:
            half_life_days: Half-life for confidence decay computation (default 30).

        Returns:
            Dict with proposed, executed, recent_scenarios, summary, and temporal.
        """
        from ohm.queries import query_loop_status

        return query_loop_status(self._conn, agent_name=self.actor, half_life_days=half_life_days)

    def compute_confidence_with_decay(
        self,
        *,
        base_confidence: float,
        last_observed_at: str | None = None,
        half_life_days: float = 30.0,
        floor: float = 0.1,
    ) -> dict[str, Any]:
        """Compute decayed confidence based on observation age (OHM-2x2u).

        Args:
            base_confidence: Original confidence value.
            last_observed_at: ISO 8601 timestamp of last observation.
            half_life_days: Half-life for decay (default 30).
            floor: Minimum confidence after decay (default 0.1).

        Returns:
            Dict with decayed_confidence, age_days, decay_factor, is_stale.
        """
        from ohm.queries import compute_confidence_with_decay

        return compute_confidence_with_decay(
            self._conn,
            base_confidence=base_confidence,
            last_observed_at=last_observed_at,
            half_life_days=half_life_days,
            floor=floor,
        )

    def apply_decay_to_edges(
        self,
        *,
        half_life_days: float = 30.0,
        floor: float = 0.1,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Apply confidence decay to L3/L4 edges (OHM-2x2u).

        Args:
            half_life_days: Override half-life for all layers (default 30).
            floor: Minimum confidence after decay (default 0.1).
            dry_run: If true, return what would change without modifying (default true).

        Returns:
            Dict with edges_examined, edges_decayed, average_decay_factor, summary.
        """
        from ohm.queries import apply_decay_to_edges

        return apply_decay_to_edges(
            self._conn,
            half_life_days=half_life_days,
            floor=floor,
            dry_run=dry_run,
            created_by=self.actor,
        )

    def register_twin(
        self,
        label: str,
        target_node_id: str,
        *,
        endpoint_url: str | None = None,
        description: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register an external domain twin (OHM-josq).

        Args:
            label: Human-readable twin name.
            target_node_id: The node this twin models.
            endpoint_url: Optional URL of the external twin service.
            description: Optional description of what the twin models.
            connects_to: Additional nodes to cross-link.

        Returns:
            The registered twin node record.
        """
        from ohm.queries import register_twin

        return register_twin(
            self._conn,
            label=label,
            target_node_id=target_node_id,
            created_by=self.actor,
            endpoint_url=endpoint_url,
            description=description,
            connects_to=connects_to,
        )

    def register_twin_with_bindings(
        self,
        label: str,
        target_node_id: str,
        *,
        decision_node_id: str | None = None,
        feed_node_ids: list[str] | None = None,
        model_candidate_ids: list[str] | None = None,
        description: str | None = None,
        endpoint_url: str | None = None,
    ) -> dict[str, Any]:
        """Register a twin with bindings in one call (OHM-f7tl).

        Args:
            label: Human-readable twin name.
            target_node_id: The node this twin models.
            decision_node_id: Optional decision node to bind.
            feed_node_ids: Optional feed nodes to bind.
            model_candidate_ids: Optional model candidates to attach.
            description: Optional description.
            endpoint_url: Optional URL of the external twin service.

        Returns:
            Dict with twin, target_node_id, decision_bound, feeds_bound, models_bound.
        """
        from ohm.queries import register_twin_with_bindings

        return register_twin_with_bindings(
            self._conn,
            label=label,
            target_node_id=target_node_id,
            decision_node_id=decision_node_id,
            feed_node_ids=feed_node_ids,
            model_candidate_ids=model_candidate_ids,
            created_by=self.actor,
            description=description,
            endpoint_url=endpoint_url,
        )

    def add_twin_bindings(
        self,
        twin_id: str,
        *,
        feed_node_ids: list[str] | None = None,
        feed_node_ids_remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or remove feed bindings on a twin (OHM-f7tl).

        Args:
            twin_id: The twin node ID.
            feed_node_ids: Feed nodes to add.
            feed_node_ids_remove: Feed nodes to remove.

        Returns:
            Dict with twin_id, added, removed, current_feeds.
        """
        from ohm.queries import add_twin_bindings

        return add_twin_bindings(
            self._conn,
            twin_id=twin_id,
            feed_node_ids=feed_node_ids,
            feed_node_ids_remove=feed_node_ids_remove,
            created_by=self.actor,
        )

    def attach_twin_models(
        self,
        twin_id: str,
        *,
        model_candidate_ids: list[str] | None = None,
        model_candidate_ids_remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Attach or detach model candidates on a twin (OHM-f7tl).

        Args:
            twin_id: The twin node ID.
            model_candidate_ids: Model candidates to attach.
            model_candidate_ids_remove: Model candidates to detach.

        Returns:
            Dict with twin_id, added, removed, current_models.
        """
        from ohm.queries import attach_twin_models

        return attach_twin_models(
            self._conn,
            twin_id=twin_id,
            model_candidate_ids=model_candidate_ids,
            model_candidate_ids_remove=model_candidate_ids_remove,
            created_by=self.actor,
        )

    def get_twin_readiness(self, twin_id: str) -> dict[str, Any]:
        """Check twin readiness gates (OHM-f7tl).

        Args:
            twin_id: The twin node ID.

        Returns:
            Dict with twin_id, gates, ready, missing, blocking.
        """
        from ohm.queries import get_twin_readiness

        return get_twin_readiness(self._conn, twin_id=twin_id)

    def twin_predict(
        self,
        twin_id: str,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Get twin predictions as edge_overrides-compatible dict (OHM-josq).

        Args:
            twin_id: The twin node ID.
            inputs: Optional input parameters for the twin.

        Returns:
            Dict with twin_id, edge_overrides, and nodes.
        """
        from ohm.queries import twin_predict

        return twin_predict(self._conn, twin_id, inputs=inputs)

    def twin_constraints(self, twin_id: str) -> dict[str, Any]:
        """Get twin constraints (OHM-josq).

        Args:
            twin_id: The twin node ID.

        Returns:
            Dict with twin, evaluates_edges, and constraints.
        """
        from ohm.queries import twin_constraints

        return twin_constraints(self._conn, twin_id)

    def validate_action_against_twin(
        self,
        twin_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        """Validate an action against twin constraints (OHM-josq).

        Args:
            twin_id: The twin node ID.
            action_id: The action node ID to validate.

        Returns:
            Dict with valid (bool) and violations (list).
        """
        from ohm.queries import validate_action_against_twin

        return validate_action_against_twin(self._conn, twin_id=twin_id, action_id=action_id)

    def explain_twin(self, twin_id: str) -> dict[str, Any]:
        """Explain what the twin models (OHM-josq).

        Args:
            twin_id: The twin node ID.

        Returns:
            Dict with twin_id, label, target_node_id, target_label,
            endpoint_url, constraint_count, edge_count, summary.
        """
        from ohm.queries import explain_twin

        return explain_twin(self._conn, twin_id)

    def create_twin_template(
        self,
        label: str,
        target_node_id: str,
        *,
        constraint_schema: dict[str, Any] | None = None,
        required_edges: list[str] | None = None,
        description: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a twin template (OHM-hl61).

        Args:
            label: Human-readable template name.
            target_node_id: The node this template models.
            constraint_schema: Optional dict of constraints for instantiated twins.
            required_edges: Optional list of edge types required on the target.
            description: Optional description of what the template models.
            connects_to: Additional nodes to cross-link.

        Returns:
            The created twin_template node record.
        """
        from ohm.queries import create_twin_template

        return create_twin_template(
            self._conn,
            label=label,
            target_node_id=target_node_id,
            created_by=self.actor,
            constraint_schema=constraint_schema,
            required_edges=required_edges,
            description=description,
            connects_to=connects_to,
        )

    def list_twin_templates(
        self,
        *,
        target_node_id: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List twin templates (OHM-hl61).

        Args:
            target_node_id: Optional filter — only templates evaluating this node.
            created_by: Optional filter — only templates by this agent.
            limit: Maximum number of templates to return.

        Returns:
            List of twin_template node records.
        """
        from ohm.queries import list_twin_templates

        return list_twin_templates(
            self._conn,
            target_node_id=target_node_id,
            created_by=created_by,
            limit=limit,
        )

    def get_twin_template(self, template_id: str) -> dict[str, Any]:
        """Get a twin template with its edges and metadata (OHM-hl61).

        Args:
            template_id: The twin_template node ID.

        Returns:
            Dict with template, evaluates_edges, constraint_schema, required_edges.
        """
        from ohm.queries import get_twin_template

        return get_twin_template(self._conn, template_id)

    def instantiate_twin_from_template(
        self,
        template_id: str,
        target_node_id: str,
        *,
        label: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Instantiate a twin from a template (OHM-hl61).

        Args:
            template_id: The twin_template to instantiate.
            target_node_id: The node the new twin will model.
            label: Optional label for the twin.
            connects_to: Additional nodes to cross-link.

        Returns:
            The instantiated twin node record.
        """
        from ohm.queries import instantiate_twin_from_template

        return instantiate_twin_from_template(
            self._conn,
            template_id=template_id,
            target_node_id=target_node_id,
            created_by=self.actor,
            label=label,
            connects_to=connects_to,
        )

    def assemble_twin_for_decision(
        self,
        decision_node_id: str,
        goal: str,
        *,
        horizon: int = 7,
        preferred_template_id: str | None = None,
        preferred_model_id: str | None = None,
        apply_decay: bool = True,
        half_life_days: float = 30.0,
        decay_floor: float = 0.1,
    ) -> dict[str, Any]:
        """Assemble a decision-specific twin from templates + primitives (OHM-f7tl).

        Args:
            decision_node_id: The decision node this twin will support.
            goal: Natural-language goal describing what the twin should model.
            horizon: Planning horizon in days (default 7).
            preferred_template_id: Override template selection.
            preferred_model_id: Override model selection.
            apply_decay: Decay model score by observation age (default True).
            half_life_days: Confidence half-life in days (default 30).
            decay_floor: Floor on decayed score (default 0.1).

        Returns:
            Dict with twin, template, model, ranking, reasoning.
        """
        from ohm.queries import assemble_twin_for_decision

        return assemble_twin_for_decision(
            self._conn,
            decision_node_id=decision_node_id,
            goal=goal,
            horizon=horizon,
            preferred_template_id=preferred_template_id,
            preferred_model_id=preferred_model_id,
            created_by=self.actor,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )

    def register_model_candidate(
        self,
        label: str,
        twin_id: str,
        *,
        model_parameters: dict[str, Any] | None = None,
        description: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Register a model candidate competing for a twin (OHM-75tw).

        Args:
            label: Human-readable model name.
            twin_id: The twin this model competes for.
            model_parameters: Optional dict of model hyperparameters.
            description: Optional description of the model.
            connects_to: Additional nodes to cross-link.

        Returns:
            The registered model_candidate node record.
        """
        from ohm.queries import register_model_candidate

        return register_model_candidate(
            self._conn,
            label=label,
            twin_id=twin_id,
            created_by=self.actor,
            model_parameters=model_parameters,
            description=description,
            connects_to=connects_to,
        )

    def evaluate_model(
        self,
        model_candidate_id: str,
        *,
        metrics: dict[str, float],
        dataset: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate a model candidate and store metrics (OHM-75tw).

        Args:
            model_candidate_id: The model candidate being evaluated.
            metrics: Dict of metric name → score (e.g., {"mae": 0.12, "rmse": 0.18}).
            dataset: Optional name of the evaluation dataset.
            description: Optional description of the evaluation.

        Returns:
            The created model_evaluation node record.
        """
        from ohm.queries import evaluate_model

        return evaluate_model(
            self._conn,
            model_candidate_id=model_candidate_id,
            created_by=self.actor,
            metrics=metrics,
            dataset=dataset,
            description=description,
        )

    def compare_models(
        self,
        twin_id: str,
        *,
        apply_decay: bool = True,
        half_life_days: float = 30.0,
        decay_floor: float | None = None,
    ) -> dict[str, Any]:
        """Compare all model candidates competing for a twin (OHM-75tw).

        Args:
            twin_id: The twin whose competing models to compare.
            apply_decay: Decay each candidate's composite_score by its
                evaluation age before ranking (default True).
            half_life_days: Confidence half-life in days (default 30).
            decay_floor: Floor on decayed score (default None — see
                ``compare_models`` docs for why composite_score is unbounded).

        Returns:
            Dict with twin_id, candidates (ranked list), and recommendation.
        """
        from ohm.queries import compare_models

        return compare_models(
            self._conn,
            twin_id=twin_id,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )

    def promote_model(
        self,
        model_candidate_id: str,
        *,
        policy: str = "accuracy",
        decision_node_id: str | None = None,
        min_improvement: float = 0.0,
        apply_decay: bool = True,
        half_life_days: float = 30.0,
        decay_floor: float = 0.1,
    ) -> dict[str, Any]:
        """Promote a model candidate to active status for its twin (OHM-75tw).

        Args:
            model_candidate_id: The model candidate to promote.
            policy: Promotion policy — "accuracy" (default) or "decision_value".
            decision_node_id: Required when policy="decision_value".
            min_improvement: Minimum decision_value improvement over active model.
            apply_decay: Decay evaluation accuracy by age (default True; only
                used when policy="decision_value").
            half_life_days: Confidence half-life in days (default 30).
            decay_floor: Floor on decayed accuracy (default 0.1).

        Returns:
            The promoted model_candidate node record.
        """
        from ohm.queries import promote_model

        return promote_model(
            self._conn,
            model_candidate_id=model_candidate_id,
            created_by=self.actor,
            policy=policy,
            decision_node_id=decision_node_id,
            min_improvement=min_improvement,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )

    def register_shadow_model(
        self,
        twin_id: str,
        label: str,
        *,
        source_model_id: str,
        model_parameters: dict[str, Any] | None = None,
        description: str | None = None,
        connects_to: list[str] | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import register_shadow_model

        return register_shadow_model(
            self._conn,
            twin_id=twin_id,
            label=label,
            source_model_id=source_model_id,
            created_by=self.actor,
            model_parameters=model_parameters,
            description=description,
            connects_to=connects_to,
        )

    def set_promotion_policy(
        self,
        model_candidate_id: str,
        *,
        policy: str,
        decision_node_id: str | None = None,
        min_improvement: float = 0.0,
    ) -> dict[str, Any]:
        """Set promotion policy on a model candidate (OHM-75tw).

        Args:
            model_candidate_id: The model candidate to configure.
            policy: "accuracy" or "decision_value".
            decision_node_id: Required when policy="decision_value".
            min_improvement: Minimum decision_value improvement threshold.

        Returns:
            The updated model_candidate node record.
        """
        from ohm.queries import set_promotion_policy

        return set_promotion_policy(
            self._conn,
            model_candidate_id=model_candidate_id,
            policy=policy,
            decision_node_id=decision_node_id,
            min_improvement=min_improvement,
            created_by=self.actor,
        )

    def auto_promote_best_model(
        self,
        twin_id: str,
        *,
        decision_node_id: str | None = None,
        policy: str = "decision_value",
        min_improvement: float = 0.0,
    ) -> dict[str, Any]:
        """Auto-promote the best model for a twin (OHM-75tw).

        Args:
            twin_id: The twin whose models to evaluate.
            decision_node_id: Required when policy="decision_value".
            policy: "decision_value" (default) or "accuracy".
            min_improvement: Minimum improvement threshold.

        Returns:
            Dict with promoted model, twin_id, and ranking.
        """
        from ohm.queries import auto_promote_best_model

        return auto_promote_best_model(
            self._conn,
            twin_id=twin_id,
            decision_node_id=decision_node_id,
            policy=policy,
            min_improvement=min_improvement,
            created_by=self.actor,
        )

    def detect_drift(
        self,
        twin_id: str,
        *,
        window_size: int = 100,
        residual_threshold: float = 0.15,
    ) -> dict[str, Any]:
        from ohm.queries import detect_drift

        return detect_drift(
            self._conn,
            twin_id=twin_id,
            window_size=window_size,
            residual_threshold=residual_threshold,
            created_by=self.actor,
        )

    def run_walk_forward_validation(
        self,
        model_id: str,
        *,
        n_splits: int = 5,
        min_train_size: int = 50,
    ) -> dict[str, Any]:
        from ohm.queries import run_walk_forward_validation

        return run_walk_forward_validation(
            self._conn,
            model_id=model_id,
            n_splits=n_splits,
            min_train_size=min_train_size,
            created_by=self.actor,
        )

    def ensemble_predict(
        self,
        twin_id: str,
        *,
        observation_window: int = 50,
        apply_decay: bool = True,
        half_life_days: float = 30.0,
        decay_floor: float | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import ensemble_predict

        return ensemble_predict(
            self._conn,
            twin_id=twin_id,
            observation_window=observation_window,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )

    def compute_decision_value(
        self,
        model_id: str,
        decision_node_id: str,
        *,
        utility_scale: float,
        apply_decay: bool = True,
        half_life_days: float = 30.0,
        decay_floor: float = 0.1,
    ) -> dict[str, Any]:
        from ohm.queries import compute_decision_value

        return compute_decision_value(
            self._conn,
            model_id=model_id,
            decision_node_id=decision_node_id,
            utility_scale=utility_scale,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )

    def auto_retire_model(
        self,
        model_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        from ohm.queries import auto_retire_model

        return auto_retire_model(
            self._conn,
            model_id=model_id,
            reason=reason,
            created_by=self.actor,
        )

    def set_freshness_threshold(
        self,
        decision_id: str,
        max_age_seconds: int,
        *,
        label: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import set_freshness_threshold

        return set_freshness_threshold(
            self._conn,
            decision_id=decision_id,
            max_age_seconds=max_age_seconds,
            created_by=self.actor,
            label=label,
        )

    def get_freshness_status(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import get_freshness_status

        return get_freshness_status(self._conn, decision_id=decision_id)

    def compute_feed_investment(
        self,
        decision_id: str,
        *,
        observation_cost: float = 0.5,
        label: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import compute_feed_investment

        return compute_feed_investment(
            self._conn,
            decision_id=decision_id,
            created_by=self.actor,
            observation_cost=observation_cost,
            label=label,
        )

    def recommend_mode(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import recommend_mode

        return recommend_mode(self._conn, decision_id=decision_id)

    def record_mode_switch(
        self,
        decision_id: str,
        from_mode: str,
        to_mode: str,
        *,
        reason: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        from ohm.queries import record_mode_switch

        return record_mode_switch(
            self._conn,
            decision_id=decision_id,
            from_mode=from_mode,
            to_mode=to_mode,
            created_by=self.actor,
            reason=reason,
            label=label,
        )

    def temporal_decision_summary(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        from ohm.queries import temporal_decision_summary

        return temporal_decision_summary(self._conn, decision_id=decision_id)

    def path(
        self,
        from_node: str,
        to_node: str,
        *,
        max_depth: int = 10,
    ) -> list[dict[str, Any]]:
        """Shortest path between two nodes."""
        from ohm.queries import query_path

        return query_path(self._conn, from_node, to_node, max_depth=max_depth)

    def get_edges_by_path(
        self,
        from_prefix: str,
        *,
        to_prefix: str | None = None,
        layer: str | None = None,
        edge_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query edges by node_path prefix (OHM-809).

        Uses the node_path column (OHM-ivlt) on ohm_nodes to find edges
        where the from-node's path starts with from_prefix. If to_prefix
        is given, also filters on the to-node's path prefix.

        Args:
            from_prefix: Path prefix for the from-node (e.g. 'TA.MA.CEM.RCC')
            to_prefix: Optional path prefix for the to-node
            layer: Optional edge layer filter (e.g. 'L3')
            edge_type: Optional edge type filter (e.g. 'CAUSES')
            limit: Maximum results (default 100)

        Returns:
            List of edge dicts with from_path and to_path included.
        """
        query = """
            SELECT e.*, nf.node_path AS from_path, nt.node_path AS to_path
            FROM ohm_edges e
            JOIN ohm_nodes nf ON nf.id = e.from_node
            JOIN ohm_nodes nt ON nt.id = e.to_node
            WHERE nf.node_path LIKE ? || '%'
              AND e.deleted_at IS NULL
              AND nf.deleted_at IS NULL
              AND nt.deleted_at IS NULL
        """
        params: list[Any] = [from_prefix]
        if to_prefix:
            query += " AND nt.node_path LIKE ? || '%'"
            params.append(to_prefix)
        if layer:
            query += " AND e.layer = ?"
            params.append(layer)
        if edge_type:
            query += " AND e.edge_type = ?"
            params.append(edge_type)
        query += " LIMIT ?"
        params.append(limit)

        result = self._conn.execute(query, params)
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def impact(self, node_id: str, *, depth: int = 5) -> list[dict[str, Any]]:
        """Downstream impact analysis."""
        from ohm.queries import query_impact

        return query_impact(self._conn, node_id, depth=depth)

    def confidence(self, edge_id: str) -> dict[str, Any]:
        """Full provenance and challenge audit for an edge."""
        from ohm.queries import query_confidence

        return query_confidence(self._conn, edge_id)

    def confidence_chain(self, node_id: str, *, max_depth: int = 5) -> dict[str, Any]:
        """Trace all incoming evidence edges to compute aggregate confidence.

        Walks incoming L2/L3 evidence edges recursively to build an evidence
        tree and computes aggregate confidence. Universal substrate method —
        works for any domain.

        Args:
            node_id: The node to trace evidence for.
            max_depth: Maximum chain depth (default 5).

        Returns:
            Dict with evidence_chain, aggregate_confidence, evidence_count.
        """
        from ohm.queries import query_confidence_chain

        return query_confidence_chain(self._conn, node_id, max_depth=max_depth)

    def agent_state(self, agent_name: str | None = None) -> list[dict[str, Any]]:
        """Query agent state."""
        from ohm.queries import query_agent_state

        return query_agent_state(self._conn, agent_name=agent_name)

    def stats(self) -> dict[str, Any]:
        """Graph statistics — edge counts by layer/type, node counts, challenge ratio."""
        from ohm.queries import query_stats

        return query_stats(self._conn)
