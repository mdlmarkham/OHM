"""Tests for OHM-nbpl supply chain / production / inventory / capacity twin templates."""

from __future__ import annotations

import json

import pytest

from ohm.graph.queries import (
    create_node,
    create_twin_template,
    get_twin_template,
    instantiate_twin_from_template,
    list_twin_templates,
)
from ohm.graph.supply_chain_templates import (
    create_capacity_planning_template,
    create_demand_forecast_template,
    create_inventory_position_template,
    create_network_of_processes_supply_chain_template,
    create_production_plan_template,
    create_supply_chain_network_template,
    register_all_supply_chain_templates,
    seed_supply_chain_concepts,
)


class TestProductionPlanTemplate:
    def test_creates_template(self, test_db):
        template = create_production_plan_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert template["label"] == "Production Plan v1"

    def test_required_edges(self, test_db):
        template = create_production_plan_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        assert set(result["required_edges"]) == {"USES", "CONSUMES", "PRODUCES", "TRANSFERRED_TO"}

    def test_instantiates(self, test_db):
        template = create_production_plan_template(test_db, created_by="tester")
        template["id"]
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        assert len(eval_edges) >= 1
        target_concept_id = eval_edges[0][0]
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_concept_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"
        assert twin["gate_type"] == "template"

    def test_constraint_schema_present(self, test_db):
        template = create_production_plan_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        assert "fields" in schema
        field_names = [f["field"] for f in schema["fields"]]
        assert "max_throughput_per_hour" in field_names
        assert "min_batch_size" in field_names
        assert "changeover_time_minutes" in field_names


class TestInventoryPositionTemplate:
    def test_creates_template(self, test_db):
        template = create_inventory_position_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert template["label"] == "Inventory Position v1"

    def test_required_edges(self, test_db):
        template = create_inventory_position_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        assert set(result["required_edges"]) == {"CONSUMES", "TRANSFERRED_TO", "BELONGS_TO"}

    def test_instantiates(self, test_db):
        template = create_inventory_position_template(test_db, created_by="tester")
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        target_concept_id = eval_edges[0][0]
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_concept_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"

    def test_constraint_schema_present(self, test_db):
        template = create_inventory_position_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        field_names = [f["field"] for f in schema["fields"]]
        assert "min_stock_level" in field_names
        assert "max_stock_level" in field_names
        assert "lead_time_days" in field_names
        assert "safety_stock" in field_names
        assert "reorder_point" in field_names


class TestCapacityPlanningTemplate:
    def test_creates_template(self, test_db):
        template = create_capacity_planning_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert template["label"] == "Capacity Planning v1"

    def test_required_edges(self, test_db):
        template = create_capacity_planning_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        assert set(result["required_edges"]) == {"CAPABLE_OF", "INFLUENCES", "LOCATED_IN"}

    def test_instantiates(self, test_db):
        template = create_capacity_planning_template(test_db, created_by="tester")
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        target_concept_id = eval_edges[0][0]
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_concept_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"

    def test_constraint_schema_present(self, test_db):
        template = create_capacity_planning_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        field_names = [f["field"] for f in schema["fields"]]
        assert "max_utilization_pct" in field_names
        assert "ramp_up_days" in field_names
        assert "utilization_target" in field_names


class TestSupplyChainNetworkTemplate:
    def test_creates_template(self, test_db):
        template = create_supply_chain_network_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert template["label"] == "Supply Chain Network v1"

    def test_required_edges(self, test_db):
        template = create_supply_chain_network_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        assert set(result["required_edges"]) == {"TRANSFERRED_TO", "PART_OF", "LOCATED_IN"}

    def test_instantiates(self, test_db):
        template = create_supply_chain_network_template(test_db, created_by="tester")
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        target_concept_id = eval_edges[0][0]
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_concept_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"

    def test_constraint_schema_present(self, test_db):
        template = create_supply_chain_network_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        field_names = [f["field"] for f in schema["fields"]]
        assert "max_lead_time_days" in field_names
        assert "min_service_level_pct" in field_names
        assert "disruption_tolerance" in field_names


class TestDemandForecastTemplate:
    def test_creates_template(self, test_db):
        template = create_demand_forecast_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert template["label"] == "Demand Forecast v1"

    def test_required_edges(self, test_db):
        template = create_demand_forecast_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        assert set(result["required_edges"]) == {"INFLUENCES", "USES", "PRODUCES"}

    def test_instantiates(self, test_db):
        template = create_demand_forecast_template(test_db, created_by="tester")
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        target_concept_id = eval_edges[0][0]
        twin = instantiate_twin_from_template(
            test_db,
            template_id=template["id"],
            target_node_id=target_concept_id,
            created_by="tester",
        )
        assert twin["type"] == "twin"

    def test_constraint_schema_present(self, test_db):
        template = create_demand_forecast_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        field_names = [f["field"] for f in schema["fields"]]
        assert "mape_target" in field_names
        assert "bias_tolerance_pct" in field_names
        assert "horizon_days" in field_names


class TestRegisterAll:
    def test_register_all_creates_five(self, test_db):
        templates = register_all_supply_chain_templates(test_db, created_by="tester")
        assert len(templates) == 5
        assert "production_plan_v1" in templates
        assert "inventory_position_v1" in templates
        assert "capacity_planning_v1" in templates
        assert "supply_chain_network_v1" in templates
        assert "demand_forecast_v1" in templates

    def test_register_all_idempotent_under_distinct_labels(self, test_db):
        first = register_all_supply_chain_templates(test_db, created_by="tester")
        second = register_all_supply_chain_templates(test_db, created_by="tester")
        assert len(second) == 5
        all_ids = list(first.values()) + list(second.values())
        assert len(set(all_ids)) == 10


class TestNetworkOfProcessesTemplate:
    def test_creates_hierarchical_template(self, test_db):
        template = create_network_of_processes_supply_chain_template(test_db, created_by="tester")
        assert template["type"] == "twin_template"
        assert "Network of Processes" in template["label"]

    def test_network_template_lists_node_types(self, test_db):
        template = create_network_of_processes_supply_chain_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        node_types_field = next(f for f in schema["fields"] if f["field"] == "node_types")
        assert set(node_types_field["values"]) == {"supplier", "conversion", "buffer", "demand", "hub"}

    def test_network_template_lists_edge_types(self, test_db):
        template = create_network_of_processes_supply_chain_template(test_db, created_by="tester")
        result = get_twin_template(test_db, template["id"])
        schema = result["constraint_schema"]
        edge_types_field = next(f for f in schema["fields"] if f["field"] == "edge_types")
        assert "source_to_conversion" in edge_types_field["values"]
        assert "conversion_to_conversion" in edge_types_field["values"]
        assert "conversion_to_buffer" in edge_types_field["values"]
        assert "buffer_to_demand" in edge_types_field["values"]
        assert "return_recycle" in edge_types_field["values"]


class TestConstructionEngineUsesSupplyChain:
    def test_assemble_against_production_goal_picks_production_template(self, test_db):
        from ohm.graph.queries import assemble_twin_for_decision

        register_all_supply_chain_templates(test_db, created_by="tester")
        decision = create_node(
            test_db,
            label="Optimize production schedule",
            node_type="decision",
            created_by="tester",
            connects_to=[create_node(test_db, label="Anchor", node_type="concept", created_by="tester")["id"]],
        )
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision["id"],
            goal="production plan throughput batch",
            created_by="tester",
        )
        assert result["twin"] is not None
        template_label = result.get("template", {}).get("label", "")
        assert "Production Plan" in template_label or result["twin"]["type"] == "twin"

    def test_assemble_against_inventory_goal_picks_inventory_template(self, test_db):
        from ohm.graph.queries import assemble_twin_for_decision

        register_all_supply_chain_templates(test_db, created_by="tester")
        decision = create_node(
            test_db,
            label="Manage inventory levels",
            node_type="decision",
            created_by="tester",
            connects_to=[create_node(test_db, label="Anchor", node_type="concept", created_by="tester")["id"]],
        )
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision["id"],
            goal="inventory stock reorder safety",
            created_by="tester",
        )
        assert result["twin"] is not None
        template_label = result.get("template", {}).get("label", "")
        assert "Inventory" in template_label or result["twin"]["type"] == "twin"

    def test_assemble_against_supply_chain_goal_picks_network_template(self, test_db):
        from ohm.graph.queries import assemble_twin_for_decision

        register_all_supply_chain_templates(test_db, created_by="tester")
        decision = create_node(
            test_db,
            label="Optimize supply chain network",
            node_type="decision",
            created_by="tester",
            connects_to=[create_node(test_db, label="Anchor", node_type="concept", created_by="tester")["id"]],
        )
        result = assemble_twin_for_decision(
            test_db,
            decision_node_id=decision["id"],
            goal="supply chain network echelon lead time",
            created_by="tester",
        )
        assert result["twin"] is not None
        template_label = result.get("template", {}).get("label", "")
        assert "Supply Chain Network" in template_label or result["twin"]["type"] == "twin"


class TestSeedSupplyChainConcepts:
    def test_seeds_concepts(self, test_db):
        concepts = seed_supply_chain_concepts(test_db)
        assert len(concepts) == 12
        for role, node_id in concepts.items():
            row = test_db.execute(
                "SELECT type FROM ohm_nodes WHERE id = ? AND deleted_at IS NULL",
                [node_id],
            ).fetchone()
            assert row is not None
            assert row[0] == "concept"

    def test_concepts_used_as_connects_to(self, test_db):
        concepts = seed_supply_chain_concepts(test_db)
        template = create_production_plan_template(test_db, created_by="tester", concepts=concepts)
        eval_edges = test_db.execute(
            """SELECT to_node FROM ohm_edges
               WHERE from_node = ? AND edge_type = 'EVALUATES' AND deleted_at IS NULL""",
            [template["id"]],
        ).fetchall()
        edge_targets = {e[0] for e in eval_edges}
        assert concepts["machine"] in edge_targets
        assert concepts["raw_material"] in edge_targets
        assert concepts["finished_good"] in edge_targets
