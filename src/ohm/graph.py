"""
OHM Graph — recursive CTE queries for neighborhood, path, and impact analysis.

All queries are standard SQL (no extensions required) and work through Quack.
Depth is bounded to prevent runaway recursion.
"""

from typing import Optional


# Bounded-depth neighborhood traversal (default depth=3, max=5)
NEIGHBORHOOD_CTE = """
WITH RECURSIVE neighborhood AS (
    -- Base: start node
    SELECT
        e.from_node,
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        e.challenge_type,
        e.challenge_of,
        1 AS depth,
        ARRAY[e.id] AS path
    FROM ohm_edges e
    WHERE e.from_node = ? OR e.to_node = ?

    UNION ALL

    -- Recursive: follow edges from discovered nodes
    SELECT
        e.from_node,
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        e.challenge_type,
        e.challenge_of,
        n.depth + 1 AS depth,
        array_append(n.path, e.id) AS path
    FROM ohm_edges e
    JOIN neighborhood n ON (
        e.from_node = n.to_node
        OR e.to_node = n.from_node
        OR e.from_node = n.from_node
        OR e.to_node = n.to_node
    )
    WHERE n.depth < ?
      AND e.id NOT IN (SELECT unnest(n.path))  -- prevent cycles
)
SELECT DISTINCT
    n.from_node,
    n.to_node,
    n.edge_id,
    n.edge_type,
    n.layer,
    n.confidence,
    n.created_by,
    n.challenge_type,
    n.depth
FROM neighborhood n
ORDER BY n.depth, n.layer
"""

# Shortest path between two nodes (BFS via CTE)
PATH_CTE = """
WITH RECURSIVE path_search AS (
    -- Base: start node
    SELECT
        e.from_node,
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        1 AS depth,
        ARRAY[e.id] AS edge_path,
        ARRAY[e.from_node, e.to_node] AS node_path
    FROM ohm_edges e
    WHERE e.from_node = ?

    UNION ALL

    -- Recursive: expand frontier
    SELECT
        e.from_node,
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        p.depth + 1 AS depth,
        array_append(p.edge_path, e.id) AS edge_path,
        array_append(p.node_path, e.to_node) AS node_path
    FROM ohm_edges e
    JOIN path_search p ON e.from_node = p.to_node
    WHERE p.depth < ?
      AND e.to_node NOT IN (SELECT unnest(p.node_path))  -- no revisiting
)
SELECT
    edge_path,
    node_path,
    depth,
    array_agg(edge_type) AS edge_types,
    array_agg(layer) AS layers,
    avg(confidence) AS avg_confidence
FROM path_search
WHERE to_node = ?
GROUP BY edge_path, node_path, depth
ORDER BY depth, avg_confidence DESC
LIMIT 5
"""

# Impact analysis: all downstream nodes from a given node
# Follows L2 (flow) and L3 (knowledge) edges forward
IMPACT_CTE = """
WITH RECURSIVE impact AS (
    -- Base: direct downstream from start node
    SELECT
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        e.condition,
        1 AS depth,
        ARRAY[e.id] AS path
    FROM ohm_edges e
    WHERE e.from_node = ?
      AND e.layer IN ('L2', 'L3')
      AND e.edge_type NOT IN ('CHALLENGED_BY', 'CONTRADICTS')  -- don't follow challenges downstream

    UNION ALL

    -- Recursive: follow downstream from discovered nodes
    SELECT
        e.to_node,
        e.id AS edge_id,
        e.edge_type,
        e.layer,
        e.confidence,
        e.created_by,
        e.condition,
        i.depth + 1 AS depth,
        array_append(i.path, e.id) AS path
    FROM ohm_edges e
    JOIN impact i ON e.from_node = i.to_node
    WHERE i.depth < ?
      AND e.layer IN ('L2', 'L3')
      AND e.edge_type NOT IN ('CHALLENGED_BY', 'CONTRADICTS')
      AND e.id NOT IN (SELECT unnest(i.path))
)
SELECT DISTINCT
    i.to_node AS impacted_node,
    i.edge_type,
    i.layer,
    i.confidence,
    i.created_by,
    i.condition,
    i.depth,
    n.label AS node_label,
    n.type AS node_type
FROM impact i
JOIN ohm_nodes n ON n.id = i.to_node
ORDER BY i.depth, i.layer, i.confidence DESC
"""

# Confidence audit: all challenges and supports for an edge
CONFIDENCE_AUDIT = """
SELECT
    e.id AS edge_id,
    e.edge_type,
    e.confidence,
    e.created_by,
    e.created_at,
    e.updated_at,
    e.updated_by,
    e.challenge_of,
    e.challenge_type,
    COALESCE(challenge_count.cnt, 0) AS challenge_count,
    COALESCE(support_count.cnt, 0) AS support_count,
    COALESCE(challenge_avg.avg_conf, 0) AS avg_challenge_confidence,
    COALESCE(support_avg.avg_conf, 0) AS avg_support_confidence
FROM ohm_edges e
LEFT JOIN (
    SELECT challenge_of, COUNT(*) AS cnt
    FROM ohm_edges
    WHERE challenge_type = 'CHALLENGED_BY' AND challenge_of = ?
    GROUP BY challenge_of
) challenge_count ON challenge_count.challenge_of = e.id
LEFT JOIN (
    SELECT challenge_of, COUNT(*) AS cnt
    FROM ohm_edges
    WHERE challenge_type = 'SUPPORTS' AND challenge_of = ?
    GROUP BY challenge_of
) support_count ON support_count.challenge_of = e.id
LEFT JOIN (
    SELECT challenge_of, AVG(confidence) AS avg_conf
    FROM ohm_edges
    WHERE challenge_type = 'CHALLENGED_BY' AND challenge_of = ?
    GROUP BY challenge_of
) challenge_avg ON challenge_avg.challenge_of = e.id
LEFT JOIN (
    SELECT challenge_of, AVG(confidence) AS avg_conf
    FROM ohm_edges
    WHERE challenge_type = 'SUPPORTS' AND challenge_of = ?
    GROUP BY challenge_of
) support_avg ON support_avg.challenge_of = e.id
WHERE e.id = ?
"""

# Change feed: what changed since a given timestamp
CHANGE_FEED = """
SELECT
    c.table_name,
    c.row_id,
    c.operation,
    c.agent_name,
    c.layer,
    c.changed_at,
    c.change_data
FROM ohm_change_log c
WHERE c.changed_at > ?
ORDER BY c.changed_at ASC
"""

# Change feed filtered by agent (what did others write?)
CHANGE_FEED_OTHER_AGENTS = """
SELECT
    c.table_name,
    c.row_id,
    c.operation,
    c.agent_name,
    c.layer,
    c.changed_at,
    c.change_data
FROM ohm_change_log c
WHERE c.changed_at > ?
  AND c.agent_name != ?
ORDER BY c.changed_at ASC
"""


def build_neighborhood_query(
    node_id: str,
    depth: int = 3,
    layer: Optional[str] = None,
    edge_type: Optional[str] = None,
) -> tuple[str, list]:
    """Build a parameterized neighborhood query.

    Returns (sql, params) for execution.
    """
    params = [node_id, node_id, min(depth, 5)]

    sql = NEIGHBORHOOD_CTE

    # Wrap with filters
    filter_parts = []
    if layer:
        filter_parts.append("n.layer = ?")
        params.append(layer)
    if edge_type:
        filter_parts.append("n.edge_type = ?")
        params.append(edge_type)

    if filter_parts:
        sql = sql.replace(
            "ORDER BY n.depth, n.layer",
            "WHERE " + " AND ".join(filter_parts) + "\nORDER BY n.depth, n.layer",
        )

    return sql, params


def build_path_query(
    from_node: str, to_node: str, max_depth: int = 5
) -> tuple[str, list]:
    """Build a shortest-path query.

    Returns (sql, params) for execution.
    """
    params = [from_node, min(max_depth, 5), to_node]
    return PATH_CTE, params


def build_impact_query(node_id: str, depth: int = 5) -> tuple[str, list]:
    """Build an impact analysis query.

    Returns (sql, params) for execution.
    """
    params = [node_id, min(depth, 5)]
    return IMPACT_CTE, params


def build_confidence_audit_query(edge_id: str) -> tuple[str, list]:
    """Build a confidence audit query for an edge.

    Returns (sql, params) for execution.
    """
    params = [edge_id, edge_id, edge_id, edge_id, edge_id]
    return CONFIDENCE_AUDIT, params


def build_change_feed_query(
    since: str, agent_name: Optional[str] = None
) -> tuple[str, list]:
    """Build a change feed query.

    Args:
        since: ISO timestamp for "changes since"
        agent_name: If provided, exclude this agent's own changes

    Returns (sql, params) for execution.
    """
    if agent_name:
        params = [since, agent_name]
        return CHANGE_FEED_OTHER_AGENTS, params
    else:
        params = [since]
        return CHANGE_FEED, params
