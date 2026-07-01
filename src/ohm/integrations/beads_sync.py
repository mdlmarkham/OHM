from __future__ import annotations

"""Beads → OHM task sync (OHM-sdrr).

Beads is the canonical issue store; OHM task nodes are the runtime
representation. This module mirrors assigned Beads issues into OHM task
nodes so agents can discover and claim work through the OHM API.

Source-of-truth rules:
- Beads owns planning state (title, description, priority, assignee).
- OHM owns runtime state (task_status transitions, observations, outcomes).
- Status changes propagate Beads → OHM during sync.
- OHM → Beads propagation is deferred to a follow-up issue.

Field mapping:
    Beads id        → ohm_nodes.metadata.beads_id
    Beads title     → ohm_nodes.label
    Beads description → ohm_nodes.content
    Beads status    → ohm_nodes.task_status  (via BD_STATUS_MAP)
    Beads priority  → ohm_nodes.priority     (via BD_PRIORITY_MAP, 0-4 → P0-P3)
    Beads assignee  → ohm_nodes.assigned_to  (strip @olympus.local suffix)
    Beads labels    → ohm_nodes.tags
    Beads issue_type → ohm_nodes.metadata.beads_issue_type
"""

import json
import logging
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# ── Mapping tables ─────────────────────────────────────────────────────────

BD_STATUS_MAP: dict[str, str] = {
    "open": "open",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "review": "review",
    "closed": "done",
    "deferred": "cancelled",
}

BD_PRIORITY_MAP: dict[int, str] = {
    0: "P0",
    1: "P1",
    2: "P2",
    3: "P3",
    4: "P3",
}

BEADS_BACKLOG_ANCHOR_ID = "beads-backlog-anchor"


def _normalize_assignee(assignee: str | None) -> str | None:
    """Strip the @olympus.local suffix from a Beads assignee."""
    if not assignee:
        return None
    if "@" in assignee:
        return assignee.split("@")[0]
    return assignee


def _ensure_anchor_node(conn) -> None:
    """Create the beads-backlog anchor node if it doesn't exist.

    All synced task nodes link to this anchor to satisfy the cross-link
    requirement (ADR-018) without requiring a domain-specific anchor.
    """
    existing = conn.execute(
        "SELECT 1 FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
        [BEADS_BACKLOG_ANCHOR_ID],
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence, visibility) VALUES (?, ?, 'concept', 'System anchor for Beads-synced tasks. Auto-created by beads_sync.', 'system', 1.0, 'team')",
        [BEADS_BACKLOG_ANCHOR_ID, "Beads Backlog Anchor"],
    )


def _find_task_by_beads_id(conn, beads_id: str) -> str | None:
    """Return the OHM task node id for a given Beads issue id, or None."""
    rows = conn.execute("SELECT id FROM ohm_nodes WHERE type = 'task' AND deleted_at IS NULL").fetchall()
    for (node_id,) in rows:
        meta_raw = conn.execute("SELECT metadata FROM ohm_nodes WHERE id = ?", [node_id]).fetchone()
        if not meta_raw or not meta_raw[0]:
            continue
        try:
            meta = json.loads(meta_raw[0]) if isinstance(meta_raw[0], str) else meta_raw[0]
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(meta, dict) and meta.get("beads_id") == beads_id:
            return node_id
    return None


def sync_beads_to_ohm_tasks(
    conn,
    issues: list[dict[str, Any]],
    *,
    actor: str = "system",
) -> dict[str, Any]:
    """Sync Beads issues into OHM task nodes.

    For each issue with an assignee:
    1. Check if an OHM task with ``metadata.beads_id = issue.id`` exists.
    2. If not, create a task node linked to the beads-backlog anchor.
    3. If yes, update fields that Beads owns (label, content, priority,
       assigned_to, tags) — but **not** ``task_status`` if the OHM side
       has already advanced it past the Beads status.

    Args:
        conn: DuckDB connection (write).
        issues: List of Beads issue dicts (as returned by ``bd list --json``).
        actor: Agent name to attribute the writes to.

    Returns:
        Sync report dict::

            {
                "created": int,
                "updated": int,
                "skipped": int,
                "errors": list[str],
                "total": int,
            }
    """
    _ensure_anchor_node(conn)

    report = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "total": len(issues)}

    for issue in issues:
        beads_id = issue.get("id")
        if not beads_id:
            continue

        assignee = _normalize_assignee(issue.get("assignee"))
        if not assignee:
            # No assignee → not actionable yet; skip.
            report["skipped"] += 1
            continue

        bd_status = issue.get("status", "open")
        ohm_status = BD_STATUS_MAP.get(bd_status, "open")
        bd_priority = issue.get("priority", 2)
        ohm_priority = BD_PRIORITY_MAP.get(bd_priority, "P2")
        title = issue.get("title", beads_id)
        description = issue.get("description", "")
        labels = issue.get("labels", [])
        issue_type = issue.get("issue_type", "task")

        metadata = {
            "beads_id": beads_id,
            "beads_issue_type": issue_type,
            "beads_status": bd_status,
        }

        existing_task_id = _find_task_by_beads_id(conn, beads_id)

        if existing_task_id is None:
            # Create a new task node.
            task_id = f"beads_{beads_id.lower().replace('-', '_')}"
            try:
                conn.execute(
                    "INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence, visibility,  task_status, assigned_to, priority, tags, metadata) VALUES (?, ?, 'task', ?, ?, 1.0, 'team', ?, ?, ?, ?, ?)",
                    [
                        task_id,
                        title,
                        description,
                        actor,
                        ohm_status,
                        assignee,
                        ohm_priority,
                        json.dumps(labels),
                        json.dumps(metadata),
                    ],
                )
                # Cross-link to anchor (ADR-018).
                conn.execute(
                    "INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence, created_by) VALUES (?, ?, ?, 'L2', 'REFERENCES', 1.0, ?)",
                    [f"beads_link_{task_id}", BEADS_BACKLOG_ANCHOR_ID, task_id, actor],
                )
                report["created"] += 1
            except Exception as exc:
                report["errors"].append(f"{beads_id}: {exc}")
        else:
            # Update fields that Beads owns.
            # Don't regress task_status if OHM has advanced it.
            current = conn.execute(
                "SELECT task_status FROM ohm_nodes WHERE id = ?",
                [existing_task_id],
            ).fetchone()
            current_status = current[0] if current else None
            status_precedence = ["open", "in_progress", "blocked", "review", "done", "cancelled"]
            current_idx = status_precedence.index(current_status) if current_status in status_precedence else 0
            new_idx = status_precedence.index(ohm_status) if ohm_status in status_precedence else 0
            final_status = ohm_status if new_idx >= current_idx else current_status

            try:
                conn.execute(
                    "UPDATE ohm_nodes SET label = ?, content = ?, priority = ?, assigned_to = ?, tags = ?, metadata = ?, task_status = ?, updated_at = CURRENT_TIMESTAMP, updated_by = ? WHERE id = ?",
                    [
                        title,
                        description,
                        ohm_priority,
                        assignee,
                        json.dumps(labels),
                        json.dumps(metadata),
                        final_status,
                        actor,
                        existing_task_id,
                    ],
                )
                report["updated"] += 1
            except Exception as exc:
                report["errors"].append(f"{beads_id}: {exc}")

    return report


def fetch_beads_issues() -> list[dict[str, Any]]:
    """Fetch open/in-progress issues from the ``bd`` CLI.

    Falls back to reading ``.beads/issues.jsonl`` if ``bd`` is not
    available on PATH.
    """
    try:
        result = subprocess.run(
            ["bd", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            import json as _json

            return _json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        logger.warning("bd CLI unavailable, falling back to JSONL: %s", exc)

    # Fallback: read the JSONL export.
    issues: list[dict[str, Any]] = []
    try:
        with open(".beads/issues.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    issue = json.loads(line)
                    if issue.get("_type") == "issue":
                        issues.append(issue)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        logger.warning(".beads/issues.jsonl not found")
    return issues
