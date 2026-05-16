# OHM — Beads Issue Tracker

This repo uses **Beads** (`bd`) for project-level task tracking. All collaborating agents should use it.

## Quick Start

```bash
# See available work
bd list
bd ready              # Issues you can start on (not blocked)

# Pick up work
bd show OHM-xxx       # Full details, dependencies, design notes
bd claim OHM-xxx      # Claim an issue (sets assignee + status)

# While working
bd update OHM-xxx --status in_progress --notes "progress update"

# When done
bd close OHM-xxx      # Close with optional notes

# After changes
bd sync               # Push issue state to git
```

## Architecture

```
OHM-y2i    P0: Production Daemon — ohmd with Quack, auth, error handling
├── OHM-y2i.1  Error handling (exit codes, correlation IDs)
├── OHM-y2i.2  Token auth (Bearer tokens, per-agent roles)
├── OHM-y2i.3  Health check + status endpoints
├── OHM-y2i.4  Quack protocol integration
├── OHM-y2i.5  Systemd unit file + daemon lifecycle
├── OHM-654     Server tests — 17 HTTP endpoints, zero coverage
├── OHM-4w7     Remove dead queries.py top-level module
├── OHM-n16     Document store.py vs queries/ boundaries
└── OHM-ad3     Update AGENTS.md with module boundaries

OHM-xgm    P1: DuckLake + Time Travel
├── OHM-xgm.1  DuckLake shared backend (WAL, snapshots, change feed)
├── OHM-xgm.2  ohm snapshot (historical graph state)
├── OHM-xgm.3  ohm diff (change comparison)
└── OHM-xgm.4  Agent heartbeat sync (local ↔ DuckLake)

OHM-a35     P1: Agent Integration
├── OHM-a35.1  Métis — zettelkasten → OHM nodes/edges
├── OHM-a35.2  Clio — research findings → L3 edges
├── OHM-a35.3  Hephaestus — audit findings → observations
├── OHM-a35.4  Socrates — challenges → CHALLENGED_BY edges
└── OHM-a35.5  Agent values and goals config

OHM-3w1     P2: TOPO — Industrial Knowledge Graph
├── OHM-3w1.1  TOPO schema (equipment, system, area, site)
├── OHM-3w1.2  TOPO CLI (failure-analysis, compliance-map)
└── OHM-3w1.3  Shared ohmd/topod daemon codebase

Cross-cutting:
  OHM-654  P0: Server tests (under OHM-y2i)
  OHM-tjr  P1: SDK tests (under OHM-a35)
  OHM-4w7  P1: Remove dead queries.py (under OHM-y2i)
  OHM-n16  P1: Document module boundaries (under OHM-y2i)
  OHM-ad3  P1: Update AGENTS.md module docs (under OHM-y2i)
  OHM-hol  P2: Remove dead query.py NLP parser
  OHM-l5k  P1: CI/CD pipeline (GitHub Actions)
  OHM-xj4  P1: marimo-pair integration
  OHM-dy9  P2: Performance benchmarks
```

## Conventions

- **Claim before you start:** `bd claim OHM-xxx` — prevents two agents working the same issue
- **Notes, not novels:** `bd update OHM-xxx --notes "brief progress"` — keep it scannable
- **Close when done:** `bd close OHM-xxx` — don't leave things in_progress
- **Sync when you push:** `bd sync` then `git push` — keeps issue state visible to other agents

## Current State

- **144 tests passing** (schema, store, graph, boundary, queries, CLI, integration, exceptions, validation)
- **Error handling:** server.py maps all OHMError types to HTTP status codes with correlation IDs ✅
- **Health endpoints:** /health, /ready, /status with uptime and DB connectivity check ✅
- **CI/CD:** GitHub Actions for test + release ✅ (but ruff/mypy not in dev deps yet — OHM-dw1)
- **Performance benchmarks:** Framework exists, needs pytest-benchmark dep
- **Phase 0 (Foundation):** Schema, CLI, recursive CTEs, boundary enforcement ✅
- **Phase 1 (Core Operations):** All read/write/challenge/support/observe/listen/state commands ✅
- **SDK:** Python SDK (`ohm.sdk`) for programmatic agent access ✅
- **Validation:** Input validation module (SQL injection prevention for CTE identifiers) ✅
- **Phase 2 (Daemon + Multi-Agent):** Scaffold + error handling + health endpoints ✅. Token auth and Quack remaining.
- **Phase 3 (DuckLake):** Not started
- **Phase 4 (Agent Integration):** Not started

## Known Issues

- **Dead code:** `src/ohm/queries.py` (top-level) shadows the `queries/` package and is unreachable — needs removal (OHM-4w7)
- **Dead code:** `src/ohm/query.py` (NLP parser) is never used by production code — needs removal (OHM-hol)
- **Module boundary unclear:** `store.py` (daemon ORM) and `queries/` (direct-connection API) both do create_node/create_edge etc. — needs documentation (OHM-n16, OHM-ad3)
- **No server tests:** `server.py` has zero test coverage despite error handling being implemented (OHM-654)
- **No SDK tests:** `sdk.py` has no dedicated test file (OHM-tjr)
- **CI will fail:** `ruff`, `mypy`, `pytest-benchmark` not in dev dependencies (OHM-dw1)
- **Benchmark tests broken:** Need `pytest-benchmark` fixture (8 tests error on missing fixture)