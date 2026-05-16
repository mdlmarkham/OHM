"""
OHM Boundary Enforcement — ADR-003

Rules:
1. Any agent can write to L1 and L2 (with attribution)
2. Only the owning agent can update L3/L4 edges
3. Any agent can challenge any L3/L4 edge (creates new edge, never modifies)
4. No agent can delete another agent's edge
5. L1/L2 edges cannot be challenged or supported
"""

from .exceptions import EdgeNotFoundError, PermissionDeniedError


def check_can_write_layer(agent_name: str, layer: str) -> None:
    """All layers are writable for new edges. No restriction on creation."""
    # Any agent can write to any layer — attribution is on the edge itself
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
