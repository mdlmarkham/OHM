# Cattle Operations Scenario

Complete agent workflow for herd health monitoring, grazing optimization, and feed management using OHM's reasoning primitives.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **Halter** | Location tracking via GPS collars | L2 | `create_edge(CITES)`, `observe()` |
| **Vegetation** | NDVI satellite imagery analysis | L2 | `create_edge(FLOWS_TO)`, `observe()` |
| **Veterinarian** | Health assessments, hoof scores | L3 | `confidence_chain()`, `create_edge(CAUSES)` |
| **Ranch Manager** | Rotation planning, decision synthesis | L4 | `challenge()`, `composite_score()`, `contradictions()` |
| **Weather** | Forecast vs observed conditions | L3 | `create_edge(PREDICTS)`, `contradictions()` |

## Workflow 1: Pasture Rotation Decision

### Step 1 — Halter agent observes cattle location

```python
import ohm.sdk as ohm

with ohm.connect("ranch.duckdb", actor="halter") as g:
    # Register agent with domain values
    g.register_agent(values=["grazing", "animal_welfare"])

    # Create pasture nodes
    north_pasture = g.create_node(
        label="North Pasture (40 acres)",
        node_type="area",
        priority="P1",
    )
    south_pasture = g.create_node(
        label="South Pasture (35 acres)",
        node_type="area",
        priority="P1",
    )

    # Observe cattle count per pasture (from GPS collar data)
    g.observe(
        north_pasture["id"],
        obs_type="head_count",
        value=85.0,
        sigma=2.0,
        metadata={"source": "collar_api", "timestamp": "2026-05-17T08:00:00Z"},
    )
    g.observe(
        south_pasture["id"],
        obs_type="head_count",
        value=42.0,
        sigma=1.5,
        metadata={"source": "collar_api", "timestamp": "2026-05-17T08:00:00Z"},
    )

    # Link collar data to pasture nodes
    collar_data = g.create_node(
        label="Collar API Feed",
        node_type="source",
        provenance="feed-ingest",
    )
    g.create_edge(
        from_node=collar_data["id"],
        to_node=north_pasture["id"],
        edge_type="CITES",
        layer="L2",
        confidence=0.95,
    )
```

### Step 2 — Vegetation agent analyzes NDVI

```python
with ohm.connect("ranch.duckdb", actor="vegetation") as g:
    g.register_agent(values=["forage", "sustainability"])

    # Create vegetation health nodes
    north_veg = g.create_node(
        label="North Pasture Vegetation Health",
        node_type="concept",
    )
    south_veg = g.create_node(
        label="South Pasture Vegetation Health",
        node_type="concept",
    )

    # NDVI observations (0-1 scale, higher = healthier)
    g.observe(
        north_veg["id"],
        obs_type="ndvi",
        value=0.32,  # Low — overgrazed
        sigma=0.05,
        metadata={"satellite": "sentinel-2", "date": "2026-05-15"},
    )
    g.observe(
        south_veg["id"],
        obs_type="ndvi",
        value=0.78,  # High — recovered
        sigma=0.05,
        metadata={"satellite": "sentinel-2", "date": "2026-05-15"},
    )

    # Link vegetation to pastures
    g.create_edge(
        from_node=north_veg["id"],
        to_node=north_pasture["id"],
        edge_type="FLOWS_TO",
        layer="L2",
        confidence=0.9,
    )
    g.create_edge(
        from_node=south_veg["id"],
        to_node=south_pasture["id"],
        edge_type="FLOWS_TO",
        layer="L2",
        confidence=0.9,
    )
```

### Step 3 — Veterinarian agent assesses health

```python
with ohm.connect("ranch.duckdb", actor="veterinarian") as g:
    g.register_agent(values=["animal_health", "welfare"])

    # Create health assertion nodes
    hoof_health = g.create_node(
        label="Herd Hoof Health — Acceptable",
        node_type="concept",
    )
    body_condition = g.create_node(
        label="Body Condition Score — Declining",
        node_type="concept",
    )

    # Observations from recent check
    g.observe(
        hoof_health["id"],
        obs_type="hoof_score",
        value=0.85,  # Good
        sigma=0.1,
    )
    g.observe(
        body_condition["id"],
        obs_type="bcs",
        value=0.45,  # Below target
        sigma=0.08,
    )

    # Link health to north pasture (where most cattle are)
    g.create_edge(
        from_node=body_condition["id"],
        to_node=north_pasture["id"],
        edge_type="CAUSES",
        layer="L3",
        confidence=0.75,
        urgency="high",
    )

    # Trace evidence chain for body condition
    chain = g.confidence_chain(body_condition["id"])
    print(f"Evidence depth: {chain['max_depth']}, "
          f"Aggregate confidence: {chain['aggregate_confidence']}")
```

### Step 4 — Ranch Manager proposes rotation plan

```python
with ohm.connect("ranch.duckdb", actor="ranch_manager") as g:
    g.register_agent(values=["productivity", "sustainability", "animal_welfare"])

    # Compute composite scores for each pasture
    north_score = g.composite_score(
        north_pasture["id"],
        observation_weight=0.4,
        evidence_weight=0.6,
    )
    south_score = g.composite_score(
        south_pasture["id"],
        observation_weight=0.4,
        evidence_weight=0.6,
    )

    print(f"North pasture composite: {north_score['composite_score']}")
    print(f"South pasture composite: {south_score['composite_score']}")

    # Detect trends in NDVI
    north_trend = g.detect_trend(north_veg["id"], window_days=60)
    print(f"North NDVI trend: {north_trend['trend']} "
          f"(slope: {north_trend['slope_per_day']})")

    # Propose rotation: move herd from north to south
    rotation_plan = g.create_node(
        label="Rotation Plan: North → South (May 20)",
        node_type="plan",
        priority="P1",
    )

    g.create_edge(
        from_node=rotation_plan["id"],
        to_node=south_pasture["id"],
        edge_type="PREDICTS",
        layer="L4",
        confidence=0.8,
        condition="IF body_condition continues declining AND south NDVI > 0.7",
    )
```

### Step 5 — Challenge and contradiction resolution

```python
with ohm.connect("ranch.duckdb", actor="weather") as g:
    g.register_agent(values=["forecast", "risk"])

    # Weather agent challenges the rotation plan
    # (forecast shows heavy rain in south pasture next week)
    rain_forecast = g.create_node(
        label="Heavy Rain Forecast — South Region (May 19-22)",
        node_type="event",
    )

    g.create_edge(
        from_node=rain_forecast["id"],
        to_node=south_pasture["id"],
        edge_type="PREDICTS",
        layer="L3",
        confidence=0.7,
        urgency="critical",
    )

    # Challenge the rotation plan
    rotation_edges = g.query(filter_type="PREDICTS", layer="L4")
    for edge in rotation_edges:
        if edge["to_node"] == south_pasture["id"]:
            g.challenge(
                edge["id"],
                reason="Heavy rain forecast — rotation would cause mud damage",
                confidence=0.7,
            )

# Ranch manager checks for contradictions
with ohm.connect("ranch.duckdb", actor="ranch_manager") as g:
    conflicts = g.contradictions()
    for c in conflicts:
        print(f"CONTRADICTS: {c['node_a_label']} vs {c['node_b_label']} "
              f"(confidence: {c['contradiction_confidence']})")

    # Revised plan: delay rotation by 5 days
    revised_plan = g.create_node(
        label="Rotation Plan: North → South (May 25, post-rain)",
        node_type="plan",
        priority="P1",
    )
    g.create_edge(
        from_node=revised_plan["id"],
        to_node=south_pasture["id"],
        edge_type="PREDICTS",
        layer="L4",
        confidence=0.85,
    )
```

## Workflow 2: Temporal Confidence Decay for NDVI

NDVI observations are time-sensitive — a reading from 30 days ago is less relevant than one from 3 days ago.

```python
with ohm.connect("ranch.duckdb", actor="vegetation") as g:
    g.register_agent(values=["forage"])

    # Apply temporal decay with 7-day half-life (cattle-appropriate)
    decayed = g.decay_observations(
        north_veg["id"],
        temporal_decay_hours=168.0,  # 7 days
        dry_run=True,  # Preview without modifying
    )
    for d in decayed:
        print(f"Obs {d['id']}: {d['original_value']:.2f} → "
              f"{d['decayed_value']:.2f} "
              f"(age: {d['age_hours']:.1f}h, decay: {d['decay_factor']:.3f})")

    # Use decay in composite scoring
    score = g.composite_score(
        north_pasture["id"],
        method="geometric",
        temporal_decay_hours=168.0,
    )
    print(f"Time-weighted composite: {score['composite_score']}")
```

## Workflow 3: Multi-Domain Agent (Cattle + Retail)

One agent can work across domains using `SchemaConfig` set union:

```python
from ohm.schema import SchemaConfig

# Combined ontology for an agent that manages both ranch and farm store
combined = SchemaConfig.cattle() | SchemaConfig.retail()

with ohm.connect("multi_domain.duckdb", actor="operations_manager") as g:
    g.register_agent(values=["efficiency", "profitability"])

    # Cattle domain: check herd health
    herd = g.create_node(label="Herd A", node_type="system")
    g.observe(herd["id"], obs_type="weight_gain", value=2.3, sigma=0.2)

    # Retail domain: check inventory
    feed_inventory = g.create_node(label="Feed Inventory", node_type="equipment")
    g.create_edge(
        from_node=feed_inventory["id"],
        to_node=herd["id"],
        edge_type="FEEDS",
        layer="L2",
    )

    # Check for expiring feed batches
    expiring = g.expiring_soon(product_type="equipment", days=7)
    for batch in expiring:
        print(f"EXPIRING: {batch['batch_label']} in "
              f"{batch['days_until_expiry']} days")

    # Composite view across domains
    status = g.status()
    print(f"Multi-domain graph: {status['total_nodes']} nodes, "
          f"{status['total_edges']} edges")
```

## Key Reasoning Primitives Used

| Primitive | Cattle Use Case |
|-----------|----------------|
| `confidence_chain()` | Trace evidence from hoof scores → body condition → pasture health |
| `contradictions()` | Surface conflicts between rotation plan and weather forecast |
| `composite_score()` | Combine head count + NDVI + health into pasture decision score |
| `detect_trend()` | Track NDVI decline over 60-day window |
| `decay_observations()` | Weight recent NDVI readings higher (7-day half-life) |
| `challenge()` | Weather agent challenges rotation plan with rain forecast |
| `expiring_soon()` | Cross-domain: check feed inventory expiry |

## Domain-Specific Schema

```python
from ohm.schema import SchemaConfig

cattle_config = SchemaConfig.cattle()
# Adds node types: herd, pasture, treatment, breed
# Adds edge types: GRAZES_ON, TREATED_WITH, BRED_FROM
# Validation policy: lenient (advisory for domain types)
```
