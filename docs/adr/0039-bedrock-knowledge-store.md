# ADR-039: Bedrock Knowledge Store — Write-Through Wrapper for Managed Embeddings

**Date:** 2026-06-21
**Status:** Accepted
**Related issues:** OHM-tmtm (this work), ADR-016 (staged ingestion pipeline), ADR-015 (multi-tenancy — per-tenant KB isolation)

## Context

OHM's document library (`src/ohm/documents/store.py`) provides a `DocumentStore` ABC with two concrete backends: `LocalDocumentStore` (filesystem) and `S3DocumentStore` (AWS S3 / MinIO). Documents flow through `ingest_file()` (`src/ohm/documents/ingest.py:17`) which persists raw bytes via the store, then extracts text for the graph.

AWS Bedrock Knowledge Bases provide managed embeddings and agentic RAG retrieval — exactly the kind of managed vector index that OHM's ingestion pipeline would otherwise need to build and maintain itself. The question is how to integrate Bedrock KB without duplicating storage or breaking the existing document pipeline.

The key constraint: Bedrock KB is a retrieval service, not a raw document store. It does not provide `get(document_id) → bytes` or `exists(document_id) → bool` semantics. OHM still needs the raw bytes for its own document tree ingestion, content extraction, and provenance tracking. A standalone `BedrockKnowledgeStore` implementing the full `DocumentStore` interface would be impossible — the `get` and `exists` methods have no Bedrock API to call.

## Decision

### 1. BedrockKnowledgeStore is a write-through wrapper, not a standalone DocumentStore

`BedrockKnowledgeStore` (`src/ohm/documents/store.py:310`) wraps an inner `DocumentStore` and delegates all read operations (`get`, `exists`, `get_record`, `update_metadata`, `delete`) to it. On `save()`, the document is persisted via the inner store first, then synced to Bedrock KB. If the Bedrock sync fails, the document is still persisted locally — graceful degradation.

```python
class BedrockKnowledgeStore(DocumentStore):
    def __init__(self, inner_store=None, knowledge_base_id=None, ...):
        self.inner = inner_store or self._default_inner_store()
        self._s3_reference_mode = isinstance(self.inner, S3DocumentStore)
        ...

    def save(self, document_id, filename, content_bytes, content_type):
        record = self.inner.save(...)          # always succeeds
        try:
            self._sync_to_bedrock(...)          # fire-and-forget
        except Exception as exc:
            record["bedrock_sync_status"] = "failed"
            record["bedrock_sync_error"] = str(exc)
        else:
            record["bedrock_sync_status"] = "synced"
        return record

    def get(self, document_id):       return self.inner.get(document_id)
    def exists(self, document_id):    return self.inner.exists(document_id)
```

### 2. Two sync strategies

| Strategy | Trigger | Bedrock API | When used |
|----------|---------|-------------|-----------|
| **S3 reference** | `inner` is `S3DocumentStore` AND `data_source_id` is set | `bedrock-agent:StartIngestionJob` | S3-stored documents are already in the KB's configured S3 data source; no re-upload needed |
| **Direct upload** | Otherwise (inner is `LocalDocumentStore` or no `data_source_id`) | `bedrock-agent-runtime:IngestKnowledgeBaseDocuments` | Push document content inline to the KB |

S3 reference mode avoids double-upload: the document lands in S3 once (via `S3DocumentStore.save`), and the Bedrock KB's data source ingests from that S3 prefix. Direct upload mode is the fallback for local-only deployments.

### 3. Metadata propagation

`ingest_file()` passes a `metadata` dict to `store.save()`. The wrapper translates it into Bedrock `IN_LINE_ATTRIBUTE` metadata on each document:

| OHM field | Bedrock attribute type | Value |
|---|---|---|
| `ohm_document_id` | STRING | document id |
| `ohm_filename` | STRING | original filename |
| `ohm_content_type` | STRING | MIME type |
| `ohm_provenance` | STRING | provenance tag |
| `ohm_source_url` | STRING | URL source (if provided) |
| `ohm_tags` | STRING_LIST | tags list |

This makes Bedrock retrieval results OHM-aware: retrieved chunks carry the document id so they can be reconciled with the OHM graph.

### 4. Retrieval

The wrapper exposes two retrieval methods that call `bedrock-agent-runtime`:

- `retrieve(query, number_of_results=5, filters=None)` — raw vector search returning retrieval results.
- `retrieve_and_generate(query, model_arn=None, number_of_results=5, filters=None)` — RAG response generation.

These are surfaced through HTTP endpoints:

- `POST /documents/bedrock/retrieve`
- `POST /documents/bedrock/retrieve-and-generate`

### 5. Explicit backfill sync

`sync_existing_document(document_id)` reads a document already stored in the inner store and pushes it to Bedrock. This is exposed as:

- `POST /documents/{id}/sync-to-bedrock`

Useful when Bedrock is enabled after documents already exist, or when an automatic sync failed.

### 6. Configuration

Selected via `OHM_DOCUMENT_STORE=bedrock` env var, consistent with existing `local`/`s3` backends (`src/ohm/server/handlers/documents.py:93`).

| Env var | Required | Purpose |
|---------|----------|---------|
| `OHM_DOCUMENT_STORE` | Yes for auto-sync | Set to `bedrock` to activate |
| `OHM_BEDROCK_KB_ID` | Yes | Bedrock Knowledge Base ID |
| `OHM_BEDROCK_DATA_SOURCE_ID` | S3 ref mode | Data source ID inside the KB (required for S3 reference mode, optional for direct upload) |
| `OHM_BEDROCK_REGION` / `AWS_REGION` | No | Region (default: `us-east-1`) |
| `OHM_BEDROCK_MODEL_ARN` | No | Default model for `retrieve_and_generate` |

The `bedrock` config section in `DEFAULT_CONFIG` (`src/ohm/server/server.py:68`):

```python
"bedrock": {
    "knowledge_base_id": "",  # OHM_BEDROCK_KB_ID env var
    "data_source_id": "",     # OHM_BEDROCK_DATA_SOURCE_ID env var
    "region": "us-east-1",   # AWS_REGION / OHM_BEDROCK_REGION
},
```

### 7. Dependency group

`aws` optional dependency group in `pyproject.toml:25`:

```toml
[project.optional-dependencies]
aws = [
    "boto3>=1.34.0",
]
```

`boto3` is imported lazily inside `__init__` (not at module level), so the package loads without `aws` extras installed — only `BedrockKnowledgeStore()` and `S3DocumentStore()` raise at instantiation time.

## Mapping to existing concepts

| Existing concept | Relationship |
|------------------|-------------|
| `DocumentStore` ABC (`store.py:21`) | `BedrockKnowledgeStore` implements the same interface; all read ops delegate to inner |
| `LocalDocumentStore` (`store.py:43`) | Default inner store when `OHM_DOCUMENT_STORE` is unset or `local` |
| `S3DocumentStore` (`store.py:156`) | Inner store when `OHM_DOCUMENT_STORE=s3`; triggers S3 reference mode |
| `ingest_file()` (`ingest.py:17`) | Passes metadata to `save()`; Bedrock sync is transparent |
| `OHM_DOCUMENT_STORE` env var | Extended from `{local, s3}` to `{local, s3, bedrock}` |
| `DEFAULT_CONFIG` (`server.py:43`) | `bedrock` section added for KB ID, data source ID, region |
| ADR-016 staged ingestion | Bedrock sync occurs at the `save()` step; downstream extraction and graph writes are unaffected |
| ADR-015 multi-tenancy | Future: per-tenant KB IDs via `bedrock.knowledge_base_id` in tenant config |

## HTTP routes

Added to `src/ohm/server/server.py`:

- `POST /documents/upload` (existing) — auto-syncs when `OHM_DOCUMENT_STORE=bedrock`
- `POST /documents/{id}/sync-to-bedrock` — backfill/retry existing documents
- `POST /documents/bedrock/retrieve` — query the KB
- `POST /documents/bedrock/retrieve-and-generate` — RAG response

## Consequences

**Positive:**
- Managed embeddings and RAG retrieval without maintaining a vector index — Bedrock KB handles chunking, embedding, and retrieval
- S3 reference mode avoids double-upload when documents are already in S3
- Graceful degradation: Bedrock sync failure does not block document ingestion (fire-and-forget with error logging)
- Backward compatible: `OHM_DOCUMENT_STORE=local` and `OHM_DOCUMENT_STORE=s3` paths unchanged
- Lazy `boto3` import means non-AWS deployments have zero dependency overhead
- `bedrock_sync_status` field in the save record enables monitoring and alerting on sync failures
- Metadata propagation makes retrieval results OHM-aware and linkable to the graph
- Explicit backfill endpoint covers documents uploaded before Bedrock was configured

**Negative:**
- `boto3>=1.34.0` is a new optional dependency — adds ~100 MB to the `aws` install
- `OHM_BEDROCK_KB_ID` is required when using the `bedrock` backend; missing value raises `RuntimeError` at init
- S3 reference mode requires `OHM_BEDROCK_DATA_SOURCE_ID` to be configured — an additional operational step
- Fire-and-forget sync means a document can be in the local store but not yet in the KB (eventual consistency)
- `start_ingestion_job` in S3 reference mode triggers a full data source sync, not a single-document sync — may re-ingest unchanged documents at scale
- Direct upload mode decodes content as UTF-8 with `errors="replace"` — binary documents (PDFs, images) lose fidelity in the KB
- `retrieve_and_generate` requires a model ARN; without `OHM_BEDROCK_MODEL_ARN` the caller must provide one

## References

- `src/ohm/documents/store.py:310` — `BedrockKnowledgeStore` implementation
- `src/ohm/documents/store.py:21` — `DocumentStore` ABC
- `src/ohm/documents/store.py:43` — `LocalDocumentStore`
- `src/ohm/documents/store.py:156` — `S3DocumentStore`
- `src/ohm/documents/ingest.py:17` — `ingest_file()` entry point
- `src/ohm/server/handlers/documents.py` — document library HTTP handlers
- `src/ohm/server/server.py` — HTTP routing
- `pyproject.toml:25` — `aws` optional dependency group
- ADR-016 — Staged Ingestion Pipeline
- ADR-015 — Multi-Tenancy
