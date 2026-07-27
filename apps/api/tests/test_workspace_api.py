from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tripchord.main import app, get_session
from tripchord.persistence.database import Database


@pytest.mark.asyncio
async def test_persisted_workspace_plan_replan_and_diff_flow() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with database.sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            created_response = await client.post(
                "/api/v1/workspaces",
                json={
                    "title": "北京周末",
                    "spec": {
                        "origin": "上海",
                        "destinations": ["北京"],
                        "start_date": "2026-10-02",
                        "end_date": "2026-10-03",
                    },
                },
            )
            assert created_response.status_code == 201
            workspace_id = created_response.json()["id"]
            plan = {
                "id": f"{workspace_id}:plan:v1",
                "trip_id": workspace_id,
                "version": 1,
                "items": [
                    {
                        "id": "museum",
                        "kind": "activity",
                        "title": "博物馆",
                        "starts_at": "2026-10-02T09:00:00+08:00",
                        "ends_at": "2026-10-02T11:00:00+08:00",
                    },
                    {
                        "id": "park",
                        "kind": "activity",
                        "title": "公园",
                        "starts_at": "2026-10-02T13:00:00+08:00",
                        "ends_at": "2026-10-02T15:00:00+08:00",
                    },
                ],
            }
            saved = await client.post(
                f"/api/v1/workspaces/{workspace_id}/plans",
                json={"plan": plan},
            )
            assert saved.status_code == 200
            assert len(saved.json()["plans"]) == 1

            replanned = await client.post(
                f"/api/v1/workspaces/{workspace_id}/events/replan",
                json={
                    "event": {
                        "id": "closure-1",
                        "trip_id": workspace_id,
                        "kind": "place_closed",
                        "occurred_at": "2026-10-01T08:00:00+08:00",
                        "target_refs": ["museum"],
                    },
                    "dependencies": [],
                },
            )
            assert replanned.status_code == 200
            body = replanned.json()
            assert body["result"]["status"] == "ready"
            assert len(body["workspace"]["plans"]) == 2
            assert len(body["workspace"]["events"]) == 1

            compared = await client.get(
                f"/api/v1/workspaces/{workspace_id}/plans/1/diff/2"
            )
            assert compared.status_code == 200
            assert compared.json()["removed_item_ids"] == ["museum"]

            loaded = await client.get(f"/api/v1/workspaces/{workspace_id}")
            assert loaded.status_code == 200
            assert loaded.json()["title"] == "北京周末"
    finally:
        app.dependency_overrides.clear()
        await database.dispose()
