"""OHM database schema — DDL statements and schema management.

Tables:
    - ohm_nodes: Graph nodes (ideas, sources, people, concepts, etc.)
    - ohm_edges: Directed edges between nodes with layer/type/confidence
    - ohm_observations: Observations attached to nodes or edges
    - ohm_agent_state: Per-agent focus, values, goals, and configuration
    - ohm_change_feed: Append-only log of graph mutations
    - ohm_snapshots: Named snapshots for time-travel queries

Layer model (L1-L4):
    L1: Structure — Fully shared, all agents read/write
    L2: Flow — Shared with attribution
    L3: Knowledge — Agent-owned, challengeable
    L4: Prospect — Agent-owned, visible
    Private: Agent-only, not shared
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

# ── Node Types ──────────────────────────────────────────────────────────────

VALID_NODE_TYPES = frozenset({
    "idea", "source", "person", "concept", "pattern",
    "event", "institution", "technology", "equipment",
    "system", "area", "site",
})

VALID_VISIBILITIES = frozenset({"private", "team", "public"})

VALID_PROVENANCES = frozenset({
    "conversation", "research", "bookmark", "observation", "feed-ingest",
})

# ── Edge Types by Layer ─────────────────────────────────────────────────────

LAYER_EDGE_TYPES: dict[str, frozenset[str]] = {
    "L1": frozenset({"CONTAINS", "BELONGS_TO", "HAS_COMPONENT", "PART_OF"}),
    "L2": frozenset({"DERIVES_FROM", "INFLUENCES", "REFERENCES", "USES", "FEEDS", "FLOWS_TO"}),
    "L3": frozenset({
        "CAUSES", "CORRELATES_WITH", "PREDICTS", "EXPLAINS",
        "CHALLENGED_BY", "SUPPORTS", "REFINES", "CONTRADICTS",
    }),
    "L4": frozenset({"EXPECTS", "PLANS", "RISKS", "DEPENDS_ON", "THREATENS", "ENABLES"}),
}

ALL_EDGE_TYPES: frozenset[str] = frozenset().union(*LAYER_EDGE_TYPES.values())

VALID_LAYERS = frozenset(LAYER_EDGE_TYPES.keys())

# ── Observation Types ───────────────────────────────────────────────────────

VALID_OBSERVATION_TYPES = frozenset({
    "anomaly", "measurement", "pattern", "challenge", "support",
})

VALID_OBSERVATION_SOURCES = frozenset({
    "signal", "research", "conversation", "analysis",
})

# ── DDL Statements ──────────────────────────────────────────────────────────

DDL_STATEMENTS: list[str] = [
    # ── Nodes ────────────────────────────────────────────────────────────
    """
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
        visibility    VARCHAR DEFAULT 'team',
        provenance    VARCHAR,
        tags          JSON,
        metadata      JSON
    );
    """,
    # ── Edges ────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_edges (
        id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        from_node       VARCHAR NOT NULL,
        to_node         VARCHAR NOT NULL,
        layer           VARCHAR NOT NULL,
        edge_type       VARCHAR NOT NULL,
        confidence      FLOAT,
        condition       TEXT,
        provenance      VARCHAR,
        created_by      VARCHAR NOT NULL,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by      VARCHAR,
        challenge_of    VARCHAR,
        challenge_type  VARCHAR,
        metadata        JSON
    );
    """,
    # ── Observations ─────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_observations (
        id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        node_id     VARCHAR,
        edge_id     VARCHAR,
        type        VARCHAR NOT NULL,
        value       FLOAT,
        baseline    FLOAT,
        sigma       FLOAT,
        source      VARCHAR,
        created_by  VARCHAR NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        metadata    JSON
    );
    """,
    # ── Agent State ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_agent_state (
        agent_name           VARCHAR PRIMARY KEY,
        current_focus        TEXT,
        active_patterns      TEXT,
        last_sync            TIMESTAMP,
        confidence_threshold FLOAT DEFAULT 0.7,
        available_services   TEXT,
        current_session_id   VARCHAR,
        values               TEXT,
        goals                TEXT,
        updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Change Feed ──────────────────────────────────────────────────────
    """
    CREATE SEQUENCE IF NOT EXISTS seq_change_feed START 1;
    CREATE TABLE IF NOT EXISTS ohm_change_feed (
        id          BIGINT PRIMARY KEY DEFAULT nextval('seq_change_feed'),
        table_name  VARCHAR NOT NULL,
        row_id      VARCHAR NOT NULL,
        operation   VARCHAR NOT NULL,
        agent_name  VARCHAR NOT NULL,
        old_data    JSON,
        new_data    JSON,
        occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Snapshots ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_snapshots (
        id          VARCHAR PRIMARY KEY,
        description VARCHAR,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Change Log (compatibility with store.py) ─────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_change_log (
        id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        table_name  VARCHAR NOT NULL,
        row_id      VARCHAR NOT NULL,
        operation   VARCHAR NOT NULL,
        agent_name  VARCHAR NOT NULL,
        layer       VARCHAR,
        snapshot_id VARCHAR,
        changed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        change_data JSON
    );
    """,
    # ── Agent Config (admin-set, read-only for agents) ───────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_agent_config (
        agent_name           VARCHAR PRIMARY KEY,
        optimization_target  VARCHAR NOT NULL,
        services             JSON,
        confidence_threshold FLOAT DEFAULT 0.7,
        sync_interval_sec    INTEGER DEFAULT 300,
        created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── Schema Metadata ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS ohm_meta (
        key   VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    );
    """,
]

# ── Schema Version ──────────────────────────────────────────────────────────

SCHEMA_VERSION = "0.3.0"

# ── Migrations ──────────────────────────────────────────────────────────────
# Each migration is (version, description, list_of_sql_statements).
# Applied incrementally: if current version < migration version, apply it.

MIGRATIONS: list[tuple[str, str, list[str]]] = [
    ("0.2.0", "add values/goals columns to agent_state", [
        "ALTER TABLE ohm_agent_state ADD COLUMN values TEXT",
        "ALTER TABLE ohm_agent_state ADD COLUMN goals TEXT",
    ]),
    ("0.3.0", "add tags/metadata JSON columns and agent_config table", [
        "ALTER TABLE ohm_nodes ADD COLUMN tags JSON",
        "ALTER TABLE ohm_nodes ADD COLUMN metadata JSON",
        "ALTER TABLE ohm_edges ADD COLUMN metadata JSON",
        "ALTER TABLE ohm_observations ADD COLUMN metadata JSON",
    ]),
]

# ── Indexes ─────────────────────────────────────────────────────────────────

INDEX_DDL: list[str] = [
    # Edge traversal indexes
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON ohm_edges(from_node);",
    "CREATE INDEX IF NOT EXISTS idx_edges_to ON ohm_edges(to_node);",
    "CREATE INDEX IF NOT EXISTS idx_edges_layer ON ohm_edges(layer);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON ohm_edges(edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_created_by ON ohm_edges(created_by);",
    "CREATE INDEX IF NOT EXISTS idx_edges_challenge_of ON ohm_edges(challenge_of);",
    # Node lookup indexes
    "CREATE INDEX IF NOT EXISTS idx_nodes_type ON ohm_nodes(type);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_created_by ON ohm_nodes(created_by);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_visibility ON ohm_nodes(visibility);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_provenance ON ohm_nodes(provenance);",
    # Observation indexes
    "CREATE INDEX IF NOT EXISTS idx_obs_node ON ohm_observations(node_id);",
    "CREATE INDEX IF NOT EXISTS idx_obs_edge ON ohm_observations(edge_id);",
    "CREATE INDEX IF NOT EXISTS idx_obs_type ON ohm_observations(type);",
    "CREATE INDEX IF NOT EXISTS idx_obs_created_by ON ohm_observations(created_by);",
    # Change feed index
    "CREATE INDEX IF NOT EXISTS idx_feed_agent ON ohm_change_feed(agent_name);",
    "CREATE INDEX IF NOT EXISTS idx_feed_time ON ohm_change_feed(occurred_at);",
    # Composite index for CTE traversal (from_node + layer + edge_type)
    "CREATE INDEX IF NOT EXISTS idx_edges_traversal ON ohm_edges(from_node, layer, edge_type);",
]


def initialize_schema(conn: "DuckDBPyConnection") -> None:
    """Create all tables and indexes if they don't exist.

    Then applies any pending migrations based on the stored schema version.

    Args:
        conn: An active DuckDB connection.
    """
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)
    for idx in INDEX_DDL:
        conn.execute(idx)
    # Set initial schema version if not present
    _ensure_meta_table(conn)
    # Apply migrations incrementally
    _apply_migrations(conn)
    # Seed default agent configs
    _seed_agent_configs(conn)


def _ensure_meta_table(conn: "DuckDBPyConnection") -> None:
    """Ensure the ohm_meta table exists and has a schema_version entry."""
    # Table is created by DDL_STATEMENTS, but ensure version row exists
    existing = conn.execute(
        "SELECT COUNT(*) FROM ohm_meta WHERE key = 'schema_version'"
    ).fetchone()
    if existing is None or existing[0] == 0:
        conn.execute(
            "INSERT INTO ohm_meta (key, value) VALUES ('schema_version', ?)",
            ["0.1.0"],  # Base version before migrations
        )


def _apply_migrations(conn: "DuckDBPyConnection") -> None:
    """Apply pending migrations based on the current schema version."""
    current = conn.execute(
        "SELECT value FROM ohm_meta WHERE key = 'schema_version'"
    ).fetchone()
    current_version = current[0] if current else "0.1.0"

    for version, description, statements in MIGRATIONS:
        if current_version < version:
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except Exception:
                    # DuckDB ALTER TABLE ADD COLUMN fails if column exists
                    pass  # Column/table already exists — safe to ignore
            conn.execute(
                "UPDATE ohm_meta SET value = ? WHERE key = 'schema_version'",
                [version],
            )
            current_version = version


def _seed_agent_configs(conn: "DuckDBPyConnection") -> None:
    """Seed default agent configurations if the table is empty.

    Each agent has distinct optimization targets and services.
    Config is admin-set (read-only for agents) — this is a governance
    decision, not a technical constraint.
    """
    import json

    existing = conn.execute("SELECT COUNT(*) FROM ohm_agent_config").fetchone()
    if existing and existing[0] > 0:
        return  # Already seeded

    agents = [
        {
            "agent_name": "metis",
            "optimization_target": "pattern density and cross-domain connections",
            "services": json.dumps(["research", "critique", "synthesize", "pattern-detection"]),
            "confidence_threshold": 0.7,
            "sync_interval_sec": 300,
        },
        {
            "agent_name": "clio",
            "optimization_target": "source coverage and confidence",
            "services": json.dumps(["deep-research", "source-analysis", "citation-tracking"]),
            "confidence_threshold": 0.8,
            "sync_interval_sec": 600,
        },
        {
            "agent_name": "hephaestus",
            "optimization_target": "completeness and reproducibility",
            "services": json.dumps(["code-audit", "security-review", "correctness-check"]),
            "confidence_threshold": 0.9,
            "sync_interval_sec": 120,
        },
        {
            "agent_name": "socrates",
            "optimization_target": "falsifiability and logical coherence",
            "services": json.dumps(["critique", "devils-advocate", "logic-check"]),
            "confidence_threshold": 0.5,
            "sync_interval_sec": 300,
        },
    ]
    for a in agents:
        conn.execute(
            """INSERT OR IGNORE INTO ohm_agent_config
               (agent_name, optimization_target, services, confidence_threshold, sync_interval_sec)
               VALUES (?, ?, ?, ?, ?)""",
            [a["agent_name"], a["optimization_target"], a["services"],
             a["confidence_threshold"], a["sync_interval_sec"]],
        )


def get_schema_version(conn: "DuckDBPyConnection") -> str:
    """Return the current schema version from the database.

    Args:
        conn: An active DuckDB connection.

    Returns:
        The schema version string (e.g., '0.3.0').
    """
    result = conn.execute(
        "SELECT value FROM ohm_meta WHERE key = 'schema_version'"
    ).fetchone()
    return result[0] if result else "0.0.0"


def validate_edge_type(layer: str, edge_type: str) -> bool:
    """Check that *edge_type* is valid for the given *layer*.

    Returns True if valid, False otherwise.
    """
    allowed = LAYER_EDGE_TYPES.get(layer)
    if allowed is None:
        return False
    return edge_type in allowed


def validate_node_type(node_type: str) -> bool:
    """Check that *node_type* is a known type."""
    return node_type in VALID_NODE_TYPES


def generate_node_id(label: str) -> str:
    """Generate a human-readable node ID from a label.

    Converts to lowercase, replaces spaces and special characters with
    underscores, transliterates unicode to ASCII, and appends a short
    suffix for uniqueness.

    Examples:
        'AND→OR conversion' → 'and-or-conversion_a1b2c3'
        'Métis Pattern'     → 'metis-pattern_d4e5f6'
    """
    import unicodedata
    import re

    # Normalize unicode: decompose accented chars, then strip diacritics
    normalized = unicodedata.normalize('NFKD', label)
    ascii_label = normalized.encode('ascii', 'ignore').decode('ascii')

    # Replace any remaining non-alphanumeric chars with underscores
    base = re.sub(r'[^a-zA-Z0-9]+', '_', ascii_label).strip('_').lower()
    if not base:
        base = "node"

    suffix = uuid.uuid4().hex[:6]
    return f"{base}_{suffix}"


# Compatibility: single-string schema for modules that expect SCHEMA_SQL
SCHEMA_SQL = "\n".join(DDL_STATEMENTS + INDEX_DDL)

# Compatibility exports for server.py
EDGE_TYPES = {k: list(v) for k, v in LAYER_EDGE_TYPES.items()}
NODE_TYPES = sorted(VALID_NODE_TYPES)
LAYER_DESCRIPTIONS = {
    "L1": "Structure — Fully shared, all agents read/write",
    "L2": "Flow — Shared with attribution",
    "L3": "Knowledge — Agent-owned, challengeable",
    "L4": "Prospect — Agent-owned, visible",
}
