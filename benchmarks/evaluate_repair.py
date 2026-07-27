from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tripchord.domain.itinerary import PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "repair-v0.jsonl"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in load_scenarios(path):
        outcome = PlanningWorkflow().run(
            TripSpec.model_validate(scenario["spec"]),
            PlanVersion.model_validate(scenario["plan"]),
            VerificationContext.model_validate(scenario["context"]),
        )
        changed = sorted(
            change.item_id for trace in outcome.traces for change in trace.diff.changed_items
        )
        removed = sorted(
            item_id for trace in outcome.traces for item_id in trace.diff.removed_item_ids
        )
        passed = (
            outcome.status == scenario["expected_status"]
            and changed == sorted(scenario["expected_changed"])
            and removed == sorted(scenario["expected_removed"])
        )
        results.append(
            {
                "id": scenario["id"],
                "passed": passed,
                "actual_status": outcome.status,
                "changed": changed,
                "removed": removed,
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
