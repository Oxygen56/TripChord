from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from tripchord.domain.common import Coordinates, DomainModel
from tripchord.domain.source import SourceMode, SourceRecord
from tripchord.domain.travel_data import WeatherKind, WeatherWindow
from tripchord.providers.base import ProviderError


class OpenMeteoConfig(DomainModel):
    forecast_base_url: str = "https://api.open-meteo.com"
    geocoding_base_url: str = "https://geocoding-api.open-meteo.com"
    source_mode: SourceMode = SourceMode.PRODUCTION
    cache_ttl_seconds: int = Field(default=1800, gt=0, le=3600)


class OpenMeteoProvider:
    """Authorised public weather source; not a transport or inventory price source."""

    name = "open-meteo"

    def __init__(
        self,
        config: OpenMeteoConfig | None = None,
        *,
        forecast_client: httpx.AsyncClient | None = None,
        geocoding_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or OpenMeteoConfig()
        self._forecast_client = forecast_client or httpx.AsyncClient(
            base_url=self._config.forecast_base_url,
            timeout=15,
        )
        self._geocoding_client = geocoding_client or httpx.AsyncClient(
            base_url=self._config.geocoding_base_url,
            timeout=15,
        )
        self._owns_forecast = forecast_client is None
        self._owns_geocoding = geocoding_client is None

    async def aclose(self) -> None:
        if self._owns_forecast:
            await self._forecast_client.aclose()
        if self._owns_geocoding:
            await self._geocoding_client.aclose()

    async def geocode_city(self, name: str, *, country_code: str = "CN") -> Coordinates:
        response = await self._geocoding_client.get(
            "/v1/search",
            params={
                "name": name,
                "count": 1,
                "language": "zh",
                "format": "json",
                "countryCode": country_code,
            },
        )
        payload = self._payload(response, "geocoding")
        results = payload.get("results", [])
        if not results:
            raise ProviderError(self.name, "not_found", f"city not found: {name}")
        first = results[0]
        return Coordinates(
            latitude=float(first["latitude"]),
            longitude=float(first["longitude"]),
        )

    async def forecast(
        self,
        coordinates: Coordinates,
        *,
        start_date: date,
        end_date: date,
        timezone: str = "Asia/Shanghai",
    ) -> tuple[WeatherWindow, ...]:
        response = await self._forecast_client.get(
            "/v1/forecast",
            params={
                "latitude": coordinates.latitude,
                "longitude": coordinates.longitude,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "timezone": timezone,
            },
        )
        payload = self._payload(response, "forecast")
        daily = payload.get("daily", {})
        dates = daily.get("time", [])
        codes = daily.get("weather_code", [])
        highs = daily.get("temperature_2m_max", [])
        lows = daily.get("temperature_2m_min", [])
        precipitation = daily.get("precipitation_probability_max", [])
        zone = ZoneInfo(timezone)
        source = self._source(response)
        windows: list[WeatherWindow] = []
        for index, raw_date in enumerate(dates):
            day = date.fromisoformat(str(raw_date))
            starts = datetime.combine(day, time.min, tzinfo=zone)
            probability = self._number(self._at(precipitation, index))
            weather_code = self._number(self._at(codes, index))
            windows.append(
                WeatherWindow(
                    location=coordinates,
                    starts_at=starts,
                    ends_at=starts + timedelta(days=1),
                    kind=self._weather_kind(int(weather_code if weather_code is not None else -1)),
                    temperature_low_c=self._float(self._at(lows, index)),
                    temperature_high_c=self._float(self._at(highs, index)),
                    precipitation_probability=(
                        probability / 100 if probability is not None else None
                    ),
                    source=source,
                )
            )
        return tuple(windows)

    def _payload(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        if not response.is_success:
            raise ProviderError(
                self.name,
                f"http_{response.status_code}",
                f"Open-Meteo {operation} failed: {response.text[:300]}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        payload: dict[str, Any] = response.json()
        return payload

    def _source(self, response: httpx.Response) -> SourceRecord:
        captured = datetime.now(UTC)
        return SourceRecord(
            provider=self.name,
            mode=self._config.source_mode,
            request_id=response.headers.get("x-request-id"),
            captured_at=captured,
            expires_at=captured + timedelta(seconds=self._config.cache_ttl_seconds),
        )

    def _weather_kind(self, code: int) -> WeatherKind:
        if code in {95, 96, 99}:
            return WeatherKind.EXTREME
        if code in {71, 73, 75, 77, 85, 86}:
            return WeatherKind.SNOW
        if code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}:
            return WeatherKind.RAIN
        if code in {1, 2, 3, 45, 48}:
            return WeatherKind.CLOUDY
        if code == 0:
            return WeatherKind.CLEAR
        return WeatherKind.UNKNOWN

    def _at(self, values: object, index: int) -> object | None:
        return values[index] if isinstance(values, list) and index < len(values) else None

    def _float(self, value: object | None) -> float | None:
        return self._number(value)

    def _number(self, value: object | None) -> float | None:
        return float(value) if isinstance(value, int | float) else None
