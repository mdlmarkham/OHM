"""handoff queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile


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


