import httpx
import pytest
from tripchord.domain.common import Coordinates
from tripchord.domain.source import SourceMode
from tripchord.domain.travel_data import PlaceKind, RouteMode, WeatherKind
from tripchord.providers.amap import AmapConfig, AmapTravelDataProvider


def amap_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["key"] == "key"
    if request.url.path.endswith("/geocode/geo"):
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "geocodes": [{"location": "116.397499,39.908722"}],
            },
        )
    if request.url.path.endswith("/place/text"):
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "pois": [
                    {
                        "id": "B000A",
                        "name": "示例博物馆",
                        "type": "科教文化服务;博物馆",
                        "address": "示例路1号",
                        "location": "116.400000,39.900000",
                        "biz_ext": {"rating": "4.8", "cost": "60", "open_time": "0900-1700"},
                    }
                ],
            },
        )
    if request.url.path.endswith("/direction/walking"):
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "route": {"paths": [{"distance": "1500", "duration": "1200"}]},
            },
        )
    if request.url.path.endswith("/weather/weatherInfo"):
        return httpx.Response(
            200,
            json={
                "status": "1",
                "infocode": "10000",
                "forecasts": [
                    {
                        "casts": [
                            {
                                "date": "2026-10-01",
                                "dayweather": "小雨",
                                "nightweather": "多云",
                                "daytemp": "18",
                                "nighttemp": "10",
                            }
                        ]
                    }
                ],
            },
        )
    raise AssertionError(f"unexpected request {request.url}")


@pytest.mark.asyncio
async def test_amap_normalizes_places_routes_weather_and_coordinates() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(amap_handler),
        base_url="https://restapi.amap.test",
    )
    provider = AmapTravelDataProvider(
        AmapConfig(
            api_key="key",
            base_url="https://restapi.amap.test",
            source_mode=SourceMode.PRODUCTION,
        ),
        client,
    )

    coordinates = await provider.geocode("天安门", "北京")
    places = await provider.search_places("博物馆", "北京")
    route = await provider.route(
        coordinates,
        Coordinates(latitude=39.9, longitude=116.4),
        RouteMode.WALKING,
    )
    weather = await provider.weather("110101", coordinates)
    await client.aclose()

    assert coordinates.longitude == pytest.approx(116.397499)
    assert places[0].kind == PlaceKind.ATTRACTION
    assert places[0].rating == pytest.approx(4.8)
    assert route.duration_minutes == 20
    assert route.distance_meters == 1500
    assert weather[0].kind == WeatherKind.RAIN
    assert weather[0].temperature_low_c == 10
