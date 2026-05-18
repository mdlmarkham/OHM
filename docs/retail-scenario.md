# Retail Scenario

Complete agent workflow for inventory management, demand forecasting, and supplier coordination using OHM's reasoning primitives.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **Weather** | Temperature observations, demand multipliers | L2 | `observe()`, `create_edge(INFLUENCES)` |
| **Marketing** | Promotion performance tracking | L3 | `create_edge(APPLIES_TO)`, `composite_score()` |
| **Staff Planning** | Labor hour composition | L3 | `composite_score(method="arithmetic")` |
| **Inventory** | Batch tracking, expiry alerts | L2 | `expiring_soon()`, `create_edge(BATCH_EXPIRES_BEFORE)` |
| **Finance** | Revenue vs cost tracking | L2 | `create_edge(CAUSES)`, `detect_trend()` |
| **Competitor** | Competitive intelligence | L3 | `create_edge(COMPETITOR_RUNS_SALE)`, `contradictions()` |
| **Store Manager** | Decision synthesis, conflict resolution | L4 | `challenge()`, `contradictions()`, `composite_score()` |

## Workflow 1: Demand Forecasting with Multiplicative Factors

### Step 1 — Weather agent observes temperature

```python
import ohm.sdk as ohm

with ohm.connect("retail.duckdb", actor="weather") as g:
    g.register_agent(values=["forecast", "demand"])

    # Create store location node
    store = g.create_node(
        label="Store #7 — Downtown",
        node_type="site",
        priority="P1",
    )

    # Temperature observations throughout the day
    g.observe(
        store["id"],
        obs_type="temperature_f",
        value=94.0,
        sigma=1.0,
        metadata={"timestamp": "2026-05-17T14:00:00Z"},
    )
    g.observe(
        store["id"],
        obs_type="temperature_f",
        value=88.0,
        sigma=1.0,
        metadata={"timestamp": "2026-05-17T10:00:00Z"},
    )

    # Create demand multiplier node (hot day = 1.3x baseline demand)
    hot_day_factor = g.create_node(
        label="Hot Day Demand Multiplier (1.3x)",
        node_type="concept",
    )
    g.observe(
        hot_day_factor["id"],
        obs_type="multiplier",
        value=1.3,
        sigma=0.1,
    )

    g.create_edge(
        from_node=hot_day_factor["id"],
        to_node=store["id"],
        edge_type="INFLUENCES",
        layer="L2",
        confidence=0.85,
    )
```

### Step 2 — Marketing agent tracks promotion

```python
with ohm.connect("retail.duckdb", actor="marketing") as g:
    g.register_agent(values=["revenue", "engagement"])

    # Promotion node
    promotion = g.create_node(
        label="50% Off Cold Beverages (May 17)",
        node_type="event",
    )

    # Promotion performance observation
    g.observe(
        promotion["id"],
        obs_type="demand_multiplier",
        value=2.0,  # 2x normal demand during promotion
        sigma=0.15,
    )

    g.create_edge(
        from_node=promotion["id"],
        to_node=store["id"],
        edge_type="APPLIES_TO",
        layer="L3",
        confidence=0.9,
    )
```

### Step 3 — Composite demand score (multiplicative)

```python
with ohm.connect("retail.duckdb", actor="staff_planning") as g:
    g.register_agent(values=["efficiency", "labor"])

    # Compute multiplicative composite score
    # Hot day (1.3x) × Promotion (2.0x) = 2.6x baseline demand
    arith_score = g.composite_score(
        store["id"],
        method="arithmetic",
        observation_weight=0.5,
        evidence_weight=0.5,
    )
    geom_score = g.composite_score(
        store["id"],
        method="geometric",
        observation_weight=0.5,
        evidence_weight=0.5,
    )

    print(f"Arithmetic composite: {arith_score['composite_score']:.2f} "
          f"(wrong — averages factors)")
    print(f"Geometric composite:  {geom_score['composite_score']:.2f} "
          f"(correct — compounds factors)")

    # Plan labor hours based on geometric score
    baseline_hours = 80.0
    required_hours = baseline_hours * geom_score["composite_score"]
    print(f"Staffing: {required_hours:.0f} labor hours needed "
          f"(baseline: {baseline_hours})")
```

### Step 4 — Temporal decay for time-sensitive observations

```python
with ohm.connect("retail.duckdb", actor="weather") as g:
    g.register_agent(values=["forecast"])

    # Apply temporal decay with 4-hour half-life (retail-appropriate)
    # The 10am reading at 4pm has aged 6 hours = 1.5 half-lives
    # Weight = 0.5^1.5 ≈ 0.35
    decayed = g.decay_observations(
        store["id"],
        temporal_decay_hours=4.0,
        dry_run=True,
    )
    for d in decayed:
        print(f"Obs {d['id']}: value={d['original_value']:.1f} → "
              f"decayed={d['decayed_value']:.2f} "
              f"(age: {d['age_hours']:.1f}h, factor: {d['decay_factor']:.3f})")

    # Composite with temporal decay
    score = g.composite_score(
        store["id"],
        method="geometric",
        temporal_decay_hours=4.0,
    )
    print(f"Time-weighted demand composite: {score['composite_score']:.2f}")
```

## Workflow 2: Inventory Batch Expiry Tracking

```python
with ohm.connect("retail.duckdb", actor="inventory") as g:
    g.register_agent(values=["freshness", "waste_reduction"])

    from datetime import datetime, timezone, timedelta

    # Create product batch nodes
    milk_batch = g.create_node(
        label="Milk Batch #42 — 200 gallons",
        node_type="equipment",
    )
    produce_batch = g.create_node(
        label="Lettuce Batch #18 — 50 cases",
        node_type="equipment",
    )

    # Create location nodes
    cooler = g.create_node(label="Walk-in Cooler #3", node_type="site")
    produce_section = g.create_node(label="Produce Section", node_type="area")

    # Create expiry edges with metadata
    milk_expires = datetime.now(timezone.utc) + timedelta(days=3)
    produce_expires = datetime.now(timezone.utc) + timedelta(days=1)

    g.create_edge(
        from_node=milk_batch["id"],
        to_node=cooler["id"],
        edge_type="BATCH_EXPIRES_BEFORE",
        layer="L2",
        metadata={"expires_at": milk_expires.isoformat()},
        urgency="medium",
    )
    g.create_edge(
        from_node=produce_batch["id"],
        to_node=produce_section["id"],
        edge_type="BATCH_EXPIRES_BEFORE",
        layer="L2",
        metadata={"expires_at": produce_expires.isoformat()},
        urgency="high",
    )

    # Find all batches expiring within 5 days
    expiring = g.expiring_soon(days=5)
    for batch in expiring:
        print(f"EXPIRING: {batch['batch_label']} — "
              f"{batch['days_until_expiry']} days left "
              f"(urgency: {batch.get('urgency', 'N/A')})")

    # Filter by product type
    dairy_expiring = g.expiring_soon(product_type="equipment", days=5)
    print(f"Dairy batches expiring soon: {len(dairy_expiring)}")

    # Escalate urgent batches
    for batch in expiring:
        if batch["days_until_expiry"] < 2:
            g.escalate(batch["edge_id"], "critical")
            print(f"ESCALATED: {batch['batch_label']} to critical")
```

## Workflow 3: Competitor Contradiction Resolution

```python
with ohm.connect("retail.duckdb", actor="competitor") as g:
    g.register_agent(values=["market_intel"])

    # Competitor running a sale on the same day
    competitor_sale = g.create_node(
        label="Competitor: 40% Off Cold Beverages (May 17)",
        node_type="event",
    )

    g.create_edge(
        from_node=competitor_sale["id"],
        to_node=store["id"],
        edge_type="COMPETITOR_RUNS_SALE",
        layer="L3",
        confidence=0.95,
        urgency="high",
    )

# Finance agent detects contradiction
with ohm.connect("retail.duckdb", actor="finance") as g:
    g.register_agent(values=["margin", "revenue"])

    # Actual sales vs predicted
    actual_sales = g.create_node(
        label="Actual Sales — Below Forecast (May 17)",
        node_type="event",
    )
    g.observe(
        actual_sales["id"],
        obs_type="revenue_vs_forecast",
        value=0.72,  # 72% of forecast
        sigma=0.05,
    )

    # Check for contradictions
    conflicts = g.contradictions()
    for c in conflicts:
        print(f"CONTRADICTS: {c['node_a_label']} vs {c['node_b_label']}")

    # Detect trend in revenue
    trend = g.detect_trend(store["id"], window_days=30)
    print(f"Revenue trend: {trend['trend']} "
          f"(slope: {trend['slope_per_day']:.4f}/day)")

# Store manager resolves
with ohm.connect("retail.duckdb", actor="store_manager") as g:
    g.register_agent(values=["profitability", "customer_satisfaction"])

    # Challenge the original promotion plan
    promo_edges = g.query(filter_type="APPLIES_TO", layer="L3")
    for edge in promo_edges:
        if edge["to_node"] == store["id"]:
            g.challenge(
                edge["id"],
                reason="Competitor sale diluting promotion effectiveness",
                confidence=0.8,
            )

    # Revised strategy: shift promotion to next week
    revised_promo = g.create_node(
        label="Revised: 50% Off Cold Beverages (May 24)",
        node_type="plan",
        priority="P0",
    )
    g.create_edge(
        from_node=revised_promo["id"],
        to_node=store["id"],
        edge_type="APPLIES_TO",
        layer="L4",
        confidence=0.85,
    )
```

## Workflow 4: Urgency-Aware Change Feed

```python
with ohm.connect("retail.duckdb", actor="store_manager") as g:
    # Get only critical and high urgency changes
    urgent = g.urgent_changes(urgency_filter=["critical", "high"])
    for change in urgent:
        print(f"URGENT: {change['operation']} on {change['table_name']} "
              f"by {change['agent_name']}")

    # SSE equivalent (when connected to ohmd):
    # GET /events?urgency=critical,high&node_type=equipment
```

## Key Reasoning Primitives Used

| Primitive | Retail Use Case |
|-----------|----------------|
| `composite_score(method="geometric")` | Compound demand factors: temperature × day × promotion |
| `composite_score(temporal_decay_hours=4.0)` | Weight recent weather observations higher |
| `expiring_soon()` | Find inventory batches approaching expiry |
| `contradictions()` | Detect competitor sale vs predicted demand conflict |
| `detect_trend()` | Track revenue trajectory over 30-day window |
| `challenge()` | Challenge promotion plan when competitor undercuts |
| `escalate()` | Raise urgency on soon-to-expire batches |
| `urgent_changes()` | Filter change feed to critical/high only |
| `decay_observations()` | Preview time-weighted observation values |

## Domain-Specific Schema

```python
from ohm.schema import SchemaConfig

retail_config = SchemaConfig.retail()
# Adds node types: product, store, promotion, batch
# Adds edge types: BATCH_EXPIRES_BEFORE, COMPETITOR_RUNS_SALE, APPLIES_TO
# Validation policy: lenient (advisory for domain types)
```
