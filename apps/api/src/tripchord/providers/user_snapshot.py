from datetime import datetime

from pydantic import Field, HttpUrl

from tripchord.domain.common import DomainModel
from tripchord.domain.offers import (
    OfferKind,
    OfferSource,
    OfferTerms,
    PriceBreakdown,
    PriceState,
    TravelOffer,
)
from tripchord.domain.source import SourceMode


class UserQuoteInput(DomainModel):
    id: str = Field(min_length=1)
    provider_label: str = Field(min_length=1)
    kind: OfferKind
    title: str = Field(min_length=1)
    captured_at: datetime
    price: PriceBreakdown
    comparison_key: str = Field(min_length=1)
    terms: OfferTerms = Field(default_factory=OfferTerms)
    original_url: HttpUrl | None = None
    destination: str
    user_confirmed: bool = False


def import_user_quote(quote: UserQuoteInput) -> TravelOffer:
    if not quote.user_confirmed:
        raise ValueError("the user must confirm extracted quote fields before import")
    return TravelOffer(
        id=quote.id,
        kind=quote.kind,
        title=quote.title,
        source=OfferSource(
            provider=f"user:{quote.provider_label}",
            mode=SourceMode.USER_SNAPSHOT,
            captured_at=quote.captured_at,
        ),
        price_state=PriceState.ESTIMATED,
        price=quote.price,
        terms=quote.terms,
        comparison_key=quote.comparison_key,
        booking_url=quote.original_url,
        metadata={"destination": quote.destination, "user_confirmed": True},
    )
