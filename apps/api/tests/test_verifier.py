from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripchord.domain.common import Money
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion, ViolationCode
from tripchord.domain.trip import TripSpec
from tripchord.planning import PlanVerifier

SHANGHAI = ZoneInfo("Asia/Shanghai")


def item(
    item_id: str,
    title: str,
    start_hour: int,
    end_hour: int,
    *,
    kind: ItemKind = ItemKind.ACTIVITY,
    cost: str | None = None,
    source_refs: tuple[str, ...] = (),
) -> ItineraryItem:
    return ItineraryItem(
        id=item_id,
        kind=kind,
        title=title,
        starts_at=datetime(2026, 10, 2, start_hour, tzinfo=SHANGHAI),
        ends_at=datetime(2026, 10, 2, end_hour, tzinfo=SHANGHAI),
        cost=Money(amount=Decimal(cost), currency="CNY") if cost else None,
        source_refs=source_refs,
    )


def spec() -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 4),
        budget=Money(amount=Decimal("1000"), currency="CNY"),
    )


def test_verifier_finds_overlap_budget_and_missing_source() -> None:
    plan = PlanVersion(
        id="plan-1",
        trip_id="trip-1",
        version=1,
        items=(
            item("rail", "高铁", 8, 12, kind=ItemKind.TRANSPORT, cost="800"),
            item("museum", "博物馆", 11, 14, cost="300"),
        ),
    )

    violations = PlanVerifier().verify(spec(), plan)
    codes = {violation.code for violation in violations}

    assert ViolationCode.OVERLAP in codes
    assert ViolationCode.BUDGET_EXCEEDED in codes
    assert ViolationCode.MISSING_PROVENANCE in codes


def test_verifier_accepts_feasible_plan() -> None:
    plan = PlanVersion(
        id="plan-1",
        trip_id="trip-1",
        version=1,
        items=(
            item(
                "rail",
                "高铁",
                8,
                12,
                kind=ItemKind.TRANSPORT,
                cost="500",
                source_refs=("offer:rail-1",),
            ),
            item("museum", "博物馆", 13, 16, cost="100"),
        ),
    )

    assert PlanVerifier().verify(spec(), plan) == ()

