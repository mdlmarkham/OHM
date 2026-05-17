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
"""

from .exceptions import EdgeNotFoundError, PermissionDeniedError, ValidationError


def check_can_write_layer(agent_name: str, layer: str) -> None:
    """All layers are writable for new edges. No restriction on creation."""
    pass


def check_can_update_edge(agent_name: str, edge_owner: str, edge_id: str) -> None:
    """Only the owning agent can update their own edges."""
    if agent_name != edge_owner:
        raise PermissionDeniedError(
            f"Agent '{agent_name}' cannot update edge '{edge_id}' owned by '{edge_owner}'. "
            f"Use challenge/support edges instead."
        )


def check_can_delete_edge(agent_name: str, edge_owner: str, edge_id: str) -> None:
    """Only the owning agent can delete their own edges."""
    if agent_name != edge_owner:
        raise PermissionDeniedError(
            f"Agent '{agent_name}' cannot delete edge '{edge_id}' owned by '{edge_owner}'."
        )


def check_can_challenge(agent_name: str, layer: str) -> None:
    """Any agent can challenge L3/L4 edges. L1/L2 cannot be challenged."""
    if layer in ("L1", "L2"):
        raise PermissionDeniedError(
            f"Cannot challenge {layer} edges. {layer} edges are shared and authoritative."
        )


def check_can_support(agent_name: str, layer: str) -> None:
    """Any agent can support L3/L4 edges. L1/L2 cannot be supported."""
    if layer in ("L1", "L2"):
        raise PermissionDeniedError(
            f"Cannot support {layer} edges. {layer} edges are shared and authoritative."
        )


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
        raise PermissionDeniedError(
            f"Cannot update L2 source node '{node_id}'. "
            f"Sources are immutable after creation. "
            f"Create a new source node and link with DERIVES_FROM instead."
        )


def check_can_evolve_identity_edge(agent_name: str, edge_owner: str, edge_type: str) -> None:
    """L1 identity edges (VALUES, GOALS, CAPABLE_OF, INTERESTED_IN) can be
    evolved by the owning agent.

    Evolution is not modification — it's a directed replacement where
    the old edge is marked superseded and a new edge is created.
    The change feed preserves the full history.
    """
    IDENTITY_EDGE_TYPES = frozenset({"VALUES", "GOALS", "CAPABLE_OF", "INTERESTED_IN"})

    if edge_type not in IDENTITY_EDGE_TYPES:
        raise PermissionDeniedError(
            f"Cannot evolve non-identity edge type '{edge_type}'. "
            f"Only VALUES, GOALS, CAPABLE_OF, INTERESTED_IN edges can be evolved."
        )
    if agent_name != edge_owner:
        raise PermissionDeniedError(
            f"Agent '{agent_name}' cannot evolve identity edge owned by '{edge_owner}'. "
            f"Only the owning agent can evolve their own identity edges."
        )


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
