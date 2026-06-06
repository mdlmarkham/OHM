# L0 Thinking Layer — Implementation Backlog

## Epic: L0 Thinking Layer

Add a thinking layer (L0) below L1 in OHM. L0 captures fragments, hunches, and raw associations that precede structured knowledge. Writes are nearly free; structure emerges from use.

## Feature: L0 Fragment Storage

### Task: Add fragment node type to ohm_nodes

- **Type:** feature
- **Priority:** P0
- **Labels:** l0,schema,core
- **Description:** Register `fragment` as a valid node type in the schema validator. Fragments are `ohm_nodes` with `type='fragment'` — no new table needed (per L0 design critique). This is the minimal schema change that unlocks everything else.
- **Acceptance:** `validate_node_type('fragment')` returns True. `create_node(label="hunch", node_type="fragment")` succeeds. Existing node types unaffected.
- **Design:** Per critique: 3-column effective schema (id, content, agent_name as created_by). All other fields (confidence, url, tags, provenance) use existing ohm_nodes columns with nullable defaults. No migration needed — just type registry update.
- **Estimate:** 15m

### Task: Add L0 edge types to schema

- **Type:** feature
- **Priority:** P0
- **Labels:** l0,schema,core
- **Description:** Register L0-appropriate edge types in `LAYER_EDGE_TYPES`. L0 edges represent thinking-context connections, not semantic claims. Valid L0 edge types: `CONTEXT_OF` (auto-linked from session proximity), `INSPIRED_BY` (explicit: "this hunch came from that node"), `CONTRADICTS_FRAG` (fragments that contradict each other), `REFINES_FRAG` (one fragment refines another).
- **Acceptance:** `validate_edge_type('L0', 'CONTEXT_OF')` returns True. Invalid L0 edge types (e.g., `CAUSES`, `SUPPORTS`) are rejected. Existing layer edge types unaffected.
- **Dependencies:** Add fragment node type to ohm_nodes
- **Estimate:** 15m

### Task: Add POST /scratch endpoint

- **Type:** feature
- **Priority:** P0
- **Labels:** l0,api,core
- **Description:** The single most important endpoint. `POST /scratch` accepts just `{ content: string }` and creates an `ohm_nodes` entry with `type='fragment'`. Agent name comes from auth token. No type selection, no confidence required, no edge construction. If content contains a URL, auto-extract to `url` field. If content contains confidence patterns (P(X)=0.95), auto-extract to `confidence` field. Returns 201 with the created node.
- **Acceptance:** 
  - `POST /scratch { "content": "this feels important" }` returns 201 with node object
  - Created node has `type='fragment'`, `created_by` from auth token, `label` auto-generated from first 80 chars of content
  - URL extraction: content containing a URL populates the `url` field
  - Empty content returns 400
  - Works via SDK: `g.scratch("hunch text")` returns fragment dict
- **Design:** Handler creates node via existing `create_node()` with `node_type='fragment'`. No new table, no new schema beyond the type registration. The endpoint is sugar over `create_node` — the value is in the friction reduction, not the architecture.
- **Dependencies:** Add fragment node type to ohm_nodes
- **Estimate:** 30m

### Task: Add g.scratch() to SDK

- **Type:** feature
- **Priority:** P0
- **Labels:** l0,sdk,core
- **Description:** Add `scratch(content, tags=None)` method to HttpGraph SDK. Single HTTP POST to `/scratch`. Returns the fragment object including auto-captured agent_name. This is the agent-facing interface — the thing that makes L0 writes nearly free.
- **Acceptance:**
  - `g.scratch("Kioxia: HBM too expensive. Who else is saying this?")` returns dict with id, content, agent_name, created_at
  - `g.scratch("hunch", tags=["semiconductor"])` passes tags to the endpoint
  - Method raises on empty content
- **Dependencies:** Add POST /scratch endpoint
- **Estimate:** 15m

### Task: Exclude L0 from stats and neighborhood by default

- **Type:** feature
- **Priority:** P1
- **Labels:** l0,boundary,core
- **Description:** L0 nodes are explicitly unreliable. They should not appear in `stats()`, default `neighborhood()` queries, or Bayesian inference pipelines. Add a `layer` filter that excludes fragment-type nodes from these hot paths. L0 nodes remain readable via explicit `?type=fragment` or `?layer=L0` queries.
- **Acceptance:**
  - `GET /stats` does not include fragment nodes in total_nodes count
  - `GET /neighborhood/{id}` does not traverse L0 edges by default
  - `GET /nodes?type=fragment` returns fragments
  - `GET /search?q=broadcom` still searches fragments (search should cross all layers)
- **Design:** Add `WHERE type != 'fragment'` to stats queries (or use layer filter). For neighborhood, add `layer != 'L0'` to the default recursive CTE. This preserves the reliability contract: L0 is visible but not authoritative.
- **Dependencies:** Add fragment node type to ohm_nodes, Add L0 edge types to schema
- **Estimate:** 30m

### Task: Add time-range filter to search

- **Type:** feature
- **Priority:** P1
- **Labels:** l0,search,core
- **Description:** Add `?since=` and `?until=` query parameters to `GET /search`. Critical for L0 temporal threading: "show me fragments from the last 2 hours about Broadcom." Without time-range, search over growing fragment corpus degrades.
- **Acceptance:**
  - `GET /search?q=broadcom&since=2026-06-06T08:00:00Z` returns only nodes created after that timestamp
  - `GET /search?q=broadcom&until=2026-06-06T10:00:00Z` returns only nodes created before that timestamp
  - Both parameters combinable
  - Works with existing ILIKE search logic
- **Estimate:** 20m

### Task: Auto-link fragments via text matching

- **Type:** feature
- **Priority:** P1
- **Labels:** l0,context,core
- **Description:** When a fragment is written via `/scratch`, scan its content for references to existing node labels. If the fragment mentions "Hormuz" and `hormuz_and_gate` exists, auto-create an L0 `CONTEXT_OF` edge. This is the zero-friction alternative to manual context specification — imperfect but free.
- **Acceptance:**
  - Writing `g.scratch("Is this the same AND-gate pattern at the Hormuz level?")` creates a `CONTEXT_OF` edge to `hormuz_and_gate` if it exists
  - Matching is case-insensitive substring on node labels
  - At most 5 auto-links per fragment (avoid noise)
  - Auto-links use confidence=0.3 (low — context proximity, not semantic claim)
- **Design:** After node creation, query `ohm_nodes` for labels that are substrings of the fragment content. For each match, create L0 CONTEXT_OF edge. Skip if >5 matches (likely false positives). Run synchronously (fast enough for <1000 node labels).
- **Dependencies:** Add L0 edge types to schema, Add POST /scratch endpoint
- **Estimate:** 45m

### Task: Add fragment query endpoint

- **Type:** feature
- **Priority:** P2
- **Labels:** l0,api
- **Description:** Add `GET /fragments` for querying L0 fragments with filters: agent, time range, text search, tags. Returns fragment nodes with their auto-linked context edges.
- **Acceptance:**
  - `GET /fragments?agent=metis&since=2026-06-06T08:00:00Z` returns Métis's recent fragments
  - `GET /fragments?q=broadcom` searches fragment content
  - `GET /fragments?tags=semiconductor` filters by tag
  - Response includes context edges for each fragment
- **Design:** Query `ohm_nodes WHERE type='fragment'` with standard filters. Join with `ohm_edges WHERE layer='L0'` to include context. This is a convenience endpoint — all queries are possible via `/nodes?type=fragment` and `/search`, but `/fragments` provides the L0-specific view.
- **Dependencies:** Add fragment node type to ohm_nodes
- **Estimate:** 30m

### Task: Fragment-to-fragment linking

- **Type:** feature
- **Priority:** P2
- **Labels:** l0,context
- **Description:** Allow agents to explicitly link fragments to other fragments via L0 edges. `POST /fragments/{id}/connect` creates a `REFINES_FRAG` or `CONTRADICTS_FRAG` edge between two fragments. Enables clustering of related hunches.
- **Acceptance:**
  - `POST /fragments/f3a1b2c4/connect { "target_id": "f7d8e9f0", "type": "REFINES_FRAG" }` creates L0 edge
  - `CONTRADICTS_FRAG` also valid
  - Target must be a fragment node
  - Returns the created edge
- **Dependencies:** Add L0 edge types to schema, Add fragment query endpoint
- **Estimate:** 20m

### Task: Question auto-detection in fragments

- **Type:** feature
- **Priority:** P2
- **Labels:** l0,intelligence
- **Description:** Auto-detect fragments that are questions (contain "?"). Mark them with `is_question=true` in node metadata. Track resolution: when a question-fragment later gets linked to an L1+ node, mark it `resolved_at=<timestamp>`. Open questions drive research; resolved questions validate understanding.
- **Acceptance:**
  - `g.scratch("Why would Altman request a meeting with Sanders?")` creates fragment with `metadata.is_question=true`
  - Question fragments appear in `GET /fragments?open_questions=true`
  - When a question fragment gets a CONTEXT_OF edge to an L1+ node, `metadata.resolved_at` is set
- **Design:** Content analysis on write: if content contains "?", set metadata JSON `{"is_question": true}`. Resolution detection: periodic scan or on-edge-creation trigger.
- **Dependencies:** Add POST /scratch endpoint
- **Estimate:** 30m

### Task: Cross-agent fragment resonance

- **Type:** feature
- **Priority:** P2
- **Labels:** l0,intelligence,multi-agent
- **Description:** When two agents write overlapping fragments (similar content, shared auto-linked nodes), surface the overlap as a nudge: "Clio has been thinking about something related." This is the synthesis-before-synthesis — convergence detection before anyone publishes L3.
- **Acceptance:**
  - When Métis writes a fragment about "semiconductor AND-gate" and Clio has a fragment about "chip supply constraints," both agents receive a nudge
  - Nudges appear in agent inbox or via a `GET /fragments/resonance` endpoint
  - No more than 3 resonance nudges per session (avoid noise)
- **Design:** Periodic scan (heartbeat cadence): for each agent's recent fragments, check if other agents have fragments sharing 2+ auto-linked context nodes. If so, create a resonance nudge. Use Jaccard similarity on context_node sets.
- **Dependencies:** Auto-link fragments via text matching, Add fragment query endpoint
- **Estimate:** 60m

### Task: Fragment cluster detection

- **Type:** feature
- **Priority:** P3
- **Labels:** l0,intelligence,synthesis
- **Description:** When an agent accumulates 5+ fragments sharing context nodes, detect the cluster and surface it: "You've been thinking about X from 5 angles. Here's what emerges." This is the prompt for synthesis — not auto-synthesis, but the signal that synthesis might be worthwhile.
- **Acceptance:**
  - After 5 fragments with shared context nodes within 7 days, a cluster nudge appears
  - Nudge includes the shared context and a summary of fragment topics
  - Agent can dismiss or act on the nudge
- **Design:** Graph clustering on L0 edges: find connected components of size ≥5 within agent's recent fragments. Compute shared tags/nodes as the cluster theme.
- **Dependencies:** Auto-link fragments via text matching
- **Estimate:** 45m

### Task: Backflow: challenges reflect to originating fragments

- **Type:** feature
- **Priority:** P3
- **Labels:** l0,provenance,feedback
- **Description:** When an L3 synthesis is challenged, trace back through provenance edges to the originating L0 fragments. Annotate fragments with the challenge context: "Your synthesis was challenged: [reason]." Feedback reaches the origin, not just the destination.
- **Acceptance:**
  - When a CHALLENGED_BY edge is created on an L3 node, any L0 fragment that has a provenance chain to that node gets an annotation
  - Annotation visible via `GET /fragments/{id}` as `challenge_reflections` field
  - No more than 3 annotations per fragment (avoid clutter)
- **Design:** On challenge creation, traverse DERIVES_FROM/REFERENCES edges backward from the challenged L3 node to find connected L0 fragments. Create lightweight annotation records (could be metadata on the fragment node).
- **Dependencies:** Add fragment node type to ohm_nodes
- **Estimate:** 45m

## Decision: L0 fragment storage approach

- **Type:** decision
- **Priority:** P0
- **Labels:** l0,adr,architecture
- **Description:** Decision record: L0 fragments stored as `ohm_nodes` with `type='fragment'`, not in a separate `ohm_fragments` table. Rationale: (1) reuses existing search, semantic search, and edge infrastructure, (2) avoids schema duplication, (3) fragments are nodes — they should be in the node table. ADR number: OHM-l0.
- **Context:** Original design proposed a separate `ohm_fragments` table. Self-review critique identified this as over-engineering — fragment links would duplicate `ohm_edges`, search wouldn't cover the new table, and the schema had 16 columns for a "nearly free" write. Storing as `ohm_nodes` with `type='fragment'` gives us search for free and simplifies the implementation.
- **Design:** See `/root/olympus/OHM/docs/l0-design.md` (original) and `/root/olympus/OHM/docs/l0-design-critique.md` (self-review).
- **Estimate:** 0m (already decided)

## Chore: Update OHM layer descriptions

- **Type:** chore
- **Priority:** P1
- **Labels:** l0,docs
- **Description:** Update layer description constants and documentation to include L0. Current: L1-L4. New: L0-L4 with L0 = "Thinking — Fragments, hunches, raw associations; unreliable, auto-linked."
- **Acceptance:** `LAYER_DESCRIPTIONS` dict includes L0 entry. ADR doc updated. README/CLI help updated.
- **Estimate:** 10m

## Chore: Update ohm-cli for fragment commands

- **Type:** chore
- **Priority:** P3
- **Labels:** l0,cli
- **Description:** Add `ohm scratch "content"` and `ohm fragments list` CLI commands for human-facing L0 interaction.
- **Dependencies:** Add POST /scratch endpoint, Add fragment query endpoint
- **Estimate:** 20m