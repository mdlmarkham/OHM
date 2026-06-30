"""Tests for OHM-hl61 twin template catalog."""

from __future__ import annotations

import json

import pytest

from ohm.exceptions import NodeNotFoundError
from ohm.graph.queries import (
    create_node,
    create_twin_template,
    get_twin_template,
    instantiate_twin_from_template,
    list_twin_templates,
)


def _seed_target(conn, label: str = "Target Node") -> str:
    node = create_node(conn, label=label, node_type="concept", created_by="tester")
    return node["id"]


class TestTwinTemplateSchema:
    def test_twin_template_in_valid_types(self, test_db):
        from ohm.graph.schema import VALID_NODE_TYPES

        assert "twin_template" in VALID_NODE_TYPES

    def test_evaluates_in_l3(self, test_db):
        from ohm.graph.schema import LAYER_EDGE_TYPES

        assert "EVALUATES" in LAYER_EDGE_TYPES.get("L3", set())


class TestCreateTwinTemplate:
    def test_creates_twin_template_node(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="HVAC Template",
            target_node_id=target_id,
            created_by="tester",
            constraint_schema={"max_temp": 30.0},
            required_edges=["DEPENDS_ON"],
        )
        assert template["type"] == "twin_template"
        assert template["label"] == "HVAC Template"
        assert template["created_by"] == "tester"

    def test_creates_evaluates_edge_to_target(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="HVAC Template",
            target_node_id=target_id,
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND layer = 'L3'""",
            [template["id"]],
        ).fetchall()
        assert any(e[0] == target_id for e in edges)

    def test_missing_target_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            create_twin_template(
                test_db,
                label="Bad",
                target_node_id="nonexistent_target_xyz",
                created_by="tester",
            )

    def test_stores_constraint_schema_in_metadata(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="Constraint Test",
            target_node_id=target_id,
            created_by="tester",
            constraint_schema={"min_pressure": 1.0, "max_pressure": 5.0},
            required_edges=["SUPPORTS"],
        )
        meta = template.get("metadata")
        assert meta is not None
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["constraint_schema"] == {"min_pressure": 1.0, "max_pressure": 5.0}
        assert meta["required_edges"] == ["SUPPORTS"]


class TestListTwinTemplates:
    def test_returns_all(self, test_db):
        target_id = _seed_target(test_db)
        create_twin_template(
            test_db, label="T1", target_node_id=target_id, created_by="tester"
        )
        create_twin_template(
            test_db, label="T2", target_node_id=target_id, created_by="tester"
        )
        templates = list_twin_templates(test_db)
        assert len(templates) >= 2
        labels = {t["label"] for t in templates}
        assert {"T1", "T2"}.issubset(labels)

    def test_filter_by_target_node_id(self, test_db):
        t1 = _seed_target(test_db, label="T1")
        t2 = _seed_target(test_db, label="T2")
        create_twin_template(test_db, label="A", target_node_id=t1, created_by="tester")
        create_twin_template(test_db, label="B", target_node_id=t2, created_by="tester")
        filtered = list_twin_templates(test_db, target_node_id=t1)
        labels = {t["label"] for t in filtered}
        assert "A" in labels
        assert "B" not in labels

    def test_filter_by_created_by(self, test_db):
        target_id = _seed_target(test_db)
        create_twin_template(test_db, label="A", target_node_id=target_id, created_by="agent-x")
        create_twin_template(test_db, label="B", target_node_id=target_id, created_by="agent-y")
        filtered = list_twin_templates(test_db, created_by="agent-x")
        labels = {t["label"] for t in filtered}
        assert "A" in labels
        assert "B" not in labels


class TestGetTwinTemplate:
    def test_returns_schema(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="Full Schema",
            target_node_id=target_id,
            created_by="tester",
            constraint_schema={"k": "v"},
            required_edges=["CAUSES"],
        )
        result = get_twin_template(test_db, template["id"])
        assert result["template"]["id"] == template["id"]
        assert result["constraint_schema"] == {"k": "v"}
        assert result["required_edges"] == ["CAUSES"]
        assert len(result["evaluates_edges"]) >= 1

    def test_missing_raises(self, test_db):
        with pytest.raises(NodeNotFoundError):
            get_twin_template(test_db, "nonexistent_template_abc")


class TestInstantiateTwinFromTemplate:
    def test_creates_twin(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="HVAC",
            target_node_id=target_id,
            created_by="tester",
        )
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"
        assert twin["gate_type"] == "template"

    def test_links_to_target(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db, label="T", target_node_id=target_id, created_by="tester"
        )
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_id,
            created_by="tester",
        )
        edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND layer = 'L3'""",
            [twin["id"]],
        ).fetchall()
        assert any(e[0] == target_id for e in edges)

    def test_discovers_required_edges(self, test_db):
        target_id = _seed_target(test_db)
        related_id = _seed_target(test_db, label="Related")
        create_edge = __import__("ohm.graph.queries", fromlist=["create_edge"]).create_edge
        create_edge(
            test_db,
            from_node=target_id,
            to_node=related_id,
            edge_type="CAUSES",
            layer="L3",
            created_by="tester",
        )
        template = create_twin_template(
            test_db,
            label="WithReqs",
            target_node_id=target_id,
            created_by="tester",
            required_edges=["CAUSES"],
        )
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_id,
            created_by="tester",
        )
        twin_edges = test_db.execute(
            "SELECT to_node FROM ohm_edges WHERE from_node = ? AND deleted_at IS NULL",
            [twin["id"]],
        ).fetchall()
        targets = {e[0] for e in twin_edges}
        assert target_id in targets
        assert related_id in targets

    def test_copies_constraint_schema(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db,
            label="WithConstraints",
            target_node_id=target_id,
            created_by="tester",
            constraint_schema={"limit": 42},
        )
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_id,
            created_by="tester",
        )
        meta = twin.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["constraint_schema"] == {"limit": 42}
        assert meta["source_template_id"] == template["id"]

    def test_missing_template_raises(self, test_db):
        target_id = _seed_target(test_db)
        with pytest.raises(NodeNotFoundError):
            instantiate_twin_from_template(
                test_db,
                template_id="nonexistent_template_xyz",
                target_node_id=target_id,
                created_by="tester",
            )

    def test_missing_target_raises(self, test_db):
        target_id = _seed_target(test_db)
        template = create_twin_template(
            test_db, label="T", target_node_id=target_id, created_by="tester"
        )
        with pytest.raises(NodeNotFoundError):
            instantiate_twin_from_template(
                test_db,
                template_id=template["id"],
                target_node_id="nonexistent_target_abc",
                created_by="tester",
            )


class TestTwinTemplateSDK:
    def test_sdk_create_and_list(self, test_db):
        from ohm.framework.sdk import Graph

        target_id = _seed_target(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            template = g.create_twin_template(
                "SDK Template",
                target_node_id=target_id,
                constraint_schema={"x": 1},
                required_edges=["CAUSES"],
            )
            assert template["type"] == "twin_template"
            templates = g.list_twin_templates()
            assert any(t["id"] == template["id"] for t in templates)

    def test_sdk_instantiate(self, test_db):
        from ohm.framework.sdk import Graph

        target_id = _seed_target(test_db)
        with Graph(test_db, actor="sdk-tester") as g:
            template = g.create_twin_template(
                "SDK Template", target_node_id=target_id
            )
            twin = g.instantiate_twin_from_template(
                template_id=template["id"],
                label="My Twin",
                target_node_id=target_id,
            )
            assert twin["type"] == "twin"
            fetched = g.get_twin_template(template["id"])
            assert fetched["template"]["id"] == template["id"]
