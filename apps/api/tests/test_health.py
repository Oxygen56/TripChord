import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.main import app


@pytest.mark.asyncio
async def test_health() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "tripchord"


@pytest.mark.asyncio
async def test_replay_offer_search_endpoint() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/offers/search",
            json={
                "kind": "flight",
                "origin": "上海",
                "destination": "北京",
                "start_date": "2026-10-01",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["offers"]) == 1
    assert body["offers"][0]["source"]["mode"] == "replay"
