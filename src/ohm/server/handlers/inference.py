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
        evidence = {}
        if evidence_str:
            for pair in evidence_str.split(","):
                if ":" in pair:
                    node_id, state = pair.split(":", 1)
                    evidence[validate_identifier(node_id.strip(), name="evidence_node")] = int(state.strip())
        layers_str = qs.get("layers", [""])[0]
        layers = [lyr.strip() for lyr in layers_str.split(",") if lyr.strip()] if layers_str else None
        from ohm.bayesian import bayesian_inference

        result = bayesian_inference(self.current_store.conn, target, evidence, edge_types=None, layers=layers, leak_probability=leak_probability, half_life_days=half_life_days)
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

        result = compute_ate(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
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

        result = compute_sensitivity(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
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

        result = find_adjustment_sets(self.current_store.conn, cause, effect, layers=layers, leak_probability=leak_probability)
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