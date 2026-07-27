from __future__ import annotations

import hashlib
import json
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any

from tripchord.domain.trip import TripSpec
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningProblem

from benchmarks.baselines import GreedyPlanner, validate_result

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "planning-scale-v1.jsonl"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * fraction))
    return ordered[index]


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    optimizer = ItineraryOptimizer()
    greedy = GreedyPlanner()
    rows: list[dict[str, Any]] = []
    started = perf_counter()
    for scenario in scenarios:
        problem = PlanningProblem.model_validate(scenario["problem"])
        cp_result = optimizer.solve(problem)
        greedy_result = greedy.solve(problem)
        no_travel_result = optimizer.solve(problem.model_copy(update={"travel_times": ()}))
        no_budget_trip = TripSpec.model_validate(
            {**problem.trip.model_dump(mode="json"), "budget": None}
        )
        no_budget_result = optimizer.solve(problem.model_copy(update={"trip": no_budget_trip}))
        rows.append(
            {
                "id": scenario["id"],
                "cp_failures": validate_result(problem, cp_result),
                "greedy_failures": validate_result(problem, greedy_result),
                "no_travel_failures": validate_result(problem, no_travel_result),
                "no_budget_failures": validate_result(problem, no_budget_result),
                "cp_utility": cp_result.total_utility,
                "greedy_utility": greedy_result.total_utility,
                "cp_latency_ms": cp_result.solver_wall_time_seconds * 1000,
                "greedy_latency_ms": greedy_result.solver_wall_time_seconds * 1000,
                "schedule": [
                    (item.activity_id, item.date.isoformat(), item.start_minute)
                    for item in cp_result.scheduled
                ],
            }
        )
    wall_seconds = perf_counter() - started
    replay_hash = hashlib.sha256(
        json.dumps([row["schedule"] for row in rows], sort_keys=True).encode()
    ).hexdigest()
    cp_latencies = [row["cp_latency_ms"] for row in rows]
    cp_utilities = [row["cp_utility"] for row in rows]
    greedy_utilities = [row["greedy_utility"] for row in rows]
    return {
        "scenario_count": len(rows),
        "cp_valid_rate": mean(not row["cp_failures"] for row in rows),
        "greedy_valid_rate": mean(not row["greedy_failures"] for row in rows),
        "no_travel_valid_rate": mean(not row["no_travel_failures"] for row in rows),
        "no_budget_valid_rate": mean(not row["no_budget_failures"] for row in rows),
        "cp_mean_utility": mean(cp_utilities),
        "greedy_mean_utility": mean(greedy_utilities),
        "utility_lift_over_greedy": (mean(cp_utilities) / mean(greedy_utilities)) - 1,
        "cp_latency_ms_p50": median(cp_latencies),
        "cp_latency_ms_p95": percentile(cp_latencies, 0.95),
        "wall_seconds": wall_seconds,
        "scenarios_per_second": len(rows) / wall_seconds,
        "schedule_sha256": replay_hash,
        "failure_counts": {
            "greedy": sum(bool(row["greedy_failures"]) for row in rows),
            "no_travel": sum(bool(row["no_travel_failures"]) for row in rows),
            "no_budget": sum(bool(row["no_budget_failures"]) for row in rows),
        },
    }


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
