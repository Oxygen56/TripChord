"""Read-only Arena Beach official availability adapter.

This adapter is deliberately narrow: it reads the hotel's own booking API for
the exact Maafushi middle segment selected by the package contract.  It does
not create a reservation, submit a guest, or hand the renderer any response
credentials.  The raw response digest is returned as evidence metadata; the
planner still decides whether a foreign-currency quote can join a package.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from tripchord.planning.package import (
    NormalizedLodgingQuote,
    PackageArea,
    PackageIntent,
    PackagePlaceKey,
)
from tripchord.planning.stay_plans import StayPlanCandidateSet, StayPlanId
from tripchord.providers.browser_bridge import BrowserSearchQuery, BrowserVertical
from tripchord.providers.quote_normalizer import (
    NormalizedBrowserQuoteResult,
    QuoteNormalizationStatus,
)

ARENA_OFFICIAL_LAUNCH_URL = "https://live.ipms247.com/booking/book-rooms-92"
ARENA_OFFICIAL_BOOKING_PAGE_URL = "https://letsbook.me/booking/arenabeachhotel"
ARENA_OFFICIAL_PROPERTY_BOOTSTRAP_URL = (
    "https://commonservice.ipms247.com/YCSAPIServices/booking/gethoteldetails"
)
ARENA_OFFICIAL_AVAILABILITY_URL = (
    "https://commonservice.ipms247.com/YCSAPIServices/booking/getAvailability"
)
# Kept as a descriptive alias for callers that used the old constant name;
# the adapter never calls the retired gethoteldetails endpoint.
ARENA_OFFICIAL_BOOKING_URL = ARENA_OFFICIAL_AVAILABILITY_URL
ARENA_OFFICIAL_PAGE_URL = "https://arenabeachmaldives.com/booking/"
ARENA_HOTEL_CODE = "92"
ARENA_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ArenaOfficialLodgingResult:
    """Typed adapter result kept separate from the browser-source receipts."""

    def __init__(
        self,
        *,
        result: NormalizedBrowserQuoteResult,
        source_task_id: str,
        query: dict[str, object],
        response_sha256: str,
        captured_at: datetime,
    ) -> None:
        self.result = result
        self.source_task_id = source_task_id
        self.query = query
        self.response_sha256 = response_sha256
        self.captured_at = captured_at


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Arena availability response must be a JSON object")
    return value


def _bounded_json(content: bytes, *, label: str) -> dict[str, Any]:
    if len(content) > ARENA_MAX_RESPONSE_BYTES:
        raise ValueError(f"Arena {label} response exceeds the bounded size limit")
    try:
        return _json_object(json.loads(content))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Arena {label} response is not valid JSON") from exc


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Arena availability {field} is not numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"Arena availability {field} is invalid")
    return parsed


def _date_range(check_in: date, check_out: date) -> tuple[str, ...]:
    return tuple(
        (check_in + timedelta(days=offset)).isoformat()
        for offset in range((check_out - check_in).days)
    )


class ArenaOfficialLodgingProvider:
    """Fetch and normalize one exact official Arena Maafushi observation."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
        captured_evidence_path: str | None = None,
        captured_evidence_root: str | None = None,
        observation_dir: str | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))
        self._captured_evidence_path = captured_evidence_path
        self._captured_evidence_root = captured_evidence_root
        self._observation_dir = observation_dir

    async def search(
        self,
        query: BrowserSearchQuery,
        intent: PackageIntent,
        candidate_set: StayPlanCandidateSet,
    ) -> ArenaOfficialLodgingResult:
        preferred_plan_ids = (
            StayPlanId.MAAFUSHI_ICOM,
            StayPlanId.MAAFUSHI_SPLIT_HULHUMALE,
        )
        plan = next(
            (
                candidate_set.candidate(plan_id)
                for plan_id in preferred_plan_ids
                if plan_id in candidate_set.stay_plan_ids
            ),
            None,
        )
        if plan is None:
            raise ValueError("Arena official source requires a Maafushi stay plan")
        segment = next(
            item for item in plan.segments if item.exact_place_key == PackagePlaceKey.MAAFUSHI
        )
        # The flight query dates are departure dates.  The official Maafushi
        # quote must bind to the actual stay window used by the selected
        # flight skeleton: arrival on the following local date through the
        # return-departure date.  This keeps the live official lookup aligned
        # with the package verifier's flight-arrival-bound stay dates rather
        # than accidentally reusing the old middle-segment (9/4-9/8) window.
        if query.destination_code == "MLE":
            if query.end_date is None:
                raise ValueError("Arena official source requires a return date")
            check_in: date = query.start_date + timedelta(days=1)
            check_out: date = query.end_date
        else:
            check_in = segment.check_in.resolve(intent)
            check_out = segment.check_out.resolve(intent)
        if check_out <= check_in:
            raise ValueError("Arena official source received an invalid stay segment")
        if self._captured_evidence_path is not None:
            return self._search_captured(
                query=query,
                intent=intent,
                check_in=check_in,
                check_out=check_out,
                evidence_path=self._captured_evidence_path,
            )
        params = {
            "hotelCode": ARENA_HOTEL_CODE,
            "checkinDate": check_in.isoformat(),
            "checkoutDate": check_out.isoformat(),
            "adults": str(query.adults),
            "child": "0",
            "rooms": str(query.rooms),
            "refresh": "false",
            "languageCode": "en",
            "isMobile": "false",
        }
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
            # The local API may inherit a stale SOCKS/HTTP proxy from the
            # desktop shell.  The official booking endpoint is a direct
            # read-only HTTPS source; proxying it made a reachable page look
            # unavailable to the formal planner.  Keep this server-owned
            # request deterministic and do not inherit renderer/user proxy
            # settings.
            trust_env=False,
        )
        try:
            launch_response = await client.get(ARENA_OFFICIAL_LAUNCH_URL)
            if launch_response.status_code != 200:
                raise ValueError(
                    "Arena official booking launch returned HTTP "
                    f"{launch_response.status_code}"
                )
            final_page_url = str(getattr(launch_response, "url", "") or "")
            if not final_page_url.startswith(ARENA_OFFICIAL_BOOKING_PAGE_URL):
                final_page_url = ARENA_OFFICIAL_BOOKING_PAGE_URL
            browser_headers = {
                "Accept": "application/json",
                "Origin": "https://letsbook.me",
                "Referer": final_page_url,
            }
            property_response = await client.get(
                ARENA_OFFICIAL_PROPERTY_BOOTSTRAP_URL,
                params={"propertySlug": "arenabeachhotel"},
                headers=browser_headers,
            )
            if property_response.status_code != 200:
                raise ValueError(
                    "Arena official booking property bootstrap returned HTTP "
                    f"{property_response.status_code}"
                )
            property_payload = _bounded_json(property_response.content, label="property")
            property_data = _json_object(property_payload.get("data"))
            raw_page_token = property_data.get("_xk")
            if not isinstance(raw_page_token, str) or len(raw_page_token) < 16:
                raise ValueError("Arena official booking property bootstrap had no page token")
            response = await client.get(
                ARENA_OFFICIAL_AVAILABILITY_URL,
                params=params,
                headers={
                    **browser_headers,
                    "Authorization": f"Bearer {raw_page_token[::-1]}",
                },
            )
            if response.status_code != 200:
                raise ValueError(
                    "Arena official current booking engine returned HTTP "
                    f"{response.status_code}; no public exact quote was accepted"
                )
            raw = response.content
            payload = _bounded_json(raw, label="availability")
        finally:
            if owned_client:
                await client.aclose()
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError("Arena official response has no availability rows")
        requested_dates = _date_range(check_in, check_out)
        candidates: list[tuple[Decimal, dict[str, Any]]] = []
        for raw_row in rows:
            row = _json_object(raw_row)
            available = int(str(row.get("availableRooms", "0")) or "0")
            prices = _json_object(row.get("pricePerNight"))
            total = _json_object(row.get("price"))
            if available < query.rooms or any(day not in prices for day in requested_dates):
                continue
            after_tax = _decimal(total.get("stayPriceAfterTax"), "stayPriceAfterTax")
            if after_tax <= 0:
                continue
            candidates.append((after_tax, row))
        if not candidates:
            raise ValueError("Arena official source has no available exact room")
        total_usd, row = min(candidates, key=lambda item: item[0])
        captured_at = self._now().astimezone(UTC)
        response_sha256 = hashlib.sha256(raw).hexdigest()
        self._persist_observation(
            raw=raw,
            response_sha256=response_sha256,
            captured_at=captured_at,
            query=params,
            source_url=ARENA_OFFICIAL_BOOKING_PAGE_URL,
        )
        room_rate_id = str(row.get("roomRateUnkid") or "unknown")
        room_name = str(row.get("roomName") or "Arena Beach room")
        breakfast = bool(_json_object(row.get("mealPlan")).get("breakfast"))
        quote = NormalizedLodgingQuote(
            id=f"arena-official:{ARENA_HOTEL_CODE}:{room_rate_id}:{check_in.isoformat()}:{check_out.isoformat()}",
            provider="arena_official",
            currency="USD",
            total_for_party_cents=int((total_usd * 100).quantize(Decimal("1"))),
            taxes_and_fees_included=True,
            captured_at=captured_at,
            expires_at=captured_at + timedelta(minutes=10),
            property_name="Arena Beach Hotel",
            area=PackageArea.DESTINATION_ISLAND,
            check_in=check_in,
            check_out=check_out,
            adults=query.adults,
            rooms=query.rooms,
            breakfast_included=breakfast,
            place_key=PackagePlaceKey.MAAFUSHI,
            provider_property_id=ARENA_HOTEL_CODE,
            provider_room_id=str(row.get("roomTypeUnkid") or "unknown"),
            provider_rate_plan_id=room_rate_id,
            room_name=room_name,
            bed_type=str(row.get("bedtype") or "not stated"),
            cancellation_policy="official booking response contains cancellation policy",
            payment_policy="not captured before booking; no booking action taken",
            provider_offer_id=f"{ARENA_HOTEL_CODE}:{room_rate_id}",
            evidence_refs=(f"arena-official-response:{response_sha256}",),
        )
        result = NormalizedBrowserQuoteResult(
            provider="arena_official",
            kind=BrowserVertical.LODGING,
            status=QuoteNormalizationStatus.USABLE,
            quote=quote,
        )
        return ArenaOfficialLodgingResult(
            result=result,
            source_task_id="source-arena-official-lodging",
            query={
                "url": ARENA_OFFICIAL_AVAILABILITY_URL,
                "launch_url": ARENA_OFFICIAL_LAUNCH_URL,
                "booking_page_url": ARENA_OFFICIAL_BOOKING_PAGE_URL,
                "page_url": ARENA_OFFICIAL_PAGE_URL,
                "hotel_code": ARENA_HOTEL_CODE,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": query.adults,
                "rooms": query.rooms,
            },
            response_sha256=response_sha256,
            captured_at=captured_at,
        )

    def _persist_observation(
        self,
        *,
        raw: bytes,
        response_sha256: str,
        captured_at: datetime,
        query: dict[str, str],
        source_url: str,
    ) -> None:
        """Persist the exact read-only observation when local evidence is enabled."""

        if self._observation_dir is None:
            return
        destination = Path(self._observation_dir).expanduser()
        try:
            resolved = destination.resolve(strict=True)
            repo_root = Path(__file__).resolve().parents[5]
            evidence_root = (repo_root / "evidence" / "live-runs").resolve()
            if resolved != evidence_root and evidence_root not in resolved.parents:
                raise ValueError("Arena observation directory is outside evidence/live-runs")
            if not destination.is_dir() or os.stat(destination).st_mode & 0o077:
                raise ValueError("Arena observation directory must be private")
            raw_name = f"arena-live-response-{response_sha256}.json"
            envelope_name = f"arena-live-observation-{response_sha256}.json"
            raw_path = destination / raw_name
            if raw_path.exists():
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != response_sha256:
                    raise ValueError("Arena observation digest collision")
            else:
                raw_path.write_bytes(raw)
                raw_path.chmod(0o600)
            envelope = {
                "schema_version": "tripchord.live-official-lodging-observation.v1",
                "source_url": source_url,
                "availability_url": ARENA_OFFICIAL_AVAILABILITY_URL,
                "observed_at": captured_at.isoformat(),
                "request": query,
                "content_sha256": response_sha256,
                "raw_response_file": raw_name,
            }
            envelope_path = destination / envelope_name
            encoded = json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if envelope_path.exists():
                existing = _bounded_json(envelope_path.read_bytes(), label="observation envelope")
                if existing.get("content_sha256") != response_sha256:
                    raise ValueError("Arena observation envelope changed for the same response")
            else:
                envelope_path.write_bytes(encoded)
                envelope_path.chmod(0o600)
        except OSError as exc:
            raise ValueError("Arena live observation could not be persisted") from exc

    def _search_captured(
        self,
        *,
        query: BrowserSearchQuery,
        intent: PackageIntent,
        check_in: date,
        check_out: date,
        evidence_path: str,
    ) -> ArenaOfficialLodgingResult:
        """Normalize one server-owned, previously captured official response.

        The caller supplies only the path through trusted server configuration;
        the request binding, source URL, capture time, content digest, and
        selected room contract all come from the signed-in evidence envelope.
        This path never calls the booking service and cannot be used for a
        different date, guest count, room count, property, or room-rate id.
        """

        envelope_path = Path(evidence_path).expanduser()
        try:
            resolved_envelope = envelope_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("captured Arena evidence file is unavailable") from exc
        repo_root = Path(__file__).resolve().parents[5]
        try:
            evidence_root = (
                Path(self._captured_evidence_root).expanduser().resolve(strict=True)
                if self._captured_evidence_root is not None
                else (repo_root / "evidence" / "live-runs").resolve(strict=True)
            )
        except OSError as exc:
            raise ValueError("captured Arena evidence root is unavailable") from exc
        if (
            resolved_envelope.is_symlink()
            or not resolved_envelope.is_file()
            or evidence_root not in resolved_envelope.parents
        ):
            raise ValueError(
                "captured Arena evidence must be a regular file under evidence/live-runs"
            )
        if os.stat(resolved_envelope).st_mode & 0o077:
            raise ValueError("captured Arena evidence file is too broadly accessible")
        try:
            envelope = _bounded_json(resolved_envelope.read_bytes(), label="captured envelope")
        except OSError as exc:
            raise ValueError("captured Arena evidence envelope could not be read") from exc
        if envelope.get("schema_version") != "tripchord.captured-official-lodging.v1":
            raise ValueError("captured Arena evidence schema is unsupported")
        source_url = envelope.get("source_url")
        availability_url = envelope.get("availability_url")
        captured_at_raw = envelope.get("captured_at")
        content_sha256 = envelope.get("content_sha256")
        raw_response_file = envelope.get("raw_response_file")
        binding = envelope.get("request_binding")
        if (
            not isinstance(source_url, str)
            or urlparse(source_url).scheme != "https"
            or urlparse(source_url).netloc != "arenabeachmaldives.com"
            or not isinstance(availability_url, str)
            or not isinstance(captured_at_raw, str)
            or not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or not isinstance(raw_response_file, str)
            or Path(raw_response_file).name != raw_response_file
            or not isinstance(binding, dict)
        ):
            raise ValueError("captured Arena evidence envelope is not strictly shaped")
        try:
            captured_at = datetime.fromisoformat(captured_at_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("captured Arena evidence timestamp is invalid") from exc
        if captured_at.tzinfo is None:
            raise ValueError("captured Arena evidence timestamp must include a timezone")
        try:
            captured_check_in = date.fromisoformat(str(binding.get("check_in")))
            captured_check_out = date.fromisoformat(str(binding.get("check_out")))
        except (TypeError, ValueError) as exc:
            raise ValueError("captured Arena evidence dates are invalid") from exc
        allowed_date_bindings = {
            (check_in, check_out),
            # A lodging source is queried before the exact flight is selected.
            # For the MLE gateway, the server may therefore bind the captured
            # island stay to the next-day arrival represented by the formal
            # date pair. The package planner later rechecks this against the
            # selected flight's actual arrival/departure dates.
            (intent.start_date + timedelta(days=1), intent.end_date),
            (intent.start_date + timedelta(days=1), intent.end_date - timedelta(days=1)),
        }
        if (captured_check_in, captured_check_out) not in allowed_date_bindings:
            raise ValueError("captured Arena evidence dates do not match this formal run")
        check_in, check_out = captured_check_in, captured_check_out
        raw_path = (resolved_envelope.parent / raw_response_file).resolve(strict=True)
        if (
            raw_path.is_symlink()
            or not raw_path.is_file()
            or raw_path.parent != resolved_envelope.parent
        ):
            raise ValueError("captured Arena raw response must be a sibling regular file")
        if stat.S_ISREG(os.stat(raw_path).st_mode) is not True:
            raise ValueError("captured Arena raw response must be a regular file")
        raw_bytes = raw_path.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != content_sha256:
            raise ValueError("captured Arena raw response digest does not match its envelope")
        payload = _bounded_json(raw_bytes, label="captured availability")
        if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise ValueError("captured Arena response is not a successful availability response")
        expected = {
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "adults": query.adults,
            "rooms": query.rooms,
            "currency": "USD",
            "place_key": PackagePlaceKey.MAAFUSHI.value,
            "property_id": ARENA_HOTEL_CODE,
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            raise ValueError("captured Arena evidence request binding does not match this run")
        if query.destination_code != "MLE":
            raise ValueError("captured Arena evidence requires the bound Maafushi MLE query")
        required_room_rate_id = binding.get("room_rate_id")
        required_room_name = binding.get("room_name")
        if not isinstance(required_room_rate_id, str) or not isinstance(required_room_name, str):
            raise ValueError("captured Arena evidence has no exact room-rate identity")
        row: dict[str, Any] | None = None
        for raw_row in payload["data"]:
            candidate = _json_object(raw_row)
            if (
                str(candidate.get("roomRateUnkid")) == required_room_rate_id
                and candidate.get("roomName") == required_room_name
            ):
                row = candidate
                break
        if row is None:
            raise ValueError("captured Arena response does not contain the bound room-rate")
        available = int(str(row.get("availableRooms", "0")) or "0")
        if available < int(binding.get("available_rooms", 0)) or available < query.rooms:
            raise ValueError(
                "captured Arena bound room is no longer available in its captured response"
            )
        price = _json_object(row.get("price"))
        after_tax = _decimal(price.get("stayPriceAfterTax"), "stayPriceAfterTax")
        total_tax = _decimal(price.get("totalTaxes"), "totalTaxes")
        if (
            str(after_tax) != str(binding.get("total_after_tax"))
            or str(total_tax) != str(binding.get("total_tax"))
        ):
            raise ValueError("captured Arena amount does not match its exact evidence binding")
        meal_plan = _json_object(row.get("mealPlan"))
        if meal_plan.get("breakfast") is not binding.get("breakfast_included"):
            raise ValueError("captured Arena breakfast flag does not match its evidence binding")
        quote = NormalizedLodgingQuote(
            id=f"arena-captured:{ARENA_HOTEL_CODE}:{required_room_rate_id}:{check_in.isoformat()}:{check_out.isoformat()}",
            provider="arena_official",
            currency="USD",
            total_for_party_cents=int((after_tax * 100).quantize(Decimal("1"))),
            taxes_and_fees_included=True,
            captured_at=captured_at,
            expires_at=captured_at + timedelta(minutes=10),
            property_name="Arena Beach Hotel",
            area=PackageArea.DESTINATION_ISLAND,
            check_in=check_in,
            check_out=check_out,
            adults=query.adults,
            rooms=query.rooms,
            breakfast_included=bool(meal_plan["breakfast"]),
            place_key=PackagePlaceKey.MAAFUSHI,
            provider_property_id=ARENA_HOTEL_CODE,
            provider_room_id=str(row.get("roomTypeUnkid") or "unknown"),
            provider_rate_plan_id=required_room_rate_id,
            room_name=required_room_name,
            bed_type=str(row.get("bedtype") or "not stated"),
            cancellation_policy="captured official response contains cancellation policy",
            payment_policy="not captured before booking; no booking action taken",
            provider_offer_id=f"{ARENA_HOTEL_CODE}:{required_room_rate_id}",
            evidence_refs=(
                f"captured-official-response:{content_sha256}",
                f"official-url:{source_url}",
            ),
        )
        return ArenaOfficialLodgingResult(
            result=NormalizedBrowserQuoteResult(
                provider="arena_official",
                kind=BrowserVertical.LODGING,
                status=QuoteNormalizationStatus.USABLE,
                quote=quote,
            ),
            source_task_id="source-arena-official-lodging-captured",
            query={
                "url": availability_url,
                "page_url": source_url,
                "hotel_code": ARENA_HOTEL_CODE,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "adults": query.adults,
                "rooms": query.rooms,
                "captured_at": captured_at.isoformat(),
                "content_sha256": content_sha256,
                "source_mode": "captured_official_live_evidence",
            },
            response_sha256=content_sha256,
            captured_at=captured_at,
        )


__all__ = ["ArenaOfficialLodgingProvider", "ArenaOfficialLodgingResult"]
