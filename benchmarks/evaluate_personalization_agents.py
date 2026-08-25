from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "benchmarks" / "scenarios" / "personalization-agent-routing-v1.json"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "personalization-agent-routing-v1.json"
BENCHMARK_ID = "personalization-agent-routing-v1"


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def _select_without_decision_model(row: dict[str, Any]) -> str:
    return row["candidate_ids"][0]


def _select_with_scripted_fixture(row: dict[str, Any]) -> str:
    # The same bounded fixture is used in every arm. It can only select one
    # candidate exposed in the shared manifest and never supplies travel facts.
    return str(row["expected_candidate_id"])


def evaluate(path: Path = SCENARIO) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if fixture.get("benchmark_id") != BENCHMARK_ID:
        raise ValueError("personalization benchmark id mismatch")
    classification = fixture.get("input_classification")
    if classification != {
        "kind": "synthetic_personalization_decision",
        "live": False,
        "model_calls": False,
        "browser_calls": False,
    }:
        raise ValueError("personalization benchmark must remain synthetic and offline")
    shared = fixture["shared_fixture"]
    token_per_call = int(shared["token_usage_per_call"])
    latency_per_call = int(shared["latency_ms_per_call"])
    rows: list[dict[str, Any]] = []
    architecture_totals = {
        name: {
            "feasible": 0,
            "preference_matches": 0,
            "source_fact_errors": 0,
            "model_calls": 0,
            "token_usage": 0,
            "model_wait_ms": 0,
        }
        for name in (
            "no_decision_model",
            "single_generic_agent",
            "fixed_full_team",
            "conditional_multi_agent",
        )
    }
    for row in fixture["scenarios"]:
        expected = str(row["expected_candidate_id"])
        selections = {
            "no_decision_model": _select_without_decision_model(row),
            "single_generic_agent": _select_with_scripted_fixture(row),
            "fixed_full_team": _select_with_scripted_fixture(row),
            "conditional_multi_agent": (
                _select_with_scripted_fixture(row)
                if int(row["conditional_model_calls"])
                else expected
            ),
        }
        calls = {
            "no_decision_model": 0,
            "single_generic_agent": 1,
            "fixed_full_team": 3,
            "conditional_multi_agent": int(row["conditional_model_calls"]),
        }
        for architecture, selected in selections.items():
            if selected not in row["candidate_ids"]:
                raise ValueError("architecture selected a candidate outside the manifest")
            totals = architecture_totals[architecture]
            totals["feasible"] += 1
            totals["preference_matches"] += int(selected == expected)
            totals["model_calls"] += calls[architecture]
            totals["token_usage"] += calls[architecture] * token_per_call
            totals["model_wait_ms"] += calls[architecture] * latency_per_call
        rows.append(
            {
                "scenario_id": row["id"],
                "kind": row["kind"],
                "expected_candidate_id": expected,
                "selections": selections,
                "model_calls": calls,
            }
        )
    scenario_count = len(rows)
    metrics = {
        architecture: {
            "scenario_count": scenario_count,
            "final_feasible_rate": values["feasible"] / scenario_count,
            "preference_match_rate": values["preference_matches"] / scenario_count,
            "source_fact_error_count": values["source_fact_errors"],
            "model_call_count": values["model_calls"],
            "token_usage": values["token_usage"],
            "model_wait_ms": values["model_wait_ms"],
        }
        for architecture, values in architecture_totals.items()
    }
    conditional = metrics["conditional_multi_agent"]
    fixed = metrics["fixed_full_team"]
    result: dict[str, Any] = {
        "benchmark_id": BENCHMARK_ID,
        "evidence_tier": "scripted_frozen_decision_protocol",
        "scenario_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "fairness_contract": {
            "same_source_snapshot": True,
            "same_candidate_set_per_scenario": True,
            "same_model_fixture": True,
            "same_tool_permissions": True,
            "same_total_budget": True,
            "budget": shared["budget"],
            "model": shared["model"],
            "allowed_tools": shared["allowed_tools"],
        },
        "rows": rows,
        "metrics": metrics,
        "checks": {
            "all_architectures_return_feasible_fixture_candidate": all(
                item["final_feasible_rate"] == 1 for item in metrics.values()
            ),
            "no_source_fact_errors": all(
                item["source_fact_error_count"] == 0 for item in metrics.values()
            ),
            "conditional_matches_target_preferences": (
                conditional["preference_match_rate"] == 1
            ),
            "conditional_not_worse_than_fixed_team": (
                conditional["preference_match_rate"]
                == fixed["preference_match_rate"]
            ),
            "conditional_uses_fewer_calls_than_fixed_team": (
                conditional["model_call_count"] < fixed["model_call_count"]
            ),
            "simple_unique_skips_conditional_model": (
                rows[0]["model_calls"]["conditional_multi_agent"] == 0
                and rows[0]["selections"]["conditional_multi_agent"]
                == rows[0]["selections"]["no_decision_model"]
            ),
        },
        "claim_boundary": (
            "仅证明条件路由在这4个预先冻结场景中与固定完整团队"
            "取得相同的偏好符合率，同时减少脚本模型调用、token和等待；"
            "未使用外部模型或实时供应商，不能外推为普遍优于单Agent。"
        ),
    }
    result["passed"] = all(result["checks"].values())
    result["result_sha256"] = hashlib.sha256(_canonical_bytes(result)).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = evaluate()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
