"""
OHM Store — DuckDB connection management for local cache and shared backend.

Supports two modes:
1. Local mode: single DuckDB file for development or single-agent use
2. Quack mode: HTTP connection to ohmd daemon for multi-agent shared access
"""

import json
import os
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
    ):
        """Initialize the store.

        Args:
            db_path: Path to DuckDB file. Defaults to ~/.ohm/ohm.duckdb
            agent_name: Name of the owning agent (for attribution)
            readonly: Open in read-only mode
        """
        self.agent_name = agent_name
        self.readonly = readonly

        if db_path is None:
            db_path = os.environ.get("OHM_DB_PATH", str(Path.home() / ".ohm" / "ohm.duckdb"))

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.conn = duckdb.connect(str(self.db_path), read_only=readonly)
        self._init_schema()

    def _init_schema(self):
        """Initialize the schema if not already present."""
        if not self.readonly:
            self.conn.execute(SCHEMA_SQL)

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
        from datetime import datetime, timezone
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
    ) -> dict[str, Any]:
        """Create or update a node. Attributed to the current agent.

        Returns a dict with the node record and a 'created' key
        indicating whether this was a new creation (True) or an
        update of an existing node (False).
        """
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
                    updated_at = ?, updated_by = ?
                WHERE id = ?
                """,
                [label, type, content, confidence, visibility, provenance,
                 tags_json, metadata_json, now, self.agent_name, id],
            )
            self._log_change("ohm_nodes", id, "UPDATE", None)
            result = self.get_node(id) or {}
            result["created"] = False
            return result
        else:
            self.conn.execute(
                """
                INSERT INTO ohm_nodes (id, label, type, content, created_by, confidence,
                                       visibility, provenance, tags, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [id, label, type, content, self.agent_name, confidence,
                 visibility, provenance, tags_json, metadata_json, now, now],
            )
            self._log_change("ohm_nodes", id, "INSERT", None)
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
    ) -> Optional[dict[str, Any]]:
        """Create an edge. Attributed to the current agent.

        Enforces boundary rules:
        - L1/L2: any agent can write
        - L3/L4: creates with attribution, cannot overwrite
        - Challenge edges: create separate, don't modify
        """
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_edges (from_node, to_node, layer, edge_type, confidence,
                                    condition, provenance, created_by, challenge_of,
                                    challenge_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [from_node, to_node, layer, edge_type, confidence, condition,
             provenance, self.agent_name, challenge_of, challenge_type, now, now],
        )

        edge = self.execute_one(
            "SELECT * FROM ohm_edges WHERE from_node = ? AND to_node = ? "
            "AND edge_type = ? AND created_by = ? ORDER BY created_at DESC LIMIT 1",
            [from_node, to_node, edge_type, self.agent_name],
        )

        if edge:
            self._log_change("ohm_edges", edge["id"], "INSERT", layer)
        return edge

    def challenge_edge(
        self,
        edge_id: str,
        reason: str,
        confidence: float,
        challenge_type: str = "CHALLENGED_BY",
    ) -> Optional[dict[str, Any]]:
        """Challenge an existing edge. Creates a new edge referencing the original.

        Boundary rule: cannot modify the original edge — only create a challenge.
        Enforces that only L3/L4 edges can be challenged (via enforce_challenge_boundary).
        """
        from .boundary import enforce_challenge_boundary

        original = self.get_edge(edge_id)
        if not original:
            return None

        # Enforce boundary: only L3/L4 edges can be challenged
        enforce_challenge_boundary(self.conn, self.agent_name, edge_id)

        return self.write_edge(
            from_node=original["to_node"],
            to_node=original["from_node"],
            edge_type=challenge_type,
            layer=original["layer"],
            confidence=confidence,
            provenance=reason,
            challenge_of=edge_id,
            challenge_type=challenge_type,
        )

    def update_edge_confidence(
        self,
        edge_id: str,
        new_confidence: float,
        reason: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update confidence on an edge owned by the current agent.

        Boundary rule: only the owning agent can update their own edges.
        """
        edge = self.get_edge(edge_id)
        if not edge:
            return None

        # Enforce ownership
        if edge["created_by"] != self.agent_name:
            raise PermissionError(
                f"Cannot update edge {edge_id}: owned by {edge['created_by']}, not {self.agent_name}. "
                f"Use challenge_edge instead."
            )

        now = self._now()
        self.conn.execute(
            "UPDATE ohm_edges SET confidence = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            [new_confidence, now, self.agent_name, edge_id],
        )

        self._log_change("ohm_edges", edge_id, "UPDATE", edge["layer"])
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
    ) -> Optional[dict[str, Any]]:
        """Create an observation. Attributed to the current agent."""
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_observations
                (node_id, edge_id, type, value, baseline, sigma, source, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [node_id, edge_id, type, value, baseline, sigma, source, self.agent_name, now],
        )

        obs = self.execute_one(
            "SELECT * FROM ohm_observations WHERE node_id = ? AND created_by = ? ORDER BY created_at DESC LIMIT 1",
            [node_id, self.agent_name],
        )
        if obs:
            self._log_change("ohm_observations", obs["id"], "INSERT", None)
        return obs

    def update_agent_state(
        self,
        current_focus: Optional[str] = None,
        active_patterns: Optional[list[str]] = None,
        available_services: Optional[list[str]] = None,
        session_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Update the current agent's state in the hive mind awareness layer."""
        patterns_json = json.dumps(active_patterns or [])
        services_json = json.dumps(available_services or [])
        now = self._now()

        # Check if agent state exists
        existing = self.get_agent_state(self.agent_name)
        if existing:
            self.conn.execute(
                """
                UPDATE ohm_agent_state SET
                    current_focus = ?, active_patterns = ?, available_services = ?,
                    current_session_id = ?, last_sync = ?, updated_at = ?
                WHERE agent_name = ?
                """,
                [current_focus, patterns_json, services_json, session_id, now, now, self.agent_name],
            )
        else:
            self.conn.execute(
                """
                INSERT INTO ohm_agent_state (agent_name, current_focus, active_patterns,
                                               confidence_threshold, available_services,
                                               current_session_id, last_sync, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self.agent_name, current_focus, patterns_json, 0.7, services_json, session_id, now, now],
            )

        return self.get_agent_state(self.agent_name)

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
    ):
        """Log a change to the change log."""
        now = self._now()
        self.conn.execute(
            """
            INSERT INTO ohm_change_log (table_name, row_id, operation, agent_name, layer, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [table_name, row_id, operation, self.agent_name, layer, now],
        )

    def close(self):
        """Close the DuckDB connection."""
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
