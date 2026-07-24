"""Metrics → Actions control loop for the OHM semantic layer.

Evaluates YAML-defined metric thresholds and turns them into concrete actions:
- `create_task`: creates a Beads task via the `bd create` CLI.
- `prompt_agent`: issues an OHM task or records an agent prompt.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OPERATORS = {
    "<": lambda a, b: a is not None and b is not None and a < b,
    "<=": lambda a, b: a is not None and b is not None and a <= b,
    ">": lambda a, b: a is not None and b is not None and a > b,
    ">=": lambda a, b: a is not None and b is not None and a >= b,
    "==": lambda a, b: a is not None and b is not None and a == b,
    "!=": lambda a, b: a is not None and b is not None and a != b,
}

_THRESHOLD_RE = re.compile(r"^\s*(?P<op><=|>=|<|>|==|!=)\s*(?P<value>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$")

# Default rate-limit window for semantic-layer auto actions: same
# (metric, threshold, action_type) cannot create duplicate tasks within
# this many seconds.
DEFAULT_RATE_LIMIT_SECONDS = 24 * 60 * 60  # 24 hours


def _action_key(metric: str, threshold: str, action_type: str | None) -> str:
    """Return a stable opaque key for a metric action."""
    action_type = action_type or "unknown"
    raw = f"{metric}|{threshold.strip()}|{action_type}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _action_table_exists(conn: Any) -> bool:
    """Check whether the ohm_metric_action_log table exists."""
    try:
        result = conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'ohm_metric_action_log'").fetchone()
        return bool(result and result[0])
    except Exception:
        return False


def _is_rate_limited(
    conn: Any,
    metric: str,
    threshold: str,
    action_type: str | None,
    window_seconds: float,
) -> bool:
    """Return True if this action already fired within the rate-limit window."""
    if window_seconds <= 0 or not _action_table_exists(conn):
        return False
    try:
        # INTERVAL does not accept a bound parameter in DuckDB, so the
        # number of seconds is interpolated safely as an integer.
        result = conn.execute(
            f"""
            SELECT COUNT(*) FROM ohm_metric_action_log
            WHERE metric = ?
              AND threshold = ?
              AND action_type = ?
              AND created_at >= CURRENT_TIMESTAMP - INTERVAL '{int(window_seconds)} seconds'
            """,
            [metric, threshold.strip(), action_type or "unknown"],
        ).fetchone()
        return bool(result and result[0] > 0)
    except Exception as exc:
        logger.warning("Rate-limit check failed for %s/%s: %s", metric, threshold, exc)
        return False


def _record_action(
    conn: Any,
    metric: str,
    threshold: str,
    action_type: str | None,
    created_task_id: str | None = None,
) -> dict[str, Any] | None:
    """Record that a metric action fired so future runs can deduplicate."""
    if not _action_table_exists(conn):
        return None
    try:
        conn.execute(
            """
            INSERT INTO ohm_metric_action_log
                (metric, threshold, action_type, created_task_id, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [metric, threshold.strip(), action_type or "unknown", created_task_id],
        )
    except Exception as exc:
        logger.warning("Failed to record metric action %s/%s: %s", metric, threshold, exc)
        return None
    return {"metric": metric, "threshold": threshold, "action_type": action_type}


def _parse_threshold(condition: str) -> tuple[str, float]:
    """Parse a threshold condition string like '< 0.3' into (operator, value)."""
    match = _THRESHOLD_RE.match(condition)
    if not match:
        raise ValueError(f"Invalid threshold condition: {condition!r}")
    op = match.group("op")
    value = float(match.group("value"))
    return op, value


def evaluate_thresholds(
    metric_values: dict[str, float | None],
    metric_definitions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Evaluate metric definitions' thresholds against current values.

    Args:
        metric_values: Mapping of metric name to computed scalar value (or None).
        metric_definitions: Metric definitions as returned by `load_metrics()`.

    Returns:
        List of action dicts, each including metric, triggered threshold,
        and action parameters. Empty if no thresholds fire.
    """
    actions: list[dict[str, Any]] = []
    for metric_name, value in metric_values.items():
        definition = metric_definitions.get(metric_name, {})
        thresholds = definition.get("thresholds") or []
        if not isinstance(thresholds, list):
            continue
        for threshold in thresholds:
            condition = threshold.get("when")
            if not condition:
                continue
            try:
                op, limit = _parse_threshold(condition)
            except ValueError:
                logger.warning("Skipping malformed threshold for %s: %s", metric_name, condition)
                continue
            if op not in _OPERATORS:
                continue
            if _OPERATORS[op](value, limit):
                action = {
                    "metric": metric_name,
                    "value": value,
                    "threshold": condition,
                    "action": threshold.get("action"),
                }
                # Copy all action-specific fields except 'when'.
                for key in threshold:
                    if key != "when" and key != "action":
                        action[key] = threshold[key]
                actions.append(action)
    return actions


def create_beads_task(
    repo_path: str,
    title: str,
    description: str,
    priority: str,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Beads task by invoking `bd create`.

    Args:
        repo_path: Path to a Beads repository (contains .beads directory).
        title: Task title.
        description: Task description body.
        priority: Priority string such as P0, P1, P2, P3, or P4.
        labels: Optional list of labels to attach.

    Returns:
        Dict with `issue_id`, `title`, `priority`, and `labels`.
    """
    import warnings

    warnings.warn(
        "create_beads_task shells out to the deprecated `bd` CLI; beads is "
        "deprecated for new work (see AGENTS.md — use GitHub Issues instead).",
        DeprecationWarning,
        stacklevel=2,
    )

    repo = Path(repo_path)
    if not (repo / ".beads").exists():
        raise FileNotFoundError(f"No Beads repository found at {repo_path}")

    cmd = ["bd", "create", title, "--priority", priority, "--description", description, "--json"]
    if labels:
        for label in labels:
            cmd.extend(["--label", label])

    # On Windows, bd may be installed as a .CMD wrapper (e.g., via npm).
    # subprocess.run with a list doesn't search PATHEXT, so resolve the
    # full path via shutil.which to avoid FileNotFoundError.
    import shutil as _shutil

    bd_path = _shutil.which("bd")
    if bd_path:
        cmd[0] = bd_path

    result = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bd create failed: {result.stderr.strip()}")

    stdout = result.stdout.strip()
    issue_id = stdout.splitlines()[-1].strip() if stdout else None
    return {
        "issue_id": issue_id,
        "title": title,
        "priority": priority,
        "labels": labels or [],
    }


def _create_ohm_task(
    conn: Any,
    title: str,
    description: str,
    priority: str,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Create an OHM task node in the graph (fallback when Beads is unavailable)."""
    from ohm.queries import create_node

    task = create_node(
        conn,
        label=title,
        node_type="task",
        content=description,
        priority=priority,
        tags=labels or [],
        created_by="ohm_metrics",
    )
    return task


def run_actions(
    conn: Any,
    repo_path: str = "/root/olympus/OHM",
    actions: list[dict[str, Any]] | None = None,
    rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
) -> list[dict[str, Any]]:
    """Execute a list of threshold-derived actions.

    Args:
        conn: DuckDB connection (used for `prompt_agent` / `create_task` fallback
            and for recording action executions in `ohm_metric_action_log`).
        repo_path: Path to the Beads repository for `create_task` actions.
        actions: List of action dicts from `evaluate_thresholds()`. If None,
            the caller should have already provided computed actions.
        rate_limit_window_seconds: Minimum seconds between creating the same
            (metric, threshold, action_type) task. Default 24 hours. Set to 0
            to disable deduplication.

    Returns:
        List of result dicts, one per executed action, including `action`,
        `metric`, and outcome fields.
    """
    if actions is None:
        actions = []

    results: list[dict[str, Any]] = []
    repo = Path(repo_path)
    beads_available = (repo / ".beads").exists()

    for action in actions:
        action_type = action.get("action")
        metric = action.get("metric")
        threshold = action.get("threshold", "")
        result: dict[str, Any] = {"metric": metric, "action": action_type, "status": "skipped"}

        # OHM-wx42: deduplicate within the configured window. Same
        # (metric, threshold, action_type) cannot create duplicate tasks.
        if rate_limit_window_seconds > 0 and _is_rate_limited(conn, metric, threshold, action_type, rate_limit_window_seconds):
            result["status"] = "skipped"
            result["reason"] = "rate_limited"
            results.append(result)
            continue

        created_task_id: str | None = None

        if action_type == "create_task":
            title = action.get("title", f"Metric action for {metric}")
            description = action.get("description", "")
            priority = action.get("priority", "P2")
            labels = action.get("labels")
            if isinstance(labels, str):
                labels = [labels]
            try:
                if beads_available:
                    bead = create_beads_task(
                        repo_path=repo_path,
                        title=title,
                        description=description,
                        priority=priority,
                        labels=labels,
                    )
                    created_task_id = bead.get("issue_id")
                    result.update({"status": "created", "beads": bead})
                else:
                    task = _create_ohm_task(conn, title, description, priority, labels)
                    created_task_id = task.get("id")
                    result.update({"status": "created", "ohm_task": task})
                _record_action(conn, metric, threshold, action_type, created_task_id)
            except Exception as exc:
                logger.warning("create_task action failed for %s: %s", metric, exc)
                result.update({"status": "error", "message": str(exc)})

        elif action_type == "prompt_agent":
            skill = action.get("skill", "critique")
            target = action.get("target", "high_confidence_l3_edges")
            description = action.get("description", "")
            title = action.get("title") or f"Prompt {skill} agent for {metric}"
            try:
                # Record as an OHM task so it appears in the work queue.
                task = _create_ohm_task(
                    conn,
                    title=title,
                    description=description,
                    priority=action.get("priority", "P1"),
                    labels=action.get("labels", ["metrics", "prompt_agent", skill, target]),
                )
                created_task_id = task.get("id")
                result.update({"status": "created", "skill": skill, "target": target, "ohm_task": task})
                _record_action(conn, metric, threshold, action_type, created_task_id)
            except Exception as exc:
                logger.warning("prompt_agent action failed for %s: %s", metric, exc)
                result.update({"status": "error", "message": str(exc)})

        results.append(result)
    return results
