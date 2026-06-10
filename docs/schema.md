# OHM Schema

## Nodes

```sql
CREATE TABLE IF NOT EXISTS ohm_nodes (
    id            VARCHAR PRIMARY KEY,
    label         VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,
    content       TEXT,
    created_by    VARCHAR NOT NULL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by    VARCHAR,
    confidence    FLOAT DEFAULT 1.0,
    visibility    VARCHAR DEFAULT 'team',     -- 'private','team','public'
    provenance    VARCHAR,                     -- 'conversation','research','bookmark','observation','feed-ingest'
    tags          JSON,                        -- JSON array of tags
    metadata      JSON,                        -- extensible key-value pairs
    priority      VARCHAR                       -- NULL | 'P0' | 'P1' | 'P2' | 'P3'
);
```

## Edges

```sql
CREATE TABLE IF NOT EXISTS ohm_edges (
    id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node       VARCHAR NOT NULL,
    to_node         VARCHAR NOT NULL,
    layer           VARCHAR NOT NULL,           -- 'L1','L2','L3','L4'
    edge_type       VARCHAR NOT NULL,
    confidence      FLOAT,
    probability     FLOAT,                      -- NULL = use confidence; distinct from confidence
    condition       TEXT,                       -- "holds_when: {fuel_mode: 'oil'}"
    provenance      VARCHAR,
    created_by      VARCHAR NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by      VARCHAR,
    challenge_of    VARCHAR,
    challenge_type  VARCHAR,                    -- Semantic type: 'CONTRADICTS','REFUTES','QUESTIONS','SUPPORTS','REFINES' (ADR-025: stored as metadata, edge_type always CHALLENGED_BY)
    urgency         VARCHAR,                    -- NULL | 'critical' | 'high' | 'medium' | 'low'
    metadata        JSON
);
```

**Key distinction:** `confidence` is the agent's belief strength (how sure am I?), while `probability` is an objective claim about the world (how likely is this outcome?). A supply chain edge can have confidence=0.9 (I'm very sure) and probability=0.2 (20% chance of disruption). See ADR-008.

## Observations

```sql
CREATE TABLE IF NOT EXISTS ohm_observations (
    id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id     VARCHAR NOT NULL,
    edge_id     VARCHAR,
    type        VARCHAR NOT NULL,               -- 'anomaly','measurement','pattern','challenge','support'
    value       FLOAT,
    baseline    FLOAT,
    sigma       FLOAT,                          -- standard deviations from baseline
    scale       VARCHAR,                        -- 'probability','count','currency','percent','binary','unknown' (ADR-025)
    source      VARCHAR,                        -- 'signal','research','conversation','analysis'
    source_url  VARCHAR,                        -- ADR-013: required for external sources
    created_by  VARCHAR NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata    JSON
);
```

**Observation scales** (ADR-025):
- `probability`: 0.0–1.0 probability
- `count`: Integer count
- `currency`: Monetary value
- `percent`: Percentage (0–100)
- `binary`: Boolean observation, normalized to `probability` (1.0=true, 0.0=false)
- `unknown`: Unspecified scale

## Agent State

```sql
CREATE TABLE IF NOT EXISTS ohm_agent_state (
    agent_name          VARCHAR PRIMARY KEY,
    current_focus       TEXT,
    active_patterns     JSON,                   -- JSON array of tags/topics
    last_sync           TIMESTAMP,
    confidence_threshold FLOAT DEFAULT 0.7,
    available_services  JSON,                   -- JSON array of service names
    current_session_id  VARCHAR,
    values              TEXT,                    -- JSON array of declared values
    goals               TEXT,                    -- JSON array of declared goals
    updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Agent Config

```sql
CREATE TABLE IF NOT EXISTS ohm_agent_config (
    agent_name  VARCHAR PRIMARY KEY,
    config      JSON NOT NULL,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Change Log

```sql
CREATE TABLE IF NOT EXISTS ohm_change_log (
    id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name  VARCHAR NOT NULL,
    row_id       VARCHAR NOT NULL,
    operation    VARCHAR NOT NULL,              -- 'INSERT','UPDATE','DELETE'
    agent_name   VARCHAR NOT NULL,
    layer        VARCHAR,
    snapshot_id  VARCHAR,
    changed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    change_data  JSON
);
```

## Schema Meta

```sql
CREATE TABLE IF NOT EXISTS ohm_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);
-- Current schema version: 0.5.0
-- Migrations applied automatically on connect
```

## Layer Descriptions

| Layer | Question | Ownership | Edge Types | Confidence Model |
|-------|----------|-----------|------------|-----------------|
| L1: Structure | Where does this belong? | Shared (any agent) | CONTAINS, BELONGS_TO, HAS_COMPONENT, PART_OF, CAPABLE_OF, VALUES, GOALS, INTERESTED_IN | Authoritative (by design) |
| L2: Flow | What connects what? | Shared + attributed | DERIVES_FROM, INFLUENCES, REFERENCES, USES, FEEDS, FLOWS_TO, NOTIFIES, TRUSTS, SERVES, BATCH_EXPIRES_BEFORE, TRANSFERRED_TO, OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY, CLOSED_BY, INVESTIGATED_BY, CONTAINED_BY, ERADICATED_BY, RECOVERED_BY, NEGOTIATES_WITH | High (validated) |
| L3: Knowledge | What does it mean? | Agent-owned, challengeable | CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS, REFINES, CONTRADICTS, LISTENS_TO, DEFERS_TO, COLLABORATES_WITH, APPLIES_TO, RELATED_TO, NEGATES, EXPECTED_LIKELIHOOD, ESCALATED_TO, DELEGATED_TO, THREAT_CLUSTER | Popperian (grows through refutation) |
| L4: Prospect | What's ahead? | Agent-owned, visible | EXPECTS, PLANS, RISKS, DEPENDS_ON, THREATENS, ENABLES, EXPECTS_FROM, PREDICTS, ORDERS_TEST, TRIGGERS_INCIDENT | Predictive (decays with time) |
| Private | Working notes | Agent-only | (not shared) | Not applicable |

## Boundary Rules

1. Any agent can write to L1 and L2 (with attribution)
2. Only the owning agent can update L3/L4 edges
3. Any agent can create CHALLENGED_BY, SUPPORTS, or REFINES edges referencing any L3/L4 edge
4. No agent can delete another agent's edge
5. Private layer is never shared or promoted automatically
6. Promotion from private to shared is per-agent, with per-agent confidence thresholds

## Node Types

`idea`, `source`, `person`, `concept`, `pattern`, `event`, `institution`, `technology`, `equipment`, `system`, `infrastructure`, `service`, `release`, `area`, `site`, `agent`, `skill`, `value`, `goal`, `topic`, `task`, `decision`, `fragment`

Plus TOPO types: `process`, `instrument`, `controller`, `valve`, `pump`, `motor`, `sensor`, `pipeline`, `vessel`, `reactor`, `heat_exchanger`, `tank`, `compressor`, `generator`, `transformer`, `circuit`, `bus`, `line`

## New Fields (Schema v0.5.0)

### `urgency` on edges
For time-sensitive information — cybersecurity alerts, customer support tickets, incident response. Filters: `GET /events?urgency=critical,high`. SDK: `create_edge(..., urgency='critical')`.

### `priority` on nodes
For entity importance — P0 through P3. SDK: `create_node(..., priority='P0')`.

### `probability` on edges
Distinct from confidence. Confidence = agent belief; probability = objective likelihood. Used for supply chain risk modeling and cascade simulation. SDK: `create_edge(..., probability=0.2)`.

### New edge types (v0.5.0)
- **NEGATES** (L3): Negative evidence — "fever absent NEGATES malaria candidate"
- **EXPECTED_LIKELIHOOD** (L3): Probability claim — "this disruption CAUSES output reduction at probability P"
- **ESCALATED_TO** (L3): Support escalation
- **DELEGATED_TO** (L3): Support delegation
- **THREAT_CLUSTER** (L3): Cybersecurity IOC linkage
- **ORDERS_TEST** (L4): Medical — trigger diagnostic test
- **TRIGGERS_INCIDENT** (L4): Cybersecurity — finding triggers incident
- **BATCH_EXPIRES_BEFORE** (L2): Retail inventory expiry
- **TRANSFERRED_TO** (L2): Customer support handoff
- **OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY, CLOSED_BY** (L2): Support state machine
- **INVESTIGATED_BY, CONTAINED_BY, ERADICATED_BY, RECOVERED_BY** (L2): Incident state machine
- **NEGOTIATES_WITH** (L2): SLAs, commitments

### Identity Override (X-Ohm-Agent)

The `created_by` field on nodes and edges is normally derived from the bearer token's mapped agent name. For multi-agent setups where agents share a token or use a generic admin token, the SDK sends an `X-Ohm-Agent` header with the `actor` parameter value.

The server honors `X-Ohm-Agent` when:
1. The bearer token is valid (agent is authenticated)
2. The authenticated agent has write access (not read-only)
3. The header value is a non-empty string

Example:
```python
g = connect_http("http://127.0.0.1:8710", actor="thalia",
                  token="ohm-metis-u0-...")
# → X-Ohm-Agent: thalia header sent automatically
# → created_by: "thalia" (not "metis" from the token)
```

This ensures correct attribution regardless of token configuration, which is especially useful during initial team onboarding before each agent has its own token.