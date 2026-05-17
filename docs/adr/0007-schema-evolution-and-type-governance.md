# ADR-007: Schema Evolution and Type Governance for Domain Expansion

**Date:** 2026-05-17
**Status:** Decided

## Context

OHM's original schema defines a small set of node types (source, claim, decision, observation, agent, topic) and edge types (CAUSES, SUPPORTS, CHALLENGED_BY, etc.) organized across four layers (L1–L4). As OHM is applied to new domains — cattle operations (CALVING_EVENT, HOOF_SCORE, PARASITE_LOAD, ANIMAL_GROUP), industrial monitoring (TOPO), and others — the schema must accommodate domain-specific types without polluting the core ontology or breaking existing queries.

ADR-006 established graduated enforcement (advisory → lenient → strict) and `SchemaConfig` for domain-specific configurations. This ADR addresses the remaining concerns: how domain types are registered, how they evolve from experimental to canonical, and how domains remain isolated from each other.

## Decision

### 1. Schema Versioning

Schema changes are versioned through the existing migration framework (`MIGRATIONS` in `schema.py`). Each migration has a version string, description, and SQL statements. The `ohm_meta` table tracks the current schema version.

- **Core schema** (OHM base types) is versioned in `MIGRATIONS` and applied by `initialize_schema()`.
- **Domain extensions** are versioned separately via `SchemaConfig` subclasses (e.g., `SchemaConfig.topo()`).
- Adding new core types requires a migration entry; adding domain types requires only a `SchemaConfig` extension.

### 2. Type Registry

`SchemaConfig` serves as the type registry. Each configuration declares:

- `node_types: frozenset[str]` — valid node types for this domain
- `layer_edge_types: dict[str, frozenset[str]]` — valid edge types per layer
- `observation_types: frozenset[str]` — valid observation types
- `observation_sources: frozenset[str]` — valid observation sources
- `provenances: frozenset[str]` — valid provenance values

The base OHM `SchemaConfig()` contains the core types. Domain configurations extend these using set union:

```python
cattle = SchemaConfig(
    name="cattle",
    node_types=VALID_NODE_TYPES | frozenset({
        "calving_event", "hoof_score", "parasite_load", "animal_group",
    }),
    ...
)
```

### 3. Migration Path: Experimental → Canonical

Domain types follow a three-stage lifecycle:

1. **Experimental** — Created ad-hoc by agents in advisory mode. No registration required. Logged as unknown types.
2. **Registered** — Added to a `SchemaConfig` subclass. Validated in lenient mode. Documented in the domain's configuration.
3. **Canonical** — Promoted to the base `VALID_NODE_TYPES` / `LAYER_EDGE_TYPES` sets via a schema migration. Enforced in strict mode.

Promotion from registered → canonical requires:
- The type is used by at least two agents across multiple sessions
- A migration entry in `MIGRATIONS` adds it to the core sets
- The `SchemaConfig` subclass removes the type from its extension set (it's now in the base)

### 4. Validation Policy

As established in ADR-006, validation is graduated:

| Mode | Known Types | Unknown Types | Use Case |
|------|-------------|---------------|----------|
| Advisory | Accepted | Accepted (warned) | Exploration, prototyping |
| Lenient | Validated | Accepted (warned) | Domain maturation |
| Strict | Validated | Rejected | Production enforcement |

The validation mode is set per-connection via `SchemaConfig`:

```python
# Advisory (default) — any type accepted
config = SchemaConfig()

# Lenient — known types validated, unknown accepted with warnings
config = SchemaConfig(enforce_types=True, allow_unknown=True)

# Strict — only registered types accepted
config = SchemaConfig(enforce_types=True, allow_unknown=False)
```

### 5. Domain Separation

Domain types are isolated through `SchemaConfig` instances. Each domain has its own configuration that extends — but never modifies — the base OHM types.

**Principle: Domains extend, never override.**

- The base `SchemaConfig()` always represents the core OHM ontology.
- `SchemaConfig.topo()` adds industrial types; `SchemaConfig.cattle()` adds livestock types.
- Domain configurations are composable: a domain can extend another domain's config.
- Domain types are invisible to agents using the base config in strict mode.
- Domain types are visible but unvalidated in advisory mode (the default).

**Implementation:**

```python
# Cattle domain extends OHM base
class CattleSchemaConfig(SchemaConfig):
    @classmethod
    def cattle(cls):
        return cls(
            name="cattle",
            node_types=VALID_NODE_TYPES | frozenset({
                "calving_event", "hoof_score", "parasite_load", "animal_group",
            }),
            observation_types=VALID_OBSERVATION_TYPES | frozenset({
                "weight", "body_condition", "milk_yield", "temperature",
            }),
            observation_sources=VALID_OBSERVATION_SOURCES | frozenset({
                "vet_record", "farm_log", "sensor_collar",
            }),
        )
```

**Cross-domain queries** work because all domains share the same DuckDB tables. An agent using the cattle config can see TOPO nodes in advisory mode, but cannot create them in strict mode. This is intentional — domain boundaries are enforcement boundaries, not visibility boundaries.

## Consequences

- **No schema pollution:** Cattle types (CALVING_EVENT, HOOF_SCORE) never appear in the base `VALID_NODE_TYPES` set unless promoted via migration.
- **Domain autonomy:** Each domain controls its own type lifecycle without coordinating with other domains.
- **Graduated enforcement:** New domains start in advisory mode and opt into strict mode when mature.
- **Composable configurations:** Domains can extend other domains (e.g., a "livestock" config extending "cattle").
- **Migration audit trail:** Every type promotion from experimental → canonical is recorded in `MIGRATIONS` and `ohm_meta`.
- **Cross-domain visibility:** All domains share the same physical tables. Domain boundaries are logical, not physical — agents in advisory mode can see everything.

## References

- ADR-001: DuckDB as Local Cache — schema migrations run against DuckDB
- ADR-005: Self-Documenting CLI as Agent Interface — `ohm graph schema` shows active domain types
- ADR-006: Advisory Schema with Graduated Enforcement — `SchemaConfig` and enforcement levels