from datetime import date, time
from enum import StrEnum

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel, Money


class Pace(StrEnum):
    RELAXED = "relaxed"
    BALANCED = "balanced"
    INTENSIVE = "intensive"


class TransportMode(StrEnum):
    FLIGHT = "flight"
    RAIL = "rail"
    TRANSIT = "transit"
    WALKING = "walking"
    TAXI = "taxi"
    DRIVING = "driving"


class TravelParty(DomainModel):
    adults: int = Field(default=1, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    rooms: int = Field(default=1, ge=1, le=8)
    includes_elderly: bool = False
    accessibility_needs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_children_ages(self) -> "TravelParty":
        """Never infer a booking party from a partial child-age list."""

        if self.children > 0 and len(self.children_ages) != self.children:
            raise ValueError(
                "children_ages must contain exactly one age for every child"
            )
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        return self

    @property
    def traveller_count(self) -> int:
        return self.adults + self.children + self.infants


class DailyWindow(DomainModel):
    start: time = time(9, 0)
    end: time = time(21, 0)

    @model_validator(mode="after")
    def validate_order(self) -> "DailyWindow":
        if self.end <= self.start:
            raise ValueError("daily end must be after start")
        return self


class TripSpec(DomainModel):
    origin: str = Field(min_length=1)
    destinations: tuple[str, ...] = Field(min_length=1)
    start_date: date
    end_date: date
    party: TravelParty = Field(default_factory=TravelParty)
    budget: Money | None = None
    pace: Pace = Pace.BALANCED
    max_main_activities_per_day: int = Field(default=3, ge=1, le=8)
    daily_window: DailyWindow = Field(default_factory=DailyWindow)
    preferred_transport: tuple[TransportMode, ...] = ()
    interests: tuple[str, ...] = ()
    must_visit: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    notes: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "TripSpec":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if (self.end_date - self.start_date).days > 60:
            raise ValueError("a single trip may not exceed 61 calendar days")
        return self

    @property
    def day_count(self) -> int:
        return (self.end_date - self.start_date).days + 1
