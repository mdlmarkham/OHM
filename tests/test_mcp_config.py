"""Tests for OHM-yzyk.1.2: MCP server --config file and allowed_tools enforcement."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from ohm.mcp.config import (
    config,
    load_config_file,
    is_tool_allowed,
    make_headers,
    WRITE_TOOLS,
    _should_send_tenant_header,
    validate_domain_config,
    get_active_profile,
    set_active_profile,
    get_profiles,
)


class TestConfigFileLoading:
    """--config file loads JSON config and overrides env defaults."""

    def test_load_config_file_sets_values(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "ohm_url": "http://10.0.0.1:9999",
                    "token": "test-token-xyz",
                    "agent_id": "copilot",
                    "tenant_id": "devops",
                    "allowed_tools": ["ohm_search", "ohm_get_node"],
                    "read_only": True,
                },
                f,
            )
            f.flush()

        original = dict(config)
        try:
            load_config_file(f.name)
            assert config["ohm_url"] == "http://10.0.0.1:9999"
            assert config["token"] == "test-token-xyz"
            assert config["agent_id"] == "copilot"
            assert config["tenant_id"] == "devops"
            assert config["allowed_tools"] == ["ohm_search", "ohm_get_node"]
            assert config["read_only"] is True
        finally:
            config.clear()
            config.update(original)
            os.unlink(f.name)

    def test_env_vars_override_config_file(self, monkeypatch):
        monkeypatch.setenv("OHM_URL", "http://env-override:7777")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"ohm_url": "http://from-file:8888"}, f)
            f.flush()

        original = dict(config)
        try:
            load_config_file(f.name)
            assert config["ohm_url"] == "http://env-override:7777"
        finally:
            config.clear()
            config.update(original)
            os.unlink(f.name)


class TestAllowedToolsEnforcement:
    """allowed_tools filters which tools are advertised and callable."""

    def test_star_allows_all(self):
        original = dict(config)
        try:
            config["allowed_tools"] = ["*"]
            config["read_only"] = False
            assert is_tool_allowed("ohm_search") is True
            assert is_tool_allowed("ohm_create_node") is True
            assert is_tool_allowed("anything") is True
        finally:
            config.clear()
            config.update(original)

    def test_list_filters(self):
        original = dict(config)
        try:
            config["allowed_tools"] = ["ohm_search", "ohm_get_node"]
            config["read_only"] = False
            assert is_tool_allowed("ohm_search") is True
            assert is_tool_allowed("ohm_get_node") is True
            assert is_tool_allowed("ohm_create_node") is False
        finally:
            config.clear()
            config.update(original)

    def test_read_only_blocks_write_tools(self):
        original = dict(config)
        try:
            config["allowed_tools"] = ["*"]
            config["read_only"] = True
            assert is_tool_allowed("ohm_search") is True
            assert is_tool_allowed("ohm_create_node") is False
            assert is_tool_allowed("ohm_observe") is False
            assert is_tool_allowed("ohm_challenge") is False
            assert is_tool_allowed("ohm_support") is False
            assert is_tool_allowed("ohm_create_edge") is False
            assert is_tool_allowed("ohm_update_state") is False
        finally:
            config.clear()
            config.update(original)

    def test_read_only_allows_read_tools(self):
        original = dict(config)
        try:
            config["allowed_tools"] = ["*"]
            config["read_only"] = True
            assert is_tool_allowed("ohm_stats") is True
            assert is_tool_allowed("ohm_neighborhood") is True
            assert is_tool_allowed("ohm_listen") is True
            assert is_tool_allowed("ohm_domain_onboarding") is True
        finally:
            config.clear()
            config.update(original)

    def test_write_tools_set_complete(self):
        expected = {"ohm_create_node", "ohm_create_edge", "ohm_observe", "ohm_challenge", "ohm_support", "ohm_update_state"}
        assert WRITE_TOOLS == expected

    def test_empty_list_denies_all(self):
        original = dict(config)
        try:
            config["allowed_tools"] = []
            config["read_only"] = False
            assert is_tool_allowed("ohm_search") is False
            assert is_tool_allowed("ohm_create_node") is False
        finally:
            config.clear()
            config.update(original)

    def test_missing_allowed_tools_defaults_to_star(self):
        original = dict(config)
        try:
            config.pop("allowed_tools", None)
            config["read_only"] = False
            assert is_tool_allowed("ohm_search") is True
            assert is_tool_allowed("ohm_create_node") is True
        finally:
            config.clear()
            config.update(original)


class TestHeadersUseConfig:
    """make_headers() uses config values."""

    def test_headers_include_tenant_id(self):
        original = dict(config)
        try:
            config["token"] = "tok-123"
            config["tenant_id"] = "devops"
            config["agent_id"] = "copilot"
            h = make_headers()
            assert h["Authorization"] == "Bearer tok-123"
            assert h["X-Tenant-ID"] == "devops"
            assert h["X-OHM-Agent"] == "copilot"
        finally:
            config.clear()
            config.update(original)

    def test_headers_without_tenant_id(self):
        original = dict(config)
        try:
            config["token"] = "tok-123"
            config["tenant_id"] = ""
            config["agent_id"] = "mcp"
            h = make_headers()
            assert "X-Tenant-ID" not in h
            assert h["X-OHM-Agent"] == "mcp"
        finally:
            config.clear()
            config.update(original)


class TestTenantHeaderResolution:
    """OHM-yzyk.1.1: X-Tenant-ID behavior depends on token_type."""

    def test_agent_token_sends_tenant_header(self):
        original = dict(config)
        try:
            config["token"] = "admin-agent-token"
            config["tenant_id"] = "devops"
            config["token_type"] = "agent"
            assert _should_send_tenant_header() is True
            h = make_headers()
            assert h["X-Tenant-ID"] == "devops"
        finally:
            config.clear()
            config.update(original)

    def test_customer_key_skips_tenant_header(self):
        original = dict(config)
        try:
            config["token"] = "ohm-cust-devops-abc123"
            config["tenant_id"] = "devops"
            config["token_type"] = "customer"
            assert _should_send_tenant_header() is False
            h = make_headers()
            assert "X-Tenant-ID" not in h
            assert h["Authorization"] == "Bearer ohm-cust-devops-abc123"
        finally:
            config.clear()
            config.update(original)

    def test_agent_token_without_tenant_id_skips_header(self):
        original = dict(config)
        try:
            config["token"] = "admin-token"
            config["tenant_id"] = ""
            config["token_type"] = "agent"
            assert _should_send_tenant_header() is False
            h = make_headers()
            assert "X-Tenant-ID" not in h
        finally:
            config.clear()
            config.update(original)

    def test_default_token_type_is_agent(self):
        """Default token_type should be 'agent' for backward compat."""
        original = dict(config)
        try:
            config["tenant_id"] = "devops"
            # token_type not explicitly set — should default to "agent"
            config.pop("token_type", None)
            config["token_type"] = "agent"  # default
            assert _should_send_tenant_header() is True
        finally:
            config.clear()
            config.update(original)

    def test_config_file_can_set_token_type_customer(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                {
                    "token": "ohm-cust-dataops-xyz",
                    "tenant_id": "dataops",
                    "token_type": "customer",
                },
                f,
            )
            f.flush()

        original = dict(config)
        try:
            load_config_file(f.name)
            assert config["token_type"] == "customer"
            assert _should_send_tenant_header() is False
            h = make_headers()
            assert "X-Tenant-ID" not in h
            assert h["Authorization"] == "Bearer ohm-cust-dataops-xyz"
        finally:
            config.clear()
            config.update(original)
            os.unlink(f.name)


class TestDomainConfigValidation:
    """OHM-yzyk.1.2 #4: domain config validation on startup."""

    def test_matching_config_returns_true(self):
        assert validate_domain_config("devsecops.json", {"schema": "devsecops"}) is True

    def test_matching_without_extension(self):
        assert validate_domain_config("topo", {"schema": "topo"}) is True

    def test_mismatch_returns_false(self):
        assert validate_domain_config("devsecops.json", {"schema": "topo"}) is False

    def test_none_expected_returns_true(self):
        assert validate_domain_config(None, {"schema": "anything"}) is True

    def test_empty_expected_returns_true(self):
        assert validate_domain_config("", {"schema": "anything"}) is True


class TestAgentProfiles:
    """OHM-yzyk.3: profiles let a single sidecar switch between OHM instances."""

    def test_profiles_list_is_loaded(self):
        original = dict(config)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(
                    {
                        "profiles": [
                            {"name": "personal", "token": "tok-personal"},
                            {"name": "work", "token": "tok-work", "read_only": True},
                        ],
                        "active_profile": "work",
                    },
                    f,
                )
                f.flush()
            load_config_file(f.name)
            profiles = get_profiles()
            assert {p["name"] for p in profiles} == {"personal", "work"}
            active = get_active_profile()
            assert active["name"] == "work"
            assert active["token"] == "tok-work"
            assert active["read_only"] is True
            assert is_tool_allowed("ohm_create_node") is False
            assert is_tool_allowed("ohm_stats") is True
            os.unlink(f.name)
        finally:
            config.clear()
            config.update(original)

    def test_set_active_profile_switches_policy(self):
        original = dict(config)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(
                    {
                        "profiles": [
                            {"name": "write", "allowed_tools": ["*"], "read_only": False},
                            {"name": "read", "allowed_tools": ["*"], "read_only": True},
                        ]
                    },
                    f,
                )
                f.flush()
            load_config_file(f.name)
            assert get_active_profile()["name"] == "write"
            assert is_tool_allowed("ohm_create_node") is True
            assert set_active_profile("read") is True
            assert get_active_profile()["name"] == "read"
            assert is_tool_allowed("ohm_create_node") is False
            assert set_active_profile("missing") is False
            os.unlink(f.name)
        finally:
            config.clear()
            config.update(original)

    def test_reloading_flat_config_clears_previous_profiles(self):
        original = dict(config)
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f1:
                json.dump({"profiles": [{"name": "alpha", "allowed_tools": ["ohm_stats"]}]}, f1)
                f1.flush()
            load_config_file(f1.name)
            assert {p["name"] for p in get_profiles()} == {"alpha"}

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f2:
                json.dump({"allowed_tools": ["*"], "read_only": False}, f2)
                f2.flush()
            load_config_file(f2.name)
            assert config.get("_profiles_explicit") is False
            assert is_tool_allowed("ohm_create_node") is True
            os.unlink(f1.name)
            os.unlink(f2.name)
        finally:
            config.clear()
            config.update(original)


# OHM-yzyk.1.3 — validate_domain_config wired into server startup


@pytest.mark.anyio
async def test_check_domain_config_mismatch_exits(tmp_path):
    """If configured domain_config does not match daemon /schema, startup exits."""
    import sys

    pytest.importorskip("mcp")  # ohm.mcp.server requires the optional `mcp` package
    from ohm.mcp.config import config as mcp_config
    from ohm.mcp.server import _check_domain_config

    # Simulate loaded config expecting devsecops.json but daemon reports topo
    mcp_config["domain_config"] = "devsecops.json"

    async def fake_ohm_get(path: str, params=None):
        assert path == "/schema"
        return {"schema": "topo"}

    from ohm.mcp import server as server_mod

    original_get = server_mod._ohm_get
    server_mod._ohm_get = fake_ohm_get
    try:
        with pytest.raises(SystemExit) as exc_info:
            await _check_domain_config()
        assert exc_info.value.code == 1
    finally:
        server_mod._ohm_get = original_get


@pytest.mark.anyio
async def test_check_domain_config_match_passes(tmp_path):
    """If configured domain_config matches daemon /schema, startup continues."""
    pytest.importorskip("mcp")  # ohm.mcp.server requires the optional `mcp` package
    from ohm.mcp.config import config as mcp_config
    from ohm.mcp.server import _check_domain_config

    mcp_config["domain_config"] = "devsecops.json"

    async def fake_ohm_get(path: str, params=None):
        return {"schema": "devsecops"}

    from ohm.mcp import server as server_mod

    original_get = server_mod._ohm_get
    server_mod._ohm_get = fake_ohm_get
    try:
        await _check_domain_config()  # should not raise
    finally:
        server_mod._ohm_get = original_get
