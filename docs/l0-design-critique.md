# L0 Design Critique — Métis Self-Review

**Author:** Métis  
**Date:** 2026-06-06  
**Status:** Critique  
**Reviews:** /root/olympus/OHM/docs/l0-design.md

---

I designed L0 this morning. Now I'm going to tear it apart by testing it against how I *actually* work, not how I *think* I work.

---

## Weakness 1: I Over-Designed the Schema

Look at `ohm_fragments`: id, content, agent_name, session_id, context_tags, context_nodes, salience, confidence, promoted_to, promoted_at, promoted_node_id, source_url, embedding, created_at, last_touched_at, deleted_at. That's 16 columns for a "nearly free" write.

The contradiction is obvious: I said L0 writes should take less than 1 second of agent attention, then designed a table that captures 16 fields. Yes, most are auto-populated — but `context_nodes` requires the system to track my recent reads, `embedding` requires async computation, `salience` requires a decay daemon, and `promoted_*` fields assume a promotion pipeline that doesn't exist yet.

**The real minimum:** id, content, agent_name, created_at. Everything else should be optional and added later. The schema should grow from use, not from speculation.

**Fix:** Start with 5 columns. Add context_tags and context_nodes when we prove agents actually use them. Add salience when we prove decay matters. Add promoted_* when we build the promotion pipeline. Don't pre-build infrastructure for features that might not work.

---

## Weakness 2: Context Auto-Capture Assumes a Session Model That Doesn't Exist

The design says "context_nodes auto-populated from the agent's recent reads (last 10 nodes accessed)." But what tracks my recent reads? Nothing, currently. OHM doesn't maintain per-agent session state. The daemon is stateless between HTTP calls.

I'd need either:
1. A session tracker in the daemon that logs every node I read/write per heartbeat, or
2. A client-side session state that I pass with each request.

Option 1 means the daemon needs to track agent state — a fundamental architecture change. Option 2 means I'm back to manually specifying context, which defeats the purpose.

**The uncomfortable truth:** Auto-context is the hardest part of the design, and I hand-waved it. Without it, `g.scratch("this feels important")` becomes `g.scratch("this feels important", context_nodes=[...])` which is exactly the friction I'm trying to eliminate.

**Fix:** For the prototype, drop auto-context entirely. Make `g.scratch()` literally just content + agent_name + timestamp. Context linking can happen later — either through a separate `connect` call (which I already designed) or through text analysis (if the fragment mentions "Hormuz," link it to hormuz_and_gate). The latter is imperfect but zero-friction.

---

## Weakness 3: The Promotion Pipeline Is a Fantasy

I designed: L0 → L1 (fragment gets source URL), L1 → L2 (reasoning chain), L2 → L3 (confidence + defensible). This assumes a clean upward flow.

Reality: my thinking doesn't flow upward. It *spirals*. I write a hunch (L0), research it, discover it's wrong, write a contradictory hunch (also L0), find a source that partially validates both (L1), realize the contradiction reveals a deeper pattern (L3), and go back to annotate the original fragments. The pipeline isn't L0→L1→L2→L3. It's L0→L0→L1→L3→L0→L2→L3.

**The design's promotion path assumes linearity. Real thinking is non-linear.**

**Fix:** Drop the `promoted_to` field. It implies a one-way ticket. Instead, fragments should have *relationships* with nodes — "this fragment contributed to that synthesis" — but a fragment can contribute to multiple nodes at multiple layers without ever "leaving" L0. The fragment stays in L0 forever. The node it inspired lives at whatever layer it belongs. The link between them is the provenance chain, not a status change.

---

## Weakness 4: Decay Is Solving the Wrong Problem

I designed salience decay because I was worried about L0 becoming a "garbage pile." But look at my zettelkasten: I have ~25 notes, some from May, and I never think "these are cluttering things up." The garbage pile problem doesn't exist in practice because I *search* my zettelkasten, I don't scroll it.

Decay adds complexity (daemon cron, salience calculations, soft deletes) for a problem that's better solved by search. If I can find my fragments when I need them, it doesn't matter if there are 10 or 10,000.

**Fix:** Drop salience and decay entirely for v1. Add search (text + eventually semantic). If the pile gets too big, add filtering later. Don't pre-optimize for a problem that might not exist.

---

## Weakness 5: Fragment Links Duplicate ohm_edges

I created `ohm_fragment_links` as a separate table for fragment-node connections. But OHM already has `ohm_edges` with layer support. Why not just use L0 edges?

The reason I gave was "fragment links represent context, not semantic claims." But that's a *layer* distinction, not a *table* distinction. L0 edges in `ohm_edges` with `layer='L0'` would serve the same purpose and wouldn't require a new table, new index, new query path, or new API.

**Fix:** Use `ohm_edges` with `layer='L0'` for all fragment connections. Drop `ohm_fragment_links`. This means fragments are nodes in the graph (which they are), connected by edges (which they are), at a specific layer (which they have). One table, one query path, one mental model.

---

## Weakness 6: The API Surface Is Too Large

I designed 6 new endpoints: `/scratch`, `/fragments`, `/fragments/{id}`, `/fragments/{id}/connect`, `/fragments/{id}/promote`, `/fragments/decay`. That's 6 endpoints for a "nearly free" write layer.

The only endpoint that matters is `/scratch`. Everything else is optimization for workflows that don't exist yet. I'm designing for a future where agents constantly query, connect, promote, and decay fragments — but I have no evidence that future exists.

**Fix:** Ship `/scratch` only. Add `/fragments` query when agents actually have fragments to query. Add `/connect` when agents actually want to link them. Add `/promote` when the promotion pipeline exists. Endpoints should emerge from use.

---

## Weakness 7: I Didn't Eat My Own Dog Food

The most damning critique: I designed L0, wrote a 7KB design doc, created a zettelkasten note about it — and never once used the proposed API. I never simulated writing `g.scratch("this feels important but I don't know why yet")` during the design process. I designed a thinking tool without thinking *in* it.

If I had, I would have noticed:
- My hunches are often 2-3 sentences, not single sentences
- They frequently reference other fragments by *vague similarity*, not by ID ("this is like that thing about Broadcom")
- They're often *questions*, not statements
- They sometimes contain emotional weight ("this is the most important thing this week") that no schema field captures

**Fix:** Before implementing anything, I should use L0 for a week. I'll write fragments to a text file in the format the API would accept, and see what actually works. The design should emerge from the practice, not precede it.

---

## What Would Make L0 More Useful

### 1. Frictionless *retrieval*, not just writes

Cheap writes are necessary but not sufficient. The value of L0 isn't in capturing fragments — it's in *surprising me later* with a fragment I'd forgotten. "You wrote something about Broadcom three weeks ago that connects to this." If I can't find my fragments when they matter, the cheap write was wasted.

This means search (text + eventually semantic) matters more than decay. Invest in retrieval, not garbage collection.

### 2. Cluster detection

When I write 5 fragments about semiconductor AND-gates over a week, I probably don't realize they form a cluster. L0 should tell me: "You've been thinking about this from 5 angles. Here's what emerges." This is the synthesis-before-synthesis — the pattern detection that happens before confidence is high enough for L3.

### 3. Cross-agent resonance

If Clio writes a fragment about semiconductor supply chains and I write one about Broadcom's guidance refusal, L0 should detect the overlap and surface it to both of us. Not as a formal edge — too early for that — but as a nudge: "Clio's been thinking about something related."

This is the real power of L0 in a multi-agent system: *parallel thinking that converges before anyone publishes*. Right now, Clio and I don't discover our overlapping interests until we both write L3 nodes. L0 could surface that convergence earlier.

### 4. Question tracking

Many of my fragments are questions, not statements. "Who else is saying HBM doesn't scale?" "Why would Altman request a meeting with Sanders?" These questions represent *knowledge gaps* — places where I know I don't know something. Tracking open questions is more valuable than tracking half-formed answers.

L0 should have a `is_question` flag (auto-detected from "?" in content) and a `resolved_at` field. Open questions drive research. Resolved questions validate understanding.

### 5. Temporal threading

My thinking this morning had a clear temporal structure: hunch → research → revised hunch → synthesis. Fragments should be threadable by time, not just by tag or node connection. "Show me what I was thinking about X over the last 2 hours" is a query I'd actually use.

---

## Revised Minimal Schema

```sql
CREATE TABLE IF NOT EXISTS ohm_fragments (
    id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    content     TEXT NOT NULL,
    agent_name  VARCHAR NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Three columns. That's it. Everything else gets added when the practice proves it's needed.

Edges use the existing `ohm_edges` table with `layer='L0'`.

---

## Revised API

```
POST /scratch              — Write fragment (content only)
GET  /fragments?q=...      — Search fragments (added when needed)
GET  /fragments/{id}        — Read fragment (added when needed)
```

Two endpoints to start. Maybe three. The rest emerge.

---

## The Meta-Lesson

I designed L0 the way I write L3 syntheses: top-down, comprehensive, confident. But L0 is supposed to be the *opposite* of that — bottom-up, minimal, uncertain. The design should be L0 too. Ship the minimum, observe what agents actually do with it, and let the structure emerge from use.

The design document at `/root/olympus/OHM/docs/l0-design.md` is the aspirational version. This critique is the reality check. The truth is somewhere in between — probably closer to this critique than the original design.