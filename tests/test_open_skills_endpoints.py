"""Tests for OHM-461f.1: Open Skills schema guide, templates, and query endpoints."""

from __future__ import annotations

import duckdb
import pytest

from ohm.graph.schema import DEFAULT_SCHEMA, initialize_schema
from ohm.graph.queries import (
    node_type_template,
    skill_runbook_query_guide,
    suggest_edge_type,
    create_skill,
    create_runbook,
    create_node,
)


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    initialize_schema(c, DEFAULT_SCHEMA)
    return c


class TestNodeTypeTemplate:
    """GET /templates?type=<node_type> — node_type_template()."""

    def test_skill_template(self, conn):
        result = node_type_template(conn, node_type="skill")
        assert result["node_type"] == "skill"
        assert "trigger" in result["required_fields"]
        assert "scope" in result["optional_fields"]
        assert "example" in result
        assert "suggested_edge_types" in result
        assert result["create_endpoint"] == "POST /skill"

    def test_runbook_template(self, conn):
        result = node_type_template(conn, node_type="runbook")
        assert result["node_type"] == "runbook"
        assert "skill_ids" in result["required_fields"]
        assert "example" in result
        assert result["create_endpoint"] == "POST /runbook"
        assert result["query_endpoint"] == "GET /runbook/{id}/steps"

    def test_unknown_type_returns_available(self, conn):
        result = node_type_template(conn, node_type="unknown")
        assert "error" in result
        assert "skill" in result["available_types"]
        assert "runbook" in result["available_types"]

    def test_template_is_case_insensitive(self, conn):
        result = node_type_template(conn, node_type="SKILL")
        assert result["node_type"] == "skill"


class TestSkillRunbookQueryGuide:
    """GET /queries?domain=skill — skill_runbook_query_guide()."""

    def test_returns_query_list(self, conn):
        result = skill_runbook_query_guide(conn)
        assert "queries" in result
        assert len(result["queries"]) >= 5
        names = {q["name"] for q in result["queries"]}
        assert "list_all_skills" in names
        assert "list_all_runbooks" in names
        assert "get_runbook_steps" in names
        assert "suggest_edge_for_skill_pair" in names

    def test_returns_edge_type_guide(self, conn):
        result = skill_runbook_query_guide(conn)
        assert "edge_types" in result
        assert "skill_to_skill" in result["edge_types"]
        assert "runbook_to_skill" in result["edge_types"]
        assert "agent_to_skill" in result["edge_types"]

    def test_each_query_has_endpoint_and_description(self, conn):
        result = skill_runbook_query_guide(conn)
        for q in result["queries"]:
            assert "endpoint" in q
            assert "description" in q
            assert q["endpoint"].startswith("GET ")


class TestSuggestEdgeTypeForSkills:
    """Edge type suggestions for skill/runbook pairs (OHM-461f.1 acceptance #4)."""

    @pytest.fixture
    def skill_and_runbook(self, conn):
        skill_a = create_skill(
            conn,
            label="Skill A",
            trigger="When X happens",
            created_by="test",
            connects_to=None,
        )
        skill_b = create_skill(
            conn,
            label="Skill B",
            trigger="When Y happens",
            created_by="test",
            connects_to=None,
        )
        runbook = create_runbook(
            conn,
            label="My Runbook",
            skill_ids=[skill_a["id"], skill_b["id"]],
            created_by="test",
            connects_to=None,
        )
        return skill_a, skill_b, runbook

    def test_runbook_to_skill_suggests_depends_on(self, conn, skill_and_runbook):
        skill_a, _skill_b, runbook = skill_and_runbook
        result = suggest_edge_type(conn, from_node_id=runbook["id"], to_node_id=skill_a["id"])
        assert result["suggested_edge_type"] == "DEPENDS_ON"
        assert result["suggested_layer"] == "L4"
        assert "runbook" in result["reasoning"].lower()

    def test_skill_to_runbook_suggests_enables(self, conn, skill_and_runbook):
        skill_a, _skill_b, runbook = skill_and_runbook
        result = suggest_edge_type(conn, from_node_id=skill_a["id"], to_node_id=runbook["id"])
        assert result["suggested_edge_type"] == "ENABLES"
        assert result["suggested_layer"] == "L4"

    def test_skill_to_skill_suggests_depends_on(self, conn, skill_and_runbook):
        skill_a, skill_b, _runbook = skill_and_runbook
        result = suggest_edge_type(conn, from_node_id=skill_a["id"], to_node_id=skill_b["id"])
        assert result["suggested_edge_type"] == "DEPENDS_ON"
        assert result["suggested_layer"] == "L4"

    def test_skill_to_agent_suggests_capable_of(self, conn, skill_and_runbook):
        skill_a, _skill_b, _runbook = skill_and_runbook
        agent = create_node(conn, label="Test Agent", node_type="agent", created_by="test")
        result = suggest_edge_type(conn, from_node_id=skill_a["id"], to_node_id=agent["id"])
        assert result["suggested_edge_type"] == "CAPABLE_OF"
        assert result["suggested_layer"] == "L1"