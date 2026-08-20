from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from tripchord.planning.package import PackageIntent
from tripchord.planning.stay_plans import system_stay_plan_candidate_set
from tripchord.providers.arena_official import ArenaOfficialLodgingProvider
from tripchord.providers.browser_bridge import BrowserSearchQuery


class _Response:
    status_code = 200

    def __init__(
        self, content: bytes, url: str = "https://letsbook.me/booking/arenabeachhotel"
    ) -> None:
        self.content = content
        self.url = url


class _Client:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        if url.endswith("gethoteldetails"):
            return _Response(b'{"status":"success","data":{"_xk":"0123456789abcdef"}}')
        if url.endswith("book-rooms-92"):
            return _Response(b"<html>booking engine</html>")
        return _Response(self.content)


@pytest.mark.asyncio
async def test_arena_official_normalizes_exact_maafushi_segment() -> None:
    fixture = Path(__file__).parent / "fixtures" / "arena_official_availability.json"
    client = _Client(fixture.read_bytes())
    intent = PackageIntent(
        trip_id="arena-test",
        origin="杭州",
        destination="马累",
        destination_place_key=None,
        start_date=date(2026, 9, 3),
        end_date=date(2026, 9, 9),
        adults=2,
        rooms=1,
        currency="CNY",
    )
    query = BrowserSearchQuery(
        destination="马累",
        destination_code="MLE",
        start_date=intent.start_date,
        end_date=intent.end_date,
        adults=2,
        rooms=1,
        currency="CNY",
    )
    result = await ArenaOfficialLodgingProvider(client=client).search(
        query,
        intent,
        system_stay_plan_candidate_set("马累"),
    )
    assert client.calls[0]["url"] == "https://live.ipms247.com/booking/book-rooms-92"
    assert client.calls[0]["params"] == {}
    assert client.calls[1]["url"].endswith("gethoteldetails")
    assert client.calls[1]["params"] == {"propertySlug": "arenabeachhotel"}
    assert client.calls[1]["headers"] == {
        "Accept": "application/json",
        "Origin": "https://letsbook.me",
        "Referer": "https://letsbook.me/booking/arenabeachhotel",
    }
    assert client.calls[2]["params"] == {
        "hotelCode": "92",
        "checkinDate": "2026-09-04",
        "checkoutDate": "2026-09-09",
        "adults": "2",
        "child": "0",
        "rooms": "1",
        "refresh": "false",
        "languageCode": "en",
        "isMobile": "false",
    }
    assert client.calls[2]["url"].endswith("getAvailability")
    assert client.calls[2]["headers"] == {
        "Accept": "application/json",
        "Origin": "https://letsbook.me",
        "Referer": "https://letsbook.me/booking/arenabeachhotel",
        "Authorization": "Bearer fedcba9876543210",
    }
    assert result.result.usable
    assert result.result.quote is not None
    assert result.result.quote.place_key.value == "maafushi"
    assert result.result.quote.check_in == date(2026, 9, 4)
    assert result.result.quote.check_out == date(2026, 9, 9)
    assert result.result.quote.adults == 2
    assert result.result.quote.rooms == 1
    assert result.result.quote.currency == "USD"
    assert result.result.quote.evidence_refs[0].startswith("arena-official-response:")


@pytest.mark.asyncio
async def test_captured_arena_evidence_binds_to_next_day_arrival_window(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "arena_official_availability.json"
    raw = fixture.read_bytes()
    raw_name = "arena-availability.json"
    raw_path = tmp_path / raw_name
    raw_path.write_bytes(raw)
    raw_path.chmod(0o600)
    evidence = tmp_path / "arena-captured-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema_version": "tripchord.captured-official-lodging.v1",
                "source_url": "https://arenabeachmaldives.com/booking/",
                "availability_url": (
                    "https://commonservice.ipms247.com/YCSAPIServices/booking/"
                    "getAvailability"
                ),
                "captured_at": "2026-08-18T20:52:15Z",
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_response_file": raw_name,
                "request_binding": {
                    "check_in": "2026-09-04",
                    "check_out": "2026-09-09",
                    "adults": 2,
                    "rooms": 1,
                    "currency": "USD",
                    "place_key": "maafushi",
                    "property_id": "92",
                    "room_rate_id": "9200000000000002",
                    "room_name": (
                        "Deluxe Double Room with Balcony and Seaview- Bed&Breakfast"
                    ),
                    "available_rooms": 1,
                    "total_after_tax": "1058.26",
                    "total_tax": "282.61",
                    "breakfast_included": True,
                },
            }
        ),
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    intent = PackageIntent(
        trip_id="arena-captured-test",
        origin="杭州",
        destination="马累",
        destination_place_key=None,
        start_date=date(2026, 9, 3),
        end_date=date(2026, 9, 9),
        adults=2,
        rooms=1,
        currency="CNY",
    )
    query = BrowserSearchQuery(
        destination="马累",
        destination_code="MLE",
        start_date=intent.start_date,
        end_date=intent.end_date,
        adults=2,
        rooms=1,
        currency="CNY",
    )
    result = await ArenaOfficialLodgingProvider(
        captured_evidence_path=str(evidence),
        captured_evidence_root=str(tmp_path),
    ).search(query, intent, system_stay_plan_candidate_set("马累"))
    assert result.result.usable
    assert result.result.quote is not None
    assert result.result.quote.check_in == date(2026, 9, 4)
    assert result.result.quote.check_out == date(2026, 9, 9)
    assert result.result.quote.total_for_party_cents == 105826
    assert result.result.quote.evidence_refs[0].startswith("captured-official-response:")
