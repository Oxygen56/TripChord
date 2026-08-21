"""PostgreSQL acceptance tests for durable Browser acquisition state."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from tripchord.persistence.browser_tasks import (
    DurableBrowserTaskConflict,
    DurableBrowserTaskStore,
)
from tripchord.persistence.database import Database
from tripchord.persistence.models import (
    BrowserAcquisitionRow,
    BrowserTaskConsumerRow,
    CompanionSessionRow,
)
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskCompletion,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
)

POSTGRES_URL = os.environ.get("TRIPCHORD_POSTGRES_TEST_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TRIPCHORD_POSTGRES_TEST_URL is not configured"
    ),
]


def _partition(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def _submission(
    provider: BrowserProvider = BrowserProvider.CTRIP,
    kind: BrowserVertical = BrowserVertical.LODGING,
    *,
    max_attempts: int = 2,
    reuse_partition: str | None = None,
    force_fresh: bool = False,
) -> BrowserTaskSubmission:
    return BrowserTaskSubmission(
        provider=provider,
        kind=kind,
        max_attempts=max_attempts,
        reuse_partition_sha256=reuse_partition or _partition("pg-default"),
        query=BrowserSearchQuery(
            destination="Male",
            destination_code="MLE",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 27),
            adults=2,
            rooms=1,
            options={
                "__tripchord_allow_recent_quote_reuse": True,
                "__tripchord_force_fresh": force_fresh,
            },
        ),
    )


def _formal_capability(
    label: str, *, job_id: str, request_sha: str, run_id: str
) -> dict[str, object]:
    return {
        "schema_version": "tripchord-formal-live-source-execution-capability-v1",
        "capability_id": f"cap-{label}",
        "challenge_id": f"challenge-{label}",
        "run_id": run_id,
        "terminal_job_id": job_id,
        "request_sha256": request_sha,
        "job_graph_sha256": "a" * 64,
        "attempt_digest": hashlib.sha256(label.encode()).hexdigest(),
    }


async def _session(
    store: DurableBrowserTaskStore,
    session_id: str,
    *,
    runtime: str | None = None,
    build: dict[str, object] | None = None,
    companion: str | None = None,
    expires_seconds: int = 600,
    providers: list[str] | None = None,
    scopes: list[str] | None = None,
) -> CompanionSessionRow:
    return await store.upsert_companion_session(
        session_id=session_id,
        companion_id=companion or f"companion-{session_id}",
        runtime_instance_id=runtime,
        build_identity=build,
        providers=providers or [provider.value for provider in BrowserProvider],
        scopes=scopes or [],
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_seconds),
    )


def _failed_completion() -> BrowserTaskCompletion:
    return BrowserTaskCompletion(
        state=BrowserTaskState.FAILED,
        failure=BrowserFailure(
            code=BrowserFailureCode.TIMEOUT,
            message="test completion",
            retryable=True,
            captured_at=datetime.now(UTC),
        ),
    )


async def _new_stores() -> tuple[
    Database, Database, DurableBrowserTaskStore, DurableBrowserTaskStore
]:
    assert POSTGRES_URL is not None
    database_a = Database(POSTGRES_URL)
    database_b = Database(POSTGRES_URL)
    async with database_a.sessions() as session:
        await session.execute(delete(BrowserTaskConsumerRow))
        await session.execute(delete(BrowserAcquisitionRow))
        await session.execute(delete(CompanionSessionRow))
        await session.commit()
    authority = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return (
        database_a,
        database_b,
        DurableBrowserTaskStore(database_a, authority_partition_sha256=authority),
        DurableBrowserTaskStore(database_b, authority_partition_sha256=authority),
    )


@pytest.mark.asyncio
async def test_postgres_singleflight_formal_scope_and_force_fresh() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        submission = _submission(reuse_partition=_partition("tenant"))
        request_sha = "b" * 64
        capability = _formal_capability(
            "same", job_id="job", request_sha=request_sha, run_id="run"
        )
        first, second = await asyncio.gather(
            store_a.submit_consumer(
                submission,
                consumer_id="formal-a",
                tenant_id="tenant",
                tenant_partition="user",
                capability=capability,
                job_id="job",
                request_sha256=request_sha,
                run_id="run",
                run_revision=1,
            ),
            store_b.submit_consumer(
                submission,
                consumer_id="formal-b",
                tenant_id="tenant",
                tenant_partition="user",
                capability=capability,
                job_id="job",
                request_sha256=request_sha,
                run_id="run",
                run_revision=1,
            ),
        )
        assert first.acquisition_id == second.acquisition_id
        assert first.snapshot.id == second.snapshot.id == first.consumer_id
        other = await store_b.submit_consumer(
            submission,
            consumer_id="formal-other",
            tenant_id="tenant",
            tenant_partition="user",
            capability=_formal_capability(
                "other", job_id="job", request_sha=request_sha, run_id="run"
            ),
            job_id="job",
            request_sha256=request_sha,
            run_id="run",
            run_revision=1,
        )
        assert other.acquisition_id != first.acquisition_id
        force = await store_a.submit_consumer(
            submission,
            consumer_id="force-fresh",
            tenant_id="tenant",
            tenant_partition="user",
            force_fresh=True,
        )
        assert force.acquisition_id not in {first.acquisition_id, other.acquisition_id}
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_companion_session_first_upsert_is_atomic_and_authority_scoped() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    other_authority = hashlib.sha256(b"other-authority").hexdigest()
    other_a = DurableBrowserTaskStore(
        database_a, authority_partition_sha256=other_authority
    )
    other_b = DurableBrowserTaskStore(
        database_b, authority_partition_sha256=other_authority
    )
    try:
        # Two independent engines race the first creation in one authority;
        # ON CONFLICT DO NOTHING followed by a row lock must converge to one
        # row/generation instead of leaking an IntegrityError.
        same_a, same_b = await asyncio.gather(
            _session(store_a, "same-session"),
            _session(store_b, "same-session"),
        )
        # The same client session id is valid in a separate authority and
        # must not collide with the first authority's primary key.
        other_session_a, other_session_b = await asyncio.gather(
            _session(other_a, "same-session"),
            _session(other_b, "same-session"),
        )
        assert same_a.session_generation == same_b.session_generation == 1
        assert other_session_a.session_generation == other_session_b.session_generation == 1
        async with database_a.sessions() as session:
            same_count = await session.scalar(
                select(func.count(CompanionSessionRow.id)).where(
                    CompanionSessionRow.id == "same-session",
                    CompanionSessionRow.authority_partition_sha256 == store_a._authority_partition,
                )
            )
            other_count = await session.scalar(
                select(func.count(CompanionSessionRow.id)).where(
                    CompanionSessionRow.id == "same-session",
                    CompanionSessionRow.authority_partition_sha256 == other_authority,
                )
            )
        assert same_count == other_count == 1
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_skip_locked_claim_and_cross_instance_completion() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        submitted = await store_a.submit_consumer(
            _submission(),
            consumer_id="cross-instance",
            tenant_id="tenant",
            tenant_partition="user",
        )
        await _session(store_a, "session-a")
        await _session(store_b, "session-b")
        leases_a, leases_b = await asyncio.gather(
            store_a.claim_acquisitions(
                owner="owner-a", session_id="session-a", session_generation=1
            ),
            store_b.claim_acquisitions(
                owner="owner-b", session_id="session-b", session_generation=1
            ),
        )
        assert len(leases_a) + len(leases_b) == 1
        lease = (leases_a or leases_b)[0]
        completed = await store_b.complete_acquisition(
            lease.acquisition_id,
            tenant_id="tenant",
            owner=lease.owner,
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id=lease.session_id,
            session_generation=lease.session_generation,
            completion=_failed_completion(),
            runtime_instance_id=lease.runtime_instance_id,
            build_identity=lease.build_identity,
        )
        assert completed.state == BrowserTaskState.FAILED
        waited = await store_a.wait_consumer(submitted.consumer_id, tenant_id="tenant")
        assert waited is not None and waited.acquisition_state == BrowserTaskState.FAILED
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_session_renewal_takeover_and_runtime_fence() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        submitted = await store_a.submit_consumer(
            _submission(), consumer_id="lease", tenant_id="tenant", tenant_partition="user"
        )
        await _session(store_a, "session-a", runtime="runtime-a", build={"build": "a"})
        lease = (
            await store_a.claim_acquisitions(
                owner="owner-a",
                session_id="session-a",
                session_generation=1,
                runtime_instance_id="runtime-a",
                build_identity={"build": "a"},
                lease_seconds=1,
            )
        )[0]
        assert await store_b.renew_session_leases(
            session_id="session-a",
            session_generation=1,
            runtime_instance_id="runtime-a",
            build_identity={"build": "a"},
            lease_seconds=1,
        ) == 1
        await asyncio.sleep(1.1)
        await _session(store_b, "session-b", runtime="runtime-b", build={"build": "b"})
        takeover = (
            await store_b.claim_acquisitions(
                owner="owner-b",
                session_id="session-b",
                session_generation=1,
                runtime_instance_id="runtime-b",
                build_identity={"build": "b"},
                lease_seconds=2,
            )
        )[0]
        assert takeover.generation == lease.generation + 1
        assert not await store_a.heartbeat_acquisition(
            lease.acquisition_id,
            owner=lease.owner,
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id=lease.session_id,
            session_generation=lease.session_generation,
            runtime_instance_id=lease.runtime_instance_id,
            build_identity=lease.build_identity,
        )
        with pytest.raises(DurableBrowserTaskConflict):
            await store_a.complete_acquisition(
                lease.acquisition_id,
                tenant_id="tenant",
                owner=lease.owner,
                generation=lease.generation,
                claim_token=lease.claim_token,
                session_id=lease.session_id,
                session_generation=lease.session_generation,
                completion=_failed_completion(),
                runtime_instance_id=lease.runtime_instance_id,
                build_identity=lease.build_identity,
            )
        await store_b.complete_acquisition(
            takeover.acquisition_id,
            tenant_id="tenant",
            owner=takeover.owner,
            generation=takeover.generation,
            claim_token=takeover.claim_token,
            session_id=takeover.session_id,
            session_generation=takeover.session_generation,
            completion=_failed_completion(),
            runtime_instance_id=takeover.runtime_instance_id,
            build_identity=takeover.build_identity,
        )
        final = await store_a.get_consumer(submitted.consumer_id, tenant_id="tenant")
        assert final is not None and final.snapshot.state == BrowserTaskState.FAILED
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_tenant_provider_scope_and_qunar_serialization() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        first = await store_a.submit_consumer(
            _submission(), consumer_id="tenant-a", tenant_id="tenant-a", tenant_partition="user"
        )
        second = await store_b.submit_consumer(
            _submission(), consumer_id="tenant-b", tenant_id="tenant-b", tenant_partition="user"
        )
        assert first.acquisition_id != second.acquisition_id
        # Provider/scope filtering must happen before LIMIT; otherwise a
        # Qunar-only worker can starve behind six earlier Ctrip rows forever.
        for index in range(6):
            await store_a.submit_consumer(
                _submission(),
                consumer_id=f"ctrip-{index}",
                tenant_id=f"ctrip-tenant-{index}",
                tenant_partition="user",
                force_fresh=True,
            )
        for index in range(2):
            await store_a.submit_consumer(
                _submission(BrowserProvider.QUNAR),
                consumer_id=f"qunar-{index}",
                tenant_id=f"qunar-tenant-{index}",
                tenant_partition="user",
                force_fresh=True,
            )
        await _session(
            store_a,
            "scope-session",
            providers=[BrowserProvider.QUNAR.value],
            scopes=[f"{BrowserProvider.QUNAR.value}:{BrowserVertical.LODGING.value}"],
        )
        await _session(
            store_b,
            "scope-session-b",
            providers=[BrowserProvider.QUNAR.value],
            scopes=[f"{BrowserProvider.QUNAR.value}:{BrowserVertical.LODGING.value}"],
        )
        qunar_a, qunar_b = await asyncio.gather(
            store_a.claim_acquisitions(
                owner="qunar-owner-a",
                session_id="scope-session",
                session_generation=1,
                limit=6,
            ),
            store_b.claim_acquisitions(
                owner="qunar-owner-b",
                session_id="scope-session-b",
                session_generation=1,
                limit=6,
            ),
        )
        qunar_leases = (*qunar_a, *qunar_b)
        assert len(qunar_leases) == 1
        assert sum(
            lease.submission.provider == BrowserProvider.QUNAR
            and lease.submission.kind == BrowserVertical.LODGING
            for lease in qunar_leases
        ) <= 1
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_claim_provider_rotation_survives_fifo_prefix() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        for index in range(4):
            await store_a.submit_consumer(
                _submission(
                    provider=BrowserProvider.CTRIP,
                    reuse_partition=_partition(f"ctrip-{index}"),
                ),
                consumer_id=f"rotation-ctrip-{index}",
                tenant_id="tenant",
                tenant_partition="user",
            )
        await store_a.submit_consumer(
            _submission(
                provider=BrowserProvider.QUNAR,
                reuse_partition=_partition("qunar-late"),
            ),
            consumer_id="rotation-qunar",
            tenant_id="tenant",
            tenant_partition="user",
        )
        await _session(
            store_b,
            "rotation-session",
            providers=[BrowserProvider.CTRIP.value, BrowserProvider.QUNAR.value],
            scopes=["ctrip:lodging", "qunar:lodging"],
        )
        leases = await store_b.claim_acquisitions(
            owner="rotation-owner",
            session_id="rotation-session",
            session_generation=1,
            limit=2,
        )
        assert {lease.submission.provider for lease in leases} == {
            BrowserProvider.CTRIP,
            BrowserProvider.QUNAR,
        }
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_shared_cancel_consumes_references_without_fencing_early() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        first = await store_a.submit_consumer(
            _submission(), consumer_id="cancel-a", tenant_id="tenant", tenant_partition="user"
        )
        second = await store_b.submit_consumer(
            _submission(), consumer_id="cancel-b", tenant_id="tenant", tenant_partition="user"
        )
        assert first.snapshot.id == second.snapshot.id == "cancel-a"
        await store_a.cancel_consumer(first.snapshot.id, tenant_id="tenant")
        remaining = await store_b.get_consumer("cancel-b", tenant_id="tenant")
        assert remaining is not None and remaining.acquisition_state != BrowserTaskState.CANCELLED
        await store_b.cancel_consumer(first.snapshot.id, tenant_id="tenant")
        final = await store_a.get_consumer("cancel-b", tenant_id="tenant")
        assert final is not None and final.acquisition_state == BrowserTaskState.CANCELLED
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_public_task_id_survives_primary_cancel_and_completion() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        first = await store_a.submit_consumer(
            _submission(),
            consumer_id="public-a",
            tenant_id="tenant",
            tenant_partition="user",
        )
        second = await store_b.submit_consumer(
            _submission(),
            consumer_id="public-b",
            tenant_id="tenant",
            tenant_partition="user",
        )
        assert first.snapshot.id == second.snapshot.id == "public-a"
        await store_a.cancel_consumer("public-a", tenant_id="tenant")
        await _session(store_b, "public-session")
        (lease,) = await store_b.claim_acquisitions(
            owner="public-owner", session_id="public-session", session_generation=1
        )
        assert lease.public_task_id == "public-a"
        completed = await store_a.complete_acquisition(
            lease.acquisition_id,
            tenant_id="tenant",
            owner=lease.owner,
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id=lease.session_id,
            session_generation=lease.session_generation,
            completion=_failed_completion(),
        )
        assert completed.id == "public-a"
        remaining = await store_b.get_consumer("public-b", tenant_id="tenant")
        assert remaining is not None and remaining.snapshot.id == "public-a"
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.asyncio
async def test_postgres_durable_bridge_cross_instance_submit_claim_complete_wait() -> None:
    database_a, database_b, store_a, store_b = await _new_stores()
    try:
        bridge_a = BrowserTaskBridge(
            durable_store=store_a,
            durable_tenant_id="bridge-tenant",
            now=lambda: datetime.now(UTC),
        )
        bridge_b = BrowserTaskBridge(
            durable_store=store_b,
            durable_tenant_id="bridge-tenant",
            now=lambda: datetime.now(UTC),
        )
        submitted = (await bridge_a.submit_many((_submission(),)))[0]
        await bridge_b.heartbeat(
            "companion-b", providers=(BrowserProvider.CTRIP,)
        )
        (lease,) = await bridge_b.claim(
            "companion-b", providers=(BrowserProvider.CTRIP,)
        )
        assert lease.task_id == submitted.id
        await bridge_b.complete(lease.task_id, lease.claim_token, _failed_completion())
        (waited,) = await bridge_a.wait_many((submitted.id,), timeout_seconds=2)
        assert waited.id == submitted.id
        assert waited.state == BrowserTaskState.FAILED

        # A claim can be completed by another API instance using only the
        # public task id and token; no process-local lease map is required.
        second = (await bridge_a.submit_many((_submission(force_fresh=True),)))[0]
        await bridge_a.heartbeat(
            "companion-a", providers=(BrowserProvider.CTRIP,)
        )
        (second_lease,) = await bridge_a.claim(
            "companion-a", providers=(BrowserProvider.CTRIP,)
        )
        completed = await bridge_b.complete(
            second_lease.task_id,
            second_lease.claim_token,
            _failed_completion(),
        )
        assert completed.id == second.id and completed.state == BrowserTaskState.FAILED
    finally:
        await database_a.dispose()
        await database_b.dispose()
