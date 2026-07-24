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


def query_edges(
    conn: DuckDBPyConnection,
    *,
    from_node: str | None = None,
    to_node: str | None = None,
    edge_type: str | list[str] | None = None,
    layer: str | None = None,
    created_by: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List edges with optional filtering and pagination (OHM-972).

    Returns active edges by default (``deleted_at IS NULL``). Pass
    ``include_deleted=True`` to also return soft-deleted rows.

    Args:
        conn: DuckDB connection.
        from_node: Filter by exact from_node id.
        to_node: Filter by exact to_node id.
        edge_type: Filter by edge type (string or list of strings).
        layer: Filter by layer (e.g. "L3").
        created_by: Filter by creating agent.
        limit: Max results (default 100).
        offset: Pagination offset (default 0).
        include_deleted: If True, include soft-deleted edges.

    Returns:
        List of edge dicts with keys: id, from_node, to_node, edge_type,
        layer, confidence, probability, created_by, created_at, deleted_at.
    """
    from ohm.validation import validate_identifier, validate_layer

    conditions: list[str] = []
    params: list[Any] = []

    if not include_deleted:
        conditions.append("deleted_at IS NULL")

    if from_node is not None:
        from_node = validate_identifier(from_node, name="from_node")
        conditions.append("from_node = ?")
        params.append(from_node)

    if to_node is not None:
        to_node = validate_identifier(to_node, name="to_node")
        conditions.append("to_node = ?")
        params.append(to_node)

    if edge_type is not None:
        if isinstance(edge_type, str):
            edge_type = [edge_type]
        placeholders = ",".join(["?"] * len(edge_type))
        conditions.append(f"edge_type IN ({placeholders})")
        params.extend(edge_type)

    if layer is not None:
        layer = validate_layer(layer)
        conditions.append("layer = ?")
        params.append(layer)

    if created_by is not None:
        conditions.append("created_by = ?")
        params.append(created_by)

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)
    params.append(offset)

    sql = (
        "SELECT id, from_node, to_node, edge_type, layer, "
        "confidence, probability, created_by, created_at, deleted_at "
        "FROM ohm_edges "
        f"WHERE {where_clause} "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    result = conn.execute(sql, params)
    return _rows_to_dicts(result)


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

    # Edge counts by layer (OHM-776: filter deleted_at IS NULL to match total_edges)
    result = conn.execute("""
        SELECT layer, COUNT(*) AS count
        FROM ohm_edges
        WHERE deleted_at IS NULL
        GROUP BY layer
        ORDER BY layer
    """)
    stats["edges_by_layer"] = {row[0]: row[1] for row in result.fetchall()}

    # Edge counts by type (OHM-776: filter deleted_at IS NULL to match total_edges)
    result = conn.execute("""
        SELECT edge_type, COUNT(*) AS count
        FROM ohm_edges
        WHERE deleted_at IS NULL
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
        WHERE value IS NOT NULL AND deleted_at IS NULL
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
        ORDER BY obs_count DESC, n.id ASC
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
    from datetime import timezone

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
              WHERE e.deleted_at IS NULL
                AND (e.from_node = n.id OR e.to_node = n.id)
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
              WHERE e.deleted_at IS NULL
                AND (e.from_node = n.id OR e.to_node = n.id)
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
    from datetime import timezone

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
    register_twin,
    twin_predict,
    twin_constraints,
    validate_action_against_twin,
    explain_twin,
    create_twin_template,
    list_twin_templates,
    get_twin_template,
    instantiate_twin_from_template,
    assemble_twin_for_decision,
    register_model_candidate,
    evaluate_model,
    compare_models,
    promote_model,
    register_shadow_model,
    detect_drift,
    run_walk_forward_validation,
    ensemble_predict,
    compute_decision_value,
    auto_retire_model,
    set_freshness_threshold,
    get_freshness_status,
    start_twin_design_session,
    transition_session,
    add_session_observation,
    propose_twin_config,
    review_proposal,
    instantiate_from_session,
    record_calibration,
    evolve_session,
    get_session_state,
    get_session_audit,
    set_promotion_policy,
    auto_promote_best_model,
    register_twin_with_bindings,
    add_twin_bindings,
    attach_twin_models,
    get_twin_readiness,
)
from ohm.graph.queries.feed_investment import (
    compute_feed_investment,
    recommend_mode,
    record_mode_switch,
    get_current_mode,
    temporal_decision_summary,
    VALID_SESSION_STATES,
    SESSION_TRANSITIONS,
)

# OHM-447 Phase 3: mid-weight domains re-exports
from ohm.graph.queries.cascade import (
    query_deterministic_cascade,
    query_cascade_scenario,
    monte_carlo_cascade,
    query_what_if,
    propagate_observation,
)
from ohm.graph.queries.handoff import (
    query_handoff,
    query_escalate,
    query_ticket_provenance,
)
from ohm.graph.queries.embeddings import (
    generate_embedding,
    semantic_search,
    search,
    fuzzy_search,
    update_node_embedding,
)
from ohm.graph.queries.discovery import (
    queue_discovery_candidates,
    query_discovery_queue,
    review_discovery_candidate,
)
from ohm.graph.queries.fragments import (
    scratch,
    resolve_question,
    promote_fragment,
    detect_fragment_resonance,
    detect_fragment_clusters,
    evict_expired_fragments,
    hd_membership_search,
    batch_update_hd_fingerprints,
    query_fragment_clusters,
    reflect_challenge_to_fragments,
    update_node_hd_fingerprint,
)
from ohm.graph.queries.suggestions import (
    create_suggestion,
    query_suggestions,
    promote_suggestion,
    reject_suggestion,
    expire_suggestions,
    batch_orphan_triage,
    query_claim_lineage,
    query_confidence_report,
    query_contradiction_summary,
    query_neighborhood_narrative,
    query_task_context,
)
from ohm.graph.queries.cascade_scenario import (
    query_counterfactual_cascade,
    query_compare_scenarios,
)
from ohm.graph.queries.actions import (
    propose_action,
    execute_action,
)
from ohm.graph.queries.loop_status import (
    query_loop_status,
)
from ohm.graph.queries.verification import (
    detect_verifiable_claims,
    create_verification_nudge,
    record_verification_outcome,
    list_pending_verifications,
)

# OHM-447 Phase 4: confidence/decay re-exports (must come before injection)
from ohm.graph.queries.confidence import (
    apply_confidence_decay,
    compute_confidence_with_decay,
    apply_decay_to_edges,
    log_confidence_change,
    recompute_confidence_from_log,
    get_confidence_history,
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

import ohm.graph.queries.rul as _rul_mod

_rul_mod.node_exists = node_exists

import ohm.graph.queries.cascade_scenario as _cs_mod

_cs_mod.query_deterministic_cascade = query_deterministic_cascade

import ohm.graph.queries.loop_status as _ls_mod

_ls_mod.apply_decay_to_edges = apply_decay_to_edges
_ls_mod.compute_confidence_with_decay = compute_confidence_with_decay

import ohm.graph.queries.verification as _ver_mod

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

# OHM-447 Phase 4: inject into confidence module
import ohm.graph.queries.confidence as _conf_mod

_conf_mod.query_stale_edges = query_stale_edges
_conf_mod.create_node = create_node
_conf_mod.create_edge = create_edge


# OHM-447 Phase 5: changefeed + outcomes re-exports (must come BEFORE
# the injection block below, since some injected names are defined here)
from ohm.graph.queries.changefeed import (
    query_change_feed,
    query_agent_changes,
    query_threat_cluster,
    restore_outcomes_from_change_feed,
    query_record_outcome,
    query_close_task_with_outcome,
    query_source_reliability,
    query_agent_state,
)

# OHM-447 Phase 5: inject into suggestions module (was missing entirely)
import ohm.graph.queries.suggestions as _sugg_mod

_sugg_mod.query_neighborhood = query_neighborhood

# OHM-447 Phase 5: inject query_record_outcome into verification module
_ver_mod.query_record_outcome = query_record_outcome

# OHM-447 Phase 5: inject query_source_reliability into data_products module
_dp_mod.query_source_reliability = query_source_reliability


# ── OHM-802: External signal attachments ────────────────────────────────────


def create_external_signal(
    conn: "DuckDBPyConnection",
    *,
    node_id: str,
    source_type: str,
    source_id: str | None = None,
    source_path: str | None = None,
    unit: str | None = None,
    domain: str = "ohm",
    metadata: dict | None = None,
    created_by: str = "system",
) -> dict[str, Any]:
    """Attach an external signal to a graph node (OHM-802).

    Idempotent: if a signal with the same (node_id, source_type, source_id)
    already exists and is not deleted, returns the existing record.
    """
    import json as _json

    # Check for existing (idempotency)
    if source_id:
        result = conn.execute(
            "SELECT * FROM external_signals WHERE node_id = ? AND source_type = ? AND source_id = ? AND deleted_at IS NULL LIMIT 1",
            [node_id, source_type, source_id],
        )
        columns = [desc[0] for desc in result.description]
        row = result.fetchone()
        if row:
            return dict(zip(columns, row))

    metadata_json = _json.dumps(metadata) if metadata else None
    conn.execute(
        """
        INSERT INTO external_signals (node_id, source_type, source_id, source_path, unit, domain, metadata, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [node_id, source_type, source_id, source_path, unit, domain, metadata_json, created_by],
    )

    result = conn.execute(
        "SELECT * FROM external_signals WHERE node_id = ? AND source_type = ? AND created_by = ? ORDER BY created_at DESC LIMIT 1",
        [node_id, source_type, created_by],
    )
    columns = [desc[0] for desc in result.description]
    row = result.fetchone()
    if row:
        return dict(zip(columns, row))
    return {}


def get_external_signals(
    conn: "DuckDBPyConnection",
    node_id: str,
    *,
    source_type: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    """Get external signals attached to a node (OHM-802)."""
    query = "SELECT * FROM external_signals WHERE node_id = ? AND deleted_at IS NULL"
    params: list[Any] = [node_id]
    if source_type:
        query += " AND source_type = ?"
        params.append(source_type)
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY created_at DESC"

    result = conn.execute(query, params)
    columns = [desc[0] for desc in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def delete_external_signal(conn: "DuckDBPyConnection", signal_id: str) -> bool:
    """Soft-delete an external signal attachment (OHM-802)."""
    existing = conn.execute(
        "SELECT id FROM external_signals WHERE id = ? AND deleted_at IS NULL",
        [signal_id],
    ).fetchone()
    if not existing:
        return False
    conn.execute(
        "UPDATE external_signals SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
        [signal_id],
    )
    return True


# ── Backend introspection (OHM #917) ────────────────────────────────────────


def _version_key(v: str) -> tuple[int, ...]:
    """Convert a dotted version string to a comparable tuple."""
    parts: list[int] = []
    for chunk in v.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def query_backend_status(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Database-queryable backend status (OHM #917).

    Returns the parts of the backend status payload that can be derived from
    a bare ``DuckDBPyConnection``: the persisted schema version, the list of
    pending migrations, and graph-size counts (nodes, edges, observations,
    fragments). Store-level attributes (db_path, store_type, storage_bytes,
    write_mode, tenant, sync status, uptime) are assembled by the HTTP handler
    from the ``OhmStore`` instance — they are not available from a conn alone.

    Read-only — no side effects.
    """
    from ohm.graph.schema import MIGRATIONS, get_schema_version

    schema_version = get_schema_version(conn)
    current_key = _version_key(schema_version)
    pending: list[str] = []
    for version, _desc, _stmts in MIGRATIONS:
        if current_key < _version_key(version):
            pending.append(version)

    nodes_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'"
    ).fetchone()
    nodes = nodes_row[0] if nodes_row else 0

    edges_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL"
    ).fetchone()
    edges = edges_row[0] if edges_row else 0

    obs_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_observations WHERE deleted_at IS NULL"
    ).fetchone()
    observations = obs_row[0] if obs_row else 0

    fragments_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type = 'fragment'"
    ).fetchone()
    fragments = fragments_row[0] if fragments_row else 0

    return {
        "schema_version": schema_version,
        "pending_migrations": pending,
        "graph_size": {
            "nodes": nodes,
            "edges": edges,
            "observations": observations,
            "fragments": fragments,
        },
    }


def _storage_recommendation(
    deleted_rows: int,
    active_rows: int,
    fragment_ratio: float,
    orphan_rate: float,
    embedding_coverage: float,
) -> str:
    """Heuristic recommendation string for storage efficiency (OHM #917)."""
    if active_rows > 0 and deleted_rows > 0.05 * active_rows:
        return "Consider compaction — deleted-row count exceeds 5% of active rows."
    if orphan_rate > 0.10:
        return "High orphan rate — review and connect isolated nodes."
    if embedding_coverage < 0.50:
        return "Low embedding coverage — run /admin/embeddings to backfill semantic search."
    if fragment_ratio > 0.50:
        return "High fragment density — consider promoting or evicting L0 fragments."
    return "Storage health is good — no action needed."


def query_storage_efficiency(conn: "DuckDBPyConnection") -> dict[str, Any]:
    """Storage efficiency health signals (OHM #917).

    Returns:
        - ``deleted_rows_estimate``: soft-deleted node + edge rows.
        - ``fragment_ratio``: fragment-type nodes / active non-fragment nodes.
        - ``orphan_rate``: non-fragment orphan rate (reuses ``query_graph_health``).
        - ``embedding_coverage``: nodes with embeddings / all active nodes.
        - ``recommendation``: heuristic action string.

    Read-only — no side effects. Degrades gracefully when optional columns
    (e.g. ``embedding``) are absent.
    """
    deleted_nodes_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NOT NULL"
    ).fetchone()
    deleted_nodes = deleted_nodes_row[0] if deleted_nodes_row else 0

    deleted_edges_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NOT NULL"
    ).fetchone()
    deleted_edges = deleted_edges_row[0] if deleted_edges_row else 0

    deleted_rows_estimate = deleted_nodes + deleted_edges

    total_nodes_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type != 'fragment'"
    ).fetchone()
    total_nodes = total_nodes_row[0] if total_nodes_row else 0

    total_edges_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_edges WHERE deleted_at IS NULL"
    ).fetchone()
    total_edges = total_edges_row[0] if total_edges_row else 0

    fragment_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND type = 'fragment'"
    ).fetchone()
    fragment_count = fragment_row[0] if fragment_row else 0
    fragment_ratio = round(fragment_count / total_nodes, 4) if total_nodes > 0 else 0.0

    health = query_graph_health(conn)
    orphan_rate = health.get("orphan_rate_non_fragments", 0.0)

    all_active_nodes_row = conn.execute(
        "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL"
    ).fetchone()
    all_active_nodes = all_active_nodes_row[0] if all_active_nodes_row else 0

    nodes_with_embedding = 0
    try:
        emb_row = conn.execute(
            "SELECT COUNT(*) FROM ohm_nodes WHERE deleted_at IS NULL AND embedding IS NOT NULL"
        ).fetchone()
        nodes_with_embedding = emb_row[0] if emb_row else 0
    except Exception:
        pass
    embedding_coverage = (
        round(nodes_with_embedding / all_active_nodes, 4) if all_active_nodes > 0 else 0.0
    )

    active_rows = total_nodes + total_edges
    recommendation = _storage_recommendation(
        deleted_rows_estimate,
        active_rows,
        fragment_ratio,
        orphan_rate,
        embedding_coverage,
    )

    return {
        "deleted_rows_estimate": deleted_rows_estimate,
        "fragment_ratio": fragment_ratio,
        "orphan_rate": orphan_rate,
        "embedding_coverage": embedding_coverage,
        "recommendation": recommendation,
    }
