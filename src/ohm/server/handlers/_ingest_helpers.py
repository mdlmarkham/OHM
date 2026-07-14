"""Shared ingest helpers used by Nodes, Edges, Fragments, and Tasks handler mixins."""

from __future__ import annotations

from typing import Any

from ohm.server.handlers._base import OhmHandlerBase


def _resolve_type_field(body: dict, *aliases: str, default: str | None = None) -> str | None:
    """Resolve a type-like field from an HTTP body, accepting multiple aliases.

    Per OHM-0abu (live daemon review, 2026-06-30): the HTTP body uses the
    generic key ``type`` for node/edge/observation types, which collides with
    natural-language naming — clients reasonably send ``node_type``,
    ``edge_type``, or ``obs_type`` instead. Accept all aliases for backward
    compatibility. The first non-empty value in priority order wins; the
    descriptive name (``node_type``/``edge_type``/``obs_type``) should be
    listed first when called. Empty string is treated as missing.
    """
    for key in aliases:
        value = body.get(key)
        if value is not None and value != "":
            return value
    return default


class IngestHelperMixin(OhmHandlerBase):
    """Provides _enforce_cross_link_requirement to Nodes/Edges/Fragments/Tasks mixins."""

    def _run_pre_ingest_hooks(
        self,
        agent: str,
        action: str,
        body: dict,
        batch_edges: list[dict] | None = None,
        batch_node_ids: set[str] | None = None,
    ) -> dict | None:
        """Run pre_ingest hooks. Return error dict if any hook rejects, else None.

        When ``batch_edges`` is provided, it is forwarded to hooks so they can
        implement ADR-018 option 2 (accept a node when an edge in the same batch
        references it). ``batch_node_ids`` is the set of node ids being created
        in the same batch, so hooks can verify the edge's counterpart exists or
        is being co-created.
        """
        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        payload: dict[str, Any] = {"agent": agent, "action": action, "body": body}
        if batch_edges is not None:
            payload["batch_edges"] = batch_edges
        if batch_node_ids is not None:
            payload["batch_node_ids"] = batch_node_ids
        results = runner.run_hooks("pre_ingest", payload)
        for r in results:
            if not r.success:
                return {
                    "error": "hook_rejected",
                    "hook_id": r.hook_id,
                    "exit_code": r.exit_code,
                    "message": r.stderr or "Hook rejected the operation",
                    "timed_out": r.timed_out,
                }
        return None

    def _run_post_ingest_hooks(self, agent: str, action: str, result: dict) -> dict:
        """Run post_ingest hooks. Return hook_decorations dict if any hook provides JSON stdout."""
        import json

        from ohm.hooks import HookRunner

        runner = HookRunner(self.current_store.conn)
        results = runner.run_hooks("post_ingest", {"agent": agent, "action": action, "result": result})
        decorations = {}
        for r in results:
            if r.success and r.stdout.strip():
                try:
                    merge = json.loads(r.stdout.strip())
                    if isinstance(merge, dict):
                        decorations.update(merge)
                except json.JSONDecodeError:
                    pass
            elif not r.success:
                import logging

                logging.getLogger(__name__).warning(
                    "post_ingest hook %s failed (exit_code=%d): %s",
                    r.hook_id,
                    r.exit_code,
                    r.stderr,
                )
        return decorations

    def _enforce_cross_link_requirement(self, node_id: str, body: dict) -> dict | None:
        """Return a 422 response body if *body* describes a node that must link.

        Per OHM-tjzh / ADR-018: synthesis-like node types (pattern, idea, task,
        decision, and the forward-compat synthesis/observation/interpretation/
        challenge types) cannot stand alone. They must reference an existing
        node via `connects_to` so the claim is anchored to graph structure.

        Exempt types (source, concept, entity) and updates of pre-existing
        nodes pass through. The caller should ``_json_response(422, error)``
        and ``return`` if a non-None error dict is returned.
        """
        from ohm.schema import requires_cross_link

        node_type = _resolve_type_field(body, "node_type", "type", default="concept") or "concept"
        if not requires_cross_link(node_type):
            return None

        # Updates of pre-existing nodes are exempt — you cannot fix a
        # historical dead-end by refusing to update it. The check only
        # applies to new nodes.
        existing = self.current_store.conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if existing:
            return None

        connects_to = body.get("connects_to")
        if not connects_to:
            return {
                "error": "cross_link_required",
                "message": (
                    f"Nodes of type '{node_type}' must reference at least one existing "
                    f"node via the 'connects_to' field. A bare claim cannot be reached "
                    f"from context, cannot be challenged, and cannot propagate through "
                    f"Bayesian inference. See OHM-tjzh / ADR-018."
                ),
                "node_type": node_type,
                "hint": "Add a 'connects_to' field with one or more existing node ids, or use POST /batch to atomically create the node and at least one edge.",
            }

        if not isinstance(connects_to, list) or not all(isinstance(c, str) for c in connects_to):
            return {
                "error": "validation_error",
                "message": "connects_to must be a list of node id strings",
            }
        if not connects_to:
            return {
                "error": "cross_link_required",
                "message": f"connects_to for type '{node_type}' must list at least one existing node id",
                "node_type": node_type,
            }

        # Verify every referenced id actually exists. Reject 422 (not 404) —
        # the request is well-formed but cannot be processed because the
        # cross-link target is missing.
        placeholders = ",".join(["?"] * len(connects_to))
        rows = self.current_store.conn.execute(
            f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            connects_to,
        ).fetchall()
        existing_ids = {row[0] for row in rows}
        missing = [cid for cid in connects_to if cid not in existing_ids]
        if missing:
            return {
                "error": "cross_link_unknown_target",
                "message": (f"connects_to references unknown node id(s): {missing}. Cross-link targets must already exist in the graph."),
                "missing": missing,
            }

        return None
