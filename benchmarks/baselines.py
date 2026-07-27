from __future__ import annotations

from collections import defaultdict
from datetime import date
from itertools import pairwise
from time import perf_counter

from tripchord.planning.problem import (
    ActivityCandidate,
    OptimizationResult,
    PlanningInfeasible,
    PlanningProblem,
    ScheduledActivity,
)


class GreedyPlanner:
    """Deterministic earliest-fit baseline; intentionally lacks global search."""

    def solve(self, problem: PlanningProblem) -> OptimizationResult:
        started = perf_counter()
        travel = {
            (item.origin_id, item.destination_id): item.minutes for item in problem.travel_times
        }
        ordered = sorted(
            problem.activities,
            key=lambda item: (
                not item.must_visit,
                -item.utility,
                item.cost_cents,
                item.id,
            ),
        )
        scheduled_by_day: dict[date, list[ScheduledActivity]] = defaultdict(list)
        selected: list[ScheduledActivity] = []
        total_cost = 0
        budget_cents = (
            int(problem.trip.budget.amount * 100)
            if problem.trip.budget is not None and problem.trip.budget.currency == "CNY"
            else None
        )

        for candidate in ordered:
            if budget_cents is not None and total_cost + candidate.cost_cents > budget_cents:
                if candidate.must_visit:
                    raise PlanningInfeasible("greedy baseline cannot afford a must-visit item")
                continue
            placement = self._place(candidate, scheduled_by_day, travel, problem)
            if placement is None:
                if candidate.must_visit:
                    raise PlanningInfeasible("greedy baseline cannot place a must-visit item")
                continue
            scheduled_by_day[placement.date].append(placement)
            scheduled_by_day[placement.date].sort(key=lambda item: item.start_minute)
            selected.append(placement)
            total_cost += candidate.cost_cents

        selected.sort(key=lambda item: (item.date, item.start_minute, item.activity_id))
        selected_ids = {item.activity_id for item in selected}
        return OptimizationResult(
            status="greedy",
            objective_value=float(sum(item.utility for item in selected)),
            scheduled=tuple(selected),
            skipped_activity_ids=tuple(
                item.id for item in problem.activities if item.id not in selected_ids
            ),
            total_cost_cents=total_cost,
            total_utility=sum(item.utility for item in selected),
            solver_wall_time_seconds=perf_counter() - started,
        )

    def _place(
        self,
        candidate: ActivityCandidate,
        scheduled_by_day: dict[date, list[ScheduledActivity]],
        travel: dict[tuple[str, str], int],
        problem: PlanningProblem,
    ) -> ScheduledActivity | None:
        windows = sorted(
            candidate.availability,
            key=lambda item: (item.date, item.start_minute),
        )
        for window in windows:
            day_items = scheduled_by_day[window.date]
            if len(day_items) >= problem.trip.max_main_activities_per_day:
                continue
            previous = day_items[-1] if day_items else None
            earliest = window.start_minute
            if previous is not None:
                earliest = max(
                    earliest,
                    previous.end_minute
                    + travel.get((previous.activity_id, candidate.id), 0),
                )
            if earliest + candidate.duration_minutes > window.end_minute:
                continue
            return ScheduledActivity(
                activity_id=candidate.id,
                title=candidate.title,
                date=window.date,
                start_minute=earliest,
                end_minute=earliest + candidate.duration_minutes,
                cost_cents=candidate.cost_cents,
                utility=candidate.utility,
                source_refs=candidate.source_refs,
                location_name=candidate.location_name,
            )
        return None


def validate_result(problem: PlanningProblem, result: OptimizationResult) -> tuple[str, ...]:
    activities = {item.id: item for item in problem.activities}
    travel = {
        (item.origin_id, item.destination_id): item.minutes for item in problem.travel_times
    }
    failures: set[str] = set()
    selected_ids = {item.activity_id for item in result.scheduled}
    if any(item.must_visit and item.id not in selected_ids for item in problem.activities):
        failures.add("must_visit")
    if len(selected_ids) != len(result.scheduled):
        failures.add("duplicate")
    if (
        problem.trip.budget is not None
        and problem.trip.budget.currency == "CNY"
        and result.total_cost_cents > int(problem.trip.budget.amount * 100)
    ):
        failures.add("budget")

    by_day: dict[date, list[ScheduledActivity]] = defaultdict(list)
    for item in result.scheduled:
        candidate = activities[item.activity_id]
        by_day[item.date].append(item)
        if item.end_minute - item.start_minute != candidate.duration_minutes:
            failures.add("duration")
        if not any(
            window.date == item.date
            and item.start_minute >= window.start_minute
            and item.end_minute <= window.end_minute
            for window in candidate.availability
        ):
            failures.add("availability")
    for items in by_day.values():
        ordered = sorted(items, key=lambda item: item.start_minute)
        if len(ordered) > problem.trip.max_main_activities_per_day:
            failures.add("daily_cap")
        for previous, current in pairwise(ordered):
            required = travel.get((previous.activity_id, current.activity_id), 0)
            if current.start_minute < previous.end_minute + required:
                failures.add("travel")
    return tuple(sorted(failures))
