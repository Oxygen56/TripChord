"""SQLAlchemy control plane for resumable live-planning jobs.

This repository is intentionally the single-row authority for a job snapshot:
idempotency, lease claims, checkpoints, cancellation, and terminal settlement
all use the same row lock and version.  Browser/model execution remains outside
the repository and can only publish through these guarded transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord.agents.live_jobs import (
    TERMINAL_LIVE_PLANNING_JOB_STATES,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
)
from tripchord.persistence.models import LivePlanningJobRow, LivePlanningPairResultRow, utc_now


class DurableLivePlanningJobConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class DurableLease:
    snapshot: LivePlanningJobSnapshot
    owner: str
    generation: int


@dataclass(frozen=True)
class DurableRecoveryRecord:
    snapshot: LivePlanningJobSnapshot
    command_spec: dict[str, Any]


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class DurableLivePlanningJobRepository:
    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def create_or_get(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        snapshot: LivePlanningJobSnapshot,
        command_spec: dict[str, Any] | None = None,
    ) -> LivePlanningJobSnapshot:
        if snapshot.request_sha256 != request_sha256:
            raise DurableLivePlanningJobConflict("snapshot request digest does not match")
        existing = await self._session.scalar(
            select(LivePlanningJobRow).where(
                LivePlanningJobRow.tenant_id == self._tenant_id,
                LivePlanningJobRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_sha256 != request_sha256:
                raise DurableLivePlanningJobConflict("idempotency key conflicts with request")
            return LivePlanningJobSnapshot.model_validate(existing.snapshot)
        row = LivePlanningJobRow(
            id=snapshot.id,
            tenant_id=self._tenant_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            state=snapshot.state.value,
            version=snapshot.revision,
            snapshot=snapshot.model_dump(mode="json"),
            command_spec=command_spec,
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return await self.create_or_get(
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                snapshot=snapshot,
            )
        return snapshot

    async def get(self, job_id: str) -> LivePlanningJobSnapshot | None:
        row = await self._session.scalar(
            select(LivePlanningJobRow).where(
                LivePlanningJobRow.id == job_id,
                LivePlanningJobRow.tenant_id == self._tenant_id,
            )
        )
        return None if row is None else LivePlanningJobSnapshot.model_validate(row.snapshot)

    async def get_by_idempotency(self, idempotency_key: str) -> LivePlanningJobSnapshot | None:
        row = await self._session.scalar(
            select(LivePlanningJobRow).where(
                LivePlanningJobRow.tenant_id == self._tenant_id,
                LivePlanningJobRow.idempotency_key == idempotency_key,
            )
        )
        return None if row is None else LivePlanningJobSnapshot.model_validate(row.snapshot)

    async def list_recoverable(self) -> tuple[str, ...]:
        now = utc_now()
        queued = (
            await self._session.scalars(
                select(LivePlanningJobRow).where(
                    LivePlanningJobRow.tenant_id == self._tenant_id,
                    LivePlanningJobRow.state == LivePlanningJobState.QUEUED.value,
                )
            )
        ).all()
        expired = (
            await self._session.scalars(
                select(LivePlanningJobRow).where(
                    LivePlanningJobRow.tenant_id == self._tenant_id,
                    LivePlanningJobRow.state == LivePlanningJobState.RUNNING.value,
                    (LivePlanningJobRow.lease_expires_at.is_(None))
                    | (LivePlanningJobRow.lease_expires_at < now),
                )
            )
        ).all()
        return tuple(row.id for row in (*queued, *expired))

    async def list_tenants(self) -> tuple[str, ...]:
        rows = await self._session.scalars(select(LivePlanningJobRow.tenant_id).distinct())
        return tuple(rows.all())

    async def recovery_record(self, job_id: str) -> DurableRecoveryRecord | None:
        row = await self._session.scalar(
            select(LivePlanningJobRow).where(
                LivePlanningJobRow.id == job_id,
                LivePlanningJobRow.tenant_id == self._tenant_id,
            )
        )
        if row is None or not isinstance(row.command_spec, dict):
            return None
        return DurableRecoveryRecord(
            LivePlanningJobSnapshot.model_validate(row.snapshot), row.command_spec
        )

    async def store_pair_result(
        self,
        job_id: str,
        *,
        checkpoint: LivePlanningPairCheckpoint,
        execution: dict[str, Any],
        execution_sha256: str,
        owner: str,
        lease_generation: int,
    ) -> bool:
        job = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(job.snapshot)
        if (
            snapshot.state != LivePlanningJobState.RUNNING
            or snapshot.cancel_pending
            or snapshot.cancellation_requested
            or job.lease_owner != owner
            or job.lease_generation != lease_generation
        ):
            await self._session.rollback()
            return False
        existing = await self._session.scalar(
            select(LivePlanningPairResultRow).where(
                LivePlanningPairResultRow.tenant_id == self._tenant_id,
                LivePlanningPairResultRow.job_id == job_id,
                LivePlanningPairResultRow.date_pair_id == checkpoint.date_pair_id,
            )
        )
        if existing is not None:
            if existing.execution_sha256 != execution_sha256:
                await self._session.rollback()
                raise DurableLivePlanningJobConflict("date pair result digest conflicts")
            await self._session.rollback()
            return True
        self._session.add(
            LivePlanningPairResultRow(
                tenant_id=self._tenant_id,
                job_id=job_id,
                date_pair_id=checkpoint.date_pair_id,
                request_sha256=checkpoint.request_sha256,
                sequence=checkpoint.sequence,
                checkpoint=checkpoint.model_dump(mode="json"),
                execution=execution,
                execution_sha256=execution_sha256,
                lease_owner=owner,
                lease_generation=lease_generation,
            )
        )
        await self._session.commit()
        return True

    async def load_pair_results(self, job_id: str) -> tuple[dict[str, Any], ...]:
        rows = (
            await self._session.scalars(
                select(LivePlanningPairResultRow)
                .where(
                    LivePlanningPairResultRow.tenant_id == self._tenant_id,
                    LivePlanningPairResultRow.job_id == job_id,
                )
                .order_by(LivePlanningPairResultRow.sequence)
            )
        ).all()
        return tuple(row.execution for row in rows)

    async def replace_snapshot(
        self,
        job_id: str,
        snapshot: LivePlanningJobSnapshot,
        *,
        expected_revision: int | None = None,
        owner: str | None = None,
        lease_generation: int | None = None,
    ) -> LivePlanningJobSnapshot:
        row = await self._locked(job_id)
        current = LivePlanningJobSnapshot.model_validate(row.snapshot)
        if current.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            await self._session.rollback()
            return current
        if owner is not None and (
            row.lease_owner != owner or row.lease_generation != lease_generation
        ):
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("stale live job lease")
        if current.cancel_pending or current.cancellation_requested:
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("live job cancellation is in progress")
        if expected_revision is not None and current.revision != expected_revision:
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("stale live job revision")
        if snapshot.revision <= current.revision:
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("live job revision must increase")
        row.state = snapshot.state.value
        row.version = snapshot.revision
        row.snapshot = snapshot.model_dump(mode="json")
        await self._session.commit()
        return snapshot

    async def claim(
        self,
        job_id: str,
        *,
        lease_seconds: int = 300,
    ) -> LivePlanningJobSnapshot | None:
        row = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
        now = utc_now()
        if snapshot.state == LivePlanningJobState.QUEUED or (
            snapshot.state == LivePlanningJobState.RUNNING
            and (row.lease_expires_at is None or _aware(row.lease_expires_at) < now)
        ):
            updated = snapshot.model_copy(
                update={
                    "state": LivePlanningJobState.RUNNING,
                    "stage": "claimed",
                    "revision": snapshot.revision + 1,
                    "updated_at": now,
                }
            )
            row.state = updated.state.value
            row.version = updated.revision
            row.snapshot = updated.model_dump(mode="json")
            row.lease_generation += 1
            row.lease_owner = f"worker:{job_id}:{row.lease_generation}"
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            await self._session.commit()
            return updated
        await self._session.rollback()
        return None

    async def renew_lease(
        self,
        job_id: str,
        *,
        owner: str,
        lease_generation: int,
        lease_seconds: int = 300,
    ) -> bool:
        row = await self._locked(job_id)
        if (
            row.lease_owner != owner
            or row.lease_generation != lease_generation
            or LivePlanningJobState(row.state) != LivePlanningJobState.RUNNING
        ):
            await self._session.rollback()
            return False
        row.lease_expires_at = utc_now() + timedelta(seconds=lease_seconds)
        await self._session.commit()
        return True

    async def release_lease(
        self, job_id: str, *, owner: str, lease_generation: int
    ) -> bool:
        row = await self._locked(job_id)
        if row.lease_owner != owner or row.lease_generation != lease_generation:
            await self._session.rollback()
            return False
        row.lease_expires_at = utc_now()
        row.lease_owner = None
        await self._session.commit()
        return True

    async def claim_with_identity(
        self, job_id: str, *, lease_seconds: int = 300
    ) -> DurableLease | None:
        claimed = await self.claim(job_id, lease_seconds=lease_seconds)
        if claimed is None:
            return None
        row = await self._session.scalar(
            select(LivePlanningJobRow).where(LivePlanningJobRow.id == job_id)
        )
        if row is None or row.lease_owner is None:
            raise DurableLivePlanningJobConflict("claimed job has no lease identity")
        owner = row.lease_owner
        generation = row.lease_generation
        await self._session.rollback()
        return DurableLease(claimed, owner, generation)

    async def append_checkpoint(
        self,
        job_id: str,
        checkpoint: LivePlanningPairCheckpoint,
        *,
        owner: str | None = None,
        lease_generation: int | None = None,
    ) -> LivePlanningJobSnapshot:
        row = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
        if snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            raise DurableLivePlanningJobConflict("terminal job cannot accept a checkpoint")
        if owner is not None and (
            snapshot.state != LivePlanningJobState.RUNNING
            or snapshot.cancel_pending
            or snapshot.cancellation_requested
            or row.lease_owner != owner
            or row.lease_generation != lease_generation
        ):
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("stale or cancelled live job lease")
        duplicate = next(
            (
                item
                for item in snapshot.pair_checkpoints
                if item.date_pair_id == checkpoint.date_pair_id
            ),
            None,
        )
        if duplicate is not None:
            if duplicate == checkpoint:
                await self._session.rollback()
                return snapshot
            raise DurableLivePlanningJobConflict("checkpoint conflicts for date pair")
        if checkpoint.sequence != len(snapshot.pair_checkpoints) + 1:
            raise DurableLivePlanningJobConflict("checkpoint sequence is not contiguous")
        updated = snapshot.model_copy(
            update={
                "pair_checkpoints": (*snapshot.pair_checkpoints, checkpoint),
                "revision": snapshot.revision + 1,
                "updated_at": utc_now(),
            }
        )
        row.version = updated.revision
        row.snapshot = updated.model_dump(mode="json")
        await self._session.commit()
        return updated

    async def cancel(self, job_id: str) -> LivePlanningJobSnapshot:
        row = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
        if snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            await self._session.rollback()
            return snapshot
        if (
            snapshot.state == LivePlanningJobState.QUEUED
            and row.lease_owner is None
            and row.lease_generation == 0
            and row.cancel_target_owner is None
            and row.cancel_target_generation is None
        ):
            updated = snapshot.model_copy(
                update={
                    "state": LivePlanningJobState.CANCELLED,
                    "stage": "cancelled",
                    "progress": 100,
                    "revision": snapshot.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            row.state = updated.state.value
            row.version = updated.revision
            row.snapshot = updated.model_dump(mode="json")
            await self._session.commit()
            return updated
        updated = snapshot.model_copy(
            update={
                "state": LivePlanningJobState.CANCELLED,
                "stage": "cancelled",
                "progress": 100,
                "revision": snapshot.revision + 1,
                "updated_at": utc_now(),
            }
        )
        row.state = updated.state.value
        row.version = updated.revision
        row.snapshot = updated.model_dump(mode="json")
        await self._session.commit()
        return updated

    async def request_cancel(self, job_id: str) -> LivePlanningJobSnapshot:
        """Persist a cross-process cancellation intent without guessing a drain."""
        row = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
        if snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            await self._session.rollback()
            return snapshot
        if (
            snapshot.state == LivePlanningJobState.QUEUED
            and row.lease_owner is None
            and row.lease_generation == 0
            and row.cancel_target_owner is None
            and row.cancel_target_generation is None
        ):
            updated = snapshot.model_copy(
                update={
                    "state": LivePlanningJobState.CANCELLED,
                    "stage": "cancelled",
                    "progress": 100,
                    "revision": snapshot.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            row.state = updated.state.value
            row.version = updated.revision
            row.snapshot = updated.model_dump(mode="json")
            await self._session.commit()
            return updated
        updated = snapshot.model_copy(
            update={
                "cancel_pending": True,
                "cancellation_requested": True,
                "stage": "cancelling",
                "revision": snapshot.revision + 1,
                "updated_at": utc_now(),
            }
        )
        revoked_owner = row.lease_owner
        revoked_generation = row.lease_generation
        row.version = updated.revision
        row.snapshot = updated.model_dump(mode="json")
        row.lease_generation += 1
        row.lease_owner = None
        row.lease_expires_at = utc_now()
        row.cancel_target_owner = revoked_owner
        row.cancel_target_generation = revoked_generation
        await self._session.commit()
        return updated

    async def settle(
        self,
        job_id: str,
        *,
        state: LivePlanningJobState,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        owner: str | None = None,
        lease_generation: int | None = None,
    ) -> LivePlanningJobSnapshot:
        if state not in TERMINAL_LIVE_PLANNING_JOB_STATES:
            raise ValueError("settle requires a terminal state")
        row = await self._locked(job_id)
        snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
        if snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            await self._session.rollback()
            return snapshot
        if owner is not None and (
            snapshot.state != LivePlanningJobState.RUNNING
            or snapshot.cancel_pending
            or snapshot.cancellation_requested
            or row.lease_owner != owner
            or row.lease_generation != lease_generation
        ):
            await self._session.rollback()
            raise DurableLivePlanningJobConflict("stale or cancelled live job lease")
        updated = snapshot.model_copy(
            update={
                "state": state,
                "stage": "complete" if state == LivePlanningJobState.SUCCEEDED else "failed",
                "progress": 100,
                "result": result,
                "error": error,
                "revision": snapshot.revision + 1,
                "updated_at": utc_now(),
            }
        )
        row.state = updated.state.value
        row.version = updated.revision
        row.snapshot = updated.model_dump(mode="json")
        await self._session.commit()
        return updated

    async def _locked(self, job_id: str) -> LivePlanningJobRow:
        row = await self._session.scalar(
            select(LivePlanningJobRow)
            .where(
                LivePlanningJobRow.id == job_id,
                LivePlanningJobRow.tenant_id == self._tenant_id,
            )
            .with_for_update()
        )
        if row is None:
            raise LookupError(job_id)
        return row


class DurableLivePlanningJobStore:
    """Session-scoped facade used by the API registry.

    A new repository/session is opened for every transition.  The registry may
    retain coroutine handles, but never a second in-memory snapshot authority.
    """

    def __init__(self, database: Any) -> None:
        self._database = database

    def _repo(self, session: AsyncSession, tenant_id: str) -> DurableLivePlanningJobRepository:
        return DurableLivePlanningJobRepository(session, tenant_id)

    async def create_or_get(self, *, tenant_id: str, **kwargs: Any) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).create_or_get(**kwargs)

    async def get(self, job_id: str, *, tenant_id: str) -> LivePlanningJobSnapshot | None:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).get(job_id)

    async def get_by_idempotency(
        self, key: str, *, tenant_id: str
    ) -> LivePlanningJobSnapshot | None:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).get_by_idempotency(key)

    async def list_recoverable(self, *, tenant_id: str) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).list_recoverable()

    async def list_tenants(self) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await self._repo(session, "anonymous").list_tenants()

    async def authorize_orphan_reap(
        self, job_id: str, *, tenant_id: str, owner: str | None, generation: int | None
    ) -> bool:
        """Authorize killing a marker only when its exact lease is not active."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(LivePlanningJobRow)
                .with_for_update()
                .where(
                    LivePlanningJobRow.id == job_id,
                    LivePlanningJobRow.tenant_id == tenant_id,
                )
            )
            if row is None:
                return False
            if owner is None or generation is None:
                await session.rollback()
                return False
            snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
            exact_generation = row.lease_generation == generation
            cancelled_generation = (
                snapshot.cancel_pending and row.lease_generation == generation + 1
            )
            if (not exact_generation and not cancelled_generation) or row.lease_owner not in {
                owner,
                None,
            }:
                await session.rollback()
                return False
            if (
                row.lease_owner == owner
                and row.lease_expires_at is not None
                and _aware(row.lease_expires_at) > utc_now()
                and not snapshot.cancel_pending
            ):
                await session.rollback()
                return False
            if not cancelled_generation:
                row.lease_generation += 1
            row.lease_owner = None
            row.lease_expires_at = utc_now()
            await session.commit()
            return True

    async def recovery_record(
        self, job_id: str, *, tenant_id: str
    ) -> DurableRecoveryRecord | None:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).recovery_record(job_id)

    async def store_pair_result(self, job_id: str, *, tenant_id: str, **kwargs: Any) -> bool:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).store_pair_result(job_id, **kwargs)

    async def load_pair_results(
        self, job_id: str, *, tenant_id: str
    ) -> tuple[dict[str, Any], ...]:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).load_pair_results(job_id)

    async def replace_snapshot(
        self, job_id: str, snapshot: LivePlanningJobSnapshot, *, tenant_id: str, **kwargs: Any
    ) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).replace_snapshot(job_id, snapshot, **kwargs)

    async def claim(
        self, job_id: str, *, tenant_id: str, lease_seconds: int = 300
    ) -> LivePlanningJobSnapshot | None:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).claim(job_id, lease_seconds=lease_seconds)

    async def claim_with_identity(
        self, job_id: str, *, tenant_id: str, lease_seconds: int = 300
    ) -> DurableLease | None:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).claim_with_identity(
                job_id, lease_seconds=lease_seconds
            )

    async def renew_lease(self, job_id: str, *, tenant_id: str, **kwargs: Any) -> bool:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).renew_lease(job_id, **kwargs)

    async def release_lease(self, job_id: str, *, tenant_id: str, **kwargs: Any) -> bool:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).release_lease(job_id, **kwargs)

    async def append_checkpoint(
        self,
        job_id: str,
        checkpoint: LivePlanningPairCheckpoint,
        *,
        tenant_id: str,
        owner: str,
        lease_generation: int,
    ) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).append_checkpoint(
                job_id,
                checkpoint,
                owner=owner,
                lease_generation=lease_generation,
            )

    async def cancel(self, job_id: str, *, tenant_id: str) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).cancel(job_id)

    async def consume_orphan_death_proof(
        self,
        job_id: str,
        *,
        tenant_id: str,
        proof_owner: str,
        proof_generation: int,
    ) -> LivePlanningJobSnapshot | None:
        async with self._database.sessions() as session:
            repo = self._repo(session, tenant_id)
            row = await repo._locked(job_id)
            snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
            if (
                not snapshot.cancel_pending
                or row.lease_owner is not None
                or row.lease_generation != proof_generation + 1
                or row.cancel_target_owner != proof_owner
                or row.cancel_target_generation != proof_generation
            ):
                await session.rollback()
                return None
            return await repo.cancel(job_id)

    async def request_cancel(
        self, job_id: str, *, tenant_id: str
    ) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).request_cancel(job_id)

    async def settle(
        self, job_id: str, *, tenant_id: str, **kwargs: Any
    ) -> LivePlanningJobSnapshot:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).settle(job_id, **kwargs)
