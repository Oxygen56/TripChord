from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from tripchord.domain.common import Money
from tripchord.domain.offers import (
    OfferKind,
    OfferSource,
    PriceBreakdown,
    PriceState,
    SourceMode,
    TravelOffer,
)


def price() -> PriceBreakdown:
    return PriceBreakdown(
        base=Money(amount=Decimal("100"), currency="cny"),
        taxes=Money(amount=Decimal("10"), currency="CNY"),
        fees=Money(amount=Decimal("5"), currency="CNY"),
        total=Money(amount=Decimal("115"), currency="CNY"),
        components_complete=True,
    )


def test_live_offer_requires_production_source() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError, match="production"):
        TravelOffer(
            id="hotel-1",
            kind=OfferKind.LODGING,
            title="Example hotel",
            source=OfferSource(provider="fixture", mode=SourceMode.REPLAY, captured_at=now),
            price_state=PriceState.LIVE_SEARCH,
            price=price(),
            comparison_key="hotel-1:room-1:refundable:no-breakfast",
        )


def test_offer_freshness_uses_expiry() -> None:
    now = datetime.now(UTC)
    source = OfferSource(
        provider="provider",
        mode=SourceMode.PRODUCTION,
        captured_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    assert source.is_fresh(now + timedelta(minutes=5))
    assert not source.is_fresh(now + timedelta(minutes=11))


def test_complete_components_must_sum_to_total() -> None:
    with pytest.raises(ValidationError, match="sum to total"):
        PriceBreakdown(
            base=Money(amount=Decimal("100"), currency="CNY"),
            taxes=Money(amount=Decimal("10"), currency="CNY"),
            total=Money(amount=Decimal("120"), currency="CNY"),
            components_complete=True,
        )

