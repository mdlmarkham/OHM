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

import importlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VALID_HOOK_EVENTS = frozenset({"pre_ingest", "post_ingest", "pre_query", "post_query"})

_SHELL_NOT_FOUND_EXIT = 127
_TIMEOUT_EXIT = 124


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

        Shell command hooks: spawn subprocess, write JSON payload to stdin,
        capture stdout/stderr, enforce timeout.

        python: prefix hooks: import and call the named function.
        Callable signature: def hook(payload: dict) -> tuple[int, str, str]
        returning (exit_code, stdout, stderr).
        """
        if hook.command.startswith("python:"):
            result = self._run_python_hook(hook, payload)
        else:
            result = self._run_shell_hook(hook, payload)
        self._log_invocation(hook, payload, result)
        return result

    def _log_invocation(self, hook: HookRecord, payload: dict, result: HookResult) -> None:
        """Insert a row into ohm_hook_log after each hook invocation."""
        import uuid as _uuid

        log_id = str(_uuid.uuid4())
        payload_json = json.dumps(payload, default=str)
        try:
            self._conn.execute(
                """INSERT INTO ohm_hook_log
                   (id, hook_id, event, payload, exit_code, stdout, stderr,
                    duration_ms, timed_out)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    log_id, hook.id, hook.event, payload_json,
                    result.exit_code, result.stdout, result.stderr,
                    result.duration_ms, result.timed_out,
                ],
            )
        except Exception:
            logger.debug("Failed to log hook invocation to ohm_hook_log", exc_info=True)

    def _run_shell_hook(self, hook: HookRecord, payload: dict) -> HookResult:
        """Execute a shell command hook via subprocess."""
        timeout_sec = hook.timeout_ms / 1000.0
        payload_json = json.dumps(payload, default=str)
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                hook.command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    input=payload_json.encode(),
                    timeout=timeout_sec,
                )
                duration_ms = (time.monotonic() - start) * 1000
                return HookResult(
                    hook_id=hook.id,
                    exit_code=proc.returncode or 0,
                    stdout=stdout_bytes.decode(errors="replace"),
                    stderr=stderr_bytes.decode(errors="replace"),
                    duration_ms=round(duration_ms, 2),
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                duration_ms = (time.monotonic() - start) * 1000
                return HookResult(
                    hook_id=hook.id,
                    exit_code=_TIMEOUT_EXIT,
                    stderr="Hook timed out",
                    duration_ms=round(duration_ms, 2),
                    timed_out=True,
                )
        except OSError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return HookResult(
                hook_id=hook.id,
                exit_code=_SHELL_NOT_FOUND_EXIT,
                stderr=str(exc),
                duration_ms=round(duration_ms, 2),
            )

    def _run_python_hook(self, hook: HookRecord, payload: dict) -> HookResult:
        """Execute a python: prefix hook by importing and calling the function."""
        module_path, _, func_name = hook.command[len("python:"):].rpartition(".")
        if not module_path or not func_name:
            return HookResult(
                hook_id=hook.id,
                exit_code=1,
                stderr=f"Invalid python: hook format: {hook.command!r} — expected python:module.function",
            )
        start = time.monotonic()
        try:
            mod = importlib.import_module(module_path)
            func = getattr(mod, func_name)
            exit_code, stdout, stderr = func(payload)
            duration_ms = (time.monotonic() - start) * 1000
            return HookResult(
                hook_id=hook.id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=round(duration_ms, 2),
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - start) * 1000
            return HookResult(
                hook_id=hook.id,
                exit_code=1,
                stderr=str(exc),
                duration_ms=round(duration_ms, 2),
            )

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
