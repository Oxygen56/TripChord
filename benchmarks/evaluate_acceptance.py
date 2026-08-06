"""Five anti-surface end-to-end acceptance (contract 第七节, item 5).

The product contract requires at least five machine-executable anti-surface
acceptance checks to stay frozen:

1. dynamic 0/1/2/4 provider DAGs build correctly (and 0 providers refuses to
   start);
2. a slow source / late result never triggers the Planner early (Planner first
   call is strictly after the last selected Source terminal_at);
3. a user-closed scope produces zero browser tasks, zero model tool calls and
   zero network access;
4. open-redirect / payment-path handoff URLs are all rejected;
5. under any event sequence, the booked-component modification rate is 0
   unless an explicit override is applied.

Each surface is a deterministic test module in ``apps/api/tests/``.  This
runner executes the frozen set, aggregates a typed verdict and writes an atomic
``benchmarks/results/product-acceptance.json``.  Exit code 0 only when every
applicable surface passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "benchmarks" / "results" / "product-acceptance.json"

ACCEPTANCE_SCHEMA = "tripchord-five-anti-surface-acceptance-v1"

# Each surface -> the frozen test module that proves it.
SURFACES: tuple[tuple[str, str], ...] = (
    (
        "dynamic_provider_matrix",
        "apps/api/tests/test_platform_kernel.py",
    ),
    (
        "planner_after_all_sources_terminal",
        "apps/api/tests/test_terminal_barrier.py",
    ),
    (
        "closed_scope_zero_access",
        "apps/api/tests/test_browser_cancellation.py",
    ),
    (
        "handoff_dangerous_urls_rejected",
        "apps/api/tests/test_official_handoff.py",
    ),
    (
        "booked_component_modification_zero",
        "apps/api/tests/test_booking_gate.py",
    ),
    (
        "booking_gate_consumed_by_planning",
        "apps/api/tests/test_booking_planning_integration.py",
    ),
)


def _run_surface(name: str, module: str) -> dict[str, object]:
    result = subprocess.run(
        ["uv", "run", "pytest", module, "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        "name": name,
        "module": module,
        "passed": result.returncode == 0,
        "detail": (result.stdout[-400:] if result.returncode else ""),
    }


def evaluate() -> dict[str, object]:
    checks = [_run_surface(name, module) for name, module in SURFACES]
    passed = all(bool(check["passed"]) for check in checks)
    return {
        "schema_version": ACCEPTANCE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "surfaces": checks,
        "passed": passed,
        "summary": "all five anti-surface acceptance checks passed"
        if passed
        else "one or more anti-surface acceptance checks failed",
    }


def main() -> int:
    report = evaluate()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(tmp, OUTPUT_PATH)
    passed = bool(report["passed"])
    print(f"five anti-surface acceptance  {report['generated_at']}")
    for check in report["surfaces"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(f"  [{marker}] {check['name']}  {check['module']}")
    print(f"\nverdict: {report['summary']}")
    print(f"evidence: {OUTPUT_PATH}")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
