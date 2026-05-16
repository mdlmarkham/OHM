# OHM Architecture Decision Records

## ADR-001: DuckDB + Recursive CTEs over DuckPGQ for Graph Traversal

**Date:** 2026-05-16
**Status:** Decided

### Context

OHM needs graph traversal (neighborhood queries, impact analysis, path finding). DuckPGQ provides SQL/PGQ `MATCH` syntax but is a community extension maintained by a research group with no release tags and breaking changes on DuckDB upgrades. Kuzu was acquired by Apple (Oct 2025, repo archived) and is not viable.

### Decision

Use recursive CTEs for all production graph queries. DuckPGQ remains optional for ad-hoc exploration.

### Consequences

- Zero-dependency graph queries (standard SQL, works through Quack, survives DuckDB upgrades)
- Bounded-depth traversals (1-5 hops) are well within CTE performance at OHM's scale
- Need to implement ~7 parameterized CTE views for common query patterns
- DuckPGQ `MATCH` syntax is nicer to write but cannot be relied on for production

---

## ADR-002: Quack for Concurrent Access

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents need to read and write the knowledge graph simultaneously. DuckDB is single-writer by default.

### Decision

Use DuckDB's Quack protocol (HTTP-based, token-authenticated, multi-reader/multi-writer). Requires a persistent daemon (`ohmd`) to own the DuckDB file and serve connections.

### Consequences

- Requires `ohmd` daemon (systemd service, auto-restart, health check)
- All agents connect via Quack instead of direct file access
- Token auth with role-based access control per agent
- Quack currently ships from `core_nightly` — must pin binary for production

---

## ADR-003: Agent-Owned Edges with Challenge Semantics

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents will create L3 (Knowledge) and L4 (Prospect) edges about the same topics. Averaging confidence scores would destroy individuality. Allowing overwrites would lose perspectives.

### Decision

Every L3/L4 edge has a single owner (`created_by`). Other agents can create CHALLENGED_BY, SUPPORTS, or DERIVED_FROM edges that reference the original, but cannot modify or delete it.

### Consequences

- The graph accumulates perspectives without collapsing them
- Confidence scores reflect the owning agent's judgment
- Humans see the full picture including disagreements
- Challenge edges create productive tension, not consensus averaging
- Requires `created_by` and `updated_by` columns on all L3/L4 tables

---

## ADR-004: Three-Layer Data Architecture

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents need fast local access to their working set, shared access to the canonical graph, and private space for unfinished work.

### Decision

Three layers:
1. **Local DuckDB cache** — per-agent working memory, synced from DuckLake on heartbeat
2. **DuckLake shared backend** — canonical graph, time travel, change feed, agent state
3. **Private** — agent-only notes below confidence threshold, personal observations, scratch calculations

### Consequences

- Cache invalidation via DuckLake `table_changes()` (incremental, not full sync)
- Private layer never promoted automatically; per-agent threshold for promotion
- Change feed carries `agent_name` attribution on every write
- Time travel enables "what did we know at time T?" queries

---

## ADR-005: Self-Documenting CLI as Agent Interface

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents should not need to know SQL, DuckDB internals, CTE structure, or Quack protocol details to use the graph.

### Decision

Package the entire stack as `ohm` CLI. Agents call `ohm graph write`, `ohm graph listen`, `ohm state show`, not raw SQL.

### Consequences

- Implementation can evolve (CTEs→DuckPGQ, file→Quack, Kuzu→DuckLake) without breaking agents
- `ohm graph schema` and `ohm graph layers` are living documentation
- `ohm graph listen --since last-check` wraps the change feed
- The CLI is the contract between agents and the graph