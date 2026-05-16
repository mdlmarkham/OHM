# OHM Scenarios and Common Features

## Six Scenarios

### 1. TOPO — Geopolitical Intelligence
Multiple agents track a live situation. Each agent has different sources, different confidence, different values. Challenge edges model disagreement. Layer model: L1 events, L2 citations, L3 interpretations, L4 predictions. Observations accumulate, don't collapse.

### 2. Code Review Pipeline
Agent submits code → security agent audits → style agent critiques → Socrates challenges assumptions. Each review is an edge with confidence and layer. The code node accumulates perspectives without overwriting. **Needs:** temporal versioning (code changes over time, edges reference specific versions).

### 3. Trading System
Signal agent detects pattern → risk agent challenges position size → portfolio agent synthesizes. Each signal is an L4 prediction. Risk agent observes anomalies (sigma). **Needs:** time-series observations, prediction decay.

### 4. Research Synthesis
Fragments → atomic notes → pattern detection → deep research → challenge → synthesis. Multiple agents converge on patterns from different angles. **Needs:** source quality scoring, cross-reference to external sources.

### 5. Incident Response / Ops Monitoring
Alert → diagnose → propose fix → challenge if risky. Each agent has different risk tolerance and different data. **Needs:** urgency/priority as first-class attribute, escalation paths, real-time change feed.

### 6. Product Decision Framework
Market research → engineering estimate → design proposal → business challenge. Decision node shows convergence and divergence. **Needs:** decision state machine (proposed → reviewed → approved → implemented).

## Universal Features (bake into core)

These appear in every scenario and should be part of the OHM core:

1. **Challenge semantics** — Don't overwrite, add perspective. Already in.
2. **Attribution on every write** — Who wrote what, how confident, from what provenance. Already in.
3. **Confidence + observations** — value, baseline, sigma. Already in.
4. **Change feed** — Agents react to each other's writes. Already designed (`listen`).
5. **Layer ownership** — L1/L2 shared, L3/L4 agent-owned. Already in.
6. **Agent registration** — Each agent declares values, goals, capabilities as a first-class node. **Not yet implemented.**

## Variable Features (extensible, don't bake in)

These vary by scenario. The core should allow them without schema changes:

1. **Temporal versioning** — "this was true at time T." Observations with timestamps handle this, but some scenarios need explicit version edges.
2. **Decision state machines** — proposed → reviewed → approved → blocked. Edge types: PROPOSED_BY, REVIEWED_BY, APPROVED_BY, BLOCKED_BY. Already supported as custom edge types.
3. **Urgency/priority** — Node attribute or edge type. Not core, but schema should allow it.
4. **Time-series observations** — Stream of values. Graph holds the *claim*, not the data. External reference pattern.
5. **External references** — URLs, commit hashes, order IDs. Handled by `context` JSON on edges.
6. **Confidence aggregation** — Bayesian for research, weighted for trading, voting for decisions. Graph stores raw observations; consumers choose strategy. Already the design.

## Design Implications

The current schema is right. Every scenario needs the same core: challenge semantics, layers, confidence, attribution, observations, change feed. The variable features are edge types and node attributes — already extensible.

The one missing universal feature: **agent registration**. Agent identity should be a first-class node with edges describing values, goals, and capabilities. This enables:
- Agents to discover each other's optimization targets
- Challenge edges to reference agent values ("you value precision, I value connections")
- Synthesis agents to weight perspectives by agent reliability and domain expertise