"""Analysis handler mixin — graph health, structural analysis, and reliability endpoints."""

from __future__ import annotations


class AnalysisHandlerMixin:
    """Handler mixin for graph analysis endpoints (OHM-lzhk)."""

    def _get_health_graph(self, path: str, qs: dict) -> None:
        """GET /health/graph — graph health check."""
        from ohm.queries import query_graph_health

        result = query_graph_health(self.current_store.conn)
        self._json_response(200, result)

    def _get_health_agents(self, path: str, qs: dict) -> None:
        """GET /health/agents — agent health check."""
        from ohm.methods import query_agent_health

        result = query_agent_health(self.current_store.conn)
        self._json_response(200, result)

    def _get_health_sync(self, path: str, qs: dict) -> None:
        """GET /health/sync — DuckLake sync health check."""
        alias = qs.get("alias", ["ohm_lake"])[0]
        result = self.current_store.check_ducklake_health(alias=alias)
        status = 200 if result.get("healthy") and not result.get("sync_degraded") else 503
        self._json_response(status, result)

    def _get_contradictions(self, path: str, qs: dict) -> None:
        """GET /contradictions — detect contradictions."""
        from ohm.methods import detect_contradictions

        conf_thresh = float(qs.get("confidence", [0.5])[0])
        result = detect_contradictions(self.current_store.conn, confidence_threshold=conf_thresh)
        self._json_response(200, result)

    def _get_anomalies(self, path: str, qs: dict) -> None:
        """GET /anomalies — detect anomalies."""
        from ohm.methods import detect_anomalies

        sigma = float(qs.get("sigma", [2.0])[0])
        layer = qs.get("layer", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = detect_anomalies(self.current_store.conn, sigma_threshold=sigma, layer=layer, limit=limit)
        self._json_response(200, result)

    def _get_aggregate(self, path: str, qs: dict) -> None:
        """GET /aggregate/<id> — aggregate observations."""
        node_id = path[11:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        method = qs.get("method", ["weighted"])[0]
        from ohm.methods import aggregate_observations

        result = aggregate_observations(self.current_store.conn, node_id, method=method)
        self._json_response(200, result)

    def _get_provenance(self, path: str, qs: dict) -> None:
        """GET /provenance/<id> — provenance trace."""
        node_id = path[12:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        max_depth = int(qs.get("depth", [10])[0])
        from ohm.queries import query_provenance

        result = query_provenance(self.current_store.conn, node_id, max_depth=max_depth)
        self._json_response(200, result)

    def _get_stale(self, path: str, qs: dict) -> None:
        """GET /stale — list stale edges."""
        from ohm.queries import query_stale_edges

        threshold = float(qs.get("threshold", [0.1])[0])
        result = query_stale_edges(self.current_store.conn, stale_threshold=threshold)
        self._json_response(200, result)

    def _get_decay(self, path: str, qs: dict) -> None:
        """GET /decay — apply confidence decay."""
        self._require_write_auth()
        from ohm.queries import apply_confidence_decay

        threshold = float(qs.get("threshold", [0.1])[0])
        layer = qs.get("layer", [None])[0]
        dry_run = qs.get("dry_run", ["false"])[0].lower() == "true"
        result = apply_confidence_decay(
            self.current_store.conn,
            stale_threshold=threshold,
            layer=layer,
            dry_run=dry_run,
        )
        self._json_response(200, result)

    def _get_monte_carlo(self, path: str, qs: dict) -> None:
        """GET /monte-carlo/<id> — Monte Carlo impact simulation."""
        node_id = path[13:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        from ohm.methods import monte_carlo_impact

        sims = int(qs.get("simulations", [1000])[0])
        depth = int(qs.get("depth", [3])[0])
        default_prob = float(qs.get("default_probability", [0.5])[0])
        seed_val = qs.get("seed", [None])[0]
        seed = int(seed_val) if seed_val is not None else None
        result = monte_carlo_impact(
            self.current_store.conn,
            node_id,
            simulations=sims,
            depth=depth,
            default_probability=default_prob,
            seed=seed,
        )
        self._json_response(200, result)

    def _get_duplicates(self, path: str, qs: dict) -> None:
        """GET /duplicates — detect near-duplicate nodes."""
        from ohm.methods import detect_near_duplicates

        threshold = float(qs.get("similarity", [0.8])[0])
        result = detect_near_duplicates(self.current_store.conn, similarity_threshold=threshold)
        self._json_response(200, result)

    def _get_calibration(self, path: str, qs: dict) -> None:
        """GET /calibration/<agent> — confidence calibration."""
        agent_name = path[13:]
        from ohm.validation import validate_identifier

        agent_name = validate_identifier(agent_name, name="agent_name")
        from ohm.methods import compute_confidence_calibration

        result = compute_confidence_calibration(self.current_store.conn, agent_name)
        self._json_response(200, result)

    def _get_orphans(self, path: str, qs: dict) -> None:
        """GET /orphans — find disconnected nodes."""
        from ohm.methods import find_orphans

        node_type = qs.get("type", [None])[0]
        exclude_system = qs.get("exclude_system", ["true"])[0].lower() == "true"
        limit = int(qs.get("limit", [50])[0])
        result = find_orphans(self.current_store.conn, node_type=node_type, exclude_system=exclude_system, limit=limit)
        self._json_response(200, result)

    def _get_hubs(self, path: str, qs: dict) -> None:
        """GET /hubs — find most-connected nodes."""
        from ohm.methods import find_hubs

        node_type = qs.get("type", [None])[0]
        min_connections = int(qs.get("min_connections", [3])[0])
        limit = int(qs.get("limit", [20])[0])
        result = find_hubs(self.current_store.conn, node_type=node_type, min_connections=min_connections, limit=limit)
        self._json_response(200, result)

    def _get_dead_ends(self, path: str, qs: dict) -> None:
        """GET /dead_ends — find sink nodes."""
        from ohm.methods import find_dead_ends

        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = find_dead_ends(self.current_store.conn, node_type=node_type, limit=limit)
        self._json_response(200, result)

    def _get_centrality(self, path: str, qs: dict) -> None:
        """GET /centrality — compute causal influence centrality via PageRank."""
        from ohm.methods import compute_centrality

        edge_types_raw = qs.get("edge_types", [None])[0]
        edge_types = edge_types_raw.split(",") if edge_types_raw else None
        layer = qs.get("layer", [None])[0]
        weight_by_confidence = qs.get("weight_by_confidence", ["true"])[0].lower() == "true"
        limit = int(qs.get("limit", [20])[0])
        result = compute_centrality(
            self.current_store.conn,
            edge_types=edge_types,
            layer=layer,
            weight_by_confidence=weight_by_confidence,
            limit=limit,
        )
        self._json_response(200, result)

    def _get_communities(self, path: str, qs: dict) -> None:
        """GET /communities — detect communities via Louvain algorithm."""
        from ohm.methods import compute_communities

        edge_types_raw = qs.get("edge_types", [None])[0]
        edge_types = edge_types_raw.split(",") if edge_types_raw else None
        layer = qs.get("layer", [None])[0]
        result = compute_communities(self.current_store.conn, edge_types=edge_types, layer=layer)
        self._json_response(200, result)

    def _get_bridges(self, path: str, qs: dict) -> None:
        """GET /bridges — find bridge edges and articulation points."""
        from ohm.methods import find_bridges

        edge_types_raw = qs.get("edge_types", [None])[0]
        edge_types = edge_types_raw.split(",") if edge_types_raw else None
        layer = qs.get("layer", [None])[0]
        result = find_bridges(self.current_store.conn, edge_types=edge_types, layer=layer)
        self._json_response(200, result)

    def _get_suggest(self, path: str, qs: dict) -> None:
        """GET /suggest — suggest connections."""
        from ohm.methods import suggest_connections

        method = qs.get("method", ["shared_provenance"])[0]
        min_shared = int(qs.get("min_shared", [2])[0])
        limit = int(qs.get("limit", [20])[0])
        result = suggest_connections(self.current_store.conn, method=method, min_shared=min_shared, limit=limit)
        self._json_response(200, result)

    def _get_graph_stats(self, path: str, qs: dict) -> None:
        """GET /graph/stats — extended graph statistics."""
        from ohm.methods import graph_stats

        result = graph_stats(self.current_store.conn)
        self._json_response(200, result)

    def _get_lint(self, path: str, qs: dict) -> None:
        """GET /lint — lint graph against contract."""
        from ohm.contract import ContractConfig, lint_graph

        node_type_filter = qs.get("node_types", [None])[0]
        node_types = node_type_filter.split(",") if node_type_filter else None
        limit = int(qs.get("limit", ["1000"])[0])
        contract = ContractConfig()
        result = lint_graph(self.current_store.conn, contract, limit=limit, node_types=node_types)
        self._json_response(200, result)

    def _get_contract(self, path: str, qs: dict) -> None:
        """GET /contract — return current contract configuration."""
        from ohm.contract import ContractConfig

        contract = ContractConfig()
        self._json_response(200, contract.to_dict())

    def _get_deduplicate(self, path: str, qs: dict) -> None:
        """GET /deduplicate — remove duplicate edges."""
        self._require_write_auth()
        from ohm.exceptions import ValidationError

        layer = qs.get("layer", [None])[0]
        if layer:
            from ohm.validation import validate_layer

            try:
                validate_layer(layer)
            except ValueError as e:
                raise ValidationError(str(e))
        removed = self.current_store.deduplicate_edges(layer=layer)
        self._json_response(200, {"removed": removed, "layer": layer})

    def _get_graph_at(self, path: str, qs: dict) -> None:
        """GET /graph/at — query graph at snapshot version."""
        from ohm.exceptions import ValidationError

        version = qs.get("version", [None])[0]
        if not version:
            raise ValidationError("?version=N is required for /graph/at")
        try:
            version_int = int(version)
        except ValueError:
            raise ValidationError("?version must be an integer snapshot ID")
        result = self.current_store.graph_at_version(version_int)
        self._json_response(200, result)

    def _get_graph_changes(self, path: str, qs: dict) -> None:
        """GET /graph/changes — changes between snapshot versions."""
        from ohm.exceptions import ValidationError

        from_version = qs.get("from_version", [None])[0]
        to_version = qs.get("to_version", [None])[0]
        if not from_version or not to_version:
            raise ValidationError("?from_version=M&to_version=N are required for /graph/changes")
        try:
            from_int = int(from_version)
            to_int = int(to_version)
        except ValueError:
            raise ValidationError("?from_version and ?to_version must be integers")
        result = self.current_store.graph_changes(from_int, to_int)
        self._json_response(200, result)

    def _get_reliability(self, path: str, qs: dict) -> None:
        """GET /reliability/<agent> — source reliability metrics."""
        source_agent = path[13:]
        from ohm.validation import validate_identifier

        source_agent = validate_identifier(source_agent, name="source_agent")
        from ohm.queries import query_source_reliability

        result = query_source_reliability(self.current_store.conn, source_agent)
        self._json_response(200, result)

    def _get_source_reliability(self, path: str, qs: dict) -> None:
        """GET /source_reliability — alias for /reliability/{source} accepting ?source= param."""
        from ohm.exceptions import ValidationError

        source_agent = qs.get("source", [None])[0]
        if not source_agent:
            raise ValidationError("?source=<agent_name> is required")
        from ohm.validation import validate_identifier

        source_agent = validate_identifier(source_agent, name="source_agent")
        from ohm.queries import query_source_reliability

        result = query_source_reliability(self.current_store.conn, source_agent)
        self._json_response(200, result)

    def _get_compound_confidence(self, path: str, qs: dict) -> None:
        """GET /compound_confidence/<node_id> — compound confidence from node observations."""
        node_id = path[21:]
        from ohm.exceptions import NodeNotFoundError
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        node = self.current_store.get_node(node_id)
        if not node:
            raise NodeNotFoundError(f"Node not found: {node_id}")
        correlation = float(qs.get("correlation", ["0.0"])[0])
        half_life_days = float(qs.get("half_life", ["0.0"])[0])
        observations = self.current_store.execute(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC",
            [node_id],
        )
        from datetime import datetime

        from ohm.methods import compound_confidence

        now = datetime.now()

        def _obs_confidence(obs: dict) -> float:
            sigma = obs.get("sigma")
            if sigma is not None and sigma > 0:
                return max(0.0, min(1.0, 1.0 / (1.0 + float(sigma))))
            return 1.0

        def _decay_weight(obs: dict) -> float:
            if half_life_days <= 0.0:
                return 1.0
            created_at = obs.get("created_at")
            if not created_at:
                return 1.0
            try:
                obs_time = datetime.fromisoformat(str(created_at))
                age_days = max(0.0, (now - obs_time).total_seconds() / 86400.0)
                return 0.5 ** (age_days / half_life_days)
            except (ValueError, TypeError):
                return 1.0

        obs_with_confidence = [
            {
                "confidence": _obs_confidence(obs) * _decay_weight(obs),
                "source": obs.get("created_by"),
                "created_at": obs.get("created_at"),
            }
            for obs in observations
        ]
        result = compound_confidence(obs_with_confidence, correlation=correlation)
        result["node_id"] = node_id
        result["observations"] = len(observations)
        result["half_life_days"] = half_life_days
        self._json_response(200, result)