# Industrial Agent Manifesto — Conformance Checklist

**Status:** Accepted
**Issue:** OHM-dp38
**Date:** 2026-06-30
**Related ADRs:** ADR-003, ADR-018, ADR-028, ADR-029, ADR-030, ADR-033, ADR-035, ADR-036, ADR-037

A minimal set of principles that any agent operating in an industrial
decision-intelligence context (OpenClaw + OHM, Hermes + OHM, or any equivalent)
must satisfy. The principles are not aspirations — they are enforced by the
substrate. Each principle has a corresponding conformance test in
`tests/test_industrial_manifesto_conformance.py`.

The manifesto exists because industrial decisions compound failure. A missed
cross-link, a forged confidence, a recursive agreement that wasn't checked,
each is harmless in isolation. Together they cause an agent to act with
unearned certainty on a chemical reactor, a power grid, or a supply chain.
The principles are the floor.

---

## Principle 1 — Agent-owned edges

Every L3/L4 edge carries `created_by` and cannot be overwritten by another
agent. Other agents may `CHALLENGED_BY`, `SUPPORTS`, or `DERIVED_FROM` the
original — never mutate it. Confidence and probability are the original
author's judgment, preserved.

**Enforced by:** `enforce_write_boundary` in `ohm/boundary.py` (ADR-003).

## Principle 2 — Mandatory cross-link

Derived-claim nodes (`pattern`, `idea`, `task`, `decision`, `synthesis`,
`observation`, `interpretation`, `challenge`) must reference at least one
existing node. Bare claims cannot exist; they cannot be challenged, reached
from context, or propagated through inference. The server returns HTTP 422
with `error: "cross_link_required"` when violated.

**Enforced by:** `ohm/server/handlers/graph.py` synthesis validation
(OHM-tjzh, ADR-018).

## Principle 3 — Source tier ceilings

`source_tier` is a quality dimension, not a confidence alias. A 0.9 claim
from raw UGC is not the same as a 0.9 claim from a peer-reviewed replication.
When `source_tier` is set, confidence is capped at the tier ceiling:

| tier | ceiling |
|---|---|
| `raw` | 0.3 |
| `unverified` | 0.5 |
| `preliminary` | 0.7 |
| `official` | 0.9 |
| `verified` | 1.0 |

NULL bypasses enforcement (legacy write paths). Industrial agents should
opt in by populating `source_tier` on every write.

**Enforced by:** `enforce_confidence_ceiling` in
`ohm/framework/validation.py:213` (ADR-028).

## Principle 4 — AND-gate governance

Industrial decisions are AND-gates: a reactor requires feed stock AND
catalyst AND a calibrated temperature profile. The substrate supports this
via three columns:

- `gate_type` on `ohm_nodes` (`AND` | `OR` | NULL)
- `gate_status` on `ohm_nodes` (`intact` | `converted` | `compromised` |
  `failed`; `open` | `closed` | `stuck` as aliases)
- `constraint_expr` on `ohm_edges` (a boolean expression over upstream
  edge statuses)

`compromised` gate_status must propagate. A node with `gate_type=AND` and
`gate_status=compromised` cannot drive an autonomy-loop action.

**Enforced by:** schema validators and application-layer checks
(OHM-as17).

## Principle 5 — Verification decay

Unverified CAUSES/PREDICTS/EXPECTS edges decay with a 30-day half-life.
Verified edges (with at least one recorded outcome) decay with a 365-day
half-life. The `POST /heartbeat` response surfaces `verification_overdue`:
edges that have crossed the 14-day verification window without an outcome.
Agents record outcomes honestly; refusing to verify is itself a signal.

**Enforced by:** `POST /admin/verification-decay` and
`POST /heartbeat` `verification_overdue` field (ADR-018).

## Principle 6 — Consensus-only detection

Recursive agreement without outcomes is not verification. CAUSES edges whose
SUPPORTS edges share a single `source_tier` or `source_author` form a
homogeneous support structure. The substrate detects this and fires
`CONSENSUS_FLAG` challenge nudges (low confidence, capped at 3/heartbeat,
idempotent). Industrial agents must respond with outcomes, not more support.

**Enforced by:** `detect_consensus_only_support` and
`fire_verification_nudge` (ADR-029).

## Principle 7 — Oppositional review

A dissenting peer reviewer is the institutional substitute for verification.
L3 CAUSES edges whose support is homogeneous in `source_tier` (≥0.8
homogeneity, ≥2 supporters) trigger a `oppositional_review()` cycle that
emits a low-confidence challenge with `reviewer_agent=system_oppositional`.
Industrial agents must not self-challenge their own work; the system
supplies the opposition.

**Enforced by:** `find_homogeneous_causes` and `oppositional_review` in
`ohm/graph/methods.py` (ADR-030).

## Principle 8 — Cryptographic attribution

`created_by` is a plain string by default — any agent can forge it. TELOS
signing attaches an HMAC-SHA256 signature over the canonical payload
(whitelisted fields, sorted keys). Signatures verify post-hoc; tampering
invalidates them. Industrial deployments should enable per-agent signing
keys and require signed writes for L3 edges touching safety-critical nodes.

**Enforced by:** `sign_node_write`, `sign_edge_write`,
`verify_node_write`, `verify_edge_write` (ADR-035).

## Principle 9 — Source diversity

Ten SUPPORTS edges from the same institution are one position, not ten
verifications. `source_diversity_score` is the normalized weighted Shannon
entropy over `source_author`, `source_institution`, and `data_origin` along
the CAUSES/SUPPORTS/EXPECTS/PREDICTS path. Low diversity triggers
oppositional review (Principle 7). Score is annotated on every synthesis
response.

**Enforced by:** `source_diversity_score` and
`POST /agent/synthesis` (ADR-033).

## Principle 10 — Autonomy-loop integrity

The propose → execute → outcome → status flow is the only path from
counterfactual to action. Every action node carries:

- `task_status` (`proposed` → `executed` | `rejected`)
- `executed_by` (the agent that took the action)
- `outcome` (`TRUE` | `FALSE` | `PARTIAL` | `NULL`)
- `outcome_notes` (human-readable context)

Actions cannot be re-executed; outcomes are immutable once recorded. The
proposing scenario must reference existing nodes (Principle 2).

**Enforced by:** `propose_action`, `execute_action`, `query_loop_status`
(OHM-446a).

## Principle 11 — Twin registration

Digital twins are first-class `twin` nodes linked to the system they model
via an `EVALUATES` edge. A twin without a target is unanchored and cannot
be queried by `/scenario` or `/loop`. Twin `/predict` outputs feed into
`/scenario` as `edge_overrides` or `node_interventions`; twin `/constraints`
map to `constraint_expr`; twin gate health maps to `gate_type`/`gate_status`.

**Enforced by:** `VALID_NODE_TYPES` membership of `twin` and the
`EVALUATES` link requirement (OHM-8dg4, OHM-f7tl).

## Principle 12 — Temporal mode awareness

Industrial decisions are not all equal-time. A 30-minute batch decision and
a 30-second control decision have different freshness requirements. The
temporal decision layer adds `temporal_mode` (`real_time` | `deliberative`)
and `freshness_threshold` per decision node. Real-time decisions cannot
drive deliberative actions and vice versa. VoI ranks research by decision
urgency, not just gap size.

**Enforced by:** schema and `query_loop_status` temporal section
(OHM-2x2u).

## Principle 13 — Read scopes

Agents see what their trust boundary permits. `read_scope` is a JSON column
on `ohm_agent_config` with four dimensions: `layer`, `source_tier`,
`created_by`, `node_id`. NULL = full access (legacy). Soft-deleted items
(filtered by `deleted_at IS NULL`) are never visible regardless of scope.
Multi-tenant agents are scoped to `created_by: ["customer:{id}"]`.

**Enforced by:** `enforce_read_scope` in `ohm/boundary.py` (ADR-037).

## Principle 14 — Suggestion lifecycle

Substrate methods and agents produce candidate edges that should not enter
the canonical graph immediately. Suggestions live in `ohm_suggestions` with
lifecycle `ripe → promoted | expired | rejected`. Ripeness is multiplicative:
`time_factor × evidence_factor × confidence_factor` — an AND-gate of
maturity, evidence, and strength. Auto-promotion at ≥0.7; auto-expiry at
30 days; duplicate detection by `(from_node, to_node, target_node)`.

**Enforced by:** `compute_ripeness`, `ripen_then_decide` (ADR-036).

## Principle 15 — Boundary respect

No agent may overwrite another agent's L3/L4 edges. The only valid
responses to an edge are:

- `CHALLENGED_BY` (question the confidence)
- `SUPPORTS` (independent verification)
- `DERIVED_FROM` (computed from this edge)
- `NEGATES` (rules out the implied conclusion)

Forging, deleting, or mutating another agent's edge is a violation. The
write is rejected by `enforce_write_boundary`.

**Enforced by:** `enforce_write_boundary` in `ohm/boundary.py`
(ADR-003, ADR-009).

---

## Conformance Test Coverage

Each principle has one or more tests in
`tests/test_industrial_manifesto_conformance.py`:

| Principle | Test class |
|---|---|
| 1. Agent-owned edges | `TestAgentOwnedEdges` |
| 2. Mandatory cross-link | `TestMandatoryCrossLink` |
| 3. Source tier ceilings | `TestSourceTierCeilings` |
| 4. AND-gate governance | `TestAndGateGovernance` |
| 5. Verification decay | `TestVerificationDecay` |
| 6. Consensus-only detection | `TestConsensusOnlyDetection` |
| 7. Oppositional review | `TestOppositionalReview` |
| 8. Cryptographic attribution | `TestCryptographicAttribution` |
| 9. Source diversity | `TestSourceDiversity` |
| 10. Autonomy-loop integrity | `TestAutonomyLoopIntegrity` |
| 11. Twin registration | `TestTwinRegistration` |
| 12. Temporal mode awareness | `TestTemporalModeAwareness` |
| 13. Read scopes | `TestReadScopes` |
| 14. Suggestion lifecycle | `TestSuggestionLifecycle` |
| 15. Boundary respect | `TestBoundaryRespect` |

Industrial deployments gate agent promotion on passing all 15 classes.

---

## Related Issues

- OHM-8dg4 — Synthesize OHM decision-intelligence architecture
- OHM-brps — Industrial process example (the worked reactor scenario)
- OHM-446a — Proposed/executed action API
- OHM-as17 — Constraint and gate_status schema
- OHM-tjzh — Cross-link enforcement
- OHM-8gyd — Verification loop automation
