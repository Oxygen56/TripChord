from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningInfeasible, PlanningProblem

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "optimizer-v0.jsonl"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in load_scenarios(path):
        problem = PlanningProblem.model_validate(scenario["problem"])
        try:
            solved = ItineraryOptimizer().solve(problem)
            actual = sorted(item.activity_id for item in solved.scheduled)
            infeasible = False
        except PlanningInfeasible:
            actual = []
            infeasible = True
        expected = sorted(scenario["expected_selected"])
        passed = actual == expected and infeasible == scenario["expected_infeasible"]
        results.append(
            {
                "id": scenario["id"],
                "passed": passed,
                "actual_selected": actual,
                "expected_selected": expected,
                "actual_infeasible": infeasible,
                "expected_infeasible": scenario["expected_infeasible"],
            }
        )
    passed = sum(1 for result in results if result["passed"])
    return {
        "scenario_count": len(results),
        "passed": passed,
        "pass_rate": passed / len(results) if results else 0,
        "results": results,
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

