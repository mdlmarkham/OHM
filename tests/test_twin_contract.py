"""Tests for the twin snap-in contract (OHM-josq)."""

from __future__ import annotations

import json

import pytest

from ohm.schema import VALID_NODE_TYPES, LAYER_EDGE_TYPES


class TestTwinSchemaTypes:
    """Verify twin node type and EVALUATES edge type are registered."""

    def test_twin_node_type_exists(self):
        assert "twin" in VALID_NODE_TYPES

    def test_evaluates_in_l3(self):
        assert "EVALUATES" in LAYER_EDGE_TYPES["L3"]

    def test_twin_requires_cross_link(self):
        from ohm.schema import MUST_HAVE_EDGE_NODE_TYPES

        assert "twin" in MUST_HAVE_EDGE_NODE_TYPES


class TestRegisterTwin:
    """Tests for register_twin() (OHM-josq)."""

    def test_register_twin_creates_node_and_edge(self, test_db):
        from ohm.queries import create_node, register_twin

        target = create_node(test_db, label="Target Concept", node_type="concept", created_by="metis")
        twin = register_twin(
            test_db,
            label="Supply Chain Twin",
            target_node_id=target["id"],
            created_by="metis",
        )
        assert twin["type"] == "twin"
        assert twin["gate_type"] == "external"
        assert twin["id"]

        edges = test_db.execute(
            "SELECT edge_type, layer FROM ohm_edges WHERE from_node = ? AND to_node = ? AND deleted_at IS NULL",
            [twin["id"], target["id"]],
        ).fetchall()
        assert len(edges) >= 1
        assert edges[0][0] == "EVALUATES"
        assert edges[0][1] == "L3"

    def test_register_twin_missing_target_raises(self, test_db):
        from ohm.queries import register_twin
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            register_twin(
                test_db,
                label="Orphan Twin",
                target_node_id="does-not-exist",
                created_by="metis",
            )

    def test_register_twin_with_connects_to(self, test_db):
        from ohm.queries import create_node, register_twin

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        extra = create_node(test_db, label="Extra", node_type="concept", created_by="metis")
        twin = register_twin(
            test_db,
            label="Multi-Target Twin",
            target_node_id=target["id"],
            created_by="metis",
            connects_to=[extra["id"]],
        )
        assert twin["type"] == "twin"

        edges = test_db.execute(
            "SELECT COUNT(*) FROM ohm_edges WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL",
            [twin["id"]],
        ).fetchone()
        assert edges[0] >= 2

    def test_register_twin_with_endpoint_url(self, test_db):
        from ohm.queries import create_node, register_twin

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        twin = register_twin(
            test_db,
            label="URL Twin",
            target_node_id=target["id"],
            created_by="metis",
            endpoint_url="https://twin.example.com/api",
            description="A supply chain twin",
        )
        assert twin["url"] == "https://twin.example.com/api"


class TestTwinPredict:
    """Tests for twin_predict() (OHM-josq)."""

    def test_twin_predict_returns_edge_overrides_dict(self, test_db):
        from ohm.queries import create_node, create_edge, register_twin, twin_predict

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        twin = register_twin(test_db, label="Predict Twin", target_node_id=target["id"], created_by="metis")

        result = twin_predict(test_db, twin_id=twin["id"])
        assert "twin_id" in result
        assert "edge_overrides" in result
        assert "nodes" in result
        assert isinstance(result["edge_overrides"], dict)
        assert target["id"] in result["edge_overrides"]

    def test_twin_predict_missing_twin_raises(self, test_db):
        from ohm.queries import twin_predict
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            twin_predict(test_db, twin_id="does-not-exist")


class TestTwinConstraints:
    """Tests for twin_constraints() (OHM-josq)."""

    def test_twin_constraints_returns_constraint_metadata(self, test_db):
        from ohm.queries import create_node, create_edge, register_twin, twin_constraints

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        twin = register_twin(test_db, label="Constraint Twin", target_node_id=target["id"], created_by="metis")

        other = create_node(test_db, label="Other", node_type="concept", created_by="metis")
        e = create_edge(
            test_db,
            from_node=target["id"],
            to_node=other["id"],
            edge_type="CAUSES",
            layer="L3",
            created_by="metis",
        )
        test_db.execute(
            "UPDATE ohm_edges SET constraint_expr = ? WHERE id = ?",
            ['{"max_confidence": 0.8}', e["id"]],
        )

        result = twin_constraints(test_db, twin_id=twin["id"])
        assert "twin" in result
        assert "evaluates_edges" in result
        assert "constraints" in result
        assert result["twin"]["gate_type"] == "external"
        assert len(result["constraints"]) >= 1

    def test_twin_constraints_missing_twin_raises(self, test_db):
        from ohm.queries import twin_constraints
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            twin_constraints(test_db, twin_id="does-not-exist")


class TestValidateActionAgainstTwin:
    """Tests for validate_action_against_twin() (OHM-josq)."""

    def test_validate_action_no_violations(self, test_db):
        from ohm.queries import create_node, create_edge, propose_action, register_twin, validate_action_against_twin

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        scenario = create_node(test_db, label="Scenario", node_type="scenario", created_by="metis", connects_to=[target["id"]])
        twin = register_twin(test_db, label="Twin", target_node_id=target["id"], created_by="metis")
        action = propose_action(test_db, scenario_id=scenario["id"], label="Safe Action", created_by="metis")

        result = validate_action_against_twin(test_db, twin_id=twin["id"], action_id=action["id"])
        assert result["valid"] is True
        assert result["violations"] == []

    def test_validate_action_with_violation(self, test_db):
        from ohm.queries import create_node, create_edge, propose_action, register_twin, validate_action_against_twin

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        scenario = create_node(test_db, label="Scenario", node_type="scenario", created_by="metis", connects_to=[target["id"]])
        twin = register_twin(test_db, label="Twin", target_node_id=target["id"], created_by="metis")
        action = propose_action(test_db, scenario_id=scenario["id"], label="Risky Action", created_by="metis")

        other = create_node(test_db, label="Other", node_type="concept", created_by="metis")
        constraint_edge = create_edge(
            test_db,
            from_node=target["id"],
            to_node=other["id"],
            edge_type="CAUSES",
            layer="L3",
            created_by="metis",
        )
        test_db.execute(
            "UPDATE ohm_edges SET constraint_expr = ? WHERE id = ?",
            ['{"max_confidence": 0.1}', constraint_edge["id"]],
        )

        result = validate_action_against_twin(test_db, twin_id=twin["id"], action_id=action["id"])
        assert result["valid"] is False
        assert len(result["violations"]) >= 1
        assert result["violations"][0]["violation_type"] == "max_confidence_exceeded"

    def test_validate_action_missing_twin_raises(self, test_db):
        from ohm.queries import validate_action_against_twin
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            validate_action_against_twin(test_db, twin_id="does-not-exist", action_id="some-action")

    def test_validate_action_missing_action_raises(self, test_db):
        from ohm.queries import create_node, register_twin, validate_action_against_twin
        from ohm.exceptions import NodeNotFoundError

        target = create_node(test_db, label="Target", node_type="concept", created_by="metis")
        twin = register_twin(test_db, label="Twin", target_node_id=target["id"], created_by="metis")

        with pytest.raises(NodeNotFoundError):
            validate_action_against_twin(test_db, twin_id=twin["id"], action_id="does-not-exist")


class TestExplainTwin:
    """Tests for explain_twin() (OHM-josq)."""

    def test_explain_twin_returns_readable_summary(self, test_db):
        from ohm.queries import create_node, register_twin, explain_twin

        target = create_node(test_db, label="Target Concept", node_type="concept", created_by="metis")
        twin = register_twin(
            test_db,
            label="Supply Chain Twin",
            target_node_id=target["id"],
            created_by="metis",
            endpoint_url="https://twin.example.com",
        )

        result = explain_twin(test_db, twin_id=twin["id"])
        assert result["twin_id"] == twin["id"]
        assert result["label"] == "Supply Chain Twin"
        assert result["target_node_id"] == target["id"]
        assert result["target_label"] == "Target Concept"
        assert result["endpoint_url"] == "https://twin.example.com"
        assert "constraint_count" in result
        assert "edge_count" in result
        assert "summary" in result
        assert "Supply Chain Twin" in result["summary"]

    def test_explain_twin_missing_raises(self, test_db):
        from ohm.queries import explain_twin
        from ohm.exceptions import NodeNotFoundError

        with pytest.raises(NodeNotFoundError):
            explain_twin(test_db, twin_id="does-not-exist")


class TestTwinSDK:
    """Tests for SDK twin methods (OHM-josq)."""

    def test_sdk_register_twin(self, test_db):
        from ohm.sdk import Graph

        g = Graph(test_db, actor="metis")
        target = g.create_node(label="SDK Target", node_type="concept")
        twin = g.register_twin("SDK Twin", target["id"])
        assert twin["type"] == "twin"
        assert twin["gate_type"] == "external"

    def test_sdk_twin_predict(self, test_db):
        from ohm.sdk import Graph

        g = Graph(test_db, actor="metis")
        target = g.create_node(label="SDK Predict Target", node_type="concept")
        twin = g.register_twin("SDK Predict Twin", target["id"])
        result = g.twin_predict(twin["id"])
        assert "edge_overrides" in result
        assert target["id"] in result["edge_overrides"]

    def test_sdk_twin_constraints(self, test_db):
        from ohm.sdk import Graph

        g = Graph(test_db, actor="metis")
        target = g.create_node(label="SDK Constraint Target", node_type="concept")
        twin = g.register_twin("SDK Constraint Twin", target["id"])
        result = g.twin_constraints(twin["id"])
        assert "twin" in result
        assert "constraints" in result

    def test_sdk_validate_action_against_twin(self, test_db):
        from ohm.sdk import Graph

        g = Graph(test_db, actor="metis")
        target = g.create_node(label="SDK Validate Target", node_type="concept")
        scenario = g.create_node(label="SDK Scenario", node_type="scenario", connects_to=[target["id"]])
        twin = g.register_twin("SDK Validate Twin", target["id"])
        action = g.propose_action(scenario["id"], "SDK Action")
        result = g.validate_action_against_twin(twin["id"], action["id"])
        assert result["valid"] is True

    def test_sdk_explain_twin(self, test_db):
        from ohm.sdk import Graph

        g = Graph(test_db, actor="metis")
        target = g.create_node(label="SDK Explain Target", node_type="concept")
        twin = g.register_twin("SDK Explain Twin", target["id"])
        result = g.explain_twin(twin["id"])
        assert result["twin_id"] == twin["id"]
        assert "summary" in result


@pytest.mark.xdist_group("server")
class TestTwinHTTP:
    """HTTP integration tests for twin endpoints (OHM-josq)."""

    def test_post_register_twin(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            {
                "id": "http_target",
                "label": "HTTP Target",
                "type": "concept",
            },
        )
        assert status in (200, 201)

        status, data = _request(
            "POST",
            port,
            "/twin/register",
            {
                "label": "HTTP Twin",
                "target_node_id": "http_target",
            },
        )
        assert status == 201
        assert data["ok"] is True
        assert data["data"]["type"] == "twin"
        assert data["data"]["gate_type"] == "external"

    def test_get_twin_explain(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            {
                "id": "explain_target",
                "label": "Explain Target",
                "type": "concept",
            },
        )
        assert status in (200, 201)

        status, data = _request(
            "POST",
            port,
            "/twin/register",
            {
                "label": "Explain Twin",
                "target_node_id": "explain_target",
            },
        )
        assert status == 201
        twin_id = data["data"]["id"]

        status, data = _request("GET", port, f"/twin/{twin_id}/explain")
        assert status == 200
        assert data["ok"] is True
        assert "summary" in data["data"]
        assert data["data"]["twin_id"] == twin_id

    def test_get_twin_constraints(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            {
                "id": "constraints_target",
                "label": "Constraints Target",
                "type": "concept",
            },
        )
        assert status in (200, 201)

        status, data = _request(
            "POST",
            port,
            "/twin/register",
            {
                "label": "Constraints Twin",
                "target_node_id": "constraints_target",
            },
        )
        assert status == 201
        twin_id = data["data"]["id"]

        status, data = _request("GET", port, f"/twin/{twin_id}/constraints")
        assert status == 200
        assert data["ok"] is True
        assert "twin" in data["data"]
        assert "constraints" in data["data"]

    def test_post_validate_action_bad_action_id(self, test_server):
        from tests.conftest import _request

        port, store = test_server
        status, data = _request(
            "POST",
            port,
            "/node",
            {
                "id": "validate_target",
                "label": "Validate Target",
                "type": "concept",
            },
        )
        assert status in (200, 201)

        status, data = _request(
            "POST",
            port,
            "/twin/register",
            {
                "label": "Validate Twin",
                "target_node_id": "validate_target",
            },
        )
        assert status == 201
        twin_id = data["data"]["id"]

        status, data = _request(
            "POST",
            port,
            f"/twin/{twin_id}/validate-action",
            {
                "action_id": "does-not-exist",
            },
        )
        assert status == 404
        assert data["ok"] is False
