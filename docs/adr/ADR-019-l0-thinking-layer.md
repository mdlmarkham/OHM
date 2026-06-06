# ADR-019: L0 Thinking Layer — Fragments as Nodes

**Status:** Accepted
**Date:** 2026-06-06
**Author:** Metis

## Context

Agents jump straight to L3 writes. The messy thinking that produces syntheses — hunches, fragments, half-connections — never enters OHM. It stays in zettelkasten notes or evaporates entirely (see `/docs/l0-design.md`).

Two storage designs were considered:

1. **Separate `ohm_fragments` table** — 16-column table with salience, session_id, promoted_to, embedding, etc.
2. **`ohm_nodes` with `type='fragment'`** — Reuse existing node infrastructure, L0 edges via `ohm_edges` with `layer='L0'`.

Self-review (`/docs/l0-design-critique.md`) identified the separate table as over-engineering:
- 16 columns for a "nearly free" write contradicts the design principle
- `ohm_fragment_links` duplicates `ohm_edges` — layer distinction is sufficient
- Search, semantic search, and edge infrastructure all work on `ohm_nodes` for free
- Promotion pipeline is speculative; `promoted_to` field implies linear flow that doesn't match real thinking

## Decision

Fragments are stored as `ohm_nodes` with `type='fragment'`. L0 edges use existing `ohm_edges` with `layer='L0'`.

No new tables. No migration. The existing schema already supports this — `fragment` is added to `VALID_NODE_TYPES` and `EXEMPT_CROSS_LINK_NODE_TYPES`.

### L0 Edge Types

```
LAYER_EDGE_TYPES["L0"] = frozenset({"CONTEXT_OF", "INSPIRED_BY", "CONTRADICTS_FRAG", "REFINES_FRAG"})
```

- `CONTEXT_OF`: Fragment was written while thinking about this node (auto-linked or explicit)
- `INSPIRED_BY`: Fragment was inspired by another node or fragment
- `CONTRADICTS_FRAG`: Fragment contradicts another fragment (L0-specific, not the L3 CONTRADICTS)
- `REFINES_FRAG`: Fragment refines or extends another fragment

### L0 Layer Description

```
"L0": "Thinking — Fragments, hunches, raw associations; unreliable, auto-linked"
```

### Cross-link Policy

`fragment` is in `EXEMPT_CROSS_LINK_NODE_TYPES`. L0 nodes can exist as bare stubs — the whole point is zero-friction writes. Linking happens organically or through the `/scratch` endpoint's optional `connects_to` parameter.

### L0 Exclusion from Default Queries

Per design principle 3 ("L0 is explicitly unreliable"), L0 nodes are excluded from `stats()` and neighborhood queries by default. This is implemented separately (OHM-a5rz.6).

## Consequences

### Positive
- Zero schema migration — `fragment` node type works immediately
- Full search, edge, and query infrastructure available at no cost
- L0 edges participate in the same `ohm_edges` table — one query path, one mental model
- Promotion is organic: when a fragment gains structure, the agent creates a higher-layer node and links it

### Negative
- `ohm_nodes` table grows with fragments — but DuckDB handles this fine
- No salience/decay tracking in v1 — deferred until practice proves it's needed
- No session auto-context — deferred; agents can pass `connects_to` explicitly

### Risks
- L0 fragments could clutter `ohm_nodes` if agents produce them in volume. Mitigated by: (1) L0 exclusion from default queries, (2) text search as the primary retrieval mechanism, (3) future decay if needed.

## References

- Design doc: `/docs/l0-design.md`
- Self-review critique: `/docs/l0-design-critique.md`
- Related: OHM-a5rz (L0 epic), OHM-a5rz.1 (this ADR), OHM-a5rz.2 (fragment node type), OHM-a5rz.3 (L0 edge types)
