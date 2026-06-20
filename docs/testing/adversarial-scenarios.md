# Adversarial Scenarios for OHM Internalized Verification

Concrete test scenarios for `tests/test_internalized_verification_scenario.py`. Each scenario simulates an attack on OHM's epistemic quality and verifies that the internalized verification stack detects it.

## Scenario 1: Cornell-style UGC consensus capture

**Pattern:** Many user-generated sources with the same author, institution, and `ugc` origin all claim the same causal relationship. None have recorded outcomes.

**Initial graph:**
- Target node: `claim-X` with `source_tier=unverified`, `confidence=0.5`
- 10 evidence nodes: `ugc-1` through `ugc-10`, all `source_author='anon_redditor'`, `source_institution='Reddit'`, `data_origin='ugc'`, `source_tier='raw'`
- 10 `SUPPORTS` edges from each `ugc-i` to `claim-X`, all L3, no outcomes recorded

**Attack:** An agent synthesizes `claim-X` and treats the 10 supporting edges as independent corroboration.

**Expected detections:**
- `source_diversity_score(conn, 'claim-X')` returns a low score (< 0.3) because author, institution, and origin are homogeneous.
- Oppositional review flags the homogeneous SUPPORTS cluster.
- `source_tier` ceilings prevent any single UGC source from carrying confidence above 0.3.

**Expected final confidence:** Synthesized confidence should be capped or challenged, not amplified.

## Scenario 2: Institutional echo chamber

**Pattern:** Three respectable-looking institutions all cite the same underlying press release, producing the illusion of independent verification.

**Initial graph:**
- Target node: `claim-Y` with `source_tier='preliminary'`, `confidence=0.7`
- Evidence nodes:
  - `inst-A`: `source_author='Dr. A'`, `source_institution='Institute A'`, `data_origin='news_wire'`
  - `inst-B`: `source_author='Dr. B'`, `source_institution='Institute B'`, `data_origin='news_wire'`
  - `inst-C`: `source_author='Dr. C'`, `source_institution='Institute C'`, `data_origin='news_wire'`
- All three nodes have a `REFERENCES` edge to a shared press release node `pr-1`.
- All three have `SUPPORTS` edges to `claim-Y`.

**Attack:** Synthesis treats three institutional sources as independent verification.

**Expected detections:**
- `source_diversity_score` is moderate on institution but low on origin and shared reference graph.
- ADR-029 consensus-only detection flags that no outcomes are recorded for the SUPPORTS edges.
- ADR-030 oppositional review flags homogeneous `news_wire` origin cluster.

**Expected final confidence:** Challenged or capped; the shared press release collapses the independence claim.

## Scenario 3: Stale-fact revival

**Pattern:** A claim that was well-supported at one time is revived years later without checking whether the evidence is still valid.

**Initial graph:**
- Target node: `claim-Z` created 2020-01-01, `source_tier='verified'`, `confidence=0.95`
- Evidence nodes created 2020-01-01 through 2020-03-01.
- No new evidence after 2020.

**Attack:** An agent queries `query_snapshot(conn, '2026-01-01')` and uses the old verified state as current truth.

**Expected detections:**
- ADR-018 verification decay: unrenewed evidence has lost confidence over time.
- Temporal pinning (`query_snapshot`) must be used explicitly; default queries operate on current state.
- If evidence nodes have `source_tier='verified'` but no recent observations, confidence decays.

**Expected final confidence:** Lower than the original 0.95 unless new evidence is added.

## Scenario 4: Agent-impersonation / TELOS forgery

**Pattern:** A malicious agent forges a node write signature to make it appear as if a trusted agent created it.

**Initial graph:**
- Trusted agent: `atlas@olympus.local` with signing key `K_atlas`.
- Forged node: `claim-W` labeled as created by `atlas@olympus.local` but signed with a wrong or missing key.

**Attack:** An agent reads `claim-W`, sees `created_by='atlas@olympus.local'`, and trusts it without verifying the signature.

**Expected detections:**
- `verify_node_write(conn, 'claim-W', key=K_atlas)` returns `verified: false`.
- Tamper detection fails if any field (label, confidence, provenance) is modified after signing.
- Read-scope enforcement (ADR-037) can block unverified writes from being consumed.

**Expected outcome:** The forged node is flagged; synthesis does not use it as evidence until verified.

## Scenario 5: Tier escalation bypass

**Pattern:** A caller passes `source_tier=None` to avoid confidence ceilings, then later an agent assigns a higher tier without evidence.

**Initial graph:**
- Node `claim-V` created with `source_tier=None`, `confidence=0.95`.

**Attack:** An update path allows setting `source_tier='verified'` without increasing evidence quality.

**Expected detections:**
- `create_node` with `source_tier=None` and high confidence is allowed (backward compatibility), but no ceiling bypass.
- Any attempt to update `source_tier` to `verified` without outcome recording should be rejected or require justification.
- `source_tier` promotion must be monotonic and evidence-gated.

## Implementation notes

Each scenario should be implemented as a separate test class in `tests/test_internalized_verification_scenario.py` and marked with `pytest.mark.adversarial` plus any relevant additional markers (`integration`, `slow`).
