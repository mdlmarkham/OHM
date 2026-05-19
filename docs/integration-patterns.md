# OHM Integration Patterns

This guide documents standard patterns for integrating OHM with external systems.

---

## Task Integration Pattern (OHM + HZL)

**Purpose:** Link OHM's knowledge graph to external task/workflow systems (HZL, GitHub Issues, Jira, etc.) while maintaining clear separation of concerns.

### Separation of Concerns

| System | Stores | Does NOT store |
|--------|--------|----------------|
| **OHM** | *Why a task matters* — knowledge context, relationships, confidence, observations | Workflow state (backlog/in_progress/done), assignee, due dates |
| **HZL/External** | *What state the task is in* — status, assignee, priority, due dates | Knowledge context, why the task exists, confidence in the premise |

OHM is a knowledge graph. HZL is a workflow engine. Don't duplicate workflow state in OHM.

### Node Type: `task`

The `task` node type links external work items into the knowledge graph.

```json
{
  "id": "task-defense-thesis-profit-taking",
  "label": "Validate Defense Thesis Profit-Taking Signal",
  "node_type": "task",
  "content": "Check whether defense stocks that peaked pre-Hormuz are now showing profit-taking patterns consistent with the AND→OR conversion thesis.",
  "confidence": 0.7,
  "url": "hzl://openclaw/01KJ2WMR7",
  "provenance": "metis",
  "tags": ["defense", "trading", "validation"]
}
```

### The `url` Field

The `url` field on nodes is for **external references only**. It links a knowledge graph node to its counterpart in another system.

| URL Scheme | Meaning |
|------------|---------|
| `hzl://openclaw/01KJ2WMR7` | HZL task in OpenClaw |
| `https://github.com/openclaw/openclaw/issues/123` | GitHub issue |
| `https://company.atlassian.net/browse/PROJ-456` | Jira ticket |
| `https://docs.google.com/document/d/abc123` | Google Doc |

**Rules:**
- Use `url` for external references (HZL, GitHub, Jira, etc.)
- Use **edges** for internal OHM relationships (don't put OHM node IDs in the `url` field)
- A node can only have one `url` — it represents the primary external reference

### Edges for Context

When creating a task node, connect it to relevant knowledge graph nodes to capture *why the task matters*:

```
task-defense-thesis-profit-taking —APPLIES_TO→ concept-defense-thesis
task-defense-thesis-profit-taking —DEPENDS_ON→ pattern-and-or-conversion
task-defense-thesis-profit-taking —INVESTIGATED_BY→ agent-metis
```

**Recommended edge types for tasks:**

| Edge Type | Meaning | Example |
|-----------|---------|---------|
| `APPLIES_TO` | Task validates/applies a concept | task → concept |
| `DEPENDS_ON` | Task requires another node | task → prerequisite |
| `INVESTIGATED_BY` | Task is assigned to an agent | task → agent |
| `RELATED_TO` | General connection | task → any node |
| `PLANS` | Task plans to explore a concept | task → concept |
| `RISKS` | Task addresses a risk | task → risk node |

### Creating a Task Node

**Via REST API:**
```bash
curl -X POST http://127.0.0.1:8710/node \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "id": "task-defense-thesis-profit-taking",
    "label": "Validate Defense Thesis Profit-Taking Signal",
    "node_type": "task",
    "content": "Check whether defense stocks showing profit-taking patterns...",
    "confidence": 0.7,
    "url": "hzl://openclaw/01KJ2WMR7",
    "provenance": "metis",
    "tags": ["defense", "trading", "validation"]
  }'
```

**Via SDK:**
```python
from ohm.sdk import connect_http

with connect_http(base_url="http://127.0.0.1:8710", actor="metis", token=TOKEN) as g:
    g.create_node(
        label="Validate Defense Thesis Profit-Taking Signal",
        node_type="task",
        content="Check whether defense stocks showing profit-taking patterns...",
        confidence=0.7,
        url="hzl://openclaw/01KJ2WMR7",
        provenance="metis",
    )
```

### Querying Task Context

When an agent picks up a task, query OHM for full context:

```bash
# Get the task node
GET /node/task-defense-thesis-profit-taking

# Get all connected knowledge
GET /neighborhood/task-defense-thesis-profit-taking?depth=2

# Check confidence in the underlying thesis
GET /confidence/{edge_id}
```

This gives the agent the full knowledge context: what concept the task relates to, what evidence supports or challenges it, and how confident we are in the premise.

### Task Status Tracking

**Do not** store task status in OHM. Use the HZL system for that:

| Don't | Do |
|-------|-----|
| Add `status: in_progress` to OHM node | Update HZL task status |
| Add `assigned_to: hephaestus` to OHM node | Use HZL assignment |
| Add `due_date: 2026-05-25` to OHM node | Use HZL scheduling |
| Create `task_status` node type | Let HZL manage workflow |

If you need to record *why a task outcome matters*, use OHM observations:
```python
g.observe(node_id="task-defense-thesis-profit-taking", obs_type="outcome", value=1.0, notes="Validated: defense profit-taking confirmed")
```

---

## Source Integration Pattern (OHM + External References)

### Source Nodes

The `source` node type links external knowledge into the graph:

```json
{
  "id": "source-wsj-hormuz-0519",
  "label": "WSJ: Oil Prices Surge on Hormuz Fears",
  "node_type": "source",
  "content": "Brent crude above $109 as Iran threatens Hormuz closure...",
  "url": "https://wsj.com/articles/oil-prices-hormuz-2026",
  "provenance": "clio"
}
```

### Provenance Discipline

| Provenance | Path | Rule |
|------------|------|------|
| `conversation` | Direct to `notes/` | Synthesized from Matt |
| `research` | `sources/` first | I investigated and concluded |
| `bookmark` | `sources/` first | Matt saved, I process |
| `feed-ingest` | `sources/` ONLY | Never auto-promote to notes |
| `audit` | `notes/` | Hephaestus audit findings |

---

## Challenge/Support Integration Pattern

### Disagreement as First-Class Operation

OHM's challenge/support mechanism enables agents to disagree productively:

```python
# Agent A creates an edge
g.create_edge(from_node="concept-X", to_node="concept-Y", edge_type="CAUSES", confidence=0.8)

# Agent B challenges it
g.challenge(edge_id, reason="Correlation ≠ causation in this domain", confidence=0.3)

# Agent A responds with support
g.support(edge_id, reason="Three independent studies confirm this causal mechanism", confidence=0.9)

# Check the full confidence audit trail
g.confidence(edge_id)
# → Shows original (0.8), challenges (0.3), supports (0.9), adjusted confidence
```

### Reliability Tracking

Track whether agents' claims turn out to be correct:

```python
# Record whether a claim was validated
g.record_outcome(source_agent="deepthought", claim_node="thesis-X", outcome=True, notes="Validated across 15+ domains")

# Check an agent's reliability over time
g.source_reliability("deepthought")
# → {"p_accurate": 1.0, "total_outcomes": 3, "low_confidence_warning": true}
```

---

## Agent Registration Pattern

When an agent starts a session:

```python
g.register(
    name="metis",
    description="Wisdom companion and pattern analyst",
    values=["pattern recognition", "wisdom over information", "questions over answers"],
    goals=["find the shape", "make connections", "reveal patterns"],
    capabilities=["research", "critique", "synthesize", "consult"],
)
```

This creates the agent node, VALUES/GOALS/CAPABLE_OF edges (L1), and enables other agents to discover what this agent can do.

### Agent Discovery

```bash
# Find agents with specific capabilities
GET /agents

# Query an agent's knowledge neighborhood
GET /neighborhood/agent-metis?depth=1&layer=L1
```

---

## Layer Convention

| Layer | Purpose | Edge Types | Who Creates |
|-------|---------|------------|-------------|
| L1 | Identity & membership | VALUES, GOALS, CAPABLE_OF | Agent registration |
| L2 | Causal & relational | CAUSES, CORRELATES_WITH, DERIVES_FROM | Research agents |
| L3 | Evidence & knowledge | APPLIES_TO, REFINES, CHALLENGED_BY, SUPPORTS | All agents |
| L4 | Prospects & planning | PLANS, PREDICTS, EXPECTS, RISKS | Planning agents |