# Multi-Tenant Operations Guide

Day-2 operations for `ohmd` running in multi-tenant mode. For initial setup and migration, see [upgrade_multi_tenancy.md](upgrade_multi_tenancy.md).

## Configuration Reference

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OHM_MULTI_TENANT` | unset (off) | Feature flag. Set to `1`, `true`, or `yes` to enable |
| `OHM_CONFIG` | `~/.ohm/ohmd.json` | Path to config file |
| `OHM_PORT` | `8710` | Override bind port |
| `OHM_HOST` | `127.0.0.1` | Override bind address |
| `OHM_DB_PATH` | `~/.ohm/ohm.duckdb` | Override core DB path |
| `OHM_NO_AUTH` | unset | Disable auth (dev only) |
| `OHM_REQUIRE_READ_AUTH` | unset | Require auth for reads |
| `OHM_QUACK` | unset | Enable Quack concurrent-writer protocol |
| `OHM_TOKEN` | none | SDK/daemon bearer token |

### ohmd.json Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `multi_tenant` | bool | `false` | Enable multi-tenancy |
| `tenants_dir` | string | `<parent of db_path>/tenants` | Tenant data directory |
| `tenant_cache_size` | int | `100` | LRU cache capacity (max open connections) |
| `templates_dir` | string | null | Custom domain schema templates directory |
| `customer_tokens` | object | `{}` | Map: `customer_id → {hash: "sha256_hex"}` |
| `tokens` | object | `{}` | Agent tokens: `agent_name → {hash, role}` |
| `roles` | object | `{}` | Agent roles: `agent_name → "read-write"|"read-only"|"admin"` |

Precedence: CLI flag > env var > config file value > built-in default.

### TenantManager Parameters

| Parameter | Default | Mapped From |
|-----------|---------|-------------|
| `tenants_dir` | required | `config.tenants_dir` |
| `templates_dir` | `None` | `config.templates_dir` |
| `max_cached` | `100` | `config.tenant_cache_size` |
| `checkpoint_interval` | `300` (5 min) | `_CHECKPOINT_INTERVAL_SECONDS` |
| `wal_size_threshold` | `104857600` (100 MB) | `_WAL_SIZE_THRESHOLD_BYTES` |

### Tuning Constants

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| `_IDLE_EVICT_SECONDS` | `600` | `tenant.py` | Idle eviction threshold |
| `_CHECKPOINT_INTERVAL_SECONDS` | `300` | `tenant.py` | Background checkpoint interval |
| `_WAL_SIZE_THRESHOLD_BYTES` | `104857600` | `tenant.py` | WAL size triggers early checkpoint |

### Quota Tiers

| Resource | starter | professional | enterprise |
|----------|---------|-------------|------------|
| `max_nodes` | 10,000 | 100,000 | 1,000,000 |
| `max_edges` | 50,000 | 500,000 | 5,000,000 |
| `max_db_size_bytes` | 500 MB | 5 GB | 50 GB |
| `max_requests_per_day` | 10,000 | 100,000 | 1,000,000 |
| `max_inference_timeout` | 30s | 120s | 600s |

## Provisioning Walkthrough

### Provision a new tenant

```bash
curl -X POST http://127.0.0.1:8710/tenant/provision \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_id": "acme_hvac",
    "domain": "home_services",
    "tier": "starter"
  }'
```

Response contains the customer API key (shown **once**):
```json
{
  "status": "provisioned",
  "customer_id": "acme_hvac",
  "api_key": "ohm-cust-acme_hvac-<random>",
  "tier": "starter",
  "domain": "home_services"
}
```

Save the `api_key` immediately — only the SHA-256 hash is stored in `ohmd.json`.

### Generate additional customer tokens

```bash
ohmd --init-customer-token acme_hvac
```

Prints a new token. Hash is appended to `customer_tokens` in `ohmd.json`.

### List tenants

```bash
curl -H "Authorization: Bearer <admin-token>" \
  http://127.0.0.1:8710/tenants
```

### Check tenant health

```bash
curl -H "Authorization: Bearer <admin-token>" \
  http://127.0.0.1:8710/tenant/acme_hvac/health
```

Response:
```json
{
  "customer_id": "acme_hvac",
  "schema_version": "0.5.0",
  "tier": "starter",
  "needs_attention": false,
  "wal_size_bytes": 4194304,
  "last_checkpoint_at": "2026-05-24T18:30:00Z",
  "cached": true,
  "refcount": 0,
  "quotas": {
    "max_nodes": 10000,
    "max_edges": 50000,
    "max_db_size_bytes": 524288000,
    "max_requests_per_day": 10000,
    "max_inference_timeout": 30
  },
  "usage": {
    "nodes": 342,
    "edges": 1205,
    "db_size_bytes": 8388608,
    "requests_today": 47
  }
}
```

### Deprovision a tenant

```bash
curl -X DELETE "http://127.0.0.1:8710/tenant/acme_hvac?confirm=true" \
  -H "Authorization: Bearer <admin-token>"
```

**This is irreversible.** The DuckDB file, WAL, meta.json, and migration locks are deleted. Customer tokens are revoked from the in-memory lookup.

### Export tenant data

```bash
curl -X POST http://127.0.0.1:8710/tenant/acme_hvac/export \
  -H "Authorization: Bearer <admin-token>"
```

Returns all nodes and edges as JSON.

## Monitoring

### Health Endpoint

`GET /tenant/{id}/health` provides per-tenant observability:

| Field | Alert Threshold | Meaning |
|-------|-----------------|---------|
| `needs_attention` | `true` | Migration failure or drift — see runbook |
| `wal_size_bytes` | > `wal_size_threshold` (100 MB) | WAL growing unchecked |
| `last_checkpoint_at` | > 10 min ago | Checkpoint thread may be stuck |
| `cached` | always `false` for hot tenants | Cache thrashing — increase `max_cached` |
| `usage.nodes` / `usage.edges` | > 80% of quota | Approaching quota limit |
| `usage.db_size_bytes` | > 80% of `max_db_size_bytes` | Approaching storage quota |
| `usage.requests_today` | > 80% of `max_requests_per_day` | Rate limit approaching |

### Recommended Alerting

```yaml
alerts:
  - name: tenant_needs_attention
    condition: health.needs_attention == true
    severity: critical

  - name: tenant_wal_oversized
    condition: health.wal_size_bytes > 80_000_000
    severity: warning

  - name: tenant_quota_near_limit
    condition: >
      health.usage.nodes > health.quotas.max_nodes * 0.8
      or health.usage.edges > health.quotas.max_edges * 0.8
    severity: warning

  - name: tenant_stale_checkpoint
    condition: health.last_checkpoint_at < now() - 15m
    severity: warning

  - name: tenant_cache_miss
    condition: health.cached == false and tenant is_hot
    severity: info
```

### Log Correlation

All tenant-scoped requests include `customer_id` in the request context. Use `journalctl` filtering:

```bash
# Watch a specific tenant's requests
journalctl -u ohmd -f | grep "customer_id=acme_hvac"

# Watch all tenant provisioning events
journalctl -u ohmd -f | grep "tenant_manager"
```

### Backup Strategy

Each tenant is an isolated DuckDB file — backup at the filesystem level:

```bash
# Single tenant (while ohmd is running — WAL may be incomplete)
sudo cp /var/lib/ohm/tenants/acme_hvac/ohm.duckdb /backup/

# Consistent backup: checkpoint first, then copy
# The checkpoint thread runs every 5 minutes, so a recent checkpoint
# means the WAL is small and copy is nearly consistent.
# For full consistency, stop ohmd first.

# Full tenants backup
sudo tar czf /backup/ohm_tenants_$(date +%Y%m%d).tar.gz /var/lib/ohm/tenants/
```

## Runbooks

### RB-1: Migration Failure Recovery

**Symptom**: `tenant_health()` returns `needs_attention: true`.

**Cause**: A schema migration crashed mid-way, leaving a `.migration_lock` file.

**Steps**:

1. Check the tenant health:
   ```bash
   curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenant/{customer_id}/health
   ```

2. Check if `.migration_lock` exists:
   ```bash
   ls -la /var/lib/ohm/tenants/{customer_id}/.migration_lock
   ```

3. **If `.migration_lock` exists and the DB is intact**: The migration didn't complete. Remove the lock and retry:
   ```bash
   rm /var/lib/ohm/tenants/{customer_id}/.migration_lock
   # Next get_store() call will re-attempt migration
   curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenant/{customer_id}/health
   ```

4. **If the DB is corrupted**: Restore from backup:
   ```bash
   sudo systemctl stop ohmd
   sudo cp /backup/{customer_id}_ohm.duckdb /var/lib/ohm/tenants/{customer_id}/ohm.duckdb
   rm /var/lib/ohm/tenants/{customer_id}/.migration_lock 2>/dev/null
   sudo systemctl start ohmd
   ```

5. **If `reconcile_tenants()` reports `db_behind`**: The DB schema version is behind `meta.json`. This indicates a partial migration. Remove the lock file and trigger a re-attempt via `get_store()`.

6. **If `reconcile_tenants()` reports `meta_behind`**: This is auto-corrected — `meta.json` is updated to match the actual DB version. No action needed.

### RB-2: LRU Cache Tuning

**Symptom**: Slow requests for frequently-accessed tenants; `/health` shows `cached: false` for hot tenants.

**Diagnosis**: The LRU cache is too small — tenants are being evicted and re-opened (~50ms latency per re-open).

**Recommendation formula**:

```
max_cached >= active_tenants + headroom
headroom = active_tenants * 0.2  (20% buffer)
```

Where `active_tenants` = number of tenants accessed in the last `_IDLE_EVICT_SECONDS` (10 min).

**Steps**:

1. Count hot tenants:
   ```bash
   curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenants | python -c "
   import json, sys
   data = json.load(sys.stdin)
   print(f'Provisioned: {data[\"count\"]}')
   "
   ```

2. Check cache utilization at peak:
   ```bash
   for tenant in $(curl -s -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenants | python -c "
   import json, sys
   print(' '.join(t['customer_id'] for t in json.load(sys.stdin)['tenants']))
   "); do
     cached=$(curl -s -H "Authorization: Bearer <admin-token>" \
       http://127.0.0.1:8710/tenant/$tenant/health | python -c "
   import json, sys
   print(json.load(sys.stdin)['cached'])
   ")
     echo "$tenant cached=$cached"
   done
   ```

3. Increase `max_cached` in `ohmd.json`:
   ```json
   {
     "tenant_cache_size": 150
   }
   ```

4. Restart ohmd:
   ```bash
   sudo systemctl restart ohmd
   ```

**Memory impact**: Each cached DuckDB connection uses ~10-20 MB RSS. 100 tenants ≈ 1-2 GB. Adjust based on available RAM.

### RB-3: WAL Growth

**Symptom**: WAL files growing beyond 100 MB; disk usage alerting.

**Cause**: Checkpoint thread not running, or write-heavy tenant exceeding checkpoint interval.

**Steps**:

1. Check WAL size:
   ```bash
   curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenant/{customer_id}/health | python -c "
   import json, sys
   h = json.load(sys.stdin)
   wal_mb = h['wal_size_bytes'] / 1048576
   print(f'WAL: {wal_mb:.1f} MB, last checkpoint: {h[\"last_checkpoint_at\"]}')
   "
   ```

2. If `last_checkpoint_at` is stale (> 10 min), the checkpoint thread may be stuck. Restart ohmd:
   ```bash
   sudo systemctl restart ohmd
   ```

3. For write-heavy tenants, reduce `checkpoint_interval`:
   ```json
   {
     "checkpoint_interval": 120
   }
   ```

   Note: `checkpoint_interval` is a TenantManager constructor parameter, not an `ohmd.json` key yet. To change it, set `OHM_CHECKPOINT_INTERVAL=120` (not yet implemented — file an issue).

4. Force a manual checkpoint via the SDK:
   ```python
   from ohm.tenant import TenantManager
   tm = TenantManager("/var/lib/ohm/tenants")
   tm._checkpoint_tenant("acme_hvac")
   ```

### RB-4: Secure Deprovision

**Symptom**: Customer offboarding; tenant data must be permanently deleted.

**Pre-deletion checks**:

1. Export data for the customer's records:
   ```bash
   curl -X POST http://127.0.0.1:8710/tenant/{customer_id}/export \
     -H "Authorization: Bearer <admin-token>" > /backup/{customer_id}_export.json
   ```

2. Verify export integrity:
   ```bash
   python -c "import json; d=json.load(open('/backup/{customer_id}_export.json')); \
     print(f'Nodes: {len(d.get(\"nodes\",[]))}, Edges: {len(d.get(\"edges\",[]))}')"
   ```

3. Deprovision:
   ```bash
   curl -X DELETE "http://127.0.0.1:8710/tenant/{customer_id}?confirm=true" \
     -H "Authorization: Bearer <admin-token>"
   ```

4. Verify deletion:
   ```bash
   ls /var/lib/ohm/tenants/{customer_id} 2>&1
   # Should show: No such file or directory
   ```

5. Revoke customer tokens from `ohmd.json` (the API does this automatically for the in-memory lookup, but persist the change):
   ```bash
   sudo systemctl restart ohmd  # Picks up the token revocation
   ```

### RB-5: Quota Exceeded

**Symptom**: Customer API writes return `QuotaExceededError` (HTTP 429).

**Diagnosis**:

1. Check current usage vs quotas:
   ```bash
   curl -H "Authorization: Bearer <admin-token>" \
     http://127.0.0.1:8710/tenant/{customer_id}/health
   ```

2. Compare `usage` vs `quotas` fields.

**Resolution**:

- **Upgrade tier**: Re-provision with a higher tier (requires deprovision + re-provision — export first).
- **Manual quota override**: Edit `meta.json` directly:
  ```bash
  sudo vi /var/lib/ohm/tenants/{customer_id}/meta.json
  # Update the "quotas" section
  ```
  Restart ohmd to pick up the change.
- **Rate limit reset**: The `max_requests_per_day` counter resets at midnight UTC. No manual reset needed.

### RB-6: Startup Reconciliation

On startup, `TenantManager.reconcile_tenants()` scans all tenant directories for version drift.

**What it detects**:

| Status | Meaning | Auto-fix? |
|--------|---------|-----------|
| `ok` | Everything normal | N/A |
| `meta_behind` | `meta.json` version behind DB | Yes — meta updated automatically |
| `half_migrated` | `.migration_lock` exists | No — flagged `needs_attention` |
| `db_behind` | DB version behind meta | No — flagged `needs_attention` |

**Run manually**:

```python
from ohm.tenant import TenantManager
tm = TenantManager("/var/lib/ohm/tenants")
results = tm.reconcile_tenants()
for r in results:
    if r["status"] != "ok":
        print(f"ALERT: {r['customer_id']} → {r['status']}")
```

## API Endpoint Reference

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/tenants` | admin | List all tenants |
| `GET` | `/tenant/{id}` | admin | Tenant metadata |
| `GET` | `/tenant/{id}/schema` | admin | Domain schema (node/edge types) |
| `GET` | `/tenant/{id}/health` | admin | Health, quotas, usage, WAL |
| `POST` | `/tenant/provision` | admin | Create tenant (body: `customer_id`, `domain`?, `tier`?) |
| `POST` | `/tenant/{id}/export` | admin | Export graph as JSON |
| `DELETE` | `/tenant/{id}?confirm=true` | admin | Irreversible deprovision |

All tenant endpoints call `_require_multi_tenant_active()` first — returns `ValidationError` if multi-tenancy is off.

## Directory Layout

```
/var/lib/ohm/
├── ohm.duckdb                    # Core OHM (agent tokens → this DB)
├── tenants/
│   ├── acme_hvac/
│   │   ├── ohm.duckdb            # Isolated tenant DB
│   │   ├── ohm.duckdb.wal        # Write-ahead log
│   │   ├── meta.json             # {customer_id, domain, tier, schema_version, quotas, ...}
│   │   └── .migration_lock      # Present only during/after crashed migration
│   ├── wayne_mfg/
│   │   ├── ohm.duckdb
│   │   └── meta.json
│   └── ...
├── agents/
│   └── metis/
│       ├── ohm.duckdb            # Agent's default local DB
│       └── acme_hvac/
│           └── ohm.duckdb        # Agent's tenant-scoped DB
└── shared_patterns/              # Cross-tenant pattern library
```

## Scaling Guidance

### Single-Instance Limits

| Metric | Ceiling | Reason |
|--------|---------|--------|
| Cached tenants | ~100-200 | RAM: ~1-2 GB for 100 open connections |
| Concurrent writes | 1 per tenant | DuckDB single-writer (serialized by per-tenant lock) |
| Total tenants on disk | No hard limit | Only cached tenants consume RAM |

### Horizontal Scaling Path

When a single instance exceeds capacity:

1. Deploy N `ohmd` instances behind a consistent-hash router
2. Each instance owns a shard of tenants (tenant filesystem isolation already supports this)
3. Router maps `customer_id → ohmd instance` (static config or consistent hash)
4. Shared mutable state: only `shared_patterns/` directory (document-only for now)

See ADR-015 "Scaling Path" for the full design.
