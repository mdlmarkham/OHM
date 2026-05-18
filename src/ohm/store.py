"""
OHM Store — DuckDB connection management for local cache and shared backend.

Supports three modes:
1. Local mode: single DuckDB file for development or single-agent use
2. Quack mode: DuckDB connection with Quack server for concurrent multi-writer access
3. Remote mode: HTTP connection to ohmd daemon for multi-agent shared access
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from .schema import SCHEMA_SQL


class OhmStore:
    """Manages the OHM knowledge graph in DuckDB."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        agent_name: str = "ohm",
        readonly: bool = False,
        quack: bool = False,
        quack_uri: str = "quack:localhost",
        quack_token_env: str = "QUACK_TOKEN",
    ):
        """Initialize the store.

        Args:
            db_path: Path to DuckDB file. Defaults to ~/.ohm/ohm.duckdb
            agent_name: Name of the owning agent (for attribution)
            readonly: Open in read-only mode
            quack: Enable Quack server for concurrent multi-writer access.
                When True and Quack is available, starts a Quack server
                on this connection. Falls back to direct DuckDB if unavailable.
            quack_uri: Quack server URI (default: quack:localhost)
            quack_token_env: Environment variable for Quack token
        """
        self.agent_name = agent_name
        self.readonly = readonly
        self.quack = quack
        self.quack_uri = quack_uri
        self.quack_token_env = quack_token_env
        self.quack_started = False

        if db_path is None:
            db_path = os.environ.get("OHM_DB_PATH", str(Path.home() / ".ohm" / "ohm.duckdb"))

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = self._connect_with_wal_recovery(str(self.db_path), readonly)
        self._init_schema()

        # Start Quack server if requested and available
        if self.quack and not self.readonly:
            self._start_quack()

    def _init_schema(self):
        """Initialize the schema if not already present, including migrations."""
        if not self.readonly:
            from .schema import initialize_schema
            initialize_schema(self.conn)

    @staticmethod
    def _connect_with_wal_recovery(db_path_str: str, readonly: bool = False):
        """Connect to DuckDB with WAL corruption recovery (OHM-b5a).

        If DuckDB fails to open due to WAL replay errors, deletes the
        WAL file and retries. The WAL contains only uncommitted writes,
        so this is safe — the main DB file is intact.
        """
        try:
            return duckdb.connect(db_path_str, read_only=readonly)
        except duckdb.IOException as e:
            error_msg = str(e)
            if "WAL" in error_msg or "wal" in error_msg.lower():
                wal_path = db_path_str + ".wal"
                if os.path.exists(wal_path):
                    os.remove(wal_path)
                return duckdb.connect(db_path_str, read_only=readonly)
            raise

    def _start_quack(self) -> None:
        """Start Quack server if available. Sets self.quack_started on success."""
        try:
            from .quack import is_available, start_server

            if is_available(self.conn):
                start_server(
                    self.conn,
                    uri=self.quack_uri,
                    token_env=self.quack_token_env,
                )
                self.quack_started = True
            else:
                import sys
                print(
                    "Quack extension not available — running in single-writer mode",
                    file=sys.stderr,
                )
        except Exception as e:
            import sys
            print(f"Quack server failed to start: {e}", file=sys.stderr)
            print("Falling back to single-writer mode", file=sys.stderr)
            self.quack_started = False

    def _stop_quack(self) -> None:
        """Stop Quack server if it was started."""
        if self.quack_started:
            try:
                from .quack import stop_server
                stop_server(self.conn, uri=self.quack_uri)
            except Exception:
                pass
            self.quack_started = False

    def execute(self, sql: str, params: Optional[list] = None) -> list[dict[str, Any]]:
        """Execute a SQL query and return results as list of dicts."""
        if params:
            result = self.conn.execute(sql, params)
        else:
            result = self.conn.execute(sql)

        columns = [desc[0] for desc in result.description]
        rows = result.fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def execute_one(self, sql: str, params: Optional[list] = None) -> Optional[dict[str, Any]]:
        """Execute a query and return a single result or None."""
        results = self.execute(sql, params)
        return results[0] if results else None

    def _now(self) -> str:
        """Return current timestamp as ISO string."""
        from datetime import datetime
        return datetime.now(timezone.utc).isoformat()

    def write_node(
        self,
        id: str,
        label: str,
        type: str,
        content: Optional[str] = None,
        confidence: float = 1.0,
        visibility: str = "team",
        provenance: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        priority: Optional[str] = None,
        url: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create or update a node. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the write to. Defaults to self.agent_name.
            priority: Node priority (P0-P3).
            url: External URL reference for this node.

        Returns a dict with the node record and a 'created' key
        indicating whether this was a new creation (True) or an
        update of an existing node (False).
        """
        actor = agent_name or self.agent_name
        metadata_json = json.dumps(metadata) if metadata else None
        tag_list = tags if tags else []
        tags_json = json.dumps(tag_list) if tag_list else None
        now = self._now()

        # Check if node exists
        existing = self.get_node(id)
        if existing:
            self.conn.execute(
                """
                UPDATE ohm_nodes SET
                    label = ?, type = ?, content = ?, confidence = ?,
                    visibility = ?, provenance = ?, tags = ?, metadata = ?,
                    priority = ?, url = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                [label, type, content, confidence, visibility, provenance,
                 tags_json, metadata_json, priority, url, now, actor, id],
            )
            self._log_change("ohm_nodes", id, "UPDATE", None, agent_name=actor)
            result = self.get_node(id) or {}
            result["created"] = False
            return result
        else:
            self.conn.execute(
                """
                INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence,
                                       visibility, provenance, tags, metadata, priority, url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [id, label, type, content, actor, confidence,
                 visibility, provenance, tags_json, metadata_json, priority, url, now, now],
            )
            self._log_change("ohm_nodes", id, "INSERT", None, agent_name=actor)
            result = self.get_node(id) or {}
            result["created"] = True
            return result

    def write_edge(
        self,
        from_node: str,
        to_node: str,
        edge_type: str,
        layer: str,
        confidence: Optional[float] = None,
        condition: Optional[str] = None,
        provenance: Optional[str] = None,
        challenge_of: Optional[str] = None,
        challenge_type: Optional[str] = None,
        urgency: Optional[str] = None,
        probability: Optional[float] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Create an edge. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the write to. Defaults to self.agent_name.
            urgency: Edge urgency (critical, high, medium, low).
            probability: Objective likelihood of the outcome (0.0-1.0).

        Enforces boundary rules:
        - L1/L2: any agent can write
        - L3/L4: creates with attribution, cannot overwrite
        - Challenge edges: create separate, don't modify
        """
        actor = agent_name or self.agent_name
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence,
                                    condition, provenance, created_by, challenge_of,
                                    challenge_type, urgency, probability, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [from_node, to_node, layer, edge_type, confidence, condition,
             provenance, actor, challenge_of, challenge_type, urgency, probability, now, now],
        )

        edge = self.execute_one(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND to_node = ? "
            "AND edge_type = ? AND created_by = ? ORDER BY created_at DESC LIMIT 1",
            [from_node, to_node, edge_type, actor],
        )

        if edge:
            self._log_change("ohm_edges", edge["id"], "INSERT", layer, agent_name=actor)
        return edge

    def challenge_edge(
        self,
        edge_id: str,
        reason: str,
        confidence: float,
        challenge_type: str = "CHALLENGED_BY",
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Challenge an existing edge. Creates a new edge referencing the original.

        Args:
            agent_name: Agent to attribute the challenge to. Defaults to self.agent_name.

        Boundary rule: cannot modify the original edge — only create a challenge.
        Enforces that only L3/L4 edges can be challenged (via enforce_challenge_boundary).
        """
        actor = agent_name or self.agent_name
        from .boundary import enforce_challenge_boundary

        original = self.get_edge(edge_id)
        if not original:
            return None

        # Enforce boundary: only L3/L4 edges can be challenged
        enforce_challenge_boundary(self.conn, actor, edge_id)

        return self.write_edge(
            from_node=original["to_node"],
            to_node=original["from_node"],
            edge_type=challenge_type,
            layer=original["layer"],
            confidence=confidence,
            provenance=reason,
            challenge_of=edge_id,
            challenge_type=challenge_type,
            agent_name=actor,
        )

    def update_edge_confidence(
        self,
        edge_id: str,
        new_confidence: float,
        reason: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update confidence on an edge owned by the given agent.

        Args:
            agent_name: Agent performing the update. Defaults to self.agent_name.

        Boundary rule: only the owning agent can update their own edges.
        """
        actor = agent_name or self.agent_name
        edge = self.get_edge(edge_id)
        if not edge:
            return None

        # Enforce ownership
        if edge["created_by"] != actor:
            raise PermissionError(
                f"Cannot update edge {edge_id}: owned by {edge['created_by']}, not {actor}. "
                f"Use challenge_edge instead."
            )

        now = self._now()
        self.conn.execute(
            "UPDATE ohm_edges SET confidence = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            [new_confidence, now, actor, edge_id],
        )

        self._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"], agent_name=actor)
        return self.get_edge(edge_id)

    def write_observation(
        self,
        node_id: str,
        type: str,
        value: Optional[float] = None,
        baseline: Optional[float] = None,
        sigma: Optional[float] = None,
        source: Optional[str] = None,
        edge_id: Optional[str] = None,
        notes: Optional[str] = None,
        source_name: Optional[str] = None,
        source_url: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Create an observation. Attributed to the given agent.

        Args:
            agent_name: Agent to attribute the observation to. Defaults to self.agent_name.
            notes: Optional free-text notes for the observation.
            source_name: Name of the source (e.g., 'Reuters').
            source_url: URL of the source (e.g., 'https://reuters.com/...').
        """
        actor = agent_name or self.agent_name
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_observations
                (node_id, edge_id, type, value, baseline, sigma, source, created_by, created_at, notes, source_name, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [node_id, edge_id, type, value, baseline, sigma, source, actor, now, notes, source_name, source_url],
        )

        obs = self.execute_one(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND created_by = ? ORDER BY created_at DESC LIMIT 1",
            [node_id, actor],
        )
        if obs:
            self._log_change("ohm_observations", obs["id"], "INSERT", None, agent_name=actor)
        return obs

    def update_agent_state(
        self,
        current_focus: Optional[str] = None,
        active_patterns: Optional[list[str]] = None,
        available_services: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update the given agent's state in the hive mind awareness layer.

        Args:
            agent_name: Agent whose state to update. Defaults to self.agent_name.
        """
        actor = agent_name or self.agent_name
        patterns_json = json.dumps(active_patterns or [])
        services_json = json.dumps(available_services or [])
        now = self._now()

        # Check if agent state exists
        existing = self.get_agent_state(actor)
        if existing:
            self.conn.execute(
                """
                UPDATE ohm_agent_state SET
                    current_focus = ?, active_patterns = ?, available_services = ?,
                    current_session_id = ?, last_sync = ?, updated_at = ?
                WHERE agent_name = ?
                """,
                [current_focus, patterns_json, services_json, session_id, now, now, actor],
            )
        else:
            self.conn.execute(
                """
                INSERT INTO ohm_agent_state (agent_name, current_focus, active_patterns,
                                               confidence_threshold, available_services,
                                               current_session_id, last_sync, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [actor, current_focus, patterns_json, 0.7, services_json, session_id, now, now],
            )

        return self.get_agent_state(actor)

    def get_node(self, node_id: str) -> Optional[dict[str, Any]]:
        """Get a node by ID."""
        return self.execute_one("SELECT * FROM ohm_nodes WHERE id = ?", [node_id])

    def get_edge(self, edge_id: str) -> Optional[dict[str, Any]]:
        """Get an edge by ID."""
        return self.execute_one("SELECT * FROM ohm_edges WHERE id = ?", [edge_id])

    def get_agent_state(self, agent_name: str) -> Optional[dict[str, Any]]:
        """Get an agent's current state."""
        return self.execute_one("SELECT * FROM ohm_agent_state WHERE agent_name = ?", [agent_name])

    def who_is_working_on(self, topic: str) -> list[dict[str, Any]]:
        """Find agents working on a topic."""
        return self.execute(
            """SELECT * FROM ohm_agent_state
               WHERE current_focus ILIKE ?
               OR active_patterns::VARCHAR ILIKE ?""",
            [f"%{topic}%", f"%{topic}%"],
        )

    def status(self) -> dict[str, Any]:
        """Get graph status: node count, edge count, agent count, last sync."""
        nc = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_nodes")
        ec = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_edges")
        oc = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_observations")
        ac = self.execute_one("SELECT COUNT(*) AS cnt FROM ohm_agent_state")
        node_count = nc["cnt"] if nc else 0
        edge_count = ec["cnt"] if ec else 0
        obs_count = oc["cnt"] if oc else 0
        agent_count = ac["cnt"] if ac else 0

        edges_by_layer = self.execute(
            "SELECT layer, COUNT(*) AS cnt FROM ohm_edges GROUP BY layer ORDER BY layer"
        )

        return {
            "node_count": node_count,
            "edge_count": edge_count,
            "observation_count": obs_count,
            "agent_count": agent_count,
            "edges_by_layer": {row["layer"]: row["cnt"] for row in edges_by_layer},
            "db_path": str(self.db_path),
        }

    def _log_change(
        self,
        table_name: str,
        row_id: str,
        operation: str,
        layer: Optional[str],
        agent_name: Optional[str] = None,
    ):
        """Log a change to both the change log and the change feed.

        ohm_change_log is the internal audit trail (used by push_to_ducklake).
        ohm_change_feed is the agent-facing change feed (used by listen() and SSE /events).
        Both must be populated for agents to see each other's writes.

        Args:
            agent_name: Agent to attribute the change to. Defaults to self.agent_name.
        """
        actor = agent_name or self.agent_name
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_change_log (table_name, row_id, operation, agent_name, layer, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [table_name, row_id, operation, actor, layer, now],
        )
        # Also populate the agent-facing change feed
        self.conn.execute(
            """
            INSERT INTO ohm_change_feed (table_name, row_id, operation, agent_name, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [table_name, row_id, operation, actor, now],
        )
        # Update agent's last_sync so they appear in active_agents
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM ohm_agent_state WHERE agent_name = ?", [actor],
        ).fetchone()
        if existing and existing[0] > 0:
            self.conn.execute(
                "UPDATE ohm_agent_state SET last_sync = ?, updated_at = ? WHERE agent_name = ?",
                [now, now, actor],
            )
        else:
            self.conn.execute(
                """INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at)
                   VALUES (?, ?, ?)""",
                [actor, now, now],
            )

    def close(self):
        """Close the DuckDB connection. Stops Quack server if running."""
        self._stop_quack()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ── DuckLake Sync ────────────────────────────────────────────────

    def sync_heartbeat(
        self,
        ducklake_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Push local changes to DuckLake and pull remote changes.

        This is the main sync method called on each agent heartbeat.
        It:
        1. Pushes local changes to DuckLake (if path configured)
        2. Pulls changes from DuckLake (if path configured)
        3. Updates last_sync timestamp

        Args:
            ducklake_path: Optional path to DuckLake database.
                If None, uses OHM_DUCKLAKE_PATH env var.
                If neither set, sync is a no-op (local-only mode).

        Returns:
            Dict with sync results: pushed_count, pulled_count, last_sync.
        """
        if ducklake_path is None:
            ducklake_path = os.environ.get("OHM_DUCKLAKE_PATH")

        pushed = 0
        pulled = 0
        last_sync = None

        if ducklake_path:
            # Push local changes to DuckLake
            pushed = self.push_to_ducklake(ducklake_path)
            # Pull remote changes from DuckLake
            pulled = self.pull_from_ducklake(ducklake_path)

        # Update last_sync timestamp — ensure agent row exists
        existing = self.conn.execute(
            "SELECT 1 FROM ohm_agent_state WHERE agent_name = ?",
            [self.agent_name],
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE ohm_agent_state SET last_sync = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE agent_name = ?",
                [self.agent_name],
            )
        else:
            self.conn.execute(
                "INSERT INTO ohm_agent_state (agent_name, last_sync, updated_at) "
                "VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                [self.agent_name],
            )
        row = self.conn.execute(
            "SELECT last_sync FROM ohm_agent_state WHERE agent_name = ?",
            [self.agent_name],
        ).fetchone()
        if row:
            last_sync = row[0]

        return {
            "pushed": pushed,
            "pulled": pulled,
            "last_sync": last_sync,
            "agent": self.agent_name,
        }

    def push_to_ducklake(self, ducklake_path: str) -> int:
        """Push local changes to DuckLake shared backend.

        Reads changes from local ohm_change_log since last_push,
        replicates them to DuckLake's ohm_change_feed, and records
        the push timestamp.

        Args:
            ducklake_path: Path to DuckLake database.

        Returns:
            Number of changes pushed.
        """
        from datetime import datetime

        # Get last push timestamp for this agent
        last_push = self._get_last_push_timestamp()
        last_push_str = last_push if last_push else "1970-01-01T00:00:00Z"

        # Read local changes since last push
        changes = self.execute(
            """
            SELECT table_name, row_id, operation, layer, change_data, changed_at
            FROM ohm_change_log
            WHERE agent_name = ? AND changed_at > ?::TIMESTAMP
            ORDER BY changed_at ASC
            """,
            [self.agent_name, last_push_str],
        )

        if not changes:
            return 0

        # Connect to DuckLake and insert changes
        ducklake = duckdb.connect(ducklake_path, read_only=False)

        try:
            for change in changes:
                table_name = change["table_name"]
                row_id = change["row_id"]
                operation = change["operation"]
                change["layer"]
                change_data = change["change_data"]
                changed_at = change["changed_at"]

                # Insert into DuckLake's change feed
                ducklake.execute(
                    """
                    INSERT INTO ohm_change_feed
                    (table_name, row_id, operation, agent_name, old_data, new_data, occurred_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [table_name, row_id, operation, self.agent_name,
                     None, change_data, changed_at],
                )

            # Record push timestamp
            now = datetime.now(timezone.utc)
            self._set_last_push_timestamp(now)

            return len(changes)
        finally:
            ducklake.close()

    def pull_from_ducklake(self, ducklake_path: str) -> int:
        """Pull remote changes from DuckLake shared backend.

        Reads changes from DuckLake's ohm_change_feed that occurred
        after the last pull timestamp, and applies them to the local
        database (if they don't conflict).

        Args:
            ducklake_path: Path to DuckLake database.

        Returns:
            Number of changes pulled and applied.
        """
        # Get last pull timestamp for this agent
        last_pull = self._get_last_pull_timestamp()
        last_pull_str = last_pull if last_pull else "1970-01-01T00:00:00Z"

        # Connect to DuckLake and read changes
        ducklake = duckdb.connect(ducklake_path, read_only=True)

        try:
            # Read remote changes since last pull (excluding our own)
            changes = ducklake.execute(
                """
                SELECT table_name, row_id, operation, agent_name, new_data, occurred_at
                FROM ohm_change_feed
                WHERE agent_name != ? AND occurred_at > ?::TIMESTAMP
                ORDER BY occurred_at ASC
                """,
                [self.agent_name, last_pull_str],
            ).fetchall()

            if not changes:
                return 0

            applied = 0
            for change in changes:
                table_name, row_id, operation, remote_agent, new_data, occurred_at = change
                self._apply_remote_change(
                    table_name, row_id, operation, remote_agent, new_data, occurred_at
                )
                applied += 1

            # Record pull timestamp
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            self._set_last_pull_timestamp(now)

            return applied
        finally:
            ducklake.close()

    def _apply_remote_change(
        self,
        table_name: str,
        row_id: str,
        operation: str,
        remote_agent: str,
        new_data: Any,
        occurred_at: str | None = None,
    ) -> None:
        """Apply a remote change from DuckLake to local database.

        Only applies INSERT and UPDATE. Skips DELETE (not implemented yet).
        Uses attribution-based conflict resolution: remote agent's data
        wins if the record was created by a different agent.
        """
        import json

        if new_data:
            data = json.loads(new_data) if isinstance(new_data, str) else new_data
        else:
            data = {}

        if table_name == "ohm_nodes":
            if operation == "INSERT":
                # Check if already exists
                existing = self.get_node(row_id)
                if not existing:
                    self.conn.execute(
                        """
                        INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence,
                                               visibility, provenance, tags, metadata, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [row_id, data.get("label", ""), data.get("type", "concept"),
                         data.get("content"), remote_agent, data.get("confidence", 1.0),
                         data.get("visibility", "team"), data.get("provenance"),
                         json.dumps(data.get("tags", [])) if data.get("tags") else None,
                         json.dumps(data.get("metadata", {})) if data.get("metadata") else None,
                         occurred_at or self._now(),
                         occurred_at or self._now()],
                    )
            elif operation == "UPDATE":
                self.conn.execute(
                    """
                    UPDATE ohm_nodes SET
                        label = COALESCE(?, label),
                        type = COALESCE(?, type),
                        content = COALESCE(?, content),
                        confidence = COALESCE(?, confidence),
                        visibility = COALESCE(?, visibility),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    [data.get("label"), data.get("type"), data.get("content"),
                     data.get("confidence"), data.get("visibility"), row_id],
                )

        elif table_name == "ohm_edges":
            if operation == "INSERT":
                # Check if already exists
                existing = self.get_edge(row_id)
                if not existing:
                    self.conn.execute(
                        """
                        INSERT INTO ohm_edges (id, from_node, to_node, layer, edge_type, confidence,
                                               condition, provenance, created_by, challenge_of,
                                               challenge_type, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [row_id, data.get("from_node"), data.get("to_node"),
                         data.get("layer", "L3"), data.get("edge_type"),
                         data.get("confidence"), data.get("condition"),
                         data.get("provenance"), remote_agent,
                         data.get("challenge_of"), data.get("challenge_type"),
                         occurred_at or self._now(),
                         occurred_at or self._now()],
                    )

    def _get_last_push_timestamp(self) -> Optional[str]:
        """Get the last push timestamp for this agent."""
        row = self.conn.execute(
            """
            SELECT value FROM ohm_meta
            WHERE key = ? || '_last_push'
            """,
            [self.agent_name],
        ).fetchone()
        return row[0] if row else None

    def _set_last_push_timestamp(self, timestamp: datetime) -> None:
        """Set the last push timestamp for this agent."""
        ts_str = timestamp.isoformat()
        self.conn.execute(
            """
            INSERT INTO ohm_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            [f"{self.agent_name}_last_push", ts_str],
        )

    def _get_last_pull_timestamp(self) -> Optional[str]:
        """Get the last pull timestamp for this agent."""
        row = self.conn.execute(
            """
            SELECT value FROM ohm_meta
            WHERE key = ? || '_last_pull'
            """,
            [self.agent_name],
        ).fetchone()
        return row[0] if row else None

    def _set_last_pull_timestamp(self, timestamp: datetime) -> None:
        """Set the last pull timestamp for this agent."""
        ts_str = timestamp.isoformat()
        self.conn.execute(
            """
            INSERT INTO ohm_meta (key, value)
            VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            [f"{self.agent_name}_last_pull", ts_str],
        )
