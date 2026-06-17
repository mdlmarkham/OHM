# ADR-026: Context Gates — AND-Gate Confidence from Combined Context

**Date:** 2026-06-17
**Status:** Proposed
**Author:** Metis
**Tags:** context-gate, confidence, AND-gate, layer-promotion, orient, discovery

## Context

The 7-layer AND→OR convergence pattern (Understand-Anything → OrionBelt → Bank Teller → Anthropic → OKF → Version Control → Context Management) reveals a structural property that OHM doesn't currently exploit:

**The CNC experiment proved:** telemetry alone = medium confidence, history alone = medium confidence, both = **HIGH** confidence. The AND-gate of combined context produces a measurable confidence lift that neither context provides alone.

OHM currently computes `compound_confidence()` on observations and `effective_layer()` on nodes, but neither measures the **context completeness** of a node — whether it has observations *and* pattern history *and* source attribution, and how much confidence each context layer contributes.

This is also the structural answer to the video's question: "What you give an agent matters more than how powerful it is." We need OHM to tell agents *which context layers are missing*, not just *what confidence the node has*.

## Decision

### 1. Context Gate Endpoint

Add `GET /context-gate/{node_id}` that computes confidence at each context layer and returns the delta between them.

This is **not a new computation engine**. It reuses existing `compound_confidence()`, `effective_layer()`, and `chain_validity()` — it just computes them over different context subsets.

**Context layers (matching the CNC experiment):**

| Layer | Name | Computes confidence from | Missing context signal |
|-------|------|------------------------|----------------------|
| R1 | Observations only | Direct observations on this node, no edge context | "Has signal but no pattern history" |
| R2 | Pattern only | Edge context from connected nodes (their confidence, observations) | "Has patterns but no current signal" |
| R3 | Combined | Both observations AND edge context (the AND-gate) | "High confidence synthesis" |

**Implementation** — extend the existing `_get_confidence()` handler:

```python
# In handlers/graph.py — extend _get_confidence to support context layers

def _compute_context_gate(self, node_id: str) -> dict:
    """Compute confidence at each context layer for a node.
    
    Reuses compound_confidence() and chain_validity() with different
    observation subsets to measure the confidence lift from context combination.
    """
    from ohm.graph.methods import compound_confidence
    from ohm.graph.constraints import chain_validity, count_observations, count_sources
    
    conn = self.current_store.read_conn
    
    # R1: Observations only — direct evidence, no pattern history
    obs_rows = conn.execute(
        "SELECT value, sigma, source, created_by, created_at FROM ohm_observations "
        "WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC",
        [node_id]
    ).fetchall()
    observations = [
        {"confidence": max(0, min(1, r[0])), "source": r[2], 
         "created_by": r[3], "created_at": str(r[4])}
        for r in obs_rows
    ]
    
    r1_result = compound_confidence(observations, use_diversity_correlation=True)
    r1_confidence = r1_result["compound_confidence"]
    
    # R2: Pattern only — edge context, no direct observations
    # Get confidence of connected nodes (via L2/L3 edges)
    edge_nodes = conn.execute(
        "SELECT DISTINCT CASE WHEN e.from_node = ? THEN e.to_node ELSE e.from_node END AS neighbor "
        "FROM ohm_edges e "
        "WHERE (e.from_node = ? OR e.to_node = ?) AND e.deleted_at IS NULL "
        "AND e.layer IN ('L2', 'L3')",
        [node_id, node_id, node_id]
    ).fetchall()
    neighbor_ids = [r[0] for r in edge_nodes]
    
    # Collect observations from neighbors (pattern context)
    pattern_obs = []
    for nid in neighbor_ids[:20]:  # Limit for performance
        n_obs = conn.execute(
            "SELECT value, sigma, source, created_by, created_at FROM ohm_observations "
            "WHERE node_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 5",
            [nid]
        ).fetchall()
        pattern_obs.extend([
            {"confidence": max(0, min(1, r[0])), "source": r[2],
             "created_by": r[3], "created_at": str(r[4])}
            for r in n_obs
        ])
    
    r2_result = compound_confidence(pattern_obs, use_diversity_correlation=True)
    r2_confidence = r2_result["compound_confidence"]
    
    # R3: Combined — AND(current signal + pattern history)
    combined_obs = observations + pattern_obs
    r3_result = compound_confidence(combined_obs, use_diversity_correlation=True)
    r3_confidence = r3_result["compound_confidence"]
    
    # Compute the lift
    lift = None
    best_single = max(r1_confidence or 0, r2_confidence or 0)
    if r3_confidence and best_single:
        lift = r3_confidence - best_single
    
    return {
        "node_id": node_id,
        "r1_observations_only": {
            "confidence": r1_confidence,
            "observation_count": len(observations),
            "method": r1_result.get("method", "compound"),
        },
        "r2_pattern_only": {
            "confidence": r2_confidence,
            "neighbor_count": len(neighbor_ids),
            "pattern_observation_count": len(pattern_obs),
            "method": r2_result.get("method", "compound"),
        },
        "r3_combined": {
            "confidence": r3_confidence,
            "total_observation_count": len(combined_obs),
            "method": r3_result.get("method", "compound"),
            "diversity_correlation": r3_result.get("diversity_correlation"),
        },
        "lift": lift,
        "lift_pct": round(lift / best_single * 100, 1) if lift and best_single else None,
        "diagnosis": _diagnose_context_gap(r1_confidence, r2_confidence, r3_confidence),
    }


def _diagnose_context_gap(r1, r2, r3):
    """Return a human-readable diagnosis of what context is missing.
    
    This is the "bank teller question" — what one question would
    elevate this from medium to high confidence?
    """
    if r3 is None:
        return "no_combined_confidence"
    
    has_signal = r1 is not None and r1 > 0.3
    has_pattern = r2 is not None and r2 > 0.3
    has_combined = r3 > 0.6
    
    if has_combined:
        return "high_confidence_synthesis"
    elif has_signal and not has_pattern:
        return "has_signal_no_pattern — add historical context or causal edges"
    elif has_pattern and not has_signal:
        return "has_pattern_no_signal — add current observations"
    elif not has_signal and not has_pattern:
        return "no_context — needs both observations and connections"
    else:
        return "medium_confidence_both_present — context exists but not yet combining effectively"
```

### 2. Context Scope in Agent State

The video's key insight: **the human controls the canvas, the agent sees only what's on it.** OHM's `agent_state` table already exists but doesn't control what context agents receive.

**Implementation** — extend the existing `orient` endpoint with `context_scope`:

```python
# In _get_orient — add context_scope parameter

# context_scope controls what the agent sees
CONTEXT_SCOPES = {
    "full": {"include_l0": True, "include_l1": True, "max_depth": 3, "max_nodes": 200},
    "operational": {"include_l0": False, "include_l1": True, "max_depth": 2, "max_nodes": 100},
    "stale": {"include_l0": False, "include_l1": False, "max_depth": 1, "max_nodes": 50},
    "focused": {"include_l0": False, "include_l1": True, "max_depth": 2, "max_nodes": 30, "tags_filter": True},
}

# Default: "operational" — heartbeat agents get stale data, deep research gets full
```

This is the **canvas control** from Layer 7. Different agents see different context scopes. Heartbeat checks get `stale` (only L3+ nodes updated in the last 24 hours). Deep research gets `full`. A focused query gets `focused` (only nodes matching the query tags).

### 3. Wrong Aggregate Rejection (OrionBelt Pattern)

OrionBelt rejects `SUM(COUNT_measure)` because the declared aggregation is `COUNT`. OHM already has `EDGE_CONSTRAINTS` and nudges, but doesn't reject semantically invalid edges.

**Implementation** — extend the existing nudge system (which already fires on edge creation) with semantic validation:

```python
# In nudges.py — add semantic validation nudge

SEMANTIC_EDGE_RULES = {
    "CAUSES": {
        "from_types": {"concept", "event", "pattern"},
        "to_types": {"concept", "event", "pattern"},
        "invalid_pairs": [("source", "*")],  # Sources can't cause things
        "nudge": "CAUSES edges should connect concepts, events, or patterns. "
                  "Sources support claims; they don't cause outcomes. "
                  "Consider SUPPORTS or REFERENCES instead.",
    },
    "SUPPORTS": {
        "from_types": {"source", "concept", "pattern"},
        "to_types": {"concept", "pattern", "event"},
        "invalid_pairs": [],
    },
    "REFERENCES": {
        "requires_source_url": True,
        "nudge": "REFERENCES edges should have a source_url on the source node (ADR-013).",
    },
}

def validate_edge_semantics(from_node: dict, to_node: dict, edge_type: str) -> dict | None:
    """Validate that an edge makes semantic sense.
    
    Returns None if valid, or a nudge dict if the edge is semantically questionable.
    This is the OrionBelt pattern: reject wrong aggregates loudly.
    """
    rules = SEMANTIC_EDGE_RULES.get(edge_type)
    if not rules:
        return None
    
    from_type = from_node.get("type", "")
    to_type = to_node.get("type", "")
    
    # Check from_type
    if rules.get("from_types") and from_type not in rules["from_types"]:
        valid_types = ", ".join(sorted(rules["from_types"]))
        return {
            "type": "semantic_edge_warning",
            "severity": "warning",
            "message": f"{edge_type} edges typically originate from: {valid_types}. "
                       f"Your from_node is type '{from_type}'. Is this the correct edge type?",
        }
    
    # Check invalid pairs
    for invalid_from, invalid_to in rules.get("invalid_pairs", []):
        if (invalid_from == "*" or from_type == invalid_from) and \
           (invalid_to == "*" or to_type == invalid_to):
            return {
                "type": "semantic_edge_rejection",
                "severity": "error",
                "message": rules["nudge"],
            }
    
    return None
```

This is **advisory first, strict later** — matching the graduated enforcement pattern from ADR-022 and ADR-007. In advisory mode, it adds a nudge. In strict mode, it rejects the write.

### 4. Discovery Scope (OKF Governance Layer)

OKF's governance question: **who decides which knowledge bundles agents find?** OHM's `/suggest` and `/orphans` return all disconnected nodes to any agent. There's no scoping.

**Implementation** — add `discovery_scope` to the existing `/suggest` endpoint:

```python
# Extend _get_suggest in analysis.py with discovery_scope

# Discovery scopes control what agents can discover
# This is the OKF governance layer — who controls what agents find
DISCOVERY_SCOPES = {
    "team": {  # Default: confidence >= 0.8, L2+ only
        "min_confidence": 0.8,
        "min_layer": "L2",
        "exclude_types": ["fragment"],
        "require_observations": True,
    },
    "public": {  # For external consumers (OKF bundles)
        "min_confidence": 0.9,
        "min_layer": "L3",
        "min_verified_outcomes": 1,
        "require_source_url": True,
    },
    "private": {  # Agent's own nodes only
        "min_confidence": 0.0,
        "min_layer": "L0",
        "require_observations": False,
    },
}
```

This is **not new infrastructure** — it's a filter on the existing `/suggest` endpoint using the existing `effective_layer()` and `chain_validity()` from constraints.py. The team scope (default) only suggests L2+ nodes with confidence ≥ 0.8 and at least one observation. The public scope (for OKF bundles) requires L3+ with verified outcomes and source URLs.

### 5. Temporal Versioning for Knowledge (Deployment AND-Gate)

The version control article's key insight: **floating model aliases are hidden OR-gates.** OHM has this problem — observations have `created_at` but no `effective_at` or `superseded_at` for knowledge versioning.

**Implementation** — extend the existing `ohm_observations` table with temporal validity:

```sql
-- These columns already exist in the schema (from ADR-018 temporal validity)
-- We're just wiring them to the inference engine
ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS effective_from TIMESTAMP;
ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMP;
ALTER TABLE ohm_observations ADD COLUMN IF NOT EXISTS supersedes VARCHAR;
```

Wait — these columns may already exist. Let me check:

```sql
SELECT column_name FROM information_schema.columns 
WHERE table_name = 'ohm_observations' 
ORDER BY ordinal_position;
```

The key change isn't the schema — it's **querying with temporal pinning**. Add an `as_of` parameter to `/neighborhood`, `/inference`, and `/orient`:

```
GET /neighborhood/hormuz_and_gate?depth=2&as_of=2026-06-15T00:00:00Z
GET /inference?target=hormuz_and_gate&as_of=2026-06-15T00:00:00Z
GET /orient?agent=metis&as_of=2026-06-15T00:00:00Z
```

This lets agents ask: "What did I know on June 15?" — the knowledge version pinning from Layer 6.

## The AND-Gate Architecture (Revised)

| Mechanism | Gate Type | Layer | Status | This ADR |
|-----------|-----------|-------|--------|-----------|
| Layer promotion constraints | AND-gate | Structural | ADR-022 | Extended (context-gate) |
| Edge-level constraints | AND-gate | Structural | ADR-022 | Extended (semantic validation) |
| Temporal demotion | AND-gate | Structural | ADR-022 | Extended (as_of pinning) |
| **Context gate** | **AND-gate** | **Query** | **New** | **This ADR** |
| **Canvas control / discovery scope** | **AND-gate** | **Query** | **New** | **This ADR** |
| **Semantic edge validation** | **AND-gate** | **Write** | **New** | **This ADR** |
| Verification nudges | OR-gate | Behavioral | ADR-018 | Unchanged |
| Challenge nudges | OR-gate | Behavioral | ADR-018 | Unchanged |

The three new mechanisms all implement the same structural pattern:

- **Context gate** = "What context is missing to get from medium to high confidence?" (Layer 7)
- **Discovery scope** = "Who controls what the agent sees?" (Layers 5+7)
- **Semantic edge validation** = "Wrong aggregates rejected loudly" (Layer 2)

## Implementation Priority

1. **Context gate** (`/context-gate/{node_id}`) — Extends existing `_get_confidence` handler. No new infrastructure. Uses existing `compound_confidence()`. **Small change, high value.**

2. **Semantic edge validation** — Extends existing nudge system. No new tables. Uses existing node type information. **Small change, high value.**

3. **Discovery scope** — Filter on existing `/suggest` endpoint using existing `effective_layer()` and `chain_validity()`. **Small change, medium value.**

4. **Canvas control** — Extends existing `/orient` with `context_scope` parameter. **Small change, medium value.**

5. **Temporal versioning** (`as_of` parameter) — Requires wiring existing `effective_from`/`supersedes` columns to inference engine. **Medium change, high value, but more complex.**

## Consequences

### Positive
- **Context completeness scoring** — agents can now see *which context layer is missing*, not just *what confidence the node has*. This is the "bank teller question" implemented as a query.
- **Wrong aggregate rejection** — semantically invalid edges get nudged or rejected. This is the OrionBelt pattern applied to knowledge graphs.
- **Discovery governance** — agents see only appropriately-scoped suggestions. This is the OKF governance layer.
- **All built on existing infrastructure** — compound_confidence, effective_layer, chain_validity, nudges, orient, suggest. No new tables, no new engines, no new sediment.

### Negative
- **Context gate is computationally expensive** — it runs compound_confidence 3 times per query. Should be cached or computed lazily.
- **Semantic validation is advisory first** — agents will still create CAUSES edges from source nodes. The nudge will warn, not block, until strict mode is enabled.
- **Discovery scope might be too restrictive** — team scope (confidence ≥ 0.8) could hide useful low-confidence suggestions. May need tuning.

### Migration Path

1. **Phase 1 (advisory):** Add `/context-gate` endpoint and semantic nudges. No changes to existing behavior.
2. **Phase 2 (scoped):** Add `context_scope` to `/orient` and `discovery_scope` to `/suggest`. Default to current behavior (no filtering).
3. **Phase 3 (temporal):** Add `as_of` parameter to `/neighborhood` and `/inference`. Requires `effective_from`/`supersedes` wiring.
4. **Phase 4 (strict):** Enable strict semantic edge validation. Block invalid edge types.

## References

- ADR-007: Schema Evolution and Type Governance — Graduated enforcement
- ADR-018: Verification Loops — Behavioral nudges + automated decay
- ADR-022: Layer Promotion Constraints — SHACL-like write gates (this extends it)
- OrionBelt: Semantic layer that refuses wrong aggregates loudly
- 4.0 Solutions: Context Management as AND-gate (CNC experiment)
- Google OKF: Discovery governance (who controls what agents find)
- Agent Version Control: Deployment AND-gates with model pinning
- Karpathy Constraint Engineering: 4 behavioral (OR) + 4 structural (AND) = 41%→5%