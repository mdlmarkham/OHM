# ADR-030: Oppositional Review Pipeline

**Date:** 2026-06-19
**Status:** Accepted
**Related issues:** OHM-jbsr (this ADR), OHM-wvz8 (parent epic), ADR-018 (verification), ADR-028 (source tier), OHM-qi6r (Phase 2 dep), OHM-wvz8.2 (Phase 3 dep)

## Context

Atlas framing: oppositional review is the institutional substitute for a dissenting peer reviewer. It should challenge CAUSES edges whose support is homogeneous in source tier, consensus language, or embedding cluster, to break recursive agreement loops before they stabilize. ADR-028 made `source_tier` available on edges; this ADR uses it as the first homogeneity dimension.

Without oppositional review, a CAUSES edge backed by three `raw`-tier SUPPORTS edges from the same agent looks identical to one backed by three `official`-tier SUPPORTS edges from three independent agents. The compound confidence formula (ADR-018.4) partially addresses this via source diversity correlation, but it cannot flag the structural homogeneity for agent review — it only downweights. Oppositional review makes the homogeneity visible and actionable.

## Decision

### 1. Homogeneous-causes detection

`find_homogeneous_causes(conn, *, target_node_id=None, min_confidence=0.5, homogeneity_threshold=0.8, min_support_count=2, limit=50)` in `src/ohm/graph/queries/__init__.py:1215`.

Finds L3 CAUSES edges whose SUPPORTS edges (linked via `challenge_of`) are homogeneous in `source_tier`. The homogeneity score:

```
homogeneity_score = 1.0  when distinct_tiers ≤ 1 (all supporters share one tier, or all NULL)
homogeneity_score = 1 - (distinct_tiers / support_count)  otherwise
```

Only edges with ≥ `min_support_count` supporters are considered — a single unverified edge is NOT homogeneous; it is just unverified, which ADR-018.3 already handles. The `reason` string reports the supporters' shared tier (via `MAX(sup.source_tier)`), not the CAUSES edge's own tier.

Returns a list sorted by `(homogeneity_score DESC, confidence DESC)`, capped at `limit`.

### 2. Oppositional review orchestration

`oppositional_review(conn, *, ..., auto_challenge=False, reviewer_agent='system_oppositional', challenge_budget=3, limit=50)` in `src/ohm/graph/methods.py:239`.

Mirrors `detect_contradictions` — the substrate detects, agents decide. `auto_challenge` defaults to `False` (flag only). When `True`, creates `CHALLENGED_BY` edges via `create_challenge` with `confidence=0.3` and a reason describing the homogeneity; capped at `challenge_budget` (3) per run.

Returns:

```python
{
    "flagged_edges": list[dict],      # from find_homogeneous_causes
    "challenged_edges": list[dict],   # edge_id + challenge_id pairs
    "review_summary": {
        "total_flagged": int,
        "total_challenged": int,
        "dimensions_used": ["source_tier", "agent_authorship"],
        "homogeneity_threshold": float,
        "min_support_count": int,
        "auto_challenge": bool,
    },
}
```

### 3. Synthesis hook

`_post_synthesis` in `src/ohm/server/handlers/graph.py:1784` runs `oppositional_review` scoped to the synthesis's `cluster_ids` (the `to_nodes` it backs), `auto_challenge=False`, after the node/edges/observation are created. Non-fatal (wrapped in `try/except` + `logging.debug`) — a review failure never breaks synthesis. The result is attached as `oppositional_review` only when `flagged_edges` is non-empty.

### 4. SDK

`Graph.run_oppositional_review(*, target_node_id=None, auto_challenge=False, ...)` in `src/ohm/framework/sdk.py:614`. Note: the SDK passes `reviewer_agent=self.actor` (the calling agent), not the default `system_oppositional`. This lets agents run oppositional review under their own identity when calling via SDK.

### 5. No DDL

Reuses existing `challenge_of`, `challenge_type`, `source_tier`, `created_by` columns. The `reviewer_agent` defaults to `'system_oppositional'` (not the creating agent) to avoid the self-challenge paradox — an agent should not auto-challenge its own edges under a system identity.

## Consequences

**Positive:**
- Phase 1 uses `source_tier` + `agent_authorship` homogeneity — the two dimensions already available on edges.
- `auto_challenge=False` default prevents over-charging on small homogeneous graphs.
- `min_support_count=2` ensures single-supporter edges are not flagged (they are unverified, not homogeneous).
- The 3-per-run `challenge_budget` caps auto-challenges even when opted in.
- Auto-challenges are low-confidence (0.3) and clearly labeled so agents can `SUPPORTS` the challenged edge if the challenge is spurious; ADR-018 verification loops then apply.
- The synthesis hook makes oppositional review automatic for new syntheses without blocking the write path.

**Negative:**
- Phase 1 homogeneity is coarse — two `official`-tier edges from different agents with different methodologies still score 1.0 on homogeneity. Phase 2's `source_diversity_score` (OHM-qi6r) and Phase 3's embedding-cluster dimension (OHM-wvz8.2) are needed for finer discrimination.
- The `system_oppositional` reviewer identity is a convention, not enforced — an agent could set `reviewer_agent` to any string. This is acceptable per ADR-003 (agent-owned edges).

## Alternatives considered

- **Auto-challenge by default** — rejected. Over-charging risk on small graphs where homogeneity is structural (e.g., a new domain with only one agent). Flag-only default lets agents decide whether to challenge.
- **Separate `review_status` queue column** — rejected for Phase 1. The computed stage (flagged vs. challenged) is enough; a persistent queue is deferred to Phase 2 if needed.
- **Semantic similarity now** — rejected. Needs embedding comparison at query time, which is expensive without pre-computed clusters. Deferred to Phase 3 (OHM-wvz8.2) with pre-computed embedding clusters.

## Phase roadmap

| Phase | Dimensions | Issue | Status |
|-------|-----------|-------|--------|
| 1 | `source_tier` + `agent_authorship` | OHM-jbsr | Implemented |
| 2 | + `source_diversity_score` + lexical text similarity (jaro_winkler) | OHM-qi6r | Deferred |
| 3 | + embedding-cluster dimension | OHM-wvz8.2 | Deferred |

## References

- ADR-028 — Source Tier Architecture (provides the `source_tier` dimension used here)
- ADR-018 — Verification Loops (auto-challenges feed into the verification-overdue cycle)
- ADR-003 — Agent-Owned Edges (challenge semantics apply to auto-challenges)
- ADR-029 — Consensus-Only Confidence Ceilings (complementary: detects consensus-only support, this detects homogeneous support)
- Atlas framing — oppositional review as institutional substitute for dissenting peer reviewer
