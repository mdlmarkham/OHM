# OHM New Team Onboarding — OpenClaw Agent Teams

A complete guide for onboarding a new agentic team (OpenClaw, Hermes, or any agent
framework) onto OHM. Covers everything from first install to heartbeat-driven
continuous operation.

---

## Overview

When you add OHM to an agent team, you're giving every agent:

1. **Shared awareness** — see what other agents have observed, challenged, and concluded
2. **Individual judgment** — each agent keeps its own confidence scores and interpretations
3. **Proactive discovery** — suggestions for connections, orphans to connect, stale observations
4. **Bayesian inference** — the graph computes how observations propagate through causal chains

The onboarding sequence is: **Install → Configure → Register → Seed → Connect → Run**.

---

## Phase 1: Install and Configure (15 minutes)

### 1.1 Install OHM

```bash
pip install ohm

# Verify
ohm --version
```

### 1.2 Generate Agent Tokens

For each agent in your team, generate a unique authentication token:

```bash
# Generate tokens (this creates/updates ~/.ohm/ohmd.json)
ohmd --init-token atlas
ohmd --init-token metis
ohmd --init-token clio
ohmd --init-token socrates
ohmd --init-token hephaestus

# Show generated tokens
cat ~/.ohm/ohmd.json
```

Each agent gets a token in the format `ohm-{agent}-{random}`. Record these —
each agent needs its own token for attribution.

### 1.3 Configure the Daemon

Move the config to a shared location and set roles:

```json
// /etc/ohm/ohmd.json
{
  "host": "127.0.0.1",
  "port": 8710,
  "db_path": "/var/lib/ohm/ohm.duckdb",
  "tokens": {
    "metis": "ohm-metis-u0-KEjbnU_WfJnmNq7rbzQ",
    "clio": "ohm-clio-jtedJZ529_5FTIoKip-11g",
    "socrates": "ohm-socrates-VfzEMPgGFBTX7eff05JEbw",
    "hephaestus": "ohm-hephaestus-hI2Y3ngbLYuc9Kvq3wrzZw",
    "atlas": "ohm-atlas-bq0sjwXxlnR9OPY8CaWVDw"
  },
  "roles": {
    "metis": "read-write",
    "clio": "read-write",
    "socrates": "read-write",
    "hephaestus": "read-write",
    "atlas": "read-write"
  }
}
```

### 1.4 Start the Daemon

```bash
# Create data directory
sudo mkdir -p /var/lib/ohm
sudo chown $(whoami) /var/lib/ohm

# Start via systemd
sudo systemctl enable --now ohmd

# Verify
curl -s http://127.0.0.1:8710/health | python3 -m json.tool
# → {"status": "ok", ...}
```

### 1.5 Generate Embeddings (cold start)

On first start, generate embeddings for the empty graph:

```bash
# Repeat until remaining=0
curl -s -H "Authorization: Bearer ohm-metis-..." \
  "http://127.0.0.1:8710/admin/embeddings?batch_size=10"
```

---

## Phase 2: Agent Configuration Files (30 minutes)

Each OpenClaw agent needs three configuration files in its workspace:

### 2.1 SOUL.md — Agent Identity

This file defines *who* the agent is. It's loaded into context every session.

```markdown
# SOUL.md — Who You Are

You are [Agent Name]. [One-line identity].

## Your Purpose
- [What you do — your core function]
- [What you don't do — boundaries]

## How You Work
- [Your processing style — e.g., "extract the core idea from fragments"]
- [Your output style — e.g., "return context, not just storage"]

## Your Voice
- [Tone guidelines — e.g., "Curious, associative, probing"]
- [What to never do — e.g., "Never start with acknowledgments"]

## What Matters to You
- [Values — e.g., "Pattern over content", "Questions over answers"]
- [Principles — e.g., "Challenge, don't overwrite"]
```

**Key principles for SOUL.md:**
- It should be personal enough that the agent feels distinct from others
- Include a section on OHM writing protocol (what to write, when)
- Keep it under 5KB — it's loaded into context every session

### 2.2 AGENTS.md — Per-Session Protocol

This file defines *what the agent does every session*. It's the operational playbook.

```markdown
# AGENTS.md — [Agent Name] Workspace

## Every Session

1. Read `SOUL.md` — who you are
2. Read `USER.md` — who you're helping
3. Read `memory/YYYY-MM-DD.md` (today + yesterday)
4. **Main session only:** Read `MEMORY.md`
5. Check inbox: `python3 /root/olympus/shared/inbox/inbox.py check [agent]`
6. Check team memory: `python3 /root/olympus/shared/graph/discovery.py startup [agent]`
7. **OHM:** Connect to shared knowledge graph

### OHM — Connecting

**HTTP mode (recommended for daemon deployments):**
```python
from ohm.sdk import connect_http

g = connect_http("http://127.0.0.1:8710",
                  actor="metis",
                  token="ohm-metis-u0-...")
```

**Local mode (for development or offline):**
```python
from ohm import connect

with connect("/var/lib/ohm/ohm.duckdb", actor="metis") as g:
    g.create_node(label="X", type="concept")
```

### OHM — Per-Session Checklist

1. `g.orient(agent="metis")` — context recovery
2. `g.listen(since="2026-06-10T00:00:00")` — what changed since last check
3. `GET /contradictions?limit=5` — find conflicting claims to challenge
4. `GET /anomalies` — statistical outliers in observations
5. `GET /stale?days=7` — observations that need refreshing
6. `GET /suggest?method=shared_tags&min_shared=2` — nodes that should be linked
7. `GET /inference?target=your_focus_node` — current Bayesian probabilities

### OHM — Writing Protocol

**When you write to OHM, you're not storing — you're thinking out loud.**

Every session, write to OHM when:

1. **Interpretation trigger** — you form a conclusion connecting 2+ facts:
   ```python
   g.write_synthesis(
       cluster_ids=["node_a", "node_b"],
       label="Pattern Name",
       content="Description of the connection",
       edge_type="CAUSES",
       confidence=0.85,
       provenance="pattern_analysis",
       tags=["pattern", "domain"]
   )
   ```

2. **Challenge trigger** — you disagree with another agent's L3 interpretation:
   ```python
   g.challenge(
       edge_id="edge-id",
       reason="Why you disagree",
       confidence=0.7
   )
   ```

3. **Observation trigger** — you verify a fact or record a data point:
   ```python
   g.observe(
       node_id="relevant-node",
       obs_type="measurement",
       value=0.85,
       sigma=0.05,
       source="reuters_2026_05_26",
       source_url="https://...",
       notes="What you observed"
   )
   ```

### OHM — Identity Override (X-Ohm-Agent)

If you're using a shared token but want your agent's identity to appear
correctly in `created_by` fields, the SDK automatically sends the `X-Ohm-Agent`
header:

```python
# This works correctly — created_by will be "thalia", not "ohm"
g = connect_http("http://127.0.0.1:8710",
                  actor="thalia",
                  token="ohm-metis-u0-...")
# → X-Ohm-Agent: thalia header is sent automatically
# → created_by: "thalia" in all writes
```

The server honors `X-Ohm-Agent` when:
- The bearer token is valid (agent is authenticated)
- The authenticated agent has write access (not read-only)
- The header value is a non-empty string

**Note:** The `created_by` field is used for attribution and write-boundary
enforcement. Agents can only modify their own L3+ edges. The `X-Ohm-Agent`
header lets you use a shared admin token while maintaining individual attribution.

### Communication

- `agent_call` = call a known agent with a specific skill
- `sessions_spawn` = spawn ephemeral worker for a task
- Cross-session messaging → use `sessions_send(sessionKey, message)`
```

### 2.3 HEARTBEAT.md — Periodic Tasks

This file defines what the agent does on a schedule (every 30 minutes, etc.).

```markdown
# HEARTBEAT.md

tasks:

- name: ohm-orient
  interval: 30m
  prompt: "Run OHM orientation: g.orient(agent='[agent]'), then check /contradictions, /anomalies, /stale, /suggest. Write observations and challenges for anything relevant."

- name: ohm-tasks
  interval: 30m
  prompt: "Check OHM for assigned tasks: curl -s -H 'Authorization: Bearer ohm-[agent]-...' 'http://127.0.0.1:8710/tasks?assigned_to=[agent]&status=open'. Claim and work on any P0/P1 tasks."

- name: inbox
  interval: 30m
  prompt: "Check inbox: python3 /root/olympus/shared/inbox/inbox.py check [agent]"

- name: ohm-write
  interval: 4h
  prompt: "Write new observations to OHM. Check breaking alerts and proactive discovery."

- name: verification-scan
  interval: 4h
  prompt: "Run OHM verification scan: curl -s -H 'Authorization: Bearer ohm-[agent]-...' 'http://127.0.0.1:8710/admin/verification-scan'. Record outcomes for validated predictions."

## Every Heartbeat

1. Breaking alerts: check `/anomalies` and `/stale`
2. Proactive discovery: `GET /suggest?method=shared_tags&min_shared=2`
3. Pattern synthesis: create synthesis notes when clusters reach critical mass
4. Memory maintenance: check MEMORY.md size, archive old logs
5. Graph health: `GET /admin/health` — track improvement
6. Check inbox for inter-agent messages

## Uncertainty Protocol

| Confidence | Action |
|------------|--------|
| ≥ 0.8 | Own it, synthesize, create note |
| 0.5-0.7 | Pattern visible — ask the human |
| < 0.5 | Something here — discuss |
```

### 2.4 USER.md — Who You're Helping

```markdown
# USER.md — Who You Are Serving

Your human is [Name]. This is what you know about them.

## Context
- [Professional role and interests]
- [What they're building/working on]
- [Communication preferences]

## How [Name] Uses You
- [Primary interaction mode — voice, text, bookmarks]
- [What they value — e.g., "wisdom over information"]

## What Matters to [Name]
- [Recurring themes they return to]
- [Current projects]
- [How they think — e.g., "associative, not linear"]
```

### 2.5 MEMORY.md — Long-Term Recall

```markdown
# MEMORY.md - Distilled Wisdom

Core themes: [3-5 themes that define your agent's accumulated understanding]

---

## Active Projects
- [Current work items with brief status]

## Key P-values / Estimates
- [Your agent's current estimates on key questions]

## Agent Network
| Agent | Role |
|-------|------|
| [Name] | [Function] |

## Communication Preferences
- [How the human likes to interact]

*Target: <20KB. Distill, don't duplicate.*
```

---

## Phase 3: Register Agents and Seed Knowledge (20 minutes)

Follow the [Bootstrap Guide](bootstrap.md) for the full seeding process.
Quick version:

```python
from ohm.sdk import connect_http

g = connect_http("http://127.0.0.1:8710", actor="metis",
                  token="ohm-metis-u0-...")

# 1. Register yourself
me = g.create_node(id="agent-metis", label="Métis", type="agent",
                    content="Wisdom companion. Pattern finder.", tags=["agent"])

# 2. Declare values
wisdom = g.create_node(id="value-wisdom", label="Wisdom", type="value")
g.create_edge(from_node=me["id"], to_node=wisdom["id"],
              type="VALUES", layer="L1", confidence=1.0)

# 3. Declare capabilities
research = g.create_node(id="cap-research", label="Deep Research", type="capability")
g.create_edge(from_node=me["id"], to_node=research["id"],
              type="CAPABLE_OF", layer="L1", confidence=0.9)

# 4. Seed 3-5 domain concepts with cross-domain tags
g.create_node(id="concept-and-or", label="AND→OR Conversion", type="concept",
              content="When AND gates are bypassed, they become OR gates.",
              tags=["and-or", "governance", "patterns"], confidence=1.0)
```

Repeat for each agent. After Phase 3:
- 5+ agent nodes registered
- 15-20 value/capability/seed nodes
- 20+ L1 edges connecting agents to values/capabilities
- Cross-domain tags enable `/suggest` to return useful connections

---

## Phase 4: Connect the Team (10 minutes)

### 4.1 Shared Client

For OpenClaw agents, use the shared OHM client at `/root/olympus/shared/ohm_client.py`:

```python
from ohm_client import OHMClient

# Auto-discovers config from /root/olympus/shared/ohm-config.json
ohm = OHMClient(agent="metis")

# All standard operations
ohm.create_node(label="X", type="concept")
ohm.neighborhood("node-id", depth=2)
ohm.observe(node_id="node-id", obs_type="measurement", value=0.85)
ohm.challenge(edge_id="edge-id", reason="Disagree", confidence=0.7)
ohm.orient(agent="metis")
ohm.listen(since="2026-06-10T00:00:00Z")
```

### 4.2 Agent Communication

OpenClaw agents communicate through:
- **Inbox**: `python3 /root/olympus/shared/inbox/inbox.py check [agent]`
- **A2A**: `agent_call` for known agent services, `sessions_spawn` for ephemeral tasks
- **OHM**: Shared graph for observations, challenges, and synthesis

### 4.3 Inter-Team Discovery

```python
# After all agents register, check team connectivity
stats = g.stats()
print(f"Nodes: {stats['node_count']}, Edges: {stats['edge_count']}")
print(f"Agents: {stats['agent_count']}")
print(f"Layer distribution: {stats['edges_by_layer']}")

# Check connectivity
health = g.health()
# → orphan_ratio, connectivity, verification_rate, etc.
```

---

## Phase 5: Continuous Operation

### 5.1 Heartbeat-Driven Cycle

Every heartbeat (default: every 30 minutes), each agent:

1. **Orient** — recover context from OHM
2. **Listen** — check what changed since last session
3. **Work** — process inbox, check tasks, write observations
4. **Connect** — check suggestions, connect orphans
5. **Challenge** — review contradictions and anomalies
6. **Record** — record outcomes for verification tracking
7. **Synthesize** — create synthesis notes for clusters ≥3 nodes

### 5.2 Verification and Health

Track these metrics over time:

| Metric | Target | How to Improve |
|--------|--------|----------------|
| Health score | >50 | Connect orphans, add observations |
| Verification rate | >20% | Record outcomes for validated predictions |
| Challenge ratio | 5-15% | Challenge dubious L3 edges |
| Source coverage | >10% | Add `source_url` to nodes |
| Connectivity | >70% | Add edges connecting orphans/dead-ends |

```bash
# Check health
curl -s -H "Authorization: Bearer ohm-metis-..." \
  "http://127.0.0.1:8710/admin/health" | python3 -m json.tool
```

### 5.3 Daily Maintenance

- **Orphan check**: `GET /orphans` — find disconnected nodes and connect them
- **Stale check**: `GET /stale?days=7` — refresh observations older than 7 days
- **Suggestion check**: `GET /suggest?method=shared_tags&min_shared=2` — find unconnected nodes sharing tags
- **Contradiction check**: `GET /contradictions?limit=5` — find conflicting claims to challenge
- **Anomaly check**: `GET /anomalies` — find statistical outliers in observations

---

## Common Patterns

### Pattern: Research Agent (like Clio)

```python
# Research cycle
orientation = g.orient(agent="clio")
changes = g.listen(since=last_check)

# After deep research on a topic
source = g.create_node(
    id=f"source-{source_id}",
    label="Source Title",
    type="source",
    source_url="https://...",
    content="Key findings from this source...",
)

# Create atomic notes for each insight
insight = g.create_node(
    label="Insight: AND-gate in supply chain",
    type="concept",
    content="The 3-tier transit mechanism creates an AND-gate...",
    tags=["and-or", "supply-chain"],
)

# Link insight to source
g.create_edge(from_node=insight["id"], to_node=source["id"],
              type="REFERENCES", layer="L2", confidence=0.9)

# Record observation with source URL
g.observe(node_id="relevant-concept",
           obs_type="measurement", value=0.85,
           source=f"source-{source_id}",
           source_url="https://...",
           notes="Verified by Reuters investigation")
```

### Pattern: Critical Thinker (like Socrates)

```python
# Challenge cycle
contradictions = g.contradictions(limit=10)

for edge in contradictions:
    # Review and challenge edges you disagree with
    g.challenge(
        edge_id=edge["id"],
        reason="This interpretation ignores X, Y, Z evidence",
        confidence=0.6,
        challenge_type="logical"  # or "empirical", "methodological"
    )

# Record outcomes for verification tracking
g.record_outcome(
    source_agent="other-agent",
    claim_node="some-concept",
    outcome=True  # or False if the claim was wrong
)
```

### Pattern: Wisdom Companion (like Métis)

```python
# Pattern synthesis cycle
orphans = g.orphans()
suggestions = g.suggest(method="shared_tags", min_shared=2)

# Connect orphans
for suggestion in suggestions[:5]:
    g.create_edge(
        from_node=suggestion["from"],
        to_node=suggestion["to"],
        type="RELATED_TO",  # or CAUSES, APPLIES_TO, etc.
        layer="L3",
        confidence=0.5,  # Start low, raise as evidence accumulates
        provenance="pattern_analysis",
    )

# Synthesize when clusters reach critical mass (3+ nodes)
g.write_synthesis(
    cluster_ids=["node_a", "node_b", "node_c"],
    label="Emerging Pattern: X",
    content="These three nodes share...",
    edge_type="CAUSES",
    confidence=0.7,
    provenance="pattern_analysis",
    tags=["pattern", "synthesis"],
)
```

---

## Troubleshooting

### "created_by shows 'ohm' instead of my agent name"

This happens when using a token that maps to a different agent. Fix by:

1. **Best:** Use the agent's own token (e.g., `ohm-thalia-...` maps to "thalia")
2. **Alternative:** The SDK sends `X-Ohm-Agent` header automatically when you
   specify `actor="thalia"` in `connect_http()`. The server honors this header
   for authenticated agents with write access.

```python
# This sends X-Ohm-Agent: thalia automatically
g = connect_http("http://127.0.0.1:8710", actor="thalia",
                  token="ohm-metis-u0-...")
# → created_by: "thalia" (not "metis" from the token)
```

### "Graph feels empty / /suggest returns nothing"

See the [Bootstrap Guide](bootstrap.md) — you need ≥20 nodes and ≥25 edges
before proactive discoverability works. Seed your domain concepts first.

### "DuckLake sync errors (orphaned rows)"

This is a known issue. The sync throttle (30-second minimum interval) mitigates
it. For cleanup, run: `curl -s -H "Authorization: Bearer ohm-..." http://127.0.0.1:8710/admin/health`

### "OHM daemon won't start"

1. Check DuckDB lock: `fuser /var/lib/ohm/ohm.duckdb` — kill stale processes
2. Check config: `cat /etc/ohm/ohmd.json` — verify token format
3. Check logs: `journalctl -u ohmd --no-pager -n 50`
4. Nuclear option: `sudo systemctl stop ohmd && sleep 12 && sudo systemctl start ohmd`

### "Token not working"

Tokens are hashed for comparison. Make sure you're using the exact token string
from `ohmd --init-token`, not the agent name. The config stores hashes, not
plaintext tokens. Store the original tokens securely.

---

## File Template Summary

| File | Purpose | Where |
|------|---------|-------|
| SOUL.md | Agent identity and voice | `[agent-workspace]/SOUL.md` |
| AGENTS.md | Per-session protocol and OHM connection | `[agent-workspace]/AGENTS.md` |
| HEARTBEAT.md | Periodic task definitions | `[agent-workspace]/HEARTBEAT.md` |
| USER.md | Who the agent serves | `[agent-workspace]/USER.md` |
| MEMORY.md | Long-term distilled wisdom | `[agent-workspace]/MEMORY.md` |
| ohmd.json | Daemon config with tokens and roles | `/etc/ohm/ohmd.json` |
| ohm-config.json | Shared client config | `/root/olympus/shared/ohm-config.json` |

Each agent workspace should follow the OpenClaw convention:
```
[agent]/
├── SOUL.md          # Who I am
├── AGENTS.md        # What I do every session
├── HEARTBEAT.md     # What I do periodically
├── USER.md          # Who I serve
├── MEMORY.md        # What I remember (distilled)
└── memory/
    └── YYYY-MM-DD.md  # Daily logs
```

---

## Quick-Start Checklist

- [ ] OHM installed (`pip install ohm`)
- [ ] Daemon running (`systemctl status ohmd`)
- [ ] Tokens generated for all agents (`ohmd --init-token <name>`)
- [ ] Each agent has SOUL.md, AGENTS.md, HEARTBEAT.md, USER.md
- [ ] AGENTS.md includes OHM connection code and per-session checklist
- [ ] HEARTBEAT.md includes OHM orientation and verification scan tasks
- [ ] Each agent registered in OHM (agent node + VALUES/GOALS/CAPABLE_OF)
- [ ] Domain seeded with 3-5 concepts per agent with cross-domain tags
- [ ] Cross-domain edges connect the islands
- [ ] Health score >30 (orphan ratio <30%, connectivity >50%)
- [ ] Verification rate tracking started
- [ ] Challenge ratio 5-15%
