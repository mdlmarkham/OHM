"""
OHM Boundary Enforcement — ADR-003

Rules:
1. Any agent can write to L1 and L2 (with attribution)
2. Only the owning agent can update L3/L4 edges
3. Any agent can challenge any L3/L4 edge (creates new edge, never modifies)
4. No agent can delete another agent's edge
5. L1/L2 edges cannot be challenged or supported
6. L2 nodes are immutable after creation (no updates, only new edges)
7. L1 identity edges (VALUES, GOALS, CAPABLE_OF, INTERESTED_IN) can be
   updated by the owning agent with evolution tracking

Multi-tenancy (OHM-l1vs):
8. Customer API writes use created_by='customer:{customer_id}' format
9. Customer identities follow the same L3/L4 ownership rules as agents
10. Tenant isolation (current_store routing) prevents cross-tenant violations
    — boundary.py does not need a tenant_id parameter
"""

from ohm.exceptions import EdgeNotFoundError, PermissionDeniedError


def is_customer_identity(agent_name: str) -> bool:
    """Check if agent_name is a customer API identity (OHM-l1vs)."""
    return agent_name.startswith("customer:")


def customer_id_from_identity(agent_name: str) -> str | None:
    """Extract customer_id from a 'customer:{id}' identity string."""
    if agent_name.startswith("customer:"):
        return agent_name[len("customer:") :]
    return None


def check_can_write_layer(agent_name: str, layer: str) -> None:
    """All layers are writable for new edges. No restriction on creation."""
    pass


def check_can_update_edge(agent_name: str, edge_owner: str, edge_id: str) -> None:
    """Only the owning agent can update their own edges."""
    if agent_name != edge_owner:
        raise PermissionDeniedError(f"Agent '{agent_name}' cannot update edge '{edge_id}' owned by '{edge_owner}'. Use challenge/support edges instead.")


def check_can_delete_node(agent_name: str, node_owner: str, node_id: str) -> None:
    """Only the owning agent can delete their own nodes."""
    if agent_name != node_owner:
        raise PermissionDeniedError(f"Agent '{agent_name}' cannot delete node '{node_id}' owned by '{node_owner}'.")


def check_can_delete_edge(agent_name: str, edge_owner: str, edge_id: str) -> None:
    """Only the owning agent can delete their own edges."""
    if agent_name != edge_owner:
        raise PermissionDeniedError(f"Agent '{agent_name}' cannot delete edge '{edge_id}' owned by '{edge_owner}'.")


def check_can_challenge(agent_name: str, layer: str) -> None:
    """Any agent or customer identity can challenge L3/L4 edges. L1/L2 cannot be challenged (OHM-l1vs)."""
    if layer in ("L1", "L2"):
        raise PermissionDeniedError(f"Cannot challenge {layer} edges. {layer} edges are shared and authoritative.")


def check_can_support(agent_name: str, layer: str) -> None:
    """Any agent or customer identity can support L3/L4 edges. L1/L2 cannot be supported (OHM-l1vs)."""
    if layer in ("L1", "L2"):
        raise PermissionDeniedError(f"Cannot support {layer} edges. {layer} edges are shared and authoritative.")


def check_can_update_l2_node(agent_name: str, node_id: str, conn) -> None:
    """L2 nodes (sources, citations) are immutable after creation.

    L2 nodes represent shared authoritative references. Once created,
    they cannot be updated by anyone. If the reference is wrong, create
    a new node and link with DERIVES_FROM or CORRECTIONS edges.
    """
    result = conn.execute(
        "SELECT type, created_by FROM ohm_nodes WHERE id = ?",
        [node_id],
    ).fetchone()
    if result is None:
        return  # New node — allowed
    node_type, owner = result
    if node_type == "source":
        raise PermissionDeniedError(f"Cannot update L2 source node '{node_id}'. Sources are immutable after creation. Create a new source node and link with DERIVES_FROM instead.")


def check_can_evolve_identity_edge(agent_name: str, edge_owner: str, edge_type: str) -> None:
    """L1 identity edges (VALUES, GOALS, CAPABLE_OF, INTERESTED_IN) can be
    evolved by the owning agent.

    Evolution is not modification — it's a directed replacement where
    the old edge is marked superseded and a new edge is created.
    The change feed preserves the full history.
    """
    IDENTITY_EDGE_TYPES = frozenset({"VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN"})

    if edge_type not in IDENTITY_EDGE_TYPES:
        raise PermissionDeniedError(f"Cannot evolve non-identity edge type '{edge_type}'. Only VALUES, GOALS, CAPABLE_OF, INTERESTED_IN edges can be evolved.")
    if agent_name != edge_owner:
        raise PermissionDeniedError(f"Agent '{agent_name}' cannot evolve identity edge owned by '{edge_owner}'. Only the owning agent can evolve their own identity edges.")


def get_edge_owner(conn, edge_id: str) -> str:
    """Return the owning agent name for an edge."""
    result = conn.execute(
        "SELECT created_by FROM ohm_edges WHERE id = ?",
        [edge_id],
    ).fetchone()
    if result is None:
        raise EdgeNotFoundError(f"Edge '{edge_id}' not found")
    return result[0]


def get_edge_layer(conn, edge_id: str) -> str:
    """Return the layer for an edge."""
    result = conn.execute(
        "SELECT layer FROM ohm_edges WHERE id = ?",
        [edge_id],
    ).fetchone()
    if result is None:
        raise EdgeNotFoundError(f"Edge '{edge_id}' not found")
    return result[0]


def get_edge_type(conn, edge_id: str) -> str:
    """Return the edge type."""
    result = conn.execute(
        "SELECT edge_type FROM ohm_edges WHERE id = ?",
        [edge_id],
    ).fetchone()
    if result is None:
        raise EdgeNotFoundError(f"Edge '{edge_id}' not found")
    return result[0]


def enforce_write_boundary(conn, agent_name: str, edge_id: str) -> None:
    """Enforce boundary rule: only the owner can update their edge."""
    owner = get_edge_owner(conn, edge_id)
    check_can_update_edge(agent_name, owner, edge_id)


def enforce_challenge_boundary(conn, agent_name: str, edge_id: str) -> None:
    """Enforce boundary rule: can only challenge L3/L4 edges."""
    layer = get_edge_layer(conn, edge_id)
    check_can_challenge(agent_name, layer)


def enforce_support_boundary(conn, agent_name: str, edge_id: str) -> None:
    """Enforce boundary rule: can only support L3/L4 edges."""
    layer = get_edge_layer(conn, edge_id)
    check_can_support(agent_name, layer)


def enforce_l2_immutability(conn, agent_name: str, node_id: str) -> None:
    """Enforce L2 node immutability: sources cannot be updated after creation."""
    check_can_update_l2_node(agent_name, node_id, conn)


def enforce_identity_evolution(conn, agent_name: str, edge_id: str) -> None:
    """Enforce identity evolution: only owner can evolve L1 identity edges."""
    owner = get_edge_owner(conn, edge_id)
    edge_type = get_edge_type(conn, edge_id)
    check_can_evolve_identity_edge(agent_name, owner, edge_type)


def get_agent_read_scope(conn, agent_name: str) -> dict | None:
    """Resolve an agent's read scope from ohm_agent_config (OHM-ybyb, ADR-037).

    Returns the read_scope JSON dict, or None for full access (backward compat).
    """
    row = conn.execute(
        "SELECT read_scope FROM ohm_agent_config WHERE agent_name = ?",
        [agent_name],
    ).fetchone()
    if not row or row[0] is None:
        return None
    import json

    try:
        scope = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return scope
    except (json.JSONDecodeError, TypeError):
        return None


def enforce_read_scope(
    conn,
    agent_name: str,
    *,
    layer: str | None = None,
    source_tier: str | None = None,
    node_id: str | None = None,
    created_by: str | None = None,
) -> None:
    """Enforce read-scope restrictions for an agent (OHM-ybyb, ADR-037).

    Raises PermissionDeniedError if the agent's read_scope excludes the
    requested resource. NULL scope = full access (backward compat).
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return

    if layer is not None:
        allowed_layers = scope.get("layer")
        if allowed_layers is not None and layer not in allowed_layers:
            raise PermissionDeniedError(f"Agent '{agent_name}' read scope excludes layer '{layer}'")

    if source_tier is not None:
        allowed_tiers = scope.get("source_tier")
        if allowed_tiers is not None and source_tier not in allowed_tiers:
            raise PermissionDeniedError(f"Agent '{agent_name}' read scope excludes source_tier '{source_tier}'")

    if created_by is not None:
        allowed_creators = scope.get("created_by")
        if allowed_creators is not None and created_by not in allowed_creators:
            raise PermissionDeniedError(f"Agent '{agent_name}' read scope excludes nodes by '{created_by}'")

    if node_id is not None:
        allowed_nodes = scope.get("node_id")
        if allowed_nodes is not None and node_id not in allowed_nodes:
            raise PermissionDeniedError(f"Agent '{agent_name}' read scope excludes node '{node_id}'")


def set_agent_read_scope(conn, agent_name: str, scope: dict | None) -> dict:
    """Set or clear an agent's read scope (OHM-ybyb, ADR-037)."""
    import json

    from ohm.validation import validate_read_scope

    scope = validate_read_scope(scope)
    scope_json = json.dumps(scope) if scope is not None else None

    existing = conn.execute(
        "SELECT agent_name FROM ohm_agent_config WHERE agent_name = ?",
        [agent_name],
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE ohm_agent_config SET read_scope = ? WHERE agent_name = ?",
            [scope_json, agent_name],
        )
    else:
        conn.execute(
            "INSERT INTO ohm_agent_config (agent_name, optimization_target, read_scope) VALUES (?, ?, ?)",
            [agent_name, "balanced", scope_json],
        )
    return {"agent_name": agent_name, "read_scope": scope}


def apply_read_scope_filters(
    conn,
    agent_name: str,
    *,
    table_alias: str = "",
) -> tuple[list[str], list]:
    """Build SQL WHERE-clause fragments for an agent's read scope (OHM-oqyc).

    Returns ``(conditions, params)`` where ``conditions`` is a list of
    SQL fragments (e.g. ``"created_by IN (?, ?)"``) and ``params`` is the
    corresponding parameter list. Append these to any node/edge query to
    enforce read scope at the SQL level.

    For node queries, only ``created_by``, ``source_tier``, and ``node_id``
    dimensions are applied (nodes have no ``layer`` field). For edge
    queries, all four dimensions are applied.

    Returns ``([], [])`` if the agent has no scope set (full access).

    Args:
        conn: Active DuckDB connection.
        agent_name: The agent whose scope to apply.
        table_alias: Optional table alias prefix (e.g. ``"n."`` for
            ``ohm_nodes n``). Empty string means no prefix.
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return [], []

    conditions: list[str] = []
    params: list = []
    prefix = table_alias

    allowed_creators = scope.get("created_by")
    if allowed_creators is not None:
        placeholders = ", ".join("?" * len(allowed_creators))
        conditions.append(f"{prefix}created_by IN ({placeholders})")
        params.extend(allowed_creators)

    allowed_tiers = scope.get("source_tier")
    if allowed_tiers is not None:
        placeholders = ", ".join("?" * len(allowed_tiers))
        conditions.append(f"{prefix}source_tier IN ({placeholders})")
        params.extend(allowed_tiers)

    allowed_nodes = scope.get("node_id")
    if allowed_nodes is not None:
        placeholders = ", ".join("?" * len(allowed_nodes))
        conditions.append(f"{prefix}id IN ({placeholders})")
        params.extend(allowed_nodes)

    return conditions, params


def filter_results_by_read_scope(
    conn,
    agent_name: str,
    results: list[dict],
    *,
    id_field: str = "id",
    created_by_field: str = "created_by",
    source_tier_field: str = "source_tier",
    layer_field: str | None = None,
) -> list[dict]:
    """Post-filter a list of result dicts by an agent's read scope (OHM-oqyc).

    Used for endpoints that return pre-computed results (e.g. semantic
    search) where SQL-level filtering isn't practical. Returns only the
    results the agent is permitted to see.

    Args:
        conn: Active DuckDB connection.
        agent_name: The agent whose scope to enforce.
        results: List of result dicts.
        id_field: Dict key for the node/edge id (default ``"id"``).
        created_by_field: Dict key for the creator (default ``"created_by"``).
        source_tier_field: Dict key for source tier (default ``"source_tier"``).
        layer_field: Optional dict key for layer (applies to edge results).

    Returns:
        Filtered list of results. If no scope is set, returns all results.
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return results

    filtered: list[dict] = []
    for r in results:
        if layer_field and layer_field in r:
            allowed_layers = scope.get("layer")
            if allowed_layers is not None and r.get(layer_field) not in allowed_layers:
                continue

        allowed_creators = scope.get("created_by")
        if allowed_creators is not None and r.get(created_by_field) not in allowed_creators:
            continue

        allowed_tiers = scope.get("source_tier")
        if allowed_tiers is not None and r.get(source_tier_field) not in allowed_tiers:
            continue

        allowed_nodes = scope.get("node_id")
        if allowed_nodes is not None and r.get(id_field) not in allowed_nodes:
            continue

        filtered.append(r)

    return filtered


def enforce_read_scope_for_edge(conn, agent_name: str, edge: dict) -> None:
    """Enforce read scope on a single edge and both endpoint nodes.

    Edges themselves have no ``source_tier`` column, so visibility to an edge
    requires visibility to both nodes it connects. This helper checks edge
    ``layer``/``created_by`` and then looks up the two endpoint nodes and
    enforces node scope on each.
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return

    # Edge-level dimensions
    enforce_read_scope(
        conn,
        agent_name,
        layer=edge.get("layer"),
        created_by=edge.get("created_by"),
    )

    from_node = edge.get("from_node")
    to_node = edge.get("to_node")
    if not from_node or not to_node:
        return

    rows = conn.execute(
        "SELECT id, source_tier, created_by FROM ohm_nodes WHERE id IN (?, ?) AND deleted_at IS NULL",
        [from_node, to_node],
    ).fetchall()
    node_map = {row[0]: {"source_tier": row[1], "created_by": row[2]} for row in rows}
    for node_id in (from_node, to_node):
        node = node_map.get(node_id)
        if node is None:
            raise PermissionDeniedError(f"Agent '{agent_name}' cannot read edge: endpoint node '{node_id}' is not visible")
        enforce_read_scope(
            conn,
            agent_name,
            node_id=node_id,
            source_tier=node.get("source_tier"),
            created_by=node.get("created_by"),
        )


def filter_edges_by_read_scope(
    conn,
    agent_name: str,
    edges: list[dict],
    endpoint_nodes: dict[str, dict] | None = None,
) -> list[dict]:
    """Post-filter a list of edges by an agent's read scope.

    Uses SQL-level scope when possible, but this helper is useful for
    endpoints (e.g. neighborhood) that compute edges before scope is known.

    Args:
        conn: Active DuckDB connection.
        agent_name: The agent whose scope to enforce.
        edges: List of edge dicts with ``from_node`` and ``to_node``.
        endpoint_nodes: Optional map of node_id -> node dict. If not
            provided, endpoint nodes are looked up in one SQL query.

    Returns:
        Filtered list of edges. If no scope is set, returns all edges.
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return edges

    if endpoint_nodes is None:
        node_ids = set()
        for e in edges:
            node_ids.add(e.get("from_node"))
            node_ids.add(e.get("to_node"))
        node_ids.discard(None)
        node_map: dict[str, dict] = {}
        if node_ids:
            placeholders = ", ".join("?" * len(node_ids))
            rows = conn.execute(
                f"SELECT id, source_tier, created_by FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                list(node_ids),
            ).fetchall()
            node_map = {row[0]: {"source_tier": row[1], "created_by": row[2]} for row in rows}
    else:
        node_map = endpoint_nodes

    filtered: list[dict] = []
    for e in edges:
        allowed_layers = scope.get("layer")
        if allowed_layers is not None and e.get("layer") not in allowed_layers:
            continue

        allowed_creators = scope.get("created_by")
        if allowed_creators is not None and e.get("created_by") not in allowed_creators:
            continue

        from_node = e.get("from_node")
        to_node = e.get("to_node")
        from_node_data = node_map.get(from_node)
        to_node_data = node_map.get(to_node)
        if from_node_data is None or to_node_data is None:
            continue

        allowed_tiers = scope.get("source_tier")
        if allowed_tiers is not None:
            if from_node_data.get("source_tier") not in allowed_tiers:
                continue
            if to_node_data.get("source_tier") not in allowed_tiers:
                continue

        if allowed_creators is not None:
            if from_node_data.get("created_by") not in allowed_creators:
                continue
            if to_node_data.get("created_by") not in allowed_creators:
                continue

        allowed_nodes = scope.get("node_id")
        if allowed_nodes is not None:
            if from_node not in allowed_nodes or to_node not in allowed_nodes:
                continue

        filtered.append(e)

    return filtered


def apply_read_scope_edge_filters(
    conn,
    agent_name: str,
    *,
    edge_alias: str = "e.",
    from_alias: str = "ns_from",
    to_alias: str = "ns_to",
) -> tuple[list[str], list[str], list]:
    """Build SQL WHERE-clause fragments for edge-list queries (GET /edges).

    Unlike ``apply_read_scope_filters``, this helper knows that edges have
    no ``source_tier`` column. It joins ``ohm_nodes`` for both endpoints
    and applies source_tier / created_by / node_id scope to each endpoint,
    while applying layer / created_by scope to the edge itself.

    Returns:
        ``(joins, conditions, params)`` ready to splice into a query.
    """
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return [], [], []

    joins: list[str] = []
    conditions: list[str] = []
    params: list[Any] = []
    e_prefix = edge_alias

    allowed_layers = scope.get("layer")
    if allowed_layers is not None:
        placeholders = ", ".join("?" * len(allowed_layers))
        conditions.append(f"{e_prefix}layer IN ({placeholders})")
        params.extend(allowed_layers)

    allowed_creators = scope.get("created_by")
    if allowed_creators is not None:
        placeholders = ", ".join("?" * len(allowed_creators))
        conditions.append(f"{e_prefix}created_by IN ({placeholders})")
        params.extend(allowed_creators)

    joins.append(
        f"JOIN ohm_nodes {from_alias} ON {from_alias}.id = {e_prefix}from_node AND {from_alias}.deleted_at IS NULL"
    )
    joins.append(
        f"JOIN ohm_nodes {to_alias} ON {to_alias}.id = {e_prefix}to_node AND {to_alias}.deleted_at IS NULL"
    )

    allowed_tiers = scope.get("source_tier")
    if allowed_tiers is not None:
        placeholders = ", ".join("?" * len(allowed_tiers))
        for alias in (from_alias, to_alias):
            conditions.append(
                f"({alias}.source_tier IS NULL OR {alias}.source_tier IN ({placeholders}))"
            )
            params.extend(allowed_tiers)

    if allowed_creators is not None:
        placeholders = ", ".join("?" * len(allowed_creators))
        for alias in (from_alias, to_alias):
            conditions.append(f"{alias}.created_by IN ({placeholders})")
            params.extend(allowed_creators)

    allowed_nodes = scope.get("node_id")
    if allowed_nodes is not None:
        placeholders = ", ".join("?" * len(allowed_nodes))
        for alias in (from_alias, to_alias):
            conditions.append(f"{alias}.id IN ({placeholders})")
            params.extend(allowed_nodes)

    return joins, conditions, params
