# PM Update — May 16, 2026 17:56 EDT

This replaces all previous PM updates. Read this first.

## BUILD NEXT (in order)

### 1. OHM-5of — Security hardening [P0, bug]
TLS termination, rate limiting, request size cap, encrypted token storage.
SQL injection and auth bypass are FIXED. This is the remaining security work.
Run `bd show OHM-5of` for acceptance criteria.

### 2. OHM-y2i.4 — Quack protocol integration [P0, feature]
Replace single-threaded http.server with Quack for multi-process concurrent DuckDB access.
This is the last P0 before team testing can begin.
Run `bd show OHM-y2i.4` for acceptance criteria.

### 3. OHM-a35.10 — Agent registration [P1, feature]
First-class AGENT nodes with VALUES, GOALS, CAPABLE_OF edges.
This unblocks all agent integration (a35.1-4).
Run `bd show OHM-a35.10` for acceptance criteria.

### 4. OHM-a35.8 — SDK documentation [P1, feature]
Agent-facing guide: how to connect, register, write edges, read the graph.
Not CLI docs — agent integration guide.

### 5. OHM-xgm.1 — DuckLake shared backend [P1, feature]
WAL, snapshots, change feed for multi-agent shared truth.

That's it. Do not start anything else until these five are done.

---

## STOP — Read This Before Starting Work

Run `git pull && bd list` to see current state. 21 issues were closed today. Do not rebuild any of them.

## Completed Issues (DO NOT REBUILD)

These are DONE. Do not start work on them:
- ✅ OHM-y2i.1: Error handling (commit 89e2848)
- ✅ OHM-y2i.2: Token auth (commit 2567515)
- ✅ OHM-y2i.3: Health endpoints (commit 89e2848)
- ✅ OHM-y2i.5: Systemd unit file (commit d11cf9c)
- ✅ OHM-y2i.6: Parameterized queries (commit 6e1ba86)
- ✅ OHM-y2i.7: Auth fail-closed with --no-auth flag (commit 6e1ba86)
- ✅ OHM-y2i.9: Server parameter ID validation (commit 0dea116)
- ✅ OHM-evm: Boundary enforcement fix for store.py (commit 010b68b)
- ✅ OHM-654: Server tests — 17+ HTTP endpoints (commit d11cf9c)
- ✅ OHM-tjr: SDK tests — all Graph methods (commit d11cf9c)
- ✅ OHM-l5k: CI/CD (commit 89e2848)
- ✅ OHM-dw1: CI deps (commit 2567515)
- ✅ OHM-4w7: Dead queries.py removed (commit 2567515)
- ✅ OHM-hol: Dead query.py removed (commit 2963d97)
- ✅ OHM-ad3: Module boundary docs (commit 06f017b)
- ✅ OHM-n16: store.py vs queries/ docs (commit 06f017b)
- ✅ OHM-a35.6: Unicode node IDs (commit 6bbda35)
- ✅ OHM-a35.7: Richer SDK return types (commit 6bbda35)
- ✅ OHM-a35.9: SDK read methods — get_node, get_edge, find_or_create, search (commit 1c69836)
- ✅ OHM-8c1: Schema migration framework (commit ef8c123)
- ✅ OHM-imf: Observation stats in /stats endpoint (commit 5cd151c)

## Current State

- **193 tests passing** (up from 134 at start of day)
- **Core daemon working** — ohmd starts, handles auth, serves all 17+ endpoints
- **SDK feature-complete for P0** — create, read, challenge, support, observe, search, find_or_create, confidence, neighborhood, path, impact, listen
- **Security hardening done** — parameterized queries, fail-closed auth, boundary enforcement, ID validation

## Remaining P0 Issues

1. **OHM-5of** — Security: TLS, rate limiting, request size cap, plaintext token storage. The SQL injection and auth bypass issues are FIXED (y2i.6, y2i.7, evm). Remaining: TLS termination, rate limiting, request size limit, encrypted token storage.

2. **OHM-y2i.4** — Quack protocol integration for concurrent access. ohmd currently uses Python's http.server (single-threaded). Quack gives multi-process concurrent access to DuckDB.

3. **OHM-y2i (epic)** — The P0 epic. All children are done except y2i.4 (Quack) and 5of (security). When those close, close the epic.

## P1 Priority Order (after P0s close)

| Priority | Issue | What to build |
|---|---|---|
| 1 | OHM-a35.10 | Agent registration — first-class nodes for identity, values, goals, capabilities |
| 2 | OHM-a35.8 | SDK documentation — agent-facing guide, not CLI docs |
| 3 | OHM-a35.5 | Agent values and goals — capture what each agent optimizes for |
| 4 | OHM-xgm.1 | DuckLake shared backend — WAL, snapshots, change feed |
| 5 | OHM-xgm.4 | Agent heartbeat sync — local cache → DuckLake propagation |
| 6 | OHM-xj4 | marimo-pair integration — OHM queries in notebooks |

Agent integration (a35.1-4) blocked until a35.10 (registration) and a35.8 (docs) are done. Don't wire agents to the SDK until registration and docs exist.

## P2 (Design Questions, Not Buildable Yet)

- **OHM-qhq**: Substrate services (memory, connections, attention, decay, provenance, dedup, calibration, synthesis prompts)
- **OHM-8m3**: Substrate methods (Monte Carlo, Bayesian fusion, anomaly detection)
- **OHM-uo4**: Change feed consumer with push notifications
- **OHM-dy9**: Performance benchmarks
- **OHM-3w1**: TOPO industrial knowledge graph

See `docs/substrate-services.md` and `docs/substrate-methods.md` for design thinking. These are not buildable until P0 and P1 are done.

## Architecture Direction (LOCKED)

- **SDK is the primary agent interface.** CLI is for human diagnostics. ohmd is for shared HTTP access.
- **Challenge edges, not modification.** ADR-002. Locked.
- **DuckDB, not Postgres.** Locked.
- **Quack for concurrent access.** Locked.
- **Attribution on every write.** Locked.
- **Two codepaths are intentional:** `queries/` for direct-connection (CLI, SDK, tests), `store.py` for daemon (ohmd only). Do not merge them.
- **Cognition substrate, not just knowledge graph.** OHM provides memory, connections, attention, decay, provenance. Agents provide meaning-making. See docs/substrate-services.md.

## PM Decisions (ALL RESOLVED — nothing is blocked on PM)

All 20 previously flagged decisions are resolved. See the previous PM update for the full table. Key ones:

- Auth: **fail-closed** by default. `--no-auth` for dev mode. `OHM_NO_AUTH=1` env var.
- SDK is **primary agent interface**. CLI for diagnostics only.
- Challenge edges, **not modification**. Locked.
- Observation aggregation: **accumulate, don't collapse**. Three observations stay as three.
- Confidence decay: **30-day half-life** for L4 predictions.
- DuckLake sync: **heartbeat-based, 5-15 min intervals**.
- Conflict resolution: **last-write-wins for L2, agent-owned for L3/L4**.

## What NOT To Do

- Do NOT rebuild closed issues (see list above — 21 issues closed today)
- Do NOT add NL query parsing (removed, dead code)
- Do NOT merge store.py and queries/ (intentional separation)
- Do NOT wire agents through the CLI for regular operations
- Do NOT ask for PM decisions — they're all answered
- Do NOT start P2 work until P0 and P1 are done
- Do NOT implement agent integrations (a35.1-4) until agent registration (a35.10) and SDK docs (a35.8) exist