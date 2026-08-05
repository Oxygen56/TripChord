from datetime import date, datetime
from zoneinfo import ZoneInfo

from tripchord.domain.common import Money
from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.impact import DependencyKind, PlanDependency
from tripchord.planning.repair import RepairStrategy
from tripchord.planning.replanner import LocalReplanner, ReplanStatus

ZONE = ZoneInfo("Asia/Shanghai")


def spec(*, budget: str | None = None) -> TripSpec:
    data: dict[str, object] = {
        "origin": "上海",
        "destinations": ("北京",),
        "start_date": date(2026, 10, 2),
        "end_date": date(2026, 10, 2),
    }
    if budget is not None:
        data["budget"] = {"amount": budget, "currency": "CNY"}
    return TripSpec.model_validate(data)


def item(
    item_id: str,
    start_hour: int,
    end_hour: int,
    *,
    utility: int = 100,
    locked: bool = False,
) -> ItineraryItem:
    return ItineraryItem(
        id=item_id,
        kind=ItemKind.ACTIVITY,
        title=item_id,
        starts_at=datetime(2026, 10, 2, start_hour, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 2, end_hour, tzinfo=ZONE),
        utility=utility,
        locked=locked,
    )


def event(kind: EventKind, target: str, **payload: str | int) -> PlanEvent:
    return PlanEvent(
        id=f"event-{kind}",
        trip_id="trip-1",
        kind=kind,
        occurred_at=datetime(2026, 10, 1, 8, tzinfo=ZONE),
        target_refs=(target,),
        payload=payload,
    )


def test_delay_changes_only_direct_and_downstream_items() -> None:
    before = item("breakfast", 9, 10)
    transport = item("train", 10, 11)
    after = item("museum", 12, 13)
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(before, transport, after),
    )
    dependencies = (
        PlanDependency(
            upstream_item_id="train",
            downstream_item_id="museum",
            kind=DependencyKind.TEMPORAL,
        ),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(EventKind.TRANSPORT_DELAYED, "train", delay_minutes=30),
        dependencies=dependencies,
    )

    assert result.status == ReplanStatus.READY
    assert result.impact.affected_item_ids == ("museum", "train")
    assert result.unaffected_preservation_ratio == 1.0
    assert result.overall_preservation_ratio == 1 / 3
    assert next(entry for entry in result.final_plan.items if entry.id == "breakfast") == before
    museum = next(entry for entry in result.final_plan.items if entry.id == "museum")
    assert museum.starts_at.minute == 30
    assert result.repair_plan.strategy == RepairStrategy.IN_PLACE_REPAIR
    assert result.repair_plan.direct_item_ids == ("train",)
    assert result.repair_plan.cascade_item_ids == ("museum",)


def test_sold_out_item_can_be_replaced_locally() -> None:
    original = item("museum-a", 9, 10)
    replacement = item("museum-b", 9, 10)
    rest = item("park", 11, 12)
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(original, rest),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(EventKind.SOLD_OUT, "museum-a"),
        dependencies=(),
        replacements={"museum-a": replacement},
    )

    assert result.status == ReplanStatus.READY
    assert result.diff.removed_item_ids == ("museum-a",)
    assert result.diff.added_item_ids == ("museum-b",)
    assert result.impact.unaffected_item_ids == ("park",)
    assert result.unaffected_preservation_ratio == 1.0


def test_price_change_can_trigger_scoped_budget_repair() -> None:
    cheap = item("cheap", 9, 10, utility=10).model_copy(
        update={"cost": Money(amount="20", currency="CNY")}
    )
    fixed = item("fixed", 11, 12, utility=500).model_copy(
        update={"cost": Money(amount="80", currency="CNY")}
    )
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(cheap, fixed),
    )

    result = LocalReplanner().replan(
        spec(budget="150"),
        plan,
        event(EventKind.PRICE_CHANGED, "cheap", new_amount="100", currency="CNY"),
        dependencies=(),
    )

    assert result.status == ReplanStatus.READY
    assert result.diff.removed_item_ids == ("cheap",)
    assert next(entry for entry in result.final_plan.items if entry.id == "fixed") == fixed


def test_locked_direct_target_blocks_automatic_replan() -> None:
    locked = item("booked", 9, 10, locked=True)
    plan = PlanVersion(id="trip-1:plan:v1", trip_id="trip-1", version=1, items=(locked,))

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(EventKind.PLACE_CLOSED, "booked"),
    )

    assert result.status == ReplanStatus.BLOCKED
    assert result.final_plan == plan
    assert result.repair_plan.strategy == RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL
    assert result.repair_plan.candidate_pool_expansion_required
    assert result.repair_plan.requested_candidate_count == 5


def test_unmatched_event_is_an_auditable_noop() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(item("museum", 9, 10),),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(EventKind.WEATHER_ALERT, "unknown"),
    )

    assert result.status == ReplanStatus.NO_EFFECT
    assert not result.diff.changed
    assert result.overall_preservation_ratio == 1.0


def test_sold_out_transport_requires_a_sourced_replacement() -> None:
    transport = item("train", 9, 10).model_copy(update={"kind": ItemKind.TRANSPORT})
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(transport,),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(EventKind.SOLD_OUT, "train"),
    )

    assert result.status == ReplanStatus.BLOCKED
    assert result.final_plan == plan


def test_global_user_budget_change_reverifies_the_whole_plan() -> None:
    low_value = item("optional", 9, 10, utility=10).model_copy(
        update={"cost": Money(amount="80", currency="CNY")}
    )
    high_value = item("essential", 11, 12, utility=500).model_copy(
        update={"cost": Money(amount="80", currency="CNY")}
    )
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(low_value, high_value),
    )
    changed = PlanEvent(
        id="event-budget",
        trip_id="trip-1",
        kind=EventKind.USER_CHANGED_REQUIREMENT,
        occurred_at=datetime(2026, 10, 1, 8, tzinfo=ZONE),
    )

    result = LocalReplanner().replan(spec(budget="100"), plan, changed)

    assert result.status == ReplanStatus.READY
    assert result.impact.direct_item_ids == ("essential", "optional")
    assert result.diff.removed_item_ids == ("optional",)


def test_replaying_the_same_delay_event_is_idempotent() -> None:
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(item("train", 10, 11),),
    )
    delayed = event(EventKind.TRANSPORT_DELAYED, "train", delay_minutes=30)

    first = LocalReplanner().replan(spec(), plan, delayed, dependencies=())
    second = LocalReplanner().replan(spec(), first.final_plan, delayed, dependencies=())

    assert first.status == ReplanStatus.READY
    assert second.status == ReplanStatus.NO_EFFECT
    assert second.final_plan == first.final_plan
    assert not second.diff.changed


def test_same_price_event_is_a_noop_and_does_not_create_a_plan_version() -> None:
    priced = item("hotel", 9, 10).model_copy(
        update={"cost": Money(amount="100", currency="CNY")}
    )
    plan = PlanVersion(
        id="trip-1:plan:v1",
        trip_id="trip-1",
        version=1,
        items=(priced,),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(
            EventKind.PRICE_CHANGED,
            "hotel",
            old_amount="100",
            new_amount="100",
            currency="CNY",
        ),
        dependencies=(),
    )

    assert result.status == ReplanStatus.NO_EFFECT
    assert result.final_plan == plan
    assert not result.diff.changed
    assert result.repair_plan.strategy == RepairStrategy.NO_ACTION


def test_stale_old_price_blocks_event_instead_of_overwriting_newer_state() -> None:
    priced = item("hotel", 9, 10).model_copy(
        update={"cost": Money(amount="120", currency="CNY")}
    )
    plan = PlanVersion(
        id="trip-1:plan:v2",
        trip_id="trip-1",
        version=2,
        items=(priced,),
    )

    result = LocalReplanner().replan(
        spec(),
        plan,
        event(
            EventKind.PRICE_CHANGED,
            "hotel",
            old_amount="100",
            new_amount="130",
            currency="CNY",
        ),
        dependencies=(),
    )

    assert result.status == ReplanStatus.BLOCKED
    assert result.final_plan == plan
    assert "stale" in result.message
    assert result.repair_plan.strategy == RepairStrategy.HUMAN_BLOCK
