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


class TestStartupResolution:
    """Test _apply_ohm_meta_config — behavioral config from DB into runtime config."""

    def test_db_values_apply_when_file_silent(self, db):
        from ohm.server.server import _apply_ohm_meta_config, DEFAULT_CONFIG

        set_meta(db, "semantic_layer.enabled", "true")
        set_meta(db, "semantic_layer.interval_sec", "5")
        set_meta(db, "beads_sync.enabled", "false")
        set_meta(db, "ducklake.sync_interval_sec", "99")

        config = dict(DEFAULT_CONFIG)  # start with defaults
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["semantic_layer"]["auto_actions_enabled"] is True
        assert config["semantic_layer"]["auto_actions_interval_seconds"] == 5
        assert config["beads_sync"]["enabled"] is False
        assert config["ducklake"]["sync_interval_seconds"] == 99

    def test_file_values_win_over_db(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "semantic_layer.enabled", "true")
        set_meta(db, "semantic_layer.interval_sec", "5")

        config = {"semantic_layer": {"auto_actions_enabled": False, "auto_actions_interval_seconds": 3600}}
        file_config = {"semantic_layer": {"auto_actions_enabled": False, "auto_actions_interval_seconds": 3600}}
        _apply_ohm_meta_config(db, config, file_config=file_config)

        # File values should win
        assert config["semantic_layer"]["auto_actions_enabled"] is False
        assert config["semantic_layer"]["auto_actions_interval_seconds"] == 3600

    def test_false_string_disables_feature(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "semantic_layer.enabled", "false")
        config = {}
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["semantic_layer"]["auto_actions_enabled"] is False

    def test_integer_coercion(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "semantic_layer.interval_sec", "42")
        config = {}
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["semantic_layer"]["auto_actions_interval_seconds"] == 42
        assert isinstance(config["semantic_layer"]["auto_actions_interval_seconds"], int)

    def test_onboarding_node_id_from_db(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "onboarding_node_id", "node_abc")
        config = {}
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["onboarding_node_id"] == "node_abc"

    def test_bedrock_keys_from_db(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "bedrock.kb_id", "kb-123")
        set_meta(db, "bedrock.region", "us-east-1")
        config = {}
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["bedrock"]["knowledge_base_id"] == "kb-123"
        assert config["bedrock"]["region"] == "us-east-1"

    def test_agent_onboarding_enabled_from_db(self, db):
        from ohm.server.server import _apply_ohm_meta_config

        set_meta(db, "agent_onboarding_enabled", "true")
        config = {}
        _apply_ohm_meta_config(db, config, file_config={})

        assert config["agent_onboarding_enabled"] is True

    def test_no_db_values_leaves_defaults(self, db):
        from ohm.server.server import _apply_ohm_meta_config, DEFAULT_CONFIG

        config = dict(DEFAULT_CONFIG)
        original_sl_enabled = config["semantic_layer"]["auto_actions_enabled"]
        _apply_ohm_meta_config(db, config, file_config={})

        # Should be unchanged from defaults (no ohm_meta values to apply)
        assert config["semantic_layer"]["auto_actions_enabled"] == original_sl_enabled


class TestPutConfigHttp:
    """End-to-end test: PUT /config over real HTTP (OHM-801)."""

    def test_put_config_over_http(self, test_server):
        """PUT /config writes to ohm_meta and GET /config reads it back."""
        port, _ = test_server
        from tests.conftest import _request

        # PUT a config value
        status, data = _request("PUT", port, "/config", body={"test_key": "test_value"})
        assert status == 200
        assert "updated" in data

        # GET it back
        status, data = _request("GET", port, "/config")
        assert status == 200
        assert data.get("test_key") == "test_value"

    def test_put_config_rejects_reserved_keys(self, test_server):
        """PUT /config should silently skip reserved keys."""
        port, _ = test_server
        from tests.conftest import _request

        status, data = _request("PUT", port, "/config", body={"schema_version": "99.99.99"})
        assert status == 200
        # schema_version should not be updated
        assert "99.99.99" not in str(data.get("updated", {}))

    def test_put_unregistered_route_returns_405(self, test_server):
        """PUT to a route not registered under PUT should return 405, not 501."""
        port, _ = test_server
        from tests.conftest import _request

        # /node is a GET/POST/DELETE route, not PUT
        status, data = _request("PUT", port, "/node/test_node", body={"key": "value"})
        assert status == 405
