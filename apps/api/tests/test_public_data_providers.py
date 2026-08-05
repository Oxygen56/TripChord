from datetime import date

import httpx
import pytest
from tripchord.domain.common import Coordinates
from tripchord.domain.source import SourceMode
from tripchord.domain.travel_data import WeatherKind
from tripchord.providers.base import ProviderError
from tripchord.providers.browser_research import (
    BrowserResearchPolicy,
    ControlledBrowserResearchProvider,
)
from tripchord.providers.open_meteo import OpenMeteoProvider


def geocoding_handler(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": [{"name": "北京", "latitude": 39.9, "longitude": 116.4}]},
    )


def forecast_handler(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-request-id": "weather-request"},
        json={
            "daily": {
                "time": ["2026-08-01"],
                "weather_code": [63],
                "temperature_2m_max": [31.5],
                "temperature_2m_min": [23.0],
                "precipitation_probability_max": [70],
            }
        },
    )


@pytest.mark.asyncio
async def test_open_meteo_normalizes_public_production_weather() -> None:
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(geocoding_handler),
            base_url="https://geocode.test",
        ) as geocoding,
        httpx.AsyncClient(
            transport=httpx.MockTransport(forecast_handler),
            base_url="https://forecast.test",
        ) as forecast,
    ):
        provider = OpenMeteoProvider(
            forecast_client=forecast,
            geocoding_client=geocoding,
        )
        coordinates = await provider.geocode_city("北京")
        windows = await provider.forecast(
            coordinates,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
        )
    assert coordinates == Coordinates(latitude=39.9, longitude=116.4)
    assert windows[0].kind == WeatherKind.RAIN
    assert windows[0].precipitation_probability == pytest.approx(0.7)
    assert windows[0].source.mode == SourceMode.PRODUCTION
    assert windows[0].source.request_id == "weather-request"


@pytest.mark.asyncio
async def test_controlled_browser_enforces_allowlist_and_hashes_public_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.dpm.org.cn"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><title>开放时间</title><script>ignore()</script>"
                "<body>旺季 8:30 开馆</body></html>"
            ),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = ControlledBrowserResearchProvider(
            BrowserResearchPolicy(allowed_domains=("dpm.org.cn",)),
            client,
        )
        result = await provider.read_public_page("https://www.dpm.org.cn/Visit.html")
        with pytest.raises(ProviderError, match="not allowlisted"):
            await provider.read_public_page("https://example.com/private")
    assert result.title == "开放时间"
    assert "8:30" in result.text_excerpt
    assert "ignore" not in result.text_excerpt
    assert len(result.content_sha256) == 64
