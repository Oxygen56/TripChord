from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tripchord.domain.events import PlanEvent
from tripchord.domain.itinerary import ItineraryItem, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.impact import PlanDependency
from tripchord.planning.replanner import LocalReplanner

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "events-v0.jsonl"


def load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in load_scenarios(path):
        expected = scenario["expected"]
        outcome = LocalReplanner().replan(
            TripSpec.model_validate(scenario["spec"]),
            PlanVersion.model_validate(scenario["plan"]),
            PlanEvent.model_validate(scenario["event"]),
            dependencies=tuple(
                PlanDependency.model_validate(item) for item in scenario["dependencies"]
            ),
            replacements={
                item_id: ItineraryItem.model_validate(item)
                for item_id, item in scenario.get("replacements", {}).items()
            },
        )
        changed = sorted(change.item_id for change in outcome.diff.changed_items)
        removed = sorted(outcome.diff.removed_item_ids)
        passed = (
            outcome.status == expected["status"]
            and changed == sorted(expected["changed"])
            and removed == sorted(expected["removed"])
            and outcome.unaffected_preservation_ratio
            == expected["unaffected_preservation_ratio"]
        )
        results.append(
            {
                "id": scenario["id"],
                "passed": passed,
                "status": outcome.status,
                "changed": changed,
                "removed": removed,
                "unaffected_preservation_ratio": outcome.unaffected_preservation_ratio,
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
