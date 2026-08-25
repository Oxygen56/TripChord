"""Read-only current iCom excursion observations for the complex catalog.

The public iCom API exposes a dated trip schedule and a published USD/MVR
activity price.  This adapter deliberately keeps the source currency and
tax boundary on the resulting contract; an ECB conversion is only a display
reference and can never make the activity execution-ready.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from tripchord.planning.complex_trip import (
    ActivityOffer,
    PriceContract,
    SourceState,
    SourceStatus,
    TravelIntent,
)
from tripchord.providers.base import ProviderError
from tripchord.providers.fx_reference import (
    UsdCnyReferenceRate,
    fetch_usd_cny_reference_rate,
)

_API_BASE = "https://sfs-api.icomtours.com/api/v1"
_EXCURSIONS_URL = f"{_API_BASE}/public/excursions"
_MVT = timezone(timedelta(hours=5), name="MVT")
_USER_AGENT = "TripChord/0.1 (+read-only iCom public excursion evidence)"


@dataclass(frozen=True, slots=True)
class IComActivityCatalogResult:
    activities: tuple[ActivityOffer, ...]
    contracts: tuple[PriceContract, ...]
    source_statuses: tuple[SourceStatus, ...]
    query_task_ids: tuple[str, ...]


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    return " ".join(value.split())


def _slug_score(slug: str, name: str, hint: str) -> int:
    haystack = f"{slug} {name}".lower()
    tokens = [item.lower() for item in re.findall(r"[A-Za-z\u4e00-\u9fff]{2,}", hint)]
    score = sum(2 for token in tokens if token in haystack)
    if "浮潜" in hint or "snorkel" in hint.lower():
        score += 4 if "fish" in haystack or "half-day" in haystack else 0
    return score


class IComCurrentActivitySource:
    """Fetch bounded, dated iCom activities without booking or payment calls."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_excursions: int = 4,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._client = client
        self._max_excursions = max(1, max_excursions)
        self._timeout_seconds = timeout_seconds

    async def catalog_for(
        self,
        intent: TravelIntent,
        *,
        reference_rate: UsdCnyReferenceRate | None = None,
        fetch_reference: bool = True,
    ) -> IComActivityCatalogResult:
        requirements = intent.activity_requirements
        if not requirements:
            return IComActivityCatalogResult(
                activities=(), contracts=(), source_statuses=(), query_task_ids=()
            )
        if not any(
            "maafushi" in item.place_id.lower() or "马富施" in item.place_id
            for item in requirements
        ):
            captured_at = datetime.now(UTC)
            task_ids = tuple(f"icom:activity:{item.id}" for item in requirements)
            return IComActivityCatalogResult(
                activities=(),
                contracts=(),
                query_task_ids=task_ids,
                source_statuses=tuple(
                    SourceStatus(
                        source_id=f"icom:activity:{item.id}",
                        provider="icom-public-activity",
                        state=SourceState.NOT_QUERIED,
                        detail="iCom当前活动来源仅覆盖Maafushi",
                        query_task_ids=(f"icom:activity:{item.id}",),
                        captured_at=captured_at,
                    )
                    for item in requirements
                ),
            )
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds, connect=8.0),
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        try:
            if reference_rate is None and fetch_reference:
                try:
                    reference_rate = await fetch_usd_cny_reference_rate(client=client)
                except (ProviderError, httpx.HTTPError, ValueError, AttributeError):
                    reference_rate = None
            results = await asyncio.gather(
                *(
                    self._fetch_requirement(
                        client,
                        intent,
                        item,
                        reference_rate=reference_rate,
                    )
                    for item in requirements
                )
            )
        finally:
            if owned_client:
                await client.aclose()
        return IComActivityCatalogResult(
            activities=tuple(item for result in results for item in result[0]),
            contracts=tuple(item for result in results for item in result[1]),
            source_statuses=tuple(result[2] for result in results),
            query_task_ids=tuple(result[3] for result in results),
        )

    async def _fetch_requirement(
        self,
        client: httpx.AsyncClient,
        intent: TravelIntent,
        requirement: Any,
        *,
        reference_rate: Any | None,
    ) -> tuple[tuple[ActivityOffer, ...], tuple[PriceContract, ...], SourceStatus, str]:
        captured_at = datetime.now(UTC)
        task_id = f"icom:activity:{requirement.id}"
        try:
            listing_url = f"{_EXCURSIONS_URL}?page=1&perPage={self._max_excursions}"
            listing_response = await client.get(listing_url)
            listing_response.raise_for_status()
            listing = listing_response.json()
            rows = listing.get("data") if isinstance(listing, dict) else None
            if not isinstance(rows, list) or not rows:
                raise ValueError("iCom活动列表为空")
            candidates = sorted(
                (
                    item
                    for item in rows
                    if isinstance(item, dict)
                    and item.get("isPublished", True)
                    and isinstance(item.get("slug"), str)
                ),
                key=lambda item: (
                    -_slug_score(
                        str(item.get("slug", "")),
                        str(item.get("name", "")),
                        str(requirement.name_hint),
                    ),
                    str(item.get("slug", "")),
                ),
            )[: self._max_excursions]
            if not candidates:
                raise ValueError("iCom活动列表没有可用项目")
            detail_results = await asyncio.gather(
                *(
                    self._fetch_detail_and_trips(
                        client,
                        str(item["slug"]),
                        requirement.activity_date,
                    )
                    for item in candidates
                )
            )
            selected: tuple[ActivityOffer, ...] = ()
            contracts: tuple[PriceContract, ...] = ()
            selected_name = ""
            for candidate, (detail, trips) in zip(candidates, detail_results, strict=True):
                options, option_contracts = await self._normalize_options(
                    intent,
                        requirement,
                        candidate,
                        detail,
                        trips,
                        reference_rate=reference_rate,
                )
                if options:
                    selected = options
                    contracts = option_contracts
                    selected_name = str(candidate.get("name", candidate["slug"]))
                    break
            if not selected:
                raise ValueError("iCom活动当前日期没有至少两人可用班次")
            captured_at = datetime.now(UTC)
            status = SourceStatus(
                source_id=f"icom:activity:{requirement.id}",
                provider="icom-public-activity",
                state=SourceState.SUCCEEDED,
                detail=(
                    f"iCom官方当前活动 {selected_name} 已返回日期班次、时间和余位；"
                    "价格为公开USD基础价，ECB换算仅作人民币参考，税费/锁位未确认"
                ),
                query_task_ids=(task_id,),
                captured_at=captured_at,
            )
            return selected, contracts, status, task_id
        except (httpx.HTTPError, ValueError, TypeError, KeyError, InvalidOperation) as exc:
            captured_at = datetime.now(UTC)
            return (
                (),
                (),
                SourceStatus(
                    source_id=f"icom:activity:{requirement.id}",
                    provider="icom-public-activity",
                    state=SourceState.FAILED,
                    detail=f"iCom当前活动查询未形成完整班次:{type(exc).__name__}",
                    query_task_ids=(task_id,),
                    captured_at=captured_at,
                ),
                task_id,
            )

    async def _fetch_detail_and_trips(
        self,
        client: httpx.AsyncClient,
        slug: str,
        activity_date: date,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        detail_url = f"{_EXCURSIONS_URL}/{slug}"
        trips_url = (
            f"{_EXCURSIONS_URL}/{slug}/trips?"
            f"startDate={activity_date.isoformat()}&endDate={activity_date.isoformat()}"
        )
        detail_response, trips_response = await asyncio.gather(
            client.get(detail_url), client.get(trips_url)
        )
        detail_response.raise_for_status()
        trips_response.raise_for_status()
        detail_payload = detail_response.json()
        trips_payload = trips_response.json()
        detail = detail_payload.get("data") if isinstance(detail_payload, dict) else None
        trips = trips_payload.get("data") if isinstance(trips_payload, dict) else None
        if not isinstance(detail, dict) or not isinstance(trips, list):
            raise ValueError("iCom活动详情响应结构不完整")
        return detail, [item for item in trips if isinstance(item, dict)]

    async def _normalize_options(
        self,
        intent: TravelIntent,
        requirement: Any,
        candidate: dict[str, Any],
        detail: dict[str, Any],
        trips: list[dict[str, Any]],
        *,
        reference_rate: Any | None,
    ) -> tuple[tuple[ActivityOffer, ...], tuple[PriceContract, ...]]:
        prices = detail.get("prices")
        if not isinstance(prices, list):
            return (), ()
        usd_price = next(
            (
                Decimal(str(item.get("amount")))
                for item in prices
                if isinstance(item, dict)
                and isinstance(item.get("currency"), dict)
                and item["currency"].get("code") == "USD"
            ),
            None,
        )
        if usd_price is None or usd_price <= 0:
            return (), ()
        adults = len(requirement.participant_ids) or intent.travelers
        original_cents = int((usd_price * Decimal(100) * adults).quantize(Decimal("1")))
        detail_url = f"https://www.icomtours.com/excursions/{candidate['slug']}"
        offers: list[ActivityOffer] = []
        contracts: list[PriceContract] = []
        richtext = detail.get("richtext")
        description = ""
        if isinstance(richtext, dict):
            blocks = richtext.get("blocks")
            if isinstance(blocks, list):
                description = " ".join(
                    _text(item.get("data", {}).get("text"))
                    for item in blocks
                    if isinstance(item, dict) and isinstance(item.get("data"), dict)
                )
        for trip in trips:
            try:
                if trip.get("tripDate") != requirement.activity_date.isoformat():
                    continue
                remaining = int(trip.get("remainingCapacity", 0))
                if remaining < adults:
                    continue
                departure = datetime.combine(
                    requirement.activity_date,
                    time.fromisoformat(str(trip["departureTime"])),
                    tzinfo=_MVT,
                )
                arrival = datetime.combine(
                    requirement.activity_date,
                    time.fromisoformat(str(trip["arrivalTime"])),
                    tzinfo=_MVT,
                )
                if arrival <= departure:
                    continue
                trip_id = str(trip.get("id"))
                offer_id = f"icom:activity:{candidate['slug']}:{trip_id}:{adults}a"
                if reference_rate is None:
                    contract_id = f"unpriced:{offer_id}"
                    boundary = (
                        f"公开价USD {usd_price}/人 × {adults}；当前未取得可审计人民币汇率，"
                        "未计入整趟人民币总价"
                    )
                    cny_total = 0
                    price_basis = "unknown"
                else:
                    cny_total = int(
                        (Decimal(original_cents) * reference_rate.usd_to_cny).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    contract_id = f"reference:{offer_id}:{reference_rate.response_sha256[:12]}"
                    boundary = (
                        f"公开价USD {usd_price}/人 × {adults}，按ECB {reference_rate.rate_date} "
                        "参考汇率换算；不是交易汇率，税费和锁位未确认"
                    )
                    price_basis = "reference"
                contracts.append(
                    PriceContract(
                        id=contract_id,
                        currency="CNY",
                        total_for_party_cents=cny_total,
                        component_ids=(offer_id,),
                        covered_traveler_ids=_participant_ids(requirement, intent),
                        shared_between_travelers=adults > 1,
                        taxes_and_fees_included=False,
                        source="current:icom-public-activity",
                        price_basis=price_basis,
                        original_currency="USD",
                        original_total_for_party_cents=original_cents,
                        price_boundary=boundary,
                    )
                )
                offers.append(
                    ActivityOffer(
                        id=offer_id,
                        provider="icom-public-activity",
                        place_id=requirement.place_id,
                        start=departure,
                        end=arrival,
                        price_contract_id=contract_id,
                        detail_url=detail_url,
                        label=(
                            f"{requirement.name_hint or '在线活动'}："
                            f"{detail.get('name', candidate['slug'])}"
                            f"（Maafushi出发，{description[:90]}）"
                        ),
                        participant_ids=_participant_ids(requirement, intent),
                        party_capacity_confirmed=True,
                        available_units=remaining,
                        original_currency="USD",
                        original_price_for_party_cents=original_cents,
                        price_boundary=boundary,
                    )
                )
            except (TypeError, ValueError, InvalidOperation):
                continue
        return tuple(offers), tuple(contracts)


def _participant_ids(requirement: Any, intent: TravelIntent) -> tuple[str, ...]:
    if requirement.participant_ids:
        return tuple(requirement.participant_ids)
    if intent.traveler_profiles:
        return tuple(item.id for item in intent.traveler_profiles)
    return tuple(f"traveler:{index + 1}" for index in range(intent.travelers))


__all__ = ["IComActivityCatalogResult", "IComCurrentActivitySource"]
