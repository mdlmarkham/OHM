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

## The `--extra-schema` Extension Mechanism (OHM-835)

If your deployment needs tables beyond the bundled domain template,
write a small JSON file with just your additional `DomainTable` entries
and load it via `--extra-schema`. This is the general mechanism for
any domain-specific extension — not just this one migration.

### How it works

1. Create a JSON file with your extra `DomainTable` entries (see
   `docs/examples/topo-legacy-migration-extension.json` for a reference).
2. Start ohmd with both `--schema` and `--extra-schema`:

   ```bash
   ohmd --schema topo --extra-schema /path/to/my-extension.json
   ```

3. The extra tables are additively merged onto the base schema.
   **Name collisions raise an error** — the extra schema must not
   redefine tables already in the base.

### Repeatable flag

Pass `--extra-schema` multiple times to chain several extension files:

```bash
ohmd --schema topo \
  --extra-schema /path/to/extension-a.json \
  --extra-schema /path/to/extension-b.json
```

### What goes in the extension file

The file is a full `SchemaConfig` JSON, but in practice you only need
to set `domain_tables` — the vocabulary fields (`node_types`,
`observation_types`, etc.) can be empty arrays:

```json
{
  "name": "topo",
  "node_types": [],
  "layer_edge_types": {},
  "layer_descriptions": {},
  "observation_types": [],
  "observation_sources": [],
  "visibilities": [],
  "provenances": [],
  "domain_tables": [
    {
      "name": "my_custom_table",
      "ordering": 300,
      "description": "My deployment-specific table.",
      "columns": [
        ["id", "VARCHAR"],
        ["value", "DOUBLE"],
        ["created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"]
      ],
      "primary_key": "id"
    }
  ]
}
```

### Design guarantees

- **`ohmd --schema topo`** (no `--extra-schema`) creates **only** the
  bundled tables. Extension tables do NOT appear by default.
- **`SchemaConfig.extend()`** returns a plain `SchemaConfig` — no
  wrapper type. Every downstream consumer (DuckLake mirror, `/health`
  comparison, `to_db`/`from_db`) treats an extended schema identically
  to one that always contained those tables.
- **Reconnect safety**: On restart, the persisted schema (which includes
  previously-applied extensions) is merged additively with the current
  invocation's `--extra-schema` flags. No extension tables are silently
  lost.

### Example use case

The 12 legacy TOPO migration tables (from the TitanAmerica/MCT_CLIs
system) are too specific to bake into OHM's bundled `topo.json`. Instead,
they live in a reference extension file that operators copy and adapt:

- `docs/examples/topo-legacy-migration-extension.json`

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