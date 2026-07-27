from datetime import UTC, date, datetime, time
from decimal import Decimal

from tripchord.domain.common import Coordinates, Money
from tripchord.domain.source import SourceMode, SourceRecord
from tripchord.domain.travel_data import OpeningWindow, Place, PlaceKind
from tripchord.domain.trip import TripSpec
from tripchord.planning.candidates import ActivityCandidateBuilder


def place(name: str, tags: tuple[str, ...]) -> Place:
    return Place(
        id=f"place:{name}",
        name=name,
        kind=PlaceKind.ATTRACTION,
        coordinates=Coordinates(latitude=39.9, longitude=116.4),
        opening_windows=(
            OpeningWindow(weekday=4, opens_at=time(10), closes_at=time(16)),
        ),
        expected_visit_minutes=120,
        estimated_cost=Money(amount=Decimal("50"), currency="CNY"),
        rating=4.8,
        tags=tags,
        source=SourceRecord(
            provider="fixture",
            mode=SourceMode.REPLAY,
            captured_at=datetime.now(UTC),
        ),
    )


def test_candidate_builder_applies_preferences_opening_hours_and_avoid_list() -> None:
    trip = TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 2),
        interests=("历史",),
        must_visit=("历史博物馆",),
        avoid=("商业街",),
    )

    candidates = ActivityCandidateBuilder().build(
        trip,
        (
            place("历史博物馆", ("历史", "博物馆")),
            place("商业街", ("购物",)),
        ),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.must_visit
    assert candidate.cost_cents == 5000
    assert candidate.availability[0].start_minute == 10 * 60
    assert candidate.availability[0].end_minute == 16 * 60
    assert candidate.utility > 600

