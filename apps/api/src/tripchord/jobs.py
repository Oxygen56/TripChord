from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord.domain.common import DomainModel
from tripchord.persistence.database import Database
from tripchord.persistence.models import JobRow, utc_now
from tripchord.persistence.repository import WorkspaceRepository
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningProblem
from tripchord.planning.workflow import PlanningWorkflow


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobSnapshot(DomainModel):
    id: str
    workspace_id: str
    status: JobStatus
    stage: str
    progress: int
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class JobNotFoundError(LookupError):
    pass


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, workspace_id: str, problem: PlanningProblem) -> JobSnapshot:
        row = JobRow(
            id=str(uuid4()),
            workspace_id=workspace_id,
            status=JobStatus.QUEUED,
            stage="queued",
            progress=0,
            request=problem.model_dump(mode="json"),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    async def get(self, job_id: str) -> JobSnapshot:
        row = await self._session.scalar(select(JobRow).where(JobRow.id == job_id))
        if row is None:
            raise JobNotFoundError(job_id)
        return self._snapshot(row)

    async def update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        stage: str,
        progress: int,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobSnapshot:
        row = await self._session.scalar(select(JobRow).where(JobRow.id == job_id))
        if row is None:
            raise JobNotFoundError(job_id)
        row.status = status
        row.stage = stage
        row.progress = progress
        row.result = result
        row.error = error
        row.updated_at = utc_now()
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    def _snapshot(self, row: JobRow) -> JobSnapshot:
        return JobSnapshot(
            id=row.id,
            workspace_id=row.workspace_id,
            status=JobStatus(row.status),
            stage=row.stage,
            progress=row.progress,
            result=row.result,
            error=row.error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class PlanningJobRunner:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._tasks: set[asyncio.Task[None]] = set()

    def enqueue(self, job_id: str, workspace_id: str, problem: PlanningProblem) -> None:
        task = asyncio.create_task(self._run(job_id, workspace_id, problem))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def get(self, job_id: str) -> JobSnapshot:
        async with self._database.sessions() as session:
            return await JobRepository(session).get(job_id)

    async def _run(self, job_id: str, workspace_id: str, problem: PlanningProblem) -> None:
        async with self._database.sessions() as session:
            jobs = JobRepository(session)
            try:
                await jobs.update(
                    job_id,
                    status=JobStatus.RUNNING,
                    stage="optimizing",
                    progress=35,
                )
                workspace = await WorkspaceRepository(session).get(workspace_id)
                version = len(workspace.plans) + 1
                parent = workspace.plans[-1].id if workspace.plans else None
                optimizer = ItineraryOptimizer()
                solved = await asyncio.to_thread(optimizer.solve, problem)
                draft = optimizer.to_plan(
                    solved,
                    problem,
                    trip_id=workspace_id,
                    plan_id=f"{workspace_id}:plan:v{version}",
                    version=version,
                ).model_copy(update={"parent_version_id": parent})
                await jobs.update(
                    job_id,
                    status=JobStatus.RUNNING,
                    stage="verifying",
                    progress=75,
                )
                workflow = PlanningWorkflow().run(problem.trip, draft)
                if workflow.status != "ready":
                    raise RuntimeError(
                        f"planning workflow ended with {workflow.status}: "
                        f"{len(workflow.remaining_violations)} violations"
                    )
                final_plan = workflow.final_plan.model_copy(
                    update={
                        "id": f"{workspace_id}:plan:v{version}",
                        "version": version,
                        "parent_version_id": parent,
                    }
                )
                await WorkspaceRepository(session).save_plan(workspace_id, final_plan)
                await jobs.update(
                    job_id,
                    status=JobStatus.SUCCEEDED,
                    stage="complete",
                    progress=100,
                    result={
                        "plan": final_plan.model_dump(mode="json"),
                        "optimization": solved.model_dump(mode="json"),
                    },
                )
            except Exception as exc:
                await session.rollback()
                await jobs.update(
                    job_id,
                    status=JobStatus.FAILED,
                    stage="failed",
                    progress=100,
                    error=str(exc),
                )
