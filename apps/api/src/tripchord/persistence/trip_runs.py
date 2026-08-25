"""Tenant-scoped persistence for the authoritative complex-trip state.

``TripRun`` owns the complete mutable travel-planning truth while its contained
plan versions remain append-only domain history.  This module deliberately
stores the whole typed aggregate in one snapshot row: callers cannot observe a
partially written catalog, plan version, or change receipt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord.persistence.database import Database
from tripchord.persistence.models import TripRunRow, utc_now
from tripchord.planning.trip_run import TripRun


class TripRunNotFoundError(LookupError):
    """Raised when a run is not visible to the requesting tenant."""


class TripRunConflictError(RuntimeError):
    """Raised when a create or optimistic save conflicts with stored state."""


@dataclass(frozen=True, slots=True)
class StoredTripRun:
    """A complete typed aggregate paired with its persistence revision."""

    trip_run: TripRun
    revision: int


class TripRunStore(Protocol):
    """Shared contract for process-local and durable TripRun storage."""

    async def create(self, tenant_id: str, trip_run: TripRun) -> StoredTripRun:
        """Create a run, accepting an exact retry of the same initial snapshot."""

    async def get(self, tenant_id: str, run_id: str) -> StoredTripRun | None:
        """Return a tenant-owned run without revealing another tenant's state."""

    async def save(
        self,
        tenant_id: str,
        trip_run: TripRun,
        *,
        expected_revision: int | None = None,
        expected_active_plan_version_id: str | None = None,
    ) -> StoredTripRun:
        """Replace the aggregate after an optimistic-concurrency check."""


def _validate_identity(tenant_id: str, run_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id cannot be empty")
    if not run_id:
        raise ValueError("TripRun id cannot be empty")


def _require_save_guard(
    *,
    expected_revision: int | None,
    expected_active_plan_version_id: str | None,
) -> None:
    if expected_revision is None and expected_active_plan_version_id is None:
        raise ValueError(
            "save requires expected_revision or expected_active_plan_version_id"
        )
    if expected_revision is not None and expected_revision < 1:
        raise ValueError("expected_revision must be at least 1")
    if expected_active_plan_version_id == "":
        raise ValueError("expected_active_plan_version_id cannot be empty")


def _check_expected_state(
    stored: StoredTripRun,
    *,
    expected_revision: int | None,
    expected_active_plan_version_id: str | None,
) -> None:
    if expected_revision is not None and stored.revision != expected_revision:
        raise TripRunConflictError("stale TripRun revision")
    if (
        expected_active_plan_version_id is not None
        and stored.trip_run.active_plan_version_id
        != expected_active_plan_version_id
    ):
        raise TripRunConflictError("stale TripRun active plan version")


def _serialize(trip_run: TripRun) -> dict[str, object]:
    return trip_run.model_dump(mode="json")


def _stored_from_row(row: TripRunRow) -> StoredTripRun:
    if row.revision < 1:
        raise TripRunConflictError("stored TripRun revision is invalid")
    trip_run = TripRun.model_validate(row.snapshot)
    if trip_run.id != row.id:
        raise TripRunConflictError("stored TripRun id does not match its row")
    return StoredTripRun(trip_run=trip_run, revision=row.revision)


@dataclass(frozen=True, slots=True)
class _InMemoryEntry:
    tenant_id: str
    stored: StoredTripRun


class InMemoryTripRunStore:
    """Lock-serialized implementation with the same semantics as the DB store."""

    def __init__(self) -> None:
        # TripRun ids are job-derived global ids, matching TripRunRow's primary
        # key.  The tenant remains part of every read/write authorization check.
        self._entries: dict[str, _InMemoryEntry] = {}
        self._lock = asyncio.Lock()

    async def create(self, tenant_id: str, trip_run: TripRun) -> StoredTripRun:
        _validate_identity(tenant_id, trip_run.id)
        async with self._lock:
            existing = self._entries.get(trip_run.id)
            if existing is not None:
                if existing.tenant_id == tenant_id and existing.stored.trip_run == trip_run:
                    return existing.stored
                raise TripRunConflictError("TripRun id already exists")
            stored = StoredTripRun(trip_run=trip_run, revision=1)
            self._entries[trip_run.id] = _InMemoryEntry(
                tenant_id=tenant_id,
                stored=stored,
            )
            return stored

    async def get(self, tenant_id: str, run_id: str) -> StoredTripRun | None:
        _validate_identity(tenant_id, run_id)
        async with self._lock:
            existing = self._entries.get(run_id)
            if existing is None or existing.tenant_id != tenant_id:
                return None
            return existing.stored

    async def save(
        self,
        tenant_id: str,
        trip_run: TripRun,
        *,
        expected_revision: int | None = None,
        expected_active_plan_version_id: str | None = None,
    ) -> StoredTripRun:
        _validate_identity(tenant_id, trip_run.id)
        _require_save_guard(
            expected_revision=expected_revision,
            expected_active_plan_version_id=expected_active_plan_version_id,
        )
        async with self._lock:
            existing = self._entries.get(trip_run.id)
            if existing is None or existing.tenant_id != tenant_id:
                raise TripRunNotFoundError(trip_run.id)
            _check_expected_state(
                existing.stored,
                expected_revision=expected_revision,
                expected_active_plan_version_id=expected_active_plan_version_id,
            )
            stored = StoredTripRun(
                trip_run=trip_run,
                revision=existing.stored.revision + 1,
            )
            self._entries[trip_run.id] = _InMemoryEntry(
                tenant_id=tenant_id,
                stored=stored,
            )
            return stored


class TripRunRepository:
    """Session-bound durable repository for one tenant."""

    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        _validate_identity(tenant_id, "tenant-scope")
        self._session = session
        self._tenant_id = tenant_id

    async def create(self, trip_run: TripRun) -> StoredTripRun:
        _validate_identity(self._tenant_id, trip_run.id)
        now = utc_now()
        row = TripRunRow(
            id=trip_run.id,
            tenant_id=self._tenant_id,
            revision=1,
            snapshot=_serialize(trip_run),
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self.get(trip_run.id)
            if existing is not None and existing.trip_run == trip_run:
                return existing
            raise TripRunConflictError("TripRun id already exists") from exc
        return StoredTripRun(trip_run=trip_run, revision=1)

    async def get(self, run_id: str) -> StoredTripRun | None:
        _validate_identity(self._tenant_id, run_id)
        row = await self._session.scalar(
            select(TripRunRow).where(
                TripRunRow.id == run_id,
                TripRunRow.tenant_id == self._tenant_id,
            )
        )
        return None if row is None else _stored_from_row(row)

    async def save(
        self,
        trip_run: TripRun,
        *,
        expected_revision: int | None = None,
        expected_active_plan_version_id: str | None = None,
    ) -> StoredTripRun:
        _validate_identity(self._tenant_id, trip_run.id)
        _require_save_guard(
            expected_revision=expected_revision,
            expected_active_plan_version_id=expected_active_plan_version_id,
        )
        current = await self.get(trip_run.id)
        if current is None:
            raise TripRunNotFoundError(trip_run.id)
        _check_expected_state(
            current,
            expected_revision=expected_revision,
            expected_active_plan_version_id=expected_active_plan_version_id,
        )

        next_revision = current.revision + 1
        statement = (
            update(TripRunRow)
            .where(
                TripRunRow.id == trip_run.id,
                TripRunRow.tenant_id == self._tenant_id,
                TripRunRow.revision == current.revision,
            )
            .values(
                revision=next_revision,
                snapshot=_serialize(trip_run),
                updated_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
            .returning(TripRunRow.revision)
        )
        updated_revision = await self._session.scalar(statement)
        if updated_revision != next_revision:
            await self._session.rollback()
            raise TripRunConflictError("TripRun changed while it was being saved")
        await self._session.commit()
        return StoredTripRun(trip_run=trip_run, revision=next_revision)


class DbTripRunStore:
    """Session-per-operation adapter over the application's shared Database."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, tenant_id: str, trip_run: TripRun) -> StoredTripRun:
        async for session in self._database.session():
            return await TripRunRepository(session, tenant_id=tenant_id).create(trip_run)
        raise RuntimeError("database session was not available")

    async def get(self, tenant_id: str, run_id: str) -> StoredTripRun | None:
        async for session in self._database.session():
            return await TripRunRepository(session, tenant_id=tenant_id).get(run_id)
        raise RuntimeError("database session was not available")

    async def save(
        self,
        tenant_id: str,
        trip_run: TripRun,
        *,
        expected_revision: int | None = None,
        expected_active_plan_version_id: str | None = None,
    ) -> StoredTripRun:
        async for session in self._database.session():
            return await TripRunRepository(session, tenant_id=tenant_id).save(
                trip_run,
                expected_revision=expected_revision,
                expected_active_plan_version_id=expected_active_plan_version_id,
            )
        raise RuntimeError("database session was not available")


__all__ = [
    "DbTripRunStore",
    "InMemoryTripRunStore",
    "StoredTripRun",
    "TripRunConflictError",
    "TripRunNotFoundError",
    "TripRunRepository",
    "TripRunStore",
]
