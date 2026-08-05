from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

from tripchord.providers.browser_research import (
    BrowserResearchPolicy,
    ControlledBrowserResearchProvider,
)
from tripchord.providers.open_meteo import OpenMeteoProvider


async def evaluate() -> dict[str, Any]:
    weather = OpenMeteoProvider()
    browser = ControlledBrowserResearchProvider(
        BrowserResearchPolicy(allowed_domains=("dpm.org.cn",))
    )
    report: dict[str, Any] = {}
    try:
        coordinates = await weather.geocode_city("北京")
        today = date.today()
        windows = await weather.forecast(
            coordinates,
            start_date=today,
            end_date=today + timedelta(days=2),
        )
        report["open_meteo"] = {
            "ok": bool(windows),
            "coordinates": coordinates.model_dump(mode="json"),
            "days": len(windows),
            "source_mode": windows[0].source.mode.value if windows else None,
            "fresh": windows[0].source.is_fresh() if windows else False,
        }
        page = await browser.read_public_page("https://www.dpm.org.cn/Visit.html")
        report["controlled_browser"] = {
            "ok": page.status_code == 200 and bool(page.text_excerpt),
            "status": page.status_code,
            "final_url": page.final_url,
            "title": page.title,
            "sha256": page.content_sha256,
            "excerpt_chars": len(page.text_excerpt),
            "source_mode": page.source.mode.value,
            "fresh": page.source.is_fresh(),
        }
    finally:
        await weather.aclose()
        await browser.aclose()
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(evaluate()), ensure_ascii=False, indent=2))
