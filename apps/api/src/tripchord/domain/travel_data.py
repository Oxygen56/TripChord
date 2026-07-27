from datetime import datetime, time
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import Coordinates, DomainModel, Money
from tripchord.domain.source import SourceRecord


class PlaceKind(StrEnum):
    ATTRACTION = "attraction"
    RESTAURANT = "restaurant"
    LODGING = "lodging"
    TRANSIT_STATION = "transit_station"
    AIRPORT = "airport"
    OTHER = "other"


class OpeningWindow(DomainModel):
    weekday: int = Field(ge=0, le=6)
    opens_at: time
    closes_at: time

    @model_validator(mode="after")
    def validate_window(self) -> "OpeningWindow":
        if self.closes_at <= self.opens_at:
            raise ValueError("overnight opening windows must be split by day")
        return self


class Place(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: PlaceKind
    coordinates: Coordinates
    address: str | None = None
    opening_windows: tuple[OpeningWindow, ...] = ()
    expected_visit_minutes: int | None = Field(default=None, gt=0)
    estimated_cost: Money | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    raw_opening_hours: str | None = None
    tags: tuple[str, ...] = ()
    source: SourceRecord
    provider_url: str | None = None


class RouteMode(StrEnum):
    WALKING = "walking"
    TRANSIT = "transit"
    DRIVING = "driving"
    TAXI = "taxi"
    RAIL = "rail"
    FLIGHT = "flight"


class RouteLeg(DomainModel):
    id: str = Field(min_length=1)
    origin: Coordinates
    destination: Coordinates
    mode: RouteMode
    departs_at: datetime | None = None
    arrives_at: datetime | None = None
    duration_minutes: int = Field(gt=0)
    distance_meters: int | None = Field(default=None, ge=0)
    estimated_cost: Money | None = None
    source: SourceRecord

    @field_validator("departs_at", "arrives_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("route timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_times(self) -> "RouteLeg":
        if (
            self.departs_at is not None
            and self.arrives_at is not None
            and self.arrives_at <= self.departs_at
        ):
            raise ValueError("route arrival must be after departure")
        return self


class WeatherKind(StrEnum):
    CLEAR = "clear"
    CLOUDY = "cloudy"
    RAIN = "rain"
    SNOW = "snow"
    EXTREME = "extreme"
    UNKNOWN = "unknown"


class WeatherWindow(DomainModel):
    location: Coordinates
    starts_at: datetime
    ends_at: datetime
    kind: WeatherKind
    temperature_low_c: float | None = None
    temperature_high_c: float | None = None
    precipitation_probability: float | None = Field(default=None, ge=0, le=1)
    alert: str | None = None
    source: SourceRecord

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_weather_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("weather timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_weather_window(self) -> "WeatherWindow":
        if self.ends_at <= self.starts_at:
            raise ValueError("weather window must have positive duration")
        return self
