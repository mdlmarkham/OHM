"""Metrics → Actions control loop for the OHM semantic layer.

Evaluates YAML-defined metric thresholds and turns them into concrete actions:
- `create_task`: creates a Beads task via the `bd create` CLI.
- `prompt_agent`: issues an OHM task or records an agent prompt.
"""

from __future__ import annotations

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

_THRESHOLD_RE = re.compile(
    r"^\s*(?P<op><=|>=|<|>|==|!=)\s*(?P<value>[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$"
)


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
    repo = Path(repo_path)
    if not (repo / ".beads").exists():
        raise FileNotFoundError(f"No Beads repository found at {repo_path}")

    cmd = ["bd", "create", title, "--priority", priority, "--description", description, "--json"]
    if labels:
        for label in labels:
            cmd.extend(["--label", label])

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
) -> list[dict[str, Any]]:
    """Execute a list of threshold-derived actions.

    Args:
        conn: DuckDB connection (used for `prompt_agent` / `create_task` fallback).
        repo_path: Path to the Beads repository for `create_task` actions.
        actions: List of action dicts from `evaluate_thresholds()`. If None,
            the caller should have already provided computed actions.

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
        result: dict[str, Any] = {"metric": metric, "action": action_type, "status": "skipped"}

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
                    result.update({"status": "created", "beads": bead})
                else:
                    task = _create_ohm_task(conn, title, description, priority, labels)
                    result.update({"status": "created", "ohm_task": task})
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
                result.update({"status": "created", "skill": skill, "target": target, "ohm_task": task})
            except Exception as exc:
                logger.warning("prompt_agent action failed for %s: %s", metric, exc)
                result.update({"status": "error", "message": str(exc)})

        results.append(result)
    return results
