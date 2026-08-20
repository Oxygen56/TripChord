"""Read-only Samann Host public booking-chain adapter.

The hotel site currently hands the search to the public IPMS booking page.
This adapter may submit only the booking-search form and the read-only rate
fragment request; it never selects a room, starts a reservation, or sends a
guest/payment payload.  If the public engine does not return an exact room
card, the adapter returns a blocked observation rather than inventing a rate.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

SAMANN_HOST_URL = "https://samannhost.com"
SAMANN_HOST_BOOKING_URL = "https://live.ipms247.com/booking/book-rooms-samannhost"
SAMANN_HOST_SEARCH_URL = "https://live.ipms247.com/booking/loadroomlisting.php"
SAMANN_HOST_ROOM_FRAGMENT_URL = "https://live.ipms247.com/booking/bx-15568"
SAMANN_HOST_RATES_URL = "https://live.ipms247.com/booking/rmdetails"
SAMANN_HOST_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class SamannHostObservation:
    request: dict[str, object]
    source_url: str
    captured_at: datetime
    response_sha256: str
    raw_response: bytes
    status: str
    room_name: str | None = None
    currency: str | None = None
    total_for_party_minor: int | None = None
    taxes_included: bool | None = None
    breakfast: bool | None = None
    cancellation_policy: str | None = None


class SamannHostOfficialLodgingProvider:
    """Fetch one exact 2-adult, 1-room Samann Host public rate observation."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
        observation_dir: str | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))
        self._observation_dir = observation_dir

    async def search_exact(
        self,
        *,
        check_in: date,
        check_out: date,
        adults: int = 2,
        rooms: int = 1,
    ) -> SamannHostObservation:
        if adults != 2 or rooms != 1:
            raise ValueError("Samann Host adapter only supports the audited 2-adult/1-room query")
        if check_out <= check_in:
            raise ValueError("Samann Host checkout must follow checkin")
        request = {
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "adults": adults,
            "rooms": rooms,
            "children": 0,
            "search_url": SAMANN_HOST_BOOKING_URL,
            "official_site_url": SAMANN_HOST_URL,
        }
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        )
        captured: list[bytes] = []
        try:
            landing = await client.get(SAMANN_HOST_URL)
            self._bounded(landing.content, "official site")
            captured.append(landing.content)
            booking = await client.get(SAMANN_HOST_BOOKING_URL)
            self._bounded(booking.content, "booking page")
            captured.append(booking.content)
            form = {
                "eZ_chkin": check_in.strftime("%d/%m/%Y"),
                "HotelId": "samannhost",
                "eZ_chkout": check_out.strftime("%d/%m/%Y"),
                "calformat": "",
                "eZ_adult": str(adults),
                "eZ_child": "0",
                "eZ_room": str(rooms),
                "promotioncode": "",
                "_resaffiliatecode": "",
                "roomtypeunkid": "",
            }
            search = await client.post(
                SAMANN_HOST_SEARCH_URL,
                params={"HotelId": "samannhost"},
                data=form,
                headers={"Referer": SAMANN_HOST_BOOKING_URL},
            )
            self._bounded(search.content, "search")
            captured.append(search.content)
            fragment = await client.post(
                SAMANN_HOST_ROOM_FRAGMENT_URL,
                data={
                    "calendarDateFormat": "dd-mm-yy",
                    "calendarDefaultDays": "0",
                    "HotelId": "15568",
                    "isLogin": "lf",
                    "eZ_chkin": check_in.strftime("%d-%m-%Y"),
                    "eZ_chkout": check_out.strftime("%d-%m-%Y"),
                    "eZ_Nights": str((check_out - check_in).days),
                    "eZ_adult": str(adults),
                    "eZ_child": "0",
                    "promotionalcode": "",
                    "selectedLang": "",
                    "CalLanguage": "en",
                    "LayoutTheme": "2",
                },
                headers={"Referer": SAMANN_HOST_BOOKING_URL},
            )
            self._bounded(fragment.content, "search form")
            captured.append(fragment.content)
            rates = await client.post(
                SAMANN_HOST_RATES_URL,
                data={
                    "checkin": check_in.strftime("%d-%m-%Y"),
                    "gridcolumn": str((check_out - check_in).days),
                    "adults": str(adults),
                    "child": "0",
                    "nonights": str((check_out - check_in).days),
                    "ShowSelectedNights": "true",
                    "DefaultSelectedNights": "1",
                    "calendarDateFormat": "dd-mm-yy",
                    "rooms": str(rooms),
                    "promotion": "",
                    "ArrvalDt": self._now().date().isoformat(),
                    "HotelId": "15568",
                    "isLogin": "lf",
                    "selectedLang": "",
                    "modifysearch": "0",
                    "promotioncode": "",
                    "layoutView": "2",
                    "ShowMinNightsMatchedRatePlan": "false",
                    "LayoutTheme": "2",
                    "w_showadult": "true",
                    "w_showchild_bb": "true",
                    "ShowMoreLessOpt": "",
                    "w_showchild": "true",
                    "metasearch": "",
                    "ischeckavailabilityclicked": "0",
                },
                headers={"Referer": SAMANN_HOST_BOOKING_URL},
            )
            self._bounded(rates.content, "rates")
            captured.append(rates.content)
        finally:
            if owned_client:
                await client.aclose()
        raw = captured[-1] if captured else b""
        observation = self._parse(raw, request=request)
        self._persist(observation)
        return observation

    @staticmethod
    def _bounded(content: bytes, label: str) -> None:
        if len(content) > SAMANN_HOST_MAX_RESPONSE_BYTES:
            raise ValueError(f"Samann Host {label} response exceeds the bounded size limit")

    def _parse(self, raw: bytes, *, request: dict[str, object]) -> SamannHostObservation:
        captured_at = self._now().astimezone(UTC)
        digest = hashlib.sha256(raw).hexdigest()
        text = html.unescape(raw.decode("utf-8", errors="replace"))
        if not text.strip() or "urlerror" in text.lower():
            return SamannHostObservation(
                request=request,
                source_url=SAMANN_HOST_RATES_URL,
                captured_at=captured_at,
                response_sha256=digest,
                raw_response=raw,
                status="blocked_no_exact_rate",
            )
        room = self._first(text, r"(?:room_name|roomname|room-name)[^>:=]*[:=]\s*[\"']?([^\"'<]+)")
        currency = self._first(text, r"\b(USD|US\$|MVR)\b")
        amount = self._first(
            text, r"(?:total|stay|amount)[^0-9]{0,40}([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
        )
        if room is None or currency is None or amount is None:
            return SamannHostObservation(
                request=request,
                source_url=SAMANN_HOST_RATES_URL,
                captured_at=captured_at,
                response_sha256=digest,
                raw_response=raw,
                status="blocked_unparseable_exact_rate",
            )
        numeric = round(float(amount.replace(",", "")) * 100)
        return SamannHostObservation(
            request=request,
            source_url=SAMANN_HOST_RATES_URL,
            captured_at=captured_at,
            response_sha256=digest,
            raw_response=raw,
            status="usable_exact_rate",
            room_name=room.strip(),
            currency="USD" if currency == "US$" else currency,
            total_for_party_minor=numeric,
            taxes_included=bool(re.search(r"tax(?:es)?\s+included|inclusive|含税", text, re.I)),
            breakfast=bool(re.search(r"breakfast", text, re.I)),
            cancellation_policy=self._first(text, r"([^<]{0,100}cancel[^<]{0,180})"),
        )

    @staticmethod
    def _first(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else None

    def _persist(self, observation: SamannHostObservation) -> None:
        if self._observation_dir is None:
            return
        destination = Path(self._observation_dir).expanduser().resolve(strict=True)
        if destination.stat().st_mode & 0o077:
            raise ValueError("Samann Host observation directory must be private")
        raw_path = destination / f"samann-host-response-{observation.response_sha256}.html"
        if not raw_path.exists():
            raw_path.write_bytes(observation.raw_response)
            raw_path.chmod(0o600)
        envelope_path = destination / f"samann-host-observation-{observation.response_sha256}.json"
        envelope = {
            "schema_version": "tripchord.samann-host.observation.v1",
            "request": observation.request,
            "source_url": observation.source_url,
            "captured_at": observation.captured_at.isoformat(),
            "response_sha256": observation.response_sha256,
            "raw_response_file": raw_path.name,
            "status": observation.status,
            "room_name": observation.room_name,
            "currency": observation.currency,
            "total_for_party_minor": observation.total_for_party_minor,
            "taxes_included": observation.taxes_included,
            "breakfast": observation.breakfast,
            "cancellation_policy": observation.cancellation_policy,
        }
        envelope_path.write_text(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        envelope_path.chmod(0o600)
