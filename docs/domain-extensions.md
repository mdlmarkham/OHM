# Domain Extension Pattern for OHM

## Core Principle

> **TOPO-specific is configuration; OHM-wide is schema.**

OHM's core schema should hold cross-cutting concepts. Domain-specific
details (plant names, OPC namespaces, KPI scope, campaign variables,
staleness thresholds) should live in JSON `metadata` columns and
adapter/plugin configuration, not in dedicated columns or tables per
application.

## Pattern

When adding a new domain-specific capability to OHM:

1. **Generic table** with `domain` and `source_type`/`target_type`
   discriminator columns — not a domain-prefixed table
   (e.g. `external_signals`, not `topo_signal_attachments`).
2. **JSON `metadata`** for domain-specific fields (plant, address, KPI
   scope, etc.) — not first-class columns.
3. **Adapter/plugin config** for domain-specific behavior (tag
   normalization, simulation parameters, trigger thresholds).
4. **Domain registry** so DuckLake mirror, MCP tools, and REST endpoints
   expose the same table uniformly across domains.

## What This Means in Practice

### Going Forward (New Tables)

New domain-specific tables should follow the generic pattern:

```sql
CREATE TABLE IF NOT EXISTS external_signals (
    id          VARCHAR PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id     VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,  -- 'opc_ua', 'timescale', 'market_feed', etc.
    source_id   VARCHAR,
    source_path VARCHAR,
    domain      VARCHAR NOT NULL DEFAULT 'ohm',
    metadata    JSON,              -- domain-specific details
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by  VARCHAR
);
```

A TOPO deployment stores `source_type='opc_ua'`, `domain='topo'`, with
OPC node IDs in `source_id` and plant/equipment info in `metadata`.

A trading deployment stores `source_type='market_feed'`, `domain='trading'`,
with ticker symbols in `source_id` and exchange info in `metadata`.

Both use the same table, same queries, same MCP tools — the `domain` and
`source_type` columns discriminate.

### Existing Tables (No Retrofit Required)

The existing `topo_rul_assessments`, `topo_observations`,
`topo_observation_assessments`, and other TOPO-prefixed tables in
`SchemaConfig.topo()` are **not** retrofitted. They work, they're
scoped to the TOPO domain via `SchemaConfig`, and retrofitting them
would be scope creep. The generic pattern applies to **new** tables
going forward.

### Cross-Domain Isolation

`DomainTable` (the existing mechanism for domain table registration)
already scopes every domain table to whichever `SchemaConfig` factory
registers it. A trading-research or devsecops deployment never sees
`topo_*` tables at all because it never calls `SchemaConfig.topo()`.
The generic-tables pattern is **not** about cross-domain isolation
(that's already solved) — it's about **avoiding N different domains
each reinventing near-identical tables with different column names**.

## Adapter/Plugin Registry

Domain-specific ingest behavior (UNS tag normalization, VSM event
parsing, metric calculation) is handled by adapter plugins, not
core schema. The existing `IngestAdapter` protocol in
`framework/ingest.py` provides the interface:

```python
class IngestAdapter(Protocol):
    def ingest(self, conn, config: dict) -> list[dict]:
        """Ingest data and return created/updated nodes."""
        ...
```

Domain packages register adapters via the `ohm ingest` CLI:

```bash
ohm ingest --source uns --domain topo --plant rcc
ohm ingest --source vsm --domain topo --plant pns
ohm ingest --source metrics --domain trading
```

Each adapter (`UNSAdapter`, `VSMAdapter`, `MetricsAdapter`) lives in
its domain package and is discovered via entry-point or explicit
registration. The core `ohm ingest` command loads the appropriate
adapter based on `--source` and passes `--domain` and other flags
as config.

## When to Use This Pattern

| Situation | Use Generic Pattern? |
|---|---|
| New cross-cutting capability (signals, simulations, staleness) | Yes — generic table with `domain` column |
| Domain-specific vocabulary (node types, edge types) | No — use `SchemaConfig` with domain templates |
| Domain-specific ingest behavior | No — use adapter plugins |
| Domain-specific query convenience methods | Yes — generic method with `domain` parameter |
| Existing TOPO tables | No — leave as-is, don't retrofit |

## Related Issues

- #811: This umbrella issue
- #802: `external_signals` table (generic signal attachments)
- #803: Plugin-based ingest adapter system
- #804: `domain_simulation_runs`/`domain_simulation_results`
- #805: `domain_assumptions`/`domain_expectations`
- #806: `staleness_log` with domain-configured triggers
- #807: `node_context(node_id, domain=None)` enrichment
- #808: `metric_versions` for formula lineage
- #809: `get_edges_by_path()` path-based edge queries