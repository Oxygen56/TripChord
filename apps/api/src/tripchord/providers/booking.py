from __future__ import annotations

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


class BookingConfig(DomainModel):
    api_token: str = Field(min_length=1)
    affiliate_id: str = Field(min_length=1)
    base_url: str = "https://demandapi-sandbox.booking.com/3.2"
    source_mode: SourceMode = SourceMode.SANDBOX
    cache_ttl_seconds: int = Field(default=300, gt=0, le=1800)


class BookingAccommodationProvider:
    """Booking.com Demand API v3.2 search and availability adapter."""

    name = "booking"
    supported_kinds = frozenset({OfferKind.LODGING})

    def __init__(self, config: BookingConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(base_url=config.base_url, timeout=20)
        self._owns_client = client is None
        self._contexts: dict[str, tuple[OfferSearchQuery, str, str | None]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        if query.kind != OfferKind.LODGING or query.end_date is None:
            return ()
        city_id = query.provider_location_ids.get("booking_city")
        if city_id is None:
            raise ProviderError(
                self.name,
                "missing_location_id",
                "Booking search requires provider_location_ids.booking_city",
            )
        response = await self._client.post(
            "/accommodations/search",
            headers=self._headers,
            json={
                "booker": {"country": query.booker_country, "platform": "desktop"},
                "checkin": query.start_date.isoformat(),
                "checkout": query.end_date.isoformat(),
                "city": int(city_id),
                "currency": query.currency,
                "extras": ["products"],
                "guests": {
                    "number_of_adults": query.adults,
                    "number_of_rooms": query.rooms,
                    "children": list(query.children_ages),
                },
            },
        )
        self._raise_for_status(response)
        payload = response.json()
        request_id = str(payload.get("request_id") or response.headers.get("x-request-id") or "")
        captured_at = datetime.now(UTC)
        offers: list[TravelOffer] = []
        for accommodation in payload.get("data", []):
            if not isinstance(accommodation, dict):
                continue
            products = accommodation.get("products") or [None]
            for product in products:
                offer = self._normalize_offer(
                    accommodation,
                    product if isinstance(product, dict) else None,
                    query=query,
                    captured_at=captured_at,
                    request_id=request_id or None,
                    revalidated=False,
                )
                product_id = str(product.get("id")) if isinstance(product, dict) else None
                self._contexts[offer.id] = (query, str(accommodation["id"]), product_id)
                offers.append(offer)
        return tuple(offers)

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        context = self._contexts.get(offer.id)
        if context is None:
            raise ProviderError(
                self.name,
                "missing_revalidation_context",
                "offer context is unavailable",
            )
        query, accommodation_id, product_id = context
        if query.end_date is None:  # pragma: no cover - protected by search validation
            raise ProviderError(self.name, "invalid_context", "checkout date is unavailable")
        response = await self._client.post(
            "/accommodations/availability",
            headers=self._headers,
            json={
                "accommodations": [int(accommodation_id)],
                "booker": {"country": query.booker_country, "platform": "desktop"},
                "checkin": query.start_date.isoformat(),
                "checkout": query.end_date.isoformat(),
                "currency": query.currency,
                "extras": ["products"],
                "guests": {
                    "number_of_adults": query.adults,
                    "number_of_rooms": query.rooms,
                    "children": list(query.children_ages),
                },
            },
        )
        self._raise_for_status(response)
        payload = response.json()
        accommodations = payload.get("data", [])
        if not accommodations:
            raise ProviderError(self.name, "offer_unavailable", "accommodation is unavailable")
        accommodation = accommodations[0]
        products = accommodation.get("products", [])
        product = next((item for item in products if str(item.get("id")) == product_id), None)
        if product is None:
            raise ProviderError(
                self.name,
                "product_unavailable",
                "the selected room rate is unavailable",
            )
        return self._normalize_offer(
            accommodation,
            product,
            query=query,
            captured_at=datetime.now(UTC),
            request_id=str(payload.get("request_id") or "") or None,
            revalidated=True,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_token}",
            "X-Affiliate-Id": self._config.affiliate_id,
            "Content-Type": "application/json",
        }

    def _normalize_offer(
        self,
        accommodation: dict[str, Any],
        product: dict[str, Any] | None,
        *,
        query: OfferSearchQuery,
        captured_at: datetime,
        request_id: str | None,
        revalidated: bool,
    ) -> TravelOffer:
        price = (product or {}).get("price") or accommodation.get("price") or {}
        currency_data = accommodation.get("currency", {})
        currency = str(currency_data.get("booker") or query.currency).upper()
        total = self._amount(price.get("total"), currency)
        base_value = self._amount(price.get("base"), currency, required=False)
        base = base_value if base_value is not None else total
        property_id = str(accommodation["id"])
        product_id = str((product or {}).get("id") or "best")
        room = (product or {}).get("room") or {}
        room_id = str(room.get("id") or "unspecified")
        policies = (product or {}).get("policies") or {}
        cancellation = policies.get("cancellation") or []
        meal_plan = policies.get("meal_plan") or {}
        mode = self._config.source_mode
        price_state = (
            PriceState.REVALIDATED
            if revalidated and mode == SourceMode.PRODUCTION
            else PriceState.LIVE_SEARCH
            if mode == SourceMode.PRODUCTION
            else PriceState.ESTIMATED
        )
        booking_url = accommodation.get("url")
        if isinstance(booking_url, dict):
            booking_url = booking_url.get("web")
        if booking_url is None:
            booking_url = accommodation.get("deep_link_url")
        return TravelOffer(
            id=f"booking:{property_id}:{product_id}",
            kind=OfferKind.LODGING,
            title=f"Accommodation {property_id} · room {room_id}",
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
                total=Money(amount=total, currency=currency),
                components_complete=base == total,
            ),
            terms=OfferTerms(
                refundable=self._is_refundable(cancellation),
                cancellation_summary=self._cancellation_summary(cancellation),
                meal_summary=str(meal_plan.get("plan")) if meal_plan else None,
            ),
            comparison_key=(
                f"lodging:{property_id}:room={room_id}:adults={query.adults}:"
                f"children={','.join(map(str, query.children_ages))}:"
                f"checkin={query.start_date}:checkout={query.end_date}:"
                f"meal={meal_plan.get('plan', 'unknown')}:"
                f"refundable={self._is_refundable(cancellation)}"
            ),
            revalidation_token=f"{property_id}:{product_id}",
            booking_url=booking_url,
            metadata={
                "destination": query.destination,
                "property_id": property_id,
                "product_id": product_id,
                "room_id": room_id,
                "checkin": query.start_date.isoformat(),
                "checkout": query.end_date.isoformat() if query.end_date else None,
                "adults": query.adults,
                "rooms": query.rooms,
            },
        )

    def _amount(
        self,
        value: Any,
        currency: str,
        *,
        required: bool = True,
    ) -> Decimal | None:
        if isinstance(value, dict):
            value = value.get("booker_currency") or value.get("accommodation_currency")
        if value is None:
            if required:
                raise ProviderError(self.name, "missing_price", f"missing {currency} price")
            return None
        return Decimal(str(value))

    def _is_refundable(self, cancellation: Any) -> bool | None:
        if not isinstance(cancellation, list) or not cancellation:
            return None
        return any(
            item.get("free_cancellation_until")
            for item in cancellation
            if isinstance(item, dict)
        )

    def _cancellation_summary(self, cancellation: Any) -> str | None:
        if not isinstance(cancellation, list) or not cancellation:
            return None
        return "free cancellation available" if self._is_refundable(cancellation) else "restricted"

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        retryable = response.status_code in {429, 500, 502, 503, 504}
        try:
            payload = response.json()
            detail = payload.get("message") or payload.get("detail") or response.text
        except ValueError:
            detail = response.text
        raise ProviderError(
            self.name,
            f"http_{response.status_code}",
            str(detail or "provider request failed"),
            retryable=retryable,
        )
