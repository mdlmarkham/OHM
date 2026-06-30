# OHM Live Daemon Integration Test Report

**Date:** 2026-06-30 14:32-14:45 EDT  
**Tester:** Métis  
**Daemon:** `ohmd` at `http://127.0.0.1:8710`  
**Source:** `/root/olympus/OHM/src` at commit `4025354`

---

## Initial State

First call to `GET /loop-status` returned:
```json
{"error": "Unknown endpoint: /loop-status"}
```

This indicated the daemon was running stale code. A `systemctl restart ohmd` resolved it. After restart, the daemon loaded the new code and `/loop-status` returned a full response including the `temporal` section.

**Action:** Service restart should be part of the deployment workflow for new commits.

---

## Endpoints Tested

### ✅ `/loop-status`

- Returns `temporal` section with `upcoming_evaluations`, `stale_feeds`, `compromised_gates`, `stuck_gates`, `decay_summary`.
- Live response includes stale feeds and compromised gates from the shared graph.

### ✅ Conversational `/twin/design`

Tested full flow:
1. `POST /twin/design/start` — created session `metis_test_design_session_20260630_868775`
2. `POST /twin/design/{id}/transition` → `discover`
3. `POST /twin/design/{id}/transition` → `observe`
4. `POST /twin/design/{id}/observe` — observations stored
5. `POST /twin/design/{id}/transition` → `propose`
6. `POST /twin/design/{id}/propose` — proposal created, session auto-transitioned to `approve`
7. `GET /twin/design/{id}/audit` — full transition history and provenance visible

All steps returned 200/201 and correct state machine behavior.

### ✅ Twin Binding Flow

Created test nodes:
- `metis_test_target_roth_wealth` (concept)
- `metis_test_decision_roth_2026` (attempted decision, stored as concept — see bug below)
- `metis_test_feed_trad_ira` (concept)

`POST /twin/register-with-bindings` successfully registered twin `metis_test_twin_roth_conversion_e3af47` with EVALUATES, DECISION_DEPENDS_ON, and FEEDS edges.

`GET /twin/{id}/readiness` correctly reported:
- `target_bound: true`
- `decision_bound: true`
- `feeds_present: true`
- `feeds_fresh: false` (no observation yet)
- `models_available: false`
- `models_evaluated: false`

### ✅ Model Marketplace + Decision-Value Promotion

1. `POST /model/register` — registered candidate `metis_test_baseline_no_convert_59ba53`
2. `POST /twin/{id}/attach-models` — attached candidate to twin
3. `POST /model/{id}/evaluate` — attached metrics
4. `POST /model/{id}/promotion-policy` — set policy to `decision_value` with decision node
5. `POST /twin/{id}/auto-promote` — promoted candidate to active, returned `gate_status: active`

Decision-value promotion works end-to-end on the live daemon.

### ✅ Temporal Summary

`GET /temporal/{decision_id}/summary` returns:
```json
{
  "decision_id": "...",
  "freshness": {},
  "mode": { ... },
  "feed_investments": [],
  "mode_switches": []
}
```

---

## Bugs / API Gaps Found

### 🔴 Bug: HTTP `/node` stores `node_type: "decision"` as `concept`

**Repro:**
```bash
curl -X POST http://127.0.0.1:8710/node \
  -d '{"label":"Test Decision","node_type":"decision","id":"...","created_by":"metis"}'
```

**Expected:** `type: "decision"`  
**Actual:** `type: "concept"`

**Impact:** This breaks any endpoint that looks up a decision node by ID and validates its type, including `/temporal/freshness` and `/temporal/mode-switch`. It also means `query_loop_status` cannot correctly classify decision nodes created through the HTTP API.

**Likely cause:** The HTTP handler is not passing `node_type` correctly, or the validator is coercing unknown types to `concept` without error.

### 🟡 API Mismatch: `/temporal/freshness` expects `decision_id`, not `decision_node_id`

The underlying function `set_freshness_threshold()` uses `decision_node_id`, but the HTTP endpoint expects `decision_id`. The error message is `decision_id is required`.

**Recommendation:** Standardize the field name across SDK, queries, and HTTP, or accept both aliases.

### 🟡 API Requirement: `/temporal/mode-switch` requires `from_mode`

The endpoint requires the caller to supply the current mode. This is cumbersome for clients that just want a recommendation. The underlying `recommend_mode()` function does not require `from_mode`.

**Recommendation:** Make `from_mode` optional; the endpoint can look up the most recent `mode_switch` node for the decision and default to that.

### 🟡 Minor: `/twin/{id}/readiness` reports `feeds_fresh: false` even when no freshness threshold exists

A feed without a threshold and without observations is reported as not fresh. This is technically correct (no fresh observation), but the distinction between "no threshold set" and "threshold exceeded" would help UX.

---

## Test Artifacts Left in Graph

These nodes were created during testing and are tagged `test`:
- `metis_test_target_roth_wealth`
- `metis_test_decision_roth_2026`
- `metis_test_decision_v3`
- `metis_test_feed_trad_ira`
- `metis_test_twin_roth_conversion_e3af47`
- `metis_test_baseline_no_convert_59ba53`
- `metis_test_design_session_20260630_868775`
- Associated proposal, edges, observations

They should be cleaned up after the decision-node bug is fixed and tests are rerun.

---

## Recommendations

1. **Fix the decision-node type bug immediately** — it blocks the Ledger pilot and any decision-aware temporal workflow.
2. **Standardize field names** between HTTP handlers and query functions for temporal endpoints.
3. **Make `/temporal/mode-switch` easier to call** by deriving `from_mode` from recent history.
4. **Add a deployment step** that restarts `ohmd` after code updates.
5. **Add live-daemon integration tests** to CI so endpoint availability is checked automatically.

---

## Conclusion

The foundation is deployed and most new endpoints work on the live daemon. The two critical blockers for real pilots are:
1. The `/node` decision-type bug.
2. The daemon restart requirement.

Once those are fixed, the Ledger pilot can proceed end-to-end against the live daemon.
