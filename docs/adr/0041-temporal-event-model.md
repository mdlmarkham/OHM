# ADR-041: Temporal Event Model — Intervals, Plans, and Horizons

**Date:** 2026-07-02
**Status:** Decided (updated 2026-07-06 — corrected column vocabulary to match shipped pilot)

## Context

TOPO needs to represent time-bounded events that are richer than OHM's point-in-time observations: a 4-day planned maintenance window, a historical outage, a forecast production campaign, or a current operating state. These events have:

- a bounded duration (`start_time` / `end_time`)
- a horizon (`HISTORICAL`, `CURRENT`, `PLANNED`, `FORECAST`)
- an event class and operating state (`PM`, `CM`, `FAILURE`, `OPERATIONAL`, ...)
- impact on flows and L3 causal context
- grouping under plans (annual outage, PM schedule, campaign)

OHM's core `ohm_observations` table is designed for point-in-time measurements: a single value with a timestamp, sigma, source, and notes. It is not a fit for bounded intervals with horizon semantics, state machines, and plan grouping.

The question was whether to:

- **Option A**: Add first-class core tables `ohm_intervals` and `ohm_plans` immediately.
- **Option B**: Pilot the model as TOPO DomainTables (`topo_plans`, `topo_events`, `topo_event_links`), then generalize to core once semantics stabilize.
- **Option C**: Extend `ohm_observations` with JSON metadata for start/end/horizon/state.

## Decision

**Option B**: TOPO immediately unblocks with DomainTables, while the generic `ohm_intervals` / `ohm_plans` primitives are designed in parallel and promoted to core once field usage proves the schema.

### Rationale

1. The DomainTable hook from ADR-040 (`SchemaConfig.domain_tables`) already provides a first-class, recoverable, schema-managed home for domain-specific tables.
2. TOPO's event semantics are still being discovered: operating-state taxonomies, plan rollup rules, and horizon transitions need real usage before they are frozen into core OHM DDL.
3. A pilot avoids forcing every OHM consumer to adopt immature temporal primitives. When the pilot stabilizes, the tables can be renamed/merged into core with a documented migration path.
4. Extending `ohm_observations` with JSON (Option C) would lose queryability, append-only clarity, and the distinction between a measurement and a duration.

## Pilot Table Design (TOPO DomainTables — as shipped, commit e93238d)

These tables are registered in `SchemaConfig.topo()` alongside `topo_observations`, `topo_observation_assessments`, etc. The pilot uses a simplified column vocabulary (`start_time`/`end_time`, `event_type`, `severity`); the target vocabulary (`start_ts`/`end_ts`, `event_class`, `operating_state`) is tracked by OHM-dh9l.1.

### `topo_plans` (ordering=150)

A plan is a purpose-bound container for intervals.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | VARCHAR | Primary key |
| `node_id` | VARCHAR | Optional OHM node the plan applies to |
| `plan_type` | VARCHAR | `maintenance_window`, `annual_outage`, `pm_schedule`, `campaign` |
| `horizon_start` | TIMESTAMP | Start of plan window |
| `horizon_end` | TIMESTAMP | End of plan window |
| `status` | VARCHAR DEFAULT 'active' | `draft`, `approved`, `active`, `completed`, `cancelled` |
| `created_by` | VARCHAR NOT NULL | Agent/account |
| `created_at` | TIMESTAMP | Default CURRENT_TIMESTAMP |
| `updated_at` | TIMESTAMP | Default CURRENT_TIMESTAMP |
| `metadata` | JSON | Extensible rollup config |

Indexes: `idx_topo_plans_node` (node_id), `idx_topo_plans_type` (plan_type), `idx_topo_plans_horizon` (horizon_start, horizon_end), `idx_topo_plans_status` (status).

### `topo_events` (ordering=160)

An event is a bounded interval with state and impact.

| Column | Type | Purpose |
|--------|------|---------|
| `id` | VARCHAR | Primary key |
| `plan_id` | VARCHAR | FK-ish reference to `topo_plans` |
| `node_id` | VARCHAR | OHM node the event applies to |
| `event_type` | VARCHAR | `shutdown`, `restart`, `inspection`, `outage`, `failure` |
| `start_time` | TIMESTAMP | Start of the interval |
| `end_time` | TIMESTAMP | End of the interval |
| `severity` | VARCHAR | `low`, `medium`, `high`, `critical` |
| `description` | TEXT | Human description |
| `created_by` | VARCHAR NOT NULL | Agent/account |
| `created_at` | TIMESTAMP | Default CURRENT_TIMESTAMP |
| `updated_at` | TIMESTAMP | Default CURRENT_TIMESTAMP |
| `metadata` | JSON | Extensibility |

Indexes: `idx_topo_events_plan` (plan_id), `idx_topo_events_node` (node_id), `idx_topo_events_type` (event_type), `idx_topo_events_time` (start_time, end_time).

### `topo_event_links` (ordering=170)

Explicit relationships between events (e.g., a planned PM must follow a diagnostic event).

| Column | Type | Purpose |
|--------|------|---------|
| `id` | VARCHAR | Primary key |
| `from_event_id` | VARCHAR | Source event |
| `to_event_id` | VARCHAR | Target event |
| `link_type` | VARCHAR | `caused_by`, `followed_by`, `overlaps`, `contains` |
| `created_by` | VARCHAR NOT NULL | Agent/account |
| `created_at` | TIMESTAMP | Default CURRENT_TIMESTAMP |
| `metadata` | JSON | Extensibility |

Indexes: `idx_topo_elinks_from` (from_event_id), `idx_topo_elinks_to` (to_event_id), `idx_topo_elinks_type` (link_type).

### Provisioning

Tables are created by `_create_domain_tables()` in `src/ohm/graph/schema.py`, called during `initialize_schema()`. The `SchemaConfig` factory `SchemaConfig.topo()` returns a schema config with all TOPO domain tables. The `topo.json` template in `src/ohm/graph/templates/` mirrors the Python definitions; a test verifies parity.

## Horizon Semantics

| Horizon | Meaning | Example | Update rule |
|---------|---------|---------|-------------|
| `HISTORICAL` | Actually happened, timestamps in the past | A completed outage | Immutable after close |
| `CURRENT` | Active now, `start_time <= now < end_time` | Running maintenance | Updated to `HISTORICAL` when `end_time` passes |
| `PLANNED` | Committed future work, `start_time > now` | Approved annual outage | Can be promoted to `CURRENT` or demoted to `CANCELLED` |
| `FORECAST` | Hypothetical future, may not happen | Predicted failure window | Can be promoted to `PLANNED` or demoted to `SUPERSEDED` |

Horizon is **not** a truth tier. It is a temporal classification. The confidence and source-tier of the underlying evidence still apply.

**Note**: The initial pilot shipped without a first-class `horizon` column. Horizon is implied by the event's timestamps relative to now. OHM-dh9l.1 will add `horizon` as a first-class column.

## Intervals vs Observations

| | `ohm_observations` | `topo_events` (intervals) |
|--|-------------------|---------------------------|
| Time model | Point-in-time | Bounded duration |
| Value | `value` + `sigma` (numeric) | `operating_state` + `event_class` (categorical) + impact |
| Horizon | N/A | `HISTORICAL/CURRENT/PLANNED/FORECAST` |
| Source tier | Source URL + tier | Source + forecast basis |
| Use case | "Sensor read 4.2 at 09:00" | "Pump P-101 was stopped for PM from June 1 to June 4" |
| Bayesian input | Direct as evidence | Must be converted to an observation or used as a conditioning window |

Intervals can **generate** observations (e.g., an outage interval produces a downtime measurement), but they are not observations themselves.

## Plan Grouping and Rollup Semantics

- A plan groups intervals by shared purpose.
- A plan has its own window (`horizon_start`/`horizon_end`), but child intervals may have tighter windows.
- Rollup queries answer: "what is the total downtime under the annual outage plan?" or "which events in the PM schedule are currently active?"
- Rollups traverse `topo_event_links` (`followed_by`, `contains`) and can be filtered by horizon and date range.
- A plan's own horizon is derived from its child intervals unless explicitly set: if any child is `CURRENT`, the plan is `CURRENT`; otherwise the most forward horizon among children wins.

## Migration Path to Core

Once the pilot tables are exercised and the schema stabilizes:

1. **Column alignment** (tracked by OHM-dh9l.1): migrate from pilot column vocabulary (`event_type`/`severity`/`start_time`/`end_time`) to the target vocabulary (`event_class`/`operating_state`/`start_ts`/`end_ts`), add `node_path`, `horizon`, `l3_context`, `flow_impact`, `forecast_basis`, `decision_metadata`, `confidence`, `authority`, `revision`.
2. **Rename**: `topo_events` → `ohm_intervals`, `topo_plans` → `ohm_plans`, `topo_event_links` → `ohm_interval_links`.
3. **Schema promotion**: move table definitions from `SchemaConfig.topo()` to the base `SchemaConfig`.
4. **Migration**: a single OHM migration copies existing `topo_*` rows into `ohm_intervals` / `ohm_plans`, then drops the old tables or keeps them as views.
5. **SDK**: expose `create_interval`, `create_plan`, `link_intervals` on the OHM HTTP and Python SDKs.
6. **Query primitives**: add date-range and horizon filters to `neighborhood()` and a new `interval_rollup()` endpoint.

Until then, TOPO uses the DomainTable equivalents and documents the eventual migration in code comments and the ADR registry.

## Consequences

- TOPO can proceed immediately with a schema-managed, DuckLake-mirrored event model (shipped in commit e93238d, 27 tests).
- Other OHM consumers are not forced to adopt immature temporal primitives.
- The core schema team can design `ohm_intervals` / `ohm_plans` with real field evidence rather than guessing at taxonomy upfront.
- A future migration is acknowledged and planned, not deferred as technical debt.
- Bayesian updates over temporal windows (OHM-vatf) can read from `topo_events` and later switch to `ohm_intervals` without changing their logic.
- The pilot column vocabulary (`event_type`/`severity`) is a simplification that will need migration to the TOPO-aligned vocabulary (tracked by OHM-dh9l.1).

## Acceptance

1. `SchemaConfig.topo()` registers DomainTables `topo_plans`, `topo_events`, `topo_event_links` (shipped in commit e93238d).
2. This ADR is cross-referenced from ADR-040 and the issues OHM-ay5k, OHM-4qdk, OHM-xggk, and OHM-vatf.
3. End-to-end test demonstrates a 4-day planned maintenance window rolling up under an annual-outage plan (27 tests in `tests/test_topo_temporal_tables.py`).
4. Migration to TOPO-aligned DDL (OHM-dh9l.1) and eventual core generalization (OHM-dh9l.3) are tracked as separate follow-ups.

## References

- ADR-040: TOPO Observation Lifecycle — Domain DDL Tables
- Epic: OHM-dh9l — Temporal event model
- Children: OHM-dh9l.1 (DomainTable column migration), OHM-dh9l.2 (this ADR)
- Related: OHM-ay5k, OHM-4qdk, OHM-xggk, OHM-vatf
- Source: `src/ohm/graph/schema.py:1056-1125` (SchemaConfig.topo() domain tables)
- Tests: `tests/test_topo_temporal_tables.py` (27 tests)
- Template: `src/ohm/graph/templates/topo.json`
