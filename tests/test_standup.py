"""Tests for `ohm standup` detection and config helpers (ADR-022)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ohm.cli.standup import (
    detect_agent_hosts,
    detect_os,
    detect_service_manager,
    ensure_config_dir,
    write_mcp_config,
    write_sdk_config,
    _mcp_server_entry,
)


def test_detect_os_returns_known_value():
    os_name = detect_os()
    assert os_name in {"linux", "macos", "windows", "unknown"}


def test_detect_service_manager_returns_known_value():
    manager = detect_service_manager()
    assert manager in {"systemd", "launchd", "windows", "docker", "foreground"}


def test_detect_agent_hosts_returns_list():
    hosts = detect_agent_hosts()
    assert isinstance(hosts, list)
    for host in hosts:
        assert "name" in host
        assert "path" in host
        assert isinstance(host["path"], Path)


def test_ensure_config_dir_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("ohm.cli.standup.OHM_MCP_CONFIG_DIR", tmp_path / "ohm")
    ensure_config_dir()
    assert (tmp_path / "ohm").exists()


def test_write_mcp_config(tmp_path, monkeypatch):
    monkeypatch.setattr("ohm.cli.standup.OHM_MCP_CONFIG_DIR", tmp_path)
    path = write_mcp_config(
        tenant_id="devops",
        url="http://127.0.0.1:8710",
        customer_key="test-key",
        agent_id="copilot-vscode",
        domain_config="devsecops.json",
    )
    assert path == tmp_path / "mcp-devops.json"
    data = json.loads(path.read_text())
    assert data["ohm_url"] == "http://127.0.0.1:8710"
    assert data["tenant_id"] == "devops"
    assert data["token"] == "test-key"
    assert data["token_type"] == "customer"
    assert data["domain_config"] == "devsecops.json"


def test_write_sdk_config(tmp_path, monkeypatch):
    monkeypatch.setattr("ohm.cli.standup.OHM_MCP_CONFIG_DIR", tmp_path)
    path = write_sdk_config("metis", "http://127.0.0.1:8710", "agent-token", tenant_id="personal")
    assert path == tmp_path / "agent-metis.json"
    data = json.loads(path.read_text())
    assert data["agent_id"] == "metis"
    assert data["tenant_id"] == "personal"


def test_mcp_server_entry():
    entry = _mcp_server_entry(Path("/tmp/mcp-foo.json"))
    assert entry["command"] == "ohm-mcp"
    assert entry["args"] == ["--config", "/tmp/mcp-foo.json"]


# Re-import json inside tests because the module-level import above is not real
import json  # noqa: E402
