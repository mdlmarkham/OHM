# Beef Herd Management — OHM Scenario Architecture

## Overview

This document describes how OHM's multi-agent knowledge graph architecture models a beef cattle herd management system. Each domain expert is an "agent" that observes, challenges, and synthesizes decisions in a shared graph with confidence-weighted edges and temporal decay.

## Why Beef Herd Management?

Beef operations are a natural fit for OHM because:

1. **Multi-domain decisions**: Grazing, health, market timing, weather, breeding — each requires specialized knowledge
2. **Conflicting signals**: Weather agent says "rain coming" → challenge rotation; vet says "BCS declining" → challenge holding pattern
3. **Temporal sensitivity**: NDVI from 30 days ago is nearly worthless; market signals decay at different rates
4. **Cascading consequences**: Heifer retention decision today → market impact in 2-3 years
5. **Data silos**: GPS, satellite, RFID, weather, market data live in different vendor-locked systems
6. **AND-gate structure**: Biology enforces irreducible constraints (gestation, one calf/year)

## The Cattle Cycle AND-Gate

The ~10-year cattle cycle IS an AND→OR amplification system:

- **Expansion AND-gate**: Need high prices AND available forage AND water AND financial reserves AND 2-3 years
- **Contraction OR-gate**: Any one failure (drought, price crash, feed shortage) triggers destocking
- **Autocatalytic feedback**: Heifer retention → reduced supply → higher prices → more retention (positive feedback loop)
- **Irreducible time constraint**: Biology cannot be shortcut. 283-day gestation is the hard AND.

This maps directly to OHM's existing AND→OR conversion pattern framework.

## Agent Architecture

### Layer 2: Data Collection (L2 — Structure)

| Agent | Observes | OHM Methods | Edge Types |
|-------|----------|-------------|------------|
| **Halter** | GPS collar data, movement patterns, pasture occupancy | `observe()`, `create_edge(CITES)` | CITES, FLOWS_TO |
| **Vegetation** | NDVI satellite imagery, soil moisture, forage availability | `observe()`, `create_edge(FLOWS_TO)` | FLOWS_TO, PREDICTS |

### Layer 3: Knowledge & Reasoning (L3 — Knowledge)

| Agent | Observes | Decides | OHM Methods | Edge Types |
|-------|----------|---------|-------------|------------|
| **Veterinarian** | BCS, hoof scores, BRD symptoms, reproductive status | Treatment protocols, culling decisions | `confidence_chain()`, `create_edge(CAUSES)` | CAUSES, ORDERS_TEST, NEGATES |
| **Weather** | Forecast vs observed, precipitation, drought risk | Rotation challenges, heat stress alerts | `create_edge(PREDICTS)`, `contradictions()` | PREDICTS, THREATENS |
| **Market Analyst** | CME futures, USDA reports, input costs | Sell/hold timing, forward contracts | `composite_score()`, `detect_trend()` | PREDICTS, INFLUENCES |

### Layer 4: Prospect Synthesis (L4 — Prospects)

| Agent | Observes | Decides | OHM Methods | Edge Types |
|-------|----------|---------|-------------|------------|
| **Ranch Manager** | All L2/L3 data + financial statements | Rotation plans, destock/restock, capital allocation | `challenge()`, `composite_score()`, `contradictions()` | PLANS, SUPPORTS, CHALLENGED_BY |

## Workflow: Pasture Rotation Decision

### Step 1 — Halter observes cattle location

```python
from ohm_client import OHMClient

g = OHMClient(actor="halter", token="...")

# Create pasture nodes
north_pasture = g.create_node("North Pasture (40 acres)", node_type="area", priority="P1")
south_pasture = g.create_node("South Pasture (35 acres)", node_type="area", priority="P1")

# Observe cattle count per pasture
g.observe(north_pasture["id"], obs_type="head_count", value=85.0, sigma=2.0,
          metadata={"source": "collar_api", "timestamp": "2026-05-19T08:00:00Z"})
g.observe(south_pasture["id"], obs_type="head_count", value=42.0, sigma=1.5,
          metadata={"source": "collar_api", "timestamp": "2026-05-19T08:00:00Z"})
```

### Step 2 — Vegetation agent analyzes NDVI

```python
g = OHMClient(actor="vegetation", token="...")

# NDVI observations (0-1 scale)
g.observe(north_veg_id, obs_type="ndvi", value=0.32, sigma=0.05,
          metadata={"satellite": "sentinel-2", "date": "2026-05-17"})
g.observe(south_veg_id, obs_type="ndvi", value=0.78, sigma=0.05,
          metadata={"satellite": "sentinel-2", "date": "2026-05-17"})
```

### Step 3 — Veterinarian assesses health

```python
g = OHMClient(actor="veterinarian", token="...")

# Body condition score declining → CAUSES urgency
body_condition = g.create_node("Body Condition Score — Declining", node_type="concept",
                               content="BCS 4.5/9, declining trend over 30 days")
g.create_edge(from_node=body_condition["id"], to_node=north_pasture["id"],
              edge_type="CAUSES", layer="L3", confidence=0.75, urgency="high",
              content="Overstocking on declining forage")
```

### Step 4 — Weather challenges the rotation plan

```python
g = OHMClient(actor="weather", token="...")

rain_forecast = g.create_node("Heavy Rain Forecast — South Region (May 21-24)",
                              node_type="event",
                              content="60mm expected, flood risk for low-lying pastures")
g.create_edge(from_node=rain_forecast["id"], to_node=south_pasture["id"],
              edge_type="THREATENS", layer="L3", confidence=0.7, urgency="critical")

# Challenge the rotation plan
rotation_edge = g.search("rotation", node_type="plan")
for edge in rotation_edge:
    g.challenge(edge["id"], reason="Heavy rain forecast — rotation would cause mud damage",
                confidence=0.7)
```

### Step 5 — Ranch Manager synthesizes

```python
g = OHMClient(actor="ranch_manager", token="...")

# Composite scores
north_score = g.composite_score(north_pasture["id"],
                                observation_weight=0.4, evidence_weight=0.6)
south_score = g.composite_score(south_pasture["id"],
                                observation_weight=0.4, evidence_weight=0.6)

# Check contradictions
conflicts = g.contradictions()
for c in conflicts:
    print(f"CONTRADICTS: {c['node_a_label']} vs {c['node_b_label']}")

# Revised plan: delay rotation by 5 days (post-rain)
revised = g.create_node("Rotation Plan: North → South (May 25, post-rain)",
                        node_type="plan", priority="P1")
g.create_edge(from_node=revised["id"], to_node=south_pasture["id"],
              edge_type="PREDICTS", layer="L4", confidence=0.85)
```

## Key Reasoning Primitives

| Primitive | Beef Use Case |
|-----------|--------------|
| `confidence_chain()` | Trace evidence: hoof scores → BCS → pasture health → rotation decision |
| `contradictions()` | Surface: rotation plan vs weather forecast vs vet recommendation |
| `composite_score()` | Combine: head count + NDVI + BCS + market signal → pasture decision |
| `detect_trend()` | Track: NDVI decline over 60-day window |
| `decay_observations()` | Weight: recent NDVI higher than 30-day-old readings (7-day half-life) |
| `challenge()` | Weather agent challenges rotation plan with rain forecast |
| `expiring_soon()` | Cross-domain: feed inventory approaching expiry |

## AND-Gate Mapping

| Beef Operation Decision | AND Requirements | OR Escape Valves |
|------------------------|------------------|------------------|
| Heifer retention | Price + Forage + Water + Capital + 2-3 years | Forward contracts (price), supplemental feed (forage), insurance (capital) |
| Drought response | Water + Forage + Financial reserves | Buy feed (forage), drought insurance (financial), destock (reduce need) |
| PLF adoption | Trust + Affordability + Connectivity + ROI | Proof of ROI (trust), subsidy (affordability), satellite-based (connectivity) |
| Early BRD detection | Observation + Data + Vet + Timely intervention | Automated sensors (observation), alerts (data), telemedicine (vet) |
| Market timing | Price signal + Inventory data + Forward contracts | Hedging (forward contracts), diversification (inventory) |

## Implications for OHM Development

1. **Temporal decay is critical** — NDVI half-life ~7 days, market data half-life ~1 day, health observations ~14 days
2. **Challenge mechanism maps to real disagreements** — weather vs vet vs market is the core multi-agent dynamic
3. **Condition-based edges** — "IF BCS < 5 AND NDVI < 0.4 THEN challenge rotation" maps to condition field
4. **Urgency dimension** — Drought alerts are urgent; long-term trends are not
5. **Probability-weighted paths** — Weather forecasts, market predictions all have uncertainty
6. **Autocatalytic detection** — Cattle cycle amplification should be detectable as CAUSES loops
7. **Cross-domain synthesis** — Ranch manager (L4) must integrate all L2/L3 signals — exactly what composite_score() does

## U.S. Beef Industry Context (2024-2026)

- **Herd at 75-year low** (2023-2026): Drought forced massive liquidation
- **Rebuilding "spotty"** (Tyson, May 2026): Heifer retention inconsistent
- **Prices at record highs**: Boxed beef $300+/cwt, live cattle $180+/cwt
- **47% of producers plan expansion** (AgWeb survey): But biology constrains timeline
- **USDA rebuilding plan**: No direct payments, relying on market signals (AND-gate: no financial OR path)
- **Argentina beef imports floated**: Would convert domestic supply AND-gate to OR (alternative source)
- **Virtual fencing pilot projects**: Halter, Nofence gaining traction in Sheridan County WY
- **PLF adoption <30%**: Trust, cost, connectivity, ROI barriers all active AND-gates