from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from tripchord.providers import RtlAirportHulhumaleFeasibilityProvider
from tripchord.providers.base import ProviderError
from tripchord.providers.rtl_feasibility import (
    RtlAirportHulhumaleConfig,
    RtlTransferAssurance,
    RtlTransferDirection,
)

NOW = datetime(2026, 7, 30, 13, 35, tzinfo=UTC)


def _stop(
    code: str,
    name: str,
    timings: tuple[tuple[int, str], ...],
) -> dict[str, object]:
    return {
        "code": code,
        "name": name,
        "timings": [{"order": order, "timing": timing} for order, timing in timings],
    }


def _route(
    number: str,
    code: str,
    name: str,
    fare: int,
    stops: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "routeNumber": number,
        "code": code,
        "name": name,
        "fare": fare,
        "busRouteStopList": list(stops),
    }


def _payload(*, include_r9_return: bool = True) -> dict[str, object]:
    r9_stops = [
        _stop("601", "Hiya Flats Stop 1", ((21, "08:00:00"),)),
        _stop("306", "VIA Domestic Terminal", ((21, "08:11:00"),)),
        _stop("11306", "VIA Domestic Terminal OPP", ((22, "08:30:00"),)),
    ]
    if include_r9_return:
        r9_stops.append(_stop("604", "Masjid Ameel Jaleel", ((22, "08:37:00"),)))
    return {
        "routeResponse": [
            _route(
                "R4",
                "124",
                "HM Phase 1 to VIA",
                10,
                (
                    _stop("114", "Amin Avenue", ((11, "07:00:00"),)),
                    _stop("306", "VIA Domestic Terminal", ((11, "07:05:00"),)),
                    _stop(
                        "11306",
                        "VIA Domestic Terminal OPP",
                        ((12, "07:30:00"),),
                    ),
                    _stop("11401", "Amin Avenue Opp", ((12, "07:40:00"),)),
                ),
            ),
            _route(
                "R9",
                "129",
                "HM Phase 2 to VIA",
                15,
                tuple(r9_stops),
            ),
        ],
        "atollRouteResponse": [],
    }


def _handler(
    payload: dict[str, object] | None = None,
    *,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    response_payload = payload or _payload()

    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "date": "Thu, 30 Jul 2026 13:35:31 GMT",
            },
            json=response_payload,
            request=request,
        )

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_public_get_returns_bidirectional_feasibility_without_planner_upgrade() -> None:
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=_handler(requests=requests),
        follow_redirects=False,
    ) as client:
        hint = await RtlAirportHulhumaleFeasibilityProvider(
            client=client,
            now=lambda: NOW,
        ).fetch_hint()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "https://bo.rtl.mv:4455/maldives/api/booking/v2/bus/routedetails"
    )
    assert request.content == b""
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert hint.assurance == RtlTransferAssurance.FEASIBILITY_HINT
    assert hint.planner_eligible is False
    assert hint.bidirectional_observed
    assert hint.service_date is None
    assert hint.applicable_days == ()
    assert hint.service_hours_confirmed is False
    assert hint.price_currency_confirmed is False
    assert hint.requires_live_schedule_confirmation
    assert hint.operator_confirmation_window_hours == 24
    assert hint.operator_station_arrival_buffer_minutes == 10
    assert hint.response_http_date == datetime(2026, 7, 30, 13, 35, 31, tzinfo=UTC)
    assert len(hint.response_sha256) == 64
    assert len(hint.observed_legs) == 4
    assert {item.direction for item in hint.observed_legs} == {
        RtlTransferDirection.AIRPORT_TO_HULHUMALE,
        RtlTransferDirection.HULHUMALE_TO_AIRPORT,
    }
    r4_outbound = next(
        item
        for item in hint.observed_legs
        if item.route_number == "R4" and item.direction == RtlTransferDirection.AIRPORT_TO_HULHUMALE
    )
    assert r4_outbound.origin_stop_name == "VIA Domestic Terminal OPP"
    assert r4_outbound.destination_stop_name == "Amin Avenue Opp"
    assert r4_outbound.observed_duration_minutes == 10
    assert r4_outbound.raw_route_fare_value == Decimal("10")
    assert r4_outbound.fare_currency is None
    assert "不得进入预算" in " ".join(hint.limitations_zh)
    assert "公开 GET 不能替代运营方确认" in " ".join(hint.limitations_zh)
    assert "不得转换为 Planner TransferOption" in " ".join(hint.limitations_zh)


@pytest.mark.asyncio
async def test_missing_required_route_stop_fails_closed_as_schema_drift() -> None:
    async with httpx.AsyncClient(
        transport=_handler(_payload(include_r9_return=False)),
    ) as client:
        with pytest.raises(ProviderError, match="missing required stop"):
            await RtlAirportHulhumaleFeasibilityProvider(
                client=client,
                now=lambda: NOW,
            ).fetch_hint()


@pytest.mark.asyncio
async def test_duplicate_schedule_order_fails_closed_as_schema_drift() -> None:
    payload = _payload()
    routes = payload["routeResponse"]
    assert isinstance(routes, list)
    route = routes[0]
    assert isinstance(route, dict)
    stops = route["busRouteStopList"]
    assert isinstance(stops, list)
    stop = stops[0]
    assert isinstance(stop, dict)
    timings = stop["timings"]
    assert isinstance(timings, list)
    timings.append({"order": 11, "timing": "07:01:00"})

    async with httpx.AsyncClient(transport=_handler(payload)) as client:
        with pytest.raises(ProviderError, match="duplicate or invalid timing order"):
            await RtlAirportHulhumaleFeasibilityProvider(
                client=client,
                now=lambda: NOW,
            ).fetch_hint()


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/login"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        follow_redirects=True,
    ) as client:
        with pytest.raises(ProviderError, match="redirected"):
            await RtlAirportHulhumaleFeasibilityProvider(
                client=client,
                now=lambda: NOW,
            ).fetch_hint()


@pytest.mark.asyncio
async def test_oversized_or_non_json_response_is_rejected() -> None:
    responses = (
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"x" * 2048,
        ),
        httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html>not json</html>",
        ),
    )
    for response in responses:

        def handle(request: httpx.Request, *, current: httpx.Response = response) -> httpx.Response:
            current.request = request
            return current

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            provider = RtlAirportHulhumaleFeasibilityProvider(
                RtlAirportHulhumaleConfig(max_response_bytes=1024),
                client=client,
                now=lambda: NOW,
            )
            with pytest.raises(ProviderError):
                await provider.fetch_hint()
