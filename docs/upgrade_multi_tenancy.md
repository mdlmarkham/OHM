# Upgrading OHM to Multi-Tenancy

This guide covers migrating an existing single-tenant `ohmd` deployment to multi-tenant mode. Multi-tenancy is **feature-flagged** — existing deployments continue working unchanged until the flag is explicitly enabled.

## Compatibility Guarantees

| Aspect | Guarantee |
|--------|-----------|
| Agent tokens | Unchanged — agent tokens continue to authenticate to core OHM |
| API endpoints | All existing `/nodes`, `/edges`, `/stats`, etc. work identically |
| Core DB | `~/.ohm/ohm.duckdb` remains the single-tenant store when flag is off |
| Agent SDK | `connect(db_path, actor=...)` unchanged — `tenant_id` is optional |
| ohmd port | Same default port (8710) — no port changes |
| Performance | Zero overhead when multi-tenancy is off (`_customer_id` short-circuits to None) |

## Prerequisites

- OHM >= version with `--multi-tenant` support
- Existing `ohmd` deployment with agents (metis, clio, etc.) working
- `/var/lib/ohm/` directory writable by ohmd process
- Backup of existing `ohm.duckdb` (see rollback below)

## Step-by-Step Migration

### 1. Stop the daemon

```bash
sudo systemctl stop ohmd
# Verify it's stopped
systemctl status ohmd
```

### 2. Backup existing database

```bash
sudo cp /var/lib/ohm/ohm.duckdb /var/lib/ohm/ohm.duckdb.pre-migration
sudo cp /var/lib/ohm/ohm.duckdb.wal /var/lib/ohm/ohm.duckdb.wal.pre-migration 2>/dev/null || true
```

### 3. Enable multi-tenancy in config

Edit `/etc/ohm/ohmd.json` (or your config path):

```json
{
  "multi_tenant": true,
  "tenants_dir": "/var/lib/ohm/tenants",
  "max_cached_tenants": 100,
  "customer_tokens": {}
}
```

Or use the environment variable:

```bash
export OHM_MULTI_TENANT=1
```

Or pass the CLI flag:

```bash
ohm serve start --multi-tenant
```

### 4. Create the tenants directory

```bash
sudo mkdir -p /var/lib/ohm/tenants
sudo chown ohm:ohm /var/lib/ohm/tenants
```

### 5. Provision your first tenant

Each customer gets an isolated DuckDB instance. Use the provisioning API or CLI:

```bash
# Via CLI (if ohmd is running)
curl -X POST http://127.0.0.1:8710/tenants \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "acme_hvac", "domain": "home_services", "tier": "starter"}'
```

For the core OHM instance (your existing data), agent tokens continue to route to `self.store` — no tenant provisioning needed for the core.

### 6. Generate customer API keys

Customer API keys authenticate external clients to tenant-scoped data:

```bash
ohm serve start --init-customer-token acme_hvac
```

This prints the token **once** (it is never stored in plaintext). The SHA-256 hash is saved to `ohmd.json`:

```json
{
  "customer_tokens": {
    "acme_hvac": {"hash": "sha256_hex..."}
  }
}
```

Store the printed token securely — it cannot be recovered from the hash.

### 7. Restart the daemon

```bash
sudo systemctl restart ohmd
```

### 8. Verify

```bash
# Health check
curl http://127.0.0.1:8710/health

# Status should show multi_tenant: true
curl -H "Authorization: Bearer <agent-token>" http://127.0.0.1:8710/status

# Existing agent writes should still work
curl -X POST http://127.0.0.1:8710/nodes \
  -H "Authorization: Bearer <agent-token>" \
  -H "Content-Type: application/json" \
  -d '{"label": "Test node", "type": "concept"}'

# Customer-scoped access
curl -H "Authorization: Bearer <customer-api-key>" \
  http://127.0.0.1:8710/stats
```

### 9. Run reconciliation (optional)

On first startup after upgrade, scan all tenants for version drift:

```python
from ohm.tenant import TenantManager

tm = TenantManager("/var/lib/ohm/tenants")
results = tm.reconcile_tenants()
for r in results:
    if r["status"] != "ok":
        print(f"ALERT: {r['customer_id']} status={r['status']}")
```

This detects:
- **meta_behind**: meta.json schema_version is behind the actual DB (auto-corrected)
- **half_migrated**: migration crash left a `.migration_lock` file (flagged as `needs_attention`)
- **db_behind**: DB version behind meta version (flagged as `needs_attention`)

## TOPO Migration

TOPO users currently run `topod` as a separate binary. To migrate to multi-tenant `ohmd`:

1. Stop `topod`
2. Enable `--multi-tenant` on `ohmd`
3. Provision a tenant with the TOPO domain:

```bash
curl -X POST http://127.0.0.1:8710/tenants \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "topo_instance", "domain": "topo", "tier": "professional"}'
```

4. Import existing TOPO data into the tenant DB (copy the DuckDB file or use DuckLake sync)
5. Generate a customer API key for TOPO agents
6. Update TOPO agents to use the customer API key instead of direct `topod` connection

The TOPO domain template (`topo.json`) includes industrial node types (equipment, process, material, sensor) and edge types appropriate for manufacturing/industrial contexts.

## Per-Tenant Quotas

Tenants are provisioned with tier-based quotas:

| Resource | Starter | Professional | Enterprise |
|----------|---------|-------------|-----------|
| max_nodes | 10,000 | 100,000 | 1,000,000 |
| max_edges | 50,000 | 500,000 | 5,000,000 |
| max_db_size | 500 MB | 5 GB | 50 GB |
| max_requests/day | 10,000 | 100,000 | 1,000,000 |
| max_inference_timeout | 30s | 120s | 600s |

Write operations that exceed quotas return `QuotaExceededError`. Check current usage via `tenant_health()`.

## Per-Tenant Backups

Each tenant's data is an isolated DuckDB file:

```bash
# Single tenant backup
sudo cp /var/lib/ohm/tenants/acme_hvac/ohm.duckdb /backup/acme_hvac_$(date +%Y%m%d).duckdb

# Full tenants backup
sudo tar czf /backup/ohm_tenants_$(date +%Y%m%d).tar.gz /var/lib/ohm/tenants/
```

WAL files (`.duckdb.wal`) should also be backed up. The TenantManager checkpoints WAL on idle eviction and periodically (every 5 min).

## Agent Multi-Tenant Routing

Agents that serve multiple tenants use `tenant_id` to scope their local DB:

```python
# Agent serving one tenant
with ohm.connect("/var/lib/ohm/tenants/acme_hvac", actor="metis", tenant_id="acme_hvac") as g:
    g.create_node(label="HVAC pattern", node_type="pattern")

# Agent serving multiple tenants — one Graph per tenant
stores = {}
for tenant_id in ["acme_hvac", "wayne_plumbing"]:
    stores[tenant_id] = ohm.connect("/var/lib/ohm/agents", actor="metis", tenant_id=tenant_id)
```

Each tenant gets an isolated DB at `{base_dir}/{agent_name}/{tenant_id}/ohm.duckdb`. When `tenant_id` is None, the original path `{base_dir}/{agent_name}/ohm.duckdb` is used (backward compatible).

## Rollback Procedure

If multi-tenancy causes issues, disable it instantly:

### Option A: Toggle flag + restart

```bash
# Remove from config
sudo sed -i 's/"multi_tenant": true/"multi_tenant": false/' /etc/ohm/ohmd.json
sudo systemctl restart ohmd
```

Or unset the env var:

```bash
unset OHM_MULTI_TENANT
sudo systemctl restart ohmd
```

When `multi_tenant` is False:
- `OhmHandler._customer_id` short-circuits to None
- `current_store` returns `self.store` unconditionally (zero indirection)
- Customer token authentication is skipped
- TenantManager is not initialized

### Option B: Restore database from backup

```bash
sudo systemctl stop ohmd
sudo cp /var/lib/ohm/ohm.duckdb.pre-migration /var/lib/ohm/ohm.duckdb
sudo systemctl start ohmd
```

## Verification Checklist

After migration, verify each item:

- [ ] `ohmd` starts without errors in `journalctl -u ohmd`
- [ ] `/health` returns 200
- [ ] `/status` shows `multi_tenant: true`
- [ ] Existing agent tokens still authenticate (write a test node)
- [ ] Customer API key authenticates to the correct tenant
- [ ] `/stats` returns correct node/edge counts for each scope
- [ ] Tenant data is isolated (write to tenant A, verify not in tenant B)
- [ ] LRU eviction works (provision > max_cached tenants, verify oldest evicted)
- [ ] Reconciliation reports no drifted tenants
- [ ] Quota enforcement works (provision with low limit, verify rejection)
- [ ] WAL checkpoint fires periodically (check `tenant_health().last_checkpoint_at`)
- [ ] Rollback: disable flag, restart, verify single-tenant behavior restored
