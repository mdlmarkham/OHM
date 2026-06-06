# L0 Design Document — The Thinking Layer

**Author:** Métis  
**Date:** 2026-06-06  
**Status:** Draft  
**ADR:** OHM-l0 (proposed)

---

## Problem

Agents jump straight to L3 writes. The messy thinking that produces syntheses — hunches, fragments, half-connections, contradictions — never enters OHM. It stays in zettelkasten notes or evaporates entirely. This means:

1. **Syntheses appear without provenance.** An L3 node surfaces a conclusion, but the reasoning trail that produced it is invisible. Other agents can't challenge the premises because the premises aren't in the graph.
2. **Translation tax kills lower-layer writes.** Writing an L3 node requires the same API overhead as any other node — type validation, confidence values, cross-link requirements. The cost is acceptable for publishable knowledge. It's unacceptable for "I have a feeling about this."
3. **The layer model is underutilized.** L1–L4 exist but agents treat them as address labels on nodes, not as a pipeline from thinking to knowledge. L1 is supposed to be "structure" but in practice it's just where sources and events go. The thinking never gets captured.

## Solution

Add L0 — a thinking layer below L1. L0 is explicitly unreliable, cheap to write, and self-promoting.

```
L0 (thinking)  → fragments, hunches, raw associations, contradictions
L1 (structure) → facts, events, sources — "what happened"
L2 (flow)      → citations, reasoning chains — "why I connected these"
L3 (knowledge) → interpretations, syntheses — "what I believe"
L4 (prospects) → predictions, scenarios — "what I expect"
```

## Design Principles

### 1. L0 writes must be nearly free

The write cost for an L0 fragment should be close to zero — both cognitive and computational. An agent in the middle of reasoning should be able to emit a fragment without:
- Choosing a node type
- Assigning a confidence value
- Finding a node to connect it to
- Constructing a valid edge
- Worrying about whether it's "good enough" for the graph

If writing an L0 fragment takes more than 1 second of agent attention, the design has failed.

### 2. Context is automatic, not manual

When an agent writes an L0 fragment, the system should automatically associate it with:
- The agent's current session
- Other nodes the agent has recently read or written
- The timestamp
- Any tags the agent is currently working with

The agent should never have to specify these manually. Proximity IS the connection.

### 3. L0 is explicitly unreliable

L0 nodes carry no reliability contract. Other agents should be able to read them (for awareness) but should never build decisions on them. L0 is the kitchen — you can look, but you don't eat until the food reaches the table (L1+).

This means:
- L0 nodes do NOT appear in `stats()` or neighborhood queries by default
- L0 nodes do NOT trigger Bayesian inference
- L0 nodes CAN be read by other agents for situational awareness
- L0 nodes CAN be challenged, but challenges at L0 are "you might want to look at X" rather than "you're wrong"

### 4. Promotion is organic, not bureaucratic

L0 nodes promote upward when they accumulate structure:
- **L0 → L1:** Fragment gets a source URL, or is linked from a verified event/source
- **L1 → L2:** Structure node gets a reasoning chain (why A connects to B)
- **L2 → L3:** Reasoned connection gets a confidence value and is defensible

Promotion happens through *use*, not through a form. If an agent revisits a fragment, adds evidence, connects it to other structure — it promotes. If a fragment is never revisited, it decays.

### 5. Decay is natural

L0 fragments that are never connected to anything should fade. Not deleted — just deprioritized. After 30 days with no connections or revisits, an L0 fragment's `salience` drops toward zero. It's still searchable but won't appear in any active queries.

This prevents the "garbage pile" problem. L0 accepts everything, but time and neglect filter what matters.

---

## Schema

### New table: `ohm_fragments`

```sql
CREATE TABLE IF NOT EXISTS ohm_fragments (
    id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    content         TEXT NOT NULL,                    -- The fragment text
    agent_name      VARCHAR NOT NULL,                 -- Who wrote it
    session_id      VARCHAR,                          -- Current session context
    context_tags    JSON DEFAULT '[]',                -- Auto-captured from session
    context_nodes   JSON DEFAULT '[]',                -- Node IDs agent was working with
    salience        FLOAT DEFAULT 1.0,                -- Decay score (1.0 = fresh, 0.0 = stale)
    confidence      FLOAT,                            -- NULL = no confidence assigned yet
    promoted_to     VARCHAR,                           -- 'L1', 'L2', 'L3' when promoted
    promoted_at     TIMESTAMP,                         -- When promotion happened
    promoted_node_id VARCHAR,                          -- ID of the node created at promotion
    source_url      TEXT,                              -- Optional: if fragment has a source
    embedding       FLOAT[768],                       -- For semantic search within L0
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_touched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, -- Updated on revisit/link
    deleted_at      TIMESTAMP                          -- Soft delete
);
```

### New table: `ohm_fragment_links`

Implicit links between fragments and nodes. These represent "this fragment was written while thinking about that node" — NOT explicit semantic claims.

```sql
CREATE TABLE IF NOT EXISTS ohm_fragment_links (
    id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    fragment_id     VARCHAR NOT NULL REFERENCES ohm_fragments(id),
    node_id         VARCHAR,                          -- Can link to ohm_nodes OR other ohm_fragments
    link_type       VARCHAR NOT NULL DEFAULT 'context', -- 'context' (auto) or 'explicit' (agent-specified)
    strength        FLOAT DEFAULT 0.5,                -- 'context' links default to 0.5, 'explicit' to 1.0
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Index changes

```sql
CREATE INDEX IF NOT EXISTS idx_fragments_agent ON ohm_fragments(agent_name);
CREATE INDEX IF NOT EXISTS idx_fragments_session ON ohm_fragments(session_id);
CREATE INDEX IF NOT EXISTS idx_fragments_salience ON ohm_fragments(salience) WHERE salience > 0.1;
CREATE INDEX IF NOT EXISTS idx_fragments_promoted ON ohm_fragments(promoted_to) WHERE promoted_to IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_fragment_links_fragment ON ohm_fragment_links(fragment_id);
CREATE INDEX IF NOT EXISTS idx_fragment_links_node ON ohm_fragment_links(node_id);
```

---

## API

### `POST /scratch` — Write an L0 fragment

The single most important endpoint. Everything else is secondary.

**Request:**
```json
{
    "content": "Broadcom didn't miss — they refused to raise. That's different.",
    "tags": ["semiconductor", "AND-OR"]    // optional
}
```

**Response:**
```json
{
    "id": "f3a1b2c4",
    "content": "Broadcom didn't miss — they refused to raise. That's different.",
    "agent_name": "metis",
    "session_id": "sess-20260606-morning",
    "context_tags": ["semiconductor", "AND-OR"],
    "context_nodes": ["hormuz_and_gate", "event-semi-crash-2026-06-05"],
    "salience": 1.0,
    "created_at": "2026-06-06T09:30:00Z"
}
```

**Behavior:**
- `agent_name` comes from auth token — never specified in body
- `session_id` comes from agent's current session state — if not available, auto-generated
- `context_tags` merged with agent's current working tags from session
- `context_nodes` auto-populated from the agent's recent reads (last 10 nodes accessed)
- If `content` contains a URL, auto-extract to `source_url`
- If `content` contains confidence-like patterns (`P(X) = 0.95`, `confidence: 0.8`), auto-extract to `confidence`
- Returns 201 on success. No validation beyond non-empty content.

### `GET /fragments` — Query L0 fragments

**Parameters:**
- `agent` — filter by agent (default: all)
- `session` — filter by session
- `tags` — filter by tags (any match)
- `q` — text search
- `min_salience` — minimum salience (default: 0.1, to exclude decayed)
- `limit` — max results (default: 20)
- `include_promoted` — include fragments that have been promoted (default: false)

**Response:** List of fragment objects.

### `GET /fragments/{id}` — Read single fragment

Includes its context links (what nodes the agent was working with).

### `POST /fragments/{id}/connect` — Add explicit link

**Request:**
```json
{
    "node_id": "hormuz_and_gate",
    "note": "Same AND-gate pattern at fab level"
}
```

Creates an `explicit` link (strength 1.0) with the optional note. This is the action that triggers promotion evaluation — if a fragment now has explicit links + a source URL, it's a candidate for L1 promotion.

### `POST /fragments/{id}/promote` — Promote fragment to L1+

**Request:**
```json
{
    "type": "event",            // ohm_nodes type
    "label": "Broadcom Guidance Refusal",  // required
    "connects_to": "event-semi-crash-2026-06-05"  // required for cross-link policy
}
```

**Behavior:**
1. Creates an ohm_nodes entry at the specified type with the fragment's content
2. Copies source_url, confidence, and tags from the fragment
3. Creates REFERENCES edges from the new node to all context-linked nodes
4. Updates the fragment: `promoted_to = 'L1'`, `promoted_at = now()`, `promoted_node_id = new_node_id`
5. The fragment becomes a permanent provenance record for the promoted node

### `POST /fragments/decay` — Run decay sweep

Decay all L0 fragments based on age and connection count. Called by the daemon periodically (daily?).

**Algorithm:**
```
age_days = (now - last_touched_at) / 86400
connection_count = count of fragment_links for this fragment
decay_factor = 0.95 ^ age_days  // ~30 day half-life
connection_bonus = min(connection_count * 0.1, 0.5)  // connections slow decay
new_salience = max(decay_factor + connection_bonus - 0.5, 0.0)  // floor at 0
```

Fragments with `salience < 0.05` are soft-deleted.

---

## SDK Method

### `g.scratch(content, tags=None)`

```python
from ohm.sdk import connect_http
g = connect_http("http://127.0.0.1:8710", actor="metis", token="...")

# The only thing you need to write:
g.scratch("Kioxia: HBM too expensive. Who else is saying this?")

# With optional tags:
g.scratch("Altman met Sanders. Why would Altman *request* that meeting?", 
          tags=["sovereign-wealth", "AND-OR"])

# Returns the fragment object, including auto-captured context
```

**Implementation:** Single HTTP POST to `/scratch`. No node type selection, no confidence assignment, no edge construction. The agent just writes what they're thinking.

---

## Promotion Pipeline

```
Fragment written (L0)
    │
    ├─ Agent revisits → last_touched_at updated → salience preserved
    ├─ Agent adds source_url → promotion candidate
    ├─ Agent adds explicit link → promotion candidate  
    ├─ Agent calls promote() → becomes L1 node
    │
    ├─ No activity → salience decays → eventually soft-deleted
    │
    └─ Another agent reads fragment → can suggest connections
       → "Have you considered linking this to [node]?"
```

**Auto-promotion trigger:** When a fragment has:
1. At least 1 explicit link to an existing node, AND
2. A source_url or a confidence value, AND
3. Has been touched within the last 7 days

The system can *suggest* promotion (via nudge), but never auto-promotes. The agent decides.

---

## Backflow: Challenges and Observations Flowing Down

When an L3 observation is challenged, the challenge should be visible not just on the L3 node but also on the L0 fragments that contributed to it.

**Implementation:** When a challenge edge is created at L3, the system traces the promoted_node_id back to the originating fragment and creates an annotation:

```python
# Pseudo-code in challenge handler
for fragment in fragments_promoted_to(challenged_node_id):
    annotate_fragment(fragment.id, type="challenge_reflection", 
                      content=f"Your synthesis was challenged: {challenge_reason}",
                      challenge_edge_id=edge.id)
```

This means the agent sees the challenge *where they were thinking*, not just on the published node. The feedback reaches the origin, not just the destination.

---

## Migration

### Schema version: 0.24.0

```python
(
    "0.24.0",
    "add L0 thinking layer — ohm_fragments and ohm_fragment_links tables",
    [
        """CREATE TABLE IF NOT EXISTS ohm_fragments (
        id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        content         TEXT NOT NULL,
        agent_name      VARCHAR NOT NULL,
        session_id      VARCHAR,
        context_tags    JSON DEFAULT '[]',
        context_nodes   JSON DEFAULT '[]',
        salience        FLOAT DEFAULT 1.0,
        confidence      FLOAT,
        promoted_to     VARCHAR,
        promoted_at     TIMESTAMP,
        promoted_node_id VARCHAR,
        source_url      TEXT,
        embedding       FLOAT[768],
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_touched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        deleted_at      TIMESTAMP
    )""",
        """CREATE TABLE IF NOT EXISTS ohm_fragment_links (
        id              VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
        fragment_id     VARCHAR NOT NULL,
        node_id         VARCHAR,
        link_type       VARCHAR NOT NULL DEFAULT 'context',
        strength        FLOAT DEFAULT 0.5,
        note            TEXT,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
        "CREATE INDEX IF NOT EXISTS idx_fragments_agent ON ohm_fragments(agent_name)",
        "CREATE INDEX IF NOT EXISTS idx_fragments_session ON ohm_fragments(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_fragments_salience ON ohm_fragments(salience) WHERE salience > 0.1",
        "CREATE INDEX IF NOT EXISTS idx_fragments_promoted ON ohm_fragments(promoted_to) WHERE promoted_to IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS idx_fragment_links_fragment ON ohm_fragment_links(fragment_id)",
        "CREATE INDEX IF NOT EXISTS idx_fragment_links_node ON ohm_fragment_links(node_id)",
    ],
)
```

### Schema config changes

```python
LAYER_EDGE_TYPES = {
    "L0": frozenset({"CONTEXT_OF", "INSPIRED_BY", "CONTRADICTS_FRAG", "REFINES_FRAG"}),
    "L1": frozenset({...}),  # existing
    ...
}

LAYER_DESCRIPTIONS = {
    "L0": "Thinking — Fragments, hunches, raw associations; unreliable, auto-linked",
    "L1": "Structure — Fully shared, all agents read/write",
    ...
}
```

### Server handler additions

New routes in `graph.py`:
- `POST /scratch` → `_post_scratch()`
- `GET /fragments` → `_get_fragments()`
- `GET /fragments/{id}` → `_get_fragment()`
- `POST /fragments/{id}/connect` → `_post_fragment_connect()`
- `POST /fragments/{id}/promote` → `_post_fragment_promote()`
- `POST /fragments/decay` → `_post_fragment_decay()`

### SDK method

```python
def scratch(self, content: str, tags: list[str] | None = None) -> dict:
    """Write an L0 thinking fragment. Returns the fragment object."""
    body = {"content": content}
    if tags:
        body["tags"] = tags
    return self._request("POST", "/scratch", json=body)
```

---

## What L0 Changes for Each Agent

**Métis:** I'd write 15-20 fragments per session instead of 0. The hunch stage currently vanishes. L0 captures it. My syntheses would have provenance chains traceable to the fragments that produced them.

**Clio:** Deep research generates dozens of observations before reaching conclusions. Currently she has to either write them all as L1 events (expensive, cluttering) or lose them. L0 is the natural place for "this source says X but I haven't verified it yet."

**Socrates:** Teaching and critical thinking exercises generate half-formed challenges. L0 lets Socrates note "this argument seems circular but I need to check premise 3" without committing to an L3 challenge edge.

**Hephaestus:** Code review observations like "this function looks like it has a race condition but I need to trace the call path" — L0 fragments, not L3 knowledge claims.

---

## What L0 Does NOT Do

- **Replace zettelkasten.** L0 is a capture layer, not a thinking workspace. My zettelkasten is where I draft, revise, and structure. L0 is where the fragments go so they're visible to the graph.
- **Auto-generate L3 syntheses.** L0 captures raw material. Promotion to L1/L2/L3 is agent-driven, with system nudges.
- **Solve the API friction problem on existing endpoints.** That's a separate fix. L0 adds a new cheap endpoint; existing endpoints should also get easier.
- **Replace the existing layer model.** L0 extends it downward. L1–L4 remain as-is.

---

## Open Questions

1. **Embedding generation for L0 fragments:** Should fragments get embeddings automatically for semantic search within L0? Pro: enables "show me fragments related to X." Con: adds latency to the scratch write. Recommendation: async embedding — write returns immediately, embedding computed in background.

2. **Cross-agent fragment visibility:** Should Clio be able to read Métis's L0 fragments? Pro: situational awareness, enables "I was thinking the same thing." Con: L0 is explicitly unreliable, and exposing raw thinking could create false confidence. Recommendation: readable but with clear visual labeling as "unreliable / thinking."

3. **Fragment-to-fragment links:** Should two fragments be linkable to each other? Pro: enables clustering of related hunches. Con: creates graph complexity in the layer that's supposed to be simple. Recommendation: yes, but only via `ohm_fragment_links` (not ohm_edges), and only with `link_type = 'explicit'` (never auto-generated).

4. **Session context persistence:** How long should session context (recent nodes, working tags) persist? Recommendation: session context lasts the duration of the agent's current heartbeat. If the agent restarts, session context resets.

5. **Nudge integration:** Should the existing ADR-017 nudge system suggest promotions? Pro: automated pipeline. Con: nudge fatigue. Recommendation: yes, but capped at 1 promotion nudge per session.

---

*This is a living document. Implementation begins after review.*