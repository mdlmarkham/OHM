# OHM Agent Test Runbook

How the agent team runs and interprets the OHM test harness.

## Quick start

```bash
# Fast unit tests — run on every push (~2.5 min)
make test-unit

# Python harness alternative
python3 scripts/agent-test.py --unit --workers=4
```

Reports are written to `reports/`. JUnit XML for CI/Atlas is at `reports/junit/`.

## When to run each suite

| Command | When | Why |
|---|---|---|
| `make test-unit` | Every push / before merge | Fast feedback on core logic |
| `make test-integration` | Before merge / nightly | HTTP handlers, CLI, multi-tenancy, DuckLake sync |
| `make test-adversarial` | Security review / release | UGC poisoning, consensus capture, TELOS forgery |
| `make test-performance` | Release / regression checks | Semantic search + HD fingerprint latency |
| `make test-concurrent` | Stability checks | Multi-agent write races and tenant isolation |
| `make test-junit` | CI | Full suite with machine-readable XML reports |

## Interpreting reports

- `reports/unit-test.log` — human-readable summary
- `reports/junit/` — JUnit XML for CI dashboards
- `reports/agent-test-report.json` — JSON summary from `scripts/agent-test.py`

A healthy run: unit tests pass in under 5 minutes with zero failures.

## Known flaky tests

Two tests are pre-existing flaky and may fail under load:

1. `TestConcurrentAccess::test_concurrent_writes_same_tenant_no_corruption`
2. `TestDuckLakeHealthCheck::test_sync_degraded_flag`

If either fails, re-run in isolation before filing a bug:

```bash
python3 -m pytest tests/test_concurrent.py::TestConcurrentAccess::test_concurrent_writes_same_tenant_no_corruption -v
python3 -m pytest tests/test_ducklake_health.py::TestDuckLakeHealthCheck::test_sync_degraded_flag -v
```

## How to mark a new test

Use `pytestmark` at the top of the test file:

```python
import pytest

pytestmark = pytest.mark.unit
```

For files that start services, hit HTTP, or run benchmarks:

```python
pytestmark = pytest.mark.integration   # or concurrent / performance / adversarial / slow
```

A file can have multiple markers:

```python
pytestmark = [pytest.mark.adversarial, pytest.mark.integration, pytest.mark.slow]
```

## Production smoke tests

After deployment or restart, check:

```bash
curl -s http://127.0.0.1:8710/health | python3 -m json.tool
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8710/admin/health | python3 -m json.tool
curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8710/stats | python3 -m json.tool
```

Watch for:
- `verification_rate` near 0% — causal edges lack recorded outcomes
- `source_coverage` below 10% — most nodes lack source URLs
- `orphan_rate` above 15% — too many disconnected nodes

## Adding an adversarial scenario

1. Describe the attack in `docs/testing/adversarial-scenarios.md`.
2. Implement the test in `tests/test_internalized_verification_scenario.py`.
3. Mark it with `pytest.mark.adversarial` and any other relevant markers.
4. Verify it fails without the protection and passes with it.

## Questions?

If the harness breaks or a suite hangs, check `reports/unit-test.log` and ping Hephaestus / Métis.
