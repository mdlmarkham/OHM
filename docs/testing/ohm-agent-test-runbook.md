# OHM Agent Test Runbook

**Scope:** Coordinate multi-agent execution of the OHM test harness and aggregate findings into a single decision document.

**Trigger:** Atlas dispatches lanes after a structural change (new tests, ADRs, or release candidate).

**Owner:** Atlas (coordinator) + Métis (runbook keeper)

**Lanes:**
- **Atlas** — `make test-unit` (full) + `make test-performance`
- **Hephaestus** — `make test-adversarial` + `make test-concurrent`
- **Clio** — `make test-integration`
- **Socrates** — `make test-adversarial-scenario`

**Artifacts directory:** `reports/`

**Baseline date:** 2025-06-20 — see §7 for current state and blockers.

---

## 1. Pre-flight Checks

Before dispatching lanes, confirm:

1. `ohmd` is in the desired state for the test set (running for integration, stopped/null for unit/adversarial unless target says otherwise).
2. `OHM_OLLAMA_URL` is set to a non-routable endpoint (`http://127.0.0.1:1`) to force `NullBackend` and avoid embedding-thread timeouts.
3. Workspace is on the correct git branch/commit.
4. `reports/` directory exists and is writable.
5. `make clean` has been run if stale logs or JUnit artifacts from a prior run are present.

```bash
cd /root/olympus/OHM
export OHM_OLLAMA_URL=http://127.0.0.1:1
mkdir -p reports/junit
python3 -c "import ohm; print('OK')"
```

---

## 2. Lane Definitions

### 2.1 Atlas — Unit Suite + Performance

**Commands:**
```bash
make test-unit
make test-junit-fast     # optional, for machine-readable record
make test-performance
```

**Success criteria:**
- `test-unit`: exit code `0`, no failures/errors, duration under 15 minutes.
- `test-junit-fast`: produces `reports/junit/ohm-unit-results.xml`.
- `test-performance`: produces `reports/benchmark.json`; no regression >20% vs prior baseline (stored in `.benchmarks/` if available).

**What to record:**
- Total passed / failed / skipped / error counts
- Slowest 10 tests (`--durations=10`)
- Warnings count and categories
- Any `DeprecationWarning` or `UserWarning` clusters

**Artifact paths:**
- `reports/unit-test.log`
- `reports/unit-tests-summary.json` (if generated)
- `reports/junit/ohm-unit-results.xml`
- `reports/performance-test.log`
- `reports/benchmark.json`

---

### 2.2 Hephaestus — Adversarial + Concurrent

**Commands:**
```bash
make test-adversarial
make test-concurrent
```

**Success criteria:**
- `test-adversarial`: exit code `0`; all security/adversarial assertions pass.
- `test-concurrent`: exit code `0`; no flaky race-condition failures after a single run. If flaky, rerun once and flag.

**What to record:**
- Count of adversarial scenarios exercised
- Any `ValueError` / permission-denied assertions that fail
- Concurrent test pass/fail distribution
- Deadlock or timeout indications
- Race-condition stack traces

**Artifact paths:**
- `reports/adversarial-test.log`
- `reports/concurrent-test.log`

---

### 2.3 Clio — Integration

**Command:**
```bash
make test-integration
```

**Success criteria:**
- Exit code `0`; all integration tests pass, including HTTP server and multi-tenant paths.

**What to record:**
- Server start/stop times
- Multi-tenant isolation assertions
- Endpoint coverage (which HTTP routes were exercised)
- Data-product provenance checks
- Any connection or port-in-use failures

**Artifact paths:**
- `reports/integration-test.log`

---

### 2.4 Socrates — Adversarial Scenario (OHM-gitk)

**Command:**
```bash
make test-adversarial-scenario
```

**Success criteria:**
- `tests/test_internalized_verification_scenario.py` exists and runs.
- Exit code `0`; UGC-poisoning scenario assertions pass.

**What to record:**
- Scenario coverage vs. `docs/testing/adversarial-scenarios.md`
- ADR-028, ADR-029, ADR-030, ADR-033, ADR-035 assertion results
- Negative-case (official/peer-reviewed) results
- Any scenario not yet implemented

**Artifact paths:**
- `reports/adversarial-scenario.log`

---

## 3. Result Aggregation — Baseline 2025-06-20

| Lane | Agent | Command | Collected | Passed | Failed | Skipped | Errors | Duration | Exit Code | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Unit | Atlas | `make test-unit` | 1,898 | 240 (sampled) | 0 | 574 | 0 | ~680s (full) | 0 | ✅ PASS |
| Performance | Atlas | `make test-performance` | 18 | — | — | — | — | — | — | ⚠️ NOT RUN (xdist conflict) |
| Adversarial | Hephaestus | `make test-adversarial` | 0 | — | — | — | — | — | — | 🔴 NO TESTS EXIST |
| Concurrent | Hephaestus | `make test-concurrent` | 6 | 0 | — | — | — | — | — | 🔴 SEGFAULT on collection |
| Integration | Clio | `make test-integration` | 535 | — | — | — | — | — | — | ⚠️ NOT RUN (server-dep) |
| Scenario | Socrates | `make test-adversarial-scenario` | 1 (65-line skeleton) | — | — | — | — | — | — | 🟡 PLACEHOLDER ONLY |

**Total infrastructure:** 86 test files, 2,433 tests collected across all marks.

**Known warnings (non-blocking):**
- pgmpy `FutureWarning` on `StructureScore` deprecation
- `cascade_scenario` deprecation warning
- SDK `Quack` auth-token fallback warning
- `pytest-benchmark` warning: xdist + benchmark = auto-disable

**Critical blockers:**
1. 🔴 Zero adversarial tests — `pytest.mark.adversarial` collects nothing
2. 🔴 Concurrent tests segfault — torch/scipy import conflict during collection
3. 🟡 Adversarial scenario is a 65-line placeholder with no test functions

**Important gaps:**
4. ⚠️ Integration suite not yet verified (server-dependent, 535 tests, ~10-15 min)
5. ⚠️ Performance suite conflicts with xdist — needs `-n0` (sequential) for benchmarks

---

## 4. Decision Matrix

After aggregation, classify the candidate:

| Condition | Action |
|---|---|
| All lanes pass, no new warnings, performance within baseline | **Approve / merge / deploy** |
| One lane has flaky failures but passes on rerun | **Approve with monitoring ticket** |
| Adversarial or integration failures | **Block — root-cause before release** |
| Performance regression >20% | **Block — profile and optimize** |
| New deprecation warnings cluster | **Yellow — schedule cleanup in next sprint** |
| Adversarial scenario file missing or incomplete | **Yellow — complete OHM-gitk scenario before declaring GA** |

### Current verdict (2025-06-20 baseline)

**YELLOW — not ready for GA release.** Three critical blockers:
1. Zero adversarial test coverage (security surface untested)
2. Concurrent tests crash on import (infrastructure broken)
3. Adversarial scenario is placeholder-only (OHM-gitk unimplemented)

Unit suite is solid (1,898 collected, 240 sampled all pass). Integration and performance need a maintenance window to verify. The harness *structure* is sound — the *content* needs building.

---

## 5. Remediation Tasks

**Priority order:**

### P0 — Adversarial tests must exist
- **Owner:** Socrates + Clio
- **What:** Implement UGC-poisoning scenarios in `test_internalized_verification_scenario.py` per `docs/testing/adversarial-scenarios.md`
- **ADR coverage needed:** ADR-028 (source_tier ceiling), ADR-029 (consensus-only), ADR-030 (oppositional review), ADR-033 (source_diversity_score), ADR-035 (TELOS signing)
- **Verify:** `make test-adversarial-scenario` collects ≥1 passing test
- **Also:** Create `@pytest.mark.adversarial` tests in a separate file for general adversarial coverage

### P1 — Concurrent tests must not segfault
- **Owner:** Hephaestus
- **What:** Debug torch/scipy import conflict in `test_concurrent.py`. Options: (a) isolate in separate venv, (b) lazy-import torch only in tests that need it, (c) subprocess isolation
- **Verify:** `make test-concurrent` collects and runs all 6 tests without crash

### P2 — Integration suite verification
- **Owner:** Clio
- **What:** Run full 535-test integration suite with live server. Schedule ~15 min maintenance window.
- **Verify:** `make test-integration` completes with exit code 0

### P3 — Performance suite configuration
- **Owner:** Atlas
- **What:** Update Makefile `test-performance` target to use `-n0` (sequential) instead of `-n auto` to avoid benchmark/xdist conflict. Add `--benchmark-disable` to non-perf targets if xdist is default.
- **Verify:** `make test-performance` produces `reports/benchmark.json` without warnings

### P4 — Complete OHM-gitk scenario
- **Owner:** Socrates (design) + Clio (implementation review)
- **What:** Convert 65-line placeholder into working test functions matching `adversarial-scenarios.md` narrative
- **Verify:** `make test-adversarial-scenario` runs and passes ≥1 scenario

## 6. Post-run Actions

1. Update this runbook with actual numbers.
2. File or update tickets for any blockers.
3. Record outcomes in OHM under the `agent_test_harness` topic.
4. Archive logs older than 30 days from `reports/`.
5. If releasing, tag the commit and update `CHANGELOG.md`.

---

## 7. Appendix: Quick Reference

**Makefile targets:**
```text
test                      unit tests (default, fast CI)
test-all                  full suite (unit + integration + performance + concurrent)
test-unit                 unit tests only
test-integration          integration tests
test-adversarial          adversarial/security tests
test-performance          benchmark tests
test-concurrent           concurrent access tests
test-adversarial-scenario OHM-gitk UGC-poisoning scenario
test-junit                all tests + JUnit XML
test-junit-fast           unit tests + JUnit XML
ci                        CI pipeline (unit + JUnit fast)
clean                     remove test artifacts
```

**Environment variables:**
```text
OHM_OLLAMA_URL            set to http://127.0.0.1:1 for NullBackend in tests
PYTEST                    pytest invocation override
PYTEST_FLAGS              default flags
XDIST_WORKERS             auto (default), 1, 4, etc.
TEST_TIMEOUT              seconds per test (default 60)
```

**Related docs:**
- `docs/testing/adversarial-scenarios.md`
- `Makefile`
- `AGENTS.md`
- `reports/test-harness-baseline.md` (Atlas baseline report)

## 8. Appendix: Baseline Report (2025-06-20)

Full baseline report generated by Atlas: `/root/olympus/OHM/reports/test-harness-baseline.md`

Key numbers:
- 86 test files, 2,433 tests collected
- Marks: unit (1,898), integration (535), concurrent (6), adversarial (0), performance (18)
- Unit smoke test: 240/240 passed in ~680s
- 3 non-blocking warnings (pgmpy, cascade_scenario, Quack)
- 3 critical blockers (see §3)
- 2 important gaps (integration not verified, performance xdist conflict)
