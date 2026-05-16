# OHM Schema

## Nodes

```sql
CREATE TABLE ohm_nodes (
    id VARCHAR PRIMARY KEY,
    label VARCHAR NOT NULL,
    type VARCHAR NOT NULL,  -- 'idea', 'source', 'person', 'concept', 'pattern', 'event', 'institution', 'technology'
    content TEXT,
    created_by VARCHAR NOT NULL,  -- agent name
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR,
    confidence FLOAT DEFAULT 1.0,
    visibility VARCHAR DEFAULT 'team',  -- 'private', 'team', 'public'
    provenance VARCHAR  -- 'conversation', 'research', 'bookmark', 'observation'
);
```

## Edges

```sql
CREATE TABLE ohm_edges (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    to_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    layer VARCHAR NOT NULL,  -- 'L1', 'L2', 'L3', 'L4'
    edge_type VARCHAR NOT NULL,
    confidence FLOAT,
    condition TEXT,  -- "holds_when: {fuel_mode: 'oil'}"
    provenance VARCHAR,
    created_by VARCHAR NOT NULL,  -- owning agent
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR,  -- for owner updates only
    challenge_of VARCHAR REFERENCES ohm_edges(id),  -- if this is a challenge edge
    challenge_type VARCHAR,  -- 'CHALLENGED_BY', 'SUPPORTS', 'REFINES', 'CONTRADICTS'
    
    -- Layer-specific edge types:
    -- L1: CONTAINS, BELONGS_TO, HAS_COMPONENT
    -- L2: DERIVES_FROM, INFLUENCES, REFERENCES, USES
    -- L3: CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS
    -- L4: EXPECTS, PLANS, RISKS, DEPENDS_ON
);
```

## Observations

```sql
CREATE TABLE ohm_observations (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    edge_id VARCHAR REFERENCES ohm_edges(id),
    type VARCHAR NOT NULL,  -- 'anomaly', 'measurement', 'pattern', 'challenge', 'support'
    value FLOAT,
    baseline FLOAT,
    sigma FLOAT,
    source VARCHAR,  -- 'signal', 'research', 'conversation', 'analysis'
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Agent State

```sql
CREATE TABLE ohm_agent_state (
    agent_name VARCHAR PRIMARY KEY,
    current_focus TEXT,
    active_patterns TEXT[],
    last_sync TIMESTAMP,
    confidence_threshold FLOAT DEFAULT 0.7,
    available_services TEXT[],  -- 'research', 'critique', 'synthesize', 'consult', 'audit'
    current_session_id VARCHAR,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Layer Descriptions

| Layer | Question | Ownership | Edge Types | Confidence Model |
|-------|----------|-----------|------------|-----------------|
| L1: Structure | Where does this belong? | Shared | CONTAINS, BELONGS_TO, HAS_COMPONENT | Authoritative (by design) |
| L2: Flow | What connects what? | Shared + attributed | DERIVES_FROM, INFLUENCES, REFERENCES | High (validated) |
| L3: Knowledge | What does it mean? | Agent-owned, challengeable | CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS | Popperian (grows through refutation) |
| L4: Prospect | What's ahead? | Agent-owned, visible | EXPECTS, PLANS, RISKS, DEPENDS_ON | Predictive (decays with time) |
| Private | Working notes | Agent-only | (not shared) | Not applicable |

## Boundary Rules

1. Any agent can write to L1 and L2 (with attribution)
2. Only the owning agent can update L3/L4 edges
3. Any agent can create CHALLENGED_BY, SUPPORTS, or REFINES edges referencing any L3/L4 edge
4. No agent can delete another agent's edge
5. Private layer is never shared or promoted automatically
6. Promotion from private to shared is per-agent, with per-agent confidence thresholds