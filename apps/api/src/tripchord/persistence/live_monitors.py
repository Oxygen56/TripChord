"""Persistence for opt-in live-quote monitors (v0.9).

A monitor is created by an explicit user action and revalidates one live
component per round.  Before this module its status and check history lived
only in the process-local registry, so a restart silently dropped the record.
Here the status (plus the boundary text in force) and the append-only check
history are persisted per tenant, and the registry can rehydrate ACTIVE
monitors whose run context is still resolvable after a restart.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripchord.agents.live_monitor import (
    LiveMonitorCheck,
    LiveMonitorState,
    LiveMonitorStatus,
)
from tripchord.persistence.database import Database
from tripchord.persistence.models import LiveMonitorCheckRow, LiveMonitorRow


class LiveMonitorNotFoundError(LookupError):
    pass


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops timezone info on read; these columns are always stored UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _check_to_row(monitor_id: str, check: LiveMonitorCheck) -> LiveMonitorCheckRow:
    return LiveMonitorCheckRow(
        monitor_id=monitor_id,
        sequence=check.sequence,
        checked_at=check.checked_at,
        target_component_id=check.target_component_id,
        event_id=check.event_id,
        applied_disposition=check.applied_disposition,
        decision_state=check.decision_state,
        package_changed=check.package_changed,
        summary=check.summary,
    )


def _check_from_row(row: LiveMonitorCheckRow) -> LiveMonitorCheck:
    return LiveMonitorCheck(
        sequence=row.sequence,
        checked_at=_as_utc(row.checked_at),
        target_component_id=row.target_component_id,
        event_id=row.event_id,
        applied_disposition=row.applied_disposition,
        decision_state=row.decision_state,
        package_changed=row.package_changed,
        summary=row.summary,
    )


def _status_from_row(row: LiveMonitorRow) -> LiveMonitorStatus:
    checks = tuple(_check_from_row(item) for item in row.checks)
    return LiveMonitorStatus(
        id=row.id,
        run_id=row.run_id,
        state=LiveMonitorState(row.state),
        interval_seconds=row.interval_seconds,
        max_checks=row.max_checks,
        timeout_seconds=row.timeout_seconds,
        check_count=row.check_count,
        next_check_at=_as_utc(row.next_check_at),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        last_check=checks[-1] if checks else None,
        last_error=row.last_error,
        boundary=row.boundary,
    )


def _status_to_row(
    tenant_id: str,
    status: LiveMonitorStatus,
    row: LiveMonitorRow | None,
) -> LiveMonitorRow:
    if row is None:
        return LiveMonitorRow(
            id=status.id,
            tenant_id=tenant_id,
            run_id=status.run_id,
            state=status.state.value,
            interval_seconds=status.interval_seconds,
            max_checks=status.max_checks,
            timeout_seconds=status.timeout_seconds,
            check_count=status.check_count,
            next_check_at=status.next_check_at,
            created_at=status.created_at,
            updated_at=status.updated_at,
            last_error=status.last_error,
            boundary=status.boundary,
        )
    row.run_id = status.run_id
    row.state = status.state.value
    row.interval_seconds = status.interval_seconds
    row.max_checks = status.max_checks
    row.timeout_seconds = status.timeout_seconds
    row.check_count = status.check_count
    row.next_check_at = status.next_check_at
    row.created_at = status.created_at
    row.updated_at = status.updated_at
    row.last_error = status.last_error
    row.boundary = status.boundary
    return row


class LiveMonitorRepository:
    """Tenant-scoped persistence for :class:`LiveMonitorStatus` records."""

    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def save_status(self, status: LiveMonitorStatus) -> None:
        existing = await self._session.scalar(
            select(LiveMonitorRow).where(
                LiveMonitorRow.id == status.id,
                LiveMonitorRow.tenant_id == self._tenant_id,
            )
        )
        row = _status_to_row(self._tenant_id, status, existing)
        if existing is None:
            self._session.add(row)
        await self._session.commit()

    async def append_check(self, monitor_id: str, check: LiveMonitorCheck) -> None:
        self._session.add(_check_to_row(monitor_id, check))
        await self._session.commit()

    async def get_status(self, monitor_id: str) -> LiveMonitorStatus:
        row = await self._session.scalar(
            select(LiveMonitorRow)
            .where(
                LiveMonitorRow.id == monitor_id,
                LiveMonitorRow.tenant_id == self._tenant_id,
            )
            .options(selectinload(LiveMonitorRow.checks))
        )
        if row is None:
            raise LiveMonitorNotFoundError(monitor_id)
        return _status_from_row(row)

    async def list_checks(
        self,
        monitor_id: str,
        *,
        limit: int = 50,
    ) -> tuple[LiveMonitorCheck, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("check list limit must be between 1 and 200")
        rows = (
            await self._session.scalars(
                select(LiveMonitorCheckRow)
                .join(LiveMonitorRow)
                .where(
                    LiveMonitorCheckRow.monitor_id == monitor_id,
                    LiveMonitorRow.tenant_id == self._tenant_id,
                )
                .order_by(LiveMonitorCheckRow.sequence.desc())
                .limit(limit)
            )
        ).all()
        return tuple(_check_from_row(item) for item in rows)


class DbLiveMonitorStore:
    """Session-per-operation adapter implementing the registry persistence
    protocol, using the shared :class:`Database`."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save_status(self, tenant_id: str, status: LiveMonitorStatus) -> None:
        async for session in self._database.session():
            await LiveMonitorRepository(session, tenant_id=tenant_id).save_status(status)

    async def append_check(
        self,
        tenant_id: str,
        monitor_id: str,
        check: LiveMonitorCheck,
    ) -> None:
        async for session in self._database.session():
            await LiveMonitorRepository(session, tenant_id=tenant_id).append_check(
                monitor_id, check
            )

    async def get_status(self, tenant_id: str, monitor_id: str) -> LiveMonitorStatus:
        async for session in self._database.session():
            result = await LiveMonitorRepository(session, tenant_id=tenant_id).get_status(
                monitor_id
            )
            return result
        raise LiveMonitorNotFoundError(monitor_id)

    async def list_active(self) -> tuple[tuple[str, LiveMonitorStatus], ...]:
        """Return (tenant_id, status) for every persisted ACTIVE monitor."""
        async for session in self._database.session():
            rows = (
                await session.scalars(
                    select(LiveMonitorRow)
                    .where(LiveMonitorRow.state == LiveMonitorState.ACTIVE.value)
                    .options(selectinload(LiveMonitorRow.checks))
                )
            ).all()
            return tuple((row.tenant_id, _status_from_row(row)) for row in rows)
        return ()


__all__ = [
    "DbLiveMonitorStore",
    "LiveMonitorNotFoundError",
    "LiveMonitorRepository",
]
