# ADR-014: Agent Task Routing from Value of Information

## Status: Proposed

## Context

OHM's `/voi` endpoint identifies which observations would most reduce decision uncertainty, but there is no mechanism to automatically create tasks for agents to research those areas. Agents currently wait for manual assignment, which means:
- High-VoI nodes sit unobserved until a human notices
- Agents with relevant expertise aren't proactively engaged
- The mixture-of-experts approach (ADR-013) requires manual solicitation

## Decision

Implement a three-phase agent task routing system:

### Phase 1: VoI → Task Generation

When `/voi` identifies a node with VoI score above a threshold (default: 0.05), create an OHM task:
- `label`: "Research: {node_label}"
- `assigned_to`: determined by capability matching (Phase 2)
- `priority`: derived from VoI score and downstream decision utility_scale
- `metadata`: `{"voi_score": float, "downstream_decisions": [...], "edge_type": "observation"}`

New endpoint: `POST /voi/tasks?threshold=0.05&max_tasks=5`

### Phase 2: Capability Matching

Each agent has VALUES and SKILL edges in OHM. Match VoI nodes to agents by:
1. Tag overlap between node tags and agent SKILL tags
2. Semantic similarity between node content and agent VALUES
3. Source reliability weighting (agents with higher accuracy on similar topics get priority)

New endpoint: `GET /agents/match?node_id=X` returns ranked list of agents.

### Phase 3: Mixture-of-Experts Solicitation

For decisions with `utility_scale >= 0.7`:
1. Identify top-3 VoI ancestors
2. Solicit independent PERT estimates from 3+ agents
3. Weight by `source_reliability`
4. Socrates provides CHALLENGED_BY alternative estimates
5. Aggregate into final PERT distribution

New endpoint: `POST /voi/solicit?decision=X&n_agents=3`

## Consequences

**Positive:**
- Agents self-route to high-impact research
- No manual assignment needed for routine observations
- Mixture-of-experts runs automatically for important decisions
- Token spend is proportional to decision value (VoI × utility)

**Negative:**
- Agents burn tokens on low-value research if threshold is too low
- Need to rate-limit task generation (max N tasks per heartbeat)
- Socrates challenge mechanism requires careful prompt design

## Token Budget

Each agent gets a token budget per heartbeat cycle:
- Default: 5000 tokens for OHM-related research
- High-VoI tasks (>0.08): 10000 tokens
- Mixture-of-experts: 5000 tokens per agent per solicitation
- Socrates challenges: 3000 tokens per challenge

Total budget per heartbeat: ~25000 tokens across all agents.
This keeps costs bounded while ensuring high-value research happens.
