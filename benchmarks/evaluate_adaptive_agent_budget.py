from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from tripchord.agents.adaptive_control import (
    BROWSER_CONCURRENCY,
    LOGICAL_AGENT_HARD_CAP,
    QUNAR_LODGING_CONCURRENCY,
    AdaptiveControlInput,
    ScaleDirective,
    derive_scale_directive,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "benchmarks" / "scenarios" / "adaptive-agent-budget-v1.json"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "adaptive-agent-budget-v1.json"
BENCHMARK_ID = "adaptive-agent-budget-v1"
FROZEN_INPUT_SHA256 = "62c8c8f0cc9977fcdca61361bd47183a0617ab3effa3e1875844a919c1416157"
REPEAT_COUNT = 5
EXPECTED_CLASSES = ("simple", "standard", "complex", "audit")

_EXPECTED_FIELDS = (
    "date_shards",
    "date_mergers",
    "candidate_shards",
    "background_batches",
    "raw_logical_agents",
    "logical_agent_cap",
    "logical_saturated",
    "raw_model_concurrency",
    "desired_model_concurrency",
    "health_adjusted_model_concurrency",
    "browser_concurrency",
    "qunar_lodging_concurrency",
    "theoretical_browser_task_count",
    "theoretical_icom_task_count",
    "state_fingerprint",
)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def load_scenarios(path: Path = SCENARIO) -> dict[str, Any]:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != FROZEN_INPUT_SHA256:
        raise ValueError(
            "frozen adaptive Agent budget fixture hash mismatch; create a new benchmark version"
        )
    fixture: dict[str, Any] = json.loads(content)
    if fixture.get("benchmark_id") != BENCHMARK_ID:
        raise ValueError("adaptive Agent budget benchmark id mismatch")
    if fixture.get("input_classification") != {
        "kind": "synthetic_controller_state",
        "live": False,
        "model_calls": False,
        "browser_calls": False,
    }:
        raise ValueError("adaptive Agent budget inputs must remain synthetic and offline")
    scenarios = fixture.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("adaptive Agent budget scenarios must be a list")
    scenario_classes = tuple(row.get("class") for row in scenarios)
    scenario_ids = tuple(row.get("id") for row in scenarios)
    if scenario_classes != EXPECTED_CLASSES or scenario_ids != EXPECTED_CLASSES:
        raise ValueError("adaptive Agent budget scenario classes or order changed")
    if any(set(row.get("expected", {})) != set(_EXPECTED_FIELDS) for row in scenarios):
        raise ValueError("adaptive Agent budget expected-result schema changed")
    return fixture


def _project_directive(directive: ScaleDirective) -> dict[str, Any]:
    payload = directive.model_dump(mode="json")
    return {field: payload[field] for field in _EXPECTED_FIELDS}


def _evaluate_scenario(row: dict[str, Any]) -> dict[str, Any]:
    control = AdaptiveControlInput.model_validate(row["input"])
    runs = tuple(derive_scale_directive(control) for _ in range(REPEAT_COUNT))
    serialized_runs = tuple(run.model_dump(mode="json") for run in runs)
    actual = _project_directive(runs[0])
    expected = dict(row["expected"])
    reproducible = all(item == serialized_runs[0] for item in serialized_runs[1:])
    checks = {
        "expected_budget": actual == expected,
        "logical_agent_cap_enforced": runs[0].logical_agent_cap
        <= LOGICAL_AGENT_HARD_CAP,
        "model_concurrency_level_expected": (
            runs[0].desired_model_concurrency
            == expected["desired_model_concurrency"]
            and runs[0].health_adjusted_model_concurrency
            == expected["health_adjusted_model_concurrency"]
        ),
        "browser_concurrency_fixed": runs[0].browser_concurrency == BROWSER_CONCURRENCY == 6,
        "qunar_lodging_concurrency_fixed": (
            runs[0].qunar_lodging_concurrency == QUNAR_LODGING_CONCURRENCY == 1
        ),
        "same_input_reproducible": reproducible,
    }
    if row["class"] == "audit":
        checks["cap_behavior_expected"] = (
            runs[0].raw_logical_agents > LOGICAL_AGENT_HARD_CAP
            and runs[0].logical_agent_cap == LOGICAL_AGENT_HARD_CAP
            and runs[0].logical_saturated
        )
    else:
        checks["cap_behavior_expected"] = (
            runs[0].raw_logical_agents == runs[0].logical_agent_cap
            and not runs[0].logical_saturated
        )
    return {
        "id": row["id"],
        "class": row["class"],
        "input": control.model_dump(mode="json"),
        "expected": expected,
        "actual": actual,
        "repeat_count": REPEAT_COUNT,
        "checks": checks,
        "passed": all(checks.values()),
    }


def evaluate(path: Path = SCENARIO) -> dict[str, Any]:
    fixture = load_scenarios(path)
    rows = [_evaluate_scenario(row) for row in fixture["scenarios"]]
    checks = {
        "four_frozen_requirement_classes": tuple(row["class"] for row in rows)
        == EXPECTED_CLASSES,
        "all_expected_budgets_match": all(row["checks"]["expected_budget"] for row in rows),
        "model_concurrency_ladder_is_2_6_8_12": tuple(
            row["actual"]["desired_model_concurrency"] for row in rows
        )
        == (2, 6, 8, 12),
        "logical_agent_budget_never_exceeds_96": all(
            row["actual"]["logical_agent_cap"] <= LOGICAL_AGENT_HARD_CAP for row in rows
        ),
        "audit_input_is_capped_at_96": rows[-1]["checks"]["cap_behavior_expected"],
        "browser_concurrency_is_always_6": all(
            row["checks"]["browser_concurrency_fixed"] for row in rows
        ),
        "qunar_lodging_concurrency_is_always_1": all(
            row["checks"]["qunar_lodging_concurrency_fixed"] for row in rows
        ),
        "same_input_is_reproducible": all(
            row["checks"]["same_input_reproducible"] for row in rows
        ),
    }
    result: dict[str, Any] = {
        "benchmark_id": BENCHMARK_ID,
        "policy_version": "adaptive-control-v1",
        "scenario_file": "benchmarks/scenarios/adaptive-agent-budget-v1.json",
        "scenario_sha256": FROZEN_INPUT_SHA256,
        "input_classification": fixture["input_classification"],
        "scenario_count": len(rows),
        "rows": rows,
        "checks": checks,
        "passed": all(checks.values()) and all(row["passed"] for row in rows),
        "claim_boundary": {
            "deterministic_budget_regression_claim_allowed": True,
            "live_travel_quality_claim_allowed": False,
            "model_quality_claim_allowed": False,
            "browser_throughput_claim_allowed": False,
            "production_sla_claim_allowed": False,
        },
    }
    result["result_sha256"] = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the frozen adaptive Agent budget.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_bytes(result))
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": result["passed"],
                "result_sha256": result["result_sha256"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
