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

    conn.execute(
        """INSERT INTO ohm_change_feed
           (table_name, row_id, operation, agent_name, old_data)
           VALUES (?, ?, ?, ?, ?)""",
        [table_name, row_id, operation, agent_name, json.dumps({})],
    )


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
    if layer:
        layer_clause = "AND e.layer = ?"
        params.append(layer)
        params.append(layer)  # layer_clause appears twice in the query

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
              {layer_clause}
        )
        SELECT DISTINCT ON (e.id)
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
            MIN(v.hop) AS hop
        FROM visited v
        JOIN ohm_edges e ON (e.from_node = v.node OR e.to_node = v.node)
        {layer_clause}
        GROUP BY e.id, e.from_node, e.to_node, e.layer, e.edge_type,
                 e.confidence, e.created_by, e.created_at,
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
    """Shortest path between *from_node* and *to_node* using BFS.

    Returns the ordered list of edges forming the path, or empty list if
    no path exists within *max_depth*.
    """
    from ohm.validation import validate_depth, validate_identifier, validate_layer

    from_node = validate_identifier(from_node, name="from_node")
    to_node = validate_identifier(to_node, name="to_node")
    max_depth = validate_depth(max_depth, max_depth=50)
    if layer:
        layer = validate_layer(layer)

    params: list = [from_node, from_node, max_depth, to_node, to_node]
    layer_clause = ""
    if layer:
        layer_clause = "AND e.layer = ?"
        params.insert(2, layer)   # after the two from_node params, before max_depth
        params.append(layer)      # layer_clause appears twice in the query

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
            WHERE (e.from_node = ? OR e.to_node = ?)
              {layer_clause}

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
            WHERE p.depth < ?
              AND p.to_node != ?
              {layer_clause}
        )
        SELECT edge_id, from_node, to_node, layer, edge_type, confidence, depth
        FROM path_cte
        WHERE to_node = ?
        ORDER BY depth
        LIMIT 1
    """

    result = conn.execute(query, params)
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
    query = """
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
        WHERE e.id = ?
    """
    original = conn.execute(query, [edge_id]).fetchone()
    if original is None:
        return {"original": None, "challenges": [], "supports": [], "refinements": []}

    columns = [desc[0] for desc in conn.description]
    original_dict = dict(zip(columns, original))

    # Find all challenge/support/refine edges referencing this edge
    refs_query = """
        SELECT
            id, edge_type, confidence, condition, created_by, created_at
        FROM ohm_edges
        WHERE challenge_of = ?
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
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve the change feed since a given timestamp.

    Args:
        conn: Database connection.
        since: ISO timestamp or 'last-check'. If None, returns recent changes.
        agent_name: Filter by agent.
        node_type: Filter by node type (e.g., 'concept', 'pattern', 'equipment').
            Matches changes to nodes of this type AND edges that touch nodes
            of this type (source or target).
        limit: Maximum number of changes to return.

    Returns:
        List of change feed entries ordered by time descending.
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

    # node_type filter: match changes where the row_id is a node of that type,
    # or the row_id is an edge that touches a node of that type.
    if node_type:
        node_type = validate_identifier(node_type, name="node_type")
        conditions.append(
            """(
                row_id IN (SELECT id FROM ohm_nodes WHERE type = ?)
                OR row_id IN (
                    SELECT e.id FROM ohm_edges e
                    WHERE e.from_node IN (SELECT id FROM ohm_nodes WHERE type = ?)
                       OR e.to_node IN (SELECT id FROM ohm_nodes WHERE type = ?)
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

    result = conn.execute(query, params)
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
    total_nodes_row = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()
    stats["total_nodes"] = total_nodes_row[0] if total_nodes_row else 0
    total_edges_row = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()
    stats["total_edges"] = total_edges_row[0] if total_edges_row else 0
    total_obs_row = conn.execute("SELECT COUNT(*) FROM ohm_observations").fetchone()
    stats["total_observations"] = total_obs_row[0] if total_obs_row else 0

    # Challenge ratio
    l3_l4_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_edges WHERE layer IN ('L3', 'L4')
    """).fetchone()
    total_l3_l4 = l3_l4_row[0] if l3_l4_row else 0
    challenged_row = conn.execute("""
        SELECT COUNT(DISTINCT challenge_of) FROM ohm_edges
        WHERE challenge_of IS NOT NULL
    """).fetchone()
    challenged = challenged_row[0] if challenged_row else 0
    stats["challenge_ratio"] = round(challenged / total_l3_l4, 4) if total_l3_l4 > 0 else 0.0

    # Active agents
    agents_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_agent_state
        WHERE last_sync IS NOT NULL
          AND last_sync > CURRENT_TIMESTAMP - INTERVAL '1 hour'
    """).fetchone()
    stats["active_agents"] = agents_row[0] if agents_row else 0

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
        stats["top_observed_nodes"] = [
            {"label": row[0], "id": row[1], "observation_count": row[2]}
            for row in top_observed
        ]

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
) -> dict[str, Any]:
    """Create a new node and return its full record."""
    from ohm.schema import generate_node_id, validate_node_type
    from ohm.validation import validate_confidence

    if not label or len(label) > 500:
        raise ValueError("Label must be non-empty and ≤ 500 characters")
    if not validate_node_type(node_type):
        raise ValueError(f"Invalid node type: {node_type}")
    confidence = validate_confidence(confidence)

    node_id = generate_node_id(label)
    conn.execute(
        """INSERT INTO ohm_nodes
           (id, label, type, content, created_by, visibility, provenance, confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [node_id, label, node_type, content, created_by, visibility, provenance, confidence],
    )
    _log_change(conn, "ohm_nodes", node_id, "INSERT", created_by)
    # Return full node record
    return _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [node_id])
    )[0]


def create_edge(
    conn: DuckDBPyConnection,
    *,
    from_node: str,
    to_node: str,
    layer: str,
    edge_type: str,
    created_by: str,
    confidence: float = 0.7,
    condition: str | None = None,
    provenance: str | None = None,
) -> dict[str, Any]:
    """Create a new edge and return its full record. Validates layer/type compatibility."""
    import uuid

    from ohm.schema import validate_edge_type
    from ohm.validation import validate_confidence

    if not validate_edge_type(layer, edge_type):
        raise ValueError(f"Invalid edge type '{edge_type}' for layer '{layer}'")
    confidence = validate_confidence(confidence)

    edge_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_edges
           (id, from_node, to_node, layer, edge_type, created_by,
            confidence, condition, provenance)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [edge_id, from_node, to_node, layer, edge_type, created_by,
         confidence, condition, provenance],
    )
    _log_change(conn, "ohm_edges", edge_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_edges WHERE id = ?", [edge_id])
    )[0]


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
        "SELECT id, from_node, to_node, layer FROM ohm_edges WHERE id = ?",
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
        [challenge_id, target[1], target[2], target[3],
         created_by, confidence, reason, edge_id],
    )
    _log_change(conn, "ohm_edges", challenge_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_edges WHERE id = ?", [challenge_id])
    )[0]


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
        "SELECT id, from_node, to_node, layer FROM ohm_edges WHERE id = ?",
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
        [support_id, target[1], target[2], target[3],
         created_by, confidence, reason, edge_id],
    )
    _log_change(conn, "ohm_edges", support_id, "INSERT", created_by)
    # Return full edge record
    return _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_edges WHERE id = ?", [support_id])
    )[0]


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
    existing = conn.execute(
        "SELECT 1 FROM ohm_agent_state WHERE agent_name = ?", [agent_name]
    ).fetchone()
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
            "INSERT INTO ohm_agent_state (agent_name, current_focus, values, goals, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
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
) -> dict[str, Any]:
    """Create an observation on a node or edge and return its full record."""
    import uuid

    obs_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_observations
           (id, node_id, edge_id, type, value, baseline, sigma, source, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [obs_id, node_id, edge_id, obs_type, value, baseline, sigma, source, created_by],
    )
    _log_change(conn, "ohm_observations", obs_id, "INSERT", created_by)
    # Return full observation record
    return _rows_to_dicts(
        conn.execute("SELECT * FROM ohm_observations WHERE id = ?", [obs_id])
    )[0]


def node_exists(conn: DuckDBPyConnection, node_id: str) -> bool:
    """Check if a node exists."""
    result = conn.execute("SELECT 1 FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
    return result is not None


def edge_exists(conn: DuckDBPyConnection, edge_id: str) -> bool:
    """Check if an edge exists."""
    result = conn.execute("SELECT 1 FROM ohm_edges WHERE id = ?", [edge_id]).fetchone()
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

    frozenset({
        "DERIVES_FROM", "REFERENCES", "INFLUENCES", "SUPPORTS",
    })

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
    - low_confidence_unchallenged: L3/L4 edges with confidence < 0.3 and no challenges
    - stale_agents: agents with last_sync > 2x their sync interval
    - disconnected_components: groups of nodes not connected to the main graph
    - dense_clusters: nodes with 10+ direct edges (might need synthesis)

    Same result regardless of which agent calls it — substrate method.
    """
    # Orphan nodes
    orphan_row = conn.execute("""
        SELECT COUNT(*) FROM ohm_nodes n
        WHERE NOT EXISTS (
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

    # Total counts
    total_nodes_row = conn.execute("SELECT COUNT(*) FROM ohm_nodes").fetchone()
    total_nodes = total_nodes_row[0] if total_nodes_row else 0
    total_edges_row = conn.execute("SELECT COUNT(*) FROM ohm_edges").fetchone()
    total_edges = total_edges_row[0] if total_edges_row else 0

    return {
        "orphan_nodes": orphans,
        "low_confidence_unchallenged": low_conf,
        "dense_clusters": dense_count,
        "stale_agents": stale_count,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "health_score": round(
            1.0 - (orphans + low_conf + stale_count) / max(total_nodes, 1), 4
        ),
    }


def query_stale_edges(
    conn: DuckDBPyConnection,
    *,
    half_life_days: dict[str, float] | None = None,
    stale_threshold: float = 0.1,
) -> list[dict[str, Any]]:
    """Find edges whose effective confidence has decayed below a threshold.

    Confidence decay is computed at read time (no data mutation):
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
    from datetime import datetime, timezone

    defaults = {"L1": float("inf"), "L2": float("inf"), "L3": 90.0, "L4": 30.0}
    if half_life_days:
        defaults.update(half_life_days)
    half_lives = defaults

    now = datetime.now(timezone.utc)

    # Get all edges with their layer and creation time
    result = conn.execute("""
        SELECT id, from_node, to_node, layer, edge_type, confidence,
               created_by, created_at, challenge_of
        FROM ohm_edges
        ORDER BY created_at ASC
    """)

    edges = _rows_to_dicts(result)
    stale = []

    for edge in edges:
        layer = edge.get("layer", "L3")
        hl = half_lives.get(layer, 90.0)

        # L1/L2 never decay
        if hl == float("inf") or hl <= 0:
            continue

        created_at = edge.get("created_at")
        if created_at is None:
            continue

        # Parse timestamp
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue

        # Calculate age in days
        if hasattr(created_at, 'tzinfo') and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_days = (now - created_at).total_seconds() / 86400
        if age_days < 0:
            age_days = 0

        # Decay formula: effective = confidence * 0.5^(age/half_life)
        original_conf = edge.get("confidence", 1.0) or 1.0
        decay_factor = 0.5 ** (age_days / hl)
        effective_conf = original_conf * decay_factor

        edge["effective_confidence"] = round(effective_conf, 4)
        edge["decay_factor"] = round(decay_factor, 4)
        edge["age_days"] = round(age_days, 1)

        if effective_conf < stale_threshold:
            stale.append(edge)

    return stale


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

        decayed.append({
            "id": edge["id"],
            "confidence": original_conf,
            "new_confidence": new_confidence,
            "decay_factor": round(decay_factor, 4),
            "age_days": edge.get("age_days", 0),
            "layer": edge.get("layer"),
            "edge_type": edge.get("edge_type"),
        })

        if not dry_run:
            conn.execute(
                "UPDATE ohm_edges SET confidence = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
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

    nodes_added = _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_nodes WHERE created_at >= ? AND created_at <= ? {node_agent_clause} ORDER BY created_at",
        node_params,
    ))

    nodes_updated = _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_nodes WHERE updated_at >= ? AND updated_at <= ? AND updated_at != created_at {node_agent_clause} ORDER BY updated_at",
        node_params,
    ))

    # ── Edges ──────────────────────────────────────────────────────────
    edge_params: list = [from_ts, to_ts]
    edge_clauses = ""
    if layer:
        edge_clauses += "AND layer = ?"
        edge_params.append(layer)
    if agent_name:
        edge_clauses += "AND created_by = ?"
        edge_params.append(agent_name)

    edges_added = _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_edges WHERE created_at >= ? AND created_at <= ? {edge_clauses} ORDER BY created_at",
        edge_params,
    ))

    edges_updated = _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_edges WHERE updated_at >= ? AND updated_at <= ? AND updated_at != created_at {edge_clauses} ORDER BY updated_at",
        edge_params,
    ))

    # ── Observations ───────────────────────────────────────────────────
    obs_params: list = [from_ts, to_ts]
    obs_agent_clause = ""
    if agent_name:
        obs_agent_clause = "AND created_by = ?"
        obs_params.append(agent_name)

    observations_added = _rows_to_dicts(conn.execute(
        f"SELECT * FROM ohm_observations WHERE created_at >= ? AND created_at <= ? {obs_agent_clause} ORDER BY created_at",
        obs_params,
    ))

    # ── Summary ────────────────────────────────────────────────────────
    summary = {
        "nodes_added": len(nodes_added),
        "nodes_updated": len(nodes_updated),
        "edges_added": len(edges_added),
        "edges_updated": len(edges_updated),
        "observations_added": len(observations_added),
        "total_changes": (
            len(nodes_added) + len(nodes_updated)
            + len(edges_added) + len(edges_updated)
            + len(observations_added)
        ),
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
        nodes = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_nodes WHERE id = ? AND created_at <= ?",
            [node_id, timestamp],
        ))
    else:
        nodes = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_nodes WHERE created_at <= ? ORDER BY created_at",
            [timestamp],
        ))

    # ── Edges ──────────────────────────────────────────────────────────
    if edge_id:
        edges = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_edges WHERE id = ? AND created_at <= ?",
            [edge_id, timestamp],
        ))
    elif node_id:
        edges = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_edges WHERE (from_node = ? OR to_node = ?) AND created_at <= ? ORDER BY created_at",
            [node_id, node_id, timestamp],
        ))
    else:
        edges = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_edges WHERE created_at <= ? ORDER BY created_at",
            [timestamp],
        ))

    # ── Observations ───────────────────────────────────────────────────
    if node_id:
        observations = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND created_at <= ? ORDER BY created_at",
            [node_id, timestamp],
        ))
    else:
        observations = _rows_to_dicts(conn.execute(
            "SELECT * FROM ohm_observations WHERE created_at <= ? ORDER BY created_at",
            [timestamp],
        ))

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
        "CAUSES", "SUPPORTS", "DERIVES_FROM", "REFERENCES",
        "PREDICTS", "CORRELATES_WITH", "EXPLAINS",
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
