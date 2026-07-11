"""IngestAdapter — protocol for domain data bridge pattern.

Every domain app has the same ingestion problem: external data source → OHM
graph. IngestAdapter gives each domain a standard interface to implement.
run_ingest() provides a generic runner with provenance tagging, per-record
error recovery, and dry-run support.

Reference: OHM-7297

Example::

    class CsvNodeAdapter:
        def source_id(self) -> str:
            return "csv-import-v1"

        def read_batch(self) -> Iterable[IngestRecord]:
            for row in csv.DictReader(open("data.csv")):
                yield IngestRecord(
                    kind="node",
                    label=row["name"],
                    node_type=row.get("type", "concept"),
                    content=row.get("description"),
                    tags=row.get("tags", "").split(",") if row.get("tags") else None,
                )

    result = run_ingest(CsvNodeAdapter(), client)
    print(result)  # {"created": 42, "updated": 3, "skipped": 1, "errors": 0}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Record type ───────────────────────────────────────────────────────────────


@dataclass
class IngestRecord:
    """One item (node or edge) to upsert into the OHM graph.

    Set kind="node" for node records; kind="edge" for edge records.
    Fields not relevant to the kind are ignored by run_ingest().

    Node fields: label, node_type, content, tags, metadata, id (optional)
    Edge fields: from_node, to_node, edge_type, layer, probability, confidence
    Shared: provenance (overrides adapter.source_id() for this record only)
    """

    kind: str  # "node" or "edge"

    # Node fields
    label: str | None = None
    node_type: str = "concept"
    content: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    id: str | None = None  # if provided, used as stable external ID for idempotency

    # Edge fields
    from_node: str | None = None
    to_node: str | None = None
    edge_type: str | None = None
    layer: str = "L3"
    probability: float | None = None
    confidence: float | None = None
    probability_p05: float | None = None
    probability_p50: float | None = None
    probability_p95: float | None = None

    # Shared
    provenance: str | None = None  # overrides adapter.source_id() for this record


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class IngestAdapter(Protocol):
    """Domain data source implementing the OHM ingest bridge pattern.

    Implement this Protocol in your domain app to integrate an external data
    source with the OHM graph. Pass an instance to run_ingest().

    Example implementations:
        TimescaleDBAdapter — TOPO time-series historian data
        CsvAdapter — bulk import from spreadsheets
        RestApiAdapter — poll a REST endpoint for new records
    """

    def source_id(self) -> str:
        """Stable identifier for this adapter's data source.

        Used as provenance tag on all ingested records. Should be stable across
        runs (e.g. "topo-historian-v1", not a timestamp). Used for deduplication
        and audit trails.
        """
        ...

    def read_batch(self) -> Iterable[IngestRecord]:
        """Yield IngestRecords to upsert into OHM.

        May be called multiple times; each call should yield the current batch.
        Adapters that support incremental sync should track their own cursor
        and yield only new/changed records on each call.
        """
        ...


# ── Runner ────────────────────────────────────────────────────────────────────


@dataclass
class IngestResult:
    """Summary of a run_ingest() execution."""

    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
            "error_details": self.error_details,
        }


def run_ingest(
    adapter: IngestAdapter,
    client: Any,
    *,
    dry_run: bool = False,
    stop_on_error: bool = False,
) -> IngestResult:
    """Run an IngestAdapter against an OHM client, yielding an IngestResult.

    Handles per-record error recovery: a failed record is logged and counted
    as an error; processing continues unless stop_on_error=True.

    Args:
        adapter: Implements IngestAdapter — provides source_id() and read_batch().
        client: OHMClient (or any object with create_node/create_edge methods).
        dry_run: If True, iterate and validate records without writing to OHM.
        stop_on_error: If True, raise on first per-record error. Default is to
            log and continue.

    Returns:
        IngestResult with counts of created, updated, skipped, and errors.
    """
    source = adapter.source_id()
    result = IngestResult()

    for record in adapter.read_batch():
        if record.kind not in ("node", "edge"):
            logger.warning("IngestAdapter %s: unknown kind %r — skipping", source, record.kind)
            result.skipped += 1
            continue

        if dry_run:
            result.created += 1
            continue

        provenance = record.provenance or source

        try:
            if record.kind == "node":
                _ingest_node(record, client, provenance, result)
            else:
                _ingest_edge(record, client, provenance, result)
        except Exception as exc:
            detail = {
                "kind": record.kind,
                "label": record.label,
                "from_node": record.from_node,
                "to_node": record.to_node,
                "error": str(exc),
            }
            logger.error("IngestAdapter %s: record error — %s", source, exc)
            result.errors += 1
            result.error_details.append(detail)
            if stop_on_error:
                raise

    return result


def _ingest_node(
    record: IngestRecord,
    client: Any,
    provenance: str,
    result: IngestResult,
) -> None:
    if not record.label:
        logger.warning("IngestAdapter: node record missing label — skipping")
        result.skipped += 1
        return

    kwargs: dict[str, Any] = {"provenance": provenance}
    if record.content is not None:
        kwargs["content"] = record.content
    if record.tags is not None:
        kwargs["tags"] = record.tags
    if record.metadata is not None:
        kwargs["metadata"] = record.metadata
    if record.id is not None:
        kwargs["id"] = record.id

    response = client.create_node(record.label, node_type=record.node_type, **kwargs)
    if response.get("created", True):
        result.created += 1
    else:
        result.updated += 1


def _ingest_edge(
    record: IngestRecord,
    client: Any,
    provenance: str,
    result: IngestResult,
) -> None:
    if not record.from_node or not record.to_node or not record.edge_type:
        logger.warning("IngestAdapter: edge record missing from_node/to_node/edge_type — skipping")
        result.skipped += 1
        return

    kwargs: dict[str, Any] = {"provenance": provenance}
    if record.probability is not None:
        kwargs["probability"] = record.probability
    if record.confidence is not None:
        kwargs["confidence"] = record.confidence
    if record.probability_p05 is not None:
        kwargs["probability_p05"] = record.probability_p05
    if record.probability_p50 is not None:
        kwargs["probability_p50"] = record.probability_p50
    if record.probability_p95 is not None:
        kwargs["probability_p95"] = record.probability_p95

    response = client.create_edge(
        from_node=record.from_node,
        to_node=record.to_node,
        edge_type=record.edge_type,
        layer=record.layer,
        **kwargs,
    )
    if response.get("created", True):
        result.created += 1
    else:
        result.updated += 1


# OHM-803: Plugin-based ingest adapter registry

_ADAPTER_REGISTRY: dict[str, type] = {}


def register_adapter(source: str, adapter_class: type) -> None:
    """Register an ingest adapter for a source name (OHM-803)."""
    _ADAPTER_REGISTRY[source] = adapter_class


def get_adapter(source: str) -> type | None:
    """Look up a registered adapter by source name."""
    return _ADAPTER_REGISTRY.get(source)


def list_adapters() -> list[str]:
    """List all registered adapter source names."""
    return sorted(_ADAPTER_REGISTRY.keys())


class TagBatchAdapter:
    """Reference adapter: batch-import signal tags from a JSON file (OHM-803).

    JSON format: [{"node_id": "...", "label": "...", "source_type": "opc_ua",
    "source_id": "ns=2;s=TAG", "domain": "topo", "metadata": {...}}]
    """

    def __init__(self, file_path: str, domain: str = "ohm", **config: Any) -> None:
        self._file_path = file_path
        self._domain = domain
        self._config = config

    def source_id(self) -> str:
        return f"tag-batch-{self._domain}"

    def read_batch(self) -> Iterable[IngestRecord]:
        import json

        with open(self._file_path) as f:
            tags = json.load(f)
        if not isinstance(tags, list):
            raise ValueError(f"Tag batch file must contain a JSON array, got {type(tags)}")
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            node_id = tag.get("node_id") or tag.get("id")
            if not node_id:
                continue
            yield IngestRecord(
                kind="node",
                id=node_id,
                label=tag.get("label", node_id),
                node_type=tag.get("node_type", "concept"),
                provenance=self.source_id(),
            )


register_adapter("tags", TagBatchAdapter)
