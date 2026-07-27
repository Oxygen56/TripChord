from datetime import date

import pytest
from fastapi import HTTPException
from tripchord.auth import authenticate
from tripchord.config import Settings
from tripchord.domain.trip import TripSpec
from tripchord.persistence.database import Database
from tripchord.persistence.repository import WorkspaceNotFoundError, WorkspaceRepository


def test_static_token_authentication_and_production_guard() -> None:
    settings = Settings(auth_required=True, auth_tokens={"secret-a": "tenant-a"})

    assert authenticate(settings, "secret-a").tenant_id == "tenant-a"
    with pytest.raises(HTTPException) as missing:
        authenticate(settings, None)
    assert missing.value.status_code == 401
    with pytest.raises(ValueError, match="auth_tokens"):
        Settings(auth_required=True)


@pytest.mark.asyncio
async def test_workspace_repository_enforces_tenant_isolation() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    spec = TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 3),
    )
    async with database.sessions() as session:
        workspace = await WorkspaceRepository(session, "tenant-a").create(spec)
        assert (await WorkspaceRepository(session, "tenant-a").get(workspace.id)).id == workspace.id
        with pytest.raises(WorkspaceNotFoundError):
            await WorkspaceRepository(session, "tenant-b").get(workspace.id)
    await database.dispose()
