# OHM Implementation Roadmap

## Phase 0: Foundation (Week 1-2)

**Goal:** `ohmd` daemon + Quack server + basic schema

- [ ] Create `ohmd` daemon (owns DuckDB file, runs Quack server)
  - systemd unit file
  - Health check endpoint
  - Token auth with role-based access per agent
  - Graceful shutdown with checkpoint
- [ ] Create OHM schema (nodes, edges, observations, agent_state)
- [ ] Create `ohm` CLI scaffold
  - `ohm serve start/stop/status`
  - `ohm graph schema`
  - `ohm graph status`
- [ ] Test Quack concurrent access with multiple agent tokens
- [ ] Verify recursive CTE queries work through Quack

**Deliverable:** `ohm serve` runs. `ohm graph status` returns node/edge counts. Multiple agents can connect concurrently.

## Phase 1: Core Operations (Week 3-4)

**Goal:** Read and write the graph via CLI

- [ ] `ohm graph write` — create nodes and edges with attribution
- [ ] `ohm graph neighborhood` — bounded-depth traversal via CTE
- [ ] `ohm graph query` — natural language query (wrap CTE views)
- [ ] `ohm graph confidence` — provenance and challenge audit
- [ ] `ohm graph challenge` — create CHALLENGED_BY edges (not overwrite)
- [ ] `ohm graph support` — create SUPPORTS edges
- [ ] Boundary enforcement: no agent can overwrite another agent's edges

**Deliverable:** Agents can create, read, and challenge edges. Boundary rules enforced.

## Phase 2: Change Feed + Awareness (Week 5-6)

**Goal:** Agents see what other agents are thinking

- [ ] `ohm graph listen` — change feed since last check
- [ ] `ohm state` — set and query agent focus
- [ ] `ohm state who-is-working-on` — find collaborators by topic
- [ ] DuckLake integration for time travel and change feed
- [ ] Local DuckDB cache per agent (sync on heartbeat)
- [ ] `ohm snapshot` — query graph state at any historical timestamp

**Deliverable:** Change feed works. Agent state visible. Local caches sync from DuckLake.

## Phase 3: Agent Integration (Week 7-8)

**Goal:** Each Olympus agent uses OHM as its knowledge graph

- [ ] Métis integration — zettelkasten notes → OHM nodes, wikilinks → OHM edges
- [ ] Clio integration — research findings → OHM L3 edges with source attribution
- [ ] Hephaestus integration — audit findings → OHM observations
- [ ] Socrates integration — challenges → OHM CHALLENGED_BY edges
- [ ] Promote shared graph from Kuzu → DuckLake migration
- [ ] marimo-pair integration — OHM queries in notebooks via Quack

**Deliverable:** All agents reading/writing via OHM. Kuzu deprecated.

## Phase 4: Advanced Queries (Week 9-10)

**Goal:** The full power of L1-L4 traversal

- [ ] `ohm graph impact` — downstream impact analysis (FR-2 pattern)
- [ ] `ohm graph path` — shortest path between two nodes
- [ ] `ohm diff` — what changed between two timestamps
- [ ] Materialized views for hot-path queries (impact radius, confidence audit)
- [ ] `ohm graph stats` — edge counts by layer, confidence distribution, challenge ratio

**Deliverable:** Full L1-L4 query capability. Impact analysis, path finding, temporal diffing.

## Phase 5: TOPO Instantiation (Ongoing)

**Goal:** Apply the same architecture to TOPO (industrial knowledge graph)

- [ ] Create `topo` as a specialized `ohm` instance
- [ ] TOPO-specific edge types (FEEDS, CAUSES, PREDICTS, etc.)
- [ ] TOPO-specific node types (equipment, system, area, site)
- [ ] `topo` CLI with industrial-specific commands
- [ ] Shared `ohmd`/`topod` daemon codebase
- [ ] Same DuckDB + Quack + DuckLake stack, different domain

**Deliverable:** `topo` CLI running on same architecture as `ohm`. One pattern, two instantiations.