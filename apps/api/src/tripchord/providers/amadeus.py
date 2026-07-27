from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from pydantic import Field

from tripchord.domain.common import DomainModel, Money
from tripchord.domain.offers import (
    OfferKind,
    OfferSource,
    OfferTerms,
    PriceBreakdown,
    PriceState,
    TravelOffer,
)
from tripchord.domain.source import SourceMode
from tripchord.providers.base import OfferSearchQuery, ProviderError


class AmadeusConfig(DomainModel):
    client_id: str = Field(min_length=1)
    client_secret: str = Field(min_length=1)
    base_url: str = "https://test.api.amadeus.com"
    source_mode: SourceMode = SourceMode.SANDBOX
    cache_ttl_seconds: int = Field(default=300, gt=0, le=1800)


class _AccessToken:
    def __init__(self, value: str, expires_in: int) -> None:
        self.value = value
        self.expires_at = time.monotonic() + max(expires_in - 30, 1)

    @property
    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


class AmadeusFlightProvider:
    """Amadeus Flight Offers Search and Price adapter.

    Production and test environments share the same contract, but test data is
    always labelled sandbox/estimated and can never become a live price.
    """

    name = "amadeus"
    supported_kinds = frozenset({OfferKind.FLIGHT})

    def __init__(self, config: AmadeusConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(base_url=config.base_url, timeout=20)
        self._owns_client = client is None
        self._token: _AccessToken | None = None
        self._raw_offers: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        if query.kind != OfferKind.FLIGHT or query.origin is None:
            return ()
        token = await self._access_token()
        params: dict[str, str | int] = {
            "originLocationCode": query.origin,
            "destinationLocationCode": query.destination,
            "departureDate": query.start_date.isoformat(),
            "adults": query.adults,
            "travelClass": query.cabin_class.value,
            "currencyCode": query.currency,
            "max": 50,
        }
        if query.end_date is not None:
            params["returnDate"] = query.end_date.isoformat()
        response = await self._client.get(
            "/v2/shopping/flight-offers",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._raise_for_status(response)
        payload = response.json()
        request_id = response.headers.get("x-request-id") or payload.get("meta", {}).get("count")
        captured_at = datetime.now(UTC)
        offers: list[TravelOffer] = []
        for raw in payload.get("data", []):
            if not isinstance(raw, dict):
                continue
            offer = self._normalize_offer(
                raw,
                query=query,
                captured_at=captured_at,
                request_id=str(request_id) if request_id is not None else None,
                revalidated=False,
            )
            self._raw_offers[offer.id] = raw
            offers.append(offer)
        return tuple(offers)

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        raw = self._raw_offers.get(offer.id)
        if raw is None:
            raise ProviderError(
                self.name,
                "missing_revalidation_context",
                "raw offer is unavailable",
            )
        token = await self._access_token()
        response = await self._client.post(
            "/v1/shopping/flight-offers/pricing",
            headers={
                "Authorization": f"Bearer {token}",
                "X-HTTP-Method-Override": "GET",
            },
            json={"data": {"type": "flight-offers-pricing", "flightOffers": [raw]}},
        )
        self._raise_for_status(response)
        payload = response.json()
        flight_offers = payload.get("data", {}).get("flightOffers", [])
        if not flight_offers:
            raise ProviderError(self.name, "offer_unavailable", "provider returned no priced offer")
        normalized = self._normalize_offer(
            flight_offers[0],
            query=self._query_from_offer(offer),
            captured_at=datetime.now(UTC),
            request_id=response.headers.get("x-request-id"),
            revalidated=True,
        )
        self._raw_offers[normalized.id] = flight_offers[0]
        return normalized

    async def _access_token(self) -> str:
        if self._token is not None and self._token.valid:
            return self._token.value
        response = await self._client.post(
            "/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._raise_for_status(response)
        payload = response.json()
        self._token = _AccessToken(payload["access_token"], int(payload.get("expires_in", 1800)))
        return self._token.value

    def _normalize_offer(
        self,
        raw: dict[str, Any],
        *,
        query: OfferSearchQuery,
        captured_at: datetime,
        request_id: str | None,
        revalidated: bool,
    ) -> TravelOffer:
        price = raw.get("price", {})
        currency = str(price.get("currency", query.currency)).upper()
        base = Decimal(str(price["base"]))
        total = Decimal(str(price.get("grandTotal", price.get("total"))))
        combined_taxes_fees = max(total - base, Decimal("0"))
        segments = [
            segment
            for itinerary in raw.get("itineraries", [])
            for segment in itinerary.get("segments", [])
        ]
        segment_key = "|".join(
            f"{segment.get('carrierCode', '')}{segment.get('number', '')}:"
            f"{segment.get('departure', {}).get('at', '')}:"
            f"{segment.get('arrival', {}).get('at', '')}"
            for segment in segments
        )
        carrier = segments[0].get("carrierCode", "") if segments else ""
        identifier = str(raw.get("id", segment_key))
        mode = self._config.source_mode
        price_state = (
            PriceState.REVALIDATED
            if revalidated and mode == SourceMode.PRODUCTION
            else PriceState.LIVE_SEARCH
            if mode == SourceMode.PRODUCTION
            else PriceState.ESTIMATED
        )
        return TravelOffer(
            id=f"amadeus:{identifier}",
            kind=OfferKind.FLIGHT,
            title=f"{query.origin} → {query.destination} · {carrier or 'flight'}",
            source=OfferSource(
                provider=self.name,
                mode=mode,
                request_id=request_id,
                captured_at=captured_at,
                expires_at=captured_at + timedelta(seconds=self._config.cache_ttl_seconds),
            ),
            price_state=price_state,
            price=PriceBreakdown(
                base=Money(amount=base, currency=currency),
                taxes=Money(amount=combined_taxes_fees, currency=currency),
                total=Money(amount=total, currency=currency),
                components_complete=True,
            ),
            terms=OfferTerms(
                baggage_summary=self._baggage_summary(raw),
                refundable=None,
            ),
            comparison_key=(
                f"flight:{segment_key}:{query.cabin_class.value}:"
                f"adults={query.adults}:children={','.join(map(str, query.children_ages))}"
            ),
            revalidation_token=identifier,
            metadata={
                "origin": query.origin,
                "destination": query.destination,
                "departure_date": query.start_date.isoformat(),
                "return_date": query.end_date.isoformat() if query.end_date else None,
                "adults": query.adults,
                "currency": query.currency,
                "cabin_class": query.cabin_class.value,
                "taxes_fees_combined": True,
                "segment_count": len(segments),
            },
        )

    def _query_from_offer(self, offer: TravelOffer) -> OfferSearchQuery:
        metadata = offer.metadata
        adults = metadata.get("adults")
        if not isinstance(adults, (str, int)):
            raise ProviderError(self.name, "invalid_context", "offer adults are unavailable")
        return OfferSearchQuery(
            kind=OfferKind.FLIGHT,
            origin=str(metadata["origin"]),
            destination=str(metadata["destination"]),
            start_date=str(metadata["departure_date"]),
            end_date=str(metadata["return_date"]) if metadata.get("return_date") else None,
            adults=int(adults),
            currency=str(metadata["currency"]),
            cabin_class=str(metadata["cabin_class"]),
        )

    def _baggage_summary(self, raw: dict[str, Any]) -> str | None:
        traveler_pricings = raw.get("travelerPricings", [])
        if not traveler_pricings:
            return None
        details = traveler_pricings[0].get("fareDetailsBySegment", [])
        quantities = [
            item.get("includedCheckedBags", {}).get("quantity")
            for item in details
            if item.get("includedCheckedBags", {}).get("quantity") is not None
        ]
        return f"checked bags: {min(quantities)}" if quantities else None

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        retryable = response.status_code in {429, 500, 502, 503, 504}
        try:
            payload = response.json()
            errors = payload.get("errors", [])
            detail = errors[0].get("detail") or errors[0].get("title") if errors else response.text
        except ValueError:
            detail = response.text
        raise ProviderError(
            self.name,
            f"http_{response.status_code}",
            str(detail or "provider request failed"),
            retryable=retryable,
        )
