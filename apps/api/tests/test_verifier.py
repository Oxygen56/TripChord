from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripchord.domain.common import Money
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion, ViolationCode
from tripchord.domain.offers import (
    OfferKind,
    OfferSource,
    PriceBreakdown,
    PriceState,
    TravelOffer,
)
from tripchord.domain.source import SourceMode
from tripchord.domain.trip import TripSpec
from tripchord.planning import PlanVerifier
from tripchord.planning.verifier import VerificationContext, VerificationMode

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


def test_confirmation_requires_offer_revalidation() -> None:
    plan = PlanVersion(
        id="plan-1",
        trip_id="trip-1",
        version=1,
        items=(
            ItineraryItem(
                id="flight",
                kind=ItemKind.TRANSPORT,
                title="航班",
                starts_at=datetime(2026, 10, 2, 8, tzinfo=SHANGHAI),
                ends_at=datetime(2026, 10, 2, 10, tzinfo=SHANGHAI),
                offer_id="offer-1",
            ),
        ),
    )
    offer = TravelOffer(
        id="offer-1",
        kind=OfferKind.FLIGHT,
        title="航班报价",
        source=OfferSource(
            provider="replay",
            mode=SourceMode.REPLAY,
            captured_at=datetime.now(UTC),
        ),
        price_state=PriceState.ESTIMATED,
        price=PriceBreakdown(
            base=Money(amount=Decimal("500"), currency="CNY"),
            total=Money(amount=Decimal("500"), currency="CNY"),
            components_complete=True,
        ),
        comparison_key="flight-1",
    )
    context = VerificationContext(mode=VerificationMode.CONFIRMATION, offers=(offer,))

    violations = PlanVerifier().verify(spec(), plan, context)

    assert {violation.code for violation in violations} == {ViolationCode.STALE_OR_UNVERIFIED_OFFER}
