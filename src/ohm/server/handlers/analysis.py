"""Analysis handler mixin — graph health, structural analysis, and reliability endpoints."""

from __future__ import annotations


class AnalysisHandlerMixin:
    """Handler mixin for graph analysis endpoints (OHM-lzhk)."""

    def _get_health_graph(self, path: str, qs: dict) -> None:
        """GET /health/graph — graph health check."""
        from ohm.queries import query_graph_health

        result = query_graph_health(self.current_store.read_conn)
        self._json_response(200, result)

    def _get_health_agents(self, path: str, qs: dict) -> None:
        """GET /health/agents — agent health check."""
        from ohm.methods import query_agent_health

        result = query_agent_health(self.current_store.read_conn)
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
        result = detect_contradictions(self.current_store.read_conn, confidence_threshold=conf_thresh)
        self._json_response(200, result)

    def _get_anomalies(self, path: str, qs: dict) -> None:
        """GET /anomalies — detect anomalies."""
        from ohm.methods import detect_anomalies

        sigma = float(qs.get("sigma", [2.0])[0])
        layer = qs.get("layer", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = detect_anomalies(self.current_store.read_conn, sigma_threshold=sigma, layer=layer, limit=limit)
        self._json_response(200, result)

    def _get_aggregate(self, path: str, qs: dict) -> None:
        """GET /aggregate/<id> — aggregate observations."""
        node_id = path[11:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        method = qs.get("method", ["weighted"])[0]
        from ohm.methods import aggregate_observations

        result = aggregate_observations(self.current_store.read_conn, node_id, method=method)
        self._json_response(200, result)

    def _get_provenance(self, path: str, qs: dict) -> None:
        """GET /provenance/<id> — provenance trace."""
        node_id = path[12:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        max_depth = int(qs.get("depth", [10])[0])
        from ohm.queries import query_provenance

        result = query_provenance(self.current_store.read_conn, node_id, max_depth=max_depth)
        self._json_response(200, result)

    def _get_stale(self, path: str, qs: dict) -> None:
        """GET /stale — list stale edges."""
        from ohm.queries import query_stale_edges

        threshold = float(qs.get("threshold", [0.1])[0])
        result = query_stale_edges(self.current_store.read_conn, stale_threshold=threshold)
        self._json_response(200, result)

    def _get_decay(self, path: str, qs: dict) -> None:
        """GET /decay — apply confidence decay."""
        self._require_write_auth()
        from ohm.queries import apply_confidence_decay

        threshold = float(qs.get("threshold", [0.1])[0])
        layer = qs.get("layer", [None])[0]
        dry_run = qs.get("dry_run", ["false"])[0].lower() == "true"
        result = apply_confidence_decay(
            self.current_store.read_conn,
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
            self.current_store.read_conn,
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
        result = detect_near_duplicates(self.current_store.read_conn, similarity_threshold=threshold)
        self._json_response(200, result)

    def _get_calibration(self, path: str, qs: dict) -> None:
        """GET /calibration/<agent> — confidence calibration."""
        agent_name = path[13:]
        from ohm.validation import validate_identifier

        agent_name = validate_identifier(agent_name, name="agent_name")
        from ohm.methods import compute_confidence_calibration

        result = compute_confidence_calibration(self.current_store.read_conn, agent_name)
        self._json_response(200, result)

    def _get_orphans(self, path: str, qs: dict) -> None:
        """GET /orphans — find disconnected nodes."""
        from ohm.methods import find_orphans

        node_type = qs.get("type", [None])[0]
        exclude_system = qs.get("exclude_system", ["true"])[0].lower() == "true"
        limit = int(qs.get("limit", [50])[0])
        result = find_orphans(self.current_store.read_conn, node_type=node_type, exclude_system=exclude_system, limit=limit)
        self._json_response(200, result)

    def _get_islands(self, path: str, qs: dict) -> None:
        """GET /islands — find disconnected components in the graph.

        Islands are clusters of nodes connected by edges but isolated
        from the main graph. Each island represents a knowledge domain
        that needs bridging to the rest of the graph.

        Query params:
            exclude_fragments: Exclude L0 fragments (default: true)
            min_size: Minimum island size to include (default: 2, 1=include orphans)
            max_islands: Maximum number of islands to return (default: 20)
            layer: Filter edges by layer (e.g., 'L3')
        """
        from ohm.methods import find_islands

        exclude_fragments = qs.get("exclude_fragments", ["true"])[0].lower() == "true"
        min_size = int(qs.get("min_size", [2])[0])
        max_islands = int(qs.get("max_islands", [20])[0])
        layer = qs.get("layer", [None])[0]
        result = find_islands(
            self.current_store.read_conn,
            exclude_fragments=exclude_fragments,
            min_size=min_size,
            max_islands=max_islands,
            layer=layer,
        )
        self._json_response(200, result)

    def _get_hubs(self, path: str, qs: dict) -> None:
        """GET /hubs — find most-connected nodes."""
        from ohm.methods import find_hubs

        node_type = qs.get("type", [None])[0]
        min_connections = int(qs.get("min_connections", [3])[0])
        limit = int(qs.get("limit", [20])[0])
        result = find_hubs(self.current_store.read_conn, node_type=node_type, min_connections=min_connections, limit=limit)
        self._json_response(200, result)

    def _get_dead_ends(self, path: str, qs: dict) -> None:
        """GET /dead_ends — find sink nodes."""
        from ohm.methods import find_dead_ends

        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [50])[0])
        result = find_dead_ends(self.current_store.read_conn, node_type=node_type, limit=limit)
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
            self.current_store.read_conn,
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
        result = compute_communities(self.current_store.read_conn, edge_types=edge_types, layer=layer)
        self._json_response(200, result)

    def _get_bridges(self, path: str, qs: dict) -> None:
        """GET /bridges — find bridge edges and articulation points."""
        from ohm.methods import find_bridges

        edge_types_raw = qs.get("edge_types", [None])[0]
        edge_types = edge_types_raw.split(",") if edge_types_raw else None
        layer = qs.get("layer", [None])[0]
        result = find_bridges(self.current_store.read_conn, edge_types=edge_types, layer=layer)
        self._json_response(200, result)

    def _get_granger(self, path: str, qs: dict) -> None:
        """GET /granger — Granger causality test between two nodes."""
        from_node = qs.get("from", [None])[0]
        to_node = qs.get("to", [None])[0]
        if not from_node or not to_node:
            self._json_response(400, {"error": "missing_parameter", "message": "?from=node_id&to=node_id required"})
            return
        from ohm.validation import validate_identifier
        from ohm.exceptions import ValidationError, OHMError

        try:
            from_node = validate_identifier(from_node, name="from")
            to_node = validate_identifier(to_node, name="to")
        except ValidationError as e:
            self._json_response(400, {"error": "validation_error", "message": str(e)})
            return

        try:
            max_lag = int(qs.get("max_lag", [3])[0])
            min_obs = int(qs.get("min_observations", [5])[0])
        except (ValueError, TypeError) as e:
            self._json_response(400, {"error": "invalid_parameter", "message": f"max_lag and min_observations must be integers: {e}"})
            return

        from ohm.methods import granger_causality

        try:
            result = granger_causality(self.current_store.read_conn, from_node, to_node, max_lag=max_lag, min_observations=min_obs)
        except OHMError as e:
            self._json_response(e.exit_code, {"error": "ohm_error", "message": str(e), "correlation_id": getattr(e, "correlation_id", None)})
            return
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Granger causality computation failed: {e}"})
            return

        self._json_response(200, result)

    def _get_edge_stability(self, path: str, qs: dict) -> None:
        """GET /edge_stability — compute edge stability scores across time windows."""
        edge_types_raw = qs.get("edge_types", [None])[0]
        edge_types = edge_types_raw.split(",") if edge_types_raw else None
        layer = qs.get("layer", [None])[0]
        try:
            window_days = int(qs.get("window_days", [7])[0])
            min_windows = int(qs.get("min_windows", [3])[0])
        except (ValueError, TypeError) as e:
            self._json_response(400, {"error": "invalid_parameter", "message": f"window_days and min_windows must be integers: {e}"})
            return
        from ohm.methods import compute_edge_stability

        try:
            result = compute_edge_stability(self.current_store.read_conn, edge_types=edge_types, layer=layer, window_days=window_days, min_windows=min_windows)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Edge stability computation failed: {e}"})
            return
        self._json_response(200, result)

    def _get_trajectory(self, path: str, qs: dict) -> None:
        """GET /trajectory/{node_id} — observation time-series analysis (OHM-vj3i)."""
        from ohm.methods import compute_trajectory

        node_id = path.strip("/")
        if "/" in node_id:
            node_id = node_id.split("/")[-1]
        if not node_id or node_id == "trajectory":
            self._json_response(400, {"error": "missing_parameter", "message": "/trajectory/{node_id} required"})
            return
        from ohm.validation import validate_identifier
        node_id = validate_identifier(node_id, name="node_id")

        since = qs.get("since", [None])[0]
        try:
            min_obs = int(qs.get("min_observations", [3])[0])
        except (ValueError, TypeError):
            min_obs = 3

        try:
            result = compute_trajectory(self.current_store.read_conn, node_id, since=since, min_observations=min_obs)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Trajectory computation failed: {e}"})
            return
        self._json_response(200, result)

    def _get_doctor(self, path: str, qs: dict) -> None:
        """GET /doctor — graph health diagnostics with remediation (OHM-6lvk)."""
        from ohm.methods import graph_doctor

        try:
            result = graph_doctor(self.current_store.read_conn)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Doctor check failed: {e}"})
            return
        self._json_response(200, result)

    def _get_gap(self, path: str, qs: dict) -> None:
        """GET /gap/{node_id} — gap analysis for a node (OHM-tnwa)."""
        node_id = path.strip("/")
        if "/" in node_id:
            node_id = node_id.split("/")[-1]
        if not node_id or node_id == "gap":
            self._json_response(400, {"error": "missing_parameter", "message": "/gap/{node_id} required"})
            return
        from ohm.validation import validate_identifier
        node_id = validate_identifier(node_id, name="node_id")
        from ohm.methods import compute_gap_analysis

        try:
            result = compute_gap_analysis(self.current_store.read_conn, node_id)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Gap analysis failed: {e}"})
            return
        self._json_response(200, result)

    def _get_suggest(self, path: str, qs: dict) -> None:
        """GET /suggest — suggest connections."""
        from ohm.methods import suggest_connections

        method = qs.get("method", ["shared_provenance"])[0]
        try:
            min_shared = int(qs.get("min_shared", [2])[0])
            limit = int(qs.get("limit", [20])[0])
        except (ValueError, TypeError) as e:
            self._json_response(400, {"error": "invalid_parameter", "message": f"min_shared and limit must be integers: {e}"})
            return
        try:
            result = suggest_connections(self.current_store.read_conn, method=method, min_shared=min_shared, limit=limit)
        except Exception as e:
            self._json_response(500, {"error": "internal_error", "message": f"Suggest computation failed: {e}"})
            return
        self._json_response(200, result)

    def _get_graph_stats(self, path: str, qs: dict) -> None:
        """GET /graph/stats — extended graph statistics."""
        from ohm.methods import graph_stats

        result = graph_stats(self.current_store.read_conn)
        self._json_response(200, result)

    def _get_lint(self, path: str, qs: dict) -> None:
        """GET /lint — lint graph against contract."""
        from ohm.contract import ContractConfig, lint_graph

        node_type_filter = qs.get("node_types", [None])[0]
        node_types = node_type_filter.split(",") if node_type_filter else None
        limit = int(qs.get("limit", ["1000"])[0])
        contract = ContractConfig()
        result = lint_graph(self.current_store.read_conn, contract, limit=limit, node_types=node_types)
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

        result = query_source_reliability(self.current_store.read_conn, source_agent)
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

        result = query_source_reliability(self.current_store.read_conn, source_agent)
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

    def _get_welcome(self, path: str, qs: dict) -> None:
        """GET /welcome?agent=NAME — Welcome packet for new/returning agents.

        Gives agents a concise orientation to the graph:
        - Graph overview (size, density, top types)
        - Agent's own footprint (nodes created, edges created, last activity)
        - Suggested connections (orphaned nodes, islands, shared tags)
        - Recent activity (what's new since last visit)
        """
        agent = qs.get("agent", [None])[0]
        since = qs.get("since", [None])[0]

        import json as _json
        from ohm.graph.methods import find_islands

        conn = self.current_store.read_conn

        # ── 1. Graph overview ────────────────────────────────────────
        node_count = conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'"
        ).fetchone()[0]
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL"
        ).fetchone()[0]
        top_types = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM ohm_nodes "
            "WHERE deleted_at IS NULL AND type != 'fragment' "
            "GROUP BY type ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        top_edge_types = conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM ohm_edges "
            "WHERE deleted_at IS NULL "
            "GROUP BY edge_type ORDER BY cnt DESC LIMIT 10"
        ).fetchall()

        overview = {
            "total_nodes": node_count,
            "total_edges": edge_count,
            "density": round(edge_count / max(node_count * (node_count - 1) / 2, 1), 6),
            "top_node_types": [{"type": t, "count": c} for t, c in top_types],
            "top_edge_types": [{"type": t, "count": c} for t, c in top_edge_types],
        }

        # ── 2. Agent's footprint ───────────────────────────────────
        agent_info = {
            "nodes_created": 0,
            "edges_created": 0,
            "observations_made": 0,
            "last_activity": None,
            "orphan_count": 0,
        }

        if agent:
            n_created = conn.execute(
                "SELECT COUNT(*) FROM ohm_nodes "
            "WHERE created_by = ? AND deleted_at IS NULL AND type != 'fragment'",
                [agent],
            ).fetchone()[0]
            e_created = conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE created_by = ? AND deleted_at IS NULL",
                [agent],
            ).fetchone()[0]
            o_made = conn.execute(
                "SELECT COUNT(*) FROM ohm_observations WHERE created_by = ?",
                [agent],
            ).fetchone()[0]
            last_act = conn.execute(
                "SELECT MAX(la) FROM ("
                "SELECT created_at AS la FROM ohm_nodes WHERE created_by = ? UNION ALL "
                "SELECT created_at AS la FROM ohm_edges WHERE created_by = ? UNION ALL "
                "SELECT created_at AS la FROM ohm_observations WHERE created_by = ?"
                ")",
                [agent, agent, agent],
            ).fetchone()[0]
            # Agent's orphans
            agent_orphans = conn.execute(
                "SELECT COUNT(*) FROM ohm_nodes n "
                "WHERE n.created_by = ? AND n.deleted_at IS NULL AND n.type != 'fragment' "
                "AND n.id NOT IN (SELECT from_node FROM ohm_edges WHERE deleted_at IS NULL) "
                "AND n.id NOT IN (SELECT to_node FROM ohm_edges WHERE deleted_at IS NULL)",
                [agent],
            ).fetchone()[0]

            agent_info = {
                "agent": agent,
                "nodes_created": n_created,
                "edges_created": e_created,
                "observations_made": o_made,
                "last_activity": last_act.isoformat() if last_act else None,
                "orphan_count": agent_orphans,
            }

        # ── 3. Suggested connections ────────────────────────────────
        # Islands (disconnected clusters)
        islands = find_islands(conn, min_size=2, max_islands=5)
        suggestions = {
            "islands": [
                {
                    "id": i["id"],
                    "size": i["size"],
                    "sample_nodes": i["nodes"][:3],
                    "internal_edges": i["internal_edges"],
                }
                for i in islands.get("islands", [])
                if i["size"] < islands.get("main_graph_size", 0)  # Skip mainland
            ],
            "orphan_count": islands.get("orphan_count", 0),
            "agent_orphans": agent_info["orphan_count"],
        }

        # If agent has orphans, suggest connecting them
        if agent and agent_info["orphan_count"] > 0:
            agent_orphan_list = conn.execute(
                "SELECT n.id, n.label, n.type FROM ohm_nodes n "
                "WHERE n.created_by = ? AND n.deleted_at IS NULL AND n.type != 'fragment' "
                "AND n.id NOT IN (SELECT from_node FROM ohm_edges WHERE deleted_at IS NULL) "
                "AND n.id NOT IN (SELECT to_node FROM ohm_edges WHERE deleted_at IS NULL) "
                "ORDER BY n.confidence DESC NULLS LAST LIMIT 5",
                [agent],
            ).fetchall()
            suggestions["your_orphans"] = [
                {"id": r[0], "label": r[1], "type": r[2]} for r in agent_orphan_list
            ]

        # ── 4. Recent activity ──────────────────────────────────────
        recent = {"nodes": [], "edges": []}
        since_clause = ""
        params = []
        if since:
            since_clause = "AND created_at > ?"
            params.append(since)

        recent_nodes = conn.execute(
            f"SELECT id, label, type, created_by, created_at FROM ohm_nodes "
            f"WHERE deleted_at IS NULL AND type != 'fragment' {since_clause} "
            f"ORDER BY created_at DESC LIMIT 5",
            params,
        ).fetchall()
        recent["nodes"] = [
            {"id": r[0], "label": r[1], "type": r[2], "created_by": r[3], "created_at": str(r[4])}
            for r in recent_nodes
        ]

        recent_edges = conn.execute(
            f"SELECT id, from_node, to_node, edge_type, created_by, created_at FROM ohm_edges "
            f"WHERE deleted_at IS NULL {since_clause} "
            f"ORDER BY created_at DESC LIMIT 5",
            params,
        ).fetchall()
        recent["edges"] = [
            {"id": r[0], "from": r[1], "to": r[2], "type": r[3], "created_by": r[4], "created_at": str(r[5])}
            for r in recent_edges
        ]

        # ── 5. Quick reference ─────────────────────────────────────
        quick_ref = {
            "create_node": "POST /node {id, label, type, content, tags, connects_to}",
            "create_edge": "POST /edge {from, to, type, layer, confidence}",
            "scratch": "POST /scratch {content, connects_to, tags}",
            "search": "GET /search?q=QUERY or GET /semantic_search?q=QUERY",
            "neighborhood": "GET /neighborhood/ID?depth=2",
            "islands": "GET /islands?min_size=2",
            "orphans": "GET /orphans",
            "suggest": "GET /suggest?method=shared_tags&min_shared=2",
            "schema": "GET /schema — Full usage guide with all endpoints",
        }

        self._json_response(200, {
            "welcome": f"Hello {agent or 'agent'}, the knowledge graph has {node_count} nodes and {edge_count} edges.",
            "overview": overview,
            "your_footprint": agent_info,
            "suggestions": suggestions,
            "recent_activity": recent,
            "quick_reference": quick_ref,
        })
    def _get_contributions(self, path: str, qs: dict) -> None:
        """GET /contributions?agent=NAME — what did this agent create?

        OHM-tr71.10: Shows all nodes, edges, and observations created by
        a specific agent. Helps agents see their own footprint.
        """
        from ohm.exceptions import ValidationError

        agent = qs.get("agent", [None])[0]
        if not agent:
            raise ValidationError("?agent=<agent_name> is required")

        limit = int(qs.get("limit", [50])[0])
        node_type = qs.get("type", [None])[0]
        since = qs.get("since", [None])[0]

        conn = self.current_store.read_conn

        # Nodes
        conditions = ["created_by = ?", "deleted_at IS NULL", "type != 'fragment'"]
        params = [agent]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        if since:
            conditions.append("created_at > ?::TIMESTAMP")
            params.append(since)
        params.append(limit)

        nodes = conn.execute(
            f"SELECT id, label, type, confidence, created_at FROM ohm_nodes "
            f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

        # Edges
        edge_params = [agent]
        edge_since = ""
        if since:
            edge_since = "AND created_at > ?::TIMESTAMP"
            edge_params.append(since)
        edge_params.append(limit)

        edges = conn.execute(
            f"SELECT id, from_node, to_node, edge_type, layer, confidence, created_at FROM ohm_edges "
            f"WHERE created_by = ? AND deleted_at IS NULL {edge_since} "
            f"ORDER BY created_at DESC LIMIT ?",
            edge_params,
        ).fetchall()

        # Observations
        obs_params = [agent]
        obs_since = ""
        if since:
            obs_since = "AND created_at > ?::TIMESTAMP"
            obs_params.append(since)
        obs_params.append(limit)

        observations = conn.execute(
            f"SELECT id, node_id, type, value, created_at FROM ohm_observations "
            f"WHERE created_by = ? {obs_since} "
            f"ORDER BY created_at DESC LIMIT ?",
            obs_params,
        ).fetchall()

        # Stats
        node_count = conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE created_by = ? AND deleted_at IS NULL AND type != 'fragment'",
            [agent],
        ).fetchone()[0]
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE created_by = ? AND deleted_at IS NULL",
            [agent],
        ).fetchone()[0]
        obs_count = conn.execute(
            "SELECT COUNT(*) FROM ohm_observations WHERE created_by = ?",
            [agent],
        ).fetchone()[0]

        self._json_response(200, {
            "agent": agent,
            "stats": {"nodes": node_count, "edges": edge_count, "observations": obs_count},
            "nodes": [
                {"id": r[0], "label": r[1], "type": r[2], "confidence": r[3], "created_at": str(r[4])}
                for r in nodes
            ],
            "edges": [
                {"id": r[0], "from": r[1], "to": r[2], "type": r[3], "layer": r[4], "confidence": r[5], "created_at": str(r[6])}
                for r in edges
            ],
            "observations": [
                {"id": r[0], "node_id": r[1], "type": r[2], "value": r[3], "created_at": str(r[4])}
                for r in observations
            ],
        })

    def _get_changes(self, path: str, qs: dict) -> None:
        """GET /changes?since=ISO8601 — what's new since a timestamp.

        OHM-tr71.11: Returns all nodes, edges, and observations created
        after the given timestamp. Helps agents catch up on what they missed.
        """
        from ohm.exceptions import ValidationError

        since = qs.get("since", [None])[0]
        if not since:
            raise ValidationError("?since=ISO8601_TIMESTAMP is required (e.g., 2026-06-06T00:00:00)")

        limit = int(qs.get("limit", [100])[0])
        agent = qs.get("agent", [None])[0]
        node_type = qs.get("type", [None])[0]

        conn = self.current_store.read_conn

        # Nodes
        node_conditions = ["deleted_at IS NULL", "type != 'fragment'", "created_at > ?::TIMESTAMP"]
        params = [since]
        if agent:
            node_conditions.append("created_by = ?")
            params.append(agent)
        if node_type:
            node_conditions.append("type = ?")
            params.append(node_type)
        params.append(limit)

        nodes = conn.execute(
            f"SELECT id, label, type, created_by, confidence, created_at FROM ohm_nodes "
            f"WHERE {' AND '.join(node_conditions)} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()

        # Edges
        edge_params = [since]
        edge_agent = ""
        if agent:
            edge_agent = "AND created_by = ?"
            edge_params.append(agent)
        edge_params.append(limit)

        edges = conn.execute(
            f"SELECT id, from_node, to_node, edge_type, layer, confidence, created_by, created_at FROM ohm_edges "
            f"WHERE deleted_at IS NULL AND created_at > ?::TIMESTAMP {edge_agent} "
            f"ORDER BY created_at DESC LIMIT ?",
            edge_params,
        ).fetchall()

        # Count totals (not limited)
        count_params = [since]
        count_agent = ""
        if agent:
            count_agent = "AND created_by = ?"
            count_params.append(agent)
        node_total = conn.execute(
            f"SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment' "
            f"AND created_at > ?::TIMESTAMP {count_agent}",
            count_params,
        ).fetchone()[0]
        edge_total = conn.execute(
            f"SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL "
            f"AND created_at > ?::TIMESTAMP {count_agent}",
            count_params,
        ).fetchone()[0]

        self._json_response(200, {
            "since": since,
            "node_total": node_total,
            "edge_total": edge_total,
            "nodes": [
                {"id": r[0], "label": r[1], "type": r[2], "created_by": r[3], "confidence": r[4], "created_at": str(r[5])}
                for r in nodes
            ],
            "edges": [
                {"id": r[0], "from": r[1], "to": r[2], "type": r[3], "layer": r[4], "confidence": r[5], "created_by": r[6], "created_at": str(r[7])}
                for r in edges
            ],
        })
