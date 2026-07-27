from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripchord.domain.common import DomainModel
from tripchord.domain.events import PlanEvent
from tripchord.domain.itinerary import PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.persistence.models import EventRow, PlanRow, WorkspaceRow, utc_now


class StoredEvent(DomainModel):
    event: PlanEvent
    result: dict[str, object] | None = None
    created_at: datetime


class WorkspaceSnapshot(DomainModel):
    id: str
    title: str
    spec: TripSpec
    plans: tuple[PlanVersion, ...] = ()
    events: tuple[StoredEvent, ...] = ()
    created_at: datetime
    updated_at: datetime


class WorkspaceNotFoundError(LookupError):
    pass


class WorkspaceConflictError(RuntimeError):
    pass


class WorkspaceRepository:
    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create(self, spec: TripSpec, title: str | None = None) -> WorkspaceSnapshot:
        workspace_id = str(uuid4())
        row = WorkspaceRow(
            id=workspace_id,
            tenant_id=self._tenant_id,
            title=title or f"{spec.destinations[0]} {spec.day_count} 日自由行",
            spec=spec.model_dump(mode="json"),
        )
        self._session.add(row)
        await self._session.commit()
        return await self.get(workspace_id)

    async def get(self, workspace_id: str) -> WorkspaceSnapshot:
        row = await self._get_row(workspace_id)
        return self._snapshot(row)

    async def save_plan(self, workspace_id: str, plan: PlanVersion) -> WorkspaceSnapshot:
        row = await self._get_row(workspace_id)
        if plan.trip_id != workspace_id:
            raise WorkspaceConflictError("plan trip_id must equal the workspace id")
        existing = next((item for item in row.plans if item.version == plan.version), None)
        if existing is not None:
            if existing.payload == plan.model_dump(mode="json"):
                return self._snapshot(row)
            raise WorkspaceConflictError(f"plan version {plan.version} already exists")
        latest = max(row.plans, key=lambda item: item.version, default=None)
        expected_version = latest.version + 1 if latest is not None else 1
        expected_parent = latest.id if latest is not None else None
        if plan.version != expected_version or plan.parent_version_id != expected_parent:
            raise WorkspaceConflictError(
                f"expected plan version {expected_version} with parent {expected_parent}"
            )
        row.plans.append(
            PlanRow(
                id=plan.id,
                workspace_id=workspace_id,
                version=plan.version,
                payload=plan.model_dump(mode="json"),
            )
        )
        row.updated_at = utc_now()
        await self._session.commit()
        return await self.get(workspace_id)

    async def record_event(
        self,
        workspace_id: str,
        event: PlanEvent,
        result: DomainModel | None = None,
    ) -> WorkspaceSnapshot:
        row = await self._get_row(workspace_id)
        if event.trip_id != workspace_id:
            raise WorkspaceConflictError("event trip_id must equal the workspace id")
        existing = next((item for item in row.events if item.id == event.id), None)
        if existing is not None:
            if existing.payload == event.model_dump(mode="json"):
                return self._snapshot(row)
            raise WorkspaceConflictError(f"event {event.id} already exists with different data")
        row.events.append(
            EventRow(
                id=event.id,
                workspace_id=workspace_id,
                payload=event.model_dump(mode="json"),
                result=result.model_dump(mode="json") if result is not None else None,
            )
        )
        row.updated_at = utc_now()
        await self._session.commit()
        return await self.get(workspace_id)

    async def record_replan(
        self,
        workspace_id: str,
        event: PlanEvent,
        result: DomainModel,
        plan: PlanVersion | None,
    ) -> WorkspaceSnapshot:
        row = await self._get_row(workspace_id)
        if event.trip_id != workspace_id:
            raise WorkspaceConflictError("event trip_id must equal the workspace id")
        existing_event = next((item for item in row.events if item.id == event.id), None)
        if existing_event is not None:
            if existing_event.payload == event.model_dump(mode="json"):
                return self._snapshot(row)
            raise WorkspaceConflictError(f"event {event.id} already exists with different data")

        if plan is not None:
            if plan.trip_id != workspace_id:
                raise WorkspaceConflictError("plan trip_id must equal the workspace id")
            latest = max(row.plans, key=lambda item: item.version, default=None)
            expected_version = latest.version + 1 if latest is not None else 1
            expected_parent = latest.id if latest is not None else None
            if plan.version != expected_version or plan.parent_version_id != expected_parent:
                raise WorkspaceConflictError(
                    f"expected plan version {expected_version} with parent {expected_parent}"
                )
            row.plans.append(
                PlanRow(
                    id=plan.id,
                    workspace_id=workspace_id,
                    version=plan.version,
                    payload=plan.model_dump(mode="json"),
                )
            )

        row.events.append(
            EventRow(
                id=event.id,
                workspace_id=workspace_id,
                payload=event.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
            )
        )
        row.updated_at = utc_now()
        await self._session.commit()
        return await self.get(workspace_id)

    async def _get_row(self, workspace_id: str) -> WorkspaceRow:
        statement = (
            select(WorkspaceRow)
            .where(
                WorkspaceRow.id == workspace_id,
                WorkspaceRow.tenant_id == self._tenant_id,
            )
            .options(selectinload(WorkspaceRow.plans), selectinload(WorkspaceRow.events))
        )
        row = await self._session.scalar(statement)
        if row is None:
            raise WorkspaceNotFoundError(workspace_id)
        return row

    def _snapshot(self, row: WorkspaceRow) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            id=row.id,
            title=row.title,
            spec=TripSpec.model_validate(row.spec),
            plans=tuple(PlanVersion.model_validate(plan.payload) for plan in row.plans),
            events=tuple(
                StoredEvent(
                    event=PlanEvent.model_validate(event.payload),
                    result=event.result,
                    created_at=event.created_at,
                )
                for event in row.events
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
