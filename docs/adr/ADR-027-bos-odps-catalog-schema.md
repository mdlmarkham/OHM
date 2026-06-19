# ADR-027: BOS Internal ODPS Data Product Catalog Schema

**Date:** 2026-06-19
**Status:** Proposed
**Related issues:** OHM-r1vc (epic), OHM-1z5j (this design), OHM-ksi0 (storage), OHM-ux7z (validation), OHM-ylx8 (first products), OHM-xdtl (MCP endpoint), OHM-ovwq (provenance), OHM-0rlw (pilot)

## Context

BOS (Business Operations System) is a business-dedicated OHM instance where agents
produce structured recurring outputs: P&L statements, risk reports, research digests,
audit summaries. These outputs are currently ad-hoc — each agent produces them in its
own format, stores them wherever, and other agents have no standard way to discover
or consume them.

The [Open Data Product Specification (ODPS) v4.1](https://opendataproducts.org/v4.1/),
a Linux Foundation standard, provides a contract layer for describing data products
with metadata, access methods, quality, SLA, and pricing. ODPS is designed for
AI-agent-first discovery: agents can read ODPS YAML/JSON, understand what a product
contains, how to access it, and whether it meets their quality requirements.

This ADR defines how OHM will use ODPS v4.1 as the contract layer for an **internal-only**
data product catalog. Data lives in DuckDB; ODPS describes the contract; OHM tracks
provenance and confidence. The catalog stays internal until discipline is proven.

## Decision

### 1. Catalog storage: `ohm_data_products` table in DuckDB

A single table stores ODPS-compliant data product entries. Each row is one product
in one language (default `en`). The full ODPS YAML is stored as a TEXT column for
round-trip fidelity; structured columns mirror the minimum required fields for
queryable discovery.

```sql
CREATE TABLE IF NOT EXISTS ohm_data_products (
    -- Internal identity
    internal_id VARCHAR PRIMARY KEY,          -- OHM-generated UUID
    customer_id VARCHAR,                      -- tenant scope (NULL = operator)

    -- ODPS v4.1 required fields (product.details.{lang})
    product_id VARCHAR NOT NULL,              -- ODPS productID (unique per customer)
    name VARCHAR NOT NULL,                    -- ODPS name
    language VARCHAR DEFAULT 'en',            -- ISO 639-1 code
    visibility VARCHAR DEFAULT 'private',     -- private|invitation|organisation|dataspace|public
    status VARCHAR DEFAULT 'draft',           -- announcement|draft|development|testing|acceptance|production|sunset|retired
    type VARCHAR NOT NULL,                    -- raw data|derived data|dataset|reports|analytic view|...

    -- ODPS v4.1 optional but important for agent discovery
    value_proposition VARCHAR,                -- ODPS valueProposition (max 512)
    description VARCHAR,                      -- ODPS description
    producer_agent VARCHAR,                   -- OHM agent that produces this output
    output_port_type VARCHAR,                 -- file|API|SQL|AI|gRPC|sFTP
    access_format VARCHAR,                    -- TOON|JSON|XML|CSV|Excel|zip|plain text|GraphQL|MCP
    access_url VARCHAR,                       -- DuckDB query, file path, or MCP endpoint
    authentication_method VARCHAR,            -- OAuth|Token|API key|HTTP Basic|none
    output_file_formats VARCHAR,              -- comma-separated array

    -- OHM provenance (links to OHM graph, populated by OHM-ovwq)
    ohm_node_id VARCHAR,                      -- associated OHM node for provenance tracking
    confidence REAL,                          -- producer agent's confidence in product quality
    source_reliability REAL,                  -- rolling accuracy score from outcomes

    -- Lifecycle
    product_version VARCHAR,                  -- ODPS productVersion
    created VARCHAR,                          -- ODPS created (ISO date)
    updated VARCHAR,                          -- ODPS updated (ISO date)

    -- Full ODPS document (round-trip fidelity)
    odps_yaml TEXT,                           -- complete ODPS v4.1 YAML document

    -- OHM metadata
    created_by VARCHAR,                       -- agent that registered this product
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    deleted_at TIMESTAMP,                     -- soft delete

    UNIQUE(customer_id, product_id, language)
);
```

### 2. Minimum required ODPS v4.1 fields for internal catalog

The ODPS v4.1 JSON schema requires only `schema`, `version`, and `product` at the
top level, and `name`, `productID`, `visibility`, `status`, `type` within
`product.details.{lang}`. For the BOS internal catalog, we enforce this minimum
plus three BOS-specific fields that map agent outputs to products:

| Field | ODPS source | Required for BOS | Notes |
|-------|-------------|-------------------|-------|
| `schema` | top-level | yes | `https://opendataproducts.org/v4.1/schema/odps.json` |
| `version` | top-level | yes | `v4.1` |
| `product.details.en.name` | product | yes | Human-readable product name |
| `product.details.en.productID` | product | yes | Unique identifier within tenant |
| `product.details.en.visibility` | product | yes | Always `private` or `organisation` for BOS |
| `product.details.en.status` | product | yes | Lifecycle stage |
| `product.details.en.type` | product | yes | See mapping table below |
| `product.dataAccess[].format` | product | BOS-required | `MCP` for agent-accessible products |
| `product.dataAccess[].specification` | product | BOS-required | `MCP` for agent-discoverable products |
| `producer_agent` | OHM extension | yes | Which BOS agent produces this output |

Fields like `pricingPlans`, `license`, `paymentGateways` are **not required** for
internal BOS use (no internal billing). `SLA` and `dataQuality` are recommended
but not enforced initially — they become required when the pilot (OHM-0rlw)
demonstrates discipline.

### 3. Agent output → ODPS product type mapping

| BOS agent output | ODPS `type` | ODPS `outputPortType` | `access_format` | Example `productID` |
|------------------|-------------|-----------------------|-----------------|---------------------|
| P&L statement | `reports` | `SQL` | `JSON` | `bos.pnl.monthly` |
| Risk report | `reports` | `SQL` | `JSON` | `bos.risk.weekly` |
| Research digest | `reports` | `file` | `JSON` | `bos.research.digest` |
| Audit summary | `reports` | `SQL` | `JSON` | `bos.audit.quarterly` |
| KPI dashboard | `analytic view` | `SQL` | `JSON` | `bos.kpi.dashboard` |
| Forecast model | `decision support` | `API` | `JSON` | `bos.forecast.demand` |
| Anomaly alert | `data-driven service` | `API` | `JSON` | `bos.alert.anomaly` |

### 4. Producer / consumer agent pairs

| Producer agent | Product | Consumer agents | Consumption pattern |
|----------------|---------|-----------------|---------------------|
| `hephaestus` (audit) | Audit summary | `metis`, `socrates` | Synthesis + challenge |
| `metis` (research) | Research digest | `clio`, `deepthought` | Evidence gathering + narrative |
| `clio` (research) | Risk report | `metis`, `hephaestus` | Synthesis + audit |
| `deepthought` (journal) | KPI dashboard | All agents | situational awareness |
| `hephaestus` (audit) | Anomaly alert | `metis`, `clio` | Investigation trigger |
| `metis` (synthesis) | Forecast model | `deepthought` | Narrative input |

### 5. Access method: MCP discovery endpoint

Agent discovery happens via a single MCP endpoint (implemented in OHM-xdtl):

```
GET /data-products?producer=<agent>&type=<type>&status=production
GET /data-products/{product_id}
POST /data-products   (register a new product — producer agents only)
```

The endpoint returns ODPS v4.1 JSON for each product. Agents parse the
`dataAccess` block to determine how to retrieve the actual data (SQL query,
file path, or MCP resource URI).

For SQL-backed products, `access_url` contains a DuckDB query string that the
consumer agent executes against its local cache or the tenant store. For
file-backed products, `access_url` is a path within the tenant's data directory.

### 6. Storage layer decision: DuckDB only (Iceberg deferred)

The catalog metadata lives in DuckDB (`ohm_data_products` table). The actual
product data (P&L results, risk scores, research text) lives in:
- **DuckDB tables** for structured/queryable outputs (P&L, KPIs, risk scores)
- **JSON/text files** in the tenant's data directory for document outputs (research digests, audit narratives)

Iceberg table format is **deferred** — it adds complexity (catalog service,
snapshot management) that is not justified at current scale. The catalog schema
is designed to be Iceberg-compatible: if a product's data outgrows DuckDB, the
`access_url` can point to an Iceberg table path without changing the catalog
schema.

### 7. Validation: ODPS v4.1 schema compliance (OHM-ux7z)

Products are validated against the ODPS v4.1 JSON schema on registration.
The validation uses the `odps-python` library (by Accenture) or the
`open-data-products` SDK for schema validation. Products that fail validation
are rejected with a 422 response indicating which fields are missing or invalid.

BOS-specific validation (producer_agent required, visibility must be
private/organisation) is layered on top of the standard ODPS validation.

## Consequences

- **Standard-based**: ODPS v4.1 compliance means products are portable to other
  ODPS-aware platforms if BOS ever opens to external consumers
- **Agent-discoverable**: MCP endpoint lets any agent find and consume products
  without knowing producer-specific conventions
- **Provenance-linked**: `ohm_node_id` connects each product to the OHM graph,
  enabling confidence tracking and challenge semantics on product quality
- **Internal-first**: `visibility` constrained to private/organisation until
  pilot (OHM-0rlw) proves discipline; no pricing/license/payment infrastructure
  needed initially
- **DuckDB-native**: No additional infrastructure (no separate catalog service,
  no Iceberg catalog) — everything runs in the existing DuckDB instance
- **Iceberg-ready**: Schema design supports future Iceberg migration via
  `access_url` without catalog changes
- **Minimal friction**: Only 10 fields required for registration (7 ODPS + 3
  BOS), lowering the barrier for agents to publish their outputs

## Open questions

1. **Multi-language support**: The catalog schema includes a `language` column,
   but BOS initially operates in English only. Multi-language product entries
   are supported by the schema but not enforced.
2. **Product versioning**: The `product_version` column tracks versions, but
   the versioning policy (semantic? date-based?) is left to the pilot.
3. **Quality enforcement**: SLA and dataQuality blocks are optional initially.
   When to make them required is a pilot decision (OHM-0rlw).
