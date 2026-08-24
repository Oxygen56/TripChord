from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.persistence.database import Database
from tripchord.persistence.repository import WorkspaceConflictError, WorkspaceRepository

ZONE = ZoneInfo("Asia/Shanghai")


def trip() -> TripSpec:
    return TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 3),
    )


def plan(workspace_id: str, version: int, parent: str | None = None) -> PlanVersion:
    return PlanVersion(
        id=f"{workspace_id}:plan:v{version}",
        trip_id=workspace_id,
        version=version,
        parent_version_id=parent,
        items=(
            ItineraryItem(
                id="museum",
                kind=ItemKind.ACTIVITY,
                title="故宫",
                starts_at=datetime(2026, 10, 2, 9, tzinfo=ZONE),
                ends_at=datetime(2026, 10, 2, 11, tzinfo=ZONE),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_workspace_round_trip_and_version_lineage() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = WorkspaceRepository(session)
        created = await repository.create(trip())
        first = plan(created.id, 1)
        with_first = await repository.save_plan(created.id, first)
        second = plan(created.id, 2, first.id)
        with_second = await repository.save_plan(created.id, second)

        assert with_first.plans == (first,)
        assert with_second.plans == (first, second)
        assert (await repository.get(created.id)).spec == trip()
    await database.dispose()


@pytest.mark.asyncio
async def test_plan_writes_are_idempotent_but_lineage_conflicts_block() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = WorkspaceRepository(session)
        workspace = await repository.create(trip())
        first = plan(workspace.id, 1)
        await repository.save_plan(workspace.id, first)

        repeated = await repository.save_plan(workspace.id, first)
        assert repeated.plans == (first,)
        with pytest.raises(WorkspaceConflictError):
            await repository.save_plan(workspace.id, plan(workspace.id, 3, first.id))
    await database.dispose()


@pytest.mark.asyncio
async def test_event_recording_is_idempotent() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = WorkspaceRepository(session)
        workspace = await repository.create(trip())
        event = PlanEvent(
            id="weather-1",
            trip_id=workspace.id,
            kind=EventKind.WEATHER_ALERT,
            occurred_at=datetime(2026, 10, 1, 8, tzinfo=ZONE),
            target_refs=("museum",),
        )

        first = await repository.record_event(workspace.id, event)
        repeated = await repository.record_event(workspace.id, event)

        assert len(first.events) == 1
        assert repeated.events == first.events
    await database.dispose()


@pytest.mark.asyncio
async def test_file_sqlite_uses_wal_and_waits_for_concurrent_writers(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tripchord.db'}")
    await database.create_schema()
    async with database.engine.connect() as connection:
        journal_mode = await connection.exec_driver_sql("PRAGMA journal_mode")
        busy_timeout = await connection.exec_driver_sql("PRAGMA busy_timeout")

        assert journal_mode.scalar_one().lower() == "wal"
        assert busy_timeout.scalar_one() == 30_000
    await database.dispose()
