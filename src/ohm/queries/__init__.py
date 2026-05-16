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
    layer_where = f"AND e.layer = '{layer}'" if layer else ""

    # Build direction join condition
    if direction == "outgoing":
        join_on = "e.from_node = v.node"
    elif direction == "incoming":
        join_on = "e.to_node = v.node"
    else:
        join_on = "(e.from_node = v.node OR e.to_node = v.node)"

    query = f"""
        WITH RECURSIVE visited AS (
            SELECT '{node_id}' AS node, 0 AS hop
            UNION
            SELECT DISTINCT
                CASE WHEN e.from_node = v.node THEN e.to_node ELSE e.from_node END AS node,
                v.hop + 1 AS hop
            FROM visited v
            JOIN ohm_edges e ON {join_on}
            WHERE v.hop < {depth}
              {layer_where}
        )
        SELECT DISTINCT
            e.id AS edge_id,
            e.from_node,
            e.to_node,
            e.layer,
            e.edge_type,
            e.confidence,
            e.created_by,
            e.created_at,
            e.challenge_of,
            e.challenge_type,
            v.hop
        FROM visited v
        JOIN ohm_edges e ON (e.from_node = v.node OR e.to_node = v.node)
        {layer_where}
        ORDER BY v.hop, e.edge_type
    """

    result = conn.execute(query)
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
    """Shortest path between *from_node* and *to_node* using BFS.

    Returns the ordered list of edges forming the path, or empty list if
    no path exists within *max_depth*.
    """
    layer_where = f"AND e.layer = '{layer}'" if layer else ""

    query = f"""
        WITH RECURSIVE path_cte AS (
            SELECT
                e.id AS edge_id,
                e.from_node,
                e.to_node,
                e.layer,
                e.edge_type,
                e.confidence,
                1 AS depth
            FROM ohm_edges e
            WHERE (e.from_node = '{from_node}' OR e.to_node = '{from_node}')
              {layer_where}

            UNION ALL

            SELECT
                e.id,
                e.from_node,
                e.to_node,
                e.layer,
                e.edge_type,
                e.confidence,
                p.depth + 1
            FROM path_cte p
            JOIN ohm_edges e ON e.from_node = p.to_node
            WHERE p.depth < {max_depth}
              AND p.to_node != '{to_node}'
              {layer_where}
        )
        SELECT edge_id, from_node, to_node, layer, edge_type, confidence, depth
        FROM path_cte
        WHERE to_node = '{to_node}'
        ORDER BY depth
        LIMIT 1
    """

    result = conn.execute(query)
    return _rows_to_dicts(result)


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
    query = f"""
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
            WHERE e.from_node = '{node_id}'
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
            WHERE i.depth < {depth}
              AND e.layer IN ('L2', 'L3')
        )
        SELECT edge_id, from_node, to_node, layer, edge_type, confidence, depth
        FROM impact_cte
        ORDER BY depth, edge_type
    """

    result = conn.execute(query)
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
    query = f"""
        SELECT
            e.id,
            e.from_node,
            e.to_node,
            e.layer,
            e.edge_type,
            e.confidence,
            e.condition,
            e.provenance,
            e.created_by,
            e.created_at,
            e.updated_at,
            e.updated_by
        FROM ohm_edges e
        WHERE e.id = '{edge_id}'
    """
    original = conn.execute(query).fetchone()
    if original is None:
        return {"original": None, "challenges": [], "supports": [], "refinements": []}

    columns = [desc[0] for desc in conn.description]
    original_dict = dict(zip(columns, original))

    # Find all challenge/support/refine edges referencing this edge
    refs_query = f"""
        SELECT
            id, edge_type, confidence, condition, created_by, created_at
        FROM ohm_edges
        WHERE challenge_of = '{edge_id}'
        ORDER BY created_at DESC
    """
    refs_result = conn.execute(refs_query)
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
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve the change feed since a given timestamp.

    Args:
        conn: Database connection.
        since: ISO timestamp or 'last-check'. If None, returns recent changes.
        agent_name: Filter by agent.
        limit: Maximum number of changes to return.

    Returns:
        List of change feed entries ordered by time descending.
    """
    conditions = []
    if since and since != "last-check":
        conditions.append(f"occurred_at >= '{since}'::TIMESTAMP")
    if agent_name:
        conditions.append(f"agent_name = '{agent_name}'")

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
        LIMIT {limit}
    """

    result = conn.execute(query)
    return _rows_to_dicts(result)


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
    if agent_name:
        query = f"SELECT * FROM ohm_agent_state WHERE agent_name = '{agent_name}'"
    else:
        query = "SELECT * FROM ohm_agent_state"

    result = conn.execute(query)
    return _rows_to_dicts(result)


# ── Stats ───────────────────────────────────────────────────────────────────

def query_stats(conn: DuckDBPyConnection) -> dict[str, Any]:
    """Aggregate graph statistics.

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

    # Total counts
    stats["total_nodes"] = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()[0]
    stats["total_edges"] = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()[0]
    stats["total_observations"] = conn.execute("SELECT COUNT(*) FROM ohm_observations").fetchone()[0]

    # Challenge ratio
    total_l3_l4 = conn.execute("""
        SELECT COUNT(*) FROM ohm_edges WHERE layer IN ('L3', 'L4')
    """).fetchone()[0]
    challenged = conn.execute("""
        SELECT COUNT(DISTINCT challenge_of) FROM ohm_edges
        WHERE challenge_of IS NOT NULL
    """).fetchone()[0]
    stats["challenge_ratio"] = round(challenged / total_l3_l4, 4) if total_l3_l4 > 0 else 0.0

    # Active agents
    stats["active_agents"] = conn.execute("""
        SELECT COUNT(*) FROM ohm_agent_state
        WHERE last_sync IS NOT NULL
          AND last_sync > CURRENT_TIMESTAMP - INTERVAL '1 hour'
    """).fetchone()[0]

    return stats
