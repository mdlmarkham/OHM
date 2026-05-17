# PM_UPDATE.md — OHM Build Status

## Latest Commit: `545be7a` (2026-05-16)

## Test Status
- **231 tests passing** (up from 134 at session start)
- 25 substrate-specific tests in `tests/test_substrate.py`

## Session Progress (May 16)
- **32 issues closed** today
- **Tests**: 134 → 231 (+97 new tests, +72% increase)
- **Commits**: ~20 today
- **Features implemented**: 14+

### Implemented This Session

**Substrate Methods (4)**
1. Anomaly detection — sigma-based flagging (OHM-a35.32)
2. Contradiction detection — opposite observations, high-confidence challenges, contradictory L3 (OHM-a35.25)
3. Agent heartbeat — last_sync tracking, alive/stale/dead (OHM-a35.23)
4. Observation aggregation — weighted, mean, max_confidence, consensus (OHM-a35.30)

**Agent Relationship Features (3)**
5. Identity evolution — L1 edges evolve with provenance trail (OHM-a35.18)
6. Cold start discovery — peer discovery by shared values + complementary capabilities (OHM-a35.19)
7. L2 write conflict resolution — source nodes immutable after creation (OHM-a35.35)

**HTTP Endpoints (9)**
8. /search, /health/graph, /health/agents, /contradictions, /anomalies (OHM-a35.13)
9. /aggregate/NODE_ID, /provenance/NODE_ID, /stale (OHM-a35.13)
10. /register (POST), /heartbeat (POST) (OHM-a35.13)

**Infrastructure**
11. Change feed consumer — listen() with filtering (OHM-uo4)
12. Marimo notebook integration — OHMPair class (OHM-xj4)
13. Batch writes — batch_create_nodes, batch_create_edges (OHM-a35.21)
14. Observation decay — query_stale_edges (OHM-a35.20)

**Bug Fixes (6)**
- Neighborhood CTE duplicate edges
- Duplicate node IDs returning 201 → 409 ConflictError
- Timestamp validation rejecting Z suffix
- APPLIES_TO and RELATED_TO missing from L3 edge types
- PREDICTS added to L4
- Change feed gap — _log_change() missing from queries/__init__.py

## Open Issues: 32
- **P0**: 5 (all Quack-related, blocked on production daemon)
- **P1**: 15 (most blocked on agent integration epic or DuckLake epic)
- **P2**: 12 (TOPO industrial KG, some infrastructure)

## BUILD NEXT

### P0 — Production Infrastructure
1. **OHM-y2i.4**: Quack protocol integration — multi-process access
2. **OHM-y2i.14**: TLS termination
3. **OHM-y2i.15**: Rate limiting
4. **OHM-y2i.16**: Request size cap

### P1 — Agent Integration
5. **OHM-a35.1-4**: Individual agent integrations (Métis, Clio, Hephaestus, Socrates)
   - SDK + onboarding docs exist; these are integration tasks, not new features
6. **OHM-xgm.1**: DuckLake shared backend

### P2 — Knowledge Domains
7. **OHM-3w1**: TOPO — industrial knowledge graph (separate concern)

## Architecture Summary

```
OHM Substrate
├── SDK (ohm.sdk.Graph)          — Agent-facing API
├── Queries (ohm.queries)        — 7 CTE functions + provenance + health + decay + batch
├── Methods (ohm.methods)        — Substrate computation: anomalies, contradictions, heartbeat, aggregation
├── Server (ohm.server)          — 17+ HTTP endpoints (ohmd daemon)
├── Boundary (ohm.boundary)      — Layer ownership + L2 immutability + identity evolution
├── Schema (ohm.schema)          — v0.4.0 DDL + validation
├── Validation (ohm.validation)  — Identifier + timestamp + confidence
├── Exceptions (ohm.exceptions)  — 8 custom types
├── Marimo (ohm.marimo_pair)     — Notebook integration
├── CLI (ohm.cli)                — Human diagnostics
└── Store (ohm.store)            — Daemon ORM (separate from queries/)
```

## Key Invariants
- Challenge edges, not modification (ADR-002)
- Attribution on every write
- Parameterized queries, not f-string interpolation
- L2 sources immutable after creation
- L1 identity edges evolvable with provenance
- Observation decay computed at read time
- Substrate methods produce same result regardless of caller
