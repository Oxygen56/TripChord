from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import DomainModel, Money


class ItemKind(StrEnum):
    TRANSPORT = "transport"
    LODGING = "lodging"
    ACTIVITY = "activity"
    MEAL = "meal"
    BUFFER = "buffer"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    VERIFYING = "verifying"
    REPAIRING = "repairing"
    READY = "ready"
    CONFIRMED = "confirmed"


class ItineraryItem(DomainModel):
    id: str = Field(min_length=1)
    kind: ItemKind
    title: str = Field(min_length=1)
    starts_at: datetime
    ends_at: datetime
    location_name: str | None = None
    cost: Money | None = None
    offer_id: str | None = None
    source_refs: tuple[str, ...] = ()
    utility: int = Field(default=0, ge=0, le=10000)
    locked: bool = False

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("itinerary timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "ItineraryItem":
        if self.ends_at <= self.starts_at:
            raise ValueError("itinerary item must have positive duration")
        return self


class ViolationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ViolationCode(StrEnum):
    DATE_OUT_OF_RANGE = "date_out_of_range"
    DAILY_WINDOW = "daily_window"
    OVERLAP = "overlap"
    BUDGET_EXCEEDED = "budget_exceeded"
    CURRENCY_MISMATCH = "currency_mismatch"
    MISSING_PROVENANCE = "missing_provenance"
    STALE_OR_UNVERIFIED_OFFER = "stale_or_unverified_offer"
    MUST_VISIT_MISSING = "must_visit_missing"
    TRAVEL_GAP = "travel_gap"


class Violation(DomainModel):
    code: ViolationCode
    severity: ViolationSeverity
    message: str
    item_ids: tuple[str, ...] = ()
    details: dict[str, str | int | float | bool] = Field(default_factory=dict)


class PlanVersion(DomainModel):
    id: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: PlanStatus = PlanStatus.DRAFT
    items: tuple[ItineraryItem, ...] = ()
    parent_version_id: str | None = None
    explanation: str | None = None
    applied_event_ids: tuple[str, ...] = ()
