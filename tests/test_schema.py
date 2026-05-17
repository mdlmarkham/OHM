"""Tests for the OHM database schema initialization."""

from ohm.schema import (
    LAYER_EDGE_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    VALID_LAYERS,
    VALID_NODE_TYPES,
    VALID_OBSERVATION_TYPES,
    VALID_VISIBILITIES,
    get_schema_version,
    initialize_schema,
    validate_edge_type,
    validate_node_type,
)


class TestSchemaValidation:
    """Tests for schema validation functions."""

    def test_valid_node_types(self):
        assert "concept" in VALID_NODE_TYPES
        assert "idea" in VALID_NODE_TYPES
        assert "source" in VALID_NODE_TYPES
        assert "invalid_type" not in VALID_NODE_TYPES

    def test_validate_node_type(self):
        assert validate_node_type("concept") is True
        assert validate_node_type("invalid") is False

    def test_valid_layers(self):
        assert VALID_LAYERS == {"L1", "L2", "L3", "L4"}

    def test_edge_types_by_layer(self):
        assert "CONTAINS" in LAYER_EDGE_TYPES["L1"]
        assert "DERIVES_FROM" in LAYER_EDGE_TYPES["L2"]
        assert "CAUSES" in LAYER_EDGE_TYPES["L3"]
        assert "CHALLENGED_BY" in LAYER_EDGE_TYPES["L3"]
        assert "EXPECTS" in LAYER_EDGE_TYPES["L4"]

    def test_validate_edge_type_valid(self):
        assert validate_edge_type("L1", "CONTAINS") is True
        assert validate_edge_type("L3", "CAUSES") is True
        assert validate_edge_type("L3", "CHALLENGED_BY") is True

    def test_validate_edge_type_invalid_layer(self):
        assert validate_edge_type("L5", "CAUSES") is False

    def test_validate_edge_type_wrong_layer(self):
        assert validate_edge_type("L1", "CAUSES") is False
        assert validate_edge_type("L4", "CONTAINS") is False

    def test_valid_observation_types(self):
        assert "anomaly" in VALID_OBSERVATION_TYPES
        assert "measurement" in VALID_OBSERVATION_TYPES

    def test_valid_visibilities(self):
        assert "private" in VALID_VISIBILITIES
        assert "team" in VALID_VISIBILITIES
        assert "public" in VALID_VISIBILITIES


class TestSchemaInitialization:
    """Tests for schema DDL execution."""

    def test_initialize_schema_creates_tables(self, test_db):
        """Verify all expected tables exist after initialization."""
        tables = test_db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        table_names = {row[0] for row in tables}

        assert "ohm_nodes" in table_names
        assert "ohm_edges" in table_names
        assert "ohm_observations" in table_names
        assert "ohm_agent_state" in table_names
        assert "ohm_change_feed" in table_names

    def test_initialize_schema_creates_indexes(self, test_db):
        """Verify indexes are created."""
        indexes = test_db.execute(
            "SELECT index_name FROM duckdb_indexes()"
        ).fetchall()
        index_names = {row[0] for row in indexes}

        assert "idx_edges_from" in index_names
        assert "idx_edges_to" in index_names
        assert "idx_edges_layer" in index_names
        assert "idx_nodes_type" in index_names

    def test_idempotent_initialization(self, test_db):
        """Running initialize_schema twice should not error."""
        initialize_schema(test_db)  # Second call
        tables = test_db.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchone()[0]
        assert tables >= 5  # At least our 5 tables


class TestSchemaVersion:
    """Tests for schema version tracking and migrations."""

    def test_schema_version_is_set(self, test_db):
        """After initialization, schema_version should be set."""
        initialize_schema(test_db)
        version = get_schema_version(test_db)
        assert version == SCHEMA_VERSION

    def test_schema_version_starts_at_base(self, test_db):
        """A fresh database should start at the current version after init."""
        initialize_schema(test_db)
        version = get_schema_version(test_db)
        assert version == "0.4.0"

    def test_migrations_applied_incrementally(self, test_db):
        """Migrations should be applied in order."""
        initialize_schema(test_db)
        # After init, all migrations should have been applied
        version = get_schema_version(test_db)
        assert version == SCHEMA_VERSION

    def test_migrations_are_idempotent(self, test_db):
        """Running initialize_schema twice should not re-apply migrations."""
        initialize_schema(test_db)
        version1 = get_schema_version(test_db)
        initialize_schema(test_db)
        version2 = get_schema_version(test_db)
        assert version1 == version2 == SCHEMA_VERSION

    def test_meta_table_exists(self, test_db):
        """The ohm_meta table should exist after initialization."""
        initialize_schema(test_db)
        result = test_db.execute(
            "SELECT COUNT(*) FROM ohm_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert result[0] == 1

    def test_migrations_list_not_empty(self):
        """The MIGRATIONS list should contain at least one migration."""
        assert len(MIGRATIONS) > 0

    def test_migrations_are_ordered(self):
        """Migrations should be in ascending version order."""
        versions = [m[0] for m in MIGRATIONS]
        assert versions == sorted(versions)

    def test_get_schema_version_on_empty_db(self):
        """get_schema_version on a database without ohm_meta should return 0.0.0."""
        import duckdb

        raw_conn = duckdb.connect(":memory:")
        try:
            # Create the table but don't seed the version row
            version = get_schema_version(raw_conn)
            assert version == "0.0.0"
        finally:
            raw_conn.close()
