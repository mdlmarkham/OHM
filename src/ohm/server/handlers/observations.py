"""Observation handler mixin."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import _resolve_type_field
from ohm.server.nudges import generate_nudges, enrich_response


class ObservationHandlerMixin(OhmHandlerBase):
    """Handler mixin for observation handler mixin."""

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

