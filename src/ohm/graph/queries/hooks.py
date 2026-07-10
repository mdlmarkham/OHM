"""hooks queries (OHM-447).

Extracted from queries/__init__.py as part of the large-module decomposition.
All functions are re-exported from __init__.py — callers should not import
this module directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries._shared import _log_change, _rows_to_dicts

# ── Hook Registry CRUD ──────────────────────────────────────────────────────


def create_hook(
    conn: DuckDBPyConnection,
    *,
    event: str,
    command: str,
    created_by: str,
    timeout_ms: int = 5000,
    enabled: bool = True,
) -> dict[str, Any]:
    """Register a new hook in the ohm_hooks table.

    Args:
        event: One of pre_ingest, post_ingest, pre_query, post_query.
        command: Shell command or python:module.function.
        created_by: Agent registering the hook.
        timeout_ms: Timeout in milliseconds (100–60000).
        enabled: Whether the hook is active.

    Returns:
        The created hook record.
    """
    import uuid

    from ohm.hooks import VALID_HOOK_EVENTS
    from ohm.validation import validate_identifier

    if event not in VALID_HOOK_EVENTS:
        raise ValueError(f"Invalid hook event: {event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")
    if not command or not isinstance(command, str):
        raise ValueError("command must be a non-empty string")
    if not (100 <= timeout_ms <= 60000):
        raise ValueError(f"timeout_ms must be 100–60000, got {timeout_ms}")
    created_by = validate_identifier(created_by, name="created_by")

    hook_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO ohm_hooks
           (id, event, command, timeout_ms, enabled, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [hook_id, event, command, timeout_ms, enabled, created_by],
    )
    _log_change(conn, "ohm_hooks", hook_id, "INSERT", created_by)
    return _rows_to_dicts(conn.execute("SELECT * FROM ohm_hooks WHERE id = ?", [hook_id]))[0]


def query_hooks(
    conn: DuckDBPyConnection,
    *,
    event: str | None = None,
) -> list[dict[str, Any]]:
    """List registered hooks, optionally filtered by event.

    Args:
        event: If provided, filter to this event type.

    Returns:
        List of hook records ordered by created_at.
    """
    from ohm.hooks import VALID_HOOK_EVENTS

    if event is not None and event not in VALID_HOOK_EVENTS:
        raise ValueError(f"Invalid hook event: {event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")

    if event:
        result = conn.execute(
            "SELECT * FROM ohm_hooks WHERE event = ? ORDER BY created_at ASC",
            [event],
        )
    else:
        result = conn.execute(
            "SELECT * FROM ohm_hooks ORDER BY created_at ASC",
        )
    return _rows_to_dicts(result)


def delete_hook(
    conn: DuckDBPyConnection,
    *,
    hook_id: str,
    deleted_by: str,
) -> dict[str, Any]:
    """Delete a hook by ID.

    Args:
        hook_id: The hook to remove.
        deleted_by: Agent performing the deletion.

    Returns:
        Dict with the deleted hook_id.

    Raises:
        ValueError if the hook doesn't exist.
    """
    from ohm.validation import validate_identifier

    hook_id = validate_identifier(hook_id, name="hook_id")
    deleted_by = validate_identifier(deleted_by, name="deleted_by")

    existing = conn.execute("SELECT id FROM ohm_hooks WHERE id = ?", [hook_id]).fetchone()
    if not existing:
        raise ValueError(f"Hook not found: {hook_id}")

    conn.execute("DELETE FROM ohm_hooks WHERE id = ?", [hook_id])
    _log_change(conn, "ohm_hooks", hook_id, "DELETE", deleted_by)
    return {"deleted": hook_id, "type": "hook"}
