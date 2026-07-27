from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from tripchord.domain.common import DomainModel


class EventKind(StrEnum):
    PRICE_CHANGED = "price_changed"
    SOLD_OUT = "sold_out"
    WEATHER_ALERT = "weather_alert"
    PLACE_CLOSED = "place_closed"
    TRANSPORT_DELAYED = "transport_delayed"
    USER_CHANGED_REQUIREMENT = "user_changed_requirement"


class PlanEvent(DomainModel):
    id: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    kind: EventKind
    occurred_at: datetime
    target_refs: tuple[str, ...] = ()
    payload: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        return value

