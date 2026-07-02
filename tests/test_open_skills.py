"""Tests for OHM-461f: Open Skills portable skill contract integration."""

from __future__ import annotations

import json

import pytest

from ohm.graph.schema import VALID_NODE_TYPES, MUST_HAVE_EDGE_NODE_TYPES
from ohm.graph.queries import create_node, create_skill, create_runbook, get_runbook_steps
from ohm.exceptions import ValidationError, NodeNotFoundError


@pytest.fixture
def test_db():
    import duckdb

    from ohm.graph.schema import initialize_schema

    conn = duckdb.connect(":memory:")
    initialize_schema(conn)
    yield conn
    conn.close()


class TestSchemaTypes:
    def test_runbook_is_valid_node_type(self):
        assert "runbook" in VALID_NODE_TYPES

    def test_skill_is_valid_node_type(self):
        assert "skill" in VALID_NODE_TYPES

    def test_runbook_requires_cross_link(self):
        assert "runbook" in MUST_HAVE_EDGE_NODE_TYPES


class TestCreateSkill:
    def test_creates_skill_node(self, test_db):
        anchor = create_node(test_db, label="Anchor", node_type="concept", created_by="test")
        skill = create_skill(
            test_db,
            label="Daily research brief",
            trigger="When agent starts a new session",
            scope="project",
            required_tools=["web_search", "ohm_graph"],
            boundaries="Only research topics, not operational tasks",
            output_format="markdown brief",
            verification_evidence=["source_urls", "ohm_observations"],
            connects_to=[anchor["id"]],
            created_by="metis",
        )
        assert skill["type"] == "skill"
        assert skill["id"]

    def test_skill_metadata_stores_trigger(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        skill = create_skill(
            test_db,
            label="Test Skill",
            trigger="on startup",
            connects_to=[anchor["id"]],
            created_by="test",
        )
        meta = json.loads(skill["metadata"]) if isinstance(skill["metadata"], str) else skill["metadata"]
        assert meta["trigger"] == "on startup"
        assert meta["scope"] == "personal"

    def test_skill_defaults_scope_personal(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        skill = create_skill(
            test_db,
            label="S",
            trigger="t",
            connects_to=[anchor["id"]],
            created_by="test",
        )
        meta = json.loads(skill["metadata"]) if isinstance(skill["metadata"], str) else skill["metadata"]
        assert meta["scope"] == "personal"

    def test_skill_stores_required_tools(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        skill = create_skill(
            test_db,
            label="S",
            trigger="t",
            required_tools=["web_search", "calculator"],
            connects_to=[anchor["id"]],
            created_by="test",
        )
        meta = json.loads(skill["metadata"]) if isinstance(skill["metadata"], str) else skill["metadata"]
        assert meta["required_tools"] == ["web_search", "calculator"]


class TestCreateRunbook:
    def test_creates_runbook_with_chain(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        s1 = create_skill(test_db, label="Step 1", trigger="t1", connects_to=[anchor["id"]], created_by="test")
        s2 = create_skill(test_db, label="Step 2", trigger="t2", connects_to=[anchor["id"]], created_by="test")
        s3 = create_skill(test_db, label="Step 3", trigger="t3", connects_to=[anchor["id"]], created_by="test")

        runbook = create_runbook(
            test_db,
            label="Daily Brief Pipeline",
            skill_ids=[s1["id"], s2["id"], s3["id"]],
            description="Run all three skills in order",
            created_by="test",
        )
        assert runbook["type"] == "runbook"
        assert runbook["id"]

    def test_runbook_creates_depends_on_edges(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        s1 = create_skill(test_db, label="S1", trigger="t", connects_to=[anchor["id"]], created_by="test")
        s2 = create_skill(test_db, label="S2", trigger="t", connects_to=[anchor["id"]], created_by="test")

        runbook = create_runbook(
            test_db,
            label="RB",
            skill_ids=[s1["id"], s2["id"]],
            created_by="test",
        )

        edges = test_db.execute(
            "SELECT from_node, to_node, edge_type FROM ohm_edges WHERE edge_type = 'DEPENDS_ON' AND deleted_at IS NULL"
        ).fetchall()
        assert len(edges) >= 2  # runbook→s1, s1→s2

    def test_runbook_empty_skill_ids_raises(self, test_db):
        with pytest.raises(ValidationError, match="skill_ids"):
            create_runbook(test_db, label="Empty", skill_ids=[], created_by="test")

    def test_runbook_nonexistent_skill_raises(self, test_db):
        with pytest.raises((NodeNotFoundError, ValueError)):
            create_runbook(test_db, label="Bad", skill_ids=["nonexistent_skill"], created_by="test")

    def test_runbook_metadata_stores_skill_ids(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        s1 = create_skill(test_db, label="S1", trigger="t", connects_to=[anchor["id"]], created_by="test")
        s2 = create_skill(test_db, label="S2", trigger="t", connects_to=[anchor["id"]], created_by="test")

        runbook = create_runbook(
            test_db,
            label="RB",
            skill_ids=[s1["id"], s2["id"]],
            created_by="test",
        )
        meta = json.loads(runbook["metadata"]) if isinstance(runbook["metadata"], str) else runbook["metadata"]
        assert meta["skill_ids"] == [s1["id"], s2["id"]]
        assert meta["skill_count"] == 2


class TestGetRunbookSteps:
    def test_returns_ordered_skills(self, test_db):
        anchor = create_node(test_db, label="A", node_type="concept", created_by="test")
        s1 = create_skill(test_db, label="Step One", trigger="t1", connects_to=[anchor["id"]], created_by="test")
        s2 = create_skill(test_db, label="Step Two", trigger="t2", connects_to=[anchor["id"]], created_by="test")
        s3 = create_skill(test_db, label="Step Three", trigger="t3", connects_to=[anchor["id"]], created_by="test")

        runbook = create_runbook(
            test_db,
            label="Pipeline",
            skill_ids=[s1["id"], s2["id"], s3["id"]],
            created_by="test",
        )

        result = get_runbook_steps(test_db, runbook_id=runbook["id"])
        assert result["skill_count"] == 3
        assert result["skills"][0]["label"] == "Step One"
        assert result["skills"][1]["label"] == "Step Two"
        assert result["skills"][2]["label"] == "Step Three"

    def test_nonexistent_runbook_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            get_runbook_steps(test_db, runbook_id="nonexistent")


class TestSDKIntegration:
    def test_sdk_create_skill_and_runbook(self, test_db):
        from ohm.framework.sdk import Graph

        g = Graph(test_db, actor="test_agent")
        anchor = g.create_node("Anchor", node_type="concept")
        skill = g.create_skill(
            "My Skill",
            trigger="on demand",
            scope="universal",
            required_tools=["ohm_graph"],
            connects_to=[anchor["id"]],
        )
        assert skill["type"] == "skill"

        skill2 = g.create_skill("Skill 2", trigger="after step 1", connects_to=[anchor["id"]])
        runbook = g.create_runbook(
            "My Runbook",
            skill_ids=[skill["id"], skill2["id"]],
            description="Two-step pipeline",
        )
        assert runbook["type"] == "runbook"

        steps = g.get_runbook_steps(runbook["id"])
        assert steps["skill_count"] == 2
        assert steps["skills"][0]["label"] == "My Skill"
        assert steps["skills"][1]["label"] == "Skill 2"
