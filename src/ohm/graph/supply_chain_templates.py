"""Supply chain / production / inventory / capacity twin templates (OHM-nbpl).

These templates compose the OHM twin-template catalog (OHM-hl61) with
domain-specific shapes for industrial decision intelligence.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

from ohm.graph.queries import create_node, create_twin_template


def seed_supply_chain_concepts(conn: DuckDBPyConnection) -> dict[str, str]:
    """Seed the foundational concept nodes these templates link to.

    Returns a dict of {role: node_id} for each concept created.
    Used by each template's connects_to to satisfy ADR-018.
    """
    concepts: dict[str, str] = {}
    for role, label in [
        ("machine", "Generic Machine"),
        ("raw_material", "Generic Raw Material"),
        ("finished_good", "Generic Finished Good"),
        ("demand_signal", "Generic Demand Signal"),
        ("replenishment_order", "Generic Replenishment Order"),
        ("warehouse", "Generic Warehouse"),
        ("production_line", "Generic Production Line"),
        ("facility", "Generic Facility"),
        ("geography", "Generic Geography"),
        ("echelon", "Generic Echelon"),
        ("historical_data", "Generic Historical Data Source"),
        ("upstream_driver", "Generic Upstream Driver"),
    ]:
        n = create_node(conn, label=label, node_type="concept", created_by="seed")
        concepts[role] = n["id"]
    return concepts


def create_production_plan_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Template for production plans: schedule feasibility + profit."""
    target = create_node(
        conn,
        label="Production Plan Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("machine", "raw_material", "finished_good"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Production Plan v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Production plan twin: schedule feasibility and profit optimization. Models throughput, batch constraints, and changeover timing.",
        required_edges=["USES", "CONSUMES", "PRODUCES", "TRANSFERRED_TO"],
        constraint_schema={
            "fields": [
                {"field": "max_throughput_per_hour", "type": "number", "min": 0, "unit": "units/hour", "description": "Maximum production throughput per hour"},
                {"field": "min_batch_size", "type": "number", "min": 1, "unit": "units", "description": "Minimum batch size for production runs"},
                {"field": "changeover_time_minutes", "type": "number", "min": 0, "unit": "minutes", "description": "Time required to switch between product runs"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )


def create_inventory_position_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Template for inventory positions: stock levels + reorder timing."""
    target = create_node(
        conn,
        label="Inventory Position Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("demand_signal", "replenishment_order", "warehouse"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Inventory Position v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Inventory position twin: stock levels and reorder timing. Models safety stock, reorder points, and lead time constraints.",
        required_edges=["CONSUMES", "TRANSFERRED_TO", "BELONGS_TO"],
        constraint_schema={
            "fields": [
                {"field": "min_stock_level", "type": "number", "min": 0, "unit": "units", "description": "Minimum allowable stock level"},
                {"field": "max_stock_level", "type": "number", "min": 0, "unit": "units", "description": "Maximum allowable stock level"},
                {"field": "lead_time_days", "type": "number", "min": 0, "unit": "days", "description": "Replenishment lead time in days"},
                {"field": "safety_stock", "type": "number", "min": 0, "unit": "units", "description": "Safety stock buffer quantity"},
                {"field": "reorder_point", "type": "number", "min": 0, "unit": "units", "description": "Stock level at which a reorder is triggered"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )


def create_capacity_planning_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Template for capacity planning: capacity vs. demand over horizon."""
    target = create_node(
        conn,
        label="Capacity Plan Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("production_line", "facility", "demand_signal"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Capacity Planning v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Capacity planning twin: capacity vs. demand over planning horizon. Models utilization targets, ramp-up, and facility constraints.",
        required_edges=["CAPABLE_OF", "INFLUENCES", "LOCATED_IN"],
        constraint_schema={
            "fields": [
                {"field": "max_utilization_pct", "type": "number", "min": 0, "max": 100, "unit": "percent", "description": "Maximum allowable utilization percentage"},
                {"field": "ramp_up_days", "type": "number", "min": 0, "unit": "days", "description": "Days required to ramp capacity to target"},
                {"field": "utilization_target", "type": "number", "min": 0, "max": 100, "unit": "percent", "description": "Target utilization percentage"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )


def create_supply_chain_network_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Template for supply chain networks: multi-echelon flow + risk + cost."""
    target = create_node(
        conn,
        label="Supply Chain Network Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("echelon", "geography", "facility"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Supply Chain Network v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Supply chain network twin: multi-echelon flow risk and cost. Models lead time, service level, and disruption tolerance across the network.",
        required_edges=["TRANSFERRED_TO", "PART_OF", "LOCATED_IN"],
        constraint_schema={
            "fields": [
                {"field": "max_lead_time_days", "type": "number", "min": 0, "unit": "days", "description": "Maximum acceptable end-to-end lead time"},
                {"field": "min_service_level_pct", "type": "number", "min": 0, "max": 100, "unit": "percent", "description": "Minimum service level percentage"},
                {"field": "disruption_tolerance", "type": "number", "min": 0, "max": 1, "unit": "ratio", "description": "Fraction of supply that can be disrupted without critical failure"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )


def create_demand_forecast_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Template for demand forecasts: forecast accuracy + bias."""
    target = create_node(
        conn,
        label="Demand Forecast Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("upstream_driver", "historical_data", "demand_signal"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Demand Forecast v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Demand forecast twin: forecast accuracy and bias. Models MAPE targets, bias tolerance, and forecast horizon.",
        required_edges=["INFLUENCES", "USES", "PRODUCES"],
        constraint_schema={
            "fields": [
                {"field": "mape_target", "type": "number", "min": 0, "unit": "percent", "description": "Target mean absolute percentage error"},
                {"field": "bias_tolerance_pct", "type": "number", "min": 0, "unit": "percent", "description": "Acceptable forecast bias percentage"},
                {"field": "horizon_days", "type": "number", "min": 1, "unit": "days", "description": "Forecast horizon in days"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )


def register_all_supply_chain_templates(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
) -> dict[str, str]:
    """Register all five supply chain templates in the catalog.

    Returns a dict of {template_name: template_id}.
    Seeds foundational concepts first if not provided.
    """
    concepts = seed_supply_chain_concepts(conn)

    templates: dict[str, str] = {}

    pp = create_production_plan_template(conn, created_by=created_by, concepts=concepts)
    templates["production_plan_v1"] = pp["id"]

    ip = create_inventory_position_template(conn, created_by=created_by, concepts=concepts)
    templates["inventory_position_v1"] = ip["id"]

    cp = create_capacity_planning_template(conn, created_by=created_by, concepts=concepts)
    templates["capacity_planning_v1"] = cp["id"]

    sc = create_supply_chain_network_template(conn, created_by=created_by, concepts=concepts)
    templates["supply_chain_network_v1"] = sc["id"]

    df = create_demand_forecast_template(conn, created_by=created_by, concepts=concepts)
    templates["demand_forecast_v1"] = df["id"]

    return templates


def create_network_of_processes_supply_chain_template(
    conn: DuckDBPyConnection,
    *,
    created_by: str = "seed",
    concepts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Hierarchical network-of-processes supply chain template (OHM-nbpl deep-dive).

    Composed of node twins: supplier/source, conversion, buffer, demand, hub/cross-dock.
    Edges: source->conversion, conversion->conversion, conversion->buffer, buffer->demand, return/recycle.
    Each node is a snap-in twin (from OHM-josq).
    """
    target = create_node(
        conn,
        label="Network of Processes Supply Chain Type",
        node_type="concept",
        created_by=created_by,
    )
    connects_to: list[str] = []
    if concepts:
        for key in ("echelon", "geography", "facility", "raw_material", "finished_good"):
            nid = concepts.get(key)
            if nid:
                connects_to.append(nid)

    return create_twin_template(
        conn,
        label="Network of Processes Supply Chain v1",
        target_node_id=target["id"],
        created_by=created_by,
        description="Hierarchical network-of-processes supply chain twin. Composes snap-in node twins for supplier, conversion, buffer, demand, and hub/cross-dock with flow edges between them.",
        required_edges=["TRANSFERRED_TO", "PART_OF", "LOCATED_IN", "FEEDS", "CONSUMES", "PRODUCES"],
        constraint_schema={
            "fields": [
                {"field": "node_types", "type": "list", "values": ["supplier", "conversion", "buffer", "demand", "hub"], "description": "Snap-in node twin types in the network"},
                {"field": "edge_types", "type": "list", "values": ["source_to_conversion", "conversion_to_conversion", "conversion_to_buffer", "buffer_to_demand", "return_recycle"], "description": "Flow edge types between node twins"},
                {"field": "max_lead_time_days", "type": "number", "min": 0, "unit": "days", "description": "Maximum acceptable end-to-end lead time across the network"},
                {"field": "min_service_level_pct", "type": "number", "min": 0, "max": 100, "unit": "percent", "description": "Minimum service level across the network"},
            ]
        },
        connects_to=connects_to if connects_to else None,
    )
