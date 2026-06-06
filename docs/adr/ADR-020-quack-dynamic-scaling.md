# ADR-020: Quack-Powered Dynamic Scaling for OHM

**Date:** 2026-06-06
**Status:** Proposed

## Context

OHM currently runs as a single-threaded HTTP daemon (ohmd) backed by DuckDB with a `threading.RLock()` for all writes. Reads use a separate read-only connection that falls back to the write connection on config mismatch. This works for 5-10 agents making occasional writes, but has two scaling limits:

1. **Write serialization**: All mutations go through one RLock. 6 concurrent agents (3 reads + 3 writes) complete in 170ms, but 20+ agents with burst writes will queue.
2. **Single-process bottleneck**: ohmd is one process on one machine. There's no horizontal scaling path.

DuckDB's Quack protocol (HTTP-based client-server) offers a different model: multiple DuckDB processes connect to one Quack server, enabling concurrent multi-writer access. Each agent could run its own DuckDB process that connects to the shared graph via Quack.

The question: **Should OHM dynamically scale endpoints using Quack when concurrent write pressure increases?**

## Analysis

### Current Architecture (Phase 1)

```
Agent → HTTP → ohmd (ThreadedHTTPServer) → RLock → DuckDB (single file)
                                                ↑
                                        All writes serialized
```

- 5-10 agents: No problem. RLock contention is negligible.
- ohmd handles auth, validation, boundary enforcement, challenge semantics
- Reads are parallel (read_conn), writes are serialized (RLock)
- Quack exists as opt-in (`--quack` flag) but is not production-ready

### Quack Architecture (Phase 2)

```
Agent → HTTP → ohmd (validation/auth) → Quack server → DuckDB
Agent → Quack client (direct writes)  → Quack server → DuckDB
```

- Multiple DuckDB processes connect to one Quack server
- Each agent can write concurrently (no RLock)
- ohmd still handles auth, validation, boundary enforcement
- Agents can bypass HTTP for low-latency local writes + Quack sync

### What "Dynamic Scaling" Means

The question isn't "should we use Quack?" — it's "should the system **dynamically** switch between modes based on load?"

**Proposal: No. Use a static deployment model, not dynamic switching.**

Here's why:

#### 1. Mode Switching Is Operationally Dangerous

Switching from RLock → Quack mid-flight means:
- Active writes must drain
- Quack server must start on the existing DuckDB file
- All in-flight HTTP requests must complete before the switch
- If Quack fails to start, must fall back to RLock
- If Quack crashes mid-operation, data consistency must be verified

This is a distributed systems consensus problem. For a knowledge graph daemon, the complexity isn't worth it.

#### 2. The Bottleneck Isn't Where You Think

The real scaling limit isn't DuckDB writes — it's **embedding generation** and **semantic search**. Each node creation triggers:
- 1 embedding generation (synchronous, ~100ms per node)
- 1 suggestion query (semantic search, ~200ms per creation)

These are CPU-bound, not I/O-bound. Quack doesn't help with compute — it helps with concurrent writes. But the bottleneck is compute, not write throughput.

Current performance:
- 6 concurrent agents (3 reads + 3 writes): 170ms total, 0 deadlocks
- Single node creation: ~50ms (without suggestions), ~250ms (with suggestions)
- Embedding batch: 3 per cron cycle, ~30s each

The write lock isn't the bottleneck. The embedding pipeline is.

#### 3. Quack's Production Readiness Is Not There

From ADR-016:
- Quack is only in `core_nightly`, not stable
- The 10-agent concurrent write test **fails** — known gap blocking activation
- Token auth works but TLS requires a proxy
- DuckDB version must be ≥ 1.1.0 for stable Quack

Until Quack ships in stable DuckDB and passes the concurrent write test, it can't be production.

#### 4. The Right Scaling Path

Instead of dynamic Quack switching, OHM should scale in three **static** phases:

### Phase 1: Current (0-15 agents)

```
Agent → HTTP → ohmd (RLock) → DuckDB
```

- Single process, single writer lock
- Works for our current 6-agent deployment
- Optimize embedding pipeline (batch, async, caching)
- This is where we are today

### Phase 2: Per-Agent Local DBs (15-50 agents)

```
Agent → local DuckDB (OhmStore.for_agent) → DuckLake sync
```

- Each agent writes to its own local DuckDB file (zero latency)
- `sync_heartbeat()` pushes local changes to shared DuckLake
- Other agents' changes pulled on heartbeat
- **No HTTP needed for writes** — only for reads from shared graph
- ohmd becomes a read server + sync coordinator
- Quack not required — DuckLake handles multi-writer via its own MVCC

This is already implemented (`OhmStore.for_agent()`). We just haven't activated it.

### Phase 3: Quack for Multi-Tenant (50+ agents)

```
Tenant A → ohmd-A (Quack) → DuckDB-A
Tenant B → ohmd-B (Quack) → DuckDB-B
Tenant C → ohmd-C (Quack) → DuckDB-C
```

- Each tenant gets their own DuckDB file and Quack server
- ohmd instances share nothing (horizontal scaling)
- Load balancer routes agents to their tenant's ohmd
- Quack enables concurrent multi-writer **within** a tenant
- Between tenants: complete isolation

This requires:
- Quack in stable DuckDB
- Passing 10-agent concurrent write test
- TLS termination (nginx/traefik in front of Quack)
- Per-tenant config routing

## Decision

**Do not implement dynamic Quack switching.** Use static deployment models:

1. **Phase 1** (current): Single ohmd with RLock. Optimized for 0-15 agents.
2. **Phase 2** (next): Per-agent local DBs with DuckLake sync. Scales to 50 agents.
3. **Phase 3** (future): Multi-tenant with per-tenant Quack. Scales to hundreds of agents.

Dynamic mode switching introduces distributed consensus problems that aren't worth solving for a knowledge graph. The real bottleneck (embedding generation) isn't solved by Quack anyway.

### What To Optimize Now

Instead of Quack, optimize the actual bottlenecks:

1. **Embedding pipeline**: Switch from per-node synchronous to batch async (already in progress — cron job runs 3/batch)
2. **Suggestion latency**: Cache embedding vectors for recently-created nodes, skip re-embedding
3. **Read throughput**: Connection pooling for the read_conn (currently single read connection)
4. **Write batching**: Accept multiple node/edge creations in one request, write in one transaction

### When To Activate Quack

Quack should be activated when **all** of these are true:
- DuckDB ships Quack in stable (not just core_nightly)
- The 10-agent concurrent write test passes
- Single-tenant write contention exceeds 50ms average wait time
- A multi-tenant deployment requires concurrent multi-writer access

## Consequences

- **No dynamic mode switching**: The deployment model is static per environment
- **Phase 2 is the next step**: Per-agent local DBs with DuckLake sync, not Quack
- **Quack remains opt-in**: `--quack` flag stays, but won't be auto-activated
- **ADR-016 stands**: Quack production readiness criteria unchanged
- **Embedding optimization first**: The real bottleneck isn't writes, it's compute

## Related

- ADR-016: Quack Production Readiness Criteria
- ADR-012: Per-Agent Local Cache (DuckLake sync)
- ADR-015: Multi-Tenancy
- `src/ohm/graph/store.py`: OhmStore.for_agent() — per-agent local DB
- `src/ohm/graph/quack.py`: Quack client-server integration