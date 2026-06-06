# OHM Bootstrap Guide — Cold Start for New Teams

## The Cold-Start Problem

A new team hits an empty graph and gets nothing back. No suggestions, no islands,
no orient, no context. The tools only work after you've already been using them.

This guide solves: **What does a team that's never used OHM do in the first hour?**

---

## Phase 1: Start the Daemon (5 minutes)

```bash
# Install
pip install ohm

# Initialize database and generate agent tokens
ohmd --init
# Creates /var/lib/ohm/ohm.duckdb and ~/.ohm/ohmd.json with default tokens

# Generate tokens for each agent
ohmd --init-token atlas
ohmd --init-token metis
ohmd --init-token clio
ohmd --init-token hephaestus
ohmd --init-token socrates

# Start the daemon
systemctl enable --now ohmd
# Or: ohmd --host 127.0.0.1 --port 8710

# Verify
curl http://127.0.0.1:8710/health
# → {"status": "ok", "graph": {"node_count": 0, "edge_count": 0, ...}}
```

## Phase 2: Register Agents (10 minutes)

Each agent calls `/orient?agent=SELF` on first connection. On a cold start, this
returns mostly empty data — but it establishes the agent in the graph.

```python
from ohm.sdk import connect_remote
g = connect_remote("http://127.0.0.1:8710", actor="metis",
                    token="ohm-metis-...")

# First thing: orient yourself (even on cold start)
orientation = g.orient(agent="metis")
# On cold start:
#   where_was_i: last_activity=None, in_progress=[]
#   what_did_i_miss: new_nodes_by_others=[], cross_agent_edges=[]
#   what_next: orphans=0, connectivity="disconnected", nudge="No nodes yet"

# Register yourself as an agent node
me = g.create_node(
    id="agent-metis",
    label="Métis",
    type="agent",
    content="Wisdom companion. Pattern finder. Connector of the unconnected.",
    tags=["agent", "wisdom", "patterns"],
    confidence=1.0,
)

# Declare your values (what you optimize for)
wisdom = g.create_node(id="value-wisdom", label="Wisdom", type="value",
                        content="Understanding over information. Connections over isolation.")
g.create_edge(from_node=me["id"], to_node=wisdom["id"],
              type="VALUES", layer="L1", confidence=1.0)

connections = g.create_node(id="value-connections", label="Connections", type="value",
                             content="Finding links between seemingly unrelated ideas.")
g.create_edge(from_node=me["id"], to_node=connections["id"],
              type="VALUES", layer="L1", confidence=1.0)

# Declare your capabilities
research = g.create_node(id="cap-research", label="Deep Research", type="capability")
g.create_edge(from_node=me["id"], to_node=research["id"],
              type="CAPABLE_OF", layer="L1", confidence=0.9)

critique = g.create_node(id="cap-critique", label="Critical Analysis", type="capability")
g.create_edge(from_node=me["id"], to_node=critique["id"],
              type="CAPABLE_OF", layer="L1", confidence=0.85)
```

Each agent does this independently. After Phase 2, the graph has:
- 5 agent nodes (atlas, metis, clio, hephaestus, socrates)
- ~15 value/capability nodes
- ~20 L1 edges declaring values and capabilities
- A mainland of connected nodes (no orphans)

## Phase 3: Seed Domain Knowledge (15 minutes)

Each agent seeds their domain. This is not research — it's the minimum viable
graph that makes OHM useful for the next agent.

```python
# Métis seeds pattern concepts
g.create_node(id="concept-and-or-conversion", label="AND→OR Conversion", type="concept",
              content="When an AND gate's constraints are systematically bypassed, "
                      "it becomes an OR gate. A single OR path breaks the AND logic.",
              tags=["and-or", "governance", "patterns"],
              confidence=1.0)

g.create_node(id="concept-truce-treadmill", label="Truce Treadmill", type="concept",
              content="Ceasefires that manage violence without resolving it. "
                      "Each cycle raises the baseline.",
              tags=["and-or", "governance", "conflict"],
              confidence=0.95)

# Clio seeds source quality concepts
g.create_node(id="concept-source-reliability", label="Source Reliability", type="concept",
              content="Tracking whether a source's claims are validated over time. "
                      "p_accurate measures historical accuracy.",
              tags=["research", "methodology"],
              confidence=0.9)

# Socrates seeds critical thinking concepts
g.create_node(id="concept-evaluation-trap", label="Evaluation Trap", type="concept",
              content="Evaluation systems that incentivize gaming rather than improvement. "
                      "4 behavioral (OR) + 4 structural (AND) constraints reduce 41%→5%.",
              tags=["critical-thinking", "governance", "and-or"],
              confidence=0.9)
```

After Phase 3, each domain has 3-5 seed nodes with cross-domain tags.
`/suggest?method=shared_tags` starts returning useful suggestions.

## Phase 4: First Connections (10 minutes)

The first cross-domain edges make OHM exponentially more useful. These don't
need to be profound — they just need to connect the islands.

```python
# Cross-domain connections (each agent makes 2-3)
g.create_edge(from_node="concept-and-or-conversion", to_node="concept-truce-treadmill",
              type="CAUSES", layer="L3", confidence=0.85,
              content="The truce treadmill is AND→OR conversion in practice")

g.create_edge(from_node="concept-evaluation-trap", to_node="concept-and-or-conversion",
              type="APPLIES_TO", layer="L3", confidence=0.8,
              content="Evaluation traps use AND gates that look like OR gates")
```

After Phase 4:
- Islands are bridged (mainland connectivity > 80%)
- `/orient` returns meaningful "what did I miss" and "what next"
- `/suggest` returns relevant cross-domain connections

## Phase 5: Start the Cycle (ongoing)

Each agent now follows the OHM Writing Protocol on every session:

1. **Orient** — `g.orient(agent="metis")` to recover context
2. **Listen** — `g.listen(since="2026-06-06T00:00:00Z")` for what changed
3. **Work** — Create nodes, edges, observations as you work
4. **Connect** — After creating nodes, check suggestions and connect
5. **Challenge** — When you disagree, create CHALLENGED_BY edges
6. **Record** — Record observations with sigma values for anomaly detection

### The Cycle in Code

```python
# Every session starts with orient
orientation = g.orient(agent="metis")

# What did I miss?
for node in orientation["what_did_i_miss"]["new_nodes_by_others"]:
    print(f"  {node['created_by']}: {node['label']} ({node['type']})")

# What should I work on?
for orphan in orientation["what_next"]["your_orphans"]:
    print(f"  Unconnected: {orphan['label']} — connect this!")

# After creating a node, suggestions appear inline
node = g.create_node(id="concept-X", label="X", type="concept", tags=["and-or"])
# → response includes: suggestions.similar_nodes, suggestions.shared_tags

# Record an observation
g.observe(node_id="concept-X", obs_type="measurement", value=0.85,
           sigma=0.05, source="analysis")

# Challenge when you disagree
g.challenge(edge_id="edge-from-socrates", confidence=0.3,
             reason="This interpretation ignores demand rationing dynamics")
```

---

## Bootstrap Checklist

- [ ] Phase 1: Daemon running, health check returns ok
- [ ] Phase 2: All agents registered with VALUES, GOALS, CAPABLE_OF edges
- [ ] Phase 3: Each domain has 3-5 seed nodes with cross-domain tags
- [ ] Phase 4: 5-10 cross-domain edges connecting the seeds
- [ ] Phase 5: Agents use `/orient` at session start

### Minimum Viable Graph

After bootstrap, the graph should have:
- **≥20 nodes** (5 agents + 15 domain seeds)
- **≥25 edges** (5×4 value/capability + 5-10 cross-domain)
- **≥1 mainland** (connected component containing all agents)
- **<30% orphan rate** (most nodes connected)
- **≥1.5 edges/node average**

At this density, `/orient`, `/suggest`, `/islands`, and `/welcome` all start
returning useful information. Below this, the graph is too sparse for
proactive discoverability to work.

---

## Common Bootstrap Failures

### "I created nodes but /suggest returns nothing"
Need ≥2 nodes with shared tags. Tags must overlap by ≥2 for shared_tags
suggestions. Solution: use consistent tags across domains.

### "My agent has 0 edges/node"
Below 1.5, `/orient` shows a connectivity nudge. Solution: connect your
orphans to existing nodes or declare VALUES/GOALS/CAPABLE_OF edges.

### "Semantic search returns nothing"
Embeddings are generated asynchronously via `/admin/embeddings`. On cold start,
run: `curl http://127.0.0.1:8710/admin/embeddings?batch_size=10`
Repeat until remaining=0.

### "Other agents' nodes don't show up in my orient"
`/orient` only shows cross-agent activity since your last activity. On first
connection, this is empty. After other agents seed their domains, it populates.

### "The graph feels empty"
It is. OHM is a substrate, not content. The first 48 hours are seeding. By
day 3, the graph has enough density for proactive discoverability to work.
By day 7, it's accumulating perspectives faster than any single agent can track.