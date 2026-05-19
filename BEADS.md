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
OHM-y2i    P0: Production Daemon — ohmd with Quack, auth, error handling ✅
├── OHM-y2i.1  Error handling (exit codes, correlation IDs) ✅
├── OHM-y2i.2  Token auth (Bearer tokens, per-agent roles) ✅
├── OHM-y2i.3  Health check + status endpoints ✅
├── OHM-y2i.4  Quack protocol integration ✅
├── OHM-y2i.5  Systemd unit file + daemon lifecycle ✅
├── OHM-654     Server tests — 17 HTTP endpoints ✅
├── OHM-4w7     Remove dead queries.py top-level module ✅
├── OHM-n16     Document store.py vs queries/ boundaries ✅
└── OHM-ad3     Update AGENTS.md with module boundaries ✅

OHM-xgm    P1: DuckLake + Time Travel (partially complete)
├── OHM-xgm.1  DuckLake shared backend (mirror tables, sync) ✅
├── OHM-xgm.2  /admin/snapshots + /graph/at endpoints ✅
├── OHM-xgm.3  /graph/changes diff endpoint ✅
├── OHM-xgm.4  WAL corruption recovery (InternalException + IOException, DuckLake fallback → WAL deletion) ✅
├── OHM-xgm.4a DuckLake pull type casting (VARCHAR→FLOAT/TIMESTAMP in mirror→local sync) ✅
├── OHM-xgm.4b /admin/checkpoint endpoint (force WAL flush to main DB) ✅
├── OHM-xgm.5  ohm snapshot CLI command
└── OHM-xgm.6  ohm diff CLI command

OHM-a35     P1: Agent Integration (in progress)
├── OHM-a35.1  Métis — zettelkasten → OHM nodes/edges ✅
├── OHM-a35.2  Hephaestus — audit findings → observations ✅ (1 pattern + CHALLENGED_BY)
├── OHM-a35.3  Clio — research findings → L3 edges
├── OHM-a35.4  Socrates — tested: 6 CHALLENGED_BY, 8 patterns, 4 domains written to graph ✅ (learnings incorporated, no agent-specific code paths)
├── OHM-a35.5  DeepThought — tested: 5 concepts + REFINES written to graph ✅ (learnings incorporated: support hangs, search empty, edge naming, quack:// URI)
└── OHM-a35.6  Agent values and goals config ✅ (9 agents registered)

Note: Agent content lives in the graph, not in code. Integrations.py provides thin SDK wrappers; no agent-specific logic in the codebase.

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

Schema gaps (P0 — blocking multi-scenario features):
  OHM-pap  P0: Add urgency (edges) + priority (nodes) to schema — blocks OHM-af8.2, af8.4, af8.5
  OHM-2xy   P0: Add probability field to edges — blocks OHM-af8.1 (supply chain, risk modeling)

Cross-cutting (security/review findings):
  OHM-zag  P1: No request size cap on POST bodies — OOM risk
  OHM-9dq  P1: SDK tests — zero coverage on primary agent interface
  OHM-7e4  P1: SDK source_reliability() + record_outcome() — needed for OHM-af8.4
  OHM-3yo  P1: SDK handoff() + escalate() — needed for OHM-af8.5
  OHM-e19  P2: No SIGPIPE handling in daemon

Documentation:
  OHM-pfk  P1: Update all docs for multi-scenario architecture (VISION, roadmap, scenarios, schema, onboarding)
  OHM-c8i  P1: ADR for probability/confidence separation, NEGATES semantics, urgency vs priority
  OHM-5di  P2: ADR for observation type extensibility

Closed cross-cutting:
  OHM-hsf  ✅ store.py boundary enforcement (fixed in OHM-evm)
  OHM-xfh  ✅ SDK read methods (added in commit 1c69836)
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

- **603+ tests passing** (schema, store, graph, boundary, queries, CLI, integration, exceptions, validation, server)
- **Phase 0 (Foundation):** Schema, CLI, recursive CTEs, boundary enforcement ✅
- **Phase 1 (Core Operations):** All read/write/challenge/support/observe/listen/state commands ✅
- **Phase 2 (Daemon):** Scaffold + error handling + health endpoints + auth + Quack ✅
- **Phase 2 (Domain Flexibility):** Multi-scenario schema v0.5.0, 6 scenario docs ✅
- **P0 Security:** Parameterized queries, auth fail-closed, path traversal fix ✅
- **SDK:** Python SDK (`ohm.sdk`) for programmatic agent access ✅
- **Validation:** Input validation module (SQL injection prevention for CTE identifiers) ✅
- **Phase 3 (Agent Integration):** Métis ✅, Hephaestus partial, Socrates ✅ (6 CHALLENGED_BY), DeepThought ✅ (content + feedback), others pending
- **Phase 4 (DuckLake):** Mirror tables ✅, /admin/snapshots ✅, /graph/at ✅, /graph/changes ✅, WAL recovery ✅ (InternalException fix), pull type casting ✅, /admin/checkpoint ✅. CLI commands pending.
- **Persistence workflow:** write → DuckLake sync → checkpoint → WAL flushed → durable
- **Live stats:** 146 nodes, 132 edges, 44 L3 edges, 9 CHALLENGED_BY, ~200 DuckLake snapshots, daemon uptime ~5min

## Known Issues

### P0 — Critical
- **DELETE on nodes corrupts DB when DuckLake mirror tables exist:** DuckDB index deletion fails ("Only deleted 0 out of 1 rows"), invalidates database, corrupts WAL. Do NOT use DELETE until fixed. (Found May 19, unfixed)
- **Silent overwrite on duplicate node IDs:** POST /node with existing ID silently overwrites content/confidence/provenance. Returns `created: false` but no 409. Any agent can destroy another's knowledge. (Atlas testing, confirmed)

### P1 — High
- **Support endpoint hangs:** POST /support/{id} never returns (DeepThought testing)
- **Edge field naming mismatch:** Request `from`/`to`/`type` vs response `from_node`/`to_node`/`edge_type` (Atlas testing, confirmed)
- **Semantic search empty:** Search returns no results for known terms — embedding index may not be built (DeepThought testing)
- **No request size cap:** `_read_body()` reads unlimited Content-Length bytes (OOM risk) (OHM-zag) — DONE (MAX_BODY_SIZE=1MB)
- **SDK read methods:** Fixed — `get_node()`, `get_edge()`, `find_or_create()`, `search()` all implemented ✅ (OHM-xfh)
- **SDK test coverage:** 137 tests on primary agent interface ✅ (OHM-9dq)
- **Duplicate agent registration:** 12 agent nodes for 4 active agents (Atlas testing, confirmed)
- **Confidence bounds not enforced:** Values > 1.0 accepted (Atlas testing, confirmed)

### P2 — Medium
- **No SIGPIPE handling:** Daemon can crash on broken connections (OHM-e19)

### Closed/Resolved
- **WAL InternalException not caught:** DuckDB throws InternalException (not IOException) for WAL replay failures. Fixed: catch both + check "replay" keyword. (bb663a7) ✅
- **DuckLake pull silently fails on type mismatch:** Mirror VARCHAR → local FLOAT/TIMESTAMP caused silent INSERT failures. Fixed: explicit CAST during pull. (a6ce948) ✅
- **No WAL flush mechanism:** Added /admin/checkpoint GET endpoint. (a6ce948) ✅
- **Dead code removed:** queries.py top-level, query.py NLP parser ✅
- **Module boundaries documented:** store.py vs queries/ ✅
- **Server tests:** 17 HTTP endpoints now covered ✅
- **Auth fail-open fixed:** Default is fail-closed, --no-auth for dev ✅
- **Parameterized queries:** f-string SQL replaced ✅