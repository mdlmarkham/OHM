# Skill: Ingest Document

## When to use
Use the document ingestion pipeline to convert external documents
(PDFs, web pages, text) into source nodes with extracted claims.

## Pipeline
1. Ingest the document via `POST /documents/ingest`.
2. The pipeline extracts claims, entities, and relationships.
3. Review the resulting nodes and edges.
4. Link extracted claims to existing graph structure (ADR-018).

## Source tiers (ADR-028)
- `raw`: Unprocessed data, confidence ceiling 0.3.
- `unverified`: Single source, ceiling 0.5.
- `preliminary`: Early analysis, ceiling 0.7.
- `official`: Published but not peer-reviewed, ceiling 0.85.
- `verified`: Peer-reviewed or confirmed, ceiling 1.0.
