# Supply Chain Disruption Scenario

Complete agent workflow for multi-tier BOM risk modeling using OHM's probability-weighted edges, cascade simulation, and what-if analysis.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **Supplier Monitor** | Track supplier health, lead times | L2 | `create_edge(DEPENDS_ON)`, `observe()` |
| **Logistics** | Shipping, inventory levels, delays | L2 | `create_edge(FLOWS_TO)`, `observe()` |
| **Risk Analyst** | Disruption probability estimation | L3 | `create_edge(EXPECTED_LIKELIHOOD)`, `cascade_scenario()` |
| **Operations** | Production scheduling, capacity | L3 | `what_if()`, `composite_score()` |
| **Supply Chain Director** | Strategic decisions, mitigation | L4 | `challenge()`, `contradictions()`, `detect_trend()` |

## Workflow 1: Multi-Tier Cascade Simulation

### Step 1 — Build the supply chain graph

```python
import ohm.sdk as ohm

with ohm.connect("supply_chain.duckdb", actor="supplier_monitor") as g:
    g.register_agent(values=["resilience", "visibility"])

    # Tier 3: Raw material suppliers
    chip_supplier = g.create_node(
        label="Chip Supplier — Taiwan Semiconductor",
        node_type="system",
        priority="P1",
    )
    chemical_supplier = g.create_node(
        label="Chemical Supplier — BASF",
        node_type="system",
        priority="P2",
    )

    # Tier 2: Component manufacturers
    pcb_manufacturer = g.create_node(
        label="PCB Manufacturer — Flex Ltd",
        node_type="system",
        priority="P1",
    )
    battery_manufacturer = g.create_node(
        label="Battery Manufacturer — CATL",
        node_type="system",
        priority="P1",
    )

    # Tier 1: Final assembly
    assembly_plant = g.create_node(
        label="Assembly Plant — Guadalajara",
        node_type="site",
        priority="P0",
    )

    # Distribution
    distribution_center = g.create_node(
        label="Distribution Center — Dallas",
        node_type="site",
        priority="P1",
    )

    # Build dependency edges with probabilities
    g.create_edge(
        from_node=chip_supplier["id"],
        to_node=pcb_manufacturer["id"],
        edge_type="DEPENDS_ON",
        layer="L2",
        probability=0.05,  # 5% chance of disruption per quarter
    )
    g.create_edge(
        from_node=chemical_supplier["id"],
        to_node=battery_manufacturer["id"],
        edge_type="DEPENDS_ON",
        layer="L2",
        probability=0.03,
    )
    g.create_edge(
        from_node=pcb_manufacturer["id"],
        to_node=assembly_plant["id"],
        edge_type="FLOWS_TO",
        layer="L2",
        probability=0.08,
    )
    g.create_edge(
        from_node=battery_manufacturer["id"],
        to_node=assembly_plant["id"],
        edge_type="FLOWS_TO",
        layer="L2",
        probability=0.06,
    )
    g.create_edge(
        from_node=assembly_plant["id"],
        to_node=distribution_center["id"],
        edge_type="FLOWS_TO",
        layer="L2",
        probability=0.04,
    )
```

### Step 2 — Risk Analyst runs cascade simulation

```python
with ohm.connect("supply_chain.duckdb", actor="risk_analyst") as g:
    g.register_agent(values=["risk", "continuity"])

    # What if the chip supplier fails? (100% probability for simulation)
    cascade = g.cascade_scenario(
        chip_supplier["id"],
        failure_probability=1.0,
        max_depth=10,
    )

    print("=== Cascade Analysis: Chip Supplier Failure ===")
    for node in cascade:
        print(f"  {node['node_label']}: "
              f"P(failure)={node['failure_probability']:.3f}, "
              f"depth={node['depth']}, "
              f"path={' → '.join(node.get('path', []))}")

    # What if the chemical supplier fails?
    cascade_chem = g.cascade_scenario(
        chemical_supplier["id"],
        failure_probability=1.0,
    )
    print(f"\nChemical supplier cascade: {len(cascade_chem)} downstream nodes")
```

### Step 3 — Operations runs what-if on specific edges

```python
with ohm.connect("supply_chain.duckdb", actor="operations") as g:
    g.register_agent(values=["production", "efficiency"])

    # Find the PCB manufacturer → assembly plant edge
    edges = g.query(filter_type="FLOWS_TO", layer="L2")
    pcb_edge = None
    for e in edges:
        if e["from_node"] == pcb_manufacturer["id"]:
            pcb_edge = e
            break

    if pcb_edge:
        # What if this specific edge's event occurs?
        impact = g.what_if(pcb_edge["id"], max_depth=10)
        print(f"What-if analysis for {pcb_edge['id']}:")
        print(f"  Downstream impact: {impact}")

    # Composite score for assembly plant risk
    score = g.composite_score(
        assembly_plant["id"],
        method="geometric",
        observation_weight=0.4,
        evidence_weight=0.6,
    )
    print(f"Assembly plant risk composite: {score['composite_score']:.2f}")
```

### Step 4 — Supply Chain Director challenges and mitigates

```python
with ohm.connect("supply_chain.duckdb", actor="supply_chain_director") as g:
    g.register_agent(values=["strategy", "cost", "resilience"])

    # Challenge the single-source dependency on chip supplier
    chip_edges = g.query(filter_type="DEPENDS_ON", layer="L2")
    for edge in chip_edges:
        if edge["to_node"] == pcb_manufacturer["id"]:
            g.challenge(
                edge["id"],
                reason="Single-source risk — recommend qualifying secondary "
                       "supplier (Samsung) within 90 days",
                confidence=0.9,
            )

    # Detect trend in supplier lead times
    trend = g.detect_trend(chip_supplier["id"], window_days=90)
    print(f"Chip supplier lead time trend: {trend['trend']} "
          f"(slope: {trend['slope_per_day']:.4f}/day, "
          f"R²: {trend.get('r_squared', 'N/A')})")

    # Mitigation plan
    mitigation = g.create_node(
        label="Mitigation: Dual-source chips (TSMC + Samsung) by Q3 2026",
        node_type="plan",
        priority="P0",
    )
    g.create_edge(
        from_node=mitigation["id"],
        to_node=assembly_plant["id"],
        edge_type="PREDICTS",
        layer="L4",
        confidence=0.85,
        condition="Reduces P(disruption) from 0.05 to 0.01",
    )
```

## Workflow 2: Probability vs Confidence Distinction

```python
with ohm.connect("supply_chain.duckdb", actor="risk_analyst") as g:
    # probability: how likely is the event? (0.05 = 5% chance)
    # confidence: how sure am I about this estimate? (0.9 = 90% sure)
    g.create_edge(
        from_node=chip_supplier["id"],
        to_node=pcb_manufacturer["id"],
        edge_type="EXPECTED_LIKELIHOOD",
        layer="L3",
        probability=0.05,   # 5% chance of disruption
        confidence=0.9,     # 90% confident in this estimate
    )

    # Cascade uses probability for computation, confidence for weighting
    cascade = g.cascade_scenario(
        chip_supplier["id"],
        failure_probability=0.05,  # Use the actual probability
    )
    for node in cascade:
        print(f"{node['node_label']}: P(failure)={node['failure_probability']:.4f}")
```

## Key Reasoning Primitives Used

| Primitive | Supply Chain Use Case |
|-----------|----------------------|
| `cascade_scenario()` | Monte Carlo downstream failure propagation from supplier disruption |
| `what_if()` | Dry-run impact of specific edge failure without modifying graph |
| `probability` field | Distinct from confidence: P(event) vs P(estimate is correct) |
| `composite_score(method="geometric")` | Compound multi-tier disruption probabilities |
| `detect_trend()` | Track supplier lead time degradation over 90 days |
| `challenge()` | Challenge single-source dependency, propose dual-sourcing |
| `contradictions()` | Surface conflicting risk assessments from different analysts |
