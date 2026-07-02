# ADR-041: Temporal Event Model — Intervals, Plans, and Horizons

**Date:** 2026-07-02
**Status:** Decided

## Context

TOPO needs to represent time-bounded events that are richer than OHM's point-in-time observations: a 4-day planned maintenance window, a historical outage, a forecast production campaign, or a current operating state. These events have:

- a bounded duration (`start_ts` / `end_ts`)
- a horizon (`HISTORICAL`, `CURRENT`, `PLANNED`, `FORECAST`)
- an event class and operating state (`PM`, `CM`, `FAILURE`, `OPERATIONAL`, ...)
- impact on flows and L3 causal context
- grouping under plans (annual outage, PM schedule, campaign)

OHM's core `ohm_observations` table is designed for point-in-time measurements: a single value with a timestamp, sigma, source, and notes. It is not a fit for bounded intervals with horizon semantics, state machines, and plan grouping.

The question was whether to:

- **Option A**: Add first-class core tables `ohm_intervals` and `ohm_plans` immediately.
- **Option B**: Pilot the model as TOPO DomainTables (`topo_intervals`, `topo_plans`, `topo_event_links`), then generalize to core once semantics stabilize.
- **Option C**: Extend `ohm_observations` with JSON metadata for start/end/horizon/state.

## Decision

**Option B**: TOPO immediately unblocks with DomainTables, while the generic `ohm_intervals` / `ohm_plans` primitives are designed in parallel and promoted to core once field usage proves the schema.

### Rationale

1. The DomainTable hook from ADR-040 (`SchemaConfig.domain_tables`) already provides a first-class, recoverable, schema-managed home for domain-specific tables.
2. TOPO's event semantics are still being discovered: operating-state taxonomies, plan rollup rules, and horizon transitions need real usage before they are frozen into core OHM DDL.
3. A pilot avoids forcing every OHM consumer to adopt immature temporal primitives. When the pilot stabilizes, the tables can be renamed/merged into core with a documented migration path.
4. Extending `ohm_observations` with JSON (Option C) would lose queryability, append-only clarity, and the distinction between a measurement and a duration.

## Pilot Table Design (TOPO DomainTables)

These tables are registered in `SchemaConfig.topo()` alongside `topo_observations`, `topo_observation_assessments`, etc.

### `topo_plans`

A plan is a purpose-bound container for intervals.

| Column | Purpose |
|--------|---------|
| `id` | Primary key (VARCHAR) |
| `node_id` | Optional OHM node the plan applies to |
| `label` | Human name |
| `plan_type` | `annual_outage`, `pm_schedule`, `campaign`, ... |
| `status` | `draft`, `approved`, `active`, `completed`, `cancelled` |
| `horizon` | `HISTORICAL`, `CURRENT`, `PLANNED`, `FORECAST` |
| `start_ts` / `end_ts` | Bounded plan window |
| `created_by` | Agent/account |
| `created_at` / `updated_at` | Timestamps |
| `metadata` | JSON for extensible rollup config |

### `topo_events` (intervals)

An event is a bounded interval with state and impact.

| Column | Purpose |
|--------|---------|
| `id` | Primary key |
| `node_id` | OHM node the event applies to |
| `plan_id` | Optional FK-ish reference to `topo_plans` |
| `label` | Human name |
| `event_class` | `PM`, `CM`, `FAILURE`, `OPERATIONAL`, `INSPECTION`, ... |
| `operating_state` | `running`, `derated`, `stopped`, `standby`, ... |
| `horizon` | `HISTORICAL`, `CURRENT`, `PLANNED`, `FORECAST` |
| `start_ts` / `end_ts` | Bounded duration |
| `flow_impact` | JSON list of affected flows |
| `l3_context` | JSON list of causal context node ids |
| `forecast_basis` | Source/id of the forecast that generated the interval |
| `decision_metadata` | JSON for decisions tied to the interval |
| `created_by` / `created_at` / `updated_at` | Audit |
| `metadata` | JSON extensibility |

### `topo_event_links`

Explicit relationships between events (e.g., a planned PM must follow a diagnostic event).

| Column | Purpose |
|--------|---------|
| `id` | Primary key |
| `from_event_id` / `to_event_id` | Event endpoints |
| `edge_type` | `PRECEDES`, `CONTAINS`, `DEPENDS_ON`, `CAUSES`, `ENABLES`, ... |
| `layer` | `L1` structural, `L2` citation, `L3` causal, `L4` prospect |
| `confidence` | 0–1 |
| `created_by` / `created_at` | Audit |
| `metadata` | JSON |

### Ordering

- `topo_observations` and friends: 100–140 (already exist)
- `topo_plans`: 200
- `topo_events`: 210
- `topo_event_links`: 220

## Horizon Semantics

| Horizon | Meaning | Example | Update rule |
|---------|---------|---------|-------------|
| `HISTORICAL` | Actually happened, timestamps in the past | A completed outage | Immutable after close |
| `CURRENT` | Active now, `start_ts <= now < end_ts` | Running maintenance | Updated to `HISTORICAL` when `end_ts` passes |
| `PLANNED` | Committed future work, `start_ts > now` | Approved annual outage | Can be promoted to `CURRENT` or demoted to `CANCELLED` |
| `FORECAST` | Hypothetical future, may not happen | Predicted failure window | Can be promoted to `PLANNED` or demoted to `SUPERSEDED` |

Horizon is **not** a truth tier. It is a temporal classification. The confidence and source-tier of the underlying evidence still apply.

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
- A plan has its own horizon and bounded window, but child intervals may have tighter windows.
- Rollup queries answer: "what is the total downtime under the annual outage plan?" or "which events in the PM schedule are currently active?"
- Rollups traverse `topo_event_links` (`CONTAINS`) and can be filtered by horizon and date range.
- A plan's own horizon is derived from its child intervals unless explicitly set: if any child is `CURRENT`, the plan is `CURRENT`; otherwise the most forward horizon among children wins.

## Migration Path to Core

Once the pilot tables are exercised and the schema stabilizes:

1. **Rename**: `topo_events` → `ohm_intervals`, `topo_plans` → `ohm_plans`, `topo_event_links` → `ohm_interval_links` (or merge into `ohm_edges` if the relationship is purely graph-like).
2. **Schema promotion**: move the table definitions from `SchemaConfig.topo()` to the base `SchemaConfig`.
3. **Migration**: a single OHM migration copies existing `topo_*` rows into `ohm_intervals` / `ohm_plans`, then drops the old tables or keeps them as views.
4. **SDK**: expose `create_interval`, `create_plan`, `link_intervals` on the OHM HTTP and Python SDKs.
5. **Query primitives**: add date-range and horizon filters to `neighborhood()` and new `interval_rollups()` endpoint.

Until then, TOPO uses the DomainTable equivalents and documents the eventual migration in code comments and the ADR registry.

## Consequences

- TOPO can proceed immediately with a schema-managed, DuckLake-mirrored event model.
- Other OHM consumers are not forced to adopt immature temporal primitives.
- The core schema team can design `ohm_intervals` / `ohm_plans` with real field evidence rather than guessing at taxonomy upfront.
- A future migration is acknowledged and planned, not deferred as technical debt.
- Bayesian updates over temporal windows (OHM-vatf) can read from `topo_events` and later switch to `ohm_intervals` without changing their logic.

## Acceptance

1. `SchemaConfig.topo()` registers DomainTables `topo_plans`, `topo_events`, `topo_event_links` (OHM-dh9l.1).
2. This ADR is cross-referenced from ADR-040 and the issues OHM-ay5k, OHM-4qdk, OHM-xggk, and OHM-vatf.
3. End-to-end test demonstrates a 4-day planned maintenance window rolling up under an annual-outage plan.
4. A follow-up issue (`OHM-dh9l.3` or equivalent) tracks the core generalization migration once the pilot is proven.

## References

- ADR-040: TOPO Observation Lifecycle — Domain DDL Tables
- Epic: OHM-dh9l — Temporal event model
- Children: OHM-dh9l.1 (DomainTable unblock), OHM-dh9l.2 (this ADR)
- Related: OHM-ay5k, OHM-4qdk, OHM-xggk, OHM-vatf
