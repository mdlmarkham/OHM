# OHM Implementation Roadmap

## Phase 0: Foundation ✅ (Complete)

**Goal:** CLI, schema, store, boundary enforcement, graph queries

- [x] OHM schema (nodes, edges, observations, agent_state, change_log)
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
- [x] 144 tests passing across all modules
- [x] `ohmd` HTTP server scaffold (GET/POST endpoints, token auth stub)

**Deliverable:** `ohm graph status` returns node/edge counts. Boundary rules enforced. Challenge edges work. SDK works end-to-end.

## Phase 1: Daemon + Multi-Agent (Week 1-2)

**Goal:** `ohmd` daemon running with Quack server, multiple agents connecting

- [ ] Production-ready ohmd with proper error handling
- [ ] Token auth with role-based access per agent
- [ ] Health check endpoint (`/health`)
- [ ] Systemd unit file
- [ ] Configuration file (`~/.ohm/ohmd.json`)
- [ ] Test concurrent access with multiple agent tokens
- [ ] Verify recursive CTE queries work through HTTP API
- [ ] `ohm serve status` properly detects running daemon
- [ ] Server test coverage (17 HTTP endpoints)
- [ ] SDK test coverage (Graph class methods)
- [ ] Remove dead code (queries.py top-level, query.py NLP parser)
- [ ] Document module boundaries (store.py vs queries/)

**Deliverable:** `ohm serve start` runs daemon. Multiple agents connect via tokens. All CLI commands work through daemon. 17 HTTP endpoints tested.

## Phase 2: DuckLake + Time Travel (Week 3-4)

**Goal:** DuckLake shared backend with change feed and time travel

- [ ] DuckLake integration for shared data storage
- [ ] `ohm snapshot <timestamp>` — query graph state at any historical point
- [ ] `ohm diff <date1> <date2>` — what changed between timestamps
- [ ] Change feed with intent (not just data)
- [ ] Local DuckDB cache sync on heartbeat
- [ ] Data versioning and cleanup policies

**Deliverable:** Change feed works across agents. Time travel queries return historical state.

## Phase 3: Agent Integration (Week 5-6)

**Goal:** Each Olympus agent uses OHM as its knowledge graph

- [ ] Métis integration — zettelkasten notes → OHM nodes, wikilinks → OHM edges
- [ ] Clio integration — research findings → OHM L3 edges with source attribution
- [ ] Hephaestus integration — audit findings → OHM observations
- [ ] Socrates integration — challenges → OHM CHALLENGED_BY edges
- [ ] Promote shared graph from Kuzu → DuckLake migration
- [ ] marimo-pair integration — OHM queries in notebooks via Quack

**Deliverable:** All agents reading/writing via OHM. Kuzu deprecated for knowledge graph.

## Phase 4: Advanced Queries + TOPO (Week 7+)

**Goal:** Full L1-L4 power + TOPO instantiation

- [ ] `ohm graph stats` — edge counts by layer, confidence distribution, challenge ratio
- [ ] Materialized views for hot-path queries
- [ ] Confidence decay for L4 (prospective) edges
- [ ] TOPO-specific CLI with industrial edge/node types
- [ ] TOPO-specific commands (failure analysis, compliance mapping)
- [ ] Shared `ohmd`/`topod` daemon codebase

**Deliverable:** Full L1-L4 query capability. TOPO running on same architecture. One pattern, two instantiations.