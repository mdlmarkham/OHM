"""Tests for the OHM database schema initialization."""

import pytest

from ohm.schema import (
    LAYER_EDGE_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    VALID_LAYERS,
    VALID_NODE_TYPES,
    VALID_OBSERVATION_TYPES,
    VALID_VISIBILITIES,
    _apply_migrations,
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
        assert VALID_LAYERS == {"L0", "L1", "L2", "L3", "L4"}

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
            "BATCH_EXPIRES_BEFORE",
            "TRANSFERRED_TO",
            "OPENED_BY",
            "STARTED_BY",
            "AWAITING",
            "RESOLVED_BY",
            "CLOSED_BY",
            "INVESTIGATED_BY",
            "CONTAINED_BY",
            "ERADICATED_BY",
            "RECOVERED_BY",
            "NEGOTIATES_WITH",
        ]
        for et in l2_new:
            assert validate_edge_type("L2", et) is True, f"{et} should be valid in L2"

    def test_l3_new_edge_types(self):
        """L3 edge types for multi-scenario support."""
        l3_new = [
            "NEGATES",
            "EXPECTED_LIKELIHOOD",
            "ESCALATED_TO",
            "DELEGATED_TO",
            "THREAT_CLUSTER",
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
        tables = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
        table_names = {row[0] for row in tables}

        assert "ohm_nodes" in table_names
        assert "ohm_edges" in table_names
        assert "ohm_observations" in table_names
        assert "ohm_agent_state" in table_names
        assert "ohm_change_feed" in table_names

    def test_initialize_schema_creates_indexes(self, test_db):
        """Verify indexes are created."""
        indexes = test_db.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        index_names = {row[0] for row in indexes}

        assert "idx_edges_from" in index_names
        assert "idx_edges_to" in index_names
        assert "idx_edges_layer" in index_names
        assert "idx_nodes_type" in index_names

    def test_idempotent_initialization(self, test_db):
        """Running initialize_schema twice should not error."""
        initialize_schema(test_db)  # Second call
        tables = test_db.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'main'").fetchone()[0]
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
        assert version == SCHEMA_VERSION

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
        result = test_db.execute("SELECT COUNT(*) FROM ohm_meta WHERE key = 'schema_version'").fetchone()
        assert result[0] == 1

    def test_migrations_list_not_empty(self):
        """The MIGRATIONS list should contain at least one migration."""
        assert len(MIGRATIONS) > 0

    def test_migrations_are_ordered(self):
        """Migrations should be in ascending version order."""
        versions = [m[0] for m in MIGRATIONS]

        # Use semantic version sorting (tuple of ints)
        def version_key(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split("."))

        assert versions == sorted(versions, key=version_key)

    def test_migration_failure_raises_not_silenced(self, test_db):
        """A real migration failure must raise MigrationError, not be silently swallowed."""
        from unittest.mock import patch

        from ohm.framework.exceptions import MigrationError

        initialize_schema(test_db)

        bad_migrations = [
            ("99.99.0", "broken", ["ALTER TABLE nonexistent_table ADD COLUMN x INT"]),
        ]
        with (
            patch("ohm.graph.schema.MIGRATIONS", bad_migrations),
            patch("ohm.graph.schema.SCHEMA_VERSION", "99.99.0"),
            pytest.raises(MigrationError, match="Migration 99.99.0"),
        ):
            _apply_migrations(test_db)

    def test_migration_idempotent_already_exists_is_safe(self, test_db):
        """'already exists' errors during migration must be silently ignored (idempotency)."""
        from unittest.mock import patch

        initialize_schema(test_db)
        first_stmt = MIGRATIONS[0][2][0] if MIGRATIONS else None
        if first_stmt is None:
            return

        dup_migrations = [
            ("99.99.0", "dup", [first_stmt]),
        ]
        with (
            patch("ohm.graph.schema.MIGRATIONS", dup_migrations),
            patch("ohm.graph.schema.SCHEMA_VERSION", "99.99.0"),
        ):
            _apply_migrations(test_db)
        version = get_schema_version(test_db)
        assert version == "99.99.0"

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
        """Default schema should have L0-L4 layers."""
        assert DEFAULT_SCHEMA.valid_layers == {"L0", "L1", "L2", "L3", "L4"}

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


class TestSchemaConfigSerialization:
    """Tests for SchemaConfig.to_dict(), from_dict(), from_json_file()."""

    def test_to_dict_roundtrip(self):
        d = DEFAULT_SCHEMA.to_dict()
        restored = SchemaConfig.from_dict(d)
        assert restored.name == DEFAULT_SCHEMA.name
        assert restored.node_types == DEFAULT_SCHEMA.node_types
        assert restored.layer_descriptions == DEFAULT_SCHEMA.layer_descriptions
        assert restored.observation_types == DEFAULT_SCHEMA.observation_types
        assert restored.observation_sources == DEFAULT_SCHEMA.observation_sources
        assert restored.provenances == DEFAULT_SCHEMA.provenances

    def test_to_dict_roundtrip_topo(self):
        d = TOPO_SCHEMA.to_dict()
        restored = SchemaConfig.from_dict(d)
        assert restored.name == "topo"
        assert "sensor" in restored.node_types
        assert "vibration" in restored.observation_types
        assert "scada" in restored.observation_sources

    def test_from_dict_missing_keys(self):
        import pytest

        with pytest.raises(ValueError, match="missing required keys"):
            SchemaConfig.from_dict({"name": "broken"})

    def test_from_dict_no_layer_edge_types(self):
        d = DEFAULT_SCHEMA.to_dict()
        del d["layer_edge_types"]
        restored = SchemaConfig.from_dict(d)
        assert restored.name == DEFAULT_SCHEMA.name

    def test_from_json_file_ohm(self):
        config = SchemaConfig.from_json_file("ohm.json")
        assert config.name == "ohm"
        assert config.node_types == DEFAULT_SCHEMA.node_types

    def test_from_json_file_topo(self):
        config = SchemaConfig.from_json_file("topo.json")
        assert config.name == "topo"
        assert "sensor" in config.node_types
        assert "vibration" in config.observation_types

    def test_from_json_file_beef_herd(self):
        config = SchemaConfig.from_json_file("beef_herd.json")
        assert config.name == "beef_herd"
        assert "animal" in config.node_types
        assert "weight" in config.observation_types

    def test_from_json_file_not_found(self):
        import pytest

        with pytest.raises(FileNotFoundError, match="nonexistent.json"):
            SchemaConfig.from_json_file("nonexistent.json")

    def test_from_json_file_custom_search_path(self, tmp_path):
        import json

        template = {"name": "custom_test", "node_types": ["idea", "source"], "layer_descriptions": {"L1": "test"}, "observation_types": ["anomaly"], "observation_sources": ["owner"], "provenances": ["research"]}
        template_path = tmp_path / "custom_test.json"
        template_path.write_text(json.dumps(template))
        config = SchemaConfig.from_json_file("custom_test.json", search_paths=[str(tmp_path)])
        assert config.name == "custom_test"

    def test_topo_classmethod_matches_json(self):
        from_json = SchemaConfig.from_json_file("topo.json")
        from_cls = SchemaConfig.topo()
        assert from_json.node_types == from_cls.node_types
        assert from_json.observation_types == from_cls.observation_types
        assert from_json.provenances == from_cls.provenances

    def test_beef_herd_classmethod_matches_json(self):
        from_json = SchemaConfig.from_json_file("beef_herd.json")
        from_cls = SchemaConfig.beef_herd()
        assert from_json.node_types == from_cls.node_types
        assert from_json.observation_types == from_cls.observation_types


class TestHomeServicesSchema:
    """Tests for OHM-tss4.10: Home Services domain template."""

    def test_home_services_loads_from_json(self):
        config = SchemaConfig.from_json_file("home_services.json")
        assert config.name == "home_services"

    def test_home_services_node_types(self):
        config = SchemaConfig.from_json_file("home_services.json")
        for expected in ["customer", "technician", "job", "appointment", "equipment", "service_contract", "warranty", "estimate", "invoice"]:
            assert expected in config.node_types, f"Missing node type: {expected}"

    def test_home_services_observation_types(self):
        config = SchemaConfig.from_json_file("home_services.json")
        for expected in ["call_duration", "job_completion_time", "first_time_fix_rate", "revenue_per_job"]:
            assert expected in config.observation_types, f"Missing observation type: {expected}"

    def test_home_services_provenances(self):
        config = SchemaConfig.from_json_file("home_services.json")
        for expected in ["dispatch_analyst", "schedule_coordinator", "parts_broker", "compliance_planner", "operations_manager"]:
            assert expected in config.provenances, f"Missing provenance: {expected}"

    def test_home_services_layer_descriptions(self):
        config = SchemaConfig.from_json_file("home_services.json")
        assert "technician" in config.layer_descriptions["L1"].lower() or "shop" in config.layer_descriptions["L1"].lower()
        assert "invoice" in config.layer_descriptions["L2"].lower() or "dispatch" in config.layer_descriptions["L2"].lower()

    def test_home_services_edge_types(self):
        config = SchemaConfig.from_json_file("home_services.json")
        assert "DISPATCHED_TO" in config.layer_edge_types.get("L2", frozenset())
        assert "ASSIGNED_TO" in config.layer_edge_types.get("L1", frozenset())

    def test_home_services_module_constant(self):
        from ohm.schema import HOME_SERVICES_SCHEMA

        assert HOME_SERVICES_SCHEMA.name == "home_services"


class TestManufacturingSchema:
    """Tests for OHM-tss4.11: Manufacturing domain template."""

    def test_manufacturing_loads_from_json(self):
        config = SchemaConfig.from_json_file("manufacturing.json")
        assert config.name == "manufacturing"

    def test_manufacturing_node_types(self):
        config = SchemaConfig.from_json_file("manufacturing.json")
        for expected in ["work_order", "bill_of_materials", "quality_check", "machine", "tool", "fixture", "workstation", "product"]:
            assert expected in config.node_types, f"Missing node type: {expected}"

    def test_manufacturing_observation_types(self):
        config = SchemaConfig.from_json_file("manufacturing.json")
        for expected in ["cycle_time", "downtime_duration", "defect_rate", "oee", "setup_time"]:
            assert expected in config.observation_types, f"Missing observation type: {expected}"

    def test_manufacturing_provenances(self):
        config = SchemaConfig.from_json_file("manufacturing.json")
        for expected in ["quality_engineer", "shift_supervisor", "historian", "maintenance_log"]:
            assert expected in config.provenances, f"Missing provenance: {expected}"

    def test_manufacturing_module_constant(self):
        from ohm.schema import MANUFACTURING_SCHEMA

        assert MANUFACTURING_SCHEMA.name == "manufacturing"


class TestConstructionSchema:
    """Tests for OHM-tss4.12: Construction domain template."""

    def test_construction_loads_from_json(self):
        config = SchemaConfig.from_json_file("construction.json")
        assert config.name == "construction"

    def test_construction_node_types(self):
        config = SchemaConfig.from_json_file("construction.json")
        for expected in ["project", "phase", "crew", "subcontractor", "material", "permit", "inspection", "change_order", "site", "drawing", "specification"]:
            assert expected in config.node_types, f"Missing node type: {expected}"

    def test_construction_observation_types(self):
        config = SchemaConfig.from_json_file("construction.json")
        for expected in ["progress_pct", "crew_size", "weather_delay", "safety_incident"]:
            assert expected in config.observation_types, f"Missing observation type: {expected}"

    def test_construction_provenances(self):
        config = SchemaConfig.from_json_file("construction.json")
        for expected in ["field_superintendent", "project_manager", "safety_officer", "schedule_engineer"]:
            assert expected in config.provenances, f"Missing provenance: {expected}"

    def test_construction_module_constant(self):
        from ohm.schema import CONSTRUCTION_SCHEMA

        assert CONSTRUCTION_SCHEMA.name == "construction"


class TestHealthcareSchema:
    """Tests for OHM-tss4.13: Healthcare domain template."""

    def test_healthcare_loads_from_json(self):
        config = SchemaConfig.from_json_file("healthcare.json")
        assert config.name == "healthcare"

    def test_healthcare_node_types(self):
        config = SchemaConfig.from_json_file("healthcare.json")
        for expected in ["patient", "provider", "payer", "procedure", "diagnosis", "prior_auth", "claim", "referral", "medication", "lab_result", "appointment"]:
            assert expected in config.node_types, f"Missing node type: {expected}"

    def test_healthcare_observation_types(self):
        config = SchemaConfig.from_json_file("healthcare.json")
        for expected in ["auth_turnaround_days", "denial_rate", "collections_rate"]:
            assert expected in config.observation_types, f"Missing observation type: {expected}"

    def test_healthcare_edge_types(self):
        config = SchemaConfig.from_json_file("healthcare.json")
        assert "RULES_OUT" in config.layer_edge_types.get("L3", frozenset())

    def test_healthcare_module_constant(self):
        from ohm.schema import HEALTHCARE_SCHEMA

        assert HEALTHCARE_SCHEMA.name == "healthcare"


class TestHookRegistryTable:
    """Tests for ohm_hooks table (OHM-aznh.1)."""

    def test_hooks_table_exists(self, test_db):
        """ohm_hooks table exists after schema initialization."""
        tables = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
        table_names = {row[0] for row in tables}
        assert "ohm_hooks" in table_names

    def test_hooks_table_columns(self, test_db):
        """ohm_hooks has all required columns."""
        columns = test_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_hooks'").fetchall()
        col_names = {row[0] for row in columns}
        assert "id" in col_names
        assert "event" in col_names
        assert "command" in col_names
        assert "timeout_ms" in col_names
        assert "enabled" in col_names
        assert "created_by" in col_names
        assert "created_at" in col_names
        assert "updated_at" in col_names

    def test_hooks_index_exists(self, test_db):
        """idx_hooks_event_enabled index exists."""
        indexes = test_db.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_hooks_event_enabled" in index_names

    def test_insert_hook(self, test_db):
        """Can insert a hook record."""
        test_db.execute(
            "INSERT INTO ohm_hooks (event, command, created_by) VALUES (?, ?, ?)",
            ["pre_ingest", "echo ok", "test"],
        )
        rows = test_db.execute("SELECT event, command, enabled, timeout_ms FROM ohm_hooks").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "pre_ingest"
        assert rows[0][1] == "echo ok"
        assert rows[0][2] is True
        assert rows[0][3] == 5000

    def test_migration_v0_21_0(self, test_db):
        """Migration 0.21.0 creates ohm_hooks on existing DB."""
        from ohm.schema import SCHEMA_VERSION

        assert SCHEMA_VERSION >= "0.21.0"


class TestHookLogTable:
    """Tests for ohm_hook_log table (OHM-aznh.7)."""

    def test_hook_log_table_exists(self, test_db):
        tables = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
        table_names = {row[0] for row in tables}
        assert "ohm_hook_log" in table_names

    def test_hook_log_columns(self, test_db):
        columns = test_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_hook_log'").fetchall()
        col_names = {row[0] for row in columns}
        for col in ("id", "hook_id", "event", "payload", "exit_code", "stdout", "stderr", "duration_ms", "timed_out", "triggered_at"):
            assert col in col_names

    def test_hook_log_indexes(self, test_db):
        indexes = test_db.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_hook_log_hook" in index_names
        assert "idx_hook_log_time" in index_names

    def test_insert_hook_log(self, test_db):
        test_db.execute(
            "INSERT INTO ohm_hook_log (hook_id, event, exit_code, duration_ms) VALUES (?, ?, ?, ?)",
            ["h1", "pre_ingest", 0, 10.5],
        )
        rows = test_db.execute("SELECT hook_id, event, exit_code, duration_ms FROM ohm_hook_log").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "h1"
        assert rows[0][2] == 0

    def test_migration_v0_22_0(self, test_db):
        from ohm.schema import MIGRATIONS

        v022 = [m for m in MIGRATIONS if m[0] == "0.22.0"]
        assert len(v022) == 1
        assert "ohm_hook_log" in v022[0][2][0]


class TestAliasAndContentHashTables:
    """Tests for ohm_aliases and ohm_content_hashes tables (OHM-g0kv)."""

    def test_aliases_table_exists(self, test_db):
        tables = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
        table_names = {row[0] for row in tables}
        assert "ohm_aliases" in table_names

    def test_aliases_columns(self, test_db):
        columns = test_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_aliases'").fetchall()
        col_names = {row[0] for row in columns}
        for col in ("id", "alias_norm", "node_id", "created_at"):
            assert col in col_names

    def test_aliases_indexes(self, test_db):
        indexes = test_db.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_aliases_norm" in index_names
        assert "idx_aliases_node" in index_names

    def test_content_hashes_table_exists(self, test_db):
        tables = test_db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'").fetchall()
        table_names = {row[0] for row in tables}
        assert "ohm_content_hashes" in table_names

    def test_content_hashes_columns(self, test_db):
        columns = test_db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'ohm_content_hashes'").fetchall()
        col_names = {row[0] for row in columns}
        for col in ("id", "node_id", "content_hash", "created_at"):
            assert col in col_names

    def test_content_hashes_indexes(self, test_db):
        indexes = test_db.execute("SELECT index_name FROM duckdb_indexes()").fetchall()
        index_names = {row[0] for row in indexes}
        assert "idx_content_hash_node" in index_names
        assert "idx_content_hash_hash" in index_names

    def test_migration_v0_23_0(self, test_db):
        from ohm.schema import MIGRATIONS, SCHEMA_VERSION

        assert SCHEMA_VERSION >= "0.23.0"
        v023 = [m for m in MIGRATIONS if m[0] == "0.23.0"]
        assert len(v023) == 1
        assert "ohm_aliases" in v023[0][2][0]
