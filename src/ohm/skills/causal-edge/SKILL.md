# Skill: Causal Edge

## When to use
Use causal edge types (`CAUSES`, `DEPENDS_ON`, `THREATENS`, `ENABLES`,
`INFLUENCES`) when the relationship represents a mechanistic or
probabilistic dependency that the Bayesian inference network should
traverse.

## Non-causal edges
Edges like `SUPPORTS`, `REFERENCES`, `MENTIONS`, `CONTAINS` do NOT flow
through the Bayesian network. Use them for structural relationships, not
causal claims.

## ADR-008: Two-stage sampling
Monte Carlo cascade simulation uses two-stage sampling:
1. Edge existence: sample `random() < confidence`
2. Effect propagation: sample `random() < probability`

Set both `confidence` (belief the edge exists) and `probability`
(likelihood the effect propagates) on causal edges.

## Cross-link requirement (ADR-018)
Synthesis-like nodes (pattern, idea, task, decision) must reference at
least one existing node via `connects_to` when created.
