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
from typing import TYPE_CHECKING, Any

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
    return _rows_to_dicts(result)


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
    """
    from collections import deque

    from ohm.validation import validate_depth, validate_identifier, validate_layer

    from_node = validate_identifier(from_node, name="from_node")
    to_node = validate_identifier(to_node, name="to_node")
    max_depth = validate_depth(max_depth, max_depth=50)

    if from_node == to_node:
        return []

    edge_query = "SELECT id, from_node, to_node, layer, edge_type, confidence FROM ohm_edges WHERE deleted_at IS NULL"
    params: list = []
    if layer:
        layer = validate_layer(layer)
        edge_query += " AND layer = ?"
        params.append(layer)

    all_edges = _rows_to_dicts(conn.execute(edge_query, params))

    # Directed adjacency list: node → outgoing edges
    adj: dict[str, list[dict[str, Any]]] = {}
    for e in all_edges:
        adj.setdefault(e["from_node"], []).append(e)

    # BFS: find shortest directed path
    queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(from_node, [])])
    visited: set[str] = {from_node}

    while queue:
        current, path = queue.popleft()
        if len(path) >= max_depth:
            continue
        for edge in adj.get(current, []):
            nxt = edge["to_node"]
            new_path = path + [edge]
            if nxt == to_node:
                return [dict(e, depth=i + 1) for i, e in enumerate(new_path)]
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, new_path))

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

    Args:
        conn: Database connection.
        source_agent: The agent whose claim is being evaluated.
        claim_node: The node representing the claim.
        outcome: True if the source was correct, False otherwise.
        recorded_by: Agent recording the outcome.
        notes: Optional context about the outcome.

    Returns:
        The created outcome record.
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
    conn.execute(
        """INSERT INTO ohm_outcomes
           (id, source_agent, claim_node, outcome, recorded_by, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [outcome_id, source_agent, claim_node, outcome, recorded_by, notes],
    )
    _log_change(conn, "ohm_outcomes", outcome_id, "INSERT", recorded_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_outcomes WHERE id = ?", [outcome_id]))[0]


def query_source_reliability(
    conn: DuckDBPyConnection,
    source_agent: str,
) -> dict[str, Any]:
    """Compute reliability metrics for a source agent.

    Returns P(accurate) and false_positive_rate computed from historical
    outcomes. If fewer than 5 outcomes recorded, returns a warning that
    the estimate is low-confidence.

    Args:
        conn: Database connection.
        source_agent: The agent to evaluate.

    Returns:
        Dict with source_agent, total_outcomes, accurate_count,
        false_positive_count, p_accurate, false_positive_rate,
        and low_confidence_warning (bool).
    """
    from ohm.validation import validate_identifier

    source_agent = validate_identifier(source_agent, name="source_agent")

    result = conn.execute(
        """SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN outcome THEN 1 ELSE 0 END) AS accurate,
            SUM(CASE WHEN NOT outcome THEN 1 ELSE 0 END) AS false_positives
        FROM ohm_outcomes
        WHERE source_agent = ?""",
        [source_agent],
    ).fetchone()

    if result:
        total = result[0]
        accurate = result[1] or 0
        false_positives = result[2] or 0
    else:
        total = accurate = false_positives = 0

    p_accurate = round(accurate / total, 4) if total > 0 else None
    fpr = round(false_positives / total, 4) if total > 0 else None

    return {
        "source_agent": source_agent,
        "total_outcomes": total,
        "accurate_count": accurate,
        "false_positive_count": false_positives,
        "p_accurate": p_accurate,
        "false_positive_rate": fpr,
        "low_confidence_warning": total < 5,
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

    # Node counts by type
    result = conn.execute("""
        SELECT type, COUNT(*) AS count
        FROM ohm_nodes
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
        fragments_total_row = conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type = 'fragment'"
        ).fetchone()
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
    utility_scale: float | None = None,
    utility_usd_per_day: float | None = None,
    utility_currency: str | None = None,
    current_best_action: str | None = None,
    action_alternatives: list[str] | None = None,
    connects_to: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new node and return its full record.

    Args:
        connects_to: Optional list of existing node ids this node will be linked
            to. Used by the cross-link requirement (OHM-tjzh / ADR-018) to prove
            the agent has anchored a derived claim to existing graph structure.
            Each id must already exist; the function does not auto-create edges.
    """
    import json
    from ohm.schema import (
        generate_node_id,
        validate_node_type,
        VALID_PRIORITY,
    )
    from ohm.validation import validate_confidence, validate_identifier

    if not label or len(label) > 500:
        raise ValueError("Label must be non-empty and ≤ 500 characters")
    if not validate_node_type(node_type):
        raise ValueError(f"Invalid node type: {node_type}")
    confidence = validate_confidence(confidence)
    if priority is not None and priority not in VALID_PRIORITY:
        raise ValueError(f"Invalid priority: {priority}. Must be one of: {sorted(VALID_PRIORITY)}")
    if utility_scale is not None and not (0.0 <= utility_scale <= 1.0):
        raise ValueError(f"utility_scale must be between 0 and 1, got {utility_scale}")

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
            raise ValueError(
                f"connects_to references unknown node id(s): {missing}. "
                f"Cross-link targets must already exist in the graph."
            )

    # Serialize action_alternatives to JSON if provided
    alternatives_json = json.dumps(action_alternatives) if action_alternatives is not None else None

    node_id = generate_node_id(label)

    # Check for soft-deleted row with same ID (primary key collision avoidance)
    soft_deleted = conn.execute("SELECT id FROM ohm_nodes WHERE id = ? AND deleted_at IS NOT NULL", [node_id]).fetchone()
    if soft_deleted:
        # Reactivate soft-deleted row with new data
        conn.execute(
            """UPDATE ohm_nodes SET
                label = ?, type = ?, content = ?, created_by = ?,
                visibility = ?, provenance = ?, confidence = ?, priority = ?, url = ?,
                utility_scale = ?, utility_usd_per_day = ?, utility_currency = ?,
                current_best_action = ?, action_alternatives = ?,
                deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?""",
            [label, node_type, content, created_by, visibility, provenance, confidence, priority, url, utility_scale, utility_usd_per_day, utility_currency, current_best_action, alternatives_json, node_id],
        )
        _log_change(conn, "ohm_nodes", node_id, "UPDATE", created_by)
        return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL", [node_id]))[0]

    conn.execute(
        """INSERT INTO ohm_nodes
           (id, label, type, content, created_by, visibility, provenance, confidence, priority, url,
            utility_scale, utility_usd_per_day, utility_currency, current_best_action, action_alternatives)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [node_id, label, node_type, content, created_by, visibility, provenance, confidence, priority, url, utility_scale, utility_usd_per_day, utility_currency, current_best_action, alternatives_json],
    )
    _log_change(conn, "ohm_nodes", node_id, "INSERT", created_by)
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
    """Find an existing node by label and type, or create one if not found.

    Used for idempotent agent registration — avoids creating duplicate
    value/goal/skill/topic nodes when re-registering.

    Returns the existing or newly created node record.
    """
    # Try to find an existing node with matching label and type (case-insensitive)
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

    # Not found — create a new one
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
) -> dict[str, Any]:
    """Create a new edge and return its full record. Validates layer/type compatibility."""
    import uuid
    import json

    from ohm.schema import validate_edge_type, VALID_URGENCY
    from ohm.validation import validate_confidence, validate_pert_triple

    if not validate_edge_type(layer, edge_type):
        raise ValueError(f"Invalid edge type '{edge_type}' for layer '{layer}'")
    confidence = validate_confidence(confidence)
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
            confidence_p05, confidence_p50, confidence_p95)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_node, to_node, layer, edge_type, created_by, confidence, probability, urgency, condition, provenance, metadata_json, probability_p05, probability_p50, probability_p95, confidence_p05, confidence_p50, confidence_p95],
    )
    _log_change(conn, "ohm_edges", edge_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_edges WHERE id = ? AND deleted_at IS NULL", [edge_id]))[0]


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
    """
    import uuid

    from ohm.boundary import enforce_challenge_boundary
    from ohm.validation import validate_confidence, validate_identifier

    edge_id = validate_identifier(edge_id, name="edge_id")
    confidence = validate_confidence(confidence)
    enforce_challenge_boundary(conn, created_by, edge_id)

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
    edges_from = conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE from_node = ? AND deleted_at IS NULL", [node_id]).fetchone()
    edges_to = conn.execute("UPDATE ohm_edges SET deleted_at = CURRENT_TIMESTAMP WHERE to_node = ? AND deleted_at IS NULL", [node_id]).fetchone()
    edges_deleted = (edges_from[0] if edges_from else 0) + (edges_to[0] if edges_to else 0)

    # Delete observations
    obs_result = conn.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE node_id = ? AND deleted_at IS NULL", [node_id])
    obs_row = obs_result.fetchone()
    obs_count = obs_row[0] if obs_row else 0

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

    # Delete observations referencing this edge
    obs_result = conn.execute("UPDATE ohm_observations SET deleted_at = CURRENT_TIMESTAMP WHERE edge_id = ? AND deleted_at IS NULL", [edge_id])

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
) -> dict[str, Any]:
    """Create an observation on a node or edge and return its full record."""
    from ohm.graph.schema import VALID_OBSERVATION_SCALES

    if scale is not None and scale not in VALID_OBSERVATION_SCALES:
        raise ValueError(
            f"Invalid scale '{scale}' — must be one of: {', '.join(sorted(VALID_OBSERVATION_SCALES))}"
        )
    if scale == "probability" and value is not None and (value < 0.0 or value > 1.0):
        raise ValueError(
            f"Observation value {value} is outside [0, 1] for scale='probability'"
        )
    import uuid

    obs_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_observations
           (id, node_id, edge_id, type, value, baseline, sigma, source, created_by, notes, source_name, source_url, scale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [obs_id, node_id, edge_id, obs_type, value, baseline, sigma, source, created_by, notes, source_name, source_url, scale],
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

    return {
        "orphan_nodes": orphans,
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
        defaults.update(half_life_days)

    when_clauses = " ".join(
        f"WHEN '{k}' THEN {999999.0 if v == float('inf') or v <= 0 else v}"
        for k, v in defaults.items()
    )
    hl_case = f"CASE layer {when_clauses} ELSE 90.0 END"

    result = conn.execute(f"""
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
              AND (deleted_at IS NULL OR deleted_at = '')
              AND {hl_case} > 0
              AND {hl_case} < 999999.0
        )
        SELECT * FROM decayed
        WHERE effective_confidence < ?
        ORDER BY effective_confidence ASC
    """, [stale_threshold])

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
                "SELECT * FROM ohm_nodes WHERE id = ? AND created_at <= ?",
                [node_id, timestamp],
            )
        )
    else:
        nodes = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_nodes WHERE created_at <= ? ORDER BY created_at",
                [timestamp],
            )
        )

    # ── Edges ──────────────────────────────────────────────────────────
    if edge_id:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE id = ? AND created_at <= ?",
                [edge_id, timestamp],
            )
        )
    elif node_id:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND created_at <= ? ORDER BY created_at",
                [node_id, node_id, timestamp],
            )
        )
    else:
        edges = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_edges WHERE created_at <= ? ORDER BY created_at",
                [timestamp],
            )
        )

    # ── Observations ───────────────────────────────────────────────────
    if node_id:
        observations = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_observations WHERE node_id = ? AND created_at <= ? ORDER BY created_at",
                [node_id, timestamp],
            )
        )
    else:
        observations = _rows_to_dicts(
            conn.execute(
                "SELECT * FROM ohm_observations WHERE created_at <= ? ORDER BY created_at",
                [timestamp],
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

    Returns:
        List of floats (embedding vector) or None on failure.
    """
    if not text or not text.strip():
        return None

    from ohm.graph.embeddings import OllamaBackend

    backend = OllamaBackend(model=model, ollama_url=ollama_url)
    embeddings = backend.embed([text])
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

    Returns:
        List of dicts with node_id, label, type, distance, and confidence.
    """
    if not query or not query.strip():
        return []

    embedding = generate_embedding(query)
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
    return _rows_to_dicts(result)


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


def update_node_embedding(
    conn: "DuckDBPyConnection",
    node_id: str,
    text: str | None = None,
) -> bool:
    """Generate and store an embedding for a node.

    Generates an embedding from the node's label (or custom text)
    and updates the embedding column. Returns False if Ollama is
    unavailable or the node doesn't exist.

    Args:
        conn: Database connection.
        node_id: ID of the node to update.
        text: Optional custom text to embed. Defaults to node label.

    Returns:
        True if embedding was updated, False otherwise.
    """
    from ohm.validation import validate_identifier

    node_id = validate_identifier(node_id, name="node_id")

    # Get node label if no custom text provided
    if text is None:
        result = conn.execute("SELECT label FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
        if result is None:
            return False
        text = result[0]

    if not text:
        return False

    embedding = generate_embedding(text)
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

        edge_id = create_edge(
            conn, from_node=from_node, to_node=to_node,
            edge_type=ohm_edge_type, layer=edge_layer,
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
    url_match = re.search(r'https?://\S+', content)
    if url_match:
        url = url_match.group(0).rstrip('.,;:)')

    node_id = generate_node_id(label)

    metadata = None
    is_question = "?" in content
    if tags or is_question:
        metadata = {}
        if tags:
            metadata["tags"] = tags
        if is_question:
            metadata["is_question"] = True

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
            explicit_links.append({
                "node_id": target_id,
                "label": _existing_label(conn, target_id),
                "edge_id": edge["id"],
                "edge_type": "CONTEXT_OF",
                "provenance": "scratch_explicit",
            })
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
                conn, fragment_id, embedding, created_by,
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
        matched.append({
            "node_id": candidate["id"],
            "label": candidate["label"],
            "edge_id": edge["id"],
            "provenance": "auto_link_semantic",
            "similarity": round(1.0 - candidate["distance"], 4),
        })

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
            matched.append({
                "node_id": candidate["id"],
                "label": candidate["label"],
                "edge_id": edge["id"],
                "provenance": "auto_link_substring",
            })

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
        resonance_edges.append({
            "node_id": fid,
            "edge_id": edge["id"],
            "edge_type": "RESONANCE",
            "shared_targets": info["shared_targets"],
            "shared_count": len(info["shared_targets"]),
        })

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

    Args:
        conn: Database connection.
        fragment_id: ID of the fragment to promote.
        promoted_by: Agent performing the promotion.

    Returns:
        Dict with the new concept node and the created edge.

    Raises:
        NodeNotFoundError: If fragment doesn't exist.
        ValueError: If node is not a fragment.
    """
    from ohm.exceptions import NodeNotFoundError

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

        results.append({
            "fragment_a": frag_a,
            "fragment_b": frag_b,
            "agent_a": agent_a,
            "agent_b": agent_b,
            "shared_context_nodes": sorted(shared),
            "shared_count": len(shared),
            "jaccard": round(jaccard, 3),
        })

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
            [ann_id, claim_node, frag_id, challenged_by,
             f"auto: challenge backflow from {challenge_edge_id}"],
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

        result.append({
            "cluster_size": len(cluster),
            "fragment_ids": sorted(cluster),
            "shared_target_count": len(cluster_targets),
            "shared_target_ids": sorted(cluster_targets),
        })

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
