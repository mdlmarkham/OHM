# Cybersecurity Incident Response Scenario

Complete agent workflow for SIEM + EDR + vulnerability scanner coordination using OHM's source reliability tracking, threat clustering, and urgency-aware change feed.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **SIEM** | Log aggregation, correlation rules | L2 | `create_edge(FLOWS_TO)`, `observe()` |
| **EDR** | Endpoint detection, process monitoring | L3 | `create_edge(THREATENS)`, `observe()` |
| **Vuln Scanner** | CVE detection, patch status | L2 | `create_edge(REFERENCES)`, `observe()` |
| **SOC Analyst** | Triage, investigation, escalation | L3 | `threat_cluster()`, `source_reliability()`, `escalate()` |
| **Incident Commander** | Containment decisions, state machine | L4 | `urgent_changes()`, `challenge()`, `composite_score()` |

## Workflow 1: Alert Triage with Source Reliability

### Step 1 — SIEM and EDR generate alerts

```python
import ohm.sdk as ohm

with ohm.connect("soc.duckdb", actor="siem") as g:
    g.register_agent(values=["detection", "correlation"])

    # Create alert nodes
    alert_1 = g.create_node(
        label="SIEM: Multiple failed logins from 203.0.113.42",
        node_type="event",
        urgency="high",
    )
    alert_2 = g.create_node(
        label="SIEM: Unusual outbound traffic to known C2 domain",
        node_type="event",
        urgency="critical",
    )

    # Create IOC node
    ioc_ip = g.create_node(
        label="IOC: 203.0.113.42 (threat intel: APT29 infrastructure)",
        node_type="concept",
    )

    # Link alerts to IOC
    g.create_edge(
        from_node=ioc_ip["id"],
        to_node=alert_1["id"],
        edge_type="THREAT_CLUSTER",
        layer="L3",
        confidence=0.9,
    )
    g.create_edge(
        from_node=ioc_ip["id"],
        to_node=alert_2["id"],
        edge_type="THREAT_CLUSTER",
        layer="L3",
        confidence=0.85,
    )

with ohm.connect("soc.duckdb", actor="edr") as g:
    g.register_agent(values=["endpoint", "behavior"])

    edr_alert = g.create_node(
        label="EDR: Suspicious PowerShell execution on workstation-17",
        node_type="event",
        urgency="critical",
    )

    g.create_edge(
        from_node=ioc_ip["id"],
        to_node=edr_alert["id"],
        edge_type="THREAT_CLUSTER",
        layer="L3",
        confidence=0.7,
    )
```

### Step 2 — SOC Analyst investigates threat cluster

```python
with ohm.connect("soc.duckdb", actor="soc_analyst") as g:
    g.register_agent(values=["investigation", "triage"])

    # Find all alerts sharing this IOC
    cluster = g.threat_cluster(ioc_ip["id"])
    print(f"Threat cluster size: {len(cluster)} alerts")
    for alert in cluster:
        print(f"  - {alert['label']}")

    # Check source reliability
    siem_reliability = g.source_reliability("siem")
    edr_reliability = g.source_reliability("edr")

    print(f"SIEM reliability: P(accurate)={siem_reliability['p_accurate']:.2f}, "
          f"FPR={siem_reliability['false_positive_rate']:.2f}, "
          f"n={siem_reliability['total_claims']}")
    print(f"EDR reliability:  P(accurate)={edr_reliability['p_accurate']:.2f}, "
          f"FPR={edr_reliability['false_positive_rate']:.2f}, "
          f"n={edr_reliability['total_claims']}")

    # Escalate if multiple independent sources agree
    if cluster and len(cluster) >= 2:
        for alert in cluster:
            g.escalate(alert["id"], "critical")
        print("ESCALATED: Multiple correlated alerts — possible incident")
```

### Step 3 — Record outcomes to calibrate reliability

```python
with ohm.connect("soc.duckdb", actor="soc_analyst") as g:
    # After investigation: SIEM alert was correct (true positive)
    g.record_outcome(
        source_agent="siem",
        claim_node=alert_1["id"],
        outcome=True,
    )

    # EDR alert was a false positive (legitimate admin script)
    g.record_outcome(
        source_agent="edr",
        claim_node=edr_alert["id"],
        outcome=False,
    )

    # Re-check reliability after recording outcomes
    edr_updated = g.source_reliability("edr")
    print(f"EDR updated: P(accurate)={edr_updated['p_accurate']:.2f}, "
          f"FPR={edr_updated['false_positive_rate']:.2f}")
```

## Workflow 2: Incident State Machine

```python
with ohm.connect("soc.duckdb", actor="incident_commander") as g:
    g.register_agent(values=["containment", "recovery"])

    # Create incident node
    incident = g.create_node(
        label="INC-2026-0042: APT29 suspected intrusion",
        node_type="event",
        priority="P0",
    )

    # State: OPEN → INVESTIGATING
    investigation = g.create_node(
        label="Investigation: Workstation-17 forensic analysis",
        node_type="plan",
    )
    g.create_edge(
        from_node=investigation["id"],
        to_node=incident["id"],
        edge_type="INVESTIGATED_BY",
        layer="L4",
        confidence=0.9,
        urgency="critical",
    )

    # State: INVESTIGATING → CONTAINED
    containment = g.create_node(
        label="Containment: Isolate workstation-17, block 203.0.113.0/24",
        node_type="plan",
    )
    g.create_edge(
        from_node=containment["id"],
        to_node=incident["id"],
        edge_type="CONTAINED_BY",
        layer="L4",
        confidence=0.95,
    )

    # State: CONTAINED → CLOSED (after eradication + recovery)
    closure = g.create_node(
        label="Closure: IOC removed, patch applied, monitoring active",
        node_type="plan",
    )
    g.create_edge(
        from_node=closure["id"],
        to_node=incident["id"],
        edge_type="CLOSED_BY",
        layer="L4",
        confidence=0.9,
    )
```

## Workflow 3: Urgency-Aware Monitoring

```python
with ohm.connect("soc.duckdb", actor="incident_commander") as g:
    # Monitor only critical changes
    urgent = g.urgent_changes(urgency_filter=["critical"])
    for change in urgent:
        print(f"CRITICAL: {change['operation']} on {change['table_name']} "
              f"by {change['agent_name']}")

    # SSE equivalent (when connected to ohmd):
    # GET /events?urgency=critical&node_type=event
    # Batched at 50 events/write for high-throughput incident response

    # Composite score for incident severity
    score = g.composite_score(
        incident["id"],
        method="geometric",
        observation_weight=0.3,
        evidence_weight=0.7,
    )
    print(f"Incident severity composite: {score['composite_score']:.2f}")
```

## Key Reasoning Primitives Used

| Primitive | Cybersecurity Use Case |
|-----------|----------------------|
| `threat_cluster()` | Find all alerts sharing an IOC (IP, domain, hash) |
| `source_reliability()` | Compute P(accurate) and FPR per detection source |
| `record_outcome()` | Build reliability history from true/false positive feedback |
| `escalate()` | Raise urgency when multiple sources corroborate |
| `urgent_changes()` | Filter change feed to critical/high only |
| `composite_score()` | Combine alert severity + source reliability into incident score |
| `challenge()` | Challenge containment strategy if risky |
| SSE batching | 50 events/write for high-velocity incident response |
