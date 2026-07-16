# ADR-0038: Temporal Planning MCP Tool Surface

**Date:** 2026-07-16
**Status:** Accepted
**Related issues:** OHM-937 (this work), ADR-041 (temporal event model — TOPO pilot tables this exposes), ADR-040 (TOPO observation lifecycle — domain DDL tables), ADR-029 (TOON MCP transport — encoding layer these tools reuse)

## Context

OHM's temporal planning capability — plans, events, reports, runs, RUL
assessments, scenarios, drift detection, and verification outcomes — was
previously accessible only through the TOPO schema's internal database
tables (`topo_plans`, `topo_events`, `topo_reports`, `topo_runs`,
`topo_rul_assessments`) and a handful of GET endpoints served by the
pre-existing `ReportsHandlerMixin`. ADR-041 piloted these tables as TOPO
DomainTables; ADR-040 established the domain DDL pattern they ride on.

Agents using the MCP protocol (the canonical agent interface per
ADR-029 and the AGENTS.md "Primary Agent Interface" guidance) had no way
to *create* plans, *register* events, *finalize* reports, *complete* runs,
*run* scenarios, or *register* RUL assessments. The temporal layer was
write-dark from the agent's perspective: an agent could read a plan list
but could not create one without dropping to raw SQL or the Python SDK
against the daemon's store directly.

This left the temporal planning layer — the core of TOPO's
remaining-useful-life, scenario-comparison, and drift-tracking workflows —
unreachable by any MCP-capable agent, which is the documented primary
interface for agents in this project.

## Decision

Expose the temporal planning primitives as **13 new MCP-callable tools**,
wired through the existing four-layer pattern (queries → store/handler →
MCP dispatch → MCP tool schema) and the server's dual routing mechanisms.

### 1. Thirteen new MCP tools

Added to `src/ohm/mcp/tools.py` (tool schemas, lines 886–1096) and
dispatched via `src/ohm/mcp/dispatch.py` (dispatch cases, lines 369–459):

| MCP tool | HTTP route | Purpose |
|----------|-----------|---------|
| `ohm_plan_create` | `POST /plan/create` | Create a time-bounded plan container |
| `ohm_event_create` | `POST /event/create` | Register a bounded temporal event under a plan |
| `ohm_event_link` | `POST /event/link` | Link two events (caused_by, followed_by, overlaps, contains) |
| `ohm_report_create` | `POST /report/create` | Create a report record |
| `ohm_report_finalize` | `POST /report/finalize` | Finalize a report (lock it) |
| `ohm_run_create` | `POST /run/create` | Create a model/analysis run |
| `ohm_run_complete` | `POST /run/complete` | Mark a run complete with results |
| `ohm_rul_register` | `POST /rul/register` | Register a remaining-useful-life assessment |
| `ohm_scenario_run` | `POST /scenario/run` | Run a scenario — chains `query_compare_scenarios` → `query_counterfactual_cascade` |
| `ohm_scenarios` | `GET /scenarios` | List scenarios |
| `ohm_verifiable_claims` | `GET /verifiable-claims` | List claims awaiting verification |
| `ohm_record_verification_outcome` | `POST /verification/outcome` | Record whether a claim was validated or falsified by reality |
| `ohm_drifts` | `GET /drifts` | List drift observations (deviations from plan/forecast) |

Total MCP tool count rises from 46 to 59.

### 2. New handler mixin (no shadowing)

`TemporalPlanningHandlerMixin` in
`src/ohm/server/handlers/temporal_planning.py:17` (extends
`OhmHandlerBase`). It defines **only NEW endpoints** — it deliberately does
not shadow the pre-existing `ReportsHandlerMixin` methods (`_get_plans`,
`_get_reports`, `_get_runs`, `_get_rul`, etc.) that already serve the GET
list/detail routes. The mixin's docstring (lines 1–6) records this
boundary: the pre-existing `temporal.py` owns decision-freshness /
mode-switch / twin-design from #862.

### 3. New schema elements (domain-agnostic)

Added to `src/ohm/graph/schema.py`:

- **Node types** (lines 96–99, under "Temporal planning layer (OHM-937)"):
  - `forecast` — a prediction with horizon, target, distribution, method
  - `plan` — time-bounded plan container (lightweight default-schema parallel to `topo_plans`)
  - `milestone` — named checkpoint inside a plan or prospect
- **Edge types** — L3 (lines 411–415) and L4 (lines 443–447):
  - `FORECAST_FOR` — forecast → target node
  - `SCENARIO_FOR` — scenario → target node or plan
  - `BASELINE_FOR` — baseline snapshot node → scenario/forecast
  - `ACTUALIZES` — observation/actual outcome → forecast (error tracking)
  - `DRIFT_FROM` — drift observation → plan/forecast it deviated from
- **Task statuses** (lines 580–583): `draft`, `resolved_hit`,
  `resolved_miss`, `resolved_ambiguous` — forecast resolution lifecycle.
- **SCHEMA_VERSION** bumped to `0.57.0` (line 1956).

These types are **domain-agnostic** — they live in the default
`VALID_NODE_TYPES` / `LAYER_EDGE_TYPES`, not in `SchemaConfig.topo()`. The
TOPO-specific tables (`topo_plans`, `topo_events`, etc.) remain TOPO
DomainTables (per ADR-040/041); the new node/edge types give non-TOPO
domains a lightweight temporal vocabulary without forcing the full TOPO
table set.

### 4. Migration is version-only (no DDL)

The `0.57.0` migration (lines 2845–2855) is a no-op `SELECT 1` — the new
node types, edge types, and task statuses are validated in application
code (`VALID_NODE_TYPES`, `LAYER_EDGE_TYPES`, `VALID_TASK_STATUSES`), not
via DDL. The version bump exists so agents can detect that temporal
planning support is available.

### 5. Dual routing registration

The server has two routing mechanisms. Both are updated:
- `_build_router()` `_RouteRegistry` — `r.add("POST", "/plan/create")`
  etc. (lines 994–1006)
- `_POST_EXACT` / `_GET_EXACT` dicts —
  `OhmHandler._POST_EXACT["/plan/create"] = "_post_plan_create"` etc.
  (lines 2986–2998)

The mixin is imported (line 1082) and added to the `OhmHandler` bases
(line 1086).

### 6. Schema JSON templates updated

`src/ohm/graph/templates/ohm.json` and
`src/ohm/graph/templates/beef_herd.json` updated to include the new node
types (`forecast`, `plan`, `milestone`) and edge types
(`FORECAST_FOR`, `SCENARIO_FOR`, `BASELINE_FOR`, `ACTUALIZES`,
`DRIFT_FROM`) in both L3 and L4 edge lists, for consistency with the
Python-defined `VALID_NODE_TYPES` / `VALID_LAYER_EDGE_TYPES`.

## Consequences

**Positive:**
- Any MCP-capable agent can now create and manage temporal plans, events,
  reports, runs, RUL assessments, scenarios, drift observations, and
  verification outcomes — the temporal planning layer is no longer
  write-dark.
- Domain-agnostic temporal vocabulary (`forecast`/`plan`/`milestone` +
  five edge types) is available to non-TOPO domains without adopting the
  full TOPO table set.
- `ohm_scenario_run` chains `query_compare_scenarios` →
  `query_counterfactual_cascade` in one MCP call, hiding the two-step
  orchestration from agents.
- `ohm_record_verification_outcome` closes the ADR-018 verification loop
  for temporal claims — agents can now record whether a forecast was
  validated or falsified by reality, feeding the 30d/365d decay split.
- No DDL migration risk — the version bump is a sentinel, not a schema
  change.

**Negative:**
- The full set of temporal tables (`topo_plans`, `topo_events`,
  `topo_reports`, `topo_runs`, `topo_rul_assessments`) is required for
  the write endpoints to function. The DEFAULT schema gets the new
  node/edge types but not the TOPO-specific tables — agents on a
  default-schema instance can create `forecast`/`plan`/`milestone` nodes
  but cannot use `ohm_plan_create` etc. without the TOPO schema.
- MCP tool count rises to 59, increasing the tool-discovery surface for
  agents (mitigated by ADR-043 profile `allowed_tools` filtering).
- Two routing mechanisms must be kept in sync on every new endpoint —
  forgetting one silently breaks either the registry path or the exact
  path.

## Alternatives considered

1. **Expose TOPO queries directly without MCP tools** — rejected. Agents
   need MCP-callable tools, not raw SQL access. The AGENTS.md "Primary
   Agent Interface" guidance is explicit: agents use the SDK/MCP, not
   direct table access. Raw SQL would also bypass validation,
   `created_by` attribution, and boundary enforcement (ADR-003).

2. **Add all temporal types to DEFAULT_SCHEMA tables** — rejected. TOPO
   tables (`topo_plans`, `topo_events`, etc.) are domain-specific and
   belong in `SchemaConfig.topo()` per ADR-040's domain DDL pattern. The
   new node/edge types *are* added to the default schema (they are
   domain-agnostic), but the TOPO tables remain TOPO-specific. Forcing
   them into the default schema would pollute the core ontology for every
   consumer.

3. **Create a separate temporal MCP server** — rejected. It would add
   deployment complexity (a second daemon to run, monitor, and
   authenticate), break the single-daemon model established by ADR-002
   (Quack) and ADR-015 (multi-tenancy), and fragment the agent's tool
   surface across two endpoints. The temporal tools belong alongside the
   existing 46 tools in the same `ohmd` process.

## References

- Issue: OHM-937 — Temporal planning MCP tool surface
- Prior work: ADR-041 (temporal event model pilot), ADR-040 (TOPO domain
  DDL tables), ADR-029 (TOON MCP transport — encoding reused by these
  tools), ADR-018 (verification loops — `ohm_record_verification_outcome`
  closes the loop for temporal claims)
- Source:
  - `src/ohm/graph/schema.py:96-99` (node types), `:411-415` & `:443-447`
    (edge types), `:580-583` (task statuses), `:1956` (SCHEMA_VERSION),
    `:2845-2855` (migration)
  - `src/ohm/mcp/tools.py:886-1096` (13 tool schemas)
  - `src/ohm/mcp/dispatch.py:369-459` (13 dispatch cases)
  - `src/ohm/server/handlers/temporal_planning.py:17` (handler mixin)
  - `src/ohm/server/server.py:994-1006` (router), `:1082` & `:1086`
    (mixin import + bases), `:2986-2998` (exact-route dicts)
  - `src/ohm/graph/templates/ohm.json`, `beef_herd.json` (template parity)
- Tests: `tests/test_temporal_planning_937.py` (46 tests — schema,
  dispatch, integration; marked `integration`)
