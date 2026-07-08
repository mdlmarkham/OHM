"""Tests for `ohm standup` detection and config helpers (ADR-022)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ohm.cli.standup import (
    detect_agent_hosts,
    detect_os,
    detect_service_manager,
    ensure_config_dir,
    install_systemd_service,
    install_launchd_service,
    run_local,
    write_default_config,
    write_mcp_config,
    write_sdk_config,
    _mcp_server_entry,
    _config_path,
    _db_path,
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


def test_write_default_config(tmp_path):
    config_path = tmp_path / "ohmd.json"
    db_path = tmp_path / "ohm.duckdb"
    token = write_default_config(config_path, db_path, multi_tenant=True)
    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert data["db_path"] == str(db_path)
    assert data["multi_tenant"] is True
    assert "standup" in data["tokens"]
    assert data["tokens"]["standup"]["role"] == "admin"
    assert len(token) > 20


def test_install_systemd_service(tmp_path):
    from ohm.cli.standup import _systemd_unit_path

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as m:
        m.setattr("ohm.cli.standup._systemd_unit_path", lambda name, user=False: tmp_path / name)
        path = install_systemd_service(
            "ohmd.service",
            "/usr/local/bin/ohmd --multi-tenant",
            "OHM daemon",
            user=True,
            env_vars={"OHM_CONFIG": "/etc/ohm/ohmd.json"},
        )
        assert path.exists()
        text = path.read_text()
        assert "ExecStart=/usr/local/bin/ohmd --multi-tenant" in text
        assert "Environment=OHM_CONFIG=/etc/ohm/ohmd.json" in text


def test_install_launchd_service(tmp_path):
    from ohm.cli.standup import _launchd_plist_path

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as m:
        m.setattr("ohm.cli.standup._launchd_plist_path", lambda label: tmp_path / f"{label}.plist")
        path = install_launchd_service(
            "org.openclaw.ohmd",
            "/usr/local/bin/ohmd",
            ["/usr/local/bin/ohmd", "--multi-tenant"],
            env_vars={"OHM_CONFIG": "/etc/ohm/ohmd.json"},
        )
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["Label"] == "org.openclaw.ohmd"
        assert data["ProgramArguments"][-1] == "--multi-tenant"
        assert data["EnvironmentVariables"]["OHM_CONFIG"] == "/etc/ohm/ohmd.json"


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


def test_run_local_creates_agent_store(tmp_path, monkeypatch):
    """Local per-agent mode creates a DuckDB and writes agent.json."""
    import argparse

    agents_dir = tmp_path / "agents"
    monkeypatch.setenv("OHM_AGENTS_DIR", str(agents_dir))
    # Ensure we do not accidentally try to attach the system DuckLake.
    monkeypatch.delenv("OHM_DUCKLAKE_PATH", raising=False)
    # Decline DuckLake sync prompt.
    monkeypatch.setattr("ohm.cli.standup._confirm", lambda prompt, default=False: False)

    args = argparse.Namespace(agent_id="test-local-agent")
    run_local(args)

    config_path = agents_dir / "test-local-agent" / "agent.json"
    db_path = agents_dir / "test-local-agent" / "ohm.duckdb"
    assert config_path.exists()
    assert db_path.exists()

    data = json.loads(config_path.read_text())
    assert data["agent_id"] == "test-local-agent"
    assert data["mode"] == "local"
    assert data["db_path"] == str(db_path)
    assert data["ducklake_path"] is None


# Re-import json inside tests because the module-level import above is not real
import json  # noqa: E402
