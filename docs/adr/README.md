# OHM Architecture Decision Records

## ADR-001: DuckDB + Recursive CTEs over DuckPGQ for Graph Traversal

**Date:** 2026-05-16
**Status:** Decided

### Context

OHM needs graph traversal (neighborhood queries, impact analysis, path finding). DuckPGQ provides SQL/PGQ `MATCH` syntax but is a community extension maintained by a research group with no release tags and breaking changes on DuckDB upgrades. Kuzu was acquired by Apple (Oct 2025, repo archived) and is not viable.

### Decision

Use recursive CTEs for all production graph queries. DuckPGQ remains optional for ad-hoc exploration.

### Consequences

- Zero-dependency graph queries (standard SQL, works through Quack, survives DuckDB upgrades)
- Bounded-depth traversals (1-5 hops) are well within CTE performance at OHM's scale
- Need to implement ~7 parameterized CTE views for common query patterns
- DuckPGQ `MATCH` syntax is nicer to write but cannot be relied on for production

---

## ADR-002: Quack for Concurrent Access

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents need to read and write the knowledge graph simultaneously. DuckDB is single-writer by default.

### Decision

Use DuckDB's Quack protocol (HTTP-based, token-authenticated, multi-reader/multi-writer). Requires a persistent daemon (`ohmd`) to own the DuckDB file and serve connections.

### Consequences

- Requires `ohmd` daemon (systemd service, auto-restart, health check)
- All agents connect via Quack instead of direct file access
- Token auth with role-based access control per agent
- Quack currently ships from `core_nightly` — must pin binary for production

---

## ADR-003: Agent-Owned Edges with Challenge Semantics

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents will create L3 (Knowledge) and L4 (Prospect) edges about the same topics. Averaging confidence scores would destroy individuality. Allowing overwrites would lose perspectives.

### Decision

Every L3/L4 edge has a single owner (`created_by`). Other agents can create CHALLENGED_BY, SUPPORTS, or DERIVED_FROM edges that reference the original, but cannot modify or delete it.

### Consequences

- The graph accumulates perspectives without collapsing them
- Confidence scores reflect the owning agent's judgment
- Humans see the full picture including disagreements
- Challenge edges create productive tension, not consensus averaging
- Requires `created_by` and `updated_by` columns on all L3/L4 tables

---

## ADR-004: Three-Layer Data Architecture — per-agent local cache, shared DuckLake, private scratch
- ADR-012: Per-Agent Local DuckDB Cache — `OhmStore.for_agent()` with zero-latency local access

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents need fast local access to their working set, shared access to the canonical graph, and private space for unfinished work.

### Decision

Three layers:
1. **Local DuckDB cache** — per-agent working memory, synced from DuckLake on heartbeat
2. **DuckLake shared backend** — canonical graph, time travel, change feed, agent state
3. **Private** — agent-only notes below confidence threshold, personal observations, scratch calculations

### Consequences

- Cache invalidation via DuckLake `table_changes()` (incremental, not full sync)
- Private layer never promoted automatically; per-agent threshold for promotion
- Change feed carries `agent_name` attribution on every write
- Time travel enables "what did we know at time T?" queries

---

## ADR-005: Self-Documenting CLI as Agent Interface

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents should not need to know SQL, DuckDB internals, CTE structure, or Quack protocol details to use the graph.

### Decision

Package the entire stack as `ohm` CLI. Agents call `ohm graph write`, `ohm graph listen`, `ohm state show`, not raw SQL.

### Consequences

- Implementation can evolve (CTEs→DuckPGQ, file→Quack, Kuzu→DuckLake) without breaking agents
- `ohm graph schema` and `ohm graph layers` are living documentation
- `ohm graph listen --since last-check` wraps the change feed
- The CLI is the contract between agents and the graph

---

## ADR-006: Advisory Schema with Graduated Enforcement

**Date:** 2026-05-17
**Status:** Decided

### Context

OHM's schema (node types, edge types, layers) is currently advisory — any node_type or edge_type can be created without validation. This is intentional for early-stage exploration, but as a domain matures, stricter enforcement becomes desirable.

### Decision

The schema remains advisory by default. Enforcement is graduated through `SchemaConfig`: advisory (default), lenient (known types validated, unknown accepted), strict (only registered types accepted). Schema evolution is handled through the existing migration framework.

### Consequences

- New projects start in advisory mode — no friction for exploration
- Mature domains opt into strict mode via `SchemaConfig`
- Schema migrations are versioned and auditable through `ohm_meta`

---

## ADR-007: Schema Evolution and Type Governance for Domain Expansion

**Date:** 2026-05-17
**Status:** Decided

### Context

As OHM is applied to new domains (cattle operations, industrial monitoring), the schema must accommodate domain-specific types without polluting the core ontology or breaking existing queries.

### Decision

Domain types are isolated through `SchemaConfig` instances. Each domain extends — but never overrides — the base OHM types. Types follow a three-stage lifecycle: experimental (advisory) → registered (lenient) → canonical (strict). Promotion to canonical requires a schema migration.

### Consequences

- No schema pollution — domain types stay in their `SchemaConfig` until promoted
- Domain autonomy — each domain controls its own type lifecycle
- Cross-domain visibility — all domains share the same physical tables
- Migration audit trail — every type promotion is recorded in `MIGRATIONS`

---

## ADR-008: Probability and Confidence Model

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM edges carry a `confidence` field (0–1) representing how certain the creating agent is about the relationship. Nodes accumulate `observations` with their own confidence values. Multiple agents may create independent edges about the same relationship, and a single node may have many observations from different sources. The system needs a principled way to combine these values — especially when observations are correlated (e.g., two blood tests from the same lab) versus independent (e.g., imaging + blood work).

Naive averaging destroys agent individuality (ADR-003). Simple multiplication over-counts correlated evidence. The system must support both medical diagnosis (where correlation between findings matters) and general knowledge graphs (where independent evidence compounds).

### Decision

Three-tier confidence model:

1. **Edge confidence** — single value per edge, owned by the creating agent (ADR-003). Not averaged or merged.
2. **Compound confidence** — combines multiple confidence values with explicit `correlation` parameter:
   - `correlation=0.0` (independent): P(at least one) = 1 − Π(1 − pᵢ). Evidence compounds multiplicatively.
   - `correlation=1.0` (perfectly correlated): result = max(pᵢ). Only the strongest evidence matters.
   - `0.0 < correlation < 1.0`: linear interpolation between independent and correlated results.
3. **Composite score** — per-node aggregate combining observation scores and evidence-chain confidence, with configurable weights (`observation_weight`, `evidence_weight`). Supports arithmetic (default, backwards-compatible) and geometric mean methods.

The `probability` column on edges (added in schema v0.5.0) is distinct from `confidence`: probability represents the likelihood of the described relationship occurring in the world (e.g., "70% chance this supplier fails"), while confidence represents the agent's certainty about the claim (e.g., "I'm 90% sure this probability estimate is correct").

### Consequences

- Agents retain ownership of their individual confidence judgments
- Correlated observations don't artificially inflate compound confidence
- Medical diagnosis can model same-modality correlation vs. cross-modality independence
- `probability` and `confidence` serve different analytical purposes and should not be conflated
- The interpolation formula is simple and auditable, but not Bayesian — future work could add prior-based updating

---

## ADR-009: NEGATES Edge Type for Ruling Out Conditions

**Date:** 2026-05-19
**Status:** Decided

### Context

In medical diagnosis, a finding can *rule out* a condition (e.g., "normal WBC count rules out bacterial infection"). In cybersecurity, a forensic result can eliminate a threat hypothesis. In supply chain, a confirmed delivery negates a "delayed" claim. These are not challenges (which question confidence) or contradictions (which assert the opposite) — they *remove a candidate from consideration entirely*.

OHM already has CHALLENGED_BY (questions confidence) and CONTRADICTS (asserts opposite). Neither captures the "ruled out" semantics cleanly. Using CONTRADICTS for this purpose conflates "I believe the opposite" with "this is eliminated from consideration."

### Decision

Add `NEGATES` as an L3 edge type. Semantics:

- `A —NEGATES→ B` means "the existence/truth of A eliminates B from consideration"
- Confidence on the NEGATES edge represents how certain the agent is that A rules out B
- NEGATES is agent-owned (ADR-003): multiple agents can independently negate or not
- `differential_diagnosis()` uses NEGATES edges to exclude ruled-out candidates from results
- Ruled-out candidates appear in results with `ruled_out=True` and `ruled_out_by=[edge_ids]` — they are not deleted, just flagged

NEGATES is placed in L3 (Knowledge) because it represents an agent's judgment about the relationship between two concepts, not a structural or flow relationship.

### Consequences

- Clean separation: CHALLENGED_BY questions confidence, CONTRADICTS asserts opposite, NEGATES eliminates from consideration
- `differential_diagnosis()` returns ruled-out candidates with provenance, not silently filtered
- Works across domains: medical (findings rule out conditions), cybersecurity (forensics eliminate hypotheses), supply chain (confirmations negate delay claims)
- NEGATES edges are challengeable — another agent can CHALLENGED_BY a NEGATES edge if they disagree with the ruling-out

---

## ADR-010: Urgency on Edges and Priority on Nodes

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM needs temporal reasoning for time-sensitive domains: customer support (SLA breaches), cybersecurity (incident response), medical (deteriorating conditions), supply chain (expiring inventory). These domains need to distinguish "how important is this thing?" (priority) from "how urgently does this relationship need attention?" (urgency).

Priority is an intrinsic property of a node — a P0 incident is always P0 regardless of which edge you approach it from. Urgency is a property of the relationship — "this ticket was escalated TO tier-2" carries urgency independent of the ticket's priority. A P3 ticket can have a critical-urgency escalation edge.

### Decision

Separate priority and urgency into different entities:

- **`priority`** on `ohm_nodes` — intrinsic importance of the node itself. Values: P0 (critical), P1 (high), P2 (medium), P3 (low), P4 (informational). Validated against `VALID_PRIORITY`.
- **`urgency`** on `ohm_edges` — time-sensitivity of the relationship. Values: low, normal, high, critical. Validated against `VALID_URGENCY`.
- `escalate()` sets `urgency="high"` on the ESCALATED_TO edge AND sets `priority="P1"` on the ticket node — both the relationship and the node reflect the escalation.
- `urgent_changes()` filters the change feed for edges with urgency ≥ the specified threshold.

Priority and urgency are advisory by default (ADR-006) — they are validated when provided but not required.

### Consequences

- Priority and urgency serve distinct analytical purposes and can evolve independently
- A node can be high-priority without any urgent edges (important but stable)
- An edge can be critical-urgency between low-priority nodes (time-sensitive but not important)
- `escalate()` correctly updates both dimensions
- Query patterns: "show me all P0 nodes" (priority filter) vs. "show me all critical-urgency edges" (urgency filter) vs. "show me P0 nodes with critical-urgency edges" (intersection)
- Future: priority could be derived from composite scoring; urgency could be auto-set by temporal decay

---

## ADR-011: Observation Type Extensibility

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM's `observe()` method records observations against nodes with a required `obs_type` field. The base schema defines `VALID_OBSERVATION_TYPES = {anomaly, measurement, pattern, challenge, support, sentiment}`. As OHM expands to new domains (industrial monitoring, financial analysis, environmental tracking), each domain needs domain-specific observation types: vibration/temperature/pressure for TOPO, volatility/spread for finance, pH/dissolved_oxygen for environmental.

The question is: should observation types be an open string (any value accepted), a closed set (only registered types), or extensible with validation?

### Decision

Observation types follow the same graduated enforcement model as node types and edge types (ADR-006/007):

1. **Advisory (default)** — `VALID_OBSERVATION_TYPES` is defined in `schema.py` as a `frozenset`. The `observe()` SDK method validates against it, raising `ValueError` for unknown types. This prevents typos and ensures query consistency.
2. **Domain extension via `SchemaConfig`** — Each `SchemaConfig` instance can define its own `observation_types` set. The TOPO schema extends the base with `{vibration, temperature, pressure, flow_rate, voltage, current, rpm, level}`. Custom domains add their own types the same way.
3. **Three-stage lifecycle** (per ADR-007) — experimental types are added to a domain's `SchemaConfig` (advisory), registered types are validated in lenient mode, canonical types require a schema migration to add to `VALID_OBSERVATION_TYPES`.

The `observation_sources` field follows the same pattern with `VALID_OBSERVATION_SOURCES` and `SchemaConfig.observation_sources`.

### Consequences

- Observation types are validated, preventing typos and ensuring downstream queries can group by type
- Domains extend observation types through `SchemaConfig`, not by modifying the base schema
- The TOPO schema demonstrates the extension pattern with 8 industrial observation types
- Adding a new base observation type (e.g., "forecast") requires updating `VALID_OBSERVATION_TYPES` and a schema migration
- The `observe()` SDK method and CLI validate against the active `SchemaConfig`, not the global constant
- Future: observation types could be stored in a database table for runtime registration (currently compile-time only)

---

## ADR-012: Per-Agent Local DuckDB Cache

**Date:** 2026-05-19
**Status:** Accepted

### Context

OHM uses a single `ohmd` daemon that owns the DuckDB file and serves all agents via HTTP REST API. This creates a single-writer bottleneck — every read and write goes through HTTP, adding latency and creating a single point of failure. Each agent needs fast local access to the knowledge graph for neighborhood queries, semantic search, graph analytics, and deep content retrieval.

### Decision

Each agent gets its own local DuckDB file for zero-latency reads and writes, with periodic sync to a shared DuckLake mirror. `OhmStore.for_agent(agent_name, ducklake_path=...)` creates a per-agent store at `~/.ohm/agents/{name}/ohm.duckdb`. Agents read/write locally (no HTTP, no network) and sync with DuckLake on heartbeat via `sync_heartbeat()`.

### Consequences

- **Zero-latency reads**: All queries are local DuckDB operations (microseconds, not milliseconds)
- **No single point of failure**: If ohmd crashes, agents continue working locally
- **No daemon dependency**: Agents can read/write without ohmd running
- **Offline capability**: Agent works disconnected, syncs when reconnected
- **Same API**: `OhmStore.for_agent()` returns the same `OhmStore` object
- **Eventual consistency**: Changes from other agents visible only after sync_heartbeat()
- **DuckLake lock**: Only one process writes to DuckLake at a time; agents sync through daemon or take turns
- **ohmd becomes optional**: Still useful for HTTP-only clients and change feed
---

## ADR-013: Value of Information for Knowledge Graphs

**Date:** 2026-05-20
**Status:** Proposed

### Context

OHM knows which nodes are uncertain but not which uncertainties matter for decisions. Edge probability fields are unpopulated, making the Bayesian inference stack return empty results. The elicitation problem — how to get principled probability estimates from subjective judgment — needs a protocol.

### Decision

Use PERT three-point estimation (P05/P50/P95) as the elicitation protocol for edge probabilities. The derived PERT mean populates `probability` for Bayesian CPTs; the derived variance feeds VoI ranking (uncertainty × decision sensitivity = research priority). Add decision nodes with utility metadata. Implement `/voi` endpoint that traces causal paths backward from decision nodes to identify which observations would most reduce decision uncertainty.

### Consequences

- Bayesian inference works with PERT-derived CPTs
- VoI prioritizes research by decision impact, not just gap size
- Decision nodes encode "how much does being wrong matter?"
- Agents can self-optimize: research what matters, not what's easy
- GIGO risk mitigated by conservative initial ranges and observation updates

---

## ADR-015: Multi-Tenancy — Single-Process Isolated DuckDB Instances

**Date:** 2026-05-24
**Status:** Accepted

### Context

OHM currently runs as a single-tenant system. The TeamWork AI platform needs to serve multiple customers from a single deployment with complete data isolation.

### Decision

One `ohmd` process, N isolated DuckDB files, per-tenant LRU cache. Customer API keys resolve to tenant instances. Domain templates replace `topod`. Feature flag (`ENABLE_MULTI_TENANCY`) ensures backward compatibility.

### Consequences

- Strong isolation — each tenant's data is a separate DuckDB file
- Domain flexibility — each tenant uses a different SchemaConfig
- Economical — one process, LRU cache manages memory
- Single-writer serialization per tenant (concurrent reads OK)
- Horizontal scaling path: consistent-hash router + N ohmd instances
- See [full ADR](0015-multi-tenancy.md)
