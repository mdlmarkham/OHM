# OHM Architecture Decision Records

## ADR-001: DuckDB + Recursive CTEs over DuckPGQ for Graph Traversal

**Date:** 2026-05-16
**Status:** Decided

### Context

OHM needs graph traversal (neighborhood queries, impact analysis, path finding). DuckPGQ provides SQL/PGQ `MATCH` syntax but is a community extension maintained by a research group with no release tags and breaking changes on DuckDB upgrades. Kuzu was acquired by Apple (Oct 2025, repo archived) and is not viable.

### Decision

Use recursive CTEs for all production graph queries. DuckPGQ remains optional for ad-hoc exploration.

### Consequences

- Zero-dependency graph queries (standard SQL, works through Quack, survives DuckDB upgrades)
- Bounded-depth traversals (1-5 hops) are well within CTE performance at OHM's scale
- Need to implement ~7 parameterized CTE views for common query patterns
- DuckPGQ `MATCH` syntax is nicer to write but cannot be relied on for production

---

## ADR-002: Quack for Concurrent Access

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents need to read and write the knowledge graph simultaneously. DuckDB is single-writer by default.

### Decision

Use DuckDB's Quack protocol (HTTP-based, token-authenticated, multi-reader/multi-writer). Requires a persistent daemon (`ohmd`) to own the DuckDB file and serve connections.

### Consequences

- Requires `ohmd` daemon (systemd service, auto-restart, health check)
- All agents connect via Quack instead of direct file access
- Token auth with role-based access control per agent
- Quack currently ships from `core_nightly` — must pin binary for production

---

## ADR-003: Agent-Owned Edges with Challenge Semantics

**Date:** 2026-05-16
**Status:** Decided

### Context

Multiple agents will create L3 (Knowledge) and L4 (Prospect) edges about the same topics. Averaging confidence scores would destroy individuality. Allowing overwrites would lose perspectives.

### Decision

Every L3/L4 edge has a single owner (`created_by`). Other agents can create CHALLENGED_BY, SUPPORTS, or DERIVED_FROM edges that reference the original, but cannot modify or delete it.

### Consequences

- The graph accumulates perspectives without collapsing them
- Confidence scores reflect the owning agent's judgment
- Humans see the full picture including disagreements
- Challenge edges create productive tension, not consensus averaging
- Requires `created_by` and `updated_by` columns on all L3/L4 tables

---

## ADR-004: Three-Layer Data Architecture — per-agent local cache, shared DuckLake, private scratch
- ADR-012: Per-Agent Local DuckDB Cache — `OhmStore.for_agent()` with zero-latency local access

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents need fast local access to their working set, shared access to the canonical graph, and private space for unfinished work.

### Decision

Three layers:
1. **Local DuckDB cache** — per-agent working memory, synced from DuckLake on heartbeat
2. **DuckLake shared backend** — canonical graph, time travel, change feed, agent state
3. **Private** — agent-only notes below confidence threshold, personal observations, scratch calculations

### Consequences

- Cache invalidation via DuckLake `table_changes()` (incremental, not full sync)
- Private layer never promoted automatically; per-agent threshold for promotion
- Change feed carries `agent_name` attribution on every write
- Time travel enables "what did we know at time T?" queries

---

## ADR-005: Self-Documenting CLI as Agent Interface

**Date:** 2026-05-16
**Status:** Decided

### Context

Agents should not need to know SQL, DuckDB internals, CTE structure, or Quack protocol details to use the graph.

### Decision

Package the entire stack as `ohm` CLI. Agents call `ohm graph write`, `ohm graph listen`, `ohm state show`, not raw SQL.

### Consequences

- Implementation can evolve (CTEs→DuckPGQ, file→Quack, Kuzu→DuckLake) without breaking agents
- `ohm graph schema` and `ohm graph layers` are living documentation
- `ohm graph listen --since last-check` wraps the change feed
- The CLI is the contract between agents and the graph

---

## ADR-006: Advisory Schema with Graduated Enforcement

**Date:** 2026-05-17
**Status:** Decided

### Context

OHM's schema (node types, edge types, layers) is currently advisory — any node_type or edge_type can be created without validation. This is intentional for early-stage exploration, but as a domain matures, stricter enforcement becomes desirable.

### Decision

The schema remains advisory by default. Enforcement is graduated through `SchemaConfig`: advisory (default), lenient (known types validated, unknown accepted), strict (only registered types accepted). Schema evolution is handled through the existing migration framework.

### Consequences

- New projects start in advisory mode — no friction for exploration
- Mature domains opt into strict mode via `SchemaConfig`
- Schema migrations are versioned and auditable through `ohm_meta`

---

## ADR-007: Schema Evolution and Type Governance for Domain Expansion

**Date:** 2026-05-17
**Status:** Decided

### Context

As OHM is applied to new domains (cattle operations, industrial monitoring), the schema must accommodate domain-specific types without polluting the core ontology or breaking existing queries.

### Decision

Domain types are isolated through `SchemaConfig` instances. Each domain extends — but never overrides — the base OHM types. Types follow a three-stage lifecycle: experimental (advisory) → registered (lenient) → canonical (strict). Promotion to canonical requires a schema migration.

### Consequences

- No schema pollution — domain types stay in their `SchemaConfig` until promoted
- Domain autonomy — each domain controls its own type lifecycle
- Cross-domain visibility — all domains share the same physical tables
- Migration audit trail — every type promotion is recorded in `MIGRATIONS`

---

## ADR-008: Probability and Confidence Model

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM edges carry a `confidence` field (0–1) representing how certain the creating agent is about the relationship. Nodes accumulate `observations` with their own confidence values. Multiple agents may create independent edges about the same relationship, and a single node may have many observations from different sources. The system needs a principled way to combine these values — especially when observations are correlated (e.g., two blood tests from the same lab) versus independent (e.g., imaging + blood work).

Naive averaging destroys agent individuality (ADR-003). Simple multiplication over-counts correlated evidence. The system must support both medical diagnosis (where correlation between findings matters) and general knowledge graphs (where independent evidence compounds).

### Decision

Three-tier confidence model:

1. **Edge confidence** — single value per edge, owned by the creating agent (ADR-003). Not averaged or merged.
2. **Compound confidence** — combines multiple confidence values with explicit `correlation` parameter:
   - `correlation=0.0` (independent): P(at least one) = 1 − Π(1 − pᵢ). Evidence compounds multiplicatively.
   - `correlation=1.0` (perfectly correlated): result = max(pᵢ). Only the strongest evidence matters.
   - `0.0 < correlation < 1.0`: linear interpolation between independent and correlated results.
3. **Composite score** — per-node aggregate combining observation scores and evidence-chain confidence, with configurable weights (`observation_weight`, `evidence_weight`). Supports arithmetic (default, backwards-compatible) and geometric mean methods.

The `probability` column on edges (added in schema v0.5.0) is distinct from `confidence`: probability represents the likelihood of the described relationship occurring in the world (e.g., "70% chance this supplier fails"), while confidence represents the agent's certainty about the claim (e.g., "I'm 90% sure this probability estimate is correct").

### Consequences

- Agents retain ownership of their individual confidence judgments
- Correlated observations don't artificially inflate compound confidence
- Medical diagnosis can model same-modality correlation vs. cross-modality independence
- `probability` and `confidence` serve different analytical purposes and should not be conflated
- The interpolation formula is simple and auditable, but not Bayesian — future work could add prior-based updating

---

## ADR-009: NEGATES Edge Type for Ruling Out Conditions

**Date:** 2026-05-19
**Status:** Decided

### Context

In medical diagnosis, a finding can *rule out* a condition (e.g., "normal WBC count rules out bacterial infection"). In cybersecurity, a forensic result can eliminate a threat hypothesis. In supply chain, a confirmed delivery negates a "delayed" claim. These are not challenges (which question confidence) or contradictions (which assert the opposite) — they *remove a candidate from consideration entirely*.

OHM already has CHALLENGED_BY (questions confidence) and CONTRADICTS (asserts opposite). Neither captures the "ruled out" semantics cleanly. Using CONTRADICTS for this purpose conflates "I believe the opposite" with "this is eliminated from consideration."

### Decision

Add `NEGATES` as an L3 edge type. Semantics:

- `A —NEGATES→ B` means "the existence/truth of A eliminates B from consideration"
- Confidence on the NEGATES edge represents how certain the agent is that A rules out B
- NEGATES is agent-owned (ADR-003): multiple agents can independently negate or not
- `differential_diagnosis()` uses NEGATES edges to exclude ruled-out candidates from results
- Ruled-out candidates appear in results with `ruled_out=True` and `ruled_out_by=[edge_ids]` — they are not deleted, just flagged

NEGATES is placed in L3 (Knowledge) because it represents an agent's judgment about the relationship between two concepts, not a structural or flow relationship.

### Consequences

- Clean separation: CHALLENGED_BY questions confidence, CONTRADICTS asserts opposite, NEGATES eliminates from consideration
- `differential_diagnosis()` returns ruled-out candidates with provenance, not silently filtered
- Works across domains: medical (findings rule out conditions), cybersecurity (forensics eliminate hypotheses), supply chain (confirmations negate delay claims)
- NEGATES edges are challengeable — another agent can CHALLENGED_BY a NEGATES edge if they disagree with the ruling-out

---

## ADR-010: Urgency on Edges and Priority on Nodes

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM needs temporal reasoning for time-sensitive domains: customer support (SLA breaches), cybersecurity (incident response), medical (deteriorating conditions), supply chain (expiring inventory). These domains need to distinguish "how important is this thing?" (priority) from "how urgently does this relationship need attention?" (urgency).

Priority is an intrinsic property of a node — a P0 incident is always P0 regardless of which edge you approach it from. Urgency is a property of the relationship — "this ticket was escalated TO tier-2" carries urgency independent of the ticket's priority. A P3 ticket can have a critical-urgency escalation edge.

### Decision

Separate priority and urgency into different entities:

- **`priority`** on `ohm_nodes` — intrinsic importance of the node itself. Values: P0 (critical), P1 (high), P2 (medium), P3 (low), P4 (informational). Validated against `VALID_PRIORITY`.
- **`urgency`** on `ohm_edges` — time-sensitivity of the relationship. Values: low, normal, high, critical. Validated against `VALID_URGENCY`.
- `escalate()` sets `urgency="high"` on the ESCALATED_TO edge AND sets `priority="P1"` on the ticket node — both the relationship and the node reflect the escalation.
- `urgent_changes()` filters the change feed for edges with urgency ≥ the specified threshold.

Priority and urgency are advisory by default (ADR-006) — they are validated when provided but not required.

### Consequences

- Priority and urgency serve distinct analytical purposes and can evolve independently
- A node can be high-priority without any urgent edges (important but stable)
- An edge can be critical-urgency between low-priority nodes (time-sensitive but not important)
- `escalate()` correctly updates both dimensions
- Query patterns: "show me all P0 nodes" (priority filter) vs. "show me all critical-urgency edges" (urgency filter) vs. "show me P0 nodes with critical-urgency edges" (intersection)
- Future: priority could be derived from composite scoring; urgency could be auto-set by temporal decay

---

## ADR-011: Observation Type Extensibility

**Date:** 2026-05-19
**Status:** Decided

### Context

OHM's `observe()` method records observations against nodes with a required `obs_type` field. The base schema defines `VALID_OBSERVATION_TYPES = {anomaly, measurement, pattern, challenge, support, sentiment}`. As OHM expands to new domains (industrial monitoring, financial analysis, environmental tracking), each domain needs domain-specific observation types: vibration/temperature/pressure for TOPO, volatility/spread for finance, pH/dissolved_oxygen for environmental.

The question is: should observation types be an open string (any value accepted), a closed set (only registered types), or extensible with validation?

### Decision

Observation types follow the same graduated enforcement model as node types and edge types (ADR-006/007):

1. **Advisory (default)** — `VALID_OBSERVATION_TYPES` is defined in `schema.py` as a `frozenset`. The `observe()` SDK method validates against it, raising `ValueError` for unknown types. This prevents typos and ensures query consistency.
2. **Domain extension via `SchemaConfig`** — Each `SchemaConfig` instance can define its own `observation_types` set. The TOPO schema extends the base with `{vibration, temperature, pressure, flow_rate, voltage, current, rpm, level}`. Custom domains add their own types the same way.
3. **Three-stage lifecycle** (per ADR-007) — experimental types are added to a domain's `SchemaConfig` (advisory), registered types are validated in lenient mode, canonical types require a schema migration to add to `VALID_OBSERVATION_TYPES`.

The `observation_sources` field follows the same pattern with `VALID_OBSERVATION_SOURCES` and `SchemaConfig.observation_sources`.

### Consequences

- Observation types are validated, preventing typos and ensuring downstream queries can group by type
- Domains extend observation types through `SchemaConfig`, not by modifying the base schema
- The TOPO schema demonstrates the extension pattern with 8 industrial observation types
- Adding a new base observation type (e.g., "forecast") requires updating `VALID_OBSERVATION_TYPES` and a schema migration
- The `observe()` SDK method and CLI validate against the active `SchemaConfig`, not the global constant
- Future: observation types could be stored in a database table for runtime registration (currently compile-time only)

---

## ADR-012: Per-Agent Local DuckDB Cache

**Date:** 2026-05-19
**Status:** Accepted

### Context

OHM uses a single `ohmd` daemon that owns the DuckDB file and serves all agents via HTTP REST API. This creates a single-writer bottleneck — every read and write goes through HTTP, adding latency and creating a single point of failure. Each agent needs fast local access to the knowledge graph for neighborhood queries, semantic search, graph analytics, and deep content retrieval.

### Decision

Each agent gets its own local DuckDB file for zero-latency reads and writes, with periodic sync to a shared DuckLake mirror. `OhmStore.for_agent(agent_name, ducklake_path=...)` creates a per-agent store at `~/.ohm/agents/{name}/ohm.duckdb`. Agents read/write locally (no HTTP, no network) and sync with DuckLake on heartbeat via `sync_heartbeat()`.

### Consequences

- **Zero-latency reads**: All queries are local DuckDB operations (microseconds, not milliseconds)
- **No single point of failure**: If ohmd crashes, agents continue working locally
- **No daemon dependency**: Agents can read/write without ohmd running
- **Offline capability**: Agent works disconnected, syncs when reconnected
- **Same API**: `OhmStore.for_agent()` returns the same `OhmStore` object
- **Eventual consistency**: Changes from other agents visible only after sync_heartbeat()
- **DuckLake lock**: Only one process writes to DuckLake at a time; agents sync through daemon or take turns
- **ohmd becomes optional**: Still useful for HTTP-only clients and change feed
---

## ADR-013: Value of Information for Knowledge Graphs

**Date:** 2026-05-20
**Status:** Accepted

### Context

OHM knows which nodes are uncertain but not which uncertainties matter for decisions. Edge probability fields are unpopulated, making the Bayesian inference stack return empty results. The elicitation problem — how to get principled probability estimates from subjective judgment — needs a protocol.

### Decision

Use PERT three-point estimation (P05/P50/P95) as the elicitation protocol for edge probabilities. The derived PERT mean populates `probability` for Bayesian CPTs; the derived variance feeds VoI ranking (uncertainty × decision sensitivity = research priority). Add decision nodes with utility metadata. Implement `/voi` endpoint and `compute_voi()` function that traces causal paths backward from decision nodes to identify which observations would most reduce decision uncertainty.

**Implementation:** `compute_voi(conn, decision_nodes=None, edge_types=None, layers=None, top=10, leak_probability=0.15, root_prior=0.3, timeout=30, semantic_roles=None, min_observations=0)` — VoI ranked by uncertainty × sensitivity with configurable leak probability and root prior.

### Consequences

- Bayesian inference works with PERT-derived CPTs
- VoI prioritizes research by decision impact, not just gap size
- Decision nodes encode "how much does being wrong matter?"
- Agents can self-optimize: research what matters, not what's easy
- GIGO risk mitigated by conservative initial ranges and observation updates
- CLI: `ohm graph voi --decision d1,d2 --top 5 --layers L3,L4`
- SDK: `graph.compute_voi(decision_nodes, top=10)` — see `ohm.bayesian.compute_voi`

---

## ADR-015: Multi-Tenancy — Single-Process Isolated DuckDB Instances

**Date:** 2026-05-24
**Status:** Accepted

### Context

OHM currently runs as a single-tenant system. The TeamWork AI platform needs to serve multiple customers from a single deployment with complete data isolation.

### Decision

One `ohmd` process, N isolated DuckDB files, per-tenant LRU cache. Customer API keys resolve to tenant instances. Domain templates replace `topod`. Feature flag (`ENABLE_MULTI_TENANCY`) ensures backward compatibility.

### Consequences

- Strong isolation — each tenant's data is a separate DuckDB file
- Domain flexibility — each tenant uses a different SchemaConfig
- Economical — one process, LRU cache manages memory
- Single-writer serialization per tenant (concurrent reads OK)
- Horizontal scaling path: consistent-hash router + N ohmd instances
- See [full ADR](0015-multi-tenancy.md)

---

## ADR-027: BOS Internal ODPS Data Product Catalog Schema

**Date:** 2026-06-19
**Status:** Proposed

### Context

BOS (Business Operations System) agents produce structured recurring outputs (P&L, risk reports, research digests, audit summaries) with no standard way for other agents to discover or consume them. ODPS v4.1 (Linux Foundation) provides a contract layer for data products designed for AI-agent-first discovery.

### Decision

Store ODPS-compliant data product entries in a `ohm_data_products` DuckDB table with structured columns for queryable discovery plus full ODPS YAML for round-trip fidelity. Enforce 10 minimum fields (7 ODPS + 3 BOS-specific). Agent outputs map to ODPS types (reports, analytic view, decision support, data-driven service). Discovery via MCP endpoint. DuckDB-only storage (Iceberg deferred). Internal-only visibility until pilot proves discipline.

### Consequences

- Standard-based: ODPS v4.1 compliance enables portability
- Agent-discoverable: MCP endpoint for any agent to find and consume products
- Provenance-linked: `ohm_node_id` connects products to OHM graph
- Minimal friction: 10 required fields, no pricing/license infrastructure
- Iceberg-ready: `access_url` can point to Iceberg tables without schema changes
- See [full ADR](ADR-027-bos-odps-catalog-schema.md)

---

## ADR-028: Source Tier Architecture and Confidence Ceilings

**Date:** 2026-06-19
**Status:** Accepted

### Context

OHM's `confidence` field has a range check [0, 1] but no quality dimension — a 0.9 claim from a single Reddit post is indistinguishable from a 0.9 claim from a peer-reviewed replication. Cornell UGC poisoning (arxiv 2605.24245) shows user-generated content can claim institutional consensus that doesn't exist; Hillman's truth-vs-consensus framing makes institutional consensus an AND-gate on independent verifications, not an OR-gate on popularity. ADR-015's `citation_status` and ADR-018.3's 30d/365d half-life split both implicitly encode a source-quality dimension that has never been write-time enforced.

### Decision

Add a five-tier `source_tier` enum on `ohm_nodes` and `ohm_edges` with confidence ceilings: `raw` (0.3), `unverified` (0.5), `preliminary` (0.7), `official` (0.9), `verified` (1.0). NULL bypasses enforcement to preserve backward compatibility. `create_node` / `create_edge` raise `ValueError` when tier is set and confidence exceeds ceiling. No default — opt-in via the `source_tier=` parameter. Migration `0.30.0`.

### Consequences

- OHM can no longer confuse high-confidence claims with high-quality sources
- Unlocks source diversity scoring (OHM-qi6r), tier-aware verification loops (OHM-2yq2), and oppositional review (OHM-jbsr)
- Legacy write paths without tier pass NULL — protection is opt-in until callers migrate
- See [full ADR](0028-source-tier-architecture.md)

---

## ADR-029: Consensus-Only Confidence Ceilings and Auto-Challenge Nudges

**Date:** 2026-06-19
**Status:** Accepted

### Context

Hillman's truth-vs-consensus framing and Cornell UGC poisoning (arxiv 2605.24245) show that recursive agreement loops compound into false confidence. ADR-028 added per-source confidence ceilings, but a CAUSES edge can still reach high confidence when many SUPPORTS edges agree even if none have recorded outcomes. The AND-gate of independent verification is missing for the support structure.

### Decision

Detect consensus-only support (SUPPORTS edges with no recorded outcomes on their from_nodes) via `detect_consensus_only_support()`. Recommend a confidence ceiling from the strongest supporter's tier when consensus-only. Auto-fire `CONSENSUS_FLAG` challenge nudges via `fire_verification_nudge()` (idempotent, low-confidence 0.3). Surface up to 3 consensus-only edges per heartbeat. No DDL — computed property, no schema change. CAUSES edges only (Phase 1).

### Consequences

- Consensus-only edges surfaced for verification but not forcibly ceiling-clamped at write time (deferred)
- Auto-nudges capped at 3/heartbeat and idempotent to avoid spam
- Bridges ADR-028 (source tier ceilings) and ADR-018 (verification loops)
- See [full ADR](0029-consensus-verification-loops.md)

---

## ADR-030: Oppositional Review Pipeline

**Date:** 2026-06-19
**Status:** Accepted

### Context

Atlas framing: oppositional review is the institutional substitute for a dissenting peer reviewer. CAUSES edges whose support is homogeneous in source tier or agent authorship can form recursive agreement loops. ADR-028 made `source_tier` available; this ADR uses it as the first homogeneity dimension.

### Decision

`find_homogeneous_causes()` detects L3 CAUSES edges whose SUPPORTS edges share a single `source_tier` (homogeneity_score ≥ 0.8, min 2 supporters). `oppositional_review()` orchestrates detection + optional auto-challenge (default off, budget 3/run, confidence 0.3). Synthesis hook runs review on new clusters non-fatally. Phase 1 uses `source_tier` + `agent_authorship` only. No DDL — reuses existing columns. `reviewer_agent` defaults to `system_oppositional` to avoid self-challenge paradox.

### Consequences

- Flag-only default prevents over-charging on small homogeneous graphs
- Phase 2 adds `source_diversity_score` + lexical similarity (OHM-qi6r); Phase 3 adds embedding clusters (OHM-wvz8.2)
- Auto-challenges are low-confidence and clearly labeled; ADR-018 verification loops apply
- See [full ADR](0030-oppositional-review-pipeline.md)

---

## ADR-031: Hyperdimensional Fingerprinting Prototype

**Date:** 2026-06-19
**Status:** Accepted

### Context

OHM's semantic search (768-dim float embeddings + cosine distance) captures meaning but not structural/type similarity. Hyperdimensional computing with 10,000-bit binary hypervectors (XOR binding, majority-rule bundling, Hamming similarity) provides a complementary membership signal. This is a compute-on-demand prototype — no DDL, no persistent storage.

### Decision

Pure-Python HD module at `src/ohm/inference/hd.py`. 10,000-bit hypervectors, seeded SHA-256 counter-mode for determinism, XOR bind/unbind, majority-rule bundle. Node fingerprint = bind(type, label) ⊕ bundle(content, tags, provenance). Text fingerprint = majority-rule of whitespace-token vectors. Naive O(n²) all-pairs search. Version tag `tastebud_hd_v1`. No DDL / no BLOB column (deferred to OHM-wvz8.2).

### Consequences

- Fast approximate structural similarity complementary to semantic search
- Deterministic and reproducible (same input + seed = same vector)
- All-pairs search won't scale past ~1K nodes without indexing (wvz8.2)
- Primitive tokenization (whitespace split, no TF-IDF) — iterated later
- See [full ADR](0031-hd-fingerprint-prototype.md)

---

## ADR-032: HD Membership Layer — Persistent Fingerprints in DuckDB

**Date:** 2026-06-19
**Status:** Accepted

### Context

ADR-031's compute-on-demand prototype recomputes all fingerprints per query and loses them on DB restart. Persistent storage eliminates redundant work and enables membership search without recomputation overhead.

### Decision

Add `hd_fingerprint BLOB` column to `ohm_nodes` (nullable, default NULL). Schema version `0.31.0` with idempotent ALTER TABLE. Partial index `WHERE hd_fingerprint IS NOT NULL`. `VALID_HD_DIMENSIONS = frozenset({10_000})` for validation. `validate_hd_fingerprint` checks byte length = `(dim+7)//8`. Opt-in storage via `update_node_hd_fingerprint` (not auto-populated on create). `hd_membership_search` fetches stored BLOBs, computes Hamming similarity in Python (DuckDB lacks BLOB XOR). `batch_update_hd_fingerprints` for bulk migration. Complementary to `semantic_search` (cosine on FLOAT[768]), not replacing it.

### Consequences

- Fingerprints survive DB restarts (unlike ADR-031 prototype)
- Search is O(n) fetch + compare vs. O(n) with recomputation
- Still brute-force Python-side; no HNSW for Hamming distance
- 1.25 KB/node storage overhead
- Future: `hybrid_search` combining cosine + Hamming with configurable weights

- See [full ADR](0032-hd-membership-layer.md)

---

## ADR-033: Source Diversity Score — Independence-Weighted Shannon Entropy

**Date:** 2026-06-19
**Status:** Accepted

### Context

Cornell UGC poisoning (arxiv 2605.24245) shows deep-research agents poisoned when many UGC sources agree. Hillman's truth-vs-consensus framing identifies this as peer-review capture — "many agree" is not "independently verified." ADR-028/029/030 address quality ceilings and consensus detection but cannot measure whether sources are independent. Ten SUPPORTS edges from the same institution are one position, not ten verifications.

### Decision

Three nullable columns on `ohm_nodes`: `source_author`, `source_institution`, `data_origin` (validated against `VALID_DATA_ORIGINS` frozenset). Schema version `0.32.0`. `source_diversity_score` computes weighted normalized Shannon entropy over evidence sources (walk CAUSES/SUPPORTS/EXPECTS/PREDICTS depth=3): `score = 0.4*H(author) + 0.4*H(institution) + 0.2*H(origin)`. Falls back to `created_by` when `source_author` is NULL. Computed-on-read, annotates synthesis response. Four-layer plumbing (queries + store + SDK + server). Backward compatible (None defaults).

### Consequences

- Detects homogeneous UGC citation rings (low score → capture signal)
- Distinguishes "many agree" from "independently verified" — the Hillman AND-gate
- Requires agents to populate source_author/institution for full utility
- Annotation only, not enforcement — low diversity feeds oppositional review (ADR-030)
- See [full ADR](0033-source-diversity-score.md)

---

## ADR-034: Emerging Concept Detection via HD Fingerprint Residual Mass

**Date:** 2026-06-19
**Status:** Accepted

### Context

OHM agents create nodes that may represent genuinely novel concepts the graph has not yet named. ADR-031/032 gave us persistent HD fingerprints with Hamming similarity, but no automated signal for structural novelty. The residual mass (1 − max_similarity_to_concepts) of a node's fingerprint against all stored fingerprints detects emerging concepts before they are named.

### Decision

`residual_mass = 1 - max_similarity_to_concepts`. Stability = residual_mass when total_observations ≥ 3, else 0.0. Stability threshold 0.45 for promotion (below random similarity floor ~0.5 for 10K-bit vectors). `emerging_concept_score JSON` column on `ohm_nodes` stores status lifecycle: unnamed → naming_candidate → named/rejected. `promote_emerging_concept()` gated on stability ≥ 0.45. `detect_unknown_ingredients()` scans for high-residual-mass nodes with sufficient evidence. Schema version 0.33.0.

### Consequences

- Automated detection of structurally novel nodes — no manual scanning
- Residual mass grounded in HD theory (random floor ~0.5)
- Evidence gate (≥3 observations) prevents noise promotion
- O(n²) scan in `detect_unknown_ingredients()` — needs indexing at scale
- JSON column not SQL-queryable without json extension functions
- See [full ADR](0034-emerging-concept-detection.md)

---

## ADR-035: TELOS Signing — Cryptographic Audit Trail for Agent Writes

**Date:** 2026-06-19
**Status:** Accepted

### Context

OHM's `created_by` column attributes writes to agents but is a plain string — any agent can set `created_by="metis"` and the graph cannot detect forgery. ADR-003's boundary enforcement depends on `created_by` authenticity. ADR-017's encryption at rest protects confidentiality but not integrity. The graph needs a cryptographic audit trail: a signature over each write that can be verified later to prove the write came from a key holder.

### Decision

Three nullable columns on `ohm_nodes` and `ohm_edges`: `write_signature` (VARCHAR, algorithm-prefixed hex), `signing_key_id` (VARCHAR), `signed_at` (TIMESTAMP). NULL defaults = unsigned writes valid (flag, not reject). Canonical payload = sorted-key JSON of whitelisted fields (`NODE_FIELDS` / `EDGE_FIELDS`). HMAC-SHA256 default (stdlib only, `hmac` + `hashlib`). Ed25519 optional via `pynacl`. `sign_node_write` / `sign_edge_write` + `verify_node_write` / `verify_edge_write` in queries. SDK `_signing_key` property + `sign_node` / `sign_edge` / `verify_node` / `verify_edge` methods. Schema version 0.33.0. Partial indexes on `signing_key_id`. Graduated enforcement: Phase 1 advisory (opt-in), Phase 2 per-agent flag, Phase 3 boundary enforcement.

### Consequences

- Tamper evidence — post-signature modification invalidates signature
- Zero-dependency default (HMAC-SHA256 via stdlib)
- Backward compatible — NULL defaults, unsigned writes pass through
- Post-hoc signing (not write-time) leaves unsigned window; Phase 2 mitigates
- Canonical payload whitelist must be maintained as schema evolves
- See [full ADR](0035-telos-signing.md)

---

## ADR-036: Ripen-Then-Decide Triage for Suggestions

**Date:** 2026-06-19
**Status:** Accepted

### Context

Agents and substrate methods produce candidate edges that should not be written directly to `ohm_edges`. Writing immediately pollutes the canonical graph with unverified suggestions and inflates confidence through recursive agreement (ADR-029). A staging area with automated triage that accumulates evidence over time bridges the gap.

### Decision

`ohm_suggestions` table with lifecycle: `ripe → promoted | expired | rejected`. `compute_ripeness = time_factor * evidence_factor * confidence_factor` (inverted decay — ripeness GROWS with age). `ripen_then_decide`: ripen all, auto-promote ≥ threshold (0.7), auto-expire stale (>30 days). Duplicate prevention: same `(from_node, to_node, target_node)` → increment `evidence_count`. Promoted suggestions create real edges in `ohm_edges`.

### Consequences

- Canonical graph stays clean — only promoted edges enter it
- Evidence accumulates across agents — three independent suggestions = one strong suggestion
- Auto-promote/auto-expire reduce manual triage burden
- Multiplicative ripeness enforces AND-gate: time + evidence + confidence all required
- See [full ADR](0036-suggestions-lifecycle.md)

---

## ADR-037: Per-Agent Read Scopes and Temporal Pinning

**Date:** 2026-06-19
**Status:** Accepted

### Context

ADR-003 enforces write boundaries but has no read-side equivalent — every agent reads everything. Soft-deleted items leaked into `query_snapshot` (no `deleted_at` filter). No point-in-time read capability exists without DuckLake (OHM-xgm).

### Decision

`read_scope` JSON column on `ohm_agent_config` with four dimensions: `layer`, `source_tier`, `created_by`, `node_id`. NULL = full access (backward compat). `enforce_read_scope()` in `boundary.py` — read-side parallel to ADR-003's `enforce_write_boundary()`. Bug fix: `query_snapshot` now filters `deleted_at` (soft-deleted items excluded from historical snapshots). Temporal pinning via `created_at <= timestamp` — full time-travel deferred to DuckLake (OHM-xgm). Schema version 0.33.0.

### Consequences

- Agents restricted to their trust boundary (layer, tier, provenance)
- Multi-tenant agents scoped to `created_by: ["customer:{id}"]`
- Soft-delete contract now consistent across all query paths
- Temporal pinning is `created_at`-only, not MVCC — updated values after `as_of` are visible
- See [full ADR](0037-read-scopes-temporal-pinning.md)

---

## ADR-039: Bedrock Knowledge Store — Write-Through Wrapper for Managed Embeddings

**Date:** 2026-06-21
**Status:** Accepted

### Context

OHM's document library has `LocalDocumentStore` and `S3DocumentStore` backends. AWS Bedrock Knowledge Bases provide managed embeddings and agentic RAG, but Bedrock KB is a retrieval service, not a raw document store — `get`/`exists` semantics don't map. OHM still needs raw bytes for its own ingestion pipeline.

### Decision

`BedrockKnowledgeStore` is a write-through wrapper (not standalone): wraps an inner `DocumentStore`, delegates all reads to it, syncs to Bedrock KB on `save()`. Two sync strategies: S3 reference mode (trigger ingestion job on configured S3 data source) and direct upload mode (`IngestKnowledgeBaseDocuments` API). Bedrock sync failure does not block document persistence (graceful degradation). Selected via `OHM_DOCUMENT_STORE=bedrock`. New `aws` optional dependency group (`boto3>=1.34.0`). `OHM_BEDROCK_KB_ID` required. `bedrock` config section added to `DEFAULT_CONFIG`.

### Consequences

- Managed embeddings without maintaining a vector index
- S3 reference mode avoids double-upload
- Graceful degradation: local persistence succeeds even if Bedrock sync fails
- `boto3` imported lazily — zero overhead for non-AWS deployments
- Fire-and-forget sync means eventual consistency between local store and KB
- See [full ADR](0039-bedrock-knowledge-store.md)

---

## ADR-040: TOPO Observation Lifecycle — Domain DDL Tables (Option A)

**Date:** 2026-07-02
**Status:** Decided

### Context

TOPO's observation system uses a 5-table relational model (observations, assessments, annotations, followups, prospects) with append-only assessment history and `is_current` flags. OHM's core `ohm_observations` is flat. The domain DDL hook (OHM-vl8o / `SchemaConfig.domain_tables`) provides a first-class mechanism for domain-specific tables.

### Decision

Option A: declare the 4 TOPO observation lifecycle tables as `DomainTable` instances in `SchemaConfig.topo()` and `topo.json`, created by `_create_domain_tables()` during `initialize_schema()`. Preserves relational integrity, append-only semantics, and `is_current` flagging. No `SCHEMA_VERSION` bump — domain tables are per-config, not core schema.

### Consequences

- TOPO's relational observation model preserved as domain DDL tables
- Tables created idempotently via `CREATE TABLE IF NOT EXISTS`
- Application-layer integrity enforcement (consistent with OHM — no FK constraints in DuckDB)
- `topo.json` template and `SchemaConfig.topo()` Python factory kept in sync
- See [full ADR](0040-topo-observation-lifecycle.md)

---

## ADR-041: Temporal Event Model — Intervals, Plans, and Horizons

**Date:** 2026-07-02
**Status:** Decided

### Context

TOPO needs to represent time-bounded events with bounded durations, horizons (`HISTORICAL`/`CURRENT`/`PLANNED`/`FORECAST`), event classes, operating states, and plan grouping. OHM's core `ohm_observations` is point-in-time and not a fit for intervals.

### Decision

Pilot the model as TOPO DomainTables (`topo_plans`, `topo_events`, `topo_event_links`) immediately, while designing generic `ohm_intervals` / `ohm_plans` primitives in parallel. Promote to core once semantics stabilize, with a documented migration path. Rejected Option C (extend `ohm_observations` with JSON) because it would lose queryability and the interval-vs-measurement distinction.

### Consequences

- TOPO unblocks immediately with schema-managed, DuckLake-mirrored temporal tables
- Other OHM consumers are not forced to adopt immature temporal primitives
- Core schema team can design `ohm_intervals` / `ohm_plans` with real field evidence
- Future migration is acknowledged and planned, not deferred as technical debt
- See [full ADR](0041-temporal-event-model.md)

---

## ADR-042: Instance Registry and Monitoring for Local Agent Mesh

**Date:** 2026-07-08
**Status:** Accepted

### Context

In a small-team multi-agent mesh, multiple OHM instances run on a host (one ohmd daemon, per-agent local stores, remote instances). Operators and agents need to discover which instances exist, their health, and sync status. No centralized registry existed — discovery was tribal knowledge passed through environment variables and config files.

### Decision

Local-first, pull-based registry: each ohmd exposes no-auth `GET /instance` with structured metadata (instance_id, version, purpose, multi_tenant, tenants, domain_configs, listen_url, DuckLake sync status, uptime, agent_count). `ohm instances discover` CLI scans well-known locations (127.0.0.1:8710, `OHM_URL` env, `~/.ohm/agents/*/ohm.json`, `/etc/ohm/ohmd*.json`) and probes each with `GET /instance`, writing a registry JSON to `~/.ohm/registry.json` consumable by SDK and MCP. Prometheus `/metrics` endpoint exposes graph counts, uptime, request stats, and DuckLake sync lag. `ohm instances health` re-probes all registered instances. MCP tool `ohm_list_instances` exposes the registry to agents.

### Consequences

- Single command reveals every reachable OHM instance — no more tribal knowledge of URLs
- Agents discover the correct endpoint programmatically (SDK reads registry JSON; MCP agents call `ohm_list_instances`)
- Prometheus `/metrics` plugs into standard scraping/alerting with graph counts, latency, and DuckLake sync lag
- Discovery is pull-based — no push registration; remote instances outside well-known configs must be added manually until push is added
- Registry is local-first (no central server), consistent with OHM's local-DuckDB architecture
- See [full ADR](0042-instance-registry-monitoring.md)

---

## ADR-043: Agent Profiles — Multi-Instance Access for a Single Agent

**Date:** 2026-07-08
**Status:** Accepted

### Context

In a small-team multi-agent mesh, a single agent may need to access multiple OHM instances: different tenants on the same `ohmd`, separate `ohmd` daemons, or a mix of local and remote instances. Today, selecting the right store means hardcoding URLs, tokens, and tenant IDs in every call site. ADR-042 solved discovery (which instances exist); this ADR solves connection (how an agent picks and uses the right one for each operation). A single-profile version already exists in the MCP server (`mcp/config.py`); profiles are its multi-profile, SDK-and-CLI-facing generalization.

### Decision

Agent Profiles: a JSON catalog file (`.ohm/profiles.json` project-level, `~/.ohm/profiles.json` user-level) of named profiles, each with `ohm_url`, `tenant_id`, `token`, `agent_id`, `domain_config`, `allowed_tools`, `read_only`, `token_type`, and a `default` flag. Profile routing: if `ohm_url` is present, connect via HTTP (`connect_http`); if absent, connect to local DuckDB (`connect`). If `tenant_id` is present, route to that tenant (`agent` tokens send `X-Tenant-ID`; `customer` keys are already tenant-scoped, header omitted). Selection: explicit (`--profile devops`), heuristic (future: repo/file-based), or default (the profile marked `default: true`). CLI: `ohm profile list/show/use` commands; global `--profile` flag for `ohm --profile devops graph search`. SDK: `Graph.from_profile(name)` loads the catalog and returns a context manager. Tokens support `${ENV_VAR}` interpolation so committed catalogs carry no secrets. Profiles compose with server-side multi-tenancy (ADR-015) — a profile routes to a tenant/daemon/file; the server still enforces isolation and boundaries.

### Consequences

- Single agent seamlessly works across dev/sec/ops tenants and local/remote instances with no code changes — switching is a `--profile` flag or `from_profile(name)` call
- Profile catalog is declarative and version-controllable (`.ohm/profiles.json`); tokens interpolated from env vars so no secrets committed
- Pure client-side — no server changes; profiles resolve into existing `connect` / `connect_http` calls and reuse `mcp/config.py` tool-filtering / tenant-header semantics
- `read_only` and `allowed_tools` add client-side guardrails on top of server-side boundary enforcement (ADR-003/037)
- Token still lives in env var or (worst case) user-level catalog; `${ENV_VAR}` interpolation is the documented pattern, `ohm profile show --reveal` is the only token-printing command
- Two catalog locations (project + user) introduce a merge order; `ohm profile show` prints the source of each resolved field for debuggability
- Profiles compose with — do not replace — ADR-015 multi-tenancy and ADR-042 instance registry
- See [full ADR](0043-agent-profiles-tenants.md)
