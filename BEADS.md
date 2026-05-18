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

OHM-0e0    P1: Domain Flexibility — cattle, retail, and beyond
├── OHM-0e0.2  docs: cattle + retail agent workflow scenarios
├── OHM-0e0.4  SDK: temporal confidence decay for observations
├── OHM-0e0.5  ohmd: SSE /events filter by node_type and node_id
├── OHM-0e0.6  SDK: batch expiry detection helper
└── OHM-0e0.7  docs: domain edge type guide

OHM-af8    P1: Multi-scenario Extensibility — medical, cybersecurity, supply chain, customer support
├── OHM-af8.1  Probability-weighted edges for supply chain (IN PROGRESS)
├── OHM-af8.2  Urgency and priority as first-class edge attributes (IN PROGRESS)
├── OHM-af8.3  Medical diagnosis: NEGATES edge type and compound confidence
├── OHM-af8.4  Cybersecurity: source reliability and SSE throughput
├── OHM-af8.5  Customer support: triage, handoff chains, resolution state machine
├── OHM-af8.6  Schema: new edge types across all four scenarios
└── OHM-af8.7  docs: add medical, cybersecurity, supply chain, customer support scenarios

Cross-cutting (security/review findings):
  OHM-hsf  P0: store.py bypasses boundary enforcement on challenges (security bug)
  OHM-zag  P1: No request size cap on POST bodies — OOM risk
  OHM-xfh  P1: SDK missing basic read methods (get_node, get_edge, find_or_create, search)
  OHM-9dq  P1: SDK tests — zero coverage on primary agent interface
  OHM-e19  P2: No SIGPIPE handling in daemon

Closed cross-cutting (completed):
  OHM-654  ✅ Server tests (17 HTTP endpoints)
  OHM-tjr  → replaced by OHM-9dq
  OHM-4w7  ✅ Remove dead queries.py
  OHM-n16  ✅ Document module boundaries
  OHM-ad3  ✅ Update AGENTS.md module docs
  OHM-hol  ✅ Remove dead query.py NLP parser
  OHM-l5k  ✅ CI/CD pipeline (GitHub Actions)
  OHM-xj4  P1: marimo-pair integration
  OHM-dy9  P2: Performance benchmarks
```

## Conventions

- **Claim before you start:** `bd claim OHM-xxx` — prevents two agents working the same issue
- **Notes, not novels:** `bd update OHM-xxx --notes "brief progress"` — keep it scannable
- **Close when done:** `bd close OHM-xxx` — don't leave things in_progress
- **Sync when you push:** `bd sync` then `git push` — keeps issue state visible to other agents

## Current State

- **180 tests passing** (schema, store, graph, boundary, queries, CLI, integration, exceptions, validation, server)
- **Phase 0 (Foundation):** Schema, CLI, recursive CTEs, boundary enforcement ✅
- **Phase 1 (Core Operations):** All read/write/challenge/support/observe/listen/state commands ✅
- **Phase 2 (Daemon):** Scaffold + error handling + health endpoints + auth + Quack ✅
- **P0 Security:** Parameterized queries (OHM-y2i.6), auth fail-closed (OHM-y2i.7), path traversal fix (OHM-y2i.9) ✅
- **SDK:** Python SDK (`ohm.sdk`) for programmatic agent access ✅
- **Validation:** Input validation module (SQL injection prevention for CTE identifiers) ✅
- **Phase 3 (DuckLake):** Not started
- **Phase 4 (Agent Integration):** Blocked by SDK gaps (OHM-xfh, OHM-9dq)

## Known Issues

### P0 — Critical
- **store.py boundary bypass:** `challenge_edge()` in store.py doesn't call `enforce_challenge_boundary()`, allowing L1/L2 challenges through the daemon path (OHM-hsf)

### P1 — High
- **No request size cap:** `_read_body()` reads unlimited Content-Length bytes (OOM risk) (OHM-zag)
- **SDK missing read methods:** No `get_node()`, `get_edge()`, `find_or_create()`, `search()` — blocks all agent integrations (OHM-xfh)
- **SDK zero test coverage:** Primary agent interface has no tests (OHM-9dq)

### P2 — Medium
- **No SIGPIPE handling:** Daemon can crash on broken connections (OHM-e19)

### Closed/Resolved
- **Dead code removed:** queries.py top-level, query.py NLP parser ✅
- **Module boundaries documented:** store.py vs queries/ ✅
- **Server tests:** 17 HTTP endpoints now covered ✅
- **Auth fail-open fixed:** Default is fail-closed, --no-auth for dev ✅
- **Parameterized queries:** f-string SQL replaced ✅