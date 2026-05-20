# ADR-013: Value of Information for Knowledge Graphs

**Status:** Accepted

**Date:** 2026-05-20

**Authors:** Métis (proposal), Matt Markham (decision)

## Context

OHM currently knows *which nodes are uncertain* but not *which uncertainties matter for decisions*. Confidence scores identify gaps; compound confidence traces them through chains. But neither answers: "If I could observe one more thing, which observation would most improve my decisions?"

This is the **Expected Value of Information (EVI)** problem from Hubbard & Savage (2016): EVI = Expected Value WITH information − Expected Value WITHOUT information. EVI prioritizes research by decision impact, not by gap size.

The Bayesian inference stack is complete (`/inference`, `/intervene`, `/ate`, `/sensitivity`, `/adjustment`, `/suggest_causes`, `/refute`), but it returns empty results because edge `probability` fields are unpopulated. Without causal strength values, the Bayesian network can't build meaningful CPTs.

### The Elicitation Problem

Edge probability (P(effect|cause)) is not something we can measure from data. These are L3 knowledge edges — expert judgments about causal relationships. The question isn't "what is the probability?" but "how do we elicit a principled estimate from subjective judgment?"

### Probability Elicitation: A Spectrum, Not a Single Method

No single elicitation method fits all edges. The appropriate method depends on the evidence available:

| Evidence Level | Method | Source | Variance | Example |
|---|---|---|---|---|
| **Empirical** | Fitted distribution | Published study, dataset, actuarial table | Measured σ² | Hormuz shipping data (daily transit counts), CPI inflation rates |
| **Anchored PERT** | PERT + reference class | Study provides base rate, PERT adjusts for context | Derived σ² | Trap formation rates from institutional studies, adjusted for AND→OR context |
| **Pure PERT** | Three-point expert estimate | Agent or human judgment | Derived σ² | "noble_lie → trap_origins: 0.70/0.85/0.95" |
| **Mixture-of-experts** | Weighted PERT from multiple agents | Clio researches, Socrates challenges, Métis synthesizes | Aggregated σ² | High-value decisions where multiple perspectives reduce bias |
| **Uninformative** | Uniform prior (0.05/0.50/0.95) | No basis for judgment | Maximum σ² | Edges where we know nothing yet |

**Selection rule:** Use the highest-evidence method available. If a statistical study exists, use it. If not, PERT. For the highest-value decisions, solicit mixture-of-experts input.

**PERT as default:** Most L3 knowledge edges are subjective — there's no actuarial table for "does institutional trap formation cause knowledge-action gaps?" PERT is the right tool for these cases. It produces principled estimates from expert judgment while preserving variance for VoI ranking.

For edges where empirical data exists (commodity prices, shipping volumes, inflation rates), the system should accept fitted distributions directly — Normal, Beta, or empirical CDF — stored in `metadata.distribution` alongside the PERT fields.

### Mixture-of-Experts Protocol

For high-value decisions (utility ≥ 7 on the 1-10 scale), solicit PERT estimates from multiple agents:

1. **Each agent provides independent PERT:** Clio (research-backed), Socrates (skeptical), Métis (synthesizing)
2. **Weight by source reliability:** `source_reliability` from OHM's outcome tracking
3. **Aggregate:** μ = Σ(wᵢ × μᵢ), σ² = Σ(wᵢ × σᵢ²) + Σ(wᵢ × (μᵢ − μ)²)
4. **Challenge mechanism:** Socrates can CHALLENGED_BY any edge, creating an alternative PERT estimate
5. **The human decides:** The graph preserves all estimates; the Bayesian network uses the mixture by default

This is ADR-003 (agent-owned edges) applied to probability elicitation. Each agent owns their PERT; the mixture is derived, not averaged.

### PERT Elicitation Detail

Each causal edge is characterized by three values:

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
- Variance is available as `(p95 - p05) / 6`²
- The Bayesian network builder uses `probability` for CPT construction
- VoI uses variance × downstream decision sensitivity to rank research priorities

**Elicitation method tracking:** The `metadata` JSON field stores the elicitation method:
- `{"pert_method": "expert", "pert_source": "metis"}` — single-agent PERT
- `{"pert_method": "mixture", "pert_sources": ["metis", "socrates", "clio"], "pert_weights": [0.4, 0.3, 0.3]}` — mixture-of-experts
- `{"pert_method": "empirical", "pert_source": "hormuz-shipping-data", "pert_distribution": "beta", "pert_reference": "IMF-2026-05"}` — fitted distribution from study
- `{"pert_method": "uninformative"}` — uniform prior (0.05/0.50/0.95), maximum variance

**Challenge mechanism:** When Socrates (or another agent) challenges a PERT estimate, a CHALLENGED_BY edge is created with its own PERT values. The Bayesian network uses the original (unchallenged) estimate by default; mixture-of-experts aggregation is available as an alternative. This is ADR-003 (agent-owned edges) applied to probability elicitation — each agent owns their PERT; the mixture is derived, not averaged.

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
| PERT elicitation on AND→OR cluster | OHM-6mv.11 | P1 | Métis | PERT schema |
| Mixture-of-experts PERT aggregation | (new) | P2 | Hephaestus | PERT schema, /voi |
| Empirical distribution import (for studied edges) | (new) | P2 | Hephaestus | PERT schema |
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