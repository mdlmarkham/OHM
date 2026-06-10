# ADR-025: Conversational Analytics, Challenge Metadata, Binary Scale

**Date**: 2026-06-10
**Status**: Accepted
**Authors**: metis

## Context

Three related issues surfaced during operational testing:

1. **AND-gate interface problem**: OHM's API requires agents to know (a) which endpoint to call, (b) the Bayesian network structure, and (c) which node ID to target. This is a three-input AND-gate — failure in any condition means no insight. Agents developing dashboards or answering questions conversationally hit this barrier immediately.

2. **Challenge type stored as edge_type**: When calling `POST /challenge/{edge_id}` with `challenge_type="CONTRADICTS"`, the handler created an edge with `edge_type="CONTRADICTS"` instead of `edge_type="CHALLENGED_BY"` with challenge_type as metadata. This violated the invariant that all challenge edges have type `CHALLENGED_BY`.

3. **Binary scale rejected**: The `/observe/{node_id}` endpoint rejected `scale="binary"` even though binary observations (true/false, yes/no) are a natural fit for many use cases (event occurrence, prediction verification).

## Decision

### 1. POST /ask — Conversational Analytics Endpoint

Add a `/ask` endpoint that converts OHM's AND-gate interface into an OR-gate. A single natural language question triggers:

- **Direct ID lookup** — If the question matches a known node ID, use it immediately
- **Text search** — ILIKE pattern matching on node labels and content
- **Semantic search** — Embedding similarity via Ollama mxbai-embed-large
- **Fuzzy fallback** — Jaro-Winkler similarity if exact and prefix fail
- **Neighborhood expansion** — Depth-2 traversal of matched nodes for context
- **Bayesian inference** — Optional Variable Elimination on up to 3 target nodes found via causal edges
- **Challenge check** — Surface CHALLENGED_BY edges on relevant nodes
- **Synthesis** — Structured response with posteriors, challenges, confidence score, and source IDs

Input:
```json
{
  "question": "What is the probability of a Hormuz deal?",
  "agent": "metis",
  "depth": 2,
  "include_inference": true,
  "limit": 5
}
```

Output:
```json
{
  "question": "What is the probability of a Hormuz deal?",
  "matched_nodes": [...],
  "neighborhood": {"nodes": [...], "edges": [...]},
  "inference_results": {"hormuz_and_gate": {"posterior": {"good": 0.025, "bad": 0.975}, "n_nodes": 50, "n_edges": 69}},
  "challenges": [...],
  "synthesis": "...",
  "confidence": 0.65,
  "sources": ["hormuz_and_gate", "concept-truce-treadmill", ...]
}
```

This is AND→OR domain #49: the analytics interface AND-gate (access + literacy + relevance) becomes an OR-gate (ask any question, the system finds relevance).

### 2. Challenge Type as Metadata

The `/challenge/{edge_id}` endpoint now:
- Always creates edges with `edge_type="CHALLENGED_BY"`
- Stores the caller's `challenge_type` (e.g., "CONTRADICTS", "REFUTES", "QUESTIONS") as metadata in the edge's `provenance` field
- Preserves the invariant: all challenge edges have type `CHALLENGED_BY`

### 3. Binary Scale Support

The `/observe/{node_id}` endpoint now accepts `scale="binary"`:
- Binary observations are normalized to `scale="probability"` with value mapping: `1.0 → 1.0` (true), `0.0 → 0.0` (false)
- Bulk observations (`POST /observations`) also support binary scale
- `VALID_OBSERVATION_SCALES` expanded to include "binary"

## Consequences

### Positive
- Agents can query OHM without knowing endpoint structure or node IDs
- Bayesian inference is accessible through natural language questions
- Challenge edges maintain consistent type invariant
- Binary observations no longer require manual normalization

### Negative
- `/ask` response time depends on inference complexity (capped at 3 targets)
- Semantic search requires Ollama embedding service (graceful fallback if unavailable)
- Challenge type information is in provenance, not a first-class field (acceptable for now)

### Risks
- Agents may over-rely on `/ask` instead of building targeted queries
- Synthesis quality depends on search relevance — poor matches yield poor synthesis
- Confidence scoring is heuristic-based (match quality + inference certainty + challenge count)