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
    children_ages: tuple[int, ...] = ()
    includes_elderly: bool = False
    accessibility_needs: tuple[str, ...] = ()


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

