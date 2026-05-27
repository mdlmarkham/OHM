"""Inference handler mixin — Bayesian, causal, and value-of-information endpoints."""

from __future__ import annotations


class InferenceHandlerMixin:
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
                    evidence[validate_identifier(node_id.strip(), name="evidence_node")] = int(state.strip())
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(self.current_store.conn, target, evidence, edge_types=None, layers=layers, leak_probability=leak_probability, half_life_days=half_life_days, observation_window_days=observation_window_days, include_soft_evidence=include_soft_evidence, soft_edge_types=soft_edge_types, customer_id=self._customer_id)
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
            self.current_store.conn,
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
            self.current_store.conn,
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
                    evidence[validate_identifier(node_id.strip(), name="evidence_node")] = int(state.strip())
        leak_probability = float(qs.get("leak", ["0.15"])[0])
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference

        full_result = bayesian_inference(
            self.current_store.conn, target, evidence, layers=layers, leak_probability=leak_probability, observation_window_days=None, customer_id=self._customer_id
        )
        window_days = float(qs.get("window_days", ["30.0"])[0])
        windowed_result = bayesian_inference(
            self.current_store.conn, target, evidence, layers=layers, leak_probability=leak_probability, observation_window_days=window_days, customer_id=self._customer_id
        )
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
        self._json_response(200, {
            "target": target,
            "full_history": {"good": round(full_good, 4), "bad": round(full_posterior.get("bad", 0.5), 4)},
            "windowed": {"good": round(windowed_good, 4), "bad": round(windowed_posterior.get("bad", 0.5), 4), "window_days": window_days},
            "shift": round(shift, 4),
            "regime": regime,
            "method": "bayesian_regime_detection",
        })

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
        """GET /policy — belief-state decision: observe vs. act."""
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
        from ohm.methods import belief_state_decision

        result = belief_state_decision(
            self.current_store.conn, target, observation_cost=observation_cost,
            horizon=horizon, layers=layers, leak_probability=leak_probability,
        )
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
        from ohm.inference.discovery import discover_causal

        result = discover_causal(
            self.current_store.conn,
            node_ids=node_ids,
            method=method,
            alpha=alpha,
            min_observations=min_obs,
            indep_test=indep_test,
            score_class=score_class,
        )
        self._json_response(200, result)