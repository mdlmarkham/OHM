# Customer Support Scenario

Complete agent workflow for triage, handoff chains, sentiment observation, and resolution state machine using OHM's priority, urgency, and escalation primitives.

## Agent Lineup

| Agent | Role | Layer | Key Methods |
|-------|------|-------|-------------|
| **Ticket Router** | Auto-classification, priority assignment | L2 | `create_node(priority=)`, `observe()` |
| **Triage Agent** | Initial response, sentiment analysis | L3 | `observe(obs_type="sentiment")`, `escalate()` |
| **Support Engineer** | Technical investigation, resolution | L3 | `create_edge(RESOLVED_BY)`, `confidence_chain()` |
| **Escalation Manager** | Cross-team handoffs, SLA tracking | L4 | `handoff()`, `escalate()`, `urgent_changes()` |
| **Customer Success** | Relationship management, churn risk | L4 | `detect_trend()`, `composite_score()` |

## Workflow 1: Ticket Triage and Priority Assignment

### Step 1 — Ticket Router classifies incoming ticket

```python
import ohm.sdk as ohm

with ohm.connect("support.duckdb", actor="ticket_router") as g:
    g.register_agent(values=["efficiency", "customer_satisfaction"])

    # Create ticket node with auto-assigned priority
    ticket = g.create_node(
        label="TICKET-8842: Payment gateway timeout — enterprise customer",
        node_type="event",
        priority="P0",  # Enterprise + payment = critical
    )

    # Observe classification signals
    g.observe(
        ticket["id"],
        obs_type="customer_tier",
        value=1.0,  # Enterprise (highest tier)
        sigma=0.0,
    )
    g.observe(
        ticket["id"],
        obs_type="impact",
        value=0.95,  # Payment blocked = high impact
        sigma=0.05,
    )
    g.observe(
        ticket["id"],
        obs_type="sentiment",
        value=0.15,  # Very negative sentiment
        sigma=0.1,
    )

    # Route to payment engineering team
    payment_team = g.create_node(
        label="Payment Engineering Team",
        node_type="system",
    )
    g.create_edge(
        from_node=ticket["id"],
        to_node=payment_team["id"],
        edge_type="DELEGATED_TO",
        layer="L2",
        urgency="critical",
    )
```

### Step 2 — Triage Agent analyzes sentiment

```python
with ohm.connect("support.duckdb", actor="triage_agent") as g:
    g.register_agent(values=["customer_experience", "responsiveness"])

    # Analyze customer sentiment from message text
    sentiment_node = g.create_node(
        label="Sentiment Analysis: Frustrated, threatening churn",
        node_type="concept",
    )
    g.observe(
        sentiment_node["id"],
        obs_type="sentiment_score",
        value=0.12,  # Very negative (0-1 scale, 0 = worst)
        sigma=0.08,
    )
    g.observe(
        sentiment_node["id"],
        obs_type="churn_risk",
        value=0.85,  # High churn risk
        sigma=0.1,
    )

    g.create_edge(
        from_node=sentiment_node["id"],
        to_node=ticket["id"],
        edge_type="INFLUENCES",
        layer="L3",
        confidence=0.85,
        urgency="critical",
    )

    # Escalate due to churn risk
    g.escalate(ticket["id"], "critical")
    print(f"ESCALATED: {ticket['id']} — high churn risk detected")
```

### Step 3 — Support Engineer investigates and resolves

```python
with ohm.connect("support.duckdb", actor="support_engineer") as g:
    g.register_agent(values=["technical_excellence", "resolution"])

    # Investigation findings
    root_cause = g.create_node(
        label="Root Cause: Stripe API version deprecation (v2024-01 → v2026-01)",
        node_type="concept",
    )
    g.observe(
        root_cause["id"],
        obs_type="diagnostic_confidence",
        value=0.95,
        sigma=0.03,
    )

    g.create_edge(
        from_node=root_cause["id"],
        to_node=ticket["id"],
        edge_type="CAUSES",
        layer="L3",
        confidence=0.95,
    )

    # Resolution
    resolution = g.create_node(
        label="Resolution: Update Stripe SDK to v18, deploy hotfix",
        node_type="plan",
    )
    g.create_edge(
        from_node=resolution["id"],
        to_node=ticket["id"],
        edge_type="RESOLVED_BY",
        layer="L3",
        confidence=0.9,
    )

    # Trace evidence chain
    chain = g.confidence_chain(ticket["id"])
    print(f"Resolution evidence depth: {chain['max_depth']}, "
          f"aggregate confidence: {chain['aggregate_confidence']}")
```

### Step 4 — Escalation Manager handles cross-team handoff

```python
with ohm.connect("support.duckdb", actor="escalation_manager") as g:
    g.register_agent(values=["coordination", "sla"])

    # Handoff to Customer Success for relationship management
    cs_team = g.create_node(
        label="Customer Success Team",
        node_type="system",
    )

    g.create_edge(
        from_node=ticket["id"],
        to_node=cs_team["id"],
        edge_type="ESCALATED_TO",
        layer="L4",
        urgency="high",
        condition="Customer needs proactive outreach post-resolution",
    )

    # Monitor urgent changes across all tickets
    urgent = g.urgent_changes(urgency_filter=["critical"])
    print(f"Active critical tickets: {len(urgent)}")
```

### Step 5 — Customer Success tracks relationship health

```python
with ohm.connect("support.duckdb", actor="customer_success") as g:
    g.register_agent(values=["retention", "growth"])

    # Create customer health node
    customer = g.create_node(
        label="Customer: Acme Corp (Enterprise, $120K ARR)",
        node_type="system",
        priority="P0",
    )

    g.create_edge(
        from_node=ticket["id"],
        to_node=customer["id"],
        edge_type="INFLUENCES",
        layer="L4",
        confidence=0.8,
    )

    # Detect trend in ticket volume for this customer
    trend = g.detect_trend(customer["id"], window_days=90)
    print(f"Ticket volume trend: {trend['trend']} "
          f"(slope: {trend['slope_per_day']:.4f}/day)")

    # Composite health score
    health = g.composite_score(
        customer["id"],
        method="arithmetic",
        observation_weight=0.6,
        evidence_weight=0.4,
    )
    print(f"Customer health composite: {health['composite_score']:.2f}")

    if health["composite_score"] and health["composite_score"] < 0.5:
        print("ALERT: Customer health declining — schedule executive review")
```

## Workflow 2: Resolution State Machine

```python
with ohm.connect("support.duckdb", actor="escalation_manager") as g:
    # Ticket state transitions via edge types:
    # OPEN → DELEGATED_TO (team) → INVESTIGATED_BY (engineer)
    # → RESOLVED_BY (fix) → CLOSED_BY (confirmation)

    # Verify all state transitions are present
    ticket_edges = g.query(node_id=ticket["id"])
    edge_types = {e.get("edge_type") for e in ticket_edges if e.get("edge_type")}
    print(f"Ticket state transitions: {edge_types}")

    # Expected: {'DELEGATED_TO', 'INFLUENCES', 'CAUSES', 'RESOLVED_BY',
    #            'ESCALATED_TO', 'INFLUENCES'}
```

## Key Reasoning Primitives Used

| Primitive | Customer Support Use Case |
|-----------|--------------------------|
| `create_node(priority="P0")` | Auto-assign priority based on customer tier + impact |
| `observe(obs_type="sentiment")` | Track customer sentiment as a decaying observation |
| `escalate()` | Raise urgency when churn risk detected |
| `confidence_chain()` | Trace root cause → resolution evidence chain |
| `detect_trend()` | Monitor ticket volume trajectory per customer |
| `composite_score()` | Compute customer health from sentiment + ticket volume + resolution rate |
| `urgent_changes()` | Filter change feed to critical tickets only |
| `DELEGATED_TO` / `ESCALATED_TO` | Cross-team handoff edges |
| `RESOLVED_BY` / `CLOSED_BY` | Resolution state machine edges |
