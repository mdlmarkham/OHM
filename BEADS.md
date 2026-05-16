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
└── OHM-y2i.5  Systemd unit file + daemon lifecycle

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

- **130 tests passing** (schema, store, graph, boundary, queries, CLI, integration)
- **Phase 0 (Foundation):** Schema, CLI, recursive CTEs, boundary enforcement ✅
- **Phase 1 (Core Operations):** All read/write/challenge/support/observe/listen/state commands ✅
- **Phase 2 (Daemon + Multi-Agent):** Scaffold exists, needs production hardening
- **Phase 3 (DuckLake):** Not started
- **Phase 4 (Agent Integration):** Not started