from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tripchord.domain.trip import TripSpec
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import OptimizationResult, PlanningProblem

from benchmarks.baselines import GreedyPlanner, validate_result

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "planning-scale-v1.jsonl"
OUTPUT = ROOT / "training" / "data"
SPLIT_GROUPS = {
    "train": frozenset(range(8)),
    "validation": frozenset({8, 9}),
    "test": frozenset({10, 11}),
}


def city_group_index(problem: PlanningProblem) -> int:
    destination = problem.trip.destinations[0]
    return int(destination.rsplit("-", maxsplit=1)[1])


def split_for(problem: PlanningProblem) -> str:
    group = city_group_index(problem)
    return next(name for name, groups in SPLIT_GROUPS.items() if group in groups)


def serialize_result(result: OptimizationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "total_cost_cents": result.total_cost_cents,
        "total_utility": result.total_utility,
        "scheduled": [
            {
                "activity_id": item.activity_id,
                "date": item.date.isoformat(),
                "start_minute": item.start_minute,
                "end_minute": item.end_minute,
                "source_refs": list(item.source_refs),
            }
            for item in result.scheduled
        ],
        "skipped_activity_ids": list(result.skipped_activity_ids),
    }


def planning_prompt(problem: PlanningProblem) -> str:
    payload = {
        "trip": {
            "origin": problem.trip.origin,
            "destinations": problem.trip.destinations,
            "dates": (problem.trip.start_date, problem.trip.end_date),
            "budget_cents": (
                int(problem.trip.budget.amount * 100) if problem.trip.budget is not None else None
            ),
            "daily_cap": problem.trip.max_main_activities_per_day,
            "must_visit": problem.trip.must_visit,
        },
        "activity_columns": [
            "id",
            "title",
            "duration_minutes",
            "cost_cents",
            "utility",
            "must_visit",
            "availability[date,start_minute,end_minute]",
        ],
        "activities": [
            [
                item.id,
                item.title,
                item.duration_minutes,
                item.cost_cents,
                item.utility,
                item.must_visit,
                [
                    [window.date, window.start_minute, window.end_minute]
                    for window in item.availability
                ],
            ]
            for item in problem.activities
        ],
        "travel_columns": ["origin_id", "destination_id", "minutes"],
        "travel_times": [
            [item.origin_id, item.destination_id, item.minutes]
            for item in problem.travel_times
        ],
        "contract": {
            "hard_constraints": [
                "must_visit",
                "budget",
                "availability",
                "daily_cap",
                "travel_gap",
            ],
            "objective": "maximise utility minus route minutes",
            "output": "strict JSON only",
        },
    }
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    )


def sft_record(
    scenario_id: str,
    problem: PlanningProblem,
    chosen: OptimizationResult,
) -> dict[str, Any]:
    answer = {
        "plan": serialize_result(chosen),
        "verification": {
            "hard_constraint_failures": list(validate_result(problem, chosen)),
            "verdict": "pass",
        },
        "claim_boundary": "synthetic deterministic oracle; reverify volatile facts before booking",
    }
    return {
        "id": scenario_id,
        "city_group": problem.trip.destinations[0],
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are TripChord's constrained planning policy. Produce strict JSON, "
                    "never invent availability or prices, and leave deterministic verification "
                    "to the verifier."
                ),
            },
            {"role": "user", "content": planning_prompt(problem)},
            {
                "role": "assistant",
                "content": json.dumps(
                    answer, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ),
            },
        ],
    }


def dpo_record(
    scenario_id: str,
    problem: PlanningProblem,
    chosen: OptimizationResult,
    rejected: OptimizationResult,
    variant: str,
    reasons: tuple[str, ...],
) -> dict[str, Any]:
    chosen_payload = serialize_result(chosen)
    rejected_payload = serialize_result(rejected)
    rejected_payload["rejection"] = {"variant": variant, "reasons": list(reasons)}
    return {
        "id": f"{scenario_id}:{variant}",
        "scenario_id": scenario_id,
        "city_group": problem.trip.destinations[0],
        "prompt": planning_prompt(problem),
        "chosen": json.dumps(
            chosen_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        "rejected": json.dumps(
            rejected_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
        "rejection_reasons": list(reasons),
        "label_source": "deterministic synthetic oracle",
    }


def build(path: Path = SCENARIOS) -> dict[str, list[dict[str, Any]]]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    optimizer = ItineraryOptimizer()
    greedy = GreedyPlanner()
    datasets: dict[str, list[dict[str, Any]]] = {
        f"sft_{split}": [] for split in SPLIT_GROUPS
    } | {f"dpo_{split}": [] for split in SPLIT_GROUPS}

    for scenario in scenarios:
        scenario_id = str(scenario["id"])
        problem = PlanningProblem.model_validate(scenario["problem"])
        split = split_for(problem)
        chosen = optimizer.solve(problem)
        chosen_failures = validate_result(problem, chosen)
        if chosen_failures:
            raise ValueError(f"oracle plan is invalid for {scenario_id}: {chosen_failures}")
        datasets[f"sft_{split}"].append(sft_record(scenario_id, problem, chosen))

        greedy_result = greedy.solve(problem)
        if greedy_result.total_utility < chosen.total_utility:
            datasets[f"dpo_{split}"].append(
                dpo_record(
                    scenario_id,
                    problem,
                    chosen,
                    greedy_result,
                    "greedy_lower_utility",
                    ("lower_global_utility",),
                )
            )

        no_travel = optimizer.solve(problem.model_copy(update={"travel_times": ()}))
        no_travel_failures = validate_result(problem, no_travel)
        if no_travel_failures:
            datasets[f"dpo_{split}"].append(
                dpo_record(
                    scenario_id,
                    problem,
                    chosen,
                    no_travel,
                    "travel_ablation",
                    no_travel_failures,
                )
            )

        no_budget_trip = TripSpec.model_validate(
            {**problem.trip.model_dump(mode="json"), "budget": None}
        )
        no_budget = optimizer.solve(problem.model_copy(update={"trip": no_budget_trip}))
        no_budget_failures = validate_result(problem, no_budget)
        if no_budget_failures:
            datasets[f"dpo_{split}"].append(
                dpo_record(
                    scenario_id,
                    problem,
                    chosen,
                    no_budget,
                    "budget_ablation",
                    no_budget_failures,
                )
            )
    return datasets


def write(datasets: dict[str, list[dict[str, Any]]], output: Path = OUTPUT) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    for name, records in datasets.items():
        payload = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for record in records
        )
        path = output / f"{name}.jsonl"
        path.write_text(payload)
        files[path.name] = {
            "records": len(records),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
    manifest = {
        "source": str(SCENARIOS.relative_to(ROOT)),
        "label_source": "deterministic synthetic oracle; no human preference labels",
        "split_policy": "destination city group; groups 0-7 train, 8-9 validation, 10-11 test",
        "files": files,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return manifest


if __name__ == "__main__":
    print(json.dumps(write(build()), ensure_ascii=False, indent=2))
