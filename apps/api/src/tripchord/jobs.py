from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripchord.domain.common import DomainModel
from tripchord.persistence.database import Database
from tripchord.persistence.models import JobRow, WorkspaceRow, utc_now
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
    attempts: int
    max_attempts: int
    lease_expires_at: datetime | None = None
    trace_id: str
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class JobNotFoundError(LookupError):
    pass


class JobConflictError(RuntimeError):
    pass


class JobRepository:
    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create(
        self,
        workspace_id: str,
        problem: PlanningProblem,
        idempotency_key: str | None = None,
        trace_id: str | None = None,
    ) -> JobSnapshot:
        workspace = await self._session.scalar(
            select(WorkspaceRow).where(
                WorkspaceRow.id == workspace_id,
                WorkspaceRow.tenant_id == self._tenant_id,
            )
        )
        if workspace is None:
            raise JobNotFoundError(workspace_id)
        if idempotency_key is not None:
            existing = await self._session.scalar(
                select(JobRow).where(
                    JobRow.workspace_id == workspace_id,
                    JobRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request != problem.model_dump(mode="json"):
                    raise JobConflictError(
                        "idempotency key was already used with a different planning problem"
                    )
                return self._snapshot(existing)
        row = JobRow(
            id=str(uuid4()),
            workspace_id=workspace_id,
            status=JobStatus.QUEUED,
            stage="queued",
            progress=0,
            attempts=0,
            max_attempts=3,
            idempotency_key=idempotency_key,
            trace_id=trace_id or str(uuid4()),
            request=problem.model_dump(mode="json"),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    async def claim(self, job_id: str, lease_seconds: int = 300) -> JobSnapshot | None:
        now = utc_now()
        row = await self._session.scalar(
            select(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.workspace.has(tenant_id=self._tenant_id),
                (
                    (JobRow.status == JobStatus.QUEUED)
                    | (
                        (JobRow.status == JobStatus.RUNNING)
                        & (
                            JobRow.lease_expires_at.is_(None)
                            | (JobRow.lease_expires_at < now)
                        )
                    )
                ),
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.attempts >= row.max_attempts:
            row.status = JobStatus.FAILED
            row.stage = "attempts_exhausted"
            row.progress = 100
            row.lease_expires_at = None
            row.updated_at = now
            await self._session.commit()
            return None
        row.status = JobStatus.RUNNING
        row.stage = "claimed"
        row.attempts += 1
        row.lease_expires_at = now + timedelta(seconds=lease_seconds)
        row.updated_at = now
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    async def schedule_retry(self, job_id: str, error: str) -> JobSnapshot:
        row = await self._locked_row(job_id)
        row.status = JobStatus.QUEUED if row.attempts < row.max_attempts else JobStatus.FAILED
        row.stage = "retry_scheduled" if row.status == JobStatus.QUEUED else "failed"
        row.progress = 0 if row.status == JobStatus.QUEUED else 100
        row.error = error
        row.lease_expires_at = None
        row.updated_at = utc_now()
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    async def recoverable(self) -> list[tuple[str, str, str, PlanningProblem]]:
        now = utc_now()
        rows = (
            await self._session.scalars(
                select(JobRow)
                .where(
                    (JobRow.status == JobStatus.QUEUED)
                    | (
                        (JobRow.status == JobStatus.RUNNING)
                        & (
                            JobRow.lease_expires_at.is_(None)
                            | (JobRow.lease_expires_at < now)
                        )
                    )
                )
                .options(selectinload(JobRow.workspace))
                .order_by(JobRow.created_at)
            )
        ).all()
        return [
            (
                row.id,
                row.workspace_id,
                row.workspace.tenant_id,
                PlanningProblem.model_validate(row.request),
            )
            for row in rows
        ]

    async def get(self, job_id: str) -> JobSnapshot:
        row = await self._session.scalar(
            select(JobRow).where(
                JobRow.id == job_id,
                JobRow.workspace.has(tenant_id=self._tenant_id),
            )
        )
        if row is None:
            raise JobNotFoundError(job_id)
        return self._snapshot(row)

    async def latest_problem(self, workspace_id: str) -> PlanningProblem | None:
        row = await self._session.scalar(
            select(JobRow)
            .where(
                JobRow.workspace_id == workspace_id,
                JobRow.workspace.has(tenant_id=self._tenant_id),
            )
            .order_by(JobRow.created_at.desc())
            .limit(1)
        )
        return PlanningProblem.model_validate(row.request) if row is not None else None

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
        row = await self._session.scalar(
            select(JobRow).where(
                JobRow.id == job_id,
                JobRow.workspace.has(tenant_id=self._tenant_id),
            )
        )
        if row is None:
            raise JobNotFoundError(job_id)
        row.status = status
        row.stage = stage
        row.progress = progress
        row.result = result
        row.error = error
        row.lease_expires_at = (
            utc_now() + timedelta(minutes=5) if status == JobStatus.RUNNING else None
        )
        row.updated_at = utc_now()
        await self._session.commit()
        await self._session.refresh(row)
        return self._snapshot(row)

    async def _locked_row(self, job_id: str) -> JobRow:
        row = await self._session.scalar(
            select(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.workspace.has(tenant_id=self._tenant_id),
            )
            .with_for_update()
        )
        if row is None:
            raise JobNotFoundError(job_id)
        return row

    def _snapshot(self, row: JobRow) -> JobSnapshot:
        return JobSnapshot(
            id=row.id,
            workspace_id=row.workspace_id,
            status=JobStatus(row.status),
            stage=row.stage,
            progress=row.progress,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            lease_expires_at=row.lease_expires_at,
            trace_id=row.trace_id,
            result=row.result,
            error=row.error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class PlanningJobRunner:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._tasks: set[asyncio.Task[None]] = set()

    def enqueue(
        self,
        job_id: str,
        workspace_id: str,
        problem: PlanningProblem,
        tenant_id: str = "anonymous",
    ) -> None:
        task = asyncio.create_task(self._run(job_id, workspace_id, problem, tenant_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def get(self, job_id: str, tenant_id: str = "anonymous") -> JobSnapshot:
        async with self._database.sessions() as session:
            return await JobRepository(session, tenant_id).get(job_id)

    async def recover(self) -> int:
        async with self._database.sessions() as session:
            rows = await JobRepository(session).recoverable()
        for job_id, workspace_id, tenant_id, problem in rows:
            self.enqueue(job_id, workspace_id, problem, tenant_id)
        return len(rows)

    async def _run(
        self,
        job_id: str,
        workspace_id: str,
        problem: PlanningProblem,
        tenant_id: str,
    ) -> None:
        async with self._database.sessions() as session:
            jobs = JobRepository(session, tenant_id)
            try:
                claimed = await jobs.claim(job_id)
                if claimed is None:
                    return
                await jobs.update(
                    job_id,
                    status=JobStatus.RUNNING,
                    stage="optimizing",
                    progress=35,
                )
                workspace = await WorkspaceRepository(session, tenant_id).get(workspace_id)
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
                await WorkspaceRepository(session, tenant_id).save_plan(workspace_id, final_plan)
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
                retry = await jobs.schedule_retry(job_id, str(exc))
                if retry.status == JobStatus.QUEUED:
                    self.enqueue(job_id, workspace_id, problem, tenant_id)
