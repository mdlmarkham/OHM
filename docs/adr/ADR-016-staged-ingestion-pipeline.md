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

## Hook Architecture (OHM-aznh)

The staged pipeline is extended with a deterministic hook system that runs
before and after graph writes and reads. Hooks are registered in the
`ohm_hooks` table and executed by `HookRunner`.

### Hook Lifecycle

```
pre_ingest → [WRITE] → post_ingest
pre_query  → [READ]  → post_query
```

For writes (POST /node, POST /edge):
1. `pre_ingest` hooks run. Any non-zero exit aborts the write (422).
2. The write executes.
3. `post_ingest` hooks run. JSON stdout merged into `hook_decorations`.
   Failures logged but do NOT abort.

For reads (GET endpoints):
1. `pre_query` hooks run. Any non-zero exit returns 403 (query blocked).
   JSON stdout can override `query_params`.
2. The query handler executes.
3. `post_query` hooks run. JSON stdout merged into `hook_decorations`.
   Failures logged but do NOT block the response.

### Hook Events

| Event | Trigger | Can abort? | Stdout effect |
|-------|---------|-----------|---------------|
| `pre_ingest` | Before POST /node, /edge | Yes (422) | Ignored |
| `post_ingest` | After successful write | No | Merged as `hook_decorations` |
| `pre_query` | Before GET handler | Yes (403) | `query_params` override |
| `post_query` | After GET handler | No | Merged as `hook_decorations` |

### Hook Registration

- `POST /hooks` — Register a hook (event, command, timeout_ms, created_by)
- `GET /hooks` — List hooks (filter by event)
- `DELETE /hooks/{id}` — Remove a hook

### Hook Payloads

**pre_ingest / post_ingest:**
```json
{
  "agent": "metis",
  "action": "node" | "edge",
  "body": { /* node or edge body */ },
  "__conn": "<DuckDB connection, python: hooks only>"
}
```

**pre_query / post_query:**
```json
{
  "agent": "metis",
  "path": "/stats",
  "query_params": { /* parsed query string */ },
  "response_body": { /* post_query only */ },
  "__conn": "<DuckDB connection, python: hooks only>"
}
```

### Exit Code Semantics

- `0` — Pass (no action; stdout may carry decorations)
- Non-zero — Reject (pre hooks abort the operation; post hooks log warning)
- `124` — Timeout (hook exceeded `timeout_ms`)
- `127` — Command not found (shell hooks only)

### Hook Types

**Shell hooks:** `command` is a shell command. Payload written to stdin as JSON.
stdout/stderr captured. Timeout enforced via `subprocess.Popen`.

**Python hooks:** `command` starts with `python:`. The module is imported and
the named function called directly. Signature: `def hook(payload: dict) -> tuple[int, str, str]`.
The `__conn` key is injected into the payload for DB access.

### Built-in Hooks (`ohm.hooks_builtin`)

| Hook | Event | Purpose |
|------|-------|---------|
| `cross_link_check` | pre_ingest | Rejects derived-claim nodes without `connects_to` (ADR-018) |
| `source_url_required` | pre_ingest | Rejects source nodes without `source_url` |
| `rate_limit` | pre_ingest | Per-agent write rate limiting |

### Audit Trail

All hook invocations are logged to `ohm_hook_log` with:
- hook_id, event, payload, exit_code, stdout, stderr, duration_ms, timed_out

### Security Model

- Shell hooks run in the host environment (no sandbox yet — OHM-aznh.8)
- Python hooks run in-process with full DB access
- `__conn` injection is only available to `python:` prefix hooks
- Hook execution is synchronous — slow hooks block the request
- Timeout enforcement prevents runaway hooks