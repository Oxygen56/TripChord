"""Provider selection DB-backed persistence contract tests (v0.2 deviation)."""

from __future__ import annotations

import pytest
from tripchord.persistence.database import Database
from tripchord.persistence.provider_selection import ProviderSelectionRepository


@pytest.mark.asyncio
async def test_provider_selection_repository_round_trip() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = ProviderSelectionRepository(session, tenant_id="tenant-a")
        assert await repository.load_all() == {}
        await repository.set_enabled("ctrip:flight", False)
        loaded = await repository.load_all()
        assert loaded == {"ctrip:flight": False}
        await repository.set_enabled("ctrip:flight", True)
        assert await repository.load_all() == {"ctrip:flight": True}
        await repository.set_enabled("qunar:lodging", False)
        assert await repository.load_all() == {
            "ctrip:flight": True,
            "qunar:lodging": False,
        }
    await database.dispose()


@pytest.mark.asyncio
async def test_provider_selection_repository_tenant_isolation() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository_a = ProviderSelectionRepository(session, tenant_id="tenant-a")
        await repository_a.set_enabled("ctrip:flight", False)
    async with database.sessions() as session:
        repository_b = ProviderSelectionRepository(session, tenant_id="tenant-b")
        assert await repository_b.load_all() == {}
    await database.dispose()
