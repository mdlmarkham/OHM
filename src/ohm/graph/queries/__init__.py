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
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    # Convenience aliases
    for row in rows:
        if "from_node" in row:
            row["from"] = row["from_node"]
            row["to"] = row["to_node"]
            # Not every from_node/to_node table is an edge table (e.g.
            # ohm_suggestions has from_node/to_node but no edge_type column).
            if "edge_type" in row:
                row["type"] = row["edge_type"]
        # node_type is the write API field name; DB column is type. Expose both.
        if "type" in row and "from_node" not in row and "node_type" not in row:
            row["node_type"] = row["type"]
    return rows


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
    allowed_nodes: set[str] | None = None,
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

    Args:
        allowed_nodes: Optional set of node ids the caller is permitted to
            see (OHM-737). When set, the BFS only traverses through nodes
            in this set — restricted nodes can never appear as path
            intermediates. ``from_node`` and ``to_node`` are NOT checked
            against this set; the caller is responsible for enforcing scope
            on the endpoints (and should 403 before calling if either is
            out of scope). ``None`` means no scope constraint (full access).
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
                f"SELECT id, from_node, to_node, layer, edge_type, confidence, created_by FROM ohm_edges WHERE deleted_at IS NULL AND from_node IN ({placeholders}) {layer_clause}",
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
                # OHM-737: scope-aware traversal — skip edges to nodes the
                # agent can't see, so restricted nodes never appear as path
                # intermediates. The destination (to_node) is always allowed
                # since the caller has already verified it's in scope.
                if allowed_nodes is not None and nxt != to_node and nxt not in allowed_nodes:
                    continue
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
                e.created_by,
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
                e.created_by,
                i.depth + 1
            FROM impact_cte i
            JOIN ohm_edges e ON e.from_node = i.to_node
            WHERE i.depth < ?
              AND e.deleted_at IS NULL
              AND e.layer IN ('L2', 'L3')
        )
        SELECT edge_id, from_node, to_node, layer, edge_type, confidence, created_by, depth
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
    utility_scale: str | float | list[str | float] | None = None,
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
        elif isinstance(utility_scale, (int, float)):
            if not (0 <= utility_scale <= 1):
                raise ValueError(f"utility_scale must be between 0 and 1, got {utility_scale}")
        elif isinstance(utility_scale, (list, tuple)):
            # OHM-n9us: accept arrays of numbers (e.g. [0.0, 0.5, 1.0])
            # for decision nodes with multiple outcomes. Store the mean as
            # the FLOAT utility_scale and the full array in metadata.
            validated = []
            for v in utility_scale:
                if isinstance(v, str):
                    v = _utility_scale_map.get(v, v)
                if not isinstance(v, (int, float)):
                    raise ValueError(f"utility_scale array elements must be numbers or best/neutral/worst, got {v}")
                if not (0 <= v <= 1):
                    raise ValueError(f"utility_scale array elements must be between 0 and 1, got {v}")
                validated.append(float(v))
            if metadata is None:
                metadata = {}
            metadata["utility_scale_array"] = validated
            utility_scale = sum(validated) / len(validated) if validated else 0.5
        else:
            raise ValueError(f"utility_scale must be a number, best/neutral/worst, or a list of numbers, got {type(utility_scale).__name__}: {utility_scale}")

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
    elif from_type in SOURCE_TYPES and to_type in {"pattern", "concept", "synthesis", "interpretation"}:
        suggested_edge_type = "REFERENCES"
        suggested_layer = "L2"
        reasoning = f"Source→{to_type} should use REFERENCES (L2 citation edge), not L3 RELATED_TO. A source citing a pattern is a citation, not an agent interpretation."
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
    elif from_type == "runbook" and to_type == "skill":
        suggested_edge_type = "DEPENDS_ON"
        suggested_layer = "L4"
        reasoning = "runbook→skill should use DEPENDS_ON (L4). A runbook is an ordered chain of skills; each DEPENDS_ON edge encodes step order."
        alternatives = ["ENABLES"]
    elif from_type == "skill" and to_type == "runbook":
        suggested_edge_type = "ENABLES"
        suggested_layer = "L4"
        reasoning = "skill→runbook should use ENABLES (L4). A skill enables the runbook it participates in."
        alternatives = ["DEPENDS_ON"]
    elif from_type == "skill" and to_type == "skill":
        suggested_edge_type = "DEPENDS_ON"
        suggested_layer = "L4"
        reasoning = "skill→skill should use DEPENDS_ON (L4) to express prerequisite ordering between skill steps."
        alternatives = ["RELATED_TO"]
    elif from_type in {"skill", "runbook"} and to_type == "agent":
        suggested_edge_type = "CAPABLE_OF"
        suggested_layer = "L1"
        reasoning = f"{from_type}→agent should use CAPABLE_OF (L1). The agent is capable of performing this {from_type}."
        alternatives = ["USES"]
    else:
        suggested_edge_type = "RELATED_TO"
        suggested_layer = "L3"
        reasoning = f"Default for {from_type}→{to_type}: RELATED_TO. Use CAUSES only when there is a genuine causal mechanism."
        alternatives = ["CAUSES", "INFLUENCES", "CORRELATES_WITH", "EXPLAINS"]

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
        row = conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [sid]).fetchone()
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
        skill_row = _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [sid]))
        if skill_row:
            skills.append(skill_row[0])

    return {
        "runbook_id": runbook_id,
        "label": row[1],
        "skills": skills,
        "skill_count": len(skills),
    }


def node_type_template(
    conn: DuckDBPyConnection,
    *,
    node_type: str,
) -> dict[str, Any]:
    """Return a usage template for a node type (OHM-461f.1).

    Provides required/optional fields, an example payload, and suggested edge
    types so agents can construct valid nodes without reading the ADR.
    """
    from ohm.validation import validate_identifier

    node_type = validate_identifier(node_type, name="node_type").lower()

    templates: dict[str, dict[str, Any]] = {
        "concept": {
            "node_type": "concept",
            "description": "Core abstract idea, pattern, or theory. Can stand alone until it becomes a derived claim.",
            "required_fields": {"label": "str"},
            "optional_fields": {
                "id": "str (auto-generated from label if omitted)",
                "content": "str (description)",
                "node_type": "'concept' (default)",
                "confidence": "float 0.0-1.0 (default 1.0)",
                "provenance": "str",
                "tags": "list[str]",
                "metadata": "dict",
                "source_url": "str (alias for url)",
                "source_tier": "raw|unverified|preliminary|official|verified",
                "source_author": "str",
                "source_institution": "str",
            },
            "example": {
                "label": "AND→OR Conversion",
                "content": "A Boolean gate where all inputs must be TRUE...",
                "node_type": "concept",
                "tags": ["boolean", "governance"],
            },
            "suggested_edge_types": [
                {"edge_type": "CAUSES", "layer": "L3", "when": "concept→concept causal claim"},
                {"edge_type": "APPLIES_TO", "layer": "L3", "when": "pattern→instance"},
            ],
            "create_endpoint": "POST /node",
        },
        "source": {
            "node_type": "source",
            "description": "External reference (article, book, paper, URL). MUST include source_url.",
            "required_fields": {"label": "str", "source_url": "str"},
            "optional_fields": {
                "id": "str",
                "content": "str",
                "node_type": "'source'",
                "confidence": "float",
                "provenance": "str",
                "tags": "list[str]",
                "metadata": "dict",
                "source_tier": "raw|unverified|preliminary|official|verified",
                "source_author": "str",
                "source_institution": "str",
            },
            "example": {
                "label": "Reuters Hormuz transit report",
                "node_type": "source",
                "source_url": "https://www.reuters.com/article/hormuz-transit",
                "source_tier": "verified",
            },
            "suggested_edge_types": [
                {"edge_type": "REFERENCES", "layer": "L2", "when": "interpretation/source→source"},
            ],
            "create_endpoint": "POST /node",
            "hook_constraints": ["source_url_required"],
        },
        "pattern": {
            "node_type": "pattern",
            "description": "Recurring structure (AND-gate, trap, cycle, equilibrium). Must anchor to existing nodes.",
            "required_fields": {"label": "str"},
            "optional_fields": {
                "id": "str",
                "content": "str",
                "node_type": "'pattern'",
                "confidence": "float",
                "provenance": "str",
                "tags": "list[str]",
                "metadata": "dict",
                "connects_to": "list[str] (existing node ids — required by cross_link rule)",
            },
            "example": {
                "label": "AND→OR Conversion Family",
                "node_type": "pattern",
                "connects_to": ["concept-and-or-conversion"],
            },
            "suggested_edge_types": [
                {"edge_type": "CONTAINS", "layer": "L1", "when": "pattern→member concept"},
                {"edge_type": "APPLIES_TO", "layer": "L3", "when": "pattern→instance"},
            ],
            "create_endpoint": "POST /node",
            "hook_constraints": ["cross_link_required"],
        },
        "task": {
            "node_type": "task",
            "description": "Action item with status, priority, assignment, due date.",
            "required_fields": {"label": "str"},
            "optional_fields": {
                "id": "str",
                "content": "str",
                "node_type": "'task'",
                "priority": "P0|P1|P2|P3|P4",
                "task_status": "open|in_progress|blocked|review|done|cancelled",
                "assigned_to": "str (agent name)",
                "due_date": "ISO 8601 timestamp",
                "connects_to": "list[str] (existing node ids — required by cross_link rule)",
            },
            "example": {
                "label": "Verify AND→OR framework",
                "node_type": "task",
                "priority": "P1",
                "task_status": "open",
                "assigned_to": "socrates",
                "connects_to": ["concept-and-or-conversion"],
            },
            "suggested_edge_types": [
                {"edge_type": "REFERENCES", "layer": "L2", "when": "task→concept it validates"},
                {"edge_type": "DELEGATED_TO", "layer": "L2", "when": "task→agent"},
            ],
            "create_endpoint": "POST /node or POST /tasks",
            "hook_constraints": ["cross_link_required"],
        },
        "decision": {
            "node_type": "decision",
            "description": "Choice node with utility, alternatives, and current best action. Enables VoI and game-theoretic analysis.",
            "required_fields": {"label": "str"},
            "optional_fields": {
                "id": "str",
                "content": "str",
                "node_type": "'decision'",
                "utility_scale": "float 0-1 or best/neutral/worst",
                "utility_usd_per_day": "float",
                "utility_currency": "ISO 4217 code",
                "current_best_action": "str",
                "action_alternatives": "list[str]",
                "connects_to": "list[str] (existing node ids — required by cross_link rule)",
            },
            "example": {
                "label": "Escalate Hormuz response",
                "node_type": "decision",
                "utility_scale": 0.9,
                "current_best_action": "diplomatic_channel",
                "action_alternatives": ["military_response", "sanctions"],
                "connects_to": ["event-hormuz-closure"],
            },
            "suggested_edge_types": [
                {"edge_type": "INFLUENCES", "layer": "L2", "when": "factor→decision"},
                {"edge_type": "DEPENDS_ON", "layer": "L4", "when": "decision→prerequisite"},
            ],
            "create_endpoint": "POST /node",
            "hook_constraints": ["cross_link_required"],
        },
        "observation": {
            "node_type": "observation",
            "description": "A recorded measurement or assessment on an existing node. Use POST /observe/{id} in most cases.",
            "required_fields": {"label": "str"},
            "optional_fields": {
                "id": "str",
                "content": "str",
                "node_type": "'observation'",
                "connects_to": "list[str] (existing node ids — required by cross_link rule)",
            },
            "example": {
                "label": "Brent price observation",
                "node_type": "observation",
                "connects_to": ["concept-hormuz-demand-rationing"],
            },
            "suggested_edge_types": [
                {"edge_type": "REFERENCES", "layer": "L2", "when": "observation→source"},
            ],
            "create_endpoint": "POST /node (rare) or POST /observe/{node_id}",
            "hook_constraints": ["cross_link_required"],
        },
        "skill": {
            "node_type": "skill",
            "description": "Portable agent capability with trigger, scope, tools, boundaries, output, and verification evidence.",
            "required_fields": {"label": "str", "trigger": "str (stored in metadata.trigger)"},
            "optional_fields": {
                "scope": "personal | project | universal (default personal)",
                "required_tools": "list[str] (tools the skill needs)",
                "boundaries": "str (what the skill should NOT do)",
                "output_format": "str (expected output shape)",
                "verification_evidence": "list[str] (evidence types the skill produces)",
                "connects_to": "list[str] (existing node ids to cross-link)",
            },
            "example": {
                "label": "Verify causal claim against source",
                "trigger": "When a new CAUSES edge is created with confidence > 0.8",
                "scope": "project",
                "required_tools": ["ohm.graph.queries.get_edge", "ohm.graph.queries.get_node"],
                "boundaries": "Read-only. Do not modify the edge under review.",
                "output_format": "observation record with outcome=True/False",
                "verification_evidence": ["source_url", "source_tier"],
                "connects_to": ["existing_claim_node_id"],
            },
            "suggested_edge_types": [
                {"edge_type": "DEPENDS_ON", "layer": "L4", "when": "skill→skill prerequisite"},
                {"edge_type": "CAPABLE_OF", "layer": "L1", "when": "agent→skill (agent can perform)"},
                {"edge_type": "ENABLES", "layer": "L4", "when": "skill→runbook"},
            ],
            "create_endpoint": "POST /skill",
            "hook_constraints": ["cross_link_required"],
        },
        "runbook": {
            "node_type": "runbook",
            "description": "Ordered chain of skill nodes connected by DEPENDS_ON edges. Represents a repeatable procedure.",
            "required_fields": {"label": "str", "skill_ids": "list[str] (existing skill node ids, in order)"},
            "optional_fields": {
                "description": "str (what the runbook accomplishes)",
                "connects_to": "list[str] (existing node ids to cross-link)",
            },
            "example": {
                "label": "Causal claim verification runbook",
                "skill_ids": ["skill_fetch_edge", "skill_check_source", "skill_record_observation"],
                "description": "Fetch a causal edge, validate its source, and record an observation outcome.",
            },
            "suggested_edge_types": [
                {"edge_type": "DEPENDS_ON", "layer": "L4", "when": "runbook→skill (step ordering)"},
                {"edge_type": "ENABLES", "layer": "L4", "when": "skill→runbook (skill participates)"},
            ],
            "create_endpoint": "POST /runbook",
            "query_endpoint": "GET /runbook/{id}/steps",
            "hook_constraints": ["cross_link_required"],
        },
    }

    if node_type not in templates:
        return {
            "node_type": node_type,
            "error": "no template available for this node type",
            "available_types": list(templates.keys()),
        }

    return templates[node_type]


def skill_runbook_query_guide(
    conn: DuckDBPyConnection,
) -> dict[str, Any]:
    """Return useful query patterns for skill/runbook graphs (OHM-461f.1).

    Lists endpoint recipes an agent can call to discover, traverse, and audit
    skill and runbook nodes.
    """
    return {
        "queries": [
            {
                "name": "list_all_skills",
                "endpoint": "GET /nodes?type=skill",
                "description": "List every skill node in the graph.",
            },
            {
                "name": "list_all_runbooks",
                "endpoint": "GET /nodes?type=runbook",
                "description": "List every runbook node in the graph.",
            },
            {
                "name": "get_runbook_steps",
                "endpoint": "GET /runbook/{runbook_id}/steps",
                "description": "Fetch the ordered skill chain for a runbook.",
            },
            {
                "name": "suggest_edge_for_skill_pair",
                "endpoint": "GET /edge/suggest-type?from={skill_id}&to={skill_id}",
                "description": "Suggest the correct edge type between two skills (typically DEPENDS_ON L4).",
            },
            {
                "name": "find_skills_for_agent",
                "endpoint": "GET /neighborhood/{agent_node_id}?layer=L1",
                "description": "Find skills an agent is capable of via CAPABLE_OF edges.",
            },
            {
                "name": "trace_runbook_dependencies",
                "endpoint": "GET /neighborhood/{runbook_id}?layer=L4",
                "description": "Trace the DEPENDS_ON chain from a runbook to its skills.",
            },
            {
                "name": "search_skills_by_trigger",
                "endpoint": "GET /search?q={trigger_keyword}&type=skill",
                "description": "Full-text search skill nodes by trigger keywords.",
            },
        ],
        "edge_types": {
            "skill_to_skill": "DEPENDS_ON (L4) — prerequisite ordering",
            "runbook_to_skill": "DEPENDS_ON (L4) — step order in the chain",
            "skill_to_runbook": "ENABLES (L4) — skill participates in runbook",
            "agent_to_skill": "CAPABLE_OF (L1) — agent can perform this skill",
        },
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
            # OHM-733: append to confidence log instead of direct UPDATE
            log_confidence_change(
                conn,
                edge_id=edge["id"],
                agent="system",
                old_value=original_conf,
                new_value=new_confidence,
                reason="decay",
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




# OHM-447: Re-exports from extracted submodules (leaf domains — Phase 1)
from ohm.graph.queries.rul import register_rul_assessment, get_rul_assessments
from ohm.graph.queries.runs import create_run, get_run, list_runs, complete_run
from ohm.graph.queries.node_paths import set_node_path, get_nodes_by_path_prefix
from ohm.graph.queries.plans_events import (
    create_plan,
    get_plan,
    list_plans,
    create_event,
    get_event,
    get_events_for_node,
    get_events_for_plan,
    create_event_link,
    get_event_links,
    timeline_rollup,
)
from ohm.graph.queries.reports import (
    create_report,
    get_report,
    list_reports,
    finalize_report,
    supersede_report,
)
# OHM-447: Re-exports from extracted submodules (leaf domains — Phase 1)
from ohm.graph.queries.rul import register_rul_assessment, get_rul_assessments
from ohm.graph.queries.runs import create_run, get_run, list_runs, complete_run
from ohm.graph.queries.node_paths import set_node_path, get_nodes_by_path_prefix
from ohm.graph.queries.plans_events import (
    create_plan,
    get_plan,
    list_plans,
    create_event,
    get_event,
    get_events_for_node,
    get_events_for_plan,
    create_event_link,
    get_event_links,
    timeline_rollup,
)
from ohm.graph.queries.reports import (
    create_report,
    get_report,
    list_reports,
    finalize_report,
    supersede_report,
)
from ohm.graph.queries.hooks import (
    create_hook,
    query_hooks,
    delete_hook,
)
from ohm.graph.queries.aliases import (
    register_alias,
    resolve_alias,
    query_aliases,
    register_content_hash,
    lookup_content_hash,
    resolve_node_by_alias,
    log_confidence_change,
    recompute_confidence_from_log,
    get_confidence_history,
)
from ohm.graph.queries.data_products import (
    register_data_product,
    refresh_data_product_provenance,
    get_data_product,
    get_data_product_by_odps_id,
    list_data_products,
)
from ohm.graph.queries.signing import (
    sign_node_write,
    sign_edge_write,
    verify_node_write,
    verify_edge_write,
)

# OHM-447 Phase 2: twins/ML cluster re-exports
from ohm.graph.queries.twins import (
    register_twin, twin_predict, twin_constraints,
    validate_action_against_twin, explain_twin,
    create_twin_template, list_twin_templates,
    get_twin_template, instantiate_twin_from_template,
    assemble_twin_for_decision,
    register_model_candidate, evaluate_model, compare_models,
    promote_model, register_shadow_model,
    detect_drift, run_walk_forward_validation,
    ensemble_predict, compute_decision_value,
    auto_retire_model, set_freshness_threshold,
    get_freshness_status,
    start_twin_design_session, transition_session,
    add_session_observation, propose_twin_config,
    review_proposal, instantiate_from_session,
    record_calibration, evolve_session,
    get_session_state, get_session_audit,
    set_promotion_policy,
    auto_promote_best_model,
    register_twin_with_bindings, add_twin_bindings,
    attach_twin_models, get_twin_readiness,
)
from ohm.graph.queries.feed_investment import (
    compute_feed_investment, recommend_mode,
    record_mode_switch, get_current_mode,
    temporal_decision_summary,
)

# OHM-447 Phase 3: mid-weight domains re-exports
from ohm.graph.queries.cascade import (
    query_deterministic_cascade, query_cascade_scenario,
    monte_carlo_cascade, query_what_if, propagate_observation,
)
from ohm.graph.queries.handoff import (
    query_handoff, query_escalate, query_ticket_provenance,
)
from ohm.graph.queries.embeddings import (
    generate_embedding, semantic_search, search,
    fuzzy_search, update_node_embedding,
)
from ohm.graph.queries.discovery import (
    queue_discovery_candidates, query_discovery_queue,
    review_discovery_candidate,
)
from ohm.graph.queries.fragments import (
    scratch, resolve_question, promote_fragment,
    detect_fragment_resonance, detect_fragment_clusters,
    evict_expired_fragments, hd_membership_search,
    batch_update_hd_fingerprints, query_fragment_clusters,
    reflect_challenge_to_fragments, update_node_hd_fingerprint,
)
from ohm.graph.queries.suggestions import (
    create_suggestion, query_suggestions, promote_suggestion,
    reject_suggestion, expire_suggestions, batch_orphan_triage,
    query_claim_lineage, query_confidence_report,
    query_contradiction_summary, query_neighborhood_narrative,
    query_task_context,
)
from ohm.graph.queries.cascade_scenario import (
    query_counterfactual_cascade, query_compare_scenarios,
)
from ohm.graph.queries.actions import (
    propose_action, execute_action,
    apply_decay_to_edges, compute_confidence_with_decay,
)
from ohm.graph.queries.loop_status import (
    query_loop_status,
)
from ohm.graph.queries.verification import (
    detect_verifiable_claims, create_verification_nudge,
    record_verification_outcome, list_pending_verifications,
)

# OHM-447: Inject cross-domain functions into submodules that need them.
# This runs AFTER all submodule imports, so all functions are fully defined.
# Submodules use bare-name access inside their function bodies, so the names
# must be in the submodule's own namespace, not just resolvable via __getattr__.
import ohm.graph.queries.fragments as _frag_mod
_frag_mod.create_node = create_node
_frag_mod.create_edge = create_edge
_frag_mod.generate_embedding = generate_embedding
_frag_mod.search = search

import ohm.graph.queries.actions as _act_mod
_act_mod.create_node = create_node
_act_mod.create_edge = create_edge

import ohm.graph.queries.discovery as _disc_mod
_disc_mod.create_edge = create_edge

import ohm.graph.queries.handoff as _ho_mod
_ho_mod.create_edge = create_edge

import ohm.graph.queries.feed_investment as _fi_mod
_fi_mod.create_node = create_node
_fi_mod.create_edge = create_edge
_fi_mod.get_freshness_status = get_freshness_status

import ohm.graph.queries.data_products as _dp_mod
_dp_mod.create_node = create_node
_dp_mod.create_edge = create_edge
_dp_mod.find_or_create_node = find_or_create_node
_dp_mod.query_source_reliability = query_source_reliability

import ohm.graph.queries.rul as _rul_mod
_rul_mod.node_exists = node_exists

import ohm.graph.queries.cascade_scenario as _cs_mod
_cs_mod.query_deterministic_cascade = query_deterministic_cascade

import ohm.graph.queries.loop_status as _ls_mod
_ls_mod.apply_decay_to_edges = apply_decay_to_edges
_ls_mod.compute_confidence_with_decay = compute_confidence_with_decay

import ohm.graph.queries.verification as _ver_mod
_ver_mod.query_record_outcome = query_record_outcome

import ohm.graph.queries.twins.core as _tc_mod
_tc_mod.create_node = create_node
_tc_mod.create_edge = create_edge
_tc_mod.compute_confidence_with_decay = compute_confidence_with_decay
_tc_mod.query_counterfactual_cascade = query_counterfactual_cascade

import ohm.graph.queries.twins.model_registry as _tm_mod
_tm_mod.create_node = create_node
_tm_mod.create_edge = create_edge
_tm_mod.compute_confidence_with_decay = compute_confidence_with_decay

import ohm.graph.queries.twins.design_sessions as _td_mod
_td_mod.create_node = create_node
_td_mod.create_edge = create_edge
_td_mod.assemble_twin_for_decision = assemble_twin_for_decision
_td_mod.compute_decision_value = compute_decision_value
_td_mod.instantiate_twin_from_template = instantiate_twin_from_template
_td_mod.promote_model = promote_model
_td_mod.register_model_candidate = register_model_candidate

import ohm.graph.queries.twins.bindings as _tb_mod
_tb_mod.create_node = create_node
_tb_mod.create_edge = create_edge