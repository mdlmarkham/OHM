# ADR-013: Value of Information for Knowledge Graphs

**Status:** Proposed

**Date:** 2026-05-20

**Authors:** Métis (proposal), Matt Markham (decision)

## Context

OHM currently knows *which nodes are uncertain* but not *which uncertainties matter for decisions*. Confidence scores identify gaps; compound confidence traces them through chains. But neither answers: "If I could observe one more thing, which observation would most improve my decisions?"

This is the **Expected Value of Information (EVI)** problem from Hubbard & Savage (2016): EVI = Expected Value WITH information − Expected Value WITHOUT information. EVI prioritizes research by decision impact, not by gap size.

The Bayesian inference stack is complete (`/inference`, `/intervene`, `/ate`, `/sensitivity`, `/adjustment`, `/suggest_causes`, `/refute`), but it returns empty results because edge `probability` fields are unpopulated. Without causal strength values, the Bayesian network can't build meaningful CPTs.

### The Elicitation Problem

Edge probability (P(effect|cause)) is not something we can measure from data. These are L3 knowledge edges — expert judgments about causal relationships. The question isn't "what is the probability?" but "how do we elicit a principled estimate from subjective judgment?"

### PERT as Elicitation Protocol

The PERT (Program Evaluation and Review Technique) three-point estimation method provides the bridge. Instead of a single point estimate, each causal edge is characterized by three values:

- **Optimistic (P05):** Given this cause is active, probability of effect under most favorable conditions
- **Most Likely (P50):** Best judgment of the conditional probability
- **Pessimistic (P95):** Given this cause is active, probability of effect under least favorable conditions

The PERT beta distribution approximation derives:

- **Mean:** μ = (O + 4M + P) / 6
- **Variance:** σ² = ((P - O) / 6)²

This preserves variance — which is exactly what VoI needs. High-variance edges connected to high-impact decisions are the ones worth researching.

### Connection to Existing Bayesian Stack

ADR-008 separated `probability` from `confidence` on edges. ADR-009 added NEGATES semantics. The Noisy-OR gate implementation already handles multi-parent nodes with leak probability. PERT extends this:

1. The derived mean (μ) becomes the `probability` field for Bayesian CPT construction
2. The derived variance (σ²) becomes the VoI uncertainty signal
3. Leak probability modulates by confidence (ADR-008: `leak = base × (1 - avg_confidence)`)
4. Wide P05–P95 spread = high uncertainty = high VoI if downstream decision impact is also high

## Decision

### Phase 1: PERT Elicitation Protocol (Implement First)

Extend the edge schema to store three-point probability estimates:

```sql
ALTER TABLE ohm_edges ADD COLUMN probability_p05 FLOAT;
ALTER TABLE ohm_edges ADD COLUMN probability_p50 FLOAT;
ALTER TABLE ohm_edges ADD COLUMN probability_p95 FLOAT;
```

The existing `probability` column becomes the PERT-derived mean: `probability = (p05 + 4 * p50 + p95) / 6`.

When PERT values are provided:
- `probability` is computed (not manually set)
- Variance is available as `(p95 - p05) / 6`
- The Bayesian network builder uses `probability` for CPT construction
- VoI uses variance × downstream decision sensitivity to rank research priorities

When PERT values are not provided (backwards compatibility):
- `probability` can still be set manually (single point estimate)
- Variance is unknown → VoI treats these as maximum uncertainty (σ² = 0.25, uniform prior)

### Phase 2: Decision Nodes

Add `decision` as a first-class node type with a `utility` field in `metadata`:

```python
# Decision nodes encode: "If I'm wrong about this, what does it cost?"
g.create_node(
    id="decision-hormuz-response",
    label="Decision: Hormuz Response Strategy",
    type="concept",  # Use 'concept' type with decision metadata
    metadata={
        "decision": True,
        "utility_function": "cost_of_wrong_decision",
        "utility_scale": "1-10",  # ordinal for now
        "wrong_cost": 9,  # 1-10 scale: how much does being wrong matter?
    }
)
```

Decision nodes connect to their causal ancestors via `DEPENDS_ON` edges. The `/voi` endpoint traces causal paths backward from each decision node to identify which observations would most reduce decision uncertainty.

### Phase 3: `/voi` Endpoint

```python
def compute_voi(graph, decision_nodes, agent_id=None):
    """
    For each decision node:
    1. Find all causal ancestors (backward traversal through CAUSES, ENABLES, INFLUENCES)
    2. For each ancestor:
       - uncertainty = 1 - confidence (or σ² from PERT if available)
       - sensitivity = ATE(ancestor → decision) or path-weighted confidence product
       - voi_score = uncertainty × sensitivity × decision_utility
    3. Rank ancestors by voi_score
    4. Return: [(node_id, voi_score, uncertainty, sensitivity, decision_id)]
    """
```

### Phase 4: Agent Task Assignment

Wire VoI output into the task system:
- Auto-generate tasks for high-VoI research targets
- Assign to agents based on expertise match (tags ↔ provenance)
- Track: did the research reduce uncertainty? (source_reliability feedback loop)
- Temporal decay: observations age, confidence decreases over time (ADR-012 local sync)

## Consequences

### Enables
- **Principled research prioritization** — Not "what am I most uncertain about?" but "which uncertainty most affects my decisions?"
- **Bayesian inference that works** — PERT-derived probabilities populate CPTs; `/inference` returns real posteriors
- **Self-optimizing knowledge graph** — The graph can tell you what to research next
- **Cross-domain applicability** — PERT works for subjective judgment in any domain (geopolitics, governance, trading)

### Requires
- **Human elicitation** — PERT values come from expert judgment. Agents can suggest initial estimates, but humans should validate.
- **Decision nodes with utility** — Without utility, VoI degenerates to "reduce uncertainty everywhere" rather than "reduce uncertainty where it matters"
- **Causal structure** — VoI is only meaningful where CAUSES/ENABLES/INFLUENCES edges exist. Areas without causal structure need structural work first.

### Risks
- **GIGO** — Bad PERT estimates produce bad VoI rankings. Mitigation: start with conservative ranges and update as observations arrive.
- **Causal model incompleteness** — Missing edges mean missing causal paths, which means VoI underestimates impact. Mitigation: `/suggest_causes` identifies candidate causal edges from existing non-causal relationships.
- **Over-precision illusion** — Three-point estimates feel precise but are still subjective. Mitigation: display PERT-derived variance alongside point estimates.

## Implementation Priority

| Task | Beads ID | Priority | Assignee | Blocked By |
|------|----------|----------|----------|------------|
| PERT schema (p05/p50/p95 columns) | OHM-6mv.3 | P2 | Hephaestus | This ADR |
| Decision node metadata pattern | OHM-6mv.2 | P1 | Hephaestus | This ADR |
| `/voi` endpoint | OHM-6mv.1 | P1 | Hephaestus | Decision nodes |
| Seed decision nodes | OHM-6mv.9 | P1 | Métis | Decision node type |
| PERT elicitation on AND→OR cluster | (new) | P1 | Métis | PERT schema |
| Self-referential VoI validation | OHM-6mv.7 | P1 | Métis | `/voi` endpoint |
| ADR-013 (this document) | OHM-6mv.10 | P1 | Métis | — |
| Temporal confidence decay | OHM-6mv.4 | P2 | Hephaestus | `/voi` endpoint |
| Source reliability weighting | OHM-6mv.6 | P2 | Hephaestus | `/voi` endpoint |
| Agent task assignment from VoI | OHM-6mv.5 | P2 | Hephaestus | `/voi` endpoint |
| `ohm voi` CLI command | OHM-6mv.8 | P2 | Hephaestus | `/voi` endpoint |

## References

- Hubbard, D. & Savage, S. (2016). *How to Measure Anything in Cybersecurity Risk*. Wiley.
- ADR-008: Probability and Confidence Model
- ADR-009: NEGATES Edge Type
- ADR-012: Per-Agent Local DuckDB Cache
- Zettelkasten note: `202605200030_VoI_for_OHM_Knowledge_Graph.md`
- PERT three-point estimation: https://pmstudycircle.com/three-point-estimation/