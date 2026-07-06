"""Tests for OHM-yzyk.1.2: MCP server --config file and allowed_tools enforcement."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest
from ohm.mcp.config import config, load_config_file, is_tool_allowed, make_headers, WRITE_TOOLS


class TestConfigFileLoading:
    """--config file loads JSON config and overrides env defaults."""

    def test_load_config_file_sets_values(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "ohm_url": "http://10.0.0.1:9999",
                "token": "test-token-xyz",
                "agent_id": "copilot",
                "tenant_id": "devops",
                "allowed_tools": ["ohm_search", "ohm_get_node"],
                "read_only": True,
            }, f)
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
        expected = {"ohm_create_node", "ohm_create_edge", "ohm_observe",
                    "ohm_challenge", "ohm_support", "ohm_update_state"}
        assert WRITE_TOOLS == expected


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