"""Inference handler mixin — Bayesian, causal, and value-of-information endpoints."""

from __future__ import annotations

from typing import Any

from ohm.server.handlers._base import OhmHandlerBase
from ohm.semantic_roles import SemanticRoles


class InferenceHandlerMixin(OhmHandlerBase):
    """Handler mixin for Bayesian/causal inference endpoints (OHM-rx7h)."""

    def _get_inference(self, path: str, qs: dict) -> None:
        """GET /inference — Bayesian inference."""
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        evidence_str = qs.get("evidence", [""])[0]
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        half_life_days = float(qs.get("half_life", ["0.0"])[0])
        obs_window_str = qs.get("observation_window", [""])[0]
        observation_window_days = float(obs_window_str) if obs_window_str else None
        include_soft_evidence = qs.get("soft_evidence", ["0"])[0] == "1"
        soft_edge_str = qs.get("soft_edges", [""])[0]
        soft_edge_types = [e.strip() for e in soft_edge_str.split(",") if e.strip()] if soft_edge_str else None
        evidence = {}
        if evidence_str:
            for pair in evidence_str.split(","):
                if ":" in pair:
                    node_id, state = pair.split(":", 1)
                    node_id = validate_identifier(node_id.strip(), name="evidence_node")
                    state = state.strip()
                    # Support float evidence values (probability-based, OHM-vatf.1)
                    # e.g., ?evidence=node:0.7 means "70%% bad"
                    try:
                        evidence[node_id] = float(state)
                    except ValueError:
                        evidence[node_id] = int(state)
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference
        from ohm.semantic_roles import SemanticRoles

        edge_types = qs.get("edge_types", [""])[0]
        edge_types = [e.strip() for e in edge_types.split(",") if e.strip()] if edge_types else SemanticRoles().inference_edge_types()

        result = bayesian_inference(
            self.current_store.conn.cursor(),
            target,
            evidence,
            edge_types=edge_types,
            layers=layers,
            leak_probability=leak_probability,
            half_life_days=half_life_days,
            observation_window_days=observation_window_days,
            include_soft_evidence=include_soft_evidence,
            soft_edge_types=soft_edge_types,
            customer_id=self._customer_id,
        )
        self._json_response(200, result)

    def _get_intervene(self, path: str, qs: dict) -> None:
        """GET /intervene — causal intervention (do-operator)."""
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        state_str = qs.get("state", [None])[0]
        if state_str is None:
            self._json_response(400, {"error": "missing_parameter", "message": "?state=0 (bad) or ?state=1 (good) required"})
            return
        try:
            intervention_state = int(state_str)
        except ValueError:
            self._json_response(400, {"error": "invalid_parameter", "message": "state must be 0 or 1"})
            return
        query_str = qs.get("query", [""])[0]
        query_nodes = None
        if query_str:
            query_nodes = [validate_identifier(q.strip(), name="query_node") for q in query_str.split(",") if q.strip()]
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        include_soft_evidence = qs.get("soft_evidence", ["0"])[0] == "1"
        soft_edge_str = qs.get("soft_edges", [""])[0]
        soft_edge_types = [e.strip() for e in soft_edge_str.split(",") if e.strip()] if soft_edge_str else None
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        pe_str = qs.get("preferred_edges", [""])[0]
        preferred_edges: set[tuple[str, str]] | None = None
        if pe_str:
            preferred_edges = set()
            for pair in pe_str.split(","):
                parts = pair.strip().split(":")
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    preferred_edges.add((parts[0].strip(), parts[1].strip()))
        from ohm.bayesian import causal_intervention

        result = causal_intervention(
            self.current_store.conn.cursor(),
            target,
            intervention_state,
            query_nodes=query_nodes,
            layers=layers,
            leak_probability=leak_probability,
            preferred_edges=preferred_edges,
            include_soft_evidence=include_soft_evidence,
            soft_edge_types=soft_edge_types,
            customer_id=self._customer_id,
        )
        self._json_response(200, result)

    def _get_ate(self, path: str, qs: dict) -> None:
        """GET /ate — average treatment effect."""
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import compute_ate

        result = compute_ate(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability, customer_id=self._customer_id)
        self._json_response(200, result)

    def _get_sensitivity(self, path: str, qs: dict) -> None:
        """GET /sensitivity — sensitivity analysis (E-value)."""
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import compute_sensitivity

        result = compute_sensitivity(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability, customer_id=self._customer_id)
        self._json_response(200, result)

    def _get_adjustment(self, path: str, qs: dict) -> None:
        """GET /adjustment — find adjustment sets."""
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import find_adjustment_sets

        result = find_adjustment_sets(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability, customer_id=self._customer_id)
        self._json_response(200, result)

    def _get_voi(self, path: str, qs: dict) -> None:
        """GET /voi — value of information ranking."""
        decision_str = qs.get("decision", [None])[0]
        decision_nodes = [d.strip() for d in decision_str.split(",") if d.strip()] if decision_str else None
        top = int(qs.get("top", ["10"])[0])
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        root_prior = float(qs.get("root_prior", ["0.3"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        edge_types_str = qs.get("edge_types", [""])[0]
        edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] if edge_types_str else None
        include_soft_evidence = qs.get("soft_evidence", ["0"])[0] == "1"
        soft_edge_str = qs.get("soft_edges", [""])[0]
        soft_edge_types = [e.strip() for e in soft_edge_str.split(",") if e.strip()] if soft_edge_str else None
        timeout = float(qs.get("timeout", ["0"])[0]) or None
        min_observations = int(qs.get("min_observations", ["0"])[0])
        from ohm.bayesian import compute_voi

        result = compute_voi(
            self.current_store.conn.cursor(),
            decision_nodes=decision_nodes,
            edge_types=edge_types,
            layers=layers,
            top=top,
            leak_probability=leak_probability,
            root_prior=root_prior,
            timeout=timeout,
            min_observations=min_observations,
            include_soft_evidence=include_soft_evidence,
            soft_edge_types=soft_edge_types,
            customer_id=self._customer_id,
        )
        self._json_response(200, result)

    def _get_voi_tasks(self, path: str, qs: dict) -> None:
        """GET /voi/tasks — VoI task assignments."""
        agent = qs.get("agent", [None])[0]
        decision_str = qs.get("decision", [None])[0]
        decision_nodes = [d.strip() for d in decision_str.split(",") if d.strip()] if decision_str else None
        top = int(qs.get("top", ["5"])[0])
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        root_prior = float(qs.get("root_prior", ["0.3"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import generate_voi_tasks

        result = generate_voi_tasks(
            self.current_store.conn,
            agent=agent,
            decision_nodes=decision_nodes,
            layers=layers,
            top=top,
            leak_probability=leak_probability,
            root_prior=root_prior,
            customer_id=self._customer_id,
        )
        self._json_response(200, result)

    def _get_suggest_causes(self, path: str, qs: dict) -> None:
        """GET /suggest_causes — suggest candidate causal edges."""
        min_confidence = float(qs.get("min_confidence", ["0.5"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import suggest_causes

        result = suggest_causes(self.current_store.conn, min_confidence=min_confidence, layers=layers)
        self._json_response(200, result)

    def _get_refute(self, path: str, qs: dict) -> None:
        """GET /refute — causal refutation tests."""
        cause = qs.get("cause", [None])[0]
        effect = qs.get("effect", [None])[0]
        if not cause or not effect:
            self._json_response(400, {"error": "missing_parameter", "message": "?cause=X&effect=Y required"})
            return
        from ohm.validation import validate_identifier

        cause = validate_identifier(cause, name="cause")
        effect = validate_identifier(effect, name="effect")
        n_samples = int(qs.get("n_samples", ["1000"])[0])
        seed = int(qs.get("seed", ["42"])[0])
        methods_str = qs.get("methods", [None])[0]
        refutation_methods = methods_str.split(",") if methods_str else None
        from ohm.causal_refutation import refute_causal_effect

        result = refute_causal_effect(
            self.current_store.conn,
            cause,
            effect,
            n_samples=n_samples,
            seed=seed,
            refutation_methods=refutation_methods,
        )
        self._json_response(200, result)

    def _get_regime(self, path: str, qs: dict) -> None:
        """GET /regime — regime detection: compare full-history vs windowed inference posteriors."""
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        evidence_str = qs.get("evidence", [""])[0]
        evidence = {}
        if evidence_str:
            for pair in evidence_str.split(","):
                if ":" in pair:
                    node_id, state = pair.split(":", 1)
                    node_id = validate_identifier(node_id.strip(), name="evidence_node")
                    state = state.strip()
                    # Support float evidence values (probability-based, OHM-vatf.1)
                    try:
                        evidence[node_id] = float(state)
                    except ValueError:
                        evidence[node_id] = int(state)
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference

        full_result = bayesian_inference(self.current_store.conn, target, evidence, layers=layers, leak_probability=leak_probability, observation_window_days=None, customer_id=self._customer_id)
        window_days = float(qs.get("window_days", ["30.0"])[0])
        windowed_result = bayesian_inference(self.current_store.conn, target, evidence, layers=layers, leak_probability=leak_probability, observation_window_days=window_days, customer_id=self._customer_id)
        full_posterior = full_result.get("posterior", {}).get(target, {})
        windowed_posterior = windowed_result.get("posterior", {}).get(target, {})
        full_good = full_posterior.get("good", 0.5)
        windowed_good = windowed_posterior.get("good", 0.5)
        shift = windowed_good - full_good
        regime = "stable"
        if abs(shift) > 0.15:
            regime = "regime_shift" if shift > 0 else "deteriorating"
        elif abs(shift) > 0.05:
            regime = "drifting"
        self._json_response(
            200,
            {
                "target": target,
                "full_history": {"good": round(full_good, 4), "bad": round(full_posterior.get("bad", 0.5), 4)},
                "windowed": {"good": round(windowed_good, 4), "bad": round(windowed_posterior.get("bad", 0.5), 4), "window_days": window_days},
                "shift": round(shift, 4),
                "regime": regime,
                "method": "bayesian_regime_detection",
            },
        )

    def _get_game(self, path: str, qs: dict) -> None:
        """GET /game — extract normal-form game from causal graph."""
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        players_str = qs.get("players", [""])[0]
        players = [p.strip() for p in players_str.split(",") if p.strip()] if players_str else None
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.graph_reader import coerce_reader

        reader = coerce_reader(self.current_store.conn)
        from ohm.game import extract_game

        result = extract_game(reader, target, players=players, layers=layers)
        self._json_response(200, result)

    def _get_nash(self, path: str, qs: dict) -> None:
        """GET /nash — compute Nash equilibrium for extracted game."""
        players_str = qs.get("players", [""])[0]
        if not players_str:
            self._json_response(400, {"error": "missing_parameter", "message": "?players=a,b,... required"})
            return
        players = [p.strip() for p in players_str.split(",") if p.strip()]
        payoff_str = qs.get("payoffs", [None])[0]
        if not payoff_str:
            self._json_response(400, {"error": "missing_parameter", "message": "?payoffs=matrix format required (use /game first)"})
            return
        try:
            import json

            payoff_matrices = json.loads(payoff_str)
        except (json.JSONDecodeError, Exception):
            self._json_response(400, {"error": "invalid_parameter", "message": "?payoffs must be a valid JSON array of payoff matrices"})
            return
        from ohm.game import compute_nash

        result = compute_nash(payoff_matrices, players)
        self._json_response(200, result)

    def _get_policy(self, path: str, qs: dict) -> None:
        """GET /policy — belief-state decision: observe vs. act (OHM-od01.5 Phase 1).

        Phase 1 POMDP: compare Expected Value of Perfect Information (EVPI)
        against the cost of an observation. If EVPI > cost → observe
        (explore); else → act (exploit) on the best known action.
        """
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        obs_cost_str = qs.get("observation_cost", [None])[0]
        observation_cost = float(obs_cost_str) if obs_cost_str else None
        horizon = int(qs.get("horizon", [1])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        # OHM-od01.5: route through the canonical Phase 1 POMDP
        # (compute_policy in ohm.inference.pomdp). It supersedes the older
        # belief_state_decision in ohm.graph.methods and exposes the
        # richer response (confidence, current_belief, top_voi_candidates).
        from ohm.inference.pomdp import compute_policy

        kwargs: dict = {
            "horizon": horizon,
            "leak_probability": leak_probability,
        }
        if observation_cost is not None:
            kwargs["cost_of_observation"] = observation_cost
        if layers is not None:
            kwargs["layers"] = layers
        result = compute_policy(self.current_store.conn, target, **kwargs)
        self._json_response(200, result)

    def _get_discover(self, path: str, qs: dict) -> None:
        """GET /discover — causal structure discovery from observation data."""
        from ohm.validation import validate_identifier

        nodes_str = qs.get("nodes", [""])[0]
        node_ids = [validate_identifier(n.strip()) for n in nodes_str.split(",") if n.strip()] if nodes_str else None
        method = qs.get("method", ["pc"])[0]
        if method not in ("pc", "ges", "both"):
            self._json_response(400, {"error": "invalid_parameter", "message": "?method must be pc, ges, or both"})
            return
        alpha = float(qs.get("alpha", ["0.05"])[0])
        min_obs = int(qs.get("min_observations", ["5"])[0])
        indep_test = qs.get("indep_test", ["fisherz"])[0]
        score_class = qs.get("score_class", ["local_score_BIC"])[0]
        queue = qs.get("queue", ["false"])[0].lower() in ("true", "1", "yes")
        from ohm.inference.discovery import discover_causal

        try:
            result = discover_causal(
                self.current_store.conn,
                node_ids=node_ids,
                method=method,
                alpha=alpha,
                min_observations=min_obs,
                indep_test=indep_test,
                score_class=score_class,
            )
        except (ValueError, TypeError) as e:
            self._json_response(400, {"error": "invalid_parameter", "message": str(e)})
            return
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": str(e)})
            return

        if queue and result.get("candidate_edges"):
            from ohm.graph.queries import queue_discovery_candidates

            actor = getattr(self, "_actor", None) or "system"
            queued_ids = queue_discovery_candidates(
                self.current_store.conn,
                result["candidate_edges"],
                created_by=actor,
            )
            result["queued_ids"] = queued_ids

        self._json_response(200, result)

    def _get_discovery_queue(self, path: str, qs: dict) -> None:
        """GET /discover/queue — list pending discovery candidates for agent review."""
        from ohm.graph.queries import query_discovery_queue

        status = qs.get("status", [None])[0]
        method = qs.get("method", [None])[0]
        limit = int(qs.get("limit", ["100"])[0])

        result = query_discovery_queue(
            self.current_store.conn,
            status=status,
            method=method,
            limit=limit,
        )
        self._json_response(200, {"queue": result, "count": len(result)})

    def _post_discovery_review(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /discover/queue/review — accept or reject a discovery candidate."""
        from ohm.graph.queries import review_discovery_candidate
        from ohm.exceptions import EdgeNotFoundError, ValidationError

        queue_id = body.get("queue_id", "")
        action = body.get("action", "")
        reviewed_by = body.get("reviewed_by", agent or "unknown")
        review_notes = body.get("review_notes")
        edge_layer = body.get("edge_layer", "L3")

        if not queue_id:
            self._json_response(400, {"error": "missing_parameter", "message": "queue_id required"})
            return
        if action not in ("accept", "reject"):
            self._json_response(400, {"error": "invalid_parameter", "message": "action must be 'accept' or 'reject'"})
            return

        try:
            result = review_discovery_candidate(
                self.current_store.conn,
                queue_id,
                action=action,
                reviewed_by=reviewed_by,
                review_notes=review_notes,
                edge_layer=edge_layer,
            )
            if "error" in result:
                self._json_response(409, result)
            else:
                self._json_response(200, result)
        except EdgeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})
        except ValidationError as e:
            self._json_response(400, {"error": "validation_error", "message": str(e)})

    def _get_belief(self, path: str, qs: dict) -> None:
        """GET /belief — composed belief summary for agents (OHM-765).

        Wraps /inference + /voi + /neighborhood into a single response
        so agents get the posterior, why it believes it, and what to do
        next in one call.

        OHM-934: Enhanced to expose full posterior percentiles, prior
        distribution + KL surprise, evidence movers, belief_statement
        calibration, and method metadata.
        """
        target = qs.get("target", [None])[0]
        if not target:
            self._json_response(400, {"error": "missing_parameter", "message": "?target=node_id required"})
            return
        from ohm.validation import validate_identifier

        target = validate_identifier(target, name="target")
        evidence_str = qs.get("evidence", [""])[0]
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        edge_types_str = qs.get("edge_types", [""])[0]
        include_evidence_movers = qs.get("include_evidence_movers", ["true"])[0].lower() in ("1", "true", "yes")
        include_prior = qs.get("include_prior", ["true"])[0].lower() in ("1", "true", "yes")
        belief_statement_str = qs.get("belief_statement", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        edge_types = [e.strip() for e in edge_types_str.split(",") if e.strip()] if edge_types_str else None
        evidence: dict[str, float | int] = {}
        if evidence_str:
            for pair in evidence_str.split(","):
                if ":" in pair:
                    node_id, state = pair.split(":", 1)
                    node_id = validate_identifier(node_id.strip(), name="evidence_node")
                    try:
                        evidence[node_id] = float(state.strip())
                    except ValueError:
                        evidence[node_id] = int(state.strip())

        import math

        try:
            from ohm.bayesian import bayesian_inference, compute_voi
            from ohm.graph.queries import query_neighborhood

            # 1. Posterior (graceful: empty graph returns uniform prior)
            method = "none"
            pgmpy_available = False
            try:
                inference_result = bayesian_inference(
                    self.current_store.conn.cursor(),
                    target,
                    evidence,
                    edge_types=edge_types,
                    layers=layers,
                    leak_probability=leak_probability,
                    customer_id=self._customer_id,
                )
                method = inference_result.get("method", "unknown")
                pgmpy_available = inference_result.get("pgmpy_available", False)
            except Exception:
                inference_result = {"posterior": {}}

            # 2. Drivers (1-hop neighborhood — may return list or dict)
            try:
                neighborhood = query_neighborhood(
                    self.current_store.read_conn,
                    target,
                    depth=1,
                )
            except Exception:
                neighborhood = []

            # Normalize: query_neighborhood may return a list of edges or a dict
            if isinstance(neighborhood, dict):
                edges = neighborhood.get("edges", [])
            elif isinstance(neighborhood, list):
                edges = neighborhood
            else:
                edges = []

            # 3. Value of Information
            try:
                voi_result = compute_voi(
                    self.current_store.conn.cursor(),
                    decision_nodes=[target],
                    edge_types=edge_types,
                    layers=layers,
                    top=5,
                    leak_probability=leak_probability,
                    customer_id=self._customer_id,
                )
            except Exception:
                voi_result = {"recommendations": []}

            # Extract posterior values (OHM-781: bayesian_inference returns
            # {"posterior": {"target_node_id": {"bad": 0.7, "good": 0.3}}, ...})
            posterior_raw = inference_result.get("posterior", {})
            posterior = posterior_raw.get(target, {}) if isinstance(posterior_raw, dict) else {}
            p_bad = float(posterior.get("bad", 0.0))
            p_good = float(posterior.get("good", 1.0 - p_bad))

            # Build driver list from neighborhood edges
            # Use the same edge_types as inference/VoI (default: inference_edge_types from SemanticRoles)
            driver_edge_types = edge_types or SemanticRoles.defaults().inference_edge_types()
            drivers = []
            for edge in edges:
                if isinstance(edge, dict) and edge.get("to_node") == target and edge.get("edge_type") in driver_edge_types:
                    drivers.append(
                        {
                            "node": edge.get("from_node"),
                            "edge_type": edge.get("edge_type"),
                            "confidence": edge.get("confidence"),
                        }
                    )

            # Build VoI suggestions (voi_result may be dict or list)
            voi_recs = voi_result.get("rankings", voi_result.get("recommendations", [])) if isinstance(voi_result, dict) else (voi_result if isinstance(voi_result, list) else [])
            suggestions = []
            for rec in voi_recs:
                if isinstance(rec, dict):
                    suggestions.append(
                        {
                            "node": rec.get("node_id", rec.get("node", "")),
                            "expected_info_gain": rec.get("voi", rec.get("expected_info_gain", 0.0)),
                        }
                    )

            entropy = -(p_bad * math.log2(p_bad + 1e-10) + p_good * math.log2(p_good + 1e-10)) if 0 < p_bad < 1 else 0.0

            uncertainty = "high" if entropy > 0.8 else "medium" if entropy > 0.5 else "low"
            summary = f"P(bad) = {p_bad:.2f}. Uncertainty is {uncertainty}."

            response: dict[str, Any] = {
                "target": target,
                "summary": summary,
                "posterior": {
                    "P(bad)": round(p_bad, 4),
                    "P(good)": round(p_good, 4),
                    "entropy_bits": round(entropy, 4),
                },
                "why": {
                    "drivers": drivers[:10],
                    "most_influential": drivers[0]["node"] if drivers else None,
                },
                "what_to_do_next": {
                    "suggested_observations": suggestions[:5],
                },
                "method": method,
                "pgmpy_available": pgmpy_available,
            }

            # ── OHM-934: Prior distribution + KL surprise ──
            if include_prior and evidence:
                try:
                    prior_result = bayesian_inference(
                        self.current_store.conn.cursor(),
                        target,
                        {},
                        edge_types=edge_types,
                        layers=layers,
                        leak_probability=leak_probability,
                        customer_id=self._customer_id,
                    )
                    prior_posterior = prior_result.get("posterior", {})
                    prior_target = prior_posterior.get(target, {}) if isinstance(prior_posterior, dict) else {}
                    p_bad_prior = float(prior_target.get("bad", 0.0))
                    p_good_prior = float(prior_target.get("good", 1.0 - p_bad_prior))

                    response["prior"] = {
                        "P(bad)": round(p_bad_prior, 4),
                        "P(good)": round(p_good_prior, 4),
                    }

                    # KL divergence: D_KL(posterior || prior)
                    kl = 0.0
                    for p_post, p_prior in [(p_bad, p_bad_prior), (p_good, p_good_prior)]:
                        if p_post > 1e-10 and p_prior > 1e-10:
                            kl += p_post * math.log(p_post / p_prior)
                    kl = round(kl, 6)

                    if kl < 0.01:
                        surprise_level = "negligible"
                    elif kl < 0.1:
                        surprise_level = "low"
                    elif kl < 0.5:
                        surprise_level = "moderate"
                    elif kl < 1.0:
                        surprise_level = "high"
                    else:
                        surprise_level = "very_high"

                    response["surprise"] = {
                        "kl_divergence": kl,
                        "level": surprise_level,
                    }
                except Exception:
                    pass

            # ── OHM-934: Posterior percentiles via Beta approximation ──
            # Count effective observations from the evidence size + drivers
            n_effective = max(len(evidence) + len(drivers), 2)
            if 0 < p_bad < 1:
                kappa = float(n_effective)
                alpha = p_bad * kappa + 1.0
                beta_param = (1.0 - p_bad) * kappa + 1.0

                def _beta_quantile(q: float) -> float:
                    """Inverse CDF of Beta(alpha, beta) via bisection."""
                    lo, hi = 0.0, 1.0
                    for _ in range(50):
                        mid = (lo + hi) / 2.0
                        # Regularised incomplete beta via simple numeric integration
                        # For small params, use scipy-like approximation
                        from math import betainc as _betainc  # type: ignore[attr-defined]
                        try:
                            cdf = _betainc(alpha, beta_param, mid)
                        except (ImportError, ValueError):
                            # Fallback: normal approximation
                            mean = alpha / (alpha + beta_param)
                            std_val = math.sqrt(alpha * beta_param / ((alpha + beta_param) ** 2 * (alpha + beta_param + 1)))
                            from math import erfc, sqrt
                            cdf = 0.5 * erfc(-(mid - mean) / (std_val * sqrt(2)))
                        if cdf < q:
                            lo = mid
                        else:
                            hi = mid
                    return (lo + hi) / 2.0

                # Compute percentiles
                percentiles = {}
                for pct_name, q in [("p05", 0.05), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p95", 0.95)]:
                    try:
                        percentiles[pct_name] = round(_beta_quantile(q), 4)
                    except Exception:
                        # Fallback: normal approximation
                        mean = alpha / (alpha + beta_param)
                        std_val = math.sqrt(alpha * beta_param / ((alpha + beta_param) ** 2 * (alpha + beta_param + 1)))
                        from math import erfc, sqrt
                        from statistics import NormalDist
                        nd = NormalDist(mu=mean, sigma=std_val)
                        z_map = {0.05: -1.645, 0.25: -0.674, 0.50: 0.0, 0.75: 0.674, 0.95: 1.645}
                        percentiles[pct_name] = round(mean + std_val * z_map[q], 4)

                # Mode of Beta distribution
                if alpha > 1 and beta_param > 1:
                    mode = round((alpha - 1) / (alpha + beta_param - 2), 4)
                else:
                    mode = round(p_bad, 4)

                # Standard deviation
                std = round(math.sqrt(alpha * beta_param / ((alpha + beta_param) ** 2 * (alpha + beta_param + 1))), 4)

                response["posterior"].update({
                    **percentiles,
                    "mean": round(p_bad, 4),
                    "mode": mode,
                    "std": std,
                })

                # Update summary with top driver
                if drivers:
                    summary += f" Top driver: {drivers[0]['node']}."
                    response["summary"] = summary

            # ── OHM-934: Evidence movers ──
            if include_evidence_movers and evidence:
                movers = []
                for obs_node, obs_state in evidence.items():
                    try:
                        # Re-run inference without this observation
                        reduced_evidence = {k: v for k, v in evidence.items() if k != obs_node}
                        reduced_result = bayesian_inference(
                            self.current_store.conn.cursor(),
                            target,
                            reduced_evidence,
                            edge_types=edge_types,
                            layers=layers,
                            leak_probability=leak_probability,
                            customer_id=self._customer_id,
                        )
                        reduced_posterior = reduced_result.get("posterior", {})
                        reduced_target = reduced_posterior.get(target, {}) if isinstance(reduced_posterior, dict) else {}
                        p_bad_reduced = float(reduced_target.get("bad", p_bad))

                        delta = p_bad - p_bad_reduced

                        # Look up observation metadata from neighborhood edges
                        obs_type = "observation"
                        effective_confidence = float(obs_state) if isinstance(obs_state, (int, float)) else 0.5
                        age_days = 0.0
                        source = None

                        for edge in edges:
                            if isinstance(edge, dict):
                                if edge.get("from_node") == obs_node or edge.get("to_node") == obs_node:
                                    ec = edge.get("confidence")
                                    if ec is not None:
                                        effective_confidence = float(ec)
                                    break

                        # Try to get observation age from the node's created_at
                        try:
                            from ohm.graph.queries import _rows_to_dicts
                            cursor = self.current_store.read_conn.cursor()
                            rows = _rows_to_dicts(cursor.execute(
                                "SELECT type, created_at FROM ohm_nodes WHERE id = ?",
                                [obs_node],
                            ))
                            if rows:
                                obs_type = rows[0].get("type", obs_type)
                                created_at = rows[0].get("created_at")
                                if created_at:
                                    import datetime
                                    try:
                                        if isinstance(created_at, str):
                                            created = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                                        else:
                                            created = created_at
                                        age_days = round((datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 86400, 1)
                                    except (ValueError, TypeError, AttributeError):
                                        pass
                        except Exception:
                            pass

                        # Try to find source agent
                        try:
                            from ohm.graph.queries import _rows_to_dicts
                            cursor = self.current_store.read_conn.cursor()
                            src_rows = _rows_to_dicts(cursor.execute(
                                "SELECT provenance FROM ohm_edges WHERE from_node = ? AND to_node = ? LIMIT 1",
                                [obs_node, target],
                            ))
                            if not src_rows:
                                src_rows = _rows_to_dicts(cursor.execute(
                                    "SELECT provenance FROM ohm_edges WHERE from_node = ? AND to_node = ? LIMIT 1",
                                    [target, obs_node],
                                ))
                            if src_rows:
                                source = src_rows[0].get("provenance")
                        except Exception:
                            pass

                        movers.append({
                            "node": obs_node,
                            "type": obs_type,
                            "delta_p_bad": round(delta, 4),
                            "direction": "increases_bad" if delta > 0 else "decreases_bad",
                            "effective_confidence": round(effective_confidence, 4),
                            "age_days": age_days,
                            "source": source,
                        })
                    except Exception:
                        continue

                # Sort by absolute impact
                movers.sort(key=lambda m: abs(m["delta_p_bad"]), reverse=True)
                response["evidence_movers"] = movers

            # ── OHM-934: Belief statement calibration ──
            if belief_statement_str:
                try:
                    from ohm.mcp.belief import parse_belief_statement, compare_belief_to_posterior
                    parsed = parse_belief_statement(belief_statement_str)
                    if parsed:
                        comparison = compare_belief_to_posterior(
                            parsed["claimed_probability"],
                            {"P(bad)": p_bad, "P(good)": p_good},
                            parsed.get("state", "bad"),
                        )
                        response["calibration"] = {
                            "agent_belief_statement": parsed.get("raw", belief_statement_str),
                            "graph_probability": comparison["graph_probability"],
                            "divergence": comparison["divergence"],
                            "severity": comparison["severity"],
                        }
                except Exception:
                    pass

            self._json_response(200, response)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Belief computation failed: {e}"})
