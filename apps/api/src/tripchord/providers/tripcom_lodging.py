"""Read-only current lodging offers from Trip.com's public SSR hotel lists."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from tripchord.planning.complex_trip import (
    PriceContract,
    SourceState,
    SourceStatus,
    StayOffer,
    TravelIntent,
    TripLegRequirement,
)

_TRIPCOM_HOTEL_LIST_URL = "https://www.trip.com/hotels/list"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
)
_MAX_TRAVELERS = 8
_TOTAL_PRICE_PATTERN = re.compile(
    r"^Total price: CNY "
    r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?) "
    r"1 room × (?P<nights>\d+) nights incl\. taxes & fees$"
)


@dataclass(frozen=True, slots=True)
class TripComCityIdentity:
    city_id: int
    city_name: str
    city_name_en: str
    aliases: frozenset[str]


_TRIPCOM_CITIES = (
    TripComCityIdentity(
        city_id=12,
        city_name="南京",
        city_name_en="Nanjing",
        aliases=frozenset({"南京", "nanjing"}),
    ),
    TripComCityIdentity(
        city_id=2,
        city_name="上海",
        city_name_en="Shanghai",
        aliases=frozenset({"上海", "shanghai"}),
    ),
)


@dataclass(frozen=True, slots=True)
class _StaySpec:
    place_id: str
    city: TripComCityIdentity
    check_in: date
    check_out: date
    adults: int
    rooms: int = 1
    participant_ids: tuple[str, ...] = ()

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def query_parameters(self) -> tuple[tuple[str, str], ...]:
        return (
            ("city", str(self.city.city_id)),
            ("checkin", self.check_in.isoformat()),
            ("checkout", self.check_out.isoformat()),
            ("adult", str(self.adults)),
            ("children", "0"),
            ("crn", str(self.rooms)),
            ("curr", "CNY"),
        )

    @property
    def query_url(self) -> str:
        return f"{_TRIPCOM_HOTEL_LIST_URL}?{urlencode(self.query_parameters)}"

    @property
    def query_task_id(self) -> str:
        base = (
            f"trip.com:hotel:{self.city.city_id}:"
            f"{self.check_in.isoformat()}:{self.check_out.isoformat()}:"
            f"{self.adults}a"
        )
        participant_scope = ",".join(self.participant_ids)
        return (
            f"{base}:{self.rooms}r:{participant_scope}"
            if participant_scope
            else base
        )


@dataclass(frozen=True, slots=True)
class _HotelCard:
    hotel_id: str
    hotel_name: str
    room_name: str
    adult_icons: int
    price_explain: str


@dataclass(slots=True)
class _MutableHotelCard:
    hotel_id: str
    hotel_name: list[str] = field(default_factory=list)
    room_name: list[str] = field(default_factory=list)
    price_explain: list[str] = field(default_factory=list)
    adult_icons: int = 0


class _HotelCardParser(HTMLParser):
    """Extract only the visible fields carried by each SSR hotel card."""

    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_HotelCard] = []
        self._card: _MutableHotelCard | None = None
        self._div_depth = 0
        self._open_tag_counts: dict[str, int] = {}
        self._capture_scopes: list[tuple[str, str, int]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if self._card is None:
            hotel_id = attributes.get("id") or ""
            if tag != "div" or "hotel-card" not in classes or not hotel_id.isdigit():
                return
            self._card = _MutableHotelCard(hotel_id=hotel_id)
            self._div_depth = 1
            self._open_tag_counts = {"div": 1}
            self._capture_scopes = []
            return

        if tag not in self._VOID_TAGS:
            self._open_tag_counts[tag] = self._open_tag_counts.get(tag, 0) + 1
        if tag == "div":
            self._div_depth += 1

        target: str | None = None
        if "hotelName" in classes:
            target = "hotel_name"
        elif "room-name" in classes:
            target = "room_name"
        elif "price-explain" in classes:
            target = "price_explain"
        if target is not None and tag not in self._VOID_TAGS:
            self._capture_scopes.append((target, tag, self._open_tag_counts[tag]))

        if tag == "i" and {"ic_adult", "people-icon"}.issubset(classes):
            self._card.adult_icons += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if self._card is None:
            return
        classes = frozenset((dict(attrs).get("class") or "").split())
        if tag == "i" and {"ic_adult", "people-icon"}.issubset(classes):
            self._card.adult_icons += 1

    def handle_data(self, data: str) -> None:
        if self._card is None or not data:
            return
        for target, _, _ in self._capture_scopes:
            parts = getattr(self._card, target)
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        open_count = self._open_tag_counts.get(tag, 0)
        self._capture_scopes = [
            scope
            for scope in self._capture_scopes
            if not (scope[1] == tag and scope[2] == open_count)
        ]
        if tag not in self._VOID_TAGS and open_count > 0:
            self._open_tag_counts[tag] = open_count - 1
        if tag != "div":
            return
        self._div_depth -= 1
        if self._div_depth != 0:
            return
        self.cards.append(
            _HotelCard(
                hotel_id=self._card.hotel_id,
                hotel_name=_normalized_text(self._card.hotel_name),
                room_name=_normalized_text(self._card.room_name),
                adult_icons=self._card.adult_icons,
                price_explain=_normalized_text(self._card.price_explain),
            )
        )
        self._card = None
        self._open_tag_counts = {}
        self._capture_scopes = []


@dataclass(frozen=True, slots=True)
class TripComLodgingCatalogResult:
    stays: tuple[StayOffer, ...]
    contracts: tuple[PriceContract, ...]
    source_statuses: tuple[SourceStatus, ...]
    query_task_ids: tuple[str, ...]


def _normalized_alias(value: str) -> str:
    return re.sub(r"[\s\-_]", "", value.strip().lower())


def _normalized_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _city_identity(value: str) -> TripComCityIdentity | None:
    normalized = _normalized_alias(value)
    return next(
        (
            city
            for city in _TRIPCOM_CITIES
            if normalized in {_normalized_alias(alias) for alias in city.aliases}
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


class TripComCurrentLodgingSource:
    """Fetch a bounded set of exact, tax-inclusive CNY lodging totals."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_candidates_per_stay: int = 8,
        timeout_seconds: float = 60.0,
    ) -> None:
        if max_candidates_per_stay < 1:
            raise ValueError("max_candidates_per_stay must be positive")
        self._client = client
        self._max_candidates_per_stay = max_candidates_per_stay
        self._timeout_seconds = timeout_seconds

    async def catalog_for(self, intent: TravelIntent) -> TripComLodgingCatalogResult:
        prepared = self._prepare(intent)
        if isinstance(prepared, str):
            captured_at = datetime.now(UTC)
            return TripComLodgingCatalogResult(
                stays=(),
                contracts=(),
                source_statuses=(
                    SourceStatus(
                        source_id="trip.com:hotel:bounded-current",
                        provider="trip.com",
                        state=SourceState.NOT_QUERIED,
                        detail=prepared,
                        captured_at=captured_at,
                    ),
                ),
                query_task_ids=(),
            )

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        client = self._client or httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout_seconds, connect=10.0),
        )
        owns_client = self._client is None
        try:
            results = await asyncio.gather(*(self._fetch_stay(client, spec) for spec in prepared))
        finally:
            if owns_client:
                await client.aclose()

        return TripComLodgingCatalogResult(
            stays=tuple(offer for offers, _, _ in results for offer in offers),
            contracts=tuple(contract for _, contracts, _ in results for contract in contracts),
            source_statuses=tuple(status for _, _, status in results),
            query_task_ids=tuple(spec.query_task_id for spec in prepared),
        )

    def _prepare(self, intent: TravelIntent) -> tuple[_StaySpec, ...] | str:
        if not 1 <= intent.travelers <= _MAX_TRAVELERS:
            return "Trip.com当前住宿查询只支持1至8名成人、1间房"
        if intent.stay_requirements:
            scoped_specs: list[_StaySpec] = []
            for requirement in intent.stay_requirements:
                city = _city_identity(requirement.place_id)
                if city is None:
                    return "Trip.com当前住宿查询尚未支持某个停留城市"
                if requirement.check_out <= requirement.check_in:
                    return "Trip.com当前住宿查询的退房日期必须晚于入住日期"
                adults = len(requirement.participant_ids)
                if not 1 <= adults <= _MAX_TRAVELERS or requirement.room_count != 1:
                    return "Trip.com当前住宿查询只支持1至8名成人、1间房"
                scoped_specs.append(
                    _StaySpec(
                        place_id=requirement.place_id,
                        city=city,
                        check_in=requirement.check_in,
                        check_out=requirement.check_out,
                        adults=adults,
                        rooms=requirement.room_count,
                        participant_ids=requirement.participant_ids,
                    )
                )
            return tuple(scoped_specs)
        if len(intent.route_legs) < 2:
            return "Trip.com多城市住宿查询需要至少两段连续行程"

        specs: list[_StaySpec] = []
        for inbound, outbound in zip(
            intent.route_legs,
            intent.route_legs[1:],
            strict=False,
        ):
            if inbound.destination_place_id != outbound.origin_place_id:
                return "Trip.com当前住宿查询需要连续的多城市路线"
            check_in = _exact_departure_date(inbound)
            check_out = _exact_departure_date(outbound)
            city = _city_identity(inbound.destination_place_id)
            if check_in is None or check_out is None:
                return "Trip.com当前住宿查询需要确切的入住和退房日期"
            if check_out <= check_in:
                return "Trip.com当前住宿查询的退房日期必须晚于入住日期"
            if city is None:
                return "Trip.com当前住宿查询尚未支持某个停留城市"
            specs.append(
                _StaySpec(
                    place_id=inbound.destination_place_id,
                    city=city,
                    check_in=check_in,
                    check_out=check_out,
                        adults=intent.travelers,
                )
            )
        return tuple(specs)

    async def _fetch_stay(
        self,
        client: httpx.AsyncClient,
        spec: _StaySpec,
    ) -> tuple[tuple[StayOffer, ...], tuple[PriceContract, ...], SourceStatus]:
        captured_at = datetime.now(UTC)
        try:
            response: httpx.Response | None = None
            for attempt in range(2):
                try:
                    response = await client.get(spec.query_url)
                    break
                except (httpx.TimeoutException, httpx.RemoteProtocolError):
                    if attempt == 1:
                        raise
                    await asyncio.sleep(0.5)
            if response is None:
                raise ValueError("Trip.com current response is missing")
            response.raise_for_status()
            captured_at = datetime.now(UTC)
            self._validate_response_url(response, spec)
            self._validate_page_readback(response.text, spec)
            parser = _HotelCardParser()
            parser.feed(response.text)
            complete = self._complete_cards(parser.cards, spec)
            offers: list[StayOffer] = []
            contracts: list[PriceContract] = []
            for card, total_cents in complete[: self._max_candidates_per_stay]:
                digest = hashlib.sha256(
                    (
                        f"{spec.place_id}|{spec.check_in.isoformat()}|"
                        f"{spec.check_out.isoformat()}|{spec.adults}|{card.hotel_id}|"
                        f"{spec.rooms}|{','.join(sorted(spec.participant_ids))}|"
                        f"{card.hotel_name}|{card.room_name}|{total_cents}"
                    ).encode()
                ).hexdigest()[:20]
                offer_id = f"trip.com:stay:{digest}"
                contract_id = f"trip.com:price:{digest}"
                offers.append(
                    StayOffer(
                        id=offer_id,
                        provider="trip.com",
                        place_id=spec.place_id,
                        check_in=spec.check_in,
                        check_out=spec.check_out,
                        price_contract_id=contract_id,
                        detail_url=spec.query_url,
                        label=f"{card.hotel_name}｜{card.room_name}",
                        confirmed_traveler_count=spec.adults,
                        confirmed_room_count=1,
                        participant_ids=spec.participant_ids,
                    )
                )
                contracts.append(
                    PriceContract(
                        id=contract_id,
                        currency="CNY",
                        total_for_party_cents=total_cents,
                        component_ids=(offer_id,),
                        covered_traveler_ids=spec.participant_ids,
                        shared=False,
                        shared_between_travelers=len(spec.participant_ids) > 1,
                        taxes_and_fees_included=True,
                        source=(
                            "current:trip.com:public-ssr-list:"
                            f"{captured_at.isoformat()}"
                        ),
                    )
                )
            state = SourceState.SUCCEEDED if offers else SourceState.FAILED
            detail = (
                f"Trip.com当前页面已回读{spec.adults}名成人、1间房、"
                f"{spec.nights}晚含税人民币合计；"
                f"有界接纳{len(offers)}个酒店房型，未下单"
                if offers
                else "Trip.com当前页面未形成人数、晚数和含税总价都完整的住宿报价"
            )
            return (
                tuple(offers),
                tuple(contracts),
                SourceStatus(
                    source_id=spec.query_task_id,
                    provider="trip.com",
                    state=state,
                    detail=detail,
                    query_task_ids=(spec.query_task_id,),
                    captured_at=captured_at,
                ),
            )
        except (httpx.HTTPError, ValueError, InvalidOperation) as exc:
            return (
                (),
                (),
                SourceStatus(
                    source_id=spec.query_task_id,
                    provider="trip.com",
                    state=SourceState.FAILED,
                    detail=f"Trip.com当前住宿查询失败:{type(exc).__name__}",
                    query_task_ids=(spec.query_task_id,),
                    captured_at=captured_at,
                ),
            )

    @staticmethod
    def _validate_response_url(response: httpx.Response, spec: _StaySpec) -> None:
        url = urlsplit(str(response.url))
        if (
            url.scheme != "https"
            or url.hostname != "www.trip.com"
            or url.path != "/hotels/list"
            or tuple(parse_qsl(url.query, keep_blank_values=True)) != spec.query_parameters
        ):
            raise ValueError("Trip.com response left the exact hotel-list query")

    @staticmethod
    def _validate_page_readback(page: str, spec: _StaySpec) -> None:
        readback = html.unescape(page).replace(r"\"", '"')
        required_fragments = (
            (
                '"searchBarData":{"destinationInfo":'
                f'{{"keywordInputValue":"","destinationInputValue":"'
                f'{spec.city.city_name_en}","cityId":{spec.city.city_id},'
            ),
            (
                f'"calendarInfo":{{"checkIn":"{spec.check_in.isoformat()}",'
                f'"checkOut":"{spec.check_out.isoformat()}",'
                f'"nights":{spec.nights},'
            ),
            (
                f'"guestInfo":{{"roomsNum":{spec.rooms},'
                f'"adultsNum":{spec.adults},"childNum":0,'
            ),
            '"cargo":{"locale":"en-XX","currency":"CNY",',
        )
        if not all(fragment in readback for fragment in required_fragments):
            raise ValueError("Trip.com page did not echo the requested hotel contract")

    @staticmethod
    def _complete_cards(
        cards: list[_HotelCard],
        spec: _StaySpec,
    ) -> tuple[tuple[_HotelCard, int], ...]:
        complete: list[tuple[_HotelCard, int]] = []
        seen_hotel_ids: set[str] = set()
        for card in cards:
            if (
                not card.hotel_name
                or not card.room_name
                or card.adult_icons != spec.adults
                or card.hotel_id in seen_hotel_ids
            ):
                continue
            match = _TOTAL_PRICE_PATTERN.fullmatch(card.price_explain)
            if match is None or int(match.group("nights")) != spec.nights:
                continue
            amount = Decimal(match.group("amount").replace(",", ""))
            cents = amount * 100
            if amount <= 0 or cents != cents.to_integral_value():
                continue
            seen_hotel_ids.add(card.hotel_id)
            complete.append((card, int(cents)))
        return tuple(complete)


__all__ = [
    "TripComCityIdentity",
    "TripComCurrentLodgingSource",
    "TripComLodgingCatalogResult",
]
