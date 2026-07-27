from datetime import date

import httpx
import pytest
from tripchord.domain.offers import OfferKind, PriceState
from tripchord.domain.source import SourceMode
from tripchord.providers.base import OfferSearchQuery
from tripchord.providers.booking import BookingAccommodationProvider, BookingConfig


def product(total: str) -> dict[str, object]:
    return {
        "id": "product-1",
        "room": {"id": "room-1"},
        "price": {
            "base": {"booker_currency": "1000.00"},
            "total": {"booker_currency": total},
        },
        "policies": {
            "cancellation": [{"free_cancellation_until": "2026-09-29T18:00:00Z"}],
            "meal_plan": {"plan": "breakfast_included"},
        },
    }


def booking_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer token"
    assert request.headers["x-affiliate-id"] == "affiliate"
    if request.url.path.endswith("/accommodations/search"):
        assert b'"city":-2140479' in request.content
        return httpx.Response(
            200,
            json={
                "request_id": "search-1",
                "data": [
                    {
                        "id": 10004,
                        "currency": {"booker": "CNY", "accommodation": "CNY"},
                        "url": {"web": "https://www.booking.com/hotel/example"},
                        "products": [product("1100.00")],
                    }
                ],
            },
        )
    if request.url.path.endswith("/accommodations/availability"):
        return httpx.Response(
            200,
            json={
                "request_id": "availability-1",
                "data": [
                    {
                        "id": 10004,
                        "currency": {"booker": "CNY", "accommodation": "CNY"},
                        "url": {"web": "https://www.booking.com/hotel/example"},
                        "products": [product("1120.00")],
                    }
                ],
            },
        )
    raise AssertionError(f"unexpected request {request.url}")


@pytest.mark.asyncio
async def test_booking_search_and_availability_reprice() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(booking_handler),
        base_url="https://demandapi.booking.test/3.2",
    )
    provider = BookingAccommodationProvider(
        BookingConfig(
            api_token="token",
            affiliate_id="affiliate",
            base_url="https://demandapi.booking.test/3.2",
            source_mode=SourceMode.PRODUCTION,
        ),
        client,
    )
    query = OfferSearchQuery(
        kind=OfferKind.LODGING,
        destination="北京",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 4),
        provider_location_ids={"booking_city": "-2140479"},
    )

    offers = await provider.search(query)
    repriced = await provider.revalidate(offers[0])
    await client.aclose()

    assert offers[0].price_state == PriceState.LIVE_SEARCH
    assert offers[0].price.total.amount == 1100
    assert repriced.price_state == PriceState.REVALIDATED
    assert repriced.price.total.amount == 1120
    assert repriced.terms.refundable is True
