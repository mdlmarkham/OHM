# OHM Backlog — 2026-07-02

## TOPO Gap Analysis — Reconciled 2026-07-02

### CLOSED (existing OHM primitives already cover the gap)

- **Source provenance for CMMS** — `OHM-wdrg` (ADR-013 source_url enforcement) + ADR-028 structured source_refs.
  - Closed `OHM-psf2` as resolved.
- **Append-only assessment lifecycle** — ADR-040: `SchemaConfig.topo()` now carries five TOPO DomainTables (`topo_observations`, `topo_assessments`, `topo_followups`, `topo_regimes`, `topo_supersedes`).
  - Closed `OHM-svu5` as resolved.

### NEW P0 — ADR-041 Temporal Event Model

```
OHM-dh9l  ● P0 — TOPO: temporal event model (ADR-041) — ohm_intervals + ohm_plans core primitives
├── OHM-dh9l.1  ● P0 — immediate DomainTable unblock: topo_plans, topo_events, topo_event_links
└── OHM-dh9l.2  ● P1 — ADR-041 temporal event model decision record
```

TOPO can be unblocked immediately via the ADR-040 DomainTable pilot (`topo_plans`, `topo_events`, `topo_event_links`) while the generic OHM primitive (`ohm_intervals` + `ohm_plans`) is designed and landed.

Re-parented temporal TOPO issues under `OHM-dh9l`:
- `OHM-4qdk` — plan container
- `OHM-ay5k` — structured temporal events
- `OHM-xggk` — timeline rollup
- `OHM-vatf` — temporal-aware Bayesian propagation

### Downgraded to P2 (alias / DomainTable workaround viable)

- `OHM-ivlt` — node_path / UNS address (alias workaround viable for timeline/rollup).
- `OHM-q4ku` — RUL assessment storage hook (stat engine stays in TOPO; DomainTable can wait on node_path).

### Reports / DataProducts — Unblocked by ADR-040 pattern

- `OHM-08uk` — DataProductRun execution tracking (topo_runs DomainTable).
- `OHM-o3rd` — versioned analytical report artifacts (topo_reports DomainTable).

## Metis Test Findings — 2026-07-02

Already filed under correct parents:

| ID | Parent | Priority | Title |
|----|--------|----------|-------|
| `OHM-mzyc.1` | `OHM-mzyc` | P1 | INFLUENCES causal-status contradiction |
| `OHM-sbtz.1` | `OHM-sbtz` | P1 | `/admin/sync-beads` not idempotent, `dry_run` crashes |
| `OHM-sbtz.2` | `OHM-sbtz` | P1 | task node validation too permissive |
| `OHM-ezt5.1` | `OHM-ezt5` | P2 | copy-paste reasoning text in `/edge/suggest-type` for idea→task |
| `OHM-ezt5.2` | `OHM-ezt5` | P2 | source→pattern should default to L2 citation edge |
| `OHM-461f.1` | `OHM-461f` | P2 | Open Skills needs schema guide + template/query endpoints |
| `OHM-mzyc.2` | `OHM-mzyc` | P2 | duplicate `/challenge` and `/support` edges |
| `OHM-cbui` | — | P3 | `/perf` logs literal node/edge IDs |
| `OHM-mzyc.3` | `OHM-mzyc` | P3 | nudges are only surfaced in `POST /edge` response (no persistent log) |

## Edge-Typing Guardrails Epic — `OHM-mzyc`

Status as of 2026-07-02:
- `OHM-ezt5` — `/edge/suggest-type` implemented in commit `a729eb1`; pending adversarial test harness.
- `OHM-tsxk` + `OHM-bm5r` — creation-time nudges + mechanism gate implemented in commit `c1dbe96`; pending adversarial test harness.
- `OHM-1azk` — closed (fixed by `OHM-7el6`).
- `OHM-9zae` — open; Hephaestus adversarial test harness for the guardrails.
- Child issues `OHM-mzyc.1`, `OHM-mzyc.2`, `OHM-mzyc.3` remain open.

## Already-Closed Items (noted for context)

- `OHM-1azk` — fixed by `OHM-7el6`.
- `OHM-sbtz` — fixed via `dcd474c` + systemd `WorkingDirectory`.

## Priority Table (post-reconciliation)

| Priority | Count focus |
|----------|-------------|
| P0 | `OHM-dh9l`, `OHM-dh9l.1` — temporal event model (ADR-041) |
| P1 | `OHM-mzyc` epic + children, `OHM-sbtz.1`, `OHM-sbtz.2`, `OHM-ay5k`, `OHM-4qdk`, `OHM-vatf`, `OHM-q9rt`, etc. |
| P2 | `OHM-ivlt`, `OHM-q4ku`, `OHM-08uk`, `OHM-o3rd`, `OHM-xggk`, `OHM-ezt5.1`, `OHM-ezt5.2`, `OHM-461f.1`, `OHM-mzyc.2` |
| P3 | `OHM-cbui`, `OHM-mzyc.3` |
