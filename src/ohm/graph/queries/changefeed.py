"""Change feed, agent audit, and outcomes queries (OHM-447 Phase 5).

Extracted from queries/__init__.py as part of the large-module decomposition.
Contains the change feed, agent changes, threat cluster, outcome recording,
task closure, source reliability, and agent state queries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts


def query_change_feed(
    conn: DuckDBPyConnection,
    *,
    since: str | None = None,
    agent_name: str | None = None,
    node_type: str | None = None,
    node_id: str | None = None,
    limit: int = 100,
    enrich: bool = False,
) -> list[dict[str, Any]]:
    """Retrieve the change feed since a given timestamp.

    Args:
        conn: Database connection.
        since: ISO timestamp or 'last-check'. If None, returns recent changes.
        agent_name: Filter by agent.
        node_type: Filter by node type (e.g., 'concept', 'pattern', 'equipment').
            Matches changes to nodes of this type AND edges that touch nodes
            of this type (source or target).
        node_id: Filter to changes for a specific node (by node ID).
            Matches changes where row_id is that node OR edges touching it.
        limit: Maximum number of changes to return.
        enrich: If True, include node/edge data (label, type, content for
            nodes; from_node, to_node, edge_type for edges) in each entry.

    Returns:
        List of change feed entries ordered by time descending.
        If enrich=True, each entry includes a 'data' field with node/edge content.
    """
    from ohm.validation import validate_identifier, validate_timestamp

    conditions: list[str] = []
    params: list = []
    if since and since != "last-check":
        since = validate_timestamp(since)
        conditions.append("occurred_at >= ?::TIMESTAMP")
        params.append(since)
    if agent_name:
        agent_name = validate_identifier(agent_name, name="agent_name")
        conditions.append("agent_name = ?")
        params.append(agent_name)

    # node_id filter: match changes where row_id is the node OR
    # it's an edge that touches the node (from_node or to_node).
    if node_id:
        node_id = validate_identifier(node_id, name="node_id")
        conditions.append("(row_id = ? OR row_id IN (  SELECT e.id FROM ohm_edges e WHERE (e.from_node = ? OR e.to_node = ?) AND e.deleted_at IS NULL))")
        params.extend([node_id, node_id, node_id])

    # node_type filter: match changes where the row_id is a node of that type,
    # or the row_id is an edge that touches a node of that type.
    if node_type:
        node_type = validate_identifier(node_type, name="node_type")
        conditions.append(
            """(
                row_id IN (SELECT id FROM ohm_nodes WHERE type = ? AND deleted_at IS NULL)
                OR row_id IN (
                    SELECT e.id FROM ohm_edges e
                    WHERE e.from_node IN (SELECT id FROM ohm_nodes WHERE type = ? AND deleted_at IS NULL)
                       OR e.to_node IN (SELECT id FROM ohm_nodes WHERE type = ? AND deleted_at IS NULL)
                )
            )"""
        )
        params.extend([node_type, node_type, node_type])

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT
            id, table_name, row_id, operation, agent_name,
            old_data, new_data, occurred_at
        FROM ohm_change_feed
        {where_clause}
        ORDER BY occurred_at DESC
        LIMIT ?
    """
    params.append(limit)

    try:
        result = conn.execute(query, params)
        entries = _rows_to_dicts(result)
    except Exception:
        # ohm_change_feed missing on DBs that pre-date the 0.18.0 migration or
        # where CREATE SEQUENCE/TABLE silently failed — fall through to the
        # ohm_change_log fallback below.
        entries = []

    # Fallback to ohm_change_log when feed is empty (e.g., database migrated from older version)
    if not entries:
        log_conditions = []
        log_params: list = []
        if since and since != "last-check":
            log_conditions.append("changed_at >= ?::TIMESTAMP")
            log_params.append(since)
        if agent_name:
            log_conditions.append("agent_name = ?")
            log_params.append(agent_name)
        if node_id:
            log_conditions.append("(row_id = ? OR row_id IN (  SELECT e.id FROM ohm_edges e WHERE (e.from_node = ? OR e.to_node = ?) AND e.deleted_at IS NULL))")
            log_params.extend([node_id, node_id, node_id])
        log_where = ("WHERE " + " AND ".join(log_conditions)) if log_conditions else ""
        log_params.append(limit)
        try:
            log_result = conn.execute(
                f"""SELECT
                        NULL AS id, table_name, row_id, operation, agent_name,
                        NULL AS old_data, NULL AS new_data, changed_at AS occurred_at
                    FROM ohm_change_log
                    {log_where}
                    ORDER BY changed_at DESC
                    LIMIT ?""",
                log_params,
            )
            entries = _rows_to_dicts(log_result)
        except Exception:
            pass

    # Optional enrichment: fetch node/edge data for each entry
    if enrich and entries:
        for entry in entries:
            table = entry.get("table_name")
            row_id = entry.get("row_id")
            if table == "ohm_nodes" and row_id:
                node = conn.execute(
                    "SELECT label, type, content, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [row_id],
                ).fetchone()
                if node:
                    entry["data"] = {
                        "label": node[0],
                        "type": node[1],
                        "content": node[2],
                        "created_by": node[3],
                    }
            elif table == "ohm_edges" and row_id:
                edge = conn.execute(
                    "SELECT from_node, to_node, edge_type, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
                    [row_id],
                ).fetchone()
                if edge:
                    entry["data"] = {
                        "from_node": edge[0],
                        "to_node": edge[1],
                        "edge_type": edge[2],
                        "layer": edge[3],
                    }

    return entries


# ── Agent Changes (OHM-b7l7) ────────────────────────────────────────────────


def query_agent_changes(
    conn: DuckDBPyConnection,
    *,
    agent_name: str | None = None,
    since: str | None = None,
    node_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Build a personalised "what changed" delta for an agent (OHM-b7l7).

    Consolidates the data that an agent would otherwise have to assemble
    by polling /listen, /contradictions, /anomalies, /stale, /suggest, and
    /tasks separately.

    Args:
        conn: Database connection (read side is fine).
        agent_name: Optional agent to scope the agent-specific sections to.
            When the value is ``None`` the function still returns the
            core node/edge feed (identical to the legacy ``/changes``
            behaviour); the agent-scoped sections are simply omitted.
        since: ISO 8601 timestampthst anchors the delta. If ``None`` the
            caller is responsible for resolving a default (typically
            ``ohm_agent_state.last_sync`` for the same agent, then 24h
            ago — the HTTP handler does this; the SDK mirrors the
            ``listen()`` convention).
        node_type: Optional filter on node ``type`` (e.g., ``'concept'``).
            Applied to both the core node feed and the agent-scoped node
            observations.
        limit: Maximum number of rows returned per section (separate
            per-section cap keeps the payload bounded).

    Returns:
        Dict with the legacy fields ``since``, ``agent``, ``query_timestamp``,
        ``node_total``, ``edge_total``, ``nodes``, ``edges`` (always
        present) and, when ``agent_name`` is provided, the five
        agent-scoped sections:

          * ``new_observations_on_my_nodes`` — observations added to
            nodes authored by this agent since ``since``.
          * ``edges_touching_my_nodes`` — edges (by any agent) added
            since ``since`` whose ``from_node`` or ``to_node`` belongs
            to this agent.
          * ``challenges_to_my_edges`` — CHALLENGED_BY edges added since
            ``since`` that target one of the agent's own edges.
          * ``tasks_assigned_or_status_changed`` — task nodes assigned
            to this agent OR whose status changed since ``since``.
          * ``stale_nodes_needing_refresh`` — this agent's edges whose
            effective confidence has decayed below the stale threshold.

        Each agent-scoped section is capped at ``limit`` rows.
    """
    from ohm.validation import validate_identifier, validate_timestamp

    agent_clean: str | None = None
    if agent_name:
        agent_clean = validate_identifier(agent_name, name="agent_name")

    since_clean = since
    if since_clean:
        since_clean = validate_timestamp(since_clean)

    now = None
    try:
        now = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
    except Exception:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()

    response: dict[str, Any] = {
        "since": since_clean,
        "agent": agent_clean,
        "query_timestamp": now,
        "node_total": 0,
        "edge_total": 0,
        "nodes": [],
        "edges": [],
    }

    # ── Core feed (legacy /changes shape) ──
    node_conditions = ["deleted_at IS NULL", "type != 'fragment'"]
    node_params: list[Any] = []
    if since_clean:
        node_conditions.append("created_at > ?::TIMESTAMP")
        node_params.append(since_clean)
    if node_type:
        node_conditions.append("type = ?")
        node_params.append(node_type)
    if agent_clean:
        node_conditions.append("created_by = ?")
        node_params.append(agent_clean)
    node_query_params = list(node_params)
    node_query_params.append(limit)
    nodes_rows = conn.execute(
        f"SELECT id, label, type, created_by, confidence, created_at FROM ohm_nodes WHERE {' AND '.join(node_conditions)} ORDER BY created_at DESC LIMIT ?",
        node_query_params,
    ).fetchall()
    response["nodes"] = [
        {
            "id": r[0],
            "label": r[1],
            "type": r[2],
            "created_by": r[3],
            "confidence": r[4],
            "created_at": str(r[5]) if r[5] is not None else None,
        }
        for r in nodes_rows
    ]

    edge_conditions = ["deleted_at IS NULL"]
    edge_params: list[Any] = []
    if since_clean:
        edge_conditions.append("created_at > ?::TIMESTAMP")
        edge_params.append(since_clean)
    if agent_clean:
        edge_conditions.append("created_by = ?")
        edge_params.append(agent_clean)
    edge_query_params = list(edge_params)
    edge_query_params.append(limit)
    edges_rows = conn.execute(
        f"SELECT id, from_node, to_node, edge_type, layer, confidence, created_by, created_at FROM ohm_edges WHERE {' AND '.join(edge_conditions)} ORDER BY created_at DESC LIMIT ?",
        edge_query_params,
    ).fetchall()
    response["edges"] = [
        {
            "id": r[0],
            "from": r[1],
            "to": r[2],
            "type": r[3],
            "layer": r[4],
            "confidence": r[5],
            "created_by": r[6],
            "created_at": str(r[7]) if r[7] is not None else None,
        }
        for r in edges_rows
    ]

    # Totals are unbounded-by-limit
    count_node_conditions = ["deleted_at IS NULL", "type != 'fragment'"]
    count_node_params: list[Any] = []
    if since_clean:
        count_node_conditions.append("created_at > ?::TIMESTAMP")
        count_node_params.append(since_clean)
    if agent_clean:
        count_node_conditions.append("created_by = ?")
        count_node_params.append(agent_clean)
    node_total_row = conn.execute(
        f"SELECT COUNT(*) FROM ohm_nodes WHERE {' AND '.join(count_node_conditions)}",
        count_node_params,
    ).fetchone()
    response["node_total"] = int(node_total_row[0]) if node_total_row else 0

    count_edge_conditions = ["deleted_at IS NULL"]
    count_edge_params: list[Any] = []
    if since_clean:
        count_edge_conditions.append("created_at > ?::TIMESTAMP")
        count_edge_params.append(since_clean)
    if agent_clean:
        count_edge_conditions.append("created_by = ?")
        count_edge_params.append(agent_clean)
    edge_total_row = conn.execute(
        f"SELECT COUNT(*) FROM ohm_edges WHERE {' AND '.join(count_edge_conditions)}",
        count_edge_params,
    ).fetchone()
    response["edge_total"] = int(edge_total_row[0]) if edge_total_row else 0

    # ── Agent-scoped sections ──
    if agent_clean:
        response.update(_agent_changes_scoped(conn, agent_clean, since_clean, limit))

    return response


def _agent_changes_scoped(
    conn: DuckDBPyConnection,
    agent: str,
    since: str | None,
    limit: int,
) -> dict[str, Any]:
    """Compute the agent-specific sections of ``query_agent_changes``.

    Kept as a private helper so the public function stays readable and so
    the sections can be skipped entirely when no agent is supplied.
    """

    sections: dict[str, Any] = {
        "new_observations_on_my_nodes": [],
        "edges_touching_my_nodes": [],
        "challenges_to_my_edges": [],
        "tasks_assigned_or_status_changed": [],
        "stale_nodes_needing_refresh": [],
    }

    # 1) New observations on this agent's nodes since `since` (or all if None).
    since_clause_obs = ""
    obs_params: list[Any] = [agent]
    if since:
        since_clause_obs = "AND o.created_at > ?::TIMESTAMP"
        obs_params.append(since)
    obs_params.append(limit)
    obs_rows = conn.execute(
        f"""
        SELECT
            o.id, o.node_id, n.label AS node_label, o.type AS obs_type,
            o.value, o.baseline, o.sigma, o.source, o.created_by,
            o.created_at
        FROM ohm_observations o
        JOIN ohm_nodes n ON n.id = o.node_id AND n.deleted_at IS NULL
        WHERE n.created_by = ?
          {since_clause_obs}
        ORDER BY o.created_at DESC
        LIMIT ?
        """,
        obs_params,
    ).fetchall()
    sections["new_observations_on_my_nodes"] = [
        {
            "obs_id": r[0],
            "node_id": r[1],
            "node_label": r[2],
            "obs_type": r[3],
            "value": r[4],
            "baseline": r[5],
            "sigma": r[6],
            "source": r[7],
            "created_by": r[8],
            "created_at": str(r[9]) if r[9] is not None else None,
        }
        for r in obs_rows
    ]

    # 2) Edges (any author) touching this agent's nodes since `since`.
    since_clause_touch = ""
    touch_params: list[Any] = [agent, agent]
    if since:
        since_clause_touch = "AND e.created_at > ?::TIMESTAMP"
        touch_params.append(since)
    touch_params.append(limit)
    touch_rows = conn.execute(
        f"""
        SELECT
            e.id, e.from_node, n1.label AS from_label,
            e.to_node, n2.label AS to_label,
            e.edge_type, e.layer, e.confidence, e.created_by, e.created_at
        FROM ohm_edges e
        JOIN ohm_nodes n1 ON n1.id = e.from_node
        JOIN ohm_nodes n2 ON n2.id = e.to_node
        WHERE e.deleted_at IS NULL
          AND (n1.created_by = ? OR n2.created_by = ?)
          {since_clause_touch}
        ORDER BY e.created_at DESC
        LIMIT ?
        """,
        touch_params,
    ).fetchall()
    sections["edges_touching_my_nodes"] = [
        {
            "id": r[0],
            "from_node": r[1],
            "from_label": r[2],
            "to_node": r[3],
            "to_label": r[4],
            "edge_type": r[5],
            "layer": r[6],
            "confidence": r[7],
            "created_by": r[8],
            "created_at": str(r[9]) if r[9] is not None else None,
        }
        for r in touch_rows
    ]

    # 3) CHALLENGED_BY edges added since `since` that target this agent's edges.
    since_clause_chal = ""
    chal_params: list[Any] = [agent]
    if since:
        since_clause_chal = "AND c.created_at > ?::TIMESTAMP"
        chal_params.append(since)
    chal_params.append(limit)
    chal_rows = conn.execute(
        f"""
        SELECT
            c.id AS challenge_id,
            c.created_by AS challenger,
            c.challenge_of AS target_edge_id,
            target.edge_type AS target_edge_type,
            c.confidence AS challenge_confidence,
            target.confidence AS target_confidence,
            COALESCE(c.provenance, c.condition) AS challenge_reason,
            c.created_at
        FROM ohm_edges c
        JOIN ohm_edges target ON target.id = c.challenge_of
        WHERE c.challenge_type = 'CHALLENGED_BY'
          AND target.created_by = ?
          AND target.deleted_at IS NULL
          {since_clause_chal}
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        chal_params,
    ).fetchall()
    sections["challenges_to_my_edges"] = [
        {
            "challenge_id": r[0],
            "challenger": r[1],
            "target_edge_id": r[2],
            "target_edge_type": r[3],
            "challenge_confidence": r[4],
            "target_confidence": r[5],
            "challenge_reason": r[6],
            "created_at": str(r[7]) if r[7] is not None else None,
        }
        for r in chal_rows
    ]

    # 4) Tasks assigned to this agent OR whose status changed since `since`.
    task_conditions = ["type = 'task'", "deleted_at IS NULL"]
    task_params: list[Any] = []
    if since:
        task_conditions.append("(updated_at > ?::TIMESTAMP OR assigned_to = ? OR task_status IN ('in_progress','blocked','review','done','cancelled'))")
        task_params.extend([since, agent])
    else:
        task_conditions.append("assigned_to = ?")
        task_params.append(agent)
    task_params.append(limit)
    task_rows = conn.execute(
        f"""
        SELECT id, label, task_status, assigned_to, created_by, created_at, updated_at
        FROM ohm_nodes
        WHERE {" AND ".join(task_conditions)}
        ORDER BY COALESCE(updated_at, created_at) DESC
        LIMIT ?
        """,
        task_params,
    ).fetchall()
    sections["tasks_assigned_or_status_changed"] = [
        {
            "id": r[0],
            "label": r[1],
            "status": r[2],
            "assigned_to": r[3],
            "created_by": r[4],
            "created_at": str(r[5]) if r[5] is not None else None,
            "updated_at": str(r[6]) if r[6] is not None else None,
        }
        for r in task_rows
    ]

    # 5) Stale edges authored by this agent (effective confidence decayed
    #    below the threshold). Delegate to the existing substrate method
    #    and filter — re-implementing the decay math here would drift.
    try:
        from ohm.methods import detect_anomalies  # noqa: F401  (kept for parity/symmetry)
    except Exception:
        pass
    since_clause_stale = ""
    stale_params: list[Any] = [agent]
    if since:
        since_clause_stale = "AND e.created_at > ?::TIMESTAMP"
        stale_params.append(since)
    stale_params.append(limit)
    try:
        stale_rows = conn.execute(
            f"""
            SELECT
                e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                e.confidence, e.created_by, e.created_at,
                e.half_life, e.challenge_of
            FROM ohm_edges e
            WHERE e.deleted_at IS NULL
              AND e.created_by = ?
              AND e.confidence IS NOT NULL
              {since_clause_stale}
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            stale_params,
        ).fetchall()
        sections["stale_nodes_needing_refresh"] = [
            {
                "id": r[0],
                "from_node": r[1],
                "to_node": r[2],
                "edge_type": r[3],
                "layer": r[4],
                "confidence": r[5],
                "created_by": r[6],
                "created_at": str(r[7]) if r[7] is not None else None,
                "half_life": r[8],
                "challenge_of": r[9],
            }
            for r in stale_rows
        ]
    except Exception:
        # Half-life column may be absent on older schemas — leave section empty.
        sections["stale_nodes_needing_refresh"] = []

    return sections


# ── Threat Cluster ──────────────────────────────────────────────────────────


def query_threat_cluster(
    conn: DuckDBPyConnection,
    ioc_node_id: str,
    *,
    edge_type: str | None = None,
) -> list[dict[str, Any]]:
    """Find all alerts sharing a given IOC (Indicator of Compromise).

    Traverses THREAT_CLUSTER edges from the IOC node to find all related
    alerts — used in cybersecurity incident response to correlate IOCs
    across multiple alerts.
    """
    from ohm.validation import validate_identifier

    ioc_node_id = validate_identifier(ioc_node_id, name="ioc_node_id")

    edge_filter = ""
    params: list = [ioc_node_id, ioc_node_id, ioc_node_id, ioc_node_id]
    if edge_type:
        edge_type = validate_identifier(edge_type, name="edge_type")
        edge_filter = "AND e.edge_type = ?"
        params.append(edge_type)

    # Find all nodes connected to IOC via THREAT_CLUSTER edges
    query = f"""
        SELECT DISTINCT ON (n.id)
            n.id AS node_id,
            n.label,
            n.type AS node_type,
            e.id AS edge_id,
            e.edge_type,
            e.confidence,
            e.created_by,
            e.created_at
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = (
            CASE WHEN e.from_node = ? THEN e.to_node ELSE e.from_node END
        )
        WHERE (e.from_node = ? OR e.to_node = ?)
          AND n.id != ?
          AND e.deleted_at IS NULL
          {edge_filter}
        ORDER BY n.id, e.confidence DESC
    """
    result = conn.execute(query, params)
    return _rows_to_dicts(result)


# ── Source Reliability ──────────────────────────────────────────────────────


def restore_outcomes_from_change_feed(
    conn: DuckDBPyConnection,
) -> dict[str, Any]:
    """Restore missing ohm_outcomes rows from ohm_change_feed (OHM-knxf).

    If ohm_outcomes was emptied by a recovery/sync event, this function
    identifies the lost outcome IDs from the change feed. The change feed
    logs INSERT operations with row_id, so we can identify which outcomes
    existed but the full row data is not stored in the feed. This function
    checks if any of those IDs still exist in ohm_outcomes and reports the
    gap.

    Returns:
        Dict with total_feed_records, existing_count, and missing_ids list.
    """
    feed_rows = conn.execute(
        """SELECT row_id FROM ohm_change_feed
           WHERE table_name = 'ohm_outcomes' AND operation = 'INSERT'
           ORDER BY occurred_at ASC"""
    ).fetchall()

    if not feed_rows:
        return {"total_feed_records": 0, "existing_count": 0, "missing_ids": []}

    feed_ids = [r[0] for r in feed_rows]
    existing_ids = set()
    for batch_start in range(0, len(feed_ids), 500):
        batch = feed_ids[batch_start : batch_start + 500]
        placeholders = ",".join(["?"] * len(batch))
        rows = conn.execute(f"SELECT id FROM ohm_outcomes WHERE id IN ({placeholders})", batch).fetchall()
        existing_ids.update(r[0] for r in rows)

    missing_ids = [fid for fid in feed_ids if fid not in existing_ids]

    return {
        "total_feed_records": len(feed_ids),
        "existing_count": len(existing_ids),
        "missing_ids": missing_ids,
    }


def query_record_outcome(
    conn: DuckDBPyConnection,
    *,
    source_agent: str,
    claim_node: str,
    outcome: bool,
    recorded_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record whether a source agent's claim was correct.

    Stores an outcome record in ohm_outcomes for later reliability
    computation. Used in cybersecurity incident response to calibrate
    source trustworthiness over time.

    When the claim_node is an experiment node, this also:
    1. Creates an experiment_result observation on the experiment node.
    2. Updates the hypothesis_status of linked hypotheses via TESTS edges:
       - outcome=True + SUPPORTS_EVIDENCE dominant → hypothesis verified
       - outcome=False + CONTRADICTS_EVIDENCE dominant → hypothesis pruned

    Args:
        conn: Database connection.
        source_agent: The agent whose claim is being evaluated.
        claim_node: The node representing the claim.
        outcome: True if the source was correct, False otherwise.
        recorded_by: Agent recording the outcome.
        notes: Optional context about the outcome.

    Returns:
        The created outcome record, with extra keys if hypothesis
        status was updated.
    """
    import uuid

    from ohm.validation import validate_identifier

    source_agent = validate_identifier(source_agent, name="source_agent")
    claim_node = validate_identifier(claim_node, name="claim_node")
    recorded_by = validate_identifier(recorded_by, name="recorded_by")

    # Verify the claim_node exists
    node_exists = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [claim_node],
    ).fetchone()
    if not node_exists:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"claim_node not found: {claim_node}")

    outcome_id = str(uuid.uuid4())

    # OHM-yiui: Auto-derive claimed_by from the originating edge's
    # created_by. The claim_node is the from_node of the edge that made
    # the claim; we look up the oldest L3 edge with that from_node and
    # credit its created_by. Falls back to source_agent if no edge is
    # found (preserving backward compatibility).
    claimed_by_row = conn.execute(
        """SELECT e.created_by FROM ohm_edges e
           WHERE e.from_node = ? AND e.deleted_at IS NULL
           ORDER BY e.created_at ASC LIMIT 1""",
        [claim_node],
    ).fetchone()
    claimed_by = claimed_by_row[0] if claimed_by_row else source_agent

    # OHM-avkj: Auto-derive domain from the claim node's provenance.
    # This enables domain-aware source reliability — an agent reliable
    # about cattle health may be unreliable about stock prices.
    # Falls back to '*' (unscoped) when the node has no provenance.
    domain_row = conn.execute(
        "SELECT provenance FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [claim_node],
    ).fetchone()
    domain = domain_row[0] if domain_row and domain_row[0] else "*"

    conn.execute(
        """INSERT INTO ohm_outcomes
           (id, source_agent, claim_node, outcome, recorded_by, notes, claimed_by, verified_by, domain)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [outcome_id, source_agent, claim_node, outcome, recorded_by, notes, claimed_by, recorded_by, domain],
    )
    _log_change(conn, "ohm_outcomes", outcome_id, "INSERT", recorded_by)

    result = _rows_to_dicts(conn.execute("SELECT * FROM ohm_outcomes WHERE id = ?", [outcome_id]))[0]

    # ── Hypothesis-tree integration (OHM-nlbm) ──
    # When an outcome is recorded on an experiment node:
    # 1. Create experiment_result observation on the experiment
    # 2. Update linked hypothesis statuses
    node_type_row = conn.execute(
        "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [claim_node],
    ).fetchone()

    hypothesis_updates = []

    if node_type_row and node_type_row[0] == "experiment":
        # 1. Create experiment_result observation on the experiment node
        obs_id = str(uuid.uuid4())
        obs_value = 1.0 if outcome else 0.0
        try:
            conn.execute(
                """INSERT INTO ohm_observations
                   (id, node_id, type, value, source, created_by, scale, created_at)
                   VALUES (?, ?, 'experiment_result', ?, ?, ?, 'probability', CURRENT_TIMESTAMP)""",
                [obs_id, claim_node, obs_value, source_agent, recorded_by],
            )
            _log_change(conn, "ohm_observations", obs_id, "INSERT", recorded_by)
            result["experiment_result_observation"] = obs_id
        except Exception:
            # Non-fatal: if observation creation fails, continue
            result["experiment_result_observation"] = None

        # 2. Find hypotheses linked via TESTS edges and update their status
        linked_hypotheses = conn.execute(
            """
            SELECT n.id, n.hypothesis_status
            FROM ohm_edges e
            JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
            WHERE e.from_node = ?
              AND e.edge_type = 'TESTS'
              AND e.deleted_at IS NULL
            """,
            [claim_node],
        ).fetchall()

        for hyp_id, current_status in linked_hypotheses:
            new_status = None
            if outcome:
                # Positive outcome counts as supporting evidence.
                support_count = (
                    1
                    + conn.execute(
                        "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'SUPPORTS_EVIDENCE' AND deleted_at IS NULL",
                        [hyp_id],
                    ).fetchone()[0]
                )
                contradict_count = conn.execute(
                    "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONTRADICTS_EVIDENCE' AND deleted_at IS NULL",
                    [hyp_id],
                ).fetchone()[0]
                if support_count > contradict_count:
                    new_status = "verified"
                else:
                    new_status = "tested"
            else:
                # Negative outcome counts as contradicting evidence.
                support_count = conn.execute(
                    "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'SUPPORTS_EVIDENCE' AND deleted_at IS NULL",
                    [hyp_id],
                ).fetchone()[0]
                contradict_count = (
                    1
                    + conn.execute(
                        "SELECT COUNT(*) FROM ohm_edges WHERE to_node = ? AND edge_type = 'CONTRADICTS_EVIDENCE' AND deleted_at IS NULL",
                        [hyp_id],
                    ).fetchone()[0]
                )
                if contradict_count > support_count:
                    new_status = "pruned"
                else:
                    new_status = "tested"

            if new_status and new_status != current_status:
                conn.execute(
                    "UPDATE ohm_nodes SET hypothesis_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [new_status, hyp_id],
                )
                _log_change(conn, "ohm_nodes", hyp_id, "UPDATE", recorded_by)
                hypothesis_updates.append({"hypothesis_id": hyp_id, "old_status": current_status, "new_status": new_status})
                # Re-evaluate any decision nodes linked to this hypothesis
                try:
                    from ohm.decision import recompute_linked_decisions

                    decision_updates = recompute_linked_decisions(conn, hyp_id)
                    if decision_updates:
                        hypothesis_updates[-1]["decision_updates"] = decision_updates
                except Exception:
                    pass

    if hypothesis_updates:
        result["hypothesis_status_updates"] = hypothesis_updates

    return result


def query_close_task_with_outcome(
    conn: DuckDBPyConnection,
    *,
    task_id: str,
    outcome: str,
    recorded_by: str,
    notes: str | None = None,
    claim_node: str | None = None,
) -> dict[str, Any]:
    """Close a task and record its outcome against the linked claim (OHM-f5iq).

    This closes the feedback loop between execution and beliefs:
    1. Sets ``task_status = 'done'`` and stores ``outcome`` / ``outcome_notes``
       on the task node.
    2. If the task has an ``expected_claim`` (or *claim_node* is supplied),
       records an entry in ``ohm_outcomes`` via :func:`query_record_outcome`
       with ``source_agent`` set to the task's ``created_by`` — the agent
       whose prediction is being evaluated.
    3. Returns the updated task node, the outcome row, and any hypothesis
       cascades triggered downstream.

    Args:
        conn: Database connection.
        task_id: The task node id to close.
        outcome: One of ``TRUE`` / ``FALSE`` / ``AMBIGUOUS`` (see
            :data:`ohm.graph.schema.VALID_TASK_OUTCOMES`).
        recorded_by: Agent recording the outcome.
        notes: Optional justification for the outcome.
        claim_node: Optional explicit claim node id. Defaults to the
            task's ``expected_claim`` column. When neither is set and
            *outcome* is ``AMBIGUOUS``, no outcome row is written.

    Returns:
        Dict with ``task`` (updated node), ``outcome_record`` (or None),
        and ``outcome`` (the canonical uppercase value).

    Raises:
        NodeNotFoundError: If the task id does not exist or is not a task.
        ValidationError: If *outcome* is not a valid value.
    """
    from ohm.exceptions import NodeNotFoundError, ValidationError
    from ohm.framework.validation import validate_task_outcome
    from ohm.validation import validate_identifier

    task_id = validate_identifier(task_id, name="task_id")
    recorded_by = validate_identifier(recorded_by, name="recorded_by")
    try:
        outcome = validate_task_outcome(outcome) or ""
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if outcome not in ("TRUE", "FALSE", "AMBIGUOUS"):
        raise ValidationError(f"outcome must be TRUE, FALSE, or AMBIGUOUS — got {outcome!r}")

    # Fetch the task row. Must exist and be a task.
    row = conn.execute(
        "SELECT id, type, created_by, expected_claim, task_status FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [task_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"task not found: {task_id}")
    if row[1] != "task":
        raise ValidationError(f"Node {task_id} is type={row[1]!r}, not 'task'")

    source_agent = row[2] or recorded_by
    expected_claim = claim_node or row[3]

    # 1. Update the task node: status done, record outcome + notes.
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'done', outcome = ?, outcome_notes = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [outcome, notes, recorded_by, task_id],
    )
    _log_change(conn, "ohm_nodes", task_id, "UPDATE", recorded_by)

    task = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [task_id]))[0]

    # 2. Record the outcome against the claim, if we have one.
    #    AMBIGUOUS with no claim is a valid state (task closed without a
    #    testable prediction) — skip the outcome row in that case.
    outcome_record: dict | None = None
    if expected_claim and outcome in ("TRUE", "FALSE"):
        outcome_record = query_record_outcome(
            conn,
            source_agent=source_agent,
            claim_node=validate_identifier(expected_claim, name="claim_node"),
            outcome=(outcome == "TRUE"),
            recorded_by=recorded_by,
            notes=notes or f"Task {task_id} closed with outcome={outcome}",
        )

    return {
        "task": task,
        "outcome": outcome,
        "outcome_record": outcome_record,
    }


def query_source_reliability(
    conn: DuckDBPyConnection,
    source_agent: str,
    *,
    domain: str | None = None,
) -> dict[str, Any]:
    """Compute reliability metrics for a source agent.

    Returns P(accurate) and false_positive_rate computed from historical
    outcomes. If fewer than 5 outcomes recorded, returns a warning that
    the estimate is low-confidence.

    Args:
        conn: Database connection.
        source_agent: The agent to evaluate.
        domain: Optional domain filter (OHM-avkj). When set, only
            outcomes with matching ``domain`` (or ``'*'`` for unscoped)
            are counted. When None, all domains are counted (backward
            compat).

    Returns:
        Dict with source_agent, total_outcomes, accurate_count,
        false_positive_count, p_accurate, false_positive_rate,
        and low_confidence_warning (bool).
    """
    from ohm.validation import validate_identifier

    source_agent = validate_identifier(source_agent, name="source_agent")

    domain_clause = ""
    params: list = [source_agent]
    if domain is not None:
        # Match the exact domain OR unscoped ('*') outcomes — unscoped
        # outcomes apply to all domains.
        domain_clause = " AND (domain = ? OR domain = '*')"
        params.append(domain)

    result = conn.execute(
        f"""SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN outcome THEN 1 ELSE 0 END) AS accurate,
            SUM(CASE WHEN NOT outcome THEN 1 ELSE 0 END) AS false_positives
        FROM ohm_outcomes
        WHERE COALESCE(claimed_by, source_agent) = ?{domain_clause}""",
        params,
    ).fetchone()

    if result:
        total = result[0]
        accurate = result[1] or 0
        false_positives = result[2] or 0
    else:
        total = accurate = false_positives = 0

    p_accurate = round(accurate / total, 4) if total > 0 else None
    fpr = round(false_positives / total, 4) if total > 0 else None

    # OHM-8fdb: Add authority decay (effective reliability)
    from ohm.graph.calibration import effective_reliability

    reliability_data = effective_reliability(conn, source_agent)

    return {
        "source_agent": source_agent,
        "total_outcomes": total,
        "accurate_count": accurate,
        "false_positive_count": false_positives,
        "p_accurate": p_accurate,
        "false_positive_rate": fpr,
        "low_confidence_warning": total < 5,
        # OHM-8fdb: authority decay fields
        "effective_reliability": reliability_data["effective_reliability"],
        "days_since_verification": reliability_data["days_since_verification"],
        "community_prior": reliability_data["community_prior"],
        "decay_lambda": reliability_data["decay_lambda"],
        "last_outcome_at": reliability_data["last_outcome_at"],
    }


# ── Agent State ─────────────────────────────────────────────────────────────


def query_agent_state(
    conn: DuckDBPyConnection,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    """Query current agent focus and state.

    Args:
        conn: Database connection.
        agent_name: If given, returns state for that agent only.

    Returns:
        List of agent state records.
    """
    from ohm.validation import validate_identifier

    if agent_name:
        agent_name = validate_identifier(agent_name, name="agent_name")
        query = "SELECT * FROM ohm_agent_state WHERE agent_name = ?"
        result = conn.execute(query, [agent_name])
    else:
        query = "SELECT * FROM ohm_agent_state"
        result = conn.execute(query)
    return _rows_to_dicts(result)


# ── Stats ───────────────────────────────────────────────────────────────────
