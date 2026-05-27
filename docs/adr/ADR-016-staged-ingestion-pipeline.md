# ADR-016: Staged Ingestion Pipeline

**Status:** Accepted
**Date:** 2026-05-27
**Author:** Metis

## Context

OHM needs a pipeline to ingest external sources (RSS, SearXNG, Karakeep) and convert them into observations. Without one, the graph grows only through manual agent writing — too slow for 2000+ observations.

The risk: automated ingestion without agent review floods the graph with noise. Every Reuters headline becomes a node, every reference page becomes a source, and the signal-to-noise ratio collapses.

A second risk: token cost. Running a strong model on every ingested item wastes intelligence on items that don't deserve it.

## Decision

Implement a five-stage pipeline with agent gates at each level:

### Stage 1: Ingest (zero tokens)
Fetch + parse + deduplicate from RSS feeds and SearXNG. Mechanical. No judgment needed.

### Stage 2: Triage (~50 tokens per item)
Cheap model (GLM-5) makes two binary calls: **Relevant?** and **Novel?** Items must pass BOTH gates. Currently uses keyword matching as fallback (zero tokens); model integration deferred.

### Stage 3: Source Node (zero tokens)
Auto-create source nodes for items that pass triage. Provenance: `feed-ingest`. Tags: category + domain. This is mechanical — no judgment needed.

### Stage 4: Assess (~300 tokens per item)
Domain agent reads the full article and decides:
- Does this update any observation values?
- Does this create new causal links?
- Does this challenge existing edges?

Only the agent can promote feed-ingest sources to L3 knowledge.

### Stage 5: Synthesize (~1000 tokens, rare)
Strong agent identifies patterns from clusters of 3+ assessed items. Expensive but rare — maybe once per day.

## Token-Value Ladder

| Stage | Model | Token Cost | Decision Value |
|-------|-------|-----------|----------------|
| 1. Ingest | Code | 0 | None (mechanical) |
| 2. Triage | Cheap (GLM-5) | ~50 | Binary filter |
| 3. Source | Code | 0 | None (mechanical) |
| 4. Assess | Medium (Metis) | ~300 | Domain judgment |
| 5. Synthesize | Strong (Clio) | ~1000 | Pattern revelation |

**Efficiency principle:** Most items die at Stage 2 for 50 tokens. Only survivors reach Stage 4+.

## Credibility Management

- Source reliability tracked via `record_outcome()` in OHM
- Sources with `p_accurate < 0.5` are downweighted automatically
- Feed sources start at trust=0.4 (SearXNG) or trust=0.5 (RSS)
- Agent can override trust score during assessment

## Consequences

- Graph grows faster without noise contamination
- Token cost scales with O(passed_items), not O(fetched_items)
- Agent curation preserved: feed-ingest NEVER auto-promotes to L3
- Source reliability creates a feedback loop for triage quality

## Implementation

`/root/olympus/OHM/scripts/ingestion/ingestion_pipeline.py`

Queue directories: `/var/lib/ohm/ingestion/{raw,triage_pass,triage_fail,source_created,assessed}`

CLI: `python3 ingestion_pipeline.py --stage {fetch|triage|source|assess|full|queue-status|drain-triage}`