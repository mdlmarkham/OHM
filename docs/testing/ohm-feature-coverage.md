# OHM Feature Test Coverage — ADRs 028–037 + In-Flight Features

**Owner:** DeepThought (synthesis) · **Coordinator:** Metis (OHM-w82p, OHM-l1nd)
**Bead:** OHM-w82p · **Date:** 2026-06-20
**Status:** First-pass — re-run after Hephaestus (OHM-9zae, OHM-zaow) lands the harness output and Hephaestus (OHM-bsse) wires the HTTP scenarios.
**Scope:** This report maps unit, integration (Python SDK + HTTP), adversarial, performance, and agent-campaign coverage for the internalized-verification stack (ADR-028 through 037) and the in-flight features currently tracked in `.beads`.

> **Reading the matrix:** ✅ covered · ⚠️ partial · ❌ missing. "Unit" = pytest functions in `tests/`. "HTTP" = endpoint behavior in `tests/test_server.py`. "Adversarial" = scenarios in `docs/testing/adversarial-scenarios.md` and `tests/test_internalized_verification_scenario.py`. "Perf" = `tests/test_benchmarks.py` / `tests/test_multi_tenancy_perf.py`. "Agent campaign" = end-to-end run by Socrates / Hephaestus / Clio / Metis via the OHM agent harness (OHM-a35).

---

## 1. Coverage Matrix

| Feature | Unit | HTTP | Adversarial | Perf | Agent campaign | Gaps |
|---|---|---|---|---|---|---|
| **ADR-028 — Source tier + confidence ceilings** | ✅ 19 (`test_source_tier.py`: `TestSourceTierSchema`, `TestCreateNodeWithTier`, `TestCreateEdgeWithTier`, `TestOhmStoreTier`, `TestSdkTier`, `TestBackwardCompatibility`) | ⚠️ Tier accepted in handlers; no dedicated HTTP assertion for ceiling rejection | ✅ Scenario 1 in `adversarial-scenarios.md` (UGC poisoning) | ❌ | ⚠️ Implicit in OHM-a35 agent writes; no campaign-level ceiling assertion | HTTP rejection path; multi-tenant tier conflict; migration backfill for legacy rows |
| **ADR-029 — Consensus-only detection + auto-nudge** | ✅ 11 (`test_consensus_verification.py`: `TestDetectConsensusOnlySupport`, `TestFireVerificationNudge`, `TestSdkConsensus`) | ❌ No `POST /edges/:id/nudge` HTTP test | ✅ Scenarios 1 & 2 (UGC poisoning, institutional echo chamber) | ❌ | ❌ | Endpoint test; idempotency under concurrent firings; nudge payload schema |
| **ADR-030 — Oppositional review pipeline** | ✅ 17 (`test_oppositional_review.py`: `TestFindHomogeneousCauses`, `TestOppositionalReview`, `TestSdkOppositionalReview`) | ❌ No HTTP trigger for `auto_challenge=True` | ✅ Scenario 1 (UGC), Scenario 2 (echo chamber) | ❌ | ❌ | HTTP endpoint; flag-only vs. auto-challenge behavior; rate limits |
| **ADR-031/032 — HD fingerprint prototype + persistent layer** | ✅ 70+ (`test_hd.py`: 12 classes — bind/disbind, similarity, determinism, majority rule, fingerprint text/node, similarity search, membership search, batch update, validation; `test_emerging_concepts.py` cross-cuts) | ⚠️ `update_hd_fingerprint` exposed via SDK only; no `GET /nodes/:id/hd_neighbors` HTTP test | ❌ | ❌ (Hephaestus OHM-c1id in-flight) | ❌ | HTTP membership-search endpoint; persistent-index benchmark; refresh-on-write path |
| **ADR-033 — Source diversity score** | ✅ 8 (`test_source_diversity.py`: `TestSourceDiversityScore`, `TestSDKSourceDiversity`) | ❌ Not surfaced as a node/edge response field | ✅ Scenario 1 (UGC, expected score < 0.1) | ❌ | ❌ | HTTP exposure; multi-author weighting; CLI output |
| **ADR-034 — Emerging concept detection** | ✅ 19 (`test_emerging_concepts.py`: `TestComputeResidualMass`, `TestEmergingConceptStability`, `TestDetectUnknownIngredients`, `TestUpdateEmergingConceptScore`, `TestPromoteEmergingConcept`, `TestSDKEmergingConcepts`) | ❌ | ❌ | ❌ | ❌ | Threshold tuning for 5K+ node graphs; "stability" semantics under concurrent writes; HTTP `GET /emerging` |
| **ADR-035 — TELOS signing** | ✅ 14 (`test_telos_signing.py`: `TestCanonicalPayload`, `TestHmacSigning`, `TestSignWrite`, `TestSignNodeWrite`, `TestSignEdgeWrite`, `TestSDKSigning`) | ❌ Unsigned writes still accepted (correct per ADR); no HTTP test for verification endpoint | ✅ Scenario 3 (forged `created_by`) — `test_telos_signing_traces_forgery` in `test_internalized_verification_scenario.py` | ❌ | ❌ | HTTP verify endpoint; key-rotation test; key-id registry fixture |
| **ADR-036 — Suggestions lifecycle (ripen-then-decide)** | ✅ 14 (`test_suggestions_lifecycle.py`: create, query, promote, reject, ripen, SDK) + 25 (`test_suggestions.py` — proactive discoverability) | ✅ `test_resolve_suggestions_on_prefix` in `test_server.py` (line 1900) | ❌ | ❌ | ⚠️ Socrates/DeepThought writes count evidence in OHM-a35.4/5 | Ripen-curve correctness; status transitions; expired-vs-stale semantics |
| **ADR-037 — Read scopes + temporal pinning** | ✅ 14 (`test_read_scopes.py`: `TestEnforceReadScope`, `TestGetAgentReadScope`, `TestSetAgentReadScope`, `TestQuerySnapshotDeletedAt`, `TestSDKReadScope`) | ❌ No HTTP test for `read_scope` enforcement on `/graph/at` or `/node/:id` | ❌ | ❌ | ❌ | HTTP path; soft-delete interaction; DuckLake time-travel deferral (OHM-xgm) |
| **In-flight: OHM-wvz8.4 — Manifold-density scoring** | ❌ | ❌ | ❌ | ❌ | ❌ | All categories — depends on Atlas (OHM-wvz8.4) |
| **In-flight: OHM-c1id — Perf tests for semantic search + HD** | ⚠️ `test_benchmarks.py` exists but no HD fingerprints benchmark | n/a | n/a | ⚠️ Skeleton only | n/a | Need explicit HD throughput, batch update, and 5K-node scenario |
| **In-flight: OHM-bsse — HTTP tests for internalized verification** | ❌ | ❌ | ⚠️ `test_internalized_verification_scenario.py` mixes HTTP fixture use with stubbed assertions | n/a | n/a | Wire each adversarial scenario to a real HTTP path |

**Totals (ADRs 028–037, 10 rows):**
- Unit: **9 / 10** rows covered (ADR-034 has 19 tests; ADR-031/032 has the most with 70+)
- HTTP: **1 / 10** (`ADR-036` only)
- Adversarial: **5 / 10** documented in `adversarial-scenarios.md`
- Performance: **0 / 10** directly measured (skeleton exists)
- Agent campaign: **1 / 10** (`ADR-036` via OHM-a35.4/5)

---

## 2. Top 5 Untested or Undertested Behaviors

Ranked by risk × likelihood of regression in production. These are the highest-leverage gaps for the next sprint.

### 1. **HTTP surface for the internalized-verification stack (ADR-028 → 037)**

Eight of ten ADRs have unit coverage and zero HTTP coverage. `ohmd` exposes these via REST, but `tests/test_server.py` only checks the suggestions-lifecycle `resolve_suggestions_on_prefix` path (line 1900). The entire adversarial story (UGC poisoning, echo chamber, forged `created_by`) is real-world reachable only via HTTP, not via Python SDK calls. **This is the largest functional gap.**

Risk: a single endpoint regression silently disables ADR-029, 030, 033, 035 — the very features the Cornell UGC paper motivates.

**Fix:** OHM-bsse (Hephaestus) — wire `test_internalized_verification_scenario.py`'s three scenarios to real HTTP calls against `test_server`. Each scenario already specifies the expected behavior in HTTP-resolvable terms.

### 2. **TELOS signing key registry and rotation (ADR-035)**

Signing is unit-tested (HMAC roundtrip, tamper detection, wrong key fails). The missing piece is the **key-id lifecycle**: how does an agent rotate `signing_key_id`? What happens when a node is signed with a retired key? Is there a `GET /keys` endpoint? `test_telos_signing_traces_forgery` checks detection but not the operational path.

Risk: agents will run out of date on `signing_key_id`; the verification endpoint will reject valid historical writes.

**Fix:** Add a key registry fixture, then a `test_key_rotation_*` class. The Metis challenge is "can a verifier recover trust after a legitimate rotation?".

### 3. **Read scope enforcement on the HTTP path (ADR-037)**

Unit tests cover `enforce_read_scope()` and the snapshot deleted-at filter. But the read-side HTTP endpoints (`/graph/at`, `/node/:id`, `/edges`) don't have HTTP-level tests verifying that an agent with `source_tier=["official"]` cannot read `raw`-tier nodes. The soft-delete fix in `query_snapshot` is unit-tested, but only as a Python call.

Risk: information leakage across trust boundaries in multi-tenant deployments (ADR-015). The "raw"-tier Reddit claim becoming visible to a customer-scoped agent is exactly the failure mode the ADR describes.

**Fix:** `test_read_scopes_http.py` — 6 tests, one per scope dimension × agent-role matrix.

### 4. **Performance under load: HD fingerprints + semantic search at 5K+ nodes (ADR-031/032/034)**

ADR-031 is explicit: the prototype is O(n) per query with Python-side Hamming distance. The batch-update optimization (ADR-032) is unit-tested but not benchmarked. OHM-c1id is in-flight but not yet started. We don't know the latency profile of `hd_membership_search` against a 5K-node graph, and the residual-mass scan in ADR-034 is O(n²) in the worst case.

Risk: graph grows past the prototype's tolerance and the emerging-concept detector becomes a hot path.

**Fix:** Add three benchmarks to `test_benchmarks.py`: (a) `hd_fingerprint` compute on 1K/5K/10K nodes, (b) `hd_membership_search` latency, (c) `compute_residual_mass` full scan. Set SLOs before the next deployment.

### 5. **Oppositional review: flag-only vs. auto-challenge under concurrent writes (ADR-030)**

The unit tests cover both modes but in isolation. The `auto_challenge=True` path creates a `CHALLENGED_BY` edge; if two agents run oppositional review concurrently on the same homogeneous cluster, do we get duplicate challenges? Does the homogeneous flag re-trigger if the cluster is re-formed? The integration test for idempotency in `TestFireVerificationNudge` is for ADR-029, not 030.

Risk: noisy CHALLENGED_BY edges from parallel review runs; loss of signal in the challenge graph.

**Fix:** Add `test_oppositional_review_concurrent.py` with two threads racing on the same target node; assert idempotent challenge creation (mirroring ADR-029's pattern).

---

## 3. Recommendations for Next Test Investments

| Priority | Investment | Why | Estimated effort |
|---|---|---|---|
| **P0** | Wire `test_internalized_verification_scenario.py` to HTTP via OHM-bsse | Largest single coverage gap; covers 5 ADRs in one stroke | M (1–2 days) |
| **P0** | Add `test_read_scopes_http.py` for ADR-037 | Read-side leakage is a multi-tenant breach risk | S (0.5 day) |
| **P1** | HD + semantic-search benchmarks (OHM-c1id) | We're flying blind on latency past 5K nodes | S (0.5 day) |
| **P1** | Concurrent oppositional review test (ADR-030) | Cheap to add; closes a real concurrency hazard | S (0.5 day) |
| **P1** | TELOS key-rotation test (ADR-035) | Operational path is untested | S (0.5 day) |
| **P2** | Agent-campaign assertion for source-tier ceiling (ADR-028) | OHM-a35.4/5 should fail-write when an agent pushes a 0.85 confidence on `raw` | S (0.5 day) |
| **P2** | Ripen-curve integration test (ADR-036) | Unit covers compute; integration with cron/agent loop is unverified | S (0.5 day) |
| **P3** | Multi-tenant tier conflict test (ADR-015 × ADR-028) | One tenant's `official` tier should not leak into another's view | M (1 day) |

**Net reading:** Unit coverage is **strong** (8/10 ADRs have ≥ 8 dedicated tests; ADR-031/032 has 70+). The stack is held together at the Python layer. What is missing is the **wire**: HTTP, concurrency, performance, and the read-side path. Hephaestus's OHM-bsse ticket is the single highest-leverage test investment on the board right now.

---

## 4. Data Provenance

| Source | Used for |
|---|---|
| `docs/adr/0028-…0037*.md` | Per-ADR scope and decision text |
| `tests/test_*.py` (76 files, counted via `grep -cE "^(def test_\|async def test_\|class Test)"`) | Unit coverage counts |
| `tests/test_server.py` (HTTP) | HTTP coverage audit |
| `tests/test_internalized_verification_scenario.py` + `docs/testing/adversarial-scenarios.md` | Adversarial coverage |
| `tests/test_benchmarks.py`, `tests/test_multi_tenancy_perf.py` | Performance coverage |
| `.beads/issues.jsonl` (bd show OHM-w82p, OHM-l1nd, OHM-9zae, OHM-bsse, OHM-c1id, OHM-zaow) | In-flight status of agent campaign, harness, HTTP tests, perf tests |
| `BEADS.md` (OHM-a35.x) | Agent campaign attribution (Socrates OHM-a35.4, DeepThought OHM-a35.5) |

**Method note:** Test counts derive from `grep -nE "^(class |def test_|    def test_)"` against each test file. The adversarial column counts scenarios *specified* in `adversarial-scenarios.md`, not scenarios with bound test fixtures. After Hephaestus (OHM-9zae, OHM-zaow) lands the harness output, this report should be regenerated with the fixture-bound counts from the harness JSON.

---

*Synthesis by DeepThought. Metis owns the follow-up synthesis (OHM-l1nd) and the team onboarding runbook (OHM-xhng); this report is the input, not the output, of OHM-l1nd.*
