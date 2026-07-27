from __future__ import annotations

from datetime import time, timedelta
from decimal import Decimal

from tripchord.domain.travel_data import Place
from tripchord.domain.trip import Pace, TripSpec
from tripchord.planning.problem import ActivityAvailability, ActivityCandidate


def minute_of_day(value: time) -> int:
    return value.hour * 60 + value.minute


class ActivityCandidateBuilder:
    def build(self, trip: TripSpec, places: tuple[Place, ...]) -> tuple[ActivityCandidate, ...]:
        candidates: list[ActivityCandidate] = []
        for place in places:
            if self._matches(place, trip.avoid):
                continue
            must_visit = self._matches(place, trip.must_visit)
            utility = 100
            utility += sum(
                40
                for interest in trip.interests
                if interest in place.tags or interest in place.name
            )
            if place.rating is not None:
                utility += round(place.rating * 10)
            if must_visit:
                utility += 500
            duration = (
                place.expected_visit_minutes
                or {
                    Pace.RELAXED: 150,
                    Pace.BALANCED: 120,
                    Pace.INTENSIVE: 90,
                }[trip.pace]
            )
            cost_cents = 0
            if place.estimated_cost is not None and place.estimated_cost.currency == "CNY":
                cost_cents = int(place.estimated_cost.amount * Decimal("100"))
            candidates.append(
                ActivityCandidate(
                    id=place.id,
                    title=place.name,
                    duration_minutes=duration,
                    cost_cents=cost_cents,
                    utility=utility,
                    must_visit=must_visit,
                    availability=self._availability(trip, place),
                    source_refs=(f"{place.source.provider}:{place.source.request_id or place.id}",),
                    location_name=place.address or place.name,
                )
            )
        return tuple(candidates)

    def _availability(self, trip: TripSpec, place: Place) -> tuple[ActivityAvailability, ...]:
        result: list[ActivityAvailability] = []
        current = trip.start_date
        while current <= trip.end_date:
            windows = [
                window for window in place.opening_windows if window.weekday == current.weekday()
            ]
            if windows:
                for window in windows:
                    result.append(
                        ActivityAvailability(
                            date=current,
                            start_minute=max(
                                minute_of_day(window.opens_at),
                                minute_of_day(trip.daily_window.start),
                            ),
                            end_minute=min(
                                minute_of_day(window.closes_at),
                                minute_of_day(trip.daily_window.end),
                            ),
                        )
                    )
            else:
                result.append(
                    ActivityAvailability(
                        date=current,
                        start_minute=minute_of_day(trip.daily_window.start),
                        end_minute=minute_of_day(trip.daily_window.end),
                    )
                )
            current += timedelta(days=1)
        return tuple(
            window
            for window in result
            if window.end_minute - window.start_minute >= (place.expected_visit_minutes or 1)
        )

    def _matches(self, place: Place, terms: tuple[str, ...]) -> bool:
        haystack = f"{place.name} {' '.join(place.tags)}"
        return any(term in haystack for term in terms)
