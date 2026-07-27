from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from tripchord.domain.common import Money
from tripchord.domain.trip import TripSpec
from tripchord.planning.problem import (
    ActivityAvailability,
    ActivityCandidate,
    PlanningProblem,
    TravelTime,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "benchmarks" / "scenarios" / "planning-scale-v1.jsonl"


def generate(count: int = 120, seed: int = 20260727) -> list[PlanningProblem]:
    randomizer = random.Random(seed)
    problems: list[PlanningProblem] = []
    for scenario_index in range(count):
        day_count = 2 + scenario_index % 2
        start = date(2026, 10, 1) + timedelta(days=scenario_index % 20)
        budget_yuan = 260 + scenario_index % 5 * 35
        trip = TripSpec(
            origin="上海",
            destinations=(f"冻结城市-{scenario_index % 12}",),
            start_date=start,
            end_date=start + timedelta(days=day_count - 1),
            budget=Money(amount=str(budget_yuan), currency="CNY"),
            max_main_activities_per_day=3,
            must_visit=(f"必去-{scenario_index}",),
        )
        activities: list[ActivityCandidate] = []
        for activity_index in range(10):
            activity_id = f"s{scenario_index}-a{activity_index}"
            duration = randomizer.choice((75, 90, 105, 120, 150))
            cost_cents = randomizer.randrange(15, 95, 5) * 100
            utility = randomizer.randint(60, 240)
            must_visit = activity_index == 0
            title = f"必去-{scenario_index}" if must_visit else f"候选-{activity_index}"
            availability: list[ActivityAvailability] = []
            for day_offset in range(day_count):
                day = start + timedelta(days=day_offset)
                if activity_index == 1:
                    window_start, window_end = 540, 720
                elif activity_index == 2:
                    window_start, window_end = 900, 1140
                else:
                    window_start, window_end = 540, 1200
                availability.append(
                    ActivityAvailability(
                        date=day,
                        start_minute=window_start,
                        end_minute=window_end,
                    )
                )
            activities.append(
                ActivityCandidate(
                    id=activity_id,
                    title=title,
                    duration_minutes=duration,
                    cost_cents=cost_cents,
                    utility=utility + (200 if activity_index == 1 else 0),
                    must_visit=must_visit,
                    availability=tuple(availability),
                    source_refs=(f"frozen:{activity_id}",),
                )
            )
        travel_times: list[TravelTime] = []
        for origin in activities:
            for destination in activities:
                if origin.id == destination.id:
                    continue
                pair_seed = sum(ord(char) for char in origin.id + destination.id)
                travel_times.append(
                    TravelTime(
                        origin_id=origin.id,
                        destination_id=destination.id,
                        minutes=15 + pair_seed % 46,
                        source_ref="frozen:route-matrix",
                    )
                )
        problems.append(
            PlanningProblem(
                trip=trip,
                activities=tuple(activities),
                travel_times=tuple(travel_times),
                solver_time_limit_seconds=2,
            )
        )
    return problems


def write_scenarios(path: Path = OUTPUT) -> None:
    lines = [
        json.dumps(
            {"id": f"planning-scale-{index:04d}", "problem": problem.model_dump(mode="json")},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for index, problem in enumerate(generate())
    ]
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    write_scenarios()
