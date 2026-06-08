"""
OHM — Shared awareness, individual judgment.

Multi-agent knowledge graph with local DuckDB caches,
DuckLake shared backend, and change-feed-driven coordination.
"""

__version__ = "0.26.0"

from .framework.graph_reader import (
    DuckDBGraphReader,
    EdgeRecord,
    GraphReader,
    MockGraphReader,
    NodeRecord,
    ObservationRecord,
)
from .framework.ingest import IngestAdapter, IngestRecord, IngestResult, run_ingest
from .framework.semantic_roles import SemanticRoles

__all__ = [
    "DuckDBGraphReader",
    "EdgeRecord",
    "GraphReader",
    "IngestAdapter",
    "IngestRecord",
    "IngestResult",
    "MockGraphReader",
    "NodeRecord",
    "ObservationRecord",
    "SemanticRoles",
    "run_ingest",
]
