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