"""Tests for OHM-849: Skills provider.

Covers OhmSkillsProvider resource listing, reading, manifest
generation, core skill content, and nudge skill_uri integration.
"""

import json
import pytest
from pathlib import Path

from tests.conftest import _request

pytestmark = pytest.mark.integration


class TestOhmSkillsProvider:
    """OhmSkillsProvider resource listing and reading."""

    def test_list_resources_includes_decision_node(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        resources = provider.list_resources()
        uris = [r["uri"] for r in resources]
        assert any("decision-node" in u for u in uris)

    def test_list_resources_includes_causal_edge(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        resources = provider.list_resources()
        uris = [r["uri"] for r in resources]
        assert any("causal-edge" in u for u in uris)

    def test_list_resources_includes_manifests(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        resources = provider.list_resources()
        manifest_uris = [r["uri"] for r in resources if "_manifest" in r["uri"]]
        assert len(manifest_uris) >= 2

    def test_read_decision_node_skill(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/decision-node/SKILL.md")
        assert "utility_scale" in content
        assert "action_alternatives" in content

    def test_read_causal_edge_skill(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/causal-edge/SKILL.md")
        assert "CAUSES" in content

    def test_read_manifest(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/decision-node/_manifest")
        data = json.loads(content)
        assert "files" in data
        assert data["file_count"] >= 1
        assert any(f["path"] == "SKILL.md" for f in data["files"])

    def test_manifest_has_sha256(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        manifest = provider.get_manifest("decision-node")
        for f in manifest["files"]:
            assert len(f["sha256"]) == 64

    def test_read_nonexistent_skill_raises(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider
        provider = OhmSkillsProvider()
        with pytest.raises(ValueError, match="not found"):
            provider.read_resource("skill://ohm/nonexistent/SKILL.md")

    def test_read_invalid_uri_raises(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider
        provider = OhmSkillsProvider()
        with pytest.raises(ValueError, match="Unsupported"):
            provider.read_resource("http://example.com/foo")

    def test_read_nonexistent_file_raises(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        with pytest.raises(ValueError, match="not found"):
            provider.read_resource("skill://ohm/decision-node/nonexistent.md")

    def test_get_manifest_nonexistent_raises(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider
        provider = OhmSkillsProvider()
        with pytest.raises(ValueError, match="not found"):
            provider.get_manifest("nonexistent")


class TestCoreSkillsContent:
    """Verify core skill content is meaningful."""

    def test_decision_node_skill_mentions_autoresearch(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/decision-node/SKILL.md")
        assert "autoresearch" in content.lower()

    def test_causal_edge_skill_mentions_adr008(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/causal-edge/SKILL.md")
        assert "ADR-008" in content or "two-stage" in content.lower()

    def test_observation_skill_mentions_source_url(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/observation-recording/SKILL.md")
        assert "source_url" in content

    def test_challenge_support_skill_mentions_negates(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/challenge-support/SKILL.md")
        assert "NEGATES" in content

    def test_ingest_document_skill_mentions_source_tiers(self):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()
        provider = OhmSkillsProvider()
        content = provider.read_resource("skill://ohm/ingest-document/SKILL.md")
        assert "verified" in content.lower()
        assert "raw" in content.lower()


class TestEnsureCoreSkills:
    """Test the ensure_core_skills_exist function."""

    def test_ensure_creates_skills(self, tmp_path):
        from ohm.mcp.skills_provider import OhmSkillsProvider
        provider = OhmSkillsProvider(core_skills_dir=tmp_path / "skills")
        assert not (tmp_path / "skills").exists()

        from ohm.mcp.skills_provider import ensure_core_skills_exist
        ensure_core_skills_exist()


class TestDomainSkills:
    """Test domain skill loading from a custom directory."""

    def test_domain_skill_appears_in_list(self, tmp_path):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()

        domain_dir = tmp_path / "domain_skills" / "trading-research"
        domain_dir.mkdir(parents=True)
        (domain_dir / "SKILL.md").write_text("# Trading Research Skill\n\nDomain-specific.", encoding="utf-8")

        provider = OhmSkillsProvider(domain_skills_dir=tmp_path / "domain_skills")
        resources = provider.list_resources()
        uris = [r["uri"] for r in resources]
        assert any("trading-research" in u for u in uris)

    def test_domain_skill_readable(self, tmp_path):
        from ohm.mcp.skills_provider import OhmSkillsProvider, ensure_core_skills_exist
        ensure_core_skills_exist()

        domain_dir = tmp_path / "domain_skills" / "custom-skill"
        domain_dir.mkdir(parents=True)
        (domain_dir / "SKILL.md").write_text("# Custom Skill\n\nCustom content.", encoding="utf-8")

        provider = OhmSkillsProvider(domain_skills_dir=tmp_path / "domain_skills")
        content = provider.read_resource("skill://ohm/custom-skill/SKILL.md")
        assert "Custom content" in content


class TestNudgeSkillUri:
    """Nudge responses include skill_uri for relevant skills."""

    def test_decision_nudge_includes_skill_uri(self, test_server):
        port, store = test_server
        conn = store.conn
        conn.execute(
            "INSERT INTO ohm_nodes (id, label, type, created_by, created_at) VALUES "
            "('anchor1', 'Anchor', 'concept', 'test', CURRENT_TIMESTAMP)"
        )
        conn.commit()
        status, data = _request("POST", port, "/node", {
            "id": "dec_skill_test",
            "label": "Decision without fields",
            "type": "decision",
            "created_by": "test",
            "connects_to": ["anchor1"],
        })
        assert status == 201
        nudges = data.get("nudges", [])
        decision_nudges = [n for n in nudges if n.get("type") == "decision_node_incomplete"]
        if decision_nudges:
            assert "skill_uri" in decision_nudges[0]
            assert "decision-node" in decision_nudges[0]["skill_uri"]