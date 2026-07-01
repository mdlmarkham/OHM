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

**`topod` is deprecated.** It will continue to work for 2 releases but emits a `DeprecationWarning` on startup. Migrate to multi-tenant `ohmd` using one of these paths:

### Path A: Single-tenant replacement (simplest)

Replace `topod` with `ohmd --schema topo`:

1. Stop `topod`: `systemctl stop topod`
2. Start `ohmd` with TOPO schema: `ohmd --schema topo --db /var/lib/ohm/ohm.duckdb`
3. Update your systemd unit:

```ini
# /etc/systemd/system/ohmd.service (was topod.service)
[Service]
ExecStart=/usr/local/bin/ohmd --schema topo --db /var/lib/ohm/ohm.duckdb
```

4. `systemctl daemon-reload && systemctl start ohmd`

No data migration needed — same DDL, same tables, same DB path. Only the entry point changes.

### Path B: Multi-tenant migration (recommended for new deployments)

Move TOPO into an isolated tenant within a multi-tenant `ohmd`:

1. Stop `topod`: `systemctl stop topod`
2. Enable multi-tenant `ohmd`: `ohmd --multi-tenant` (or set `OHM_MULTI_TENANT=1`)
3. Provision a TOPO tenant:

```bash
curl -X POST http://127.0.0.1:8710/tenants \
  -H "Authorization: Bearer <admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"customer_id": "topo_instance", "domain": "topo", "tier": "professional"}'
```

4. Import existing TOPO data:

```bash
# Copy the existing TOPO DuckDB into the tenant directory
cp /var/lib/ohm/ohm.duckdb /var/lib/ohm/tenants/topo_instance/ohm.duckdb
# Remove WAL if present (will be recreated on open)
rm -f /var/lib/ohm/ohm.duckdb.wal
```

5. Generate a customer API key for TOPO agents:

```bash
# Add to /etc/ohm/ohmd.json under customers:
"topo_instance": {
  "token": "ohm-topo-<random>",
  "role": "readwrite"
}
```

6. Update TOPO agents to use the customer API key with `X-Tenant-ID` header (or via SDK: `connect_http(tenant_id="topo_instance")`)
7. Disable the old `topod` systemd unit: `systemctl disable topod`

### Deprecation timeline

| Release | Status |
|---------|--------|
| v0.5.x (current) | `topod` works with deprecation warning |
| v0.6.x | `topod` works with deprecation warning |
| v0.7.x | `topod` removed; use `ohmd --schema topo` or multi-tenant |

The TOPO domain template (`topo.json`) includes industrial node types (equipment, process, instrument, sensor, etc.) and edge types appropriate for manufacturing/industrial contexts.

## TOPO Node-Type & Layer Vocabulary Migration (OHM-ue9k)

TOPO's pre-migration store used a different vocabulary than OHM's canonical
schema. After the `ohmd --schema topo` switch, the runtime must be able to
read the legacy vocabulary without rejecting existing rows. This section
documents the migration recipe for the vocabulary gap.

### Vocabulary differences

| Field | Legacy TOPO | OHM canonical | Notes |
|-------|-------------|----------------|-------|
| NodeType case | `UPPERCASE` (`METRIC`, `DATA_PRODUCT`) | lowercase (`metric`, `data_product`) | OHM-ue9k reconciliation |
| Layer encoding | `INTEGER` (`1`, `2`, `3`) | `VARCHAR` (`'L1'`, `'L2'`, `'L3'`, `'L4'`) | Done in migration 0.4.x |
| NodeType set | 4 missing types | full topo set | Added in OHM-ue9k |

### What ships in OHM-ue9k

`SchemaConfig.topo()` now declares:

- `metric`, `data_product`, `component`, `other` — the four node types
  that were in legacy TOPO but missing from the OHM port
- `case_strategy="uppercase"` — accepts the legacy `UPPERCASE` form for
  read-side validation so existing data validates without a one-shot rename
- `validate_node_type()` and `SchemaConfig.validate_node_type()` are now
  case-insensitive against the canonical lowercase set
- `normalize_node_type(node_type)` returns the canonical lowercase form
  of any recognized type (used by writes to keep the graph consistent)

The `ohm.graph.normalize_node_type()` function is the canonicalization
entry point. Downstream writes should call it before persisting node
types so the graph stays in canonical form regardless of where the
input originated.

### Migration recipe

**Step 1 — inventory the legacy types**

```sql
-- In the legacy TOPO DuckDB, count distinct UPPERCASE types
SELECT type, COUNT(*) FROM nodes GROUP BY type ORDER BY 2 DESC;
```

For each uppercase type in the result, confirm it maps to a canonical
lowercase OHM type:

| Legacy | Canonical OHM | Migration action |
|--------|---------------|------------------|
| `METRIC` | `metric` | lowercase + add to `topo()` (done) |
| `DATA_PRODUCT` | `data_product` | lowercase + add to `topo()` (done) |
| `COMPONENT` | `component` | lowercase + add to `topo()` (done) |
| `OTHER` | `other` | lowercase + add to `topo()` (done) |
| `SITE` | `site` | already in OHM core |
| `EQUIPMENT` | `equipment` | already in OHM core |
| `PROCESS` | `process` | already in TOPO extension |

**Step 2 — copy the data through `ohmd --schema topo`**

```bash
# With case_strategy="uppercase" in topo.json, ingest accepts legacy types
# but normalize_node_type() canonicalizes on write. So even if the source
# has UPPERCASE, the OHM store ends up with lowercase.
python -m ohm.migrate_topo /path/to/legacy/topo.duckdb \
    --target /var/lib/ohm/tenants/topo_instance/ohm.duckdb
```

The migrator reads each row, calls `normalize_node_type()` on the
`type` column, and inserts into the OHM store. Validation passes
either way because of the case-insensitive check.

**Step 3 — verify**

```python
import ohm.sdk as ohm
with ohm.connect("/var/lib/ohm/tenants/topo_instance", actor="ops", tenant_id="topo_instance") as g:
    stats = g.stats()
    # All types should be lowercase. Any UPPERCASE rows in the result
    # indicate a type that wasn't normalized — file a beads issue.
    type_counts = stats["by_type"]
    upper = {k: v for k, v in type_counts.items() if k != k.lower()}
    assert not upper, f"unnormalized types remain: {upper}"
```

**Step 4 — switch off `case_strategy="uppercase"` (optional, future)**

Once the migration is complete and operators are confident no legacy
data remains, the `case_strategy` can be flipped to `"lowercase"` (or
removed from the template) to lock in the canonical form. Reads of
UPPERCASE types will then fail validation, which is the right behavior
once there are no more legacy rows.

### Layer encoding (already migrated in 0.4.x)

If you have any INTEGER-encoded layers (`1`, `2`, `3`), run:

```sql
UPDATE edges SET layer = 'L' || layer WHERE layer IN ('1', '2', '3', '4');
UPDATE nodes SET layer = 'L' || layer WHERE layer IN ('1', '2', '3', '4');
```

This was already shipped via the migration framework in OHM 0.4.x, so
most existing TOPO deployments are already on VARCHAR layers.

### Rollback

If the migration fails or produces unexpected results, the legacy
`topod` entry point still works (it calls `main(schema_config=TOPO_SCHEMA)`
with the deprecation warning). Operators can stop `ohmd --schema topo`,
start `topod`, and continue with the legacy stack while the migration
is debugged. No data is lost.

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
