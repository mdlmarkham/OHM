"""Discovery-and-export Graph mixin."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class DiscoveryExportGraphMixin(GraphMixinBase):
    """suggest_connections and import/export helpers."""

    def suggest_connections(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Suggest links between nodes that share tags or co-occur in neighborhoods.

        Discovery strategies:
        1. Shared tags: nodes with overlapping tag sets
        2. Co-occurrence: nodes that appear in the same 2-hop neighborhood
        3. Type affinity: nodes of types that frequently connect

        The substrate suggests; agents decide whether to connect.
        Same result regardless of which agent calls it — substrate method.

        Args:
            limit: Maximum suggestions.

        Returns:
            List of {from_node, from_label, to_node, to_label, reason, score}.
        """
        suggestions = []

        # Strategy 1: Shared tags
        shared_tag_pairs = self._conn.execute(
            """
            SELECT
                n1.id AS from_node, n1.label AS from_label,
                n2.id AS to_node, n2.label AS to_label,
                COUNT(*) AS shared_tags
            FROM ohm_nodes n1
            JOIN ohm_nodes n2 ON n1.id < n2.id
            WHERE n1.tags IS NOT NULL AND n2.tags IS NOT NULL
              AND n1.tags != '[]' AND n2.tags != '[]'
              AND NOT EXISTS (
                  SELECT 1 FROM ohm_edges e
                  WHERE (e.from_node = n1.id AND e.to_node = n2.id)
                     OR (e.from_node = n2.id AND e.to_node = n1.id)
              )
            GROUP BY n1.id, n1.label, n2.id, n2.label
            HAVING COUNT(*) >= 2
            ORDER BY shared_tags DESC
            LIMIT ?
        """,
            [limit],
        ).fetchall()

        for row in shared_tag_pairs:
            suggestions.append(
                {
                    "from_node": row[0],
                    "from_label": row[1],
                    "to_node": row[2],
                    "to_label": row[3],
                    "reason": f"shared_tags({row[4]})",
                    "score": row[4] / 5.0,  # Normalize
                }
            )

        # Strategy 2: Co-occurrence in neighborhoods
        cooccur = self._conn.execute(
            """
            SELECT
                e1.from_node AS from_node,
                n1.label AS from_label,
                e2.from_node AS to_node,
                n2.label AS to_label,
                COUNT(*) AS cooccurrence
            FROM ohm_edges e1
            JOIN ohm_edges e2 ON e1.to_node = e2.to_node AND e1.from_node < e2.from_node
            LEFT JOIN ohm_nodes n1 ON n1.id = e1.from_node
            LEFT JOIN ohm_nodes n2 ON n2.id = e2.from_node
            WHERE NOT EXISTS (
                SELECT 1 FROM ohm_edges e
                WHERE (e.from_node = e1.from_node AND e.to_node = e2.from_node)
                   OR (e.from_node = e2.from_node AND e.to_node = e1.from_node)
            )
            GROUP BY e1.from_node, n1.label, e2.from_node, n2.label
            HAVING COUNT(*) >= 2
            ORDER BY cooccurrence DESC
            LIMIT ?
        """,
            [limit],
        ).fetchall()

        for row in cooccur:
            from_node, from_label, to_node, to_label, count = row
            # Don't duplicate if already in suggestions
            if not any(s["from_node"] == from_node and s["to_node"] == to_node for s in suggestions):
                suggestions.append(
                    {
                        "from_node": from_node,
                        "from_label": from_label,
                        "to_node": to_node,
                        "to_label": to_label,
                        "reason": f"cooccurrence({count})",
                        "score": count / 5.0,
                    }
                )

        return sorted(suggestions, key=lambda s: -s["score"])[:limit]

    def export_graph(self) -> dict[str, Any]:
        """Export the entire graph as JSON-compatible dict.

        Used for backup, migration, and sharing.

        Returns:
            Dict with 'nodes', 'edges', 'observations', 'agent_state',
            'meta' (schema version, export timestamp, counts).
        """
        nodes = self._conn.execute("SELECT * FROM ohm_nodes ORDER BY created_at").fetchall()
        node_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_nodes LIMIT 0").description]
        nodes_json = []
        for row in nodes:
            d = dict(zip(node_cols, row))
            # Convert non-serializable types
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            nodes_json.append(d)

        edges = self._conn.execute("SELECT * FROM ohm_edges ORDER BY created_at").fetchall()
        edge_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_edges LIMIT 0").description]
        edges_json = []
        for row in edges:
            d = dict(zip(edge_cols, row))
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            edges_json.append(d)

        obs = self._conn.execute("SELECT * FROM ohm_observations ORDER BY created_at").fetchall()
        obs_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_observations LIMIT 0").description]
        obs_json = []
        for row in obs:
            d = dict(zip(obs_cols, row))
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            obs_json.append(d)

        agent_state = self._conn.execute("SELECT * FROM ohm_agent_state ORDER BY agent_name").fetchall()
        as_cols = [desc[0] for desc in self._conn.execute("SELECT * FROM ohm_agent_state LIMIT 0").description]
        as_json = []
        for row in agent_state:
            d = dict(zip(as_cols, row))
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif isinstance(v, (bytes, bytearray)):
                    d[k] = v.hex()
            as_json.append(d)

        return {
            "meta": {
                "format": "ohm-export-v1",
                "schema_version": (
                    sv_row[0]
                    if (
                        sv_row := self._conn.execute(
                            "SELECT value FROM ohm_meta WHERE key = 'schema_version'",
                        ).fetchone()
                    )
                    else "unknown"
                ),
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "node_count": len(nodes_json),
                "edge_count": len(edges_json),
                "observation_count": len(obs_json),
            },
            "nodes": nodes_json,
            "edges": edges_json,
            "observations": obs_json,
            "agent_state": as_json,
        }

    def import_graph(self, data: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
        """Import graph data from an export dict.

        Args:
            data: Export dict (from export_graph()).
            merge: If True, merge with existing data (skip duplicates).
                   If False, replace all data (WARNING: destructive).

        Returns:
            Dict with import statistics.
        """
        import_count = {"nodes": 0, "edges": 0, "observations": 0, "skipped": 0}

        # Column allowlists — only known schema columns are permitted as column
        # names in the generated INSERT statements (OHM-ftwx).
        _NODE_COLS = frozenset(
            {
                "label",
                "type",
                "content",
                "url",
                "created_by",
                "created_at",
                "updated_at",
                "updated_by",
                "confidence",
                "visibility",
                "provenance",
                "tags",
                "metadata",
                "priority",
                "task_status",
                "assigned_to",
                "due_date",
                "utility_scale",
                "current_best_action",
                "action_alternatives",
                "deleted_at",
                "embedding",
                "utility_usd_per_day",
                "utility_currency",
            }
        )
        _EDGE_COLS = frozenset(
            {
                "from_node",
                "to_node",
                "layer",
                "edge_type",
                "confidence",
                "probability",
                "probability_p05",
                "probability_p50",
                "probability_p95",
                "confidence_p05",
                "confidence_p50",
                "confidence_p95",
                "urgency",
                "condition",
                "provenance",
                "created_by",
                "created_at",
                "updated_at",
                "updated_by",
                "challenge_of",
                "challenge_type",
                "metadata",
                "deleted_at",
            }
        )
        _OBS_COLS = frozenset(
            {
                "node_id",
                "edge_id",
                "type",
                "value",
                "baseline",
                "sigma",
                "source",
                "created_by",
                "created_at",
                "metadata",
                "notes",
                "source_name",
                "source_url",
                "deleted_at",
                "sentiment",
            }
        )

        if not merge:
            # Destructive: clear all tables
            for table in ["ohm_observations", "ohm_edges", "ohm_nodes", "ohm_agent_state"]:
                self._conn.execute(f"DELETE FROM {table}")

        # OHM-od01.14: pre-group rows by their column set, then use
        # executemany for the INSERT. Existence check is a single IN-clause
        # SELECT instead of one query per row. For N nodes this turns
        # ~3N round-trips into 3 (one SELECT, one executemany per column-set
        # group, plus a fall-through for one-off column sets).

        # ── Nodes ──────────────────────────────────────────────────────
        node_rows = data.get("nodes", [])
        existing_node_ids: set[str] = set()
        if merge and node_rows:
            ids = [n["id"] for n in node_rows if "id" in n]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                existing = self._conn.execute(
                    f"SELECT id FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    ids,
                ).fetchall()
                existing_node_ids = {row[0] for row in existing}

        for node in node_rows:
            node_id = node.get("id")
            if not node_id:
                import_count["skipped"] += 1
                continue
            if merge and node_id in existing_node_ids:
                import_count["skipped"] += 1
                continue
            try:
                cols = [k for k in node.keys() if k != "id" and k in _NODE_COLS]
                vals = [node[k] for k in cols]
                col_str = ", ".join(["id"] + cols)
                val_str = ", ".join(["?"] * (1 + len(vals)))
                self._conn.execute(
                    f"INSERT INTO ohm_nodes ({col_str}) VALUES ({val_str})",
                    [node_id] + vals,
                )
                import_count["nodes"] += 1
            except Exception:
                import_count["skipped"] += 1

        # ── Edges ──────────────────────────────────────────────────────
        edge_rows = data.get("edges", [])
        existing_edge_ids: set[str] = set()
        if merge and edge_rows:
            ids = [e["id"] for e in edge_rows if "id" in e]
            if ids:
                placeholders = ",".join(["?"] * len(ids))
                existing = self._conn.execute(
                    f"SELECT id FROM ohm_edges WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                    ids,
                ).fetchall()
                existing_edge_ids = {row[0] for row in existing}

        for edge in edge_rows:
            edge_id = edge.get("id")
            if not edge_id:
                import_count["skipped"] += 1
                continue
            if merge and edge_id in existing_edge_ids:
                import_count["skipped"] += 1
                continue
            try:
                cols = [k for k in edge.keys() if k != "id" and k in _EDGE_COLS]
                vals = [edge[k] for k in cols]
                col_str = ", ".join(["id"] + cols)
                val_str = ", ".join(["?"] * (1 + len(vals)))
                self._conn.execute(
                    f"INSERT INTO ohm_edges ({col_str}) VALUES ({val_str})",
                    [edge_id] + vals,
                )
                import_count["edges"] += 1
            except Exception:
                import_count["skipped"] += 1

        # ── Observations ──────────────────────────────────────────────
        # Observations have heterogeneous shapes; group by column-set so
        # executemany can apply within each group.
        obs_rows = data.get("observations", [])
        obs_groups: dict[tuple, list] = {}
        for obs in obs_rows:
            try:
                cols = tuple(k for k in obs.keys() if k != "id" and k in _OBS_COLS)
                if cols not in obs_groups:
                    obs_groups[cols] = []
                obs_groups[cols].append(obs)
            except Exception:
                import_count["skipped"] += 1

        for cols, group in obs_groups.items():
            col_str = ", ".join(["id"] + list(cols))
            val_str = ", ".join(["?"] * (1 + len(cols)))
            try:
                params_list = []
                for obs in group:
                    params_list.append([obs["id"]] + [obs[k] for k in cols])
                self._conn.executemany(
                    f"INSERT INTO ohm_observations ({col_str}) VALUES ({val_str})",
                    params_list,
                )
                import_count["observations"] += len(group)
            except Exception:
                # On batch failure, fall back to per-row to salvage what we can
                for obs in group:
                    try:
                        self._conn.execute(
                            f"INSERT INTO ohm_observations ({col_str}) VALUES ({val_str})",
                            [obs["id"]] + [obs[k] for k in cols],
                        )
                        import_count["observations"] += 1
                    except Exception:
                        import_count["skipped"] += 1

        return import_count
