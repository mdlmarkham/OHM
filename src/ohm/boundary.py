"""Boundary enforcement — ADR-003 agent-owned edge rules.

Core rules (from docs/schema.md and ADR-003):
    1. Any agent can write to L1 and L2 (with attribution)
    2. Only the owning agent can update L3/L4 edges
    3. Any agent can create CHALLENGED_BY, SUPPORTS, or REFINES edges
       referencing any L3/L4 edge
    4. No agent can delete another agent's edge
    5. Private layer is never shared or promoted automatically
    6. Promotion from private to shared is per-agent

Enforcement happens at the application layer (DuckDB has no REFERENCES
or row-level security).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ohm.exceptions import EdgeNotFoundError, PermissionDeniedError

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection


def check_can_write_layer(actor: str, layer: str) -> None:
    """Verify *actor* can write to *layer*.

    L1 and L2 are open to all agents. L3 and L4 are agent-owned —
    only the owning agent can update existing edges, but any agent
    can create new edges.
    """
    # All layers are writable for new edges — ownership is checked
    # at the edge level, not the layer level.
    _ = actor, layer  # no-op; layer-level write is always allowed


def check_can_update_edge(actor: str, edge_owner: str, edge_id: str) -> None:
    """Verify *actor* can update an existing edge.

    Only the owning agent can update L3/L4 edges. L1/L2 edges
    can be updated by any agent (with attribution).
    """
    if actor != edge_owner:
        raise PermissionDeniedError(
            f"Agent '{actor}' cannot update edge '{edge_id}' "
            f"(owned by '{edge_owner}')"
        )


def check_can_delete_edge(actor: str, edge_owner: str, edge_id: str) -> None:
    """Verify *actor* can delete an edge.

    No agent can delete another agent's edge (rule 4).
    """
    if actor != edge_owner:
        raise PermissionDeniedError(
            f"Agent '{actor}' cannot delete edge '{edge_id}' "
            f"(owned by '{edge_owner}')"
        )


def check_can_challenge(actor: str, target_layer: str) -> None:
    """Verify *actor* can challenge an edge.

    Any agent can challenge any L3/L4 edge. L1/L2 edges cannot
    be challenged (they are structural/flow, not knowledge).
    """
    if target_layer not in ("L3", "L4"):
        raise PermissionDeniedError(
            f"Cannot challenge {target_layer} edges — only L3 and L4 "
            f"edges are challengeable"
        )


def check_can_support(actor: str, target_layer: str) -> None:
    """Verify *actor* can support an edge.

    Any agent can support any L3/L4 edge.
    """
    if target_layer not in ("L3", "L4"):
        raise PermissionDeniedError(
            f"Cannot support {target_layer} edges — only L3 and L4 "
            f"edges are supportable"
        )


def get_edge_owner(conn: DuckDBPyConnection, edge_id: str) -> str:
    """Return the *created_by* field for an edge.

    Raises:
        EdgeNotFoundError: If the edge does not exist.
    """
    result = conn.execute(
        "SELECT created_by, layer FROM ohm_edges WHERE id = ?", [edge_id]
    ).fetchone()
    if result is None:
        raise EdgeNotFoundError(f"Edge not found: {edge_id}")
    return str(result[0])


def get_edge_layer(conn: DuckDBPyConnection, edge_id: str) -> str:
    """Return the *layer* field for an edge.

    Raises:
        EdgeNotFoundError: If the edge does not exist.
    """
    result = conn.execute(
        "SELECT layer FROM ohm_edges WHERE id = ?", [edge_id]
    ).fetchone()
    if result is None:
        raise EdgeNotFoundError(f"Edge not found: {edge_id}")
    return str(result[0])


def enforce_write_boundary(
    conn: DuckDBPyConnection,
    actor: str,
    edge_id: str,
) -> None:
    """Full boundary check before updating an existing edge.

    Combines ownership check with existence validation.
    """
    owner = get_edge_owner(conn, edge_id)
    check_can_update_edge(actor, owner, edge_id)


def enforce_delete_boundary(
    conn: DuckDBPyConnection,
    actor: str,
    edge_id: str,
) -> None:
    """Full boundary check before deleting an edge."""
    owner = get_edge_owner(conn, edge_id)
    check_can_delete_edge(actor, owner, edge_id)


def enforce_challenge_boundary(
    conn: DuckDBPyConnection,
    actor: str,
    edge_id: str,
) -> None:
    """Full boundary check before challenging an edge."""
    layer = get_edge_layer(conn, edge_id)
    check_can_challenge(actor, layer)


def enforce_support_boundary(
    conn: DuckDBPyConnection,
    actor: str,
    edge_id: str,
) -> None:
    """Full boundary check before supporting an edge."""
    layer = get_edge_layer(conn, edge_id)
    check_can_support(actor, layer)
