#!/usr/bin/env python3
"""
OHM Agent Test Harness (OHM-9zae)

Runs unit, integration, adversarial, and performance suites separately
with machine-readable output (JUnit XML + JSON summary).

Usage:
    python3 scripts/agent-test.py                # Run unit tests (default, fast)
    python3 scripts/agent-test.py --all          # Run full suite
    python3 scripts/agent-test.py --unit         # Unit tests only
    python3 scripts/agent-test.py --integration  # Integration tests only
    python3 scripts/agent-test.py --adversarial  # Adversarial/security tests
    python3 scripts/agent-test.py --performance  # Performance/benchmark tests
    python3 scripts/agent-test.py --concurrent   # Concurrent access tests
    python3 scripts/agent-test.py --junit        # All + JUnit XML (for Atlas)
    python3 scripts/agent-test.py --fast-junit   # Unit + JUnit XML (for CI)
    python3 scripts/agent-test.py --watch        # Retry until stable
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
REPORTS_DIR = REPO_ROOT / "reports"
JUNIT_DIR = REPORTS_DIR / "junit"
OHM_DIR = REPO_ROOT

# Force NullBackend to avoid embedding threads during tests
os.environ.setdefault("OHM_OLLAMA_URL", "http://127.0.0.1:1")


def run_pytest(
    label: str,
    marker: str | None = None,
    test_file: str | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 60,
    workers: str = "auto",
    junit: bool = False,
    json_report: bool = False,
) -> dict:
    """Run a pytest suite and return summary.

    Returns:
        dict with keys: label, passed, failed, skipped, errors, duration, exit_code, report_path
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    if junit:
        os.makedirs(JUNIT_DIR, exist_ok=True)

    cmd = [
        sys.executable, "-m", "pytest",
        "--tb=short", "-q",
    ]

    if marker:
        cmd.extend(["-m", marker])

    if test_file:
        cmd.append(str(test_file))
    else:
        cmd.append(str(TESTS_DIR))

    if extra_args:
        cmd.extend(extra_args)

    cmd.extend(["--timeout", str(timeout)])

    if workers == "auto":
        cmd.extend(["-n", "auto"])
    elif workers != "1":
        cmd.extend(["-n", str(workers)])

    junit_path = None
    if junit:
        prefix = label.replace(" ", "_").lower()
        junit_path = JUNIT_DIR / f"ohm-{prefix}-results.xml"
        cmd.extend(["--junitxml", str(junit_path), "--junit-prefix", "ohm."])

    log_path = REPORTS_DIR / f"{label.replace(' ', '-').lower()}-test.log"

    start = time.monotonic()
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    duration = time.monotonic() - start

    with open(log_path, "w") as f:
        f.write(result.stdout)
        if result.stderr:
            f.write("\n--- STDERR ---\n")
            f.write(result.stderr)

    # Parse summary from output
    passed = failed = skipped = errors = 0
    for line in result.stdout.split("\n"):
        if "passed" in line or "failed" in line:
            import re
            m = re.search(r"(\d+) passed", line)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", line)
            if m:
                failed = int(m.group(1))
            m = re.search(r"(\d+) skipped", line)
            if m:
                skipped = int(m.group(1))
            m = re.search(r"(\d+) errors", line)
            if m:
                errors = int(m.group(1))

    summary = {
        "label": label,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "duration": round(duration, 2),
        "exit_code": result.returncode,
        "log_path": str(log_path),
        "junit_path": str(junit_path) if junit_path else None,
    }

    # Write JSON report
    if json_report:
        json_path = REPORTS_DIR / f"{label.replace(' ', '-').lower()}-summary.json"
        with open(json_path, "w") as f:
            json.dump(summary, f, indent=2)

    return summary


def print_summary(results: list[dict]):
    """Print a formatted summary table."""
    print()
    print("═" * 60)
    print("  OHM Agent Test Harness — Summary")
    print("═" * 60)
    total_passed = total_failed = total_skipped = total_errors = 0
    total_duration = 0.0
    for r in results:
        status = "✓" if r["failed"] == 0 and r["errors"] == 0 else "✗"
        print(f"  {status} {r['label']:25s}  {r['passed']:4d} passed  "
              f"{r['failed']:2d} failed  {r['skipped']:2d} skipped  "
              f"{r['duration']:6.1f}s")
        total_passed += r["passed"]
        total_failed += r["failed"]
        total_skipped += r["skipped"]
        total_errors += r["errors"]
        total_duration += r["duration"]

    print("─" * 60)
    print(f"  TOTAL{'':23s}  {total_passed:4d} passed  "
          f"{total_failed:2d} failed  {total_skipped:2d} skipped  "
          f"{total_duration:6.1f}s")
    print("═" * 60)
    return total_failed + total_errors


def main():
    parser = argparse.ArgumentParser(description="OHM Agent Test Harness")
    parser.add_argument("--all", action="store_true", help="Run full suite")
    parser.add_argument("--unit", action="store_true", help="Run unit tests")
    parser.add_argument("--integration", action="store_true", help="Run integration tests")
    parser.add_argument("--adversarial", action="store_true", help="Run adversarial tests")
    parser.add_argument("--performance", action="store_true", help="Run performance benchmarks")
    parser.add_argument("--concurrent", action="store_true", help="Run concurrent access tests")
    parser.add_argument("--junit", action="store_true", help="Output JUnit XML for Atlas")
    parser.add_argument("--fast-junit", action="store_true", help="Unit + JUnit XML (CI)")
    parser.add_argument("--watch", action="store_true", help="Retry until stable")
    parser.add_argument("--workers", default="auto", help="XDist workers (default: auto)")
    parser.add_argument("--timeout", type=int, default=60, help="Per-test timeout (seconds)")

    args = parser.parse_args()

    # Determine what to run
    suites = []

    if args.integration:
        suites.append(("Unit tests", "unit", None))
        suites.append(("Integration tests", None, None))
    elif args.adversarial:
        suites.append(("Adversarial tests", "adversarial", None))
    elif args.performance:
        suites.append(("Performance tests", "performance", None))
    elif args.concurrent:
        suites.append(("Concurrent tests", "concurrent", None))
    elif args.fast_junit:
        suites.append(("Unit tests (JUnit)", "unit", None))
    elif args.all:
        suites = [
            ("Unit tests", "unit", None),
            ("Integration tests", None, None),
            ("Performance tests", "performance", None),
        ]
    else:
        # Default: unit tests
        suites.append(("Unit tests", "unit", None))

    results = []
    exit_code = 0

    for label, marker, test_file in suites:
        if args.fast_junit or args.junit:
            r = run_pytest(label, marker, test_file, timeout=args.timeout,
                          workers=args.workers, junit=True, json_report=True)
        else:
            r = run_pytest(label, marker, test_file, timeout=args.timeout,
                          workers=args.workers, junit=False, json_report=True)
        results.append(r)

        if r["failed"] > 0 or r["errors"] > 0:
            exit_code = 1
            print(f"\n  ⚠  {r['label']} failed ({r['failed']} failures, {r['errors']} errors)")
            print(f"     Log: {r['log_path']}")

    total_issues = print_summary(results)

    if args.watch and total_issues > 0:
        print("\n  🔄 Watch mode: retrying failed suites...")
        time.sleep(2)
        return main()  # Recursive retry (simplified)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()