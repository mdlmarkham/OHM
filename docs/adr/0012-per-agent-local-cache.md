# ADR-004: Per-Agent Local DuckDB Cache

**Status:** Accepted

**Date:** 2026-05-19

## Context

OHM currently uses a single `ohmd` daemon that owns the DuckDB file and serves all agents via HTTP REST API. This creates a single-writer bottleneck — every read and write goes through HTTP, adding latency and creating a single point of failure.

Each agent (Métis, Clio, Socrates, etc.) needs fast local access to the knowledge graph for:
- Neighborhood queries (what concepts connect to this one?)
- Semantic search (find concepts related to X)
- Graph analytics (orphans, hubs, suggestions)
- Deep content retrieval (follow node URLs to full sources)

All of these should be local operations, not HTTP calls.

## Decision

Each agent gets its own local DuckDB file for zero-latency reads and writes, with periodic sync to a shared DuckLake mirror.

```
Atlas ──→ ~/.ohm/agents/atlas/ohm.duckdb
Métis ──→ ~/.ohm/agents/metis/ohm.duckdb  ← embedded, no HTTP
Clio ───→ ~/.ohm/agents/clio/ohm.duckdb
                                    │
                        sync_heartbeat() every 30-60s
                                    │
                                    ▼
                         /var/lib/ohm/ohm_lake.ducklake (shared Parquet)
```

## Usage

```python
from ohm.store import OhmStore
from ohm.schema import SchemaConfig

# Each agent creates its own store
store = OhmStore.for_agent(
    agent_name="metis",
    ducklake_path="/var/lib/ohm/ohm_lake.ducklake",
)

# Read/write locally (zero latency, no HTTP)
store.write_node(id="concept-x", label="X", type="concept", ...)
node = store.get_node("concept-x")

# Sync with other agents on heartbeat
result = store.sync_heartbeat()
# → {"pushed": 3, "pulled": 7, "last_sync": "..."}
```

## Sync Model

1. **Push**: Local changes (new nodes, edges, observations) are written to DuckLake since last sync
2. **Pull**: Changes from other agents in DuckLake are upserted into local DuckDB
3. **Conflict resolution**: Last-write-wins by `updated_at` timestamp. For knowledge graphs, conflicts are rare — agents typically write different perspectives (challenge edges, not competing updates to the same node)
4. **Frequency**: On heartbeat (every 30-60 seconds) or on-demand

## Consequences

### Positive
- **Zero-latency reads**: All neighborhood queries, search, and analytics are local DuckDB queries (microseconds, not milliseconds)
- **No single point of failure**: If ohmd crashes, agents continue working locally
- **No daemon dependency**: Agents can read/write without ohmd running
- **Offline capability**: Agent can work disconnected, sync when reconnected
- **Same API**: `OhmStore.for_agent()` returns the same `OhmStore` object, all methods work identically

### Negative
- **Eventual consistency**: Changes from other agents are visible only after sync_heartbeat()
- **DuckLake lock contention**: Only one process can write to DuckLake at a time. Agents sync through the daemon or take turns
- **Disk space**: Each agent has its own ~5-10MB DuckDB file
- **Sync complexity**: Push/pull logic needs to handle conflicts gracefully

### Neutral
- **ohmd is still useful**: As coordination layer (agent registration, change feed, semantic search endpoint) and for agents that prefer HTTP over direct DuckDB access
- **Library mode first**: The recommended adoption path is `OhmStore.for_agent()` in library mode, not `connect_http()` through the daemon

## Migration Path

1. **Current**: All agents use `connect_http()` → ohmd → single DuckDB
2. **Phase 1**: Each agent creates local `OhmStore.for_agent()` for reads, still uses ohmd for writes
3. **Phase 2**: Each agent uses local `OhmStore.for_agent()` for reads AND writes, syncs via DuckLake
4. **Phase 3**: ohmd becomes optional — only needed for HTTP-only clients and change feed

## Implementation Notes

- `OhmStore.for_agent(agent_name, ducklake_path=...)` creates `~/.ohm/agents/{name}/ohm.duckdb`
- Schema initialization happens automatically on first run
- DuckLake attachment is best-effort — if the lock is held by ohmd, the agent works locally and syncs later
- The DuckDB markdown extension is loaded per-agent (optional, graceful fallback)
- Deep content retrieval (`deep_content()`) works with local file URLs