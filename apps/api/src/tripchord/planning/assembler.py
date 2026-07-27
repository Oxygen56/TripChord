from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from pydantic import TypeAdapter

from tripchord.domain.common import DomainModel
from tripchord.domain.travel_data import Place
from tripchord.domain.trip import TripSpec
from tripchord.planning.candidates import ActivityCandidateBuilder
from tripchord.planning.problem import PlanningProblem, TravelTime


class PlaceCatalogEntry(DomainModel):
    city: str
    place: Place


class ReplayPlaceCatalog:
    def __init__(self, path: Path) -> None:
        self._entries = TypeAdapter(tuple[PlaceCatalogEntry, ...]).validate_json(path.read_text())

    def search(self, city: str, terms: tuple[str, ...]) -> tuple[Place, ...]:
        city_places = tuple(entry.place for entry in self._entries if entry.city == city)
        if not terms:
            return city_places
        matched = tuple(
            place
            for place in city_places
            if any(
                term in f"{place.name} {' '.join(place.tags)}"
                for term in terms
            )
        )
        return matched or city_places


class PlanningProblemAssembler:
    def __init__(self, catalog: ReplayPlaceCatalog, *, candidate_limit: int = 6) -> None:
        self._catalog = catalog
        self._candidate_limit = candidate_limit

    def assemble(self, trip: TripSpec) -> PlanningProblem:
        terms = (*trip.must_visit, *trip.interests)
        places = self._catalog.search(trip.destinations[0], terms)
        candidates = ActivityCandidateBuilder().build(trip, places)
        ranked = tuple(
            sorted(candidates, key=lambda item: (-item.must_visit, -item.utility, item.id))[
                : self._candidate_limit
            ]
        )
        if not ranked:
            raise ValueError(f"replay catalog has no candidates for {trip.destinations[0]}")
        by_id = {place.id: place for place in places}
        travel_times: list[TravelTime] = []
        for origin in ranked:
            for destination in ranked:
                if origin.id == destination.id:
                    continue
                distance_km = self._haversine_km(
                    by_id[origin.id].coordinates.latitude,
                    by_id[origin.id].coordinates.longitude,
                    by_id[destination.id].coordinates.latitude,
                    by_id[destination.id].coordinates.longitude,
                )
                travel_times.append(
                    TravelTime(
                        origin_id=origin.id,
                        destination_id=destination.id,
                        minutes=max(15, round(distance_km / 15 * 60 + 10)),
                        source_ref="tripchord-replay:synthetic-transit-estimate",
                        estimated=True,
                    )
                )
        return PlanningProblem(
            trip=trip,
            activities=ranked,
            travel_times=tuple(travel_times),
        )

    def _haversine_km(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        earth_radius_km = 6371.0
        delta_lat = radians(lat2 - lat1)
        delta_lon = radians(lon2 - lon1)
        value = (
            sin(delta_lat / 2) ** 2
            + cos(radians(lat1)) * cos(radians(lat2)) * sin(delta_lon / 2) ** 2
        )
        return 2 * earth_radius_km * asin(sqrt(value))
