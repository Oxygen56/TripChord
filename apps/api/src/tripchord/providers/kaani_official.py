"""Read-only Kaani Beach Hotel official booking-page adapter."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx

from tripchord.planning.package import (
    LodgingLocationConvenience,
    NormalizedLodgingQuote,
    PackageArea,
    PackagePlaceKey,
    QuoteAvailability,
)
from tripchord.providers.base import ProviderError
from tripchord.providers.browser_bridge import BrowserSearchQuery, BrowserVertical
from tripchord.providers.fx_reference import (
    ECB_DAILY_REFERENCE_URL,
    fetch_usd_cny_reference_rate,
)
from tripchord.providers.quote_normalizer import (
    NormalizedBrowserQuoteResult,
    QuoteNormalizationStatus,
)

KAANI_OFFICIAL_BOOKING_URL = "https://kaanihotels.com/stays/Beach-Hotel/book"
KAANI_OFFICIAL_PROPERTY_URL = "https://kaanihotels.com/stays/Beach-Hotel"
KAANI_SEAVIEW_ROOM_ID = "1861800000000000002"
KAANI_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
KAANI_PARSER_CONTRACT = "kaani-official-ssr-v1"
KAANI_RATE_CHUNK = "page-6bc33c7732fbd0d6.js"


@dataclass(frozen=True)
class KaaniOfficialLodgingResult:
    result: NormalizedBrowserQuoteResult
    query: dict[str, object]
    response_sha256: str
    captured_at: datetime


def _text(raw: bytes) -> str:
    if len(raw) > KAANI_MAX_RESPONSE_BYTES:
        raise ValueError("Kaani official response exceeds the bounded size limit")
    decoded = html.unescape(raw.decode("utf-8"))
    decoded = re.sub(r"(?is)<(script|style).*?</\1>", " ", decoded)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", decoded)).strip()


def _require(text: str, pattern: str, label: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        raise ValueError(f"Kaani official page missing {label}")
    return match.group(0)


class KaaniOfficialLodgingProvider:
    """Fetch one exact current official Kaani Beach comparison quote."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))

    async def search(self, query: BrowserSearchQuery) -> KaaniOfficialLodgingResult:
        if (
            query.start_date is None
            or query.end_date is None
            or query.destination_code != "MLE"
        ):
            raise ValueError("Kaani official source requires an MLE lodging query")
        if query.adults != 2 or query.rooms != 1 or query.children != 0:
            raise ValueError("Kaani official source only supports the audited party shape")
        if query.end_date <= query.start_date:
            raise ValueError("Kaani official source received an invalid stay window")
        nights = (query.end_date - query.start_date).days
        params = {
            "from": query.start_date.isoformat(),
            "to": query.end_date.isoformat(),
            "adults": "2",
            "rooms": "1",
            "children": "0",
        }
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True
        )
        try:
            booking_response = await client.get(KAANI_OFFICIAL_BOOKING_URL, params=params)
            property_response = await client.get(KAANI_OFFICIAL_PROPERTY_URL)
        finally:
            if owned_client:
                await client.aclose()
        if booking_response.status_code != 200 or property_response.status_code != 200:
            raise ValueError("Kaani official page did not return HTTP 200")
        self._validate_response_url(booking_response, KAANI_OFFICIAL_BOOKING_URL)
        self._validate_response_url(property_response, KAANI_OFFICIAL_PROPERTY_URL)
        booking_raw = booking_response.content
        property_raw = property_response.content
        booking_text = _text(booking_raw)
        property_text = _text(property_raw)
        self._validate_booking_page(
            booking_raw=booking_raw,
            booking_text=booking_text,
            query=query,
            nights=nights,
        )
        self._validate_property_page(property_raw, property_text)
        rate_match = re.search(
            rf'\\?"{KAANI_SEAVIEW_ROOM_ID}\\?"\s*:\s*\{{\s*'
            r'\\?"qty\\?"\s*:\s*(\d+)\s*,\s*'
            r'\\?"rate\\?"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            html.unescape(booking_raw.decode("utf-8")),
        )
        if rate_match is None:
            raise ValueError("Kaani official SSR has no exact seaview rate")
        qty = int(rate_match.group(1))
        total_usd = Decimal(rate_match.group(2))
        if qty < query.rooms or not total_usd.is_finite() or total_usd <= 0:
            raise ValueError("Kaani official seaview rate is not currently available")
        rate_chunk_ref = f"kaani-official-rate-chunk:{KAANI_RATE_CHUNK}"
        captured_at = self._now().astimezone(UTC)
        booking_sha = hashlib.sha256(booking_raw).hexdigest()
        property_sha = hashlib.sha256(property_raw).hexdigest()
        total_usd_cents = int((total_usd * 100).quantize(Decimal("1")))
        reference_total_cents: int | None = None
        reference_rate_date: date | None = None
        reference_rate: Decimal | None = None
        reference_response_sha: str | None = None
        reference_captured_at: datetime | None = None
        try:
            fx = await fetch_usd_cny_reference_rate(now=self._now)
            reference_total_cents = int(
                (total_usd * 100 * fx.usd_to_cny).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            reference_rate_date = fx.rate_date
            reference_rate = fx.usd_to_cny
            reference_response_sha = fx.response_sha256
            reference_captured_at = fx.captured_at
        except (AttributeError, ProviderError, httpx.HTTPError, ValueError):
            pass
        evidence_refs = (
            f"kaani-official-booking-url:{KAANI_OFFICIAL_BOOKING_URL}",
            f"kaani-official-booking-captured-at:{captured_at.isoformat()}",
            f"kaani-official-booking-sha256:{booking_sha}",
            f"kaani-official-property-url:{KAANI_OFFICIAL_PROPERTY_URL}",
            f"kaani-official-property-captured-at:{captured_at.isoformat()}",
            f"kaani-official-property-sha256:{property_sha}",
            f"kaani-official-room-id:{KAANI_SEAVIEW_ROOM_ID}",
            "kaani-official-currency-from-audited-rate-chunk:USD",
            f"kaani-official-rate-qty:{qty}",
            "kaani-official-stay-total-semantics:rate-is-total-for-requested-stay",
            "kaani-official-taxes:inclusive-of-all-applicable-taxes-service-charge-and-green-tax",
            rate_chunk_ref,
            f"kaani-official-parser-contract:{KAANI_PARSER_CONTRACT}",
            *(
                (f"ecb-reference-url:{ECB_DAILY_REFERENCE_URL}",)
                if reference_total_cents is not None
                else ()
            ),
        )
        quote = NormalizedLodgingQuote(
            id=f"kaani-official:{KAANI_SEAVIEW_ROOM_ID}:{query.start_date}:{query.end_date}",
            provider="kaani_official",
            currency="USD",
            total_for_party_cents=total_usd_cents,
            taxes_and_fees_included=True,
            captured_at=captured_at,
            expires_at=captured_at + timedelta(minutes=10),
            availability=QuoteAvailability.AVAILABLE,
            evidence_refs=evidence_refs,
            property_name="Kaani Beach Hotel",
            area=PackageArea.DESTINATION_ISLAND,
            place_key=PackagePlaceKey.MAAFUSHI,
            check_in=query.start_date,
            check_out=query.end_date,
            adults=2,
            rooms=1,
            room_name="Deluxe Double Room Seaview + Balcony",
            breakfast_included=True,
            cancellation_policy=(
                None
            ),
            provider_property_id="Beach-Hotel",
            provider_room_id=KAANI_SEAVIEW_ROOM_ID,
            provider_rate_plan_id=None,
            location_address="Aabaadhee Hingun Road",
            nearby_location_evidence=(
                "near Maafushi's main ferry jetty; official page states: "
                "steps from Maafushi's main ferry jetty",
                "beachfront",
            ),
            location_convenience=LodgingLocationConvenience.CONFIRMED_NOT_REMOTE,
            reference_total_cents=reference_total_cents,
            reference_currency="CNY" if reference_total_cents is not None else None,
            reference_rate_source=(
                ECB_DAILY_REFERENCE_URL if reference_total_cents is not None else None
            ),
            reference_rate_date=reference_rate_date,
            reference_usd_to_cny=reference_rate,
            reference_rate_response_sha256=reference_response_sha,
            reference_rate_captured_at=reference_captured_at,
        )
        return KaaniOfficialLodgingResult(
            result=NormalizedBrowserQuoteResult(
                provider="kaani_official",
                kind=BrowserVertical.LODGING,
                status=QuoteNormalizationStatus.USABLE,
                quote=quote,
            ),
            query={**params, "url": KAANI_OFFICIAL_BOOKING_URL, "nights": nights},
            response_sha256=booking_sha,
            captured_at=captured_at,
        )

    @staticmethod
    def _validate_booking_page(
        *, booking_raw: bytes, booking_text: str, query: BrowserSearchQuery, nights: int
    ) -> None:
        if query.start_date is None or query.end_date is None:
            raise ValueError("Kaani official booking validation requires stay dates")
        _require(
            booking_text,
            rf"Check.?in\s*{re.escape(query.start_date.isoformat())}",
            "check-in echo",
        )
        _require(
            booking_text,
            rf"Check.?out\s*{re.escape(query.end_date.isoformat())}",
            "check-out echo",
        )
        _require(booking_text, r"2\s*Adults", "adult echo")
        _require(booking_text, rf"Nights\s*{nights}", "night echo")
        _require(booking_text, r"Bed\s*&\s*Breakfast", "breakfast inclusion")
        _require(
            booking_text,
            r"Rates inclusive of all applicable taxes, service charge, and green tax",
            "tax inclusion",
        )
        decoded = html.unescape(booking_raw.decode("utf-8"))
        if query.rooms != 1:
            raise ValueError("Kaani official booking echo does not match the audited query")
        room_match = re.search(re.escape(KAANI_SEAVIEW_ROOM_ID), decoded)
        if room_match is None:
            raise ValueError("Kaani official page missing exact room metadata")
        room_window = decoded[max(0, room_match.start() - 900) : room_match.end() + 300]
        _require(room_window, r"Deluxe Double Room Seaview \+ Balcony", "room title")
        _require(room_window, r"(?:adults|capacity).{0,20}[2-9]", "room capacity")
        _require(booking_raw.decode("utf-8"), re.escape(KAANI_RATE_CHUNK), "rate semantics chunk")

    @staticmethod
    def _validate_property_page(property_raw: bytes, property_text: str) -> None:
        decoded = html.unescape(property_raw.decode("utf-8"))
        scripts = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            decoded,
            re.IGNORECASE | re.DOTALL,
        )
        hotel: dict[str, object] | None = None
        for script in scripts:
            try:
                payload = json.loads(script)
            except json.JSONDecodeError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("@type") == "Hotel"
                    and candidate.get("name") == "Kaani Beach Hotel"
                ):
                    hotel = candidate
                    break
            if hotel is not None:
                break
        if hotel is None:
            raise ValueError("Kaani official property page missing Hotel JSON-LD")
        if hotel.get("name") != "Kaani Beach Hotel":
            raise ValueError("Kaani official Hotel JSON-LD name mismatch")
        address = hotel.get("address")
        if not isinstance(address, dict) or address.get("streetAddress") != "Aabaadhee Hingun Road":
            raise ValueError("Kaani official Hotel JSON-LD address missing")
        geo = hotel.get("geo")
        if not isinstance(geo, dict) or not geo.get("latitude") or not geo.get("longitude"):
            raise ValueError("Kaani official Hotel JSON-LD geo missing")
        star_rating = hotel.get("starRating")
        if not isinstance(star_rating, dict) or str(star_rating.get("ratingValue")) != "3":
            raise ValueError("Kaani official Hotel JSON-LD star rating missing")
        amenities = hotel.get("amenityFeature")
        if not isinstance(amenities, list) or not amenities:
            raise ValueError("Kaani official Hotel JSON-LD amenities missing")
        faq = None
        for script in scripts:
            try:
                payload = json.loads(script)
            except json.JSONDecodeError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "FAQPage":
                    faq = candidate
                    break
            if faq is not None:
                break
        if faq is None:
            raise ValueError("Kaani official property page missing FAQPage JSON-LD")
        faq_text = json.dumps(faq, ensure_ascii=False)
        _require(faq_text, r"Kaani Beach Hotel", "FAQ hotel identity")
        _require(faq_text, r"steps from Maafushi.?s main ferry jetty", "ferry proximity")
        for pattern, label in (
            (r"Deluxe Double Room Seaview \+ Balcony", "room identity"),
            (r"(?:balcony|sea.?view|Free WiFi|In-Room Safe)", "quality facility"),
        ):
            _require(property_text, pattern, label)

    @staticmethod
    def _validate_response_url(response: httpx.Response, expected_url: str) -> None:
        final_url = httpx.URL(str(getattr(response, "url", "") or ""))
        expected = httpx.URL(expected_url)
        if final_url.host != expected.host or final_url.path != expected.path:
            raise ValueError("Kaani official response redirected outside the expected page")
        for redirect in getattr(response, "history", ()):
            redirect_url = httpx.URL(str(getattr(redirect, "url", "") or ""))
            if redirect_url.host != expected.host:
                raise ValueError("Kaani official response crossed an external redirect")


__all__ = ["KaaniOfficialLodgingProvider", "KaaniOfficialLodgingResult"]
