import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.main import app


@pytest.mark.asyncio
async def test_health() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health", headers={"X-Request-ID": "test-request-1"})
        metrics = await client.get("/metrics")

    assert response.status_code == 200
    assert response.json()["service"] == "tripchord"
    assert response.headers["X-Request-ID"] == "test-request-1"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "tripchord_http_requests_total" in metrics.text
    assert 'route="/health"' in metrics.text


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


@pytest.mark.asyncio
async def test_trip_parse_endpoint_returns_missing_questions() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/trips/parse",
            json={"text": "想从上海去北京看历史建筑", "default_year": 2026},
        )

    assert response.status_code == 200
    assert response.json()["missing_fields"] == ["start_date", "end_date"]


@pytest.mark.asyncio
async def test_repair_endpoint_returns_a_versioned_diff() -> None:
    request = {
        "spec": {
            "origin": "上海",
            "destinations": ["北京"],
            "start_date": "2026-10-02",
            "end_date": "2026-10-02",
        },
        "plan": {
            "id": "trip-1:plan:v1",
            "trip_id": "trip-1",
            "version": 1,
            "items": [
                {
                    "id": "a",
                    "kind": "activity",
                    "title": "故宫",
                    "starts_at": "2026-10-02T09:00:00+08:00",
                    "ends_at": "2026-10-02T10:00:00+08:00",
                },
                {
                    "id": "b",
                    "kind": "activity",
                    "title": "景山",
                    "starts_at": "2026-10-02T10:00:00+08:00",
                    "ends_at": "2026-10-02T11:00:00+08:00",
                },
            ],
        },
        "context": {
            "travel_requirements": [
                {"from_item_id": "a", "to_item_id": "b", "minimum_minutes": 30}
            ]
        },
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/plans/repair", json=request)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["final_plan"]["version"] == 2
    assert body["traces"][0]["diff"]["changed_items"][0]["item_id"] == "b"


@pytest.mark.asyncio
async def test_event_replan_endpoint_reports_preservation() -> None:
    request = {
        "spec": {
            "origin": "上海",
            "destinations": ["北京"],
            "start_date": "2026-10-02",
            "end_date": "2026-10-02",
        },
        "plan": {
            "id": "trip-1:plan:v1",
            "trip_id": "trip-1",
            "version": 1,
            "items": [
                {
                    "id": "museum",
                    "kind": "activity",
                    "title": "博物馆",
                    "starts_at": "2026-10-02T09:00:00+08:00",
                    "ends_at": "2026-10-02T10:00:00+08:00",
                },
                {
                    "id": "park",
                    "kind": "activity",
                    "title": "公园",
                    "starts_at": "2026-10-02T11:00:00+08:00",
                    "ends_at": "2026-10-02T12:00:00+08:00",
                },
            ],
        },
        "event": {
            "id": "event-1",
            "trip_id": "trip-1",
            "kind": "place_closed",
            "occurred_at": "2026-10-01T08:00:00+08:00",
            "target_refs": ["museum"],
        },
        "dependencies": [],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/plans/replan", json=request)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["diff"]["removed_item_ids"] == ["museum"]
    assert body["unaffected_preservation_ratio"] == 1.0
