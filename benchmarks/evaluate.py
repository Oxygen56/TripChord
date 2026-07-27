from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tripchord.domain.itinerary import PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning import PlanVerifier

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "verifier-v0.jsonl"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    verifier = PlanVerifier()
    results: list[dict[str, Any]] = []
    for scenario in load_scenarios(path):
        spec = TripSpec.model_validate(scenario["spec"])
        plan = PlanVersion.model_validate(scenario["plan"])
        actual = sorted(item.code.value for item in verifier.verify(spec, plan))
        expected = sorted(scenario["expected_codes"])
        results.append(
            {
                "id": scenario["id"],
                "passed": actual == expected,
                "actual_codes": actual,
                "expected_codes": expected,
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

