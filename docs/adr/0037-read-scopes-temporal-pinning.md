# ADR-037: Per-Agent Read Scopes and Temporal Pinning

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-ybyb (this work), OHM-xgm (DuckLake time-travel, future), ADR-003 (write-side boundary enforcement), ADR-028 (source_tier), ADR-015 (multi-tenancy)

## Context

OHM's boundary enforcement (ADR-003) governs writes — only the owning agent can update L3/L4 edges, and only L3/L4 edges can be challenged. There is no corresponding read-side enforcement. Every agent with a valid token can read every node and edge in the graph, regardless of layer, source quality, or provenance.

Three problems emerge from this asymmetry:

1. **Information leakage across trust boundaries.** A `raw`-tier claim from an unverified source is visible to agents that should only consume `official`-tier or higher content. In multi-tenant deployments (ADR-015), a customer-scoped agent can read another customer's L3 edges if they share the same DuckDB file.

2. **Soft-deleted items leak into historical snapshots.** `query_snapshot()` reconstructs graph state at a timestamp using `created_at <= timestamp` but did not filter `deleted_at`. A node soft-deleted at T2 would still appear in a snapshot for T3 because the `deleted_at` column was ignored. This violates the soft-delete contract: deleted items are invisible to all queries, including time-travel.

3. **No temporal scoping for point-in-time reads.** Agents querying the graph today see the full history of all edges, including ones created after the decision they are evaluating. An agent assessing "what did we know on June 1?" has no way to pin reads to that point in time without full DuckLake time-travel (OHM-xgm, deferred).

## Decision

### 1. Per-agent read scopes

Add a `read_scope` JSON column to `ohm_agent_config`. When set, the agent's reads are restricted to the dimensions specified. When NULL (default), the agent has full access — backward compatible.

**Scope dimensions** (`VALID_READ_SCOPE_DIMENSIONS` in `src/ohm/graph/schema.py:339`):

| Dimension | Filters on | Example |
|-----------|-----------|---------|
| `layer` | Edge layer (L0–L4) | `["L1", "L2", "L3"]` — exclude L4 prospect edges |
| `source_tier` | Node/edge quality tier (ADR-028) | `["official", "verified"]` — exclude raw/unverified |
| `created_by` | Owning agent name | `["metis", "clio"]` — read only own + named agents |
| `node_id` | Specific node IDs (whitelist) | `["hormuz_and_gate_abc123"]` — narrow focus |

**Scope JSON shape:**

```json
{
  "layer": ["L1", "L2", "L3"],
  "source_tier": ["official", "verified"],
  "created_by": ["metis", "clio"],
  "node_id": ["hormuz_and_gate_abc123"]
}
```

Each key is optional. A key present with an empty list `[]` means "nothing in this dimension" (deny-all for that axis). A key absent means "no restriction on this dimension." NULL `read_scope` means no restriction on any dimension.

**Enforcement** — `enforce_read_scope()` in `src/ohm/server/boundary.py:188`:

```python
def enforce_read_scope(
    conn, agent_name, *,
    layer=None, source_tier=None, node_id=None, created_by=None,
) -> None:
    scope = get_agent_read_scope(conn, agent_name)
    if scope is None:
        return  # full access
    # Check each provided dimension against scope allow-lists
    # Raise PermissionDeniedError if excluded
```

This is the read-side parallel to ADR-003's `enforce_write_boundary()`. Both live in `boundary.py`. Read-scope checks are called before returning query results in the server handler layer, not inside CTE queries (which remain scope-agnostic for substrate methods like `query_stats`).

**Admin API** — `set_agent_read_scope(conn, agent_name, scope)` in `src/ohm/server/boundary.py:235` validates the scope dict via `validate_read_scope()` (checks keys against `VALID_READ_SCOPE_DIMENSIONS`, values must be `list[str]`), then upserts into `ohm_agent_config.read_scope`.

**Migration** — Schema version `0.33.0` adds `ALTER TABLE ohm_agent_config ADD COLUMN IF NOT EXISTS read_scope JSON` (`src/ohm/graph/schema.py:1506`).

### 2. Soft-delete fix in query_snapshot

`query_snapshot()` now filters `deleted_at` in all three entity queries (nodes, edges, observations):

```sql
-- Before (bug): soft-deleted items included
WHERE created_at <= ?

-- After (fix): soft-deleted items excluded
WHERE created_at <= ? AND (deleted_at IS NULL OR deleted_at > ?)
```

This applies the same soft-delete contract used by all other queries (`deleted_at IS NULL` for current reads, `deleted_at > timestamp` for point-in-time reads). The fix is in `src/ohm/graph/queries/__init__.py:2213–2264`.

### 3. Temporal pinning

Add an optional `as_of` timestamp parameter to read-path queries. When provided, results are filtered to `created_at <= as_of` in addition to the soft-delete filter. This gives agents point-in-time read capability without requiring DuckLake's full time-travel infrastructure (OHM-xgm).

**Scope:** Temporal pinning applies to `query_neighborhood`, `query_snapshot`, and the SDK's `graph.read()` path. It does NOT apply to write operations or substrate methods (`query_stats`, `query_graph_health`).

**Limitation:** This is a `created_at` filter, not a true MVCC snapshot. Edges updated after `as_of` but created before will still appear with their current (not historical) values. Full time-travel with versioned snapshots requires DuckLake (OHM-xgm). The `created_at` pin is a pragmatic approximation that covers the common case: "what nodes and edges existed at time T?"

## Mapping to existing concepts

| Existing concept | Maps to |
|------------------|---------|
| ADR-003 write boundary (`enforce_write_boundary`) | Read-side parallel (`enforce_read_scope`) |
| ADR-028 `source_tier` | Scope dimension `source_tier` — agents can be restricted to high-tier content only |
| ADR-015 multi-tenancy | `created_by` scope dimension — customer agents read only their own edges |
| `deleted_at IS NULL` (all current queries) | `query_snapshot` now respects this contract too |
| DuckLake time-travel (OHM-xgm) | Temporal pinning is the `created_at`-only approximation; full MVCC deferred |

## Consequences

**Positive:**
- Agents can be restricted to reading only within their trust boundary — a `raw`-only agent cannot see `verified` content, and vice versa
- Multi-tenant deployments can scope customer agents to `created_by: ["customer:{id}"]` without separate DuckDB files
- `query_snapshot` no longer leaks soft-deleted items — the soft-delete contract is now consistent across all query paths
- Temporal pinning enables point-in-time reasoning without waiting for DuckLake
- NULL scope = full access preserves backward compatibility — no migration burden on existing agents

**Negative:**
- Read-scope enforcement is at the handler layer, not in CTE queries — a direct-connection caller bypassing the server could read without scope checks. This mirrors the existing write-side pattern (ADR-003 enforcement is also at the boundary layer, not in SQL)
- Temporal pinning is `created_at`-only, not MVCC — updated values after `as_of` are visible. Agents needing true historical state must wait for DuckLake (OHM-xgm)
- `node_id` scope is a whitelist, not a pattern — agents cannot express "all nodes matching prefix X." This is intentional: pattern-based scopes would require runtime evaluation per row, degrading query performance
- Scope dimensions are additive (AND), not compositional (OR) — an agent scoped to `layer: ["L3"]` AND `source_tier: ["verified"]` cannot read L3/unverified OR L4/verified. This is the correct default for security; OR-scopes can be added later if needed

## Alternatives considered

- **Row-level security in DuckDB** — rejected. DuckDB has no RLS policies (unlike PostgreSQL). Application-layer enforcement is the only option and matches the existing ADR-003 pattern.
- **Separate read-scope table instead of JSON column** — rejected. The scope is small (≤4 keys, each a short list), queried once per request, and rarely updated. A separate table would add JOIN overhead for no normalization benefit. JSON column on `ohm_agent_config` keeps the scope co-located with the agent's other config.
- **Temporal pinning via snapshot tables** — rejected. `ohm_snapshots` already exists but requires explicit creation. Temporal pinning via `created_at` filter is zero-setup and covers the common case. Full snapshot-based time-travel is DuckLake's job (OHM-xgm).
