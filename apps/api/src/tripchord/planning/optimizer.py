from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

from ortools.sat.python import cp_model

from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion
from tripchord.planning.problem import (
    ActivityAvailability,
    OptimizationResult,
    PlanningInfeasible,
    PlanningProblem,
    ScheduledActivity,
)


class ItineraryOptimizer:
    def solve(self, problem: PlanningProblem) -> OptimizationResult:
        model = cp_model.CpModel()
        days = [
            problem.trip.start_date + timedelta(days=offset)
            for offset in range(problem.trip.day_count)
        ]
        travel = {
            (item.origin_id, item.destination_id): item.minutes for item in problem.travel_times
        }

        selected: dict[tuple[str, date], cp_model.IntVar] = {}
        starts: dict[tuple[str, date], cp_model.IntVar] = {}
        ends: dict[tuple[str, date], cp_model.IntVar] = {}
        intervals: dict[date, list[cp_model.IntervalVar]] = defaultdict(list)

        for activity in problem.activities:
            windows_by_day: dict[date, list[ActivityAvailability]] = defaultdict(list)
            for window in activity.availability:
                windows_by_day[window.date].append(window)
            activity_choices: list[cp_model.IntVar] = []
            for day in days:
                presence = model.new_bool_var(f"selected_{activity.id}_{day}")
                start = model.new_int_var(0, 1440, f"start_{activity.id}_{day}")
                end = model.new_int_var(0, 1440, f"end_{activity.id}_{day}")
                interval = model.new_optional_interval_var(
                    start,
                    activity.duration_minutes,
                    end,
                    presence,
                    f"interval_{activity.id}_{day}",
                )
                selected[(activity.id, day)] = presence
                starts[(activity.id, day)] = start
                ends[(activity.id, day)] = end
                intervals[day].append(interval)
                activity_choices.append(presence)
                windows = [
                    window
                    for window in windows_by_day.get(day, [])
                    if window.end_minute - window.start_minute >= activity.duration_minutes
                ]
                if not windows:
                    model.add(presence == 0)
                else:
                    window_choices: list[cp_model.IntVar] = []
                    for index, window in enumerate(windows):
                        window_selected = model.new_bool_var(
                            f"window_{activity.id}_{day}_{index}"
                        )
                        model.add(start >= window.start_minute).only_enforce_if(window_selected)
                        model.add(end <= window.end_minute).only_enforce_if(window_selected)
                        window_choices.append(window_selected)
                    model.add(sum(window_choices) == presence)
            model.add(
                sum(activity_choices) == 1
                if activity.must_visit
                else sum(activity_choices) <= 1
            )

        for day in days:
            model.add_no_overlap(intervals[day])
            model.add(
                sum(selected[(activity.id, day)] for activity in problem.activities)
                <= problem.trip.max_main_activities_per_day
            )

        route_penalties: list[tuple[int, cp_model.IntVar]] = []
        for first, second in combinations(problem.activities, 2):
            for day in days:
                first_before = model.new_bool_var(f"before_{first.id}_{second.id}_{day}")
                second_before = model.new_bool_var(f"before_{second.id}_{first.id}_{day}")
                first_selected = selected[(first.id, day)]
                second_selected = selected[(second.id, day)]
                both = model.new_bool_var(f"both_{first.id}_{second.id}_{day}")
                model.add_multiplication_equality(both, [first_selected, second_selected])
                model.add(first_before + second_before == both)
                first_to_second = travel.get((first.id, second.id), 0)
                second_to_first = travel.get((second.id, first.id), 0)
                model.add(
                    starts[(second.id, day)]
                    >= ends[(first.id, day)] + first_to_second
                ).only_enforce_if(first_before)
                model.add(
                    starts[(first.id, day)]
                    >= ends[(second.id, day)] + second_to_first
                ).only_enforce_if(second_before)
                route_penalties.extend(
                    [
                        (first_to_second, first_before),
                        (second_to_first, second_before),
                    ]
                )

        if problem.trip.budget is not None and problem.trip.budget.currency == "CNY":
            budget_cents = int(problem.trip.budget.amount * 100)
            model.add(
                sum(
                    activity.cost_cents * selected[(activity.id, day)]
                    for activity in problem.activities
                    for day in days
                )
                <= budget_cents
            )

        utility_terms = [
            activity.utility * selected[(activity.id, day)]
            for activity in problem.activities
            for day in days
        ]
        travel_terms = [minutes * variable for minutes, variable in route_penalties]
        model.maximize(sum(utility_terms) - sum(travel_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = problem.solver_time_limit_seconds
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        status = solver.solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            must = [activity.id for activity in problem.activities if activity.must_visit]
            raise PlanningInfeasible(
                f"no feasible schedule; check availability, budget, and must-visit items: {must}"
            )

        scheduled: list[ScheduledActivity] = []
        for activity in problem.activities:
            for day in days:
                if solver.value(selected[(activity.id, day)]):
                    scheduled.append(
                        ScheduledActivity(
                            activity_id=activity.id,
                            title=activity.title,
                            date=day,
                            start_minute=solver.value(starts[(activity.id, day)]),
                            end_minute=solver.value(ends[(activity.id, day)]),
                            cost_cents=activity.cost_cents,
                            utility=activity.utility,
                            source_refs=activity.source_refs,
                            location_name=activity.location_name,
                        )
                    )
        scheduled.sort(key=lambda item: (item.date, item.start_minute, item.activity_id))
        selected_ids = {item.activity_id for item in scheduled}
        return OptimizationResult(
            status="optimal" if status == cp_model.OPTIMAL else "feasible",
            objective_value=solver.objective_value,
            scheduled=tuple(scheduled),
            skipped_activity_ids=tuple(
                activity.id for activity in problem.activities if activity.id not in selected_ids
            ),
            total_cost_cents=sum(item.cost_cents for item in scheduled),
            total_utility=sum(item.utility for item in scheduled),
            solver_wall_time_seconds=solver.wall_time,
        )

    def to_plan(
        self,
        result: OptimizationResult,
        problem: PlanningProblem,
        *,
        trip_id: str,
        plan_id: str,
        version: int = 1,
    ) -> PlanVersion:
        timezone = ZoneInfo(problem.timezone)
        items = tuple(
            ItineraryItem(
                id=f"activity:{item.activity_id}",
                kind=ItemKind.ACTIVITY,
                title=item.title,
                starts_at=datetime.combine(item.date, time.min, tzinfo=timezone)
                + timedelta(minutes=item.start_minute),
                ends_at=datetime.combine(item.date, time.min, tzinfo=timezone)
                + timedelta(minutes=item.end_minute),
                location_name=item.location_name,
                source_refs=item.source_refs,
            )
            for item in result.scheduled
        )
        return PlanVersion(
            id=plan_id,
            trip_id=trip_id,
            version=version,
            items=items,
            explanation="Deterministic activity schedule generated by CP-SAT.",
        )
