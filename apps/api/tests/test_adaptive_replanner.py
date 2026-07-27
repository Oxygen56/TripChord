import json
from datetime import UTC, datetime
from pathlib import Path

from tripchord.domain.events import EventKind, PlanEvent
from tripchord.planning.adaptive import AdaptiveReplanner
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.policy import ReplanMode, ReplanPolicySelector, ReplanPreference
from tripchord.planning.problem import PlanningProblem

ROOT = Path(__file__).resolve().parents[3]


def frozen_problem() -> PlanningProblem:
    scenario = json.loads(
        (ROOT / "benchmarks/scenarios/planning-scale-v1.jsonl").read_text().splitlines()[0]
    )
    return PlanningProblem.model_validate(scenario["problem"])


def test_adaptive_replanner_selects_verified_policy_tradeoff() -> None:
    problem = frozen_problem()
    optimizer = ItineraryOptimizer()
    solved = optimizer.solve(problem)
    plan = optimizer.to_plan(solved, problem, trip_id="trip", plan_id="trip:plan:v1")
    candidates = {item.id: item for item in problem.activities}
    target = next(
        item for item in solved.scheduled if not candidates[item.activity_id].must_visit
    )
    event = PlanEvent(
        id="closure",
        trip_id="trip",
        kind=EventKind.PLACE_CLOSED,
        occurred_at=datetime(2026, 10, 1, tzinfo=UTC),
        target_refs=(f"activity:{target.activity_id}",),
    )
    replanner = AdaptiveReplanner(
        ReplanPolicySelector.from_path(ROOT / "training/artifacts/replan-policy.json")
    )

    stable = replanner.replan(
        problem.trip,
        plan,
        event,
        ReplanPreference.MINIMUM_CHANGE,
        problem,
    )
    quality = replanner.replan(
        problem.trip,
        plan,
        event,
        ReplanPreference.QUALITY_FIRST,
        problem,
    )

    assert stable.status == "ready"
    assert stable.selected_mode == ReplanMode.LOCAL
    assert quality.selected_mode == ReplanMode.GLOBAL
    assert all(candidate.hard_valid for candidate in quality.candidates)
    local, global_candidate = quality.candidates
    assert local.preservation_ratio > global_candidate.preservation_ratio
    assert global_candidate.utility_retention > local.utility_retention
    assert event.id in quality.final_plan.applied_event_ids
