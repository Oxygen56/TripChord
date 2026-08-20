"""SQLAlchemy control plane for resumable live-planning jobs.

This repository is intentionally the single-row authority for a job snapshot:
idempotency, lease claims, checkpoints, cancellation, and terminal settlement
all use the same row lock and version.  Browser/model execution remains outside
the repository and can only publish through these guarded transitions.
"""

from __future__ import annotations

import secrets
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
                    LivePlanningJobRow.reap_owner.is_(None),
                )
            )
        ).all()
        expired = (
            await self._session.scalars(
                select(LivePlanningJobRow).where(
                    LivePlanningJobRow.tenant_id == self._tenant_id,
                    LivePlanningJobRow.state == LivePlanningJobState.RUNNING.value,
                    LivePlanningJobRow.reap_owner.is_(None),
                    (LivePlanningJobRow.lease_expires_at.is_(None))
                    | (LivePlanningJobRow.lease_expires_at < now),
                )
            )
        ).all()
        return tuple(row.id for row in (*queued, *expired))

    async def list_expired_reaping(self) -> tuple[str, ...]:
        now = utc_now()
        rows = await self._session.scalars(
            select(LivePlanningJobRow).where(
                LivePlanningJobRow.tenant_id == self._tenant_id,
                LivePlanningJobRow.reap_owner.is_not(None),
                LivePlanningJobRow.reap_expires_at.is_not(None),
                LivePlanningJobRow.reap_expires_at < now,
            )
        )
        return tuple(row.id for row in rows.all())

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
        if row.reap_owner is not None:
            await self._session.rollback()
            return None
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

    def __init__(self, database: Any, *, reap_lease_seconds: int = 300) -> None:
        self._database = database
        if reap_lease_seconds < 1:
            raise ValueError("reap_lease_seconds must be at least one second")
        self._reap_lease_seconds = reap_lease_seconds

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

    async def list_expired_reaping(self, *, tenant_id: str) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await self._repo(session, tenant_id).list_expired_reaping()

    async def list_tenants(self) -> tuple[str, ...]:
        async with self._database.sessions() as session:
            return await self._repo(session, "anonymous").list_tenants()

    async def authorize_orphan_reap(
        self,
        job_id: str,
        *,
        tenant_id: str,
        owner: str | None,
        generation: int | None,
        reaper_id: str,
    ) -> str | None:
        """Acquire an atomic, non-claimable fence for orphan cleanup.

        The lease is fenced immediately, but the row remains blocked from
        claiming until the caller proves authentication and process death via
        :meth:`complete_orphan_reap`.  This closes the window between the
        database check and the actual process kill.
        """
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
                return None
            if owner is None or generation is None:
                await session.rollback()
                return None
            now = utc_now()
            expired_reap = False
            if row.reap_owner is not None:
                if row.reap_expires_at is not None and _aware(row.reap_expires_at) <= now:
                    # A crashed cleanup owner may be replaced, but only under
                    # the same row lock used by claim/lease fencing.
                    expired_reap = True
                    if (
                        row.reap_target_owner != owner
                        or row.reap_target_generation != generation
                        or row.lease_generation != generation + 1
                    ):
                        await session.rollback()
                        return None
                else:
                    # A previous reaper may have died after confirming the
                    # process group but before clearing this row.  Returning
                    # the still-valid token lets the next startup complete
                    # the same fenced transition; it does not grant claim.
                    if (
                        row.reap_target_owner != owner
                        or row.reap_target_generation != generation
                        or row.reap_controller != reaper_id
                    ):
                        await session.rollback()
                        return None
                    token = row.reap_owner if isinstance(row.reap_owner, str) else None
                    await session.rollback()
                    return token
            snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
            exact_generation = row.lease_generation == generation
            cancelled_generation = (
                snapshot.cancel_pending and row.lease_generation == generation + 1
            )
            if (
                not expired_reap
                and ((not exact_generation and not cancelled_generation) or row.lease_owner not in {
                owner,
                None,
                })
            ):
                await session.rollback()
                return None
            if (
                row.lease_owner == owner
                and row.lease_expires_at is not None
                and _aware(row.lease_expires_at) > utc_now()
                and not snapshot.cancel_pending
            ):
                await session.rollback()
                return None
            if not expired_reap and not cancelled_generation:
                row.lease_generation += 1
            row.lease_owner = None
            row.lease_expires_at = now
            reap_token = f"reaper:{secrets.token_urlsafe(24)}"
            row.reap_owner = reap_token
            row.reap_generation = generation
            row.reap_expires_at = now + timedelta(seconds=self._reap_lease_seconds)
            row.reap_target_owner = owner
            row.reap_target_generation = generation
            row.reap_controller = reaper_id
            updated = snapshot.model_copy(
                update={
                    "stage": "reaping",
                    "error": "worker cleanup is pending confirmed process death",
                    "revision": snapshot.revision + 1,
                    "updated_at": now,
                }
            )
            row.state = updated.state.value
            row.version = updated.revision
            row.snapshot = updated.model_dump(mode="json")
            await session.commit()
            return reap_token

    async def quarantine_orphan_identity(self, job_id: str, *, reaper_id: str) -> bool:
        """Fence a non-terminal job whose marker identity cannot be trusted."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(LivePlanningJobRow).with_for_update().where(
                    LivePlanningJobRow.id == job_id
                )
            )
            if row is None or row.reap_owner is not None:
                await session.rollback()
                return False
            snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
            now = utc_now()
            if snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES or (
                row.lease_expires_at is not None and _aware(row.lease_expires_at) > now
            ):
                await session.rollback()
                return False
            target_owner = row.lease_owner
            target_generation = row.lease_generation
            if target_owner is None or target_generation < 1:
                await session.rollback()
                return False
            row.lease_generation += 1
            row.lease_owner = None
            row.lease_expires_at = now
            row.reap_owner = f"reaper:{secrets.token_urlsafe(24)}"
            row.reap_generation = target_generation
            row.reap_expires_at = now + timedelta(minutes=5)
            row.reap_target_owner = target_owner
            row.reap_target_generation = target_generation
            row.reap_controller = reaper_id
            updated = snapshot.model_copy(
                update={
                    "stage": "reaping",
                    "error": "worker marker identity mismatch; manual cleanup required",
                    "revision": snapshot.revision + 1,
                    "updated_at": now,
                }
            )
            row.state = updated.state.value
            row.version = updated.revision
            row.snapshot = updated.model_dump(mode="json")
            await session.commit()
            return True

    async def complete_orphan_reap(
        self,
        job_id: str,
        *,
        tenant_id: str,
        owner: str,
        generation: int,
        reap_token: str,
    ) -> bool:
        """Release a reaping fence only after confirmed orphan death."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(LivePlanningJobRow)
                .with_for_update()
                .where(
                    LivePlanningJobRow.id == job_id,
                    LivePlanningJobRow.tenant_id == tenant_id,
                )
            )
            if (
                row is None
                or row.reap_owner != reap_token
                or row.reap_generation != generation
                or row.reap_target_owner != owner
                or row.reap_target_generation != generation
                or row.reap_marker_digest is None
                or row.reap_proof_kind not in {"worker_death", "spawn_absent"}
                or row.reap_proof_verified_at is None
                or (
                    row.reap_proof_kind == "worker_death"
                    and (
                        row.reap_pgid is None
                        or row.reap_authenticated_at is None
                        or row.reap_death_confirmed_at is None
                    )
                )
                or (
                    row.reap_proof_kind == "spawn_absent"
                    and (
                        row.reap_pgid is not None
                        or row.reap_authenticated_at is not None
                        or row.reap_death_confirmed_at is not None
                    )
                )
            ):
                await session.rollback()
                return False
            row.reap_owner = None
            row.reap_generation = None
            row.reap_expires_at = None
            row.reap_target_owner = None
            row.reap_target_generation = None
            row.reap_controller = None
            row.reap_pgid = None
            row.reap_marker_digest = None
            row.reap_proof_kind = None
            row.reap_proof_verified_at = None
            row.reap_authenticated_at = None
            row.reap_death_confirmed_at = None
            await session.commit()
            return True

    async def record_orphan_reap_proof(
        self,
        job_id: str,
        *,
        tenant_id: str,
        owner: str,
        generation: int,
        reap_token: str,
        pgid: int | None,
        marker_digest: str,
        proof_kind: str = "worker_death",
    ) -> bool:
        """Atomically persist authenticated and confirmed death evidence."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(LivePlanningJobRow).with_for_update().where(
                    LivePlanningJobRow.id == job_id,
                    LivePlanningJobRow.tenant_id == tenant_id,
                )
            )
            if (
                row is None
                or row.reap_owner != reap_token
                or row.reap_target_owner != owner
                or row.reap_target_generation != generation
                or proof_kind not in {"worker_death", "spawn_absent"}
                or (proof_kind == "worker_death" and (type(pgid) is not int or pgid <= 0))
                or (proof_kind == "spawn_absent" and pgid is not None)
                or len(marker_digest) != 64
                or any(char not in "0123456789abcdef" for char in marker_digest.lower())
            ):
                await session.rollback()
                return False
            now = utc_now()
            row.reap_pgid = pgid
            row.reap_marker_digest = marker_digest
            row.reap_proof_kind = proof_kind
            row.reap_proof_verified_at = now
            row.reap_authenticated_at = now if proof_kind == "worker_death" else None
            row.reap_death_confirmed_at = now if proof_kind == "worker_death" else None
            await session.commit()
            return True

    async def consume_expired_orphan_reap_proof(
        self, job_id: str, *, tenant_id: str
    ) -> LivePlanningJobSnapshot | None:
        """Consume a DB-backed proof after a reaper crashed before completion."""
        async with self._database.sessions() as session:
            row = await session.scalar(
                select(LivePlanningJobRow).with_for_update().where(
                    LivePlanningJobRow.id == job_id,
                    LivePlanningJobRow.tenant_id == tenant_id,
                )
            )
            if row is None or row.reap_owner is None:
                await session.rollback()
                return None
            if row.reap_expires_at is None or _aware(row.reap_expires_at) > utc_now():
                await session.rollback()
                return None
            if not (
                row.reap_proof_verified_at is not None
                and row.reap_marker_digest is not None
                and row.reap_proof_kind in {"worker_death", "spawn_absent"}
                and (
                    (
                        row.reap_proof_kind == "worker_death"
                        and row.reap_pgid is not None
                        and row.reap_authenticated_at is not None
                        and row.reap_death_confirmed_at is not None
                    )
                    or (
                        row.reap_proof_kind == "spawn_absent"
                        and row.reap_pgid is None
                        and row.reap_authenticated_at is None
                        and row.reap_death_confirmed_at is None
                    )
                )
                and row.reap_target_owner is not None
                and row.reap_target_generation is not None
            ):
                await session.rollback()
                return None
            snapshot = LivePlanningJobSnapshot.model_validate(row.snapshot)
            row.reap_owner = None
            row.reap_generation = None
            row.reap_expires_at = None
            row.reap_target_owner = None
            row.reap_target_generation = None
            row.reap_controller = None
            row.reap_pgid = None
            row.reap_marker_digest = None
            row.reap_proof_kind = None
            row.reap_proof_verified_at = None
            row.reap_authenticated_at = None
            row.reap_death_confirmed_at = None
            await session.commit()
            return snapshot

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
