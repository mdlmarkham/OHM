"""Tests for OHM Agent Profiles (ohm.framework.profiles)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ohm.framework.profiles import (
    AgentProfiles,
    Profile,
    from_profile,
    load_catalog,
)


@pytest.fixture
def empty_cwd_and_home(tmp_path, monkeypatch):
    """Point cwd and home at empty tmp dirs so no catalog is found."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    return {"cwd": tmp_path, "home": home}


@pytest.fixture
def sample_catalog_dict():
    return {
        "version": "1",
        "selectors": {"by_repo": {"github.com/acme/security-ops": "devops"}},
        "profiles": {
            "devops": {
                "label": "Dev-Sec-Ops",
                "ohm_url": "http://ohmd.internal:8710",
                "tenant_id": "devops",
                "token": "ohm-cust-devops-abc",
                "agent_id": "copilot-agent",
                "domain_config": "devsecops.json",
                "allowed_tools": ["ohm_search", "ohm_get_node", "ohm_observe"],
                "read_only": False,
                "token_type": "customer",
                "default": False,
            },
            "dataops": {
                "label": "Data Operations",
                "ohm_url": "http://ohmd.internal:8710",
                "tenant_id": "dataops",
                "token": "ohm-cust-dataops-xyz",
                "agent_id": "copilot-agent",
                "domain_config": "datapipelines.json",
                "allowed_tools": ["*"],
                "read_only": False,
                "default": True,
            },
            "core": {
                "label": "Core Store",
                "agent_id": "copilot-agent",
            },
        },
    }


class TestLoadCatalog:
    def test_load_catalog_returns_none_when_no_file(self, empty_cwd_and_home):
        assert load_catalog() is None

    def test_load_catalog_from_user_level(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".ohm").mkdir(parents=True)
        catalog = {
            "version": "1",
            "profiles": {"devops": {"label": "DevOps", "default": True}},
        }
        (home / ".ohm" / "profiles.json").write_text(json.dumps(catalog))

        empty_cwd = tmp_path / "cwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))

        result = load_catalog()
        assert result is not None
        assert "devops" in result["profiles"]
        assert result["version"] == "1"

    def test_load_catalog_project_level_takes_priority(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        (project / ".ohm").mkdir(parents=True)
        (project / ".ohm" / "profiles.json").write_text(json.dumps({"version": "1", "profiles": {"project-profile": {}}}))

        home = tmp_path / "home"
        (home / ".ohm").mkdir(parents=True)
        (home / ".ohm" / "profiles.json").write_text(json.dumps({"version": "1", "profiles": {"home-profile": {}}}))

        monkeypatch.chdir(project)
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))

        result = load_catalog()
        assert result is not None
        assert "project-profile" in result["profiles"]


class TestProfileDataclass:
    def test_profile_dataclass_fields(self):
        profile = Profile(
            name="devops",
            label="Dev-Sec-Ops",
            ohm_url="http://ohmd.internal:8710",
            tenant_id="devops",
            token="ohm-cust-devops-abc",
            agent_id="copilot-agent",
            domain_config="devsecops.json",
            allowed_tools=["ohm_search", "ohm_get_node"],
            read_only=False,
            token_type="customer",
            default=True,
        )
        assert profile.name == "devops"
        assert profile.label == "Dev-Sec-Ops"
        assert profile.ohm_url == "http://ohmd.internal:8710"
        assert profile.tenant_id == "devops"
        assert profile.token == "ohm-cust-devops-abc"
        assert profile.agent_id == "copilot-agent"
        assert profile.domain_config == "devsecops.json"
        assert profile.allowed_tools == ["ohm_search", "ohm_get_node"]
        assert profile.read_only is False
        assert profile.token_type == "customer"
        assert profile.default is True

    def test_profile_dataclass_defaults(self):
        profile = Profile(name="core")
        assert profile.label == ""
        assert profile.ohm_url is None
        assert profile.tenant_id is None
        assert profile.token is None
        assert profile.agent_id == "unknown"
        assert profile.domain_config is None
        assert profile.allowed_tools == []
        assert profile.read_only is False
        assert profile.token_type is None
        assert profile.default is False


class TestAgentProfiles:
    def test_agent_profiles_get_returns_profile(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        profile = catalog.get("devops")
        assert profile is not None
        assert profile.name == "devops"
        assert profile.label == "Dev-Sec-Ops"
        assert profile.tenant_id == "devops"
        assert profile.token == "ohm-cust-devops-abc"

    def test_agent_profiles_get_missing_returns_none(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        assert catalog.get("nonexistent") is None

    def test_agent_profiles_select_returns_default(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        profile = catalog.select()
        assert profile is not None
        assert profile.name == "dataops"
        assert profile.default is True

    def test_agent_profiles_select_explicit_name(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        profile = catalog.select("devops")
        assert profile is not None
        assert profile.name == "devops"

    def test_agent_profiles_select_explicit_missing_returns_none(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        assert catalog.select("nonexistent") is None

    def test_agent_profiles_select_no_default_returns_none(self):
        catalog = AgentProfiles({"profiles": {"devops": {"label": "DevOps", "default": False}}})
        assert catalog.select() is None

    def test_agent_profiles_list_profiles(self, sample_catalog_dict):
        catalog = AgentProfiles(sample_catalog_dict)
        profiles = catalog.list_profiles()
        names = {p.name for p in profiles}
        assert names == {"devops", "dataops", "core"}
        assert len(profiles) == 3

    def test_agent_profiles_from_files_returns_none(self, empty_cwd_and_home):
        assert AgentProfiles.from_files() is None

    def test_agent_profiles_from_files_loads_catalog(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".ohm").mkdir(parents=True)
        catalog = {
            "version": "1",
            "profiles": {"devops": {"label": "DevOps", "default": True}},
        }
        (home / ".ohm" / "profiles.json").write_text(json.dumps(catalog))

        empty_cwd = tmp_path / "cwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("HOME", str(home))

        loaded = AgentProfiles.from_files()
        assert loaded is not None
        selected = loaded.select("devops")
        assert selected is not None
        assert selected.name == "devops"


class TestFromProfile:
    def test_from_profile_core_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OHM_DB", str(tmp_path / "core.duckdb"))
        profile = Profile(name="core", agent_id="test-agent")
        graph = from_profile(profile)
        assert graph.actor == "test-agent"
        graph.close()

    def test_from_profile_http(self):
        profile = Profile(
            name="devops",
            ohm_url="http://ohmd.internal:8710",
            tenant_id="devops",
            token="ohm-cust-devops-abc",
            agent_id="copilot-agent",
        )
        graph = from_profile(profile)
        assert graph.actor == "copilot-agent"
        assert graph.tenant_id == "devops"
        graph.close()

    def test_from_profile_http_no_tenant(self):
        profile = Profile(
            name="single",
            ohm_url="http://ohmd.internal:8710",
            token="ohm-token",
            agent_id="copilot-agent",
        )
        graph = from_profile(profile)
        assert graph.actor == "copilot-agent"
        assert graph.tenant_id is None
        graph.close()


class TestCliProfileShow:
    """``ohm profile show`` masks bearer tokens in output."""

    def test_profile_show_masks_token(self, tmp_path, monkeypatch, capsys):
        home = tmp_path / "home"
        (home / ".ohm").mkdir(parents=True)
        catalog = {
            "version": "1",
            "profiles": {
                "devops": {
                    "label": "DevOps",
                    "ohm_url": "http://127.0.0.1:8710",
                    "tenant_id": "devops",
                    "token": "ohm-cust-devops-super-secret-token-1234",
                    "agent_id": "copilot",
                }
            },
        }
        (home / ".ohm" / "profiles.json").write_text(json.dumps(catalog))
        empty_cwd = tmp_path / "cwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("HOME", str(home))

        from ohm.cli import _handle_profile, _mask_token
        import argparse

        args = argparse.Namespace(profile_command="show", profile_name="devops")
        _handle_profile(args)
        captured = capsys.readouterr()
        assert "super-secret" not in captured.out
        assert "ohm-cust-devops-super-secret-token-1234" not in captured.out
        assert _mask_token(catalog["profiles"]["devops"]["token"]) in captured.out

    def test_mask_token_short_input(self):
        from ohm.cli import _mask_token
        assert _mask_token("short") == "***"
        assert _mask_token("x" * 20) == ("x" * 8) + "..." + ("x" * 4)
