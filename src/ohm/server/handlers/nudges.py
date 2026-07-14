"""Nudge handler mixin."""

from __future__ import annotations

from ohm.server.handlers._base import OhmHandlerBase


class NudgeHandlerMixin(OhmHandlerBase):
    """Handler mixin for nudge handler mixin."""

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
