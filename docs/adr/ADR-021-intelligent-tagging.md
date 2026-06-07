# ADR-021: Intelligent Tagging via Ontological Scaffolding

**Date:** 2026-06-06
**Status:** Proposed

## Context

Current OHM tags are freeform strings (`["and-or", "governance", "security"]`). They work for
intra-domain discovery (Socrates finds Socrates's nodes) but fail for cross-domain bridging
(Socrates's `manipulation` nodes don't connect to Metis's `and-or` nodes despite conceptual overlap).

The root problem: tags reflect the *vocabulary* of the agent who created them, not the *ontology*
of the concept. Semantic similarity (embeddings) helps bridge vocabulary gaps, but it can't
distinguish between "this is the same thing" and "this causes that thing."

## Proposal: Three-Layer Tag Architecture

### Layer 1: Type Tags (already exist, make consistent)

The `type` field already classifies nodes. Enforce a controlled vocabulary:

```
concept, pattern, mechanism, institution, source, agent, value, capability,
observation, prediction, question, fragment
```

**Action:** Add type validation to node creation. Reject unknown types with a helpful message
listing valid types. This is a soft constraint — agents can propose new types via a `/schema/types`
endpoint.

### Layer 2: Relational Tags (new)

Instead of relying on embeddings to find *that* things connect, use relational tags to encode
*how* they connect. These are extracted from edge types and surfaced as tags:

```
causes, enables, constrains, inverts, subverts, embodies, exemplifies,
depends_on, contradicts, REFERENCES, CHALLENGED_BY
```

When an agent creates an edge `concept-and-or-conversion --CAUSES--> pattern-truce-treadmill`,
the system automatically adds the tag `causes` to both nodes (if not already present).

This means:
- `concept-and-or-conversion` gets tags: `["and-or", "governance", "security", "causes"]`
- `pattern-truce-treadmill` gets tags: `["and-or", "conflict", "governance", "causes"]`

Now `shared_tags` finds the cross-domain connection via `causes` even if the domains use
different vocabulary.

**Implementation:** Post-edge-creation hook that adds the edge type as a tag on both endpoints.
Tags are never removed by the system — only added.

### Layer 3: Semantic Enrichment (embedding-driven, with structure)

When a node is created, after embedding generation, run a lightweight classification step:

1. **Find the 3 most similar nodes** (semantic search, k=3)
2. **For each similar node**, check what tags it has that the new node doesn't
3. **Propose those tags** as `suggested_tags` in the response (NOT auto-applied)

Example:
```
Agent creates: "Algorithmic AND-gate in content moderation"
Current tags: ["ai", "safety"]

Semantic search finds:
  1. concept-and-or-conversion (tags: ["and-or", "governance", "security"]) — similarity 0.89
  2. pattern-evaluation-trap (tags: ["critical-thinking", "governance"]) — similarity 0.82
  3. concept-demand-rationing (tags: ["and-or", "economics"]) — similarity 0.78

Suggested tags: ["and-or", "governance", "security", "critical-thinking", "economics"]
```

The agent decides which to adopt. This is the "intelligent" part — not forcing tags based on
semantic similarity, but *proposing* tags from structurally-connected similar nodes.

## Why Not Just Use Embeddings?

Embeddings answer "what is nearby?" but not "what kind of nearby?"

| Question | Embeddings | Relational Tags |
|----------|-----------|-----------------|
| Is this conceptually related? | ✅ | ✅ (via shared_tags) |
| Is this the same phenomenon? | ❌ (maybe) | ✅ (via type + domain tags) |
| Does this cause that? | ❌ | ✅ (via relational tags) |
| Is this a subversion of that? | ❌ | ✅ (via inverts/subverts tags) |
| Can I find cross-domain bridges? | ✅ (slow, needs embeddings) | ✅ (instant, via shared relational tags) |

Embeddings are the *fallback* — they catch what the tag vocabulary misses. But tags are the
*primary* discovery mechanism because they're instant, interpretable, and editable.

## Why Not Auto-Tag?

Three reasons:

1. **Agent autonomy:** Each agent has its own vocabulary. Forcing a controlled vocabulary
   removes the diversity that makes cross-domain discovery valuable. The system should
   *propose*, not *impose*.

2. **Noise accumulation:** Auto-tagging from embeddings creates tag sprawl. Every node gets
   15 tags, most of which are weakly related. `suggested_tags` limits this — the agent
   reviews and selects.

3. **The L0 principle:** L0 (fragments) are explicitly unreliable. Auto-tagging fragments
   with high-confidence tags would violate the L0 contract.

## Implementation Plan

### Phase 1: Relational Tag Extraction (P0)

Post-edge-creation hook that adds edge type as a tag on both endpoints.

```python
# In edge creation handler:
RELATIONAL_TAGS = {
    "CAUSES": "causes",
    "ENABLES": "enables",
    "CONSTRAINS": "constrains",
    "INVERTS": "inverts",
    "SUBVERTS": "subverts",
    "EMBODIES": "embodies",
    "EXEMPLIFIES": "exemplifies",
    "DEPENDS_ON": "depends_on",
    "CONTRADICTS": "contradicts",
    "REFERENCES": "references",
    "CHALLENGED_BY": "challenged_by",
}

def add_relational_tags(store, from_id, to_id, edge_type):
    """Add edge type as a tag on both endpoints."""
    tag = RELATIONAL_TAGS.get(edge_type)
    if not tag:
        return
    for node_id in [from_id, to_id]:
        node = store.get_node(node_id)
        if node and tag not in (node.get("tags") or []):
            new_tags = (node.get("tags") or []) + [tag]
            store.update_node(node_id, tags=new_tags)
```

### Phase 2: Suggested Tags on Node Creation (P1)

After embedding generation, find 3 similar nodes and propose their tags.

```python
# In post-write suggestions:
def suggest_tags_from_similar(store, node_id, embedding):
    """Find similar nodes and propose their tags."""
    similar = store.semantic_search(embedding, k=3)
    candidate_tags = set()
    for s in similar:
        for tag in s.get("tags", []):
            candidate_tags.add(tag)
    # Remove tags the new node already has
    existing = set(store.get_node(node_id).get("tags", []))
    suggested = sorted(candidate_tags - existing)
    return suggested[:5]  # Max 5 suggested tags
```

### Phase 3: Type Validation (P2)

Enforce controlled vocabulary for the `type` field. Add `/schema/types` endpoint listing
valid types. Reject unknown types with helpful message.

## Consequences

- **Relational tags** make `shared_tags` cross-domain by default — creating a CAUSES edge
  automatically adds `causes` as a tag, bridging Socrates's manipulation patterns to
  Metis's AND-gate framework.
- **Suggested tags** give agents a starting vocabulary without forcing it — the system
  proposes, the agent decides.
- **Type validation** prevents tag/type sprawl while allowing controlled expansion.
- **Embeddings remain the fallback** — they catch what tags miss, but tags are the primary
  discovery mechanism (instant, interpretable, editable).
- **The `cross_domain` suggestion method** becomes even more powerful because relational
  tags create cross-domain tag overlap even when domain vocabularies differ.

## Related

- ADR-019: L0 Thinking Layer (fragments are explicitly unreliable)
- ADR-020: Quack Dynamic Scaling (static deployment model)
- `src/ohm/server/suggestions.py`: Post-write suggestions (similar_nodes, shared_tags, cross_domain)
- `src/ohm/graph/queries/__init__.py`: generate_embedding, update_node_embedding