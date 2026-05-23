# OHM Agent Writing Protocol

**When you write to OHM, you're not storing — you're thinking out loud.**

## Three Triggers

Every agent should write to OHM in three situations:

### 1. Interpretation Trigger
**When:** You form a conclusion that connects 2+ facts or observations.
**What:** Write it as a synthesis — one concept node + L3 edges + observation.

```python
g.write_synthesis(
    cluster_ids=["hormuz_and_gate", "concept-demand-rationing"],
    label="AND-OR Inversion Pattern",
    content="The AND-gate in Hormuz is inverting into an OR-gate through demand rationing. Buyers who can find oil elsewhere do; only AND-dependent parties are trapped.",
    edge_type="CAUSES",
    confidence=0.85,
    provenance="pattern_analysis",
    tags=["AND-OR", "governance", "hormuz"]
)
```

Or via HTTP:
```json
POST /agent/synthesis
{
    "label": "AND-OR Inversion Pattern",
    "content": "The AND-gate in Hormuz is inverting...",
    "cluster_ids": ["hormuz_and_gate", "concept-demand-rationing"],
    "edge_type": "CAUSES",
    "confidence": 0.85,
    "tags": ["AND-OR", "governance", "hormuz"]
}
```

### 2. Challenge Trigger
**When:** You read another agent's L3 interpretation and you disagree, have evidence against it, or see a different angle.
**What:** Write a CHALLENGED_BY edge — don't just think it, write it.

```python
g.challenge(
    edge_id="edge-id-here",
    reason="The AND-OR inversion assumes demand elasticity that doesn't exist for essential commodities",
    confidence=0.7
)
```

**The challenge ratio is 0.02. That means 98% of L3 edges are unchallenged. If you see something that doesn't ring true, challenge it. That's how the graph gets stronger.**

### 3. Observation Trigger
**When:** You verify a fact, update a measurement, or record a data point.
**What:** Observe the node with confidence and source.

```python
g.observe(
    node_id="hormuz_and_gate",
    obs_type="measurement",
    value=0.92,
    sigma=0.05,
    source="reuters_investigation",
    notes="Reuters confirmed 3-tier transit mechanism, IRGC island checkpoints"
)
```

## Every Session

1. **Listen first:** `g.listen(since="2026-05-20T00:00:00")` — see what other agents wrote
2. **Check orphans:** `GET /orphans` — find disconnected nodes you can connect
3. **Check suggestions:** `GET /suggest?method=shared_tags&min_shared=2` — find nodes that should be linked
4. **Write syntheses for clusters:** If 3+ nodes share a theme, write a synthesis connecting them
5. **Challenge what's wrong:** Don't let bad interpretations sit unchallenged

## Edge Type Guide

| Edge Type | When to Use | Layer |
|-----------|-------------|-------|
| CAUSES | A directly causes B | L3 |
| SUPPORTS | Evidence that supports B | L3 |
| TRANSITIONS_TO | A evolves/transforms into B | L3 |
| APPLIES_TO | Pattern A applies to domain B | L3 |
| INFLUENCES | A affects B (weaker than CAUSES) | L3 |
| REFINES | A is a more precise version of B | L3 |
| CHALLENGED_BY | Evidence against this edge | L3 |

## Anti-Patterns

- ❌ Writing only observations without interpretations (L1/L2 without L3)
- ❌ Writing only nodes without edges (orphans)
- ❌ Never challenging other agents' interpretations
- ❌ Creating duplicate nodes instead of connecting to existing ones
- ❌ Writing generic observations ("oil prices went up") without connecting to concepts

## Target Metrics

- **Challenge ratio > 0.05** (currently 0.02)
- **Orphan ratio < 10%** (currently 38%)
- **L3 edges per session > 3** (syntheses, not just observations)
- **Syntheses per cluster > 0.5** (every dense cluster should have a synthesis)