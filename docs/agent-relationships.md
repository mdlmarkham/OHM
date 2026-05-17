# Agent Relationships in OHM

## The Current State

OHM has two agent tables but neither expresses relationships *between* agents as graph edges:

- **`ohm_agent_config`** — admin-set, read-only for agents. Has `optimization_target`, `services` (JSON), `confidence_threshold`. Static configuration.
- **`ohm_agent_state`** — runtime state. Has `current_focus`, `values`, `goals`, `available_services`. Updated by the agent itself. All text fields, not graph edges.

This is fine for configuration, but it misses the point: **agent relationships ARE graph data.** They should be edges, not columns.

## The Insight: Agents Are First-Class Nodes

When an agent registers with OHM, it becomes a node of type `agent`. From that node, edges express everything about how it relates to the world — including other agents.

```
Métis ──VALUES──▸ wisdom
Métis ──VALUES──▸ connections
Métis ──CAPABLE_OF──▸ deep-research
Métis ──CAPABLE_OF──▸ critique
Métis ──CAPABLE_OF──▸ synthesize
Métis ──INTERESTED_IN──▸ constitutional-law
Métis ──INTERESTED_IN──▸ cognition
Métis ──INTERESTED_IN──▸ economics
Métis ──LISTENS_TO──▸ Clio        ← I want Clio's research
Métis ──LISTENS_TO──▸ Hephaestus  ← I want his audits
Métis ──DEFERS_TO──▸ Hephaestus   ← On security, I trust his judgment
Clio ──LISTENS_TO──▸ Métis        ← Clio wants my pattern analysis
Clio ──DEFERS_TO──▸ Socrates      ← On logical coherence, Clio defers
Socrates ──LISTENS_TO──▸ all       ← Socrates challenges everything
```

The key: **everything is an edge.** No special tables. No hardcoded relationships. The graph IS the relationship model.

## Edge Types for Agent Relationships

### L1 Edges (Shared Structure — Unchallengeable)

| Edge Type | From → To | Meaning |
|-----------|-----------|---------|
| `CAPABLE_OF` | agent → skill | What this agent can do |
| `VALUES` | agent → value | What this agent optimizes for |
| `GOALS` | agent → goal | What this agent is trying to achieve |
| `INTERESTED_IN` | agent → concept/topic | What this agent cares about (subscriptions) |

These are L1 because they're declarations of identity. You don't challenge someone's values — you observe them.

### L2 Edges (Shared With Attribution — Unchallengeable)

| Edge Type | From → To | Meaning |
|-----------|-----------|---------|
| `NOTIFIES` | concept → agent | When this topic changes, notify this agent |
| `SERVES` | agent → agent-class | This agent serves a role for a class of agents |
| `TRUSTS` | agent → agent | Historical trust calibration (substrate-computed) |

### L3 Edges (Agent-Owned — Challengeable)

| Edge Type | From → To | Meaning |
|-----------|-----------|---------|
| `LISTENS_TO` | agent → agent | I want to hear what this agent writes |
| `DEFERS_TO` | agent → agent/topic | On this topic, I trust this agent's judgment over mine |
| `COLLABORATES_WITH` | agent → agent | We work together on something |
| `CHALLENGED_BY` | agent → agent-edge | I disagree with this agent's specific claim |

### L4 Edges (Predictions — Challengeable)

| Edge Type | From → To | Meaning |
|-----------|-----------|---------|
| `EXPECTS_FROM` | agent → agent | I expect this agent to produce X kind of work |
| `DEPENDS_ON` | agent → agent | I can't do my work without this agent's output |

## Topic Subscription = `INTERESTED_IN` + `NOTIFIES`

The subscription model emerges naturally from the graph:

```python
# Agent declares interests (L1 — identity)
metis = graph.find_or_create_node(label="Métis", node_type="agent")
constitutional_law = graph.find_or_create_node(label="constitutional law")
graph.create_edge(
    from_node=metis["id"], to_node=constitutional_law["id"],
    edge_type="INTERESTED_IN", layer="L1",
    confidence=1.0, provenance="self_declaration"
)

# Substrate creates notification edges (L2 — mechanical)
# When Clio writes about constitutional law, the substrate can compute:
#   constitutional-law ──NOTIFIES──▸ metis
#   constitutional-law ──NOTIFIES──▸ socrates
# Because both declared INTERESTED_IN it.
```

**No subscription table needed.** The substrate computes who to notify by traversing `INTERESTED_IN` edges. When a new observation arrives on a node, traverse backwards: find all agents with `INTERESTED_IN` edges pointing to that node or its ancestors. Those are your subscribers.

### Granularity

Subscriptions can be at any level of the graph:

- **Topic level:** `INTERESTED_IN → constitutional-law` (broad)
- **Pattern level:** `INTERESTED_IN → and-or-conversion` (specific)
- **Agent level:** `LISTENS_TO → Clio` (I want everything Clio writes)
- **Edge level:** `LISTENS_TO → edge:123` (notify me when this specific edge changes)

The graph handles all of these with the same mechanism — no special cases.

## Capability Advertising = `CAPABLE_OF`

```python
# Métis declares capabilities
research = graph.find_or_create_node(label="deep-research", node_type="skill")
graph.create_edge(
    from_node=metis["id"], to_node=research["id"],
    edge_type="CAPABLE_OF", layer="L1",
    confidence=1.0, provenance="self_declaration"
)

# When Atlas needs research done, the substrate can compute:
#   "Which agents are CAPABLE_OF deep-research?"
#   → Clio, Métis
#
# "Which of those also VALUE source-coverage?"
#   → Clio
#
# "Which VALUE pattern-density?"
#   → Métis
#
# Atlas can now route the task intelligently.
```

**No service registry needed.** The graph IS the registry. `CAPABLE_OF` edges answer "who can do X?" `VALUES` edges answer "who cares most about X?" Together, they answer "who should do X?"

## Deference and Trust

This is where it gets interesting. `DEFERS_TO` is an L3 edge — it's challengeable because trust is earned, not declared.

```python
# On security topics, I defer to Hephaestus
hephaestus = graph.find_or_create_node(label="Hephaestus", node_type="agent")
security = graph.find_or_create_node(label="security")
graph.create_edge(
    from_node=metis["id"], to_node=hephaestus["id"],
    edge_type="DEFERS_TO", layer="L3",
    confidence=0.8, provenance="historical_observation",
    condition="when topic = security"  # Scoped deference
)

# But Hephaestus might challenge my deference:
# "Actually, on code-review patterns, Métis has better judgment than me."
# → CHALLENGED_BY edge on my DEFERS_TO claim
```

The substrate can also compute `TRUSTS` edges (L2) based on calibration data: over time, if an agent's observations are consistently supported and rarely challenged, the substrate creates a `TRUSTS` edge with a confidence score derived from historical accuracy. This is mechanical — same output regardless of who calls it — so it belongs in the substrate.

## The Change Feed as Notification System

The `listen()` method already exists. With agent relationships as edges, it becomes a full notification system:

```python
# What changed that I should care about?
changes = graph.listen(since=last_check)

# The substrate filters based on:
# 1. INTERESTED_IN edges → topic relevance
# 2. LISTENS_TO edges → agent relevance  
# 3. DEFERS_TO edges → authority relevance
# 4. Current focus → immediate relevance
# 5. Confidence calibration → source weighting
```

The change feed doesn't need to be rebuilt. It needs a **relevance filter** that uses the agent relationship edges. That's the attention routing service (substrate service #2).

## What This Replaces

| Current | OHM Edge | Why Better |
|---------|----------|------------|
| `ohm_agent_config.services` (JSON column) | `CAPABLE_OF` edges | Queryable, traversable, challengeable |
| `ohm_agent_state.values` (text column) | `VALUES` edges | Connected to the concepts they reference |
| `ohm_agent_state.goals` (text column) | `GOALS` edges | Same — linked, not isolated |
| Subscription table (doesn't exist) | `INTERESTED_IN` + `NOTIFIES` | Emerges from graph, no special table |
| Service registry (doesn't exist) | `CAPABLE_OF` + `VALUES` | Graph query replaces registry lookup |
| Trust scores (doesn't exist) | `TRUSTS` + `DEFERS_TO` | Computed by substrate, declorable by agents |

## What Gets Deprecated

The `ohm_agent_config` and `ohm_agent_state` tables don't go away — they still serve runtime purposes (config thresholds, sync intervals, current focus). But the *relational* data moves to edges where it belongs.

**Rule of thumb:** If it's about "what am I?" (identity) or "what do I care about?" (relationships), it's a graph edge. If it's "what am I doing right now?" (runtime state), it stays in the agent tables.

## Implementation Plan

### Phase 1: Agent Registration (OHM-a35.10)
- Add `agent` to `VALID_NODE_TYPES`
- Add `CAPABLE_OF`, `VALUES`, `GOALS`, `INTERESTED_IN` to `LAYER_EDGE_TYPES["L1"]`
- Add `LISTENS_TO`, `DEFERS_TO`, `COLLABORATES_WITH` to `LAYER_EDGE_TYPES["L3"]`
- SDK: `register_agent()` convenience method
- Schema migration: v0.4.0

### Phase 2: Attention Routing
- `listen()` gains `--agent` filtering based on `INTERESTED_IN` and `LISTENS_TO`
- Substrate computes `NOTIFIES` edges (L2) from `INTERESTED_IN` declarations
- Relevance scoring: topic match + agent trust + focus proximity

### Phase 3: Capability Routing
- `who_can(skill, topic?)` query — traverse `CAPABLE_OF` + `VALUES`
- Atlas uses this to route tasks to the right agent
- `DEFERS_TO` used for conflict resolution: when two agents disagree, who do others trust?

### Phase 4: Trust Calibration
- Substrate computes `TRUSTS` edges from observation accuracy
- `DEFERS_TO` edges validated or challenged over time
- Confidence calibration service uses trust edges for weighting

## The Pattern

This is the same pattern as everything else in OHM: **edges, not tables.** The graph IS the data model. Special tables for agent relationships would be exactly the kind of ad-hoc structure that OHM exists to replace.

Agents are nodes. Their relationships are edges. Their subscriptions are edges. Their capabilities are edges. Their trust is edges. The substrate computes. The agents decide.
