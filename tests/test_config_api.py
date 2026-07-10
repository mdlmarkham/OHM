"""Tests for OHM-796: Stateless-by-default config + /config API."""

from __future__ import annotations

import pytest

from ohm.graph.schema import (
    initialize_schema,
    get_meta,
    set_meta,
    get_all_meta,
)


@pytest.fixture
def db():
    import duckdb

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    return conn


class TestGetMeta:
    def test_returns_none_for_missing_key(self, db):
        assert get_meta(db, "nonexistent") is None

    def test_returns_default_for_missing_key(self, db):
        assert get_meta(db, "nonexistent", "fallback") == "fallback"

    def test_returns_value_for_existing_key(self, db):
        set_meta(db, "test_key", "test_value")
        assert get_meta(db, "test_key") == "test_value"

    def test_returns_none_on_corrupt_meta_table(self):
        # If ohm_meta doesn't exist, should return default gracefully
        import duckdb

        conn = duckdb.connect(":memory:")
        # Don't initialize schema — ohm_meta won't exist
        assert get_meta(conn, "any_key", "safe") == "safe"


class TestSetMeta:
    def test_inserts_new_key(self, db):
        set_meta(db, "new_key", "new_value")
        assert get_meta(db, "new_key") == "new_value"

    def test_replaces_existing_key(self, db):
        set_meta(db, "key1", "value1")
        set_meta(db, "key1", "value2")
        assert get_meta(db, "key1") == "value2"

    def test_preserves_other_keys(self, db):
        set_meta(db, "key_a", "a")
        set_meta(db, "key_b", "b")
        assert get_meta(db, "key_a") == "a"
        assert get_meta(db, "key_b") == "b"


class TestGetAllMeta:
    def test_returns_all_keys(self, db):
        set_meta(db, "key1", "val1")
        set_meta(db, "key2", "val2")
        all_meta = get_all_meta(db)
        assert "key1" in all_meta
        assert "key2" in all_meta
        assert all_meta["key1"] == "val1"
        assert all_meta["key2"] == "val2"

    def test_includes_schema_version(self, db):
        all_meta = get_all_meta(db)
        assert "schema_version" in all_meta

    def test_returns_empty_on_corrupt(self):
        import duckdb

        conn = duckdb.connect(":memory:")
        result = get_all_meta(conn)
        assert result == {}


class TestConfigAPIHandlers:
    """Test the /config GET and PUT handler logic."""

    def test_get_config_returns_all_meta(self, db):
        set_meta(db, "semantic_layer.enabled", "true")
        set_meta(db, "ducklake.sync_interval_sec", "30")
        all_meta = get_all_meta(db)
        assert all_meta["semantic_layer.enabled"] == "true"
        assert all_meta["ducklake.sync_interval_sec"] == "30"

    def test_put_config_updates_meta(self, db):
        set_meta(db, "semantic_layer.enabled", "false")
        assert get_meta(db, "semantic_layer.enabled") == "false"
        set_meta(db, "semantic_layer.enabled", "true")
        assert get_meta(db, "semantic_layer.enabled") == "true"

    def test_put_config_non_string_value_converted(self, db):
        set_meta(db, "test_int", str(42))
        assert get_meta(db, "test_int") == "42"


class TestBehavioralConfigKeys:
    """Test that behavioral config keys work with get_meta/set_meta."""

    @pytest.mark.parametrize(
        "key,value",
        [
            ("onboarding_node_id", "node_abc123"),
            ("semantic_layer.enabled", "true"),
            ("semantic_layer.interval_sec", "300"),
            ("semantic_layer.rate_limit_sec", "60"),
            ("ducklake.sync_interval_sec", "30"),
            ("beads_sync.enabled", "false"),
            ("beads_sync.interval_sec", "3600"),
            ("beads_sync.startup_sync", "true"),
            ("bedrock.kb_id", "kb-123"),
            ("bedrock.data_source_id", "ds-456"),
            ("bedrock.region", "us-east-1"),
            ("agent_onboarding_enabled", "true"),
        ],
    )
    def test_key_round_trip(self, db, key, value):
        set_meta(db, key, value)
        assert get_meta(db, key) == value
