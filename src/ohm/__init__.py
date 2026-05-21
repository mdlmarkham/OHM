"""
OHM — Shared awareness, individual judgment.

Multi-agent knowledge graph with local DuckDB caches,
DuckLake shared backend, and change-feed-driven coordination.
"""

__version__ = "0.1.0"

from .graph_reader import (
    DuckDBGraphReader,
    EdgeRecord,
    GraphReader,
    MockGraphReader,
    NodeRecord,
    ObservationRecord,
)

__all__ = [
    "DuckDBGraphReader",
    "EdgeRecord",
    "GraphReader",
    "MockGraphReader",
    "NodeRecord",
    "ObservationRecord",
]
