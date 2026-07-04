"""Infrastructure handler mixin — root, health, readiness, metrics, and OpenAPI endpoints."""

import time


class InfraHandlerMixin:
    """Handler mixin for infrastructure endpoints (OHM-shpq)."""

    def _set_extra_cors_headers(self) -> None:
        """Set CORS headers appropriate for OHM's agent-only access model.

        OHM is consumed by agents over Bearer-token auth (curl, SDK, HTTP
        clients) — not by browsers. By design we do NOT emit a permissive
        ``Access-Control-Allow-Origin`` header, so cross-origin browser reads
        are blocked by default. This no-op stub exists so callers that pre-flush
        CORS headers (e.g. webhook outbox/dead-letter listing endpoints) do not
        raise ``AttributeError``. If browser access is ever required, override
        this method to emit an explicit, restricted origin policy.
        """

    def _get_infra_root(self, path: str, qs: dict) -> None:
        """GET / — root discovery endpoint (no auth)."""
        self._json_response(
            200,
            {
                "service": "ohmd",
                "version": "0.2.0",
                "schema": self.schema_config.name,
                "description": "Multi-agent knowledge graph daemon",
                "auth_model": "public-read" if not self.require_read_auth else "authenticated",
                "endpoints": {
                    "/": {"method": "GET", "description": "This discovery index (no auth required)"},
                    "/health": {"method": "GET", "description": "Health check (no auth required)"},
                    "/ready": {"method": "GET", "description": "Readiness check (no auth required)"},
                    "/metrics": {"method": "GET", "description": "Prometheus-style metrics"},
                    "/stats": {"method": "GET", "description": "Graph statistics (nodes, edges, layers)"},
                    "/inference": {"method": "GET", "description": "Bayesian inference: compute posterior probabilities given evidence (observation, includes confounders). ?layers=L3,L4 to scope by layer"},
                    "/intervene": {
                        "method": "GET",
                        "description": "Causal intervention (do-operator): required ?target=node_id&state=0|1. Optional: ?query=node1,node2 ?layers=L3,L4 ?leak=0.15",
                    },
                    "/ate": {"method": "GET", "description": "Average Treatment Effect: model-based ATE from noisy-OR CPDs (ATE = P(effect=bad|do(cause=bad)) - P(effect=bad|do(cause=good))). ?layers=L3,L4 to scope by layer"},
                    "/sensitivity": {"method": "GET", "description": "Sensitivity analysis: E-value quantifying how much unmeasured confounding would overturn a causal conclusion. ?layers=L3,L4 to scope by layer"},
                    "/adjustment": {"method": "GET", "description": "Find valid backdoor/frontdoor adjustment sets for causal identification (Pearl's criteria). ?layers=L3,L4 to scope by layer"},
                    "/voi": {
                        "method": "GET",
                        "description": "Value of Information: rank nodes by research priority (uncertainty × sensitivity to decision). ?decision=node1,node2&top=10&layers=L3,L4&edge_types=CAUSES,DEPENDS_ON&min_observations=3&timeout=30.",
                    },
                    "/voi/tasks": {"method": "GET", "description": "Generate research tasks from VoI rankings, matched to agent expertise. ?agent=metis&decision=node1,node2&top=5&layers=L3,L4"},
                    "/suggest_causes": {"method": "GET", "description": "Suggest candidate CAUSES edges from existing non-causal relationships (DEPENDS_ON, APPLIES_TO, etc.). Optional: ?layers=L3,L4 to scope by layer, ?min_confidence=0.5"},
                    "/deduplicate": {"method": "POST", "description": "Remove duplicate edges (same from→to, type, layer), keeping the most recent"},
                    "/refute": {"method": "GET", "description": "Test robustness of causal conclusions using DoWhy refutation methods (random common cause, placebo, data subset, unobserved confounder)"},
                    "/lint": {"method": "GET", "description": "Contract layer linting: validate graph against naming conventions and required fields"},
                    "/contract": {"method": "GET", "description": "Current contract configuration (naming conventions, required fields, schema)"},
                    "/status": {"method": "GET", "description": "Daemon status and configuration"},
                    "/schema": {"method": "GET", "description": "Node types, edge types, layers"},
                    "/layers": {"method": "GET", "description": "L1-L4 layer descriptions"},
                    "/node/{id}": {"method": "GET", "description": "Get a single node by ID"},
                    "/edge/{id}": {"method": "GET", "description": "Get a single edge by ID"},
                    "/neighborhood/{id}": {"method": "GET", "description": "Bounded-depth graph traversal"},
                    "/path/{from}/{to}": {"method": "GET", "description": "Shortest path between two nodes"},
                    "/impact/{id}": {"method": "GET", "description": "Downstream failure impact analysis"},
                    "/confidence/{id}": {"method": "GET", "description": "Provenance and challenge audit"},
                    "/agent/{name}": {"method": "GET", "description": "Agent state and focus"},
                    "/agents": {"method": "GET", "description": "List all registered agents"},
                    "/nodes": {"method": "GET", "description": "List nodes with pagination and filtering"},
                    "/listen": {"method": "GET", "description": "Change feed since last check"},
                    "/events": {"method": "GET", "description": "SSE stream of real-time change feed events"},
                    "/node": {"method": "POST", "description": "Create a new node"},
                    "/edge": {"method": "POST", "description": "Create a new edge"},
                    "/challenge/{id}": {"method": "POST", "description": "Challenge an existing edge"},
                    "/support/{id}": {"method": "POST", "description": "Support an existing edge"},
                    "/observe/{id}": {"method": "POST", "description": "Record an observation on a node"},
                    "/observations": {"method": "GET", "description": "List observations with filtering by type, source, node_id. POST for bulk upload: {observations: [{node_id, value, sigma, obs_type, source}]}"},
                    "/outcome": {"method": "POST", "description": "Record whether a source agent's claim was correct"},
                    "/agent/synthesis": {"method": "POST", "description": "Write a synthesis: one concept node + L3 edges + observation in one call"},
                    "/reliability/{source}": {"method": "GET", "description": "Compute source reliability metrics from historical outcomes"},
                    "/markov/absorbing": {"method": "GET", "description": "Markov absorbing-state risk: probability of reaching an absorbing state. ?start=<node_id>&edge_types=TRANSITIONS_TO,LEADS_TO"},
                    "/markov/expected_steps": {"method": "GET", "description": "Markov expected steps to absorption. ?start=<node_id>&target=<node_id>&edge_types=TRANSITIONS_TO"},
                    "/state": {"method": "POST", "description": "Update agent state/focus"},
                    "/register": {"method": "POST", "description": "Register a new agent"},
                    "/heartbeat": {"method": "POST", "description": "Agent heartbeat with sync"},
                    "/sync": {"method": "POST", "description": "Trigger explicit DuckLake sync (push local writes to shared lake)"},
                    "/tasks": {"method": "GET", "description": "List task nodes with filtering. POST to create a task node (requires id, label)"},
                    "/webhook/{agent}": {"method": "POST", "description": "Register a webhook callback"},
                    "/search": {"method": "GET", "description": "ILIKE text search (?q=QUERY)"},
                    "/semantic_search": {"method": "GET", "description": "Semantic vector search (requires Ollama)"},
                    "/admin/checkpoint": {"method": "POST", "description": "Force DuckDB CHECKPOINT (flush WAL to main DB)"},
                    "/admin/embeddings": {"method": "GET", "description": "Batch generate embeddings for nodes missing them (?batch_size=N&delay_ms=M)"},
                    "/admin/snapshots": {"method": "GET", "description": "List DuckLake snapshots (time-travel)"},
                    "/graph/at": {"method": "GET", "description": "Query graph at snapshot version (?version=N)"},
                    "/graph/changes": {"method": "GET", "description": "Changes between snapshots"},
                },
                "links": {
                    "schema": "/schema",
                    "layers": "/layers",
                    "health": "/health",
                    "docs": "https://github.com/mdlmarkham/OHM",
                },
            },
        )

    def _get_infra_openapi(self, path: str, qs: dict) -> None:
        """GET /openapi.json — OpenAPI 3.0 spec endpoint (ADR-005)."""
        self._json_response(
            200,
            {
                "openapi": "3.0.3",
                "info": {
                    "title": "OHM Daemon API",
                    "version": "0.2.0",
                    "description": "Multi-agent knowledge graph daemon — shared awareness, individual judgment.",
                },
                "servers": [{"url": (f"http://{self.config.get('host', '127.0.0.1')}:{self.config.get('port', 8710)}")}],
                "paths": {
                    "/": {"get": {"summary": "Discovery index", "responses": {"200": {"description": "Route listing"}}}},
                    "/health": {"get": {"summary": "Health check", "responses": {"200": {"description": "OK"}}}},
                    "/ready": {"get": {"summary": "Readiness check", "responses": {"200": {"description": "Ready"}, "503": {"description": "Not ready"}}}},
                    "/metrics": {"get": {"summary": "Prometheus-style metrics", "responses": {"200": {"description": "Metrics"}}}},
                    "/stats": {"get": {"summary": "Graph statistics", "responses": {"200": {"description": "Stats"}}}},
                    "/status": {"get": {"summary": "Daemon status", "responses": {"200": {"description": "Status"}}}},
                    "/schema": {"get": {"summary": "Node/edge types", "responses": {"200": {"description": "Schema"}}}},
                    "/layers": {"get": {"summary": "L1-L4 descriptions", "responses": {"200": {"description": "Layers"}}}},
                    "/node/{id}": {"get": {"summary": "Get node"}, "post": {"summary": "Create node"}},
                    "/edge/{id}": {"get": {"summary": "Get edge"}, "post": {"summary": "Create edge"}},
                    "/neighborhood/{id}": {"get": {"summary": "Graph traversal"}},
                    "/path/{from}/{to}": {"get": {"summary": "Shortest path"}},
                    "/impact/{id}": {"get": {"summary": "Impact analysis"}},
                    "/confidence/{id}": {"get": {"summary": "Confidence audit"}},
                    "/agent/{name}": {"get": {"summary": "Agent state"}},
                    "/agents": {"get": {"summary": "List agents"}},
                    "/nodes": {"get": {"summary": "List nodes with pagination and filtering"}},
                    "/listen": {"get": {"summary": "Change feed"}},
                    "/events": {"get": {"summary": "SSE event stream"}},
                    "/challenge/{id}": {"post": {"summary": "Challenge edge"}},
                    "/support/{id}": {"post": {"summary": "Support edge"}},
                    "/observe/{id}": {"post": {"summary": "Record observation"}},
                    "/observations": {"get": {"summary": "List observations"}, "post": {"summary": "Bulk upload observations"}},
                    "/state": {"post": {"summary": "Update agent state"}},
                    "/register": {"post": {"summary": "Register agent"}},
                    "/heartbeat": {"post": {"summary": "Agent heartbeat"}},
                    "/webhook/{agent}": {"post": {"summary": "Register webhook"}},
                    "/search": {"get": {"summary": "ILIKE text search", "parameters": [{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}]}},
                    "/semantic_search": {
                        "get": {
                            "summary": "Semantic vector search (requires Ollama)",
                            "parameters": [
                                {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                                {"name": "type", "in": "query", "required": False, "schema": {"type": "string"}},
                                {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
                                {"name": "min_confidence", "in": "query", "required": False, "schema": {"type": "number"}},
                            ],
                            "responses": {
                                "200": {"description": "Search results"},
                                "503": {"description": "Ollama not available"},
                            },
                        }
                    },
                    "/admin/checkpoint": {"post": {"summary": "Force CHECKPOINT", "responses": {"200": {"description": "WAL flushed to main DB"}}}},
                    "/graph/at": {"get": {"summary": "Graph at snapshot version", "responses": {"200": {"description": "Historical graph state"}}}},
                    "/graph/changes": {"get": {"summary": "Changes between snapshots", "responses": {"200": {"description": "Insertions/deletions"}}}},
                    "/voi/tasks": {"get": {"summary": "VoI task assignment for agent routing", "responses": {"200": {"description": "Research tasks ranked by VoI"}}}},
                },
            },
        )

    def _get_infra_health(self, path: str, qs: dict) -> None:
        """GET /health — health check (no auth)."""
        from ohm.server.server import _START_TIME

        payload: dict = {
            "status": "ok",
            "uptime": round(time.time() - _START_TIME, 1),
        }
        try:
            from ohm.queries import query_graph_health

            graph = query_graph_health(self.current_store.conn)
            total_nodes = graph.get("total_nodes") or 0
            orphan_nodes = graph.get("orphan_nodes") or 0
            dead_end_count = graph.get("dead_end_count") or 0
            payload["graph"] = {
                "health_score": graph.get("health_score"),
                "node_count": total_nodes,
                "edge_count": graph.get("total_edges"),
                "orphan_count": orphan_nodes,
                "orphan_rate": round(orphan_nodes / total_nodes, 4) if total_nodes else 0,
                "orphan_type_breakdown": graph.get("orphan_type_breakdown", {}),
                "dead_end_count": dead_end_count,
                "dead_end_rate": round(dead_end_count / total_nodes, 4) if total_nodes else 0,
                "low_confidence_count": graph.get("low_confidence_unchallenged"),
            }
        except Exception:
            pass
        self._json_response(200, payload)

    def _get_infra_ready(self, path: str, qs: dict) -> None:
        """GET /ready — readiness check (no auth)."""
        try:
            self.current_store.execute("SELECT 1")
            self._json_response(
                200,
                {
                    "status": "ready",
                    "database": str(self.current_store.db_path),
                },
            )
        except Exception:
            self._json_response(
                503,
                {
                    "status": "not_ready",
                    "database": str(self.current_store.db_path),
                },
            )

    def _get_infra_metrics(self, path: str, qs: dict) -> None:
        """GET /metrics — Prometheus-style metrics (no auth)."""
        from ohm.server import server as _server_module

        with _server_module._metrics_lock:
            metrics_snapshot = dict(_server_module._metrics)
            sorted_lats = sorted(_server_module._request_latencies) if _server_module._request_latencies else [0]
        n = len(sorted_lats)
        uptime = round(time.time() - _server_module._START_TIME, 1)
        p50 = sorted_lats[n // 2] if n > 0 else 0
        p95 = sorted_lats[int(n * 0.95)] if n > 1 else sorted_lats[0] if n > 0 else 0
        p99 = sorted_lats[int(n * 0.99)] if n > 1 else sorted_lats[0] if n > 0 else 0
        lat_max = sorted_lats[-1] if n > 0 else 0

        accept = self.headers.get("Accept", "")
        fmt = qs.get("format", [""])[0]
        if fmt == "prometheus" or "text/plain" in accept:
            lines = [
                "# HELP ohm_uptime_seconds Seconds since daemon started",
                "# TYPE ohm_uptime_seconds gauge",
                f"ohm_uptime_seconds {uptime}",
                "# HELP ohm_requests_total Total HTTP requests",
                "# TYPE ohm_requests_total counter",
                f'ohm_requests_total{{method="all"}} {metrics_snapshot.get("requests_total", 0)}',
                f'ohm_requests_total{{method="get"}} {metrics_snapshot.get("requests_get", 0)}',
                f'ohm_requests_total{{method="post"}} {metrics_snapshot.get("requests_post", 0)}',
                "# HELP ohm_errors_total Total HTTP errors",
                "# TYPE ohm_errors_total counter",
                f'ohm_errors_total{{code="4xx"}} {metrics_snapshot.get("errors_4xx", 0)}',
                f'ohm_errors_total{{code="5xx"}} {metrics_snapshot.get("errors_5xx", 0)}',
                "# HELP ohm_rate_limited_total Requests rejected by rate limiter",
                "# TYPE ohm_rate_limited_total counter",
                f"ohm_rate_limited_total {metrics_snapshot.get('rate_limited', 0)}",
                "# HELP ohm_request_duration_ms Request latency in milliseconds",
                "# TYPE ohm_request_duration_ms summary",
                f'ohm_request_duration_ms{{quantile="0.5"}} {p50}',
                f'ohm_request_duration_ms{{quantile="0.95"}} {p95}',
                f'ohm_request_duration_ms{{quantile="0.99"}} {p99}',
                f"ohm_request_duration_ms_count {n}",
                "",
            ]
            body_bytes = "\n".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)
        else:
            self._json_response(
                200,
                {
                    "uptime_seconds": uptime,
                    "requests": metrics_snapshot,
                    "latency_ms": {
                        "p50": p50,
                        "p95": p95,
                        "p99": p99,
                        "max": lat_max,
                        "sample_count": n,
                    },
                },
            )

    def _get_perf(self, path: str, qs: dict) -> None:
        """GET /perf — per-endpoint latency breakdown (OHM-lqpk.5).

        Returns per-endpoint request count, p50, p95, p99, and max
        latency for the last N requests (max 500 per endpoint).
        Sorted by total time (count × mean) descending so the hottest
        endpoints surface first.
        """
        from ohm.server import server as _server_module

        with _server_module._metrics_lock:
            endpoints_snapshot = dict(_server_module._endpoint_latencies)
            counts_snapshot = dict(_server_module._endpoint_counts)

        endpoints: list[dict] = []
        for key, lats in endpoints_snapshot.items():
            if not lats:
                continue
            sorted_lats = sorted(lats)
            n = len(sorted_lats)
            mean = sum(sorted_lats) / n
            p50 = sorted_lats[n // 2]
            p95 = sorted_lats[int(n * 0.95)] if n > 1 else sorted_lats[0]
            p99 = sorted_lats[int(n * 0.99)] if n > 1 else sorted_lats[0]
            total = counts_snapshot.get(key, n)
            endpoints.append(
                {
                    "endpoint": key,
                    "count": total,
                    "p50_ms": round(p50, 2),
                    "p95_ms": round(p95, 2),
                    "p99_ms": round(p99, 2),
                    "max_ms": round(sorted_lats[-1], 2),
                    "mean_ms": round(mean, 2),
                    "total_time_ms": round(total * mean, 2),
                }
            )

        endpoints.sort(key=lambda e: e["total_time_ms"], reverse=True)

        perf_log_enabled = _server_module._perf_log_file is not None
        self._json_response(
            200,
            {
                "endpoints": endpoints,
                "endpoint_count": len(endpoints),
                "perf_log_enabled": perf_log_enabled,
                "perf_log_file": _server_module._perf_log_file,
            },
        )

    def _get_webhooks_dead_letter(self, path: str, qs: dict) -> None:
        """GET /webhooks/dead-letter — List failed webhook deliveries for manual retry."""
        auth = self._get_auth_token()
        self._get_allowed_agents(auth)
        self._set_extra_cors_headers()
        self._check_ready()

        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])

        rows = self.current_store.db.execute(
            "SELECT id, agent_id, event_type, payload, error, attempt_count, created_at FROM webhook_dead_letter ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

        items = [dict(zip(["id", "agent_id", "event_type", "payload", "error", "attempt_count", "created_at"], r)) for r in rows]
        total = self.current_store.db.execute("SELECT count(*) FROM webhook_dead_letter").fetchone()[0]

        self._write_json(200, {"items": items, "total": total, "limit": limit, "offset": offset})

    def _get_webhooks_outbox(self, path: str, qs: dict) -> None:
        """GET /webhooks/outbox — List pending/failed webhook deliveries."""
        auth = self._get_auth_token()
        self._get_allowed_agents(auth)
        self._set_extra_cors_headers()
        self._check_ready()

        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        status = qs.get("status", [None])[0]

        if status:
            rows = self.current_store.db.execute(
                "SELECT id, customer_id, agent, event_type, event, status, attempts, next_retry FROM ohm_webhook_outbox WHERE status = ? ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = self.current_store.db.execute(
                "SELECT id, customer_id, agent, event_type, event, status, attempts, next_retry FROM ohm_webhook_outbox ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

        items = [dict(zip(["id", "customer_id", "agent", "event_type", "event", "status", "attempts", "next_retry"], r)) for r in rows]
        total = self.current_store.db.execute("SELECT count(*) FROM ohm_webhook_outbox").fetchone()[0]

        self._write_json(200, {"items": items, "total": total, "limit": limit, "offset": offset})
