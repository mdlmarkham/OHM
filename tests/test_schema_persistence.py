"""Tests for OHM-795: Persist domain schema in ohm_meta."""

from __future__ import annotations

import json
import pytest

from ohm.graph.schema import (
    SchemaConfig,
    initialize_schema,
    resolve_schema_by_name,
    DEFAULT_SCHEMA,
    SCHEMA_VERSION,
)


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestSchemaVersion:
    def test_version_bumped(self):
        assert SCHEMA_VERSION == "0.48.0"


class TestToDbFromDb:
    """Test SchemaConfig.to_db() and from_db() round-trip."""

    def test_to_db_persists_schema(self, db):
        schema = SchemaConfig(name="test_domain")
        schema.to_db(db)
        row = db.execute("SELECT value FROM ohm_meta WHERE key = 'domain_schema'").fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["name"] == "test_domain"

    def test_to_db_persists_domain_name(self, db):
        schema = SchemaConfig(name="my_domain")
        schema.to_db(db)
        row = db.execute("SELECT value FROM ohm_meta WHERE key = 'domain_name'").fetchone()
        assert row is not None
        assert row[0] == "my_domain"

    def test_from_db_returns_none_when_not_persisted(self, db):
        # Fresh DB — no domain_schema key yet
        result = SchemaConfig.from_db(db)
        assert result is None

    def test_from_db_returns_schema_after_to_db(self, db):
        schema = SchemaConfig(name="round_trip")
        schema.to_db(db)
        loaded = SchemaConfig.from_db(db)
        assert loaded is not None
        assert loaded.name == "round_trip"

    def test_to_db_is_idempotent(self, db):
        schema1 = SchemaConfig(name="first")
        schema1.to_db(db)
        schema2 = SchemaConfig(name="second")
        schema2.to_db(db)
        loaded = SchemaConfig.from_db(db)
        assert loaded is not None
        assert loaded.name == "second"

    def test_round_trip_preserves_node_types(self, db):
        schema = SchemaConfig(name="custom", node_types=frozenset({"concept", "source", "entity", "pattern", "custom_type"}))
        schema.to_db(db)
        loaded = SchemaConfig.from_db(db)
        assert loaded is not None
        assert loaded.node_types == frozenset({"concept", "source", "entity", "pattern", "custom_type"})

    def test_round_trip_preserves_edge_types(self, db):
        schema = SchemaConfig(
            name="custom",
            edge_types_by_layer={"L3": frozenset({"CAUSES", "SUPPORTS", "CUSTOM_EDGE"})},
        )
        schema.to_db(db)
        loaded = SchemaConfig.from_db(db)
        assert loaded is not None
        assert "CUSTOM_EDGE" in loaded.layer_edge_types["L3"]

    def test_from_db_returns_none_on_corrupt_json(self, db):
        db.execute("INSERT INTO ohm_meta (key, value) VALUES ('domain_schema', 'not valid json')")
        result = SchemaConfig.from_db(db)
        assert result is None


class TestInitializeSchemaPersists:
    """Test that initialize_schema() persists the schema to ohm_meta."""

    def test_initialize_with_schema_persists(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        schema = SchemaConfig(name="persisted_domain")
        initialize_schema(conn, schema=schema)
        loaded = SchemaConfig.from_db(conn)
        assert loaded is not None
        assert loaded.name == "persisted_domain"

    def test_initialize_without_schema_does_not_persist(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        initialize_schema(conn)  # no schema argument
        result = SchemaConfig.from_db(conn)
        assert result is None


class TestResolveSchemaByName:
    """Test the shared template lookup helper."""

    def test_resolves_ohm_domain(self):
        schema = resolve_schema_by_name("ohm")
        assert isinstance(schema, SchemaConfig)
        assert schema.name == "ohm"

    def test_resolves_topo_domain(self):
        schema = resolve_schema_by_name("topo")
        assert isinstance(schema, SchemaConfig)

    def test_resolves_beef_herd_domain(self):
        schema = resolve_schema_by_name("beef_herd")
        assert isinstance(schema, SchemaConfig)

    def test_falls_back_to_ohm_for_unknown(self):
        schema = resolve_schema_by_name("nonexistent_domain_xyz")
        # Should fall back to ohm.json or generic defaults
        assert isinstance(schema, SchemaConfig)

    def test_rejects_invalid_domain_name(self):
        with pytest.raises(ValueError, match="Invalid domain"):
            resolve_schema_by_name("UPPERCASE")

    def test_rejects_domain_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid domain"):
            resolve_schema_by_name("has spaces")

    def test_templates_dir_parameter(self, tmp_path):
        # Create a custom template
        template = {"name": "custom", "node_types": ["concept", "source", "entity", "pattern", "custom"], "layer_descriptions": {}, "observation_types": ["measurement", "assessment"], "observation_sources": ["agent"], "provenances": ["agent"]}
        (tmp_path / "custom.json").write_text(json.dumps(template))
        schema = resolve_schema_by_name("custom", templates_dir=str(tmp_path))
        assert schema.name == "custom"
        assert "custom" in schema.node_types


class TestBackwardCompat:
    """Existing deployments without domain_schema in ohm_meta should work."""

    def test_from_db_returns_none_on_fresh_db(self, db):
        assert SchemaConfig.from_db(db) is None

    def test_default_schema_still_works(self):
        assert DEFAULT_SCHEMA is not None
        assert isinstance(DEFAULT_SCHEMA, SchemaConfig)
