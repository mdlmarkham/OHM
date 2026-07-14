"""Change-feed Graph mixin."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class ChangeFeedGraphMixin(GraphMixinBase):
    """Listen to and query the change feed."""

    def listen(
        self,
        *,
        since: str | None = None,
        topics: list[str] | None = None,
        agents: list[str] | None = None,
        operations: list[str] | None = None,
        node_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Consume the change feed, optionally filtered by topic, agent, or operation.

        This is the primary mechanism for agents to stay aware of changes.
        Called at regular intervals (heartbeat cadence).

        Topic filtering: if topics are specified, only returns changes to nodes
        whose label matches one of the agent's INTERESTED_IN topics (fuzzy match).

        Args:
            since: ISO timestamp or None (uses last_sync from agent state).
            topics: Filter to changes affecting these topic labels.
            agents: Filter to changes by these agents.
            operations: Filter to these operations (INSERT, UPDATE, EVOLVE, CHALLENGE).
            node_type: Filter to changes affecting nodes of this type (e.g., 'concept').
            limit: Maximum changes to return.

        Returns:
            List of change feed entries relevant to this agent.
        """
        from ohm.queries import query_change_feed

        # Resolve 'since' from agent state if not provided
        if since is None:
            state = self._conn.execute(
                "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                [self.actor],
            ).fetchone()
            if state and state[0]:
                since = str(state[0])
            else:
                # Default to last hour
                since = None  # Will return recent changes

        # Get raw change feed
        changes = query_change_feed(
            self._conn,
            since=since,
            agent_name=agents[0] if agents and len(agents) == 1 else None,
            node_type=node_type,
            limit=limit * 2,  # Overfetch for filtering
        )

        # Filter by topics if specified
        if topics:
            topic_labels = set(t.lower() for t in topics)

            node_ids = [c.get("row_id", "") for c in changes if c.get("row_id", "")]
            node_label_map: dict[str, str] = {}
            edge_target_map: dict[str, str] = {}
            if node_ids:
                placeholders = ",".join(["?"] * len(node_ids))
                rows = self._conn.execute(
                    f"SELECT id, label FROM ohm_nodes WHERE id IN ({placeholders})",
                    node_ids,
                ).fetchall()
                node_label_map = {r[0]: r[1] for r in rows if r[1]}

                edge_rows = self._conn.execute(
                    f"SELECT e.id, n.label FROM ohm_edges e JOIN ohm_nodes n ON n.id = e.to_node WHERE e.id IN ({placeholders})",
                    node_ids,
                ).fetchall()
                edge_target_map = {r[0]: r[1] for r in edge_rows if r[1]}

            filtered = []
            seen = set()
            for change in changes:
                row_id = change.get("row_id", "")
                if row_id in seen:
                    continue
                label = node_label_map.get(row_id, "")
                if label and any(t in label.lower() for t in topic_labels):
                    filtered.append(change)
                    seen.add(row_id)
                    continue
                target_label = edge_target_map.get(row_id, "")
                if target_label and any(t in target_label.lower() for t in topic_labels):
                    filtered.append(change)
                    seen.add(row_id)
            changes = filtered

        # Filter by multiple agents if specified
        if agents and len(agents) > 1:
            agent_set = set(agents)
            changes = [c for c in changes if c.get("agent_name") in agent_set]

        # Filter by operations if specified
        if operations:
            op_set = set(operations)
            changes = [c for c in changes if c.get("operation") in op_set]

        # Don't include own changes by default (an agent doesn't need
        # to be notified about its own writes)
        changes = [c for c in changes if c.get("agent_name") != self.actor]

        # Update last_sync
        self._conn.execute(
            "UPDATE ohm_agent_state SET last_sync = now() WHERE agent_name = ?",
            [self.actor],
        )

        return changes[:limit]

    def pending_notifications(self) -> list[dict[str, Any]]:
        """Get pending notifications — changes since last listen() call.

        Shortcut for listen() with no filters. Returns changes from all
        agents since this agent last checked.

        Returns:
            List of change feed entries since last check.
        """
        return self.listen()

    def changes(
        self,
        *,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Personalized "what changed" delta for this agent (OHM-b7l7).

        Consolidates what an agent would otherwise poll /listen,
        /contradictions, /anomalies, /stale, /suggest, and /tasks for
        into a single call. Always returns the legacy core fields
        (``since``, ``agent``, ``query_timestamp``, ``node_total``,
        ``edge_total``, ``nodes``, ``edges``). Because the SDK is
        always called with an actor, the agent-scoped sections are
        also returned:

          * ``new_observations_on_my_nodes``
          * ``edges_touching_my_nodes``
          * ``challenges_to_my_edges``
          * ``tasks_assigned_or_status_changed``
          * ``stale_nodes_needing_refresh``

        Args:
            since: ISO 8601 timestamp. Optional — falls back to this
                agent's ``ohm_agent_state.last_sync``, then 24h ago
                (mirrors the ``listen()`` fallback).
            limit: Per-section row cap (default 100).

        Returns:
            Dict with the core fields plus the five agent-scoped
            sections.
        """
        from datetime import datetime, timedelta, timezone

        from ohm.queries import query_agent_changes

        agent = self.actor
        resolved_since = since
        if resolved_since is None:
            try:
                row = self._conn.execute(
                    "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
                    [agent],
                ).fetchone()
                if row and row[0]:
                    resolved_since = str(row[0])
            except Exception:
                pass
        if resolved_since is None:
            resolved_since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        result = query_agent_changes(
            self._conn,
            agent_name=agent,
            since=resolved_since,
            limit=limit,
        )

        # Bump last_sync so the next call only returns the delta since
        # this one (mirrors listen()'s side effect).
        try:
            self._conn.execute(
                "UPDATE ohm_agent_state SET last_sync = now() WHERE agent_name = ?",
                [agent],
            )
        except Exception:
            pass

        return result

    def urgent_changes(
        self,
        *,
        urgency_filter: list[str] | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get change feed entries filtered by urgency level.

        Returns changes associated with high-urgency edges (critical, high).
        Useful for agents that need to prioritize urgent updates.

        Args:
            urgency_filter: List of urgency levels to include
                (e.g., ['critical', 'high']). Default: ['critical', 'high'].
            since: ISO timestamp or None (uses last_sync from agent state).
            limit: Maximum changes to return.

        Returns:
            List of change feed entries matching the urgency filter.
        """
        from ohm.queries import query_change_feed

        if urgency_filter is None:
            urgency_filter = ["critical", "high"]

        # Get changes from the change feed
        changes = query_change_feed(
            self._conn,
            since=since,
            limit=limit * 2,  # Overfetch for filtering
        )

        # Filter to changes involving edges with matching urgency
        if not changes:
            return []

        # Get edge IDs from changes
        edge_ids = []
        for change in changes:
            row_id = change.get("row_id", "")
            table = change.get("table_name", "")
            if table == "ohm_edges" and row_id:
                edge_ids.append(row_id)

        # Query urgency for those edges
        urgent_edge_ids = set()
        urgency_map: dict[str, str] = {}
        if edge_ids:
            placeholders = ",".join(["?"] * len(edge_ids))
            rows = self._conn.execute(
                f"SELECT id, urgency FROM ohm_edges WHERE id IN ({placeholders}) AND urgency IN ({','.join(['?'] * len(urgency_filter))})",
                edge_ids + list(urgency_filter),
            ).fetchall()
            urgent_edge_ids = {row[0] for row in rows}
            urgency_map = {row[0]: row[1] for row in rows if row[1]}

        # Filter changes to those involving urgent edges
        result = []
        for change in changes:
            row_id = change.get("row_id", "")
            table = change.get("table_name", "")
            if table == "ohm_edges" and row_id in urgent_edge_ids:
                change["urgency"] = urgency_map.get(row_id, "unknown")
                result.append(change)

        return result[:limit]
