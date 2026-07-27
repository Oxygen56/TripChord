from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from tripchord.domain.common import Money
from tripchord.domain.offers import OfferKind, OfferTerms, PriceBreakdown, TravelOffer
from tripchord.providers import OfferSearchQuery, ProviderRegistry, ReplayOfferProvider
from tripchord.providers.base import ProviderError
from tripchord.providers.comparison import group_comparable_offers
from tripchord.providers.user_snapshot import UserQuoteInput, import_user_quote

ROOT = Path(__file__).resolve().parents[3]


class FailingProvider:
    name = "failing"
    supported_kinds = frozenset({OfferKind.FLIGHT})

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        raise ProviderError(self.name, "rate_limited", "try later", retryable=True)

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        raise ProviderError(self.name, "unavailable", "not available")


@pytest.mark.asyncio
async def test_registry_searches_replay_provider() -> None:
    provider = ReplayOfferProvider(ROOT / "data" / "replay" / "offers.json")
    registry = ProviderRegistry([provider])
    result = await registry.search(
        OfferSearchQuery(
            kind=OfferKind.FLIGHT,
            origin="上海",
            destination="北京",
            start_date=date(2026, 10, 1),
        )
    )

    assert len(result.offers) == 1
    assert result.offers[0].source.mode == "replay"
    assert result.failures == ()


@pytest.mark.asyncio
async def test_registry_isolates_provider_failure() -> None:
    provider = ReplayOfferProvider(ROOT / "data" / "replay" / "offers.json")
    registry = ProviderRegistry([provider, FailingProvider()])

    result = await registry.search(
        OfferSearchQuery(
            kind=OfferKind.FLIGHT,
            origin="上海",
            destination="北京",
            start_date=date(2026, 10, 1),
        )
    )

    assert len(result.offers) == 1
    assert result.failures[0].provider == "failing"
    assert result.failures[0].retryable is True


def test_user_quote_requires_confirmation() -> None:
    quote = UserQuoteInput(
        id="user:hotel-1",
        provider_label="示例平台",
        kind=OfferKind.LODGING,
        title="示例酒店",
        captured_at=datetime.now(UTC),
        price=PriceBreakdown(
            base=Money(amount=Decimal("500"), currency="CNY"),
            total=Money(amount=Decimal("500"), currency="CNY"),
            components_complete=True,
        ),
        terms=OfferTerms(refundable=True),
        comparison_key="same-room-same-terms",
        destination="北京",
    )

    with pytest.raises(ValueError, match="confirm"):
        import_user_quote(quote)


def test_comparison_only_groups_equivalent_context() -> None:
    base = UserQuoteInput(
        id="user:hotel-1",
        provider_label="平台甲",
        kind=OfferKind.LODGING,
        title="同一房型",
        captured_at=datetime.now(UTC),
        price=PriceBreakdown(
            base=Money(amount=Decimal("500"), currency="CNY"),
            total=Money(amount=Decimal("500"), currency="CNY"),
            components_complete=True,
        ),
        comparison_key="same-room-same-terms",
        destination="北京",
        user_confirmed=True,
    )
    cheaper = base.model_copy(
        update={
            "id": "user:hotel-2",
            "provider_label": "平台乙",
            "price": PriceBreakdown(
                base=Money(amount=Decimal("480"), currency="CNY"),
                total=Money(amount=Decimal("480"), currency="CNY"),
                components_complete=True,
            ),
        }
    )
    different_terms = base.model_copy(
        update={"id": "user:hotel-3", "comparison_key": "same-room-non-refundable"}
    )
    groups = group_comparable_offers(
        (
            import_user_quote(base),
            import_user_quote(cheaper),
            import_user_quote(different_terms),
        )
    )

    assert len(groups) == 2
    same = next(group for group in groups if group.comparison_key == "same-room-same-terms")
    assert [offer.id for offer in same.offers] == ["user:hotel-2", "user:hotel-1"]
