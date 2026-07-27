from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field

from tripchord.domain.common import Coordinates, DomainModel, Money
from tripchord.domain.source import SourceMode, SourceRecord
from tripchord.domain.travel_data import (
    Place,
    PlaceKind,
    RouteLeg,
    RouteMode,
    WeatherKind,
    WeatherWindow,
)
from tripchord.providers.base import ProviderError

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class AmapConfig(DomainModel):
    api_key: str = Field(min_length=1)
    base_url: str = "https://restapi.amap.com"
    source_mode: SourceMode = SourceMode.PRODUCTION
    cache_ttl_seconds: int = Field(default=300, gt=0, le=3600)


class AmapTravelDataProvider:
    name = "amap"

    def __init__(self, config: AmapConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(base_url=config.base_url, timeout=15)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def geocode(self, address: str, city: str | None = None) -> Coordinates:
        params = {"key": self._config.api_key, "address": address, "output": "JSON"}
        if city:
            params["city"] = city
        payload = await self._get("/v3/geocode/geo", params)
        geocodes = payload.get("geocodes", [])
        if not geocodes:
            raise ProviderError(self.name, "not_found", f"could not geocode {address!r}")
        return self._coordinates(str(geocodes[0]["location"]))

    async def search_places(
        self,
        keywords: str,
        city: str,
        *,
        types: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[Place, ...]:
        payload = await self._get(
            "/v3/place/text",
            {
                "key": self._config.api_key,
                "keywords": keywords,
                "city": city,
                "citylimit": "true",
                "types": "|".join(types),
                "offset": min(max(limit, 1), 25),
                "extensions": "all",
                "output": "JSON",
            },
        )
        source = self._source()
        places: list[Place] = []
        for raw in payload.get("pois", []):
            if not isinstance(raw, dict) or not raw.get("location"):
                continue
            raw_biz_ext = raw.get("biz_ext")
            biz_ext: dict[str, Any] = raw_biz_ext if isinstance(raw_biz_ext, dict) else {}
            rating = self._decimal(biz_ext.get("rating"))
            cost = self._decimal(biz_ext.get("cost"))
            places.append(
                Place(
                    id=f"amap:{raw.get('id')}",
                    name=str(raw.get("name") or "Unnamed place"),
                    kind=self._place_kind(str(raw.get("type") or "")),
                    coordinates=self._coordinates(str(raw["location"])),
                    address=self._string_or_none(raw.get("address")),
                    estimated_cost=(
                        Money(amount=cost, currency="CNY") if cost is not None else None
                    ),
                    rating=float(rating) if rating is not None else None,
                    raw_opening_hours=self._string_or_none(biz_ext.get("open_time")),
                    tags=tuple(
                        part.strip()
                        for part in str(raw.get("type") or "").split(";")
                        if part.strip()
                    ),
                    source=source,
                )
            )
        return tuple(places)

    async def route(
        self,
        origin: Coordinates,
        destination: Coordinates,
        mode: RouteMode,
        *,
        city: str | None = None,
    ) -> RouteLeg:
        path = {
            RouteMode.WALKING: "/v3/direction/walking",
            RouteMode.DRIVING: "/v3/direction/driving",
            RouteMode.TAXI: "/v3/direction/driving",
            RouteMode.TRANSIT: "/v3/direction/transit/integrated",
        }.get(mode)
        if path is None:
            raise ProviderError(self.name, "unsupported_mode", f"unsupported AMap mode: {mode}")
        params = {
            "key": self._config.api_key,
            "origin": self._coordinate_param(origin),
            "destination": self._coordinate_param(destination),
            "output": "JSON",
        }
        if mode == RouteMode.TRANSIT:
            if not city:
                raise ProviderError(self.name, "missing_city", "transit routing requires a city")
            params["city"] = city
        payload = await self._get(path, params)
        route = payload.get("route", {})
        candidates = route.get("transits") if mode == RouteMode.TRANSIT else route.get("paths")
        if not candidates:
            raise ProviderError(self.name, "route_not_found", "AMap returned no route")
        selected = candidates[0]
        duration_seconds = int(float(selected.get("duration", 0)))
        if duration_seconds <= 0:
            raise ProviderError(self.name, "invalid_route", "AMap route has no duration")
        distance = selected.get("distance")
        cost = self._decimal(selected.get("cost") or selected.get("tolls"))
        return RouteLeg(
            id=(
                f"amap:{mode}:{self._coordinate_param(origin)}:"
                f"{self._coordinate_param(destination)}"
            ),
            origin=origin,
            destination=destination,
            mode=mode,
            duration_minutes=max(round(duration_seconds / 60), 1),
            distance_meters=int(float(distance)) if distance not in (None, "") else None,
            estimated_cost=Money(amount=cost, currency="CNY") if cost is not None else None,
            source=self._source(),
        )

    async def weather(
        self,
        adcode: str,
        coordinates: Coordinates,
    ) -> tuple[WeatherWindow, ...]:
        payload = await self._get(
            "/v3/weather/weatherInfo",
            {
                "key": self._config.api_key,
                "city": adcode,
                "extensions": "all",
                "output": "JSON",
            },
        )
        forecasts = payload.get("forecasts", [])
        if not forecasts:
            return ()
        source = self._source()
        windows: list[WeatherWindow] = []
        for cast in forecasts[0].get("casts", []):
            forecast_date = date.fromisoformat(str(cast["date"]))
            starts_at = datetime.combine(forecast_date, time.min, tzinfo=SHANGHAI_TZ)
            day_temp = self._float_or_none(cast.get("daytemp"))
            night_temp = self._float_or_none(cast.get("nighttemp"))
            temperatures = [value for value in (day_temp, night_temp) if value is not None]
            description = f"{cast.get('dayweather', '')} {cast.get('nightweather', '')}".strip()
            windows.append(
                WeatherWindow(
                    location=coordinates,
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(days=1),
                    kind=self._weather_kind(description),
                    temperature_low_c=min(temperatures) if temperatures else None,
                    temperature_high_c=max(temperatures) if temperatures else None,
                    source=source,
                )
            )
        return tuple(windows)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.get(path, params=params)
        if not response.is_success:
            raise ProviderError(
                self.name,
                f"http_{response.status_code}",
                response.text,
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        payload: dict[str, Any] = response.json()
        if str(payload.get("status")) != "1":
            infocode = str(payload.get("infocode") or "unknown")
            raise ProviderError(
                self.name,
                infocode,
                str(payload.get("info") or "AMap request failed"),
                retryable=infocode in {"10003", "10004"},
            )
        return payload

    def _source(self) -> SourceRecord:
        captured = datetime.now(UTC)
        return SourceRecord(
            provider=self.name,
            mode=self._config.source_mode,
            captured_at=captured,
            expires_at=captured + timedelta(seconds=self._config.cache_ttl_seconds),
        )

    def _coordinates(self, value: str) -> Coordinates:
        longitude, latitude = value.split(",", maxsplit=1)
        return Coordinates(latitude=float(latitude), longitude=float(longitude))

    def _coordinate_param(self, value: Coordinates) -> str:
        return f"{value.longitude:.6f},{value.latitude:.6f}"

    def _decimal(self, value: Any) -> Decimal | None:
        if value in (None, "", []):
            return None
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    def _float_or_none(self, value: Any) -> float | None:
        try:
            return float(value) if value not in (None, "", []) else None
        except (TypeError, ValueError):
            return None

    def _string_or_none(self, value: Any) -> str | None:
        return str(value) if value not in (None, "", []) else None

    def _place_kind(self, raw_type: str) -> PlaceKind:
        if any(token in raw_type for token in ("风景名胜", "博物馆", "公园", "体育休闲")):
            return PlaceKind.ATTRACTION
        if "餐饮" in raw_type:
            return PlaceKind.RESTAURANT
        if "住宿" in raw_type:
            return PlaceKind.LODGING
        if "机场" in raw_type:
            return PlaceKind.AIRPORT
        if "交通设施" in raw_type:
            return PlaceKind.TRANSIT_STATION
        return PlaceKind.OTHER

    def _weather_kind(self, description: str) -> WeatherKind:
        if any(token in description for token in ("暴", "台风", "沙尘", "冰雹")):
            return WeatherKind.EXTREME
        if "雪" in description:
            return WeatherKind.SNOW
        if "雨" in description:
            return WeatherKind.RAIN
        if any(token in description for token in ("阴", "云")):
            return WeatherKind.CLOUDY
        if "晴" in description:
            return WeatherKind.CLEAR
        return WeatherKind.UNKNOWN
