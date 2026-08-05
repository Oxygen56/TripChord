from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripchord.domain.common import Money
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion, ViolationCode
from tripchord.domain.trip import TripSpec
from tripchord.planning.repair import RepairStrategy
from tripchord.planning.verifier import TravelRequirement, VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowStatus

ZONE = ZoneInfo("Asia/Shanghai")


def trip(*, budget: str = "1000", must_visit: tuple[str, ...] = ()) -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 2),
        budget=Money(amount=Decimal(budget), currency="CNY"),
        must_visit=must_visit,
    )


def activity(
    item_id: str,
    start_hour: int,
    end_hour: int,
    *,
    cost: str = "0",
    utility: int = 100,
) -> ItineraryItem:
    return ItineraryItem(
        id=item_id,
        kind=ItemKind.ACTIVITY,
        title=item_id,
        starts_at=datetime(2026, 10, 2, start_hour, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 2, end_hour, tzinfo=ZONE),
        cost=Money(amount=Decimal(cost), currency="CNY"),
        utility=utility,
    )


def test_workflow_repairs_travel_gap_and_emits_diff() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(activity("a", 9, 10), activity("b", 10, 11)),
    )
    context = VerificationContext(
        travel_requirements=(
            TravelRequirement(from_item_id="a", to_item_id="b", minimum_minutes=30),
        )
    )

    result = PlanningWorkflow().run(trip(), plan, context)

    assert result.status == WorkflowStatus.READY
    repaired = next(item for item in result.final_plan.items if item.id == "b")
    assert (repaired.starts_at.hour, repaired.starts_at.minute) == (10, 30)
    assert result.traces[0].diff.changed_items[0].item_id == "b"
    assert result.traces[0].actions[0].violation_code == ViolationCode.TRAVEL_GAP
    assert result.traces[0].repair_plan.strategy == RepairStrategy.IN_PLACE_REPAIR
    assert result.traces[0].repair_plan.steps[-1].success_invariant.startswith(
        "确定性 Verifier"
    )
    assert result.traces[0].reverification.engine == "declarative-plan-invariants-v1"
    assert result.traces[0].reverification.passed
    assert result.final_reverification == result.traces[0].reverification


def test_workflow_repairs_budget_by_removing_lowest_utility_optional_item() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(
            activity("keep", 9, 10, cost="80", utility=500),
            activity("remove", 11, 12, cost="40", utility=10),
        ),
    )

    result = PlanningWorkflow().run(trip(budget="100"), plan)

    assert result.status == WorkflowStatus.READY
    assert [item.id for item in result.final_plan.items] == ["keep"]
    assert result.traces[0].diff.removed_item_ids == ("remove",)


def test_workflow_blocks_when_repair_would_require_invented_source() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(
            ItineraryItem(
                id="rail",
                kind=ItemKind.TRANSPORT,
                title="无来源高铁",
                starts_at=datetime(2026, 10, 2, 9, tzinfo=ZONE),
                ends_at=datetime(2026, 10, 2, 11, tzinfo=ZONE),
            ),
        ),
    )

    result = PlanningWorkflow().run(trip(), plan)

    assert result.status == WorkflowStatus.BLOCKED
    assert result.traces == ()
    assert result.remaining_violations[0].code == ViolationCode.MISSING_PROVENANCE


def test_verifier_blocks_missing_must_visit_item() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(activity("普通景点", 9, 10),),
    )

    result = PlanningWorkflow().run(trip(must_visit=("故宫",)), plan)

    assert result.status == WorkflowStatus.BLOCKED
    assert result.remaining_violations[0].code == ViolationCode.MUST_VISIT_MISSING
