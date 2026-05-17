# PM_UPDATE.md — OHM Build Status

## Latest Commit: `9336f42` (2026-05-16)

## Test Status
- **245 tests passing** (up from 134 at session start — +83% increase)
- 39 substrate-specific tests in `tests/test_substrate.py`

## Session Progress (May 16)
- **38 issues closed** today (107 total)
- **Tests**: 134 → 245 (+111 new tests)
- **Commits**: ~25 today

### All Implemented Features

**Schema & Core (v0.4.0)**
- Agent, skill, value, goal, topic node types
- VALUES, GOALS, CAPABLE_OF, INTERESTED_IN (L1)
- NOTIFIES, SERVES, TRUSTS (L2)
- LISTENS_TO, DEFERS_TO, COLLABORATES_WITH (L3)
- EXPECTS_FROM (L4)
- register_agent(), batch writes, observation decay

**Substrate Methods (7)**
1. Anomaly detection (sigma-based)
2. Contradiction detection (3 types)
3. Agent heartbeat (alive/stale/dead)
4. Observation aggregation (weighted/mean/max_confidence/consensus)
5. Monte Carlo impact simulation
6. Near-duplicate detection
7. Confidence calibration

**Agent Features (5)**
8. Identity evolution (L1 edges with provenance trail)
9. Cold start discovery (peer matching)
10. L2 immutability (sources cannot be updated)
11. Change feed consumer (listen() with filtering)
12. Edge versioning (full lifecycle history)

**Discovery & Export (3)**
13. Connection discovery (shared tags + co-occurrence)
14. Graph export (JSON round-trip)
15. Graph import (merge or replace mode)

**Infrastructure**
16. 9+ HTTP endpoints for ohmd
17. Marimo notebook integration (OHMPair)
18. Agent onboarding docs + skill package
19. Agent relationship model + challenges doc

**Bug Fixes (6)**
- Neighborhood CTE duplicates, duplicate node IDs, timestamp Z suffix,
  APPLIES_TO/RELATED_TO in L3, PREDICTS in L4, change feed gap

## Open Issues: 26
- **P0**: 5 (all Quack — multi-process access)
- **P1**: 15 (Quack children, agent integrations, DuckLake)
- **P2**: 6 (webhook, encrypted tokens, TOPO)

## Remaining Work (all infrastructure-blocked)

### P0 — Production Infrastructure
1. **OHM-y2i.4**: Quack protocol — concurrent multi-process access
2. OHM-y2i.14: TLS termination
3. OHM-y2i.15: Rate limiting
4. OHM-y2i.16: Request size cap

### P1 — Agent Integration
5. OHM-a35.1-4: Individual agent integrations (SDK exists; wiring needed)
6. OHM-xgm.1: DuckLake shared backend

### P2 — Knowledge Domains
7. OHM-3w1: TOPO — industrial knowledge graph

## SDK Surface (complete for v0.4.0)
- create_node, create_edge, observe, challenge, support
- get_node, get_edge, find_or_create_node, search_nodes
- neighborhood, register_agent, listen, pending_notifications
- anomalies, contradictions, heartbeat, agent_health, aggregate
- evolve_identity, discover_peers, stale_edges, provenance, health
- monte_carlo, near_duplicates, calibration
- suggest_connections, export_graph, import_graph, edge_history
- batch_create_nodes, batch_create_edges

## HTTP Endpoints (17+)
GET: /health, /status, /schema, /layers, /node/:id, /edge/:id,
     /neighborhood/:id, /path/:from/:to, /impact/:id, /confidence/:id,
     /agent/:name, /agents, /listen, /search, /health/graph,
     /health/agents, /contradictions, /anomalies, /aggregate/:id,
     /provenance/:id, /stale, /monte-carlo/:id, /duplicates,
     /calibration/:name
POST: /node, /edge, /challenge/:id, /support/:id, /observe/:id,
      /state, /register, /heartbeat
