"""actions queries (OHM-447 Phase 3).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts, _percentile


def propose_action(
    conn: DuckDBPyConnection,
    *,
    scenario_id: str,
    label: str,
    created_by: str,
    rationale: str | None = None,
    connects_to: list[str] | None = None,
) -> dict[str, Any]:
    """Propose an action linked to a scenario (OHM-446a).

    Creates an ``action`` node and links it to the scenario via a
    ``PROPOSES_ACTION`` L3 edge. The action starts in 'proposed' status
    (stored in task_status column for compatibility).

    Args:
        conn: Database connection.
        scenario_id: The scenario node that suggests this action.
        label: Human-readable action description.
        created_by: Agent proposing the action.
        rationale: Optional explanation of why this action.
        connects_to: Additional nodes to cross-link (ADR-018).

    Returns:
        The created action node record.
    """
    from ohm.validation import validate_identifier

    scenario_id = validate_identifier(scenario_id, name="scenario_id")

    # Create the action node, linked to the scenario
    all_connects = [scenario_id] + (connects_to or [])
    action = create_node(
        conn,
        label=label,
        node_type="action",
        content=rationale,
        created_by=created_by,
        connects_to=all_connects,
    )

    # Set task_status to 'proposed' (reusing the existing column)
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'proposed' WHERE id = ?",
        [action["id"]],
    )

    # Create the PROPOSES_ACTION edge: scenario → action
    create_edge(
        conn,
        from_node=scenario_id,
        to_node=action["id"],
        edge_type="PROPOSES_ACTION",
        layer="L3",
        created_by=created_by,
    )

    # Return updated record
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action["id"]]))[0]


def execute_action(
    conn: DuckDBPyConnection,
    *,
    action_id: str,
    executed_by: str,
    outcome: str | None = None,
    outcome_notes: str | None = None,
) -> dict[str, Any]:
    """Mark an action as executed and record the outcome (OHM-446a).

    Sets the action's task_status to 'executed', records the outcome
    (TRUE/FALSE/AMBIGUOUS), and creates an EXECUTED_BY L4 edge from
    the action to the executing agent.

    Args:
        conn: Database connection.
        action_id: The action node to execute.
        executed_by: Agent executing the action.
        outcome: TRUE/FALSE/AMBIGUOUS/DEFERRED.
        outcome_notes: Free-text notes on the execution result.

    Returns:
        The updated action node record.
    """
    from ohm.validation import validate_identifier

    action_id = validate_identifier(action_id, name="action_id")

    # Verify the action exists
    row = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND type = 'action' AND deleted_at IS NULL",
        [action_id],
    ).fetchone()
    if not row:
        from ohm.exceptions import NodeNotFoundError

        raise NodeNotFoundError(f"Action not found: {action_id}")

    # Update the action status
    now_sql = "CURRENT_TIMESTAMP"
    conn.execute(
        "UPDATE ohm_nodes SET task_status = 'executed', outcome = ?, outcome_notes = ?, updated_at = " + now_sql + ", updated_by = ? WHERE id = ?",
        [outcome, outcome_notes, executed_by, action_id],
    )

    # Find or create the agent node for EXECUTED_BY edge
    agent_row = conn.execute(
        "SELECT id FROM ohm_nodes WHERE label = ? AND type = 'agent' AND deleted_at IS NULL LIMIT 1",
        [executed_by],
    ).fetchone()

    if agent_row:
        agent_id = agent_row[0]
    else:
        # Create a minimal agent node
        agent = create_node(
            conn,
            label=executed_by,
            node_type="agent",
            created_by=executed_by,
        )
        agent_id = agent["id"]

    # Create the EXECUTED_BY edge: action → agent
    create_edge(
        conn,
        from_node=action_id,
        to_node=agent_id,
        edge_type="EXECUTED_BY",
        layer="L4",
        created_by=executed_by,
    )

    _log_change(conn, "ohm_nodes", action_id, "EXECUTE", executed_by)

    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_nodes WHERE id = ?", [action_id]))[0]


