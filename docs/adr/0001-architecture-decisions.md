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