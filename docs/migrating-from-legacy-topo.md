# Migrating from Legacy TOPO to OHM-TOPO

This guide documents the column mapping and transformation logic for
migrating data from the legacy TOPO system's relational tables into
OHM-TOPO's `DomainTable`-based schema.

## `topo_reports` Column Mapping

The legacy TOPO `topo_reports` table stores analytical report metadata
with free-text plant/owner references. OHM's `topo_reports` uses
graph-node FK links (`node_id`, `plan_id`) and structured JSON columns
for findings/recommendations.

| Legacy Column | OHM Column | Transformation |
|---|---|---|
| `report_id` | `id` | Straight rename |
| `report_type` | `report_type` | Direct copy |
| `plant` (free text) | `node_id` | **Requires resolution** — see below |
| `owner` (free text) | `plan_id` | **Requires resolution** — see below |
| `owner` (free text) | `created_by` | Direct copy of the owner string |
| `title` | `title` | Direct copy |
| `inputs_json` | `findings` | Merge into JSON — see transformation logic |
| `structured_output` | `findings` | **Canonical target** for structured output (recommended over `inputs_json`) |
| `notes` | `recommendations` | Transform free-text notes into structured recommendations JSON |
| N/A | `confidence_adjustments` | NULL (no legacy equivalent) |
| `status` | `status` | Direct copy (`draft`, `finalized`, `superseded`) |
| `version` | `version` | Direct copy (integer) |
| `created_at` | `created_at` | Direct copy (timestamp) |
| `finalized_at` | `finalized_at` | Direct copy (timestamp, nullable) |
| N/A | `superseded_by` | NULL (set later by `supersede_report()`) |
| N/A | `metadata` | NULL or populate with legacy `inputs_json` if not mapped to `findings` |

### Resolution: Free-text `plant`/`owner` → FK Links

Legacy TOPO stores `plant` and `owner` as free-text strings (e.g.,
`"Plant A"`, `"John Smith"`). OHM requires `node_id` and `plan_id` to
reference existing graph nodes. There is no clean automatic resolution.

**Recommended approach:**

1. **Before migration**, ensure the target OHM instance has `ohm_nodes`
   entries for each distinct `plant` value (type `entity` or `concept`).
2. **Before migration**, ensure `topo_plans` has entries for each distinct
   `owner` value that represents a maintenance plan.
3. **During migration**, use a lookup table (see `migration_mapping.yaml`)
   to resolve `plant` → `node_id` and `owner` → `plan_id`.
4. **If no clean resolution exists**, store the original free-text value
   in `metadata` as `{"legacy_plant": "<value>"}` and set `node_id`/`plan_id`
   to NULL. This preserves the data for manual resolution later.

### Transformation: `structured_output` → `findings`

**Decision: map `structured_output` to `findings` (canonical target).**

Rationale: Adding a permanent `structured_output` column purely to ease a
one-time migration creates long-term ambiguity about which of
`findings`/`structured_output` is authoritative. A documented
transformation is cleaner.

Transformation rules:

1. If `structured_output` is valid JSON, store it directly as `findings`.
2. If `structured_output` is a JSON string (double-encoded), parse it
   first, then store as `findings`.
3. If `structured_output` is NULL, check `inputs_json`:
   - If `inputs_json` is valid JSON, merge it into `findings`.
   - Otherwise, store `inputs_json` as a string under
     `findings: {"raw_inputs": "<value>"}`.

### Transformation: `notes` → `recommendations`

Legacy `notes` is free-text. OHM `recommendations` is JSON.

Transformation rules:

1. If `notes` is valid JSON, store directly as `recommendations`.
2. If `notes` is free-text, wrap as:
   ```json
   {"action_items": ["<notes>"]}
   ```
3. If `notes` is NULL, set `recommendations` to NULL.

## Machine-Readable Mapping

The column mapping is also available as YAML for programmatic consumption:

```yaml
# migration_mapping.yaml — topo_reports column mapping
table: topo_reports
legacy_table: topo_reports
mappings:
  - legacy: report_id
    ohm: id
    transform: rename
  - legacy: report_type
    ohm: report_type
    transform: direct
  - legacy: plant
    ohm: node_id
    transform: lookup
    lookup_type: node
    fallback: null
    metadata_key: legacy_plant
  - legacy: owner
    ohm: plan_id
    transform: lookup
    lookup_type: plan
    fallback: null
    metadata_key: legacy_owner
  - legacy: owner
    ohm: created_by
    transform: direct
  - legacy: title
    ohm: title
    transform: direct
  - legacy: structured_output
    ohm: findings
    transform: json_passthrough
    fallback_column: inputs_json
    fallback_transform: json_or_wrap
  - legacy: notes
    ohm: recommendations
    transform: json_or_wrap
    wrap_key: action_items
  - legacy: null
    ohm: confidence_adjustments
    transform: null
  - legacy: status
    ohm: status
    transform: direct
  - legacy: version
    ohm: version
    transform: direct
  - legacy: created_at
    ohm: created_at
    transform: direct
  - legacy: finalized_at
    ohm: finalized_at
    transform: direct
  - legacy: null
    ohm: superseded_by
    transform: null
  - legacy: null
    ohm: metadata
    transform: null
```

See `scripts/migrate_topo_reports.py` for a sample ETL script that
consumes this mapping.

## `topo_report_references` (Stub — pending #835)

> **Note:** The `topo_report_references` table is owned by [#835](https://github.com/mdlmarkham/OHM/issues/835).
> Its column mapping will be documented here once that table is added to
> `topo.json`.

## Other Tables

Migration mappings for additional legacy TOPO tables will be added as
their corresponding OHM `DomainTable` entries are created:

- `topo_prospects` → `topo_rul_assessments` (done via #834 rename)
- `topo_prospect_expectations` — pending #835
- `topo_mc_assumptions` — pending #835
- `topo_mc_campaign_results` — pending #835
- `topo_prospect_provenance` — pending #835
- `topo_prospect_dependencies` — pending #835
