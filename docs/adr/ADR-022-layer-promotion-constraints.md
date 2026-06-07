# ADR-022: Layer Promotion Constraints (SHACL-like Write Gates)

**Date:** 2026-06-07
**Status:** Proposed
**Author:** Metis
**Tags:** SHACL, layer-promotion, write-gates, constraint-validation, temporal-validity, AND-gate

## Context

OHM has five layers (L0-L4) with increasing trust requirements, but the current enforcement is minimal:

- **L0 → L1:** No constraint. Fragments auto-link via semantic similarity and can be promoted with `promote_fragment()`, but promotion is procedural, not architectural.
- **L1 → L2:** No constraint. Any agent can create L1 structure and L2 flow edges without evidence.
- **L2 → L3:** No constraint. L3 knowledge edges (CAUSES, PREDICTS, etc.) can be created without any L2 REFERENCES observations backing them. This is the Evaluation Trap gap identified in ADR-018.
- **L3 → L4:** No constraint. Prospects can be created without any verification outcomes.

The result: `compound_confidence` was returning 1.0 for heavily-observed nodes regardless of whether observations confirmed or contradicted each other (ADR-018.4 fixed this with diversity correlation and verification factor), but the structural gap remains — agents can create high-confidence claims without evidence, and the system has no architectural gate to prevent it.

ADR-018 added **behavioral nudges** (OR-gates: prompt agents to verify) and **automated decay** (AND-gate: unverified claims decay over time). This ADR adds **write gates** (SHACL-like structural constraints that prevent invalid writes at the architecture level).

The layer architecture already encodes a trust gradient. Pankaj Kumar's work on SHACL semantic firewalls shows that the platforms that win enforce constraints architecturally, not procedurally. This ADR applies the same principle to OHM's layer model.

## Decision

### 1. Layer Promotion Constraints

Each layer transition requires satisfying a set of constraints. A node or edge that doesn't satisfy the constraints for its target layer is either rejected (strict mode) or accepted with a warning (advisory mode, per ADR-007).

#### L0 → L1: Fragment to Structure

**Constraint:** At least one L0 context link must exist.

```
PROMOTION_CONSTRAINTS["L0_to_L1"] = {
    "min_context_links": 1,         # At least one L0 edge (CONTEXT_OF, INSPIRED_BY)
    "require_promotion_action": True, # Must call promote_fragment(), not just change type
}
```

**Rationale:** L1 is "shared structure." A fragment with no context links is unanchored — it can't participate in any reasoning chain. The `promote_fragment()` function already exists (ADR-019); this makes it an architectural requirement rather than a procedural suggestion.

**Implementation:** When `promote_fragment()` is called, check that the fragment has at least one L0 edge. If not, reject the promotion. Auto-linked fragments (via `scratch(connects_to=[...])`) always satisfy this.

#### L1 → L2: Structure to Evidence

**Constraint:** At least one source with `source_url` (ADR-013).

```
PROMOTION_CONSTRAINTS["L1_to_L2"] = {
    "min_sources": 1,               # At least one source node linked
    "source_must_have_url": True,    # Source must have source_url (ADR-013)
    "min_observations": 1,           # At least one observation on the node
}
```

**Rationale:** L2 is "shared with attribution." Structure without evidence stays at L1. A node promoted to L2 without a source is making a claim without attribution — this is exactly the sacred reference problem (ADR-018).

**ADR-013 enforcement:** ADR-013 requires `source_url` for source observations. This constraint makes it architectural: no L2 node without a source that has a URL.

**Observation requirement:** A node with no observations has no evidence. The minimum is 1 observation to anchor the claim.

#### L2 → L3: Evidence to Knowledge

**Constraint:** ≥2 independent sources, ≥1 verification outcome, and chain_validity ≥ 0.3.

```
PROMOTION_CONSTRAINTS["L2_to_L3"] = {
    "min_sources": 2,               # ≥2 independent source nodes
    "min_independent_agents": 1,    # ≥1 source from a different agent
    "min_observations": 2,          # ≥2 observations (not all from same session)
    "min_outcomes": 1,              # ≥1 recorded verification outcome
    "min_chain_validity": 0.3,      # Weakest-link bound across supporting observations
    "require_references_edge": True, # L3 CAUSES/PREDICTS edges require L2 REFERENCES
}
```

**Rationale:** L3 is "agent-owned, challengeable." This is the critical gate. The AND-gate pattern applies:

- **Source diversity** (≥2 sources, ≥1 different agent): Prevents single-source epistemic closure. Same-agent confirmation is correlation, not corroboration (ADR-018.4 diversity correlation factor).
- **Verification outcome** (≥1): Prevents the Evaluation Trap. L3 knowledge that has never been tested against reality is a sacred reference (ADR-018.1/2/3).
- **Chain validity** (≥0.3): The weakest-link bound from OHM-wuki. A synthesis built on observations at 0.2 effective confidence each has chain validity near 0. It should not be L3.
- **REFERENCES edge requirement**: An L3 CAUSES edge without an L2 REFERENCES observation is an unsupported causal claim. The REFERENCES observation provides the evidence that the causal relationship exists.

**Temporal validity interaction:** When computing `min_chain_validity`, use `confidence_at(t=now)` for all supporting observations. An L3 node that was promoted with chain_validity ≥ 0.3 but whose supporting evidence has since decayed below 0.3 is eligible for **layer demotion** (see Section 3).

#### L3 → L4: Knowledge to Prospect

**Constraint:** ≥3 supporting L3 nodes, ≥2 verified outcomes, chain_validity ≥ 0.5, and no open challenges.

```
PROMOTION_CONSTRAINTS["L3_to_L4"] = {
    "min_L3_support": 3,           # ≥3 supporting L3 nodes via L3/L2 edges
    "min_verified_outcomes": 2,    # ≥2 outcomes recorded as True
    "min_chain_validity": 0.5,      # Higher bar than L3
    "no_open_challenges": True,     # No CHALLENGED_BY edges without resolution
}
```

**Rationale:** L4 is "book-ready." The bar is highest. Three independent L3 knowledge nodes with verified outcomes and no open challenges represent a consensus that has survived challenge.

### 2. Edge-Level Constraints

In addition to node promotion constraints, edges have layer-specific requirements:

```
EDGE_CONSTRAINTS = {
    "CAUSES": {
        "min_layer": "L2",           # CAUSES edges must be at least L2
        "require_references": True,   # Must have at least one L2 REFERENCES observation
        "require_outcome": False,     # Outcome not required at L2
    },
    "PREDICTS": {
        "min_layer": "L3",           # PREDICTS edges must be at least L3
        "require_references": True,
        "require_outcome": True,      # Predictions should be testable
    },
    "CHALLENGED_BY": {
        "require_confidence": True,   # Must include confidence value
        "require_reasoning": True,    # Must include text reasoning
    },
    "SUPPORTS": {
        "min_layer": "L1",           # SUPPORTS edges allowed from L1
        "require_references": False,  # No evidence required at L1
    },
}
```

**CAUSES at L2+ with REFERENCES:** A causal claim without evidence is the most common form of sacred reference. The REFERENCES observation provides the evidence that the causal relationship exists. At L3+, the outcome requirement adds verification.

**PREDICTS at L3+ with outcome:** Predictions are testable claims. Creating a PREDICTS edge should come with an implicit commitment to record whether the prediction came true.

### 3. Layer Demotion via Temporal Validity

Layer membership should be **re-evaluated on query**, not just on write. An observation that satisfied L3 constraints when created may no longer satisfy them after its supporting evidence decays.

```
def effective_layer(node, t=None):
    """Compute the effective layer of a node at time t, based on constraint satisfaction."""
    constraints = PROMOTION_CONSTRAINTS
    original_layer = node.layer

    # Check if the node still satisfies its current layer's constraints
    if original_layer == "L3":
        cv = chain_validity(node, t=t)
        sources = count_independent_sources(node, t=t)
        outcomes = count_verified_outcomes(node, t=t)
        challenges = count_open_challenges(node, t=t)

        if cv >= 0.3 and sources >= 2 and outcomes >= 1 and challenges == 0:
            return "L3"
        elif cv >= 0.1 and sources >= 1 and outcomes >= 0:
            return "L2"  # Demoted: evidence decayed below L3 threshold
        else:
            return "L1"  # Demoted: lost most supporting evidence

    # Similar logic for L4, L2
    ...
```

**Key principle:** Layer demotion is **computed, not stored.** The node's `layer` field in the database represents the layer at creation time. The `effective_layer()` function computes the current layer based on constraint satisfaction with temporal validity. This avoids write amplification and preserves the creation-time layer for audit trails.

**Query-time implications:** `neighborhood()` and `search()` should include `effective_layer` in responses, computed from current constraint satisfaction. Clients can filter by effective layer to get "what's still trustworthy" vs. "what was created as L3 but has since degraded."

### 4. Constraint Validation as Write Gate

Constraints are validated at write time in the HTTP handler, not in the database. This follows the existing pattern for `CROSS_LINK_REQUIRED` (ADR-018):

```python
# In handlers/graph.py

LAYER_PROMOTION_CONSTRAINTS = {
    "L0_to_L1": {...},
    "L1_to_L2": {...},
    "L2_to_L3": {...},
    "L3_to_L4": {...},
}

def validate_layer_promotion(node, target_layer, store, config):
    """Validate that a node satisfies the constraints for the target layer.

    In advisory mode: log warnings for unsatisfied constraints.
    In strict mode: reject the write.

    Returns (valid, warnings, errors).
    """
    constraints = LAYER_PROMOTION_CONSTRAINTS.get(f"{node.layer}_to_{target_layer}", {})
    warnings = []
    errors = []

    for constraint_name, threshold in constraints.items():
        value = compute_constraint(node, constraint_name, store)
        if value < threshold:
            msg = f"{constraint_name}: {value} < {threshold}"
            if config.enforce_layer_gates:  # strict mode
                errors.append(msg)
            else:
                warnings.append(msg)

    valid = len(errors) == 0
    return valid, warnings, errors
```

**Enforcement modes (per ADR-007 graduated enforcement):**

| Mode | Unsatisfied Constraint | Result |
|------|----------------------|--------|
| Advisory | Any | Accepted with warning |
| Lenient | Structural (sources, observations) | Rejected |
| Lenient | Temporal (chain_validity, outcomes) | Accepted with warning |
| Strict | Any | Rejected |

### 5. Constraint Satisfaction Metadata

Each node and edge includes constraint satisfaction metadata in API responses:

```json
{
    "id": "concept-hormuz-and-gate",
    "layer": "L3",
    "effective_layer": "L3",
    "constraint_status": {
        "L3_requirements": {
            "min_sources": {"required": 2, "actual": 4, "satisfied": true},
            "min_independent_agents": {"required": 1, "actual": 3, "satisfied": true},
            "min_observations": {"required": 2, "actual": 43, "satisfied": true},
            "min_outcomes": {"required": 1, "actual": 3, "satisfied": true},
            "min_chain_validity": {"required": 0.3, "actual": 0.398, "satisfied": true},
            "no_open_challenges": {"required": 0, "actual": 0, "satisfied": true}
        }
    }
}
```

This makes constraint satisfaction **inspectable** — any agent can see why a node is at its current layer and what would be needed to promote it.

## The AND-OR Architecture

| Mechanism | Gate Type | Layer | Error Reduction |
|-----------|-----------|-------|-----------------|
| Layer promotion constraints | AND-gate | Structural | Must satisfy ALL constraints to promote |
| Edge-level constraints | AND-gate | Structural | CAUSES without REFERENCES = rejected |
| Temporal demotion | AND-gate | Structural | Decay below threshold → auto-demote |
| Verification nudges | OR-gate | Behavioral | Prompt agents to verify |
| Challenge nudges | OR-gate | Behavioral | Prompt agents to challenge dubious claims |
| Constraint warnings | OR-gate | Behavioral | Advisory mode warns but accepts |

The AND-gate (structural) ensures that even without agent cooperation, invalid claims can't persist at high layers. The OR-gate (behavioral) encourages agents to verify and challenge, improving the quality of evidence.

## Connection to Temporal Decay (ADR-022 → OHM-xdd4/wuki)

Layer promotion constraints interact with temporal validity:

1. **At creation time:** A node must satisfy L3 constraints with `confidence_at(t=now)`.
2. **At query time:** `effective_layer()` re-evaluates constraints with `confidence_at(t=query_time)`.
3. **On supersession:** When an observation is superseded, all dependent nodes' constraint satisfaction should be re-evaluated.

This means that temporal decay (OHM-xdd4) and chain validity (OHM-wuki) are not just query-time features — they are **write gates** for layer promotion. A node whose supporting evidence has decayed below the chain_validity threshold is architecturally prevented from maintaining L3 status.

## Consequences

### Positive
- **Architectural enforcement:** Invalid claims can't persist at high layers, even if agents don't cooperate with nudges.
- **Inspectable constraints:** Any agent can see why a node is at its current layer and what's needed to promote it.
- **Temporal grounding:** Layer membership reflects current evidence quality, not just creation-time quality.
- **AND-OR defense:** Structural constraints (AND) + behavioral nudges (OR) achieve 5% error rate together.

### Negative
- **Write friction:** Agents that currently create L3 edges without evidence will get warnings or rejections.
- **Migration:** Existing L3 nodes that don't satisfy L3 constraints will show `effective_layer < layer`. This is correct behavior (they shouldn't be L3 without evidence), but may surprise agents.
- **Complexity:** Constraint validation adds logic to every write path. Must be well-tested.
- **Tuning:** Threshold values (chain_validity ≥ 0.3, min_sources ≥ 2) are initial estimates. Should be calibrated from outcome data (OHM-8fdb learned half-lives).

### Migration Path

1. **Advisory mode** (default): Log warnings for unsatisfied constraints. No writes rejected. Collect data on current constraint satisfaction rates.
2. **Lenient mode**: Reject structural violations (sources, observations). Accept temporal violations with warnings.
3. **Strict mode**: Reject all violations. Full AND-gate enforcement.

This follows the graduated enforcement pattern from ADR-007. Default to advisory; agents opt into strict when ready.

## Implementation Notes

### New Files
- `src/ohm/graph/constraints.py` — Layer promotion constraint definitions and validation functions
- `tests/test_constraints.py` — Constraint validation tests

### Modified Files
- `src/ohm/graph/schema.py` — Add `LAYER_PROMOTION_CONSTRAINTS`, `EDGE_CONSTRAINTS`, `effective_layer()`
- `src/ohm/server/handlers/graph.py` — Add constraint validation to `create_node`, `create_edge`, `promote_fragment`
- `src/ohm/graph/store.py` — Add `effective_layer()` computation, constraint satisfaction queries
- `src/ohm/graph/decay.py` — Integrate `confidence_at()` into `effective_layer()`

### Schema Changes
- No schema changes required. Constraint satisfaction is computed from existing data (observations, outcomes, sources, challenges). The `effective_layer` field is computed at query time, not stored.

### API Changes
- `GET /node/{id}` — Include `effective_layer` and `constraint_status` in response
- `GET /neighborhood/{id}` — Include `effective_layer` for each node
- `POST /node` — Validate layer promotion constraints (advisory: warn; strict: reject)
- `POST /edge` — Validate edge-level constraints
- `GET /admin/constraint-report` — Show constraint satisfaction rates across all nodes

## References

- ADR-007: Schema Evolution and Type Governance — Graduated enforcement
- ADR-013: Source Citation Architecture — source_url requirement
- ADR-018: Verification Loops — Behavioral nudges + automated decay
- ADR-019: L0 Thinking Layer — Fragment promotion
- ADR-022 (this): Layer Promotion Constraints — SHACL-like write gates
- OHM-xdd4: Core Decay — confidence_at() with half_life_days
- OHM-wuki: Chain Validity — weakest-link bound for syntheses
- Pankaj Kumar: SHACL semantic firewalls — architectural constraint enforcement
- Chronofy: STL verification — formal proof of reasoning chain validity