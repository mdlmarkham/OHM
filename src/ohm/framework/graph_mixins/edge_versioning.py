"""Edge-versioning Graph mixin."""
from __future__ import annotations
from typing import Any
from ohm.framework.graph_mixins._base import GraphMixinBase


class EdgeVersioningGraphMixin(GraphMixinBase):
    """edge_history — full history of an edge including supersessions/challenges."""

    def edge_history(self, edge_id: str) -> list[dict[str, Any]]:
        """Get the full history of an edge including supersessions and challenges.

        Edge versioning tracks the lifecycle of an edge:
        - Original creation
        - Confidence updates (by owner only)
        - Challenges (by other agents)
        - Supports (by other agents)
        - Identity evolution (L1 edges: superseded_by chain)

        Args:
            edge_id: The edge to get history for.

        Returns:
            List of events in chronological order, each with:
            type, agent, timestamp, details.
        """
        import json as _json
        from ohm.validation import validate_identifier

        edge_id = validate_identifier(edge_id, name="edge_id")

        edge = self.get_edge(edge_id)
        if edge is None:
            return []

        history = []

        # 1. Original creation
        history.append(
            {
                "type": "created",
                "agent": edge.get("created_by", "unknown"),
                "timestamp": str(edge.get("created_at", "")),
                "edge_type": edge.get("edge_type"),
                "confidence": edge.get("confidence"),
                "layer": edge.get("layer"),
            }
        )

        # 2. Confidence updates — check change feed
        updates = self._conn.execute(
            """SELECT agent_name, occurred_at, new_data
               FROM ohm_change_feed
               WHERE table_name = 'ohm_edges' AND row_id = ?
                 AND operation IN ('UPDATE', 'EVOLVE')
               ORDER BY occurred_at""",
            [edge_id],
        ).fetchall()

        for agent, ts, new_data in updates:
            history.append(
                {
                    "type": "updated",
                    "agent": agent,
                    "timestamp": str(ts),
                }
            )

        # 3. Challenges and supports
        reactions = self._conn.execute(
            """SELECT id, challenge_type, confidence, created_by, created_at, condition
               FROM ohm_edges
               WHERE challenge_of = ?
               ORDER BY created_at""",
            [edge_id],
        ).fetchall()

        for rid, rtype, rconf, ragent, rts, rreason in reactions:
            history.append(
                {
                    "type": rtype.lower() if rtype else "reaction",
                    "agent": ragent,
                    "timestamp": str(rts),
                    "confidence": rconf,
                    "reason": rreason,
                    "reaction_edge_id": rid,
                }
            )

        # 4. Identity evolution chain
        meta = edge.get("metadata")
        if meta:
            try:
                meta_dict = _json.loads(meta) if isinstance(meta, str) else meta
                if meta_dict.get("superseded"):
                    superseded_by = meta_dict.get("superseded_by")
                    history.append(
                        {
                            "type": "superseded",
                            "agent": edge.get("created_by", "unknown"),
                            "timestamp": str(edge.get("updated_at", "")),
                            "superseded_by": superseded_by,
                        }
                    )
                    # Follow the chain
                    if superseded_by:
                        next_edge = self.get_edge(superseded_by)
                        if next_edge:
                            history.append(
                                {
                                    "type": "evolved_to",
                                    "agent": next_edge.get("created_by", "unknown"),
                                    "timestamp": str(next_edge.get("created_at", "")),
                                    "edge_id": superseded_by,
                                    "provenance": next_edge.get("provenance"),
                                }
                            )
            except (ValueError, TypeError):
                pass

        # Sort by timestamp
        history.sort(key=lambda h: h.get("timestamp", ""))
        return history
