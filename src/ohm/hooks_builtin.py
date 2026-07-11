"""Built-in hook implementations for the OHM staged ingestion pipeline.

These are registered via the ``python:`` prefix in ohm_hooks.command, e.g.:
  python:ohm.hooks_builtin.cross_link_check
  python:ohm.hooks_builtin.source_url_required

Each callable follows the hook protocol:
  def hook(payload: dict) -> tuple[int, str, str]
  returning (exit_code, stdout_json, stderr).

payload keys (pre_ingest):
  agent: str         — originating agent
  action: str        — "node" or "edge"
  body: dict         — the node/edge body being written
  __conn: connection — DuckDB connection (injected by HookRunner for python: hooks)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def cross_link_check(payload: dict[str, Any]) -> tuple[int, str, str]:
    """Validate that derived-claim nodes reference at least one existing node.

    Per OHM-tjzh / ADR-018: synthesis-like node types (pattern, idea, task,
    decision, synthesis, observation, interpretation, challenge) must not
    be created as dead-end nodes.

    Returns (0, "", "") if OK, (1, "", error_message) if rejected.
    """
    from ohm.schema import EXEMPT_CROSS_LINK_NODE_TYPES, MUST_HAVE_EDGE_NODE_TYPES

    body = payload.get("body", {})
    node_type = body.get("type", "concept")

    if node_type in EXEMPT_CROSS_LINK_NODE_TYPES:
        return 0, "", ""
    if node_type not in MUST_HAVE_EDGE_NODE_TYPES:
        return 0, "", ""

    node_id = body.get("id", "")
    conn = payload.get("__conn")
    if conn is not None:
        existing = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchone()
        if existing:
            return 0, "", ""

    # ADR-018 option 2: accept if an edge in the same batch references this
    # node and its counterpart exists or is being co-created in the batch.
    batch_edges = payload.get("batch_edges")
    if batch_edges:
        batch_node_ids = set(payload.get("batch_node_ids") or ())
        for edge in batch_edges:
            edge_from = edge.get("from", "")
            edge_to = edge.get("to", "")
            if edge_from == node_id or edge_to == node_id:
                counterpart = edge_to if edge_from == node_id else edge_from
                if counterpart in batch_node_ids:
                    return 0, "", ""
                if conn is not None:
                    counterpart_exists = conn.execute(
                        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [counterpart],
                    ).fetchone()
                    if counterpart_exists:
                        return 0, "", ""

    connects_to = body.get("connects_to")
    if not connects_to:
        return (
            1,
            "",
            f"cross_link_required: Nodes of type '{node_type}' must reference at least one existing node via 'connects_to'. See ADR-018.",
        )

    if not isinstance(connects_to, list) or not all(isinstance(c, str) for c in connects_to):
        return 1, "", "connects_to must be a list of node id strings"

    if not connects_to:
        return 1, "", f"connects_to for type '{node_type}' must list at least one existing node id"

    if conn is not None:
        placeholders = ",".join(["?"] * len(connects_to))
        rows = conn.execute(
            f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            connects_to,
        ).fetchall()
        existing_ids = {row[0] for row in rows}
        missing = [cid for cid in connects_to if cid not in existing_ids]
        if missing:
            return (
                1,
                "",
                f"cross_link_unknown_target: connects_to references unknown node id(s): {missing}",
            )

    return 0, "", ""


def source_url_required(payload: dict[str, Any]) -> tuple[int, str, str]:
    """Validate that source nodes include a source_url.

    Returns (0, "", "") if OK, (1, "", error_message) if rejected.
    """
    body = payload.get("body", {})
    node_type = body.get("type", "concept")

    if node_type != "source":
        return 0, "", ""

    source_url = body.get("source_url")
    if not source_url:
        return 1, "", "source_url_required: Source nodes must include a 'source_url' field."

    return 0, "", ""


def rate_limit(payload: dict[str, Any]) -> tuple[int, str, str]:
    """Simple per-agent rate limiter.

    Checks the ohm_hook_log for recent invocations by the same agent.
    Configurable via metadata: expects ``max_writes`` and ``window_s`` keys
    in the hook's metadata (stored in ohm_hooks.timeout_ms is repurposed
    as max_writes for this hook; window_s defaults to 60).

    Returns (0, "", "") if under limit, (1, "", error) if over limit.
    """
    conn = payload.get("__conn")
    if conn is None:
        return 0, "", ""

    agent = payload.get("agent", "unknown")
    max_writes = int(payload.get("max_writes", 10))
    window_s = int(payload.get("window_s", 60))

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_s)
    row = conn.execute(
        """SELECT COUNT(*) FROM ohm_nodes
           WHERE created_by = ?
           AND created_at >= ?""",
        [agent, cutoff],
    ).fetchone()
    count = row[0] if row else 0

    if count >= max_writes:
        return (
            1,
            "",
            f"rate_limit: agent '{agent}' exceeded {max_writes} writes in {window_s}s window ({count} found)",
        )

    return 0, "", ""


def observation_source_required(payload: dict[str, Any]) -> tuple[int, str, str]:
    """Validate that high-confidence observations include a source_url.

    OHM-wdrg Feature A: For observations with confidence >= 0.8,
    requires source_url to be populated. Returns a warning in advisory
    mode (not a rejection) and logs the warning with the observation ID.

    This hook is designed to be registered as a pre_ingest hook for
    observation writes. In strict mode, it rejects the write.

    payload keys:
        agent: str — originating agent
        action: str — 'observation'
        body: dict — the observation body being written
        __strict: bool — if True, reject; if False (default), warn only
    """
    body = payload.get("body", {})
    action = payload.get("action", "")

    # Only applies to observations
    if action != "observation":
        return 0, "", ""

    # Check if this is a high-confidence observation
    value = body.get("value")
    source_url = body.get("source_url", "")

    if value is None:
        return 0, "", ""

    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0, "", ""

    if conf < 0.8:
        return 0, "", ""

    if source_url and isinstance(source_url, str) and source_url.strip():
        return 0, "", ""

    # High-confidence observation without source_url
    strict = payload.get("__strict", False)
    node_id = body.get("node_id", "unknown")

    import logging

    logger = logging.getLogger(__name__)
    logger.warning(
        "observation_source_required: high-confidence observation (value=%.2f) on node '%s' missing source_url",
        conf,
        node_id,
    )

    if strict:
        return (
            1,
            "",
            f"observation_source_required: Observations with confidence >= 0.8 must include source_url (node: {node_id}, value: {conf})",
        )

    # Advisory mode: log warning but allow
    return 0, "", ""


TRIGGER_EVENT_CLASSES = frozenset({"FAILURE", "UNPLANNED_STOP", "COMPLETED"})


def propagate_on_event(payload: dict) -> tuple[int, str, str]:
    """Propagate observation downstream when a triggering event is created.

    Registered as a ``post_event_create`` hook. When an event with a
    triggering event_class (FAILURE, UNPLANNED_STOP, COMPLETED) is created,
    calls ``propagate_observation`` from the event's source node using the
    event's confidence as the observation weight.

    payload keys:
        event: dict — the created event record returned by create_event
        __conn: connection — DuckDB connection (injected by HookRunner)
    """
    import json
    import logging

    logger = logging.getLogger(__name__)

    event = payload.get("event", {})
    event_class = event.get("event_class", "")
    node_id = event.get("node_id")
    confidence = event.get("confidence")
    conn = payload.get("__conn")

    if conn is None:
        logger.warning("propagate_on_event: no __conn in payload")
        return 1, "", "propagate_on_event: no database connection"

    if event_class not in TRIGGER_EVENT_CLASSES:
        return 0, "", ""

    if not node_id:
        return 0, "", ""

    obs_weight = confidence if confidence is not None and 0.0 < confidence <= 1.0 else 1.0

    try:
        from ohm.graph.queries import propagate_observation

        results = propagate_observation(
            conn,
            node_id,
            observation_weight=obs_weight,
        )

        summary = {
            "propagated": True,
            "source_node_id": node_id,
            "event_class": event_class,
            "observation_weight": obs_weight,
            "nodes_updated": len(results),
            "downstream": [{"node_id": r["node_id"], "posterior_mean": r["posterior_mean"]} for r in results],
        }
        return 0, json.dumps(summary), ""
    except Exception as e:
        logger.warning("propagate_on_event: propagation failed for event %s: %s", event.get("id", "?"), e)
        return 0, json.dumps({"propagated": False, "error": str(e)}), ""
