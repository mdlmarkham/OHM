"""Transport-agnostic request builder for OHM MCP tools.

Maps an MCP tool name and its JSON arguments to the HTTP method, path, and
body that the OHM daemon expects. Used by both the local stdio sidecar
(``ohm.mcp.server``) and the hosted FastMCP gateway (``ohm.mcp.gateway``).

Keeping the mapping in one place guarantees that local and remote transports
hit the same endpoints with the same parameter shapes.
"""

from __future__ import annotations

import json
from typing import Any


def build_request(name: str, arguments: dict[str, Any], agent_id: str) -> tuple[str, str, dict[str, Any] | None]:
    """Return ``(method, path, body)`` for an OHM MCP tool call.

    The returned ``path`` includes the query string when method is ``GET``.
    ``body`` is ``None`` for GET requests. The ``agent_id`` is used as the
    default provenance/source for writes.

    Raises:
        KeyError: if a required argument is missing.
    """
    # ── Read tier ──
    if name == "ohm_stats":
        return "GET", "/stats", None

    if name == "ohm_search":
        params: dict[str, str] = {"q": arguments["q"]}
        if arguments.get("type"):
            params["type"] = arguments["type"]
        if arguments.get("created_by"):
            params["created_by"] = arguments["created_by"]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        return "GET", _qs("/search", params), None

    if name == "ohm_get_node":
        return "GET", f"/node/{arguments['node_id']}", None

    if name == "ohm_neighborhood":
        params = {}
        if arguments.get("depth") is not None:
            params["depth"] = str(arguments["depth"])
        if arguments.get("layer"):
            params["layer"] = arguments["layer"]
        return "GET", _qs(f"/neighborhood/{arguments['node_id']}", params), None

    if name == "ohm_listen":
        params = {
            "enrich": str(arguments.get("enrich", True)).lower(),
            "limit": str(arguments.get("limit", 50)),
        }
        if arguments.get("since"):
            params["since"] = arguments["since"]
        if arguments.get("agent"):
            params["agent"] = arguments["agent"]
        return "GET", _qs("/listen", params), None

    if name == "ohm_confidence":
        return "GET", f"/confidence/{arguments['edge_id']}", None

    if name == "ohm_path":
        return "GET", f"/path/{arguments['from_id']}/{arguments['to_id']}", None

    if name == "ohm_agents":
        return "GET", "/agents", None

    # ── Inference / analysis tier ──
    if name == "ohm_inference":
        params = {"target": arguments["target"]}
        if arguments.get("evidence"):
            params["evidence"] = arguments["evidence"]
        if arguments.get("layers"):
            params["layers"] = arguments["layers"]
        if arguments.get("leak") is not None:
            params["leak"] = str(arguments["leak"])
        return "GET", _qs("/inference", params), None

    if name == "ohm_intervene":
        params = {
            "target": arguments["target"],
            "state": str(arguments["state"]),
        }
        if arguments.get("query"):
            params["query"] = arguments["query"]
        if arguments.get("layers"):
            params["layers"] = arguments["layers"]
        if arguments.get("leak") is not None:
            params["leak"] = str(arguments["leak"])
        return "GET", _qs("/intervene", params), None

    if name == "ohm_voi":
        params = {"decision": arguments["decision"]}
        if arguments.get("top") is not None:
            params["top"] = str(arguments["top"])
        if arguments.get("layers"):
            params["layers"] = arguments["layers"]
        if arguments.get("leak") is not None:
            params["leak"] = str(arguments["leak"])
        return "GET", _qs("/voi", params), None

    if name == "ohm_decision_recommend":
        node_id = arguments["node_id"]
        return "GET", f"/decision/{node_id}/recommendation", None

    if name == "ohm_refute":
        params = {
            "cause": arguments["cause"],
            "effect": arguments["effect"],
        }
        if arguments.get("n_samples") is not None:
            params["n_samples"] = str(arguments["n_samples"])
        if arguments.get("methods"):
            params["methods"] = arguments["methods"]
        return "GET", _qs("/refute", params), None

    if name == "ohm_belief":
        params: dict[str, str] = {"target": arguments["target"]}
        if arguments.get("evidence"):
            params["evidence"] = arguments["evidence"]
        if arguments.get("layers"):
            params["layers"] = arguments["layers"]
        if arguments.get("edge_types"):
            params["edge_types"] = arguments["edge_types"]
        if arguments.get("leak") is not None:
            params["leak"] = str(arguments["leak"])
        if arguments.get("include_evidence_movers") is not None:
            params["include_evidence_movers"] = str(arguments["include_evidence_movers"]).lower()
        if arguments.get("include_prior") is not None:
            params["include_prior"] = str(arguments["include_prior"]).lower()
        if arguments.get("belief_statement"):
            params["belief_statement"] = arguments["belief_statement"]
        return "GET", _qs("/belief", params), None

    if name == "ohm_discover":
        params: dict[str, str] = {}
        if arguments.get("nodes"):
            params["nodes"] = arguments["nodes"]
        if arguments.get("method"):
            params["method"] = arguments["method"]
        if arguments.get("alpha") is not None:
            params["alpha"] = str(arguments["alpha"])
        if arguments.get("min_observations") is not None:
            params["min_observations"] = str(arguments["min_observations"])
        return "GET", _qs("/discover", params), None

    if name == "ohm_pert":
        return "GET", f"/inference?target={arguments['target']}&pert=1", None

    if name == "ohm_monte_carlo":
        target = arguments["target"]
        n = arguments.get("n_simulations", 1000)
        return "GET", f"/monte-carlo/{target}?n_simulations={n}", None

    if name == "ohm_markov":
        target = arguments["target"]
        analysis = arguments.get("analysis", "absorbing")
        if analysis == "expected_steps":
            return "GET", f"/markov/expected_steps?start={target}", None
        return "GET", f"/markov/absorbing?start={target}", None

    if name == "ohm_game":
        return "GET", f"/game?target={arguments['target']}", None

    # ── Write tier ──
    if name == "ohm_create_node":
        body: dict[str, Any] = {
            "id": arguments["id"],
            "label": arguments["label"],
            "node_type": arguments.get("node_type", "concept"),
            "confidence": arguments.get("confidence", 0.5),
            "visibility": arguments.get("visibility", "team"),
            "provenance": arguments.get("provenance", agent_id),
        }
        if arguments.get("content"):
            body["content"] = arguments["content"]
        if arguments.get("tags"):
            tags_str = arguments["tags"]
            body["tags"] = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
        url = "/node"
        if not arguments.get("create_only", True):
            url += "?create_only=false"
        return "POST", url, body

    if name == "ohm_create_edge":
        body = {
            "from": arguments["from_node"],
            "to": arguments["to_node"],
            "type": arguments["edge_type"],
            "layer": arguments.get("layer", "L3"),
            "confidence": arguments.get("confidence", 0.5),
            "provenance": arguments.get("provenance", agent_id),
        }
        if arguments.get("condition"):
            body["condition"] = arguments["condition"]
        return "POST", "/edge", body

    if name == "ohm_observe":
        body = {
            "node_id": arguments["node_id"],
            "obs_type": arguments["obs_type"],
            "value": arguments["value"],
            "sigma": arguments.get("sigma", 1.0),
            "source": arguments.get("source", agent_id),
        }
        if arguments.get("notes"):
            body["notes"] = arguments["notes"]
        if arguments.get("idempotency_key"):
            body["idempotency_key"] = arguments["idempotency_key"]
        return "POST", f"/observe/{arguments['node_id']}", body

    if name == "ohm_observe_batch":
        observations = arguments.get("observations", [])
        if len(observations) > 1000:
            raise ValueError(f"ohm_observe_batch: max 1000 observations, got {len(observations)}")
        # Inject agent_id as default source for items that don't specify one
        for obs in observations:
            if isinstance(obs, dict):
                obs.setdefault("source", agent_id)
        body: dict[str, Any] = {"observations": observations}
        return "POST", "/observations", body

    if name == "ohm_challenge":
        body = {
            "reason": arguments["reason"],
            "confidence": arguments.get("confidence", 0.5),
        }
        return "POST", f"/challenge/{arguments['edge_id']}", body

    if name == "ohm_support":
        body = {
            "reason": arguments["reason"],
            "confidence": arguments.get("confidence", 0.7),
        }
        return "POST", f"/support/{arguments['edge_id']}", body

    if name == "ohm_batch":
        nodes = arguments.get("nodes", [])
        edges = arguments.get("edges", [])
        total = len(nodes) + len(edges)
        if total > 500:
            raise ValueError(f"ohm_batch: max 500 combined items, got {total}")
        # OHM-773: Auto-inject agent_id as default provenance for items that don't specify one
        for node in nodes:
            if isinstance(node, dict):
                node.setdefault("provenance", agent_id)
        for edge in edges:
            if isinstance(edge, dict):
                edge.setdefault("provenance", agent_id)
        body: dict[str, Any] = {"nodes": nodes, "edges": edges}
        return "POST", "/batch", body

    # ── Update / utility tier ──
    if name == "ohm_update_state":
        body = {"agent": agent_id}
        if arguments.get("focus"):
            body["focus"] = arguments["focus"]
        if arguments.get("patterns"):
            patterns = arguments["patterns"]
            body["patterns"] = json.loads(patterns) if isinstance(patterns, str) else patterns
        if arguments.get("services"):
            services = arguments["services"]
            body["services"] = json.loads(services) if isinstance(services, str) else services
        return "POST", "/state", body

    if name == "ohm_list_nodes":
        params = {}
        if arguments.get("type"):
            params["type"] = arguments["type"]
        if arguments.get("label_contains"):
            params["label_contains"] = arguments["label_contains"]
        if arguments.get("created_by"):
            params["created_by"] = arguments["created_by"]
        params["limit"] = str(arguments.get("limit", 100))
        params["offset"] = str(arguments.get("offset", 0))
        return "GET", _qs("/nodes", params), None

    if name == "ohm_domain_onboarding":
        return "GET", "/schema", None

    if name == "ohm_backend_status":
        return "GET", "/backend/status", None

    if name == "ohm_storage_efficiency":
        return "GET", "/storage/efficiency", None

    if name == "ohm_admin_graph_health":
        params: dict[str, str] = {}
        if arguments.get("stale_observation_days") is not None:
            params["stale_observation_days"] = str(arguments["stale_observation_days"])
        if arguments.get("low_confidence_threshold") is not None:
            params["low_confidence_threshold"] = str(arguments["low_confidence_threshold"])
        if arguments.get("orphan_limit") is not None:
            params["orphan_limit"] = str(arguments["orphan_limit"])
        return "GET", _qs("/admin/graph-health", params), None

    if name == "ohm_source_reliability":
        params: dict[str, str] = {}
        if arguments.get("agent_id"):
            params["agent_id"] = arguments["agent_id"]
        return "GET", _qs("/agent/reliability", params), None

    if name == "ohm_my_calibration":
        return "GET", "/agent/calibration", None

    if name == "ohm_list_instances":
        # Local-only operation; not meaningful for a hosted gateway.
        raise NotImplementedError("ohm_list_instances is not supported by the hosted gateway; use the local ohm-mcp sidecar for instance registry access.")

    if name in ("ohm_list_profiles", "ohm_select_profile"):
        # Profile management is per-sidecar state; the gateway resolves profiles
        # from the Authorization header on every request.
        raise NotImplementedError(f"{name} is not supported by the hosted gateway; use the local ohm-mcp sidecar for profile switching.")

    # ── Prospect lifecycle tier (OHM-844) ──
    if name == "ohm_prospect_create":
        body: dict[str, object] = {"label": arguments["label"]}
        if arguments.get("authority"):
            body["authority"] = arguments["authority"]
        if arguments.get("parent_scenario_id"):
            body["parent_scenario_id"] = arguments["parent_scenario_id"]
        if arguments.get("planned_start"):
            body["planned_start"] = arguments["planned_start"]
        if arguments.get("planned_end"):
            body["planned_end"] = arguments["planned_end"]
        if arguments.get("horizon_label"):
            body["horizon_label"] = arguments["horizon_label"]
        if arguments.get("tags"):
            body["tags"] = arguments["tags"]
        if arguments.get("content"):
            body["content"] = arguments["content"]
        if arguments.get("connects_to"):
            body["connects_to"] = arguments["connects_to"]
        if arguments.get("confidence") is not None:
            body["confidence"] = arguments["confidence"]
        return "POST", "/prospect", body

    if name == "ohm_prospect_transition":
        return "POST", "/prospect/transition/" + arguments["prospect_id"], {
            "new_status": arguments["new_status"],
            **({"reason": arguments["reason"]} if arguments.get("reason") else {}),
        }

    if name == "ohm_prospect_list":
        import urllib.parse
        parts: list[str] = []
        if arguments.get("status"):
            parts.append(f"status={urllib.parse.quote(arguments['status'])}")
        if arguments.get("tags"):
            for tag in arguments["tags"]:
                parts.append(f"tags={urllib.parse.quote(tag)}")
        if arguments.get("created_by"):
            parts.append(f"created_by={urllib.parse.quote(arguments['created_by'])}")
        parts.append(f"limit={arguments.get('limit', 20)}")
        return "GET", "/prospects?" + "&".join(parts), None

    if name == "ohm_prospect_detail":
        return "GET", "/prospect/" + arguments["prospect_id"], None

    # ── Monte Carlo prospect simulation (OHM-843) ──
    if name == "ohm_simulate":
        body: dict[str, object] = {}
        if arguments.get("n_iterations") is not None:
            body["n_iterations"] = arguments["n_iterations"]
        if arguments.get("seed") is not None:
            body["seed"] = arguments["seed"]
        return "POST", "/simulate/" + arguments["prospect_id"], body

    # ── Skill maintenance loop (OHM-854) ──
    if name == "ohm_skill_maintenance":
        body = {"dry_run": arguments.get("dry_run", False)}
        return "POST", "/admin/skill-maintenance/run", body

    # ── Temporal Planning (OHM-937) ────────────────────────────────────
    if name == "ohm_plan_create":
        body = {"plan_type": arguments["plan_type"]}
        for key in ("plan_id", "node_id", "label", "start_ts", "end_ts", "horizon", "status", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/plan/create", body

    if name == "ohm_event_create":
        body = {"node_id": arguments["node_id"], "event_class": arguments["event_class"], "start_ts": arguments["start_ts"]}
        for key in ("event_id", "plan_id", "title", "end_ts", "horizon", "operating_state", "description", "confidence", "authority", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/event/create", body

    if name == "ohm_event_link":
        body = {"from_event_id": arguments["from_event_id"], "to_event_id": arguments["to_event_id"], "edge_type": arguments["edge_type"]}
        for key in ("layer", "confidence", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/event/link", body

    if name == "ohm_report_create":
        body = {"report_type": arguments["report_type"]}
        for key in ("report_id", "node_id", "plan_id", "title", "summary", "findings", "recommendations", "confidence_adjustments", "status", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/report/create", body

    if name == "ohm_report_finalize":
        body = {"report_id": arguments["report_id"]}
        if arguments.get("confidence_adjustments") is not None:
            body["confidence_adjustments"] = arguments["confidence_adjustments"]
        return "POST", "/report/finalize", body

    if name == "ohm_run_create":
        body = {"run_type": arguments["run_type"]}
        for key in ("run_id", "report_id", "node_id", "inputs", "status", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/run/create", body

    if name == "ohm_run_complete":
        body = {"run_id": arguments["run_id"]}
        for key in ("status", "outputs", "error", "duration_ms"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/run/complete", body

    if name == "ohm_rul_register":
        body = {"equipment_node_id": arguments["equipment_node_id"], "rul_days": arguments["rul_days"], "risk_class": arguments["risk_class"]}
        for key in ("model_version", "site_id", "node_path", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/rul/register", body

    if name == "ohm_scenario_run":
        body: dict[str, object] = {"node_id": arguments["node_id"]}
        for key in ("failure_probability", "max_depth", "edge_overrides", "node_interventions", "disabled_edges", "disabled_nodes", "compare", "persist", "label", "tags"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/scenario/run", body

    if name == "ohm_scenarios":
        import urllib.parse
        params: dict[str, str] = {}
        if arguments.get("target_node_id"):
            params["target_node_id"] = arguments["target_node_id"]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        qs = ("?" + urllib.parse.urlencode(sorted(params.items()))) if params else ""
        return "GET", "/scenarios" + qs, None

    if name == "ohm_verifiable_claims":
        import urllib.parse
        params = {}
        if arguments.get("agent"):
            params["agent"] = arguments["agent"]
        if arguments.get("days_threshold") is not None:
            params["days_threshold"] = str(arguments["days_threshold"])
        if arguments.get("confidence_threshold") is not None:
            params["confidence_threshold"] = str(arguments["confidence_threshold"])
        qs = ("?" + urllib.parse.urlencode(sorted(params.items()))) if params else ""
        return "GET", "/verifiable-claims" + qs, None

    if name == "ohm_record_verification_outcome":
        body = {"edge_id": arguments["edge_id"], "outcome": arguments["outcome"]}
        if arguments.get("reason") is not None:
            body["reason"] = arguments["reason"]
        return "POST", "/verification/outcome", body

    if name == "ohm_drifts":
        import urllib.parse
        params = {}
        for key in ("plan_id", "drift_type", "severity"):
            if arguments.get(key):
                params[key] = arguments[key]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        qs = ("?" + urllib.parse.urlencode(sorted(params.items()))) if params else ""
        return "GET", "/drifts" + qs, None

    if name == "ohm_reconcile":
        body: dict[str, object] = {}
        if arguments.get("plan_id"):
            body["plan_id"] = arguments["plan_id"]
        if arguments.get("horizon"):
            body["horizon"] = arguments["horizon"]
        if arguments.get("dry_run") is not None:
            body["dry_run"] = arguments["dry_run"]
        if arguments.get("tolerance"):
            body["tolerance"] = arguments["tolerance"]
        if arguments.get("created_by"):
            body["created_by"] = arguments["created_by"]
        return "POST", "/reconcile", body

    if name == "ohm_drift_explain":
        import urllib.parse
        params = {"drift_id": arguments["drift_id"]}
        if arguments.get("top") is not None:
            params["top"] = str(arguments["top"])
        return "GET", "/drift/explain?" + urllib.parse.urlencode(sorted(params.items())), None

    if name == "ohm_forecast_create":
        body: dict[str, object] = {
            "label": arguments["label"],
            "target_node_id": arguments["target_node_id"],
            "horizon": arguments["horizon"],
        }
        for key in ("predicted_value", "predicted_unit", "distribution", "assumptions", "model_id", "connects_to", "metadata"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/forecast/create", body

    if name == "ohm_forecast_list":
        import urllib.parse
        params = {}
        for key in ("target_node_id", "horizon", "status", "created_by"):
            if arguments.get(key):
                params[key] = arguments[key]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        qs = ("?" + urllib.parse.urlencode(sorted(params.items()))) if params else ""
        return "GET", "/forecasts" + qs, None

    if name == "ohm_forecast_get":
        return "GET", "/forecast/" + arguments["forecast_id"], None

    if name == "ohm_forecast_transition":
        body: dict[str, object] = {
            "forecast_id": arguments["forecast_id"],
            "new_status": arguments["new_status"],
        }
        if arguments.get("reason"):
            body["reason"] = arguments["reason"]
        return "POST", "/forecast/transition", body

    if name == "ohm_forecast_resolve":
        return "POST", "/forecast/resolve", {
            "forecast_id": arguments["forecast_id"],
            "actual_value": arguments["actual_value"],
        }

    if name == "ohm_scenario_get":
        return "GET", "/scenario/" + arguments["scenario_id"], None

    if name == "ohm_scenario_rerun":
        return "POST", "/scenario/rerun", {"scenario_id": arguments["scenario_id"]}

    if name == "ohm_scenario_diff":
        return "GET", "/scenario/" + arguments["scenario_id"] + "/diff", None

    if name == "ohm_series_query":
        import urllib.parse
        params = {"series_id": arguments["series_id"]}
        for key in ("start", "end"):
            if arguments.get(key):
                params[key] = arguments[key]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        return "GET", "/series/query?" + urllib.parse.urlencode(sorted(params.items())), None

    if name == "ohm_series_baseline":
        import urllib.parse
        params = {"series_id": arguments["series_id"]}
        if arguments.get("method"):
            params["method"] = arguments["method"]
        return "GET", "/series/baseline?" + urllib.parse.urlencode(sorted(params.items())), None

    if name == "ohm_series_anomalies":
        import urllib.parse
        params = {"series_id": arguments["series_id"]}
        if arguments.get("method"):
            params["method"] = arguments["method"]
        if arguments.get("sigma") is not None:
            params["sigma"] = str(arguments["sigma"])
        return "GET", "/series/anomalies?" + urllib.parse.urlencode(sorted(params.items())), None

    if name == "ohm_propose_correction":
        body: dict[str, object] = {"old_node_id": arguments["old_node_id"], "reason": arguments["reason"]}
        for key in ("field", "old_value", "new_value", "new_node_id", "evidence_node_ids", "severity"):
            if arguments.get(key) is not None:
                body[key] = arguments[key]
        return "POST", "/correction/propose", body

    if name == "ohm_commit_correction":
        return "POST", "/correction/commit", {"correction_id": arguments["correction_id"]}

    if name == "ohm_reject_correction":
        body: dict[str, object] = {"correction_id": arguments["correction_id"]}
        if arguments.get("rejection_reason"):
            body["rejection_reason"] = arguments["rejection_reason"]
        return "POST", "/correction/reject", body

    if name == "ohm_corrections":
        import urllib.parse
        params = {}
        if arguments.get("node_id"):
            params["node_id"] = arguments["node_id"]
        if arguments.get("status"):
            params["status"] = arguments["status"]
        if arguments.get("limit") is not None:
            params["limit"] = str(arguments["limit"])
        qs = ("?" + urllib.parse.urlencode(sorted(params.items()))) if params else ""
        return "GET", "/corrections" + qs, None

    raise KeyError(f"Unknown tool: {name}")


def _qs(path: str, params: dict[str, str]) -> str:
    """Append a URL query string built from a parameter dict."""
    import urllib.parse

    if not params:
        return path
    encoded = urllib.parse.urlencode(sorted(params.items()))
    return f"{path}?{encoded}"
