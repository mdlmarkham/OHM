# OHM Live Daemon Test Report — Followup Actions

**Date:** 2026-06-30 19:30 EDT
**Author:** OHM primary agent (followup to Métis's 14:32-14:45 test report)

This document tracks the actions taken against the four findings in
[`2026-06-30_live_daemon_test_report.md`](2026-06-30_live_daemon_test_report.md).
For each finding: status, files changed, tests added, and verification.

---

## Status Summary

| # | Finding (per Métis) | Severity | Status | Notes |
|---|---|---|---|---|
| 1 | `decision_id` vs `decision_node_id` field mismatch | 🟡 API gap | **Misdiagnosis** — no mismatch exists. The query function, SDK, and HTTP handler all use `decision_id` consistently. Closed without code change. |
| 2 | `/node` stores `node_type="decision"` as `concept` | 🔴 P0 | **Fixed** — added `node_type` / `edge_type` / `obs_type` as descriptive aliases for the generic `type` field across all 9 sites in the HTTP handler. |
| 3 | `/temporal/mode-switch` requires `from_mode` | 🟡 UX | **Misdiagnosis** — the endpoint in question is `_post_record_mode_switch`, which correctly requires both endpoints to record a transition. The recommendation endpoint `_get_recommend_mode` correctly takes only `decision_id`. No change needed. |
| 4 | `/twin/{id}/readiness` reports `feeds_fresh: false` when no threshold | 🟡 Minor | **Opinion, not a bug** — the response is technically accurate. A "no threshold set" vs "threshold exceeded" distinction would be a UX enhancement, not a bug fix. Out of scope for this round. |
| 5 | Daemon restart requirement after code update | 🟡 Ops | **Mitigated** — added `scripts/deploy_ohmd.sh` that wraps pull → install → restart → health check. |
| 6 | 8 `metis_test_*` artifacts left in live graph | 🟡 Housekeeping | **Tooled** — added `scripts/cleanup_test_artifacts.py` with `--dry-run`, pattern, and explicit-id modes. Operator can run on live DB; tested in-process. |

Two of Métis's four bug reports (#1 and #3) were misdiagnoses — the affected code was actually correct, and the report's claims did not match the implementation. The two real issues (P0 field-name mismatch and ops restart) are addressed below.

---

## #2 Fix: Type-field aliases

**Files changed:**
- `src/ohm/server/handlers/graph.py` — added `_resolve_type_field()` helper at module top; replaced 9 `body.get("type", ...)` call sites with the helper. Accepts `node_type` / `edge_type` / `obs_type` (canonical, descriptive) plus the legacy `type` (backward-compat). Empty string treated as missing.

**Tests added:** `tests/test_type_field_aliases.py` — 19 tests across three classes:
- `TestResolveTypeField` (9 unit tests for the helper itself: precedence, fall-through, empty-string, defaults).
- `TestNodeTypeAlias` (5 HTTP tests: `node_type=decision` now creates type='decision', backward compat with `type=concept`, descriptive wins when both present, default fallback, find_or_create).
- `TestEdgeTypeAlias` (2 HTTP tests: `edge_type=CAUSES` + legacy `type=CAUSES`).
- `TestObsTypeAlias` (3 HTTP tests: `obs_type=anomaly` + legacy `type=anomaly` + default 'measurement').

**Verification:**
```
tests/test_type_field_aliases.py — 19 passed in 8.42s
```
Plus the bug repro at `tests/test_type_field_aliases.py::TestNodeTypeAlias::test_node_type_creates_decision_node` confirms `POST /node` with `node_type: "decision"` now stores `type='decision'`.

**Backward compatibility:** Existing clients sending `type` still work. The new aliases are additive only.

**Beads:** OHM-0abu (filed P0 for the original report; ready to close once the test report's Recommendation #1 is checked off).

---

## #5 Mitigation: Deploy script with restart

**File added:** `scripts/deploy_ohmd.sh`

Wraps the full deploy sequence:
1. `git pull --rebase --autostash`
2. `pip install -e '.[dev]'`
3. `sudo systemctl restart ohmd`
4. Polls `GET /health` for up to `${OHM_HEALTH_TIMEOUT_S:-30}s`
5. Verifies `/loop-status` returns the `temporal` section (the endpoint that exposed the stale-code bug)

Flags: `--skip-pull`, `--skip-install`, `--dry-run`. Exit codes 0–5 for each failure mode.

**Not yet tested in CI** — requires a Linux environment with systemd. The script's dry-run mode is safe to invoke anywhere; full execution requires `sudo systemctl` and a live `ohmd.service`.

---

## #6 Tooling: Test-artifact cleanup script

**File added:** `scripts/cleanup_test_artifacts.py`

Operator-facing utility for removing leftover test nodes from the production graph. Defaults to pattern `metis_test_%` (matching the live test report's artifacts).

```bash
# Dry-run to see what would be deleted
python scripts/cleanup_test_artifacts.py --db-path /var/lib/ohm/ohm.duckdb --dry-run

# Apply
python scripts/cleanup_test_artifacts.py --db-path /var/lib/ohm/ohm.duckdb

# Explicit ids (overrides pattern)
python scripts/cleanup_test_artifacts.py --db-path /var/lib/ohm/ohm.duckdb --ids node1,node2
```

Uses `ohm.queries.delete_node` so the soft-delete cascade (edges, observations) is identical to the HTTP API.

**Tests added:** `tests/test_cleanup_artifacts.py` — 7 tests covering dry-run, live apply, edge cascade, explicit-ids override, no-matches, and summary JSON.

**Verification:**
```
tests/test_cleanup_artifacts.py — 7 passed in 2.47s
```

**Live cleanup pending:** Operator should run on the live daemon once this is deployed:
```bash
ssh ohm-host 'python scripts/cleanup_test_artifacts.py --db-path /var/lib/ohm/ohm.duckdb --dry-run'
# review output, then re-run without --dry-run
```

---

## #5 of original review (live-daemon CI tests)

**Not yet done.** Original report's Recommendation #5 calls for live-daemon integration tests in CI. Out of scope for this PR — requires a deployable test fixture (ohmd + DuckDB + seed data) wired into GitHub Actions. Suggested as a P3 followup.

---

## Coverage gaps that the original test report did NOT exercise

The 2026-06-30 report covered loop-status, twin/design, twin binding, model marketplace, and temporal summary. It did NOT exercise:
- Verification loop (`/heartbeat`, `record_outcome`, `verification_overdue`) — ADR-018
- Consensus-only detection endpoints — ADR-029
- Verification decay (`/admin/verification-decay`) — ADR-018

The Industrial Agent Manifesto (OHM-dp38) shipped 15 principles that depend on these endpoints. A second live-daemon test pass covering verification + consensus is needed before the Ledger pilot goes end-to-end. Tracked as a separate followup.

---

## Net status

- **Bug count from original report:** 4 reported (1 P0, 3 P-/🟡)
- **Real bugs found:** 1 (the P0 field-name mismatch)
- **Misdiagnoses:** 2 (decision_id, from_mode)
- **Opinions:** 1 (feeds_fresh)
- **Real bugs fixed this round:** 1 (#2 — type-field aliases)
- **Operational gaps mitigated:** 2 (#5 — deploy script, #6 — cleanup script)
- **Pending live work:** Run cleanup script on live DB; second live test pass for verification/consensus
- **Net diff:** +4 files (1 source, 2 scripts, 2 tests), +19 + 7 = 26 new tests
