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
    """Residual graph handler mixin (OHM-862 decomposition complete).

    After the #862 god-module decomposition, 4 methods remain here because
    they were not assigned to any of the 11 extracted clusters:

    - ``_get_challenge_ratio`` — cached challenge-ratio helper, called by
      ``EdgeHandlerMixin._post_edge`` via MRO.
    - ``_get_listen`` — GET /listen change-feed poller (route-table only).
    - ``_post_scratch`` — POST /scratch L0 fragment writer; uses
      ``_run_post_ingest_hooks`` from ``IngestHelperMixin`` and
      ``_suggestions_module``.
    - ``_post_outcome`` — POST /outcome claim verification recorder.

    IngestHelperMixin is retained as a base because ``_post_scratch`` calls
    ``_run_post_ingest_hooks``.
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
