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
