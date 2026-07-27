from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import PlanVersion
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningProblem
from tripchord.planning.replanner import LocalReplanner, ReplanStatus

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "planning-scale-v1.jsonl"


def preservation_ratio(before: PlanVersion, after: PlanVersion) -> float:
    before_items = {item.id: item for item in before.items}
    after_items = {item.id: item for item in after.items}
    preserved = sum(after_items.get(item_id) == item for item_id, item in before_items.items())
    return preserved / len(before_items) if before_items else 1.0


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    optimizer = ItineraryOptimizer()
    rows: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios):
        problem = PlanningProblem.model_validate(scenario["problem"])
        initial_result = optimizer.solve(problem)
        before = optimizer.to_plan(
            initial_result,
            problem,
            trip_id=f"event-trip-{index}",
            plan_id=f"event-trip-{index}:plan:v1",
        )
        candidates = {item.id: item for item in problem.activities}
        target = next(
            item
            for item in initial_result.scheduled
            if not candidates[item.activity_id].must_visit
        )
        event = PlanEvent(
            id=f"closure-{index}",
            trip_id=before.trip_id,
            kind=EventKind.PLACE_CLOSED,
            occurred_at=datetime(2026, 9, 1, tzinfo=UTC),
            target_refs=(f"activity:{target.activity_id}",),
        )
        local = LocalReplanner().replan(problem.trip, before, event)

        global_problem = problem.model_copy(
            update={
                "activities": tuple(
                    item for item in problem.activities if item.id != target.activity_id
                )
            }
        )
        global_result = optimizer.solve(global_problem)
        global_plan = optimizer.to_plan(
            global_result,
            global_problem,
            trip_id=before.trip_id,
            plan_id=f"{before.trip_id}:plan:v2",
            version=2,
        )
        initial_utility = sum(item.utility for item in before.items)
        rows.append(
            {
                "local_ready": local.status == ReplanStatus.READY,
                "local_preservation": local.overall_preservation_ratio,
                "unaffected_preservation": local.unaffected_preservation_ratio,
                "global_preservation": preservation_ratio(before, global_plan),
                "local_utility_retention": (
                    sum(item.utility for item in local.final_plan.items) / initial_utility
                ),
                "global_utility_retention": (
                    sum(item.utility for item in global_plan.items) / initial_utility
                ),
            }
        )
    return {
        "scenario_count": len(rows),
        "local_recovery_rate": mean(row["local_ready"] for row in rows),
        "local_mean_preservation": mean(row["local_preservation"] for row in rows),
        "local_unaffected_preservation": mean(
            row["unaffected_preservation"] for row in rows
        ),
        "global_mean_preservation": mean(row["global_preservation"] for row in rows),
        "local_mean_utility_retention": mean(
            row["local_utility_retention"] for row in rows
        ),
        "global_mean_utility_retention": mean(
            row["global_utility_retention"] for row in rows
        ),
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
