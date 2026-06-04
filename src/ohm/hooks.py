"""Hook system for OHM staged ingestion pipeline (OHM-aznh).

Provides deterministic pre/post processing around graph writes.
Hooks are registered in the ohm_hooks table and executed by HookRunner.

Hook events:
  pre_ingest  - runs before graph write, can abort with non-zero exit
  post_ingest - runs after successful graph write, receives result payload
  pre_query   - runs before GET handlers, can modify query params
  post_query  - runs after GET handlers, can decorate response
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VALID_HOOK_EVENTS = frozenset({"pre_ingest", "post_ingest", "pre_query", "post_query"})


@dataclass
class HookRecord:
    """A registered hook from the ohm_hooks table."""

    id: str
    event: str
    command: str
    timeout_ms: int = 5000
    enabled: bool = True
    created_by: str = "system"
    created_at: str | None = None
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if self.event not in VALID_HOOK_EVENTS:
            raise ValueError(f"Invalid hook event: {self.event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")


@dataclass
class HookResult:
    """Result of a single hook invocation."""

    hook_id: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _rows_to_hook_records(result: Any) -> list[HookRecord]:
    """Convert DuckDB query result rows to HookRecord instances."""
    if not result:
        return []
    columns = [desc[0] for desc in result.description]
    records = []
    for row in result.fetchall():
        d = dict(zip(columns, row))
        records.append(
            HookRecord(
                id=str(d.get("id", "")),
                event=str(d.get("event", "")),
                command=str(d.get("command", "")),
                timeout_ms=int(d["timeout_ms"]) if d.get("timeout_ms") is not None else 5000,
                enabled=bool(d["enabled"]) if d.get("enabled") is not None else True,
                created_by=str(d.get("created_by", "system")),
                created_at=str(d["created_at"]) if d.get("created_at") is not None else None,
                updated_at=str(d["updated_at"]) if d.get("updated_at") is not None else None,
            )
        )
    return records


class HookRunner:
    """Reads registered hooks from ohm_hooks and executes them.

    The run_hook() method is a stub in this skeleton (returns exit_code=0).
    The subprocess execution engine is implemented in OHM-aznh.4.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get_hooks(self, event: str, *, enabled_only: bool = True) -> list[HookRecord]:
        """Return hooks registered for the given event.

        Args:
            event: One of pre_ingest, post_ingest, pre_query, post_query.
            enabled_only: If True, only return enabled hooks.

        Returns:
            List of HookRecord instances ordered by created_at.
        """
        if event not in VALID_HOOK_EVENTS:
            raise ValueError(f"Invalid hook event: {event!r}. Must be one of {sorted(VALID_HOOK_EVENTS)}")

        conditions = ["event = ?"]
        params: list[Any] = [event]

        if enabled_only:
            conditions.append("enabled = TRUE")

        where = " AND ".join(conditions)
        result = self._conn.execute(
            f"""SELECT id, event, command, timeout_ms, enabled, created_by, created_at, updated_at
               FROM ohm_hooks
               WHERE {where}
               ORDER BY created_at ASC""",
            params,
        )
        return _rows_to_hook_records(result)

    def run_hook(self, hook: HookRecord, payload: dict) -> HookResult:
        """Execute a single hook.

        Stub implementation: returns exit_code=0 without executing the command.
        The real subprocess runner is implemented in OHM-aznh.4.
        """
        logger.debug("Hook stub: %s (%s) — would run: %s", hook.id, hook.event, hook.command)
        return HookResult(hook_id=hook.id, exit_code=0)

    def run_hooks(self, event: str, payload: dict) -> list[HookResult]:
        """Execute all hooks for the given event.

        Returns results in execution order. For pre_ingest, callers
        should check if any result has exit_code != 0 and abort.
        """
        hooks = self.get_hooks(event)
        results = []
        for hook in hooks:
            result = self.run_hook(hook, payload)
            results.append(result)
            if event.startswith("pre_") and not result.success:
                logger.info("Hook %s (%s) rejected with exit_code=%d", hook.id, hook.event, result.exit_code)
                break
        return results
