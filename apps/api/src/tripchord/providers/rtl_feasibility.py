from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Literal

import httpx
from pydantic import AwareDatetime, Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.providers.base import ProviderError

_PUBLIC_ROUTE_URL = "https://bo.rtl.mv:4455/maldives/api/booking/v2/bus/routedetails"
_MACL_TRANSPORT_URL = "https://velana.macl.aero/guide/transport"
_EXPECTED_CONTENT_TYPE = "application/json"


class RtlTransferAssurance(StrEnum):
    FEASIBILITY_HINT = "feasibility_hint"


class RtlTransferDirection(StrEnum):
    AIRPORT_TO_HULHUMALE = "airport_to_hulhumale"
    HULHUMALE_TO_AIRPORT = "hulhumale_to_airport"


class RtlAirportHulhumaleConfig(DomainModel):
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_response_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)
    user_agent: str = "TripChord/0.1 (+read-only RTL feasibility evidence)"


class RtlObservedScheduleLeg(DomainModel):
    route_number: Literal["R4", "R9"]
    route_code: str = Field(min_length=1)
    route_name: str = Field(min_length=1)
    direction: RtlTransferDirection
    schedule_order: int = Field(ge=0)
    origin_stop_code: str = Field(min_length=1)
    origin_stop_name: str = Field(min_length=1)
    destination_stop_code: str = Field(min_length=1)
    destination_stop_name: str = Field(min_length=1)
    departure_time: time
    arrival_time: time
    observed_duration_minutes: int = Field(gt=0, le=180)
    raw_route_fare_value: Decimal | None = Field(default=None, ge=0)
    fare_currency: None = None


class RtlAirportHulhumaleFeasibilityHint(DomainModel):
    provider: Literal["rtl-public"] = "rtl-public"
    assurance: Literal[RtlTransferAssurance.FEASIBILITY_HINT] = (
        RtlTransferAssurance.FEASIBILITY_HINT
    )
    planner_eligible: Literal[False] = False
    source_url: Literal["https://bo.rtl.mv:4455/maldives/api/booking/v2/bus/routedetails"] = (
        "https://bo.rtl.mv:4455/maldives/api/booking/v2/bus/routedetails"
    )
    corroborating_source_url: Literal["https://velana.macl.aero/guide/transport"] = (
        "https://velana.macl.aero/guide/transport"
    )
    captured_at: AwareDatetime
    response_http_date: AwareDatetime | None = None
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_legs: tuple[RtlObservedScheduleLeg, ...]
    bidirectional_observed: bool
    service_date: None = None
    applicable_days: tuple[str, ...] = Field(default=(), max_length=0)
    service_hours_confirmed: Literal[False] = False
    price_currency_confirmed: Literal[False] = False
    requires_live_schedule_confirmation: Literal[True] = True
    operator_confirmation_window_hours: Literal[24] = 24
    operator_station_arrival_buffer_minutes: Literal[10] = 10
    supports_split_leg_roles_at_area_level: tuple[
        Literal[
            "airport_to_first_hulhumale_stay",
            "first_hulhumale_stay_to_airport",
            "airport_to_last_hulhumale_stay",
            "last_hulhumale_stay_to_airport",
        ],
        ...,
    ] = (
        "airport_to_first_hulhumale_stay",
        "first_hulhumale_stay_to_airport",
        "airport_to_last_hulhumale_stay",
        "last_hulhumale_stay_to_airport",
    )
    limitations_zh: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_assurance_boundary(self) -> RtlAirportHulhumaleFeasibilityHint:
        directions = {item.direction for item in self.observed_legs}
        expected_bidirectional = {
            RtlTransferDirection.AIRPORT_TO_HULHUMALE,
            RtlTransferDirection.HULHUMALE_TO_AIRPORT,
        } <= directions
        if self.bidirectional_observed != expected_bidirectional:
            raise ValueError("bidirectional flag must reflect observed schedule legs")
        return self


class _RouteSpec(DomainModel):
    route_number: Literal["R4", "R9"]
    direction: RtlTransferDirection
    origin_stop_code: str
    destination_stop_code: str


_ROUTE_SPECS = (
    _RouteSpec(
        route_number="R4",
        direction=RtlTransferDirection.HULHUMALE_TO_AIRPORT,
        origin_stop_code="114",
        destination_stop_code="306",
    ),
    _RouteSpec(
        route_number="R4",
        direction=RtlTransferDirection.AIRPORT_TO_HULHUMALE,
        origin_stop_code="11306",
        destination_stop_code="11401",
    ),
    _RouteSpec(
        route_number="R9",
        direction=RtlTransferDirection.HULHUMALE_TO_AIRPORT,
        origin_stop_code="601",
        destination_stop_code="306",
    ),
    _RouteSpec(
        route_number="R9",
        direction=RtlTransferDirection.AIRPORT_TO_HULHUMALE,
        origin_stop_code="11306",
        destination_stop_code="604",
    ),
)


class RtlAirportHulhumaleFeasibilityProvider:
    """Read current public RTL route evidence without creating bookable inventory."""

    name = "rtl-public"

    def __init__(
        self,
        config: RtlAirportHulhumaleConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or RtlAirportHulhumaleConfig()
        self._client = client or httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(UTC))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_hint(self) -> RtlAirportHulhumaleFeasibilityHint:
        try:
            response = await self._client.get(
                _PUBLIC_ROUTE_URL,
                headers={
                    "Accept": _EXPECTED_CONTENT_TYPE,
                    "User-Agent": self._config.user_agent,
                },
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                self.name,
                "timeout",
                "RTL public route request timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self.name,
                "network_error",
                f"RTL public route request failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        if response.is_redirect:
            raise ProviderError(
                self.name,
                "redirect_forbidden",
                "RTL public route endpoint redirected outside its fixed GET contract",
            )
        if response.status_code != 200:
            raise ProviderError(
                self.name,
                f"http_{response.status_code}",
                "RTL public route endpoint did not return HTTP 200",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        content_type = response.headers.get("content-type", "").partition(";")[0].strip()
        if content_type != _EXPECTED_CONTENT_TYPE:
            raise ProviderError(
                self.name,
                "invalid_content_type",
                "RTL public route endpoint did not return JSON",
            )
        if len(response.content) > self._config.max_response_bytes:
            raise ProviderError(
                self.name,
                "response_too_large",
                "RTL public route response exceeded the configured byte limit",
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                self.name,
                "invalid_json",
                "RTL public route response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                self.name,
                "schema_drift",
                "RTL public route response must be an object",
            )
        routes = payload.get("routeResponse")
        if not isinstance(routes, list):
            raise ProviderError(
                self.name,
                "schema_drift",
                "RTL public route response is missing routeResponse",
            )
        observed = tuple(leg for spec in _ROUTE_SPECS for leg in self._extract_legs(routes, spec))
        directions = {item.direction for item in observed}
        captured_at = self._utc_now()
        return RtlAirportHulhumaleFeasibilityHint(
            captured_at=captured_at,
            response_http_date=self._http_date(response),
            response_sha256=hashlib.sha256(response.content).hexdigest(),
            observed_legs=observed,
            bidirectional_observed={
                RtlTransferDirection.AIRPORT_TO_HULHUMALE,
                RtlTransferDirection.HULHUMALE_TO_AIRPORT,
            }
            <= directions,
            limitations_zh=(
                "接口响应未给服务日期或适用星期，当前可见时刻不能外推到目标旅行日。",
                "fare 只有数值而没有币种与税费口径，不得进入预算。",
                "公交站点只证明机场与 Hulhumalé 区域连通，不证明酒店门到门接送。",
                "官方门户要求出发前 24 小时向 RTL 复核；公开 GET 不能替代运营方确认。",
                "结果不是库存、预订或座位保证，不得转换为 Planner TransferOption。",
            ),
        )

    def _extract_legs(
        self,
        routes: list[Any],
        spec: _RouteSpec,
    ) -> tuple[RtlObservedScheduleLeg, ...]:
        route = next(
            (
                item
                for item in routes
                if isinstance(item, dict) and item.get("routeNumber") == spec.route_number
            ),
            None,
        )
        if route is None:
            return ()
        route_code = self._required_str(route, "code")
        route_name = self._required_str(route, "name")
        stops = route.get("busRouteStopList")
        if not isinstance(stops, list):
            raise ProviderError(
                self.name,
                "schema_drift",
                f"RTL {spec.route_number} is missing busRouteStopList",
            )
        origin = self._stop(stops, spec.origin_stop_code, spec.route_number)
        destination = self._stop(
            stops,
            spec.destination_stop_code,
            spec.route_number,
        )
        origin_times = self._timings(origin, spec.route_number)
        destination_times = self._timings(destination, spec.route_number)
        common_orders = sorted(set(origin_times) & set(destination_times))
        raw_fare = self._optional_decimal(route.get("fare"))
        legs: list[RtlObservedScheduleLeg] = []
        for order in common_orders:
            departure = origin_times[order]
            arrival = destination_times[order]
            duration = self._duration_minutes(departure, arrival)
            legs.append(
                RtlObservedScheduleLeg(
                    route_number=spec.route_number,
                    route_code=route_code,
                    route_name=route_name,
                    direction=spec.direction,
                    schedule_order=order,
                    origin_stop_code=spec.origin_stop_code,
                    origin_stop_name=self._required_str(origin, "name"),
                    destination_stop_code=spec.destination_stop_code,
                    destination_stop_name=self._required_str(destination, "name"),
                    departure_time=departure,
                    arrival_time=arrival,
                    observed_duration_minutes=duration,
                    raw_route_fare_value=raw_fare,
                )
            )
        return tuple(legs)

    def _stop(
        self,
        stops: list[Any],
        code: str,
        route_number: str,
    ) -> dict[str, Any]:
        value = next(
            (item for item in stops if isinstance(item, dict) and str(item.get("code")) == code),
            None,
        )
        if value is None:
            raise ProviderError(
                self.name,
                "schema_drift",
                f"RTL {route_number} is missing required stop {code}",
            )
        return value

    def _timings(
        self,
        stop: dict[str, Any],
        route_number: str,
    ) -> dict[int, time]:
        raw = stop.get("timings")
        if not isinstance(raw, list):
            raise ProviderError(
                self.name,
                "schema_drift",
                f"RTL {route_number} stop timings are missing",
            )
        result: dict[int, time] = {}
        for item in raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("order"), int)
                or not isinstance(item.get("timing"), str)
            ):
                raise ProviderError(
                    self.name,
                    "schema_drift",
                    f"RTL {route_number} contains an invalid timing row",
                )
            try:
                parsed = time.fromisoformat(item["timing"])
            except ValueError as exc:
                raise ProviderError(
                    self.name,
                    "schema_drift",
                    f"RTL {route_number} contains a non-ISO timing",
                ) from exc
            order = item["order"]
            if isinstance(order, bool) or order in result:
                raise ProviderError(
                    self.name,
                    "schema_drift",
                    f"RTL {route_number} contains a duplicate or invalid timing order",
                )
            result[order] = parsed
        return result

    def _duration_minutes(self, departure: time, arrival: time) -> int:
        anchor = datetime(2000, 1, 1)
        start = datetime.combine(anchor.date(), departure)
        end = datetime.combine(anchor.date(), arrival)
        if end <= start:
            end += timedelta(days=1)
        duration_seconds = int((end - start).total_seconds())
        if duration_seconds <= 0 or duration_seconds % 60:
            raise ProviderError(
                self.name,
                "schema_drift",
                "RTL observed leg duration is not a positive whole minute",
            )
        duration_minutes = duration_seconds // 60
        if duration_minutes > 180:
            raise ProviderError(
                self.name,
                "schema_drift",
                "RTL observed leg duration exceeds the feasibility boundary",
            )
        return duration_minutes

    def _required_str(self, value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ProviderError(
                self.name,
                "schema_drift",
                f"RTL field {key} must be a non-empty string",
            )
        return item.strip()

    def _optional_decimal(self, value: object) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return result if result.is_finite() and result >= 0 else None

    def _http_date(self, response: httpx.Response) -> datetime | None:
        raw = response.headers.get("date")
        if raw is None:
            return None
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("RTL feasibility clock must return an aware datetime")
        return value.astimezone(UTC)


__all__ = [
    "RtlAirportHulhumaleConfig",
    "RtlAirportHulhumaleFeasibilityHint",
    "RtlAirportHulhumaleFeasibilityProvider",
    "RtlObservedScheduleLeg",
    "RtlTransferAssurance",
    "RtlTransferDirection",
]
