from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.repair import PlanDiff
from tripchord.planning.reverification import (
    DeclarativePlanReVerifier,
    PlanInvariantCode,
)
from tripchord.planning.verifier import TravelRequirement, VerificationContext

ZONE = ZoneInfo("Asia/Shanghai")


def item(item_id: str, start_hour: int, end_hour: int) -> ItineraryItem:
    return ItineraryItem(
        id=item_id,
        kind=ItemKind.ACTIVITY,
        title=item_id,
        starts_at=datetime(2026, 10, 2, start_hour, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 2, end_hour, tzinfo=ZONE),
    )


def trip() -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 2),
    )


def test_declarative_reverifier_catches_duplicate_ids_base_verifier_does_not_model() -> None:
    original = item("museum", 9, 10)
    before = PlanVersion(
        id="trip:plan:v1",
        trip_id="trip",
        version=1,
        items=(original,),
    )
    after = PlanVersion(
        id="trip:plan:v2",
        trip_id="trip",
        version=2,
        parent_version_id=before.id,
        items=(original, original),
    )

    report = DeclarativePlanReVerifier().verify(
        trip(),
        before,
        after,
        PlanDiff(),
    )

    assert report.engine == "declarative-plan-invariants-v1"
    assert not report.passed
    assert PlanInvariantCode.UNIQUE_ITEM_IDS in report.failed_codes
    assert PlanInvariantCode.VERSION_LINEAGE in report.failed_codes


def test_declarative_reverifier_accepts_exact_declared_local_change() -> None:
    first = item("museum", 9, 10)
    second = item("park", 11, 12)
    before = PlanVersion(
        id="trip:plan:v1",
        trip_id="trip",
        version=1,
        items=(first, second),
    )
    shifted = second.model_copy(
        update={
            "starts_at": datetime(2026, 10, 2, 12, tzinfo=ZONE),
            "ends_at": datetime(2026, 10, 2, 13, tzinfo=ZONE),
        }
    )
    after = PlanVersion(
        id="trip:plan:v2",
        trip_id="trip",
        version=2,
        parent_version_id=before.id,
        items=(first, shifted),
    )
    diff = PlanDiff(
        changed_items=(
            {
                "item_id": "park",
                "changed_fields": ("ends_at", "starts_at"),
            },
        )
    )

    report = DeclarativePlanReVerifier().verify(trip(), before, after, diff)

    assert report.passed
    assert all(check.passed for check in report.checks)


def test_declarative_reverifier_catches_overlap_across_midnight() -> None:
    overnight = ItineraryItem(
        id="overnight-transfer",
        kind=ItemKind.TRANSPORT,
        title="overnight-transfer",
        starts_at=datetime(2026, 10, 2, 23, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 3, 1, tzinfo=ZONE),
        source_refs=("provider:overnight-transfer",),
    )
    original_activity = ItineraryItem(
        id="arrival-activity",
        kind=ItemKind.ACTIVITY,
        title="arrival-activity",
        starts_at=datetime(2026, 10, 3, 2, tzinfo=ZONE),
        ends_at=datetime(2026, 10, 3, 3, tzinfo=ZONE),
    )
    overlapping_activity = original_activity.model_copy(
        update={
            "starts_at": datetime(2026, 10, 3, 0, 30, tzinfo=ZONE),
            "ends_at": datetime(2026, 10, 3, 1, 30, tzinfo=ZONE),
        }
    )
    before = PlanVersion(
        id="trip:plan:v1",
        trip_id="trip",
        version=1,
        items=(overnight, original_activity),
    )
    after = PlanVersion(
        id="trip:plan:v2",
        trip_id="trip",
        version=2,
        parent_version_id=before.id,
        items=(overnight, overlapping_activity),
    )
    diff = PlanDiff(
        changed_items=(
            {
                "item_id": "arrival-activity",
                "changed_fields": ("ends_at", "starts_at"),
            },
        )
    )

    report = DeclarativePlanReVerifier().verify(trip(), before, after, diff)

    assert PlanInvariantCode.NO_TEMPORAL_OVERLAP in report.failed_codes


def test_declarative_reverifier_rejects_dangling_travel_requirement() -> None:
    plan = PlanVersion(
        id="trip:plan:v1",
        trip_id="trip",
        version=1,
        items=(item("museum", 9, 10),),
    )
    context = VerificationContext(
        travel_requirements=(
            TravelRequirement(
                from_item_id="museum",
                to_item_id="missing-transfer-target",
                minimum_minutes=30,
            ),
        )
    )

    report = DeclarativePlanReVerifier().verify(
        trip(),
        plan,
        plan,
        PlanDiff(),
        context,
    )

    assert PlanInvariantCode.TRAVEL_GAPS in report.failed_codes
    travel_check = next(
        check for check in report.checks if check.code == PlanInvariantCode.TRAVEL_GAPS
    )
    assert travel_check.item_ids == ("missing-transfer-target",)
