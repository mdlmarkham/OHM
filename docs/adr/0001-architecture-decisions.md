# Architecture Decision Records

## ADR-001: DuckDB as Local Cache

**Status:** Accepted

**Context:** Each agent needs a local cache for working memory that's fast, embeddable, and supports recursive CTEs for graph traversal.

**Decision:** Use DuckDB as the local cache. It's embedded (no server dependency), supports full SQL including recursive CTEs, and has native JSON support for metadata.

**Consequences:**
- Zero-dependency for single-agent use
- Full SQL power for graph queries
- DuckLake as shared backend when multi-agent coordination is needed
- Quack HTTP protocol for concurrent access

## ADR-002: Challenge Edges, Not Modification

**Status:** Accepted

**Context:** When one agent disagrees with another's edge, the boundary rule must prevent overwriting while allowing dissent.

**Decision:** Challenges create new edges (CHALLENGED_BY, SUPPORTS, REFINES, CONTRADICTS) that reference the original. The original edge is never modified by anyone except its owner.

**Consequences:**
- The graph accumulates perspectives, never collapses them
- Confidence audit trails are complete
- Owner can update their own confidence
- No agent can suppress another's perspective

## ADR-003: JSON Arrays for Tags and Agent State

**Status:** Accepted

**Context:** DuckDB doesn't support indexing on VARCHAR[] (list) columns.

**Decision:** Store tags and agent state arrays as JSON. Use JSON functions for containment queries.

**Consequences:**
- No index on tags (acceptable for current scale)
- `json_contains()` and string ILIKE for tag queries
- Can migrate to native arrays if DuckDB adds index support

## ADR-004: Timestamp Handling

**Status:** Accepted

**Context:** DuckDB's `DEFAULT CURRENT_TIMESTAMP` and `DEFAULT now()` work in DDL but not in parameterized INSERT ON CONFLICT statements.

**Decision:** Generate timestamps in Python (`datetime.now(timezone.utc)`) and pass them explicitly in all write operations.

**Consequences:**
- All timestamps are UTC
- No reliance on database-generated timestamps in application code
- Consistent behavior across local and Quack modes

## ADR-005: Self-Documenting CLI as Agent Interface

**Status:** Accepted

**Context:** Agents need to interact with the knowledge graph without writing raw SQL. The interface must be discoverable, consistent, and machine-readable.

**Decision:** The CLI (`ohm`) is the primary agent interface. Every command has `--help` for discovery and `--format json` for machine-readable output. The SDK (`ohm.sdk.Graph`) wraps the same queries for programmatic use.

**Consequences:**
- Agents call `ohm graph ...` or use `Graph` methods, never raw SQL
- `docs/cli.md` is the canonical reference for all commands
- New queries are added to `queries/` first, then wrapped in `sdk.py` and CLI
- Every command supports both human and JSON output

## ADR-006: Advisory Schema with Graduated Enforcement

**Status:** Accepted

**Context:** OHM's schema (node types, edge types, layers) is currently advisory — any node_type or edge_type can be created without validation. This is intentional for early-stage exploration, but as a domain matures (e.g., cattle operations with CALVING_EVENT, HOOF_SCORE, PARASITE_LOAD), stricter enforcement becomes desirable.

**Decision:** The schema remains advisory by default. Enforcement is graduated through `SchemaConfig`:

1. **Advisory (default):** `SchemaConfig()` — all types accepted, warnings logged for unknown types. This is the current behavior.
2. **Lenient:** `SchemaConfig(enforce_types=True, allow_unknown=True)` — known types validated, unknown types accepted with warnings.
3. **Strict:** `SchemaConfig(enforce_types=True, allow_unknown=False)` — only registered types accepted. Unknown types raise `ValidationError`.

Schema evolution is handled through the existing migration framework (`MIGRATIONS` list in `schema.py`). Adding new types is a migration that registers them in `SchemaConfig`. The `ohm graph upgrade` command applies migrations.

**Consequences:**
- New projects start in advisory mode — no friction for exploration
- Mature domains opt into strict mode via `SchemaConfig`
- Schema migrations are versioned and auditable through `ohm_meta`
- `SchemaConfig.topo()` already demonstrates domain-specific schemas
- The migration from advisory → strict is itself a schema migration

## ADR-007: Probability and Confidence as Separate Edge Attributes

**Status:** Accepted

**Context:** OHM originally used `confidence` as the sole quantitative attribute on edges. However, supply chain risk modeling and cybersecurity incident response revealed that two distinct concepts were being conflated:

- **Confidence:** How sure is the agent about this claim? (epistemic — "I'm 90% sure this supplier relationship exists")
- **Probability:** How likely is the event to occur? (aleatoric — "There's a 5% chance this supplier will fail this quarter")

A supplier edge with `confidence=0.2` is ambiguous: does it mean the agent is unsure about the relationship, or that disruption is 20% likely? These are semantically different and must be stored separately.

**Decision:** Add `probability FLOAT` as a distinct column on `ohm_edges`, separate from `confidence`. When `probability` is NULL, `confidence` serves as the default probability for cascade computations. The `EXPECTED_LIKELIHOOD` edge type (L3) explicitly carries probability claims.

**Consequences:**
- `cascade_scenario()` uses `COALESCE(probability, confidence)` for failure propagation
- `what_if()` treats the edge's probability as the event likelihood
- Backwards compatible: existing edges with only `confidence` continue to work
- Schema migration 0.5.0 added the `probability` column

## ADR-008: NEGATES Edge Type for Negative Evidence

**Status:** Accepted

**Context:** Medical diagnosis requires the ability to rule out conditions based on absent findings. "Fever absent" doesn't just lower the probability of malaria — it actively rules it out. This is semantically different from a low-confidence SUPPORTS edge.

Without NEGATES, an agent must either:
1. Create a low-confidence edge (which doesn't capture the semantics of "rules out")
2. Not create any edge (which loses the information entirely)

**Decision:** Add `NEGATES` as an L3 edge type. `rules_out()` is the SDK convenience method. `differential_diagnosis()` automatically excludes NEGATES-ruled-out conditions from candidate rankings.

**Consequences:**
- NEGATES edges are first-class citizens in the graph, not just low-confidence edges
- `differential_diagnosis()` surfaces ruled-out conditions separately with their ruling evidence
- `compound_confidence()` with correlation parameter handles the case where multiple findings from the same modality shouldn't double-count
- NEGATES is domain-agnostic: works for medical (rules out diagnosis), cybersecurity (rules out false positive), and any scenario with negative evidence

## ADR-009: Observation Type Extensibility

**Status:** Accepted

**Context:** OHM's observation system started with simple numeric measurements (temperature, NDVI, head count). As scenarios expanded, new observation types emerged: sentiment scores (customer support), binary outcomes (cybersecurity true/false positives), diagnostic confidences (medical), and demand multipliers (retail).

Hardcoding observation types in the schema would require a migration for every new scenario. A flexible type system allows domain-specific observations without schema changes.

**Decision:** Observation types are validated against a registry but not constrained by DDL. The `type` field on `ohm_observations` is a VARCHAR that can hold any domain-appropriate value. `SchemaConfig` maintains the registry of known types per domain. New types are registered through `SchemaConfig` without requiring DDL migrations.

**Consequences:**
- Domain-specific observation types (sentiment, vibration, hoof_score, ndvi) work without schema changes
- `SchemaConfig` provides the registry for validation in strict mode
- The `metadata` JSON field carries type-specific payload (units, modality, source)
- Observation types are discoverable through `graph.schema()`