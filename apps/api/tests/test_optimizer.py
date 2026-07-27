from datetime import date
from decimal import Decimal

import pytest
from tripchord.domain.common import Money
from tripchord.domain.trip import TripSpec
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import (
    ActivityAvailability,
    ActivityCandidate,
    PlanningInfeasible,
    PlanningProblem,
    TravelTime,
)

TRIP_DATE = date(2026, 10, 2)


def activity(
    activity_id: str,
    *,
    utility: int,
    cost_cents: int,
    must_visit: bool = False,
    available_date: date = TRIP_DATE,
) -> ActivityCandidate:
    return ActivityCandidate(
        id=activity_id,
        title=activity_id,
        duration_minutes=60,
        cost_cents=cost_cents,
        utility=utility,
        must_visit=must_visit,
        availability=(
            ActivityAvailability(date=available_date, start_minute=9 * 60, end_minute=17 * 60),
        ),
        source_refs=(f"fixture:{activity_id}",),
    )


def trip(*, budget: str = "100") -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=TRIP_DATE,
        end_date=TRIP_DATE,
        budget=Money(amount=Decimal(budget), currency="CNY"),
        max_main_activities_per_day=2,
    )


def test_optimizer_respects_budget_count_must_visit_and_travel_gap() -> None:
    problem = PlanningProblem(
        trip=trip(),
        activities=(
            activity("must", utility=100, cost_cents=3000, must_visit=True),
            activity("good", utility=90, cost_cents=2000),
            activity("expensive", utility=1000, cost_cents=20000),
        ),
        travel_times=(
            TravelTime(origin_id="must", destination_id="good", minutes=30),
            TravelTime(origin_id="good", destination_id="must", minutes=30),
        ),
    )

    result = ItineraryOptimizer().solve(problem)

    assert {item.activity_id for item in result.scheduled} == {"must", "good"}
    assert result.total_cost_cents == 5000
    first, second = result.scheduled
    assert second.start_minute >= first.end_minute + 30


def test_optimizer_reports_infeasible_must_visit() -> None:
    problem = PlanningProblem(
        trip=trip(),
        activities=(
            activity(
                "closed",
                utility=100,
                cost_cents=0,
                must_visit=True,
                available_date=date(2026, 10, 3),
            ),
        ),
    )

    with pytest.raises(PlanningInfeasible, match="no feasible schedule"):
        ItineraryOptimizer().solve(problem)

