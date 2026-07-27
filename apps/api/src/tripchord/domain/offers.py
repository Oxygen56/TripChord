from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, HttpUrl, field_validator, model_validator

from tripchord.domain.common import DomainModel, Money


class OfferKind(StrEnum):
    FLIGHT = "flight"
    RAIL = "rail"
    LODGING = "lodging"
    ACTIVITY = "activity"


class SourceMode(StrEnum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"
    REPLAY = "replay"
    USER_SNAPSHOT = "user_snapshot"


class PriceState(StrEnum):
    ESTIMATED = "estimated"
    LIVE_SEARCH = "live_search"
    REVALIDATED = "revalidated"
    BOOKED = "booked"


class OfferSource(DomainModel):
    provider: str = Field(min_length=1)
    mode: SourceMode
    request_id: str | None = None
    captured_at: datetime
    expires_at: datetime | None = None

    @field_validator("captured_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("price timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_expiry(self) -> "OfferSource":
        if self.expires_at is not None and self.expires_at <= self.captured_at:
            raise ValueError("expires_at must be after captured_at")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.expires_at is None or reference < self.expires_at


class PriceBreakdown(DomainModel):
    base: Money
    taxes: Money | None = None
    fees: Money | None = None
    total: Money
    components_complete: bool = False

    @model_validator(mode="after")
    def validate_currency_and_sum(self) -> "PriceBreakdown":
        components = [self.base, self.taxes, self.fees]
        currencies = {part.currency for part in components if part is not None}
        currencies.add(self.total.currency)
        if len(currencies) != 1:
            raise ValueError("all price components must share one currency")
        if self.components_complete:
            expected = sum(
                (part.amount for part in components if part is not None),
                start=Decimal("0"),
            )
            if expected != self.total.amount:
                raise ValueError("complete price components must sum to total")
        return self


class OfferTerms(DomainModel):
    refundable: bool | None = None
    cancellation_summary: str | None = None
    baggage_summary: str | None = None
    meal_summary: str | None = None
    eligibility: str | None = None


class TravelOffer(DomainModel):
    id: str = Field(min_length=1)
    kind: OfferKind
    title: str = Field(min_length=1)
    source: OfferSource
    price_state: PriceState
    price: PriceBreakdown
    terms: OfferTerms = Field(default_factory=OfferTerms)
    comparison_key: str = Field(
        min_length=1,
        description="Canonical product, occupancy, entitlement, and policy identity.",
    )
    revalidation_token: str | None = None
    booking_url: HttpUrl | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_truth_labels(self) -> "TravelOffer":
        if self.price_state == PriceState.LIVE_SEARCH and self.source.mode != SourceMode.PRODUCTION:
            raise ValueError("only production sources can be labelled live_search")
        if self.price_state == PriceState.REVALIDATED and self.source.mode != SourceMode.PRODUCTION:
            raise ValueError("only production sources can be labelled revalidated")
        if self.price_state == PriceState.BOOKED and self.source.mode == SourceMode.SANDBOX:
            raise ValueError("sandbox offers cannot be labelled booked")
        return self

    @property
    def requires_revalidation(self) -> bool:
        return self.price_state not in {PriceState.REVALIDATED, PriceState.BOOKED}

