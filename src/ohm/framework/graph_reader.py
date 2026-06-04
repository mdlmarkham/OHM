"""GraphReader protocol — decouples inference from DuckDB.

OHM-p2u8 / OHM-n95j: inference functions (bayesian.py, markov.py) should accept a
GraphReader rather than a raw DuckDB connection. This enables:

  - Library use: TOPO and other domain apps call compute_voi() with their own reader
  - Unit testing: MockGraphReader injects fixture data without a real database
  - Backend swapping: any storage that implements GraphReader works transparently

Protocol surface (deliberately minimal — only what inference actually queries):

    reader.get_edges(edge_types=["CAUSES"], layers=["L3"])
    reader.get_nodes(ids=["node-1", "node-2"])
    reader.get_nodes(node_type="decision")
    reader.get_observations(node_id="node-1")
    reader.get_meta("graph_generation")
    reader.get_graph_generation()  # convenience: int(get_meta("graph_generation"))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Record types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EdgeRecord:
    """Immutable snapshot of an ohm_edges row.

    All optional numeric fields are None when not stored (no PERT data, no
    probability, etc.). Callers should use ``probability or confidence`` or
    ``probability_p05 is not None`` checks rather than assuming a value is set.
    """

    from_node: str
    to_node: str
    edge_type: str
    layer: str | None = None
    probability: float | None = None
    confidence: float | None = None
    probability_p05: float | None = None
    probability_p50: float | None = None
    probability_p95: float | None = None
    confidence_p05: float | None = None
    confidence_p50: float | None = None
    confidence_p95: float | None = None


@dataclass(frozen=True)
class NodeRecord:
    """Immutable snapshot of an ohm_nodes row.

    Only the fields consumed by inference are included. Server/store code
    continues to work with full DuckDB rows; this record is the inference-layer
    view.
    """

    id: str
    label: str
    type: str
    confidence: float | None = None
    utility_scale: float | None = None
    utility_usd_per_day: float | None = None
    utility_currency: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    priority: str | None = None


@dataclass(frozen=True)
class ObservationRecord:
    """Immutable snapshot of an ohm_observations row."""

    id: str
    node_id: str | None
    edge_id: str | None
    type: str
    value: float | None = None
    source: str | None = None
    created_by: str | None = None
    scale: str = "unknown"
    created_at: str | None = None


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class GraphReader(Protocol):
    """Read-only view of the OHM graph consumed by inference functions.

    Implementors:

      DuckDBGraphReader   — wraps a live DuckDB connection (production)
      MockGraphReader     — in-memory fixture data (tests)

    All ``get_*`` methods return empty lists (not None) when no data matches,
    so callers never need to guard against None iteration.
    """

    def get_edges(
        self,
        *,
        edge_types: list[str],
        layers: list[str] | None = None,
    ) -> list[EdgeRecord]:
        """Return non-deleted edges matching the given types (and optionally layers).

        Args:
            edge_types: Must be non-empty; edges not in this list are excluded.
            layers: Optional layer filter (e.g. ["L3", "L4"]). None = all layers.
        """
        ...

    def get_nodes(
        self,
        ids: list[str] | None = None,
        *,
        node_type: str | None = None,
    ) -> list[NodeRecord]:
        """Return non-deleted nodes, optionally filtered by id list and/or type.

        Args:
            ids: If provided, only return nodes whose id is in this list.
            node_type: If provided, only return nodes with this type.
        """
        ...

    def get_node(self, node_id: str) -> NodeRecord | None:
        """Get a single node by ID, or None if not found."""
        ...

    def get_observations(self, node_id: str) -> list[ObservationRecord]:
        """Return non-deleted observations attached to the given node."""
        ...

    def get_observation_counts(self, node_ids: list[str]) -> dict[str, int]:
        """Return observation count per node for the given node IDs.

        More efficient than calling get_observations() per node when
        you only need the count (e.g. auto-select in discovery).
        """
        ...

    def get_meta(self, key: str) -> str | None:
        """Return the string value stored in ohm_meta for key, or None."""
        ...

    def get_graph_generation(self) -> int:
        """Return the current graph generation counter (0 when not tracked)."""
        raw = self.get_meta("graph_generation")
        return int(raw) if raw is not None else 0


# ── Coercion helpers ──────────────────────────────────────────────────────────


def coerce_reader(conn_or_reader: Any) -> "DuckDBGraphReader | GraphReader":
    """Accept either a raw DuckDB conn or any GraphReader; always return a GraphReader.

    Use this at the top of inference functions to support both legacy callers
    (pass a raw conn) and new callers (pass a DuckDBGraphReader or MockGraphReader).
    """
    if isinstance(conn_or_reader, GraphReader):
        return conn_or_reader
    return DuckDBGraphReader(conn_or_reader)


def raw_conn(conn_or_reader: Any) -> Any:
    """Extract the underlying DuckDB conn from a DuckDBGraphReader, or return as-is.

    Use for legacy code paths that require a raw DuckDB connection (e.g.
    functions not yet migrated to GraphReader).
    """
    if isinstance(conn_or_reader, DuckDBGraphReader):
        return conn_or_reader._conn
    return conn_or_reader


# ── DuckDB implementation ─────────────────────────────────────────────────────


class DuckDBGraphReader:
    """GraphReader backed by a live DuckDB connection.

    This is a thin translation layer — no logic, just SQL → records. Pass an
    instance wherever inference functions accept a ``GraphReader``. Existing
    code that passes a raw ``conn`` continues to work via the DuckDB-side SQL;
    this class is the migration path for callers that want the protocol.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def get_edges(
        self,
        *,
        edge_types: list[str],
        layers: list[str] | None = None,
    ) -> list[EdgeRecord]:
        if not edge_types:
            return []
        placeholders = ",".join(["?"] * len(edge_types))
        if layers:
            layer_ph = ",".join(["?"] * len(layers))
            sql = (
                f"SELECT from_node, to_node, edge_type, layer, "
                f"probability, confidence, "
                f"probability_p05, probability_p50, probability_p95, "
                f"confidence_p05, confidence_p50, confidence_p95 "
                f"FROM ohm_edges "
                f"WHERE edge_type IN ({placeholders}) "
                f"AND layer IN ({layer_ph}) "
                f"AND deleted_at IS NULL "
                f"ORDER BY from_node, to_node"
            )
            rows = self._conn.execute(sql, edge_types + layers).fetchall()
        else:
            sql = (
                f"SELECT from_node, to_node, edge_type, layer, "
                f"probability, confidence, "
                f"probability_p05, probability_p50, probability_p95, "
                f"confidence_p05, confidence_p50, confidence_p95 "
                f"FROM ohm_edges "
                f"WHERE edge_type IN ({placeholders}) "
                f"AND deleted_at IS NULL "
                f"ORDER BY from_node, to_node"
            )
            rows = self._conn.execute(sql, edge_types).fetchall()

        return [
            EdgeRecord(
                from_node=r[0],
                to_node=r[1],
                edge_type=r[2],
                layer=r[3],
                probability=float(r[4]) if r[4] is not None else None,
                confidence=float(r[5]) if r[5] is not None else None,
                probability_p05=float(r[6]) if r[6] is not None else None,
                probability_p50=float(r[7]) if r[7] is not None else None,
                probability_p95=float(r[8]) if r[8] is not None else None,
                confidence_p05=float(r[9]) if r[9] is not None else None,
                confidence_p50=float(r[10]) if r[10] is not None else None,
                confidence_p95=float(r[11]) if r[11] is not None else None,
            )
            for r in rows
        ]

    def get_all_nodes(self) -> list[NodeRecord]:
        """Get all non-deleted nodes."""
        return self.get_nodes()

    def get_node(self, node_id: str) -> NodeRecord | None:
        """Get a single node by ID, or None if not found."""
        nodes = self.get_nodes(ids=[node_id])
        return nodes[0] if nodes else None

    def get_nodes(
        self,
        ids: list[str] | None = None,
        *,
        node_type: str | None = None,
    ) -> list[NodeRecord]:
        import json as _json

        conditions = ["deleted_at IS NULL"]
        params: list[Any] = []

        if ids is not None:
            if not ids:
                return []
            placeholders = ",".join(["?"] * len(ids))
            conditions.append(f"id IN ({placeholders})")
            params.extend(ids)

        if node_type is not None:
            conditions.append("type = ?")
            params.append(node_type)

        where = " AND ".join(conditions)
        sql = f"SELECT id, label, type, confidence, utility_scale, utility_usd_per_day, utility_currency, content, tags, metadata, priority FROM ohm_nodes WHERE {where}"
        rows = self._conn.execute(sql, params).fetchall()

        records = []
        for r in rows:
            tags = None
            if r[8] is not None:
                try:
                    tags = _json.loads(r[8]) if isinstance(r[8], str) else r[8]
                except Exception:
                    tags = None

            metadata = None
            if r[9] is not None:
                try:
                    metadata = _json.loads(r[9]) if isinstance(r[9], str) else r[9]
                except Exception:
                    metadata = None

            records.append(
                NodeRecord(
                    id=r[0],
                    label=r[1],
                    type=r[2],
                    confidence=float(r[3]) if r[3] is not None else None,
                    utility_scale=float(r[4]) if r[4] is not None else None,
                    utility_usd_per_day=float(r[5]) if r[5] is not None else None,
                    utility_currency=r[6],
                    content=r[7],
                    tags=tags,
                    metadata=metadata,
                    priority=r[10],
                )
            )
        return records

    def get_observations(self, node_id: str) -> list[ObservationRecord]:
        rows = self._conn.execute(
            "SELECT id, node_id, edge_id, type, value, source, created_by, scale, created_at FROM ohm_observations WHERE node_id = ? AND deleted_at IS NULL",
            [node_id],
        ).fetchall()
        return [
            ObservationRecord(
                id=r[0],
                node_id=r[1],
                edge_id=r[2],
                type=r[3],
                value=float(r[4]) if r[4] is not None else None,
                source=r[5],
                created_by=r[6],
                scale=r[7] if len(r) > 7 and r[7] is not None else "unknown",
                created_at=str(r[8]) if len(r) > 8 and r[8] is not None else None,
            )
            for r in rows
        ]

    def get_observation_counts(self, node_ids: list[str]) -> dict[str, int]:
        if not node_ids:
            return {}
        placeholders = ",".join(["?"] * len(node_ids))
        rows = self._conn.execute(
            f"SELECT node_id, COUNT(*) FROM ohm_observations WHERE node_id IN ({placeholders}) AND deleted_at IS NULL GROUP BY node_id",
            node_ids,
        ).fetchall()
        counts = {r[0]: int(r[1]) for r in rows}
        counts.update({nid: 0 for nid in node_ids if nid not in counts})
        return counts

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM ohm_meta WHERE key = ?", [key]).fetchone()
        return str(row[0]) if row is not None else None

    def get_graph_generation(self) -> int:
        raw = self.get_meta("graph_generation")
        return int(raw) if raw is not None else 0


# ── MockGraphReader (for tests) ───────────────────────────────────────────────


@dataclass
class MockGraphReader:
    """In-memory GraphReader for unit tests — no database required.

    Populate edges/nodes/observations/meta before passing to inference functions.

    Example::

        reader = MockGraphReader(
            edges=[EdgeRecord("A", "B", "CAUSES", probability=0.8)],
            nodes=[NodeRecord("A", "Root", "concept"), NodeRecord("B", "Leaf", "concept")],
        )
        result = build_bayesian_network(reader)
    """

    edges: list[EdgeRecord] = field(default_factory=list)
    nodes: list[NodeRecord] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)

    def get_edges(
        self,
        *,
        edge_types: list[str],
        layers: list[str] | None = None,
    ) -> list[EdgeRecord]:
        result = [e for e in self.edges if e.edge_type in edge_types]
        if layers is not None:
            result = [e for e in result if e.layer in layers]
        return result

    def get_node(self, node_id: str) -> NodeRecord | None:
        """Get a single node by ID, or None if not found."""
        nodes = self.get_nodes(ids=[node_id])
        return nodes[0] if nodes else None

    def get_nodes(
        self,
        ids: list[str] | None = None,
        *,
        node_type: str | None = None,
    ) -> list[NodeRecord]:
        result = list(self.nodes)
        if ids is not None:
            id_set = set(ids)
            result = [n for n in result if n.id in id_set]
        if node_type is not None:
            result = [n for n in result if n.type == node_type]
        return result

    def get_observations(self, node_id: str) -> list[ObservationRecord]:
        return [o for o in self.observations if o.node_id == node_id]

    def get_all_nodes(self) -> list[NodeRecord]:
        """Get all nodes."""
        return self.get_nodes()

    def get_observation_counts(self, node_ids: list[str]) -> dict[str, int]:
        from collections import Counter
        counts = Counter(o.node_id for o in self.observations if o.node_id in set(node_ids))
        return {nid: counts.get(nid, 0) for nid in node_ids}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def get_graph_generation(self) -> int:
        raw = self.meta.get("graph_generation")
        return int(raw) if raw is not None else 0
