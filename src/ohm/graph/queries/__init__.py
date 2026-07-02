"""Parameterized recursive CTE views for graph traversal.

Implements the ~7 query patterns defined in ADR-001 and docs/cli.md:

    1. neighborhood  — Bounded-depth traversal from a node
    2. path          — Shortest path between two nodes
    3. impact        — Downstream failure impact analysis
    4. confidence    — Full provenance and challenge audit
    5. change_feed   — Timestamp-based change feed
    6. agent_state   — Current focus per agent
    7. stats         — Counts by layer/type/owner

All queries use standard SQL recursive CTEs (zero-dependency, works through Quack).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


# ── Neighborhood ────────────────────────────────────────────────────────────


def _rows_to_dicts(result: Any) -> list[dict[str, Any]]:
    """Convert DuckDB query result to list of dicts using column descriptions."""
    if not result:
        return []
    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _percentile(count: int, trials: int, pct: float) -> float:
    """Compute a percentile for a binomial activation count.

    For a binomial distribution with n=trials and observed count,
    returns the percentile value. Uses normal approximation for
    large trials, exact for small.
    """
    if trials == 0:
        return 0.0
    p = count / trials
    if p == 0.0 or p == 1.0:
        return p
    # Normal approximation with continuity correction
    import math

    z = {0.05: -1.645, 0.50: 0.0, 0.95: 1.645}.get(pct, 0.0)
    se = math.sqrt(p * (1 - p) / trials)
    result = p + z * se
    return max(0.0, min(1.0, result))


def _log_change(
    conn: DuckDBPyConnection,
    table_name: str,
    row_id: str,
    operation: str,
    agent_name: str,
) -> None:
    """Log a write operation to the change feed.

    This mirrors store.py._log_change() for the direct-connection
    path. Both paths must populate ohm_change_feed so that
    listen() works regardless of how agents connect.
    """
    import json

    try:
        conn.execute(
            """INSERT INTO ohm_change_feed
               (table_name, row_id, operation, agent_name, old_data)
               VALUES (?, ?, ?, ?, ?)""",
            [table_name, row_id, operation, agent_name, json.dumps({})],
        )
    except Exception:
        # ohm_change_feed may be missing on old or read-only databases;
        # change-feed logging is non-critical — skip rather than crash
        pass


def query_neighborhood(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    depth: int = 3,
    layer: str | None = None,
    direction: str = "both",
) -> list[dict[str, Any]]:
    """Bounded-depth graph traversal from *node_id*.

    Returns all edges (and their nodes) within *depth* hops.
    Uses a recursive CTE with cycle detection via visited-node tracking.
    """
    from ohm.validation import validate_depth, validate_identifier, validate_layer

    node_id = validate_identifier(node_id, name="node_id")
    depth = validate_depth(depth)
    if layer:
        layer = validate_layer(layer)

    # Build direction join condition
    if direction == "outgoing":
        join_on = "e.from_node = v.node"
    elif direction == "incoming":
        join_on = "e.to_node = v.node"
    else:
        join_on = "(e.from_node = v.node OR e.to_node = v.node)"

    params: list = [node_id, depth]
    layer_clause = ""
    if layer is not None:
        layer_clause = "AND e.layer = ?"
        params.append(layer)
        params.append(layer)  # layer_clause appears twice in the query
    else:
        # OHM-a5rz.6: exclude L0 from default neighborhood traversal.
        # L0 fragments are explicitly unreliable and should not appear in
        # default graph queries. Pass layer='L0' to include them.
        layer_clause = "AND e.layer != 'L0'"

    query = f"""
        WITH RECURSIVE visited AS (
            SELECT ? AS node, 0 AS hop
            UNION
            SELECT DISTINCT
                CASE WHEN e.from_node = v.node THEN e.to_node ELSE e.from_node END AS node,
                v.hop + 1 AS hop
            FROM visited v
            JOIN ohm_edges e ON {join_on}
            WHERE v.hop < ?
              AND e.deleted_at IS NULL
              {layer_clause}
        )
        SELECT DISTINCT ON (e.id)
            e.id AS edge_id,
            e.from_node,
            e.to_node,
            e.layer,
            e.edge_type,
            e.confidence,
            e.probability,
            e.probability_p05,
            e.probability_p50,
            e.probability_p95,
            e.provenance,
            e.created_by,
            e.created_at,
            e.challenge_of,
            e.challenge_type,
            MIN(v.hop) AS hop
        FROM visited v
        JOIN ohm_edges e ON (e.from_node = v.node OR e.to_node = v.node)
          AND e.deleted_at IS NULL
        {layer_clause}
        GROUP BY e.id, e.from_node, e.to_node, e.layer, e.edge_type,
                 e.confidence, e.probability,
                 e.probability_p05, e.probability_p50, e.probability_p95,
                 e.provenance,
                 e.created_by, e.created_at,
                 e.challenge_of, e.challenge_type
        ORDER BY hop, e.edge_type
    """

    result = conn.execute(query, params)
    edges = _rows_to_dicts(result)

    # ADR-015: Add citation_status to L3 edges (Source Citation Architecture)
    ref_from_nodes = set()
    for e in edges:
        if e.get("edge_type") == "REFERENCES" or e.get("type") == "REFERENCES":
            ref_from_nodes.add(e.get("from_node"))
    for e in edges:
        layer_val = e.get("layer")
        if layer_val == "L3":
            from_node = e.get("from_node", "")
            e["citation_status"] = "verified" if from_node in ref_from_nodes else "unverified"

    return edges


# ── Path ────────────────────────────────────────────────────────────────────


def query_path(
    conn: DuckDBPyConnection,
    from_node: str,
    to_node: str,
    *,
    max_depth: int = 10,
    layer: str | None = None,
) -> list[dict[str, Any]]:
    """Shortest path between *from_node* and *to_node* using directed BFS.

    Returns the ordered list of edges forming the path (from source to
    destination), or empty list if no path exists within *max_depth*.

    Iterative BFS: at each level, fetch only the outgoing edges from the
    current frontier (one SQL query per level) instead of the entire edge
    table. Avoids the O(N) edge load of the previous implementation while
    preserving the correct BFS visited-set semantics that DuckDB recursive
    CTEs cannot express (the CTE form explored all paths, blowing up
    exponentially in dense graphs).
    """
    from ohm.validation import validate_depth, validate_identifier, validate_layer

    from_node = validate_identifier(from_node, name="from_node")
    to_node = validate_identifier(to_node, name="to_node")
    max_depth = validate_depth(max_depth, max_depth=50)

    if from_node == to_node:
        return []

    visited: set[str] = {from_node}
    frontier: list[tuple[str, list[dict[str, Any]]]] = [(from_node, [])]

    for _ in range(max_depth):
        frontier_nodes = sorted({n for n, _ in frontier})
        if not frontier_nodes:
            return []
        placeholders = ",".join(["?"] * len(frontier_nodes))
        layer_clause = "AND layer = ?" if layer else ""
        params: list = list(frontier_nodes)
        if layer is not None:
            layer = validate_layer(layer)
            params.append(layer)
        edges = _rows_to_dicts(
            conn.execute(
                f"SELECT id, from_node, to_node, layer, edge_type, confidence FROM ohm_edges WHERE deleted_at IS NULL AND from_node IN ({placeholders}) {layer_clause}",
                params,
            )
        )

        by_from: dict[str, list[dict[str, Any]]] = {}
        for e in edges:
            by_from.setdefault(e["from_node"], []).append(e)

        next_frontier: list[tuple[str, list[dict[str, Any]]]] = []
        for current, path in frontier:
            for edge in by_from.get(current, []):
                nxt = edge["to_node"]
                if nxt == to_node:
                    final_path = path + [edge]
                    return [dict(e, depth=i + 1) for i, e in enumerate(final_path)]
                if nxt not in visited:
                    visited.add(nxt)
                    next_frontier.append((nxt, path + [edge]))

        frontier = next_frontier

    return []


# ── Impact ──────────────────────────────────────────────────────────────────


def query_impact(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    depth: int = 5,
) -> list[dict[str, Any]]:
    """Downstream failure impact analysis.

    Traverses outgoing L2 and L3 edges from *node_id* to find
    all transitively affected nodes.
    """
    from ohm.validation import validate_depth, validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    depth = validate_depth(depth)
    query = """
        WITH RECURSIVE impact_cte AS (
            SELECT
                e.id AS edge_id,
                e.from_node,
                e.to_node,
                e.layer,
                e.edge_type,
                e.confidence,
                1 AS depth
            FROM ohm_edges e
            WHERE e.from_node = ?
              AND e.deleted_at IS NULL
              AND e.layer IN ('L2', 'L3')

            UNION ALL

            SELECT
                e.id,
                e.from_node,
                e.to_node,
                e.layer,
                e.edge_type,
                e.confidence,
                i.depth + 1
            FROM impact_cte i
            JOIN ohm_edges e ON e.from_node = i.to_node
            WHERE i.depth < ?
              AND e.deleted_at IS NULL
              AND e.layer IN ('L2', 'L3')
        )
        SELECT edge_id, from_node, to_node, layer, edge_type, confidence, depth
        FROM impact_cte
        ORDER BY depth, edge_type
    """

    result = conn.execute(query, [node_id, depth])
    return _rows_to_dicts(result)


# ── Confidence Audit ────────────────────────────────────────────────────────


def query_confidence(
    conn: DuckDBPyConnection,
    edge_id: str,
) -> dict[str, Any]:
    """Full provenance and challenge audit for an edge.

    Returns the original edge details plus all CHALLENGED_BY, SUPPORTS,
    and REFINES edges referencing it.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    # SELECT * so challenge_of, challenge_type, provenance, PERT fields etc. are all present (OHM-oxdq)
    query = "SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL"
    original = conn.execute(query, [edge_id]).fetchone()
    if original is None:
        return {"original": None, "challenges": [], "supports": [], "refinements": []}

    columns = [desc[0] for desc in conn.description]
    original_dict = dict(zip(columns, original))

    # Find all challenge/support/refine edges referencing this edge
    refs_query = """
        SELECT *
        FROM ohm_edges
        WHERE challenge_of = ? AND deleted_at IS NULL
        ORDER BY created_at DESC
    """
    refs_result = conn.execute(refs_query, [edge_id])
    ref_columns = [desc[0] for desc in conn.description]
    refs = [dict(zip(ref_columns, row)) for row in refs_result.fetchall()]

    challenges = []
    supports = []
    refinements = []
    for row in refs:
        d = dict(row)
        if d["edge_type"] == "CHALLENGED_BY":
            challenges.append(d)
        elif d["edge_type"] == "SUPPORTS":
            supports.append(d)
        elif d["edge_type"] == "REFINES":
            refinements.append(d)

    return {
        "original": original_dict,
        "challenges": challenges,
        "supports": supports,
        "refinements": refinements,
    }


# ── Change Feed ─────────────────────────────────────────────────────────────


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
    from datetime import datetime, timedelta, timezone

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


def query_stats(conn: DuckDBPyConnection, include_l0: bool = False) -> dict[str, Any]:
    """Aggregate graph statistics.

    Args:
        conn: Database connection.
        include_l0: Include L0 fragment-specific metrics (OHM-a5rz.24).

    Returns:
        Dict with edge counts by layer, node counts by type,
        confidence distribution, challenge ratio, and active agents.
    """
    stats: dict[str, Any] = {}

    # Edge counts by layer
    result = conn.execute("""
        SELECT layer, COUNT(*) AS count
        FROM ohm_edges
        GROUP BY layer
        ORDER BY layer
    """)
    stats["edges_by_layer"] = {row[0]: row[1] for row in result.fetchall()}

    # Edge counts by type
    result = conn.execute("""
        SELECT edge_type, COUNT(*) AS count
        FROM ohm_edges
        GROUP BY edge_type
        ORDER BY count DESC
    """)
    stats["edges_by_type"] = {row[0]: row[1] for row in result.fetchall()}

    # Node counts by type (OHM-a5rz.6: exclude L0 fragments by default)
    if include_l0:
        result = conn.execute("""
            SELECT type, COUNT(*) AS count
            FROM ohm_nodes
            WHERE deleted_at IS NULL
            GROUP BY type
            ORDER BY count DESC
        """)
    else:
        result = conn.execute("""
            SELECT type, COUNT(*) AS count
            FROM ohm_nodes
            WHERE deleted_at IS NULL AND type != 'fragment'
            GROUP BY type
            ORDER BY count DESC
        """)
    stats["nodes_by_type"] = {row[0]: row[1] for row in result.fetchall()}

    # Total counts (OHM-a5rz.6: exclude L0 fragment nodes from totals)
    total_nodes_row = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'").fetchone()
    stats["total_nodes"] = total_nodes_row[0] if total_nodes_row else 0
    total_edges_row = conn.execute("SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL").fetchone()
    stats["total_edges"] = total_edges_row[0] if total_edges_row else 0
    total_obs_row = conn.execute("SELECT COUNT(*) FROM ohm_observations WHERE deleted_at IS NULL").fetchone()
    stats["total_observations"] = total_obs_row[0] if total_obs_row else 0

    # Challenge ratio
    l3_l4_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL AND layer IN ('L3', 'L4')
    """).fetchone()
    total_l3_l4 = l3_l4_row[0] if l3_l4_row else 0
    challenged_row = conn.execute("""
        SELECT COUNT(DISTINCT challenge_of) FROM ohm_edges
        WHERE deleted_at IS NULL AND challenge_of IS NOT NULL
    """).fetchone()
    challenged = challenged_row[0] if challenged_row else 0
    stats["challenge_ratio"] = round(challenged / total_l3_l4, 4) if total_l3_l4 > 0 else 0.0

    # Active agents — agents with writes in the last 24 hours.
    # Uses ohm_agent_state.last_sync which is updated on every write
    # via store._log_change(). Survives daemon restarts because it's
    # stored in the persistent ohm_agent_state table, not the ephemeral
    # change feed.
    agents_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_agent_state
        WHERE last_sync IS NOT NULL
          AND last_sync > CURRENT_TIMESTAMP - INTERVAL '24 hours'
    """).fetchone()
    stats["active_agents"] = agents_row[0] if agents_row else 0

    # Dead end count — nodes with incoming edges but no outgoing edges
    dead_end_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE NOT EXISTS (
            SELECT 1 FROM ohm_edges e
            WHERE e.from_node = n.id AND e.deleted_at IS NULL
        )
        AND EXISTS (
            SELECT 1 FROM ohm_edges e2
            WHERE e2.to_node = n.id AND e2.deleted_at IS NULL
        )
        AND n.deleted_at IS NULL
    """).fetchone()
    stats["dead_end_count"] = dead_end_row[0] if dead_end_row else 0

    # Observation stats — accumulate, don't collapse (ADR design note)
    # Observations stay as separate rows; consumers choose aggregation strategy.
    # These stats help consumers make informed decisions without querying every row.
    obs_stats = conn.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(DISTINCT node_id) AS nodes_with_observations,
            ROUND(AVG(value), 4) AS mean_value,
            ROUND(MIN(value), 4) AS min_value,
            ROUND(MAX(value), 4) AS max_value,
            ROUND(AVG(sigma), 4) AS mean_sigma
        FROM ohm_observations
        WHERE value IS NOT NULL
    """).fetchone()
    if obs_stats and obs_stats[0] > 0:
        stats["observation_stats"] = {
            "total": obs_stats[0],
            "nodes_with_observations": obs_stats[1],
            "mean_value": obs_stats[2],
            "min_value": obs_stats[3],
            "max_value": obs_stats[4],
            "mean_sigma": obs_stats[5],
        }

    # Top nodes by observation count (for discoverability)
    top_observed = conn.execute("""
        SELECT n.label, n.id, COUNT(*) AS obs_count
        FROM ohm_observations o
        JOIN ohm_nodes n ON n.id = o.node_id
        GROUP BY n.id, n.label
        ORDER BY obs_count DESC
        LIMIT 10
    """).fetchall()
    if top_observed:
        stats["top_observed_nodes"] = [{"label": row[0], "id": row[1], "observation_count": row[2]} for row in top_observed]

    # OHM-a5rz.24: Fragment density metrics
    if include_l0:
        fragments_total_row = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type = 'fragment'").fetchone()
        fragments_total = fragments_total_row[0] if fragments_total_row else 0

        fragments_with_links_row = conn.execute("""
            SELECT COUNT(DISTINCT n.id)
            FROM ohm_nodes n
            JOIN ohm_edges e ON e.from_node = n.id AND e.deleted_at IS NULL
            WHERE n.deleted_at IS NULL AND n.type = 'fragment'
        """).fetchone()
        fragments_with_links = fragments_with_links_row[0] if fragments_with_links_row else 0

        edge_count_row = conn.execute("""
            SELECT COUNT(*)
            FROM ohm_edges e
            JOIN ohm_nodes n ON n.id = e.from_node
            WHERE n.deleted_at IS NULL AND n.type = 'fragment' AND e.deleted_at IS NULL
        """).fetchone()
        total_fragment_edges = edge_count_row[0] if edge_count_row else 0

        fragment_density = round(total_fragment_edges / fragments_total, 4) if fragments_total > 0 else 0.0

        stats["fragment_density"] = {
            "fragments_total": fragments_total,
            "fragments_with_links": fragments_with_links,
            "total_fragment_edges": total_fragment_edges,
            "fragment_density": fragment_density,
        }

    return stats


# ── Write Operations ────────────────────────────────────────────────────────


def create_node(
    conn: DuckDBPyConnection,
    *,
    label: str,
    node_type: str = "concept",
    content: str | None = None,
    created_by: str,
    visibility: str = "team",
    provenance: str | None = None,
    confidence: float = 1.0,
    priority: str | None = None,
    url: str | None = None,
    source_url: str | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
    utility_scale: str | float | None = None,
    utility_usd_per_day: float | None = None,
    utility_currency: str | None = None,
    current_best_action: str | None = None,
    action_alternatives: list[str] | None = None,
    connects_to: list[str] | None = None,
    source_tier: str | None = None,
    source_author: str | None = None,
    source_institution: str | None = None,
    data_origin: str | None = None,
) -> dict[str, Any]:
    """Create a new node and return its full record.

    Args:
        source_url: Alias for url (ADR-015). Stored in the `url` column.
            Accepting both names for backward compatibility with agents
            sending "source_url" for source nodes.
        tags: Optional tags for categorization and discovery.
        metadata: Optional structured key-value data (JSON dict).
        connects_to: Optional list of existing node ids this node will be linked
            to. Used by the cross-link requirement (OHM-tjzh / ADR-018) to prove
            the agent has anchored a derived claim to existing graph structure.
            Each id must already exist; the function does not auto-create edges.
        source_tier: Optional quality tier for the source (ADR-028). When set,
            confidence must not exceed SOURCE_TIER_CEILINGS[tier]. None means
            tier not assessed — no ceiling applied (backward compatible).
    """
    import json
    from ohm.schema import (
        generate_node_id,
        validate_node_type,
        VALID_PRIORITY,
    )
    from ohm.validation import (
        validate_confidence,
        validate_data_origin,
        validate_identifier,
        validate_source_tier,
        enforce_confidence_ceiling,
    )

    if not label or len(label) > 500:
        raise ValueError("Label must be non-empty and ≤ 500 characters")
    if not validate_node_type(node_type):
        raise ValueError(f"Invalid node type: {node_type}")
    confidence = validate_confidence(confidence)
    source_tier = validate_source_tier(source_tier)
    data_origin = validate_data_origin(data_origin)
    enforce_confidence_ceiling(confidence, source_tier)
    if priority is not None and priority not in VALID_PRIORITY:
        raise ValueError(f"Invalid priority: {priority}. Must be one of: {sorted(VALID_PRIORITY)}")
    _utility_scale_map = {"best": 1.0, "neutral": 0.5, "worst": 0.0}
    if utility_scale is not None:
        if isinstance(utility_scale, str):
            if utility_scale not in _utility_scale_map:
                raise ValueError(f"utility_scale must be one of best/neutral/worst, got {utility_scale}")
            utility_scale = _utility_scale_map[utility_scale]
        elif not isinstance(utility_scale, (int, float)):
            raise ValueError(f"utility_scale must be one of best/neutral/worst or a number, got {utility_scale}")
        if isinstance(utility_scale, (int, float)) and not (0 <= utility_scale <= 1):
            raise ValueError(f"utility_scale must be between 0 and 1, got {utility_scale}")

    # ADR-015: source_url is an alias for url (backward compat)
    if source_url is not None and url is None:
        url = source_url

    # Validate connects_to references: each must be an existing node id.
    if connects_to is not None:
        if not isinstance(connects_to, list) or not all(isinstance(c, str) for c in connects_to):
            raise ValueError("connects_to must be a list of node id strings")
        if not connects_to:
            raise ValueError("connects_to must contain at least one node id")
        for cid in connects_to:
            validate_identifier(cid, name="connects_to entry")
        placeholders = ",".join(["?"] * len(connects_to))
        existing = conn.execute(
            f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            connects_to,
        ).fetchall()
        existing_ids = {row[0] for row in existing}
        missing = [cid for cid in connects_to if cid not in existing_ids]
        if missing:
            raise ValueError(f"connects_to references unknown node id(s): {missing}. Cross-link targets must already exist in the graph.")

    # Serialize action_alternatives to JSON if provided
    alternatives_json = json.dumps(action_alternatives) if action_alternatives is not None else None

    node_id = generate_node_id(label, node_type)

    # Check for soft-deleted row with same ID (primary key collision avoidance)
    soft_deleted = conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [node_id]).fetchone()
    # Serialize tags and metadata to JSON
    import json as _json

    tags_json = _json.dumps(tags) if tags else None
    metadata_json = _json.dumps(metadata) if metadata else None

    if soft_deleted:
        # Reactivate soft-deleted row with new data
        conn.execute(
            """UPDATE ohm_nodes SET
                label = ?, type = ?, content = ?, created_by = ?,
                visibility = ?, provenance = ?, confidence = ?, priority = ?, url = ?,
                tags = ?, metadata = ?,
                utility_scale = ?, utility_usd_per_day = ?, utility_currency = ?,
                current_best_action = ?, action_alternatives = ?,
                source_tier = ?,
                source_author = ?, source_institution = ?, data_origin = ?,
                deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
            [
                label,
                node_type,
                content,
                created_by,
                visibility,
                provenance,
                confidence,
                priority,
                url,
                tags_json,
                metadata_json,
                utility_scale,
                utility_usd_per_day,
                utility_currency,
                current_best_action,
                alternatives_json,
                source_tier,
                source_author,
                source_institution,
                data_origin,
                node_id,
            ],
        )
        _log_change(conn, "ohm_nodes", node_id, "UPDATE", created_by)
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]))[0]

    conn.execute(
        """INSERT INTO ohm_nodes
           (id, label, type, content, created_by, visibility, provenance, confidence, priority, url,
            tags, metadata, utility_scale, utility_usd_per_day, utility_currency, current_best_action, action_alternatives, source_tier,
            source_author, source_institution, data_origin)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            node_id,
            label,
            node_type,
            content,
            created_by,
            visibility,
            provenance,
            confidence,
            priority,
            url,
            tags_json,
            metadata_json,
            utility_scale,
            utility_usd_per_day,
            utility_currency,
            current_best_action,
            alternatives_json,
            source_tier,
            source_author,
            source_institution,
            data_origin,
        ],
    )
    _log_change(conn, "ohm_nodes", node_id, "INSERT", created_by)

    # OHM-z2gp: auto-register alias so future create_node() calls with the
    # same label can find this node via resolve_node_by_alias().
    try:
        from ohm.validation import normalize_alias

        norm = normalize_alias(label)
        if norm:
            existing_alias = conn.execute(
                "SELECT 1 FROM ohm_aliases WHERE alias_norm = ? AND node_id = ?",
                [norm, node_id],
            ).fetchone()
            if not existing_alias:
                import uuid as _uuid

                conn.execute(
                    "INSERT INTO ohm_aliases (id, alias_norm, node_id) VALUES (?, ?, ?)",
                    [str(_uuid.uuid4()), norm, node_id],
                )
    except Exception:
        pass

    # Return full node record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]))[0]


def find_or_create_node(
    conn: DuckDBPyConnection,
    *,
    label: str,
    node_type: str = "concept",
    content: str | None = None,
    created_by: str,
    visibility: str = "team",
    provenance: str | None = None,
    confidence: float = 1.0,
    priority: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Find an existing node by alias, label, or type, or create one if not found.

    Used for idempotent agent registration — avoids creating duplicate
    value/goal/skill/topic nodes when re-registering.

    Resolution order (OHM-z2gp):
    1. Alias resolution — normalize the label, check ohm_aliases
    2. Label + type match — case-insensitive exact match
    3. Create new node

    Returns the existing or newly created node record.
    """
    # 1. Try alias resolution first (OHM-z2gp)
    try:
        from ohm.queries import resolve_node_by_alias

        resolved = resolve_node_by_alias(conn, query=label)
        if resolved and resolved.get("type") == node_type:
            node = _rows_to_dicts(
                conn.execute(
                    "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [resolved["id"]],
                )
            )[0]
            node["created"] = False
            return node
    except Exception:
        pass

    # 2. Try to find an existing node with matching label and type (case-insensitive)
    existing = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_nodes WHERE LOWER(label) = LOWER(?) AND type = ? AND deleted_at IS NULL LIMIT 1",
            [label, node_type],
        )
    )
    if existing:
        node = existing[0]
        node["created"] = False
        return node

    # 3. Not found — create a new one
    node = create_node(
        conn,
        label=label,
        node_type=node_type,
        content=content,
        created_by=created_by,
        visibility=visibility,
        provenance=provenance,
        confidence=confidence,
        priority=priority,
        url=url,
    )
    # Add a 'created' flag to distinguish find vs create
    node["created"] = True
    return node


def merge_nodes(
    conn: DuckDBPyConnection,
    *,
    keep_id: str,
    merge_id: str,
    merged_by: str,
) -> dict[str, Any]:
    """Merge *merge_id* into *keep_id* and soft-delete *merge_id* (OHM-z2gp).

    Re-points all edges and observations from the merge target to the
    keep target, then soft-deletes the merge node. Duplicate edges
    (same from, to, type, layer) are silently skipped so the operation
    is idempotent.

    This is the queries/ path equivalent of OhmStore.merge_nodes() —
    accessible to SDK and CLI without going through the HTTP daemon.

    Args:
        conn: Database connection.
        keep_id: Node ID to keep (canonical).
        merge_id: Node ID to merge away (soft-deleted).
        merged_by: Agent performing the merge.

    Returns:
        Dict with keep, merged, edges_repointed, observations_repointed,
        and merged_by.

    Raises:
        NodeNotFoundError: If either node does not exist.
        ValueError: If keep_id equals merge_id.
    """
    from ohm.exceptions import NodeNotFoundError
    from ohm.validation import validate_identifier
    from datetime import datetime, timezone

    keep_id = validate_identifier(keep_id, name="keep_id")
    merge_id = validate_identifier(merge_id, name="merge_id")

    if keep_id == merge_id:
        raise ValueError(f"keep_id equals merge_id ({keep_id!r}) — nothing to merge")

    keep = conn.execute("SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [keep_id]).fetchone()
    if not keep:
        raise NodeNotFoundError(f"Keep node not found: {keep_id}")

    merge = conn.execute("SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [merge_id]).fetchone()
    if not merge:
        raise NodeNotFoundError(f"Merge node not found: {merge_id}")

    now = datetime.now(timezone.utc)

    # 1. Re-point edges FROM merge_id → keep_id (skip duplicates)
    conn.execute(
        """UPDATE ohm_edges SET from_node = ?, updated_at = ?, updated_by = ?
           WHERE from_node = ? AND deleted_at IS NULL
             AND (to_node, layer, edge_type) NOT IN (
               SELECT to_node, layer, edge_type FROM ohm_edges
               WHERE from_node = ? AND deleted_at IS NULL
             )""",
        [keep_id, now, merged_by, merge_id, keep_id],
    )

    # 2. Re-point edges TO merge_id → keep_id (skip duplicates)
    conn.execute(
        """UPDATE ohm_edges SET to_node = ?, updated_at = ?, updated_by = ?
           WHERE to_node = ? AND deleted_at IS NULL
             AND (from_node, layer, edge_type) NOT IN (
               SELECT from_node, layer, edge_type FROM ohm_edges
               WHERE to_node = ? AND deleted_at IS NULL
             )""",
        [keep_id, now, merged_by, merge_id, keep_id],
    )

    # 3. Re-point observations
    conn.execute(
        "UPDATE ohm_observations SET node_id = ? WHERE node_id = ? AND deleted_at IS NULL",
        [keep_id, merge_id],
    )

    # 4. Soft-delete exact-duplicate edges that remain
    conn.execute(
        """UPDATE ohm_edges SET deleted_at = ?, updated_at = ?, updated_by = ?
           WHERE id IN (
             SELECT e.id FROM ohm_edges e
             JOIN ohm_edges k ON e.from_node = k.from_node
               AND e.to_node = k.to_node
               AND e.layer = k.layer
               AND e.edge_type = k.edge_type
             WHERE e.from_node = ? AND e.deleted_at IS NULL
               AND k.from_node = ? AND k.deleted_at IS NULL
           )""",
        [now, now, merged_by, merge_id, keep_id],
    )

    # 5. Soft-delete the merge node
    conn.execute(
        "UPDATE ohm_nodes SET deleted_at = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        [now, now, merged_by, merge_id],
    )
    _log_change(conn, "ohm_nodes", merge_id, "MERGE", merged_by)

    # Count results
    edges_repointed = conn.execute(
        "SELECT COUNT(*) FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND updated_by = ? AND deleted_at IS NULL",
        [keep_id, keep_id, merged_by],
    ).fetchone()[0]
    obs_repointed = conn.execute(
        "SELECT COUNT(*) FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
        [keep_id],
    ).fetchone()[0]

    return {
        "keep": keep_id,
        "merged": merge_id,
        "edges_repointed": edges_repointed,
        "observations_repointed": obs_repointed,
        "merged_by": merged_by,
    }


def create_edge(
    conn: DuckDBPyConnection,
    *,
    from_node: str,
    to_node: str,
    layer: str,
    edge_type: str,
    created_by: str,
    confidence: float = 0.7,
    probability: float | None = None,
    urgency: str | None = None,
    condition: str | None = None,
    provenance: str | None = None,
    metadata: dict[str, Any] | None = None,
    probability_p05: float | None = None,
    probability_p50: float | None = None,
    probability_p95: float | None = None,
    confidence_p05: float | None = None,
    confidence_p50: float | None = None,
    confidence_p95: float | None = None,
    source_tier: str | None = None,
) -> dict[str, Any]:
    """Create a new edge and return its full record. Validates layer/type compatibility.

    source_tier (ADR-028): Optional quality tier for the claim. When set,
    confidence must not exceed SOURCE_TIER_CEILINGS[tier]. None means tier
    not assessed — no ceiling applied (backward compatible).
    """
    import uuid
    import json

    from ohm.schema import validate_edge_type, VALID_URGENCY
    from ohm.validation import (
        validate_confidence,
        validate_pert_triple,
        validate_source_tier,
        enforce_confidence_ceiling,
    )

    if not validate_edge_type(layer, edge_type):
        raise ValueError(f"Invalid edge type '{edge_type}' for layer '{layer}'")
    confidence = validate_confidence(confidence)
    source_tier = validate_source_tier(source_tier)
    enforce_confidence_ceiling(confidence, source_tier)
    if urgency is not None and urgency not in VALID_URGENCY:
        raise ValueError(f"Invalid urgency: {urgency}. Must be one of: {sorted(VALID_URGENCY)}")

    # Validate PERT three-point estimates (ADR-013)
    validate_pert_triple(probability_p05, probability_p50, probability_p95, name="probability PERT")
    validate_pert_triple(confidence_p05, confidence_p50, confidence_p95, name="confidence PERT")

    edge_id = str(uuid.uuid4())
    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO ohm_edges
           (id, from_node, to_node, layer, edge_type, created_by,
            confidence, probability, urgency, condition, provenance, metadata,
            probability_p05, probability_p50, probability_p95,
            confidence_p05, confidence_p50, confidence_p95, source_tier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_node, to_node, layer, edge_type, created_by, confidence, probability, urgency, condition, provenance, metadata_json, probability_p05, probability_p50, probability_p95, confidence_p05, confidence_p50, confidence_p95, source_tier],
    )
    _log_change(conn, "ohm_edges", edge_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge_id]))[0]


def suggest_edge_type(
    conn: DuckDBPyConnection,
    *,
    from_node_id: str,
    to_node_id: str,
) -> dict[str, Any]:
    """Suggest the most appropriate edge type for a from→to pair (OHM-ezt5).

    Looks up the node types and applies heuristics to recommend an edge
    type and layer. The key guardrail: pattern→case edges should use
    REFINES or EXPLAINS, NOT CAUSES (a pattern doesn't cause a case;
    it refines or explains it).

    Returns dict with:
        suggested_edge_type: The recommended edge type string
        suggested_layer: The recommended layer
        participates_in_inference: Whether this edge type flows through
            Bayesian/cascade inference
        from_type: The from_node's type
        to_type: The to_node's type
        reasoning: Human-readable explanation
        alternatives: List of other valid edge types for this pair
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    from_node_id = validate_identifier(from_node_id, name="from_node_id")
    to_node_id = validate_identifier(to_node_id, name="to_node_id")

    from_row = conn.execute(
        "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [from_node_id],
    ).fetchone()
    if not from_row:
        raise NodeNotFoundError(f"From node not found: {from_node_id}")

    to_row = conn.execute(
        "SELECT id, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [to_node_id],
    ).fetchone()
    if not to_row:
        raise NodeNotFoundError(f"To node not found: {to_node_id}")

    from_type = from_row[1]
    to_type = to_row[1]

    # Type-based heuristics
    PATTERN_TYPES = {"pattern", "idea", "synthesis", "interpretation"}
    SOURCE_TYPES = {"source", "fragment"}
    OBSERVATION_TYPES = {"observation", "metric"}
    CAUSAL_TARGET_TYPES = {"concept", "event", "decision", "task", "action", "intervention"}
    EVIDENCE_TYPES = {"experiment", "hypothesis"}

    suggested_edge_type: str
    suggested_layer: str
    reasoning: str
    alternatives: list[str]

    if from_type in PATTERN_TYPES and to_type in {"case", "decision", "task", "action"}:
        suggested_edge_type = "REFINES"
        suggested_layer = "L3"
        reasoning = f"{from_type}→{to_type} should use REFINES, not CAUSES. A {from_type} refines a {to_type}, not causes it."
        alternatives = ["EXPLAINS", "RELATED_TO"]
    elif from_type in PATTERN_TYPES and to_type in CAUSAL_TARGET_TYPES:
        suggested_edge_type = "EXPLAINS"
        suggested_layer = "L3"
        reasoning = f"{from_type}→{to_type} should use EXPLAINS, not CAUSES. A {from_type} explains a {to_type}, not causes it."
        alternatives = ["REFINES", "RELATED_TO"]
    elif from_type in SOURCE_TYPES and to_type in (CAUSAL_TARGET_TYPES | PATTERN_TYPES):
        suggested_edge_type = "REFERENCES"
        suggested_layer = "L2"
        reasoning = f"Source→{to_type} should use L2 REFERENCES, not CAUSES. A source citing a {to_type} is evidence, not an agent interpretation."
        alternatives = ["SUPPORTS_EVIDENCE"]
    elif from_type in OBSERVATION_TYPES and to_type in CAUSAL_TARGET_TYPES:
        suggested_edge_type = "SUPPORTS_EVIDENCE"
        suggested_layer = "L3"
        reasoning = f"Observation→{to_type} should use SUPPORTS_EVIDENCE, not CAUSES."
        alternatives = ["CORRELATES_WITH"]
    elif from_type in EVIDENCE_TYPES and to_type == "hypothesis":
        suggested_edge_type = "TESTS"
        suggested_layer = "L3"
        reasoning = "Experiment→hypothesis should use TESTS."
        alternatives = ["SUPPORTS_EVIDENCE", "CONTRADICTS_EVIDENCE"]
    elif from_type == "decision" and to_type in {"hypothesis", "concept"}:
        suggested_edge_type = "DECISION_DEPENDS_ON"
        suggested_layer = "L3"
        reasoning = f"Decision→{to_type} should use DECISION_DEPENDS_ON."
        alternatives = ["RELATED_TO"]
    else:
        suggested_edge_type = "RELATED_TO"
        suggested_layer = "L3"
        reasoning = f"Default for {from_type}→{to_type}: RELATED_TO. Use CAUSES only when there is a genuine causal mechanism."
        alternatives = ["CAUSES", "CORRELATES_WITH", "EXPLAINS"]

    from ohm.graph.constraints import EDGE_CONSTRAINTS

    constraints = EDGE_CONSTRAINTS.get(suggested_edge_type, {})
    participates = constraints.get("participates_in_inference", False)

    return {
        "suggested_edge_type": suggested_edge_type,
        "suggested_layer": suggested_layer,
        "participates_in_inference": participates,
        "from_type": from_type,
        "to_type": to_type,
        "reasoning": reasoning,
        "alternatives": alternatives,
    }


def create_skill(
    conn: DuckDBPyConnection,
    *,
    label: str,
    trigger: str,
    scope: str = "personal",
    required_tools: list[str] | None = None,
    boundaries: str | None = None,
    output_format: str | None = None,
    verification_evidence: list[str] | None = None,
    connects_to: list[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create a portable skill node (OHM-461f).

    Skill nodes represent reusable agent procedures with trigger,
    scope, tools, boundaries, output, and verification evidence.
    Maps to Nate Jones' Open Skills model.
    """
    metadata: dict[str, Any] = {
        "trigger": trigger,
        "scope": scope,
        "required_tools": required_tools or [],
        "boundaries": boundaries,
        "output_format": output_format,
        "verification_evidence": verification_evidence or [],
    }
    return create_node(
        conn,
        label=label,
        node_type="skill",
        content=trigger,
        created_by=created_by,
        metadata=metadata,
        connects_to=connects_to,
    )


def create_runbook(
    conn: DuckDBPyConnection,
    *,
    label: str,
    skill_ids: list[str],
    description: str | None = None,
    connects_to: list[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create a runbook node with ordered DEPENDS_ON chain of skills (OHM-461f).

    A runbook is an ordered sequence of skill nodes connected via
    DEPENDS_ON edges. The order of skill_ids determines the chain.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    if not skill_ids:
        raise ValidationError("skill_ids is required (at least one skill)")

    metadata: dict[str, Any] = {
        "skill_ids": skill_ids,
        "skill_count": len(skill_ids),
        "description": description,
    }

    # Runbook must link to at least one skill node
    anchor_ids = list(set(skill_ids + (connects_to or [])))

    runbook = create_node(
        conn,
        label=label,
        node_type="runbook",
        content=description or label,
        created_by=created_by,
        metadata=metadata,
        connects_to=anchor_ids,
    )

    # Create DEPENDS_ON chain: skill[0] → skill[1] → ... → skill[n]
    for i, sid in enumerate(skill_ids):
        sid = validate_identifier(sid, name="skill_id")
        row = conn.execute(
            "SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [sid]
        ).fetchone()
        if not row:
            raise NodeNotFoundError(f"Skill node not found: {sid}")
        if i > 0:
            create_edge(
                conn,
                from_node=skill_ids[i - 1],
                to_node=sid,
                edge_type="DEPENDS_ON",
                layer="L4",
                created_by=created_by,
                metadata={"order": i},
            )

    # Link runbook to first skill
    create_edge(
        conn,
        from_node=runbook["id"],
        to_node=skill_ids[0],
        edge_type="DEPENDS_ON",
        layer="L4",
        created_by=created_by,
        metadata={"order": 0, "entry_point": True},
    )

    return runbook


def get_runbook_steps(
    conn: DuckDBPyConnection,
    *,
    runbook_id: str,
) -> dict[str, Any]:
    """Get the ordered skill chain for a runbook (OHM-461f)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    runbook_id = validate_identifier(runbook_id, name="runbook_id")

    row = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'runbook' AND deleted_at IS NULL",
        [runbook_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Runbook not found: {runbook_id}")

    import json as _json

    meta_raw = row[2]
    meta: dict[str, Any] = {}
    if meta_raw:
        try:
            meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    skill_ids = meta.get("skill_ids", [])

    skills: list[dict[str, Any]] = []
    for sid in skill_ids:
        skill_row = _rows_to_dicts(
            conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [sid])
        )
        if skill_row:
            skills.append(skill_row[0])

    return {
        "runbook_id": runbook_id,
        "label": row[1],
        "skills": skills,
        "skill_count": len(skills),
    }


def create_challenge(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    reason: str,
    created_by: str,
    confidence: float = 0.5,
) -> dict[str, Any]:
    """Create a CHALLENGED_BY edge referencing an existing edge.

    Enforces boundary rules: only L3/L4 edges can be challenged.
    Enforces OHM-e0t1 lint: reason must be non-empty (ADR-018).
    """
    import uuid

    from ohm.boundary import enforce_challenge_boundary
    from ohm.graph.challenges import require_challenge_reason
    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)
    # OHM-e0t1: enforce non-empty reason at write time. This implements
    # the require_reasoning: True constraint declared in
    # EDGE_CONSTRAINTS['CHALLENGED_BY'] which was previously dead code.
    reason = require_challenge_reason(reason)
    enforce_challenge_boundary(conn, created_by, edge_id)

    target = conn.execute(
        "SELECT id, from_node, to_node, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        raise ValueError(f"Edge not found: {edge_id}")

    # OHM-mzyc.2: dedup — don't create a duplicate CHALLENGED_BY edge
    # from the same agent for the same original edge.
    existing = conn.execute(
        "SELECT id FROM ohm_edges WHERE challenge_of = ? AND edge_type = 'CHALLENGED_BY' AND created_by = ? AND deleted_at IS NULL",
        [edge_id, created_by],
    ).fetchone()
    if existing:
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [existing[0]]))[0]

    challenge_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
           (id, from_node, to_node, layer, edge_type, created_by,
            confidence, condition, challenge_of, challenge_type)
           VALUES (?, ?, ?, ?, 'CHALLENGED_BY', ?, ?, ?, ?, 'CHALLENGED_BY')""",
        [challenge_id, target[1], target[2], target[3], created_by, confidence, reason, edge_id],
    )
    _log_change(conn, "ohm_edges", challenge_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [challenge_id]))[0]


def create_support(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    reason: str,
    created_by: str,
    confidence: float = 0.7,
) -> dict[str, Any]:
    """Create a SUPPORTS edge referencing an existing edge.

    Enforces boundary rules: only L3/L4 edges can be supported.
    """
    import uuid

    from ohm.boundary import enforce_support_boundary
    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)
    enforce_support_boundary(conn, created_by, edge_id)

    target = conn.execute(
        "SELECT id, from_node, to_node, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        raise ValueError(f"Edge not found: {edge_id}")

    # OHM-mzyc.2: dedup — don't create a duplicate SUPPORTS edge
    # from the same agent for the same original edge.
    existing = conn.execute(
        "SELECT id FROM ohm_edges WHERE challenge_of = ? AND edge_type = 'SUPPORTS' AND created_by = ? AND deleted_at IS NULL",
        [edge_id, created_by],
    ).fetchone()
    if existing:
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [existing[0]]))[0]

    support_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
           (id, from_node, to_node, layer, edge_type, created_by,
            confidence, condition, challenge_of, challenge_type)
           VALUES (?, ?, ?, ?, 'SUPPORTS', ?, ?, ?, ?, 'SUPPORTS')""",
        [support_id, target[1], target[2], target[3], created_by, confidence, reason, edge_id],
    )
    _log_change(conn, "ohm_edges", support_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [support_id]))[0]


def find_homogeneous_causes(
    conn: DuckDBPyConnection,
    *,
    target_node_id: str | None = None,
    min_confidence: float = 0.5,
    homogeneity_threshold: float = 0.8,
    min_support_count: int = 2,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find L3 CAUSES edges whose supporting evidence is homogeneous (OHM-jbsr).

    A CAUSES edge is flagged when its SUPPORTS edges (linked via challenge_of)
    all share the same source_tier (or all lack one) — the recursive-agreement
    pattern that oppositional review targets. homogeneity_score is 1.0 when
    every supporter shares a single tier, falling toward 0 as distinct tiers
    grow. Only edges with at least min_support_count supporters are considered.
    """
    from ohm.validation import validate_identifier

    params: list[Any] = [min_confidence, min_support_count]
    target_clause = ""
    if target_node_id is not None:
        target_node_id = validate_identifier(target_node_id, name="target_node_id")
        target_clause = "AND e.to_node = ?"
        params = [min_confidence, target_node_id, min_support_count]

    rows = conn.execute(
        f"""
        SELECT
            e.id, e.from_node, e.to_node, e.confidence, e.source_tier, e.created_by,
            COUNT(sup.id) AS support_count,
            COUNT(DISTINCT sup.source_tier) AS distinct_tiers,
            COUNT(DISTINCT sup.created_by) AS distinct_agents,
            MAX(sup.source_tier) AS support_tier
        FROM ohm_edges e
        LEFT JOIN ohm_edges sup ON (
            sup.challenge_of = e.id
            AND sup.edge_type = 'SUPPORTS'
            AND sup.deleted_at IS NULL
        )
        WHERE e.edge_type = 'CAUSES'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
          AND e.confidence >= ?
          {target_clause}
        GROUP BY e.id, e.from_node, e.to_node, e.confidence, e.source_tier, e.created_by
        HAVING COUNT(sup.id) >= ?
        """,
        params,
    ).fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        (edge_id, from_node, to_node, confidence, source_tier, created_by, support_count, distinct_tiers, distinct_agents, support_tier) = row
        if support_count <= 0:
            continue
        score = 1.0 if distinct_tiers <= 1 else 1.0 - (distinct_tiers / support_count)
        if score < homogeneity_threshold:
            continue
        tier_label = support_tier if support_tier is not None else "(unassigned)"
        results.append(
            {
                "edge_id": edge_id,
                "from_node": from_node,
                "to_node": to_node,
                "confidence": confidence,
                "source_tier": source_tier,
                "created_by": created_by,
                "homogeneity_score": round(score, 4),
                "support_count": int(support_count),
                "distinct_tiers": int(distinct_tiers),
                "distinct_agents": int(distinct_agents),
                "support_tier": support_tier,
                "reason": (f"{support_count} supporting SUPPORTS edge(s) share source_tier '{tier_label}' from {distinct_agents} agent(s) — homogeneous support"),
            }
        )
    results.sort(key=lambda r: (r["homogeneity_score"], r["confidence"]), reverse=True)
    return results[:limit]


def detect_consensus_only_support(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
) -> dict[str, Any]:
    """Detect whether a CAUSES edge's strongest support is consensus-only (OHM-2yq2).

    Consensus-only = the edge has >=1 SUPPORTS edge (linked via challenge_of)
    but NONE of those supporters' from_nodes have a recorded outcome in
    ohm_outcomes. Per the Hillman truth-vs-consensus framing, agreement without
    observable outcome is consensus, not evidence. The recommended ceiling is
    SOURCE_TIER_CEILINGS[strongest_tier] when consensus-only, else None.
    """
    from ohm.graph.schema import SOURCE_TIER_CEILINGS
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    supporters = conn.execute(
        """SELECT id, from_node, confidence, source_tier, created_by
           FROM ohm_edges
           WHERE challenge_of = ? AND edge_type = 'SUPPORTS' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchall()

    if not supporters:
        return {
            "edge_id": edge_id,
            "is_consensus_only": False,
            "supporting_edges": [],
            "strongest_tier": None,
            "strongest_ceiling": None,
            "has_verified_outcome": False,
            "recommended_ceiling": None,
        }

    has_outcome = False
    supporting: list[dict[str, Any]] = []
    tiers: list[str] = []
    for row in supporters:
        sup_id, from_node, conf, tier, created_by = row
        supporting.append(
            {
                "id": sup_id,
                "from_node": from_node,
                "confidence": conf,
                "source_tier": tier,
                "created_by": created_by,
            }
        )
        if tier is not None:
            tiers.append(tier)
        if not has_outcome:
            oc = conn.execute(
                "SELECT 1 FROM ohm_outcomes WHERE claim_node = ?",
                [from_node],
            ).fetchone()
            if oc:
                has_outcome = True

    strongest_tier = None
    strongest_ceiling = None
    if tiers:
        strongest_tier = max(tiers, key=lambda t: SOURCE_TIER_CEILINGS.get(t, 0.0))
        strongest_ceiling = SOURCE_TIER_CEILINGS.get(strongest_tier)

    is_consensus_only = not has_outcome
    return {
        "edge_id": edge_id,
        "is_consensus_only": is_consensus_only,
        "supporting_edges": supporting,
        "strongest_tier": strongest_tier,
        "strongest_ceiling": strongest_ceiling,
        "has_verified_outcome": has_outcome,
        "recommended_ceiling": strongest_ceiling if is_consensus_only else None,
    }


def fire_verification_nudge(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    reason: str,
    created_by: str = "system",
    confidence: float = 0.3,
) -> dict[str, Any]:
    """Auto-fire a consensus-only challenge nudge (OHM-2yq2).

    Creates a CHALLENGED_BY edge with challenge_type='CONSENSUS_FLAG' referencing
    the target edge. Idempotent: if a CONSENSUS_FLAG nudge already exists for the
    edge, returns the existing one without creating a duplicate.
    """
    import uuid

    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)

    existing = conn.execute(
        """SELECT id FROM ohm_edges
           WHERE challenge_of = ? AND challenge_type = 'CONSENSUS_FLAG' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchone()
    if existing:
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [existing[0]]))[0]

    target = conn.execute(
        "SELECT id, from_node, to_node, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        raise ValueError(f"Edge not found: {edge_id}")

    challenge_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
             (id, from_node, to_node, layer, edge_type, created_by,
              confidence, condition, challenge_of, challenge_type)
           VALUES (?, ?, ?, ?, 'CHALLENGED_BY', ?, ?, ?, ?, 'CONSENSUS_FLAG')""",
        [challenge_id, target[1], target[2], target[3], created_by, confidence, reason, edge_id],
    )
    _log_change(conn, "ohm_edges", challenge_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [challenge_id]))[0]


def delete_node(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    deleted_by: str,
) -> dict[str, Any]:
    """Delete a node and all its associated edges and observations.

    Deletes edges in two separate statements (from_node, to_node) to avoid
    DuckDB index issues with OR conditions (OHM-cpi).

    Returns a dict with the deleted node_id and counts of removed edges/observations.
    Raises NodeNotFoundError if the node doesn't exist.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Verify node exists
    node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]))
    if not node:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Node not found: {node_id}")

    # Delete associated edges — split into two statements to avoid DuckDB
    # index issues with OR conditions (OHM-cpi)
    # Note: conn.execute() for UPDATE returns the connection itself; call
    # fetchone() to get the row count of affected rows.
    # OHM-sdp1: capture the edge row_ids BEFORE the UPDATE so each cascaded
    # edge can be logged to ohm_change_feed (operators need per-row
    # attribution when a node delete cascades to N edges — the audit feed
    # used to record only the node row, leaving edges unattributed).
    edge_ids_to_delete = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND deleted_at IS NULL",
            [node_id, node_id],
        ).fetchall()
    ]
    edges_from = conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND deleted_at IS NULL", [node_id]).fetchone()
    edges_to = conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE to_node = ? AND deleted_at IS NULL", [node_id]).fetchone()
    edges_deleted = (edges_from[0] if edges_from else 0) + (edges_to[0] if edges_to else 0)
    for eid in edge_ids_to_delete:
        _log_change(conn, "ohm_edges", eid, "DELETE", deleted_by)

    # Delete observations. Same audit-trail fix (OHM-sdp1): capture
    # observation row_ids before the UPDATE so each gets a feed entry.
    obs_ids_to_delete = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchall()
    ]
    obs_result = conn.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE node_id = ? AND deleted_at IS NULL", [node_id])
    obs_row = obs_result.fetchone()
    obs_count = obs_row[0] if obs_row else 0
    for oid in obs_ids_to_delete:
        _log_change(conn, "ohm_observations", oid, "DELETE", deleted_by)

    # Delete the node itself
    conn.execute("UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [node_id])
    _log_change(conn, "ohm_nodes", node_id, "DELETE", deleted_by)

    return {
        "deleted": node_id,
        "type": "node",
        "edges_removed": edges_deleted,
        "observations_removed": obs_count,
    }


def delete_edge(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    deleted_by: str,
) -> dict[str, Any]:
    """Delete an edge by ID.

    Returns a dict with the deleted edge_id.
    Raises EdgeNotFoundError if the edge doesn't exist.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")

    # Verify edge exists
    edge = _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge_id]))
    if not edge:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    edge[0].get("layer")

    # Delete observations referencing this edge.
    # OHM-sdp1: capture observation row_ids BEFORE the UPDATE so each
    # cascaded observation gets a feed entry (parity with the node-delete
    # cascade audit fix).
    obs_ids_to_delete = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM ohm_observations WHERE edge_id = ? AND deleted_at IS NULL",
            [edge_id],
        ).fetchall()
    ]
    obs_result = conn.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE edge_id = ? AND deleted_at IS NULL", [edge_id])
    for oid in obs_ids_to_delete:
        _log_change(conn, "ohm_observations", oid, "DELETE", deleted_by)

    # Delete the edge
    conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", [edge_id])
    _log_change(conn, "ohm_edges", edge_id, "DELETE", deleted_by)

    return {
        "deleted": edge_id,
        "type": "edge",
        "observations_removed": obs_result.rowcount or 0,
    }


def set_agent_state(
    conn: DuckDBPyConnection,
    *,
    agent_name: str,
    focus: str | None = None,
    values: str | None = None,
    goals: str | None = None,
) -> None:
    """Set or update an agent's current focus, values, and goals.

    Dynamic SET clause uses hardcoded column names only — all values
    are parameterized with ? placeholders.
    """
    existing = conn.execute("SELECT 1 FROM ohm_agent_state WHERE agent_name = ?", [agent_name]).fetchone()
    if existing:
        # Column names are hardcoded, values use ? — safe from injection
        set_parts = ["current_focus = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[str | None] = [focus]
        if values is not None:
            set_parts.append("values = ?")
            params.append(values)
        if goals is not None:
            set_parts.append("goals = ?")
            params.append(goals)
        params.append(agent_name)
        conn.execute(
            "UPDATE ohm_agent_state SET " + ", ".join(set_parts) + " WHERE agent_name = ?",
            params,
        )
    else:
        conn.execute(
            "INSERT INTO ohm_agent_state (agent_name, current_focus, values, goals, updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            [agent_name, focus, values, goals],
        )


def create_observation(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    obs_type: str,
    created_by: str,
    value: float | None = None,
    baseline: float | None = None,
    sigma: float | None = None,
    source: str | None = None,
    edge_id: str | None = None,
    notes: str | None = None,
    source_name: str | None = None,
    source_url: str | None = None,
    scale: str | None = None,
    half_life_days: float | None = None,
    weibull_shape: float | None = None,
    metadata: dict | None = None,
    worktree_ref: str | None = None,
    evaluation_script: str | None = None,
    held_out: bool | None = None,
) -> dict[str, Any]:
    """Create an observation on a node or edge and return its full record."""
    from ohm.graph.schema import VALID_OBSERVATION_SCALES
    from ohm.graph.decay import default_half_life, default_weibull_shape

    if scale is not None and scale not in VALID_OBSERVATION_SCALES:
        raise ValueError(f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}")
    if scale == "probability" and value is not None and (value < 0.0 or value > 1.0):
        raise ValueError(f"Observation value {value} is outside [0, 1] for scale='probability'")
    import uuid
    from datetime import datetime, timezone

    # OHM-xdd4: resolve half_life_days — explicit override > type default
    if half_life_days is None:
        half_life_days = default_half_life(obs_type)
    # OHM-24g9: resolve weibull_shape — explicit override > type default
    if weibull_shape is None:
        weibull_shape = default_weibull_shape(obs_type)

    obs_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    import json

    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO ohm_observations
           (id, node_id, edge_id, type, value, baseline, sigma, source, created_by, notes,
            source_name, source_url, scale, half_life_days, weibull_shape, valid_from,
            metadata, worktree_ref, evaluation_script, held_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [obs_id, node_id, edge_id, obs_type, value, baseline, sigma, source, created_by, notes, source_name, source_url, scale, half_life_days, weibull_shape, now, metadata_json, worktree_ref, evaluation_script, held_out],
    )
    _log_change(conn, "ohm_observations", obs_id, "INSERT", created_by)
    # Return full observation record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_observations WHERE id = ?", [obs_id]))[0]


def node_exists(conn: DuckDBPyConnection, node_id: str) -> bool:
    """Check if a node exists."""
    result = conn.execute("SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()
    return result is not None


def edge_exists(conn: DuckDBPyConnection, edge_id: str) -> bool:
    """Check if an edge exists."""
    result = conn.execute("SELECT 1 FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge_id]).fetchone()
    return result is not None


def query_provenance(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Trace the provenance chain backward from a node.

    Follows DERIVES_FROM, REFERENCES, INFLUENCES, and SUPPORTS edges
    (L2 provenance edges) backward from the target node to find
    primary sources. Returns each source with its chain depth and
    confidence product (how much of the original confidence survives
    the chain).

    Args:
        node_id: The node to trace from.
        max_depth: Maximum chain depth (default 10).

    Returns:
        List of dicts: source_node_id, source_label, chain_depth,
        confidence_product, chain_path (list of edge IDs).
    """
    from ohm.validation import validate_identifier, validate_depth

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth)

    frozenset(
        {
            "DERIVES_FROM",
            "REFERENCES",
            "INFLUENCES",
            "SUPPORTS",
        }
    )

    # Recursive CTE: traverse forward through provenance edges
    # DERIVES_FROM: from_node = derived, to_node = source
    # So to find sources, we follow FROM the current node
    query = """
        WITH RECURSIVE prov_chain AS (
            -- Start from the target node
            SELECT
                ? AS node_id,
                0 AS depth,
                1.0 AS conf_product,
                []::VARCHAR[] AS path

            UNION ALL

            -- Follow provenance edges forward (from_node = current → to_node = source)
            SELECT
                e.to_node AS node_id,
                pc.depth + 1 AS depth,
                pc.conf_product * COALESCE(e.confidence, 1.0) AS conf_product,
                array_append(pc.path, e.id) AS path
            FROM prov_chain pc
            JOIN ohm_edges e ON e.from_node = pc.node_id
            WHERE pc.depth < ?
              AND e.edge_type IN ('DERIVES_FROM', 'REFERENCES', 'INFLUENCES', 'SUPPORTS')
              AND NOT array_contains(pc.path, e.id)  -- cycle detection
        )
        SELECT
            pc.node_id,
            n.label AS source_label,
            n.type AS source_type,
            n.created_by AS source_author,
            pc.depth,
            ROUND(pc.conf_product, 4) AS confidence_product,
            pc.path AS chain_path
        FROM prov_chain pc
        LEFT JOIN ohm_nodes n ON n.id = pc.node_id
        WHERE pc.depth > 0  -- exclude the starting node itself
        ORDER BY pc.depth, pc.conf_product DESC
    """

    result = conn.execute(query, [node_id, max_depth])
    return _rows_to_dicts(result)


def query_graph_health(
    conn: DuckDBPyConnection,
) -> dict[str, Any]:
    """Graph health diagnostics.

    Returns counts of common graph health issues:
    - orphan_nodes: nodes with 0 edges (isolated, invisible)
    - dead_end_count: nodes with only incoming edges, no outgoing (sinks)
    - low_confidence_unchallenged: L3/L4 edges with confidence < 0.3 and no challenges
    - stale_agents: agents with last_sync > 2x their sync interval
    - disconnected_components: groups of nodes not connected to the main graph
    - dense_clusters: nodes with 10+ direct edges (might need synthesis)

    Same result regardless of which agent calls it — substrate method.
    """
    # Orphan nodes
    orphan_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE e.from_node = n.id OR e.to_node = n.id
          )
    """).fetchone()
    orphans = orphan_row[0] if orphan_row else 0

    # Low-confidence unchallenged edges
    low_conf_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_edges e
        WHERE e.confidence < 0.3
          AND e.layer IN ('L3', 'L4')
          AND NOT EXISTS (
            SELECT 1 FROM ohm_edges c
            WHERE c.challenge_of = e.id AND c.challenge_type = 'CHALLENGED_BY'
          )
    """).fetchone()
    low_conf = low_conf_row[0] if low_conf_row else 0

    # Dense clusters (nodes with 10+ edges)
    dense = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT from_node AS node_id FROM ohm_edges
            UNION ALL
            SELECT to_node AS node_id FROM ohm_edges
        ) GROUP BY node_id HAVING COUNT(*) >= 10
    """).fetchone()
    dense_count = dense[0] if dense else 0

    # Stale agents
    stale = conn.execute("""
        SELECT COUNT(*) FROM ohm_agent_state a
        WHERE a.last_sync IS NOT NULL
          AND a.last_sync < CURRENT_TIMESTAMP - INTERVAL (2 * a.confidence_threshold) HOUR
    """).fetchone()
    stale_count = stale[0] if stale else 0

    # Total counts (OHM-a5rz.6: exclude L0 fragment nodes from totals)
    total_nodes_row = conn.execute("SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'").fetchone()
    total_nodes = total_nodes_row[0] if total_nodes_row else 0
    total_edges_row = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()
    total_edges = total_edges_row[0] if total_edges_row else 0

    # Dead-end nodes: have incoming edges but no outgoing edges. Reachable
    # but cannot lead anywhere. Tracked separately from orphan_nodes (which
    # have no edges at all). The cross-link requirement (OHM-tjzh) targets
    # the prevention of new dead-ends; this metric tracks the legacy tail.
    dead_end_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND EXISTS (SELECT 1 FROM ohm_edges e WHERE e.to_node = n.id AND e.deleted_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM ohm_edges e WHERE e.from_node = n.id AND e.deleted_at IS NULL)
    """).fetchone()
    dead_end_count = dead_end_row[0] if dead_end_row else 0

    orphan_type_rows = conn.execute("""
        SELECT n.type, COUNT(*) as cnt
        FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE e.from_node = n.id OR e.to_node = n.id
          )
        GROUP BY n.type
        ORDER BY cnt DESC
    """).fetchall()
    orphan_type_breakdown = {row[0]: row[1] for row in orphan_type_rows} if orphan_type_rows else {}

    # OHM-jx4q: Distinguish L0 fragment orphans from L1-L3 orphans. Fragments
    # are expected high-churn scratch nodes (type='fragment'); excluding them
    # from the orphan rate gives a much more actionable signal for triage.
    # A "real" orphan rate of >10% triggers the heartbeat nudge (see
    # agent_heartbeat() in methods.py).
    fragment_orphans = orphan_type_breakdown.get("fragment", 0)
    non_fragment_orphans = max(0, orphans - fragment_orphans)
    total_non_fragment_nodes = max(0, total_nodes)  # already excludes fragments
    orphan_rate_non_fragments = round(non_fragment_orphans / total_non_fragment_nodes, 4) if total_non_fragment_nodes > 0 else 0.0
    orphan_threshold = 0.10  # OHM-jx4q acceptance: < 10% for non-fragments

    return {
        "orphan_nodes": orphans,
        "orphan_nodes_total": orphans,
        "orphan_nodes_fragments": fragment_orphans,
        "orphan_nodes_non_fragments": non_fragment_orphans,
        "orphan_rate_total": round(orphans / max(total_nodes, 1), 4),
        "orphan_rate_non_fragments": orphan_rate_non_fragments,
        "orphan_threshold": orphan_threshold,
        "orphan_threshold_exceeded": orphan_rate_non_fragments > orphan_threshold,
        "orphan_type_breakdown": orphan_type_breakdown,
        "dead_end_count": dead_end_count,
        "low_confidence_unchallenged": low_conf,
        "dense_clusters": dense_count,
        "stale_agents": stale_count,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "health_score": round(1.0 - (orphans + low_conf + stale_count) / max(total_nodes, 1), 4),
    }


def query_find_orphan_agents(
    conn: DuckDBPyConnection,
) -> list[dict[str, Any]]:
    """Find orphan agent nodes from pre-idempotent registration (OHM-7pf).

    Before idempotent registration, agents could create multiple nodes
    with non-deterministic IDs (e.g., metis_8b678b). Returns agent nodes
    whose IDs don't match the deterministic pattern.
    """
    import re

    result = conn.execute("SELECT id, label, type, created_by, created_at, content FROM ohm_nodes WHERE type = 'agent' ORDER BY created_at")
    agents = _rows_to_dicts(result)

    orphans = []
    for agent in agents:
        label = agent.get("label", "")
        expected_id = "agent_" + re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
        if agent["id"] != expected_id:
            orphans.append({**agent, "expected_id": expected_id})

    return orphans


def query_stale_edges(
    conn: DuckDBPyConnection,
    *,
    half_life_days: dict[str, float] | None = None,
    stale_threshold: float = 0.1,
) -> list[dict[str, Any]]:
    """Find edges whose effective confidence has decayed below a threshold.

    Confidence decay is computed entirely in SQL (OHM-od01.11):
    - L1/L2: no decay (shared facts and citations are permanent)
    - L3: 90-day half-life (interpretations age slowly)
    - L4: 30-day half-life (predictions age fast)
    - Private: 30-day half-life

    effective_confidence = confidence * 0.5 ^ ((now - created_at) / half_life_days)

    Args:
        half_life_days: Override per-layer half-lives. Default:
            {"L1": inf, "L2": inf, "L3": 90, "L4": 30}
        stale_threshold: Edges below this effective confidence are stale (default 0.1).

    Returns:
        List of stale edge records with original and effective confidence.
    """
    defaults = {"L1": float("inf"), "L2": float("inf"), "L3": 90.0, "L4": 30.0}
    if half_life_days:
        _valid_layers = frozenset({"L1", "L2", "L3", "L4"})
        for k, v in half_life_days.items():
            if k not in _valid_layers:
                raise ValueError(f"Invalid layer in half_life_days: {k!r}")
            if not isinstance(v, (int, float)) or v != v:  # NaN check
                raise ValueError(f"Invalid half_life_days value for {k}: {v!r}")
        defaults.update(half_life_days)

    # Values are validated numeric literals from the hardcoded defaults dict — safe to interpolate.
    when_clauses = " ".join(f"WHEN '{k}' THEN {999999.0 if v == float('inf') or v <= 0 else float(v)}" for k, v in defaults.items())
    hl_case = f"CASE layer {when_clauses} ELSE 90.0 END"

    result = conn.execute(
        f"""
        WITH decayed AS (
            SELECT
                id, from_node, to_node, layer, edge_type,
                created_by, confidence, created_at, challenge_of,
                {hl_case} AS half_life,
                GREATEST(date_diff('day', created_at, CURRENT_TIMESTAMP), 0)::DOUBLE AS age_days,
                COALESCE(confidence, 1.0) * power(0.5,
                    GREATEST(date_diff('day', created_at, CURRENT_TIMESTAMP), 0)::DOUBLE /
                    {hl_case}
                ) AS effective_confidence,
                power(0.5,
                    GREATEST(date_diff('day', created_at, CURRENT_TIMESTAMP), 0)::DOUBLE /
                    {hl_case}
                ) AS decay_factor
            FROM ohm_edges
            WHERE created_at IS NOT NULL
              AND (deleted_at IS NULL OR CAST(deleted_at AS VARCHAR) = '')
              AND {hl_case} > 0
              AND {hl_case} < 999999.0
        )
        SELECT * FROM decayed
        WHERE effective_confidence < ?
        ORDER BY effective_confidence ASC
    """,
        [stale_threshold],
    )

    rows = _rows_to_dicts(result)

    for edge in rows:
        edge["effective_confidence"] = round(edge["effective_confidence"], 4)
        edge["decay_factor"] = round(edge["decay_factor"], 4)
        edge["age_days"] = round(edge["age_days"], 1)

    return rows


def apply_confidence_decay(
    conn: DuckDBPyConnection,
    *,
    stale_threshold: float = 0.1,
    layer: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply confidence decay to stale edges.

    Reads effective confidence using the decay formula, then updates the
    stored confidence for edges whose effective_confidence < stale_threshold.

    L1/L2 edges are never decayed (permanent).
    L3 edges decay with 90-day half-life.
    L4 edges decay with 30-day half-life.

    effective_confidence = confidence * 0.5 ^ (age_days / half_life)

    Args:
        stale_threshold: Effective confidence below this is decayed (default 0.1).
        layer: If set, only decay edges in this layer.
        dry_run: If True, compute decay but don't update database.

    Returns:
        dict with 'updated' (int), 'skipped' (int), and 'decayed' (list of dicts).
    """
    # Get stale edges (reuse existing logic)
    stale = query_stale_edges(conn, stale_threshold=stale_threshold)

    # Filter by layer if specified
    if layer:
        stale = [e for e in stale if e.get("layer") == layer]

    decayed = []
    skipped = 0
    updated = 0

    for edge in stale:
        # L1/L2 never decay (but they won't appear in stale due to infinite half-life)
        if edge.get("layer") in ("L1", "L2"):
            skipped += 1
            continue

        original_conf = edge.get("confidence", 1.0) or 1.0
        effective_conf = edge.get("effective_confidence", original_conf)

        # Compute what the new confidence should be
        # effective = original * decay_factor, so decay_factor = effective / original
        if original_conf > 0:
            decay_factor = effective_conf / original_conf
            new_confidence = round(effective_conf, 4)
        else:
            continue

        decayed.append(
            {
                "id": edge["id"],
                "confidence": original_conf,
                "new_confidence": new_confidence,
                "decay_factor": round(decay_factor, 4),
                "age_days": edge.get("age_days", 0),
                "layer": edge.get("layer"),
                "edge_type": edge.get("edge_type"),
            }
        )

        if not dry_run:
            conn.execute(
                "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [new_confidence, edge["id"]],
            )
            _log_change(conn, "ohm_edges", edge["id"], "UPDATE", "decay")
            updated += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "decayed": decayed,
    }


def batch_create_nodes(
    conn: DuckDBPyConnection,
    *,
    nodes: list[dict[str, Any]],
    created_by: str,
) -> list[dict[str, Any]]:
    """Create multiple nodes in a single transaction.

    All succeed or all fail. Returns list of full node records.

    Args:
        nodes: List of dicts with keys: label, node_type (default 'concept'),
               content, visibility, provenance, confidence.
        created_by: Agent name for attribution.

    Returns:
        List of created node records.
    """
    # OHM-aadc: try the fast multi-row INSERT path first. Returns None
    # if the fast path doesn't apply (connects_to, soft-deleted collisions,
    # validation edge cases), in which case we fall back to the per-row
    # path that gives clearer error messages.
    try:
        from ohm.graph.batch import fast_batch_create_nodes

        fast = fast_batch_create_nodes(conn, nodes=nodes, created_by=created_by)
        if fast is not None:
            return fast
    except Exception:
        pass

    results = []
    for node_data in nodes:
        result = create_node(
            conn,
            label=node_data["label"],
            node_type=node_data.get("node_type", "concept"),
            content=node_data.get("content"),
            created_by=created_by,
            visibility=node_data.get("visibility", "team"),
            provenance=node_data.get("provenance"),
            confidence=node_data.get("confidence", 1.0),
        )
        results.append(result)
    return results


def batch_create_edges(
    conn: DuckDBPyConnection,
    *,
    edges: list[dict[str, Any]],
    created_by: str,
) -> list[dict[str, Any]]:
    """Create multiple edges in a single transaction.

    All succeed or all fail. Returns list of full edge records.

    Args:
        edges: List of dicts with keys: from_node, to_node, edge_type, layer,
               confidence, condition, provenance.
        created_by: Agent name for attribution.

    Returns:
        List of created edge records.
    """
    # OHM-aadc: try the fast multi-row INSERT path first.
    try:
        from ohm.graph.batch import fast_batch_create_edges

        fast = fast_batch_create_edges(conn, edges=edges, created_by=created_by)
        if fast is not None:
            return fast
    except Exception:
        pass

    results = []
    for edge_data in edges:
        result = create_edge(
            conn,
            from_node=edge_data["from_node"],
            to_node=edge_data["to_node"],
            edge_type=edge_data["edge_type"],
            layer=edge_data.get("layer", "L3"),
            created_by=created_by,
            confidence=edge_data.get("confidence", 0.7),
            condition=edge_data.get("condition"),
            provenance=edge_data.get("provenance"),
        )
        results.append(result)
    return results


def create_batch(
    conn: DuckDBPyConnection,
    *,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    created_by: str,
) -> dict[str, Any]:
    """Create multiple nodes and edges in a single transaction.

    All succeed or all fail. Each item populates the change feed individually.

    Args:
        nodes: Optional list of node dicts (keys: label, node_type, content,
               visibility, provenance, confidence, priority, url).
        edges: Optional list of edge dicts (keys: from_node, to_node,
               edge_type, layer, confidence, condition, provenance, urgency,
               probability).
        created_by: Agent name for attribution.

    Returns:
        Dict with keys: nodes_created, edges_created, nodes, edges.
    """
    nodes = nodes or []
    edges = edges or []

    created_nodes = batch_create_nodes(conn, nodes=nodes, created_by=created_by)
    created_edges = batch_create_edges(conn, edges=edges, created_by=created_by)

    return {
        "nodes_created": len(created_nodes),
        "edges_created": len(created_edges),
        "nodes": created_nodes,
        "edges": created_edges,
    }


# ── Diff ────────────────────────────────────────────────────────────────────


def query_diff(
    conn: DuckDBPyConnection,
    from_ts: str,
    to_ts: str,
    *,
    layer: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Show what changed between two timestamps.

    Returns nodes, edges, and observations that were created or updated
    between *from_ts* and *to_ts*. Supports optional --layer and --agent filters.

    Args:
        conn: Database connection.
        from_ts: Starting ISO timestamp.
        to_ts: Ending ISO timestamp.
        layer: Optional layer filter (L1-L4) for edges.
        agent_name: Optional agent filter for attribution.

    Returns:
        Dict with keys: from, to, nodes_added, nodes_updated, edges_added,
        edges_updated, observations_added, summary.
    """
    from ohm.validation import validate_identifier, validate_layer, validate_timestamp

    from_ts = validate_timestamp(from_ts)
    to_ts = validate_timestamp(to_ts)
    if layer:
        layer = validate_layer(layer)
    if agent_name:
        agent_name = validate_identifier(agent_name, name="agent_name")

    # ── Nodes ──────────────────────────────────────────────────────────
    node_params: list = [from_ts, to_ts]
    node_agent_clause = ""
    if agent_name:
        node_agent_clause = "AND created_by = ?"
        node_params.append(agent_name)

    nodes_added = _rows_to_dicts(
        conn.execute(
            f"SELECT * FROM ohm_nodes WHERE created_at >= ? AND created_at <= ? {node_agent_clause} ORDER BY created_at",
            node_params,
        )
    )

    nodes_updated = _rows_to_dicts(
        conn.execute(
            f"SELECT * FROM ohm_nodes WHERE updated_at >= ? AND updated_at <= ? AND updated_at != created_at {node_agent_clause} ORDER BY updated_at",
            node_params,
        )
    )

    # ── Edges ──────────────────────────────────────────────────────────
    edge_params: list = [from_ts, to_ts]
    edge_clauses = ""
    if layer:
        edge_clauses += "AND layer = ?"
        edge_params.append(layer)
    if agent_name:
        edge_clauses += "AND created_by = ?"
        edge_params.append(agent_name)

    edges_added = _rows_to_dicts(
        conn.execute(
            f"SELECT * FROM ohm_edges WHERE created_at >= ? AND created_at <= ? {edge_clauses} ORDER BY created_at",
            edge_params,
        )
    )

    edges_updated = _rows_to_dicts(
        conn.execute(
            f"SELECT * FROM ohm_edges WHERE updated_at >= ? AND updated_at <= ? AND updated_at != created_at {edge_clauses} ORDER BY updated_at",
            edge_params,
        )
    )

    # ── Observations ───────────────────────────────────────────────────
    obs_params: list = [from_ts, to_ts]
    obs_agent_clause = ""
    if agent_name:
        obs_agent_clause = "AND created_by = ?"
        obs_params.append(agent_name)

    observations_added = _rows_to_dicts(
        conn.execute(
            f"SELECT * FROM ohm_observations WHERE created_at >= ? AND created_at <= ? {obs_agent_clause} ORDER BY created_at",
            obs_params,
        )
    )

    # ── Summary ────────────────────────────────────────────────────────
    summary = {
        "nodes_added": len(nodes_added),
        "nodes_updated": len(nodes_updated),
        "edges_added": len(edges_added),
        "edges_updated": len(edges_updated),
        "observations_added": len(observations_added),
        "total_changes": (len(nodes_added) + len(nodes_updated) + len(edges_added) + len(edges_updated) + len(observations_added)),
    }

    return {
        "from": from_ts,
        "to": to_ts,
        "nodes_added": nodes_added,
        "nodes_updated": nodes_updated,
        "edges_added": edges_added,
        "edges_updated": edges_updated,
        "observations_added": observations_added,
        "summary": summary,
    }


# ── Snapshot ────────────────────────────────────────────────────────────────


def query_snapshot(
    conn: DuckDBPyConnection,
    timestamp: str,
    *,
    node_id: str | None = None,
    edge_id: str | None = None,
) -> dict[str, Any]:
    """Reconstruct graph state at a historical timestamp.

    Shows all nodes, edges, and observations that existed at or before
    the given timestamp. Useful for: "what did we know about X on May 1st?"

    Args:
        conn: Database connection.
        timestamp: ISO timestamp to reconstruct state at.
        node_id: Optional single node ID to focus on.
        edge_id: Optional single edge ID to focus on.

    Returns:
        Dict with keys: timestamp, nodes, edges, observations, summary.
    """
    from ohm.validation import validate_identifier, validate_timestamp

    timestamp = validate_timestamp(timestamp)
    if node_id:
        node_id = validate_identifier(node_id, name="node_id")
    if edge_id:
        edge_id = validate_identifier(edge_id, name="edge_id")

    # ── Nodes ──────────────────────────────────────────────────────────
    if node_id:
        nodes = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE id = ? AND created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?)",
                [node_id, timestamp, timestamp],
            )
        )
    else:
        nodes = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?) ORDER BY created_at",
                [timestamp, timestamp],
            )
        )

    # ── Edges ──────────────────────────────────────────────────────────
    if edge_id:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE id = ? AND created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?)",
                [edge_id, timestamp, timestamp],
            )
        )
    elif node_id:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?) ORDER BY created_at",
                [node_id, node_id, timestamp, timestamp],
            )
        )
    else:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?) ORDER BY created_at",
                [timestamp, timestamp],
            )
        )

    # ── Observations ───────────────────────────────────────────────────
    if node_id:
        observations = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_observations WHERE node_id = ? AND created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?) ORDER BY created_at",
                [node_id, timestamp, timestamp],
            )
        )
    else:
        observations = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_observations WHERE created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?) ORDER BY created_at",
                [timestamp, timestamp],
            )
        )

    # ── Summary ────────────────────────────────────────────────────────
    summary = {
        "nodes": len(nodes),
        "edges": len(edges),
        "observations": len(observations),
    }

    return {
        "timestamp": timestamp,
        "nodes": nodes,
        "edges": edges,
        "observations": observations,
        "summary": summary,
    }


def query_confidence_chain(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    max_depth: int = 5,
) -> dict[str, Any]:
    """Trace all incoming evidence edges to compute aggregate confidence.

    Walks incoming L2/L3 edges (CAUSES, SUPPORTS, DERIVES_FROM, REFERENCES,
    PREDICTS, CORRELATES_WITH, EXPLAINS) recursively to build an evidence
    tree. Computes aggregate confidence using inverse-variance weighting
    of all evidence paths.

    This is a universal substrate method — works for any domain:
    cattle health, constitutional law, industrial systems, etc.

    Args:
        conn: Database connection.
        node_id: The node to trace evidence for.
        max_depth: Maximum chain depth (default 5).

    Returns:
        Dict with evidence_chain (list of edges), aggregate_confidence,
        evidence_count, and chain_depth.
    """
    from ohm.validation import validate_depth, validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth, max_depth=20)

    evidence_types = (
        "CAUSES",
        "SUPPORTS",
        "DERIVES_FROM",
        "REFERENCES",
        "PREDICTS",
        "CORRELATES_WITH",
        "EXPLAINS",
    )
    placeholders = ",".join(["?"] * len(evidence_types))

    query = f"""
        WITH RECURSIVE evidence_chain AS (
            SELECT
                e.id,
                e.from_node,
                e.to_node,
                e.edge_type,
                e.layer,
                e.confidence,
                e.created_by,
                e.condition,
                1 AS depth,
                ARRAY[e.id] AS path
            FROM ohm_edges e
            WHERE e.to_node = ?
              AND e.edge_type IN ({placeholders})

            UNION ALL

            SELECT
                e.id,
                e.from_node,
                e.to_node,
                e.edge_type,
                e.layer,
                e.confidence,
                e.created_by,
                e.condition,
                c.depth + 1,
                array_append(c.path, e.id)
            FROM ohm_edges e
            JOIN evidence_chain c ON e.to_node = c.from_node
            WHERE c.depth < ?
              AND e.edge_type IN ({placeholders})
              AND e.id NOT IN (SELECT unnest(c.path))
        )
        SELECT DISTINCT
            id, from_node, to_node, edge_type, layer,
            confidence, created_by, condition, depth
        FROM evidence_chain
        ORDER BY depth, confidence DESC
    """
    params = [node_id, *evidence_types, max_depth, *evidence_types]
    result = conn.execute(query, params)
    edges = _rows_to_dicts(result)

    # Compute aggregate confidence using inverse-variance of all evidence
    if not edges:
        return {
            "node_id": node_id,
            "evidence_chain": [],
            "aggregate_confidence": None,
            "evidence_count": 0,
            "max_depth": 0,
        }

    # Weighted aggregate: deeper edges contribute less (decay factor 0.8^depth)
    total_weight = 0.0
    weighted_sum = 0.0
    for edge in edges:
        depth = edge.get("depth", 1)
        conf = edge.get("confidence", 0.5) or 0.5
        weight = 0.8 ** (depth - 1)
        weighted_sum += conf * weight
        total_weight += weight

    aggregate = round(weighted_sum / total_weight, 4) if total_weight > 0 else None

    return {
        "node_id": node_id,
        "evidence_chain": edges,
        "aggregate_confidence": aggregate,
        "evidence_count": len(edges),
        "max_depth": max(e.get("depth", 0) for e in edges) if edges else 0,
    }


# ── Batch Expiry ────────────────────────────────────────────────────────────


def query_find_expiring_batches(
    conn: DuckDBPyConnection,
    *,
    product_type: str | None = None,
    days: int = 5,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Find batches expiring within a given number of days.

    Uses BATCH_EXPIRES_BEFORE edges where the edge metadata contains an
    expires_at ISO timestamp. Returns batches sorted by expiry (soonest first).

    Args:
        conn: Database connection.
        product_type: Optional filter by source node type (e.g., 'dairy', 'produce').
        days: Look-ahead window in days (default 5).
        limit: Maximum results to return.

    Returns:
        List of dicts with batch_id, product_type, expires_at, days_until_expiry,
        from_node, to_node, and edge metadata.
    """
    from datetime import datetime, timezone

    conditions = ["e.edge_type = 'BATCH_EXPIRES_BEFORE'"]
    params: list = []

    if product_type:
        from ohm.validation import validate_identifier

        product_type = validate_identifier(product_type, name="product_type")
        conditions.append("n_from.type = ?")
        params.append(product_type)

    where_clause = "WHERE " + " AND ".join(conditions)

    query = f"""
        SELECT
            e.id AS edge_id,
            e.from_node,
            e.to_node,
            n_from.label AS batch_label,
            n_from.type AS product_type,
            n_to.label AS location_label,
            e.metadata,
            e.confidence,
            e.created_at
        FROM ohm_edges e
        JOIN ohm_nodes n_from ON e.from_node = n_from.id
        JOIN ohm_nodes n_to ON e.to_node = n_to.id
        {where_clause}
        ORDER BY e.created_at DESC
        LIMIT ?
    """
    params.append(limit)

    result = conn.execute(query, params)
    rows = _rows_to_dicts(result)

    now = datetime.now(timezone.utc)
    output: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        expires_at_str = metadata.get("expires_at") if isinstance(metadata, dict) else None

        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                days_until = (expires_at - now).total_seconds() / 86400.0
            except (ValueError, TypeError):
                days_until = None
        else:
            days_until = None

        # Filter by days window
        if days_until is not None and days_until > days:
            continue

        output.append(
            {
                "edge_id": row["edge_id"],
                "from_node": row["from_node"],
                "to_node": row["to_node"],
                "batch_label": row["batch_label"],
                "product_type": row["product_type"],
                "location_label": row["location_label"],
                "expires_at": expires_at_str,
                "days_until_expiry": round(days_until, 1) if days_until is not None else None,
                "confidence": row["confidence"],
                "metadata": metadata,
            }
        )

    # Sort by days_until_expiry ascending (soonest first)
    output.sort(key=lambda x: x["days_until_expiry"] if x["days_until_expiry"] is not None else float("inf"))

    return output[:limit]


# ── Cascade Scenario (Supply Chain / Risk Modeling) ─────────────────────────


def query_deterministic_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Deterministic cascade through downstream graph from a node.

    Starting from *node_id* with *failure_probability*, walks downstream
    through CAUSES, EXPECTED_LIKELIHOOD, DEPENDS_ON, and THREATENS edges.
    Each downstream node's failure probability is computed as:

        P_downstream = P_upstream × edge.probability (or edge.confidence if no probability)

    Returns all downstream nodes with computed failure probabilities and
    the path chain that leads to each. This is a deterministic computation,
    not a Monte Carlo simulation — for probabilistic analysis with variance
    use `monte_carlo_cascade()`.

    Args:
        conn: Database connection.
        node_id: Starting node (e.g., supplier that might fail).
        failure_probability: Probability that the starting node fails (0.0-1.0).
        max_depth: Maximum traversal depth.

    Returns:
        List of dicts with node_id, label, failure_probability, depth, and path.
    """
    from ohm.validation import validate_confidence, validate_depth, validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    failure_probability = validate_confidence(failure_probability)
    max_depth = validate_depth(max_depth)

    # Use a recursive CTE to walk downstream
    query = """
        WITH RECURSIVE cascade AS (
            -- Anchor: the starting node
            SELECT
                ? AS node_id,
                CAST(? AS FLOAT) AS failure_probability,
                0 AS depth,
                list_value(?) AS path
            UNION ALL
            -- Recursive: follow downstream edges
            SELECT
                e.to_node AS node_id,
                CAST(
                    c.failure_probability *
                    COALESCE(e.probability, e.confidence, 0.5)
                    AS FLOAT
                ) AS failure_probability,
                c.depth + 1 AS depth,
                list_concat(c.path, list_value(e.to_node)) AS path
            FROM cascade c
            JOIN ohm_edges e ON e.from_node = c.node_id
            WHERE c.depth < ?
              AND e.edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
              AND NOT list_contains(c.path, e.to_node)
        )
        SELECT DISTINCT
            c.node_id,
            n.label AS node_label,
            n.type AS node_type,
            c.failure_probability,
            c.depth,
            c.path
        FROM cascade c
        JOIN ohm_nodes n ON c.node_id = n.id
        WHERE c.depth > 0
        ORDER BY c.depth, c.failure_probability DESC
    """
    result = conn.execute(query, [node_id, failure_probability, node_id, max_depth])
    return _rows_to_dicts(result)


# Backward-compatible alias (deprecated)
def query_cascade_scenario(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """[DEPRECATED] Use query_deterministic_cascade() instead.

    This function was renamed to clarify that it performs deterministic
    cascade propagation, not Monte Carlo simulation.
    """
    import warnings

    warnings.warn(
        "query_cascade_scenario is deprecated, use query_deterministic_cascade instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return query_deterministic_cascade(conn, node_id, failure_probability=failure_probability, max_depth=max_depth)


def monte_carlo_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    trials: int = 1000,
    max_depth: int = 10,
    seed: int | None = None,
    default_probability: float = 0.5,
) -> dict[str, Any]:
    """Monte Carlo simulation of cascade through downstream graph.

    Runs *trials* number of cascade trials with two-stage sampling per ADR-008:
    - Stage 1: Edge existence — sample random() < confidence
    - Stage 2: Effect propagation — sample random() < probability

    Returns distribution statistics (p5, p50, p95, mean) for each downstream
    node rather than a single point estimate.

    For a deterministic analysis use `query_deterministic_cascade()`.

    Args:
        conn: Database connection.
        node_id: Starting node for cascade simulation.
        trials: Number of Monte Carlo trials to run (default 1000).
        max_depth: Maximum traversal depth per trial.
        seed: Random seed for reproducibility. If None, results vary each run.
        default_probability: Default probability when edge has none set (default 0.5).

    Returns:
        Dict with:
        - node_id: the starting node
        - results: list of per-node statistics {node_id, p5, p50, p95, mean, activated_count, trials}
        - trials: number of trials run
        - seed: random seed used (None if no seed)
    """
    import random
    from ohm.validation import validate_identifier, validate_depth

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth)

    if seed is not None:
        random.seed(seed)

    # Get downstream edges - traverse from node_id following outgoing edges
    # and collect all edges encountered during traversal
    edges_query = """
        WITH RECURSIVE traverse AS (
            SELECT
                ? AS start_node,
                ? AS node_id,
                0 AS depth,
                list_value(?) AS path
            UNION ALL
            SELECT
                t.start_node,
                e.to_node AS node_id,
                t.depth + 1 AS depth,
                list_concat(t.path, list_value(e.to_node)) AS path
            FROM traverse t
            JOIN ohm_edges e ON e.from_node = t.node_id
            WHERE t.depth < ?
              AND e.edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
              AND e.deleted_at IS NULL
              AND NOT list_contains(t.path, e.to_node)
        )
        SELECT
            t.node_id AS from_node,
            e.to_node AS to_node,
            e.edge_type,
            e.probability,
            e.confidence
        FROM traverse t
        JOIN ohm_edges e ON e.from_node = t.node_id
        WHERE t.depth >= 0
          AND e.deleted_at IS NULL
        ORDER BY t.depth, t.node_id
    """
    edges_result = conn.execute(edges_query, [node_id, node_id, node_id, max_depth])
    edges = _rows_to_dicts(edges_result)

    # Build adjacency for simulation: from_node -> list of {to_node, confidence, probability}
    # ADR-008: two-stage sampling — confidence (existence) then probability (propagation)
    node_edges: dict[str, list[dict[str, Any]]] = {}
    all_nodes = {node_id}
    for edge in edges:
        from_node = edge["from_node"]
        to_node = edge["to_node"]
        if from_node not in node_edges:
            node_edges[from_node] = []
        effective_prob = float(edge["probability"]) if edge["probability"] is not None else default_probability
        node_edges[from_node].append(
            {
                "to_node": to_node,
                "confidence": float(edge["confidence"]) if edge["confidence"] is not None else 0.7,
                "probability": effective_prob,
            }
        )
        all_nodes.add(from_node)
        all_nodes.add(to_node)

    # Run trials with two-stage sampling
    activated_counts: dict[str, int] = {n: 0 for n in all_nodes}

    for _ in range(trials):
        # Track which nodes are activated in this trial
        visited = set()
        frontier = [node_id]

        for _ in range(max_depth):
            next_frontier = []
            for current in frontier:
                if current in visited:
                    continue
                visited.add(current)

                # Count this node as activated
                if current in activated_counts:
                    activated_counts[current] += 1

                # Two-stage sampling at each edge
                if current in node_edges:
                    for edge in node_edges[current]:
                        target = edge["to_node"]
                        if target in visited:
                            continue
                        # Stage 1: Does this edge exist? (confidence)
                        if random.random() < edge["confidence"]:
                            # Stage 2: Does the effect propagate? (probability)
                            if random.random() < edge["probability"]:
                                next_frontier.append(target)
            frontier = next_frontier
            if not frontier:
                break

    # Compute distribution statistics
    results = []
    for nid in sorted(all_nodes):
        count = activated_counts[nid]
        activated_pct = count / trials
        results.append(
            {
                "node_id": nid,
                "activated_count": count,
                "activated_pct": round(activated_pct, 4),
                "p5": round(_percentile(count, trials, 0.05), 4),
                "p50": round(_percentile(count, trials, 0.50), 4),
                "p95": round(_percentile(count, trials, 0.95), 4),
                "mean": round(activated_pct, 4),
            }
        )

    return {
        "node_id": node_id,  # starting node (not last in loop)
        "results": results,
        "trials": trials,
        "seed": seed,
    }


def query_what_if(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Dry-run: what happens downstream if this edge's event occurs?

    Treats the edge's to_node as the failure origin with probability
    equal to the edge's probability (or confidence). Returns the cascade
    analysis without modifying the graph.

    Args:
        conn: Database connection.
        edge_id: The edge whose event we're simulating.
        max_depth: Maximum traversal depth.

    Returns:
        Dict with edge details and downstream cascade results.
    """
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")

    # Get the edge details
    edge = conn.execute(
        """SELECT id, from_node, to_node, edge_type, layer,
                  confidence, probability, condition, created_by
           FROM ohm_edges WHERE id = ?""",
        [edge_id],
    ).fetchone()
    if edge is None:
        raise ValueError(f"Edge not found: {edge_id}")

    columns = ["id", "from_node", "to_node", "edge_type", "layer", "confidence", "probability", "condition", "created_by"]
    edge_dict = dict(zip(columns, edge))

    # Use edge's probability (or confidence) as the failure probability
    trigger_prob = edge_dict.get("probability") or edge_dict.get("confidence") or 1.0

    cascade = query_deterministic_cascade(
        conn,
        edge_dict["to_node"],
        failure_probability=float(trigger_prob),
        max_depth=max_depth,
    )

    return {
        "trigger_edge": edge_dict,
        "trigger_probability": trigger_prob,
        "downstream_impact": cascade,
        "affected_nodes": len(cascade),
    }


# ── Customer Support: Handoff, Escalation, Provenance ───────────────────────

HANDOFF_EDGE_TYPES = frozenset(
    {
        "TRANSFERRED_TO",
        "ESCALATED_TO",
        "DELEGATED_TO",
    }
)

STATE_MACHINE_EDGE_TYPES = frozenset(
    {
        "OPENED_BY",
        "STARTED_BY",
        "AWAITING",
        "RESOLVED_BY",
        "CLOSED_BY",
    }
)


def query_handoff(
    conn: DuckDBPyConnection,
    *,
    from_agent: str,
    to_agent: str,
    ticket_node: str,
    reason: str,
    edge_type: str = "TRANSFERRED_TO",
    confidence: float = 0.8,
    created_by: str = "unknown",
) -> dict[str, Any]:
    """Create a handoff edge between agents for a ticket node.

    Creates a TRANSFERRED_TO (default), ESCALATED_TO, or DELEGATED_TO edge
    from the from_agent node to the to_agent node, and returns the full
    handoff chain for the ticket.

    Args:
        conn: Database connection.
        from_agent: Agent node ID transferring from.
        to_agent: Agent node ID transferring to.
        ticket_node: The ticket/case node being handed off.
        reason: Reason for the handoff.
        edge_type: One of TRANSFERRED_TO, ESCALATED_TO, DELEGATED_TO.
        confidence: Confidence for the edge (default 0.8).
        created_by: Actor creating the handoff.

    Returns:
        Dict with the created edge and the full handoff chain.
    """
    from ohm.validation import validate_identifier

    from_agent = validate_identifier(from_agent, name="from_agent")
    to_agent = validate_identifier(to_agent, name="to_agent")
    ticket_node = validate_identifier(ticket_node, name="ticket_node")

    if edge_type not in HANDOFF_EDGE_TYPES:
        raise ValueError(f"Invalid handoff edge_type '{edge_type}'. Must be one of: {sorted(HANDOFF_EDGE_TYPES)}")

    # Determine layer based on edge type
    layer = "L2" if edge_type == "TRANSFERRED_TO" else "L3"

    # Create the handoff edge
    edge = create_edge(
        conn,
        from_node=from_agent,
        to_node=to_agent,
        edge_type=edge_type,
        layer=layer,
        confidence=confidence,
        condition=reason,
        created_by=created_by,
    )

    # Get the full handoff chain for this ticket
    chain = _query_handoff_chain(conn, ticket_node)

    return {
        "edge": edge,
        "handoff_chain": chain,
    }


def _query_handoff_chain(
    conn: DuckDBPyConnection,
    ticket_node: str,
) -> list[dict[str, Any]]:
    """Get the full handoff chain for a ticket node.

    Finds all TRANSFERRED_TO, ESCALATED_TO, and DELEGATED_TO edges
    involving agents connected to this ticket, ordered by creation time.

    Args:
        conn: Database connection.
        ticket_node: The ticket/case node.

    Returns:
        List of handoff records with edge details.
    """
    # Find all handoff edges where from_node or to_node is an agent
    # connected to the ticket via any edge
    query = """
        SELECT e.id, e.from_node, e.to_node, e.edge_type,
               e.confidence, e.condition AS reason,
               e.created_at, e.created_by,
               nf.label AS from_label,
               nt.label AS to_label
        FROM ohm_edges e
        LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
        LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
        WHERE e.edge_type IN ('TRANSFERRED_TO', 'ESCALATED_TO', 'DELEGATED_TO')
          AND (e.from_node IN (
                  SELECT from_node FROM ohm_edges
                  WHERE to_node = ? AND edge_type NOT IN ('TRANSFERRED_TO', 'ESCALATED_TO', 'DELEGATED_TO')
              UNION
                  SELECT to_node FROM ohm_edges
                  WHERE from_node = ? AND edge_type NOT IN ('TRANSFERRED_TO', 'ESCALATED_TO', 'DELEGATED_TO')
              UNION
                  SELECT ?)
            OR e.to_node IN (
                  SELECT from_node FROM ohm_edges
                  WHERE to_node = ? AND edge_type NOT IN ('TRANSFERRED_TO', 'ESCALATED_TO', 'DELEGATED_TO')
              UNION
                  SELECT to_node FROM ohm_edges
                  WHERE from_node = ? AND edge_type NOT IN ('TRANSFERRED_TO', 'ESCALATED_TO', 'DELEGATED_TO')
              UNION
                  SELECT ?))
        ORDER BY e.created_at ASC
    """
    result = conn.execute(query, [ticket_node, ticket_node, ticket_node, ticket_node, ticket_node, ticket_node])
    return _rows_to_dicts(result)


def query_escalate(
    conn: DuckDBPyConnection,
    *,
    ticket_node: str,
    to_tier: str,
    reason: str,
    from_agent: str | None = None,
    confidence: float = 0.9,
    created_by: str = "unknown",
) -> dict[str, Any]:
    """Escalate a ticket to a higher tier with urgency.

    Creates an ESCALATED_TO edge and sets the ticket's urgency to 'high'.
    Returns the escalation edge and the updated ticket.

    Args:
        conn: Database connection.
        ticket_node: The ticket/case node being escalated.
        to_tier: Agent node ID or tier identifier to escalate to.
        reason: Reason for the escalation.
        from_agent: Agent node ID escalating from (optional).
        confidence: Confidence for the edge (default 0.9).
        created_by: Actor creating the escalation.

    Returns:
        Dict with the created edge and updated ticket info.
    """
    from ohm.validation import validate_identifier

    ticket_node = validate_identifier(ticket_node, name="ticket_node")
    to_tier = validate_identifier(to_tier, name="to_tier")

    # Create the ESCALATED_TO edge
    if from_agent:
        from_agent = validate_identifier(from_agent, name="from_agent")
        edge_from = from_agent
    else:
        edge_from = ticket_node

    edge = create_edge(
        conn,
        from_node=edge_from,
        to_node=to_tier,
        edge_type="ESCALATED_TO",
        layer="L3",
        confidence=confidence,
        condition=reason,
        created_by=created_by,
    )

    # Set ticket urgency to 'high'
    try:
        conn.execute(
            "UPDATE ohm_nodes SET urgency = 'high', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [ticket_node],
        )
    except Exception:
        # Column might not exist yet (pre-0.6.0 schema)
        conn.execute(
            "UPDATE ohm_nodes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [ticket_node],
        )

    # Get updated ticket
    try:
        ticket = conn.execute(
            "SELECT id, label, type, urgency, priority FROM ohm_nodes WHERE id = ?",
            [ticket_node],
        ).fetchone()
        ticket_info = None
        if ticket:
            ticket_info = {
                "id": ticket[0],
                "label": ticket[1],
                "type": ticket[2],
                "urgency": ticket[3],
                "priority": ticket[4],
            }
    except Exception:
        # Pre-0.6.0 schema without urgency/priority columns
        ticket = conn.execute(
            "SELECT id, label, type FROM ohm_nodes WHERE id = ?",
            [ticket_node],
        ).fetchone()
        ticket_info = None
        if ticket:
            ticket_info = {
                "id": ticket[0],
                "label": ticket[1],
                "type": ticket[2],
                "urgency": "high",  # We just set it
                "priority": None,
            }

    return {
        "edge": edge,
        "ticket": ticket_info,
    }


def query_ticket_provenance(
    conn: DuckDBPyConnection,
    ticket_node: str,
    *,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Show the complete handoff and state history for a ticket.

    Follows TRANSFERRED_TO, ESCALATED_TO, DELEGATED_TO edges and
    state machine edges (OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY,
    CLOSED_BY) to reconstruct the full provenance chain.

    Args:
        conn: Database connection.
        ticket_node: The ticket/case node.
        max_depth: Maximum traversal depth.

    Returns:
        List of provenance records ordered chronologically.
    """
    from ohm.validation import validate_identifier, validate_depth

    ticket_node = validate_identifier(ticket_node, name="ticket_node")
    max_depth = validate_depth(max_depth)

    # Find all handoff and state machine edges connected to this ticket
    all_types = HANDOFF_EDGE_TYPES | STATE_MACHINE_EDGE_TYPES
    type_list = ", ".join(f"'{t}'" for t in sorted(all_types))

    query = f"""
        SELECT e.id, e.from_node, e.to_node, e.edge_type,
               e.confidence, e.condition AS reason,
               e.layer, e.created_at, e.created_by,
               nf.label AS from_label, nf.type AS from_type,
               nt.label AS to_label, nt.type AS to_type
        FROM ohm_edges e
        LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
        LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
        WHERE e.edge_type IN ({type_list})
          AND (e.from_node = ? OR e.to_node = ?
               OR e.from_node IN (
                   SELECT id FROM ohm_nodes
                   WHERE type = 'agent'
               ))
        ORDER BY e.created_at ASC
    """
    result = conn.execute(query, [ticket_node, ticket_node])
    return _rows_to_dicts(result)


# ── Semantic Search ─────────────────────────────────────────────────────────


def generate_embedding(
    text: str,
    model: str = "nomic-embed-text",
    ollama_url: str = "http://localhost:11434",
    timeout: float | None = None,
) -> list[float] | None:
    """Generate an embedding vector using Ollama.

    Calls the Ollama API to generate an embedding for the given text.
    Returns None if Ollama is unavailable or the request fails.

    Note: For pluggable embedding backends (OHM-9zk7), use ohm.graph.embeddings directly.
    This function is kept for backward compatibility.

    Args:
        text: Text to embed.
        model: Ollama model name (default: nomic-embed-text, 768 dimensions).
        ollama_url: Ollama API base URL.
        timeout: Optional request timeout in seconds. Uses the backend default
            when None.

    Returns:
        List of floats (embedding vector) or None on failure.
    """
    if not text or not text.strip():
        return None

    # Test/CI guard: skip slow Ollama network attempts when embeddings are not needed.
    import os

    if os.environ.get("OHM_DISABLE_EMBEDDINGS") == "1":
        return None

    from ohm.graph.embeddings import OllamaBackend

    backend = OllamaBackend(model=model, ollama_url=ollama_url)
    embeddings = backend.embed([text], timeout=timeout)
    if embeddings and any(e != 0.0 for e in embeddings[0]):
        return embeddings[0]
    return None


def semantic_search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 10,
    node_type: str | None = None,
    min_confidence: float | None = None,
    include_l0: bool = False,
    membership_weight: float | None = None,
    hd_dim: int = 10000,
    hd_seed: int = 42,
    embedding_timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Search nodes by semantic similarity using embedding vectors.

    Generates an embedding for the query text, then finds the most
    similar nodes using cosine distance on the embedding column.

    Requires:
    - Ollama running locally with an embedding model loaded
    - VSS extension loaded for HNSW index acceleration
    - embedding column on ohm_nodes (migration 0.11.0)

    Args:
        conn: Database connection.
        query: Natural language search query.
        limit: Maximum number of results (default 10).
        node_type: Optional filter by node type.
        min_confidence: Optional minimum confidence threshold.
        include_l0: Include fragment-type nodes (default False, OHM-a5rz.20).
        membership_weight: Optional blend weight in [0, 1] for HD Hamming
            similarity alongside cosine similarity (OHM-xuf4). When None
            (default), pure cosine ranking is returned unchanged. When
            provided, each result also carries ``hd_similarity`` and a
            ``blended_score`` = (1 - w) * cosine_sim + w * hd_sim, and
            results are re-ranked by blended_score descending.
        hd_dim: HD fingerprint dimension (default 10000).
        hd_seed: HD fingerprint seed (default 42).
        embedding_timeout: Optional timeout for the Ollama embedding call.
            When None, uses the backend default. Useful for time-budgeted
            callers such as post-write suggestions.

    Returns:
        List of dicts with node_id, label, type, distance, and confidence.
        When ``membership_weight`` is set, each dict also carries
        ``cosine_similarity``, ``hd_similarity`` (None if node has no
        stored fingerprint), and ``blended_score``.
    """
    if not query or not query.strip():
        return []

    embedding = generate_embedding(query, timeout=embedding_timeout)
    if embedding is None:
        raise ValueError("Ollama is not available. Start Ollama with an embedding model (e.g., 'ollama pull nomic-embed-text') to use semantic search.")

    # Build query with optional filters
    where_clauses = ["embedding IS NOT NULL"]
    params: list[Any] = []

    if node_type is not None:
        where_clauses.append("type = ?")
        params.append(node_type)
    elif not include_l0:
        # OHM-a5rz.20: exclude L0 fragments from default semantic search
        where_clauses.append("type != 'fragment'")

    if min_confidence is not None:
        where_clauses.append("confidence >= ?")
        params.append(min_confidence)

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT
            id AS node_id,
            label,
            type,
            confidence,
            array_cosine_distance(embedding, ?::FLOAT[768]) AS distance
        FROM ohm_nodes
        WHERE {where_sql}
        ORDER BY distance ASC
        LIMIT ?
    """
    params.append(embedding)
    params.append(limit)

    result = conn.execute(sql, params)
    rows = _rows_to_dicts(result)

    if not rows:
        return rows

    if membership_weight is not None and not 0.0 <= membership_weight <= 1.0:
        raise ValueError(f"membership_weight must be in [0, 1], got {membership_weight}")

    # OHM-nnrw: Compute manifold_density_score for each result.
    # k-NN density = 1 - (mean cosine distance to k nearest neighbors).
    # Computed on-read, no cached column.
    k_density = 5
    node_ids = [r["node_id"] for r in rows]
    placeholders = ",".join(["?"] * len(node_ids))
    embed_rows = conn.execute(
        f"""SELECT id, embedding FROM ohm_nodes
            WHERE id IN ({placeholders}) AND embedding IS NOT NULL""",
        node_ids,
    ).fetchall()
    embed_map: dict[str, list] = {}
    for nid, emb in embed_rows:
        if emb is not None:
            embed_map[nid] = list(emb) if not isinstance(emb, list) else emb

    for r in rows:
        r["geodesic_distance"] = r.get("distance")
        nid = r["node_id"]
        if nid in embed_map:
            emb = embed_map[nid]
            try:
                mean_dist_row = conn.execute(
                    """SELECT AVG(d) FROM (
                        SELECT array_cosine_distance(embedding, ?::FLOAT[768]) AS d
                        FROM ohm_nodes
                        WHERE embedding IS NOT NULL AND id != ?
                        ORDER BY d ASC LIMIT ?
                    )""",
                    [emb, nid, k_density],
                ).fetchone()
                mean_dist = mean_dist_row[0] if mean_dist_row and mean_dist_row[0] is not None else 1.0
                r["manifold_density_score"] = round(max(0.0, 1.0 - float(mean_dist)), 6)
            except Exception:
                r["manifold_density_score"] = None
        else:
            r["manifold_density_score"] = None

    if membership_weight is None:
        return rows

    from ohm.inference.hd import fingerprint_text, hamming_similarity

    query_fp = fingerprint_text(query, dim=hd_dim, seed=hd_seed)
    query_bytes = bytes(query_fp)

    node_ids = [r["node_id"] for r in rows]
    if not node_ids:
        return rows

    placeholders = ",".join(["?"] * len(node_ids))
    fp_rows = conn.execute(
        f"""SELECT id, hd_fingerprint
            FROM ohm_nodes
            WHERE id IN ({placeholders}) AND hd_fingerprint IS NOT NULL""",
        node_ids,
    ).fetchall()

    fp_map: dict[str, bytes] = {}
    expected_len = (hd_dim + 7) // 8
    for nid, fp_blob in fp_rows:
        if fp_blob is None:
            continue
        candidate = bytes(fp_blob) if isinstance(fp_blob, (bytes, bytearray)) else bytes(fp_blob)
        if len(candidate) != expected_len:
            continue
        fp_map[nid] = candidate

    for r in rows:
        distance = r.get("distance")
        cosine_sim = 1.0 - float(distance) if distance is not None else 0.0
        r["cosine_similarity"] = round(cosine_sim, 6)
        nid = r["node_id"]
        if nid in fp_map:
            hd_sim = hamming_similarity(bytearray(query_bytes), bytearray(fp_map[nid]))
            r["hd_similarity"] = round(hd_sim, 6)
            blended = (1.0 - membership_weight) * cosine_sim + membership_weight * hd_sim
        else:
            r["hd_similarity"] = None
            blended = (1.0 - membership_weight) * cosine_sim
        r["blended_score"] = round(blended, 6)

    rows.sort(key=lambda x: x["blended_score"], reverse=True)
    return rows


def search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 20,
    node_type: str | None = None,
    created_by: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_l0: bool = False,
) -> list[dict[str, Any]]:
    """Text search over nodes using ILIKE matching (OHM-a5rz.18).

    Performs case-insensitive ILIKE search on both label and content.
    L0 fragments are excluded by default (matching stats/neighborhood
    behavior per ADR-019). Pass include_l0=True to include them.

    Args:
        conn: Database connection.
        query: Text to search for in labels and content.
        limit: Maximum results (default 20).
        node_type: Optional filter by node type (overrides include_l0).
        created_by: Optional filter by creator.
        since: Optional ISO 8601 lower bound on created_at.
        until: Optional ISO 8601 upper bound on created_at.
        include_l0: Include fragment-type nodes (default False).

    Returns:
        List of matching node records.
    """
    if not query or not query.strip():
        return []

    conditions: list[str] = ["deleted_at IS NULL", "(label ILIKE ? OR content ILIKE ?)"]
    params: list[Any] = [f"%{query}%", f"%{query}%"]

    if node_type:
        conditions.append("type = ?")
        params.append(node_type)
    elif not include_l0:
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

    params.append(limit)
    sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?"
    result = conn.execute(sql, params)
    return _rows_to_dicts(result)


def fuzzy_search(
    conn: "DuckDBPyConnection",
    query: str,
    limit: int = 20,
    threshold: float = 0.6,
    include_l0: bool = False,
) -> list[dict[str, Any]]:
    """Fuzzy text search using DuckDB's jaro_winkler_similarity (OHM-tr71.9).

    Fallback when ILIKE + semantic search both return 0 results.
    Uses Jaro-Winkler similarity on labels against the query.
    Returns matches with similarity score and match_type='fuzzy'.

    Args:
        conn: Database connection.
        query: Query text to fuzzy-match against labels.
        limit: Maximum results (default 20).
        threshold: Minimum similarity (0-1) to consider a match (default 0.6).
        include_l0: Include fragment-type nodes (default False).

    Returns:
        List of dicts with node fields plus distance and match_type.
    """
    if not query or not query.strip():
        return []

    type_filter = ""
    if not include_l0:
        type_filter = "AND type != 'fragment'"

    params: list[Any] = [query, query, threshold, limit]
    sql = f"""
        SELECT *, jaro_winkler_similarity(LOWER(label), LOWER(?)) AS distance
        FROM ohm_nodes
        WHERE deleted_at IS NULL
          AND jaro_winkler_similarity(LOWER(label), LOWER(?)) >= ?
          {type_filter}
        ORDER BY distance DESC
        LIMIT ?
    """
    try:
        result = conn.execute(sql, params)
        rows = _rows_to_dicts(result)
        for r in rows:
            r["match_type"] = "fuzzy"
            r["distance"] = round(float(r.get("distance", 0)), 4)
        return rows
    except Exception:
        # DuckDB may not have this function on older versions — degrade gracefully
        import logging

        logging.getLogger(__name__).debug("fuzzy_search: jaro_winkler_similarity unavailable, returning empty")
        return []


def update_node_embedding(
    conn: "DuckDBPyConnection",
    node_id: str,
    text: str | None = None,
    ollama_url: str | None = None,
) -> bool:
    """Generate and store an embedding for a node.

    Generates an embedding from the node's label (or custom text)
    and updates the embedding column. Returns False if Ollama is
    unavailable or the node doesn't exist.

    Args:
        conn: Database connection.
        node_id: ID of the node to update.
        text: Optional custom text to embed. Defaults to node label.
        ollama_url: Optional Ollama URL for parallel embedding workers.
            Defaults to localhost. Use for distributed embedding generation
            across multiple GPU nodes.

    Returns:
        True if embedding was updated, False otherwise.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Enrich embedding text: label + content + tags
    # Short labels like "Artificial Scarcity" produce shallow embeddings.
    # Concatenating label, content, and tags gives nomic-embed-text richer
    # semantic material to work with. (ADR-021, Socrates Round 4 feedback)
    if text is None:
        result = conn.execute(
            "SELECT label, content, tags FROM ohm_nodes WHERE id = ?",
            [node_id],
        ).fetchone()
        if result is None:
            return False
        label, content, tags_json = result
        parts = []
        if label:
            parts.append(label)
        if content:
            parts.append(content)
        if tags_json:
            import json as _json

            try:
                tags = _json.loads(tags_json) if isinstance(tags_json, str) else tags_json
                if isinstance(tags, list) and tags:
                    parts.append(" ".join(str(t) for t in tags))
            except (ValueError, TypeError):
                pass
        text = "\n".join(parts) if parts else label

    if not text:
        return False

    embedding = generate_embedding(text, ollama_url=ollama_url or "http://localhost:11434")
    if embedding is None:
        return False

    conn.execute(
        "UPDATE ohm_nodes SET embedding = ?::FLOAT[768] WHERE id = ?",
        [embedding, node_id],
    )
    return True


def queue_discovery_candidates(
    conn: "DuckDBPyConnection",
    candidate_edges: list[dict[str, Any]],
    *,
    created_by: str = "system",
) -> list[str]:
    """Insert candidate edges from structure learning into the discovery queue.

    Returns list of queue entry IDs.
    """
    from ohm.validation import validate_identifier

    ids = []
    for edge in candidate_edges:
        from_node = validate_identifier(edge["from"], name="from_node")
        to_node = validate_identifier(edge["to"], name="to_node")
        edge_type = edge.get("edge_type", "undirected")
        if edge_type not in ("directed", "undirected"):
            edge_type = "undirected"
        layer = edge.get("layer", "L3")
        confidence = edge.get("confidence")
        provenance = edge.get("provenance", "structure_learning")
        method = edge.get("method", "unknown")

        row_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO ohm_discovery_queue
               (id, from_node, to_node, edge_type, layer, confidence, provenance, method, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [row_id, from_node, to_node, edge_type, layer, confidence, provenance, method, created_by],
        )
        ids.append(row_id)
    return ids


def query_discovery_queue(
    conn: "DuckDBPyConnection",
    *,
    status: str | None = None,
    method: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return pending discovery queue entries for agent review."""
    conditions = ["1=1"]
    params: list[Any] = []

    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if method is not None:
        conditions.append("method = ?")
        params.append(method)

    where = " AND ".join(conditions)
    params.append(limit)

    result = conn.execute(
        f"""SELECT id, from_node, to_node, edge_type, layer, confidence,
                  provenance, method, status, reviewed_by, reviewed_at,
                  review_notes, created_by, created_at
           FROM ohm_discovery_queue
           WHERE {where}
           ORDER BY created_at DESC
           LIMIT ?""",
        params,
    )
    return _rows_to_dicts(result)


def review_discovery_candidate(
    conn: "DuckDBPyConnection",
    queue_id: str,
    *,
    action: str,
    reviewed_by: str,
    review_notes: str | None = None,
    edge_layer: str = "L3",
) -> dict[str, Any]:
    """Accept or reject a discovery queue entry.

    Accept: creates the edge in ohm_edges, marks queue entry as accepted.
    Reject: marks queue entry as rejected with optional notes.
    """
    from ohm.validation import validate_identifier

    queue_id = validate_identifier(queue_id, name="queue_id")

    row = conn.execute(
        "SELECT id, from_node, to_node, edge_type, layer, confidence, provenance, method, status FROM ohm_discovery_queue WHERE id = ?",
        [queue_id],
    ).fetchone()
    if row is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Discovery queue entry {queue_id} not found")

    if row[8] != "pending":
        return {"error": "already_reviewed", "status": row[8], "queue_id": queue_id}

    now_row = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    now = now_row[0] if now_row else None

    if action == "accept":
        from_node = row[1]
        to_node = row[2]
        edge_type = row[3]
        confidence = row[5]
        provenance = row[6]

        ohm_edge_type = edge_type
        if edge_type == "directed":
            ohm_edge_type = "CAUSES"
        elif edge_type == "undirected":
            ohm_edge_type = "CORRELATES_WITH"
        elif edge_type == "SUGGESTED_CAUSES":
            ohm_edge_type = "CAUSES"
        elif edge_type == "SUGGESTED_CORRELATES_WITH":
            ohm_edge_type = "CORRELATES_WITH"
        elif edge_type.startswith("SUGGESTED_"):
            # Strip SUGGESTED_ prefix for any other types
            ohm_edge_type = edge_type[len("SUGGESTED_") :]

        edge_id = create_edge(
            conn,
            from_node=from_node,
            to_node=to_node,
            edge_type=ohm_edge_type,
            layer=edge_layer,
            confidence=confidence if confidence is not None else 0.5,
            provenance=provenance,
            created_by=reviewed_by,
        )

        conn.execute(
            """UPDATE ohm_discovery_queue
               SET status = 'accepted', reviewed_by = ?, reviewed_at = ?, review_notes = ?
               WHERE id = ?""",
            [reviewed_by, now, review_notes, queue_id],
        )

        return {"action": "accepted", "queue_id": queue_id, "edge_id": edge_id}

    elif action == "reject":
        conn.execute(
            """UPDATE ohm_discovery_queue
               SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, review_notes = ?
               WHERE id = ?""",
            [reviewed_by, now, review_notes, queue_id],
        )
        return {"action": "rejected", "queue_id": queue_id}

    else:
        return {"error": "invalid_action", "message": "action must be 'accept' or 'reject'"}


# ── Hook Registry CRUD ──────────────────────────────────────────────────────


def create_hook(
    conn: DuckDBPyConnection,
    *,
    event: str,
    command: str,
    created_by: str,
    timeout_ms: int = 5000,
    enabled: bool = True,
) -> dict[str, Any]:
    """Register a new hook in the ohm_hooks table.

    Args:
        event: One of pre_ingest, post_ingest, pre_query, post_query.
        command: Shell command or python:module.function.
        created_by: Agent registering the hook.
        timeout_ms: Timeout in milliseconds (100–60000).
        enabled: Whether the hook is active.

    Returns:
        The created hook record.
    """
    import uuid

    from ohm.hooks import VALID_HOOK_EVENTS
    from ohm.validation import validate_identifier

    if event not in VALID_HOOK_EVENTS:
        raise ValueError(f"Invalid hook event: {event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")
    if not command or not isinstance(command, str):
        raise ValueError("command must be a non-empty string")
    if not (100 <= timeout_ms <= 60000):
        raise ValueError(f"timeout_ms must be 100–60000, got {timeout_ms}")
    created_by = validate_identifier(created_by, name="created_by")

    hook_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_hooks
           (id, event, command, timeout_ms, enabled, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [hook_id, event, command, timeout_ms, enabled, created_by],
    )
    _log_change(conn, "ohm_hooks", hook_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_hooks WHERE id = ?", [hook_id]))[0]


def query_hooks(
    conn: DuckDBPyConnection,
    *,
    event: str | None = None,
) -> list[dict[str, Any]]:
    """List registered hooks, optionally filtered by event.

    Args:
        event: If provided, filter to this event type.

    Returns:
        List of hook records ordered by created_at.
    """
    from ohm.hooks import VALID_HOOK_EVENTS

    if event is not None and event not in VALID_HOOK_EVENTS:
        raise ValueError(f"Invalid hook event: {event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")

    if event:
        result = conn.execute(
            "SELECT * FROM ohm_hooks WHERE event = ? ORDER BY created_at ASC",
            [event],
        )
    else:
        result = conn.execute(
            "SELECT * FROM ohm_hooks ORDER BY created_at ASC",
        )
    return _rows_to_dicts(result)


def delete_hook(
    conn: DuckDBPyConnection,
    *,
    hook_id: str,
    deleted_by: str,
) -> dict[str, Any]:
    """Delete a hook by ID.

    Args:
        hook_id: The hook to remove.
        deleted_by: Agent performing the deletion.

    Returns:
        Dict with the deleted hook_id.

    Raises:
        ValueError if the hook doesn't exist.
    """
    from ohm.validation import validate_identifier

    hook_id = validate_identifier(hook_id, name="hook_id")
    deleted_by = validate_identifier(deleted_by, name="deleted_by")

    existing = conn.execute("SELECT id FROM ohm_hooks WHERE id = ?", [hook_id]).fetchone()
    if not existing:
        raise ValueError(f"Hook not found: {hook_id}")

    conn.execute("DELETE FROM ohm_hooks WHERE id = ?", [hook_id])
    _log_change(conn, "ohm_hooks", hook_id, "DELETE", deleted_by)
    return {"deleted": hook_id, "type": "hook"}


# ── Alias Resolution & Content Hashing (OHM-g0kv) ────────────────────────


def register_alias(
    conn: DuckDBPyConnection,
    *,
    alias_norm: str,
    node_id: str,
) -> dict[str, Any]:
    """Register a normalized alias for a node.

    Allows multiple alias_norm entries for different node_ids (collision
    detection). Skips if this exact (alias_norm, node_id) pair already exists.

    Args:
        alias_norm: The normalized alias string.
        node_id: The node this alias points to.

    Returns:
        Dict with the alias id and node_id.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    existing = conn.execute(
        "SELECT id FROM ohm_aliases WHERE alias_norm = ? AND node_id = ?",
        [alias_norm, node_id],
    ).fetchone()
    if existing:
        return {"id": existing[0], "alias_norm": alias_norm, "node_id": node_id, "created": False}

    import uuid

    alias_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_aliases (id, alias_norm, node_id) VALUES (?, ?, ?)",
        [alias_id, alias_norm, node_id],
    )
    return {"id": alias_id, "alias_norm": alias_norm, "node_id": node_id, "created": True}


def resolve_alias(
    conn: DuckDBPyConnection,
    *,
    alias_norm: str,
) -> list[dict[str, Any]]:
    """Look up a normalized alias. Returns list of matching alias records."""
    result = conn.execute(
        "SELECT id, alias_norm, node_id, created_at FROM ohm_aliases WHERE alias_norm = ?",
        [alias_norm],
    )
    return _rows_to_dicts(result)


def query_aliases(
    conn: DuckDBPyConnection,
    *,
    node_id: str | None = None,
    prefix: str | None = None,
) -> list[dict[str, Any]]:
    """Query aliases, optionally filtered by node_id or prefix."""
    from ohm.validation import validate_identifier

    conditions = []
    params: list[Any] = []

    if node_id is not None:
        node_id = validate_identifier(node_id, name="node_id")
        conditions.append("node_id = ?")
        params.append(node_id)

    if prefix is not None:
        conditions.append("alias_norm LIKE ?")
        params.append(f"{prefix}%")

    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    result = conn.execute(
        f"SELECT id, alias_norm, node_id, created_at FROM ohm_aliases{where} ORDER BY alias_norm",
        params,
    )
    return _rows_to_dicts(result)


def register_content_hash(
    conn: DuckDBPyConnection,
    *,
    node_id: str,
    content_hash: str,
) -> dict[str, Any]:
    """Register a content hash for a node. Upsert semantics."""
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    existing = conn.execute(
        "SELECT id FROM ohm_content_hashes WHERE node_id = ?",
        [node_id],
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE ohm_content_hashes SET content_hash = ? WHERE node_id = ?",
            [content_hash, node_id],
        )
        return {"id": existing[0], "node_id": node_id, "content_hash": content_hash, "created": False}

    import uuid

    hash_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO ohm_content_hashes (id, node_id, content_hash) VALUES (?, ?, ?)",
        [hash_id, node_id, content_hash],
    )
    return {"id": hash_id, "node_id": node_id, "content_hash": content_hash, "created": True}


def lookup_content_hash(
    conn: DuckDBPyConnection,
    *,
    content_hash: str,
) -> list[dict[str, Any]]:
    """Find nodes with a given content hash (for dedup detection)."""
    result = conn.execute(
        "SELECT id, node_id, content_hash, created_at FROM ohm_content_hashes WHERE content_hash = ?",
        [content_hash],
    )
    return _rows_to_dicts(result)


def resolve_node_by_alias(
    conn: DuckDBPyConnection,
    *,
    query: str,
) -> dict[str, Any] | None:
    """Resolve a query string to a node via alias matching.

    Normalizes the query, checks ohm_aliases, returns the first
    matching node record (or None if no match found).
    """
    from ohm.validation import normalize_alias

    norm = normalize_alias(query)
    if not norm:
        return None

    alias_row = conn.execute(
        "SELECT node_id FROM ohm_aliases WHERE alias_norm = ? LIMIT 1",
        [norm],
    ).fetchone()
    if not alias_row:
        return None

    node_id = alias_row[0]
    node = conn.execute(
        "SELECT id, label, type, confidence, visibility FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node:
        return None

    return {"id": node[0], "label": node[1], "type": node[2], "confidence": node[3], "visibility": node[4]}


def _existing_label(conn: DuckDBPyConnection, node_id: str) -> str:
    """Look up the label of an existing node by id."""
    row = conn.execute("SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]).fetchone()
    return row[0] if row else node_id


def scratch(
    conn: DuckDBPyConnection,
    *,
    content: str,
    created_by: str,
    tags: list[str] | None = None,
    connects_to: list[str] | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    """Write an L0 thinking fragment (OHM-a5rz.4).

    Minimal write: content + agent_name. Auto-generates id, label, type='fragment'.
    Extracts URLs from content. Fragments are exempt from cross-link requirements.
    """
    import re
    from ohm.schema import generate_node_id

    if not content or not content.strip():
        raise ValueError("content must be non-empty")

    label = content.strip()[:80]
    url = None
    url_match = re.search(r"https?://\S+", content)
    if url_match:
        url = url_match.group(0).rstrip(".,;:)")

    generate_node_id(label)

    # Merge caller-provided metadata with auto-detected metadata
    auto_metadata = {}
    is_question = "?" in content
    if tags:
        auto_metadata["tags"] = tags
    if is_question:
        auto_metadata["is_question"] = True
    # Caller metadata takes precedence for overlapping keys
    metadata = {**(metadata or {}), **auto_metadata} if (metadata or auto_metadata) else None

    node = create_node(
        conn,
        label=label,
        node_type="fragment",
        content=content,
        created_by=created_by,
        visibility="team",
        provenance="scratch",
        confidence=0.0,
        url=url,
        connects_to=connects_to,
    )
    if metadata:
        import json as _json

        conn.execute(
            "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
            [_json.dumps(metadata), node["id"]],
        )
        node["metadata"] = _json.dumps(metadata)
    node["scratch"] = True

    # OHM-a5rz.17: Create explicit L0 CONTEXT_OF edges for connects_to targets.
    # create_node() validates the targets exist but doesn't create edges.
    explicit_links = []
    if connects_to:
        for target_id in connects_to:
            edge = create_edge(
                conn,
                from_node=node["id"],
                to_node=target_id,
                layer="L0",
                edge_type="CONTEXT_OF",
                created_by=created_by,
                confidence=0.5,
                provenance="scratch_explicit",
            )
            explicit_links.append(
                {
                    "node_id": target_id,
                    "label": _existing_label(conn, target_id),
                    "edge_id": edge["id"],
                    "edge_type": "CONTEXT_OF",
                    "provenance": "scratch_explicit",
                }
            )
    if explicit_links:
        node["explicit_links"] = explicit_links

    auto_links = _auto_link_fragment(conn, node["id"], content, created_by)
    if auto_links:
        node["auto_links"] = auto_links

    # OHM-a5rz.25: Cross-agent fragment resonance
    resonance_edges = _create_resonance_edges(conn, node["id"], created_by, auto_links)
    if resonance_edges:
        node["resonance_links"] = resonance_edges

    return node


def _auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    content: str,
    created_by: str,
    max_links: int = 5,
) -> list[dict[str, Any]]:
    """Auto-link fragment to existing nodes (OHM-a5rz.8, OHM-a5rz.19).

    Uses semantic embedding similarity when available (OHM-a5rz.19):
    computes fragment embedding, finds top-K nearest nodes by cosine similarity
    above 0.7 threshold, creates L0 CONTEXT_OF edges with provenance
    'auto_link_semantic'.

    Falls back to label-substring matching (OHM-a5rz.8) when:
    - Ollama/embedding service unavailable (generate_embedding returns None)
    - VSS extension not loaded (array_cosine_distance unavailable)

    Skips fragment-type nodes and the fragment itself. Limits to max_links.
    Returns list of created edge records.
    """
    # OHM-a5rz.19: Try semantic auto-linking first
    embedding = generate_embedding(content)
    if embedding is not None:
        try:
            sem_links = _semantic_auto_link_fragment(
                conn,
                fragment_id,
                embedding,
                created_by,
                max_links=min(max_links, 3),  # top 3 per spec
            )
            if sem_links:
                return sem_links
        except Exception:
            pass  # Fall through to substring matching

    # OHM-a5rz.8: Fallback — label-substring matching
    return _substring_auto_link_fragment(conn, fragment_id, content, created_by, max_links)


def _semantic_auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    embedding: list[float],
    created_by: str,
    max_links: int = 3,
) -> list[dict[str, Any]]:
    """Auto-link fragment using semantic embedding similarity (OHM-a5rz.19).

    Finds non-fragment nodes with embeddings closest to the fragment
    embedding using array_cosine_distance. Creates L0 CONTEXT_OF edges
    for matches above the similarity threshold (> 0.7).
    """
    DISTANCE_THRESHOLD = 0.3  # cosine similarity > 0.7 → distance < 0.3

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT id, label, type,
                      array_cosine_distance(embedding, ?::FLOAT[768]) AS distance
               FROM ohm_nodes
               WHERE deleted_at IS NULL
                 AND type != 'fragment'
                 AND embedding IS NOT NULL
                 AND id != ?
                 AND array_cosine_distance(embedding, ?::FLOAT[768]) < ?
               ORDER BY distance ASC
               LIMIT ?""",
            [embedding, fragment_id, embedding, DISTANCE_THRESHOLD, max_links],
        )
    )

    matched = []
    for candidate in candidates:
        edge = create_edge(
            conn,
            from_node=fragment_id,
            to_node=candidate["id"],
            layer="L0",
            edge_type="CONTEXT_OF",
            created_by=created_by,
            confidence=0.3,
            provenance="auto_link_semantic",
        )
        matched.append(
            {
                "node_id": candidate["id"],
                "label": candidate["label"],
                "edge_id": edge["id"],
                "provenance": "auto_link_semantic",
                "similarity": round(1.0 - candidate["distance"], 4),
            }
        )

    return matched


def _substring_auto_link_fragment(
    conn: DuckDBPyConnection,
    fragment_id: str,
    content: str,
    created_by: str,
    max_links: int = 5,
) -> list[dict[str, Any]]:
    """Auto-link fragment to existing nodes whose labels appear in content (OHM-a5rz.8).

    Scans ohm_nodes for labels that are substrings of the fragment content
    (case-insensitive). Creates L0 CONTEXT_OF edges for matches.
    Skips fragment-type nodes and the fragment itself. Limits to max_links.
    """
    content_lower = content.lower()

    candidates = _rows_to_dicts(
        conn.execute(
            "SELECT id, label, type FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment' ORDER BY LENGTH(label) DESC",
        )
    )

    matched = []
    for candidate in candidates:
        if candidate["id"] == fragment_id:
            continue
        if len(matched) >= max_links:
            break
        label_lower = candidate["label"].lower()
        if len(label_lower) >= 4 and label_lower in content_lower:
            edge = create_edge(
                conn,
                from_node=fragment_id,
                to_node=candidate["id"],
                layer="L0",
                edge_type="CONTEXT_OF",
                created_by=created_by,
                confidence=0.3,
                provenance="auto_link_substring",
            )
            matched.append(
                {
                    "node_id": candidate["id"],
                    "label": candidate["label"],
                    "edge_id": edge["id"],
                    "provenance": "auto_link_substring",
                }
            )

    return matched


def _create_resonance_edges(
    conn: DuckDBPyConnection,
    fragment_id: str,
    created_by: str,
    auto_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create RESONANCE edges when fragments from different agents share auto-link targets (OHM-a5rz.25).

    After a fragment auto-links to targets, checks if other fragments from
    different agents also link to the same targets. Creates L0 RESONANCE
    edges between the current fragment and matching fragments.

    Returns list of created resonance edge records.
    """
    if not auto_links:
        return []

    target_ids = [link["node_id"] for link in auto_links]
    placeholders = ",".join(["?"] * len(target_ids))

    rows = _rows_to_dicts(
        conn.execute(
            f"""SELECT e.from_node AS fragment_id, f.created_by AS agent,
                       e.to_node AS shared_target
                FROM ohm_edges e
                JOIN ohm_nodes f ON e.from_node = f.id
                WHERE e.edge_type = 'CONTEXT_OF' AND e.layer = 'L0' AND e.deleted_at IS NULL
                  AND f.type = 'fragment' AND f.deleted_at IS NULL
                  AND f.id != ?
                  AND f.created_by != ?
                  AND e.to_node IN ({placeholders})
            """,
            [fragment_id, created_by] + target_ids,
        )
    )

    if not rows:
        return []

    from collections import defaultdict

    # Group by fragment, collecting shared targets
    fragment_targets: dict[str, dict[str, Any]] = {}
    for row in rows:
        fid = row["fragment_id"]
        if fid not in fragment_targets:
            fragment_targets[fid] = {
                "fragment_id": fid,
                "agent": row["agent"],
                "shared_targets": [],
            }
        fragment_targets[fid]["shared_targets"].append(row["shared_target"])

    resonance_edges = []
    for fid, info in fragment_targets.items():
        edge = create_edge(
            conn,
            from_node=fragment_id,
            to_node=fid,
            layer="L0",
            edge_type="RESONANCE",
            created_by=created_by,
            confidence=0.3,
            provenance="auto_resonance",
        )
        resonance_edges.append(
            {
                "node_id": fid,
                "edge_id": edge["id"],
                "edge_type": "RESONANCE",
                "shared_targets": info["shared_targets"],
                "shared_count": len(info["shared_targets"]),
            }
        )

    return resonance_edges


def resolve_question(
    conn: DuckDBPyConnection,
    *,
    fragment_id: str,
    resolved_by: str,
) -> dict[str, Any] | None:
    """Mark a question fragment as resolved (OHM-a5rz.12).

    Updates metadata: is_question → false, adds resolved_at timestamp.
    Only resolves fragments that currently have is_question=true in metadata.

    Returns updated node dict, or None if fragment is not a question.
    """
    import json

    node = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not node:
        return None

    metadata_raw = node[1]
    metadata = json.loads(metadata_raw) if metadata_raw else {}
    if not metadata.get("is_question"):
        return None

    metadata["is_question"] = False
    now_result = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    metadata["resolved_at"] = str(now_result[0]) if now_result else ""

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [json.dumps(metadata), resolved_by, fragment_id],
    )
    _log_change(conn, "ohm_nodes", fragment_id, "UPDATE", agent_name=resolved_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [fragment_id]))[0]


def promote_fragment(
    conn: DuckDBPyConnection,
    *,
    fragment_id: str,
    promoted_by: str,
) -> dict[str, Any]:
    """Promote an L0 fragment to an L1 concept node (OHM-a5rz.26).

    Creates a new concept node with the fragment's label and content,
    sets metadata.promoted_from on the concept and metadata.promoted_to
    on the fragment, and creates a REFINES_FRAG edge from concept → fragment.

    Enforces ADR-022 L0→L1 promotion constraints (min_context_links ≥ 1).

    Args:
        conn: Database connection.
        fragment_id: ID of the fragment to promote.
        promoted_by: Agent performing the promotion.

    Returns:
        Dict with the new concept node and the created edge.

    Raises:
        NodeNotFoundError: If fragment doesn't exist.
        ValueError: If node is not a fragment or constraints not satisfied.
    """
    from ohm.exceptions import NodeNotFoundError, ConstraintViolationError

    frag = conn.execute(
        "SELECT id, label, content FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not frag:
        raise NodeNotFoundError(f"Fragment not found: {fragment_id}")

    frag_type = conn.execute(
        "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [fragment_id],
    ).fetchone()
    if not frag_type or frag_type[0] != "fragment":
        raise ValueError(f"Node {fragment_id} is not a fragment (type={frag_type[0] if frag_type else 'N/A'})")

    # ADR-022: Validate L0→L1 promotion constraints (enforced for structural constraints)
    from ohm.graph.constraints import validate_layer_promotion

    valid, warnings, errors = validate_layer_promotion(
        fragment_id,
        "L0",
        "L1",
        conn,
        enforce=True,
    )
    if errors:
        raise ConstraintViolationError(f"Cannot promote fragment {fragment_id}: {'; '.join(errors)}")

    import json as _json

    label = frag[1]
    content = frag[2]

    concept = create_node(
        conn,
        label=label,
        node_type="concept",
        content=content,
        created_by=promoted_by,
        provenance="fragment_promotion",
        confidence=0.5,
    )

    concept_id = concept["id"]

    # Set metadata.promoted_from on the concept
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps({"promoted_from": fragment_id}), concept_id],
    )

    edge = create_edge(
        conn,
        from_node=concept_id,
        to_node=fragment_id,
        layer="L0",
        edge_type="REFINES_FRAG",
        created_by=promoted_by,
        confidence=0.5,
        provenance="fragment_promotion",
    )

    # Update fragment metadata with promoted_to
    frag_meta_row = conn.execute(
        "SELECT metadata FROM ohm_nodes WHERE id = ?",
        [fragment_id],
    ).fetchone()
    frag_metadata = {}
    if frag_meta_row and frag_meta_row[0]:
        try:
            frag_metadata = _json.loads(frag_meta_row[0])
        except (ValueError, TypeError):
            frag_metadata = {}
    frag_metadata["promoted_to"] = concept_id
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(frag_metadata), fragment_id],
    )

    return {
        "concept": concept,
        "edge": edge,
        "promoted_from": fragment_id,
    }


def detect_fragment_resonance(
    conn: DuckDBPyConnection,
    *,
    min_shared: int = 2,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Detect cross-agent fragment resonance (OHM-a5rz.13).

    Finds pairs of fragments from different agents that share 2+ context
    nodes (via L0 CONTEXT_OF edges). Returns resonance pairs with Jaccard
    similarity on their context node sets.

    Args:
        min_shared: Minimum shared context nodes for a resonance pair.
        limit: Max resonance pairs to return.

    Returns:
        List of resonance dicts with fragment ids, agents, shared nodes, jaccard.
    """
    rows = _rows_to_dicts(
        conn.execute(
            """SELECT f1.id AS frag_a, f1.created_by AS agent_a,
                      f2.id AS frag_b, f2.created_by AS agent_b,
                      e1.to_node AS context_node
               FROM ohm_edges e1
               JOIN ohm_edges e2 ON e1.to_node = e2.to_node
               JOIN ohm_nodes f1 ON e1.from_node = f1.id AND f1.type = 'fragment' AND f1.deleted_at IS NULL
               JOIN ohm_nodes f2 ON e2.from_node = f2.id AND f2.type = 'fragment' AND f2.deleted_at IS NULL
               WHERE e1.edge_type = 'CONTEXT_OF' AND e1.layer = 'L0' AND e1.deleted_at IS NULL
                 AND e2.edge_type = 'CONTEXT_OF' AND e2.layer = 'L0' AND e2.deleted_at IS NULL
                 AND f1.created_by != f2.created_by
                 AND f1.id < f2.id
            """,
        )
    )

    from collections import defaultdict

    pair_contexts: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_agents: dict[tuple[str, str], tuple[str, str]] = {}

    for row in rows:
        key = (row["frag_a"], row["frag_b"])
        pair_contexts[key].add(row["context_node"])
        pair_agents[key] = (row["agent_a"], row["agent_b"])

    results = []
    for (frag_a, frag_b), shared in sorted(pair_contexts.items(), key=lambda x: -len(x[1])):
        if len(shared) < min_shared:
            continue
        if len(results) >= limit:
            break

        agent_a, agent_b = pair_agents[(frag_a, frag_b)]

        ctx_a_rows = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'CONTEXT_OF' AND layer = 'L0' AND deleted_at IS NULL",
            [frag_a],
        ).fetchall()
        ctx_a = {r[0] for r in ctx_a_rows}

        ctx_b_rows = conn.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND edge_type = 'CONTEXT_OF' AND layer = 'L0' AND deleted_at IS NULL",
            [frag_b],
        ).fetchall()
        ctx_b = {r[0] for r in ctx_b_rows}

        union = ctx_a | ctx_b
        jaccard = len(shared) / len(union) if union else 0.0

        results.append(
            {
                "fragment_a": frag_a,
                "fragment_b": frag_b,
                "agent_a": agent_a,
                "agent_b": agent_b,
                "shared_context_nodes": sorted(shared),
                "shared_count": len(shared),
                "jaccard": round(jaccard, 3),
            }
        )

    return results


def reflect_challenge_to_fragments(
    conn: DuckDBPyConnection,
    challenged_edge_id: str,
    challenge_edge_id: str,
    challenged_by: str,
) -> list[dict[str, Any]]:
    """Trace a challenge back to originating L0 fragments (OHM-a5rz.15).

    When an L3/L4 edge is challenged, follow ``DERIVES_FROM`` / ``REFERENCES``
    edges backward from the claim node to find L0 ``fragment`` nodes that may
    have originated the claim. Creates lightweight L0 annotation edges
    (``type='CHALLENGED_BY'``, ``layer='L0'``) from the challenge back to
    each originating fragment so the thinking layer is aware of the challenge.

    Returns a list of fragment IDs that were annotated.
    """
    target = conn.execute(
        "SELECT from_node, layer FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [challenged_edge_id],
    ).fetchone()
    if not target:
        return []

    claim_node, layer = target
    if not layer or layer == "L0":
        return []

    fragments = conn.execute(
        """SELECT DISTINCT n.id
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.type = 'fragment' AND n.deleted_at IS NULL
           WHERE e.to_node = ?
             AND e.edge_type IN ('DERIVES_FROM', 'REFERENCES')
             AND e.deleted_at IS NULL
           LIMIT 5""",
        [claim_node],
    ).fetchall()

    results = []
    for row in fragments:
        frag_id = row[0]
        ann_id = f"backflow_{challenge_edge_id[:36]}_{frag_id[:36]}"[:80]
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, created_by, confidence, provenance)
               VALUES (?, ?, ?, 'L0', 'CHALLENGED_BY', ?, 0.5, ?)
               ON CONFLICT (id) DO NOTHING""",
            [ann_id, claim_node, frag_id, challenged_by, f"auto: challenge backflow from {challenge_edge_id}"],
        )
        results.append({"fragment_id": frag_id})
    return results


def detect_fragment_clusters(
    conn: DuckDBPyConnection,
    *,
    min_cluster_size: int = 5,
    window_days: int = 7,
) -> list[dict[str, Any]]:
    """Detect clusters of L0 fragments sharing context nodes (OHM-a5rz.14).

    When an agent accumulates ``min_cluster_size`` or more fragments that
    share context nodes (via ``CONTEXT_OF`` edges) within ``window_days``,
    returns the cluster with a theme summary to nudge the agent toward
    synthesis.

    Returns a list of cluster dicts, each with:
    - ``agent``: the agent who owns the fragments
    - ``fragment_count``: number of fragments in the cluster
    - ``fragment_ids``: list of fragment IDs
    - ``fragment_labels``: list of fragment labels
    - ``shared_context_nodes``: context nodes shared across fragments
    - ``theme``: auto-generated theme from shared context labels
    """
    from datetime import datetime, timedelta

    cutoff = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    clusters: dict[str, dict] = {}

    # Find agents with fragments in the window
    fragment_counts = conn.execute(
        """SELECT created_by, COUNT(*) AS cnt
           FROM ohm_nodes
           WHERE type = 'fragment' AND deleted_at IS NULL
             AND created_at >= ?::TIMESTAMP
           GROUP BY created_by
           HAVING COUNT(*) >= ?""",
        [cutoff, min_cluster_size],
    ).fetchall()

    for row in fragment_counts:
        agent = row[0]
        # Get this agent's fragments with their context nodes
        ctx_rows = conn.execute(
            """SELECT n.id, n.label, c.to_node AS ctx_node
               FROM ohm_nodes n
               LEFT JOIN ohm_edges e ON e.from_node = n.id
                 AND e.edge_type = 'CONTEXT_OF'
                 AND e.deleted_at IS NULL
               LEFT JOIN ohm_nodes c ON c.id = e.to_node
               WHERE n.type = 'fragment'
                 AND n.deleted_at IS NULL
                 AND n.created_by = ?
                 AND n.created_at >= ?::TIMESTAMP
               ORDER BY n.created_at DESC
               LIMIT 200""",
            [agent, cutoff],
        ).fetchall()

        # Group by fragment
        frag_map: dict[str, dict] = {}
        for r in ctx_rows:
            fid, flabel, ctx_id = r[0], r[1], r[2]
            if fid not in frag_map:
                frag_map[fid] = {"label": flabel, "context": set()}
            if ctx_id:
                frag_map[fid]["context"].add(ctx_id)

        fragments = list(frag_map.items())

        # Check if enough fragments share at least one context node
        shared_ctx: set[str] = set()
        for fid, info in fragments:
            if not shared_ctx:
                shared_ctx = info["context"]
            else:
                shared_ctx &= info["context"]

        if len(fragments) >= min_cluster_size and len(shared_ctx) >= 1:
            cluster_key = agent
            clusters[cluster_key] = {
                "agent": agent,
                "fragment_count": len(fragments),
                "fragment_ids": [f[0] for f in fragments],
                "fragment_labels": [f[1]["label"] for f in fragments],
                "shared_context_nodes": sorted(shared_ctx),
                "theme": f"{len(fragments)} fragments sharing {len(shared_ctx)} context nodes",
            }

            # Compute theme from shared context node labels
            if shared_ctx:
                placeholders = ",".join(["?"] * len(shared_ctx))
                ctx_labels = conn.execute(
                    f"SELECT id, label FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    list(shared_ctx),
                ).fetchall()
                if ctx_labels:
                    labels = [r[1] for r in ctx_labels if r[1]]
                    if labels:
                        clusters[cluster_key]["theme"] = f"You've been thinking about: {', '.join(labels[:5])}"

    return list(clusters.values())


def query_fragment_clusters(
    conn: DuckDBPyConnection,
    *,
    min_fragments: int = 3,
    min_shared_targets: int = 2,
) -> list[dict[str, Any]]:
    """Find clusters of fragments sharing CONTEXT_OF targets (OHM-a5rz.28).

    Identifies groups of 3+ fragments that share 2+ CONTEXT_OF target
    nodes. These clusters are promotion candidates — the fragments may
    be worth combining into an L1 concept.

    Uses a graph-based approach: finds fragment pairs sharing >= 2 targets,
    then groups connected components into clusters.

    Args:
        min_fragments: Minimum fragments per cluster (default 3).
        min_shared_targets: Minimum shared targets per pair (default 2).

    Returns:
        List of cluster dicts, sorted by cluster size descending.
    """
    # Step 1: Find all fragment→target pairs (CONTEXT_OF edges from fragments)
    fragment_targets = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS fragment_id, e.to_node AS target_id
               FROM ohm_edges e
               JOIN ohm_nodes n ON e.from_node = n.id
               WHERE e.edge_type = 'CONTEXT_OF' AND e.layer = 'L0' AND e.deleted_at IS NULL
                 AND n.type = 'fragment' AND n.deleted_at IS NULL
            """,
        )
    )

    if len(fragment_targets) < min_fragments:
        return []

    # Group targets by fragment
    from collections import defaultdict

    frag_to_targets: dict[str, set[str]] = defaultdict(set)
    for row in fragment_targets:
        frag_to_targets[row["fragment_id"]].add(row["target_id"])

    fragment_ids = list(frag_to_targets.keys())

    # Step 2: Build adjacency — edge between fragments sharing >= min_shared_targets
    adj: dict[str, set[str]] = defaultdict(set)
    for i in range(len(fragment_ids)):
        fi = fragment_ids[i]
        ti = frag_to_targets[fi]
        for j in range(i + 1, len(fragment_ids)):
            fj = fragment_ids[j]
            tj = frag_to_targets[fj]
            shared = ti & tj
            if len(shared) >= min_shared_targets:
                adj[fi].add(fj)
                adj[fj].add(fi)

    # Step 3: BFS to find connected components (clusters)
    visited: set[str] = set()
    clusters: list[list[str]] = []

    for fid in adj:
        if fid in visited:
            continue
        component: list[str] = []
        queue = [fid]
        visited.add(fid)
        while queue:
            node = queue.pop(0)
            component.append(node)
            for neighbor in adj[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(component) >= min_fragments:
            clusters.append(component)

    # Sort by cluster size descending
    clusters.sort(key=lambda c: -len(c))

    # Step 4: Build response with shared target info
    result = []
    for cluster in clusters:
        # Union of all shared targets across the cluster's internal edges
        cluster_targets: set[str] = set()
        for i in range(len(cluster)):
            for j in range(i + 1, len(cluster)):
                shared = frag_to_targets[cluster[i]] & frag_to_targets[cluster[j]]
                if len(shared) >= min_shared_targets:
                    cluster_targets |= shared

        result.append(
            {
                "cluster_size": len(cluster),
                "fragment_ids": sorted(cluster),
                "shared_target_count": len(cluster_targets),
                "shared_target_ids": sorted(cluster_targets),
            }
        )

    return result


def evict_expired_fragments(
    conn: DuckDBPyConnection,
    *,
    ttl_days: int = 30,
) -> dict[str, Any]:
    """Soft-delete expired L0 fragments (OHM-a5rz.27).

    Runs the fragment TTL eviction policy:
    - Fragments older than ``ttl_days`` (based on ``updated_at``) are candidates.
    - If the fragment was promoted (has ``metadata.promoted_to``), it is **never** evicted.
    - If the fragment has any outgoing L0 edges, its TTL is **extended** (``updated_at``
      set to ``now()``) — connected fragments are worth keeping.
    - Otherwise, the fragment is **soft-deleted** (``deleted_at`` set to ``now()``).

    This is designed to run as an hourly background job in ohmd, but can also be
    called on-demand via ``POST /admin/evict-fragments``.

    Args:
        conn: Database connection.
        ttl_days: Number of days after which an unconnected fragment expires.

    Returns:
        Dict with ``evicted`` (list of fragment ids soft-deleted),
        ``extended`` (list of fragment ids whose TTL was extended),
        ``skipped_promoted`` (list of promoted fragment ids preserved),
        and ``candidate_count`` (total candidates evaluated).
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT id, metadata
               FROM ohm_nodes
               WHERE type = 'fragment'
                 AND deleted_at IS NULL
                 AND updated_at < ?
               ORDER BY updated_at ASC
            """,
            [cutoff],
        )
    )

    result: dict[str, Any] = {
        "evicted": [],
        "extended": [],
        "skipped_promoted": [],
        "candidate_count": len(candidates),
    }

    for candidate in candidates:
        fid = candidate["id"]
        meta_raw = candidate["metadata"]
        meta: dict[str, Any] = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
            except (ValueError, TypeError):
                meta = {}

        # Never evict promoted fragments (OHM-a5rz.26)
        if "promoted_to" in meta:
            result["skipped_promoted"].append(fid)
            continue

        # Check for outgoing L0 edges — extends TTL if any exist
        edge_row = conn.execute(
            """SELECT COUNT(*) FROM ohm_edges
               WHERE from_node = ? AND layer = 'L0' AND deleted_at IS NULL""",
            [fid],
        ).fetchone()
        has_edges = edge_row and edge_row[0] > 0

        if has_edges:
            # Extend TTL by bumping updated_at
            conn.execute(
                "UPDATE ohm_nodes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fid],
            )
            result["extended"].append(fid)
        else:
            # Soft-delete: no edges, not promoted, expired
            conn.execute(
                "UPDATE ohm_nodes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fid],
            )
            result["evicted"].append(fid)

    return result


# ── Data Products (ADR-027 / OHM-ksi0) ─────────────────────────────────────


def register_data_product(
    conn: DuckDBPyConnection,
    *,
    product_id: str,
    name: str,
    type: str,
    producer_agent: str,
    created_by: str,
    customer_id: str | None = None,
    language: str = "en",
    visibility: str = "private",
    status: str = "draft",
    value_proposition: str | None = None,
    description: str | None = None,
    output_port_type: str | None = None,
    access_format: str | None = None,
    access_url: str | None = None,
    authentication_method: str | None = None,
    output_file_formats: str | None = None,
    ohm_node_id: str | None = None,
    confidence: float | None = None,
    product_version: str | None = None,
    odps_yaml: str | None = None,
    consumers: list[str] | None = None,
    auto_link: bool = True,
) -> dict[str, Any]:
    """Insert or update an ODPS data product (ADR-027). Returns the full record.

    OHM-ovwq: When ``ohm_node_id`` is None and ``auto_link`` is True, auto-creates
    an OHM ``source`` node for the product, a ``PRODUCES`` L2 edge from the producer
    agent node, and ``CONSUMES`` L2 edges from each consumer agent node. Seeds
    ``source_reliability`` from the producer agent's outcome history.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    if ohm_node_id is None and auto_link:
        ohm_node_id, source_reliability = _link_provenance(
            conn,
            name=name,
            product_id=product_id,
            type=type,
            producer_agent=producer_agent,
            created_by=created_by,
            description=description,
            access_url=access_url,
            confidence=confidence,
            consumers=consumers,
        )
    elif ohm_node_id is None:
        source_reliability = None
    else:
        source_reliability = _seed_reliability(conn, producer_agent, created_by)

    existing = _rows_to_dicts(
        conn.execute(
            "SELECT internal_id FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )
    )
    if existing:
        internal_id = existing[0]["internal_id"]
        conn.execute(
            """UPDATE ohm_data_products SET
                   name = ?, type = ?, visibility = ?, status = ?,
                   value_proposition = ?, description = ?, producer_agent = ?,
                   output_port_type = ?, access_format = ?, access_url = ?,
                   authentication_method = ?, output_file_formats = ?,
                   ohm_node_id = ?, confidence = ?, source_reliability = ?,
                   product_version = ?,
                   odps_yaml = ?, updated = ?, updated_at = CURRENT_TIMESTAMP
               WHERE internal_id = ?""",
            [
                name,
                type,
                visibility,
                status,
                value_proposition,
                description,
                producer_agent,
                output_port_type,
                access_format,
                access_url,
                authentication_method,
                output_file_formats,
                ohm_node_id,
                confidence,
                source_reliability,
                product_version,
                odps_yaml,
                now,
                internal_id,
            ],
        )
        _log_change(conn, "ohm_data_products", internal_id, "UPDATE", created_by)
        rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
        return rows[0]

    internal_id = str(_uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_data_products
           (internal_id, customer_id, product_id, name, language, visibility, status, type,
            value_proposition, description, producer_agent, output_port_type, access_format,
            access_url, authentication_method, output_file_formats, ohm_node_id, confidence,
            source_reliability, product_version, odps_yaml, created_by, created_at, updated_at, created, updated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)""",
        [
            internal_id,
            customer_id,
            product_id,
            name,
            language,
            visibility,
            status,
            type,
            value_proposition,
            description,
            producer_agent,
            output_port_type,
            access_format,
            access_url,
            authentication_method,
            output_file_formats,
            ohm_node_id,
            confidence,
            source_reliability,
            product_version,
            odps_yaml,
            created_by,
            now,
            now,
        ],
    )
    _log_change(conn, "ohm_data_products", internal_id, "INSERT", created_by)
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
    return rows[0]


def _seed_reliability(conn: DuckDBPyConnection, producer_agent: str, created_by: str) -> float | None:
    """Seed source_reliability from the producer agent's outcome history."""
    prod_node = find_or_create_node(conn, label=producer_agent, node_type="agent", created_by=created_by)
    try:
        reliability = query_source_reliability(conn, prod_node["id"])
        eff = reliability.get("effective_reliability")
        return eff if eff is not None else None
    except Exception:
        return None


def _link_provenance(
    conn: DuckDBPyConnection,
    *,
    name: str,
    product_id: str,
    type: str,
    producer_agent: str,
    created_by: str,
    description: str | None = None,
    access_url: str | None = None,
    confidence: float | None = None,
    consumers: list[str] | None = None,
) -> tuple[str | None, float | None]:
    """Auto-create OHM provenance node + edges for a data product (OHM-ovwq).

    Returns (ohm_node_id, source_reliability).
    """
    import json as _json

    product_node = find_or_create_node(
        conn,
        label=name,
        node_type="source",
        content=description,
        created_by=created_by,
        provenance="bos-data-product",
        confidence=confidence or 0.7,
        url=access_url,
    )
    ohm_node_id = product_node["id"]

    prod_node = find_or_create_node(conn, label=producer_agent, node_type="agent", created_by=created_by)

    _idempotent_edge(
        conn,
        from_node=prod_node["id"],
        to_node=ohm_node_id,
        layer="L2",
        edge_type="PRODUCES",
        created_by=created_by,
        confidence=confidence or 0.7,
        provenance="bos-data-product",
    )

    if consumers:
        for consumer_label in consumers:
            consumer_node = find_or_create_node(conn, label=consumer_label, node_type="agent", created_by=created_by)
            _idempotent_edge(
                conn,
                from_node=consumer_node["id"],
                to_node=ohm_node_id,
                layer="L2",
                edge_type="CONSUMES",
                created_by=created_by,
                confidence=0.5,
                provenance="bos-data-product",
            )

    source_reliability = _seed_reliability(conn, producer_agent, created_by)
    return ohm_node_id, source_reliability


def _idempotent_edge(
    conn: DuckDBPyConnection,
    *,
    from_node: str,
    to_node: str,
    layer: str,
    edge_type: str,
    created_by: str,
    confidence: float = 0.7,
    provenance: str | None = None,
) -> None:
    """Create an edge only if it doesn't already exist (idempotent)."""
    existing = conn.execute(
        "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = ? AND layer = ? AND created_by = ? AND deleted_at IS NULL",
        [from_node, to_node, edge_type, layer, created_by],
    ).fetchone()
    if not existing:
        create_edge(
            conn,
            from_node=from_node,
            to_node=to_node,
            layer=layer,
            edge_type=edge_type,
            created_by=created_by,
            confidence=confidence,
            provenance=provenance,
        )


def refresh_data_product_provenance(
    conn: DuckDBPyConnection,
    internal_id: str,
) -> dict[str, Any] | None:
    """Refresh source_reliability and confidence for a data product from outcomes (OHM-ovwq).

    Called after recording an outcome against a product's OHM node to update
    the catalog entry with the latest reliability score.
    """
    product = get_data_product(conn, internal_id)
    if not product or not product.get("ohm_node_id"):
        return None

    producer = product.get("producer_agent")
    if not producer:
        return None

    prod_node = _rows_to_dicts(conn.execute("SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent' AND deleted_at IS NULL", [producer]))
    if not prod_node:
        return None

    try:
        reliability = query_source_reliability(conn, prod_node[0]["id"])
        eff = reliability.get("effective_reliability")
    except Exception:
        eff = None

    conn.execute(
        "UPDATE ohm_data_products SET source_reliability = ?, updated_at = CURRENT_TIMESTAMP WHERE internal_id = ?",
        [eff, internal_id],
    )
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ?", [internal_id]))
    return rows[0] if rows else None


def get_data_product(conn: DuckDBPyConnection, internal_id: str) -> dict[str, Any] | None:
    """Get a data product by internal_id."""
    rows = _rows_to_dicts(conn.execute("SELECT * FROM ohm_data_products WHERE internal_id = ? AND deleted_at IS NULL", [internal_id]))
    return rows[0] if rows else None


def get_data_product_by_odps_id(
    conn: DuckDBPyConnection,
    product_id: str,
    *,
    customer_id: str | None = None,
    language: str = "en",
) -> dict[str, Any] | None:
    """Get a data product by its ODPS product_id (+ tenant + language)."""
    rows = _rows_to_dicts(
        conn.execute(
            "SELECT * FROM ohm_data_products WHERE customer_id IS NOT DISTINCT FROM ? AND product_id = ? AND language = ? AND deleted_at IS NULL",
            [customer_id, product_id, language],
        )
    )
    return rows[0] if rows else None


def list_data_products(
    conn: DuckDBPyConnection,
    *,
    producer_agent: str | None = None,
    type: str | None = None,
    status: str | None = None,
    customer_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List data products with optional filters."""
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if producer_agent:
        conditions.append("producer_agent = ?")
        params.append(producer_agent)
    if type:
        conditions.append("type = ?")
        params.append(type)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if customer_id is not None:
        conditions.append("customer_id IS NOT DISTINCT FROM ?")
        params.append(customer_id)
    where = " WHERE " + " AND ".join(conditions)
    params.append(limit)
    return _rows_to_dicts(conn.execute(f"SELECT * FROM ohm_data_products{where} ORDER BY updated_at DESC LIMIT ?", params))


def update_node_hd_fingerprint(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    dim: int = 10000,
    seed: int = 42,
) -> dict[str, Any]:
    from ohm.exceptions import NodeNotFoundError
    from ohm.inference.hd import fingerprint_node
    from ohm.validation import validate_identifier, validate_hd_fingerprint

    node_id = validate_identifier(node_id, name="node_id")

    row = conn.execute(
        """SELECT id, label, type, content, tags, provenance
           FROM ohm_nodes
           WHERE id = ? AND deleted_at IS NULL""",
        [node_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Node {node_id} not found")

    nid, label, ntype, content, tags_json, provenance = row
    tags = None
    if tags_json:
        import json

        try:
            tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
        except (json.JSONDecodeError, TypeError):
            tags = None

    fp = fingerprint_node(
        label=label,
        node_type=ntype,
        content=content,
        tags=tags,
        provenance=provenance,
        dim=dim,
        seed=seed,
    )
    fp_bytes = bytes.fromhex(fp["fingerprint_hex"])
    validate_hd_fingerprint(fp_bytes, dimensions=dim)

    conn.execute(
        "UPDATE ohm_nodes SET hd_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        [fp_bytes, node_id],
    )
    return {
        "node_id": nid,
        "label": label,
        "type": ntype,
        "fingerprint_hex": fp["fingerprint_hex"],
        "dimension": fp["dimension"],
        "seed": fp["seed"],
        "method": fp["method"],
        "stored": True,
    }


def hd_membership_search(
    conn: DuckDBPyConnection,
    query_fingerprint_hex: str,
    *,
    threshold: float = 0.65,
    limit: int = 20,
    node_type: str | None = None,
    dim: int = 10000,
) -> list[dict[str, Any]]:
    from ohm.inference.hd import hamming_similarity
    from ohm.validation import validate_hd_fingerprint

    if not query_fingerprint_hex:
        raise ValueError("query_fingerprint_hex must be non-empty")

    query_bytes = bytearray.fromhex(query_fingerprint_hex)
    validate_hd_fingerprint(bytes(query_bytes), dimensions=dim)

    conditions = ["hd_fingerprint IS NOT NULL", "deleted_at IS NULL"]
    params: list[Any] = []
    if node_type is not None:
        conditions.append("type = ?")
        params.append(node_type)
    where_sql = " AND ".join(conditions)

    rows = conn.execute(
        f"""SELECT id, label, type, confidence, hd_fingerprint
            FROM ohm_nodes
            WHERE {where_sql}""",
        params,
    ).fetchall()

    results = []
    for r in rows:
        rid, rlabel, rtype, rconf, rfp_blob = r
        if rfp_blob is None:
            continue
        candidate_bytes = bytearray(rfp_blob) if isinstance(rfp_blob, bytes) else bytearray(rfp_blob)
        if len(candidate_bytes) != len(query_bytes):
            continue
        sim = hamming_similarity(query_bytes, candidate_bytes)
        if sim >= threshold:
            results.append(
                {
                    "node_id": rid,
                    "label": rlabel,
                    "type": rtype,
                    "confidence": rconf,
                    "hd_similarity": round(sim, 4),
                }
            )
    results.sort(key=lambda x: x["hd_similarity"], reverse=True)
    return results[:limit]


def batch_update_hd_fingerprints(
    conn: DuckDBPyConnection,
    *,
    dim: int = 10000,
    seed: int = 42,
    limit: int = 1000,
) -> dict[str, Any]:
    from ohm.inference.hd import fingerprint_node
    from ohm.validation import validate_hd_fingerprint

    rows = conn.execute(
        """SELECT id, label, type, content, tags, provenance
           FROM ohm_nodes
           WHERE hd_fingerprint IS NULL AND deleted_at IS NULL
           ORDER BY confidence DESC
           LIMIT ?""",
        [limit],
    ).fetchall()

    updated = 0
    skipped = 0
    for r in rows:
        nid, label, ntype, content, tags_json, provenance = r
        tags = None
        if tags_json:
            import json

            try:
                tags = json.loads(tags_json) if isinstance(tags_json, str) else tags_json
            except (json.JSONDecodeError, TypeError):
                tags = None
        try:
            fp = fingerprint_node(
                label=label,
                node_type=ntype,
                content=content,
                tags=tags,
                provenance=provenance,
                dim=dim,
                seed=seed,
            )
            fp_bytes = bytes.fromhex(fp["fingerprint_hex"])
            validate_hd_fingerprint(fp_bytes, dimensions=dim)
            conn.execute(
                "UPDATE ohm_nodes SET hd_fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                [fp_bytes, nid],
            )
            updated += 1
        except Exception:
            skipped += 1

    return {
        "updated": updated,
        "skipped": skipped,
        "dimension": dim,
        "seed": seed,
        "method": "tastebud_hd_v1",
    }


def sign_node_write(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    key: bytes,
    algorithm: str = "hmac-sha256",
    key_id: str = "default",
) -> dict[str, Any]:
    from ohm.exceptions import NodeNotFoundError
    from ohm.graph.crypto import sign_write
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    row = conn.execute(
        "SELECT id, label, type, content, created_by, confidence, visibility, provenance, source_tier FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Node {node_id} not found")
    record = {"id": row[0], "label": row[1], "type": row[2], "content": row[3], "created_by": row[4], "confidence": row[5], "visibility": row[6], "provenance": row[7], "source_tier": row[8]}
    sig_data = sign_write(record, kind="node", key=key, algorithm=algorithm, key_id=key_id)
    conn.execute(
        "UPDATE ohm_nodes SET write_signature = ?, signing_key_id = ?, signed_at = ? WHERE id = ?",
        [sig_data["write_signature"], sig_data["signing_key_id"], sig_data["signed_at"], node_id],
    )
    return {"node_id": node_id, **sig_data}


def sign_edge_write(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    key: bytes,
    algorithm: str = "hmac-sha256",
    key_id: str = "default",
) -> dict[str, Any]:
    from ohm.exceptions import EdgeNotFoundError
    from ohm.graph.crypto import sign_write
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, created_by, confidence, probability, source_tier FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if not row:
        raise EdgeNotFoundError(f"Edge {edge_id} not found")
    record = {"id": row[0], "from_node": row[1], "to_node": row[2], "layer": row[3], "edge_type": row[4], "created_by": row[5], "confidence": row[6], "probability": row[7], "source_tier": row[8]}
    sig_data = sign_write(record, kind="edge", key=key, algorithm=algorithm, key_id=key_id)
    conn.execute(
        "UPDATE ohm_edges SET write_signature = ?, signing_key_id = ?, signed_at = ? WHERE id = ?",
        [sig_data["write_signature"], sig_data["signing_key_id"], sig_data["signed_at"], edge_id],
    )
    return {"edge_id": edge_id, **sig_data}


def verify_node_write(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    key: bytes,
) -> dict[str, Any]:
    from ohm.graph.crypto import verify_write
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")
    row = conn.execute(
        "SELECT id, label, type, content, created_by, confidence, visibility, provenance, source_tier, write_signature FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not row:
        return {"node_id": node_id, "verified": False, "reason": "not_found"}
    record = {"id": row[0], "label": row[1], "type": row[2], "content": row[3], "created_by": row[4], "confidence": row[5], "visibility": row[6], "provenance": row[7], "source_tier": row[8], "write_signature": row[9]}
    verified = verify_write(record, kind="node", key=key)
    return {"node_id": node_id, "verified": verified}


def verify_edge_write(
    conn: DuckDBPyConnection,
    edge_id: str,
    *,
    key: bytes,
) -> dict[str, Any]:
    from ohm.graph.crypto import verify_write
    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    row = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, created_by, confidence, probability, source_tier, write_signature FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if not row:
        return {"edge_id": edge_id, "verified": False, "reason": "not_found"}
    record = {"id": row[0], "from_node": row[1], "to_node": row[2], "layer": row[3], "edge_type": row[4], "created_by": row[5], "confidence": row[6], "probability": row[7], "source_tier": row[8], "write_signature": row[9]}
    verified = verify_write(record, kind="edge", key=key)
    return {"edge_id": edge_id, "verified": verified}


def create_suggestion(
    conn: DuckDBPyConnection,
    *,
    suggestion_type: str,
    from_node: str | None = None,
    to_node: str | None = None,
    target_node: str | None = None,
    suggested_edge_type: str | None = None,
    suggested_layer: str | None = None,
    confidence: float = 0.5,
    source_method: str = "manual",
    source_agent: str = "system",
    metadata: dict | None = None,
    created_by: str = "system",
) -> dict[str, Any]:
    import json
    import uuid

    from ohm.validation import validate_suggestion_type

    suggestion_type = validate_suggestion_type(suggestion_type)

    existing = conn.execute(
        """SELECT id, evidence_count FROM ohm_suggestions
           WHERE from_node IS NOT DISTINCT FROM ?
             AND to_node IS NOT DISTINCT FROM ?
             AND target_node IS NOT DISTINCT FROM ?
             AND status = 'ripe' AND deleted_at IS NULL""",
        [from_node, to_node, target_node],
    ).fetchone()

    if existing:
        new_count = (existing[1] or 1) + 1
        conn.execute(
            "UPDATE ohm_suggestions SET evidence_count = ?, last_ripened_at = CURRENT_TIMESTAMP WHERE id = ?",
            [new_count, existing[0]],
        )
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [existing[0]]))[0]

    sid = f"sug_{uuid.uuid4().hex[:12]}"
    metadata_json = json.dumps(metadata) if metadata else None
    conn.execute(
        """INSERT INTO ohm_suggestions
           (id, suggestion_type, from_node, to_node, target_node, suggested_edge_type, suggested_layer,
            confidence, status, source_method, source_agent, metadata, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ripe', ?, ?, ?, ?)""",
        [sid, suggestion_type, from_node, to_node, target_node, suggested_edge_type, suggested_layer, confidence, source_method, source_agent, metadata_json, created_by],
    )
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [sid]))[0]


def query_suggestions(
    conn: DuckDBPyConnection,
    *,
    status: str | None = None,
    source_method: str | None = None,
    target_node: str | None = None,
    min_ripeness: float = 0.0,
    limit: int = 50,
) -> list[dict[str, Any]]:
    conditions = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    if source_method is not None:
        conditions.append("source_method = ?")
        params.append(source_method)
    if target_node is not None:
        conditions.append("target_node = ?")
        params.append(target_node)
    if min_ripeness > 0:
        conditions.append("ripeness_score >= ?")
        params.append(min_ripeness)
    where = " WHERE " + " AND ".join(conditions)
    params.append(limit)
    return _rows_to_dicts(conn.execute(f"SELECT * FROM ohm_suggestions{where} ORDER BY ripeness_score DESC, suggested_at DESC LIMIT ?", params))


def promote_suggestion(
    conn: DuckDBPyConnection,
    suggestion_id: str,
    *,
    promoted_by: str,
    edge_layer: str = "L3",
) -> dict[str, Any]:
    from ohm.exceptions import OHMError
    from ohm.validation import validate_identifier

    suggestion_id = validate_identifier(suggestion_id, name="suggestion_id")
    row = conn.execute(
        "SELECT * FROM ohm_suggestions WHERE id = ? AND deleted_at IS NULL",
        [suggestion_id],
    ).fetchone()
    if not row:
        raise OHMError(f"Suggestion {suggestion_id} not found")

    cols = [d[0] for d in conn.execute("SELECT * FROM ohm_suggestions WHERE id = ?", [suggestion_id]).description]
    sug = dict(zip(cols, row))

    if sug["status"] != "ripe":
        raise OHMError(f"Suggestion {suggestion_id} status is '{sug['status']}', must be 'ripe' to promote")

    if sug["suggestion_type"] == "edge" and sug["from_node"] and sug["to_node"] and sug["suggested_edge_type"]:
        import uuid

        edge_id = f"edge_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """INSERT INTO ohm_edges (id, from_node, to_node, edge_type, layer, created_by, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [edge_id, sug["from_node"], sug["to_node"], sug["suggested_edge_type"], edge_layer, promoted_by, sug["confidence"]],
        )

    conn.execute(
        "UPDATE ohm_suggestions SET status = 'promoted', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        [promoted_by, suggestion_id],
    )
    return {"suggestion_id": suggestion_id, "status": "promoted", "promoted_by": promoted_by}


def reject_suggestion(
    conn: DuckDBPyConnection,
    suggestion_id: str,
    *,
    rejected_by: str,
    notes: str | None = None,
) -> dict[str, Any]:
    from ohm.exceptions import OHMError
    from ohm.validation import validate_identifier

    suggestion_id = validate_identifier(suggestion_id, name="suggestion_id")
    row = conn.execute(
        "SELECT status FROM ohm_suggestions WHERE id = ? AND deleted_at IS NULL",
        [suggestion_id],
    ).fetchone()
    if not row:
        raise OHMError(f"Suggestion {suggestion_id} not found")

    conn.execute(
        "UPDATE ohm_suggestions SET status = 'rejected', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP, review_notes = ? WHERE id = ?",
        [rejected_by, notes, suggestion_id],
    )
    return {"suggestion_id": suggestion_id, "status": "rejected"}


def expire_suggestions(
    conn: DuckDBPyConnection,
    *,
    max_age_days: int = 30,
) -> dict[str, Any]:
    conn.execute(
        """UPDATE ohm_suggestions
           SET status = 'expired'
           WHERE status = 'ripe'
             AND deleted_at IS NULL
             AND suggested_at < CURRENT_TIMESTAMP - INTERVAL '?' DAY""",
        [max_age_days],
    )
    count = conn.execute("SELECT changes()").fetchone()[0]
    return {"expired_count": count}


def batch_orphan_triage(
    conn: DuckDBPyConnection,
    *,
    limit: int = 50,
    exclude_types: frozenset[str] | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """Batch triage orphan nodes, producing link suggestions.

    Scans orphan nodes (zero edges) and generates suggestions for connecting
    them to the graph. Uses two heuristics:
    1. Same-type matching: orphan shares type with a connected node.
    2. Label similarity: orphan label overlaps with connected node labels.

    Returns a triage report with per-node suggestions and summary stats.

    Args:
        conn: DuckDB connection.
        limit: Max orphans to process (default 50).
        exclude_types: Node types to skip (default: fragment, agent, skill, value, goal).
        min_confidence: Only triage orphans with confidence >= this value.
    """
    from ohm.validation import validate_identifier

    if exclude_types is None:
        exclude_types = frozenset({"fragment", "agent", "skill", "value", "goal"})

    exclude_clause = ""
    params: list[Any] = []
    if exclude_types:
        placeholders = ", ".join(["?"] * len(exclude_types))
        exclude_clause = f"AND n.type NOT IN ({placeholders})"
        params.extend(list(exclude_types))

    confidence_clause = ""
    if min_confidence is not None:
        confidence_clause = "AND n.confidence >= ?"
        params.append(min_confidence)

    orphans = conn.execute(
        f"""
        SELECT n.id, n.label, n.type, n.confidence, n.created_by, n.created_at
        FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE (e.from_node = n.id OR e.to_node = n.id)
                AND e.deleted_at IS NULL
          )
          {exclude_clause}
          {confidence_clause}
        ORDER BY n.confidence DESC NULLS LAST
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()

    if not orphans:
        return {
            "triaged_count": 0,
            "total_orphans": 0,
            "with_suggestions": 0,
            "without_suggestions": 0,
            "suggestions": [],
            "types_seen": {},
            "method": "batch_orphan_triage",
        }

    total_orphan_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE n.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM ohm_edges e
              WHERE (e.from_node = n.id OR e.to_node = n.id)
                AND e.deleted_at IS NULL
          )
    """).fetchone()
    total_orphans = total_orphan_row[0] if total_orphan_row else 0

    types_seen: dict[str, int] = {}
    suggestions: list[dict[str, Any]] = []

    for row in orphans:
        orphan_id, label, node_type, confidence, created_by, created_at = row
        types_seen[node_type] = types_seen.get(node_type, 0) + 1

        same_type_matches = conn.execute(
            """
            SELECT c.id, c.label, c.type
            FROM ohm_nodes c
            WHERE c.deleted_at IS NULL
              AND c.type = ?
              AND c.id != ?
              AND EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE (e.from_node = c.id OR e.to_node = c.id)
                    AND e.deleted_at IS NULL
              )
            ORDER BY c.confidence DESC NULLS LAST
            LIMIT 3
            """,
            [node_type, orphan_id],
        ).fetchall()

        label_words = set(label.lower().split()) if label else set()
        label_matches: list[tuple[str, str, str, int]] = []
        if label_words and len(label_words) <= 20:
            label_match_rows = conn.execute(
                """
                SELECT c.id, c.label, c.type
                FROM ohm_nodes c
                WHERE c.deleted_at IS NULL
                  AND c.id != ?
                  AND c.label IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM ohm_edges e
                      WHERE (e.from_node = c.id OR e.to_node = c.id)
                        AND e.deleted_at IS NULL
                  )
                LIMIT 200
                """,
                [orphan_id],
            ).fetchall()
            for lm in label_match_rows:
                target_words = set(lm[1].lower().split()) if lm[1] else set()
                overlap = len(label_words & target_words)
                if overlap >= 2:
                    label_matches.append((lm[0], lm[1], lm[2], overlap))
            label_matches.sort(key=lambda x: x[3], reverse=True)
            label_matches = label_matches[:3]

        node_suggestions = []
        for m in same_type_matches:
            node_suggestions.append(
                {
                    "target_id": m[0],
                    "target_label": m[1],
                    "reason": f"Same type '{node_type}'",
                    "score": 0.6,
                    "edge_type": "APPLIES_TO",
                }
            )
        for m in label_matches:
            node_suggestions.append(
                {
                    "target_id": m[0],
                    "target_label": m[1],
                    "reason": f"Label overlap ({m[3]} words)",
                    "score": 0.4 + 0.1 * m[3],
                    "edge_type": "APPLIES_TO",
                }
            )
        node_suggestions.sort(key=lambda s: s["score"], reverse=True)
        node_suggestions = node_suggestions[:3]

        suggestions.append(
            {
                "orphan_id": orphan_id,
                "orphan_label": label,
                "orphan_type": node_type,
                "confidence": confidence,
                "created_by": created_by,
                "suggestions": node_suggestions,
            }
        )

    has_suggestions = sum(1 for s in suggestions if s["suggestions"])
    return {
        "triaged_count": len(orphans),
        "total_orphans": total_orphans,
        "with_suggestions": has_suggestions,
        "without_suggestions": len(orphans) - has_suggestions,
        "suggestions": suggestions,
        "types_seen": types_seen,
        "method": "batch_orphan_triage",
    }


# ── Neighborhood Narrative (OHM-q9rt.1) ─────────────────────────────────────


def query_neighborhood_narrative(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    agent_name: str | None = None,
    depth: int = 2,
) -> dict[str, Any]:
    """Build a contextualized narrative for a node — "You care about X because of Y and Z".

    Walks the edges touching *node_id* and builds reasoning chains that explain
    WHY an agent should care about this node. When *agent_name* is provided,
    the narrative is personalized: it highlights edges authored by that agent
    and traces paths from the agent's claims to this node.

    Args:
        conn: Database connection.
        node_id: Target node to build the narrative for.
        agent_name: Optional agent to personalize the narrative for.
        depth: How many hops to walk (default 2 — immediate neighbors + their neighbors).

    Returns:
        Dict with:
          - node: {id, label, type, confidence}
          - why_it_matters: list of reasoning chains, each:
              {path: [{node_id, label, type}, ...], edges: [{edge_type, confidence, layer, created_by}], summary: str}
          - evidence: observations on this node and its immediate neighbors
          - connections_summary: human-readable string
          - agent_context: when agent_name is set, the agent's edges touching this node
    """
    from ohm.validation import validate_identifier, validate_depth
    from ohm.graph.decay import confidence_at

    node_id = validate_identifier(node_id, name="node_id")
    depth = validate_depth(depth)

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence, created_by FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_info = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
        "created_by": node_row[4],
    }

    # Fetch immediate neighbors (1-hop edges touching this node)
    edges = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_by, e.created_at,
                  nf.label AS from_label, nf.type AS from_type,
                  nt.label AS to_label, nt.type AS to_type
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node AND nf.deleted_at IS NULL
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node AND nt.deleted_at IS NULL
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.deleted_at IS NULL
             AND e.layer != 'L0'
           ORDER BY e.confidence DESC
           LIMIT 50""",
        [node_id, node_id],
    ).fetchall()

    # Build reasoning chains
    why_it_matters: list[dict[str, Any]] = []
    agent_edges: list[dict[str, Any]] = []

    for row in edges:
        eid, from_node, to_node, edge_type, layer, conf, created_by, created_at, from_label, from_type, to_label, to_type = row

        # Determine the "other" node (the one that's not the target)
        if from_node == node_id:
            other_id, other_label, other_type = to_node, to_label, to_type
            direction = "outgoing"
        else:
            other_id, other_label, other_type = from_node, from_label, from_type
            direction = "incoming"

        edge_info = {
            "edge_id": eid,
            "edge_type": edge_type,
            "layer": layer,
            "confidence": conf,
            "created_by": created_by,
            "direction": direction,
        }

        chain = {
            "path": [
                {"node_id": other_id, "label": other_label, "type": other_type},
                {"node_id": node_id, "label": node_info["label"], "type": node_info["type"]},
            ],
            "edges": [edge_info],
            "summary": f"{other_label} {edge_type} {node_info['label']}",
        }
        why_it_matters.append(chain)

        if agent_name and created_by == agent_name:
            agent_edges.append(
                {
                    "edge_id": eid,
                    "other_node": {"id": other_id, "label": other_label, "type": other_type},
                    "edge_type": edge_type,
                    "confidence": conf,
                    "direction": direction,
                }
            )

    # Fetch observations on this node and its immediate neighbors
    neighbor_ids = [node_id]
    for row in edges:
        other = row[1] if row[2] == node_id else row[2]
        if other and other not in neighbor_ids:
            neighbor_ids.append(other)

    # Limit to avoid huge queries
    neighbor_ids = neighbor_ids[:20]
    placeholders = ",".join(["?"] * len(neighbor_ids))
    obs_rows = conn.execute(
        f"""SELECT o.id, o.node_id, o.type, o.value, o.baseline, o.created_by,
                   o.created_at, n.label AS node_label
            FROM ohm_observations o
            LEFT JOIN ohm_nodes n ON n.id = o.node_id
            WHERE o.node_id IN ({placeholders})
              AND o.deleted_at IS NULL
            ORDER BY o.created_at DESC
            LIMIT 50""",
        neighbor_ids,
    ).fetchall()

    evidence = []
    for row in obs_rows:
        obs = {
            "obs_id": row[0],
            "node_id": row[1],
            "obs_type": row[2],
            "value": row[3],
            "baseline": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "node_label": row[7],
        }
        evidence.append(obs)

    # Build connections summary
    edge_types = [c["edges"][0]["edge_type"] for c in why_it_matters]
    if not edge_types:
        summary = f"{node_info['label']} has no connections yet."
    elif len(edge_types) == 1:
        summary = f"You care about {node_info['label']} because it {edge_types[0]} something."
    else:
        type_counts: dict[str, int] = {}
        for et in edge_types:
            type_counts[et] = type_counts.get(et, 0) + 1
        parts = [f"{count} {et}" for et, count in sorted(type_counts.items(), key=lambda x: -x[1])]
        summary = f"{node_info['label']} is connected via {', '.join(parts)}."

    result: dict[str, Any] = {
        "node": node_info,
        "why_it_matters": why_it_matters,
        "evidence": evidence,
        "connections_summary": summary,
        "connection_count": len(why_it_matters),
        "evidence_count": len(evidence),
    }

    if agent_name:
        result["agent_context"] = {
            "agent": agent_name,
            "my_edges": agent_edges,
            "my_edge_count": len(agent_edges),
        }

    return result


# ── Claim Lineage (OHM-q9rt.2) ──────────────────────────────────────────────


def query_claim_lineage(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    max_depth: int = 10,
) -> dict[str, Any]:
    """Explode a synthesis/pattern/decision node into its supporting evidence chain.

    Traces backward through provenance edges (DERIVES_FROM, REFERENCES,
    INFLUENCES, SUPPORTS, SUPPORTS_EVIDENCE, TESTS) to find all supporting
    observations and source nodes. Returns a tree structure with confidence
    products, gap detection (nodes with no observations), and source leaves.

    Args:
        conn: Database connection.
        node_id: The claim/synthesis/pattern node to trace from.
        max_depth: Maximum chain depth (default 10).

    Returns:
        Dict with:
          - claim: the target node {id, label, type, confidence}
          - lineage: tree of supporting nodes, each with:
              {node_id, label, type, depth, edge_type, edge_confidence,
               confidence_chain, observations, children: [...]}
          - sources: list of source nodes at the leaves (type='source')
          - gaps: nodes in the chain with NO observations (weak links)
          - max_confidence: highest confidence_chain across all leaves
          - min_confidence: lowest confidence_chain (weakest link)
    """
    from ohm.validation import validate_identifier, validate_depth
    from ohm.exceptions import NodeNotFoundError

    node_id = validate_identifier(node_id, name="node_id")
    max_depth = validate_depth(max_depth)

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    claim = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
    }

    # Walk provenance edges (L2 + L3 evidence edges)
    provenance_types = (
        "DERIVES_FROM",
        "REFERENCES",
        "INFLUENCES",
        "SUPPORTS",
        "SUPPORTS_EVIDENCE",
        "CONTRADICTS_EVIDENCE",
        "TESTS",
    )
    placeholders = ",".join(["?"] * len(provenance_types))

    chain_rows = conn.execute(
        f"""
        WITH RECURSIVE lineage AS (
            SELECT
                ? AS node_id,
                '' AS from_node,
                '' AS edge_id,
                '' AS edge_type,
                1.0 AS conf_product,
                0 AS depth,
                []::VARCHAR[] AS path
            UNION ALL
            SELECT
                e.to_node AS node_id,
                e.from_node AS from_node,
                e.id AS edge_id,
                e.edge_type AS edge_type,
                lc.conf_product * COALESCE(e.confidence, 1.0) AS conf_product,
                lc.depth + 1 AS depth,
                array_append(lc.path, e.id) AS path
            FROM lineage lc
            JOIN ohm_edges e ON e.from_node = lc.node_id
            WHERE lc.depth < ?
              AND e.edge_type IN ({placeholders})
              AND e.deleted_at IS NULL
              AND NOT array_contains(lc.path, e.id)
        )
        SELECT
            lc.node_id,
            lc.from_node,
            lc.edge_id,
            lc.edge_type,
            ROUND(lc.conf_product, 6) AS confidence_chain,
            lc.depth,
            n.label AS node_label,
            n.type AS node_type,
            n.confidence AS node_confidence,
            n.created_by AS node_created_by
        FROM lineage lc
        LEFT JOIN ohm_nodes n ON n.id = lc.node_id AND n.deleted_at IS NULL
        WHERE lc.depth > 0
        ORDER BY lc.depth, lc.conf_product DESC
        """,
        [node_id, max_depth, *provenance_types],
    ).fetchall()

    # Fetch observations for all nodes in the chain (plus the claim itself)
    all_node_ids = {node_id}
    for row in chain_rows:
        if row[0]:
            all_node_ids.add(row[0])

    obs_map: dict[str, list[dict[str, Any]]] = {}
    if all_node_ids:
        obs_placeholders = ",".join(["?"] * len(all_node_ids))
        obs_rows = conn.execute(
            f"""SELECT o.id, o.node_id, o.type, o.value, o.baseline,
                      o.created_by, o.created_at, o.source
               FROM ohm_observations o
               WHERE o.node_id IN ({obs_placeholders})
                 AND o.deleted_at IS NULL
               ORDER BY o.created_at DESC""",
            list(all_node_ids),
        ).fetchall()
        for row in obs_rows:
            nid = row[1]
            if nid not in obs_map:
                obs_map[nid] = []
            obs_map[nid].append(
                {
                    "obs_id": row[0],
                    "obs_type": row[2],
                    "value": row[3],
                    "baseline": row[4],
                    "created_by": row[5],
                    "created_at": str(row[6]) if row[6] else None,
                    "source": row[7],
                }
            )

    # Build tree: each chain row is a parent→child relationship
    # from_node is the parent (closer to claim), node_id is the child (further)
    tree_nodes: dict[str, dict[str, Any]] = {}

    def _get_tree_node(nid: str, label: str, ntype: str, depth: int, edge_type: str, edge_conf: float, conf_chain: float, created_by: str) -> dict[str, Any]:
        if nid not in tree_nodes:
            tree_nodes[nid] = {
                "node_id": nid,
                "label": label,
                "type": ntype,
                "depth": depth,
                "edge_type": edge_type,
                "edge_confidence": edge_conf,
                "confidence_chain": conf_chain,
                "created_by": created_by,
                "observations": obs_map.get(nid, []),
                "children": [],
            }
        return tree_nodes[nid]

    sources: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    all_confidences: list[float] = []

    for row in chain_rows:
        child_id, parent_id, edge_id, edge_type, conf_chain, depth, child_label, child_type, child_conf, child_created_by = row

        if not child_id:
            continue

        child_node = _get_tree_node(
            child_id,
            child_label or child_id,
            child_type or "unknown",
            depth,
            edge_type,
            child_conf or 1.0,
            conf_chain,
            child_created_by or "unknown",
        )

        # Track source nodes (leaves)
        if child_type == "source":
            sources.append(
                {
                    "node_id": child_id,
                    "label": child_label,
                    "depth": depth,
                    "confidence_chain": conf_chain,
                }
            )

        # Track gaps (nodes with no observations)
        if not obs_map.get(child_id):
            gaps.append(
                {
                    "node_id": child_id,
                    "label": child_label,
                    "type": child_type,
                    "depth": depth,
                    "edge_type": edge_type,
                }
            )

        all_confidences.append(conf_chain)

        # Attach to parent in tree
        if parent_id and parent_id in tree_nodes:
            tree_nodes[parent_id]["children"].append(child_node)

    # Also add claim's own observations to the tree root
    claim_obs = obs_map.get(node_id, [])

    return {
        "claim": claim,
        "claim_observations": claim_obs,
        "lineage": list(tree_nodes.values()),
        "sources": sources,
        "gaps": gaps,
        "max_confidence": max(all_confidences) if all_confidences else None,
        "min_confidence": min(all_confidences) if all_confidences else None,
        "chain_depth": max((r[5] for r in chain_rows), default=0),
        "total_nodes": len(tree_nodes),
        "total_sources": len(sources),
        "total_gaps": len(gaps),
    }


# ── Contradiction Summary (OHM-q9rt.3) ──────────────────────────────────────


def query_contradiction_summary(
    conn: DuckDBPyConnection,
    node_id: str,
) -> dict[str, Any]:
    """Build a structured summary of contradictions involving a node (OHM-q9rt.3).

    Given a node with contradictory observations or challenged edges, returns
    a "both sides" view: groups of conflicting observations, their supporting
    agents, effective confidence (with decay), existing reconciliation attempts
    (challenges/NEGATES), and a recommendation for which side has stronger evidence.

    Args:
        conn: Database connection.
        node_id: The node to analyze for contradictions.

    Returns:
        Dict with:
          - node: {id, label, type, confidence}
          - sides: list of conflicting observation groups, each with:
              {agent, observations, effective_confidence, supporting_edges}
          - challenges: CHALLENGED_BY edges targeting edges that touch this node
          - recommendation: which side has stronger evidence (or 'unresolved')
          - has_contradiction: bool
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError
    from ohm.graph.decay import confidence_at

    node_id = validate_identifier(node_id, name="node_id")

    # Fetch the target node
    node_row = conn.execute(
        "SELECT id, label, type, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [node_id],
    ).fetchone()
    if not node_row:
        raise NodeNotFoundError(f"Node not found: {node_id}")

    node_info = {
        "id": node_row[0],
        "label": node_row[1],
        "type": node_row[2],
        "confidence": node_row[3],
    }

    # 1. Find opposing observations on this node (different agents, opposite directions from baseline)
    obs_rows = conn.execute(
        """SELECT o.id, o.type, o.value, o.baseline, o.sigma, o.created_by,
                  o.created_at, o.source, o.half_life_days, o.weibull_shape,
                  o.valid_from, o.valid_to
           FROM ohm_observations o
           WHERE o.node_id = ?
             AND o.deleted_at IS NULL
             AND o.value IS NOT NULL
           ORDER BY o.created_at DESC
           LIMIT 100""",
        [node_id],
    ).fetchall()

    # Group observations by direction relative to baseline
    above: list[dict[str, Any]] = []
    below: list[dict[str, Any]] = []
    neutral: list[dict[str, Any]] = []

    for row in obs_rows:
        obs = {
            "obs_id": row[0],
            "obs_type": row[1],
            "value": row[2],
            "baseline": row[3],
            "sigma": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "source": row[7],
            "effective_confidence": round(
                confidence_at(
                    {
                        "value": row[2],
                        "half_life_days": row[8],
                        "weibull_shape": row[9],
                        "valid_from": row[10],
                        "valid_to": row[11],
                        "type": row[1],
                        "created_at": str(row[6]) if row[6] else None,
                    }
                ),
                4,
            ),
        }
        if row[3] is not None and row[2] is not None:
            if row[2] > row[3]:
                above.append(obs)
            elif row[2] < row[3]:
                below.append(obs)
            else:
                neutral.append(obs)
        else:
            neutral.append(obs)

    # Build sides: each side is a group of observations + the agents who made them
    sides: list[dict[str, Any]] = []

    if above:
        agents_above = list({o["created_by"] for o in above if o["created_by"]})
        avg_conf_above = sum(o["effective_confidence"] for o in above) / len(above)
        sides.append(
            {
                "direction": "above_baseline",
                "agents": agents_above,
                "observations": above,
                "effective_confidence": round(avg_conf_above, 4),
                "observation_count": len(above),
            }
        )

    if below:
        agents_below = list({o["created_by"] for o in below if o["created_by"]})
        avg_conf_below = sum(o["effective_confidence"] for o in below) / len(below)
        sides.append(
            {
                "direction": "below_baseline",
                "agents": agents_below,
                "observations": below,
                "effective_confidence": round(avg_conf_below, 4),
                "observation_count": len(below),
            }
        )

    if neutral and not above and not below:
        sides.append(
            {
                "direction": "neutral",
                "agents": list({o["created_by"] for o in neutral if o["created_by"]}),
                "observations": neutral,
                "effective_confidence": round(sum(o["effective_confidence"] for o in neutral) / len(neutral), 4) if neutral else 0.0,
                "observation_count": len(neutral),
            }
        )

    # 2. Find CHALLENGED_BY edges targeting edges that touch this node
    challenge_rows = conn.execute(
        """SELECT c.id AS challenge_id, c.challenge_of AS target_edge_id,
                  c.created_by AS challenger, c.confidence AS challenge_confidence,
                  c.provenance AS reason, c.created_at,
                  target.edge_type AS target_edge_type,
                  target.created_by AS target_author,
                  target.confidence AS target_confidence
           FROM ohm_edges c
           JOIN ohm_edges target ON target.id = c.challenge_of
           WHERE c.challenge_type = 'CHALLENGED_BY'
             AND (target.from_node = ? OR target.to_node = ?)
             AND target.deleted_at IS NULL
           ORDER BY c.confidence DESC, c.created_at DESC
           LIMIT 50""",
        [node_id, node_id],
    ).fetchall()

    challenges = [
        {
            "challenge_id": row[0],
            "target_edge_id": row[1],
            "challenger": row[2],
            "challenge_confidence": row[3],
            "reason": row[4],
            "created_at": str(row[5]) if row[5] else None,
            "target_edge_type": row[6],
            "target_author": row[7],
            "target_confidence": row[8],
        }
        for row in challenge_rows
    ]

    # 3. Recommendation: which side has stronger evidence?
    has_contradiction = len(sides) >= 2 or len(challenges) > 0
    recommendation = "no_contradiction"

    if len(sides) >= 2:
        # Compare effective confidence × observation count
        side0_weight = sides[0]["effective_confidence"] * sides[0]["observation_count"]
        side1_weight = sides[1]["effective_confidence"] * sides[1]["observation_count"]
        if side0_weight > side1_weight * 1.2:
            recommendation = f"Side '{sides[0]['direction']}' has stronger evidence (conf={sides[0]['effective_confidence']}, count={sides[0]['observation_count']})"
        elif side1_weight > side0_weight * 1.2:
            recommendation = f"Side '{sides[1]['direction']}' has stronger evidence (conf={sides[1]['effective_confidence']}, count={sides[1]['observation_count']})"
        else:
            recommendation = "unresolved — both sides have comparable evidence"

    if challenges and not has_contradiction:
        recommendation = f"{len(challenges)} challenge(s) registered — review required"

    return {
        "node": node_info,
        "sides": sides,
        "challenges": challenges,
        "recommendation": recommendation,
        "has_contradiction": has_contradiction,
        "total_observations": len(obs_rows),
        "total_challenges": len(challenges),
    }


# ── Task Context (OHM-q9rt.4) ───────────────────────────────────────────────


def query_task_context(
    conn: DuckDBPyConnection,
    task_id: str,
) -> dict[str, Any]:
    """Bundle a task node with its relevant subgraph and expected outcome (OHM-q9rt.4).

    Given a task node, returns the task with its 2-hop subgraph, rationale
    chain (decisions/observations that led to it), expected outcome, and any
    blocking tasks.

    Args:
        conn: Database connection.
        task_id: The task node ID.

    Returns:
        Dict with:
          - task: {id, label, type, status, assigned_to, expected_claim,
                   success_criteria, outcome, outcome_notes, created_by, created_at}
          - subgraph: {nodes: [...], edges: [...]} within 2 hops
          - rationale: list of reasoning chain entries (nodes connected via
            DECISION_DEPENDS_ON, DERIVES_FROM, REFERENCES edges)
          - expected_outcome: success_criteria text or a derived summary
          - blocking: list of other task nodes that block this one
          - blocked_by_count: int
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    task_id = validate_identifier(task_id, name="task_id")

    # Fetch the task node with all task-specific fields
    task_row = conn.execute(
        """SELECT id, label, type, task_status, assigned_to, expected_claim,
                  success_criteria, outcome, outcome_notes, created_by, created_at,
                  due_date, priority, confidence
           FROM ohm_nodes
           WHERE id = ? AND deleted_at IS NULL""",
        [task_id],
    ).fetchone()
    if not task_row:
        raise NodeNotFoundError(f"Task not found: {task_id}")

    task = {
        "id": task_row[0],
        "label": task_row[1],
        "type": task_row[2],
        "status": task_row[3],
        "assigned_to": task_row[4],
        "expected_claim": task_row[5],
        "success_criteria": task_row[6],
        "outcome": task_row[7],
        "outcome_notes": task_row[8],
        "created_by": task_row[9],
        "created_at": str(task_row[10]) if task_row[10] else None,
        "due_date": str(task_row[11]) if task_row[11] else None,
        "priority": task_row[12],
        "confidence": task_row[13],
    }

    # Fetch 2-hop subgraph using the existing neighborhood query
    subgraph_edges = query_neighborhood(conn, task_id, depth=2)

    # Extract unique node IDs from the subgraph edges
    subgraph_node_ids = {task_id}
    for e in subgraph_edges:
        fn = e.get("from_node")
        tn = e.get("to_node")
        if fn:
            subgraph_node_ids.add(fn)
        if tn:
            subgraph_node_ids.add(tn)

    # Fetch node info for all nodes in the subgraph
    subgraph_nodes = []
    if subgraph_node_ids:
        placeholders = ",".join(["?"] * len(subgraph_node_ids))
        node_rows = conn.execute(
            f"""SELECT id, label, type, confidence, created_by
                FROM ohm_nodes
                WHERE id IN ({placeholders}) AND deleted_at IS NULL""",
            list(subgraph_node_ids),
        ).fetchall()
        subgraph_nodes = [{"id": r[0], "label": r[1], "type": r[2], "confidence": r[3], "created_by": r[4]} for r in node_rows]

    # Rationale: trace back through DECISION_DEPENDS_ON, DERIVES_FROM,
    # REFERENCES, SUPPORTS edges to find the reasoning chain
    rationale_types = ("DECISION_DEPENDS_ON", "DERIVES_FROM", "REFERENCES", "SUPPORTS", "TESTS")
    rationale_rows = conn.execute(
        f"""SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_by,
                  nf.label AS from_label, nf.type AS from_type,
                  nt.label AS to_label, nt.type AS to_type
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node AND nf.deleted_at IS NULL
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node AND nt.deleted_at IS NULL
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND e.edge_type IN ({",".join(["?"] * len(rationale_types))})
             AND e.deleted_at IS NULL
           ORDER BY e.confidence DESC
           LIMIT 20""",
        [task_id, task_id, *rationale_types],
    ).fetchall()

    rationale = [
        {
            "edge_id": row[0],
            "from_node": {"id": row[1], "label": row[7], "type": row[8]},
            "to_node": {"id": row[2], "label": row[9], "type": row[10]},
            "edge_type": row[3],
            "layer": row[4],
            "confidence": row[5],
            "created_by": row[6],
        }
        for row in rationale_rows
    ]

    # Blocking: find other task nodes that block this one via DEPENDS_ON or
    # BLOCKED_BY edges, or tasks that this task DEPENDS_ON
    blocking_rows = conn.execute(
        """SELECT b.id, b.label, b.task_status, b.assigned_to,
                  e.edge_type, e.confidence
           FROM ohm_edges e
           JOIN ohm_nodes b ON b.id = CASE WHEN e.from_node = ? THEN e.to_node ELSE e.from_node END
           WHERE (e.from_node = ? OR e.to_node = ?)
             AND b.type = 'task'
             AND b.id != ?
             AND b.deleted_at IS NULL
             AND e.deleted_at IS NULL
             AND e.edge_type IN ('DEPENDS_ON', 'BLOCKED_BY', 'BLOCKS')
           ORDER BY b.task_status""",
        [task_id, task_id, task_id, task_id],
    ).fetchall()

    blocking = [
        {
            "task_id": row[0],
            "label": row[1],
            "status": row[2],
            "assigned_to": row[3],
            "edge_type": row[4],
            "confidence": row[5],
        }
        for row in blocking_rows
    ]

    # Expected outcome: use success_criteria if available, otherwise derive
    expected_outcome = task["success_criteria"]
    if not expected_outcome and task["expected_claim"]:
        expected_outcome = f"Verify claim: {task['expected_claim']}"

    return {
        "task": task,
        "subgraph": {
            "nodes": subgraph_nodes,
            "edges": subgraph_edges,
        },
        "rationale": rationale,
        "expected_outcome": expected_outcome,
        "blocking": blocking,
        "blocked_by_count": len(blocking),
    }


# ── Confidence Report (OHM-q9rt.5) ──────────────────────────────────────────


def query_confidence_report(
    conn: DuckDBPyConnection,
    *,
    agent_name: str,
    since: str | None = None,
) -> dict[str, Any]:
    """Per-agent confidence report — which beliefs have shifted and why (OHM-q9rt.5).

    Shows which of the agent's edges had confidence changes since a timestamp,
    with the reason for each shift. Complements /changes (what's new) by
    showing what CHANGED in the agent's existing portfolio.

    Args:
        conn: Database connection.
        agent_name: Agent whose beliefs to report on.
        since: ISO 8601 timestamp. Falls back to agent's last_sync then 30d ago.

    Returns:
        Dict with:
          - agent, since, query_timestamp
          - shifted_beliefs: edges whose confidence changed (updated_at > since),
              each with edge_id, from/to, edge_type, confidence, delta (vs
              original created_at confidence), and reason
          - new_beliefs: edges the agent created since `since`
          - stale_beliefs: agent's edges with confidence < 0.15 (approaching zero)
          - summary: counts
    """
    from ohm.validation import validate_identifier, validate_timestamp
    from datetime import datetime, timedelta, timezone

    agent_name = validate_identifier(agent_name, name="agent_name")

    # Resolve since
    since_clean = since
    if since_clean:
        since_clean = validate_timestamp(since_clean)
    else:
        try:
            row = conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                [agent_name],
            ).fetchone()
            if row and row[0]:
                since_clean = str(row[0])
        except Exception:
            pass
    if not since_clean:
        since_clean = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    now_str = None
    try:
        now_str = str(conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])
    except Exception:
        now_str = datetime.now(timezone.utc).isoformat()

    # 1. Shifted beliefs: agent's edges whose updated_at > since AND
    #    updated_at != created_at (i.e., confidence was modified after creation)
    shifted_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence AS current_confidence,
                  e.created_at, e.updated_at, e.updated_by,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.updated_at IS NOT NULL
             AND e.updated_at > ?::TIMESTAMP
             AND e.updated_at != e.created_at
           ORDER BY e.updated_at DESC
           LIMIT 100""",
        [agent_name, since_clean],
    ).fetchall()

    # Determine reason for each shift
    # Check if a CHALLENGED_BY edge was created targeting this edge since `since`
    challenge_map: dict[str, str] = {}
    if shifted_rows:
        edge_ids = [r[0] for r in shifted_rows]
        placeholders = ",".join(["?"] * len(edge_ids))
        chal_rows = conn.execute(
            f"""SELECT c.challenge_of, c.created_by, c.created_at
               FROM ohm_edges c
               WHERE c.challenge_type = 'CHALLENGED_BY'
                 AND c.challenge_of IN ({placeholders})
                 AND c.created_at > ?::TIMESTAMP
                 AND c.deleted_at IS NULL""",
            [*edge_ids, since_clean],
        ).fetchall()
        for row in chal_rows:
            challenge_map[row[0]] = f"challenged by {row[1]}"

    # Check if an outcome was recorded for the edge's source node since `since`
    outcome_map: dict[str, str] = {}
    if shifted_rows:
        outcome_rows = conn.execute(
            """SELECT DISTINCT o.claim_node
               FROM ohm_outcomes o
               WHERE o.recorded_at > ?::TIMESTAMP""",
            [since_clean],
        ).fetchall()
        outcome_nodes = {r[0] for r in outcome_rows if r[0]}
        for row in shifted_rows:
            if row[1] in outcome_nodes:
                outcome_map[row[0]] = "outcome recorded"

    shifted_beliefs = []
    for row in shifted_rows:
        eid, from_node, to_node, edge_type, layer, current_conf, created_at, updated_at, updated_by, from_label, to_label = row

        # Delta: compare current to created_at confidence (approximate)
        # We don't store old confidence, so we note the updated_by and time
        reason = challenge_map.get(eid) or outcome_map.get(eid) or "confidence updated"

        shifted_beliefs.append(
            {
                "edge_id": eid,
                "from_node": from_node,
                "from_label": from_label,
                "to_node": to_node,
                "to_label": to_label,
                "edge_type": edge_type,
                "layer": layer,
                "current_confidence": current_conf,
                "updated_at": str(updated_at) if updated_at else None,
                "updated_by": updated_by,
                "reason": reason,
            }
        )

    # 2. New beliefs: edges the agent created since `since`
    new_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.layer,
                  e.confidence, e.created_at,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.created_at > ?::TIMESTAMP
           ORDER BY e.created_at DESC
           LIMIT 100""",
        [agent_name, since_clean],
    ).fetchall()

    new_beliefs = [
        {
            "edge_id": row[0],
            "from_node": row[1],
            "from_label": row[7],
            "to_node": row[2],
            "to_label": row[8],
            "edge_type": row[3],
            "layer": row[4],
            "confidence": row[5],
            "created_at": str(row[6]) if row[6] else None,
        }
        for row in new_rows
    ]

    # 3. Stale beliefs: agent's edges with confidence < 0.15
    stale_rows = conn.execute(
        """SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
                  nf.label AS from_label, nt.label AS to_label
           FROM ohm_edges e
           LEFT JOIN ohm_nodes nf ON nf.id = e.from_node
           LEFT JOIN ohm_nodes nt ON nt.id = e.to_node
           WHERE e.created_by = ?
             AND e.deleted_at IS NULL
             AND e.confidence < 0.15
           ORDER BY e.confidence ASC
           LIMIT 50""",
        [agent_name],
    ).fetchall()

    stale_beliefs = [
        {
            "edge_id": row[0],
            "from_node": row[1],
            "from_label": row[5],
            "to_node": row[2],
            "to_label": row[6],
            "edge_type": row[3],
            "confidence": row[4],
        }
        for row in stale_rows
    ]

    return {
        "agent": agent_name,
        "since": since_clean,
        "query_timestamp": now_str,
        "shifted_beliefs": shifted_beliefs,
        "new_beliefs": new_beliefs,
        "stale_beliefs": stale_beliefs,
        "summary": {
            "shifted": len(shifted_beliefs),
            "new": len(new_beliefs),
            "stale": len(stale_beliefs),
        },
    }


# ── Scenario Engine (OHM-xagx) ──────────────────────────────────────────────


def query_counterfactual_cascade(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
    edge_overrides: dict[str, float] | None = None,
    node_interventions: dict[str, float] | None = None,
    disabled_edges: set[str] | None = None,
    disabled_nodes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Counterfactual cascade with edge overrides and node interventions (OHM-xagx).

    Like :func:`query_deterministic_cascade` but accepts modifications to the
    graph without persisting them. This enables "what if" scenario analysis:
    "What if this supplier's reliability dropped to 0.3?" or "What if we
    removed this dependency?"

    Args:
        conn: Database connection (read-only — no modifications persisted).
        node_id: Starting node for the cascade.
        failure_probability: Initial failure probability (0.0-1.0).
        max_depth: Maximum traversal depth.
        edge_overrides: Dict of ``{edge_id: new_probability}`` to use instead
            of the stored probability/confidence. The cascade uses the override
            value when traversing that edge.
        node_interventions: Dict of ``{node_id: failure_probability}`` to set
            a node's failure probability directly, bypassing its upstream
            propagation. This is the do-operator: "force this node to this
            state regardless of its inputs."
        disabled_edges: Set of edge IDs to skip (as if the edge doesn't exist).
        disabled_nodes: Set of node IDs to skip (as if the node is removed).

    Returns:
        List of dicts with node_id, node_label, node_type, failure_probability,
        depth, path, and ``intervened`` (bool) / ``overridden`` (bool) flags.
    """
    from ohm.validation import validate_identifier, validate_depth, validate_confidence

    node_id = validate_identifier(node_id, name="node_id")
    failure_probability = validate_confidence(failure_probability)
    max_depth = validate_depth(max_depth)

    edge_overrides = edge_overrides or {}
    node_interventions = node_interventions or {}
    disabled_edges = disabled_edges or set()
    disabled_nodes = disabled_nodes or set()

    # Fetch all relevant edges (same edge types as deterministic cascade)
    edge_rows = conn.execute(
        """SELECT id, from_node, to_node, edge_type, probability, confidence
           FROM ohm_edges
           WHERE edge_type IN ('CAUSES', 'EXPECTED_LIKELIHOOD', 'DEPENDS_ON', 'THREATENS')
             AND deleted_at IS NULL""",
    ).fetchall()

    # Build adjacency: from_node → list of (edge_id, to_node, effective_probability)
    adjacency: dict[str, list[tuple[str, str, float]]] = {}
    for row in edge_rows:
        eid, from_n, to_n, etype, prob, conf = row
        if eid in disabled_edges or from_n in disabled_nodes or to_n in disabled_nodes:
            continue
        eff_prob = edge_overrides.get(eid, prob if prob is not None else (conf if conf is not None else 0.5))
        if from_n not in adjacency:
            adjacency[from_n] = []
        adjacency[from_n].append((eid, to_n, eff_prob))

    # BFS cascade with override support
    results: list[dict[str, Any]] = []
    visited: dict[str, float] = {}  # node_id → best failure_probability
    queue: list[tuple[str, float, int, list[str], bool]] = [(node_id, failure_probability, 0, [node_id], False)]

    while queue:
        current, current_prob, depth, path, intervened = queue.pop(0)

        # Check if this node has a direct intervention
        if current in node_interventions:
            current_prob = node_interventions[current]
            intervened = True

        # Track best probability for this node
        if current in visited and visited[current] >= current_prob:
            continue  # Already visited with higher probability
        visited[current] = current_prob

        if depth > 0:
            # Look up node info
            node_info = conn.execute(
                "SELECT label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [current],
            ).fetchone()
            results.append(
                {
                    "node_id": current,
                    "node_label": node_info[0] if node_info else current,
                    "node_type": node_info[1] if node_info else "unknown",
                    "failure_probability": round(current_prob, 6),
                    "depth": depth,
                    "path": path,
                    "intervened": intervened,
                }
            )

        if depth >= max_depth:
            continue

        # Propagate to downstream nodes
        for eid, to_n, edge_prob in adjacency.get(current, []):
            if to_n in path:
                continue  # Cycle detection
            downstream_prob = current_prob * edge_prob
            # Skip negligible propagation UNLESS the downstream node has a
            # direct intervention (which will override the probability anyway)
            if downstream_prob <= 0.001 and to_n not in node_interventions:
                continue
            queue.append((to_n, downstream_prob, depth + 1, path + [to_n], intervened))

    # Sort by depth then probability descending
    results.sort(key=lambda r: (r["depth"], -r["failure_probability"]))
    return results


def query_compare_scenarios(
    conn: DuckDBPyConnection,
    node_id: str,
    *,
    failure_probability: float = 1.0,
    max_depth: int = 10,
    edge_overrides: dict[str, float] | None = None,
    node_interventions: dict[str, float] | None = None,
    disabled_edges: set[str] | None = None,
    disabled_nodes: set[str] | None = None,
) -> dict[str, Any]:
    """Compare baseline vs counterfactual cascade scenarios (OHM-xagx).

    Runs two cascades:
    1. **Baseline**: the current graph state (no overrides).
    2. **Counterfactual**: with the provided edge overrides, node interventions,
       disabled edges, and disabled nodes.

    Returns both results plus a delta analysis showing which nodes changed
    and by how much.

    Args:
        conn: Database connection.
        node_id: Starting node.
        failure_probability: Initial failure probability.
        max_depth: Maximum traversal depth.
        edge_overrides: Edge probability modifications.
        node_interventions: Forced node states.
        disabled_edges: Edges to remove.
        disabled_nodes: Nodes to remove.

    Returns:
        Dict with:
          - baseline: list of cascade results (unmodified)
          - counterfactual: list of cascade results (with overrides)
          - deltas: list of per-node changes (node_id, baseline_prob,
            counterfactual_prob, delta, direction)
          - summary: counts of increased/decreased/unchanged/new/removed nodes
    """
    # Run baseline
    baseline = query_counterfactual_cascade(
        conn,
        node_id,
        failure_probability=failure_probability,
        max_depth=max_depth,
    )

    # Run counterfactual
    counterfactual = query_counterfactual_cascade(
        conn,
        node_id,
        failure_probability=failure_probability,
        max_depth=max_depth,
        edge_overrides=edge_overrides,
        node_interventions=node_interventions,
        disabled_edges=disabled_edges,
        disabled_nodes=disabled_nodes,
    )

    # Build lookup maps
    baseline_map = {r["node_id"]: r["failure_probability"] for r in baseline}
    cf_map = {r["node_id"]: r["failure_probability"] for r in counterfactual}

    all_nodes = set(baseline_map.keys()) | set(cf_map.keys())

    deltas = []
    increased = 0
    decreased = 0
    unchanged = 0
    new_nodes = 0
    removed_nodes = 0

    for nid in all_nodes:
        b_prob = baseline_map.get(nid)
        c_prob = cf_map.get(nid)

        if b_prob is None and c_prob is not None:
            new_nodes += 1
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": None,
                    "counterfactual_prob": c_prob,
                    "delta": c_prob,
                    "direction": "new",
                }
            )
        elif b_prob is not None and c_prob is None:
            removed_nodes += 1
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": b_prob,
                    "counterfactual_prob": None,
                    "delta": -b_prob,
                    "direction": "removed",
                }
            )
        else:
            delta = (c_prob or 0.0) - (b_prob or 0.0)
            if abs(delta) < 0.001:
                unchanged += 1
                direction = "unchanged"
            elif delta > 0:
                increased += 1
                direction = "increased"
            else:
                decreased += 1
                direction = "decreased"
            deltas.append(
                {
                    "node_id": nid,
                    "baseline_prob": b_prob,
                    "counterfactual_prob": c_prob,
                    "delta": round(delta, 6),
                    "direction": direction,
                }
            )

    # Sort deltas by absolute delta descending
    deltas.sort(key=lambda d: abs(d["delta"]) if d["delta"] is not None else 0, reverse=True)

    return {
        "node_id": node_id,
        "baseline": baseline,
        "counterfactual": counterfactual,
        "deltas": deltas,
        "summary": {
            "total_nodes": len(all_nodes),
            "increased": increased,
            "decreased": decreased,
            "unchanged": unchanged,
            "new": new_nodes,
            "removed": removed_nodes,
        },
    }


# ── Autonomy Loop: Proposed/Executed Actions (OHM-446a) ─────────────────────


def propose_action(
    conn: DuckDBPyConnection,
    *,
    scenario_id: str,
    label: str,
    created_by: str,
    rationale: str | None = None,
    connects_to: list[str] | None = None,
) -> dict[str, Any]:
    """Propose an action linked to a scenario (OHM-446a).

    Creates an ``action`` node and links it to the scenario via a
    ``PROPOSES_ACTION`` L3 edge. The action starts in 'proposed' status
    (stored in task_status column for compatibility).

    Args:
        conn: Database connection.
        scenario_id: The scenario node that suggests this action.
        label: Human-readable action description.
        created_by: Agent proposing the action.
        rationale: Optional explanation of why this action.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The created action node record.
    """
    from ohm.validation import validate_identifier

    scenario_id = validate_identifier(scenario_id, name="scenario_id")

    # Create the action node, linked to the scenario
    all_connects = [scenario_id] + (connects_to or [])
    action = create_node(
        conn,
        label=label,
        node_type="action",
        content=rationale,
        created_by=created_by,
        connects_to=all_connects,
    )

    # Set task_status to 'proposed' (reusing the existing column)
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'proposed' WHERE id = ?",
        [action["id"]],
    )

    # Create the PROPOSES_ACTION edge: scenario → action
    create_edge(
        conn,
        from_node=scenario_id,
        to_node=action["id"],
        edge_type="PROPOSES_ACTION",
        layer="L3",
        created_by=created_by,
    )

    # Return updated record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action["id"]]))[0]


def execute_action(
    conn: DuckDBPyConnection,
    *,
    action_id: str,
    executed_by: str,
    outcome: str | None = None,
    outcome_notes: str | None = None,
) -> dict[str, Any]:
    """Mark an action as executed and record the outcome (OHM-446a).

    Sets the action's task_status to 'executed', records the outcome
    (TRUE/FALSE/AMBIGUOUS), and creates an EXECUTED_BY L4 edge from
    the action to the executing agent.

    Args:
        conn: Database connection.
        action_id: The action node to execute.
        executed_by: Agent executing the action.
        outcome: TRUE/FALSE/AMBIGUOUS/DEFERRED.
        outcome_notes: Free-text notes on the execution result.

    Returns:
        The updated action node record.
    """
    from ohm.validation import validate_identifier

    action_id = validate_identifier(action_id, name="action_id")

    # Verify the action exists
    row = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'action' AND deleted_at IS NULL",
        [action_id],
    ).fetchone()
    if not row:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Action not found: {action_id}")

    # Update the action status
    now_sql = "CURRENT_TIMESTAMP"
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'executed', outcome = ?, outcome_notes = ?, updated_at = " + now_sql + ", updated_by = ? WHERE id = ?",
        [outcome, outcome_notes, executed_by, action_id],
    )

    # Find or create the agent node for EXECUTED_BY edge
    agent_row = conn.execute(
        "SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent' AND deleted_at IS NULL LIMIT 1",
        [executed_by],
    ).fetchone()

    if agent_row:
        agent_id = agent_row[0]
    else:
        # Create a minimal agent node
        agent = create_node(
            conn,
            label=executed_by,
            node_type="agent",
            created_by=executed_by,
        )
        agent_id = agent["id"]

    # Create the EXECUTED_BY edge: action → agent
    create_edge(
        conn,
        from_node=action_id,
        to_node=agent_id,
        edge_type="EXECUTED_BY",
        layer="L4",
        created_by=executed_by,
    )

    _log_change(conn, "ohm_nodes", action_id, "EXECUTE", executed_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action_id]))[0]


def compute_confidence_with_decay(
    conn: DuckDBPyConnection,
    *,
    base_confidence: float,
    last_observed_at: datetime | str | None,
    half_life_days: float = 30.0,
    floor: float | None = 0.1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute decayed confidence based on observation age (OHM-2x2u).

    Decay model: confidence(t) = base * 2^(-age_days / half_life), optionally
    floored from below by ``floor`` (default 0.1). Pass ``floor=None`` to
    disable the floor — useful when the input is an unbounded quality score
    (e.g., model composite_score, which can be negative when error metrics
    dominate) rather than a confidence in [0, 1]. When the floor is
    disabled, ``is_stale`` is always False because there is no defined
    staleness threshold.

    Time source: by default we read ``now`` from DuckDB (CURRENT_TIMESTAMP)
    so both timestamps share the same clock + timezone. The fallback to
    ``datetime.now(timezone.utc)`` is only used when the caller passes
    ``now`` explicitly or the DB read fails.
    """
    import math
    from datetime import datetime as _dt, timezone as _tz

    if last_observed_at is None:
        return {
            "decayed_confidence": base_confidence,
            "age_days": None,
            "decay_factor": 1.0,
            "is_stale": False,
        }

    if now is None:
        # Read "now" from DuckDB so it shares the same timezone as
        # CURRENT_TIMESTAMP used by create_node's default values. Without
        # this, naive datetimes from the DB are interpreted as UTC while
        # CURRENT_TIMESTAMP carries the session TZ (e.g. EDT), causing
        # spurious ~4h "staleness" on fresh writes.
        try:
            now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        except Exception:
            now = _dt.now(_tz.utc)

    if isinstance(last_observed_at, str):
        last_observed_at = _dt.fromisoformat(last_observed_at.replace("Z", "+00:00"))
    # If the timestamp is naive, attach the same tzinfo as `now` so the
    # subtraction is TZ-correct. If `now` is also naive, treat both as UTC.
    if last_observed_at.tzinfo is None:
        ref_tz = now.tzinfo if now.tzinfo is not None else _tz.utc
        last_observed_at = last_observed_at.replace(tzinfo=ref_tz)
    if now.tzinfo is None and last_observed_at.tzinfo is not None:
        now = now.replace(tzinfo=last_observed_at.tzinfo)

    age_seconds = max(0.0, (now - last_observed_at).total_seconds())
    age_days = age_seconds / 86400.0

    if half_life_days <= 0:
        decay_factor = 1.0
    else:
        decay_factor = 2.0 ** (-age_days / half_life_days)

    raw = base_confidence * decay_factor
    if floor is not None:
        # Explicit clamp: when raw drops below floor, snap to floor exactly
        # (avoids floating-point underflow like 4.4e-16 being treated as
        # non-stale). is_stale is True iff raw was clamped (decayed == floor).
        if raw < floor:
            decayed = floor
            is_stale = True
        else:
            decayed = raw
            is_stale = False
    else:
        decayed = raw
        is_stale = False

    return {
        "decayed_confidence": round(decayed, 6),
        "age_days": round(age_days, 4),
        "decay_factor": round(decay_factor, 6),
        "is_stale": is_stale,
    }


def apply_decay_to_edges(
    conn: DuckDBPyConnection,
    *,
    half_life_days: float = 30.0,
    floor: float = 0.1,
    dry_run: bool = True,
    created_by: str | None = None,
) -> dict[str, Any]:
    """Apply decay to all edges' effective confidence based on their last observation (OHM-2x2u).

    If dry_run=True (default), returns what would change without modifying.
    If dry_run=False, UPDATE ohm_edges SET confidence = decayed_value and store
    original confidence in metadata.confidence_original.
    """
    import json
    import math

    defaults = {"L1": float("inf"), "L2": float("inf"), "L3": 90.0, "L4": 30.0}
    when_clauses = " ".join(f"WHEN '{k}' THEN {999999.0 if v == float('inf') or v <= 0 else float(v)}" for k, v in defaults.items())
    hl_case = f"CASE layer {when_clauses} ELSE 90.0 END"

    rows = conn.execute(
        f"""
        SELECT
            id, confidence, layer, created_at, metadata,
            {hl_case} AS half_life,
            GREATEST(date_diff('day', created_at, CURRENT_TIMESTAMP), 0)::DOUBLE AS age_days
        FROM ohm_edges
        WHERE deleted_at IS NULL
          AND confidence IS NOT NULL
          AND layer IN ('L3', 'L4')
        """,
    ).fetchall()

    edges_examined = len(rows)
    edges_decayed = 0
    summary: list[dict[str, Any]] = []
    total_decay_factor = 0.0

    for row in rows:
        edge_id, original_conf, layer, created_at, metadata_json, hl, age = row
        hl = float(hl)
        age = float(age)
        original_conf = float(original_conf) if original_conf is not None else original_conf
        if original_conf is None or original_conf <= 0:
            continue

        if hl <= 0 or hl >= 999999:
            continue

        decay_factor = 2.0 ** (-age / hl)
        decayed_conf = max(floor, original_conf * decay_factor)

        if decayed_conf < original_conf:
            edges_decayed += 1
            total_decay_factor += decay_factor

            entry = {
                "id": edge_id,
                "layer": layer,
                "original_confidence": round(original_conf, 6),
                "decayed_confidence": round(decayed_conf, 6),
                "decay_factor": round(decay_factor, 6),
                "age_days": round(age, 4),
            }
            summary.append(entry)

            if not dry_run:
                existing_meta = {}
                if metadata_json:
                    try:
                        existing_meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
                    except (json.JSONDecodeError, TypeError):
                        pass
                existing_meta["confidence_original"] = original_conf
                meta_str = json.dumps(existing_meta)

                conn.execute(
                    "UPDATE ohm_edges SET confidence = ?, metadata = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [round(decayed_conf, 6), meta_str, edge_id],
                )
                agent = created_by or "decay"
                _log_change(conn, "ohm_edges", edge_id, "UPDATE", agent)

    avg_decay = round(total_decay_factor / edges_decayed, 6) if edges_decayed > 0 else 1.0

    return {
        "edges_examined": edges_examined,
        "edges_decayed": edges_decayed,
        "average_decay_factor": avg_decay,
        "summary": summary[:100],
    }


def query_loop_status(
    conn: DuckDBPyConnection,
    *,
    agent_name: str | None = None,
    half_life_days: float = 30.0,
) -> dict[str, Any]:
    """Return the status of the autonomy loop — proposed/executed actions (OHM-446a).

    Summarizes the action lifecycle: how many actions are proposed,
    executing, executed, with what outcomes. Optionally filtered by agent.

    Extended with temporal section (OHM-2x2u): upcoming evaluations,
    stale feeds, compromised/stuck gates, and decay summary.

    Args:
        conn: Database connection.
        agent_name: Optional filter — only actions proposed by this agent.
        half_life_days: Half-life for confidence decay computation (default 30).

    Returns:
        Dict with:
          - proposed: list of action nodes with status 'proposed'
          - executed: list of action nodes with status 'executed'
          - summary: counts by status and outcome
          - recent_scenarios: scenario nodes linked to recent actions
          - temporal: dict with upcoming_evaluations, stale_feeds,
            compromised_gates, stuck_gates, decay_summary
    """
    conditions = ["type = 'action'", "deleted_at IS NULL"]
    params: list[Any] = []
    if agent_name:
        conditions.append("created_by = ?")
        params.append(agent_name)

    action_rows = conn.execute(
        f"""SELECT id, label, task_status, outcome, outcome_notes,
                   created_by, created_at, updated_at
           FROM ohm_nodes
           WHERE {" AND ".join(conditions)}
           ORDER BY COALESCE(updated_at, created_at) DESC
           LIMIT 100""",
        params,
    ).fetchall()

    proposed = []
    executed = []
    outcomes: dict[str, int] = {}

    for row in action_rows:
        action = {
            "id": row[0],
            "label": row[1],
            "status": row[2],
            "outcome": row[3],
            "outcome_notes": row[4],
            "created_by": row[5],
            "created_at": str(row[6]) if row[6] else None,
            "updated_at": str(row[7]) if row[7] else None,
        }
        if row[2] == "executed":
            executed.append(action)
            outcome_key = row[3] or "unrecorded"
            outcomes[outcome_key] = outcomes.get(outcome_key, 0) + 1
        else:
            proposed.append(action)

    # Fetch recent scenarios linked to actions
    scenario_rows = conn.execute(
        """SELECT DISTINCT s.id, s.label, s.created_by, s.created_at
           FROM ohm_nodes s
           JOIN ohm_edges e ON e.from_node = s.id AND e.edge_type = 'PROPOSES_ACTION'
           WHERE s.type = 'scenario' AND s.deleted_at IS NULL AND e.deleted_at IS NULL
           ORDER BY s.created_at DESC
           LIMIT 20""",
    ).fetchall()

    recent_scenarios = [
        {
            "id": row[0],
            "label": row[1],
            "created_by": row[2],
            "created_at": str(row[3]) if row[3] else None,
        }
        for row in scenario_rows
    ]

    # ── Temporal section (OHM-2x2u) ──

    # Upcoming evaluations: decision nodes with freshness thresholds
    upcoming_eval_rows = conn.execute(
        """SELECT d.id AS decision_id, d.label,
                  ft.metadata AS ft_metadata,
                  d.updated_at AS next_evaluation_due
           FROM ohm_nodes d
           JOIN ohm_edges e ON e.to_node = d.id AND e.edge_type = 'GOVERNS_FRESHNESS' AND e.deleted_at IS NULL
           JOIN ohm_nodes ft ON ft.id = e.from_node AND ft.deleted_at IS NULL
           WHERE d.type = 'decision' AND d.deleted_at IS NULL
           ORDER BY d.updated_at ASC
           LIMIT 50""",
    ).fetchall()

    upcoming_evaluations = []
    for row in upcoming_eval_rows:
        entry = {
            "decision_id": row[0],
            "label": row[1] or "",
            "next_evaluation_due": str(row[3]) if row[3] else None,
            "freshness_pressure": None,
        }
        if row[2]:
            import json as _json

            try:
                meta = _json.loads(row[2]) if isinstance(row[2], str) else row[2]
                entry["freshness_pressure"] = meta.get("max_age_seconds")
            except Exception:
                pass
        upcoming_evaluations.append(entry)

    # Stale feeds: nodes feeding decisions, ranked by decayed confidence
    # ascending (most-decayed-first = highest refresh priority). Each feed's
    # latest observation is decayed by its age so a feed that has gone silent
    # rises to the top of the actionable list.
    #
    # Subquery pattern: pick the latest non-deleted observation per feed node
    # using a window-function-free approach (DuckDB supports it, but a
    # correlated subquery is simpler and clearer here).
    stale_feed_rows = conn.execute(
        """SELECT n.id AS feed_node_id, n.label,
                  latest_o.value AS latest_value,
                  latest_o.created_at AS latest_observed_at,
                  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - COALESCE(latest_o.created_at, n.created_at)))::DOUBLE AS age_seconds
           FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL
           LEFT JOIN LATERAL (
               SELECT o.value, o.created_at
               FROM ohm_observations o
               WHERE o.node_id = n.id AND o.deleted_at IS NULL
               ORDER BY o.created_at DESC
               LIMIT 1
           ) latest_o ON true
           WHERE n.deleted_at IS NULL""",
    ).fetchall()

    # Build the list with decayed confidence; sort by it ascending.
    stale_feeds_intermediate = []
    for row in stale_feed_rows:
        feed_node_id = row[0]
        latest_value = row[2]
        latest_observed_at = row[3]
        age_seconds = row[4] if row[4] is not None else 0.0

        # Decay the latest observation's value by its age. When there is no
        # observation, treat as fully decayed (0.0) so the feed ranks high.
        if latest_value is None or latest_observed_at is None:
            decayed_confidence: float = 0.0
            decay_info = None
        else:
            decay_info = compute_confidence_with_decay(
                conn,
                base_confidence=float(latest_value),
                last_observed_at=latest_observed_at,
                half_life_days=half_life_days,
                floor=0.0,  # no floor — feeds can decay to 0
            )
            decayed_confidence = decay_info["decayed_confidence"]

        feeding_decision_rows = conn.execute(
            """SELECT DISTINCT e.to_node
               FROM ohm_edges e
               WHERE e.from_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL
               LIMIT 20""",
            [feed_node_id],
        ).fetchall()
        feeding_ids = [r[0] for r in feeding_decision_rows]
        stale_feeds_intermediate.append(
            {
                "feed_node_id": feed_node_id,
                "label": row[1] or "",
                "latest_value": float(latest_value) if latest_value is not None else None,
                "decayed_confidence": round(decayed_confidence, 6),
                "age_seconds": round(age_seconds, 2),
                "feeding_decision_ids": feeding_ids,
                "decay": decay_info,
            }
        )

    # Sort by decayed_confidence ASCENDING — most-decayed first.
    stale_feeds = sorted(
        stale_feeds_intermediate,
        key=lambda f: f["decayed_confidence"],
    )[:50]

    # Compromised gates: nodes with gate_status='compromised'
    compromised_rows = conn.execute(
        """SELECT id, label, gate_type, gate_status
           FROM ohm_nodes
           WHERE gate_status = 'compromised' AND deleted_at IS NULL
           ORDER BY updated_at DESC
           LIMIT 50""",
    ).fetchall()

    compromised_gates = [
        {
            "node_id": row[0],
            "label": row[1] or "",
            "gate_status": "compromised",
            "gate_type": row[2],
        }
        for row in compromised_rows
    ]

    # Stuck gates: nodes with gate_status='failed' AND old updated_at
    stuck_rows = conn.execute(
        """SELECT id, label, gate_type, updated_at
           FROM ohm_nodes
           WHERE gate_status = 'failed'
             AND deleted_at IS NULL
             AND updated_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
           ORDER BY updated_at ASC
           LIMIT 50""",
    ).fetchall()

    stuck_gates = [
        {
            "node_id": row[0],
            "label": row[1] or "",
            "gate_status": "failed",
            "gate_type": row[2],
            "since": str(row[3]) if row[3] else None,
        }
        for row in stuck_rows
    ]

    # Decay summary: call apply_decay_to_edges(dry_run=True)
    decay_result = apply_decay_to_edges(
        conn,
        half_life_days=half_life_days,
        floor=0.1,
        dry_run=True,
    )

    temporal = {
        "upcoming_evaluations": upcoming_evaluations,
        "stale_feeds": stale_feeds,
        "compromised_gates": compromised_gates,
        "stuck_gates": stuck_gates,
        "decay_summary": {
            "edges_examined": decay_result["edges_examined"],
            "edges_decayed": decay_result["edges_decayed"],
            "average_decay_factor": decay_result["average_decay_factor"],
        },
    }

    return {
        "proposed": proposed,
        "executed": executed,
        "recent_scenarios": recent_scenarios,
        "summary": {
            "total": len(action_rows),
            "proposed": len(proposed),
            "executed": len(executed),
            "outcomes": outcomes,
        },
        "temporal": temporal,
    }


def register_twin(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    created_by: str,
    endpoint_url: str | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Register an external domain twin as a ``twin`` node linked via EVALUATES L3 (OHM-josq).

    Creates a ``twin`` node with ``gate_type='external'`` and an EVALUATES L3
    edge from the twin to *target_node_id*. If *connects_to* is provided,
    additional EVALUATES L3 edges are created from the twin to each referenced
    node.

    Args:
        conn: Database connection.
        label: Human-readable twin name.
        target_node_id: The node this twin models.
        created_by: Agent registering the twin.
        endpoint_url: Optional URL of the external twin service.
        description: Optional description of what the twin models.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The registered twin node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    twin = create_node(
        conn,
        label=label,
        node_type="twin",
        content=description,
        created_by=created_by,
        url=endpoint_url,
        connects_to=[target_node_id] + (list(connects_to) if connects_to else []),
    )

    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'external' WHERE id = ?",
        [twin["id"]],
    )

    create_edge(
        conn,
        from_node=twin["id"],
        to_node=target_node_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=twin["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    _log_change(conn, "ohm_nodes", twin["id"], "REGISTER_TWIN", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]


def twin_predict(
    conn: DuckDBPyConnection,
    twin_id: str,
    *,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return twin prediction as edge_overrides-compatible dict (OHM-josq).

    Returns the target node + connected nodes with their current observation
    probabilities, formatted as ``{node_id: probability}`` for use with
    ``query_counterfactual_cascade``'s ``edge_overrides`` parameter.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label, url FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node, e.probability, e.confidence, n.label AS target_label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    edge_overrides: dict[str, float] = {}
    nodes: list[dict[str, Any]] = []
    for edge in eval_edges:
        prob = edge.get("probability")
        if prob is None:
            prob = edge.get("confidence", 0.5)
        edge_overrides[edge["to_node"]] = prob
        nodes.append(
            {
                "node_id": edge["to_node"],
                "label": edge.get("target_label", ""),
                "probability": prob,
            }
        )

    return {
        "twin_id": twin_id,
        "edge_overrides": edge_overrides,
        "nodes": nodes,
    }


def twin_constraints(
    conn: DuckDBPyConnection,
    twin_id: str,
) -> dict[str, Any]:
    """Return edges with constraint_expr touching twin's target, plus gate_type/gate_status (OHM-josq)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin_row = conn.execute(
        "SELECT id, label, gate_type, gate_status FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    twin_data = {
        "twin_id": twin_row[0],
        "label": twin_row[1],
        "gate_type": twin_row[2],
        "gate_status": twin_row[3],
    }

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.constraint_expr, e.edge_type, e.confidence, e.probability
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    target_ids = [e["to_node"] for e in eval_edges]

    constraints: list[dict[str, Any]] = []
    if target_ids:
        placeholders = ",".join(["?"] * len(target_ids))
        constraint_edges = _rows_to_dicts(
            conn.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.constraint_expr, e.edge_type,
                       e.confidence, e.probability, e.layer
                FROM ohm_edges e
                WHERE (e.from_node IN ({placeholders}) OR e.to_node IN ({placeholders}))
                  AND e.constraint_expr IS NOT NULL
                  AND e.deleted_at IS NULL""",
                target_ids + target_ids,
            )
        )
        constraints = constraint_edges

    return {
        "twin": twin_data,
        "evaluates_edges": eval_edges,
        "constraints": constraints,
    }


def validate_action_against_twin(
    conn: DuckDBPyConnection,
    twin_id: str,
    action_id: str,
) -> dict[str, Any]:
    """Check whether an action violates any twin constraints (OHM-josq).

    Finds the twin's target node(s) via EVALUATES edges, then checks
    constraints on edges incident to those targets against the action's
    confidence. Returns ``{valid, violations}`` where *violations* is a
    list of dicts describing each constraint that the action breaches.
    """
    import json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")
    action_id = validate_identifier(action_id, name="action_id")

    twin_row = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    action_row = conn.execute(
        "SELECT id, label, confidence FROM ohm_nodes WHERE id = ? AND type = 'action' AND deleted_at IS NULL",
        [action_id],
    ).fetchone()
    if not action_row:
        raise NodeNotFoundError(f"Action not found: {action_id}")

    action_confidence = action_row[2] if action_row[2] is not None else 0.5

    action_edge_rows = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.confidence, e.probability
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.deleted_at IS NULL""",
            [action_id],
        )
    )
    action_probability = None
    for ae in action_edge_rows:
        if ae.get("probability") is not None:
            action_probability = ae["probability"]
            break

    twin_targets = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )
    target_ids = [t["to_node"] for t in twin_targets]

    violations: list[dict[str, Any]] = []

    if target_ids:
        placeholders = ",".join(["?"] * len(target_ids))
        constraint_edges = _rows_to_dicts(
            conn.execute(
                f"""SELECT e.id, e.from_node, e.to_node, e.constraint_expr, e.confidence, e.probability
                FROM ohm_edges e
                WHERE (e.from_node IN ({placeholders}) OR e.to_node IN ({placeholders}))
                  AND e.constraint_expr IS NOT NULL
                  AND e.deleted_at IS NULL""",
                target_ids + target_ids,
            )
        )

        for ce in constraint_edges:
            expr = ce.get("constraint_expr", "")
            if not expr:
                continue
            try:
                parsed = json.loads(expr) if expr.startswith("{") else {}
            except (json.JSONDecodeError, TypeError):
                parsed = {}

            max_confidence = parsed.get("max_confidence")
            probability_range = parsed.get("probability_range")

            if max_confidence is not None and action_confidence > float(max_confidence):
                violations.append(
                    {
                        "constraint_edge_id": ce["id"],
                        "constraint_expr": expr,
                        "violation_type": "max_confidence_exceeded",
                        "action_confidence": action_confidence,
                        "max_confidence": float(max_confidence),
                    }
                )

            if probability_range is not None and action_probability is not None:
                p_min = probability_range.get("min")
                p_max = probability_range.get("max")
                if p_min is not None and action_probability < float(p_min):
                    violations.append(
                        {
                            "constraint_edge_id": ce["id"],
                            "constraint_expr": expr,
                            "violation_type": "probability_below_min",
                            "action_probability": action_probability,
                            "min_probability": float(p_min),
                        }
                    )
                if p_max is not None and action_probability > float(p_max):
                    violations.append(
                        {
                            "constraint_edge_id": ce["id"],
                            "constraint_expr": expr,
                            "violation_type": "probability_above_max",
                            "action_probability": action_probability,
                            "max_probability": float(p_max),
                        }
                    )

    return {
        "valid": len(violations) == 0,
        "violations": violations,
    }


def explain_twin(
    conn: DuckDBPyConnection,
    twin_id: str,
) -> dict[str, Any]:
    """Return human-readable explanation of what the twin models (OHM-josq)."""
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin_row = conn.execute(
        "SELECT id, label, url FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin_row:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node, n.label AS target_label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    target_node_id = eval_edges[0]["to_node"] if eval_edges else None
    target_label = eval_edges[0].get("target_label", "") if eval_edges else ""

    constraint_count = 0
    if target_node_id:
        row = conn.execute(
            """SELECT COUNT(*) FROM ohm_edges
               WHERE (from_node = ? OR to_node = ?)
                 AND constraint_expr IS NOT NULL
                 AND deleted_at IS NULL""",
            [target_node_id, target_node_id],
        ).fetchone()
        constraint_count = row[0] if row else 0

    edge_count = len(eval_edges)

    summary = f"Twin '{twin_row[1]}' models node '{target_label}'"
    if edge_count > 1:
        summary += f" and {edge_count - 1} other node(s)"
    if twin_row[2]:
        summary += f" via {twin_row[2]}"
    if constraint_count:
        summary += f" with {constraint_count} constraint(s)"

    return {
        "twin_id": twin_id,
        "label": twin_row[1],
        "target_node_id": target_node_id,
        "target_label": target_label,
        "endpoint_url": twin_row[2],
        "constraint_count": constraint_count,
        "edge_count": edge_count,
        "summary": summary,
    }


# ── Twin Template Catalog (OHM-hl61) ──────────────────────────────────────────


def create_twin_template(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    created_by: str,
    constraint_schema: dict[str, Any] | None = None,
    required_edges: list[str] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a twin_template node linked via EVALUATES L3 to target (OHM-hl61).

    A twin template is a reusable primitive that agents can instantiate to
    create twin nodes. The constraint_schema (stored as JSON in metadata)
    defines the constraints that instantiated twins must satisfy. The
    required_edges list specifies which edge types must exist on the target
    for the template to be applicable.

    Args:
        conn: Database connection.
        label: Human-readable template name.
        target_node_id: The node this template models.
        created_by: Agent creating the template.
        constraint_schema: Optional dict of constraints for instantiated twins.
        required_edges: Optional list of edge types required on the target.
        description: Optional description of what the template models.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The created twin_template node record.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    template_metadata: dict[str, Any] = {}
    if constraint_schema is not None:
        template_metadata["constraint_schema"] = constraint_schema
    if required_edges is not None:
        template_metadata["required_edges"] = required_edges

    all_connects = [target_node_id] + (list(connects_to) if connects_to else [])

    template = create_node(
        conn,
        label=label,
        node_type="twin_template",
        content=description,
        created_by=created_by,
        metadata=template_metadata if template_metadata else None,
        connects_to=all_connects,
    )

    create_edge(
        conn,
        from_node=template["id"],
        to_node=target_node_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=template["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    _log_change(conn, "ohm_nodes", template["id"], "CREATE_TWIN_TEMPLATE", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [template["id"]]))[0]


def list_twin_templates(
    conn: DuckDBPyConnection,
    *,
    target_node_id: str | None = None,
    created_by: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List twin_template nodes with optional filters (OHM-hl61).

    Args:
        conn: Database connection.
        target_node_id: Optional filter — only templates evaluating this node.
        created_by: Optional filter — only templates by this agent.
        limit: Maximum number of templates to return.

    Returns:
        List of twin_template node records.
    """
    from ohm.validation import validate_identifier

    conditions = ["n.type = 'twin_template'", "n.deleted_at IS NULL"]
    params: list[Any] = []

    if target_node_id is not None:
        target_node_id = validate_identifier(target_node_id, name="target_node_id")
        conditions.append(
            """EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES'
                  AND e.deleted_at IS NULL
            )"""
        )
        params.append(target_node_id)

    if created_by is not None:
        created_by = validate_identifier(created_by, name="created_by")
        conditions.append("n.created_by = ?")
        params.append(created_by)

    params.append(limit)

    where = " AND ".join(conditions)
    rows = _rows_to_dicts(
        conn.execute(
            f"""SELECT n.* FROM ohm_nodes n
            WHERE {where}
            ORDER BY n.created_at DESC
            LIMIT ?""",
            params,
        )
    )

    return rows


def get_twin_template(
    conn: DuckDBPyConnection,
    template_id: str,
) -> dict[str, Any]:
    """Get a twin_template node with its EVALUATES edges and metadata (OHM-hl61).

    Args:
        conn: Database connection.
        template_id: The twin_template node ID.

    Returns:
        Dict with template node, evaluates_edges, constraint_schema, required_edges.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    template_id = validate_identifier(template_id, name="template_id")

    template_row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
        [template_id],
    ).fetchone()
    if not template_row:
        raise NodeNotFoundError(f"Twin template not found: {template_id}")

    columns = [desc[0] for desc in conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [template_id]).description]
    template = dict(zip(columns, template_row))

    eval_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.confidence, e.probability,
                  n.label AS target_label, n.type AS target_type
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
            [template_id],
        )
    )

    constraint_schema = None
    required_edges = None
    metadata_raw = template.get("metadata")
    if metadata_raw:
        try:
            parsed = _json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
            constraint_schema = parsed.get("constraint_schema")
            required_edges = parsed.get("required_edges")
        except (_json.JSONDecodeError, TypeError):
            pass

    return {
        "template": template,
        "evaluates_edges": eval_edges,
        "constraint_schema": constraint_schema,
        "required_edges": required_edges,
    }


def instantiate_twin_from_template(
    conn: DuckDBPyConnection,
    *,
    template_id: str,
    target_node_id: str,
    created_by: str,
    label: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Instantiate a twin from a twin_template (OHM-hl61).

    Creates a ``twin`` node with gate_type='template' and an EVALUATES L3
    edge to target_node_id. Copies constraint_schema from the template
    metadata into the twin's metadata. If the template has required_edges,
    looks up edges of those types touching the target and creates
    corresponding EVALUATES edges from the twin to those connected nodes.

    Args:
        conn: Database connection.
        template_id: The twin_template to instantiate.
        target_node_id: The node the new twin will model.
        created_by: Agent instantiating the twin.
        label: Optional label for the twin (defaults to template label + " instance").
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The instantiated twin node record.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    template_id = validate_identifier(template_id, name="template_id")
    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    template_row = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
        [template_id],
    ).fetchone()
    if not template_row:
        raise NodeNotFoundError(f"Twin template not found: {template_id}")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    template_label = template_row[1]
    twin_label = label or f"{template_label} instance"

    twin_metadata: dict[str, Any] = {"source_template_id": template_id}
    template_metadata_raw = template_row[2]
    if template_metadata_raw:
        try:
            parsed = _json.loads(template_metadata_raw) if isinstance(template_metadata_raw, str) else template_metadata_raw
            if parsed.get("constraint_schema"):
                twin_metadata["constraint_schema"] = parsed["constraint_schema"]
        except (_json.JSONDecodeError, TypeError):
            pass

    all_connects = [target_node_id] + (list(connects_to) if connects_to else [])

    twin = create_node(
        conn,
        label=twin_label,
        node_type="twin",
        created_by=created_by,
        metadata=twin_metadata,
        connects_to=all_connects,
    )

    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'template' WHERE id = ?",
        [twin["id"]],
    )

    create_edge(
        conn,
        from_node=twin["id"],
        to_node=target_node_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != target_node_id:
                create_edge(
                    conn,
                    from_node=twin["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    if template_metadata_raw:
        try:
            parsed = _json.loads(template_metadata_raw) if isinstance(template_metadata_raw, str) else template_metadata_raw
            required = parsed.get("required_edges")
            if required and isinstance(required, list):
                for edge_type in required:
                    target_edges = _rows_to_dicts(
                        conn.execute(
                            """SELECT e.to_node FROM ohm_edges e
                           WHERE e.from_node = ? AND e.edge_type = ? AND e.deleted_at IS NULL""",
                            [target_node_id, edge_type],
                        )
                    )
                    for te in target_edges:
                        connected_node = te["to_node"]
                        already = conn.execute(
                            """SELECT 1 FROM ohm_edges
                               WHERE from_node = ? AND to_node = ? AND edge_type = 'EVALUATES'
                               AND deleted_at IS NULL""",
                            [twin["id"], connected_node],
                        ).fetchone()
                        if not already:
                            create_edge(
                                conn,
                                from_node=twin["id"],
                                to_node=connected_node,
                                edge_type="EVALUATES",
                                layer="L3",
                                created_by=created_by,
                            )
                    target_edges_in = _rows_to_dicts(
                        conn.execute(
                            """SELECT e.from_node FROM ohm_edges e
                           WHERE e.to_node = ? AND e.edge_type = ? AND e.deleted_at IS NULL""",
                            [target_node_id, edge_type],
                        )
                    )
                    for te in target_edges_in:
                        connected_node = te["from_node"]
                        already = conn.execute(
                            """SELECT 1 FROM ohm_edges
                               WHERE from_node = ? AND to_node = ? AND edge_type = 'EVALUATES'
                               AND deleted_at IS NULL""",
                            [twin["id"], connected_node],
                        ).fetchone()
                        if not already:
                            create_edge(
                                conn,
                                from_node=twin["id"],
                                to_node=connected_node,
                                edge_type="EVALUATES",
                                layer="L3",
                                created_by=created_by,
                            )
        except (_json.JSONDecodeError, TypeError):
            pass

    _log_change(conn, "ohm_nodes", twin["id"], "INSTANTIATE_TWIN", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]


# ── Twin Construction Engine (OHM-f7tl) ──────────────────────────────────────────


def _jaccard_similarity(a: str, b: str) -> float:
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def assemble_twin_for_decision(
    conn: DuckDBPyConnection,
    *,
    decision_node_id: str,
    goal: str,
    horizon: int = 7,
    preferred_template_id: str | None = None,
    preferred_model_id: str | None = None,
    created_by: str,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Assemble a decision-specific twin from templates + primitives (OHM-f7tl).

    Algorithm:
    1. Resolve decision_node_id (the decision this twin will support).
    2. Find candidate twin_templates relevant to the decision's domain
       (match on goal keywords vs template.description / template.label).
    3. If preferred_template_id provided, use it; else pick highest-ranked.
    4. Find candidate models for that template's twin (from marketplace).
    5. If preferred_model_id provided, use it; else pick highest-scoring.
    6. Instantiate the twin from the chosen template.
    7. Register the chosen model as a model_candidate linked to the new twin.
    8. Return {twin, template, model, ranking, reasoning}.

    Ranking:
    - Template relevance: keyword overlap between goal and template metadata
      (description, label, required_edges), normalized 0-1.
    - Model score: latest model_evaluation.value (a probability in [0, 1])
      decayed by observation age when apply_decay=True. Default 0.5 (neutral)
      if no observation exists. A stale evaluation loses influence vs. a fresh
      one — both raw and decayed scores are returned so callers can audit.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    decision = conn.execute(
        "SELECT id, label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_node_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    decision_label = decision[1] or ""

    template_rows = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.content, n.metadata, n.created_by
           FROM ohm_nodes n
           WHERE n.type = 'twin_template' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC""",
        )
    )

    template_candidates: list[dict[str, Any]] = []
    for t in template_rows:
        desc = t.get("content") or ""
        label = t.get("label") or ""
        required_edges_raw = t.get("metadata")
        required_edges_str = ""
        if required_edges_raw:
            try:
                parsed = _json.loads(required_edges_raw) if isinstance(required_edges_raw, str) else required_edges_raw
                required_edges_str = " ".join(parsed.get("required_edges", []))
            except (_json.JSONDecodeError, TypeError):
                pass
        corpus = f"{label} {desc} {required_edges_str}"
        relevance = _jaccard_similarity(goal, corpus)
        boost = _jaccard_similarity(goal, f"{decision_label} {label}") * 0.25
        score = min(relevance + boost, 1.0)
        template_candidates.append(
            {
                "template_id": t["id"],
                "label": label,
                "relevance_score": round(score, 4),
            }
        )

    template_candidates.sort(key=lambda c: c["relevance_score"], reverse=True)

    chosen_template_id = preferred_template_id
    if chosen_template_id:
        chosen_template_id = validate_identifier(chosen_template_id, name="preferred_template_id")
        template_exists = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin_template' AND deleted_at IS NULL",
            [chosen_template_id],
        ).fetchone()
        if not template_exists:
            raise NodeNotFoundError(f"Preferred template not found: {chosen_template_id}")
    elif template_candidates:
        chosen_template_id = template_candidates[0]["template_id"]

    model_candidates: list[dict[str, Any]] = []
    if chosen_template_id:
        template_eval_edges = _rows_to_dicts(
            conn.execute(
                """SELECT e.to_node, n.label, n.metadata
               FROM ohm_edges e
               JOIN ohm_nodes n ON n.id = e.to_node AND n.deleted_at IS NULL
               WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL""",
                [chosen_template_id],
            )
        )
        target_ids = [ee["to_node"] for ee in template_eval_edges]
        for tid in target_ids:
            twin_nodes = _rows_to_dicts(
                conn.execute(
                    """SELECT n.id, n.label, n.metadata
                   FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
                   WHERE n.type = 'twin' AND n.deleted_at IS NULL""",
                    [tid],
                )
            )
            for twin_node in twin_nodes:
                obs_rows = _rows_to_dicts(
                    conn.execute(
                        """SELECT o.value, o.created_at
                       FROM ohm_observations o
                       WHERE o.node_id = ? AND o.type = 'model_evaluation' AND o.deleted_at IS NULL
                       ORDER BY o.created_at DESC LIMIT 1""",
                        [twin_node["id"]],
                    )
                )
                score = 0.5
                decayed_score: float | None = None
                decay_info: dict | None = None
                if obs_rows:
                    val = obs_rows[0].get("value")
                    if val is not None:
                        score = float(val)
                        if apply_decay:
                            decay_info = compute_confidence_with_decay(
                                conn,
                                base_confidence=score,
                                last_observed_at=obs_rows[0].get("created_at"),
                                half_life_days=half_life_days,
                                floor=decay_floor,
                            )
                            decayed_score = decay_info["decayed_confidence"]
                if decayed_score is None:
                    decayed_score = score
                model_candidates.append(
                    {
                        "model_id": twin_node["id"],
                        "label": twin_node.get("label", ""),
                        "score": round(score, 4),
                        "decayed_score": round(decayed_score, 4) if decayed_score is not None else None,
                        "decay": decay_info,
                    }
                )

    # When apply_decay=True, rank by decayed_score (fresher observations win).
    # When False, rank by raw score (backward-compat).
    sort_key = (lambda c: c["decayed_score"] if c["decayed_score"] is not None else c["score"]) if apply_decay else (lambda c: c["score"])
    model_candidates.sort(key=sort_key, reverse=True)

    chosen_model_id = preferred_model_id
    if chosen_model_id:
        chosen_model_id = validate_identifier(chosen_model_id, name="preferred_model_id")
        model_exists = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
            [chosen_model_id],
        ).fetchone()
        if not model_exists:
            raise NodeNotFoundError(f"Preferred model not found: {chosen_model_id}")
    elif model_candidates:
        chosen_model_id = model_candidates[0]["model_id"]

    twin_result: dict[str, Any] | None = None
    template_result: dict[str, Any] | None = None
    model_result: dict[str, Any] | None = None

    if chosen_template_id:
        template_result = (
            _rows_to_dicts(
                conn.execute(
                    "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [chosen_template_id],
                )
            )[0]
            if _rows_to_dicts(
                conn.execute(
                    "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                    [chosen_template_id],
                )
            )
            else None
        )

        twin_result = instantiate_twin_from_template(
            conn,
            template_id=chosen_template_id,
            target_node_id=decision_node_id,
            created_by=created_by,
            label=f"Twin for {decision_label[:50]}",
            connects_to=[decision_node_id],
        )

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
            confidence=0.7,
        )

        if chosen_model_id:
            model_result = (
                _rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [chosen_model_id],
                    )
                )[0]
                if _rows_to_dicts(
                    conn.execute(
                        "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                        [chosen_model_id],
                    )
                )
                else None
            )

            create_edge(
                conn,
                from_node=twin_result["id"],
                to_node=chosen_model_id,
                edge_type="APPLIES_TO",
                layer="L3",
                created_by=created_by,
                confidence=0.7,
            )
    else:
        twin_result = create_node(
            conn,
            label=f"Ad-hoc twin for {decision_label[:50]}",
            node_type="twin",
            content=f"Ad-hoc twin assembled for decision '{decision_label}' with goal: {goal}",
            created_by=created_by,
            connects_to=[decision_node_id],
        )

        conn.execute(
            "UPDATE ohm_nodes SET gate_type = 'ad_hoc' WHERE id = ?",
            [twin_result["id"]],
        )

        twin_result = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [twin_result["id"]],
            )
        )[0]

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="EVALUATES",
            layer="L3",
            created_by=created_by,
        )

        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
            confidence=0.5,
        )

    template_desc = ""
    if chosen_template_id:
        t_row = conn.execute(
            "SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [chosen_template_id],
        ).fetchone()
        if t_row:
            template_desc = t_row[0]

    model_desc = ""
    if chosen_model_id:
        m_row = conn.execute(
            "SELECT label FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [chosen_model_id],
        ).fetchone()
        if m_row:
            model_desc = m_row[0]

    if chosen_template_id and chosen_model_id:
        tpl_score = template_candidates[0]["relevance_score"] if template_candidates else "N/A"
        model_score = model_candidates[0]["score"] if model_candidates else "N/A"
        reasoning = f"Selected template '{template_desc}' (relevance {tpl_score}) and model '{model_desc}' (score {model_score}) for decision '{decision_label}' with goal: {goal}"
    elif chosen_template_id:
        tpl_score = template_candidates[0]["relevance_score"] if template_candidates else "N/A"
        reasoning = f"Selected template '{template_desc}' (relevance {tpl_score}) for decision '{decision_label}' with goal: {goal}. No model candidates available."
    else:
        reasoning = f"No templates available for decision '{decision_label}' with goal: {goal}. Created ad-hoc twin."

    _log_change(conn, "ohm_nodes", twin_result["id"], "ASSEMBLE_TWIN", created_by)

    return {
        "twin": twin_result,
        "template": template_result,
        "model": model_result,
        "ranking": {
            "template_candidates": template_candidates,
            "model_candidates": model_candidates,
        },
        "reasoning": reasoning,
    }


# ── Model Marketplace (OHM-75tw) ──────────────────────────────────────────────


def register_model_candidate(
    conn: DuckDBPyConnection,
    *,
    label: str,
    twin_id: str,
    created_by: str,
    model_parameters: dict[str, Any] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Register a model candidate competing for a twin (OHM-75tw).

    Creates a ``model_candidate`` node linked via EVALUATES L3 to the twin.
    If other model_candidates already compete for the same twin, creates
    COMPETES_WITH L3 edges between them.

    Args:
        conn: Database connection.
        label: Human-readable model name.
        twin_id: The twin this model competes for.
        created_by: Agent registering the model.
        model_parameters: Optional dict of model hyperparameters (stored in metadata).
        description: Optional description of the model.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The registered model_candidate node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    metadata_dict = None
    if model_parameters is not None:
        metadata_dict = {"model_parameters": model_parameters, "gate_status": "candidate"}

    candidate = create_node(
        conn,
        label=label,
        node_type="model_candidate",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[twin_id] + (list(connects_to) if connects_to else []),
    )

    create_edge(
        conn,
        from_node=candidate["id"],
        to_node=twin_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if connects_to:
        for node_id in connects_to:
            nid = validate_identifier(node_id, name="connects_to entry")
            existing = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [nid],
            ).fetchone()
            if existing and nid != twin_id:
                create_edge(
                    conn,
                    from_node=candidate["id"],
                    to_node=nid,
                    edge_type="EVALUATES",
                    layer="L3",
                    created_by=created_by,
                )

    existing_candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
            [twin_id, candidate["id"]],
        )
    )
    for ec in existing_candidates:
        create_edge(
            conn,
            from_node=candidate["id"],
            to_node=ec["id"],
            edge_type="COMPETES_WITH",
            layer="L3",
            created_by=created_by,
        )
        create_edge(
            conn,
            from_node=ec["id"],
            to_node=candidate["id"],
            edge_type="COMPETES_WITH",
            layer="L3",
            created_by=created_by,
        )

    _log_change(conn, "ohm_nodes", candidate["id"], "REGISTER_MODEL_CANDIDATE", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [candidate["id"]]))[0]


def evaluate_model(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    created_by: str,
    metrics: dict[str, float],
    dataset: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Evaluate a model candidate and store metrics (OHM-75tw).

    Creates a ``model_evaluation`` node linked via EVALUATED_BY L3 to the
    model candidate. Metrics (MAE, RMSE, log_loss, accuracy, etc.) are
    stored in the metadata column as JSON.

    Args:
        conn: Database connection.
        model_candidate_id: The model candidate being evaluated.
        created_by: Agent performing the evaluation.
        metrics: Dict of metric name → score (e.g., {"mae": 0.12, "rmse": 0.18}).
        dataset: Optional name of the evaluation dataset.
        description: Optional description of the evaluation.

    Returns:
        The created model_evaluation node record.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    model_candidate_id = validate_identifier(model_candidate_id, name="model_candidate_id")

    candidate = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_candidate_id],
    ).fetchone()
    if not candidate:
        raise NodeNotFoundError(f"Model candidate not found: {model_candidate_id}")

    if not metrics:
        raise ValidationError("metrics dict must not be empty")

    composite_score = 0.0
    weight_sum = 0.0
    metric_weights = {"accuracy": 1.0, "rmse": -1.0, "mae": -1.0, "log_loss": -1.0}
    for metric_name, value in metrics.items():
        weight = metric_weights.get(metric_name, 0.5)
        composite_score += weight * value
        weight_sum += abs(weight)
    if weight_sum > 0:
        composite_score = composite_score / weight_sum
    else:
        composite_score = 0.5

    metadata_dict: dict[str, Any] = {
        "metrics": metrics,
        "composite_score": round(composite_score, 6),
    }
    if dataset:
        metadata_dict["dataset"] = dataset

    eval_label = f"Evaluation of {model_candidate_id}"
    if dataset:
        eval_label = f"Evaluation on {dataset}"

    evaluation = create_node(
        conn,
        label=eval_label,
        node_type="model_evaluation",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[model_candidate_id],
    )

    create_edge(
        conn,
        from_node=model_candidate_id,
        to_node=evaluation["id"],
        edge_type="EVALUATED_BY",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", evaluation["id"], "EVALUATE_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [evaluation["id"]]))[0]


def compare_models(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float | None = None,
) -> dict[str, Any]:
    """Compare all model candidates competing for a twin (OHM-75tw).

    Returns a ranked list of model candidates with their latest evaluation
    metrics and composite scores. When ``apply_decay`` is True (default),
    each candidate's ``composite_score`` is decayed by the age of its latest
    evaluation before ranking — a stale evaluation loses influence vs. a
    fresh one. Both raw and decayed scores are returned so callers can audit.

    Args:
        conn: Database connection.
        twin_id: The twin whose competing models to compare.
        apply_decay: When True (default), decay each candidate's composite
            score by the age of its latest evaluation before sorting.
        half_life_days: Confidence half-life used for decay (default 30).
        decay_floor: Lower bound on decayed score (default 0.1).

    Returns:
        Dict with twin_id, candidates (ranked list), and recommendation.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata, n.created_at
           FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC""",
            [twin_id],
        )
    )

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        meta_raw = c.get("metadata")
        model_params = None
        gate_status = "candidate"
        if meta_raw:
            try:
                parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                model_params = parsed.get("model_parameters")
                gate_status = parsed.get("gate_status", "candidate")
            except (_json.JSONDecodeError, TypeError):
                pass

        eval_nodes = _rows_to_dicts(
            conn.execute(
                """SELECT n.id, n.label, n.metadata, n.created_at
               FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
               ORDER BY n.created_at DESC LIMIT 1""",
                [c["id"]],
            )
        )

        latest_eval = None
        composite_score = None
        decayed_composite_score = None
        decay_info = None
        metrics = None
        dataset = None
        if eval_nodes:
            ev = eval_nodes[0]
            ev_meta_raw = ev.get("metadata")
            if ev_meta_raw:
                try:
                    ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                    metrics = ev_parsed.get("metrics")
                    composite_score = ev_parsed.get("composite_score")
                    dataset = ev_parsed.get("dataset")
                except (_json.JSONDecodeError, TypeError):
                    pass
            latest_eval = {
                "evaluation_id": ev["id"],
                "label": ev.get("label", ""),
                "created_at": ev.get("created_at"),
                "metrics": metrics,
                "composite_score": composite_score,
                "dataset": dataset,
            }
            if composite_score is not None and apply_decay:
                # composite_score can be negative (error metrics weighted -1.0
                # dominate). A 0.1 floor would collapse all candidates to the
                # same value, so default to floor=None (decay is multiplicative
                # only). Callers can pass decay_floor explicitly if their
                # composite_score is always non-negative.
                decay_info = compute_confidence_with_decay(
                    conn,
                    base_confidence=composite_score,
                    last_observed_at=ev.get("created_at"),
                    half_life_days=half_life_days,
                    floor=decay_floor,
                )
                decayed_composite_score = decay_info["decayed_confidence"]
            elif composite_score is not None:
                decayed_composite_score = composite_score

        ranked.append(
            {
                "model_candidate_id": c["id"],
                "label": c.get("label", ""),
                "gate_status": gate_status,
                "model_parameters": model_params,
                "latest_evaluation": latest_eval,
                "composite_score": composite_score,
                "decayed_composite_score": decayed_composite_score,
                "decay": decay_info,
            }
        )

    sort_key = (lambda r: r["decayed_composite_score"] if r["decayed_composite_score"] is not None else float("-inf")) if apply_decay else (lambda r: r["composite_score"] if r["composite_score"] is not None else float("-inf"))
    ranked.sort(key=sort_key, reverse=True)

    recommendation = None
    if ranked:
        top = ranked[0]
        top_score = top["decayed_composite_score"] if apply_decay else top["composite_score"]
        if top_score is not None:
            recommendation = {
                "model_candidate_id": top["model_candidate_id"],
                "label": top["label"],
                "composite_score": top["composite_score"],
                "decayed_composite_score": top["decayed_composite_score"],
            }

    return {
        "twin_id": twin_id,
        "twin_label": twin[1],
        "candidates": ranked,
        "recommendation": recommendation,
        "apply_decay": apply_decay,
        "half_life_days": half_life_days if apply_decay else None,
    }


def promote_model(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    created_by: str,
    policy: str = "accuracy",
    decision_node_id: str | None = None,
    min_improvement: float = 0.0,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Promote a model candidate to active status for its twin (OHM-75tw).

    Sets the promoted candidate's gate_status to 'active' and archives all
    other competing candidates (gate_status → 'archived') for the same twin.

    Args:
        conn: Database connection.
        model_candidate_id: The model candidate to promote.
        created_by: Agent performing the promotion.
        policy: Promotion policy — "accuracy" (default) or "decision_value".
        decision_node_id: Required when policy="decision_value".
        min_improvement: Minimum decision_value improvement over active model
            (only used with policy="decision_value").

    Returns:
        The promoted model_candidate node record with promotion metadata.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    model_candidate_id = validate_identifier(model_candidate_id, name="model_candidate_id")

    valid_policies = {"accuracy", "decision_value"}
    if policy not in valid_policies:
        raise ValidationError(f"Invalid policy '{policy}' — must be one of {sorted(valid_policies)}")

    if policy == "decision_value" and not decision_node_id:
        raise ValidationError("decision_node_id is required when policy='decision_value'")

    if decision_node_id is not None:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    candidate_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_candidate_id],
    ).fetchone()
    if not candidate_row:
        raise NodeNotFoundError(f"Model candidate not found: {model_candidate_id}")

    twin_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           AND e.to_node IN (SELECT id FROM ohm_nodes WHERE type = 'twin' AND deleted_at IS NULL)""",
            [model_candidate_id],
        )
    )
    twin_ids = [e["to_node"] for e in twin_edges]

    candidate_decision_value = None
    active_model_id = None
    active_decision_value = None

    if policy == "decision_value" and decision_node_id is not None:
        candidate_dv = compute_decision_value(
            conn,
            model_id=model_candidate_id,
            decision_node_id=decision_node_id,
            utility_scale=1.0,
            apply_decay=apply_decay,
            half_life_days=half_life_days,
            decay_floor=decay_floor,
        )
        candidate_decision_value = candidate_dv["decision_value_score"]

        for twin_id in twin_ids:
            active_candidates = _rows_to_dicts(
                conn.execute(
                    """SELECT n.id, n.metadata FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
                   WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
                    [twin_id, model_candidate_id],
                )
            )
            for ac in active_candidates:
                ac_meta_raw = ac.get("metadata")
                ac_meta = {}
                if ac_meta_raw:
                    try:
                        ac_meta = _json.loads(ac_meta_raw) if isinstance(ac_meta_raw, str) else ac_meta_raw
                    except (_json.JSONDecodeError, TypeError):
                        pass
                if ac_meta.get("gate_status") == "active":
                    active_model_id = ac["id"]
                    active_dv = compute_decision_value(
                        conn,
                        model_id=ac["id"],
                        decision_node_id=decision_node_id,
                        utility_scale=1.0,
                        apply_decay=apply_decay,
                        half_life_days=half_life_days,
                        decay_floor=decay_floor,
                    )
                    active_decision_value = active_dv["decision_value_score"]
                    break
            if active_model_id:
                break

        if active_model_id is not None and active_decision_value is not None:
            if candidate_decision_value < active_decision_value + min_improvement:
                raise ValidationError(f"Candidate decision_value ({candidate_decision_value}) does not exceed active model ({active_model_id}) decision_value ({active_decision_value}) + min_improvement ({min_improvement})")

    for twin_id in twin_ids:
        other_candidates = _rows_to_dicts(
            conn.execute(
                """SELECT n.id, n.metadata FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
               WHERE n.type = 'model_candidate' AND n.id != ? AND n.deleted_at IS NULL""",
                [twin_id, model_candidate_id],
            )
        )
        for oc in other_candidates:
            oc_meta_raw = oc.get("metadata")
            oc_meta = {}
            if oc_meta_raw:
                try:
                    oc_meta = _json.loads(oc_meta_raw) if isinstance(oc_meta_raw, str) else oc_meta_raw
                except (_json.JSONDecodeError, TypeError):
                    pass
            oc_meta["gate_status"] = "archived"
            conn.execute(
                "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
                [_json.dumps(oc_meta), oc["id"]],
            )

    current_meta_raw = candidate_row[1]
    current_meta = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass
    current_meta["gate_status"] = "active"
    current_meta["promotion_policy"] = policy
    if candidate_decision_value is not None:
        current_meta["promotion_decision_value"] = candidate_decision_value
    if active_model_id is not None:
        current_meta["previous_active_id"] = active_model_id
    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_candidate_id],
    )

    _log_change(conn, "ohm_nodes", model_candidate_id, "PROMOTE_MODEL", created_by)

    result = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_candidate_id]))[0]
    result["promotion_policy"] = policy
    if candidate_decision_value is not None:
        result["promotion_decision_value"] = candidate_decision_value
    if active_model_id is not None:
        result["previous_active_id"] = active_model_id
    return result


# ── Operational Twin Models (OHM-bf45) ────────────────────────────────────────


def register_shadow_model(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    label: str,
    source_model_id: str,
    created_by: str,
    model_parameters: dict[str, Any] | None = None,
    description: str | None = None,
    connects_to: Sequence[str] | None = None,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")
    source_model_id = validate_identifier(source_model_id, name="source_model_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    source = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [source_model_id],
    ).fetchone()
    if not source:
        raise NodeNotFoundError(f"Source model not found: {source_model_id}")

    metadata_dict: dict[str, Any] = {
        "model_parameters": model_parameters or {},
        "gate_status": "shadow",
    }

    shadow = create_node(
        conn,
        label=label,
        node_type="model_candidate",
        content=description,
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[twin_id, source_model_id] + (list(connects_to) if connects_to else []),
    )

    create_edge(
        conn,
        from_node=shadow["id"],
        to_node=twin_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    create_edge(
        conn,
        from_node=shadow["id"],
        to_node=source_model_id,
        edge_type="SHADOWS",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", shadow["id"], "REGISTER_SHADOW_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [shadow["id"]]))[0]


def detect_drift(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    window_size: int = 100,
    residual_threshold: float = 0.15,
    created_by: str | None = None,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    active_models = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL""",
            [twin_id],
        )
    )

    observations = _rows_to_dicts(
        conn.execute(
            """SELECT o.id, o.node_id, o.value, o.baseline, o.created_at
           FROM ohm_observations o
           WHERE o.node_id = ? AND o.deleted_at IS NULL
           ORDER BY o.created_at DESC LIMIT ?""",
            [twin_id, window_size],
        )
    )

    drift_score = 0.0
    drift_type = "none"
    drift_details: dict[str, Any] = {}

    if observations and active_models:
        values = [o["value"] for o in observations if o.get("value") is not None]
        baselines = [o.get("baseline") for o in observations if o.get("baseline") is not None]

        if values and baselines:
            residuals = [abs(v - b) for v, b in zip(values, baselines)]
            mae = sum(residuals) / len(residuals) if residuals else 0.0
            drift_details["residual_mae"] = round(mae, 6)
            if mae > residual_threshold:
                drift_score = min(mae / (residual_threshold * 2), 1.0)
                drift_type = "residual"

        if drift_type == "none" and len(values) >= 10:
            half = len(values) // 2
            recent_mean = sum(values[:half]) / half if half > 0 else 0.0
            older_mean = sum(values[half:]) / (len(values) - half) if (len(values) - half) > 0 else 0.0
            if older_mean != 0:
                feature_shift = abs(recent_mean - older_mean) / abs(older_mean)
                drift_details["feature_shift"] = round(feature_shift, 6)
                if feature_shift > 0.5:
                    drift_score = min(feature_shift, 1.0)
                    drift_type = "feature"

        if drift_type == "none" and len(active_models) > 1:
            scores = []
            for m in active_models:
                meta_raw = m.get("metadata")
                if meta_raw:
                    try:
                        _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                        eval_nodes = _rows_to_dicts(
                            conn.execute(
                                """SELECT n.metadata FROM ohm_nodes n
                               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
                               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
                               ORDER BY n.created_at DESC LIMIT 1""",
                                [m["id"]],
                            )
                        )
                        if eval_nodes:
                            ev_meta_raw = eval_nodes[0].get("metadata")
                            if ev_meta_raw:
                                ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                                cs = ev_parsed.get("composite_score")
                                if cs is not None:
                                    scores.append(cs)
                    except (_json.JSONDecodeError, TypeError):
                        pass
            if len(scores) >= 2:
                score_variance = sum((s - sum(scores) / len(scores)) ** 2 for s in scores) / len(scores)
                drift_details["ensemble_variance"] = round(score_variance, 6)
                if score_variance > 0.1:
                    drift_score = min(score_variance * 5, 1.0)
                    drift_type = "ensemble_disagreement"

    result: dict[str, Any] = {
        "twin_id": twin_id,
        "drift_score": round(drift_score, 6),
        "drift_type": drift_type,
        "drift_details": drift_details,
        "observation_count": len(observations),
        "active_model_count": len(active_models),
    }

    if drift_score > 0.0 and created_by:
        event = create_node(
            conn,
            label=f"Drift detected on {twin_id}",
            node_type="drift_event",
            created_by=created_by,
            metadata={
                "drift_score": round(drift_score, 6),
                "drift_type": drift_type,
                "window_size": window_size,
                "residual_threshold": residual_threshold,
                "drift_details": drift_details,
            },
            connects_to=[twin_id],
        )

        create_edge(
            conn,
            from_node=twin_id,
            to_node=event["id"],
            edge_type="DRIFT_SIGNAL",
            layer="L3",
            created_by=created_by,
        )

        _log_change(conn, "ohm_nodes", event["id"], "DETECT_DRIFT", created_by)
        result["drift_event_id"] = event["id"]

    return result


def run_walk_forward_validation(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    n_splits: int = 5,
    min_train_size: int = 50,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")

    model = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    twin_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.to_node FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           AND e.to_node IN (SELECT id FROM ohm_nodes WHERE type = 'twin' AND deleted_at IS NULL)""",
            [model_id],
        )
    )
    twin_id = twin_edges[0]["to_node"] if twin_edges else None

    observations = []
    if twin_id:
        observations = _rows_to_dicts(
            conn.execute(
                """SELECT o.id, o.value, o.baseline, o.created_at
               FROM ohm_observations o
               WHERE o.node_id = ? AND o.deleted_at IS NULL
               ORDER BY o.created_at ASC""",
                [twin_id],
            )
        )

    total_obs = len(observations)
    per_split_metrics: list[dict[str, Any]] = []
    overfitting_detected = False

    if total_obs >= min_train_size + n_splits:
        split_size = max((total_obs - min_train_size) // n_splits, 1)
        for i in range(n_splits):
            train_end = min_train_size + i * split_size
            test_start = train_end
            test_end = min(train_end + split_size, total_obs)

            train_obs = observations[:train_end]
            test_obs = observations[test_start:test_end]

            [o["value"] for o in train_obs if o.get("value") is not None]
            test_values = [o["value"] for o in test_obs if o.get("value") is not None]
            test_baselines = [o.get("baseline") for o in test_obs if o.get("baseline") is not None]

            split_mae = 0.0
            if test_values and test_baselines and len(test_values) == len(test_baselines):
                residuals = [abs(v - b) for v, b in zip(test_values, test_baselines)]
                split_mae = sum(residuals) / len(residuals)

            per_split_metrics.append(
                {
                    "split": i + 1,
                    "train_size": len(train_obs),
                    "test_size": len(test_obs),
                    "mae": round(split_mae, 6),
                }
            )

    if len(per_split_metrics) >= 3:
        early_mae = sum(s["mae"] for s in per_split_metrics[: len(per_split_metrics) // 2]) / max(len(per_split_metrics) // 2, 1)
        late_mae = sum(s["mae"] for s in per_split_metrics[len(per_split_metrics) // 2 :]) / max(len(per_split_metrics) - len(per_split_metrics) // 2, 1)
        if early_mae > 0 and late_mae > early_mae * 1.5:
            overfitting_detected = True

    mean_mae = sum(s["mae"] for s in per_split_metrics) / len(per_split_metrics) if per_split_metrics else 0.0
    std_mae = (sum((s["mae"] - mean_mae) ** 2 for s in per_split_metrics) / len(per_split_metrics)) ** 0.5 if per_split_metrics else 0.0

    metadata_dict: dict[str, Any] = {
        "model_id": model_id,
        "n_splits": n_splits,
        "min_train_size": min_train_size,
        "per_split_metrics": per_split_metrics,
        "mean_mae": round(mean_mae, 6),
        "std_mae": round(std_mae, 6),
        "overfitting_detected": overfitting_detected,
        "total_observations": total_obs,
    }

    validation = create_node(
        conn,
        label=f"Walk-forward validation of {model_id}",
        node_type="validation_run",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[model_id],
    )

    create_edge(
        conn,
        from_node=model_id,
        to_node=validation["id"],
        edge_type="EVALUATED_BY",
        layer="L3",
        created_by=created_by,
    )

    _log_change(conn, "ohm_nodes", validation["id"], "WALK_FORWARD_VALIDATION", created_by)

    return {
        "validation_id": validation["id"],
        "model_id": model_id,
        "n_splits": n_splits,
        "per_split_metrics": per_split_metrics,
        "mean_mae": round(mean_mae, 6),
        "std_mae": round(std_mae, 6),
        "overfitting_detected": overfitting_detected,
        "total_observations": total_obs,
    }


def ensemble_predict(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    observation_window: int = 50,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float | None = None,
) -> dict[str, Any]:
    """Weighted prediction across competing model candidates for a twin (OHM-75tw).

    Each candidate's weight is its latest ``composite_score`` (decayed by
    observation age when ``apply_decay`` is True). Negative decayed scores
    are clamped to 0 for weighting (a model with bad accuracy should not
    get a positive vote). Both raw and decayed scores are returned in the
    per-vote breakdown so callers can audit.
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL""",
            [twin_id],
        )
    )

    votes: list[dict[str, Any]] = []
    total_weight = 0.0
    weighted_prediction = 0.0

    for c in candidates:
        meta_raw = c.get("metadata")
        gate_status = "candidate"
        composite_score = None
        if meta_raw:
            try:
                parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                gate_status = parsed.get("gate_status", "candidate")
            except (_json.JSONDecodeError, TypeError):
                pass

        eval_nodes = _rows_to_dicts(
            conn.execute(
                """SELECT n.metadata, n.created_at FROM ohm_nodes n
               JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
               WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
               ORDER BY n.created_at DESC LIMIT 1""",
                [c["id"]],
            )
        )
        decay_info = None
        if eval_nodes:
            ev_meta_raw = eval_nodes[0].get("metadata")
            if ev_meta_raw:
                try:
                    ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                    composite_score = ev_parsed.get("composite_score")
                except (_json.JSONDecodeError, TypeError):
                    pass

            if apply_decay and composite_score is not None:
                decay_info = compute_confidence_with_decay(
                    conn,
                    base_confidence=composite_score,
                    last_observed_at=eval_nodes[0].get("created_at"),
                    half_life_days=half_life_days,
                    floor=decay_floor,
                )
                decayed_score: float = decay_info["decayed_confidence"]
            else:
                decayed_score = float(composite_score) if composite_score is not None else 0.0
        else:
            decayed_score = 0.0

        # Weight = max(0, decayed) — negative scores do not vote positively.
        weight = decayed_score if decayed_score > 0 else 0.0
        votes.append(
            {
                "model_id": c["id"],
                "label": c.get("label", ""),
                "gate_status": gate_status,
                "composite_score": composite_score,
                "decayed_composite_score": round(decayed_score, 6),
                "weight": round(weight, 6),
                "decay": decay_info,
            }
        )
        total_weight += weight

    if total_weight > 0:
        for v in votes:
            v["normalized_weight"] = round(v["weight"] / total_weight, 6)
            weighted_prediction += v["decayed_composite_score"] * v["normalized_weight"]
    else:
        for v in votes:
            v["normalized_weight"] = 0.0

    disagreement = 0.0
    if len(votes) >= 2 and total_weight > 0:
        scores = [v["decayed_composite_score"] for v in votes if v["composite_score"] is not None]
        if scores:
            mean_s = sum(scores) / len(scores)
            disagreement = sum((s - mean_s) ** 2 for s in scores) / len(scores)

    return {
        "twin_id": twin_id,
        "weighted_prediction": round(weighted_prediction, 6),
        "votes": votes,
        "disagreement": round(disagreement, 6),
        "candidate_count": len(candidates),
    }


def compute_decision_value(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    decision_node_id: str,
    utility_scale: float,
    apply_decay: bool = True,
    half_life_days: float = 30.0,
    decay_floor: float = 0.1,
) -> dict[str, Any]:
    """Compute a model's decision value for a given decision (OHM-75tw).

    The decision value is a utility-weighted accuracy measure penalized by
    model cost, latency, and overfitting risk. When ``apply_decay`` is True
    (default), the model's accuracy is decayed by the age of its latest
    evaluation before scoring — a stale evaluation loses influence. Both raw
    and decayed accuracy are returned so callers can audit. ``decay_floor``
    defaults to 0.1 because accuracy is in [0, 1] (different from
    ``composite_score`` which can be negative).
    """
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")
    decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    model = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_node_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    accuracy = 0.5
    latency = 0.0
    cost = 0.0
    overfitting_risk = 0.0
    decay_info: dict | None = None

    eval_nodes = _rows_to_dicts(
        conn.execute(
            """SELECT n.metadata, n.created_at FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
           WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC LIMIT 1""",
            [model_id],
        )
    )
    if eval_nodes:
        ev_meta_raw = eval_nodes[0].get("metadata")
        if ev_meta_raw:
            try:
                ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                metrics = ev_parsed.get("metrics", {})
                accuracy = metrics.get("accuracy", 0.5)
                composite_score = ev_parsed.get("composite_score", 0.5)
                if composite_score < 0.3:
                    overfitting_risk = 0.5
            except (_json.JSONDecodeError, TypeError):
                pass

        if apply_decay:
            decay_info = compute_confidence_with_decay(
                conn,
                base_confidence=accuracy,
                last_observed_at=eval_nodes[0].get("created_at"),
                half_life_days=half_life_days,
                floor=decay_floor,
            )
            decayed_accuracy: float = decay_info["decayed_confidence"]
        else:
            decayed_accuracy = accuracy
    else:
        decayed_accuracy = accuracy

    meta_raw = model[2]
    if meta_raw:
        try:
            parsed = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            params = parsed.get("model_parameters", {})
            latency = params.get("latency", 0.0)
            cost = params.get("cost", 0.0)
            overfitting_risk = params.get("overfitting_risk", overfitting_risk)
        except (_json.JSONDecodeError, TypeError):
            pass

    decision_value_score = max(0.0, min(1.0, utility_scale * decayed_accuracy - latency * cost * overfitting_risk))

    return {
        "model_id": model_id,
        "decision_node_id": decision_node_id,
        "utility_scale": utility_scale,
        "accuracy": accuracy,
        "decayed_accuracy": round(decayed_accuracy, 6),
        "latency": latency,
        "cost": cost,
        "overfitting_risk": overfitting_risk,
        "decision_value_score": round(decision_value_score, 6),
        "decay": decay_info,
    }


def auto_retire_model(
    conn: DuckDBPyConnection,
    *,
    model_id: str,
    reason: str,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    model_id = validate_identifier(model_id, name="model_id")

    model_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_id],
    ).fetchone()
    if not model_row:
        raise NodeNotFoundError(f"Model candidate not found: {model_id}")

    current_meta_raw = model_row[1]
    current_meta: dict[str, Any] = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_meta["gate_status"] = "retired"
    current_meta["retirement_reason"] = reason
    current_meta["retired_by"] = created_by

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_id],
    )

    _log_change(conn, "ohm_nodes", model_id, "AUTO_RETIRE_MODEL", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_id]))[0]


def set_freshness_threshold(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    max_age_seconds: int,
    created_by: str,
    label: str | None = None,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    if max_age_seconds < 1:
        raise ValueError("max_age_seconds must be >= 1")

    ft_label = label or f"Freshness threshold for {decision_id}"
    metadata_dict = {"max_age_seconds": max_age_seconds}

    ft_node = create_node(
        conn,
        label=ft_label,
        node_type="freshness_threshold",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=ft_node["id"],
        to_node=decision_id,
        edge_type="GOVERNS_FRESHNESS",
        layer="L3",
        created_by=created_by,
    )

    return ft_node


def get_freshness_status(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    thresholds = conn.execute(
        """
        SELECT n.id, n.label, n.metadata
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'GOVERNS_FRESHNESS'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchall()

    threshold_records = []
    for row in thresholds:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        threshold_records.append(
            {
                "id": row[0],
                "label": row[1],
                "max_age_seconds": meta.get("max_age_seconds"),
            }
        )

    latest_obs = conn.execute(
        """
        SELECT MAX(o.created_at)
        FROM ohm_observations o
        JOIN ohm_edges e ON e.to_node = o.node_id AND e.deleted_at IS NULL
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND o.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchone()

    latest_obs_time = latest_obs[0] if latest_obs and latest_obs[0] else None

    now_result = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
    now_ts = now_result[0] if now_result else None

    age_seconds = None
    if latest_obs_time and now_ts:
        try:
            age_seconds = (now_ts - latest_obs_time).total_seconds()
        except (AttributeError, TypeError):
            age_seconds = None

    max_age = None
    if threshold_records:
        max_age = min(t["max_age_seconds"] for t in threshold_records if t["max_age_seconds"] is not None)

    freshness_pressure = None
    if age_seconds is not None and max_age is not None and max_age > 0:
        freshness_pressure = round(min(age_seconds / max_age, 1.0), 4)

    return {
        "decision_id": decision_id,
        "thresholds": threshold_records,
        "latest_observation_at": str(latest_obs_time) if latest_obs_time else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "max_age_seconds": max_age,
        "freshness_pressure": freshness_pressure,
    }


def compute_feed_investment(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    created_by: str,
    observation_cost: float = 0.5,
    label: str | None = None,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, _label, _type, utility_scale, decision_conf = decision
    utility_scale = utility_scale if utility_scale is not None else 0.5
    decision_conf = decision_conf if decision_conf is not None else 0.5

    supporting_edges = conn.execute(
        """
        SELECT e.id, e.confidence
        FROM ohm_edges e
        WHERE e.from_node = ?
          AND e.edge_type = 'DECISION_DEPENDS_ON'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        """,
        [decision_id],
    ).fetchall()

    current_confidence = decision_conf
    if supporting_edges:
        edge_confs = [row[1] for row in supporting_edges if row[1] is not None]
        if edge_confs:
            current_confidence = sum(edge_confs) / len(edge_confs)

    expected_confidence_after = min(current_confidence + 0.15, 1.0)
    voi = round(utility_scale * (current_confidence - expected_confidence_after), 4)
    voi = abs(voi)

    invest = voi > observation_cost

    metadata_dict = {
        "voi": voi,
        "observation_cost": observation_cost,
        "current_confidence": round(current_confidence, 4),
        "expected_confidence_after": round(expected_confidence_after, 4),
        "utility_scale": round(utility_scale, 4),
        "recommendation": "invest" if invest else "defer",
    }

    fi_label = label or f"Feed investment analysis for {decision_id}"

    fi_node = create_node(
        conn,
        label=fi_label,
        node_type="feed_investment",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=fi_node["id"],
        to_node=decision_id,
        edge_type="INVESTS_IN",
        layer="L3",
        created_by=created_by,
    )

    return {
        "id": fi_node["id"],
        "decision_id": decision_id,
        "voi": voi,
        "observation_cost": observation_cost,
        "current_confidence": round(current_confidence, 4),
        "expected_confidence_after": round(expected_confidence_after, 4),
        "utility_scale": round(utility_scale, 4),
        "recommendation": "invest" if invest else "defer",
    }


def recommend_mode(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, _label, _type, utility_scale, decision_conf = decision
    utility_scale = utility_scale if utility_scale is not None else 0.5

    freshness = get_freshness_status(conn, decision_id=decision_id)
    freshness_pressure = freshness.get("freshness_pressure") or 0.0

    urgency_edges = conn.execute(
        """
        SELECT e.urgency
        FROM ohm_edges e
        WHERE e.from_node = ?
          AND e.deleted_at IS NULL
          AND e.urgency IS NOT NULL
        """,
        [decision_id],
    ).fetchall()

    max_urgency = 0.0
    for row in urgency_edges:
        try:
            u = float(row[0]) if row[0] else 0.0
            max_urgency = max(max_urgency, u)
        except (ValueError, TypeError):
            pass

    if max_urgency > 0.7 and freshness_pressure < 0.3:
        mode = "real_time"
    elif freshness_pressure > 0.5 or utility_scale > 0.8:
        mode = "deliberative"
    else:
        mode = "hybrid"

    return {
        "decision_id": decision_id,
        "mode": mode,
        "urgency": round(max_urgency, 4),
        "freshness_pressure": freshness_pressure,
        "utility_scale": round(utility_scale, 4),
        "reasoning": {
            "real_time": "urgency>0.7 AND freshness_pressure<0.3",
            "deliberative": "freshness_pressure>0.5 OR utility_scale>0.8",
            "hybrid": "default when neither real_time nor deliberative conditions met",
        }[mode],
    }


def record_mode_switch(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
    from_mode: str,
    to_mode: str,
    created_by: str,
    reason: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    decision_id = validate_identifier(decision_id, name="decision_id")

    valid_modes = {"real_time", "deliberative", "hybrid"}
    if from_mode not in valid_modes:
        raise ValidationError(f"Invalid from_mode: {from_mode}. Must be one of {sorted(valid_modes)}")
    if to_mode not in valid_modes:
        raise ValidationError(f"Invalid to_mode: {to_mode}. Must be one of {sorted(valid_modes)}")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    ms_label = label or f"Mode switch {from_mode}→{to_mode} for {decision_id}"
    metadata_dict = {
        "from_mode": from_mode,
        "to_mode": to_mode,
        "reason": reason,
    }

    ms_node = create_node(
        conn,
        label=ms_label,
        node_type="mode_switch",
        created_by=created_by,
        metadata=metadata_dict,
        connects_to=[decision_id],
    )

    create_edge(
        conn,
        from_node=ms_node["id"],
        to_node=decision_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
    )

    return ms_node


def get_current_mode(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any] | None:
    """Return the most recent mode for a decision, if any.

    Used by POST /temporal/mode-switch to make ``from_mode`` optional —
    the handler derives it from the most recent prior mode_switch
    node rather than requiring the caller to round-trip GET first.

    Returns a dict with ``to_mode``, ``from_mode``, ``switch_id``,
    ``switched_at`` and ``created_by`` for the most recent switch,
    or ``None`` if the decision has no prior mode_switch.

    The "current mode" is the to_mode of the most recent
    TRANSITIONS_TO edge from a mode_switch node to this decision
    (record_mode_switch stores from_mode + to_mode in the
    mode_switch node's metadata JSON).
    """
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'decision' AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    row = conn.execute(
        """
        SELECT ms.id, ms.metadata, ms.created_at, ms.created_by
        FROM ohm_nodes ms
        JOIN ohm_edges e
          ON e.from_node = ms.id
         AND e.to_node = ?
         AND e.edge_type = 'TRANSITIONS_TO'
         AND e.deleted_at IS NULL
        WHERE ms.type = 'mode_switch'
          AND ms.deleted_at IS NULL
        ORDER BY ms.created_at DESC
        LIMIT 1
        """,
        [decision_id],
    ).fetchone()

    if not row:
        return None

    ms_id, metadata_raw, switched_at, created_by = row
    metadata = _json.loads(metadata_raw) if metadata_raw else {}
    return {
        "switch_id": ms_id,
        "from_mode": metadata.get("from_mode"),
        "to_mode": metadata.get("to_mode"),
        "switched_at": str(switched_at) if switched_at else None,
        "created_by": created_by,
    }


def temporal_decision_summary(
    conn: DuckDBPyConnection,
    *,
    decision_id: str,
) -> dict[str, Any]:
    import json as _json
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    decision_id = validate_identifier(decision_id, name="decision_id")

    decision = conn.execute(
        "SELECT id, label, type, utility_scale, confidence, current_best_action FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [decision_id],
    ).fetchone()
    if not decision:
        raise NodeNotFoundError(f"Decision node not found: {decision_id}")

    _did, label, _type, utility_scale, decision_conf, current_best_action = decision

    freshness = get_freshness_status(conn, decision_id=decision_id)
    mode = recommend_mode(conn, decision_id=decision_id)

    feed_investments = conn.execute(
        """
        SELECT n.id, n.label, n.metadata
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'INVESTS_IN'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        ORDER BY n.created_at DESC
        """,
        [decision_id],
    ).fetchall()

    fi_records = []
    for row in feed_investments:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        fi_records.append(
            {
                "id": row[0],
                "label": row[1],
                "voi": meta.get("voi"),
                "recommendation": meta.get("recommendation"),
            }
        )

    mode_switches = conn.execute(
        """
        SELECT n.id, n.label, n.metadata, n.created_at
        FROM ohm_edges e
        JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
        WHERE e.to_node = ?
          AND e.edge_type = 'TRANSITIONS_TO'
          AND e.layer = 'L3'
          AND e.deleted_at IS NULL
        ORDER BY n.created_at DESC
        """,
        [decision_id],
    ).fetchall()

    ms_records = []
    for row in mode_switches:
        meta_raw = row[2]
        meta = {}
        if meta_raw:
            try:
                meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            except (_json.JSONDecodeError, TypeError):
                pass
        ms_records.append(
            {
                "id": row[0],
                "label": row[1],
                "from_mode": meta.get("from_mode"),
                "to_mode": meta.get("to_mode"),
                "reason": meta.get("reason"),
                "created_at": str(row[3]) if row[3] else None,
            }
        )

    return {
        "decision_id": decision_id,
        "label": label,
        "utility_scale": utility_scale,
        "confidence": decision_conf,
        "current_best_action": current_best_action,
        "freshness": freshness,
        "mode": mode,
        "feed_investments": fi_records,
        "mode_switches": ms_records,
    }


# ── Twin Design Session State Machine (OHM-konq) ────────────────────────────

VALID_SESSION_STATES = frozenset(
    {
        "init",
        "discover",
        "observe",
        "propose",
        "approve",
        "instantiate",
        "calibrate",
        "operate",
        "evolve",
        "completed",
        "abandoned",
    }
)

SESSION_TRANSITIONS: dict[str, set[str]] = {
    "init": {"discover", "abandoned"},
    "discover": {"observe", "propose", "abandoned"},
    "observe": {"propose", "discover", "abandoned"},
    "propose": {"approve", "observe", "abandoned"},
    "approve": {"instantiate", "propose", "abandoned"},
    "instantiate": {"calibrate", "operate"},
    "calibrate": {"operate", "evolve"},
    "operate": {"evolve"},
    "evolve": {"propose", "abandoned"},
    "completed": set(),
    "abandoned": set(),
}


def start_twin_design_session(
    conn: DuckDBPyConnection,
    *,
    goal: str,
    context: dict[str, Any] | None = None,
    created_by: str,
    label: str | None = None,
) -> dict[str, Any]:
    from ohm.exceptions import ValidationError
    from ohm.graph.schema import generate_node_id

    if not goal or not goal.strip():
        raise ValidationError("goal is required")

    session_label = label or f"Twin design: {goal[:80]}"
    generate_node_id(session_label, node_type="twin_design_session")

    metadata: dict[str, Any] = {
        "session_state": "init",
        "goal": goal,
        "observations": [],
        "calibration_records": [],
    }
    if context:
        metadata["context"] = context

    session = create_node(
        conn,
        label=session_label,
        node_type="twin_design_session",
        content=goal,
        created_by=created_by,
        metadata=metadata,
    )

    _log_change(conn, "ohm_nodes", session["id"], "START_SESSION", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session["id"]]))[0]


def transition_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    to_state: str,
    notes: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    if to_state not in VALID_SESSION_STATES:
        raise ValidationError(f"Invalid session state: '{to_state}' — must be one of: {sorted(VALID_SESSION_STATES)}")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")

    allowed = SESSION_TRANSITIONS.get(current_state, set())
    if to_state not in allowed:
        raise ValidationError(f"Invalid transition: '{current_state}' → '{to_state}'. Allowed from '{current_state}': {sorted(allowed) if allowed else ['(terminal)']}")

    current_meta["session_state"] = to_state

    transition_record = {
        "from_state": current_state,
        "to_state": to_state,
        "notes": notes,
        "actor": created_by,
    }
    history = current_meta.get("transition_history", [])
    history.append(transition_record)
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata=transition_record,
    )

    _log_change(conn, "ohm_nodes", session_id, f"TRANSITION_{current_state}_TO_{to_state}", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0]


def add_session_observation(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    observations: dict[str, Any],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "observe":
        raise ValidationError(f"Can only add observations in 'observe' state, current state is '{current_state}'")

    obs_list = current_meta.get("observations", [])
    obs_list.append(observations)
    current_meta["observations"] = obs_list

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    _log_change(conn, "ohm_nodes", session_id, "ADD_OBSERVATION", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0]


def propose_twin_config(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    decision_node_id: str | None = None,
    preferred_template_id: str | None = None,
    preferred_model_id: str | None = None,
    confidence_threshold: float = 0.6,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, label, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[2]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "propose":
        raise ValidationError(f"Can only propose in 'propose' state, current state is '{current_state}'")

    goal = current_meta.get("goal", "")

    twin_preview = None
    ranking = []
    reasoning = ""

    if decision_node_id:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")
        try:
            twin_preview = assemble_twin_for_decision(
                conn,
                decision_node_id=decision_node_id,
                goal=goal,
                preferred_template_id=preferred_template_id,
                preferred_model_id=preferred_model_id,
                created_by=created_by,
            )
            ranking = twin_preview.get("ranking", [])
            reasoning = twin_preview.get("reasoning", "")
        except (NodeNotFoundError, ValidationError):
            twin_preview = None
            ranking = []
            reasoning = "No suitable twin configuration found"

    if ranking and ranking[0].get("score", 0) < confidence_threshold:
        raise ValidationError(f"Best proposal score {ranking[0].get('score', 0):.2f} below confidence threshold {confidence_threshold}")

    from ohm.graph.schema import generate_node_id

    proposal_label = f"Proposal for: {goal[:60]}"
    generate_node_id(proposal_label, node_type="twin_design_proposal")

    proposal_metadata: dict[str, Any] = {
        "decision_node_id": decision_node_id,
        "preferred_template_id": preferred_template_id,
        "preferred_model_id": preferred_model_id,
        "confidence_threshold": confidence_threshold,
        "ranking": ranking,
        "reasoning": reasoning,
    }

    proposal = create_node(
        conn,
        label=proposal_label,
        node_type="twin_design_proposal",
        content=reasoning,
        created_by=created_by,
        metadata=proposal_metadata,
        connects_to=[session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=proposal["id"],
        edge_type="PROPOSES",
        layer="L3",
        created_by=created_by,
    )

    current_meta["session_state"] = "approve"
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "propose", "to_state": "approve", "notes": "auto-transition after proposal", "actor": created_by})
    current_meta["transition_history"] = history
    current_meta["current_proposal_id"] = proposal["id"]

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "propose", "to_state": "approve", "notes": "auto-transition after proposal", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "PROPOSE_TWIN_CONFIG", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "proposal": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [proposal["id"]]))[0],
        "twin_preview": twin_preview,
        "ranking": ranking,
        "reasoning": reasoning,
    }


def review_proposal(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    proposal_id: str,
    decision: str,
    approved_aspects: list[str] | None = None,
    declined_aspects: list[str] | None = None,
    modifications: dict[str, Any] | None = None,
    reason: str | None = None,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")
    proposal_id = validate_identifier(proposal_id, name="proposal_id")

    if decision not in ("approve", "decline", "modify"):
        raise ValidationError(f"Invalid review decision: '{decision}' — must be 'approve', 'decline', or 'modify'")

    session_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not session_row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    proposal_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_proposal' AND deleted_at IS NULL",
        [proposal_id],
    ).fetchone()
    if not proposal_row:
        raise NodeNotFoundError(f"Twin design proposal not found: {proposal_id}")

    meta_raw = session_row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "approve":
        raise ValidationError(f"Can only review proposals in 'approve' state, current state is '{current_state}'")

    edge_type_map = {"approve": "APPROVES", "decline": "DECLINES", "modify": "MODIFIES"}
    review_meta: dict[str, Any] = {
        "decision": decision,
        "approved_aspects": approved_aspects,
        "declined_aspects": declined_aspects,
        "modifications": modifications,
        "reason": reason,
    }

    create_edge(
        conn,
        from_node=session_id,
        to_node=proposal_id,
        edge_type=edge_type_map[decision],
        layer="L3",
        created_by=created_by,
        metadata=review_meta,
    )

    proposal_meta_raw = proposal_row[1]
    proposal_meta: dict[str, Any] = {}
    if proposal_meta_raw:
        try:
            proposal_meta = _json.loads(proposal_meta_raw) if isinstance(proposal_meta_raw, str) else proposal_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    reviews = proposal_meta.get("reviews", [])
    reviews.append(review_meta)
    proposal_meta["reviews"] = reviews
    if modifications:
        proposal_meta["modifications"] = modifications

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(proposal_meta), created_by, proposal_id],
    )

    new_state: str
    if decision == "approve":
        new_state = "instantiate"
    elif decision == "decline":
        new_state = "abandoned"
    else:
        new_state = "propose"

    current_meta["session_state"] = new_state
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "approve", "to_state": new_state, "notes": f"review decision: {decision}", "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "approve", "to_state": new_state, "notes": f"review decision: {decision}", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, f"REVIEW_{decision.upper()}", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "proposal": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [proposal_id]))[0],
        "decision": decision,
        "new_state": new_state,
    }


def instantiate_from_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "instantiate":
        raise ValidationError(f"Can only instantiate in 'instantiate' state, current state is '{current_state}'")

    proposal_id = current_meta.get("current_proposal_id")
    if not proposal_id:
        raise ValidationError("No current proposal to instantiate")

    proposal_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_proposal' AND deleted_at IS NULL",
        [proposal_id],
    ).fetchone()
    if not proposal_row:
        raise NodeNotFoundError(f"Current proposal not found: {proposal_id}")

    proposal_meta_raw = proposal_row[1]
    proposal_meta: dict[str, Any] = {}
    if proposal_meta_raw:
        try:
            proposal_meta = _json.loads(proposal_meta_raw) if isinstance(proposal_meta_raw, str) else proposal_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    decision_node_id = proposal_meta.get("decision_node_id")
    preferred_template_id = proposal_meta.get("preferred_template_id")
    preferred_model_id = proposal_meta.get("preferred_model_id")

    twin_result = None
    model_result = None

    if decision_node_id and preferred_template_id:
        try:
            twin_result = instantiate_twin_from_template(
                conn,
                template_id=preferred_template_id,
                target_node_id=decision_node_id,
                created_by=created_by,
            )
        except (NodeNotFoundError, ValidationError):
            twin_result = None

    if twin_result and preferred_model_id:
        try:
            model_result = register_model_candidate(
                conn,
                label=f"Model for session {session_id[:20]}",
                twin_id=twin_result["id"],
                created_by=created_by,
            )
        except (NodeNotFoundError, ValidationError):
            model_result = None

    if twin_result:
        create_edge(
            conn,
            from_node=twin_result["id"],
            to_node=session_id,
            edge_type="INSTANTIATED_FROM",
            layer="L3",
            created_by=created_by,
        )

    new_state = "calibrate"
    current_meta["session_state"] = new_state
    current_meta["instantiated_twin_id"] = twin_result["id"] if twin_result else None
    current_meta["instantiated_model_id"] = model_result["id"] if model_result else None
    history = current_meta.get("transition_history", [])
    history.append({"from_state": "instantiate", "to_state": new_state, "notes": "twin instantiated", "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": "instantiate", "to_state": new_state, "notes": "twin instantiated", "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "INSTANTIATE_FROM_SESSION", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "twin": twin_result,
        "model_candidate": model_result,
        "calibration_plan": {"recommended_observations": 5, "drift_threshold": 0.15},
    }


def record_calibration(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    observations: dict[str, float],
    actuals: dict[str, float],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state != "calibrate":
        raise ValidationError(f"Can only record calibration in 'calibrate' state, current state is '{current_state}'")

    drift_metrics: dict[str, Any] = {}
    max_drift = 0.0
    for key in observations:
        if key in actuals:
            drift = abs(observations[key] - actuals[key])
            drift_metrics[key] = {"observed": observations[key], "actual": actuals[key], "drift": drift}
            max_drift = max(max_drift, drift)

    cal_records = current_meta.get("calibration_records", [])
    cal_record = {"observations": observations, "actuals": actuals, "drift_metrics": drift_metrics, "max_drift": max_drift}
    cal_records.append(cal_record)
    current_meta["calibration_records"] = cal_records

    DRIFT_THRESHOLD = 0.15
    MIN_CALIBRATIONS = 3

    recommended_next: str
    if max_drift > DRIFT_THRESHOLD:
        recommended_next = "evolve"
    elif len(cal_records) >= MIN_CALIBRATIONS:
        recommended_next = "operate"
    else:
        recommended_next = "calibrate"

    if recommended_next != "calibrate":
        current_meta["session_state"] = recommended_next
        history = current_meta.get("transition_history", [])
        history.append({"from_state": "calibrate", "to_state": recommended_next, "notes": f"max_drift={max_drift:.4f}", "actor": created_by})
        current_meta["transition_history"] = history

        create_edge(
            conn,
            from_node=session_id,
            to_node=session_id,
            edge_type="TRANSITIONS_TO",
            layer="L3",
            created_by=created_by,
            metadata={"from_state": "calibrate", "to_state": recommended_next, "notes": f"max_drift={max_drift:.4f}", "actor": created_by},
        )

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    _log_change(conn, "ohm_nodes", session_id, "RECORD_CALIBRATION", created_by)

    return {
        "calibration_metrics": drift_metrics,
        "max_drift": max_drift,
        "recommended_next_state": recommended_next,
        "calibration_count": len(cal_records),
    }


def evolve_session(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
    reason: str,
    proposed_changes: dict[str, Any],
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import ValidationError, NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    if not reason or not reason.strip():
        raise ValidationError("reason is required")

    row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    meta_raw = row[1]
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    if current_state not in ("evolve", "operate"):
        raise ValidationError(f"Can only evolve in 'evolve' or 'operate' state, current state is '{current_state}'")

    evolution_record = {"reason": reason, "proposed_changes": proposed_changes, "actor": created_by}
    evolutions = current_meta.get("evolution_records", [])
    evolutions.append(evolution_record)
    current_meta["evolution_records"] = evolutions

    new_state = "propose"
    current_meta["session_state"] = new_state
    history = current_meta.get("transition_history", [])
    history.append({"from_state": current_state, "to_state": new_state, "notes": reason, "actor": created_by})
    current_meta["transition_history"] = history

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
        [_json.dumps(current_meta), created_by, session_id],
    )

    create_edge(
        conn,
        from_node=session_id,
        to_node=session_id,
        edge_type="TRANSITIONS_TO",
        layer="L3",
        created_by=created_by,
        metadata={"from_state": current_state, "to_state": new_state, "notes": reason, "actor": created_by},
    )

    _log_change(conn, "ohm_nodes", session_id, "EVOLVE_SESSION", created_by)

    return {
        "session": _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [session_id]))[0],
        "evolution_record": evolution_record,
    }


def get_session_state(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    columns = [desc[0] for desc in conn.description]
    session = dict(zip(columns, row))

    meta_raw = session.get("metadata")
    current_meta: dict[str, Any] = {}
    if meta_raw:
        try:
            current_meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_state = current_meta.get("session_state", "init")
    history = current_meta.get("transition_history", [])

    proposal_edges = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'PROPOSES' AND e.deleted_at IS NULL
           ORDER BY e.created_at DESC""",
            [session_id],
        )
    )

    proposals = []
    for pe in proposal_edges:
        proposal_node = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [pe["to_node"]],
            )
        )
        if proposal_node:
            proposals.append(proposal_node[0])

    final_twin_id = current_meta.get("instantiated_twin_id")

    return {
        "session": session,
        "current_state": current_state,
        "history": history,
        "proposals": proposals,
        "final_twin_id": final_twin_id,
    }


def get_session_audit(
    conn: DuckDBPyConnection,
    *,
    session_id: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    session_id = validate_identifier(session_id, name="session_id")

    row = conn.execute(
        "SELECT * FROM ohm_nodes WHERE id = ? AND type = 'twin_design_session' AND deleted_at IS NULL",
        [session_id],
    ).fetchone()
    if not row:
        raise NodeNotFoundError(f"Twin design session not found: {session_id}")

    columns = [desc[0] for desc in conn.description]
    session = dict(zip(columns, row))

    transitions = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.to_node = ? AND e.edge_type = 'TRANSITIONS_TO' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id, session_id],
        )
    )

    proposals = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type = 'PROPOSES' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    approvals = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.to_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.from_node = ? AND e.edge_type IN ('APPROVES', 'DECLINES', 'MODIFIES') AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    instantiations = _rows_to_dicts(
        conn.execute(
            """SELECT e.id, e.from_node, e.edge_type, e.metadata, e.created_at, e.created_by
           FROM ohm_edges e
           WHERE e.to_node = ? AND e.edge_type = 'INSTANTIATED_FROM' AND e.deleted_at IS NULL
           ORDER BY e.created_at""",
            [session_id],
        )
    )

    return {
        "session": session,
        "transitions": transitions,
        "proposals": proposals,
        "approvals": approvals,
        "instantiations": instantiations,
    }


def set_promotion_policy(
    conn: DuckDBPyConnection,
    *,
    model_candidate_id: str,
    policy: str,
    decision_node_id: str | None = None,
    min_improvement: float = 0.0,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    model_candidate_id = validate_identifier(model_candidate_id, name="model_candidate_id")

    valid_policies = {"accuracy", "decision_value"}
    if policy not in valid_policies:
        raise ValidationError(f"Invalid policy '{policy}' — must be one of {sorted(valid_policies)}")

    if policy == "decision_value" and not decision_node_id:
        raise ValidationError("decision_node_id is required when policy='decision_value'")

    if decision_node_id is not None:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")

    candidate_row = conn.execute(
        "SELECT id, metadata FROM ohm_nodes WHERE id = ? AND type = 'model_candidate' AND deleted_at IS NULL",
        [model_candidate_id],
    ).fetchone()
    if not candidate_row:
        raise NodeNotFoundError(f"Model candidate not found: {model_candidate_id}")

    current_meta_raw = candidate_row[1]
    current_meta: dict[str, Any] = {}
    if current_meta_raw:
        try:
            current_meta = _json.loads(current_meta_raw) if isinstance(current_meta_raw, str) else current_meta_raw
        except (_json.JSONDecodeError, TypeError):
            pass

    current_meta["promotion_policy"] = policy
    if decision_node_id is not None:
        current_meta["decision_node_id"] = decision_node_id
    current_meta["min_improvement"] = min_improvement

    conn.execute(
        "UPDATE ohm_nodes SET metadata = ? WHERE id = ?",
        [_json.dumps(current_meta), model_candidate_id],
    )

    _log_change(conn, "ohm_nodes", model_candidate_id, "SET_PROMOTION_POLICY", created_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [model_candidate_id]))[0]


def auto_promote_best_model(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    decision_node_id: str | None = None,
    policy: str = "decision_value",
    min_improvement: float = 0.0,
    created_by: str,
) -> dict[str, Any]:
    import json as _json

    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT id, label FROM ohm_nodes WHERE id = ? AND type = 'twin' AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    candidates = _rows_to_dicts(
        conn.execute(
            """SELECT n.id, n.label, n.metadata FROM ohm_nodes n
           JOIN ohm_edges e ON e.from_node = n.id AND e.to_node = ? AND e.edge_type = 'EVALUATES' AND e.deleted_at IS NULL
           WHERE n.type = 'model_candidate' AND n.deleted_at IS NULL""",
            [twin_id],
        )
    )

    if not candidates:
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": [],
            "reason": "no_candidates",
            "detail": "No model candidates compete for this twin.",
        }

    ranked: list[dict[str, Any]] = []
    for c in candidates:
        score = None
        scoring_error: str | None = None
        if policy == "decision_value" and decision_node_id is not None:
            try:
                dv = compute_decision_value(
                    conn,
                    model_id=c["id"],
                    decision_node_id=decision_node_id,
                    utility_scale=1.0,
                )
                score = dv["decision_value_score"]
            except (NodeNotFoundError, ValueError) as exc:
                scoring_error = str(exc)
        else:
            eval_nodes = _rows_to_dicts(
                conn.execute(
                    """SELECT n.metadata FROM ohm_nodes n
                   JOIN ohm_edges e ON e.from_node = ? AND e.to_node = n.id AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL
                   WHERE n.type = 'model_evaluation' AND n.deleted_at IS NULL
                   ORDER BY n.created_at DESC LIMIT 1""",
                    [c["id"]],
                )
            )
            if eval_nodes:
                ev_meta_raw = eval_nodes[0].get("metadata")
                if ev_meta_raw:
                    try:
                        ev_parsed = _json.loads(ev_meta_raw) if isinstance(ev_meta_raw, str) else ev_meta_raw
                        score = ev_parsed.get("composite_score")
                    except (_json.JSONDecodeError, TypeError):
                        scoring_error = "failed to parse evaluation metadata"

        ranked.append(
            {
                "model_candidate_id": c["id"],
                "label": c.get("label", ""),
                "score": score,
                "scoring_error": scoring_error,
            }
        )

    # Both policy branches share the same sort key — higher score wins,
    # candidates with no score sink to the bottom. Previously duplicated
    # sort blocks masked the equivalence; now single sort handles both.
    ranked.sort(key=lambda r: r["score"] if r["score"] is not None else float("-inf"), reverse=True)

    best = ranked[0] if ranked else None

    if not best or best["score"] is None:
        # Either no candidates ranked, or the best candidate has no score
        # (missing evaluation, scoring error). Surface the reason.
        reason = "no_score"
        if best and best.get("scoring_error"):
            reason = "scoring_error"
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": reason,
            "detail": (f"Best candidate '{best['label']}' has no scorable evaluation: {best.get('scoring_error', 'no evaluation found')}." if best else "No candidates with scores."),
            "best_candidate": best,
        }

    try:
        promoted = promote_model(
            conn,
            model_candidate_id=best["model_candidate_id"],
            created_by=created_by,
            policy=policy,
            decision_node_id=decision_node_id,
            min_improvement=min_improvement,
        )
        return {
            "promoted": promoted,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": "promoted",
            "detail": f"Promoted '{best['label']}' with score {best['score']:.4f}.",
            "best_candidate": best,
        }
    except ValidationError as exc:
        # Promotion blocked — most commonly the best candidate did not
        # beat the active one by min_improvement. Surface this so the
        # caller can decide whether to lower the threshold, replace the
        # active model manually, or accept the status quo.
        return {
            "promoted": None,
            "twin_id": twin_id,
            "ranking": ranked,
            "reason": "below_min_improvement" if "min_improvement" in str(exc) else "promotion_blocked",
            "detail": str(exc),
            "best_candidate": best,
        }


def register_twin_with_bindings(
    conn: DuckDBPyConnection,
    *,
    label: str,
    target_node_id: str,
    decision_node_id: str | None = None,
    feed_node_ids: Sequence[str] | None = None,
    model_candidate_ids: Sequence[str] | None = None,
    created_by: str,
    description: str | None = None,
    endpoint_url: str | None = None,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    target_node_id = validate_identifier(target_node_id, name="target_node_id")

    target = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [target_node_id],
    ).fetchone()
    if not target:
        raise NodeNotFoundError(f"Target node not found: {target_node_id}")

    if decision_node_id is not None:
        decision_node_id = validate_identifier(decision_node_id, name="decision_node_id")
        decision = conn.execute(
            "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
            [decision_node_id],
        ).fetchone()
        if not decision:
            raise NodeNotFoundError(f"Decision node not found: {decision_node_id}")

    validated_feeds: list[str] = []
    if feed_node_ids is not None:
        for fid in feed_node_ids:
            fid = validate_identifier(fid, name="feed_node_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [fid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Feed node not found: {fid}")
            validated_feeds.append(fid)

    validated_models: list[str] = []
    if model_candidate_ids is not None:
        for mid in model_candidate_ids:
            mid = validate_identifier(mid, name="model_candidate_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [mid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Model candidate node not found: {mid}")
            validated_models.append(mid)

    twin = create_node(
        conn,
        label=label,
        node_type="twin",
        content=description,
        created_by=created_by,
        url=endpoint_url,
        connects_to=[target_node_id] + validated_feeds + validated_models,
    )

    conn.execute(
        "UPDATE ohm_nodes SET gate_type = 'external' WHERE id = ?",
        [twin["id"]],
    )

    create_edge(
        conn,
        from_node=twin["id"],
        to_node=target_node_id,
        edge_type="EVALUATES",
        layer="L3",
        created_by=created_by,
    )

    if decision_node_id is not None:
        create_edge(
            conn,
            from_node=twin["id"],
            to_node=decision_node_id,
            edge_type="DECISION_DEPENDS_ON",
            layer="L3",
            created_by=created_by,
        )

    for fid in validated_feeds:
        create_edge(
            conn,
            from_node=fid,
            to_node=twin["id"],
            edge_type="FEEDS",
            layer="L2",
            created_by=created_by,
        )

    for mid in validated_models:
        create_edge(
            conn,
            from_node=mid,
            to_node=twin["id"],
            edge_type="APPLIES_TO",
            layer="L3",
            created_by=created_by,
        )

    _log_change(conn, "ohm_nodes", twin["id"], "REGISTER_TWIN_WITH_BINDINGS", created_by)

    twin = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [twin["id"]]))[0]

    return {
        "twin": twin,
        "target_node_id": target_node_id,
        "decision_bound": decision_node_id is not None,
        "feeds_bound": len(validated_feeds),
        "models_bound": len(validated_models),
    }


def add_twin_bindings(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    feed_node_ids: Sequence[str] | None = None,
    feed_node_ids_remove: Sequence[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    added: list[str] = []
    if feed_node_ids is not None:
        for fid in feed_node_ids:
            fid = validate_identifier(fid, name="feed_node_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [fid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Feed node not found: {fid}")
            existing_edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
                [fid, twin_id],
            ).fetchone()
            if not existing_edge:
                create_edge(
                    conn,
                    from_node=fid,
                    to_node=twin_id,
                    edge_type="FEEDS",
                    layer="L2",
                    created_by=created_by,
                )
                added.append(fid)

    removed: list[str] = []
    if feed_node_ids_remove is not None:
        for fid in feed_node_ids_remove:
            fid = validate_identifier(fid, name="feed_node_ids_remove entry")
            edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
                [fid, twin_id],
            ).fetchone()
            if edge:
                conn.execute(
                    "UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [edge[0]],
                )
                _log_change(conn, "ohm_edges", edge[0], "SOFT_DELETE", created_by)
                removed.append(fid)

    current_feeds = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS feed_id, n.label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    return {
        "twin_id": twin_id,
        "added": added,
        "removed": removed,
        "current_feeds": current_feeds,
    }


def attach_twin_models(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    model_candidate_ids: Sequence[str] | None = None,
    model_candidate_ids_remove: Sequence[str] | None = None,
    created_by: str,
) -> dict[str, Any]:
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError

    twin_id = validate_identifier(twin_id, name="twin_id")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    added: list[str] = []
    if model_candidate_ids is not None:
        for mid in model_candidate_ids:
            mid = validate_identifier(mid, name="model_candidate_id")
            exists = conn.execute(
                "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [mid],
            ).fetchone()
            if not exists:
                raise NodeNotFoundError(f"Model candidate node not found: {mid}")
            existing_edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'APPLIES_TO' AND deleted_at IS NULL",
                [mid, twin_id],
            ).fetchone()
            if not existing_edge:
                create_edge(
                    conn,
                    from_node=mid,
                    to_node=twin_id,
                    edge_type="APPLIES_TO",
                    layer="L3",
                    created_by=created_by,
                )
                added.append(mid)

    removed: list[str] = []
    if model_candidate_ids_remove is not None:
        for mid in model_candidate_ids_remove:
            mid = validate_identifier(mid, name="model_candidate_ids_remove entry")
            edge = conn.execute(
                "SELECT id FROM ohm_edges WHERE from_node = ? AND to_node = ? AND edge_type = 'APPLIES_TO' AND deleted_at IS NULL",
                [mid, twin_id],
            ).fetchone()
            if edge:
                conn.execute(
                    "UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [edge[0]],
                )
                _log_change(conn, "ohm_edges", edge[0], "SOFT_DELETE", created_by)
                removed.append(mid)

    current_models = _rows_to_dicts(
        conn.execute(
            """SELECT e.from_node AS model_id, n.label
           FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'APPLIES_TO' AND e.deleted_at IS NULL""",
            [twin_id],
        )
    )

    return {
        "twin_id": twin_id,
        "added": added,
        "removed": removed,
        "current_models": current_models,
    }


def get_twin_readiness(
    conn: DuckDBPyConnection,
    *,
    twin_id: str,
    freshness_days: int | None = None,
) -> dict[str, Any]:
    """Check whether a twin is ready to make decisions.

    Args:
        twin_id: ID of the twin node.
        freshness_days: Max age (in days) for a feed to count as fresh.
            Defaults to 7 days when not provided. The caller can pass
            a stricter window (e.g. 1) to surface "threshold exceeded"
            states in dashboards.

    Returns a dict with:
        - twin_id, gates, ready, missing, blocking (as before)
        - threshold: {days, configured, source} so callers can tell
          "no threshold set (default)" from "threshold set + exceeded".
          Resolves the OHM-kg16 item 4 UX concern.

    Threshold semantics (kg16 item 4):
        - "no_threshold_set": no caller has asked for a specific
          window; the default 7d is applied. If feeds_fresh is false
          in this state, the caller is seeing the default
          interpretation, not a user-set threshold.
        - "threshold_exceeded": a caller passed freshness_days (or
          a future persistent threshold is set), and feeds are
          older than that window. The failing feeds_fresh gate is
          binding the configured threshold, not the default.
    """
    from ohm.validation import validate_identifier
    from ohm.exceptions import NodeNotFoundError, ValidationError

    twin_id = validate_identifier(twin_id, name="twin_id")

    threshold_configured = freshness_days is not None
    effective_days = int(freshness_days) if threshold_configured else 7
    if effective_days <= 0:
        raise ValidationError(f"freshness_days must be a positive integer, got {freshness_days}")

    twin = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [twin_id],
    ).fetchone()
    if not twin:
        raise NodeNotFoundError(f"Twin not found: {twin_id}")

    target_bound = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    decision_bound = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE from_node = ? AND edge_type = 'DECISION_DEPENDS_ON' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    feeds_present = bool(
        conn.execute(
            "SELECT 1 FROM ohm_edges WHERE to_node = ? AND edge_type = 'FEEDS' AND deleted_at IS NULL",
            [twin_id],
        ).fetchone()
    )

    feeds_fresh = False
    if feeds_present:
        feed_edges = _rows_to_dicts(
            conn.execute(
                """SELECT e.from_node AS feed_id
               FROM ohm_edges e
               WHERE e.to_node = ? AND e.edge_type = 'FEEDS' AND e.deleted_at IS NULL""",
                [twin_id],
            )
        )
        feed_ids = [fe["feed_id"] for fe in feed_edges]
        if feed_ids:
            placeholders = ",".join(["?"] * len(feed_ids))
            fresh_count = conn.execute(
                f"""SELECT COUNT(DISTINCT o.node_id)
                    FROM ohm_observations o
                    WHERE o.node_id IN ({placeholders})
                      AND o.deleted_at IS NULL
                      AND o.created_at > CURRENT_TIMESTAMP - INTERVAL '{effective_days} days'""",
                feed_ids,
            ).fetchone()[0]
            feeds_fresh = fresh_count == len(feed_ids)

    models_available = bool(
        conn.execute(
            """SELECT 1 FROM ohm_edges e
           JOIN ohm_nodes n ON n.id = e.from_node AND n.deleted_at IS NULL
           WHERE e.to_node = ? AND e.edge_type = 'APPLIES_TO' AND e.deleted_at IS NULL
             AND (n.metadata IS NULL OR n.metadata NOT LIKE '%archived%')""",
            [twin_id],
        ).fetchone()
    )

    models_evaluated = bool(
        conn.execute(
            """SELECT 1 FROM ohm_edges e
           WHERE e.to_node = ? AND e.edge_type = 'EVALUATED_BY' AND e.deleted_at IS NULL""",
            [twin_id],
        ).fetchone()
    )

    gates = {
        "target_bound": target_bound,
        "decision_bound": decision_bound,
        "feeds_present": feeds_present,
        "feeds_fresh": feeds_fresh,
        "models_available": models_available,
        "models_evaluated": models_evaluated,
    }

    critical_gates = ["target_bound", "feeds_present", "models_available"]
    ready = all(gates[g] for g in critical_gates)
    missing = [g for g, v in gates.items() if not v]
    blocking = [g for g in critical_gates if not gates[g]]

    # Threshold state — the UX distinction called out in OHM-kg16
    # item 4. Three states surface the relationship between the
    # feeds_fresh gate and whether anyone asked for a specific
    # window.
    if not threshold_configured and feeds_present and not feeds_fresh:
        threshold_state = "no_threshold_set"  # default 7d, no caller override
    elif threshold_configured and feeds_present and not feeds_fresh:
        threshold_state = "threshold_exceeded"  # caller-set window violated
    else:
        threshold_state = "within_threshold"  # feeds fresh or no feeds

    return {
        "twin_id": twin_id,
        "gates": gates,
        "ready": ready,
        "missing": missing,
        "blocking": blocking,
        "threshold": {
            "days": effective_days,
            "configured": threshold_configured,
            "source": "configured" if threshold_configured else "default",
        },
        "threshold_state": threshold_state,
    }


def detect_verifiable_claims(
    conn: DuckDBPyConnection,
    *,
    agent: str | None = None,
    days_threshold: int = 14,
    confidence_threshold: float = 0.85,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Detect verifiable dated claims that are past their expected date with no outcome recorded.

    Scans edges of type CAUSES, PREDICTS, EXPECTS, EXPECTS_FROM whose metadata
    contains an 'expected_by' or 'window_end' ISO-8601 date that is now past,
    and for which no outcome has been recorded against the from_node (claim node).

    Args:
        conn: Database connection.
        agent: If set, only scan edges created_by this agent.
        days_threshold: Minimum age in days for edges to be considered (default 14).
        confidence_threshold: Minimum confidence to flag (default 0.85).
        limit: Maximum number of results (default 100).

    Returns:
        List of dicts with edge info, claim node info, and expected_by date.
    """
    import json as _json
    from datetime import datetime, timezone

    from ohm.validation import validate_identifier

    if agent is not None:
        agent = validate_identifier(agent, name="agent")

    verifiable_types = ["CAUSES", "PREDICTS", "EXPECTS", "EXPECTS_FROM"]
    placeholders = ",".join(["?"] * len(verifiable_types))

    query = f"""
        SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
               e.created_by, e.created_at, e.metadata,
               fn.label AS from_label, fn.type AS from_type,
               tn.label AS to_label, tn.type AS to_type
        FROM ohm_edges e
        LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
        LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
        WHERE e.deleted_at IS NULL
          AND e.edge_type IN ({placeholders})
          AND e.confidence >= ?
          AND NOT EXISTS (
              SELECT 1 FROM ohm_outcomes oc
              WHERE oc.claim_node = e.from_node
          )
          AND fn.id IS NOT NULL
        ORDER BY e.confidence DESC, e.created_at ASC
        LIMIT ?
    """

    params: list[Any] = verifiable_types + [confidence_threshold, limit]
    if agent is not None:
        query = query.replace(
            "AND fn.id IS NOT NULL",
            "AND fn.id IS NOT NULL\n          AND e.created_by = ?",
        )
        params = verifiable_types + [confidence_threshold, agent, limit]

    rows = conn.execute(query, params).fetchall()

    results = []
    now = datetime.now(timezone.utc)
    for row in rows:
        d = dict(
            zip(
                ["id", "from_node", "to_node", "edge_type", "confidence", "created_by", "created_at", "metadata", "from_label", "from_type", "to_label", "to_type"],
                row,
            )
        )
        meta_raw = d.get("metadata")
        expected_by = None
        if meta_raw:
            try:
                meta = _json.loads(str(meta_raw)) if isinstance(meta_raw, str) else meta_raw
                expected_by = meta.get("expected_by") or meta.get("window_end")
            except (ValueError, TypeError):
                pass
        if not expected_by:
            continue
        try:
            expected_dt = datetime.fromisoformat(str(expected_by).replace("Z", "+00:00"))
            if expected_dt.tzinfo is None:
                expected_dt = expected_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if expected_dt > now:
            continue
        age_days = (now - expected_dt).days
        if age_days < 0:
            continue
        d["expected_by"] = str(expected_by)
        d["days_overdue"] = age_days
        results.append(d)

    return results


def create_verification_nudge(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    created_by: str = "system",
    confidence: float = 0.5,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a NUDGES_FOR_VERIFICATION edge from a nudge task node to the claim node.

    Creates a task node representing the verification nudge, then links it to the
    claim node (from_node of the original edge) via a NUDGES_FOR_VERIFICATION edge
    in L3. Idempotent: if a nudge already exists for the edge, returns the existing one.

    Args:
        conn: Database connection.
        edge_id: The edge whose claim needs verification.
        created_by: Agent creating the nudge.
        confidence: Confidence for the nudge edge (default 0.5).
        reason: Optional reason for the nudge.

    Returns:
        Dict with the created nudge task node and nudge edge.
    """
    import uuid
    import json as _json

    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)

    existing = conn.execute(
        """SELECT id FROM ohm_edges
           WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchone()
    if existing:
        nudge_edge = _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [existing[0]]))[0]
        nudge_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [nudge_edge["from_node"]]))[0] if nudge_edge else {}
        return {"nudge_node": nudge_node, "nudge_edge": nudge_edge}

    target = conn.execute(
        "SELECT id, from_node, to_node, layer, edge_type, metadata FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    claim_node_id = target[1]
    claim_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [claim_node_id]))
    if not claim_node:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Claim node not found: {claim_node_id}")

    nudge_task_id = str(uuid.uuid4())
    nudge_label = f"Verify: {claim_node[0].get('label', claim_node_id)}"
    nudge_metadata = _json.dumps({"nudge_for_edge": edge_id, "edge_type": target[4], "reason": reason})
    conn.execute(
        """INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence, visibility, provenance, metadata, created_at, updated_at)
           VALUES (?, ?, 'task', ?, ?, ?, 'team', 'system', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
        [nudge_task_id, nudge_label, reason or f"Verification nudge for edge {edge_id}", created_by, 0.5, nudge_metadata],
    )
    _log_change(conn, "ohm_nodes", nudge_task_id, "INSERT", created_by)

    nudge_edge_id = str(uuid.uuid4())
    edge_metadata = _json.dumps({"nudge_for_edge": edge_id, "reason": reason})
    conn.execute(
        """INSERT INTO ohm_edges
             (id, from_node, to_node, layer, edge_type, created_by,
              confidence, condition, challenge_of, metadata)
           VALUES (?, ?, ?, 'L3', 'NUDGES_FOR_VERIFICATION', ?, ?, ?, ?, ?)""",
        [nudge_edge_id, nudge_task_id, claim_node_id, created_by, confidence, reason, edge_id, edge_metadata],
    )
    _log_change(conn, "ohm_edges", nudge_edge_id, "INSERT", created_by)

    nudge_node = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [nudge_task_id]))[0]
    nudge_edge = _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [nudge_edge_id]))[0]
    return {"nudge_node": nudge_node, "nudge_edge": nudge_edge}


def record_verification_outcome(
    conn: DuckDBPyConnection,
    *,
    edge_id: str,
    outcome: str,
    recorded_by: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record a verification outcome for a verifiable claim edge.

    Maps string outcome to boolean and confidence:
    - "true" → outcome=True, confidence=1.0
    - "false" → outcome=False, confidence=0.0
    - "ambiguous" → outcome=True, confidence=0.5
    - "deferred" → no outcome recorded, metadata only

    Also resolves any NUDGES_FOR_VERIFICATION edges linked to this edge by
    setting their resolved metadata.

    Args:
        conn: Database connection.
        edge_id: The edge being verified.
        outcome: One of "true", "false", "ambiguous", "deferred".
        recorded_by: Agent recording the outcome.
        reason: Optional context about the outcome.

    Returns:
        Dict with the outcome record and any nudge resolution info.
    """
    import json as _json

    from ohm.validation import validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    if outcome not in ("true", "false", "ambiguous", "deferred"):
        from ohm.exceptions import ValidationError

        raise ValidationError(f"outcome must be one of 'true', 'false', 'ambiguous', 'deferred', got '{outcome}'")

    target = conn.execute(
        "SELECT id, from_node, edge_type FROM ohm_edges WHERE id = ? AND deleted_at IS NULL",
        [edge_id],
    ).fetchone()
    if target is None:
        from ohm.exceptions import EdgeNotFoundError

        raise EdgeNotFoundError(f"Edge not found: {edge_id}")

    claim_node = target[1]
    result: dict[str, Any] = {"edge_id": edge_id, "outcome": outcome, "recorded_by": recorded_by}

    if outcome == "deferred":
        nudge_edges = conn.execute(
            """SELECT id, metadata FROM ohm_edges
               WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
            [edge_id],
        ).fetchall()
        resolved = []
        for ne_id, ne_meta in nudge_edges:
            meta = {}
            if ne_meta:
                try:
                    meta = _json.loads(str(ne_meta)) if isinstance(ne_meta, str) else ne_meta
                except (ValueError, TypeError):
                    pass
            meta["resolved"] = True
            meta["resolution"] = "deferred"
            meta["resolved_by"] = recorded_by
            conn.execute(
                "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
                [_json.dumps(meta), ne_id],
            )
            resolved.append(ne_id)
        result["nudges_resolved"] = resolved
        result["deferred"] = True
        return result

    bool_outcome = outcome in ("true", "ambiguous")
    confidence_map = {"true": 1.0, "false": 0.0, "ambiguous": 0.5}
    outcome_confidence = confidence_map[outcome]

    outcome_result = query_record_outcome(
        conn,
        source_agent=target[2] or "unknown",
        claim_node=claim_node,
        outcome=bool_outcome,
        recorded_by=recorded_by,
        notes=reason,
    )
    result["outcome_record"] = outcome_result
    result["confidence"] = outcome_confidence

    nudge_edges = conn.execute(
        """SELECT id, metadata FROM ohm_edges
           WHERE challenge_of = ? AND edge_type = 'NUDGES_FOR_VERIFICATION' AND deleted_at IS NULL""",
        [edge_id],
    ).fetchall()
    resolved = []
    for ne_id, ne_meta in nudge_edges:
        meta = {}
        if ne_meta:
            try:
                meta = _json.loads(str(ne_meta)) if isinstance(ne_meta, str) else ne_meta
            except (ValueError, TypeError):
                pass
        meta["resolved"] = True
        meta["resolution"] = outcome
        meta["resolved_by"] = recorded_by
        conn.execute(
            "UPDATE ohm_edges SET metadata = ? WHERE id = ?",
            [_json.dumps(meta), ne_id],
        )
        resolved.append(ne_id)
    result["nudges_resolved"] = resolved

    return result


def list_pending_verifications(
    conn: DuckDBPyConnection,
    *,
    agent: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List pending NUDGES_FOR_VERIFICATION edges that haven't been resolved.

    A nudge is pending if its metadata does not contain 'resolved': True.

    Args:
        conn: Database connection.
        agent: If set, only list nudges created_by this agent.
        limit: Maximum number of results (default 100).

    Returns:
        List of dicts with nudge edge and associated claim node info.
    """
    import json as _json

    from ohm.validation import validate_identifier

    if agent is not None:
        agent = validate_identifier(agent, name="agent")

    query = """
        SELECT e.id, e.from_node, e.to_node, e.edge_type, e.confidence,
               e.created_by, e.created_at, e.metadata, e.challenge_of,
               fn.label AS from_label, fn.type AS from_type,
               tn.label AS to_label, tn.type AS to_type
        FROM ohm_edges e
        LEFT JOIN ohm_nodes fn ON e.from_node = fn.id AND fn.deleted_at IS NULL
        LEFT JOIN ohm_nodes tn ON e.to_node = tn.id AND tn.deleted_at IS NULL
        WHERE e.deleted_at IS NULL
          AND e.edge_type = 'NUDGES_FOR_VERIFICATION'
          AND fn.id IS NOT NULL
    """

    params: list[Any] = []
    if agent is not None:
        query += "\n          AND e.created_by = ?"
        params.append(agent)

    query += "\n        ORDER BY e.created_at ASC\n        LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        d = dict(
            zip(
                ["id", "from_node", "to_node", "edge_type", "confidence", "created_by", "created_at", "metadata", "challenge_of", "from_label", "from_type", "to_label", "to_type"],
                row,
            )
        )
        meta_raw = d.get("metadata")
        if meta_raw:
            try:
                meta = _json.loads(str(meta_raw)) if isinstance(meta_raw, str) else meta_raw
                if meta.get("resolved"):
                    continue
                d["reason"] = meta.get("reason")
                d["nudge_for_edge"] = meta.get("nudge_for_edge")
            except (ValueError, TypeError):
                pass
        results.append(d)

    return results
