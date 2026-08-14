from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    PackageArea,
    PackagePlaceKey,
    QuoteAvailability,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
)
from tripchord.providers.base import ProviderError

_API_HOST = "sfs-api.icomtours.com"
_API_ORIGIN = f"https://{_API_HOST}"
_OPERATOR = "iCom Tours"
_MVT = timezone(timedelta(hours=5), name="MVT")
_FORBIDDEN_PATH_WORDS = frozenset({"booking", "payment", "order"})


class _Endpoint(StrEnum):
    SCHEDULES = "/api/v1/public/trips/schedules"
    BASE_FARE = "/api/v1/public/ferry-fares/schedule-base-price"
    POLICY = "/api/v1/public/policy-sections"


_ENDPOINTS = (_Endpoint.SCHEDULES, _Endpoint.BASE_FARE, _Endpoint.POLICY)
_ALLOWED_PATHS = frozenset(endpoint.value for endpoint in _ENDPOINTS)


class IComLocation(StrEnum):
    AIRPORT = "Airport"
    MAAFUSHI = "Maafushi"


class IComAvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    CANCELLED = "cancelled"
    INSUFFICIENT_REMAINING = "insufficient_remaining"


class IComFailureCode(StrEnum):
    URL_FORBIDDEN = "url_forbidden"
    REDIRECT_BOUNDARY = "redirect_boundary"
    REDIRECT_FORBIDDEN = "redirect_forbidden"
    HTTP_STATUS = "http_status"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_JSON = "invalid_json"
    SCHEMA_DRIFT = "schema_drift"
    UNEXPECTED_ERROR = "unexpected_error"


class IComTransferConfig(DomainModel):
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_response_bytes: int = Field(default=1_000_000, ge=128, le=5_000_000)
    user_agent: str = "TripChord/0.1 (+read-only iCom public transfer evidence)"


class IComTransferQuery(DomainModel):
    travel_date: date
    origin: IComLocation
    destination: IComLocation
    adults: int = Field(default=1, ge=1, le=9)

    @model_validator(mode="after")
    def validate_supported_route(self) -> IComTransferQuery:
        if self.origin == self.destination:
            raise ValueError("iCom transfer supports only Airport <-> Maafushi")
        return self


class IComFieldEvidence(DomainModel):
    normalized_field: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    json_paths: tuple[str, ...]
    derivation: Literal["direct", "combined", "provider_contract", "not_asserted"]
    value_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: AwareDatetime


class IComPublishedBaseFare(DomainModel):
    kind: Literal["published_base_fare"] = "published_base_fare"
    amount: Decimal = Field(ge=0)
    currency: Literal["USD"] = "USD"
    basis: Literal["per_person"] = "per_person"
    taxes_included: None = None
    evidence: tuple[IComFieldEvidence, ...]


class IComCurrencyPolicyEvidence(DomainModel):
    statement: str = Field(min_length=1)
    meaning: Literal["prices_displayed_and_charged_in_usd"] = "prices_displayed_and_charged_in_usd"
    tax_inclusion_confirmed: None = None
    source_url: str = Field(min_length=1)
    json_path: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: AwareDatetime


class IComTransferOption(DomainModel):
    trip_id: int = Field(ge=1)
    schedule_id: int = Field(ge=1)
    service_name: str = Field(min_length=1)
    operator: Literal["iCom Tours"] = "iCom Tours"
    vessel_name: str = Field(min_length=1)
    origin: IComLocation
    destination: IComLocation
    route: str = Field(min_length=1)
    departure_at: AwareDatetime
    arrival_at: AwareDatetime
    capacity: int = Field(ge=0)
    remaining_capacity: int = Field(ge=0)
    stops: int = Field(ge=0)
    is_cancelled: bool
    availability_status: IComAvailabilityStatus
    eligible_for_party: bool
    fare: IComPublishedBaseFare
    currency_policy_evidence: IComCurrencyPolicyEvidence | None
    source_url: str = Field(min_length=1)
    captured_at: AwareDatetime
    evidence: tuple[IComFieldEvidence, ...]

    @model_validator(mode="after")
    def validate_normalized_option(self) -> IComTransferOption:
        expected_offset = timedelta(hours=5)
        if (
            self.departure_at.utcoffset() != expected_offset
            or self.arrival_at.utcoffset() != expected_offset
        ):
            raise ValueError("iCom transfer datetimes must use MVT UTC+05:00")
        if self.arrival_at <= self.departure_at:
            raise ValueError("arrival_at must be after departure_at")
        if self.remaining_capacity > self.capacity:
            raise ValueError("remaining_capacity must not exceed capacity")
        if self.is_cancelled and self.availability_status != IComAvailabilityStatus.CANCELLED:
            raise ValueError("cancelled trips must have cancelled availability")
        if not self.is_cancelled and self.availability_status == IComAvailabilityStatus.CANCELLED:
            raise ValueError("active trips cannot have cancelled availability")
        if self.eligible_for_party != (
            self.availability_status == IComAvailabilityStatus.AVAILABLE
        ):
            raise ValueError("eligible_for_party conflicts with availability_status")
        return self


def to_package_transfer_option(
    option: IComTransferOption,
    *,
    adults: int,
    evidence_ttl: timedelta = timedelta(minutes=10),
) -> TransferOption | None:
    """Convert one eligible official iCom schedule without upgrading price truth."""
    if not 1 <= adults <= 9:
        raise ValueError("iCom package conversion requires between 1 and 9 adults")
    if evidence_ttl <= timedelta(0):
        raise ValueError("iCom package conversion evidence_ttl must be positive")
    if (
        option.is_cancelled
        or option.availability_status != IComAvailabilityStatus.AVAILABLE
        or not option.eligible_for_party
        or option.remaining_capacity < adults
    ):
        return None
    if option.departure_at <= option.captured_at:
        return None

    area_by_location = {
        IComLocation.AIRPORT: PackageArea.AIRPORT,
        IComLocation.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
    }
    fare_cents = option.fare.amount * Decimal(100)
    if fare_cents != fare_cents.to_integral_value():
        raise ValueError("iCom published base fare cannot be represented as integer cents")
    per_person_cents = int(fare_cents)
    total_for_party_cents = per_person_cents * adults
    duration_seconds = int((option.arrival_at - option.departure_at).total_seconds())
    if duration_seconds <= 0 or duration_seconds % 60:
        raise ValueError("iCom package conversion requires a positive whole-minute duration")
    duration_minutes = duration_seconds // 60

    evidence_rows = (*option.evidence, *option.fare.evidence)
    contract_payload = {
        "provider": "icom-public-transfer",
        "trip_id": option.trip_id,
        "schedule_id": option.schedule_id,
        "origin": option.origin.value,
        "destination": option.destination.value,
        "departure_at": option.departure_at.isoformat(),
        "arrival_at": option.arrival_at.isoformat(),
        "adults": adults,
        "fare_kind": option.fare.kind,
        "fare_amount_per_person": str(option.fare.amount),
        "fare_currency": option.fare.currency,
        "fare_basis": option.fare.basis,
        "taxes_included": option.fare.taxes_included,
        "source_url": option.source_url,
        "evidence": [
            {
                "field": item.normalized_field,
                "value_sha256": item.value_sha256,
                "response_sha256": item.response_sha256,
            }
            for item in evidence_rows
        ],
        "currency_policy_sha256": (
            option.currency_policy_evidence.evidence_sha256
            if option.currency_policy_evidence is not None
            else None
        ),
    }
    contract_digest = hashlib.sha256(
        json.dumps(
            contract_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    evidence_refs = list(
        dict.fromkeys(
            f"{item.source_url}#response-sha256={item.response_sha256}" for item in evidence_rows
        )
    )
    if option.currency_policy_evidence is not None:
        evidence_refs.append(
            f"{option.currency_policy_evidence.source_url}"
            f"#response-sha256={option.currency_policy_evidence.response_sha256}"
        )
    evidence_refs = list(dict.fromkeys(evidence_refs))
    total_display = Decimal(total_for_party_cents) / Decimal(100)
    contract_evidence_text = (
        f"{option.operator} {option.origin.value} → {option.destination.value}，"
        f"{option.departure_at.isoformat()} 至 {option.arrival_at.isoformat()}；"
        f"公开基础价 USD {option.fare.amount:.2f}/人 × {adults}人"
        f" = USD {total_display:.2f}；税费未确认；查询时余位 "
        f"{option.remaining_capacity}，未锁库存"
    )
    return TransferOption(
        id=f"icom:trip:{option.trip_id}:{contract_digest[:16]}",
        provider="icom-public-transfer",
        currency=option.fare.currency,
        total_for_party_cents=total_for_party_cents,
        taxes_and_fees_included=None,
        captured_at=option.captured_at,
        expires_at=min(option.captured_at + evidence_ttl, option.departure_at),
        availability=QuoteAvailability.AVAILABLE,
        evidence_refs=tuple(evidence_refs),
        origin_area=area_by_location[option.origin],
        destination_area=area_by_location[option.destination],
        origin_place_key=(
            PackagePlaceKey.VELANA_AIRPORT
            if option.origin == IComLocation.AIRPORT
            else PackagePlaceKey.MAAFUSHI
        ),
        destination_place_key=(
            PackagePlaceKey.VELANA_AIRPORT
            if option.destination == IComLocation.AIRPORT
            else PackagePlaceKey.MAAFUSHI
        ),
        adults=adults,
        service_date=option.departure_at.date(),
        schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
        duration_minutes=duration_minutes,
        depart_at=option.departure_at,
        arrive_at=option.arrival_at,
        operates_24_hours=False,
        requires_reservation=None,
        price_scope=TransferPriceScope.ONE_WAY,
        price_contract_id=f"icom:published-base-fare:{contract_digest}",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        price_guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        contract_evidence_text=contract_evidence_text,
        detail_url=option.source_url,
    )


class IComTransferSearchResult(DomainModel):
    query: IComTransferQuery
    searched_at: AwareDatetime
    options: tuple[IComTransferOption, ...]
    source_urls: tuple[str, str, str]


class _PayloadModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _ApiMeta(_PayloadModel):
    timestamp: str
    api_version: Literal["v1"] = Field(alias="apiVersion")
    status: Literal["success"]
    message: str


class _LocationRow(_PayloadModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1)


class _VesselRow(_PayloadModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    total_capacity: int = Field(alias="totalCapacity", ge=0)


class _ScheduleRow(_PayloadModel):
    id: int = Field(ge=1)
    trip_date: str = Field(alias="tripDate")
    departure_time: str = Field(alias="departureTime")
    arrival_time: str = Field(alias="arrivalTime")
    capacity: int = Field(ge=0)
    remaining_capacity: int = Field(alias="remainingCapacity", ge=0)
    cancelled_at: str | None = Field(alias="cancelledAt")
    is_cancelled: bool = Field(alias="isCancelled")
    schedule_id: int = Field(alias="scheduleId", ge=1)
    stops: int = Field(ge=0)
    ferry_name: str = Field(alias="ferryName", min_length=1)
    vessel: _VesselRow
    origin: _LocationRow
    destination: _LocationRow

    @model_validator(mode="after")
    def validate_capacity(self) -> _ScheduleRow:
        if self.remaining_capacity > self.capacity:
            raise ValueError("remainingCapacity exceeds capacity")
        if self.vessel.total_capacity < self.capacity:
            raise ValueError("trip capacity exceeds vessel totalCapacity")
        return self


class _SchedulesResponse(_PayloadModel):
    meta: _ApiMeta
    data: list[_ScheduleRow]


class _BaseFareData(_PayloadModel):
    amount: int | float | str
    currency_code: Literal["USD"] = Field(alias="currencyCode")


class _BaseFareResponse(_PayloadModel):
    meta: _ApiMeta
    data: _BaseFareData


class _PolicySection(_PayloadModel):
    id: int = Field(ge=1)
    title: str = Field(min_length=1)
    richtext: dict[str, Any]
    is_active: bool = Field(alias="isActive")


class _PolicyResponse(_PayloadModel):
    meta: _ApiMeta
    data: list[_PolicySection]


@dataclass(frozen=True)
class _FetchedPayload:
    endpoint: _Endpoint
    source_url: str
    captured_at: datetime
    response_sha256: str
    payload: object


PayloadT = TypeVar("PayloadT", bound=BaseModel)


class IComTransferProvider:
    """Read-only adapter for iCom's official public Airport-Maafushi transfer data."""

    name = "icom-public-transfer"

    def __init__(
        self,
        config: IComTransferConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or IComTransferConfig()
        self._client = client or httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=False,
            headers={"user-agent": self._config.user_agent},
        )
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: IComTransferQuery) -> IComTransferSearchResult:
        requests = (
            self._fetch(_Endpoint.SCHEDULES, params={"date": query.travel_date.isoformat()}),
            self._fetch(_Endpoint.BASE_FARE),
            self._fetch(_Endpoint.POLICY),
        )
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                settled = await asyncio.gather(*requests, return_exceptions=True)
        except TimeoutError as exc:
            raise self._error(
                IComFailureCode.TIMEOUT,
                f"iCom public reads exceeded {self._config.timeout_seconds:g}s deadline",
                retryable=True,
            ) from exc

        fetched_by_endpoint: dict[_Endpoint, _FetchedPayload] = {}
        for endpoint, result in zip(_ENDPOINTS, settled, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, ProviderError):
                raise result
            if isinstance(result, BaseException):
                raise self._error(
                    IComFailureCode.UNEXPECTED_ERROR,
                    f"unexpected iCom {endpoint.name.lower()} failure: {type(result).__name__}",
                ) from result
            fetched_by_endpoint[endpoint] = result

        schedules_source = fetched_by_endpoint[_Endpoint.SCHEDULES]
        fare_source = fetched_by_endpoint[_Endpoint.BASE_FARE]
        policy_source = fetched_by_endpoint[_Endpoint.POLICY]
        schedules = self._validate_schema(_SchedulesResponse, schedules_source)
        fare_payload = self._validate_schema(_BaseFareResponse, fare_source)
        policy_payload = self._validate_schema(_PolicyResponse, policy_source)
        fare = self._normalize_fare(fare_payload, fare_source)
        policy_evidence = self._currency_policy_evidence(policy_payload, policy_source)
        options = self._normalize_options(
            schedules,
            schedules_source,
            query,
            fare,
            policy_evidence,
        )
        return IComTransferSearchResult(
            query=query,
            searched_at=self._now(),
            options=options,
            source_urls=tuple(fetched_by_endpoint[endpoint].source_url for endpoint in _ENDPOINTS),
        )

    async def _fetch(
        self,
        endpoint: _Endpoint,
        *,
        params: dict[str, str] | None = None,
    ) -> _FetchedPayload:
        url = f"{_API_ORIGIN}{endpoint.value}"
        request = self._client.build_request("GET", url, params=params)
        self._validate_request(request)
        response: httpx.Response | None = None
        try:
            response = await self._client.send(
                request,
                follow_redirects=False,
                stream=True,
            )
            self._validate_response_boundary(response)
            if 300 <= response.status_code < 400:
                self._reject_redirect(response)
            if not response.is_success:
                raise self._error(
                    IComFailureCode.HTTP_STATUS,
                    f"iCom public endpoint returned HTTP {response.status_code}",
                    retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
                )
            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type and "+json" not in content_type:
                raise self._error(
                    IComFailureCode.INVALID_JSON,
                    "iCom public endpoint did not return JSON",
                )
            raw = await self._read_limited(response)
            try:
                payload: object = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise self._error(
                    IComFailureCode.INVALID_JSON,
                    "iCom public endpoint returned invalid JSON",
                ) from exc
            return _FetchedPayload(
                endpoint=endpoint,
                source_url=str(response.url),
                captured_at=self._now(),
                response_sha256=hashlib.sha256(raw).hexdigest(),
                payload=payload,
            )
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise self._error(
                IComFailureCode.TIMEOUT,
                "iCom public endpoint timed out",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise self._error(
                IComFailureCode.NETWORK_ERROR,
                f"iCom public endpoint network failure: {type(exc).__name__}",
                retryable=True,
            ) from exc
        finally:
            if response is not None:
                await response.aclose()

    async def _read_limited(self, response: httpx.Response) -> bytes:
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > self._config.max_response_bytes:
                raise self._error(
                    IComFailureCode.RESPONSE_TOO_LARGE,
                    "iCom public response exceeds the configured byte limit",
                )
        return bytes(raw)

    def _validate_request(self, request: httpx.Request) -> None:
        if request.method != "GET":
            raise self._error(
                IComFailureCode.URL_FORBIDDEN,
                "iCom adapter permits GET requests only",
            )
        self._validate_read_url(str(request.url))

    def _validate_response_boundary(self, response: httpx.Response) -> None:
        for historical in response.history:
            try:
                self._validate_request(historical.request)
                self._validate_read_url(str(historical.url))
            except ProviderError as exc:
                raise self._error(
                    IComFailureCode.REDIRECT_BOUNDARY,
                    "iCom redirect history left the exact public-read boundary",
                ) from exc
        try:
            self._validate_request(response.request)
            self._validate_read_url(str(response.url))
        except ProviderError as exc:
            raise self._error(
                IComFailureCode.URL_FORBIDDEN,
                "iCom response URL is outside the exact public-read boundary",
            ) from exc
        if response.history:
            raise self._error(
                IComFailureCode.REDIRECT_FORBIDDEN,
                "iCom adapter does not accept followed redirects",
            )

    def _reject_redirect(self, response: httpx.Response) -> None:
        location = response.headers.get("location")
        if location:
            redirect_url = str(response.url.join(location))
            try:
                self._validate_read_url(redirect_url)
            except ProviderError as exc:
                raise self._error(
                    IComFailureCode.REDIRECT_BOUNDARY,
                    "iCom redirect target left the exact public-read boundary",
                ) from exc
        raise self._error(
            IComFailureCode.REDIRECT_FORBIDDEN,
            "iCom public adapter does not follow redirects",
        )

    def _validate_read_url(self, url: str) -> None:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise self._error(IComFailureCode.URL_FORBIDDEN, "invalid iCom URL port") from exc
        path_parts = tuple(part.casefold() for part in parsed.path.split("/") if part)
        has_privileged_word = any(
            forbidden in part for forbidden in _FORBIDDEN_PATH_WORDS for part in path_parts
        )
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != _API_HOST
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path not in _ALLOWED_PATHS
            or has_privileged_word
        ):
            raise self._error(
                IComFailureCode.URL_FORBIDDEN,
                "URL is not one of the exact iCom public GET endpoints",
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == _Endpoint.SCHEDULES:
            dates = query.get("date")
            if set(query) != {"date"} or dates is None or len(dates) != 1:
                raise self._error(
                    IComFailureCode.URL_FORBIDDEN,
                    "schedule reads require exactly one date query parameter",
                )
            try:
                date.fromisoformat(dates[0])
            except ValueError as exc:
                raise self._error(
                    IComFailureCode.URL_FORBIDDEN,
                    "schedule date must be ISO YYYY-MM-DD",
                ) from exc
        elif query:
            raise self._error(
                IComFailureCode.URL_FORBIDDEN,
                "this iCom public endpoint does not accept query parameters",
            )

    def _validate_schema(
        self,
        model: type[PayloadT],
        fetched: _FetchedPayload,
    ) -> PayloadT:
        try:
            return model.model_validate(fetched.payload)
        except (ValidationError, TypeError, ValueError) as exc:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                f"iCom {fetched.endpoint.name.lower()} JSON contract changed",
            ) from exc

    def _normalize_fare(
        self,
        payload: _BaseFareResponse,
        fetched: _FetchedPayload,
    ) -> IComPublishedBaseFare:
        try:
            amount = Decimal(str(payload.data.amount))
        except (InvalidOperation, ValueError) as exc:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                "iCom published base fare amount is invalid",
            ) from exc
        if not amount.is_finite() or amount < 0:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                "iCom published base fare amount is invalid",
            )
        return IComPublishedBaseFare(
            amount=amount,
            evidence=(
                self._evidence(
                    "fare.amount",
                    amount,
                    fetched,
                    ("$.data.amount",),
                    "direct",
                ),
                self._evidence(
                    "fare.currency",
                    payload.data.currency_code,
                    fetched,
                    ("$.data.currencyCode",),
                    "direct",
                ),
                self._evidence(
                    "fare.basis",
                    "per_person",
                    fetched,
                    ("$.data",),
                    "provider_contract",
                ),
                self._evidence(
                    "fare.taxes_included",
                    None,
                    fetched,
                    (),
                    "not_asserted",
                ),
            ),
        )

    def _currency_policy_evidence(
        self,
        payload: _PolicyResponse,
        fetched: _FetchedPayload,
    ) -> IComCurrencyPolicyEvidence | None:
        for section_index, section in enumerate(payload.data):
            if not section.is_active:
                continue
            root = f"$.data[{section_index}].richtext"
            for path, statement in self._iter_strings(section.richtext, root):
                normalized = " ".join(statement.split())
                lowered = normalized.casefold()
                if "displayed and charged" in lowered and "usd" in lowered:
                    return IComCurrencyPolicyEvidence(
                        statement=normalized,
                        source_url=fetched.source_url,
                        json_path=path,
                        evidence_sha256=self._value_sha256(normalized),
                        response_sha256=fetched.response_sha256,
                        captured_at=fetched.captured_at,
                    )
        return None

    def _normalize_options(
        self,
        payload: _SchedulesResponse,
        fetched: _FetchedPayload,
        query: IComTransferQuery,
        fare: IComPublishedBaseFare,
        policy_evidence: IComCurrencyPolicyEvidence | None,
    ) -> tuple[IComTransferOption, ...]:
        options: list[IComTransferOption] = []
        for index, row in enumerate(payload.data):
            if (
                row.origin.name.casefold() != query.origin.value.casefold()
                or row.destination.name.casefold() != query.destination.value.casefold()
            ):
                continue
            departure_at, arrival_at = self._parse_mvt_times(row, query.travel_date)
            status = self._availability(row, query.adults)
            route = f"{query.origin.value} -> {query.destination.value}"
            root = f"$.data[{index}]"
            evidence = (
                self._evidence("trip_id", row.id, fetched, (f"{root}.id",), "direct"),
                self._evidence(
                    "schedule_id",
                    row.schedule_id,
                    fetched,
                    (f"{root}.scheduleId",),
                    "direct",
                ),
                self._evidence(
                    "operator",
                    _OPERATOR,
                    fetched,
                    (root,),
                    "provider_contract",
                ),
                self._evidence(
                    "vessel_name",
                    row.vessel.name,
                    fetched,
                    (f"{root}.vessel.name",),
                    "direct",
                ),
                self._evidence(
                    "route",
                    route,
                    fetched,
                    (f"{root}.origin.name", f"{root}.destination.name"),
                    "combined",
                ),
                self._evidence(
                    "departure_at",
                    departure_at,
                    fetched,
                    (f"{root}.tripDate", f"{root}.departureTime"),
                    "combined",
                ),
                self._evidence(
                    "arrival_at",
                    arrival_at,
                    fetched,
                    (f"{root}.tripDate", f"{root}.arrivalTime"),
                    "combined",
                ),
                self._evidence(
                    "capacity",
                    row.capacity,
                    fetched,
                    (f"{root}.capacity",),
                    "direct",
                ),
                self._evidence(
                    "remaining_capacity",
                    row.remaining_capacity,
                    fetched,
                    (f"{root}.remainingCapacity",),
                    "direct",
                ),
                self._evidence(
                    "is_cancelled",
                    row.is_cancelled,
                    fetched,
                    (f"{root}.isCancelled",),
                    "direct",
                ),
                self._evidence(
                    "availability_status",
                    status,
                    fetched,
                    (f"{root}.isCancelled", f"{root}.remainingCapacity"),
                    "combined",
                ),
            )
            options.append(
                IComTransferOption(
                    trip_id=row.id,
                    schedule_id=row.schedule_id,
                    service_name=row.ferry_name,
                    vessel_name=row.vessel.name,
                    origin=query.origin,
                    destination=query.destination,
                    route=route,
                    departure_at=departure_at,
                    arrival_at=arrival_at,
                    capacity=row.capacity,
                    remaining_capacity=row.remaining_capacity,
                    stops=row.stops,
                    is_cancelled=row.is_cancelled,
                    availability_status=status,
                    eligible_for_party=status == IComAvailabilityStatus.AVAILABLE,
                    fare=fare,
                    currency_policy_evidence=policy_evidence,
                    source_url=fetched.source_url,
                    captured_at=fetched.captured_at,
                    evidence=evidence,
                )
            )
        return tuple(options)

    def _parse_mvt_times(
        self,
        row: _ScheduleRow,
        requested_date: date,
    ) -> tuple[datetime, datetime]:
        try:
            trip_date = date.fromisoformat(row.trip_date)
            departure_time = time.fromisoformat(row.departure_time)
            arrival_time = time.fromisoformat(row.arrival_time)
        except ValueError as exc:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                "iCom schedule date or time is not ISO-compatible",
            ) from exc
        if trip_date != requested_date:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                "iCom schedule returned a different tripDate than requested",
            )
        if departure_time.tzinfo is not None or arrival_time.tzinfo is not None:
            raise self._error(
                IComFailureCode.SCHEMA_DRIFT,
                "iCom local schedule times unexpectedly contain a timezone",
            )
        departure_at = datetime.combine(trip_date, departure_time, tzinfo=_MVT)
        arrival_at = datetime.combine(trip_date, arrival_time, tzinfo=_MVT)
        if arrival_at <= departure_at:
            arrival_at += timedelta(days=1)
        return departure_at, arrival_at

    def _availability(
        self,
        row: _ScheduleRow,
        adults: int,
    ) -> IComAvailabilityStatus:
        if row.is_cancelled:
            return IComAvailabilityStatus.CANCELLED
        if row.remaining_capacity < adults:
            return IComAvailabilityStatus.INSUFFICIENT_REMAINING
        return IComAvailabilityStatus.AVAILABLE

    def _evidence(
        self,
        field: str,
        value: object,
        fetched: _FetchedPayload,
        json_paths: tuple[str, ...],
        derivation: Literal["direct", "combined", "provider_contract", "not_asserted"],
    ) -> IComFieldEvidence:
        return IComFieldEvidence(
            normalized_field=field,
            source_url=fetched.source_url,
            json_paths=json_paths,
            derivation=derivation,
            value_sha256=self._value_sha256(value),
            response_sha256=fetched.response_sha256,
            captured_at=fetched.captured_at,
        )

    def _iter_strings(self, value: object, path: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        if isinstance(value, str):
            return [(path, value)]
        if isinstance(value, list):
            for index, item in enumerate(value):
                found.extend(self._iter_strings(item, f"{path}[{index}]"))
        elif isinstance(value, dict):
            for key, item in value.items():
                found.extend(self._iter_strings(item, f"{path}.{key}"))
        return found

    def _value_sha256(self, value: object) -> str:
        if isinstance(value, datetime):
            normalized: object = value.isoformat()
        elif isinstance(value, Decimal):
            normalized = str(value)
        elif isinstance(value, StrEnum):
            normalized = value.value
        else:
            normalized = value
        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _error(
        self,
        code: IComFailureCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> ProviderError:
        return ProviderError(self.name, code.value, message, retryable=retryable)


__all__ = [
    "IComAvailabilityStatus",
    "IComCurrencyPolicyEvidence",
    "IComFailureCode",
    "IComFieldEvidence",
    "IComLocation",
    "IComPublishedBaseFare",
    "IComTransferConfig",
    "IComTransferOption",
    "IComTransferProvider",
    "IComTransferQuery",
    "IComTransferSearchResult",
    "to_package_transfer_option",
]
