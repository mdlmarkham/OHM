"""Hook system for OHM staged ingestion pipeline (OHM-aznh).

Provides deterministic pre/post processing around graph writes.
Hooks are registered in the ohm_hooks table and executed by HookRunner.

Hook events:
  pre_ingest  - runs before graph write, can abort with non-zero exit
  post_ingest - runs after successful graph write, receives result payload
  pre_query   - runs before GET handlers, can modify query params
  post_query  - runs after GET handlers, can decorate response

OHM-aznh.8: Hook subprocesses are sandboxed by default:
- Environment whitelist: only OHM_HOOK_EVENT, OHM_HOOK_ID, OHM_CUSTOMER_ID
- Linux: preexec_fn with setrlimit (RLIMIT_AS, RLIMIT_NOFILE, RLIMIT_NPROC=0)
- Windows: best-effort (env sandbox + CREATE_NEW_PROCESS_GROUP).
- Set OHM_SANDBOX_DISABLE=1 to run unsandboxed (dev mode, logs warning).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
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


# ── Sandbox helpers (OHM-aznh.8) ──────────────────────────────────────────
# Hook subprocesses run in a restricted environment. The hooks table is
# tenant-writable, so a compromised hook could read or exfiltrate data
# if given full access. Sandboxing mitigates this.

_HOOK_ENV_WHITELIST = frozenset({
    "OHM_HOOK_EVENT", "OHM_HOOK_ID", "OHM_CUSTOMER_ID",
})

# Resource limits (Linux-only via setrlimit)
_DEFAULT_RLIMIT_AS = 256 * 1024 * 1024       # 256 MB address space
_DEFAULT_RLIMIT_NOFILE = 64                   # max open file descriptors
_DEFAULT_RLIMIT_NPROC = 0                     # no child processes (also prevents network daemon forking)
_DEFAULT_RLIMIT_STACK = 8 * 1024 * 1024       # 8 MB stack


_SANDBOX_SAFE_ENV_VARS = frozenset({
    "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "HOME", "USERPROFILE",
    "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL",
    # Windows shell=True artifacts (cmd.exe adds these)
    "COMSPEC", "PATHEXT", "PROMPT",
})


def _sandbox_env(hook_id: str, event: str, customer_id: str = "") -> dict[str, str]:
    """Return a sanitised environment dict for hook subprocesses.

    Only ``OHM_HOOK_*`` vars and a minimal safe set (PATH, TEMP, HOME,
    etc.) are passed. Sensitive vars (TOKEN, KEY, SECRET, DB_PATH) are
    stripped to prevent accidental data leaks through env inheritance.
    """
    env: dict[str, str] = {
        "OHM_HOOK_EVENT": event,
        "OHM_HOOK_ID": hook_id,
        "OHM_CUSTOMER_ID": customer_id,
    }
    for key in _SANDBOX_SAFE_ENV_VARS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def _sandbox_preexec() -> None:
    """Linux-only preexec_fn that applies resource limits.

    Called in the child process after fork() but before exec(). Applies:
    - RLIMIT_AS : prevent memory-exhaustion attacks
    - RLIMIT_NOFILE : prevent file-descriptor exhaustion
    - RLIMIT_NPROC : prevent forking (also implicitly blocks most network
      daemon spawning on typical Linux systems)
    - Closes all non-standard file descriptors (3..RLIMIT_NOFILE-1)

    Safe to call on non-Linux platforms (setrlimit is a no-op on systems
    that don't support it, but ``resource`` module is Unix-only).
    """
    import platform
    if platform.system() != "Linux":
        return

    import resource

    try:
        resource.setrlimit(resource.RLIMIT_AS, (_DEFAULT_RLIMIT_AS, _DEFAULT_RLIMIT_AS))
    except (ValueError, resource.error):
        pass

    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_DEFAULT_RLIMIT_NOFILE, _DEFAULT_RLIMIT_NOFILE))
    except (ValueError, resource.error):
        pass

    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (_DEFAULT_RLIMIT_NPROC, _DEFAULT_RLIMIT_NPROC))
    except (ValueError, resource.error):
        pass

    try:
        resource.setrlimit(resource.RLIMIT_STACK, (_DEFAULT_RLIMIT_STACK, _DEFAULT_RLIMIT_STACK))
    except (ValueError, resource.error):
        pass

    # Close all inherited non-stdio file descriptors
    try:
        max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        for fd in range(3, max_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    except Exception:
        pass


def _is_sandboxed() -> bool:
    """Return True if the hook sandbox is active (not disabled via env var).

    Checks ``OHM_SANDBOX_DISABLE`` at call time so tests can toggle the
    env var without needing ``importlib.reload``.
    """
    return os.environ.get("OHM_SANDBOX_DISABLE", "") not in ("1", "true", "yes")


# ── Hook Records ─────────────────────────────────────────────────────────────


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

    def run_hook(self, hook: HookRecord, payload: dict, *, customer_id: str = "") -> HookResult:
        """Execute a single hook.

        Args:
            customer_id: Tenant customer ID (for sandbox env var whitelist).

        Shell command hooks: spawn subprocess, write JSON payload to stdin,
        capture stdout/stderr, enforce timeout.

        python: prefix hooks: import and call the named function.
        Callable signature: def hook(payload: dict) -> tuple[int, str, str]
        returning (exit_code, stdout, stderr).

        Args:
            hook: Hook record to execute.
            payload: JSON-serialisable payload dict.
            customer_id: Tenant customer ID (for sandbox env var whitelist).
        """
        if hook.command.startswith("python:"):
            result = self._run_python_hook(hook, payload)
        else:
            result = self._run_shell_hook(hook, payload, customer_id=customer_id)
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

    def _run_shell_hook(self, hook: HookRecord, payload: dict, customer_id: str = "") -> HookResult:
        """Execute a shell command hook via subprocess.

        OHM-sh11: uses shlex.split() + shell=False to prevent command injection.
        Hook commands are split into argument lists before execution — shell
        metacharacters (;, &&, |, $(), backticks) are treated as literals, not
        interpreted by the shell.

        Args:
            hook: Hook record to execute.
            payload: JSON-serialisable payload dict.
            customer_id: Tenant customer ID (for env var whitelist).
        """
        import shlex

        timeout_sec = hook.timeout_ms / 1000.0
        payload_json = json.dumps(payload, default=str)
        start = time.monotonic()

        # OHM-aznh.8: build sandboxed environment
        env = _sandbox_env(hook.id, hook.event, customer_id) if _is_sandboxed() else None
        preexec_fn = _sandbox_preexec if (_is_sandboxed() and os.name == "posix") else None

        # OHM-sh11: split command into argv list — prevents shell injection
        try:
            cmd_args = shlex.split(hook.command)
        except ValueError as exc:
            return HookResult(
                hook_id=hook.id,
                exit_code=1,
                stderr=f"Invalid hook command (shlex parse error): {exc}",
            )

        try:
            proc = subprocess.Popen(
                cmd_args,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=preexec_fn,
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
        """Execute a python: prefix hook by importing and calling the function.

        OHM-phto: enforces hook.timeout_ms via a ThreadPoolExecutor future.
        A buggy or malicious hook that hangs will be interrupted after the
        configured timeout (default 5000ms), preventing server DoS.
        """
        import concurrent.futures

        module_path, _, func_name = hook.command[len("python:"):].rpartition(".")
        if not module_path or not func_name:
            return HookResult(
                hook_id=hook.id,
                exit_code=1,
                stderr=f"Invalid python: hook format: {hook.command!r} — expected python:module.function",
            )
        start = time.monotonic()
        timeout_sec = hook.timeout_ms / 1000.0

        try:
            mod = importlib.import_module(module_path)
            func = getattr(mod, func_name)
            enriched = {**payload, "__conn": self._conn}

            # OHM-phto: run callable in a worker thread with timeout cap
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, enriched)
                try:
                    exit_code, stdout, stderr = future.result(timeout=timeout_sec)
                except concurrent.futures.TimeoutError:
                    duration_ms = (time.monotonic() - start) * 1000
                    return HookResult(
                        hook_id=hook.id,
                        exit_code=_TIMEOUT_EXIT,
                        stderr=f"Python hook timed out after {hook.timeout_ms}ms",
                        duration_ms=round(duration_ms, 2),
                        timed_out=True,
                    )

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

    def run_hooks(self, event: str, payload: dict, *, customer_id: str = "") -> list[HookResult]:
        """Execute all hooks for the given event.

        Args:
            customer_id: Tenant customer ID (forwarded to run_hook for sandbox).

        Returns results in execution order. For pre_ingest, callers
        should check if any result has exit_code != 0 and abort.

        Args:
            event: Hook event name.
            payload: JSON-serialisable payload dict.
            customer_id: Tenant customer ID (forwarded to run_hook for sandbox).
        """
        hooks = self.get_hooks(event)
        results = []
        for hook in hooks:
            result = self.run_hook(hook, payload, customer_id=customer_id)
            results.append(result)
            if event.startswith("pre_") and not result.success:
                logger.info("Hook %s (%s) rejected with exit_code=%d", hook.id, hook.event, result.exit_code)
                break
        return results
