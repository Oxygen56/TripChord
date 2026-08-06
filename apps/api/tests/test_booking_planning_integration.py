"""v0.6 integration: booking protection consumed by the planning pipeline.

The verifier and reverifier must consult the same booking ledger so a
protected (booked) component is never silently removed or modified across
candidate generation, repair, reverification or replan.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from tripchord.domain.common import Money
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion, ViolationCode
from tripchord.domain.trip import TripSpec
from tripchord.planning.repair import diff_plans
from tripchord.planning.reverification import DeclarativePlanReVerifier, PlanInvariantCode
from tripchord.planning.verifier import PlanVerifier, VerificationContext
from tripchord.platform.booking import BookingLedger
from tripchord.platform.booking_gate import BookingService

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _item(item_id: str, kind: ItemKind = ItemKind.ACTIVITY) -> ItineraryItem:
    return ItineraryItem(
        id=item_id,
        kind=kind,
        title=item_id,
        starts_at=datetime(2026, 10, 2, 9, tzinfo=SHANGHAI),
        ends_at=datetime(2026, 10, 2, 10, tzinfo=SHANGHAI),
        cost=Money(amount=Decimal("100"), currency="CNY"),
        source_refs=(f"src-{item_id}",),
    )


def _spec() -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 4),
        budget=Money(amount=Decimal("10000"), currency="CNY"),
    )


def _ledger_with_protected(component_id: str = "hotel-1") -> BookingLedger:
    service = BookingService(BookingLedger(plan_version="plan-v1"), now=NOW)
    ledger, _ = service.acknowledge_component(
        plan_version="plan-v1",
        component_id=component_id,
        checklist_id="checklist-1",
        acknowledgement_id="ack-1",
        user_token_sha256="a" * 64,
    )
    return ledger


def test_verifier_flags_dropped_protected_component() -> None:
    plan = PlanVersion(
        id="plan-2",
        trip_id="trip-1",
        version=2,
        items=(_item("hotel-1", ItemKind.LODGING), _item("activity-1")),
    )
    missing_hotel = plan.model_copy(
        update={"items": (_item("activity-1"),), "id": "plan-3", "version": 3}
    )
    context = VerificationContext(booking_ledger=_ledger_with_protected())
    violations = PlanVerifier().verify(_spec(), missing_hotel, context)
    assert any(violation.code is ViolationCode.MISSING_PROVENANCE for violation in violations)


def test_verifier_allows_protected_component_present() -> None:
    plan = PlanVersion(
        id="plan-2",
        trip_id="trip-1",
        version=2,
        items=(_item("hotel-1", ItemKind.LODGING), _item("activity-1")),
    )
    context = VerificationContext(booking_ledger=_ledger_with_protected())
    violations = PlanVerifier().verify(_spec(), plan, context)
    assert not any(violation.code is ViolationCode.MISSING_PROVENANCE for violation in violations)


def test_reverifier_blocks_removal_of_protected_component() -> None:
    before = PlanVersion(
        id="plan-2",
        trip_id="trip-1",
        version=2,
        items=(_item("hotel-1", ItemKind.LODGING), _item("activity-1")),
    )
    after = before.model_copy(
        update={
            "id": "plan-3",
            "version": 3,
            "items": (_item("activity-1"),),
            "parent_version_id": before.id,
        }
    )
    diff = diff_plans(before, after)
    context = VerificationContext(booking_ledger=_ledger_with_protected())
    report = DeclarativePlanReVerifier().verify(_spec(), before, after, diff, context)
    check = next(
        c for c in report.checks if c.code is PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is False
    assert not report.passed


def test_reverifier_blocks_change_of_protected_component() -> None:
    before = PlanVersion(
        id="plan-2",
        trip_id="trip-1",
        version=2,
        items=(_item("hotel-1", ItemKind.LODGING), _item("activity-1")),
    )
    changed_hotel = _item("hotel-1", ItemKind.LODGING).model_copy(
        update={"title": "renamed-hotel"}
    )
    after = before.model_copy(
        update={
            "id": "plan-3",
            "version": 3,
            "items": (changed_hotel, _item("activity-1")),
            "parent_version_id": before.id,
        }
    )
    diff = diff_plans(before, after)
    context = VerificationContext(booking_ledger=_ledger_with_protected())
    report = DeclarativePlanReVerifier().verify(_spec(), before, after, diff, context)
    check = next(
        c for c in report.checks if c.code is PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is False


def test_reverifier_passes_when_protected_component_preserved() -> None:
    before = PlanVersion(
        id="plan-2",
        trip_id="trip-1",
        version=2,
        items=(_item("hotel-1", ItemKind.LODGING), _item("activity-1")),
    )
    after = before.model_copy(
        update={
            "id": "plan-3",
            "version": 3,
            "items": (_item("hotel-1", ItemKind.LODGING), _item("activity-2")),
            "parent_version_id": before.id,
        }
    )
    diff = diff_plans(before, after)
    context = VerificationContext(booking_ledger=_ledger_with_protected())
    report = DeclarativePlanReVerifier().verify(_spec(), before, after, diff, context)
    check = next(
        c for c in report.checks if c.code is PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is True
    assert report.passed
