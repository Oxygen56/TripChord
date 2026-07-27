from datetime import date
from pathlib import Path

from tripchord.domain.trip import TripSpec
from tripchord.planning.assembler import PlanningProblemAssembler, ReplayPlaceCatalog
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.workflow import PlanningWorkflow, WorkflowStatus

ROOT = Path(__file__).resolve().parents[3]


def test_replay_catalog_assembles_a_traceable_end_to_end_problem() -> None:
    trip = TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 3),
        interests=("历史",),
        must_visit=("故宫",),
    )
    assembler = PlanningProblemAssembler(
        ReplayPlaceCatalog(ROOT / "data" / "replay" / "places.json")
    )

    problem = assembler.assemble(trip)
    solved = ItineraryOptimizer().solve(problem)
    plan = ItineraryOptimizer().to_plan(
        solved,
        problem,
        trip_id="trip-1",
        plan_id="trip-1:plan:v1",
    )
    verified = PlanningWorkflow().run(trip, plan)

    assert len(problem.activities) >= 3
    assert all(item.source_refs for item in problem.activities)
    assert all(item.estimated and item.source_ref for item in problem.travel_times)
    assert any(item.title == "故宫博物院" for item in solved.scheduled)
    assert verified.status == WorkflowStatus.READY
