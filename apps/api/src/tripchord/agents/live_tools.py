from __future__ import annotations

from datetime import date

from pydantic import JsonValue, TypeAdapter

from tripchord.agents.models import AgentRole, ToolPermission
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec
from tripchord.providers.browser_research import ControlledBrowserResearchProvider
from tripchord.providers.open_meteo import OpenMeteoProvider


class LiveResearchTools:
    """Registers authorised read-only providers; owns no booking or payment capability."""

    def __init__(
        self,
        weather: OpenMeteoProvider,
        browser: ControlledBrowserResearchProvider,
    ) -> None:
        self._weather = weather
        self._browser = browser

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="search_weather",
                description="通过 Open-Meteo 官方公开 API 查询城市天气预报",
                permission=ToolPermission.READ_ONLY_EXTERNAL,
                allowed_roles=(AgentRole.WEATHER,),
                input_schema={
                    "type": "object",
                    "properties": {
                        "destination": {"type": "string"},
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                    },
                    "required": ["destination", "start_date", "end_date"],
                },
            ),
            self._search_weather,
        )
        registry.register(
            ToolSpec(
                name="research_official_page",
                description="读取白名单中的官方公开网页，不登录、不执行交易",
                permission=ToolPermission.READ_ONLY_EXTERNAL,
                allowed_roles=(AgentRole.BROWSER_RESEARCH,),
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            self._research_page,
        )

    async def aclose(self) -> None:
        await self._weather.aclose()
        await self._browser.aclose()

    async def _search_weather(self, call: ToolCall) -> dict[str, JsonValue]:
        destination = self._string_argument(call, "destination")
        start_date = date.fromisoformat(self._string_argument(call, "start_date"))
        end_date = date.fromisoformat(self._string_argument(call, "end_date"))
        coordinates = await self._weather.geocode_city(destination)
        windows = await self._weather.forecast(
            coordinates,
            start_date=start_date,
            end_date=end_date,
        )
        return TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                "provider": self._weather.name,
                "source_mode": "production",
                "coordinates": coordinates.model_dump(mode="json"),
                "windows": [window.model_dump(mode="json") for window in windows],
            }
        )

    async def _research_page(self, call: ToolCall) -> dict[str, JsonValue]:
        result = await self._browser.read_public_page(self._string_argument(call, "url"))
        return TypeAdapter(dict[str, JsonValue]).validate_python(result.model_dump(mode="json"))

    def _string_argument(self, call: ToolCall, key: str) -> str:
        value = call.arguments.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"tool argument {key!r} must be a non-empty string")
        return value
