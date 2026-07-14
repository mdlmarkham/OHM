"""Scenario handler mixin."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class ScenarioHandlerMixin(OhmHandlerBase):
    """Handler mixin for scenario handler mixin."""

    def _post_scenario(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /scenario — counterfactual scenario analysis (OHM-xagx).

        Body:
            {
              "node_id": "supplier-1",
              "failure_probability": 1.0,
              "max_depth": 10,
              "edge_overrides": {"edge-id-1": 0.3},
              "node_interventions": {"node-id-2": 0.9},
              "disabled_edges": ["edge-id-3"],
              "disabled_nodes": ["node-id-4"],
              "compare": true
            }

        When ``compare`` is true, runs both baseline and counterfactual
        and returns the comparison (deltas + summary). When false, returns
        only the counterfactual result.
        """
        from ohm.queries import query_counterfactual_cascade, query_compare_scenarios
        from ohm.exceptions import ValidationError

        node_id = body.get("node_id")
        if not node_id:
            raise ValidationError("node_id is required")

        failure_probability = float(body.get("failure_probability", 1.0))
        max_depth = int(body.get("max_depth", 10))
        edge_overrides = body.get("edge_overrides")
        node_interventions = body.get("node_interventions")
        disabled_edges = set(body.get("disabled_edges", []))
        disabled_nodes = set(body.get("disabled_nodes", []))
        compare = body.get("compare", True)

        if compare:
            result = query_compare_scenarios(
                self.current_store.read_conn,
                node_id,
                failure_probability=failure_probability,
                max_depth=max_depth,
                edge_overrides=edge_overrides,
                node_interventions=node_interventions,
                disabled_edges=disabled_edges,
                disabled_nodes=disabled_nodes,
            )
        else:
            cascade = query_counterfactual_cascade(
                self.current_store.read_conn,
                node_id,
                failure_probability=failure_probability,
                max_depth=max_depth,
                edge_overrides=edge_overrides,
                node_interventions=node_interventions,
                disabled_edges=disabled_edges,
                disabled_nodes=disabled_nodes,
            )
            result = {"node_id": node_id, "cascade": cascade}

        self._json_response(200, result)

    def _post_propose_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /propose-action — propose an action linked to a scenario (OHM-446a).

        Body: {scenario_id, label, rationale?, connects_to?}
        """
        from ohm.queries import propose_action
        from ohm.exceptions import ValidationError

        scenario_id = body.get("scenario_id")
        label = body.get("label")
        if not scenario_id:
            raise ValidationError("scenario_id is required")
        if not label:
            raise ValidationError("label is required")

        result = propose_action(
            self.current_store.conn,
            scenario_id=scenario_id,
            label=label,
            created_by=agent,
            rationale=body.get("rationale"),
            connects_to=body.get("connects_to"),
        )
        self._json_response(201, result)

    def _post_execute_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /execute-action — mark an action as executed (OHM-446a).

        Body: {action_id, outcome?, outcome_notes?}
        """
        from ohm.queries import execute_action
        from ohm.exceptions import ValidationError, NodeNotFoundError

        action_id = body.get("action_id")
        if not action_id:
            raise ValidationError("action_id is required")

        try:
            result = execute_action(
                self.current_store.conn,
                action_id=action_id,
                executed_by=agent,
                outcome=body.get("outcome"),
                outcome_notes=body.get("outcome_notes"),
            )
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _get_loop_status(self, path: str, qs: dict) -> None:
        """GET /loop-status — autonomy loop status (OHM-446a).

        Returns proposed/executed actions and recent scenarios.
        Optional ?agent= filter. Optional ?half_life_days= for decay integration.
        """
        from ohm.queries import query_loop_status

        agent = qs.get("agent", [None])[0]
        half_life_days = 30.0
        hld = qs.get("half_life_days", [None])[0]
        if hld is not None:
            try:
                half_life_days = float(hld)
            except (ValueError, TypeError):
                pass
        result = query_loop_status(self.current_store.read_conn, agent_name=agent, half_life_days=half_life_days)
        self._json_response(200, result)

    def _post_simulate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /simulate/{prospect_id} — Monte Carlo prospect simulation (OHM-843).

        Body: {n_iterations?, seed?}

        Runs a Monte Carlo simulation over the prospect's expectation nodes,
        sampling from Beta-PERT distributions per expectation. Persists the
        result as an experiment_result observation.
        """
        from ohm.graph.queries.simulate import simulate_prospect

        prospect_id = path.rstrip("/").split("/")[-1]
        n_iterations = int(body.get("n_iterations", 5000))
        seed = body.get("seed")

        try:
            result = simulate_prospect(
                self.current_store.conn,
                prospect_id=prospect_id,
                n_iterations=n_iterations,
                seed=int(seed) if seed is not None else None,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "simulation_failed", "message": str(e)})

    def _post_decision_autoresearch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /decision/{id}/autoresearch — run one autoresearch round (OHM-845).

        Body: {dry_run?, max_candidates?}

        Generates candidate hypothesis edges, evaluates each via
        transaction-insert-then-rollback, and promotes any that improve
        the recommendation.
        """
        _path = path.rstrip("/")
        if not _path.endswith("/autoresearch") and not _path.endswith("autoresearch"):
            self._json_response(405, {"error": "method_not_allowed", "message": "POST not supported on this endpoint"})
            return

        from ohm.decision.autoresearch import run_autoresearch_round

        decision_id = path.rstrip("/").split("/")[-1]
        if decision_id == "autoresearch":
            decision_id = path.rstrip("/").split("/")[-2]

        dry_run = body.get("dry_run", False)
        max_candidates = int(body.get("max_candidates", 5))

        try:
            result = run_autoresearch_round(
                self.current_store.conn,
                decision_id=decision_id,
                dry_run=dry_run,
                max_candidates=max_candidates,
                agent=agent,
            )
            self._json_response(200, result)
        except ValueError as e:
            self._json_response(422, {"error": "autoresearch_failed", "message": str(e)})

