"""Task handler mixin."""

from __future__ import annotations

from ohm.server import server as _server_module
from ohm.server.handlers._base import OhmHandlerBase
from ohm.server.handlers._ingest_helpers import IngestHelperMixin


class TaskHandlerMixin(IngestHelperMixin, OhmHandlerBase):
    """Handler mixin for task list/context/create/action endpoints."""

    def _get_tasks(self, path: str, qs: dict) -> None:
        """GET /tasks — list task nodes with filtering."""
        task_status = qs.get("status", [None])[0]
        assigned_to = qs.get("assigned_to", [None])[0]
        priority = qs.get("priority", [None])[0]
        created_by = qs.get("created_by", [None])[0]
        limit = int(qs.get("limit", [100])[0])
        offset = int(qs.get("offset", [0])[0])
        conditions = ["deleted_at IS NULL", "type = 'task'"]
        params = []
        if task_status:
            conditions.append("task_status = ?")
            params.append(task_status)
        if assigned_to:
            conditions.append("assigned_to = ?")
            params.append(assigned_to)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        params.append(limit)
        params.append(offset)
        sql = "SELECT * FROM ohm_nodes WHERE " + " AND ".join(conditions) + " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 WHEN 'P4' THEN 4 ELSE 5 END, due_date ASC NULLS LAST, created_at DESC LIMIT ? OFFSET ?"
        results = self.current_store.execute(sql, params)
        count_sql = "SELECT COUNT(*) as cnt FROM ohm_nodes WHERE " + " AND ".join(conditions)
        count_params = params[:-2]
        total_result = self.current_store.execute(count_sql, count_params)
        total = total_result[0].get("cnt", len(results)) if total_result else len(results)
        self._json_response(
            200,
            {
                "tasks": results,
                "total": total,
                "limit": limit,
                "offset": offset,
            },
        )

    def _get_task_context(self, path: str, qs: dict) -> None:
        """GET /task-context/{task_id} — task context binding (OHM-q9rt.4).

        Returns a task bundled with its 2-hop subgraph, rationale chain,
        expected outcome, and blocking tasks.
        """
        from ohm.queries import query_task_context

        prefix = "/task-context/"
        if not path.startswith(prefix):
            from ohm.exceptions import ValidationError

            raise ValidationError("Invalid task-context path")
        task_id = path[len(prefix) :]
        if not task_id:
            from ohm.exceptions import ValidationError

            raise ValidationError("Missing task id")

        result = query_task_context(
            self.current_store.read_conn,
            task_id,
        )
        self._json_response(200, result)

    def _post_task(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tasks — create a task node (OHM-7304)."""
        import re
        import uuid

        task_id = body.get("id") or ("task_" + re.sub(r"[^a-z0-9]+", "_", body["label"].lower()).strip("_")[:48] + "_" + str(uuid.uuid4())[:8])

        # OHM-tjzh: tasks are derived claims (action items derived from context).
        # They must link to existing structure. The synthesized body mirrors
        # what /node would see so the same enforcement path runs.
        synthesized_body = dict(body)
        synthesized_body["id"] = task_id
        synthesized_body.setdefault("type", "task")
        cross_link_error = self._enforce_cross_link_requirement(task_id, synthesized_body)
        if cross_link_error is not None:
            self._json_response(422, cross_link_error)
            return

        result = self.current_store.write_node(
            id=task_id,
            label=body["label"],
            type="task",
            content=body.get("content"),
            confidence=body.get("confidence", 1.0),
            visibility=body.get("visibility", "team"),
            provenance=body.get("provenance"),
            tags=body.get("tags"),
            metadata=body.get("metadata"),
            priority=body.get("priority"),
            url=body.get("url"),
            task_status=body.get("task_status", "open"),
            assigned_to=body.get("assigned_to"),
            due_date=body.get("due_date"),
            utility_usd_per_day=body.get("utility_usd_per_day"),
            utility_currency=body.get("utility_currency"),
            agent_name=agent,
        )
        _server_module._trigger_webhooks({"type": "task.created", "agent": agent, "node": result}, customer_id=self._customer_id)
        if result.get("created", True):
            self._json_response(201, result)
        else:
            self._json_response(200, result)

    def _post_task_action(self, path: str, qs: dict, body: dict, agent: str) -> None:
        """POST /tasks/<id>/... — task sub-actions (OHM-f5iq).

        Currently dispatches ``/tasks/<id>/outcome`` to close a task with a
        recorded outcome. Returns 404 for unknown sub-paths.
        """
        from ohm.exceptions import ValidationError
        from ohm.validation import validate_identifier

        sub = path[len("/tasks/") :]
        parts = sub.split("/", 1)
        if len(parts) != 2 or parts[1] != "outcome":
            self._json_response(404, {"error": "unknown_task_action", "path": path})
            return
        task_id = validate_identifier(parts[0], name="task_id")

        outcome = body.get("outcome")
        notes = body.get("notes")
        claim_node = body.get("claim_node")
        if outcome is None:
            raise ValidationError("outcome is required (TRUE, FALSE, or AMBIGUOUS)")

        from ohm.graph.queries import query_close_task_with_outcome

        result = query_close_task_with_outcome(
            self.current_store.conn,
            task_id=task_id,
            outcome=str(outcome),
            recorded_by=agent,
            notes=notes,
            claim_node=claim_node,
        )
        _server_module._trigger_webhooks(
            {"type": "task.completed", "agent": agent, "task": result["task"], "outcome": result["outcome"]},
            customer_id=self._customer_id,
        )
        self._json_response(200, result)
