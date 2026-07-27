from datetime import date

import httpx
import pytest
from tripchord.domain.offers import OfferKind, PriceState
from tripchord.domain.source import SourceMode
from tripchord.providers.amadeus import AmadeusConfig, AmadeusFlightProvider
from tripchord.providers.base import OfferSearchQuery


def amadeus_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/oauth2/token"):
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "token", "expires_in": 1800})
    offer = {
        "id": "1",
        "price": {"currency": "CNY", "base": "800.00", "grandTotal": "920.00"},
        "itineraries": [
            {
                "segments": [
                    {
                        "carrierCode": "MU",
                        "number": "5101",
                        "departure": {"at": "2026-10-01T08:00:00"},
                        "arrival": {"at": "2026-10-01T10:20:00"},
                    }
                ]
            }
        ],
        "travelerPricings": [{"fareDetailsBySegment": [{"includedCheckedBags": {"quantity": 1}}]}],
    }
    if request.url.path.endswith("/flight-offers"):
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, json={"meta": {"count": 1}, "data": [offer]})
    if request.url.path.endswith("/flight-offers/pricing"):
        assert request.headers["x-http-method-override"] == "GET"
        offer["price"]["grandTotal"] = "930.00"
        return httpx.Response(200, json={"data": {"flightOffers": [offer]}})
    raise AssertionError(f"unexpected request {request.url}")


@pytest.mark.asyncio
async def test_amadeus_search_and_reprice_preserve_truth_labels() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(amadeus_handler),
        base_url="https://api.amadeus.test",
    )
    provider = AmadeusFlightProvider(
        AmadeusConfig(
            client_id="client",
            client_secret="secret",
            base_url="https://api.amadeus.test",
            source_mode=SourceMode.PRODUCTION,
        ),
        client,
    )
    query = OfferSearchQuery(
        kind=OfferKind.FLIGHT,
        origin="SHA",
        destination="PEK",
        start_date=date(2026, 10, 1),
    )

    offers = await provider.search(query)
    repriced = await provider.revalidate(offers[0])
    await client.aclose()

    assert offers[0].price_state == PriceState.LIVE_SEARCH
    assert offers[0].price.total.amount == 920
    assert repriced.price_state == PriceState.REVALIDATED
    assert repriced.price.total.amount == 930
    assert repriced.terms.baggage_summary == "checked bags: 1"
