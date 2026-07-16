"""Temporal planning handler mixin for OHM-937.

Only contains NEW endpoints not already provided by ``ReportsHandlerMixin``
(plans, reports, runs, RUL GETs and detail routes).  The pre-existing
``temporal.py`` owns decision-freshness / mode-switch / twin-design from #862.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ohm.server.handlers._base import OhmHandlerBase

if TYPE_CHECKING:
    pass


class TemporalPlanningHandlerMixin(OhmHandlerBase):
    """Handler mixin for temporal planning MCP endpoints (OHM-937)."""

    # ── Plans (POST only — GET /plans served by ReportsHandlerMixin) ─────

    def _post_plan_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        import uuid
        from ohm.graph.queries.plans_events import create_plan
        from ohm.exceptions import ValidationError

        plan_type = body.get("plan_type")
        if not plan_type:
            raise ValidationError("plan_type is required")

        plan_id = body.get("plan_id") or f"plan-{uuid.uuid4().hex[:12]}"
        result = create_plan(
            self.current_store.conn,
            plan_id=plan_id,
            node_id=body.get("node_id"),
            plan_type=plan_type,
            label=body.get("label"),
            start_ts=body.get("start_ts"),
            end_ts=body.get("end_ts"),
            horizon=body.get("horizon"),
            status=body.get("status", "active"),
            created_by=agent,
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    # ── Events ───────────────────────────────────────────────────────────

    def _post_event_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        import uuid
        from ohm.graph.queries.plans_events import create_event
        from ohm.exceptions import ValidationError

        node_id = body.get("node_id")
        event_class = body.get("event_class")
        start_ts = body.get("start_ts")
        if not node_id or not event_class or not start_ts:
            raise ValidationError("node_id, event_class, and start_ts are required")

        result = create_event(
            self.current_store.conn,
            event_id=body.get("event_id") or f"evt-{uuid.uuid4().hex[:12]}",
            plan_id=body.get("plan_id"),
            node_id=node_id,
            event_class=event_class,
            title=body.get("title"),
            start_ts=start_ts,
            end_ts=body.get("end_ts"),
            horizon=body.get("horizon"),
            operating_state=body.get("operating_state"),
            description=body.get("description"),
            confidence=body.get("confidence"),
            authority=body.get("authority"),
            created_by=agent,
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    def _post_event_link_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        import uuid
        from ohm.graph.queries.plans_events import create_event_link
        from ohm.exceptions import ValidationError

        from_event_id = body.get("from_event_id")
        to_event_id = body.get("to_event_id")
        edge_type = body.get("edge_type")
        if not from_event_id or not to_event_id or not edge_type:
            raise ValidationError("from_event_id, to_event_id, and edge_type are required")

        result = create_event_link(
            self.current_store.conn,
            link_id=body.get("link_id") or f"lnk-{uuid.uuid4().hex[:12]}",
            from_event_id=from_event_id,
            to_event_id=to_event_id,
            edge_type=edge_type,
            layer=body.get("layer", "L1"),
            confidence=body.get("confidence", 1.0),
            created_by=agent,
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    # ── Reports (POST only — GET /reports served by ReportsHandlerMixin) ─

    def _post_report_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        import uuid
        from ohm.graph.queries.reports import create_report
        from ohm.exceptions import ValidationError

        report_type = body.get("report_type")
        if not report_type:
            raise ValidationError("report_type is required")

        result = create_report(
            self.current_store.conn,
            report_id=body.get("report_id") or f"rpt-{uuid.uuid4().hex[:12]}",
            report_type=report_type,
            node_id=body.get("node_id"),
            plan_id=body.get("plan_id"),
            title=body.get("title"),
            summary=body.get("summary"),
            findings=body.get("findings"),
            recommendations=body.get("recommendations"),
            confidence_adjustments=body.get("confidence_adjustments"),
            status=body.get("status", "draft"),
            created_by=agent,
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    def _post_report_finalize(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.graph.queries.reports import finalize_report
        from ohm.exceptions import ValidationError

        report_id = body.get("report_id")
        if not report_id:
            raise ValidationError("report_id is required")

        result = finalize_report(
            self.current_store.conn,
            report_id=report_id,
            confidence_adjustments=body.get("confidence_adjustments"),
            created_by=agent,
        )
        self._json_response(200, result)

    # ── Runs (POST only — GET /runs served by ReportsHandlerMixin) ───────

    def _post_run_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        import uuid
        from ohm.graph.queries.runs import create_run
        from ohm.exceptions import ValidationError

        run_type = body.get("run_type")
        if not run_type:
            raise ValidationError("run_type is required")

        result = create_run(
            self.current_store.conn,
            run_id=body.get("run_id") or f"run-{uuid.uuid4().hex[:12]}",
            run_type=run_type,
            report_id=body.get("report_id"),
            node_id=body.get("node_id"),
            inputs=body.get("inputs"),
            status=body.get("status", "pending"),
            created_by=agent,
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    def _post_run_complete(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.graph.queries.runs import complete_run
        from ohm.exceptions import ValidationError

        run_id = body.get("run_id")
        if not run_id:
            raise ValidationError("run_id is required")

        result = complete_run(
            self.current_store.conn,
            run_id=run_id,
            status=body.get("status", "completed"),
            outputs=body.get("outputs"),
            error=body.get("error"),
            duration_ms=body.get("duration_ms"),
            created_by=agent,
        )
        self._json_response(200, result)

    # ── RUL (POST only — GET /rul served by ReportsHandlerMixin) ─────────

    def _post_rul_register(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.graph.queries.rul import register_rul_assessment
        from ohm.exceptions import ValidationError

        equipment_node_id = body.get("equipment_node_id")
        rul_days = body.get("rul_days")
        risk_class = body.get("risk_class")
        if not equipment_node_id or rul_days is None or not risk_class:
            raise ValidationError("equipment_node_id, rul_days, and risk_class are required")

        result = register_rul_assessment(
            self.current_store.conn,
            equipment_node_id=equipment_node_id,
            rul_days=float(rul_days),
            risk_class=risk_class,
            model_version=body.get("model_version"),
            site_id=body.get("site_id"),
            node_path=body.get("node_path"),
            metadata=body.get("metadata"),
            created_by=agent,
        )
        self._json_response(201, result)

    # ── Scenario Run (new POST — distinct from POST /scenario) ───────────

    def _post_scenario_run(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.graph.queries.cascade_scenario import query_compare_scenarios, query_counterfactual_cascade
        from ohm.graph.queries import create_node as _create_node, create_edge as _create_edge
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
        persist = body.get("persist", False)

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

        if persist:
            scenario_metadata = {
                "scenario_type": "counterfactual",
                "baseline_node_id": node_id,
                "edge_overrides": edge_overrides,
                "node_interventions": node_interventions,
                "disabled_edges": list(disabled_edges),
                "disabled_nodes": list(disabled_nodes),
                "failure_probability": failure_probability,
                "max_depth": max_depth,
                "compare": compare,
                "result_summary": result.get("summary", {}),
            }
            scenario_node = _create_node(
                self.current_store.conn,
                label=body.get("label", f"Scenario for {node_id}"),
                node_type="scenario",
                created_by=agent,
                tags=body.get("tags", []),
                metadata=scenario_metadata,
            )
            _create_edge(
                self.current_store.conn,
                from_node=scenario_node["id"],
                to_node=node_id,
                edge_type="SCENARIO_FOR",
                layer="L3",
                created_by=agent,
            )
            result["scenario_node_id"] = scenario_node["id"]

        self._json_response(200, result)

    def _get_scenarios(self, path: str, qs: dict) -> None:
        from ohm.graph.queries._shared import _rows_to_dicts

        target_id = qs.get("target_node_id", [None])[0]
        limit = int(qs.get("limit", ["50"])[0])
        if target_id:
            rows = _rows_to_dicts(
                self.current_store.read_conn.execute(
                    "SELECT n.* FROM ohm_nodes n "
                    "JOIN ohm_edges e ON e.to_node = n.id AND e.edge_type = 'SCENARIO_FOR' "
                    "WHERE n.type = 'scenario' AND n.deleted_at IS NULL AND e.from_node = ? "
                    "ORDER BY n.created_at DESC LIMIT ?",
                    [target_id, limit],
                )
            )
        else:
            rows = _rows_to_dicts(
                self.current_store.read_conn.execute(
                    "SELECT * FROM ohm_nodes WHERE type = 'scenario' AND deleted_at IS NULL "
                    "ORDER BY created_at DESC LIMIT ?",
                    [limit],
                )
            )
        self._json_response(200, {"scenarios": rows, "count": len(rows)})

    def _get_scenario_detail(self, path: str, qs: dict) -> None:
        from ohm.graph.queries.scenario_persist import get_scenario
        from ohm.exceptions import NodeNotFoundError

        scenario_id = path.rstrip("/").split("/")[-1]
        result = get_scenario(self.current_store.read_conn, scenario_id)
        if not result:
            raise NodeNotFoundError(f"Scenario {scenario_id} not found")
        self._json_response(200, result)

    def _post_scenario_rerun(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.scenario_persist import rerun_scenario

        scenario_id = body.get("scenario_id")
        if not scenario_id:
            raise ValidationError("scenario_id is required")

        result = rerun_scenario(self.current_store.conn, scenario_id, created_by=agent)
        self._json_response(200, result)

    def _get_scenario_diff(self, path: str, qs: dict) -> None:
        from ohm.graph.queries.scenario_persist import diff_scenario
        from ohm.exceptions import NodeNotFoundError

        scenario_id = path.rstrip("/").split("/")[0]
        try:
            result = diff_scenario(self.current_store.read_conn, scenario_id)
        except ValueError:
            raise NodeNotFoundError(f"Scenario {scenario_id} not found")
        self._json_response(200, result)

    def _route_scenario_get_or_diff(self, path: str, qs: dict) -> None:
        """Route /scenario/{id} or /scenario/{id}/diff to the right handler."""
        stripped = path.rstrip("/")
        if stripped.endswith("/diff"):
            self._get_scenario_diff(path[len("/scenario/"):], qs)
        else:
            self._get_scenario_detail(path[len("/scenario/"):], qs)

    # ── Verification Outcomes ────────────────────────────────────────────

    def _get_verifiable_claims(self, path: str, qs: dict) -> None:
        from ohm.graph.queries.verification import detect_verifiable_claims

        claims = detect_verifiable_claims(
            self.current_store.read_conn,
            agent=qs.get("agent", [None])[0],
            days_threshold=int(qs.get("days_threshold", ["14"])[0]),
            confidence_threshold=float(qs.get("confidence_threshold", ["0.85"])[0]),
        )
        self._json_response(200, {"claims": claims, "count": len(claims)})

    def _post_record_verification_outcome(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.graph.queries.verification import record_verification_outcome
        from ohm.exceptions import ValidationError

        edge_id = body.get("edge_id")
        outcome = body.get("outcome")
        if not edge_id or outcome is None:
            raise ValidationError("edge_id and outcome are required")

        result = record_verification_outcome(
            self.current_store.conn,
            edge_id=edge_id,
            outcome=outcome,
            recorded_by=agent,
            reason=body.get("reason"),
        )
        self._json_response(200, result)

    # ── Drift Observations ───────────────────────────────────────────────

    def _get_drift_list(self, path: str, qs: dict) -> None:
        from ohm.graph.queries._shared import _rows_to_dicts

        plan_id = qs.get("plan_id", [None])[0]
        drift_type = qs.get("drift_type", [None])[0]
        severity = qs.get("severity", [None])[0]
        limit = int(qs.get("limit", ["50"])[0])

        conditions = [
            "o.type = 'anomaly'",
            "o.deleted_at IS NULL",
            "json_extract_string(o.metadata, '$.drift_type') IS NOT NULL",
        ]
        params: list = []
        if plan_id:
            conditions.append("json_extract_string(o.metadata, '$.plan_id') = ?")
            params.append(plan_id)
        if drift_type:
            conditions.append("json_extract_string(o.metadata, '$.drift_type') = ?")
            params.append(drift_type)
        if severity:
            conditions.append("json_extract_string(o.metadata, '$.severity') = ?")
            params.append(severity)
        params.append(limit)

        where = " AND ".join(conditions)
        drifts = _rows_to_dicts(
            self.current_store.conn.execute(
                f"SELECT o.* FROM ohm_observations o WHERE {where} "
                "ORDER BY o.created_at DESC LIMIT ?",
                params,
            )
        )
        self._json_response(200, {"drifts": drifts, "count": len(drifts)})

    # ── Reconciliation (OHM-940 / Stage 3) ────────────────────────────────

    def _post_reconcile(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.temporal.reconciliation import reconcile_plan_actuals

        plan_id = body.get("plan_id")
        dry_run = body.get("dry_run", False)
        horizon = body.get("horizon")
        tolerance = body.get("tolerance")
        created_by = body.get("created_by", agent)

        result = reconcile_plan_actuals(
            self.current_store.conn,
            plan_id=plan_id,
            horizon=horizon,
            dry_run=dry_run,
            tolerance=tolerance,
            created_by=created_by,
        )
        self._json_response(200, result)

    def _get_drift_explain(self, path: str, qs: dict) -> None:
        from ohm.temporal.reconciliation import explain_drift

        drift_id = qs.get("drift_id", [None])[0]
        top = int(qs.get("top", ["10"])[0])
        if not drift_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("drift_id is required")

        result = explain_drift(self.current_store.conn, drift_id, top=top)
        self._json_response(200, result)

    # ── Forecast registry (OHM-941 / Stage 4) ──────────────────────────────

    def _post_forecast_create(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.forecast import create_forecast

        label = body.get("label")
        target_node_id = body.get("target_node_id")
        horizon = body.get("horizon")
        if not label or not target_node_id or not horizon:
            raise ValidationError("label, target_node_id, and horizon are required")

        result = create_forecast(
            self.current_store.conn,
            label=label,
            target_node_id=target_node_id,
            horizon=horizon,
            predicted_value=body.get("predicted_value"),
            predicted_unit=body.get("predicted_unit"),
            distribution=body.get("distribution"),
            assumptions=body.get("assumptions"),
            model_id=body.get("model_id"),
            created_by=agent,
            connects_to=body.get("connects_to"),
            metadata=body.get("metadata"),
        )
        self._json_response(201, result)

    def _get_forecasts(self, path: str, qs: dict) -> None:
        from ohm.graph.queries.forecast import list_forecasts

        target_node_id = qs.get("target_node_id", [None])[0]
        horizon = qs.get("horizon", [None])[0]
        status = qs.get("status", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", ["100"])[0])

        result = list_forecasts(
            self.current_store.read_conn,
            target_node_id=target_node_id,
            horizon=horizon,
            status=status,
            created_by=created_by,
            limit=limit,
        )
        self._json_response(200, {"forecasts": result, "count": len(result)})

    def _get_forecast_detail(self, path: str, qs: dict) -> None:
        from ohm.graph.queries.forecast import get_forecast

        forecast_id = path.rstrip("/").split("/")[-1]

        result = get_forecast(self.current_store.conn, forecast_id)
        if not result:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Forecast {forecast_id} not found")
        self._json_response(200, result)

    def _route_forecast_get_or_trajectory(self, path: str, qs: dict) -> None:
        """Route /forecast/{id} or /forecast/{id}/trajectory to the right handler."""
        stripped = path.rstrip("/")
        suffix = stripped[len("/forecast/"):] if stripped.startswith("/forecast/") else stripped
        if stripped.endswith("/trajectory"):
            self._get_forecast_trajectory(suffix, qs)
        else:
            self._get_forecast_detail(suffix, qs)

    def _post_forecast_transition(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.forecast import transition_forecast

        forecast_id = body.get("forecast_id")
        new_status = body.get("new_status")
        if not forecast_id or not new_status:
            raise ValidationError("forecast_id and new_status are required")

        result = transition_forecast(
            self.current_store.conn,
            forecast_id=forecast_id,
            new_status=new_status,
            created_by=agent,
            reason=body.get("reason"),
        )
        self._json_response(200, result)

    def _post_forecast_resolve(self, path: str, qs: dict, body: dict, agent: str) -> None:
        from ohm.exceptions import ValidationError
        from ohm.graph.queries.forecast import resolve_forecast

        forecast_id = body.get("forecast_id")
        actual_value = body.get("actual_value")
        if not forecast_id or actual_value is None:
            raise ValidationError("forecast_id and actual_value are required")

        result = resolve_forecast(
            self.current_store.conn,
            forecast_id=forecast_id,
            actual_value=float(actual_value),
            created_by=agent,
        )
        self._json_response(200, result)

    # ── Time-series helpers (OHM-943 / Stage 6) ───────────────────────────

    def _get_series_query(self, path: str, qs: dict) -> None:
        from ohm.temporal.time_series import query_series
        from ohm.exceptions import ValidationError

        series_id = qs.get("series_id", [None])[0]
        if not series_id:
            raise ValidationError("series_id is required")

        start = qs.get("start", [None])[0]
        end = qs.get("end", [None])[0]
        limit = int(qs.get("limit", ["1000"])[0])

        result = query_series(
            self.current_store.read_conn,
            series_id,
            start=start,
            end=end,
            limit=limit,
        )
        self._json_response(200, {"observations": result, "count": len(result)})

    def _get_series_baseline(self, path: str, qs: dict) -> None:
        from ohm.temporal.time_series import compute_baseline
        from ohm.exceptions import ValidationError

        series_id = qs.get("series_id", [None])[0]
        if not series_id:
            raise ValidationError("series_id is required")

        method = qs.get("method", ["rolling_30d"])[0]
        result = compute_baseline(self.current_store.read_conn, series_id, method=method)
        self._json_response(200, result)

    def _get_series_anomalies(self, path: str, qs: dict) -> None:
        from ohm.temporal.time_series import detect_series_anomalies
        from ohm.exceptions import ValidationError

        series_id = qs.get("series_id", [None])[0]
        if not series_id:
            raise ValidationError("series_id is required")

        method = qs.get("method", ["rolling_30d"])[0]
        sigma = float(qs.get("sigma", ["2.0"])[0])

        result = detect_series_anomalies(
            self.current_store.read_conn, series_id, method=method, sigma=sigma,
        )
        self._json_response(200, {"anomalies": result, "count": len(result)})

    # ── Reporting endpoints (OHM-944 / Stage 7) ────────────────────────────

    def _get_timeline(self, path: str, qs: dict) -> None:
        """GET /timeline/{ancestor_node_id} — plan + event rollup."""
        from ohm.graph.queries.plans_events import timeline_rollup

        ancestor_id = path.rstrip("/").split("/")[-1]
        horizon = qs.get("horizon", [None])[0]
        start_after = qs.get("start_after", [None])[0]
        end_before = qs.get("end_before", [None])[0]
        event_class = qs.get("event_class", [None])[0]
        include_plans = qs.get("include_plans", ["true"])[0] != "false"

        result = timeline_rollup(
            self.current_store.read_conn,
            ancestor_id,
            horizon=horizon,
            start_after=start_after,
            end_before=end_before,
            event_class=event_class,
            include_plans=include_plans,
        )
        self._json_response(200, result)

    def _get_forecast_trajectory(self, path: str, qs: dict) -> None:
        """GET /forecast/{id}/trajectory — forecast distribution + actuals over time."""
        from ohm.graph.queries.forecast import get_forecast
        from ohm.graph.queries._shared import _rows_to_dicts
        import json as _json

        parts = path.rstrip("/").split("/")
        forecast_id = parts[0] if parts else ""

        forecast = get_forecast(self.current_store.read_conn, forecast_id)
        if not forecast:
            from ohm.exceptions import NodeNotFoundError

            raise NodeNotFoundError(f"Forecast {forecast_id} not found")

        target_node_id = forecast.get("target_node_id")
        meta = forecast.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except (ValueError, TypeError):
                meta = {}

        actuals: list[dict] = []
        if target_node_id:
            actuals = _rows_to_dicts(
                self.current_store.read_conn.execute(
                    "SELECT id, value, created_at, type FROM ohm_observations "
                    "WHERE node_id = ? AND type IN ('measurement', 'experiment_result') "
                    "ORDER BY created_at ASC LIMIT 500",
                    [target_node_id],
                )
            )

        self._json_response(200, {
            "forecast": forecast,
            "distribution": meta.get("distribution"),
            "predicted_value": meta.get("predicted_value"),
            "horizon": meta.get("horizon"),
            "actuals": actuals,
        })
