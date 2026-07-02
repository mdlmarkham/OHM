# ADR-040: TOPO Observation Lifecycle — Domain DDL Tables (Option A)

**Date:** 2026-07-02
**Status:** Decided

## Context

TOPO's observation system uses a 5-table relational model:

1. **`topo_observations`** — main observation records (sensor readings, anomalies, measurements)
2. **`topo_observation_assessments`** — append-only assessment history with `is_current` flags
3. **`topo_observation_annotations`** — annotations (comments, tags, context)
4. **`topo_observation_followups`** — followup tracking (actions, investigations, monitoring)
5. **`topo_prospects`** — predictive-maintenance prospects (already shipped via OHM-vl8o)

OHM's core `ohm_observations` table is flat — a single table with no child tables for assessment history, annotations, or followups. The question was whether to:

- **Option A**: Keep TOPO's multi-table system as domain DDL tables (recommended; preserves relational integrity, append-only semantics, and `is_current` flagging)
- **Option B**: Collapse assessments, annotations, and followups into `ohm_observations.metadata` JSON (regression; loses relational constraints, queryability, and append-only semantics)

The domain DDL hook (OHM-vl8o / `SchemaConfig.domain_tables`) was already implemented, providing a first-class mechanism for domain-specific tables to be created alongside the base OHM schema in a single `initialize_schema()` call.

## Decision

**Option A**: Declare the 4 TOPO observation lifecycle tables as `DomainTable` instances in `SchemaConfig.topo()` and `topo.json`. They are created by `_create_domain_tables()` during `initialize_schema()`, alongside the existing `topo_prospects` table.

### Table Design

| Table | Purpose | Key Columns | Indexes |
|-------|---------|-------------|---------|
| `topo_observations` | Main observation records | `id`, `node_id`, `obs_type`, `obs_value`, `source`, `observed_at` | `node_id`, `obs_type`, `observed_at` |
| `topo_observation_assessments` | Append-only assessment history | `id`, `observation_id`, `assessment_type`, `is_current`, `assessed_by` | `observation_id`, `is_current` |
| `topo_observation_annotations` | Annotations on observations | `id`, `observation_id`, `annotation_type`, `annotation_value` | `observation_id` |
| `topo_observation_followups` | Followup tracking | `id`, `observation_id`, `followup_type`, `status`, `assigned_to` | `observation_id`, `status`, `assigned_to` |

### Ordering

Tables are created in dependency order via the `ordering` field:
- `topo_prospects` (100) — already existed
- `topo_observations` (110) — parent table
- `topo_observation_assessments` (120) — depends on observations
- `topo_observation_annotations` (130) — depends on observations
- `topo_observation_followups` (140) — depends on observations

### Referential Integrity

DuckDB does not support `REFERENCES` constraints (ADR-001). Referential integrity between `topo_observation_assessments.observation_id` → `topo_observations.id` (and similarly for annotations and followups) is enforced in the application layer, consistent with how OHM handles all other relationships.

### No SCHEMA_VERSION Bump

Domain tables are created by `initialize_schema()` from `SchemaConfig.domain_tables`, not via the core `MIGRATIONS` list. No `SCHEMA_VERSION` bump is needed — the domain DDL hook (OHM-vl8o, migration `0.40.0`) already handles this.

## Consequences

- TOPO's relational observation model is preserved — append-only assessments, `is_current` flags, and structured followups all work as designed
- Domain tables ride along with `initialize_schema()` — no per-domain bootstrap code needed
- Tables are created with `CREATE TABLE IF NOT EXISTS` — idempotent, safe to re-run
- Application-layer integrity enforcement is consistent with the rest of OHM (no FK constraints anywhere)
- `topo.json` template and `SchemaConfig.topo()` Python factory are kept in sync — both define the same 5 domain tables
- Future domains can follow the same pattern: declare `DomainTable` instances, pass to `SchemaConfig`, tables are auto-created

## Alternatives

- **Option B (rejected)**: Collapse into `ohm_observations.metadata` JSON. Loses SQL-queryability of assessment history, append-only semantics, and `is_current` flag management. Would require application-level code to manage assessment versioning inside a JSON blob — more complex, less robust.
- **Option C (not considered)**: Add the tables as core OHM tables. Rejected because these are domain-specific, not core to OHM's graph substrate. The domain DDL hook exists precisely for this purpose.
