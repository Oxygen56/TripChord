"""Durable browser acquisition and consumer repository."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from tripchord.persistence.database import Database
from tripchord.persistence.models import (
    BrowserAcquisitionRow,
    BrowserTaskConsumerRow,
    CompanionSessionRow,
    utc_now,
)
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserQuote,
    BrowserSourceExecutionReceipt,
    BrowserTaskCompletion,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
)


class DurableBrowserTaskConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class DurableBrowserAcquisitionLease:
    acquisition_id: str
    consumer_id: str
    public_task_id: str
    submission: BrowserTaskSubmission
    owner: str
    generation: int
    claim_token: str
    session_id: str
    session_generation: int
    runtime_instance_id: str | None
    build_identity: dict[str, Any] | None
    claimed_at: datetime
    lease_expires_at: datetime
    capability: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrowserConsumerProjection:
    consumer_id: str
    acquisition_id: str
    consumer_state: str
    acquisition_state: BrowserTaskState
    snapshot: BrowserTaskSnapshot
    source_receipt: dict[str, Any] | None
    binding_receipt: dict[str, Any] | None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class DurableBrowserTaskStore:
    def __init__(self, database: Database, *, authority_partition_sha256: str) -> None:
        if len(authority_partition_sha256) != 64:
            raise ValueError("authority partition must be a SHA-256 hex digest")
        self._database = database
        self._authority_partition = authority_partition_sha256

    @property
    def _submit_lock(self) -> asyncio.Lock:
        """Serialize SQLite writers while retaining a DB unique-key fence.

        A unique active key is still required for separate Database instances;
        this process-local lock avoids StaticPool's single SQLite connection
        seeing two overlapping ``BEGIN`` statements in the common case.
        """

        database = cast(Any, self._database)
        lock = getattr(database, "_browser_task_submit_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            database._browser_task_submit_lock = lock
        return lock

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def _submission_fingerprint(cls, submission: BrowserTaskSubmission) -> str:
        """Match the Bridge reuse key and ignore internal control options."""

        payload = submission.query.model_dump(mode="json")
        options = dict(payload.get("options") or {})
        payload["options"] = {
            key: value
            for key, value in options.items()
            if not key.startswith("__tripchord_")
        }
        return cls._digest(
            {
                "provider": submission.provider.value,
                "kind": submission.kind.value,
                "query": payload,
                "reuse_partition_sha256": submission.reuse_partition_sha256,
            }
        )

    def _snapshot(
        self,
        row: BrowserAcquisitionRow,
        consumer_id: str,
        consumer: BrowserTaskConsumerRow | None = None,
    ) -> BrowserTaskSnapshot:
        sub = BrowserTaskSubmission.model_validate(row.submission)
        return BrowserTaskSnapshot(
            id=consumer_id,
            provider=BrowserProvider(row.provider),
            kind=BrowserVertical(row.kind),
            query=sub.query,
            state=BrowserTaskState("claimed" if row.state == "completing" else row.state),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
            attempt_count=row.attempt_count,
            claimed_by=row.lease_owner,
            claimed_at=_aware(row.claimed_at) if row.claimed_at else None,
            quotes=tuple(BrowserQuote.model_validate(x) for x in (row.quotes or [])),
            failure=BrowserFailure.model_validate(row.failure) if row.failure else None,
            reused_from_task_id=(
                consumer.reused_from_task_id
                if consumer is not None
                else row.reused_from_task_id
            ),
            reuse_age_seconds=(
                consumer.reuse_age_seconds
                if consumer is not None
                else row.reuse_age_seconds
            ),
            inflight_coalesced=row.inflight_coalesced_count > 0,
        )

    async def _projection(
        self, session: Any, consumer: BrowserTaskConsumerRow, acquisition: BrowserAcquisitionRow
    ) -> BrowserConsumerProjection:
        # Formal active consumers share one public task id.  They still retain
        # separate durable bindings so cancellation and idempotency remain
        # request-specific, while the claimed/complete receipt is always the
        # primary task the Companion actually owns.
        public_id = consumer.id
        terminal_reuse = (
            acquisition.state == "succeeded"
            and consumer.reused_from_task_id is not None
        )
        if acquisition.public_task_id and not terminal_reuse:
            public_id = acquisition.public_task_id
        elif acquisition.state in {"queued", "claimed"}:
            primary = await session.scalar(
                select(BrowserTaskConsumerRow)
                .where(
                    BrowserTaskConsumerRow.acquisition_id == acquisition.id,
                    BrowserTaskConsumerRow.is_primary.is_(True),
                    BrowserTaskConsumerRow.state == "active",
                )
                .limit(1)
            )
            if primary is not None:
                public_id = primary.id
        return BrowserConsumerProjection(
            public_id,
            acquisition.id,
            consumer.state,
            BrowserTaskState(
                "claimed" if acquisition.state == "completing" else acquisition.state
            ),
            self._snapshot(acquisition, public_id, consumer),
            acquisition.source_receipt,
            consumer.binding_receipt,
        )

    async def upsert_companion_session(
        self,
        *,
        session_id: str,
        companion_id: str,
        runtime_instance_id: str | None,
        build_identity: dict[str, Any] | None,
        providers: list[str],
        scopes: list[str],
        expires_at: datetime,
        adapter_version: str | None = None,
        contract_version: str | None = None,
    ) -> CompanionSessionRow:
        async with self._database.sessions() as s:
            now = utc_now()
            dialect = s.bind.dialect.name if s.bind is not None else ""
            if dialect == "postgresql":
                # Insert first, then lock the canonical row.  This closes the
                # first-seen race where two API instances both SELECT no row
                # and one loses an IntegrityError on INSERT.
                await s.execute(
                    pg_insert(CompanionSessionRow)
                    .values(
                        id=session_id,
                        authority_partition_sha256=self._authority_partition,
                        companion_id=companion_id,
                        session_generation=1,
                        runtime_instance_id=runtime_instance_id,
                        build_identity=build_identity,
                        providers=providers,
                        scopes=scopes,
                        adapter_version=adapter_version,
                        contract_version=contract_version,
                        last_seen_at=now,
                        expires_at=expires_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            CompanionSessionRow.authority_partition_sha256,
                            CompanionSessionRow.id,
                        ]
                    )
                )
            row = await s.scalar(
                select(CompanionSessionRow)
                .where(
                    CompanionSessionRow.id == session_id,
                    CompanionSessionRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            if row is None:
                row = CompanionSessionRow(
                    id=session_id,
                    authority_partition_sha256=self._authority_partition,
                    companion_id=companion_id,
                    session_generation=1,
                    runtime_instance_id=runtime_instance_id,
                    build_identity=build_identity,
                    providers=providers,
                    scopes=scopes,
                    adapter_version=adapter_version,
                    contract_version=contract_version,
                    last_seen_at=now,
                    expires_at=expires_at,
                )
                s.add(row)
            else:
                if (
                    (runtime_instance_id is not None
                     and row.runtime_instance_id != runtime_instance_id)
                    or (build_identity is not None and row.build_identity != build_identity)
                    or (adapter_version is not None and row.adapter_version != adapter_version)
                    or (contract_version is not None and row.contract_version != contract_version)
                ):
                    row.session_generation += 1
                if runtime_instance_id is not None:
                    row.runtime_instance_id = runtime_instance_id
                if build_identity is not None:
                    row.build_identity = build_identity
                row.providers = providers
                row.scopes = scopes
                if adapter_version is not None:
                    row.adapter_version = adapter_version
                if contract_version is not None:
                    row.contract_version = contract_version
                row.last_seen_at = now
                row.expires_at = expires_at
            await s.commit()
            return row

    async def submit_consumer(
        self,
        submission: BrowserTaskSubmission,
        *,
        consumer_id: str,
        tenant_id: str,
        tenant_partition: str,
        capability: dict[str, Any] | None = None,
        job_id: str | None = None,
        request_sha256: str | None = None,
        run_id: str | None = None,
        run_revision: int | None = None,
        allow_recent_quote_reuse: bool = True,
        force_fresh: bool = False,
    ) -> BrowserConsumerProjection:
        partition = self._digest(
            {
                "tenant_id": tenant_id,
                "tenant_partition": tenant_partition,
                "capability": capability,
            }
        )
        fingerprint = self._submission_fingerprint(submission)
        # Ordinary single-flight and recent terminal reuse are explicit
        # opt-ins, matching BrowserTaskBridge. Formal capabilities may share
        # an active lineage without the UI option; force_fresh/no-reuse still
        # starts an independent acquisition.
        reuse_enabled = (
            not force_fresh
            and allow_recent_quote_reuse
            and (
                capability is not None
                or (
                    submission.query.options.get(
                        "__tripchord_allow_recent_quote_reuse"
                    )
                    is True
                    and submission.reuse_partition_sha256 is not None
                )
            )
        )
        active_key = self._digest(
            {
                "authority": self._authority_partition,
                "tenant": tenant_id,
                "partition": partition,
                "fingerprint": fingerprint,
            }
        )
        if capability is not None:
            required_capability_fields = {
                "terminal_job_id",
                "request_sha256",
                "run_id",
                "attempt_digest",
            }
            if (
                not required_capability_fields.issubset(capability)
                or capability.get("terminal_job_id") != job_id
                or capability.get("request_sha256") != request_sha256
                or capability.get("run_id") != run_id
            ):
                raise DurableBrowserTaskConflict(
                    "formal capability does not match consumer lineage"
                )
        async with self._submit_lock, self._database.sessions() as s:
            old = await s.scalar(
                select(BrowserTaskConsumerRow).where(
                    BrowserTaskConsumerRow.id == consumer_id,
                    BrowserTaskConsumerRow.tenant_id == tenant_id,
                    BrowserTaskConsumerRow.authority_partition_sha256
                    == self._authority_partition,
                )
            )
            if old:
                row = await s.scalar(
                    select(BrowserAcquisitionRow).where(
                        BrowserAcquisitionRow.id == old.acquisition_id,
                        BrowserAcquisitionRow.tenant_id == tenant_id,
                        BrowserAcquisitionRow.authority_partition_sha256
                        == self._authority_partition,
                    )
                )
                if row is None:
                    raise DurableBrowserTaskConflict("missing acquisition")
                if (
                    row.fingerprint_sha256 != fingerprint
                    or row.tenant_partition != partition
                    or old.job_id != job_id
                    or old.request_sha256 != request_sha256
                    or old.run_id != run_id
                    or old.run_revision != run_revision
                    or old.capability != capability
                ):
                    raise DurableBrowserTaskConflict(
                        "consumer idempotency key was reused with different lineage"
                    )
                return await self._projection(s, old, row)
            row = (
                await s.scalar(
                    select(BrowserAcquisitionRow)
                    .where(
                        BrowserAcquisitionRow.tenant_id == tenant_id,
                        BrowserAcquisitionRow.authority_partition_sha256
                        == self._authority_partition,
                        BrowserAcquisitionRow.tenant_partition == partition,
                        BrowserAcquisitionRow.fingerprint_sha256 == fingerprint,
                        BrowserAcquisitionRow.state.in_(("queued", "claimed", "succeeded")),
                    )
                    .order_by(BrowserAcquisitionRow.updated_at.desc())
                    .with_for_update()
                )
                if reuse_enabled
                else None
            )
            now = utc_now()
            reused_from_task_id: str | None = None
            reuse_age_seconds: float | None = None
            if row is not None and row.state == "succeeded" and capability is not None:
                # A formal capability may only consume its own active
                # single-flight acquisition; terminal reuse must not create a
                # receipt outside the signed execution attempt.
                row = None
            elif row is not None and row.state == "succeeded":
                fresh = bool(row.quotes) and all(
                    0
                    <= (
                        now - _aware(BrowserQuote.model_validate(q).captured_at)
                    ).total_seconds()
                    < 600
                    for q in row.quotes
                )
                if not fresh:
                    row = None
                else:
                    ages = tuple(
                        (
                            now
                            - _aware(BrowserQuote.model_validate(q).captured_at)
                        ).total_seconds()
                        for q in row.quotes
                    )
                    reuse_age_seconds = max(0.0, max(ages, default=0.0))
                    reused_from_task_id = await s.scalar(
                        select(BrowserTaskConsumerRow.id)
                        .where(
                            BrowserTaskConsumerRow.acquisition_id == row.id,
                            BrowserTaskConsumerRow.is_primary.is_(True),
                        )
                        .limit(1)
                    )
            if row is None:
                row = BrowserAcquisitionRow(
                    id=f"browser-acq-{uuid.uuid4()}",
                    tenant_id=tenant_id,
                    authority_partition_sha256=self._authority_partition,
                    tenant_partition=partition,
                    active_singleflight_key=(
                        None
                        if not reuse_enabled
                        else active_key
                    ),
                    public_task_id=consumer_id,
                    reference_count=1,
                    fingerprint_sha256=fingerprint,
                    provider=submission.provider.value,
                    kind=submission.kind.value,
                    submission=submission.model_dump(mode="json"),
                    state="queued",
                    attempt_count=0,
                    quotes=[],
                    created_at=now,
                    updated_at=now,
                )
                s.add(row)
                try:
                    await s.flush()
                except IntegrityError:
                    # A second Database instance may have won the unique
                    # active-key race. Re-read its acquisition and bind to it.
                    await s.rollback()
                    row = await s.scalar(
                        select(BrowserAcquisitionRow).where(
                            BrowserAcquisitionRow.active_singleflight_key == active_key,
                            BrowserAcquisitionRow.authority_partition_sha256
                            == self._authority_partition,
                            BrowserAcquisitionRow.state.in_(("queued", "claimed")),
                        ).with_for_update()
                    )
                    if row is None:
                        row = BrowserAcquisitionRow(
                            id=f"browser-acq-{uuid.uuid4()}",
                            tenant_id=tenant_id,
                            authority_partition_sha256=self._authority_partition,
                            tenant_partition=partition,
                            active_singleflight_key=active_key,
                            public_task_id=consumer_id,
                            reference_count=1,
                            fingerprint_sha256=fingerprint,
                            provider=submission.provider.value,
                            kind=submission.kind.value,
                            submission=submission.model_dump(mode="json"),
                            state="queued",
                            attempt_count=0,
                            quotes=[],
                            created_at=now,
                            updated_at=now,
                        )
                        s.add(row)
                        await s.flush()
                    else:
                        row.reference_count += 1
            elif row.state in {"queued", "claimed"}:
                row.reference_count += 1
                row.inflight_coalesced_count += 1
            consumer = BrowserTaskConsumerRow(
                id=consumer_id,
                tenant_id=tenant_id,
                authority_partition_sha256=self._authority_partition,
                acquisition_id=row.id,
                job_id=job_id,
                request_sha256=request_sha256,
                run_id=run_id,
                run_revision=run_revision,
                capability=capability,
                reused_from_task_id=reused_from_task_id,
                reuse_age_seconds=reuse_age_seconds,
                binding_receipt=(
                    {
                        "schema": "tripchord.browser-reuse-binding.v1",
                        "acquisition_id": row.id,
                        "source_receipt_sha256": self._digest(row.source_receipt),
                    }
                    if row.source_receipt is not None
                    else None
                ),
                state="active",
                is_primary=not bool(
                    await s.scalar(
                        select(BrowserTaskConsumerRow.id)
                        .where(
                            BrowserTaskConsumerRow.acquisition_id == row.id,
                            BrowserTaskConsumerRow.state == "active",
                        )
                        .limit(1)
                    )
                ),
                created_at=now,
                updated_at=now,
            )
            s.add(consumer)
            await s.commit()
            return await self._projection(s, consumer, row)

    async def get_consumer(
        self, consumer_id: str, *, tenant_id: str
    ) -> BrowserConsumerProjection | None:
        async with self._database.sessions() as s:
            c = await s.scalar(
                select(BrowserTaskConsumerRow).where(
                    BrowserTaskConsumerRow.id == consumer_id,
                    BrowserTaskConsumerRow.tenant_id == tenant_id,
                    BrowserTaskConsumerRow.authority_partition_sha256 == self._authority_partition,
                )
            )
            if c is None:
                return None
            a = await s.scalar(
                select(BrowserAcquisitionRow).where(
                    BrowserAcquisitionRow.id == c.acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            if a is None:
                return None
            now = utc_now()
            if a.state == "claimed" and (
                a.lease_expires_at is None or _aware(a.lease_expires_at) <= now
            ):
                submission = BrowserTaskSubmission.model_validate(a.submission)
                a.lease_owner = None
                a.claim_consumer_id = None
                a.claim_token_sha256 = None
                a.lease_expires_at = None
                a.session_id = None
                a.session_generation = None
                a.runtime_instance_id = None
                a.build_identity = None
                a.updated_at = now
                if a.attempt_count >= submission.max_attempts:
                    # A waiter must reach a terminal state even when no new
                    # claimant arrives to perform the usual expiry sweep.
                    a.state = "failed"
                    a.failure = BrowserFailure(
                        code=BrowserFailureCode.TIMEOUT,
                        message="browser Companion lease expired after the maximum attempts",
                        retryable=True,
                        captured_at=now,
                    ).model_dump(mode="json")
                    a.active_singleflight_key = None
                    a.terminal_at = now
                else:
                    a.state = "queued"
                await s.commit()
            return await self._projection(s, c, a)

    async def get_consumer_capability(
        self, consumer_id: str, *, tenant_id: str
    ) -> dict[str, Any] | None:
        async with self._database.sessions() as s:
            capability = await s.scalar(
                select(BrowserTaskConsumerRow.capability).where(
                    BrowserTaskConsumerRow.id == consumer_id,
                    BrowserTaskConsumerRow.tenant_id == tenant_id,
                    BrowserTaskConsumerRow.authority_partition_sha256
                    == self._authority_partition,
                )
            )
            return dict(capability) if capability is not None else None

    async def get_claim_lease(
        self,
        public_task_id: str,
        *,
        tenant_id: str,
        claim_token: str,
    ) -> DurableBrowserAcquisitionLease | None:
        """Rehydrate a claim on another API instance without persisting tokens."""

        async with self._database.sessions() as s:
            row = await s.scalar(
                select(BrowserAcquisitionRow).where(
                    BrowserAcquisitionRow.public_task_id == public_task_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                    BrowserAcquisitionRow.state.in_(("claimed", "completing")),
                )
            )
            if row is None or row.claim_consumer_id is None or row.session_id is None:
                return None
            now = utc_now()
            if (
                row.claim_token_sha256 is None
                or not hmac.compare_digest(
                    row.claim_token_sha256,
                    hashlib.sha256(claim_token.encode()).hexdigest(),
                )
                or row.session_generation is None
            ):
                return None
            # A completing row is an outbox item, not an active lease.  It is
            # deliberately rehydratable after the original lease/session has
            # expired so a publisher can finish it without a client retry.
            if row.state != "completing" and (
                row.lease_expires_at is None or _aware(row.lease_expires_at) <= now
            ):
                return None
            session = await s.scalar(
                select(CompanionSessionRow).where(
                    CompanionSessionRow.id == row.session_id,
                    CompanionSessionRow.authority_partition_sha256
                    == self._authority_partition,
                )
            )
            if row.state != "completing" and (
                session is None
                or session.session_generation != row.session_generation
                or session.runtime_instance_id != row.runtime_instance_id
                or session.build_identity != row.build_identity
                or _aware(session.expires_at) <= now
            ):
                return None
            consumer = await s.scalar(
                select(BrowserTaskConsumerRow).where(
                    BrowserTaskConsumerRow.id == row.claim_consumer_id,
                    BrowserTaskConsumerRow.acquisition_id == row.id,
                    BrowserTaskConsumerRow.authority_partition_sha256
                    == self._authority_partition,
                )
            )
            if consumer is None:
                return None
            return DurableBrowserAcquisitionLease(
                row.id,
                consumer.id,
                public_task_id,
                BrowserTaskSubmission.model_validate(row.submission),
                row.lease_owner or "",
                row.lease_generation,
                claim_token,
                row.session_id,
                row.session_generation or 0,
                row.runtime_instance_id,
                dict(row.build_identity) if row.build_identity is not None else None,
                _aware(row.claimed_at or row.updated_at),
                _aware(row.lease_expires_at or row.updated_at),
                dict(consumer.capability) if consumer.capability is not None else None,
            )

    async def count_pending(self, *, tenant_id: str) -> int:
        async with self._database.sessions() as s:
            value = await s.scalar(
                select(func.count(BrowserAcquisitionRow.id)).where(
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                    BrowserAcquisitionRow.state.in_(("queued", "claimed")),
                )
            )
            return int(value or 0)

    async def list_companion_sessions(self) -> tuple[CompanionSessionRow, ...]:
        async with self._database.sessions() as s:
            rows = (
                await s.scalars(
                    select(CompanionSessionRow)
                    .where(
                        CompanionSessionRow.authority_partition_sha256
                        == self._authority_partition
                    )
                    .order_by(CompanionSessionRow.last_seen_at.desc())
                )
            ).all()
            return tuple(rows)

    async def get_companion_session(
        self, session_id: str
    ) -> CompanionSessionRow | None:
        async with self._database.sessions() as s:
            return cast(
                CompanionSessionRow | None,
                await s.scalar(
                    select(CompanionSessionRow).where(
                        CompanionSessionRow.id == session_id,
                        CompanionSessionRow.authority_partition_sha256
                        == self._authority_partition,
                    )
                ),
            )

    async def _current_session(
        self,
        session: Any,
        *,
        session_id: str,
        session_generation: int,
        runtime_instance_id: str | None,
        build_identity: dict[str, Any] | None,
    ) -> CompanionSessionRow:
        current = await session.scalar(
            select(CompanionSessionRow)
            .where(
                CompanionSessionRow.id == session_id,
                CompanionSessionRow.authority_partition_sha256 == self._authority_partition,
            )
            .with_for_update()
        )
        if (
            current is None
            or current.session_generation != session_generation
            or (
                runtime_instance_id is not None
                and current.runtime_instance_id != runtime_instance_id
            )
            or (build_identity is not None and current.build_identity != build_identity)
            or _aware(current.expires_at) <= utc_now()
        ):
            raise DurableBrowserTaskConflict("invalid or stale Companion session")
        return cast(CompanionSessionRow, current)

    async def renew_session_leases(
        self,
        *,
        session_id: str,
        session_generation: int,
        runtime_instance_id: str | None = None,
        build_identity: dict[str, Any] | None = None,
        lease_seconds: int = 30,
    ) -> int:
        """Renew every live claim owned by one current Companion session."""

        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        async with self._database.sessions() as s:
            session = await self._current_session(
                s,
                session_id=session_id,
                session_generation=session_generation,
                runtime_instance_id=runtime_instance_id,
                build_identity=build_identity,
            )
            now = utc_now()
            rows = (
                await s.scalars(
                    select(BrowserAcquisitionRow)
                    .where(
                        BrowserAcquisitionRow.authority_partition_sha256
                        == self._authority_partition,
                        BrowserAcquisitionRow.session_id == session.id,
                        BrowserAcquisitionRow.session_generation == session_generation,
                        BrowserAcquisitionRow.state == "claimed",
                        BrowserAcquisitionRow.lease_expires_at > now,
                    )
                    .with_for_update()
                )
            ).all()
            renewed = 0
            for row in rows:
                if (
                    row.runtime_instance_id != session.runtime_instance_id
                    or row.build_identity != session.build_identity
                    or row.attempt_deadline_at is None
                    or _aware(row.attempt_deadline_at) <= now
                ):
                    continue
                row.lease_expires_at = min(
                    now + timedelta(seconds=lease_seconds),
                    _aware(row.attempt_deadline_at),
                )
                row.heartbeat_at = now
                row.updated_at = now
                renewed += 1
            await s.commit()
            return renewed

    async def claim_acquisitions(
        self,
        *,
        owner: str,
        session_id: str,
        session_generation: int,
        runtime_instance_id: str | None = None,
        build_identity: dict[str, Any] | None = None,
        limit: int = 6,
        lease_seconds: int = 30,
    ) -> tuple[DurableBrowserAcquisitionLease, ...]:
        if not 1 <= limit <= 6:
            raise ValueError("limit must be between 1 and 6")
        async with self._database.sessions() as s:
            session = await self._current_session(
                s,
                session_id=session_id,
                session_generation=session_generation,
                runtime_instance_id=runtime_instance_id,
                build_identity=build_identity,
            )
            now = utc_now()
            allowed_providers = set(session.providers)
            allowed_scopes = set(session.scopes)
            scope_filters = [
                and_(
                    BrowserAcquisitionRow.provider == provider,
                    BrowserAcquisitionRow.kind == kind,
                )
                for scope in allowed_scopes
                for provider, kind in [scope.split(":", 1)]
                if ":" in scope
            ]
            claim_filters = [
                BrowserAcquisitionRow.authority_partition_sha256
                == self._authority_partition,
                BrowserAcquisitionRow.provider.in_(allowed_providers),
                or_(
                    BrowserAcquisitionRow.state == "queued",
                    (BrowserAcquisitionRow.state == "claimed")
                    & (BrowserAcquisitionRow.lease_expires_at < now),
                ),
            ]
            if scope_filters:
                claim_filters.append(or_(*scope_filters))
            rows = (
                await s.scalars(
                    select(BrowserAcquisitionRow)
                    .where(*claim_filters)
                    .order_by(BrowserAcquisitionRow.created_at, BrowserAcquisitionRow.id)
                    # Scan past an early provider run so the deterministic
                    # provider rotation below can fill the batch fairly.
                    .limit(max(32, limit * 8))
                    .with_for_update(skip_locked=True)
                )
            ).all()
            eligible_by_provider: dict[str, list[BrowserAcquisitionRow]] = {}
            for candidate in rows:
                if (
                    candidate.provider not in allowed_providers
                    or (
                        allowed_scopes
                        and f"{candidate.provider}:{candidate.kind}" not in allowed_scopes
                    )
                ):
                    continue
                eligible_by_provider.setdefault(candidate.provider, []).append(candidate)
            provider_order = [
                provider for provider in session.providers if provider in eligible_by_provider
            ]
            provider_order.extend(
                provider
                for provider in sorted(eligible_by_provider)
                if provider not in provider_order
            )
            fair_rows: list[BrowserAcquisitionRow] = []
            while len(fair_rows) < limit and provider_order:
                made_progress = False
                for provider in tuple(provider_order):
                    candidates = eligible_by_provider.get(provider, [])
                    if not candidates:
                        provider_order.remove(provider)
                        continue
                    fair_rows.append(candidates.pop(0))
                    made_progress = True
                    if len(fair_rows) >= limit:
                        break
                if not made_progress:
                    break
            result: list[DurableBrowserAcquisitionLease] = []
            for row in fair_rows:
                if (
                    row.provider not in allowed_providers
                    or (
                        allowed_scopes
                        and f"{row.provider}:{row.kind}" not in allowed_scopes
                    )
                ):
                    continue
                if (
                    row.provider == BrowserProvider.QUNAR.value
                    and row.kind == BrowserVertical.LODGING.value
                ):
                    if s.bind is not None and s.bind.dialect.name == "postgresql":
                        await s.execute(
                            select(
                                func.pg_advisory_xact_lock(
                                    func.hashtext(f"{self._authority_partition}:qunar:lodging")
                                )
                            )
                        )
                    active_qunar = await s.scalar(
                        select(func.count(BrowserAcquisitionRow.id)).where(
                            BrowserAcquisitionRow.authority_partition_sha256
                            == self._authority_partition,
                            BrowserAcquisitionRow.provider == row.provider,
                            BrowserAcquisitionRow.kind == row.kind,
                            BrowserAcquisitionRow.state == "claimed",
                            BrowserAcquisitionRow.lease_expires_at > now,
                        )
                    )
                    if active_qunar:
                        continue
                c = await s.scalar(
                    select(BrowserTaskConsumerRow)
                    .where(
                        BrowserTaskConsumerRow.acquisition_id == row.id,
                        BrowserTaskConsumerRow.is_primary.is_(True),
                        BrowserTaskConsumerRow.state == "active",
                    )
                    .limit(1)
                )
                if c is None:
                    continue
                submission_row = BrowserTaskSubmission.model_validate(row.submission)
                if row.state == "claimed" and row.attempt_count >= submission_row.max_attempts:
                    row.state = "failed"
                    row.failure = BrowserFailure(
                        code=BrowserFailureCode.TIMEOUT,
                        message="browser Companion lease expired after the maximum attempts",
                        retryable=True,
                        captured_at=now,
                    ).model_dump(mode="json")
                    row.active_singleflight_key = None
                    row.lease_owner = None
                    row.claim_token_sha256 = None
                    row.lease_expires_at = None
                    row.terminal_at = now
                    row.updated_at = now
                    continue
                if c.capability is not None and (
                    session.runtime_instance_id is None or session.build_identity is None
                ):
                    # Formal source receipts must carry an explicit runtime
                    # and build identity; an ordinary Companion heartbeat may
                    # remain intentionally anonymous.
                    continue
                token = secrets.token_urlsafe(32)
                row.state = "claimed"
                row.lease_owner = owner
                row.lease_generation += 1
                row.attempt_count += 1
                row.claimed_at = now
                row.attempt_deadline_at = now + timedelta(seconds=submission_row.timeout_seconds)
                row.lease_expires_at = min(
                    now + timedelta(seconds=lease_seconds), row.attempt_deadline_at
                )
                row.claim_token_sha256 = hashlib.sha256(token.encode()).hexdigest()
                row.claim_consumer_id = c.id
                row.companion_id = session.companion_id
                row.session_id = session.id
                row.session_generation = session_generation
                row.runtime_instance_id = session.runtime_instance_id
                row.build_identity = session.build_identity
                result.append(
                    DurableBrowserAcquisitionLease(
                        row.id,
                        c.id,
                        row.public_task_id or c.id,
                        BrowserTaskSubmission.model_validate(row.submission),
                        owner,
                        row.lease_generation,
                        token,
                        session.id,
                        session_generation,
                        session.runtime_instance_id,
                        (
                            dict(session.build_identity)
                            if session.build_identity is not None
                            else None
                        ),
                        now,
                        _aware(row.lease_expires_at),
                        dict(c.capability) if c.capability is not None else None,
                    )
                )
            await s.commit()
            return tuple(result)

    async def heartbeat_acquisition(
        self,
        acquisition_id: str,
        *,
        owner: str,
        generation: int,
        claim_token: str,
        session_id: str,
        session_generation: int,
        runtime_instance_id: str | None = None,
        build_identity: dict[str, Any] | None = None,
    ) -> bool:
        async with self._database.sessions() as s:
            # Keep the same lock order as claim/renew: CompanionSession first,
            # then the acquisition.  Otherwise a heartbeat racing a session
            # renewal can deadlock on PostgreSQL.
            try:
                current = await self._current_session(
                    s,
                    session_id=session_id,
                    session_generation=session_generation,
                    runtime_instance_id=runtime_instance_id,
                    build_identity=build_identity,
                )
            except DurableBrowserTaskConflict:
                await s.rollback()
                return False
            row = await s.scalar(
                select(BrowserAcquisitionRow)
                .where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            now = utc_now()
            primary = (
                await s.scalar(
                    select(BrowserTaskConsumerRow)
                    .where(
                        BrowserTaskConsumerRow.acquisition_id == acquisition_id,
                        BrowserTaskConsumerRow.is_primary.is_(True),
                        BrowserTaskConsumerRow.state == "active",
                    )
                    .limit(1)
                )
                if row is not None
                else None
            )
            if (
                row is None
                or row.lease_owner != owner
                or row.lease_generation != generation
                or row.session_generation != session_generation
                or row.session_id != current.id
                or (
                    primary is not None
                    and primary.capability is not None
                    and (runtime_instance_id is None or build_identity is None)
                )
                or (
                    runtime_instance_id is not None
                    and row.runtime_instance_id != current.runtime_instance_id
                )
                or (build_identity is not None and row.build_identity != current.build_identity)
                or row.claim_token_sha256 != hashlib.sha256(claim_token.encode()).hexdigest()
                or not row.lease_expires_at
                or _aware(row.lease_expires_at) <= now
                or not row.attempt_deadline_at
                or _aware(row.attempt_deadline_at) <= now
            ):
                await s.rollback()
                return False
            row.heartbeat_at = now
            row.lease_expires_at = min(
                now + timedelta(seconds=30), _aware(row.attempt_deadline_at)
            )
            row.updated_at = now
            await s.commit()
            return True

    async def complete_acquisition(
        self,
        acquisition_id: str,
        *,
        tenant_id: str,
        owner: str,
        generation: int,
        claim_token: str,
        session_id: str,
        session_generation: int,
        completion: BrowserTaskCompletion,
        completion_snapshot: BrowserTaskSnapshot | None = None,
        source_receipt: BrowserSourceExecutionReceipt | None = None,
        runtime_instance_id: str | None = None,
        build_identity: dict[str, Any] | None = None,
    ) -> BrowserTaskSnapshot:
        async with self._database.sessions() as s:
            # Match claim/renew/heartbeat lock ordering (session ->
            # acquisition) so concurrent completion and lease renewal cannot
            # deadlock on PostgreSQL.
            current = await self._current_session(
                s,
                session_id=session_id,
                session_generation=session_generation,
                runtime_instance_id=runtime_instance_id,
                build_identity=build_identity,
            )
            row = await s.scalar(
                select(BrowserAcquisitionRow)
                .where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            now = utc_now()
            if row is not None and any(
                quote.provider.value != row.provider or quote.kind.value != row.kind
                for quote in completion.quotes
            ):
                raise DurableBrowserTaskConflict(
                    "completion quote provider or kind differs from the acquisition"
                )
            primary = (
                await s.scalar(
                    select(BrowserTaskConsumerRow)
                    .where(
                        BrowserTaskConsumerRow.acquisition_id == acquisition_id,
                        BrowserTaskConsumerRow.is_primary.is_(True),
                        BrowserTaskConsumerRow.state == "active",
                    )
                    .limit(1)
                )
                if row is not None
                else None
            )
            if (
                row is None
                or row.lease_owner != owner
                or row.lease_generation != generation
                or row.session_generation != session_generation
                or row.session_id != current.id
                or (
                    primary is not None
                    and primary.capability is not None
                    and (runtime_instance_id is None or build_identity is None)
                )
                or (
                    runtime_instance_id is not None
                    and row.runtime_instance_id != current.runtime_instance_id
                )
                or (build_identity is not None and row.build_identity != current.build_identity)
                or row.claim_token_sha256 != hashlib.sha256(claim_token.encode()).hexdigest()
                or not row.lease_expires_at
                or _aware(row.lease_expires_at) <= now
            ):
                raise DurableBrowserTaskConflict("stale acquisition lease")
            if source_receipt is not None and (
                row.public_task_id is None
                or source_receipt.task_id != row.public_task_id
            ):
                raise DurableBrowserTaskConflict("receipt is bound to another acquisition")
            row.state = completion.state.value
            row.quotes = [quote.model_dump(mode="json") for quote in completion.quotes]
            row.failure = completion.failure.model_dump(mode="json") if completion.failure else None
            row.source_receipt = source_receipt.model_dump(mode="json") if source_receipt else None
            row.active_singleflight_key = None
            row.reference_count = 0
            row.lease_owner = None
            row.claim_consumer_id = None
            row.claim_token_sha256 = None
            row.lease_expires_at = None
            row.terminal_at = now
            row.updated_at = now
            await s.commit()
            return self._snapshot(
                row,
                # The public task handle is immutable for the lifetime of
                # an active single-flight acquisition. Internal consumer
                # promotion must never make a receipt or completion drift to
                # a different externally visible id.
                row.public_task_id or row.id,
            )

    async def prepare_acquisition_completion(
        self,
        acquisition_id: str,
        *,
        tenant_id: str,
        owner: str,
        generation: int,
        claim_token: str,
        session_id: str,
        session_generation: int,
        completion: BrowserTaskCompletion,
        completion_snapshot: BrowserTaskSnapshot,
        source_receipt: BrowserSourceExecutionReceipt | None = None,
        event_details: dict[str, Any] | None = None,
        runtime_instance_id: str | None = None,
        build_identity: dict[str, Any] | None = None,
    ) -> str:
        """Freeze a validated completion before any formal ledger side effect."""

        async with self._database.sessions() as s:
            # Recovery retries first inspect the durable outbox.  They must
            # not require the old Companion session or a still-live lease.
            row = await s.scalar(
                select(BrowserAcquisitionRow)
                .where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                )
                .with_for_update()
            )
            if row is None or row.lease_owner != owner or row.lease_generation != generation:
                raise DurableBrowserTaskConflict("stale acquisition lease")
            now = utc_now()
            token_hash = hashlib.sha256(claim_token.encode()).hexdigest()
            payload = completion.model_dump(mode="json")
            snapshot_payload = completion_snapshot.model_dump(mode="json")
            receipt_payload = (
                source_receipt.model_dump(mode="json") if source_receipt is not None else None
            )
            digest = self._digest(
                {
                    "completion": payload,
                    "receipt": receipt_payload,
                    "snapshot": snapshot_payload,
                    "event_details": event_details,
                }
            )
            if row.state == "completing":
                if row.completion_sha256 != digest:
                    raise DurableBrowserTaskConflict("completion retry differs")
                return digest
            if row.state != "claimed":
                raise DurableBrowserTaskConflict("acquisition is not claimable for completion")
            current = await self._current_session(
                s,
                session_id=session_id,
                session_generation=session_generation,
                runtime_instance_id=runtime_instance_id,
                build_identity=build_identity,
            )
            if (
                row.claim_token_sha256 != token_hash
                or row.session_id != current.id
                or row.session_generation != session_generation
                or row.lease_expires_at is None
                or _aware(row.lease_expires_at) <= now
                or row.runtime_instance_id != current.runtime_instance_id
                or row.build_identity != current.build_identity
            ):
                raise DurableBrowserTaskConflict("stale acquisition lease")
            if any(
                quote.provider.value != row.provider or quote.kind.value != row.kind
                for quote in completion.quotes
            ):
                raise DurableBrowserTaskConflict(
                    "completion quote provider or kind differs from the acquisition"
                )
            if source_receipt is not None and source_receipt.task_id != row.public_task_id:
                raise DurableBrowserTaskConflict("receipt is bound to another acquisition")
            row.state = "completing"
            row.completion_payload = payload
            row.completion_receipt = receipt_payload
            row.completion_snapshot = snapshot_payload
            row.completion_event_details = event_details
            row.completion_sha256 = digest
            row.updated_at = now
            await s.commit()
            return digest

    async def get_pending_completion(
        self, acquisition_id: str, *, tenant_id: str
    ) -> tuple[
        BrowserTaskCompletion,
        BrowserTaskSnapshot,
        BrowserSourceExecutionReceipt | None,
        str,
    ] | None:
        async with self._database.sessions() as s:
            row = await s.scalar(
                select(BrowserAcquisitionRow).where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                    BrowserAcquisitionRow.state == "completing",
                )
            )
            if (
                row is None
                or row.completion_payload is None
                or row.completion_snapshot is None
                or row.completion_sha256 is None
            ):
                return None
            completion = BrowserTaskCompletion.model_validate(row.completion_payload)
            snapshot = BrowserTaskSnapshot.model_validate(row.completion_snapshot)
            receipt = (
                BrowserSourceExecutionReceipt.model_validate(row.completion_receipt)
                if row.completion_receipt is not None
                else None
            )
            return completion, snapshot, receipt, row.completion_sha256

    async def get_pending_completion_event_details(
        self, acquisition_id: str, *, tenant_id: str
    ) -> dict[str, Any] | None:
        async with self._database.sessions() as s:
            row = await s.scalar(
                select(BrowserAcquisitionRow.completion_event_details).where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                    BrowserAcquisitionRow.state == "completing",
                )
            )
            return dict(row) if row is not None else None

    async def list_pending_completions(
        self, *, tenant_id: str
    ) -> tuple[
        tuple[
            DurableBrowserAcquisitionLease,
            BrowserTaskCompletion,
            BrowserTaskSnapshot,
            BrowserSourceExecutionReceipt | None,
            str,
            dict[str, Any] | None,
        ],
        ...,
    ]:
        """Return frozen outbox items for a crash-safe publisher."""

        async with self._database.sessions() as s:
            rows = (
                await s.scalars(
                    select(BrowserAcquisitionRow)
                    .where(
                        BrowserAcquisitionRow.tenant_id == tenant_id,
                        BrowserAcquisitionRow.authority_partition_sha256
                        == self._authority_partition,
                        BrowserAcquisitionRow.state == "completing",
                    )
                    .order_by(BrowserAcquisitionRow.updated_at, BrowserAcquisitionRow.id)
                )
            ).all()
            result = []
            for row in rows:
                if (
                    row.claim_consumer_id is None
                    or row.completion_payload is None
                    or row.completion_snapshot is None
                    or row.completion_sha256 is None
                ):
                    continue
                consumer = await s.scalar(
                    select(BrowserTaskConsumerRow).where(
                        BrowserTaskConsumerRow.id == row.claim_consumer_id,
                        BrowserTaskConsumerRow.acquisition_id == row.id,
                        BrowserTaskConsumerRow.authority_partition_sha256
                        == self._authority_partition,
                    )
                )
                if consumer is None:
                    continue
                lease = DurableBrowserAcquisitionLease(
                    row.id,
                    consumer.id,
                    row.public_task_id or row.id,
                    BrowserTaskSubmission.model_validate(row.submission),
                    row.lease_owner or "",
                    row.lease_generation,
                    "",
                    row.session_id or "",
                    row.session_generation or 0,
                    row.runtime_instance_id,
                    dict(row.build_identity) if row.build_identity is not None else None,
                    _aware(row.claimed_at or row.updated_at),
                    _aware(row.lease_expires_at or row.updated_at),
                    dict(consumer.capability) if consumer.capability is not None else None,
                )
                result.append(
                    (
                        lease,
                        BrowserTaskCompletion.model_validate(row.completion_payload),
                        BrowserTaskSnapshot.model_validate(row.completion_snapshot),
                        (
                            BrowserSourceExecutionReceipt.model_validate(row.completion_receipt)
                            if row.completion_receipt is not None
                            else None
                        ),
                        row.completion_sha256,
                        (
                            dict(row.completion_event_details)
                            if row.completion_event_details is not None
                            else None
                        ),
                    )
                )
            return tuple(result)

    async def finalize_acquisition_completion(
        self,
        acquisition_id: str,
        *,
        tenant_id: str,
        completion_sha256: str,
    ) -> BrowserTaskSnapshot:
        async with self._database.sessions() as s:
            row = await s.scalar(
                select(BrowserAcquisitionRow)
                .where(
                    BrowserAcquisitionRow.id == acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256
                    == self._authority_partition,
                )
                .with_for_update()
            )
            if row is None or row.completion_payload is None:
                raise DurableBrowserTaskConflict("completion is not pending")
            if row.state != "completing":
                if row.completion_published_sha256 == completion_sha256:
                    return self._snapshot(row, row.public_task_id or row.id)
                raise DurableBrowserTaskConflict("completion is not pending")
            if row.completion_sha256 != completion_sha256:
                raise DurableBrowserTaskConflict("completion digest differs")
            completion = BrowserTaskCompletion.model_validate(row.completion_payload)
            source_receipt = (
                BrowserSourceExecutionReceipt.model_validate(row.completion_receipt)
                if row.completion_receipt is not None
                else None
            )
            now = utc_now()
            row.state = completion.state.value
            row.quotes = [quote.model_dump(mode="json") for quote in completion.quotes]
            row.failure = (
                completion.failure.model_dump(mode="json")
                if completion.failure is not None
                else None
            )
            row.source_receipt = (
                source_receipt.model_dump(mode="json") if source_receipt is not None else None
            )
            row.active_singleflight_key = None
            row.reference_count = 0
            row.lease_owner = None
            row.claim_consumer_id = None
            row.claim_token_sha256 = None
            row.lease_expires_at = None
            # Keep the frozen outbox and its published digest for audit and
            # idempotent publisher retries; terminal consumers only see the
            # normal public snapshot.
            row.completion_published_sha256 = completion_sha256
            row.completion_published_at = now
            row.terminal_at = now
            row.updated_at = now
            await s.commit()
            return self._snapshot(row, row.public_task_id or row.id)

    async def wait_consumer(
        self, consumer_id: str, *, tenant_id: str, timeout_seconds: float = 120
    ) -> BrowserConsumerProjection | None:
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            value = await self.get_consumer(consumer_id, tenant_id=tenant_id)
            if (
                value is None
                or value.acquisition_state.terminal
                or asyncio.get_running_loop().time() >= deadline
            ):
                return value
            await asyncio.sleep(0.1)

    async def cancel_consumer(
        self, consumer_id: str, *, tenant_id: str
    ) -> BrowserConsumerProjection | None:
        async with self._database.sessions() as s:
            c = await s.scalar(
                select(BrowserTaskConsumerRow)
                .where(
                    BrowserTaskConsumerRow.id == consumer_id,
                    BrowserTaskConsumerRow.tenant_id == tenant_id,
                    BrowserTaskConsumerRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            if c is None:
                return None
            a = await s.scalar(
                select(BrowserAcquisitionRow)
                .where(
                    BrowserAcquisitionRow.id == c.acquisition_id,
                    BrowserAcquisitionRow.tenant_id == tenant_id,
                    BrowserAcquisitionRow.authority_partition_sha256 == self._authority_partition,
                )
                .with_for_update()
            )
            if a is None:
                return None
            now = utc_now()
            target: BrowserTaskConsumerRow | None = c
            if c.state != "active" and a.state in {"queued", "claimed"}:
                # The public active handle is the acquisition's stable
                # primary id. Repeated cancellation of that handle consumes
                # the next durable reference rather than silently becoming a
                # no-op.
                target = await s.scalar(
                    select(BrowserTaskConsumerRow)
                    .where(
                        BrowserTaskConsumerRow.acquisition_id == a.id,
                        BrowserTaskConsumerRow.state == "active",
                    )
                    .order_by(BrowserTaskConsumerRow.created_at, BrowserTaskConsumerRow.id)
                    .limit(1)
                )
                if target is None:
                    return await self._projection(s, c, a)
            assert target is not None
            target.state = "cancelled"
            target.is_primary = False
            target.cancelled_at = now
            target.updated_at = now
            if a.state in {"queued", "claimed"}:
                a.reference_count = max(0, a.reference_count - 1)
            active = await s.scalar(
                select(func.count(BrowserTaskConsumerRow.id)).where(
                    BrowserTaskConsumerRow.acquisition_id == a.id,
                    BrowserTaskConsumerRow.state == "active",
                )
            )
            if not active:
                if a.state in {"queued", "claimed"}:
                    a.state = "cancelled"
                a.lease_generation += 1
                a.lease_owner = None
                a.claim_token_sha256 = None
                a.lease_expires_at = None
                a.active_singleflight_key = None
                a.public_task_id = None
                a.claim_consumer_id = None
                a.reference_count = 0
                if not BrowserTaskState(a.state).terminal:
                    a.terminal_at = now
            else:
                promoted = await s.scalar(
                    select(BrowserTaskConsumerRow)
                    .where(
                        BrowserTaskConsumerRow.acquisition_id == a.id,
                        BrowserTaskConsumerRow.state == "active",
                    )
                    .order_by(BrowserTaskConsumerRow.created_at, BrowserTaskConsumerRow.id)
                    .limit(1)
                )
                if promoted is not None:
                    promoted.is_primary = True
            await s.commit()
            return await self._projection(s, c, a)
