# ADR-0044: First-Class Correction Workflow with Immutable-Node Supersession

**Date:** 2026-07-16
**Status:** Accepted
**Related issues:** OHM-959 (this work), ADR-003 (agent-owned edges — correction edges inherit ownership), ADR-018 (cross-link + verification loops — corrections feed the decay split), ADR-028 (source tier ceilings — correction confidence is tier-capped), ADR-040/041 (prospect lifecycle + SUPERSEDES precedent this generalizes)

## Context

OHM's graph is append-only by design. ADR-003 makes every L3/L4 edge
agent-owned and immutable; ADR-018 requires derived-claim nodes to cross-link
existing nodes and enforces a 14-day verification loop with 30d/365d confidence
decay. This preserves audit trail and perspective diversity — but it leaves a
gap: when an agent discovers that an existing node is *factually wrong* (wrong
probability, wrong source attribution, wrong causal claim, stale value), there
is no systematic way to record the correction. The options today are:

1. **In-place edit** — silently overwrites the wrong value, destroying the
   audit trail and hiding that a correction ever happened. Violates ADR-003's
   immutability principle.
2. **CHALLENGED_BY edge** — questions *confidence* on an edge, but does not
   assert a replacement value or mark a *node* as superseded. A challenge says
   "I doubt this"; a correction says "this is wrong, here is the right value."
3. **NEGATES edge (ADR-009)** — eliminates a candidate from consideration
   (medical rule-out), but carries no replacement node and no lifecycle.
4. **Ad hoc** — create a new node and hope someone finds it. No link
   semantics, no lifecycle, no calibration feed.

None of these produce a *first-class correction object* that is itself
challengeable, carries a replacement, feeds source-reliability calibration,
and leaves the original node immutable for audit. Issue OHM-959 asks for
exactly this.

A precedent already exists: the L4 `prospect` lifecycle (ADR-040/041) uses a
`SUPERSEDES` edge (`src/ohm/graph/schema.py:439`) and a
`proposed → committed → superseded` status chain (`schema.py:572-578`) to
replace one prospect with a newer one without deleting the old. Observation
supersession chains exist in `src/ohm/graph/decay.py:supersede_observation`.
This ADR generalizes that pattern from prospects/observations to *any* node.

## Decision

Ship a two-phase correction workflow. **Option A** (minimal, MCP-only
prototype using the existing `decision` node type with structured metadata)
lands now; **Option B** (a native `correction` node type with first-class
query/discovery and server-side authority enforcement) is the validated
follow-up.

### 1. The correction model (both phases)

A correction is a *first-class graph object*, not an edit. It consists of:

- A **correction node** (Option A: a `decision` node with
  `metadata.correction = {...}`; Option B: a `correction` node type) that
  carries the replacement value, the reason, and the evidence reference.
- A **CORRECTS** edge (L3) from the correction node to the old (incorrect)
  node. New edge type. Agent-owned (ADR-003): each agent files their own
  correction.
- A **SUPERSEDES** edge (L4) from the *new* node (the replacement node, when
  one is created) to the old node. Reuses the existing `SUPERSEDES` edge type
  (`schema.py:439`), generalizing its prospect-only documentation to "new
  node replaces old node (reason in metadata)."
- A **correction status lifecycle** stored in the correction node's metadata:
  `proposed → committed | rejected`. This mirrors the `prospect` lifecycle
  (`proposed`/`committed`/`superseded` in `schema.py:572-578`) and the
  suggestion lifecycle (`ripe → promoted | expired | rejected`, ADR-036).

```
   ┌─────────────┐  CORRECTS (L3)   ┌──────────────┐
   │ correction  │ ──────────────▶ │  old node    │ (immutable, stays)
   │   node      │                  │  (wrong)     │
   └──────┬──────┘                  └──────────────┘
          │ SUPERSEDES (L4)                ▲
          ▼                                │ SUPERSEDES (L4)
   ┌──────────────┐                        │  (if a separate new node is created)
   │  new node    │ ───────────────────────┘
   │  (correct)   │
   └──────────────┘
```

When the correction *amends in place* (e.g., fixes a probability on an edge
rather than replacing a node), only the CORRECTS edge is created; the
correction node's metadata carries the field-level diff. When the correction
*replaces a node*, both CORRECTS (correction→old) and SUPERSEDES (new→old)
are created.

### 2. Option A — minimal, MCP-only prototype (ships now)

- **Node type**: reuse the existing `decision` node type (`schema.py:65`).
  `decision` is already in `MUST_HAVE_EDGE_NODE_TYPES` (`schema.py:254`), so a
  correction decision *must* reference an existing node via `connects_to` —
  which is exactly the cross-link we want (the correction must point at the
  node it corrects). No DDL.
- **Metadata contract**: `decision.metadata.correction = { "status":
  "proposed"|"committed"|"rejected", "old_node_id": "...", "field":
  "probability"|"label"|"source_url"|..., "old_value": ..., "new_value": ...,
  "evidence_node_ids": [...], "corrected_at": ISO8601, "corrected_by":
  agent }`.
- **New edge type**: add `CORRECTS` to `LAYER_EDGE_TYPES["L3"]`
  (`schema.py:362`). Validated in application code, no DDL (same pattern as
  ADR-038's version-only migration). `SCHEMA_VERSION` bump to `0.58.0` as a
  sentinel.
- **SUPERSEDES reuse**: no schema change — the edge type already exists in L4
  (`schema.py:439`). Update its docstring from "new prospect replaces old
  prospect" to "new node replaces old node (prospect or corrected claim);
  reason in metadata."
- **MCP surface**: three new tools — `ohm_propose_correction`,
  `ohm_commit_correction`, `ohm_reject_correction` — routed through the
  existing MCP dispatch (`src/ohm/mcp/dispatch.py`) and dual routing
  (`src/ohm/server/server.py`), same pattern as ADR-038's temporal tools.
- **No new CLI/SDK surface in Phase A** — MCP-only, matching the "MCP is the
  primary agent interface" guidance. CLI/SDK wrappers arrive with Option B.

### 3. Correction status lifecycle

| Status | Meaning | Transition rule |
|--------|---------|-----------------|
| `proposed` | Correction filed, awaiting review | Set on creation. Visible to other agents. Old node still authoritative. |
| `committed` | Correction accepted; new node/field is authoritative | Requires evidence + authority check (below). Activates the `SUPERSEDES` edge. Feeds calibration. |
| `rejected` | Correction withdrawn or overruled | Sets status; correction node remains for audit. A `CHALLENGED_BY` edge may explain the rejection. |

The lifecycle reuses the `proposed`/`committed`/`superseded` vocabulary
already in `VALID_TASK_STATUSES` (`schema.py:572-578`) so query tooling that
filters on prospect status picks up corrections for free. `rejected` is new
but in Option A is a metadata string (`decision.metadata.correction.status`),
not a `VALID_TASK_STATUSES` entry.

### 4. Guardrails

- **Evidence required**: a correction cannot transition to `committed`
  without at least one `evidence_node_ids` entry. Enforced in the MCP
  handler. This is the ADR-018 cross-link requirement applied to corrections —
  a correction with no evidence is a bare opinion.
- **Confidence ceilings**: the correction node's `confidence` is capped by
  its `source_tier` per ADR-028 (`raw` 0.3, `unverified` 0.5, `preliminary`
  0.7, `official` 0.9, `verified` 1.0). A correction from a single Reddit
  thread cannot override a peer-reviewed claim at confidence 0.95.
- **Cooling-off**: a `proposed` correction cannot transition to `committed`
  for a configurable window (default 24h, `OHM_CORRECTION_COOLING_OFF_HOURS`).
  Prevents drive-by commits; gives other agents a window to file a
  `CHALLENGED_BY` on the correction itself. Corrections are first-class graph
  objects, so they are *challengeable* — another agent can `CHALLENGED_BY` the
  CORRECTS edge if they disagree the old node is wrong.
- **Authority checks**: only the original node's author, a domain authority,
  or an agent with `correction:commit` scope (an ADR-037 read-scope parallel)
  may transition `proposed → committed`. The proposing agent may always
  transition `proposed → rejected` (withdraw). This mirrors ADR-003's
  boundary enforcement extended to the correction lifecycle.

### 5. Source-reliability calibration feed

When a correction is `committed`, the system records an outcome against the
*original* node's author via the existing `record_outcome` path
(`src/ohm/graph/queries/changefeed.py:query_record_outcome`, exposed as the
`ohm_record_verification_outcome` MCP tool per ADR-038):

- `source_agent = old_node.created_by`
- `claim_node = old_node.id`
- `outcome = False` (the original claim was falsified by the correction)

This feeds `source_reliability()` (`query_source_reliability`) and the
30d/365d decay split (ADR-018): an agent whose claims get corrected loses
reliability, and their uncorrected claims decay faster. Conversely, the
correcting agent's reliability rises if their correction survives its own
challenge window. This closes the loop: corrections are not just annotations
— they are *training signal* for the calibration system
(`src/ohm/graph/calibration.py:empirical_half_life` already learns from
supersession history).

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `decision` node type (`schema.py:65`) | Option A reuses it as the correction carrier; `MUST_HAVE_EDGE_NODE_TYPES` (`schema.py:254`) already forces a cross-link to the corrected node |
| `SUPERSEDES` L4 edge (`schema.py:439`) | Reused — generalizes from prospect-only to any-node replacement |
| `prospect` lifecycle `proposed`/`committed`/`superseded` (`schema.py:572-578`) | Vocabulary reused for correction status |
| `supersede_observation` (`decay.py:246`) | Precedent for supersession chains without deletion |
| ADR-036 suggestion lifecycle `ripe → promoted \| expired \| rejected` | Precedent for a staging lifecycle with auto-promote; corrections adopt the same `rejected` terminal |
| `record_outcome` / `source_reliability` (`changefeed.py:667`, `:950`) | Calibration feed — committed corrections record a `False` outcome against the original author |
| ADR-028 source tier ceilings | Caps correction confidence by source quality |
| ADR-018 verification loops + 30d/365d decay | Corrections are the `False` outcome that triggers the faster 30d decay on the original author |
| ADR-003 agent-owned edges | CORRECTS edges are agent-owned; multiple agents can independently correct the same node |
| ADR-009 NEGATES | NEGATES eliminates a candidate; CORRECTS replaces a value/node. Distinct semantics, complementary. |

## Consequences

**Positive:**
- Corrections are first-class graph objects — discoverable, challengeable,
  and auditable. The original node stays immutable (ADR-003 preserved); the
  correction is a separate node + edges.
- Corrections feed source-reliability calibration automatically via
  `record_outcome`, closing the ADR-018 loop: agents who file accurate
  corrections gain reliability; agents whose claims get corrected lose it.
- Reuses existing primitives (`decision` node, `SUPERSEDES` edge,
  `proposed`/`committed` lifecycle, `record_outcome`) — Option A is a
  metadata contract + one new edge type + three MCP tools, not a schema
  overhaul.
- Corrections are themselves challengeable: a `CHALLENGED_BY` on the
  CORRECTS edge lets a third agent dispute the correction, preserving
  perspective diversity (ADR-003).
- The cooling-off window prevents drive-by commits and gives the original
  author a chance to respond.

**Negative:**
- Option A has weaker discoverability: corrections live as `decision` nodes
  with `metadata.correction`, so a generic "show me all corrections" query
  must filter on `node_type='decision' AND metadata.correction IS NOT NULL`.
  A native `correction` node type (Option B) would make this a first-class
  query. Mitigated by the MCP tool surface, which is the primary agent
  interface.
- Option A overloads the `decision` node type. A `decision` node is
  documented as "action selection, utility optimization, policy nodes"
  (`schema.py:132-137`); corrections are not decisions in that sense. The
  `metadata.correction` discriminator avoids semantic collision but adds a
  hidden subtype. Option B removes this.
- The cooling-off window adds latency to corrections; a 24h default may be
  too long for fast-moving domains (cybersecurity incidents) and too short
  for slow ones (clinical claims). The env var
  `OHM_CORRECTION_COOLING_OFF_HOURS` is the escape hatch; per-domain tuning
  is deferred to Option B.
- Authority checks in Option A are enforced in the MCP handler only — a
  direct SDK/SQL caller can bypass them. Full enforcement requires Option
  B's server-side boundary (parallel to ADR-003's
  `enforce_write_boundary`).

## Alternatives considered

1. **In-place editing of the wrong node.** Mutate the node's fields and bump
   `updated_at`. Rejected: it destroys the audit trail (no record that a
   correction happened, no record of the old value, no record of who
   corrected it or why), violates ADR-003's immutability principle, and gives
   the calibration system no `False` outcome to learn from. The whole point
   of OHM's append-only graph is that wrong claims remain visible and
   *annotated*, not silently overwritten.

2. **Option B only — ship the native `correction` node type immediately.**
   Add `correction` to `VALID_NODE_TYPES`, a `correction_status` column to
   `ohm_nodes`, first-class query/discovery endpoints, server-side authority
   enforcement, and CLI/SDK wrappers — all before validating the workflow
   with real agents. Rejected: too much upfront schema and surface work
   before field validation. The prospect lifecycle (ADR-040/041) and
   suggestion lifecycle (ADR-036) both shipped as lightweight pilots first
   and generalized after usage; corrections should follow the same path.
   Option A validates the model (CORRECTS/SUPERSEDES semantics, lifecycle,
   guardrails, calibration feed) with minimal schema cost; Option B
   promotes the validated contract to a first-class type.

3. **Use CHALLENGED_BY + a new replacement node, no dedicated correction
   type.** File a `CHALLENGED_BY` edge on the old node's outgoing edges and
   create a new node with the right value, linking them with `RELATED_TO`.
   Rejected: `CHALLENGED_BY` questions *confidence on an edge*, not
   *correctness of a node*, and carries no replacement value or lifecycle.
   `RELATED_TO` is too weak to express supersession. The result would be
   corrections that are undiscoverable, have no lifecycle, and do not feed
   calibration — exactly the ad hoc state we have today.

## References

- Issue: OHM-959 — First-class correction workflow with immutable-node
  supersession
- Prior work: ADR-003 (agent-owned edges), ADR-009 (NEGATES — complementary
  rule-out), ADR-018 (cross-link + verification decay), ADR-028 (source tier
  ceilings), ADR-036 (suggestion lifecycle precedent), ADR-037 (read scopes
  — authority model), ADR-038 (MCP tool surface pattern), ADR-040/041
  (prospect lifecycle + SUPERSEDES precedent)
- Source:
  - `src/ohm/graph/schema.py:65` (`decision` node type), `:254`
    (`MUST_HAVE_EDGE_NODE_TYPES` includes `decision`), `:362-417` (L3 edge
    types — `CORRECTS` to be added), `:439` (`SUPERSEDES` L4 edge),
    `:572-578` (`proposed`/`committed`/`superseded` lifecycle)
  - `src/ohm/graph/decay.py:246` (`supersede_observation` — supersession
    chain precedent)
  - `src/ohm/graph/calibration.py:36` (`empirical_half_life` — learns from
    supersession)
  - `src/ohm/graph/queries/changefeed.py:667` (`query_record_outcome`),
    `:950` (`query_source_reliability`)
  - `src/ohm/mcp/dispatch.py` (MCP dispatch — `ohm_propose_correction` /
    `ohm_commit_correction` / `ohm_reject_correction` to be added)
  - `src/ohm/server/server.py` (dual routing registration, per ADR-038
    pattern)
