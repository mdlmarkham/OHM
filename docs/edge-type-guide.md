# OHM Edge Type Guide

Domain designers use edge types to express relationships between nodes. This guide covers all 30+ edge types across L1-L4 and explains when to use each.

## Layer Overview

| Layer | Purpose | Edge Count |
|-------|---------|------------|
| L1 | Identity & membership | 8 edge types |
| L2 | Causal/relational | 16+ edge types |
| L3 | Evidence & challenges | 13+ edge types |
| L4 | Intentional/planning | 8+ edge types |

---

## L1 — Identity & Membership

These edges express what a node **is** or **belongs to**. They are typically static and don't change often.

### CONTAINS / BELONGS_TO
Expresses container-contained relationships.
```
Equipment —CONTAINS→ Sensor
Warehouse —CONTAINS→ InventoryBatch
```
Use when: A node is physically or logically part of another node.

### HAS_COMPONENT / PART_OF
Expresses composition relationships (the inverse of CONTAINS).
```
Tractor —HAS_COMPONENT→ Engine
Engine —PART_OF→ Tractor
```
Use when: A node is made of smaller components.

### CAPABLE_OF
Expresses capability or function.
```
Robot —CAPABLE_OF→ Navigation
Software —CAPABLE_OF→ DataProcessing
```
Use when: A node has the ability to perform a certain function.

### VALUES
Expresses what a node considers important.
```
Agent —VALUES→ Truth
Organization —VALUES→ Profit
```
Use when: Attributing values or priorities to an agent or organization.

### GOALS
Expresses what a node is trying to achieve.
```
Project —GOALS→ CostReduction
Agent —GOALS→ Efficiency
```
Use when: A node has goals it is working toward.

### INTERESTED_IN
Expresses interest or attention focus.
```
Researcher —INTERESTED_IN→ ClimateData
Analyst —INTERESTED_IN→ MarketTrends
```
Use when: An agent is monitoring or关注ing something.

---

## L2 — Causal & Relational

These edges express causal relationships, flows, and influences between nodes.

### DERIVES_FROM
Expresses that one node is derived from another.
```
Insight —DERIVES_FROM→ RawData
Model —DERIVES_FROM→ TrainingData
```
Use when: A node is produced/generated from source material.

### INFLUENCES
Expresses that one node affects another.
```
Weather —INFLUENCES→ CropYield
Price —INFLUENCES→ Demand
```
Use when: Changes in one node propagate to another.

### REFERENCES
Expresses citation or mention relationship.
```
Paper —REFERENCES→ PreviousWork
Report —REFERENCES→ DataSource
```
Use when: One node cites or references another.

### USES
Expresses tool or resource usage.
```
Process —USES→ Algorithm
Worker —USES→ Tool
```
Use when: A node consumes or applies a resource/tool.

### FEEDS / FLOWS_TO
Expresses flow or data transfer.
```
Sensor —FEEDS→ DataPipeline
DataPipeline —FLOWS_TO→ Analytics
```
Use when: Material, data, or resources flow between nodes.

### NOTIFIES
Expresses notification or alert relationship.
```
Monitor —NOTIFIES→ Operator
AlertSystem —NOTIFIES→ OnCallEngineer
```
Use when: One node generates notifications for another.

### TRUSTS
Expresses trust relationship.
```
ServiceA —TRUSTS→ ServiceB
Agent —TRUSTS→ DataSource
```
Use when: One node trusts the outputs or behavior of another.

### SERVES
Expresses service relationship.
```
API —SERVES→ ClientApp
Worker —SERVES→ Customer
```
Use when: One node provides service to another.

### BATCH_EXPIRES_BEFORE
**Cattle/Retail**: Expresses inventory expiry.
```
InventoryBatch —BATCH_EXPIRES_BEFORE→ ExpiryDate
```
Use when: Tracking when perishable goods expire.

### TRANSFERRED_TO
**Customer Support**: Expresses handoff between agents.
```
Ticket —TRANSFERRED_TO→ AgentB
Case —TRANSFERRED_TO→ SpecializedTeam
```
Use when: Work is transferred between agents or queues.

### OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY, CLOSED_BY
**Customer Support/Incident**: State machine edges.
```
Ticket —OPENED_BY→ Customer
Incident —STARTED_BY→ Analyst
SupportCase —AWAITING→ CustomerResponse
Issue —RESOLVED_BY→ Engineer
Case —CLOSED_BY→ Manager
```
Use when: Tracking workflow state transitions.

### INVESTIGATED_BY, CONTAINED_BY, ERADICATED_BY, RECOVERED_BY
**Incident Response**: Incident lifecycle edges.
```
Incident —INVESTIGATED_BY→ Analyst
Incident —CONTAINED_BY→ Engineer
Incident —ERADICATED_BY→ Engineer
Incident —RECOVERED_BY→ OpsTeam
```
Use when: Tracking incident response phases.

### NEGOTIATES_WITH
**SLAs/Commitments**: Negotiation relationships.
```
Vendor —NEGOTIATES_WITH→ Customer
Contract —NEGOTIATES_WITH→ Agreement
```
Use when: Two parties are negotiating terms.

---

## L3 — Evidence & Challenges

These edges express evidence, challenges, and relationships that affect confidence.

### CAUSES
Expresses causal relationship.
```
Smoking —CAUSES→ LungCancer
WarmTemperature —CAUSES→ AlgalBloom
```
Use when: One node directly causes another.

### CORRELATES_WITH
Expresses statistical correlation.
```
ScreenTime —CORRELATES_WITH→ Myopia
Exercise —CORRELATES_WITH→ HealthScore
```
Use when: Two nodes are statistically correlated (not necessarily causal).

### PREDICTS
Expresses prediction or forecast.
```
Model —PREDICTS→ Outcome
Indicator —PREDICTS→ Event
```
Use when: One node forecasts another.

### EXPLAINS
Expresses explanatory relationship.
```
Theory —EXPLAINS→ Observation
Model —EXPLAINS→ Data
```
Use when: One node provides explanation for another.

### CHALLENGED_BY
Expresses that a claim is being challenged.
```
Claim —CHALLENGED_BY→ CounterEvidence
Theory —CHALLENGED_BY→ ContradictingData
```
Use when: An edge or node is being challenged by another agent.

### SUPPORTS
Expresses supporting evidence.
```
Evidence —SUPPORTS→ Claim
Data —SUPPORTS→ Hypothesis
```
Use when: One node provides support for another.

### REFINES
Expresses refinement or improvement.
```
Version2 —REFINES→ Version1
DetailedModel —REFINES→ SimplifiedModel
```
Use when: One node refines another.

### CONTRADICTS
Expresses direct contradiction.
```
FindingA —CONTRADICTS→ FindingB
Claim —CONTRADICTS→ ObservedData
```
Use when: Two nodes directly contradict each other.

### LISTENS_TO
Expresses that an agent listens to or monitors a source.
```
Agent —LISTENS_TO→ SensorFeed
MonitoringAgent —LISTENS_TO→ AlertChannel
```
Use when: An agent is subscribed to or monitoring a source.

### DEFERS_TO
Expresses that one node defers to another's authority.
```
JuniorAgent —DEFERS_TO→ SeniorAgent
LocalModel —DEFERS_TO→ CentralAuthority
```
Use when: One node respects another's judgment.

### COLLABORATES_WITH
Expresses collaboration.
```
TeamA —COLLABORATES_WITH→ TeamB
Researcher —COLLABORATES_WITH→ Analyst
```
Use when: Multiple agents are working together.

### APPLIES_TO
Expresses application or scope.
```
Policy —APPLIES_TO→ Region
Rule —APPLIES_TO→ UseCase
```
Use when: A node applies to or covers a scope.

### RELATED_TO
General-purpose related edge.
```
TopicA —RELATED_TO→ TopicB
```
Use when: Nodes are related but the relationship doesn't fit other types.

### NEGATES
**Medical**: Rules out a diagnosis or condition.
```
FeverAbsent —NEGATES→ Malaria
NegativeTest —NEGATES→ Infection
```
Use when: Absence of a finding rules out a diagnosis.

### EXPECTED_LIKELIHOOD
**Supply Chain**: Probability claim.
```
Forecast —EXPECTED_LIKELIHOOD→ DemandSpike
HistoricalData —EXPECTED_LIKELIHOOD→ Stockout
```
Use when: Expressing probability of an outcome.

### ESCALATED_TO
**Support**: Escalation path.
```
Ticket —ESCALATED_TO→ Manager
Issue —ESCALATED_TO→ Executive
```
Use when: A case is escalated to higher authority.

### DELEGATED_TO
**Support**: Delegation edge.
```
Task —DELEGATED_TO→ Contractor
Assignment —DELEGATED_TO→ Specialist
```
Use when: Work is delegated to another party.

### THREAT_CLUSTER
**Cybersecurity**: IOC linkage.
```
MaliciousIP —THREAT_CLUSTER→ Alert1
MaliciousDomain —THREAT_CLUSTER→ Alert2
```
Use when: An IOC (Indicator of Compromise) is linked to security alerts.

---

## L4 — Intentional & Planning

These edges express intentions, plans, risks, and expectations.

### EXPECTS
Expresses expectation or prediction.
```
Forecast —EXPERTS→ DemandIncrease
Analyst —EXPERTS→ RevenueGrowth
```
Use when: A node anticipates or expects something.

### PLANS
Expresses planning relationship.
```
Project —PLANS→ Milestone
Strategy —PLANS→ Initiative
```
Use when: A node is planning something.

### RISKS
Expresses risk relationship.
```
Decision —RISKS→ NegativeOutcome
Action —RISKS→ SideEffect
```
Use when: A node risks causing something negative.

### DEPENDS_ON
Expresses dependency.
```
Task —DEPENDS_ON→ Resource
Deliverable —DEPENDS_ON→ Input
```
Use when: One node depends on another to complete.

### THREATENS
Expresses threat relationship.
```
Competitor —THREATENS→ MarketShare
Vulnerability —THREATENS→ System
```
Use when: One node threatens another.

### ENABLES
Expresses enabling relationship.
```
Technology —ENABLES→ Capability
Feature —ENABLES→ UserGoal
```
Use when: One node enables another.

### EXPECTS_FROM
Expresses expectation from a source.
```
Manager —EXPECTS_FROM→ Employee→ Deliverable
Customer —EXPECTS_FROM→ Vendor→ Service
```
Use when: One node expects something from another.

### PREDICTS (L4)
Used in L4 for predictions with intentional framing.
```
ProjectPlan —PREDICTS→ Success
Investment —PREDICTS→ ROI
```
Use when: Expressing forward-looking predictions in planning context.

### ORDERS_TEST
**Medical**: Diagnostic test ordering.
```
DiagnosticRule —ORDERS_TEST→ BloodPanel
ClinicalGuideline —ORDERS_TEST→ ImagingStudy
```
Use when: A finding or guideline orders a diagnostic test.

### TRIGGERS_INCIDENT
**Cybersecurity**: Finding triggers incident.
```
IOC —TRIGGERS_INCIDENT→ SecurityIncident
Alert —TRIGGERS_INCIDENT→ Incident
```
Use when: An indicator or finding triggers a security incident.

---

## Decision Tree: Which Edge Type to Use?

```
Is the relationship about identity/membership?
├── YES → CONTAINS/BELONGS_TO, HAS_COMPONENT/PART_OF, CAPABLE_OF
└── NO
  Is it causal or relational (flow, influence)?
  ├── YES → DERIVES_FROM, INFLUENCES, FEEDS/FLOWS_TO, USES
  └── NO
    Is it evidence or challenges?
    ├── YES → CAUSES, CORRELATES_WITH, PREDICTS, SUPPORTS, CHALLENGED_BY
    └── NO
      Is it intentional/planning?
      ├── YES → EXPECTS, PLANS, RISKS, DEPENDS_ON, THREATENS
      └── NO
        Is it domain-specific?
        ├── Check domain section below
        └── Use RELATED_TO (fallback)
```

---

## Domain Extension Pattern

Add custom edge types via `SchemaConfig` (ADR-007):

```python
from ohm.schema import SchemaConfig

# Create custom schema with domain-specific edges
custom_schema = SchemaConfig(
    node_types_by_layer={
        "L1": frozenset({"Sensor", "Device", "Gateway"}),
        "L3": frozenset({"Reading", "Alert", "Command"}),
    },
    layer_edge_types={
        "L2": frozenset({"COLLECTS_FROM", "REPORTS_TO", "ALERTS"}),
        "L3": frozenset({"EXCEEDS_THRESHOLD", "COMMANDS"}),
    },
)
```

---

## Cattle Domain Edge Types

| Edge Type | Layer | Purpose |
|-----------|-------|---------|
| GRAZES_ON | L2 | Cattle grazes on pasture |
| ROTATION_SCHEDULES | L2 | Pasture rotation scheduling |
| HERD_AFFECTED_BY | L2 | Environmental factor affects herd |
| MONITORS | L2 | Sensor monitors cattle |
| ALERTS | L3 | Anomaly detected in herd data |

## Retail Domain Edge Types

| Edge Type | Layer | Purpose |
|-----------|-------|---------|
| BATCH_EXPIRES_BEFORE | L2 | Inventory batch expiry date |
| COMPETITOR_RUNS_SALE | L3 | Competitor promotion activity |
| EMPLOYEE_COVERED_BY | L2 | Employee shift coverage |
| LOCATED_AT | L1 | Inventory located at location |
| RESTOCKED_FROM | L2 | Inventory restocked from supplier |

---

## Quick Reference

| Category | Edge Types |
|----------|-----------|
| Identity | CONTAINS, BELONGS_TO, HAS_COMPONENT, PART_OF, CAPABLE_OF, VALUES, GOALS, INTERESTED_IN |
| Flow/Relational | FEEDS, FLOWS_TO, DERIVES_FROM, INFLUENCES, USES, REFERENCES |
| Evidence | CAUSES, PREDICTS, CORRELATES_WITH, SUPPORTS, CHALLENGED_BY, CONTRADICTS |
| Collaboration | LISTENS_TO, DEFERS_TO, COLLABORATES_WITH, TRUSTS |
| State Machine | OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY, CLOSED_BY |
| Incident | INVESTIGATED_BY, CONTAINED_BY, ERADICATED_BY, RECOVERED_BY |