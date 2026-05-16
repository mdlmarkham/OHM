# OHM Schema

## Nodes

```sql
CREATE TABLE ohm_nodes (
    id VARCHAR PRIMARY KEY,
    label VARCHAR NOT NULL,
    type VARCHAR NOT NULL,
    content TEXT,
    created_by VARCHAR NOT NULL,       -- agent name
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    updated_by VARCHAR,
    confidence FLOAT DEFAULT 1.0,
    visibility VARCHAR DEFAULT 'team',  -- 'private','team','public'
    provenance VARCHAR,                 -- 'conversation','research','bookmark','observation','feed-ingest'
    tags JSON,                          -- JSON array of tags
    metadata JSON                       -- extensible key-value pairs
);
```

## Edges

```sql
CREATE TABLE ohm_edges (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    to_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    layer VARCHAR NOT NULL,            -- 'L1','L2','L3','L4'
    edge_type VARCHAR NOT NULL,
    confidence FLOAT,
    condition TEXT,                      -- "holds_when: {fuel_mode: 'oil'}"
    provenance VARCHAR,
    created_by VARCHAR NOT NULL,        -- owning agent
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    updated_by VARCHAR,
    challenge_of VARCHAR REFERENCES ohm_edges(id),
    challenge_type VARCHAR,             -- 'CHALLENGED_BY','SUPPORTS','REFINES','CONTRADICTS'
    metadata JSON
);
```

## Observations

```sql
CREATE TABLE ohm_observations (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    edge_id VARCHAR REFERENCES ohm_edges(id),
    type VARCHAR NOT NULL,              -- 'anomaly','measurement','pattern','challenge','support'
    value FLOAT,
    baseline FLOAT,
    sigma FLOAT,
    source VARCHAR,                     -- 'signal','research','conversation','analysis'
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    metadata JSON
);
```

## Agent State

```sql
CREATE TABLE ohm_agent_state (
    agent_name VARCHAR PRIMARY KEY,
    current_focus TEXT,
    active_patterns JSON,               -- JSON array of tags/topics
    last_sync TIMESTAMP,
    confidence_threshold FLOAT DEFAULT 0.7,
    available_services JSON,            -- JSON array of service names
    current_session_id VARCHAR,
    updated_at TIMESTAMP DEFAULT now()
);
```

## Change Log

```sql
CREATE TABLE ohm_change_log (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name VARCHAR NOT NULL,
    row_id VARCHAR NOT NULL,
    operation VARCHAR NOT NULL,          -- 'INSERT','UPDATE','DELETE'
    agent_name VARCHAR NOT NULL,
    layer VARCHAR,
    snapshot_id VARCHAR,
    changed_at TIMESTAMP DEFAULT now(),
    change_data JSON
);
```

## Layer Descriptions

| Layer | Question | Ownership | Edge Types | Confidence Model |
|-------|----------|-----------|------------|-----------------|
| L1: Structure | Where does this belong? | Shared | CONTAINS, BELONGS_TO, HAS_COMPONENT | Authoritative (by design) |
| L2: Flow | What connects what? | Shared + attributed | DERIVES_FROM, INFLUENCES, REFERENCES | High (validated) |
| L3: Knowledge | What does it mean? | Agent-owned, challengeable | CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS | Popperian (grows through refutation) |
| L4: Prospect | What's ahead? | Agent-owned, visible | EXPECTS, PLANS, RISKS, DEPENDS_ON | Predictive (decays with time) |
| Private | Working notes | Agent-only | (not shared) | Not applicable |

## Boundary Rules

1. Any agent can write to L1 and L2 (with attribution)
2. Only the owning agent can update L3/L4 edges
3. Any agent can create CHALLENGED_BY, SUPPORTS, or REFINES edges referencing any L3/L4 edge
4. No agent can delete another agent's edge
5. Private layer is never shared or promoted automatically
6. Promotion from private to shared is per-agent, with per-agent confidence thresholds