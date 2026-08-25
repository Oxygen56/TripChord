"""Read-only current railway offers from the official 12306 query surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from tripchord.planning.complex_trip import (
    PriceContract,
    SourceState,
    SourceStatus,
    TransportOffer,
    TravelIntent,
    TripLegRequirement,
)

_RAIL_TIMEZONE = ZoneInfo("Asia/Shanghai")
_LEFT_TICKET_INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
_LEFT_TICKET_QUERY_URL = "https://kyfw.12306.cn/otn/leftTicket/query"
_TICKET_PRICE_URL = "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class RailStationIdentity:
    city_id: str
    city_name: str
    station_code: str
    station_name: str
    aliases: frozenset[str]


_RAIL_STATIONS = (
    RailStationIdentity(
        city_id="杭州",
        city_name="杭州",
        station_code="HGH",
        station_name="杭州东",
        aliases=frozenset({"杭州", "hangzhou", "hgh"}),
    ),
    RailStationIdentity(
        city_id="南京",
        city_name="南京",
        station_code="NKH",
        station_name="南京南",
        aliases=frozenset({"南京", "nanjing", "nkh"}),
    ),
    RailStationIdentity(
        city_id="上海",
        city_name="上海",
        station_code="AOH",
        station_name="上海虹桥",
        aliases=frozenset({"上海", "shanghai", "aoh"}),
    ),
)


@dataclass(frozen=True, slots=True)
class _RailLegSpec:
    requirement: TripLegRequirement
    travel_date: date
    origin: RailStationIdentity
    destination: RailStationIdentity

    @property
    def query_task_id(self) -> str:
        return (
            f"12306:{self.travel_date.isoformat()}:"
            f"{self.origin.station_code}-{self.destination.station_code}"
        )


@dataclass(frozen=True, slots=True)
class _RawTrainCandidate:
    train_no: str
    train_code: str
    origin_station_code: str
    destination_station_code: str
    departure_time: str
    arrival_time: str
    duration_text: str
    from_station_no: str
    to_station_no: str
    seat_types: str
    second_class_availability: str


@dataclass(frozen=True, slots=True)
class Rail12306CatalogResult:
    transports: tuple[TransportOffer, ...]
    contracts: tuple[PriceContract, ...]
    source_statuses: tuple[SourceStatus, ...]
    query_task_ids: tuple[str, ...]


def _normalized_alias(value: str) -> str:
    return re.sub(r"[\s\-_]", "", value.strip().lower())


def _station_identity(value: str) -> RailStationIdentity | None:
    normalized = _normalized_alias(value)
    return next(
        (
            identity
            for identity in _RAIL_STATIONS
            if normalized in {_normalized_alias(alias) for alias in identity.aliases}
        ),
        None,
    )


def _exact_departure_date(requirement: TripLegRequirement) -> date | None:
    if requirement.departure_date is not None:
        return requirement.departure_date
    if (
        requirement.earliest_departure_date is not None
        and requirement.earliest_departure_date == requirement.latest_departure_date
    ):
        return requirement.earliest_departure_date
    return None


def _bounded_even_sample[T](values: list[T], limit: int) -> tuple[T, ...]:
    if len(values) <= limit:
        return tuple(values)
    if limit == 1:
        return (values[0],)
    indexes = {
        round(index * (len(values) - 1) / (limit - 1))
        for index in range(limit)
    }
    return tuple(values[index] for index in sorted(indexes))


class Rail12306CurrentCatalogSource:
    """Fetch bounded, price-complete adult second-class rail candidates."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_candidates_per_leg: int = 8,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._max_candidates_per_leg = max_candidates_per_leg
        self._timeout_seconds = timeout_seconds

    async def catalog_for(self, intent: TravelIntent) -> Rail12306CatalogResult:
        prepared = self._prepare(intent)
        if isinstance(prepared, str):
            captured_at = datetime.now(UTC)
            return Rail12306CatalogResult(
                transports=(),
                contracts=(),
                source_statuses=(
                    SourceStatus(
                        source_id="12306:bounded-current",
                        provider="12306",
                        state=SourceState.NOT_QUERIED,
                        detail=prepared,
                        captured_at=captured_at,
                    ),
                ),
                query_task_ids=(),
            )

        headers = {"User-Agent": _USER_AGENT, "Referer": _LEFT_TICKET_INIT_URL}
        client = self._client or httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout_seconds, connect=10.0),
        )
        owns_client = self._client is None
        try:
            init_response = await client.get(_LEFT_TICKET_INIT_URL, headers=headers)
            init_response.raise_for_status()
            results = await asyncio.gather(
                *(self._fetch_leg(client, spec, intent.travelers) for spec in prepared)
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            captured_at = datetime.now(UTC)
            statuses = tuple(
                SourceStatus(
                    source_id=f"12306:rail:{spec.requirement.id}",
                    provider="12306",
                    state=SourceState.FAILED,
                    detail=f"12306当前余票查询失败:{type(exc).__name__}",
                    query_task_ids=(spec.query_task_id,),
                    captured_at=captured_at,
                )
                for spec in prepared
            )
            return Rail12306CatalogResult(
                transports=(),
                contracts=(),
                source_statuses=statuses,
                query_task_ids=tuple(spec.query_task_id for spec in prepared),
            )
        finally:
            if owns_client:
                await client.aclose()

        return Rail12306CatalogResult(
            transports=tuple(
                offer for offers, _, _ in results for offer in offers
            ),
            contracts=tuple(
                contract for _, contracts, _ in results for contract in contracts
            ),
            source_statuses=tuple(status for _, _, status in results),
            query_task_ids=tuple(spec.query_task_id for spec in prepared),
        )

    def _prepare(self, intent: TravelIntent) -> tuple[_RailLegSpec, ...] | str:
        if intent.travelers > 9:
            return "12306当前合同不支持9人以上同行"
        specs: list[_RailLegSpec] = []
        for requirement in intent.route_legs:
            travel_date = _exact_departure_date(requirement)
            origin = _station_identity(requirement.origin_place_id)
            destination = _station_identity(requirement.destination_place_id)
            if travel_date is None:
                return "12306当前查询需要每段确切出发日期"
            if origin is None or destination is None:
                return "12306当前参考路线尚不支持某个城市"
            specs.append(
                _RailLegSpec(
                    requirement=requirement,
                    travel_date=travel_date,
                    origin=origin,
                    destination=destination,
                )
            )
        return tuple(specs)

    async def _fetch_leg(
        self,
        client: httpx.AsyncClient,
        spec: _RailLegSpec,
        travelers: int,
    ) -> tuple[
        tuple[TransportOffer, ...],
        tuple[PriceContract, ...],
        SourceStatus,
    ]:
        captured_at = datetime.now(UTC)
        try:
            query_params = {
                "leftTicketDTO.train_date": spec.travel_date.isoformat(),
                "leftTicketDTO.from_station": spec.origin.station_code,
                "leftTicketDTO.to_station": spec.destination.station_code,
                "purpose_codes": "ADULT",
            }
            response = await client.get(_LEFT_TICKET_QUERY_URL, params=query_params)
            response.raise_for_status()
            self._validate_official_response(response, "/otn/leftTicket/query")
            payload = response.json()
            if (
                isinstance(payload, dict)
                and payload.get("status") is False
                and isinstance(payload.get("c_url"), str)
            ):
                current_path = str(payload["c_url"])
                if re.fullmatch(r"leftTicket/query[A-Z]", current_path) is None:
                    raise ValueError("12306 returned an invalid current query path")
                response = await client.get(
                    f"https://kyfw.12306.cn/otn/{current_path}",
                    params=query_params,
                )
                response.raise_for_status()
                self._validate_official_response(
                    response,
                    f"/otn/{current_path}",
                )
                payload = response.json()
            captured_at = datetime.now(UTC)
            rows = self._parse_available_rows(payload, spec, travelers)
            bounded = _bounded_even_sample(rows, self._max_candidates_per_leg)
            semaphore = asyncio.Semaphore(6)

            async def priced(
                candidate: _RawTrainCandidate,
            ) -> tuple[TransportOffer, PriceContract] | None:
                async with semaphore:
                    return await self._price_candidate(
                        client,
                        spec,
                        candidate,
                        travelers,
                        captured_at,
                    )

            priced_candidates = await asyncio.gather(*(priced(item) for item in bounded))
            complete = tuple(item for item in priced_candidates if item is not None)
            offers = tuple(item[0] for item in complete)
            contracts = tuple(item[1] for item in complete)
            state = SourceState.SUCCEEDED if offers else SourceState.FAILED
            detail = (
                f"12306官方当前余票和二等座票价已返回；"
                f"有界查询{len(bounded)}个车次，得到{len(offers)}个"
                f"{travelers}人人民币合计，未锁票"
                if offers
                else "12306当前页面未形成有余票且票价完整的二等座报价"
            )
            return (
                offers,
                contracts,
                SourceStatus(
                    source_id=f"12306:rail:{spec.requirement.id}",
                    provider="12306",
                    state=state,
                    detail=detail,
                    query_task_ids=(spec.query_task_id,),
                    captured_at=captured_at,
                ),
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return (
                (),
                (),
                SourceStatus(
                    source_id=f"12306:rail:{spec.requirement.id}",
                    provider="12306",
                    state=SourceState.FAILED,
                    detail=f"12306当前余票或票价查询失败:{type(exc).__name__}",
                    query_task_ids=(spec.query_task_id,),
                    captured_at=captured_at,
                ),
            )

    @staticmethod
    def _validate_official_response(response: httpx.Response, expected_prefix: str) -> None:
        url = response.url
        if (
            url.scheme != "https"
            or url.host != "kyfw.12306.cn"
            or not url.path.startswith(expected_prefix)
        ):
            raise ValueError("12306 response left the official query surface")

    @staticmethod
    def _parse_available_rows(
        payload: object,
        spec: _RailLegSpec,
        travelers: int,
    ) -> list[_RawTrainCandidate]:
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise ValueError("12306 left-ticket response is not successful")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("12306 left-ticket data is missing")
        station_map = data.get("map")
        results = data.get("result")
        if not isinstance(station_map, dict) or not isinstance(results, list):
            raise ValueError("12306 left-ticket result shape changed")
        if (
            station_map.get(spec.origin.station_code) != spec.origin.station_name
            or station_map.get(spec.destination.station_code)
            != spec.destination.station_name
        ):
            raise ValueError("12306 station identity readback mismatch")

        candidates: list[_RawTrainCandidate] = []
        for raw in results:
            if not isinstance(raw, str):
                continue
            fields = raw.split("|")
            if len(fields) < 36:
                continue
            availability = fields[30]
            # 12306's generic ``有`` marker does not prove that the requested
            # party can travel together.  Only a numeric inventory count can
            # close the capacity contract for a publishable plan.
            enough_seats = availability.isdigit() and int(availability) >= travelers
            if (
                fields[11] != "Y"
                or fields[6] != spec.origin.station_code
                or fields[7] != spec.destination.station_code
                or not re.fullmatch(r"[GDC]\d+", fields[3])
                or "O" not in fields[35]
                or not enough_seats
                or not re.fullmatch(r"\d{2}:\d{2}", fields[8])
                or not re.fullmatch(r"\d{2}:\d{2}", fields[9])
                or not re.fullmatch(r"\d{2}:\d{2}", fields[10])
            ):
                continue
            candidates.append(
                _RawTrainCandidate(
                    train_no=fields[2],
                    train_code=fields[3],
                    origin_station_code=fields[6],
                    destination_station_code=fields[7],
                    departure_time=fields[8],
                    arrival_time=fields[9],
                    duration_text=fields[10],
                    from_station_no=fields[16],
                    to_station_no=fields[17],
                    seat_types=fields[35],
                    second_class_availability=availability,
                )
            )
        candidates.sort(key=lambda item: (item.departure_time, item.train_code))
        return candidates

    async def _price_candidate(
        self,
        client: httpx.AsyncClient,
        spec: _RailLegSpec,
        candidate: _RawTrainCandidate,
        travelers: int,
        captured_at: datetime,
    ) -> tuple[TransportOffer, PriceContract] | None:
        try:
            response = await client.get(
                _TICKET_PRICE_URL,
                params={
                    "train_no": candidate.train_no,
                    "from_station_no": candidate.from_station_no,
                    "to_station_no": candidate.to_station_no,
                    "seat_types": candidate.seat_types,
                    "train_date": spec.travel_date.isoformat(),
                },
            )
            response.raise_for_status()
            self._validate_official_response(
                response, "/otn/leftTicket/queryTicketPrice"
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            # One train's price surface may drift independently.  Keep the
            # other complete current offers instead of failing the whole leg.
            return None
        if not isinstance(payload, dict) or payload.get("status") is not True:
            return None
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("train_no") != candidate.train_no:
            return None
        raw_price = data.get("O")
        if not isinstance(raw_price, str):
            return None
        match = re.fullmatch(r"[\u00a5￥](\d+(?:\.\d{1,2})?)", raw_price.strip())
        if match is None:
            return None
        try:
            price_cents_decimal = Decimal(match.group(1)) * Decimal(100)
        except InvalidOperation:
            return None
        price_cents_integral = price_cents_decimal.to_integral_value()
        if price_cents_decimal != price_cents_integral or price_cents_integral <= 0:
            return None
        per_person_cents = int(price_cents_integral)
        departure = datetime.combine(
            spec.travel_date,
            datetime.strptime(candidate.departure_time, "%H:%M").time(),
            tzinfo=_RAIL_TIMEZONE,
        )
        duration_hours, duration_minutes = (
            int(value) for value in candidate.duration_text.split(":", maxsplit=1)
        )
        arrival = departure + timedelta(hours=duration_hours, minutes=duration_minutes)
        detail_url = self._search_result_url(spec)
        digest = hashlib.sha256(
            (
                f"{spec.requirement.id}|{candidate.train_no}|{candidate.train_code}|"
                f"{departure.isoformat()}|{arrival.isoformat()}|{per_person_cents}"
            ).encode()
        ).hexdigest()[:20]
        offer_id = f"complex:12306:rail:{digest}"
        contract_id = f"{offer_id}:contract"
        party_total_cents = per_person_cents * travelers
        offer = TransportOffer(
            id=offer_id,
            provider="12306",
            origin_place_id=spec.requirement.origin_place_id,
            destination_place_id=spec.requirement.destination_place_id,
            departure=departure,
            arrival=arrival,
            price_contract_id=contract_id,
            detail_url=detail_url,
            label=(
                f"{candidate.train_code} 二等座｜{spec.origin.station_name} "
                f"{candidate.departure_time} → {spec.destination.station_name} "
                f"{candidate.arrival_time}｜{travelers}人合计"
                f"¥{Decimal(party_total_cents) / Decimal(100):,.2f}｜"
                f"查询时二等座余{candidate.second_class_availability}张"
            ),
            party_capacity_confirmed=True,
            available_units=int(candidate.second_class_availability),
        )
        contract = PriceContract(
            id=contract_id,
            total_for_party_cents=party_total_cents,
            component_ids=(offer_id,),
            shared=False,
            taxes_and_fees_included=True,
            source=(
                "current:12306:official-left-ticket+ticket-price:"
                f"{captured_at.isoformat()}"
            ),
        )
        return offer, contract

    @staticmethod
    def _search_result_url(spec: _RailLegSpec) -> str:
        query = urlencode(
            {
                "linktypeid": "dc",
                "fs": f"{spec.origin.station_name},{spec.origin.station_code}",
                "ts": f"{spec.destination.station_name},{spec.destination.station_code}",
                "date": spec.travel_date.isoformat(),
                "flag": "N,N,Y",
            }
        )
        return f"{_LEFT_TICKET_INIT_URL}?{query}"
