"""
OHM Schema — SQL DDL for the knowledge graph.

All tables are designed for DuckDB. The schema supports:
- L1 (Structure): fully shared, authoritative
- L2 (Flow): shared with attribution
- L3 (Knowledge): agent-owned, challengeable
- L4 (Prospect): agent-owned, visible
- Private: agent-only (local DuckDB, not in DuckLake)
"""

SCHEMA_SQL = """
-- ============================================================
-- OHM Knowledge Graph Schema
-- ============================================================

-- Nodes: the entities in the graph
CREATE TABLE IF NOT EXISTS ohm_nodes (
    id VARCHAR PRIMARY KEY,
    label VARCHAR NOT NULL,
    type VARCHAR NOT NULL,  -- 'idea','source','person','concept','pattern','event','institution','technology','equipment','system','area','site'
    content TEXT,
    created_by VARCHAR NOT NULL,       -- agent name
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    updated_by VARCHAR,
    confidence FLOAT DEFAULT 1.0,
    visibility VARCHAR DEFAULT 'team',  -- 'private','team','public'
    provenance VARCHAR,                 -- 'conversation','research','bookmark','observation','feed-ingest'
    tags JSON,                          -- JSON array of tags for thematic discovery
    metadata JSON                       -- extensible key-value pairs
);

-- Edges: the connections between nodes
CREATE TABLE IF NOT EXISTS ohm_edges (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    to_node VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    layer VARCHAR NOT NULL,            -- 'L1','L2','L3','L4'
    edge_type VARCHAR NOT NULL,
    confidence FLOAT,
    condition TEXT,                     -- "holds_when: {fuel_mode: 'oil'}"
    provenance VARCHAR,
    created_by VARCHAR NOT NULL,       -- owning agent
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    updated_by VARCHAR,               -- for owner updates only
    challenge_of VARCHAR REFERENCES ohm_edges(id),   -- if this is a challenge edge
    challenge_type VARCHAR,            -- 'CHALLENGED_BY','SUPPORTS','REFINES','CONTRADICTS'
    metadata JSON
);

-- Observations: timestamped, attributed measurements
CREATE TABLE IF NOT EXISTS ohm_observations (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id VARCHAR NOT NULL REFERENCES ohm_nodes(id),
    edge_id VARCHAR REFERENCES ohm_edges(id),
    type VARCHAR NOT NULL,             -- 'anomaly','measurement','pattern','challenge','support'
    value FLOAT,
    baseline FLOAT,
    sigma FLOAT,
    source VARCHAR,                     -- 'signal','research','conversation','analysis'
    created_by VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    metadata JSON
);

-- Agent state: the hive mind awareness layer
CREATE TABLE IF NOT EXISTS ohm_agent_state (
    agent_name VARCHAR PRIMARY KEY,
    current_focus TEXT,
    active_patterns JSON,              -- JSON array of tags/topics
    last_sync TIMESTAMP,
    confidence_threshold FLOAT DEFAULT 0.7,
    available_services JSON,            -- JSON array of service names
    current_session_id VARCHAR,
    updated_at TIMESTAMP DEFAULT now()
);

-- Change log: append-only audit trail (feeds the change feed)
CREATE TABLE IF NOT EXISTS ohm_change_log (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    table_name VARCHAR NOT NULL,        -- 'ohm_nodes','ohm_edges','ohm_observations','ohm_agent_state'
    row_id VARCHAR NOT NULL,
    operation VARCHAR NOT NULL,          -- 'INSERT','UPDATE','DELETE'
    agent_name VARCHAR NOT NULL,
    layer VARCHAR,                       -- which layer was affected
    snapshot_id VARCHAR,                 -- for time travel
    changed_at TIMESTAMP DEFAULT now(),
    change_data JSON                    -- the row data after change
);

-- Snapshots: for time-travel queries
CREATE TABLE IF NOT EXISTS ohm_snapshots (
    id VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    description VARCHAR,
    created_at TIMESTAMP DEFAULT now()
);

-- ============================================================
-- Indexes for common query patterns
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_nodes_type ON ohm_nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_created_by ON ohm_nodes(created_by);
CREATE INDEX IF NOT EXISTS idx_nodes_visibility ON ohm_nodes(visibility);
CREATE INDEX IF NOT EXISTS idx_nodes_provenance ON ohm_nodes(provenance);

CREATE INDEX IF NOT EXISTS idx_edges_from ON ohm_edges(from_node);
CREATE INDEX IF NOT EXISTS idx_edges_to ON ohm_edges(to_node);
CREATE INDEX IF NOT EXISTS idx_edges_layer ON ohm_edges(layer);
CREATE INDEX IF NOT EXISTS idx_edges_type ON ohm_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_edges_created_by ON ohm_edges(created_by);
CREATE INDEX IF NOT EXISTS idx_edges_challenge_of ON ohm_edges(challenge_of);

CREATE INDEX IF NOT EXISTS idx_observations_node ON ohm_observations(node_id);
CREATE INDEX IF NOT EXISTS idx_observations_type ON ohm_observations(type);
CREATE INDEX IF NOT EXISTS idx_observations_created_by ON ohm_observations(created_by);

CREATE INDEX IF NOT EXISTS idx_change_log_table ON ohm_change_log(table_name);
CREATE INDEX IF NOT EXISTS idx_change_log_agent ON ohm_change_log(agent_name);
CREATE INDEX IF NOT EXISTS idx_change_log_time ON ohm_change_log(changed_at);
CREATE INDEX IF NOT EXISTS idx_change_log_snapshot ON ohm_change_log(snapshot_id);

-- Change feed view (alias for backward compatibility)
CREATE VIEW IF NOT EXISTS ohm_change_feed AS
    SELECT id, table_name, row_id, operation, agent_name, layer, snapshot_id, changed_at AS occurred_at, change_data AS new_data, NULL AS old_data FROM ohm_change_log;
"""

# Edge types by layer
EDGE_TYPES = {
    "L1": ["CONTAINS", "BELONGS_TO", "HAS_COMPONENT", "PART_OF"],
    "L2": ["DERIVES_FROM", "INFLUENCES", "REFERENCES", "USES", "FEEDS", "FLOWS_TO"],
    "L3": ["CAUSES", "CORRELATES_WITH", "PREDICTS", "EXPLAINS", "CHALLENGED_BY", "SUPPORTS", "REFINES", "CONTRADICTS"],
    "L4": ["EXPECTS", "PLANS", "RISKS", "DEPENDS_ON", "THREATENS", "ENABLES"],
}

# Alias for test compatibility
LAYER_EDGE_TYPES = EDGE_TYPES

# Node types
NODE_TYPES = [
    "idea", "source", "person", "concept", "pattern", "event",
    "institution", "technology",
    # Industrial (TOPO) types
    "equipment", "system", "area", "site",
]

# Valid sets for validation
VALID_NODE_TYPES = set(NODE_TYPES)
VALID_LAYERS = {"L1", "L2", "L3", "L4"}
VALID_OBSERVATION_TYPES = {"anomaly", "measurement", "pattern", "challenge", "support"}
VALID_VISIBILITIES = {"private", "team", "public"}

# Layer descriptions
LAYER_DESCRIPTIONS = {
    "L1": {
        "name": "Structure",
        "question": "Where does this belong?",
        "ownership": "Shared — all agents read/write",
        "confidence": "Authoritative (by design)",
        "example": "'Hungary has a constitution'",
    },
    "L2": {
        "name": "Flow",
        "question": "What connects what?",
        "ownership": "Shared with attribution",
        "confidence": "High (validated)",
        "example": "'This idea derives from that source'",
    },
    "L3": {
        "name": "Knowledge",
        "question": "What does it mean?",
        "ownership": "Agent-owned, challengeable",
        "confidence": "Popperian (grows through refutation)",
        "example": "'AND→OR conversion conf: 0.94 (Métis)'",
    },
    "L4": {
        "name": "Prospect",
        "question": "What's ahead?",
        "ownership": "Agent-owned, visible",
        "confidence": "Predictive (decays with time)",
        "example": "'Democratic institutions will hold conf: 0.65 (Clio)'",
    },
}


def initialize_schema(conn) -> None:
    """Execute the OHM schema DDL against a DuckDB connection."""
    conn.execute(SCHEMA_SQL)


def generate_node_id(label: str) -> str:
    """Generate a node ID from a label."""
    import re
    base = label.lower().replace(' ', '_')
    base = re.sub(r'[^a-z0-9_]', '', base)
    short = __import__('uuid').uuid4().hex[:6]
    return f"{base}_{short}"


def validate_node_type(node_type: str) -> bool:
    """Check if a node type is valid."""
    return node_type in VALID_NODE_TYPES


def validate_edge_type(layer: str, edge_type: str) -> bool:
    """Check if an edge type is valid for the given layer."""
    if layer not in VALID_LAYERS:
        return False
    return edge_type in LAYER_EDGE_TYPES.get(layer, set())