# OHM Scenarios and Common Features

## Ten Scenarios

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

### 7. Cattle Operations (OHM-0e0)
Herd health monitoring, grazing optimization, feed management. Agent roles: herd monitor, feed optimizer, breeding analyst. **Needs:** multiplicative composite scoring for demand forecasting, temporal observation decay, batch expiry tracking.

### 8. Retail (OHM-0e0)
Inventory management, demand forecasting, supplier coordination. Agent roles: demand forecaster, inventory optimizer, supplier negotiator. **Needs:** BATCH_EXPIRES_BEFORE edges for inventory expiry, composite scoring, SSE filtering by node_type.

### 9. Medical Diagnosis (OHM-af8.3)
Multiple diagnostic agents, differential diagnosis, negative evidence. **Needs:** NEGATES edge type (rules out diagnoses), compound_confidence() with correlation parameter, differential_diagnosis() method.

### 10. Cybersecurity Incident Response (OHM-af8.4)
SIEM + EDR + vulnerability scanner. **Needs:** source_reliability() and record_outcome() for per-agent accuracy tracking, threat_cluster() for IOC correlation, urgency filtering, batch SSE writes.

### 11. Supply Chain Disruption (OHM-af8.1)
Multi-tier BOM, probability-weighted edges, cascade simulation. **Needs:** probability field on edges (distinct from confidence), cascade_scenario() Monte Carlo, what_if() dry-run impact analysis.

### 12. Customer Support (OHM-af8.5)
Triage priority, handoff chains, resolution state machine. **Needs:** priority on nodes, urgency on edges, handoff() and escalate() SDK methods, sentiment observation type, resolution state machine edge types.

## Universal Features (in core)

These appear in every scenario and are part of the OHM core:

1. **Challenge semantics** — Don't overwrite, add perspective. ✅
2. **Attribution on every write** — Who wrote what, how confident, from what provenance. ✅
3. **Confidence + observations** — value, baseline, sigma. ✅
4. **Change feed** — Agents react to each other's writes. ✅ (`listen()`)
5. **Layer ownership** — L1/L2 shared, L3/L4 agent-owned. ✅
6. **Urgency and priority** — First-class attributes on edges and nodes. ✅ (v0.5.0)
7. **Probability** — Distinct from confidence, for risk modeling. ✅ (v0.5.0)
8. **NEGATES** — Negative evidence for diagnosis and rules-out reasoning. ✅ (v0.5.0)

## Variable Features (extensible, don't bake in)

These vary by scenario. The core should allow them without schema changes:

1. **Temporal versioning** — "this was true at time T." Observations with timestamps handle this, but some scenarios need explicit version edges.
2. **Decision state machines** — proposed → reviewed → approved → blocked. Edge types: PROPOSED_BY, REVIEWED_BY, APPROVED_BY, BLOCKED_BY. Already supported as custom edge types.
3. **Source reliability calibration** — Per-agent accuracy tracking. `record_outcome()` and `source_reliability()`. (SDK method, not schema)
4. **Correlated confidence compounding** — `compound_confidence()` with correlation parameter. Independent findings compound more; correlated findings compound less. (SDK method)
5. **Cascade simulation** — `cascade_scenario()` and `what_if()` for supply chain and risk modeling. (SDK method, uses probability field)
6. **Handoff chains** — `handoff()` and `escalate()` for customer support workflows. (SDK method, uses TRANSFERRED_TO/ESCALATED_TO edges)
7. **External references** — URLs, commit hashes, order IDs. Handled by `context` JSON on edges. ✅

## Design Implications

The current schema is right. Every scenario needs the same core: challenge semantics, layers, confidence, attribution, observations, change feed, urgency, probability. The variable features are edge types, SDK methods, and observation patterns — all extensible without core changes.

The key additions since v0.4.0 are urgency (edges), priority (nodes), and probability (edges) as first-class schema fields, plus NEGATES and scenario-specific edge types. These enable medical diagnosis, cybersecurity incident response, supply chain modeling, and customer support without changing the core architecture.