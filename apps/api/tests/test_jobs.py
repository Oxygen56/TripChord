import asyncio
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tripchord.domain.trip import TripSpec
from tripchord.jobs import JobConflictError, JobRepository, JobStatus, PlanningJobRunner
from tripchord.main import app, get_job_runner, get_session
from tripchord.persistence.database import Database
from tripchord.persistence.repository import WorkspaceRepository
from tripchord.planning.problem import ActivityAvailability, ActivityCandidate, PlanningProblem


def problem() -> PlanningProblem:
    trip = TripSpec(
        origin="上海",
        destinations=("北京",),
        start_date=date(2026, 10, 2),
        end_date=date(2026, 10, 2),
    )
    return PlanningProblem(
        trip=trip,
        activities=(
            ActivityCandidate(
                id="museum",
                title="博物馆",
                duration_minutes=120,
                utility=300,
                source_refs=("replay:poi-1",),
                availability=(
                    ActivityAvailability(
                        date=date(2026, 10, 2),
                        start_minute=540,
                        end_minute=1020,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_persistent_planning_job_reaches_success_and_saves_plan(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    await database.create_schema()
    async with database.sessions() as session:
        workspace = await WorkspaceRepository(session).create(problem().trip)
        job = await JobRepository(session).create(workspace.id, problem())

    runner = PlanningJobRunner(database)
    runner.enqueue(job.id, workspace.id, problem())
    latest = job
    for _ in range(100):
        latest = await runner.get(job.id)
        if latest.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            break
        await asyncio.sleep(0.02)

    assert latest.status == JobStatus.SUCCEEDED
    assert latest.progress == 100
    async with database.sessions() as session:
        stored = await WorkspaceRepository(session).get(workspace.id)
        assert len(stored.plans) == 1
        assert stored.plans[0].items[0].title == "博物馆"
    await database.dispose()


@pytest.mark.asyncio
async def test_planning_job_api_streams_terminal_progress(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stream.db'}")
    await database.create_schema()
    async with database.sessions() as session:
        workspace = await WorkspaceRepository(session).create(problem().trip)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with database.sessions() as session:
            yield session

    runner = PlanningJobRunner(database)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_job_runner] = lambda: runner
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            created = await client.post(
                f"/api/v1/workspaces/{workspace.id}/jobs/planning",
                json={"problem": problem().model_dump(mode="json")},
            )
            assert created.status_code == 202
            job_id = created.json()["id"]

            streamed = await client.get(f"/api/v1/workspaces/{workspace.id}/jobs/{job_id}/events")
            assert streamed.status_code == 200
            assert "event: job" in streamed.text
            assert '"status": "succeeded"' in streamed.text
    finally:
        app.dependency_overrides.clear()
        await database.dispose()


@pytest.mark.asyncio
async def test_start_trip_endpoint_assembles_and_persists_a_plan(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'start.db'}")
    await database.create_schema()

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with database.sessions() as session:
            yield session

    runner = PlanningJobRunner(database)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_job_runner] = lambda: runner
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            started = await client.post(
                "/api/v1/trips/plan",
                json={
                    "title": "北京历史线",
                    "spec": {
                        "origin": "上海",
                        "destinations": ["北京"],
                        "start_date": "2026-10-02",
                        "end_date": "2026-10-03",
                        "interests": ["历史"],
                        "must_visit": ["故宫"],
                    },
                },
            )
            assert started.status_code == 202
            body = started.json()
            workspace_id = body["workspace"]["id"]
            job_id = body["job"]["id"]
            assert body["data_mode"] == "replay"
            assert body["candidate_count"] >= 3

            terminal = await client.get(f"/api/v1/workspaces/{workspace_id}/jobs/{job_id}/events")
            assert '"status": "succeeded"' in terminal.text
            workspace = await client.get(f"/api/v1/workspaces/{workspace_id}")
            assert len(workspace.json()["plans"]) == 1
            titles = [item["title"] for item in workspace.json()["plans"][0]["items"]]
            assert "故宫博物院" in titles
    finally:
        app.dependency_overrides.clear()
        await database.dispose()


@pytest.mark.asyncio
async def test_job_idempotency_and_single_claim(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'idempotency.db'}")
    await database.create_schema()
    async with database.sessions() as session:
        workspace = await WorkspaceRepository(session).create(
            problem().trip,
            idempotency_key="workspace-request-1",
        )
        repeated_workspace = await WorkspaceRepository(session).create(
            problem().trip,
            idempotency_key="workspace-request-1",
        )
        repository = JobRepository(session)
        job = await repository.create(
            workspace.id,
            problem(),
            idempotency_key="planning-request-1",
        )
        repeated_job = await repository.create(
            workspace.id,
            problem(),
            idempotency_key="planning-request-1",
        )
        first_claim = await repository.claim(job.id)
        duplicate_claim = await repository.claim(job.id)

        assert repeated_workspace.id == workspace.id
        assert repeated_job.id == job.id
        assert first_claim is not None
        assert first_claim.attempts == 1
        assert duplicate_claim is None
        with pytest.raises(JobConflictError):
            changed = problem().model_copy(update={"solver_time_limit_seconds": 3})
            await repository.create(
                workspace.id,
                changed,
                idempotency_key="planning-request-1",
            )
    await database.dispose()


@pytest.mark.asyncio
async def test_runner_recovers_persisted_queued_job(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    await database.create_schema()
    async with database.sessions() as session:
        workspace = await WorkspaceRepository(session).create(problem().trip)
        job = await JobRepository(session).create(workspace.id, problem())

    runner = PlanningJobRunner(database)
    assert await runner.recover() == 1
    latest = job
    for _ in range(100):
        latest = await runner.get(job.id)
        if latest.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            break
        await asyncio.sleep(0.02)

    assert latest.status == JobStatus.SUCCEEDED
    assert latest.attempts == 1
    assert latest.lease_expires_at is None
    await database.dispose()
