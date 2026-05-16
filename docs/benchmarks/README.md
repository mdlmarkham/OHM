# OHM Performance Benchmarks

Benchmark results from `test_benchmarks.py` using pytest-benchmark.

**Environment:** Python 3.13.1, DuckDB 1.5.2, Windows, in-memory database
**Date:** 2026-05-16

## Neighborhood Query

| Scale | Mean (ms) | StdDev (ms) | Median (ms) | OPS |
|-------|-----------|-------------|-------------|-----|
| Small (10 nodes) | 8.1 | 1.6 | 7.8 | 123 |
| Medium (100 nodes) | 7.8 | 0.8 | 7.7 | 128 |
| Large (1000 nodes) | 9.5 | 0.7 | 9.4 | 105 |

Neighborhood queries scale sub-linearly — the recursive CTE traversal is efficient
even at 1000 nodes, with only ~18% increase from small to large.

## Path Finding

| Scale | Mean (ms) | StdDev (ms) | Median (ms) | OPS |
|-------|-----------|-------------|-------------|-----|
| Large (1000 nodes) | 9.9 | 0.5 | 9.8 | 101 |

Path finding via recursive CTE is comparable to neighborhood queries at scale.

## Impact Analysis

| Scale | Mean (ms) | StdDev (ms) | Median (ms) | OPS |
|-------|-----------|-------------|-------------|-----|
| Large (1000 nodes) | 7.0 | 0.4 | 7.0 | 142 |

Impact analysis (downstream traversal) is the fastest CTE query, likely because
it follows a single direction (outgoing edges only).

## Stats

| Scale | Mean (ms) | StdDev (ms) | Median (ms) | OPS |
|-------|-----------|-------------|-------------|-----|
| Large (1000 nodes) | 4.2 | 0.2 | 4.2 | 238 |

Stats queries are the fastest — simple aggregations with no recursive CTEs.

## Write Operations

| Operation | Mean (ms) | StdDev (ms) | Median (ms) | OPS |
|-----------|-----------|-------------|-------------|-----|
| Create node | 7.8 | 0.3 | 7.8 | 128 |
| Create edge | 23.6 | 1.5 | 23.3 | 42 |

Edge creation is ~3x slower than node creation due to schema validation
(layer/type compatibility checks) and index updates.

## Key Takeaways

- **All CTE queries complete in <10ms** at 1000 nodes — suitable for interactive use
- **Write operations are fast** — 128 ops/sec for nodes, 42 ops/sec for edges
- **Scaling is gentle** — neighborhood queries only 18% slower from 10→1000 nodes
- **No concurrent throughput benchmarks yet** — requires Quack integration (OHM-y2i.4)