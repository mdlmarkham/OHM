"""Graph handler mixin — node/edge CRUD, search, observations, webhooks, and agent state."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin, _resolve_type_field

import logging
import time
from typing import Any

from ohm.server import suggestions as _suggestions_module

logger = logging.getLogger(__name__)

from ohm.framework.exceptions import NodeNotFoundError, AuthenticationError
from ohm.server import server as _server_module
from ohm.server.nudges import generate_nudges, enrich_response

class GraphHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for graph CRUD endpoints (OHM-hpxa).

    Methods migrated from server.py: 38 handler methods covering node/edge
    read/write/delete, search, observations, agent registration, webhooks,
    and batch operations.
    """

    _challenge_ratio_cache: float = 0.0
    _challenge_ratio_cache_time: float = 0.0

    def _get_challenge_ratio(self) -> float:
        """Get the current graph challenge ratio, cached for 5 minutes."""
        import time

        now = time.time()
        if now - self._challenge_ratio_cache_time > 300:  # 5-minute cache
            try:
                row = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE edge_type = 'CHALLENGED_BY' AND deleted_at IS NULL").fetchone()
                challenged = row[0] if row else 0
                row2 = self.current_store.conn.execute("SELECT COUNT(*) FROM edges WHERE layer = 'L3' AND deleted_at IS NULL").fetchone()
                total_l3 = row2[0] if row2 else 1
                ratio = challenged / max(total_l3, 1)
                GraphHandlerMixin._challenge_ratio_cache = ratio
                GraphHandlerMixin._challenge_ratio_cache_time = now
            except Exception:
                ratio = GraphHandlerMixin._challenge_ratio_cache
        else:
            ratio = GraphHandlerMixin._challenge_ratio_cache
        return ratio

    def _get_listen(self, path: str, qs: dict) -> None:
        """GET /listen — poll change feed since last sync."""
        from ohm.exceptions import AuthenticationError
        from datetime import datetime, timedelta, timezone

        agent = self._authenticate()
        if agent is None:
            if self.no_auth or not self.tokens:
                agent = "ohm"
            elif self.require_read_auth:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required — provide Bearer token"
                )
            else:
                agent = "ohm"
        since = qs.get("since", [None])[0]
        agent_name = qs.get("agent", [agent or "ohm"])[0]
        enrich = qs.get("enrich", ["false"])[0].lower() == "true"
        if not since:
            state = self.current_store.get_agent_state(agent_name)
            if state and state.get("last_sync"):
                since = state["last_sync"]
                if isinstance(since, datetime):
                    since = since.isoformat()
            else:
                since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        from ohm.queries import query_change_feed

        results = query_change_feed(self.current_store.conn, since=since, agent_name=agent_name, enrich=enrich)
        self._json_response(200, results)

    def _get_search(self, path: str, qs: dict) -> None:
        """GET /search — text search over nodes.

        OHM-a5rz.7: supports ``?since=`` and ``?until=`` ISO 8601 timestamp
        filters to constrain search by ``created_at`` range.

        OHM-a5rz.18: L0 fragments are excluded by default.
        Pass ``?include_l0=true`` to include fragment-type nodes.

        OHM-842: supports ``?tags=`` for AND-semantics tag filtering.
        Pass multiple ``?tags=`` params — all must be present.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        node_type = qs.get("type", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        since = qs.get("since", [None])[0]
        until = qs.get("until", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        limit = int(qs.get("limit", [20])[0])
        tags = qs.get("tags", [])
        if not query_text:
            raise ValidationError("Search requires ?q=QUERY")
        conditions = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
        params = [f"%{query_text}%", f"%{query_text}%"]
        if node_type:
            conditions.append("type = ?")
            params.append(node_type)
        elif not include_l0:
            # OHM-a5rz.18: exclude L0 fragments from default search results
            conditions.append("type != 'fragment'")
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if since:
            conditions.append("created_at >= ?::TIMESTAMP")
            params.append(since)
        if until:
            conditions.append("created_at <= ?::TIMESTAMP")
            params.append(until)
        # OHM-842: AND-semantics tag filtering — each tag must be present
        for tag in tags:
            conditions.append("json_contains(tags, ?)")
            params.append(f'"{tag}"')
        # OHM-oqyc: enforce read scope at SQL level
        from ohm.server.boundary import apply_read_scope_filters

        agent = getattr(self, "_current_agent", "ohm")
        scope_conds, scope_params = apply_read_scope_filters(self.current_store.conn, agent)
        conditions.extend(scope_conds)
        params.extend(scope_params)
        params.append(limit)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
        results = self.current_store.execute(sql, params)

        # OHM-tr71.8: Automatic semantic fallback on empty text search
        # When text search returns 0 results, try semantic search automatically.
        # OHM-738: pass node_type through to fallbacks so a typed query can
        # still benefit from semantic/fuzzy matching instead of returning 0.
        # OHM-842: skip fallbacks when tags are specified — fallbacks don't
        # support tag filtering and would bypass the user's explicit constraint.
        if not results and not tags:
            try:
                from ohm.graph.queries import semantic_search

                semantic_results = semantic_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    node_type=node_type,
                    include_l0=include_l0,
                )
                if semantic_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("node_id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": round(r.get("distance", 1.0), 4),
                                    "match_method": "semantic",
                                }
                                for r in semantic_results
                            ],
                            "count": len(semantic_results),
                            "fallback": "semantic",
                            "tip": f"No exact text matches for '{query_text}'. Showing semantic matches instead. Use /semantic_search?q={query_text} for more options.",
                        },
                    )
                    return
            except (ValueError, ImportError, Exception) as e:
                logger.debug(f"Semantic fallback failed: {e}")

            # OHM-tr71.9: Fuzzy matching fallback — try DuckDB jaro_winkler_similarity
            try:
                from ohm.graph.queries import fuzzy_search as _fuzzy_search

                fuzzy_results = _fuzzy_search(
                    self.current_store.conn,
                    query=query_text,
                    limit=limit,
                    include_l0=include_l0,
                )
                if node_type:
                    fuzzy_results = [r for r in fuzzy_results if r.get("type") == node_type]
                if fuzzy_results:
                    self._json_response(
                        200,
                        {
                            "results": [
                                {
                                    "id": r.get("id", ""),
                                    "label": r.get("label", ""),
                                    "type": r.get("type", ""),
                                    "distance": r.get("distance", 0.0),
                                    "match_method": r.get("match_type", "fuzzy"),
                                }
                                for r in fuzzy_results
                            ],
                            "count": len(fuzzy_results),
                            "fallback": "fuzzy",
                            "tip": f"No exact matches for '{query_text}'. Showing fuzzy label matches instead.",
                        },
                    )
                    return
            except Exception as e:
                logger.debug(f"Fuzzy fallback failed: {e}")

            self._json_response(
                200,
                {
                    "results": [],
                    "count": 0,
                    "tip": f"No results for '{query_text}' via text, semantic, or fuzzy search. Try a different query.",
                },
            )
            return

        self._json_response(200, results)

    def _get_semantic_search(self, path: str, qs: dict) -> None:
        """GET /semantic_search — vector similarity search.

        OHM-a5rz.20: L0 fragments excluded by default. Pass ``?include_l0=true`` to include.
        OHM-xuf4: Pass ``?membership_weight=0.3`` to blend HD Hamming similarity
        alongside cosine similarity. Results then carry ``cosine_similarity``,
        ``hd_similarity``, and ``blended_score`` fields.
        """
        from ohm.exceptions import ValidationError

        query_text = qs.get("q", [""])[0]
        if not query_text:
            raise ValidationError("Semantic search requires ?q=QUERY")
        node_type = qs.get("type", [None])[0]
        limit = int(qs.get("limit", [10])[0])
        min_confidence = qs.get("min_confidence", [None])[0]
        include_l0 = qs.get("include_l0", ["false"])[0].lower() in ("true", "1", "yes")
        membership_weight_raw = qs.get("membership_weight", [None])[0]
        membership_weight: float | None = None
        if membership_weight_raw is not None:
            try:
                membership_weight = float(membership_weight_raw)
            except ValueError:
                raise ValidationError("?membership_weight must be a number in [0, 1]")
            if not 0.0 <= membership_weight <= 1.0:
                raise ValidationError("?membership_weight must be in [0, 1]")
        if min_confidence is not None:
            try:
                min_confidence = float(min_confidence)
            except ValueError:
                raise ValidationError("?min_confidence must be a number")
        try:
            from ohm.queries import semantic_search

            results = semantic_search(
                self.current_store.conn,
                query=query_text,
                limit=limit,
                node_type=node_type,
                min_confidence=min_confidence,
                include_l0=include_l0,
                membership_weight=membership_weight,
            )
            # OHM-oqyc: post-filter results by read scope
            from ohm.server.boundary import filter_results_by_read_scope

            agent = getattr(self, "_current_agent", "ohm")
            results = filter_results_by_read_scope(
                self.current_store.conn,
                agent,
                results,
                id_field="node_id",
            )
            self._json_response(200, {"results": results, "count": len(results)})
        except ValueError as e:
            self._json_response(
                503,
                {
                    "error": "service_unavailable",
                    "message": str(e),
                },
            )

    def _get_observations(self, path: str, qs: dict) -> None:
        """GET /observations — list observations with filtering."""
        obs_type = qs.get("type", [None])[0]
        source = qs.get("source", [None])[0]
        node_id = qs.get("node_id", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL"]
        params = []
        if obs_type:
            conditions.append("type = ?")
            params.append(obs_type)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if node_id:
            conditions.append("node_id = ?")
            params.append(node_id)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_observations WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_observations WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(200, {"observations": results, "total": total, "limit": limit, "offset": offset})

    def _get_observation(self, path: str, qs: dict) -> None:
        """GET /observation/{id} or /observation/{id}/confidence (OHM-60pd).

        Without the ``/confidence`` suffix: returns the raw observation record.
        With ``/confidence``: returns effective confidence + decay metadata:

            {
              "observation_id": "...",
              "effective_confidence": 0.42,
              "weibull_shape": 1.0,
              "half_life_days": 7.0,
              "decay_function": "weibull",
              "decay_profile": "perishable",
              "age_days": 3.5,
              "evaluated_at": "2026-06-28T..."
            }

        Query params for /confidence:
            at: ISO 8601 timestamp to evaluate at (default: now).
        """
        from datetime import datetime, timezone
        from ohm.graph.decay import confidence_at, decay_profile, default_weibull_shape
        from ohm.exceptions import NodeNotFoundError, ValidationError
        from ohm.validation import validate_timestamp

        prefix = "/observation/"
        if not path.startswith(prefix):
            raise ValidationError("Invalid observation path")
        remainder = path[len(prefix) :]

        if "/" in remainder:
            obs_id, action = remainder.split("/", 1)
        else:
            obs_id, action = remainder, ""

        if not obs_id:
            raise ValidationError("Missing observation id")

        conn = self.current_store.read_conn
        row = conn.execute(
            "SELECT * FROM ohm_observations WHERE id = ? AND deleted_at IS NULL",
            [obs_id],
        ).fetchone()
        if row is None:
            raise NodeNotFoundError(f"Observation {obs_id} not found")
        cols = [d[0] for d in conn.description]
        obs = dict(zip(cols, row))

        if action == "confidence":
            at_str = qs.get("at", [None])[0]
            if at_str:
                at_str = validate_timestamp(at_str)
                t = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            else:
                t = datetime.now(timezone.utc)

            eff = confidence_at(obs, t=t)
            shape = obs.get("weibull_shape")
            if shape is None:
                shape = default_weibull_shape(obs.get("type", "_default"))
            hl = obs.get("half_life_days")
            fn = "weibull" if shape is not None else "exponential"

            # Compute age_days for the response
            anchor = obs.get("valid_from") or obs.get("created_at")
            age_days = None
            if anchor is not None:
                if isinstance(anchor, str):
                    anchor = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (t - anchor).total_seconds() / 86400.0)

            self._json_response(
                200,
                {
                    "observation_id": obs_id,
                    "effective_confidence": round(eff, 6),
                    "weibull_shape": shape,
                    "half_life_days": hl,
                    "decay_function": fn,
                    "decay_profile": decay_profile(hl, shape),
                    "age_days": round(age_days, 4) if age_days is not None else None,
                    "evaluated_at": t.isoformat(),
                },
            )
            return

        if action:
            raise ValidationError(f"Unknown observation action: {action!r}")

        # Enrich with effective_confidence + decay_profile for convenience
        from ohm.graph.decay import confidence_at as _ca, decay_profile as _dp

        obs["effective_confidence"] = round(_ca(obs), 6)
        obs["decay_profile"] = _dp(obs.get("half_life_days"), obs.get("weibull_shape"))
        self._json_response(200, obs)

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

    def _post_nudge_evaluate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /nudge/evaluate — evaluate A/B test results for a nudge type (OHM-847)."""
        from ohm.server.nudge_optimization import evaluate_nudge_variants

        nudge_type = body.get("nudge_type")
        if not nudge_type:
            self._json_response(422, {"error": "nudge_type is required"})
            return

        result = evaluate_nudge_variants(
            self.current_store.read_conn,
            nudge_type=nudge_type,
            significance_threshold=float(body.get("significance_threshold", 0.05)),
            min_exposures=int(body.get("min_exposures", 30)),
        )
        self._json_response(200, result)

    def _post_nudge_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /nudge/promote — promote a winning variant (OHM-847)."""
        from ohm.server.nudge_optimization import promote_nudge_variant

        nudge_type = body.get("nudge_type")
        variant_id = body.get("variant_id")
        if not nudge_type or not variant_id:
            self._json_response(422, {"error": "nudge_type and variant_id are required"})
            return

        result = promote_nudge_variant(
            self.current_store.conn,
            nudge_type=nudge_type,
            variant_id=variant_id,
        )
        self._json_response(200, result)

    def _post_nudge_demote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /nudge/demote — demote/reset the default variant (OHM-847)."""
        from ohm.server.nudge_optimization import demote_nudge_variant

        nudge_type = body.get("nudge_type")
        if not nudge_type:
            self._json_response(422, {"error": "nudge_type is required"})
            return

        result = demote_nudge_variant(
            self.current_store.conn,
            nudge_type=nudge_type,
        )
        self._json_response(200, result)

    def _post_skill_maintenance_run(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /admin/skill-maintenance/run — run one skill maintenance round (OHM-854).

        Body: {dry_run?: bool}

        Detects signals (low nudge acceptance), generates candidate skill
        edits, evaluates them, and promotes/demotes as appropriate.
        """
        from pathlib import Path

        from ohm.mcp.skill_maintenance import run_skill_maintenance_round

        default_skills_dir = Path(__file__).resolve().parents[2] / "skills"
        candidates_dir = Path(body.get("candidates_dir", str(default_skills_dir / ".candidates")))

        dry_run = bool(body.get("dry_run", False))

        result = run_skill_maintenance_round(
            self.current_store.conn,
            default_skills_dir=default_skills_dir,
            candidates_dir=candidates_dir,
            dry_run=dry_run,
        )
        self._json_response(200, result)

    def _post_scratch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /scratch — write an L0 thinking fragment (OHM-a5rz.4).

        Minimal write: just content. Auto-generates id, label (first 80 chars),
        type='fragment'. Extracts URLs from content. Returns 201.
        """
        from ohm.queries import scratch

        content = body.get("content", "").strip()
        if not content:
            self._json_response(400, {"error": "content is required and must be non-empty"})
            return

        try:
            node = scratch(
                self.current_store.conn,
                content=content,
                created_by=agent,
                tags=body.get("tags"),
                connects_to=body.get("connects_to"),
                metadata=body.get("metadata"),
            )
        except ValueError as e:
            self._json_response(400, {"error": str(e)})
            return

        decorations = self._run_post_ingest_hooks(agent, "scratch", node)
        if decorations:
            node["hook_decorations"] = decorations

        # ADR-021: Proactive discoverability — suggestions for scratch
        # OHM-855: isolate suggestion failures from fragment writes
        if _suggestions_module._suggestions_enabled():
            deadline = time.time() + _suggestions_module.SUGGESTION_TIMEOUT_S
            try:
                sugg = _suggestions_module.generate_suggestions(
                    store=self.current_store,
                    node_id=node.get("id", ""),
                    content=content,
                    label=node.get("label"),
                    tags=body.get("tags"),
                    node_type="fragment",
                    has_edges=bool(body.get("connects_to")),
                    deadline=deadline,
                    use_store_conn=True,
                )
                node["suggestions"] = sugg
            except Exception as e:
                logger.debug("Edge suggestions failed: %s", e)

        self._json_response(201, node)

    def _post_challenge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /challenge/{id} — challenge an existing edge.

        ADR-025: ``challenge_type`` in the request body is a semantic label
        (e.g. ``empirical``, ``logical``) stored in the ``challenge_type``
        column. The ``edge_type`` is always ``CHALLENGED_BY`` for this
        endpoint — use POST /support/{id} to create SUPPORTS edges.
        """
        edge_id = path[11:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.5)
        semantic_type = body.get("challenge_type", "CHALLENGED_BY")
        result = self.current_store.challenge_edge(
            edge_id,
            reason,
            confidence,
            "CHALLENGED_BY",
            agent_name=agent,
            challenge_type_column=semantic_type,
        )
        if result:
            # OHM-a5rz.15: reflect the challenge back to originating L0 fragments
            try:
                from ohm.graph.queries import reflect_challenge_to_fragments

                reflected = reflect_challenge_to_fragments(
                    self.current_store.conn,
                    edge_id,
                    result.get("id", ""),
                    agent,
                )
                if reflected:
                    result["backflow_fragments"] = [r["fragment_id"] for r in reflected]
            except Exception:
                pass  # backflow is advisory; never block the challenge
            _server_module._trigger_webhooks(
                {
                    "type": "edge.challenged",
                    "agent": agent,
                    "edge": result,
                    "challenge_type": semantic_type,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_support(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /support/{id} — support an existing edge."""
        edge_id = path[9:]
        from ohm.validation import validate_identifier
        from ohm.exceptions import EdgeNotFoundError

        edge_id = validate_identifier(edge_id, name="edge_id")
        reason = body.get("reason", "")
        confidence = body.get("confidence", 0.8)
        result = self.current_store.challenge_edge(edge_id, reason, confidence, "SUPPORTS", agent_name=agent, challenge_type_column="SUPPORTS")
        if result:
            _server_module._trigger_webhooks(
                {
                    "type": "edge.supported",
                    "agent": agent,
                    "edge": result,
                },
                customer_id=self._customer_id,
            )
            self._json_response(201, result)
        else:
            raise EdgeNotFoundError(f"Edge {edge_id} not found")

    def _post_observe(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observe/{id} — record an observation on a node."""
        from ohm.exceptions import NodeNotFoundError, ValidationError

        node_id = path[9:]
        from ohm.validation import validate_identifier

        node_id = validate_identifier(node_id, name="node_id")
        if not self.current_store.get_node(node_id):
            raise NodeNotFoundError(f"Node not found: {node_id}")
        obs_type = _resolve_type_field(body, "obs_type", "type", default="measurement") or "measurement"
        if obs_type not in self.schema_config.observation_types:
            raise ValidationError(f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}")
        scale = body.get("scale")
        if scale is not None:
            from ohm.graph.schema import VALID_OBSERVATION_SCALES

            if scale not in VALID_OBSERVATION_SCALES:
                raise ValidationError(f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}")
            # ADR-025: Normalize binary to probability
            if scale == "binary":
                scale = "probability"
            if scale == "probability":
                value = body.get("value")
                if value is not None and (value < 0.0 or value > 1.0):
                    raise ValidationError(f"Observation value {value} is outside [0, 1] for scale='probability'")
        # ADR-026: Validate compression framework fields
        compression_type = body.get("compression_type")
        if compression_type is not None:
            from ohm.graph.schema import VALID_COMPRESSION_TYPES

            if compression_type not in VALID_COMPRESSION_TYPES:
                raise ValidationError(f"Invalid compression_type '{compression_type}' — must be one of: {', '.join(sorted(VALID_COMPRESSION_TYPES))}")
        compression_degree = body.get("compression_degree")
        if compression_degree is not None and (compression_degree < 0.0 or compression_degree > 1.0):
            raise ValidationError(f"compression_degree {compression_degree} is outside [0, 1]")
        revisability = body.get("revisability")
        if revisability is not None and (revisability < 0.0 or revisability > 1.0):
            raise ValidationError(f"revisability {revisability} is outside [0, 1]")
        beneficiary = body.get("beneficiary")  # List of agent/node IDs
        if beneficiary is not None and not isinstance(beneficiary, list):
            raise ValidationError("beneficiary must be a list of strings")
        result = self.current_store.write_observation(
            node_id=node_id,
            type=obs_type,
            value=body.get("value"),
            baseline=body.get("baseline"),
            sigma=body.get("sigma"),
            source=body.get("source"),
            notes=body.get("notes"),
            source_name=body.get("source_name"),
            source_url=body.get("source_url"),
            scale=scale,
            agent_name=agent,
            half_life_days=body.get("half_life_days"),
            weibull_shape=body.get("weibull_shape"),
            compression_degree=compression_degree,
            compression_type=compression_type,
            beneficiary=beneficiary,
            revisability=revisability,
            idempotency_key=body.get("idempotency_key"),
        )
        _server_module._trigger_webhooks(
            {
                "type": "observation.created",
                "agent": agent,
                "observation": result,
            },
            customer_id=self._customer_id,
        )
        # ADR-017 + ADR-023: Cognitive nudge enrichment with inference delta
        nudges = generate_nudges(
            action="observation",
            node_id=node_id,
            confidence=body.get("value"),
            provenance=body.get("source"),
            source_url=body.get("source_url"),
            store=self.current_store,
            obs_type=_resolve_type_field(body, "obs_type", "type", default="measurement") or "measurement",
            half_life_days=body.get("half_life_days"),
            value=body.get("value"),
        )
        result = enrich_response(result, nudges, store=self.current_store, agent=agent, action="observation", target_id=node_id)
        self._json_response(201, result)

    def _post_nudge_accept(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /nudges/{id}/accept — accept or reject a logged nudge (OHM-jdfq).

        Body: {"helpful": bool, "notes": str?}

        Updates ohm_nudge_log.accepted and accepted_at. The agent must match
        the nudge's recorded agent (unless no agent was recorded, in which
        case the check is skipped). Re-accepting overwrites the prior
        response — last write wins.
        """
        from ohm.exceptions import ValidationError
        from ohm.server.nudges import accept_nudge

        # path is "/nudges/{id}/accept" → strip "/nudges/" and "/accept"
        nudge_id = path[len("/nudges/") :]
        if nudge_id.endswith("/accept"):
            nudge_id = nudge_id[: -len("/accept")]
        if not nudge_id:
            raise ValidationError("Missing nudge id in path")

        helpful = bool(body.get("helpful", True))
        notes = body.get("notes")

        result = accept_nudge(
            self.current_store.conn,
            nudge_id=nudge_id,
            agent=agent,
            helpful=helpful,
            notes=notes,
        )
        self._json_response(
            200,
            {
                "nudge_id": result["id"],
                "nudge_type": result["nudge_type"],
                "accepted": result["accepted"],
                "accepted_at": str(result["accepted_at"]) if result["accepted_at"] else None,
                "agent": result["agent"],
                "target_id": result["target_id"],
                "message": result["message"],
            },
        )

    def _get_nudge_quality(self, path: str, qs: dict) -> None:
        """GET /admin/nudges/quality — aggregate nudge acceptance stats.

        Query params (optional): since (ISO timestamp), agent (filter).
        Returns per-type and per-agent acceptance rates so operators can
        see which nudges are actually helping.
        """
        from ohm.server.nudges import nudge_acceptance_stats

        since = qs.get("since", [None])[0]
        agent_filter = qs.get("agent", [None])[0]
        stats = nudge_acceptance_stats(
            self.current_store.conn,
            since=since,
            agent=agent_filter,
        )
        self._json_response(200, stats)

    def _post_observations(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /observations — bulk observation upload (OHM-0lf)."""
        from ohm.exceptions import ValidationError

        obs_list = body.get("observations", [])
        if not isinstance(obs_list, list):
            raise ValidationError("'observations' must be an array")
        if len(obs_list) > 1000:
            raise ValidationError(f"Too many observations: {len(obs_list)} (max 1000)")

        results = []
        errors = []
        for i, obs in enumerate(obs_list):
            node_id = obs.get("node_id")
            if not node_id:
                errors.append({"index": i, "error": "missing node_id"})
                continue
            from ohm.validation import validate_identifier

            try:
                node_id = validate_identifier(node_id, name="node_id")
            except ValueError as e:
                errors.append({"index": i, "error": str(e)})
                continue
            try:
                obs_type = obs.get("obs_type", obs.get("type", "measurement"))
                if obs_type not in self.schema_config.observation_types:
                    errors.append({"index": i, "error": f"Invalid observation type '{obs_type}' — must be one of: {', '.join(sorted(self.schema_config.observation_types))}"})
                    continue
                scale = obs.get("scale")
                if scale is not None:
                    from ohm.graph.schema import VALID_OBSERVATION_SCALES

                    if scale not in VALID_OBSERVATION_SCALES:
                        errors.append({"index": i, "error": f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}"})
                        continue
                    # ADR-025: Normalize binary to probability
                    if scale == "binary":
                        scale = "probability"
                    if scale == "probability":
                        value = obs.get("value")
                        if value is not None and (value < 0.0 or value > 1.0):
                            errors.append({"index": i, "error": f"Observation value {value} is outside [0, 1] for scale='probability'"})
                            continue
                result = self.current_store.write_observation(
                    node_id=node_id,
                    type=obs_type,
                    value=obs.get("value"),
                    baseline=obs.get("baseline"),
                    sigma=obs.get("sigma"),
                    source=obs.get("source"),
                    notes=obs.get("notes"),
                    source_name=obs.get("source_name"),
                    source_url=obs.get("source_url"),
                    scale=scale,
                    agent_name=agent,
                    half_life_days=obs.get("half_life_days"),
                    weibull_shape=obs.get("weibull_shape"),
                    idempotency_key=obs.get("idempotency_key"),
                )
                results.append(result)
            except Exception as e:
                errors.append({"index": i, "node_id": node_id, "error": str(e)})

        self._json_response(
            201,
            {
                "created": len(results),
                "errors": errors,
                "observations": results,
            },
        )

    def _post_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /outcome — record whether a source agent's claim was correct."""
        from ohm.exceptions import ValidationError

        source_agent = body.get("source_agent")
        claim_node = body.get("claim_node")
        outcome = body.get("outcome")
        notes = body.get("notes")
        if not source_agent or not claim_node or outcome is None:
            raise ValidationError("outcome requires source_agent, claim_node, and outcome fields")
        from ohm.queries import query_record_outcome

        result = query_record_outcome(
            self.current_store.conn,
            source_agent=source_agent,
            claim_node=claim_node,
            outcome=bool(outcome),
            recorded_by=agent,
            notes=notes,
        )
        self._json_response(201, result)

    def _post_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /agent/synthesis — one-call L3 writing: concept node + edges + observation."""
        from ohm.exceptions import ValidationError

        label = body.get("label")
        content = body.get("content")
        cluster_ids = body.get("cluster_ids", [])
        edge_type = body.get("edge_type", "SUPPORTS")
        confidence = body.get("confidence", 0.8)
        sigma = body.get("sigma", 0.1)
        provenance = body.get("provenance")
        tags = body.get("tags")

        if not label or not content or not cluster_ids:
            raise ValidationError("agent/synthesis requires label, content, and cluster_ids")

        from ohm.graph.schema import generate_node_id
        from ohm.validation import validate_identifier
        from ohm.exceptions import NodeNotFoundError
        import json as _json

        # OHM-tjzh: Validate that all cluster_ids reference existing nodes
        # before creating the synthesis node. Synthesis without connections
        # is a dead end — the cross-link constraint prevents this.
        validated_cluster_ids = []
        invalid_ids = []
        for cid in cluster_ids:
            try:
                safe_cid = validate_identifier(cid, name="cluster_id")
            except ValueError:
                invalid_ids.append(cid)
                continue
            # Check that the target node exists (OHM-tjzh)
            exists = self.current_store.conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [safe_cid],
            ).fetchone()
            if exists:
                validated_cluster_ids.append(safe_cid)
            else:
                invalid_ids.append(safe_cid)

        if not validated_cluster_ids:
            raise ValidationError(f"agent/synthesis requires at least one cluster_id that references an existing node. None of the provided cluster_ids were found: {cluster_ids}")

        if invalid_ids:
            import logging

            logging.getLogger("ohm.handlers").warning("Synthesis cluster_ids not found, skipping: %s", invalid_ids)

        node_id = generate_node_id(label)
        node_result = self.current_store.write_node(
            id=node_id,
            label=label,
            type="concept",
            content=content,
            confidence=confidence,
            agent_name=agent,
            provenance=provenance or f"{agent}_synthesis",
        )
        node_id = node_result["id"] if isinstance(node_result, dict) else node_id

        if tags:
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET tags = ? WHERE id = ?",
                [_json.dumps(tags), node_id],
            )

        edges_created = 0
        edge_errors = []
        for cid in validated_cluster_ids:
            try:
                self.current_store.write_edge(
                    from_node=node_id,
                    to_node=cid,
                    edge_type=edge_type,
                    layer="L3",
                    confidence=confidence,
                    agent_name=agent,
                )
                edges_created += 1
            except NodeNotFoundError as e:
                edge_errors.append(str(e))
            except Exception:
                edge_errors.append(f"Failed to create edge to {cid}")

        from ohm.queries import create_observation

        obs_result = create_observation(
            self.current_store.conn,
            node_id=node_id,
            obs_type="pattern",
            value=confidence,
            sigma=sigma,
            source="synthesis",
            notes=content,
            created_by=agent,
        )

        result = {
            "node": node_result if isinstance(node_result, dict) else {"id": node_id, "label": label},
            "edges_created": edges_created,
            "observation": obs_result,
        }
        if invalid_ids or edge_errors:
            result["warnings"] = []
            if invalid_ids:
                result["warnings"].append(f"cluster_ids not found (skipped): {invalid_ids}")
            if edge_errors:
                result["warnings"].extend(edge_errors)

        # OHM-jbsr: Oppositional review — flag CAUSES edges with homogeneous
        # source_tier/agent support that touch the clusters this synthesis
        # backs. Non-fatal: never blocks the synthesis.
        try:
            from ohm.graph.methods import oppositional_review

            all_flagged = []
            seen = set()
            for cid in validated_cluster_ids:
                review = oppositional_review(
                    self.current_store.conn,
                    target_node_id=cid,
                    auto_challenge=False,
                    limit=10,
                )
                for entry in review["flagged_edges"]:
                    if entry["edge_id"] not in seen:
                        seen.add(entry["edge_id"])
                        all_flagged.append(entry)
            if all_flagged:
                result["oppositional_review"] = {
                    "flagged_edges": all_flagged,
                    "challenged_edges": [],
                    "review_summary": {
                        "total_flagged": len(all_flagged),
                        "total_challenged": 0,
                        "dimensions_used": ["source_tier", "agent_authorship"],
                        "auto_challenge": False,
                    },
                }
        except Exception:
            import logging

            logging.getLogger("ohm.handlers").debug("oppositional review skipped for synthesis %s", node_id, exc_info=True)

        # OHM-8q5d: Source diversity — aggregate Shannon entropy across
        # evidence backing the cluster_ids. Non-fatal enrichment.
        try:
            from ohm.graph.methods import source_diversity_score

            cluster_diversity = []
            for cid in validated_cluster_ids:
                ds = source_diversity_score(self.current_store.conn, cid)
                cluster_diversity.append(ds)
            if cluster_diversity:
                avg_score = sum(d["score"] for d in cluster_diversity) / len(cluster_diversity)
                result["source_diversity"] = {
                    "cluster_diversity": cluster_diversity,
                    "aggregate_score": round(avg_score, 4),
                    "cluster_count": len(cluster_diversity),
                }
            else:
                result["source_diversity"] = {
                    "cluster_diversity": [],
                    "aggregate_score": 0.0,
                    "cluster_count": 0,
                }
        except Exception:
            import logging

            logging.getLogger("ohm.handlers").debug("source_diversity_score skipped for synthesis %s", node_id, exc_info=True)

        self._json_response(201, result)

    def _post_batch(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /batch — batch node and edge creation (all-or-nothing transaction)."""
        from ohm.exceptions import ValidationError
        import json

        nodes = body.get("nodes", [])
        edges = body.get("edges", [])
        errors = []
        nodes_created = 0
        edges_created = 0

        if len(nodes) + len(edges) > _server_module.MAX_BATCH_SIZE:
            raise ValidationError(f"Batch too large: {len(nodes)} nodes + {len(edges)} edges = {len(nodes) + len(edges)} items exceeds limit of {_server_module.MAX_BATCH_SIZE}")

        for i, node in enumerate(nodes):
            if "id" not in node or "label" not in node:
                errors.append({"index": i, "type": "node", "error": "Missing required field: id and label"})
        for i, edge in enumerate(edges):
            if "from" not in edge or "to" not in edge or "type" not in edge:
                errors.append({"index": i, "type": "edge", "error": "Missing required field: from, to, type"})

        if errors:
            raise ValidationError(f"Batch validation failed: {json.dumps(errors)}")

        batch_node_ids = {n.get("id") for n in nodes if n.get("id")}

        try:
            self.current_store.conn.execute("BEGIN TRANSACTION")
            for node in nodes:
                # OHM-jw1x: run pre_ingest hooks for each node in the batch,
                # same as the normal POST /node path. Pass the batch's edges
                # and node ids so cross_link_check can implement ADR-018
                # option 2 (accept a node when an edge in the same batch
                # references it).
                hook_error = self._run_pre_ingest_hooks(
                    agent, "node", node, batch_edges=edges, batch_node_ids=batch_node_ids
                )
                if hook_error is not None:
                    raise ValidationError(f"Batch node {node.get('id', '?')} rejected by pre_ingest hook: {hook_error.get('message', hook_error)}")
                self.current_store.write_node(
                    id=node["id"],
                    label=node["label"],
                    type=node.get("type", "concept"),
                    content=node.get("content"),
                    confidence=node.get("confidence", 1.0),
                    visibility=node.get("visibility", "team"),
                    provenance=node.get("provenance"),
                    tags=node.get("tags"),
                    metadata=node.get("metadata"),
                    priority=node.get("priority"),
                    url=node.get("url"),
                    task_status=node.get("task_status"),
                    assigned_to=node.get("assigned_to"),
                    due_date=node.get("due_date"),
                    utility_scale=node.get("utility_scale"),
                    current_best_action=node.get("current_best_action"),
                    action_alternatives=node.get("action_alternatives"),
                    utility_usd_per_day=node.get("utility_usd_per_day"),
                    utility_currency=node.get("utility_currency"),
                    agent_name=agent,
                )
                nodes_created += 1
            for edge in edges:
                self.current_store.write_edge(
                    from_node=edge["from"],
                    to_node=edge["to"],
                    edge_type=edge["type"],
                    layer=edge.get("layer", "L3"),
                    confidence=edge.get("confidence"),
                    condition=edge.get("condition"),
                    provenance=edge.get("provenance"),
                    challenge_of=edge.get("challenge_of"),
                    challenge_type=edge.get("challenge_type"),
                    urgency=edge.get("urgency"),
                    probability=edge.get("probability"),
                    probability_p05=edge.get("probability_p05"),
                    probability_p50=edge.get("probability_p50"),
                    probability_p95=edge.get("probability_p95"),
                    confidence_p05=edge.get("confidence_p05"),
                    confidence_p50=edge.get("confidence_p50"),
                    confidence_p95=edge.get("confidence_p95"),
                    agent_name=agent,
                )
                edges_created += 1
            self.current_store.conn.execute("COMMIT")
        except Exception:
            self.current_store.conn.execute("ROLLBACK")
            raise

        self._json_response(
            201,
            {
                "nodes_created": nodes_created,
                "edges_created": edges_created,
                "errors": errors,
            },
        )

    def _post_webhook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /webhook — register or update webhook callback URL for this agent."""
        import json as _json
        from ohm.exceptions import ValidationError

        url = body.get("url", "")
        events = body.get("events", ["node.created", "node.updated", "edge.created"])
        if not url:
            raise ValidationError("Webhook requires a 'url' field")
        _server_module._validate_webhook_url(url)
        # OHM-whbk: persist to DuckDB so registrations survive restarts.
        # Single-tenant mode uses customer_id="" as the key.
        customer_id = self._customer_id or ""
        events_json = _json.dumps(list(events))
        self.current_store.conn.execute(
            """
            INSERT INTO ohm_webhook_subscriptions (customer_id, agent, url, events, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (customer_id, agent) DO UPDATE SET
                url = excluded.url,
                events = excluded.events,
                updated_at = CURRENT_TIMESTAMP
            """,
            [customer_id, agent, url, events_json],
        )
        with _server_module._webhook_lock:
            if self._customer_id not in _server_module._webhook_registry:
                _server_module._webhook_registry[self._customer_id] = {}
            _server_module._webhook_registry[self._customer_id][agent] = {"url": url, "events": events}
        self._json_response(
            200,
            {
                "status": "registered",
                "agent": agent,
                "url": url,
                "events": events,
            },
        )

    def _post_state(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /state — update agent state/focus."""
        result = self.current_store.update_agent_state(
            current_focus=body.get("focus"),
            active_patterns=body.get("patterns"),
            available_services=body.get("services"),
            session_id=body.get("session_id"),
            agent_name=agent,
        )
        self._json_response(200, result)

    def _post_register(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /register — agent registration (idempotent: creates or updates agent node + edges)."""
        from ohm.queries import create_edge, find_or_create_node
        import re

        agent_label = body.get("name", agent)
        agent_id = "agent_" + re.sub(r"[^a-zA-Z0-9]+", "_", agent_label.lower()).strip("_")

        existing_active = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id]).fetchone()
        existing_soft_deleted = self.current_store.conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [agent_id]).fetchone()

        if existing_active:
            self.current_store.conn.execute(
                "UPDATE ohm_nodes SET content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        elif existing_soft_deleted:
            self.current_store.conn.execute(
                """UPDATE ohm_nodes SET
                    content = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ?,
                    deleted_at = NULL
                WHERE id = ?""",
                [body.get("description"), agent, agent_id],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]
            reg_edge_types = ("VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN", "LISTENS_TO")
            placeholders = ",".join(["?"] * len(reg_edge_types))
            self.current_store.conn.execute(
                f"UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND edge_type IN ({placeholders}) AND deleted_at IS NULL",
                [agent_id] + list(reg_edge_types),
            )
        else:
            self.current_store.conn.execute(
                """INSERT INTO ohm_nodes
                   (id, label, type, content, created_by, confidence, visibility, created_at, updated_at)
                   VALUES (?, ?, 'agent', ?, ?, 1.0, 'team', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                [agent_id, agent_label, body.get("description"), agent],
            )
            me = self.current_store.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [agent_id])[0]

        created_edges = []
        for v in body.get("values", []):
            value_node = find_or_create_node(
                self.current_store.conn,
                label=v,
                node_type="value",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=value_node["id"],
                edge_type="VALUES",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for g in body.get("goals", []):
            goal_node = find_or_create_node(
                self.current_store.conn,
                label=g,
                node_type="goal",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=goal_node["id"],
                edge_type="GOALS",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for c in body.get("capabilities", []):
            cap_node = find_or_create_node(
                self.current_store.conn,
                label=c,
                node_type="skill",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=cap_node["id"],
                edge_type="CAPABLE_OF",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for i in body.get("interests", []):
            topic_node = find_or_create_node(
                self.current_store.conn,
                label=i,
                node_type="topic",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=topic_node["id"],
                edge_type="INTERESTED_IN",
                layer="L1",
                created_by=agent,
                confidence=1.0,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        for a in body.get("listens_to", []):
            other = find_or_create_node(
                self.current_store.conn,
                label=a,
                node_type="agent",
                created_by=agent,
            )
            edge = create_edge(
                self.current_store.conn,
                from_node=agent_id,
                to_node=other["id"],
                edge_type="LISTENS_TO",
                layer="L3",
                created_by=agent,
                confidence=0.7,
                provenance="self_declaration",
            )
            created_edges.append(edge)

        self._json_response(
            201,
            {
                "agent": me,
                "edges_created": len(created_edges),
            },
        )

    def _post_sync(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /sync — explicit DuckLake sync trigger (OHM-7301)."""
        sync_result = self.current_store.sync_heartbeat()
        self._json_response(200, sync_result)

    def _post_skill(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /skill — Create a portable skill node (OHM-461f).

        Body:
            label (required): Human-readable skill name
            trigger (required): When this skill activates
            scope (optional): personal (default), project, or universal
            required_tools (optional): List of tool names
            boundaries (optional): Constraints on what the skill does
            output_format (optional): Expected output format
            verification_evidence (optional): List of evidence types
            connects_to (optional): List of existing node IDs to link
        """
        from ohm.queries import create_skill
        from ohm.exceptions import ValidationError

        label = body.get("label")
        trigger = body.get("trigger")
        if not label or not trigger:
            raise ValidationError("label and trigger are required")

        skill = create_skill(
            self.current_store.conn,
            label=label,
            trigger=trigger,
            scope=body.get("scope", "personal"),
            required_tools=body.get("required_tools", []),
            boundaries=body.get("boundaries"),
            output_format=body.get("output_format"),
            verification_evidence=body.get("verification_evidence", []),
            connects_to=body.get("connects_to", []),
            created_by=agent,
        )
        self._json_response(201, skill)

    def _post_runbook(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /runbook — Create an ordered chain of skills (OHM-461f).

        Body:
            label (required): Human-readable runbook name
            skill_ids (required): Ordered list of existing skill node IDs
            description (optional): Free-text description
        """
        from ohm.queries import create_runbook
        from ohm.exceptions import ValidationError

        label = body.get("label")
        skill_ids = body.get("skill_ids", [])
        if not label:
            raise ValidationError("label is required")
        if not skill_ids or not isinstance(skill_ids, list):
            raise ValidationError("skill_ids must be a non-empty list")

        runbook = create_runbook(
            self.current_store.conn,
            label=label,
            skill_ids=skill_ids,
            description=body.get("description"),
            created_by=agent,
        )
        self._json_response(201, runbook)

    def _get_runbook_steps(self, path: str, qs: dict) -> None:
        """GET /runbook/{id}/steps — Get ordered skills in a runbook (OHM-461f)."""
        from ohm.queries import get_runbook_steps
        from ohm.exceptions import NodeNotFoundError, ValidationError

        prefix = "/runbook/"
        suffix = "/steps"
        if not path.endswith(suffix):
            raise ValidationError("Path must end with /steps")
        runbook_id = path[len(prefix) : -len(suffix)]
        if not runbook_id:
            raise ValidationError("runbook_id is required")

        try:
            result = get_runbook_steps(self.current_store.conn, runbook_id=runbook_id)
            self._json_response(200, result)
        except NodeNotFoundError as e:
            self._json_response(404, {"error": "not_found", "message": str(e)})

    def _get_vault(self, path: str, qs: dict) -> None:
        """GET /vault — list vault contents for the authenticated agent (OHM-cuu0).

        Returns nodes with ``visibility='vault'`` created by the authenticated
        agent, plus any edges attached to those nodes.
        """
        agent = self._authenticate()
        if agent is None:
            if self.no_auth:
                agent = "ohm"
            else:
                raise AuthenticationError(  # noqa: F821
                    "Authentication required"
                )
        nodes = self.current_store.execute(
            "SELECT * FROM ohm_nodes WHERE visibility = 'vault' AND created_by = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 100",
            [agent],
        )
        node_ids = [n["id"] for n in nodes]
        edges: list = []
        if node_ids:
            placeholders = ",".join(["?"] * len(node_ids))
            edges = self.current_store.execute(
                f"SELECT * FROM ohm_edges WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders})) AND deleted_at IS NULL",
                node_ids + node_ids,
            )
        self._json_response(200, {"agent": agent, "nodes": nodes, "edges": edges, "count": len(nodes)})

    def _post_vault_promote(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /vault/promote — promote a vault node to the shared graph (OHM-cuu0).

        Changes ``visibility`` from ``vault`` to ``team`` for the given node
        and its edges (if any). Only the owning agent can promote their own
        vault content.

        Body: {"node_id": "<node_id>"}
        """
        node_id = body.get("node_id", "")
        if not node_id:
            self._json_response(400, {"error": "validation_error", "message": "node_id is required"})
            return

        node = self.current_store.conn.execute(
            "SELECT id, visibility, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if not node:
            self._json_response(404, {"error": "not_found", "message": f"Node not found: {node_id}"})
            return
        nid, vis, creator = node

        if vis != "vault":
            self._json_response(400, {"error": "validation_error", "message": f"Node {node_id} has visibility '{vis}', not 'vault'"})
            return

        # OHM-tjzh: promotion requires at least one cross-link to shared graph
        from ohm.schema import requires_cross_link

        if requires_cross_link(node["type"] if len(node) > 3 else "concept"):
            edge_count = self.current_store.conn.execute(
                "SELECT COUNT(*) FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
                [node_id, node_id],
            ).fetchone()[0]
            if edge_count == 0:
                self._json_response(
                    422,
                    {
                        "error": "cross_link_required",
                        "message": f"Vault node '{node_id}' has no edges. Per ADR-018 / OHM-tjzh, nodes must have at least one edge before promotion to the shared graph.",
                        "hint": "Add an edge to an existing shared-graph node via POST /edge, then retry promotion.",
                    },
                )
                return

        now = self.current_store._now()
        self.current_store.conn.execute(
            "UPDATE ohm_nodes SET visibility = 'team', updated_at = ?, updated_by = ? WHERE id = ?",
            [now, agent, node_id],
        )
        # Also promote related edges
        self.current_store.conn.execute(
            "UPDATE ohm_edges SET updated_at = ?, updated_by = ? WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
            [now, agent, node_id, node_id],
        )

        self._json_response(
            200,
            {
                "promoted": node_id,
                "previous_visibility": "vault",
                "new_visibility": "team",
                "promoted_by": agent,
            },
        )

    def _post_heartbeat(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /heartbeat — agent heartbeat with sync and orient enrichment.

        ADR-023: Heartbeat now includes orient data, contradictions, stale observations,
        and anomalies so agents see what needs attention without extra API calls.
        """
        from ohm.methods import agent_heartbeat
        from ohm.server.suggestions import generate_island_nudge

        result = agent_heartbeat(
            self.current_store.conn,
            agent,
            focus=body.get("focus"),
        )
        sync_result = self.current_store.sync_heartbeat()
        result["ducklake_sync"] = sync_result

        # OHM-tr71.4: Island isolation nudge in heartbeat
        try:
            island = generate_island_nudge(self.current_store, agent)
            if island:
                result["island_warning"] = island["island_warning"]
        except Exception as exc:
            logger.debug("Heartbeat island nudge failed: %s", exc)

        # OHM-jx4q: Orphan rate nudge in heartbeat
        try:
            from ohm.queries import query_graph_health

            health = query_graph_health(self.current_store.conn)
            total_nodes = health.get("total_nodes") or 0
            orphans = health.get("orphan_nodes") or 0
            orphan_rate = round(orphans / total_nodes, 4) if total_nodes else 0
            if orphan_rate > 0.10:
                result["orphan_rate_warning"] = {
                    "orphan_rate": orphan_rate,
                    "orphan_count": orphans,
                    "total_nodes": total_nodes,
                    "orphan_type_breakdown": health.get("orphan_type_breakdown", {}),
                    "triage_endpoint": "GET /admin/orphan-triage",
                }
        except Exception as exc:
            logger.debug("Heartbeat orphan rate nudge failed: %s", exc)

        # ADR-023: Proactive orient enrichment
        try:
            orient = self._get_orient_data(agent)
            if orient:
                result["orient"] = orient
        except Exception as exc:
            logger.debug("Heartbeat orient enrichment failed: %s", exc)

        # ADR-023: Proactive contradictions (limit 3)
        try:
            contradictions = self._get_contradictions_data(limit=3)
            if contradictions:
                result["contradictions"] = contradictions
        except Exception as exc:
            logger.debug("Heartbeat contradictions enrichment failed: %s", exc)

        # ADR-023: Stale observations nudge
        try:
            stale = self._get_stale_data(days=7, limit=3)
            if stale:
                result["stale_observations"] = stale
        except Exception as exc:
            logger.debug("Heartbeat stale enrichment failed: %s", exc)

        self._json_response(200, result)

    def _get_orient_data(self, agent: str) -> dict | None:
        """Lightweight orient data for heartbeat enrichment."""
        try:
            conn = self.current_store.read_conn

            _hours = 24  # noqa: F841
            # Last activity
            last_activity = conn.execute(
                "SELECT MAX(la) FROM (SELECT created_at AS la FROM ohm_nodes WHERE created_by = ? UNION ALL SELECT created_at AS la FROM ohm_edges WHERE created_by = ? UNION ALL SELECT created_at AS la FROM ohm_observations WHERE created_by = ?)",
                [agent, agent, agent],
            ).fetchone()[0]
            # Open tasks
            tasks = conn.execute(
                "SELECT id, label, priority, due_date FROM ohm_nodes WHERE assigned_to = ? AND task_status = 'open' AND deleted_at IS NULL ORDER BY priority DESC LIMIT 5",
                [agent],
            ).fetchall()
            return {
                "last_activity": str(last_activity) if last_activity else None,
                "open_tasks": len(tasks),
                "task_summaries": [{"id": t[0], "label": t[1], "priority": t[2]} for t in tasks[:3]],
            }
        except Exception:
            return None

    def _get_contradictions_data(self, limit: int = 3) -> list | None:
        """Lightweight contradictions for heartbeat enrichment."""
        try:
            from ohm.methods import detect_contradictions

            result = detect_contradictions(self.current_store.read_conn, confidence_threshold=0.5)
            if isinstance(result, list):
                return result[:limit]
            return None
        except Exception:
            return None

    def _get_stale_data(self, days: int = 7, limit: int = 3) -> list | None:
        """Lightweight stale observations for heartbeat enrichment."""
        try:
            from ohm.queries import query_stale_edges

            result = query_stale_edges(self.current_store.read_conn, stale_threshold=0.1)
            if isinstance(result, list):
                return result[:limit]
            return None
        except Exception:
            return None

    def _post_deduplicate(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /deduplicate — remove duplicate edges (same from→to, type, layer), keeping most recent."""
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

    def _post_ask_synthesis(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /ask — conversational analytics: natural language question → synthesized insights.

        Converts OHM's AND-gate interface into an OR-gate by orchestrating
        search, neighborhood expansion, Bayesian inference, and challenge
        checking into a single structured response.

        Pipeline:
          1. Node search (text + semantic) to find relevant nodes
          2. Neighborhood expansion for top matches
          3. Bayesian inference on causal chains (optional)
          4. Challenge check for relevant edges
          5. Synthesis combining all results

        Input:
          question (required): Natural language question
          agent: Requesting agent name (defaults to authenticated agent)
          depth: Neighborhood depth, 1-3 (default 2)
          include_inference: Run Bayesian inference (default true)
          limit: Max search results per method (default 5)
        """
        from ohm.queries import search, semantic_search, query_neighborhood
        from ohm.bayesian import bayesian_inference, PGMPY_AVAILABLE
        from ohm.validation import validate_identifier

        question = body.get("question", "").strip()
        if not question:
            self._json_response(400, {"error": "missing_parameter", "message": "'question' is required"})
            return

        depth = min(max(int(body.get("depth", 2)), 1), 3)
        include_inference = body.get("include_inference", True)
        limit = min(max(int(body.get("limit", 5)), 1), 20)
        # Step 1: Node search — text + semantic
        matched_nodes = []
        search_errors = []

        # Direct node ID lookup — if the question contains a known node ID, use it
        question_lower = question.lower().replace(" ", "_").replace("-", "_")
        try:
            # Check if question matches an existing node ID directly
            direct_node = self.current_store.get_node(question_lower)
            if direct_node:
                matched_nodes.append(
                    {
                        "id": direct_node["id"],
                        "label": direct_node.get("label", ""),
                        "type": direct_node.get("type", ""),
                        "confidence": direct_node.get("confidence"),
                        "match_method": "direct_id",
                    }
                )
        except Exception:
            pass

        # Also try common variations (hormuz and gate → hormuz_and_gate)
        if not matched_nodes:
            for variant in [question_lower, question_lower.replace(" and ", "_and_").replace(" ", "_")]:
                try:
                    node = self.current_store.get_node(variant)
                    if node and node["id"] not in {n["id"] for n in matched_nodes}:
                        matched_nodes.append(
                            {
                                "id": node["id"],
                                "label": node.get("label", ""),
                                "type": node.get("type", ""),
                                "confidence": node.get("confidence"),
                                "match_method": "direct_id",
                            }
                        )
                        break
                except Exception:
                    pass

        # Text search
        try:
            text_results = search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            for r in text_results:
                matched_nodes.append(
                    {
                        "id": r.get("id", ""),
                        "label": r.get("label", ""),
                        "type": r.get("type", ""),
                        "confidence": r.get("confidence"),
                        "match_method": "text",
                    }
                )
        except Exception as e:
            search_errors.append(f"text_search: {e}")

        # Semantic search
        try:
            sem_results = semantic_search(
                self.current_store.conn,
                query=question,
                limit=limit,
            )
            # Merge: add semantic results that aren't already in matched_nodes
            existing_ids = {n["id"] for n in matched_nodes}
            for r in sem_results:
                nid = r.get("node_id", r.get("id", ""))
                if nid and nid not in existing_ids:
                    matched_nodes.append(
                        {
                            "id": nid,
                            "label": r.get("label", ""),
                            "type": r.get("type", ""),
                            "confidence": r.get("confidence"),
                            "distance": r.get("distance"),
                            "match_method": "semantic",
                        }
                    )
                    existing_ids.add(nid)
        except Exception as e:
            # Semantic search may be unavailable (no Ollama)
            search_errors.append(f"semantic_search: {e}")

        # Fuzzy search fallback
        if not matched_nodes:
            try:
                from ohm.graph.queries import fuzzy_search

                fuzzy_results = fuzzy_search(
                    self.current_store.conn,
                    query=question,
                    limit=limit,
                )
                existing_ids = {n["id"] for n in matched_nodes}
                for r in fuzzy_results:
                    nid = r.get("id", "")
                    if nid and nid not in existing_ids:
                        matched_nodes.append(
                            {
                                "id": nid,
                                "label": r.get("label", ""),
                                "type": r.get("type", ""),
                                "confidence": r.get("confidence"),
                                "distance": r.get("distance"),
                                "match_method": r.get("match_type", "fuzzy"),
                            }
                        )
                        existing_ids.add(nid)
            except Exception as e:
                search_errors.append(f"fuzzy_search: {e}")

        # Step 2: Neighborhood expansion for top matches
        all_node_ids = set()
        all_edges = []
        node_details = []
        for node in matched_nodes[:limit]:
            nid = node["id"]
            if not nid:
                continue
            all_node_ids.add(nid)
            try:
                n_edges = query_neighborhood(
                    self.current_store.conn,
                    nid,
                    depth=depth,
                )
                for edge in n_edges:
                    all_node_ids.add(edge.get("from_node", edge.get("from", "")))
                    all_node_ids.add(edge.get("to_node", edge.get("to", "")))
                    all_edges.append(edge)
            except Exception:
                pass

        # Fetch node details for all discovered nodes
        if all_node_ids:
            placeholders = ",".join(["?"] * len(all_node_ids))
            node_details = self.current_store.execute(
                f"SELECT id, label, type, confidence, content, tags, created_by, provenance FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(all_node_ids),
            )

        # Step 3: Bayesian inference on causal chains
        inference_results = {}
        inference_errors = []
        if include_inference and PGMPY_AVAILABLE and matched_nodes:
            # Find nodes with causal edges (CAUSES, DEPENDS_ON, THREATENS, NEGATES)
            target_ids = [n["id"] for n in matched_nodes if n.get("id")]
            if target_ids:
                placeholders = ",".join(["?"] * len(target_ids))
                # Find causal edges involving our matched nodes
                causal_edges = self.current_store.execute(
                    f"""SELECT DISTINCT from_node, to_node, edge_type, confidence, probability
                       FROM ohm_edges
                       WHERE (from_node IN ({placeholders}) OR to_node IN ({placeholders}))
                         AND edge_type IN ('CAUSES', 'DEPENDS_ON', 'THREATENS', 'NEGATES')
                         AND deleted_at IS NULL
                       LIMIT 50""",
                    target_ids + target_ids,
                )

                if causal_edges:
                    # Build evidence from observed nodes (high-confidence observations)
                    # OHM-w1iv.2: batch the latest probability observation for all targets.
                    if target_ids:
                        placeholders = ",".join(["?"] * len(target_ids))
                        obs_rows = self.current_store.execute(
                            f"""SELECT node_id, value FROM (
                                SELECT node_id, value,
                                    ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY created_at DESC) AS rn
                                FROM ohm_observations
                                WHERE node_id IN ({placeholders})
                                  AND type = 'probability'
                                  AND deleted_at IS NULL
                            ) WHERE rn = 1""",
                            target_ids,
                        )
                        evidence = {}
                        for row in obs_rows:
                            try:
                                val = float(row["value"])
                                if 0.0 <= val <= 1.0:
                                    evidence[row["node_id"]] = 1 if val >= 0.5 else 0
                            except (ValueError, TypeError, KeyError):
                                pass

                    # Run inference on each matched node that has causal connections
                    for target_id in target_ids[:3]:  # Limit to top 3 to avoid timeouts
                        try:
                            target_safe = validate_identifier(target_id, name="target")
                            result = bayesian_inference(
                                self.current_store.conn,
                                target_safe,
                                evidence,
                                customer_id=self._customer_id,
                            )
                            if "error" not in result:
                                # ADR-025: Extract only posteriors, not full network info
                                posterior = result.get("posterior", result)
                                network_info = result.get("network_info", {})
                                inference_results[target_safe] = {
                                    "posterior": posterior,
                                    "n_nodes": network_info.get("n_nodes", 0),
                                    "n_edges": network_info.get("n_edges", 0),
                                    "method": result.get("method", "bayesian_variable_elimination"),
                                }
                        except Exception as e:
                            inference_errors.append(f"inference({target_id}): {e}")

        # Step 4: Challenge check for relevant edges
        challenges = []
        challenge_node_ids = list(all_node_ids)[:50]  # Limit to prevent runaway queries
        if challenge_node_ids:
            placeholders = ",".join(["?"] * len(challenge_node_ids))
            challenge_edges = self.current_store.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                          e.challenge_type, e.provenance, e.created_by,
                          n_from.label AS from_label, n_to.label AS to_label
                   FROM ohm_edges e
                   LEFT JOIN ohm_nodes n_from ON e.from_node = n_from.id
                   LEFT JOIN ohm_nodes n_to ON e.to_node = n_to.id
                   WHERE e.edge_type = 'CHALLENGED_BY'
                     AND (e.to_node IN ({placeholders}) OR e.from_node IN ({placeholders}))
                     AND e.deleted_at IS NULL
                   LIMIT 20""",
                challenge_node_ids + challenge_node_ids,
            )
            for ce in challenge_edges:
                challenges.append(
                    {
                        "edge_id": ce.get("id"),
                        "challenger_node": ce.get("from_node"),
                        "challenged_node": ce.get("to_node"),
                        "challenger_label": ce.get("from_label", ""),
                        "challenged_label": ce.get("to_label", ""),
                        "challenge_type": ce.get("challenge_type"),
                        "confidence": ce.get("confidence"),
                        "provenance": ce.get("provenance"),
                        "created_by": ce.get("created_by"),
                    }
                )

        # Step 5: Build synthesis
        # Confidence based on: search match quality + inference certainty + challenge coverage
        confidence = 0.5
        match_count = len(matched_nodes)
        if match_count >= 3:
            confidence += 0.15
        elif match_count >= 1:
            confidence += 0.1

        # Boost if semantic matches are close
        semantic_matches = [n for n in matched_nodes if n.get("match_method") == "semantic"]
        if semantic_matches:
            min_dist = min((n.get("distance", 1.0) for n in semantic_matches), default=1.0)
            if min_dist < 0.3:
                confidence += 0.1
            elif min_dist < 0.5:
                confidence += 0.05

        # Boost if inference converged
        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    max_prob = max(posterior.get("good", 0), posterior.get("bad", 0))
                    confidence += 0.1 * max_prob  # Higher certainty → more confidence

        # Reduce if challenges exist on key edges
        if challenges:
            challenge_count = len(challenges)
            confidence -= 0.05 * min(challenge_count, 3)

        confidence = max(0.1, min(1.0, round(confidence, 2)))

        # Build synthesis text from gathered context
        synthesis_parts = []

        if matched_nodes:
            node_labels = [f"{n['label']} ({n['id']})" for n in matched_nodes[:5] if n.get("label")]
            if node_labels:
                synthesis_parts.append(f"Relevant nodes: {', '.join(node_labels)}.")

        if inference_results:
            for target_id, inf in inference_results.items():
                posterior = inf.get("posterior", {}).get(target_id, {})
                if posterior:
                    p_good = posterior.get("good", 0)
                    p_bad = posterior.get("bad", 0)
                    synthesis_parts.append(f"Bayesian inference on {target_id}: P(good)={p_good:.2%}, P(bad)={p_bad:.2%}.")

        if challenges:
            challenge_descs = []
            for c in challenges[:3]:
                cdesc = f"{c.get('challenger_label', c.get('challenger_node', '?'))} challenges {c.get('challenged_label', c.get('challenged_node', '?'))}"
                if c.get("challenge_type"):
                    cdesc += f" ({c['challenge_type']})"
                challenge_descs.append(cdesc)
            synthesis_parts.append(f"Active challenges: {'; '.join(challenge_descs)}.")

        if not synthesis_parts:
            synthesis_parts.append(f"No matching nodes or inference results found for '{question}'.")

        synthesis = " ".join(synthesis_parts)

        # Source node IDs for traceability
        sources = list({n["id"] for n in matched_nodes if n.get("id")})[:20]

        response = {
            "question": question,
            "matched_nodes": matched_nodes[:20],
            "neighborhood": {
                "nodes": node_details[:50],
                "edges": all_edges[:100],
            },
            "inference_results": inference_results,
            "challenges": challenges,
            "synthesis": synthesis,
            "confidence": confidence,
            "sources": sources,
        }

        if inference_errors:
            response["inference_errors"] = inference_errors
        if search_errors:
            response["search_errors"] = search_errors
        if not PGMPY_AVAILABLE:
            response["inference_skipped"] = True
            response["inference_reason"] = "pgmpy not available"
        if not include_inference:
            response["inference_skipped"] = True
            response["inference_reason"] = "include_inference=false"

        self._json_response(200, response)

    def _get_detect_verifications(self, path: str, qs: dict) -> None:
        agent = qs.get("agent", [None])[0]
        days_threshold = int(qs.get("days_threshold", ["14"])[0])
        confidence_threshold = float(qs.get("confidence_threshold", ["0.85"])[0])
        limit = int(qs.get("limit", ["100"])[0])
        from ohm.queries import detect_verifiable_claims

        results = detect_verifiable_claims(
            self.current_store.read_conn,
            agent=agent,
            days_threshold=days_threshold,
            confidence_threshold=confidence_threshold,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": results})

    def _post_create_nudge(self, path: str, qs: dict, body: dict, agent: str) -> None:
        edge_id = body.get("edge_id")
        if not edge_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("edge_id is required")
        reason = body.get("reason")
        confidence = float(body.get("confidence", 0.5))
        from ohm.queries import create_verification_nudge

        result = create_verification_nudge(
            self.current_store.conn,
            edge_id=edge_id,
            created_by=agent,
            confidence=confidence,
            reason=reason,
        )
        self._json_response(201, {"ok": True, "data": result})

    def _post_record_verification_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        edge_id = body.get("edge_id")
        outcome = body.get("outcome")
        if not edge_id or not outcome:
            from ohm.exceptions import ValidationError

            raise ValidationError("edge_id and outcome are required")
        reason = body.get("reason")
        from ohm.queries import record_verification_outcome

        result = record_verification_outcome(
            self.current_store.conn,
            edge_id=edge_id,
            outcome=outcome,
            recorded_by=agent,
            reason=reason,
        )
        self._json_response(201, {"ok": True, "data": result})

    def _get_list_verifications(self, path: str, qs: dict) -> None:
        agent = qs.get("agent", [None])[0]
        limit = int(qs.get("limit", ["100"])[0])
        from ohm.queries import list_pending_verifications

        results = list_pending_verifications(
            self.current_store.read_conn,
            agent=agent,
            limit=limit,
        )
        self._json_response(200, {"ok": True, "data": results})
