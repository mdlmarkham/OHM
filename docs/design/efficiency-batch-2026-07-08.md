# OHM Efficiency Batch — Design Document

**Date:** 2026-07-08  
**Scope:** Performance hot-path optimization in OHM daemon and inference library.  
**Status:** Design / not yet implemented.  

This document proposes four targeted performance fixes identified by code inspection of `/root/olympus/OHM`. None of these changes modify the public HTTP API or the persistence schema; they are purely internal caching and batching improvements.

---

## 1. Multi-tenant `meta.json` + template cache

### Current hot path
- File: `src/ohm/tenant.py`
- Method: `TenantManager.get_store(customer_id)` (lines 224–282)
- Method: `TenantManager._apply_lazy_migrations` (lines 765–826)
- Method: `TenantManager._propagate_template` (lines 857–900)

### What was inspected
`get_store` does at least three uncached reads from the filesystem on every **cache miss** (and the `meta.json` file on every call even when cached):

1. `_read_meta(customer_id)` reads `meta.json` from disk to know the tenant’s domain/schema version (line 251).
2. `_load_schema(domain)` re-reads the domain template JSON from disk (line 252).
3. `_apply_lazy_migrations` calls `_read_meta` again to check the schema version and possibly update it (line 251 / line 782).
4. `_propagate_template` calls `_read_meta` again to read `template_version` (line 873) and `_load_schema(domain)` again (line 876).

The pattern is repeated in `get_store` regardless of whether the tenant entry is already in the LRU cache, because `_apply_lazy_migrations` and `_propagate_template` run after cache lookup. `_load_schema` currently has **no cache**, so it parses JSON from disk every time a tenant is accessed.

### Proposed implementation sketch

#### A. Add an in-memory `meta.json` cache inside `TenantManager`
- Cache key: `customer_id`
- Cache value: parsed `meta.json` dict + `mtime`/`inode` of the file
- TTL: none, invalidated explicitly by writes
- Lock: reuse `_cache_lock` or add a lightweight `_meta_cache_lock`

`TenantManager.__init__` adds:

```python
self._meta_cache: dict[str, tuple[dict, float]] = {}  # customer_id -> (meta, mtime)
self._meta_cache_lock = threading.Lock()
```

New helper `_read_meta_cached(customer_id)`:

```python
def _read_meta_cached(self, customer_id: str) -> dict:
    meta_path = self._tenant_dir(customer_id) / _META_FILENAME
    if not meta_path.exists():
        raise TenantNotFoundError(...)
    mtime = meta_path.stat().st_mtime_ns
    with self._meta_cache_lock:
        cached_meta, cached_mtime = self._meta_cache.get(customer_id, (None, None))
        if cached_meta is not None and cached_mtime == mtime:
            return cached_meta
    raw = json.loads(meta_path.read_text())
    with self._meta_cache_lock:
        self._meta_cache[customer_id] = (raw, mtime)
    return raw
```

#### B. Add an in-memory schema/template cache
- Cache key: domain + resolved file path + file mtime
- Cache value: `SchemaConfig`
- Thread-safe under `_schema_lock` (already exists)

```python
self._schema_cache: dict[str, tuple[SchemaConfig, float]] = {}
```

In `_load_schema(domain)`:

```python
try_paths = [...]
for path in try_paths:
    if path.exists():
        mtime = path.stat().st_mtime_ns
        key = f"{domain}:{path}"
        with self._schema_lock:
            cached_schema, cached_mtime = self._schema_cache.get(key, (None, None))
            if cached_schema is not None and cached_mtime == mtime:
                return cached_schema
        schema = SchemaConfig.from_json_file(str(path))
        with self._schema_lock:
            self._schema_cache[key] = (schema, mtime)
        return schema
# fallback unchanged
return SchemaConfig()
```

#### C. Use `_read_meta_cached` everywhere `_read_meta` is used
Update `_apply_lazy_migrations`, `_propagate_template`, `_get_cached_quota`, `get_meta`, integration endpoints, backup/restore, and `get_store`.

#### D. Thread-safety and invalidation
- `get_store` cache hit path still needs `_apply_lazy_migrations` / `_propagate_template`. With cached `meta` and schema, these become CPU-only checks that run while holding the entry lock.
- When `_write_meta` is called, pop the corresponding `_meta_cache` entry.
- When `_propagate_template` updates `template_version`, it has already mutated the `meta.json`; `_write_meta` will invalidate the meta cache.
- Schema files are expected to be immutable at runtime; no invalidation is required except process restart. If dynamic template reload becomes needed, add a small TTL or explicit `reload_templates()` admin endpoint.

### Caching strategy & invalidation
| Cache | Key | Invalidation trigger |
|-------|-----|----------------------|
| `_meta_cache` | `customer_id` | `_write_meta`, `deprovision`, manual admin edits to `meta.json` |
| `_schema_cache` | `domain:file_path:mtime` | Process restart; mtime change (auto) |

### Backward compatibility
- No schema or API changes.
- All existing tests continue to pass because `_read_meta` behavior is unchanged; only the I/O path is cached.
- Cache is in-memory only; no persisted state.

### Tests
- Run existing: `tests/test_tenant.py`, `tests/test_multi_tenancy_perf.py`, `tests/test_tenant_api.py`, `tests/test_tenant_isolation.py`, `tests/test_integration_multitenant.py`.
- Add new:
  - `test_meta_json_cache_hit` — assert second `get_store` does not re-read `meta.json` from disk (monkeypatch `Path.read_text` to raise on second call or use `tmp_path` mtime).
  - `test_schema_cache_same_object` — assert `_load_schema("ohm")` returns the same `SchemaConfig` instance on repeated calls.
  - `test_meta_cache_invalidation_on_write` — modify quota via `set_quotas` and assert cache is cleared.
  - `test_template_propagation_still_works_with_cache` — provision tenant at old template, update template file, ensure `_propagate_template` applies changes.

---

## 2. Neighborhood `effective_layer` N+1 → single `WHERE id IN (...)` query

### Current hot path
- File: `src/ohm/server/handlers/graph.py`
- Method: `OhmHandler._get_neighborhood` (lines 695–775)
- Called function: `src/ohm/graph/constraints.py:effective_layer` (lines 384–460)

### What was inspected
After collecting neighborhood nodes, the handler loops over every node row and calls `effective_layer(self.current_store.conn, n["id"])` (around line 762):

```python
for n in node_rows:
    eff_layer, _cs = effective_layer(self.current_store.conn, n["id"])
    n["effective_layer"] = eff_layer
```

`effective_layer` itself issues multiple per-node SQL queries:
1. `SELECT type FROM ohm_nodes WHERE id = ? ...`
2. `SELECT COALESCE(MAX(...)) FROM ohm_edges WHERE from_node = ? OR to_node = ?`
3. For L3/L4 nodes: `chain_validity`, `count_sources`, `count_verified_outcomes`, `count_open_challenges`, and possibly `count_L3_supporting_nodes`.

Each of those helper functions runs one SQL query. In a neighborhood with 300 nodes, this can be 300–1500 queries.

### Proposed implementation sketch

#### A. Add `effective_layers(conn, node_ids)` batch function in `src/ohm/graph/constraints.py`
The existing `batch_constraint_report` already demonstrates the exact aggregate SQL pattern. We can extract the per-node effective-layer computation into a reusable function:

```python
def effective_layers(conn, node_ids: list[str]) -> dict[str, tuple[str, dict]]:
    if not node_ids:
        return {}

    # 1. Batch node types
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"SELECT id, type FROM ohm_nodes WHERE id IN ({placeholders}) AND deleted_at IS NULL",
        node_ids,
    ).fetchall()
    node_types = {r[0]: r[1] for r in rows}

    # 2. Batch max incident edge layer
    rows = conn.execute(f"""
        SELECT n.id,
               COALESCE(MAX(CASE e.layer
                   WHEN 'L0' THEN 0 WHEN 'L1' THEN 1
                   WHEN 'L2' THEN 2 WHEN 'L3' THEN 3
                   WHEN 'L4' THEN 4 ELSE 0 END), 0) AS max_level
        FROM ohm_nodes n
        LEFT JOIN ohm_edges e ON (e.from_node = n.id OR e.to_node = n.id)
            AND e.deleted_at IS NULL
        WHERE n.id IN ({placeholders})
          AND n.deleted_at IS NULL
        GROUP BY n.id
    """, node_ids).fetchall()
    node_max_levels = {r[0]: r[1] for r in rows}

    # 3. Batch sources, verified outcomes, open challenges
    #    (mirror the SQL from count_sources, count_verified_outcomes, count_open_challenges)
    # ... aggregate queries ...

    # 4. Compute effective layer using cached metrics
    result = {}
    level_map = {0: "L0", 1: "L1", 2: "L2", 3: "L3", 4: "L4"}
    for nid in node_ids:
        node_type = node_types.get(nid)
        if node_type == "fragment":
            result[nid] = ("L0", {})
            continue
        if node_type == "source":
            result[nid] = ("L1", {})
            continue
        original_layer = level_map.get(node_max_levels.get(nid, 0), "L1")
        if original_layer in ("L0", "L1", "L2"):
            result[nid] = (original_layer, {})
            continue
        # L3 / L4 require batched metric lookups
        sources = batched_sources.get(nid, 0)
        outcomes = batched_verified.get(nid, 0)
        challenges = batched_challenges.get(nid, 0)
        cv = batched_chain_validity.get(nid, 0.0)
        if original_layer == "L3":
            if cv >= 0.3 and sources >= 2 and outcomes >= 1 and challenges == 0:
                eff = "L3"
            elif cv >= 0.1 and sources >= 1:
                eff = "L2"
            else:
                eff = "L1"
        else:  # L4
            support = batched_support.get(nid, 0)
            if cv >= 0.5 and support >= 3 and outcomes >= 2 and challenges == 0:
                eff = "L4"
            elif cv >= 0.3 and support >= 2 and outcomes >= 1:
                eff = "L3"
            elif cv >= 0.1:
                eff = "L2"
            else:
                eff = "L1"
        result[nid] = (eff, {})
    return result
```

For `chain_validity` the per-node definition is `min(observation.value)`. This can be batched as:

```sql
SELECT node_id, MIN(COALESCE(value, 0.5)) AS cv
FROM ohm_observations
WHERE node_id IN (...) AND deleted_at IS NULL
GROUP BY node_id
```

#### B. Replace the loop in `_get_neighborhood`

```python
if len(node_rows) <= LARGE_NEIGHBORHOOD_THRESHOLD:
    node_ids = [n["id"] for n in node_rows]
    eff_layers = effective_layers(self.current_store.conn, node_ids)
    for n in node_rows:
        n["effective_layer"] = eff_layers.get(n["id"], ("unknown", {}))[0]
else:
    response["warning"] = ...
    response["truncated"] = True
```

#### C. Keep `effective_layer` single-node for other callers
Do not delete the old function; it is used by `/constraint-report` and other paths. Add `effective_layers` as an additive batch API.

### Caching strategy & invalidation
- This is a query batching change, not a persistent cache.
- It is inherently invalidated on every request because it reads the current DB state.
- No new invalidation rules needed.

### Backward compatibility
- `effective_layer(conn, node_id)` signature unchanged.
- `_get_neighborhood` response format unchanged.

### Tests
- Run existing: `tests/test_ohm.py`, `tests/test_server.py`, `tests/test_graph_reader.py`, any tests that call `/neighborhood`.
- Add new:
  - `test_effective_layers_batch_matches_single_node` — compare `effective_layers(conn, ids)` output against individual `effective_layer` calls for the same set of nodes.
  - `test_neighborhood_single_query_count` — use `pytest` monkeypatch or query-logging assertion to ensure the neighborhood endpoint performs O(1) effective-layer queries regardless of node count.

---

## 3. `/ask` observation lookup N+1 → batched

### Current hot path
- File: `src/ohm/server/handlers/graph.py`
- Method: `OhmHandler._post_ask_synthesis` (lines 3725–4085)
- Specific loop: lines 3915–3928

### What was inspected
During the synthesis pipeline, for each candidate `target_id` (up to 3 nodes), the handler runs:

```python
for nid in target_ids:
    obs_rows = self.current_store.execute(
        "SELECT value FROM ohm_observations WHERE node_id = ? AND type = 'probability' ...",
        [nid],
    )
    if obs_rows:
        ...
        evidence[nid] = 1 if val >= 0.5 else 0
```

This is a small N (≤3) but it runs on every `/ask` request with `include_inference=true`. The same per-node pattern appears elsewhere and should be centralized.

### Proposed implementation sketch

#### A. Add `DuckDBGraphReader.get_observations_for_nodes(node_ids, filters)` batch helper
The `GraphReader` protocol already has `get_observations_counts` for counts. Add a batched observation fetch to `src/ohm/framework/graph_reader.py`:

```python
def get_observations_for_nodes(
    self,
    node_ids: list[str],
    *,
    obs_type: str | None = None,
    scale: str | None = None,
    limit_per_node: int | None = None,
) -> dict[str, list[ObservationRecord]]:
    if not node_ids:
        return {}
    placeholders = ",".join(["?"] * len(node_ids))
    conditions = ["node_id IN ({placeholders})", "deleted_at IS NULL"]
    params: list[Any] = list(node_ids)
    if obs_type is not None:
        conditions.append("type = ?")
        params.append(obs_type)
    if scale is not None:
        conditions.append("scale = ?")
        params.append(scale)
    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, node_id, edge_id, type, value, source, created_by, scale, created_at,
               ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY created_at DESC) AS rn
        FROM ohm_observations
        WHERE {where}
    """
    if limit_per_node is not None:
        sql = f"SELECT * FROM ({sql}) ranked WHERE rn <= ?"
        params.append(limit_per_node)
    rows = self._conn.execute(sql, params).fetchall()
    result: dict[str, list[ObservationRecord]] = {nid: [] for nid in node_ids}
    for r in rows:
        rec = ObservationRecord(...)
        result[r[1]].append(rec)
    return result
```

Also add the method to the `MockGraphReader` for test parity.

#### B. Replace the `/ask` loop

```python
from ohm.graph_reader import DuckDBGraphReader

reader = DuckDBGraphReader(self.current_store.conn)
obs_by_node = reader.get_observations_for_nodes(
    target_ids,
    obs_type="probability",
    limit_per_node=1,
)
for nid in target_ids:
    obs = obs_by_node.get(nid, [])
    if obs:
        try:
            val = float(obs[0].value)
            if 0.0 <= val <= 1.0:
                evidence[nid] = 1 if val >= 0.5 else 0
        except (ValueError, TypeError):
            pass
```

#### C. Optional: extend to the root-prior loop in `build_bayesian_network`
At `src/ohm/inference/bayesian.py` line 651, root-prior computation calls `reader.get_observations(node_id)` inside a `for node_id in node_ids` loop. While not part of `/ask`, this is another N+1 that can use the same batch helper. Update after the `/ask` fix or in the same PR.

### Caching strategy & invalidation
- Batching only; no cache state.
- Result always reflects current observations.

### Backward compatibility
- `get_observations(node_id)` remains in `GraphReader` protocol.
- New `get_observations_for_nodes` has a default implementation that delegates to `get_observations` for backward compatibility, or we add it to all implementations.

### Tests
- Run existing: `tests/test_ask_endpoint.py`, `tests/test_ask_routing.py`, `tests/test_bayesian.py`, `tests/test_graph_reader.py`.
- Add new:
  - `test_graph_reader_batch_observations` — `MockGraphReader` and `DuckDBGraphReader` return the same records for single and batch calls.
  - `test_ask_observation_lookup_is_batched` — monkeypatch `execute` to count queries; assert only one observation query regardless of number of target nodes.

---

## 4. Markov inference cache (similar to Bayesian `_bayesian_network_cache`)

### Current hot path
- File: `src/ohm/inference/markov.py`
- Functions: `markov_absorbing_risk` (line 319) and `markov_expected_steps` (line 599)
- Called via: `src/ohm/server/handlers/markov.py` GET `/markov/absorbing` and `/markov/expected_steps`

### What was inspected
Both Markov functions rebuild the transition matrix from scratch on every call:

```python
nodes, matrix, transient, absorbing, sccs, meta_members = _build_transition_matrix(
    reader,
    edge_types=edge_types,
    state_nodes=state_nodes,
    semantic_roles=semantic_roles,
    collapse_sccs=False,
)
```

The matrix depends only on edges, edge types, and optional `state_nodes`. Rebuilding the SCC graph and normalizing transition probabilities is O(E + V) and recomputes the same result for repeated requests. The Bayesian module already solves the same problem with `_bayesian_network_cache` in `src/ohm/inference/bayesian.py` (lines 73–106).

### Proposed implementation sketch

#### A. Add module-level LRU cache in `src/ohm/inference/markov.py`

```python
_MAX_MARKOV_CACHE_SIZE = 50

class _LRUMarkovCache(dict):
    def __init__(self, maxsize: int = _MAX_MARKOV_CACHE_SIZE):
        super().__init__()
        self._maxsize = maxsize
        self._key_order: list[tuple] = []

    def __setitem__(self, key, value):
        if key not in self:
            self._key_order.append(key)
        super().__setitem__(key, value)
        while len(self._key_order) > self._maxsize:
            oldest = self._key_order.pop(0)
            if oldest in self:
                super().__delitem__(oldest)

    def clear(self):
        super().clear()
        self._key_order.clear()

_markov_matrix_cache: _LRUMarkovCache = _LRUMarkovCache()
```

#### B. Cache key and value
Cache key:

```python
key = (
    reader.get_graph_generation(),
    tuple(sorted(edge_types or ("CAUSES", "TRANSITIONS_TO"))),
    tuple(sorted(state_nodes)) if state_nodes else None,
    bool(semantic_roles),
)
```

Cache value:

```python
{
    "nodes": nodes,
    "matrix": matrix,
    "transient": transient,
    "absorbing": absorbing,
    "sccs": sccs,
    "meta_members": meta_members,
}
```

#### C. Use cache in both Markov functions
In `markov_absorbing_risk` and `markov_expected_steps`, replace the direct `_build_transition_matrix(..., collapse_sccs=False)` call with:

```python
cache_key = (
    reader.get_graph_generation(),
    tuple(sorted(edge_types or ("CAUSES", "TRANSITIONS_TO"))),
    tuple(sorted(state_nodes)) if state_nodes else None,
    id(semantic_roles) if semantic_roles else None,
)
cached = _markov_matrix_cache.get(cache_key)
if cached is None:
    nodes, matrix, transient, absorbing, sccs, meta_members = _build_transition_matrix(
        reader,
        edge_types=edge_types,
        state_nodes=state_nodes,
        semantic_roles=semantic_roles,
        collapse_sccs=False,
    )
    _markov_matrix_cache[cache_key] = {
        "nodes": nodes,
        "matrix": matrix,
        "transient": transient,
        "absorbing": absorbing,
        "sccs": sccs,
        "meta_members": meta_members,
    }
else:
    nodes = cached["nodes"]
    matrix = cached["matrix"]
    transient = cached["transient"]
    absorbing = cached["absorbing"]
    sccs = cached["sccs"]
    meta_members = cached["meta_members"]
```

#### D. Handle collapse_sccs fallback
The existing functions sometimes fall back to `_build_transition_matrix(..., collapse_sccs=True)` when the matrix is singular or there are no absorbing states. The collapsed version is not cached; it remains on-demand. If empirical use shows it is also hot, extend the cache with an additional flag in the key.

#### E. Clear cache on import test runs
Add `_markov_matrix_cache.clear()` in a module-level helper or expose `clear_markov_cache()` for tests.

### Caching strategy & invalidation
- Cache key includes `reader.get_graph_generation()`, which is incremented by `OhmStore._increment_graph_generation()` on every edge mutation (lines 2266–2277 in `src/ohm/graph/store.py`).
- This is the same invalidation strategy used by `_bayesian_network_cache` and is already wired into the mutation path.
- Cache is process-local, module-level, bounded at 50 entries.

### Backward compatibility
- No API changes.
- `MockGraphReader` must implement `get_graph_generation()`; it already does (line 435–438 in `src/ohm/framework/graph_reader.py`).
- Results are identical; only the second call is faster.

### Tests
- Run existing: `tests/test_markov.py`, `tests/test_server.py` (markov endpoints), `tests/test_benchmarks.py`.
- Add new:
  - `test_markov_cache_reuses_matrix` — call `markov_absorbing_risk` twice; monkeypatch `_build_transition_matrix` and assert it is called once.
  - `test_markov_cache_invalidates_on_generation_change` — mutate an edge (or manually set graph_generation), assert `_build_transition_matrix` is called again.
  - `test_markov_cache_bounded` — run 60 distinct queries and assert cache size ≤ 50.
  - `test_markov_expected_steps_uses_cache` — same pattern for expected steps.

---

## Cross-cutting concerns

### Thread safety
- All proposed caches are process-local, module-level, or `TenantManager`-level. They use small locks (`_meta_cache_lock`, `_schema_lock`, `_LRUCache` internal order list). The `_bayesian_network_cache` is already accessed concurrently without locks because dict read/write in CPython is safe for simple operations; the `_LRUCache` subclass writes `_key_order` and must be protected. Reuse the `_LRUCache` pattern from Bayesian and add the same locking if not already present.

### Memory bounds
- Tenant meta cache: one dict per tenant; negligible.
- Schema cache: one entry per loaded domain; negligible.
- Markov cache: bounded at 50 matrices. Each matrix is `V × V` doubles. For V=500, ~2 MB; 50 entries ≈ 100 MB worst case. Acceptable; tune `_MAX_MARKOV_CACHE_SIZE` if needed.

### Reversibility
- All four changes are additive. They can be reverted by removing the cache/batch helpers and restoring the original loops. No migrations or schema changes are required.

### Observability
- Consider emitting cache hit/miss counters via the existing `/metrics` or `/perf` endpoints. This can be added later; the design leaves hooks for instrumentation.

---

## Test plan

### Existing tests to run
```bash
cd /root/olympus/OHM
pytest tests/test_tenant.py tests/test_tenant_api.py tests/test_tenant_isolation.py \
       tests/test_multi_tenancy_perf.py tests/test_integration_multitenant.py \
       tests/test_ohm.py tests/test_server.py tests/test_ask_endpoint.py \
       tests/test_ask_routing.py tests/test_bayesian.py tests/test_markov.py \
       tests/test_graph_reader.py tests/test_constraints.py
```

### New tests to add
1. `tests/test_tenant_meta_cache.py` — meta + schema cache.
2. `tests/test_effective_layers_batch.py` — batch effective layer computation.
3. `tests/test_ask_batch_observations.py` — batched `/ask` observation lookup.
4. `tests/test_markov_cache.py` — Markov matrix LRU cache.

### Performance validation
- Add micro-benchmarks under `tests/test_benchmarks.py` or a new `tests/test_perf_efficiency_batch.py`:
  - `test_get_store_meta_cache_hit` — assert <1 ms after warm-up.
  - `test_neighborhood_effective_layer_300_nodes` — assert <50 ms for 300 nodes.
  - `test_ask_inference_obs_query_count` — assert exactly 1 observation query.
  - `test_markov_second_call_matrix_cache` — assert ≥10× speedup on second call.

---

## Beads issues to create/update

The current backlog (per `BEADS.md`) does not have a dedicated performance epic. Suggested issues:

| Beads ID | Title | Priority | Depends on | Notes |
|----------|-------|----------|------------|-------|
| `OHM-ef01` | Tenant meta.json + template cache | P1 | — | This batch item 1 |
| `OHM-ef02` | Batch effective_layer for `/neighborhood` | P1 | — | This batch item 2 |
| `OHM-ef03` | Batch observation lookup in `/ask` | P1 | `OHM-ef02` (same GraphReader batch helper pattern) | This batch item 3 |
| `OHM-ef04` | Markov matrix LRU cache | P1 | — | This batch item 4 |
| `OHM-ef00` | Efficiency batch 2026-07-08 tracking | P1 | `OHM-ef01`, `OHM-ef02`, `OHM-ef03`, `OHM-ef04` | Parent issue |

Parent issue `OHM-ef00` acceptance criteria:
- All four fixes implemented and tested.
- `pytest tests/test_*` for the listed test files passes.
- No regression in `/neighborhood`, `/ask`, or Markov response shapes.
- Benchmarks show measurable improvement (≥50% latency reduction or query-count reduction for the targeted paths).

### Existing issues to update
- `OHM-dy9` (Performance benchmarks) — reference the new benchmark tests.
- `OHM-tss4.2` (TenantManager tests) — add cache coverage.

---

## Suggested commit message

```text
perf(OHM-ef00): efficiency batch — tenant meta cache, neighborhood batch, /ask batch obs, Markov cache

Four internal performance improvements with no API changes:

1. Cache tenant meta.json and domain schema templates in TenantManager,
   avoiding 3× uncached disk reads per get_store() miss.
2. Add effective_layers() batch helper and use it in /neighborhood,
   replacing N per-node effective_layer() SQL calls with O(1) aggregate
   queries.
3. Add GraphReader.get_observations_for_nodes() and use it in /ask
   synthesis, batching per-target observation lookups.
4. Add module-level LRU cache to Markov matrix construction, keyed by
   graph_generation, mirroring the existing Bayesian network cache.

Adds tests for cache invalidation, batch correctness, and query-count
bounds. All existing tests pass.
```

---

## FastMCP gateway impact

The efficiency work directly improves the remote-agent experience through `ohm-gateway`:

- **Tenant meta/template cache:** Reduces per-call overhead for every remote agent session. For Lambda/Streamable HTTP this means lower cold-start latency and fewer 29s timeouts.
- **Neighborhood + /ask batching:** Makes `ohm_neighborhood` and `ohm_ask` cheaper, reducing the need to use FastMCP Background Tasks for moderately large graphs. Long-running queries can still use Background Tasks; this just raises the threshold.
- **Markov cache:** Lowers CPU and memory spikes on inference endpoints, making gateway routing more predictable and cost-efficient.
- **No protocol changes:** All improvements are behind existing HTTP endpoints, so the FastMCP gateway can consume them without tool-schema changes.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Meta cache returns stale quota/domain after manual file edit | Low | Medium | Use mtime check; invalidation in `_write_meta` covers all code paths. |
| `effective_layers` batch logic diverges from `effective_layer` | Medium | High | Add a parity test that compares batch vs single-node for random node sets. |
| Markov cache retains matrix after edge deletion that flips transient/absorbing status | Low | High | Cache key includes `graph_generation`; edge mutations invalidate. |
| Schema file mtime collision on fast edits | Low | Low | mtime in nanoseconds plus file path; if collision occurs, process restart clears cache. |
| `GraphReader` protocol change breaks `MockGraphReader` callers | Low | Medium | Add default method or update all `MockGraphReader` instances; include in test plan. |

## Reversibility notes
- All changes are file-only; no database migrations.
- Each fix can be reverted independently.
- The existing single-node `effective_layer()` and single-node `get_observations()` functions remain in place, so callers that cannot be migrated immediately continue to work.

---

*Prepared by code inspection of `/root/olympus/OHM` on 2026-07-08.*
