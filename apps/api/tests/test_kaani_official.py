from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import httpx
import pytest
from tripchord.planning.package import NormalizedLodgingQuote
from tripchord.providers.browser_bridge import BrowserSearchQuery
from tripchord.providers.kaani_official import (
    KAANI_OFFICIAL_BOOKING_URL,
    KAANI_OFFICIAL_PROPERTY_URL,
    KaaniOfficialLodgingProvider,
)

BOOKING_HTML = (
    b'''<html><body>
Check-in 2026-09-04 Check-out 2026-09-09 Guests 2 Adults Nights 5
Bed & Breakfast Rates inclusive of all applicable taxes, service charge, and green tax
Deluxe Double Room Seaview + Balcony
<script src="/_next/static/chunks/app/stays/[property]/book/page-6bc33c7732fbd0d6.js"></script>
<script>self.__next_f.push([1,"22:{\\"title\\":\\"Deluxe Double Room Seaview + Balcony\\",'''
    b'''\\"adults\\":2,\\"ezeeRoomId\\":\\"1861800000000000002\\"}"])</script>
<script>self.__next_f.push([1,"23:{\\"1861800000000000002\\":{\\"qty\\":12,\\"rate\\":546.5}}"])</script>
</body></html>'''
)
PROPERTY_HTML = (
    b'''<html><body>
<script type="application/ld+json">[{"@type":"Hotel","name":"Kaani Beach Hotel",
"description":"A beachfront hotel","starRating":{"ratingValue":"3"},
"address":{"streetAddress":"Aabaadhee Hingun Road"},
"geo":{"latitude":"4.17","longitude":"73.51"},
"amenityFeature":[{"name":"Free WiFi","value":true}]},
{"@type":"FAQPage","mainEntity":[{"name":"Kaani Beach Hotel location",
"acceptedAnswer":{"text":"Kaani Beach Hotel is steps from Maafushi's main '''
    b'''ferry jetty."}}]}]</script>
Deluxe Double Room Seaview + Balcony. Balcony, Sea View, Free WiFi, In-Room Safe.
</body></html>'''
)


class _Response:
    status_code = 200

    def __init__(self, content: bytes, url: str) -> None:
        self.content = content
        self.url = url
        self.history: tuple[object, ...] = ()


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> _Response:
        self.calls.append((url, params or {}))
        return _Response(
            BOOKING_HTML if url == KAANI_OFFICIAL_BOOKING_URL else PROPERTY_HTML,
            url,
        )


@pytest.mark.asyncio
async def test_kaani_official_parses_current_seaview_ssr_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fx(**_: object) -> object:
        return type(
            "Fx",
            (),
            {
                "usd_to_cny": Decimal("6.72"),
                "rate_date": date(2026, 8, 22),
                "response_sha256": "a" * 64,
                "captured_at": datetime(2026, 8, 22, 22, 25, tzinfo=UTC),
            },
        )()

    import tripchord.providers.kaani_official as module

    monkeypatch.setattr(module, "fetch_usd_cny_reference_rate", fake_fx)
    client = _Client()
    result = await KaaniOfficialLodgingProvider(
        client=cast("httpx.AsyncClient", client),
        now=lambda: datetime(2026, 8, 22, 22, 30, tzinfo=UTC),
    ).search(
        BrowserSearchQuery(
            destination="马累",
            destination_code="MLE",
            start_date=date(2026, 9, 4),
            end_date=date(2026, 9, 9),
            adults=2,
            rooms=1,
            children=0,
            currency="CNY",
        )
    )

    quote = cast(NormalizedLodgingQuote, result.result.quote)
    assert quote is not None
    assert quote.provider == "kaani_official"
    assert quote.total_for_party_cents == 54_650
    assert quote.availability.value == "available"
    assert quote.reference_total_cents == 367_248
    assert quote.reference_rate_captured_at == datetime(2026, 8, 22, 22, 25, tzinfo=UTC)
    assert quote.taxes_and_fees_included is True
    assert quote.breakfast_included is True
    assert quote.location_address == "Aabaadhee Hingun Road"
    assert quote.location_convenience.value == "confirmed_not_remote"
    assert quote.provider_property_id == "Beach-Hotel"
    assert quote.provider_room_id == "1861800000000000002"
    assert quote.cancellation_policy is None
    assert any(
        ref == "kaani-official-room-id:1861800000000000002"
        for ref in quote.evidence_refs
    )
    assert "kaani-official-currency-from-audited-rate-chunk:USD" in quote.evidence_refs
    assert client.calls[0] == (
        KAANI_OFFICIAL_BOOKING_URL,
        {"from": "2026-09-04", "to": "2026-09-09", "adults": "2", "rooms": "1", "children": "0"},
    )
    assert client.calls[1][0] == KAANI_OFFICIAL_PROPERTY_URL
