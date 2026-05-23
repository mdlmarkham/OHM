# OHM Multi-Tenancy Backlog

## Epic: OHM Multi-Tenancy (OHM-MT)

Single-engine, multi-tenant architecture. One ohmd process, many isolated customer DuckDB instances. Domain templates via SchemaConfig. Cross-customer pattern learning. Lazy migrations.

---

## OHM-MT-1: Domain Template JSON Files

**Type:** feature  
**Priority:** P1  
**Estimate:** 480 min (2 days)  
**Description:** Move domain templates from Python @classmethod to JSON files. Add `SchemaConfig.from_json_file()` classmethod. Create `ohm/graph/templates/` directory with base schemas.

**Acceptance criteria:**
- `SchemaConfig.from_json_file("home_services.json")` returns valid SchemaConfig
- Existing `topo()` and `beef_herd()` classmethods still work (backward compat)
- JSON templates validate correctly (node types, edge types, observation types, provenances)
- `to_dict()` → `from_dict()` roundtrip preserves all fields
- Unit tests for JSON loading, validation, and roundtrip

**Design notes:**
- JSON files stored in `ohm/graph/templates/` (shipped with package)
- Override path: `/var/lib/ohm/templates/` (for custom domain templates without code deploy)
- Search order: custom path → package templates → built-in classmethods
- Each JSON template: `{name, node_types, layer_edge_types, layer_descriptions, observation_types, observation_sources, provenances}`

**Files to create:**
- `ohm/graph/templates/ohm.json` (base schema, auto-generated from DEFAULT_SCHEMA.to_dict())
- `ohm/graph/templates/topo.json` (industrial)
- `ohm/graph/templates/beef_herd.json` (ranching)
- `ohm/graph/templates/home_services.json` (NEW)
- `ohm/graph/templates/manufacturing.json` (NEW, extends topo)
- `ohm/graph/templates/construction.json` (NEW)
- `ohm/graph/templates/healthcare.json` (NEW)

**Files to modify:**
- `ohm/graph/schema.py` — add `from_json_file()` classmethod

---

## OHM-MT-2: TenantManager Module

**Type:** feature  
**Priority:** P0  
**Estimate:** 1200 min (5 days)  
**Deps:** OHM-MT-1  
**Description:** New module `ohm/tenant.py` for provisioning, routing, caching, and lifecycle management of per-customer OHM instances.

**Acceptance criteria:**
- `TenantManager.provision(customer_id, domain, tier)` creates isolated DuckDB at `/var/lib/ohm/tenants/{customer_id}/ohm.duckdb`
- `TenantManager.get_store(customer_id)` returns OhmStore connected to that instance
- LRU cache with configurable max size (default 100), idle eviction after 10 min
- `TenantManager.deprovision(customer_id, secure_delete=True)` shreds and removes instance
- `TenantManager.list_tenants()` returns all tenant metadata
- `TenantManager.tenant_status(customer_id)` returns health/schema version/size/stats
- Provisions apply domain SchemaConfig from JSON template
- Each provision writes `meta.json` with customer_id, domain, tier, schema_version, created_at, shared_patterns flag
- Seeds initial agent role nodes based on domain + tier (e.g., dispatch_analyst for home_services scout)
- Thread-safe — concurrent get_store calls don't create duplicate connections

**Design notes:**
- `TenantManager.__init__(tenants_dir, templates_dir, max_cached=100)`
- LRU cache: `collections.OrderedDict` → evict least-recently-used when full
- Idle eviction: background thread or on-access check, close DuckDB conn after 10 min idle
- Secure delete: overwrite file with random bytes before unlink (or use encrypted DuckDB)
- `meta.json` schema: `{customer_id, domain, tier, schema_version, created_at, shared_patterns, integrations}`

**Files to create:**
- `ohm/tenant.py` (~400-500 lines)

---

## OHM-MT-3: Server Auth — Customer API Key Support

**Type:** feature  
**Priority:** P0  
**Estimate:** 960 min (4 days)  
**Deps:** OHM-MT-2  
**Description:** Add customer API key authentication alongside existing agent token auth. Customer keys resolve to tenant instances. Agent tokens continue resolving to core OHM (unchanged behavior).

**Acceptance criteria:**
- Customer API keys stored as `customer_tokens` dict: `{key_hash: customer_id}`
- `_authenticate()` returns `(agent_name, customer_id_or_none)` tuple
- Agent token auth is UNCHANGED — all existing agents continue working exactly as before
- Customer API key auth resolves to `("customer_api", customer_id)`
- `_authenticate_customer()` checks Bearer token against customer_tokens
- Customer API keys support same hash-based storage as agent tokens
- Token provisioning: `POST /admin/customer-token` creates a new customer API key (admin-only)
- Invalid customer key returns 401 (same as invalid agent token — no information leakage)
- Rate limiting applies per API key (both agent and customer)

**Design notes:**
- Agent tokens: `{agent_name: {hash: "sha256_hex", role: "read-write"}}`
- Customer tokens: `{api_key_hash: customer_id}` (separate dict, same hash function)
- API key format: `twai_live_{random_24chars}` (identifiable as customer key)
- Token provisioning endpoint returns the plaintext key ONCE (like Stripe API keys)
- Store only the hash after creation

**Files to modify:**
- `ohm/server/server.py` — `_authenticate()`, `_build_token_lookup()`, new `_build_customer_token_lookup()`, new `/admin/customer-token` endpoint

---

## OHM-MT-4: Server Request Routing — Tenant Context

**Type:** feature  
**Priority:** P0  
**Estimate:** 720 min (3 days)  
**Deps:** OHM-MT-3  
**Description:** Route all OHM API requests to the correct store (core OHM or tenant instance) based on authenticated identity. Replace direct `self.store` references with tenant-aware store resolution.

**Acceptance criteria:**
- New `current_store` property on OhmHandler resolves to core store or tenant store
- All existing endpoints work identically for agent auth (core OHM)
- All existing endpoints work for customer auth (tenant instance)
- Tenant isolation verified: Customer A's API key can ONLY access Customer A's data
- Cross-tenant access returns 404 (not 403 — don't leak tenant existence)
- Node IDs are tenant-scoped: same node_id in different tenants = different nodes
- Performance: tenant store resolution adds <1ms per request (LRU cache hit)

**Design notes:**
- `current_store` property: checks `self._customer_id`, routes to `tenant_manager.get_store()` or `self.store`
- Mechanical refactor: `s/self\.store\./self.current_store./g` across all handler methods
- Set `self._customer_id` during `_authenticate()` and store on handler instance
- Reset per-request state in `do_GET/do_POST/do_PUT/do_DELETE` entry points
- ~50-80 call sites in server.py to update

**Files to modify:**
- `ohm/server/server.py` — add `current_store` property, refactor all `self.store` references

---

## OHM-MT-5: Lazy Schema Migration for Tenant Instances

**Type:** feature  
**Priority:** P1  
**Estimate:** 480 min (2 days)  
**Deps:** OHM-MT-2  
**Description:** When a tenant instance is accessed, check its schema version against current OHM version. If behind, apply pending migrations automatically. Zero-downtime migration.

**Acceptance criteria:**
- `TenantManager.get_store()` checks `meta.json` schema_version before returning store
- If schema_version < SCHEMA_VERSION, applies pending migrations from core MIGRATIONS list
- Updates `meta.json` schema_version after successful migration
- Failed migration: logs error, returns store in read-only mode, alerts platform operator
- Migration events logged to tenant's DuckDB change feed
- Performance: migration check adds <5ms on first access (no-op if version matches)
- Concurrent access: only one migration runs at a time (per-tenant lock)

**Design notes:**
- Migration check happens in `TenantManager.get_store()` before returning from cache
- `_apply_lazy_migrations(store, meta)` — calls existing `_apply_migrations()` from schema.py
- Per-tenant threading lock for migration (not global — different tenants can migrate concurrently)
- Backup: optional `CHECKPOINT` before migration (DuckDB instant)
- Rollback: if migration fails, mark tenant as `needs_attention` in meta.json

**Files to modify:**
- `ohm/tenant.py` — add `_apply_lazy_migrations()`, integrate into `get_store()`

---

## OHM-MT-6: Provisioning API Endpoints

**Type:** feature  
**Priority:** P1  
**Estimate:** 720 min (3 days)  
**Deps:** OHM-MT-3, OHM-MT-4  
**Description:** REST API endpoints for tenant provisioning, status, listing, and deprovisioning. Admin-only access (platform operator, not customer API keys).

**Acceptance criteria:**
- `POST /tenant/provision` — create new tenant instance, returns customer_id + API key
  - Body: `{domain, tier, integrations?, customer_id?}`
  - Auto-generates customer_id if not provided
  - Auto-generates API key (returned once, hashed for storage)
- `GET /tenant/{customer_id}` — tenant status (domain, tier, schema_version, size, last_accessed)
- `GET /tenant/{customer_id}/schema` — domain schema details for this tenant
- `DELETE /tenant/{customer_id}` — deprovision (with `secure_delete` query param)
- `GET /tenants` — list all tenants with basic metadata
- `POST /tenant/{customer_id}/export` — export tenant data as JSON + DuckDB dump
- All endpoints require admin auth (agent token with admin role)
- Customer API keys cannot access these endpoints (403)

**Design notes:**
- Admin auth: existing agent tokens with `role: "admin"` in config
- Provisioning calls `TenantManager.provision()` + generates API key
- Export: DuckDB `COPY TO` + JSON dump of all nodes/edges/observations
- Deprovision: confirm with `?confirm=true` query param (safety)

**Files to modify:**
- `ohm/server/server.py` — add routes under `/tenant/` path prefix

---

## OHM-MT-7: Cross-Instance Pattern Extraction

**Type:** feature  
**Priority:** P2  
**Estimate:** 1200 min (5 days)  
**Deps:** OHM-MT-2, OHM-MT-6  
**Description:** Extract anonymized knowledge patterns from tenant instances and seed them into new instances. Each new customer starts smarter than the last.

**Acceptance criteria:**
- Pattern extraction runs on tenant instances with `shared_patterns: true` in meta.json
- Extracts L3 pattern/synthesis nodes with provenance from domain agent roles
- Anonymization strips all customer-identifiable data (IDs, names, addresses, phone numbers)
- Patterns stored in `/var/lib/ohm/shared_patterns/{domain}/` as JSON files
- Pattern format: `{id, label, content, confidence, tags, domain, sample_size, extracted_at}`
- `TenantManager.provision()` calls `_seed_patterns()` to inject domain patterns into new instances
- Seeded patterns get provenance: "platform_pattern" and confidence weighted by sample size
- Customer can opt out at any time (meta.json `shared_patterns: false` → patterns deleted from shared store)
- Extraction can be triggered manually or on schedule (e.g., nightly)

**Design notes:**
- Extraction query: `SELECT * FROM ohm_nodes WHERE type IN ('pattern', 'idea') AND provenance LIKE '%_analyst' AND deleted_at IS NULL`
- Anonymization: regex-based PII stripping + ID randomization (preserve graph structure, replace identifiers)
- Pattern confidence: start at 0.5, adjust toward source confidence weighted by N contributing tenants
- Seed on provision: `store.create_node()` for each pattern with provenance="platform_pattern"

**Files to create:**
- `ohm/patterns.py` (~300 lines) — extraction, anonymization, seeding logic

**Files to modify:**
- `ohm/tenant.py` — integrate `_seed_patterns()` and `_extract_patterns()`

---

## OHM-MT-8: SDK Tenant-Aware HTTP Client

**Type:** feature  
**Priority:** P2  
**Estimate:** 240 min (1 day)  
**Deps:** OHM-MT-4  
**Description:** Extend `connect_http()` SDK to support tenant-scoped connections. Customer-facing SDK uses API key that auto-resolves to tenant.

**Acceptance criteria:**
- `connect_http(url, actor="customer_api", token="twai_live_abc123")` auto-routes to tenant
- Optional `customer_id` parameter for explicit tenant targeting (sends X-Tenant-ID header)
- All Graph methods work identically against tenant instances (create_node, create_edge, search, etc.)
- SDK documentation updated with tenant usage examples
- Error messages distinguish between auth failures and tenant-not-found

**Design notes:**
- Primary mechanism: API key → server resolves tenant (no SDK change needed for basic use)
- Explicit targeting: `customer_id` parameter adds `X-Tenant-ID: {customer_id}` header
- Useful for admin/debug access to specific tenant instances

**Files to modify:**
- `ohm/framework/sdk.py` — add `customer_id` param to `connect_http()`, add header in `_http_request()`

---

## OHM-MT-9: Integration Tests — Multi-Tenancy

**Type:** chore  
**Priority:** P1  
**Estimate:** 480 min (2 days)  
**Deps:** OHM-MT-4  
**Description:** Comprehensive integration tests verifying tenant isolation, lazy migration, provisioning lifecycle, and cross-tenant security.

**Acceptance criteria:**
- Test: provision tenant → write node → read node → verify isolation from other tenants
- Test: provision two tenants with same domain → write different data → verify no cross-contamination
- Test: deprovision tenant → verify data is gone → verify other tenants unaffected
- Test: lazy migration — provision on old schema version → access → auto-migrate → verify new columns exist
- Test: customer API key cannot access core OHM data
- Test: agent token cannot access tenant data
- Test: pattern extraction → anonymization verification → seed into new instance
- Test: concurrent access to same tenant from multiple requests
- Test: LRU eviction → re-access → store re-opened correctly

**Files to create:**
- `tests/test_multi_tenancy.py` (~300-400 lines)

---

## OHM-MT-10: Home Services Domain Template

**Type:** feature  
**Priority:** P1  
**Estimate:** 240 min (1 day)  
**Deps:** OHM-MT-1  
**Description:** Create the home_services SchemaConfig domain template for HVAC/plumbing/electrical shops. First vertical for TeamWork AI.

**Acceptance criteria:**
- JSON template: `ohm/graph/templates/home_services.json`
- Node types: customer, technician, job, appointment, equipment, service_contract, warranty, estimate, invoice
- Edge types: existing OHM types + domain-specific L2 flows (call → dispatch → en_route → on_site → complete → invoice → follow_up)
- Observation types: call_duration, job_completion_time, customer_satisfaction, first_time_fix_rate, revenue_per_job, travel_time, parts_cost, callback_rate
- Observation sources: twilio, gps, quickbooks, housecall_pro, owner
- Provenances: dispatch_analyst, schedule_coordinator, parts_broker, compliance_planner, operations_manager
- SchemaConfig validates correctly via `from_json_file()`
- Unit test: load template, validate all types, create test node of each type

**Design notes:**
- L1 layer description: "Shop → technicians → customers → equipment → service areas"
- L2 layer description: "Call → estimate → dispatch → en_route → on_site → complete → invoice → follow_up"
- L3 layer description: "Scheduling patterns, customer LTV, technician performance, seasonal demand"
- L4 layer description: "Revenue optimization, churn risk, capacity planning, weather impact"

**Files to create:**
- `ohm/graph/templates/home_services.json`

---

## OHM-MT-11: Manufacturing Domain Template

**Type:** feature  
**Priority:** P2  
**Estimate:** 240 min (1 day)  
**Deps:** OHM-MT-1  
**Description:** Create the manufacturing SchemaConfig domain template for SMB manufacturing execution. Extends topo schema with MES-specific types.

**Acceptance criteria:**
- JSON template: `ohm/graph/templates/manufacturing.json`
- Inherits from topo node types (process, instrument, controller, valve, pump, etc.)
- Adds: work_order, bill_of_materials, quality_check, machine, tool, fixture
- Observation types: cycle_time, downtime_duration, defect_rate, oee, setup_time
- Observation sources: scada, plc, historian, erp, mes, operator
- Provenances: floor_analyst, schedule_coordinator, supply_broker, quality_planner, plant_manager
- SchemaConfig validates correctly

**Files to create:**
- `ohm/graph/templates/manufacturing.json`

---

## OHM-MT-12: Construction Domain Template

**Type:** feature  
**Priority:** P2  
**Estimate:** 240 min (1 day)  
**Deps:** OHM-MT-1  
**Description:** Create the construction SchemaConfig domain template.

**Acceptance criteria:**
- JSON template: `ohm/graph/templates/construction.json`
- Node types: project, phase, task, crew, subcontractor, material, permit, inspection, change_order, site, drawing, specification
- Observation types: progress_pct, crew_size, material_delivered, weather_delay, safety_incident, rfi_count, change_order_value
- Observation sources: foreman, project_manager, weather_api, permit_office
- Provenances: project_analyst, schedule_coordinator, supply_broker, safety_planner, project_manager

**Files to create:**
- `ohm/graph/templates/construction.json`

---

## OHM-MT-13: Healthcare Domain Template

**Type:** feature  
**Priority:** P2  
**Estimate:** 240 min (1 day)  
**Deps:** OHM-MT-1  
**Description:** Create the healthcare SchemaConfig domain template for small practices.

**Acceptance criteria:**
- JSON template: `ohm/graph/templates/healthcare.json`
- Node types: patient, provider, payer, procedure, diagnosis, prior_auth, claim, referral, medication, lab_result, appointment
- Observation types: auth_turnaround_days, denial_rate, claim_amount, patient_wait_time, no_show_rate, collections_rate
- Observation sources: ehr, clearinghouse, payer_api, fax, front_desk
- Provenances: auth_broker, coding_analyst, schedule_coordinator, claims_planner, practice_manager

**Files to create:**
- `ohm/graph/templates/healthcare.json`