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
    SchemaConfig,
    DEFAULT_SCHEMA,
    TOPO_SCHEMA,
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

    # ── Multi-scenario edge types (OHM-af8.6) ──

    def test_l2_new_edge_types(self):
        """L2 edge types for multi-scenario support."""
        l2_new = [
            "BATCH_EXPIRES_BEFORE", "TRANSFERRED_TO",
            "OPENED_BY", "STARTED_BY", "AWAITING",
            "RESOLVED_BY", "CLOSED_BY",
            "INVESTIGATED_BY", "CONTAINED_BY",
            "ERADICATED_BY", "RECOVERED_BY",
            "NEGOTIATES_WITH",
        ]
        for et in l2_new:
            assert validate_edge_type("L2", et) is True, f"{et} should be valid in L2"

    def test_l3_new_edge_types(self):
        """L3 edge types for multi-scenario support."""
        l3_new = [
            "NEGATES", "EXPECTED_LIKELIHOOD",
            "ESCALATED_TO", "DELEGATED_TO", "THREAT_CLUSTER",
        ]
        for et in l3_new:
            assert validate_edge_type("L3", et) is True, f"{et} should be valid in L3"

    def test_l4_new_edge_types(self):
        """L4 edge types for multi-scenario support."""
        l4_new = ["ORDERS_TEST", "TRIGGERS_INCIDENT"]
        for et in l4_new:
            assert validate_edge_type("L4", et) is True, f"{et} should be valid in L4"

    def test_new_edge_types_wrong_layer(self):
        """New edge types should not be valid in wrong layers."""
        # L2 types should not work in L3
        assert validate_edge_type("L3", "BATCH_EXPIRES_BEFORE") is False
        assert validate_edge_type("L3", "TRANSFERRED_TO") is False
        # L3 types should not work in L2
        assert validate_edge_type("L2", "NEGATES") is False
        assert validate_edge_type("L2", "ESCALATED_TO") is False
        # L4 types should not work in L2
        assert validate_edge_type("L2", "ORDERS_TEST") is False
        assert validate_edge_type("L2", "TRIGGERS_INCIDENT") is False

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


class TestSchemaConfig:
    """Tests for SchemaConfig — configurable domain-specific schemas."""

    def test_default_schema_has_ohm_name(self):
        """Default schema should be named 'ohm'."""
        assert DEFAULT_SCHEMA.name == "ohm"

    def test_default_schema_has_base_node_types(self):
        """Default schema should include all base OHM node types."""
        assert "concept" in DEFAULT_SCHEMA.node_types
        assert "idea" in DEFAULT_SCHEMA.node_types
        assert "source" in DEFAULT_SCHEMA.node_types
        assert "person" in DEFAULT_SCHEMA.node_types
        assert "pattern" in DEFAULT_SCHEMA.node_types
        assert "agent" in DEFAULT_SCHEMA.node_types

    def test_default_schema_has_base_edge_types(self):
        """Default schema should include all base OHM edge types."""
        assert "CONTAINS" in DEFAULT_SCHEMA.layer_edge_types["L1"]
        assert "CAUSES" in DEFAULT_SCHEMA.layer_edge_types["L3"]
        assert "EXPECTS" in DEFAULT_SCHEMA.layer_edge_types["L4"]

    def test_default_schema_has_base_layers(self):
        """Default schema should have L1-L4 layers."""
        assert DEFAULT_SCHEMA.valid_layers == {"L1", "L2", "L3", "L4"}

    def test_default_schema_all_edge_types(self):
        """all_edge_types property should return union of all layer edge types."""
        all_types = DEFAULT_SCHEMA.all_edge_types
        assert "CONTAINS" in all_types
        assert "CAUSES" in all_types
        assert "EXPECTS" in all_types
        assert "DERIVES_FROM" in all_types

    def test_default_schema_validate_node_type(self):
        """validate_node_type should work for default schema."""
        assert DEFAULT_SCHEMA.validate_node_type("concept") is True
        assert DEFAULT_SCHEMA.validate_node_type("invalid_type") is False

    def test_default_schema_validate_edge_type(self):
        """validate_edge_type should work for default schema."""
        assert DEFAULT_SCHEMA.validate_edge_type("L1", "CONTAINS") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L3", "CAUSES") is True
        assert DEFAULT_SCHEMA.validate_edge_type("L1", "CAUSES") is False
        assert DEFAULT_SCHEMA.validate_edge_type("L5", "CAUSES") is False

    def test_default_schema_validate_layer(self):
        """validate_layer should work for default schema."""
        assert DEFAULT_SCHEMA.validate_layer("L1") is True
        assert DEFAULT_SCHEMA.validate_layer("L4") is True
        assert DEFAULT_SCHEMA.validate_layer("L5") is False

    def test_default_schema_to_dict(self):
        """to_dict should serialize the schema configuration."""
        d = DEFAULT_SCHEMA.to_dict()
        assert d["name"] == "ohm"
        assert "concept" in d["node_types"]
        assert "L1" in d["layer_edge_types"]
        assert "L1" in d["layer_descriptions"]
        assert isinstance(d["node_types"], list)

    def test_custom_schema(self):
        """Custom schema should allow extending node types."""
        custom = SchemaConfig(
            name="custom",
            node_types=VALID_NODE_TYPES | {"custom_type"},
        )
        assert "custom_type" in custom.node_types
        assert "concept" in custom.node_types
        assert custom.name == "custom"

    def test_custom_schema_with_extra_layer(self):
        """Custom schema should allow adding new layers."""
        custom = SchemaConfig(
            name="extended",
            edge_types_by_layer={
                **LAYER_EDGE_TYPES,
                "L5": frozenset({"CUSTOM_EDGE", "ANOTHER_EDGE"}),
            },
            layer_descriptions={
                **{
                    "L1": "Structure",
                    "L2": "Flow",
                    "L3": "Knowledge",
                    "L4": "Prospect",
                },
                "L5": "Custom — Extended domain layer",
            },
        )
        assert custom.validate_layer("L5") is True
        assert custom.validate_edge_type("L5", "CUSTOM_EDGE") is True
        assert custom.validate_edge_type("L5", "CAUSES") is False

    def test_topo_schema_name(self):
        """TOPO schema should be named 'topo'."""
        assert TOPO_SCHEMA.name == "topo"

    def test_topo_schema_has_industrial_node_types(self):
        """TOPO schema should include industrial node types."""
        assert "equipment" in TOPO_SCHEMA.node_types
        assert "system" in TOPO_SCHEMA.node_types
        assert "area" in TOPO_SCHEMA.node_types
        assert "site" in TOPO_SCHEMA.node_types
        # TOPO-specific types
        assert "process" in TOPO_SCHEMA.node_types
        assert "sensor" in TOPO_SCHEMA.node_types
        assert "valve" in TOPO_SCHEMA.node_types
        assert "pump" in TOPO_SCHEMA.node_types
        assert "motor" in TOPO_SCHEMA.node_types
        assert "pipeline" in TOPO_SCHEMA.node_types
        assert "reactor" in TOPO_SCHEMA.node_types

    def test_topo_schema_has_base_node_types(self):
        """TOPO schema should also include all base OHM node types."""
        assert "concept" in TOPO_SCHEMA.node_types
        assert "idea" in TOPO_SCHEMA.node_types
        assert "agent" in TOPO_SCHEMA.node_types

    def test_topo_schema_has_base_edge_types(self):
        """TOPO schema should include all base OHM edge types."""
        assert "CONTAINS" in TOPO_SCHEMA.layer_edge_types["L1"]
        assert "FEEDS" in TOPO_SCHEMA.layer_edge_types["L2"]
        assert "FLOWS_TO" in TOPO_SCHEMA.layer_edge_types["L2"]
        assert "DEPENDS_ON" in TOPO_SCHEMA.layer_edge_types["L4"]
        assert "CAUSES" in TOPO_SCHEMA.layer_edge_types["L3"]

    def test_topo_schema_layer_descriptions(self):
        """TOPO schema should have industrial-specific layer descriptions."""
        assert "Physical hierarchy" in TOPO_SCHEMA.layer_descriptions["L1"]
        assert "Process flows" in TOPO_SCHEMA.layer_descriptions["L2"]
        assert "Operational insights" in TOPO_SCHEMA.layer_descriptions["L3"]
        assert "Predictive maintenance" in TOPO_SCHEMA.layer_descriptions["L4"]

    def test_topo_schema_observation_types(self):
        """TOPO schema should include industrial observation types."""
        assert "vibration" in TOPO_SCHEMA.observation_types
        assert "temperature" in TOPO_SCHEMA.observation_types
        assert "pressure" in TOPO_SCHEMA.observation_types
        assert "flow_rate" in TOPO_SCHEMA.observation_types
        # Base types should still be present
        assert "anomaly" in TOPO_SCHEMA.observation_types
        assert "measurement" in TOPO_SCHEMA.observation_types

    def test_topo_schema_observation_sources(self):
        """TOPO schema should include industrial observation sources."""
        assert "scada" in TOPO_SCHEMA.observation_sources
        assert "dcs" in TOPO_SCHEMA.observation_sources
        assert "historian" in TOPO_SCHEMA.observation_sources
        # Base sources should still be present
        assert "signal" in TOPO_SCHEMA.observation_sources
        assert "research" in TOPO_SCHEMA.observation_sources

    def test_topo_schema_provenances(self):
        """TOPO schema should include industrial provenance types."""
        assert "inspection" in TOPO_SCHEMA.provenances
        assert "monitoring" in TOPO_SCHEMA.provenances
        assert "audit" in TOPO_SCHEMA.provenances
        assert "simulation" in TOPO_SCHEMA.provenances
        # Base provenances should still be present
        assert "conversation" in TOPO_SCHEMA.provenances
        assert "research" in TOPO_SCHEMA.provenances

    def test_topo_schema_validate_node_type(self):
        """TOPO schema should validate both base and industrial node types."""
        assert TOPO_SCHEMA.validate_node_type("equipment") is True
        assert TOPO_SCHEMA.validate_node_type("sensor") is True
        assert TOPO_SCHEMA.validate_node_type("concept") is True
        assert TOPO_SCHEMA.validate_node_type("nonexistent_type") is False

    def test_topo_schema_validate_edge_type(self):
        """TOPO schema should validate edge types correctly."""
        assert TOPO_SCHEMA.validate_edge_type("L2", "FEEDS") is True
        assert TOPO_SCHEMA.validate_edge_type("L2", "FLOWS_TO") is True
        assert TOPO_SCHEMA.validate_edge_type("L4", "DEPENDS_ON") is True
        assert TOPO_SCHEMA.validate_edge_type("L1", "CAUSES") is False

    def test_topo_schema_to_dict(self):
        """TOPO schema to_dict should serialize correctly."""
        d = TOPO_SCHEMA.to_dict()
        assert d["name"] == "topo"
        assert "equipment" in d["node_types"]
        assert "sensor" in d["node_types"]
        assert "vibration" in d["observation_types"]
        assert "scada" in d["observation_sources"]

    def test_schema_config_equality(self):
        """Two default schemas should have the same node types."""
        s1 = SchemaConfig()
        s2 = SchemaConfig()
        assert s1.node_types == s2.node_types
        assert s1.layer_edge_types == s2.layer_edge_types

    def test_schema_config_independence(self):
        """Modifying one SchemaConfig should not affect another."""
        custom = SchemaConfig(name="custom", node_types=VALID_NODE_TYPES | {"custom_type"})
        assert "custom_type" in custom.node_types
        assert "custom_type" not in DEFAULT_SCHEMA.node_types
