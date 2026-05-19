# OHM Implementation Roadmap

## Phase 0: Foundation ✅ (Complete)

**Goal:** CLI, schema, store, boundary enforcement, graph queries

- [x] OHM schema (nodes, edges, observations, agent_state, change_log, agent_config, meta)
- [x] DuckDB store with all CRUD operations
- [x] Recursive CTE queries (neighborhood, path, impact, confidence audit)
- [x] `ohm` CLI: write, neighborhood, path, impact, confidence, challenge, support, observe, listen
- [x] Agent state: set, show, who-is-working-on
- [x] Boundary enforcement: no agent can overwrite another agent's edges
- [x] Challenge edges: create separate, don't modify
- [x] Python SDK (`ohm.sdk`) for programmatic agent access
- [x] Input validation module (SQL injection prevention for CTE identifiers)
- [x] Exception hierarchy with exit codes (0-5) and correlation IDs
- [x] CLI integration tests against real database
- [x] 528 tests passing across all modules
- [x] `ohmd` HTTP daemon with auth, error handling, health endpoints

## Phase 1: Daemon + Multi-Agent ✅ (Complete)

**Goal:** `ohmd` daemon running with auth, multiple agents connecting

- [x] Production-ready ohmd with proper error handling and correlation IDs
- [x] Token auth with per-agent Bearer tokens
- [x] Health check endpoints (`/health`, `/ready`, `/status`)
- [x] Systemd unit file (`ohmd.service`)
- [x] Configuration file (`/etc/ohm/ohmd.json`)
- [x] Recursive CTE queries through HTTP API
- [x] Server test coverage (17 HTTP endpoints)
- [x] Remove dead code (queries.py top-level, query.py NLP parser)
- [x] Document module boundaries (store.py vs queries/)
- [x] 9 agents registered with values, goals, and capabilities

**Deliverable:** `ohmd` runs as systemd service. Multiple agents connect via tokens. All CLI commands work through daemon. Full HTTP API.

## Phase 2: Domain Flexibility ✅ (Complete)

**Goal:** OHM works for multiple domains beyond cognitive agents

- [x] Schema v0.5.0: urgency (edges), priority (nodes), probability (edges)
- [x] NEGATES edge type for medical diagnosis (rules out conditions)
- [x] Scenario-specific edge types: BATCH_EXPIRES_BEFORE, TRANSFERRED_TO, ESCALATED_TO, THREAT_CLUSTER, ORDERS_TEST, TRIGGERS_INCIDENT, etc.
- [x] compound_confidence() with correlation parameter
- [x] differential_diagnosis() for medical reasoning
- [x] rules_out() for negative evidence
- [x] threat_cluster() for cybersecurity IOC correlation
- [x] Batch SSE writes for high-velocity scenarios
- [x] Temporal confidence decay (decay_observations, expiring_soon)
- [x] Multiplicative composite scoring
- [x] Source reliability calibration (record_outcome, source_reliability)
- [x] Handoff chains and escalation (handoff, escalate)
- [x] Cascade simulation (cascade_scenario, what_if)
- [x] Urgent change filtering (urgent_changes)
- [x] 6 scenario docs with runnable code examples
- [x] 603+ tests passing

## Phase 3: Agent Integration (Next)

**Goal:** Each Olympus agent uses OHM as its knowledge graph

- [ ] Métis integration — zettelkasten notes → OHM nodes, wikilinks → OHM edges
- [ ] Clio integration — research findings → OHM L3 edges with source attribution
- [ ] Hephaestus integration — audit findings → OHM observations
- [ ] Socrates integration — challenges → OHM CHALLENGED_BY edges
- [ ] SDK tests (OHM-9dq) — zero coverage on primary agent interface
- [ ] marimo-pair integration — OHM queries in notebooks via Quack

**Deliverable:** All agents reading/writing via OHM. Shared graph accumulates perspectives.

## Phase 4: DuckLake + Time Travel (Later)

**Goal:** DuckLake shared backend with change feed and time travel

- [ ] DuckLake integration for shared data storage
- [ ] `ohm snapshot <timestamp>` — query graph state at any historical point
- [ ] `ohm diff <date1> <date2>` — what changed between timestamps
- [ ] Change feed with intent (not just data)
- [ ] Local DuckDB cache sync on heartbeat
- [ ] Data versioning and cleanup policies

**Deliverable:** Change feed works across agents. Time travel queries return historical state.

## Phase 5: Advanced Queries + TOPO (Later)

**Goal:** Full L1-L4 power + TOPO instantiation

- [ ] Materialized views for hot-path queries
- [ ] Source reliability calibration across agents
- [ ] Cascade simulation with Monte Carlo
- [ ] TOPO-specific CLI with industrial edge/node types
- [ ] TOPO-specific commands (failure analysis, compliance mapping)
- [ ] Shared `ohmd`/`topod` daemon codebase

**Deliverable:** Full L1-L4 query capability. TOPO running on same architecture. One pattern, two instantiations.

## Cross-cutting Issues

| ID | Priority | Title | Status |
|----|----------|-------|--------|
| OHM-9dq | P1 | SDK tests — zero coverage on primary agent interface | Done (137 tests) |
| OHM-zag | P1 | No request size cap on POST bodies — OOM risk | Done (MAX_BODY_SIZE=1MB) |
| OHM-e19 | P2 | No SIGPIPE handling in daemon | Open |
| OHM-pfk | P1 | Comprehensive doc update for multi-scenario architecture | Done |
| OHM-c8i | P1 | ADRs for probability/confidence, NEGATES, urgency/priority | Done (ADR-008/009/010) |
| OHM-5di | P2 | ADR for observation type extensibility | Done (ADR-011) |