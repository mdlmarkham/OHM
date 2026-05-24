# ADR-015: Multi-Tenancy — Single-Process Isolated DuckDB Instances

## Status: Accepted

## Context

OHM currently runs as a single-tenant system: one `ohmd` process, one DuckDB file, one set of agent tokens. The TeamWork AI platform needs to serve multiple customers (home services shops, manufacturing plants, healthcare practices) from a single deployment. Each customer must have complete data isolation — no cross-tenant reads, writes, or inference bleed.

### Requirements

1. **Strong isolation**: Customer A cannot see, touch, or infer from Customer B's data
2. **Domain flexibility**: Each customer may use a different domain schema (home_services, topo, healthcare)
3. **Economical**: One process, one machine — not one ohmd per customer
4. **Backward compatible**: Existing single-tenant deployments and agent workflows unchanged

### Decision Drivers

- DuckDB is single-writer — concurrent writes to one file are serialized at the C level
- Agent model assumes 1 agent = 1 local DB (ADR-012)
- TOPO already uses a separate binary (`topod`) with different schema — domain templates should replace this
- Bayesian inference has a module-level cache keyed by query parameters (no tenant dimension)

## Decision

**One `ohmd` process, N isolated DuckDB files, per-tenant LRU cache.**

### Architecture

```
ohmd (single process, ThreadingMixIn)
├── core OHM (agent tokens → ~/.ohm/ohm.duckdb)
└── TenantManager (LRU cache, max 100)
    ├── tenant acme_hvac → /var/lib/ohm/tenants/acme_hvac/ohm.duckdb
    ├── tenant wayne_mfg → /var/lib/ohm/tenants/wayne_mfg/ohm.duckdb
    └── tenant metro_health → /var/lib/ohm/tenants/metro_health/ohm.duckdb
```

### Key Components

1. **TenantManager** (`ohm/tenant.py`): LRU cache of `OhmStore` instances keyed by `customer_id`. `get_store(customer_id)` returns cached or opens new connection. Idle eviction after 10 min. `provision()` creates new instance from domain template.

2. **Customer API Keys**: Separate `customer_tokens` dict: `{api_key_hash: customer_id}`. `_authenticate()` returns `(agent_name, customer_id_or_none)`. Agent tokens → core OHM. Customer keys → tenant instance.

3. **current_store property**: `OhmHandler.current_store` checks `self._customer_id`, routes to `tenant_manager.get_store()` or `self.store`. ~50-80 call sites in `server.py`.

4. **Domain Templates** (`ohm/graph/templates/*.json`): JSON files replace `@classmethod` schema configs. `SchemaConfig.from_json_file()` loads at provision time.

5. **Lazy Migration**: `TenantManager.get_store()` checks `meta.json` `schema_version`, applies pending migrations automatically.

6. **Feature Flag**: `ENABLE_MULTI_TENANCY=1` env var. When unset, ohmd behaves exactly as before — single tenant, no TenantManager, no customer tokens.

### Data Model

Per-tenant filesystem layout:
```
/var/lib/ohm/tenants/{customer_id}/
├── ohm.duckdb          # Isolated database
└── meta.json           # {customer_id, domain, tier, schema_version, created_at, shared_patterns, integrations}
```

Global config additions to `ohmd.json`:
```json
{
  "customer_tokens": {"<api_key_hash>": "<customer_id>"},
  "tenants_dir": "/var/lib/ohm/tenants",
  "max_cached_tenants": 100
}
```

### Concurrency Strategy

DuckDB is single-writer. The server uses `ThreadingMixIn` (one thread per request). With N tenants and M concurrent threads:

- **Reads**: DuckDB allows concurrent reads on a single connection — GET requests share the tenant's `OhmStore.conn` without locking
- **Writes**: Per-tenant write mutex (owned by TenantManager) serializes writes to each tenant's connection. Different tenants can be written concurrently
- **Background threads**: Auto-embedding and DuckLake sync threads participate in the per-tenant lock
- **Existing gap**: ~46 `self.conn.execute()` calls in `store.py` + `server.py` bypass the existing `OhmStore._lock`. These must be brought under the lock as part of this work

### Agent Multi-Tenancy

Agents currently use `OhmStore.for_agent(agent_name)` which creates one local DB per agent. For multi-tenancy, agents use one `OhmStore` per tenant:

- `for_agent(agent_name, tenant_id=tenant_id)` → DB at `{base_dir}/{agent_name}/{tenant_id}/ohm.duckdb`
- When `tenant_id=None`, behavior is unchanged (backward compat)
- SDK `connect()` gains optional `tenant_id` parameter
- Attribution: `created_by` remains `agent_name`, tenant context from DB path

### Boundary Enforcement (ADR-003)

Boundary rules are agent-scoped and **unchanged within a tenant**. Tenant isolation is the outer boundary layer — a customer in tenant A cannot see or touch tenant B's data. This is enforced by `current_store` routing, not by `boundary.py`.

Within a tenant, `customer_api` identity:
- Can create L1/L2 edges (shared)
- Can create L3/L4 edges with `created_by='customer:{customer_id}'` (ownable, challengeable)
- Can challenge agent edges within its tenant
- ADR-003 functions unchanged (no `tenant_id` parameter needed)

## Alternatives Considered

### A: Shared DB with `tenant_id` column

Add `tenant_id` column to all tables, filter every query by tenant.

| Pro | Con |
|-----|-----|
| Efficient — one connection, one DB | Schema migration to add column to 6 tables |
| Cross-tenant queries trivial | Isolation is application-level only |
| No LRU cache needed | Bug in WHERE clause = cross-tenant data leak |
| Simpler backup | Cannot have per-tenant schema configs |

**Rejected**: Isolation is the #1 requirement. Application-level filtering is error-prone — one missing WHERE clause leaks customer data.

### B: Schema-per-tenant in same DuckDB

Each tenant gets its own schema namespace (`CREATE SCHEMA tenant_acme`) in one DuckDB file.

| Pro | Con |
|-----|-----|
| One DB file | Same single-writer bottleneck |
| Cross-tenant queries possible | DuckDB schema support is limited |
| No LRU cache needed | Cannot have per-tenant CHECKPOINT |

**Rejected**: Doesn't solve the single-writer problem. DuckDB's schema support is not mature enough for production use.

### C: Multi-process — one ohmd per tenant

Each tenant gets its own `ohmd` process on a different port.

| Pro | Con |
|-----|-----|
| Perfect isolation | Resource overhead (N processes × RAM) |
| No shared state concerns | Complex orchestration (supervisord, port allocation) |
| Independent crash domains | Hard to scale to 100+ tenants |

**Rejected**: Too heavy for SMB scale (100+ tenants per machine). Revisit if single-process ceiling is reached (documented in scaling path below).

## Consequences

### Positive

- Strong isolation — each tenant's data is a separate file, no cross-contamination possible
- Domain flexibility — each tenant can use a different SchemaConfig
- Economical — one process, one machine, LRU cache manages memory
- Backward compatible — feature flag ensures existing deployments work unchanged
- TOPO migration path — `topod` becomes `ohmd --schema topo` → domain template

### Negative

- LRU cache eviction latency — evicted tenant re-opens in ~50ms on next access
- Single-writer serialization — writes to same tenant are serialized (reads are concurrent)
- Memory ceiling — 100 open DuckDB connections consume ~1-2 GB RSS
- No cross-tenant queries — cannot aggregate across tenants in a single SQL statement
- Per-tenant WAL management — need periodic CHECKPOINT for active tenants

### Security

- `customer_id` validated against path traversal before filesystem use (OHM-c864)
- Customer API keys stored as SHA-256 hashes only
- Cross-tenant access returns 404 (not 403 — don't leak tenant existence)
- Feature flag prevents accidental multi-tenancy activation

### Scaling Path

The single-process model scales to ~100-200 tenants per instance (bounded by RAM and LRU eviction rate). Horizontal scaling path:

1. **Consistent-hash router** in front of N `ohmd` instances
2. Each instance owns a shard of tenants (tenant filesystem isolation already enables this)
3. No shared mutable state across instances except `shared_patterns/` directory
4. Router maps `customer_id → ohmd instance` (static config or consistent hash)

This is document-only for now — no implementation until the ceiling is measured (OHM-s2ao load testing).

## References

- OHM_MULTI_TENANCY_BACKLOG.md — Full specification
- ADR-003 — Agent-owned edges with challenge semantics (boundary rules)
- ADR-008 — Probability and confidence model (Bayesian cache isolation)
- ADR-012 — Per-agent local DuckDB cache (agent multi-tenancy extension)
- OHM-7jcb — DuckDB concurrent access strategy (write mutex)
- OHM-g4os — Bayesian cache key customer_id (cross-tenant inference bleed fix)
