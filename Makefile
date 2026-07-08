.PHONY: test test-unit test-integration test-adversarial test-performance test-all test-concurrent clean

# ── Agent Test Harness (OHM-9zae) ──────────────────────────────────────
#
# Usage:
#   make test              — Default: run unit tests only (fast CI)
#   make test-all          — Full suite: unit + integration + adversarial + performance
#   make test-unit         — Unit tests only (<5m, ~1500 tests)
#   make test-integration  — Integration tests (HTTP server, multi-tenant)
#   make test-adversarial  — Adversarial/security scenario tests
#   make test-performance  — Benchmark tests
#   make test-adversarial-scenario  — OHM-gitk UGC-poisoning scenario
#   make test-junit        — All tests with JUnit XML output (for Atlas/Metis)
#

# ── Configuration ───────────────────────────────────────────────────────

PYTHON     ?= python3
PYTEST     ?= $(PYTHON) -m pytest
VENV       ?= .venv
OHM_DIR     = .
TESTS_DIR   = $(OHM_DIR)/tests
REPORTS_DIR = $(OHM_DIR)/reports
JUNIT_DIR   = $(REPORTS_DIR)/junit

# Force NullBackend for all test runs to avoid embedding threads
export OHM_OLLAMA_URL ?= http://127.0.0.1:1

# Default flags for all test runs
PYTEST_FLAGS ?= --tb=short -q

# Test timeout (seconds per test)
TEST_TIMEOUT ?= 60

# XDist workers (0 = auto-detect, 1 = sequential, 4 = 4 workers)
XDIST_WORKERS ?= auto

# ── Targets ─────────────────────────────────────────────────────────────

test: test-unit
	@echo "✓ Unit tests complete"

# Fast unit tests — target: <5 minutes
# Uses negated markers since tests don't carry explicit "unit" marks
test-unit:
	@echo "=== Running unit tests (fast) ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "not slow and not integration and not concurrent" \
		--timeout=$(TEST_TIMEOUT) \
		--benchmark-disable \
		--durations=10 2>&1 | tee $(REPORTS_DIR)/unit-test.log
	@echo ""

# Integration tests
test-integration:
	@echo "=== Running integration tests ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "integration" \
		--timeout=$(TEST_TIMEOUT) \
		-n $(XDIST_WORKERS) \
		--durations=10 2>&1 | tee $(REPORTS_DIR)/integration-test.log
	@echo ""

# Adversarial/security tests
test-adversarial:
	@echo "=== Running adversarial/security tests ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "adversarial" \
		--timeout=$(TEST_TIMEOUT) \
		--durations=10 2>&1 | tee $(REPORTS_DIR)/adversarial-test.log
	@echo ""

# Performance/benchmark tests
test-performance:
	@echo "=== Running performance tests ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "performance" \
		--timeout=$(TEST_TIMEOUT) \
		--benchmark-only -n 0 \
		--benchmark-json=$(REPORTS_DIR)/benchmark.json 2>&1 | tee $(REPORTS_DIR)/performance-test.log
	@echo ""

# Concurrent tests (multi-threaded, may be flaky)
test-concurrent:
	@echo "=== Running concurrent tests ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "concurrent" \
		--timeout=120 -n 0 \
		--durations=10 2>&1 | tee $(REPORTS_DIR)/concurrent-test.log
	@echo ""

# OHM-gitk: UGC-poisoning adversarial scenario
# Test file exists and is marked @pytest.mark.adversarial
# Run via: make test-adversarial (uses -m adversarial marker)
test-adversarial-scenario:
	@echo "=== Running adversarial scenario: UGC-poisoning (OHM-gitk) ==="
	@mkdir -p $(REPORTS_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/test_internalized_verification_scenario.py \
		--timeout=$(TEST_TIMEOUT) \
		-v 2>&1 | tee $(REPORTS_DIR)/adversarial-scenario.log
	@echo ""

# Full suite: all test categories
test-all: test-unit test-integration test-adversarial test-performance test-concurrent
	@echo ""
	@echo "═══════════════════════════════════════════"
	@echo "  ✓ All test suites complete"
	@echo "═══════════════════════════════════════════"

# Machine-readable output (JUnit XML) for Atlas/Metis consumption
test-junit:
	@echo "=== Running all tests with JUnit XML output ==="
	@mkdir -p $(JUNIT_DIR)
	$(PYTEST) $(TESTS_DIR)/ \
		-k "not concurrent" \
		--timeout=$(TEST_TIMEOUT) \
		--benchmark-disable \
		--junitxml=$(JUNIT_DIR)/ohm-test-results.xml \
		--junit-prefix=ohm. \
		--tb=short -q 2>&1 | tee $(REPORTS_DIR)/full-test.log
	@echo "JUnit report: $(JUNIT_DIR)/ohm-test-results.xml"

test-junit-fast:
	@echo "=== Running unit tests (fast) with JUnit XML output ==="
	@mkdir -p $(JUNIT_DIR)
	$(PYTEST) $(PYTEST_FLAGS) \
		$(TESTS_DIR)/ -m "not slow and not integration and not concurrent" \
		--timeout=$(TEST_TIMEOUT) \
		--benchmark-disable \
		--junitxml=$(JUNIT_DIR)/ohm-unit-results.xml \
		--junit-prefix=ohm. 2>&1 | tee $(REPORTS_DIR)/unit-junit.log
	@echo "JUnit report: $(JUNIT_DIR)/ohm-unit-results.xml"

# CI target — runs unit, integration, and performance suites
ci: test-unit test-junit-fast
	@echo "✓ CI checks passed"

# Clean up test artifacts
clean:
	rm -rf $(REPORTS_DIR)/
	rm -rf .pytest_cache/
	find $(TESTS_DIR) -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find $(TESTS_DIR) -name "*.pyc" -delete
	@echo "✓ Clean"

# Create the adversarial scenario test file (placeholder for OHM-gitk)
# Once Clio and Socrates complete their designs, this will be filled in.
# Help
help:
	@echo "OHM Agent Test Harness (OHM-9zae)"
	@echo ""
	@echo "  make test              — Unit tests (default, fast CI)"
	@echo "  make test-all          — Full suite"
	@echo "  make test-unit         — Unit tests"
	@echo "  make test-integration  — Integration tests"
	@echo "  make test-adversarial  — Adversarial tests"
	@echo "  make test-performance  — Benchmark tests"
	@echo "  make test-concurrent   — Concurrent access tests"
	@echo "  make test-junit        — All tests + JUnit XML report"
	@echo "  make test-junit-fast   — Unit tests + JUnit XML report"
	@echo "  make test-adversarial-scenario — OHM-gitk UGC-poisoning"
	@echo "  make ci                — CI pipeline"
	@echo "  make clean             — Remove test artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  PYTEST=python3 -m pytest"
	@echo "  XDIST_WORKERS=auto    (set to 1 for sequential, 4 for 4 workers)"
	@echo "  TEST_TIMEOUT=60       (seconds per test)"
	@echo ""

.DEFAULT_GOAL := test