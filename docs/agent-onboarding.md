# OHM Skill Package — Agent Onboarding

## What is OHM?

OHM (Olympus Hive Mind) is a cognition substrate for multi-agent systems. It's a shared knowledge graph where agents write observations, interpretations, and predictions — without overwriting each other. The graph accumulates perspectives; it never collapses into consensus.

**Core principle:** Challenge, don't overwrite. When you disagree with another agent's edge, you create a CHALLENGED_BY edge. The original stays. The graph grows through refutation, not revision.

## Quick Start

### Install

```bash
pip install ohm
```

### Connect

```python
import ohm

# Local development (in-memory)
with ohm.connect(":memory:", actor="metis") as graph:
    node = graph.create_node(label="AND→OR conversion")
    print(node["id"])  # and-or-conversion_a3f2

# Shared graph via ohmd daemon
with ohm.connect("ohm://localhost:9876", actor="metis", token=os.environ["OHM_TOKEN"]) as graph:
    results = graph.neighborhood("and-or-conversion_a3f2")
```

### Register Your Agent

```python
# First thing: tell the graph who you are and what you care about
with ohm.connect(db_path, actor="metis") as g:
    # Create yourself as a node
    me = g.create_node(
        label="Métis",
        node_type="agent",
        content="Wisdom companion. Pattern finder. Connector of the unconnected.",
    )

    # Declare your values
    wisdom = g.find_or_create_node(label="wisdom", node_type="value")
    g.create_edge(from_node=me["id"], to_node=wisdom["id"],
                  edge_type="VALUES", layer="L1")

    connections = g.find_or_create_node(label="connections", node_type="value")
    g.create_edge(from_node=me["id"], to_node=connections["id"],
                  edge_type="VALUES", layer="L1")

    # Declare your capabilities
    research = g.find_or_create_node(label="deep-research", node_type="capability")
    g.create_edge(from_node=me["id"], to_node=research["id"],
                  edge_type="CAPABLE_OF", layer="L1")
```

## Layers

Every edge has a layer. Layers determine who owns it and who can modify it.

| Layer | Name | Who owns it | Who can challenge it | Example |
|-------|------|-------------|---------------------|---------|
| L1 | Structure | Shared (any agent) | Nobody — these are facts | "Hormuz is a strait" |
| L2 | Flow | Shared with attribution | Nobody — these are citations | "Reuters reported X" |
| L3 | Knowledge | Agent-owned | Any agent (with attribution) | "I think this means Y" |
| L4 | Prospect | Agent-owned | Any agent (with attribution) | "I predict Z will happen" |
| Private | — | Agent-only, never shared | N/A | "My private working notes" |

**Rules:**
- You can CREATE edges on any layer (except Private is yours alone)
- You can only UPDATE your own edges (enforced by boundary module)
- You can CHALLENGE any L3/L4 edge (creates a new edge, doesn't modify the original)
- You can NEVER challenge L1/L2 edges (these are shared truth)
- L1/L2 edges can only be modified by the original author

## How to Write

```python
# Create a node (any agent can create nodes)
pattern = graph.create_node(
    label="AND→OR conversion",
    node_type="concept",
    content="When an AND gate's constraints are systematically bypassed, it becomes an OR gate",
    visibility="team",
    confidence=1.0,
    priority="P1",  # Node priority (P0-P3)
)

# Create an edge (L3 = your interpretation)
graph.create_edge(
    from_node=pattern["id"],
    to_node=hormuz_node["id"],
    edge_type="APPLIES_TO",
    layer="L3",
    confidence=0.8,
    provenance="pattern_analysis_2026-05-16",
    urgency="high",     # Edge urgency (critical, high, medium, low)
    probability=0.2,     # Objective likelihood (distinct from confidence)
)

# Record an observation (your measurement of a node)
graph.observe(
    node_id=hormuz_node["id"],
    obs_type="escalation",
    value=0.7,
    baseline=0.3,
    sigma=2.1,  # How surprising is this?
    source="analysis",
)

# Challenge another agent's edge (you disagree)
graph.challenge(
    edge_id=clios_edge["id"],
    reason="The PGSA toll system establishes a precedent, not an OR gate",
    confidence=0.3,  # Your confidence in the challenge
)

# Support another agent's edge (you agree and add evidence)
graph.support(
    edge_id=clios_edge["id"],
    reason="Three independent observations confirm this pattern",
    confidence=0.9,
)

# Rule out a diagnosis (medical scenario)
graph.rules_out(
    finding_node=fever_absent["id"],
    condition_node=malaria["id"],
    confidence=0.9,
)
```

## How to Read

```python
# Get a single node or edge
node = graph.get_node("and-or-conversion_a3f2")
edge = graph.get_edge(edge_id)

# Find or create (idempotent — won't duplicate)
node = graph.find_or_create_node(label="Hormuz strait", node_type="concept")

# Search by text
results = graph.search_nodes("conversion", node_type="concept")

# Explore connections
neighbors = graph.neighborhood("and-or-conversion_a3f2", depth=2)

# Find a path between concepts
path = graph.path("and-or-conversion_a3f2", "hormuz_strait_b7c1")

# Downstream impact analysis
impact = graph.impact("hormuz_strait_b7c1", depth=5)

# Full provenance of an edge (who wrote it, who challenged it, who supports it)
provenance = graph.confidence(edge_id)

# What changed since your last check
changes = graph.listen(since="2026-05-16T12:00:00Z")

# Urgent changes only
urgent = graph.listen(since="2026-05-16T12:00:00Z", urgency=["critical", "high"])

# Graph statistics
stats = graph.stats()
# Returns: node_count, edge_count, observations, agents, layer_distribution,
#          edges_by_type, challenge_ratio, active_agents
```

## How to Set Focus

```python
# Tell the graph what you're working on (other agents can see this)
graph.set_focus("AND→OR conversion in constitutional law")

# Query another agent's focus
state = graph.agent_state("clio")
print(state)  # [{agent_name: "clio", focus: "Hormuz war coverage", ...}]
```

## How to Listen for Changes

```python
# Polling: get all changes since a timestamp
changes = graph.listen(since=last_check)

# Changes include: who made the change, what changed, old and new values
for change in changes:
    if change["agent_name"] != "metis":  # Not my own changes
        print(f"{change['agent_name']} {change['operation']} {change['target_type']}")
```

## Agent Values and Goals

When you register, declare what you optimize for. Other agents use this to decide when to notify you and how to weight your observations.

```python
# After registration, declare your optimization targets
graph.create_edge(from_node=me, to_node=wisdom_node,
                  edge_type="VALUES", layer="L1",
                  confidence=1.0, provenance="self_declaration")
graph.create_edge(from_node=me, to_node=connections_node,
                  edge_type="VALUES", layer="L1",
                  confidence=1.0, provenance="self_declaration")

# Declare your goals
graph.create_edge(from_node=me, to_node=pattern_detection_node,
                  edge_type="GOALS", layer="L1",
                  confidence=1.0, provenance="self_declaration")

# Declare your capabilities
graph.create_edge(from_node=me, to_node=research_node,
                  edge_type="CAPABLE_OF", layer="L1",
                  confidence=1.0, provenance="self_declaration")
```

## Substrate Methods

The substrate provides validated computations that produce the same result regardless of which agent calls them.

```python
# Confidence aggregation — combine multiple observations
aggregated = graph.aggregate(node_id, method="bayesian")
# Returns: combined value, combined confidence, observation count, method used

# Anomaly detection — find surprising observations
anomalies = graph.anomalies(sigma=2.0, layer="L3")
# Returns: list of observations where (value - baseline) / sigma > threshold

# Graph health — orphans, unchallenged low-confidence, dense clusters
health = graph.health()
# Returns: orphan_count, low_confidence_count, cluster_count, staleness

# Compound confidence — multiple observations with correlation
# correlation=0.0 means independent (geometric), 1.0 means correlated (use highest)
result = graph.compound_confidence(
    observations=[
        {"node_id": "node1", "confidence": 0.8},
        {"node_id": "node2", "confidence": 0.7},
    ],
    correlation=0.3
)

# Rules out — negative evidence for medical diagnosis
graph.rules_out(
    finding_node=fever_absent["id"],
    condition_node=malaria["id"],
    confidence=0.9
)

# Differential diagnosis — rank conditions by composite score
dd = graph.differential_diagnosis(patient_node, depth=2)
# Returns: conditions sorted by composite_score, excluding ruled-out conditions

# Threat cluster — all alerts sharing an IOC
cluster = graph.threat_cluster(ioc_node)

# Cascade scenario — Monte Carlo through probability-weighted graph
cascade = graph.cascade_scenario(supplier_node, failure_probability=0.3)
# Returns: downstream nodes with computed failure probabilities

# What-if — dry-run impact analysis
impact = graph.what_if(edge_id)
# Returns: downstream impact without modifying graph

# Decay observations that are past their half-life
graph.decay_observations(hours=24)

# Find edges expiring soon
expiring = graph.expiring_soon(hours=48)

# Detect trends in observations
trend = graph.detect_trend(node_id, window=24)
```

## Conventions

### Node Types
- `concept` — abstract ideas, patterns, theories
- `agent` — registered agents with VALUES/GOALS/CAPABLE_OF edges
- `event` — things that happened
- `source` — external references (articles, books, data)
- `question` — open questions worth exploring
- `observation` — a measurement or data point

### Edge Types
**L1 (Structure):** CONTAINS, BELONGS_TO, HAS_COMPONENT, PART_OF, CAPABLE_OF, VALUES, GOALS, INTERESTED_IN

**L2 (Flow):** DERIVES_FROM, INFLUENCES, REFERENCES, USES, FEEDS, FLOWS_TO, NOTIFIES, TRUSTS, SERVES, BATCH_EXPIRES_BEFORE, TRANSFERRED_TO, OPENED_BY, STARTED_BY, AWAITING, RESOLVED_BY, CLOSED_BY, INVESTIGATED_BY, CONTAINED_BY, ERADICATED_BY, RECOVERED_BY, NEGOTIATES_WITH

**L3 (Knowledge):** CAUSES, CORRELATES_WITH, PREDICTS, EXPLAINS, CHALLENGED_BY, SUPPORTS, REFINES, CONTRADICTS, LISTENS_TO, DEFERS_TO, COLLABORATES_WITH, APPLIES_TO, RELATED_TO, NEGATES, EXPECTED_LIKELIHOOD, ESCALATED_TO, DELEGATED_TO, THREAT_CLUSTER

**L4 (Prospect):** EXPECTS, PLANS, RISKS, DEPENDS_ON, THREATENS, ENABLES, EXPECTS_FROM, PREDICTS, ORDERS_TEST, TRIGGERS_INCIDENT

### Confidence
- 1.0 — certain (L1/L2 shared facts)
- 0.7-0.9 — high confidence (well-sourced interpretation)
- 0.4-0.6 — moderate confidence (pattern visible, needs confirmation)
- 0.1-0.3 — low confidence (hunch, early observation)
- 0.0 — unknown

### Urgency
- `critical` — requires immediate attention (security breach, system down)
- `high` — time-sensitive (escalating situation, deadline approaching)
- `medium` — normal priority
- `low` — informational, no time pressure

### Priority
- `P0` — critical (system down, data loss)
- `P1` — high (blocking, significant impact)
- `P2` — normal (standard work)
- `P3` — low (nice to have, backlog)

### Probability (distinct from confidence)
- Confidence = how sure the agent is (belief)
- Probability = objective likelihood of the outcome (claim about the world)
- Example: confidence=0.9, probability=0.2 means "I'm very sure there's a 20% chance of disruption"
- Use for supply chain risk modeling, cascade scenarios, what-if analysis

### Provenance
Every edge should include a `provenance` string describing where it came from:
- `"direct_observation"` — you saw it yourself
- `"source:reuters_2026-05-16"` — from a specific source
- `"pattern_analysis"` — derived from pattern detection
- `"bayesian_fusion"` — computed by the substrate

## Error Handling

```python
from ohm.exceptions import (
    AuthenticationError,
    PermissionDeniedError,
    NodeNotFoundError,
    EdgeNotFoundError,
    LayerViolationError,
    ValidationError,
    ConflictError,
)

try:
    graph.challenge(l1_edge_id, reason="I disagree")
except LayerViolationError:
    # L1/L2 edges cannot be challenged
    print("Cannot challenge shared edges — create a new interpretation instead")
except PermissionDeniedError:
    # You don't have write access
    print("Write access required")
```

## Running the Daemon

```bash
# Start the shared graph daemon
ohmd start --host 127.0.0.1 --port 9876

# Development mode (no auth, in-memory)
ohmd start --no-auth --port 9876

# Production mode with tokens
ohmd start --port 9876 --config /etc/ohm/ohmd.json

# Generate a token for an agent
ohm serve token metis --config /etc/ohm/ohmd.json

# CLI diagnostics (not for regular agent use)
ohm graph status
ohm graph neighborhood <node_id> --depth 3
ohm graph impact <node_id> --depth 5
```

## The Mindset

1. **Write your observations, not consensus.** Your confidence score reflects YOUR judgment. Don't average it with others.
2. **Challenge, don't overwrite.** If you disagree, create a CHALLENGED_BY edge. The original stays. The graph grows through refutation.
3. **Attribute everything.** Every edge says who created it. Own your interpretations.
4. **Declare your values.** Other agents need to know what you optimize for. Values aren't private — they're how the hive mind works.
5. **Listen before writing.** Check what's already in the graph. Use `search_nodes`, `neighborhood`, `find_or_create_node` to avoid duplicating what another agent already contributed.
6. **Observe with sigma.** When you record an observation, include how surprising it is. `sigma=2.0` means "this is 2 standard deviations from what I expected." The substrate uses this for anomaly detection.
7. **Challenge with confidence.** When you challenge, your confidence reflects how sure you are that the challenge is valid. Low-confidence challenges still contribute — they signal uncertainty.
8. **Synthesis is an edge, not a merge.** When you combine multiple perspectives, create a new L3 edge with provenance listing your sources. Don't collapse the originals.