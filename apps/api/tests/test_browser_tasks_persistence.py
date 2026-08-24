from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tripchord.persistence.browser_tasks import DurableBrowserTaskConflict, DurableBrowserTaskStore
from tripchord.persistence.database import Database
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserQuote,
    BrowserSearchQuery,
    BrowserTaskCompletion,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    QuotePriceBasis,
)


def _terminal_quote() -> BrowserQuote:
    return BrowserQuote(
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.LODGING,
        page_url="https://hotels.ctrip.com/hotels/list",
        captured_at=datetime.now(UTC),
        parser_version="tripchord-visible-dom-v3",
        visible_evidence="{}",
        evidence_sha256="a" * 64,
        currency="CNY",
        amount=Decimal("100"),
        price_basis=QuotePriceBasis.PER_NIGHT,
        taxes_included=True,
        title="Terminal reuse fixture",
        details={
            "query": {
                "origin": None,
                "destination": "Male",
                "start_date": "2026-08-23",
                "end_date": "2026-08-27",
                "adults": 2,
                "rooms": 1,
                "currency": "CNY",
                "origin_code": None,
                "destination_code": "MLE",
                "search_url": None,
            },
            "driver": {
                "mode": "fixture",
                "triggered": True,
                "confirmed_query": {"destination": "Male"},
                "confirmation_scope": "fixture",
            },
            "price_text": "¥100",
            "visible_terms": ["含税"],
            "extraction": "visible_dom",
            "destination": "Male",
            "check_in": "2026-08-23",
            "check_out": "2026-08-27",
            "adults": 2,
            "rooms": 1,
            "room_text": "Standard",
            "area_text": "Hulhumale",
            "breakfast_text": "Included",
            "cancellation_text": "Free cancellation",
            "transfer_text": "Airport transfer",
        },
    )


def _submission(*, allow_reuse: bool = True, max_attempts: int = 2) -> BrowserTaskSubmission:
    return BrowserTaskSubmission(
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.LODGING,
        max_attempts=max_attempts,
        reuse_partition_sha256="a" * 64,
        query=BrowserSearchQuery(
            destination="Male",
            destination_code="MLE",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 27),
            adults=2,
            rooms=1,
            options=(
                {"__tripchord_allow_recent_quote_reuse": True}
                if allow_reuse
                else {}
            ),
        ),
    )


@pytest.mark.asyncio
async def test_acquisition_consumers_share_and_fence() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="a" * 64)
        first = await store.submit_consumer(
            _submission(), consumer_id="consumer-a", tenant_id="tenant-a", tenant_partition="user-a"
        )
        second = await store.submit_consumer(
            _submission(), consumer_id="consumer-b", tenant_id="tenant-a", tenant_partition="user-a"
        )
        assert first.acquisition_id == second.acquisition_id
        projection = await store.get_consumer("consumer-b", tenant_id="tenant-a")
        assert projection is not None
        assert projection.acquisition_state == BrowserTaskState.QUEUED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_active_singleflight_requires_explicit_reuse_opt_in() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="0" * 64)
        first = await store.submit_consumer(
            _submission(allow_reuse=False),
            consumer_id="no-opt-in-a",
            tenant_id="tenant",
            tenant_partition="user",
        )
        second = await store.submit_consumer(
            _submission(allow_reuse=False),
            consumer_id="no-opt-in-b",
            tenant_id="tenant",
            tenant_partition="user",
        )
        assert first.acquisition_id != second.acquisition_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_formal_active_sharing_has_one_public_primary_but_stable_snapshots() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="c" * 64)
        capability = {
            "capability_id": "cap-1",
            "terminal_job_id": "job-a",
            "request_sha256": "a" * 64,
            "run_id": "run-a",
            "attempt_digest": "d" * 64,
        }
        first = await store.submit_consumer(
            _submission(),
            consumer_id="formal-a",
            tenant_id="tenant",
            tenant_partition="user",
            capability=capability,
            job_id="job-a",
            request_sha256="a" * 64,
            run_id="run-a",
            run_revision=1,
        )
        second = await store.submit_consumer(
            _submission(),
            consumer_id="formal-b",
            tenant_id="tenant",
            tenant_partition="user",
            capability=capability,
            job_id="job-a",
            request_sha256="a" * 64,
            run_id="run-a",
            run_revision=1,
        )
        assert first.consumer_id == second.consumer_id == "formal-a"
        assert first.snapshot.id == "formal-a"
        assert second.snapshot.id == "formal-a"
        assert first.acquisition_id == second.acquisition_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_force_fresh_and_no_reuse_never_attach_to_active_acquisition() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="d" * 64)
        first = await store.submit_consumer(
            _submission(), consumer_id="normal-a", tenant_id="tenant", tenant_partition="user"
        )
        force = await store.submit_consumer(
            _submission(),
            consumer_id="force-b",
            tenant_id="tenant",
            tenant_partition="user",
            force_fresh=True,
        )
        no_reuse = await store.submit_consumer(
            _submission(),
            consumer_id="fresh-c",
            tenant_id="tenant",
            tenant_partition="user",
            allow_recent_quote_reuse=False,
        )
        assert force.acquisition_id != first.acquisition_id
        assert no_reuse.acquisition_id != first.acquisition_id
        assert no_reuse.acquisition_id != force.acquisition_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_consumer_id_replay_checks_all_lineage_fields() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="e" * 64)
        kwargs = dict(
            tenant_id="tenant",
            tenant_partition="user",
            job_id="job",
            request_sha256="a" * 64,
            run_id="run",
            run_revision=7,
            capability={
                "capability_id": "cap",
                "terminal_job_id": "job",
                "request_sha256": "a" * 64,
                "run_id": "run",
                "attempt_digest": "d" * 64,
            },
        )
        await store.submit_consumer(_submission(), consumer_id="same", **kwargs)
        with pytest.raises(DurableBrowserTaskConflict):
            await store.submit_consumer(
                _submission(), consumer_id="same", **{**kwargs, "run_revision": 8}
            )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_session_identity_fences_old_runtime_and_renews_claims() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="f" * 64)
        await store.submit_consumer(
            _submission(), consumer_id="claim", tenant_id="tenant", tenant_partition="user"
        )
        expires = datetime.now(UTC) + timedelta(minutes=2)
        await store.upsert_companion_session(
            session_id="session",
            companion_id="companion",
            runtime_instance_id="runtime-a",
            build_identity={"build": "a"},
            providers=["ctrip"],
            scopes=["ctrip:lodging"],
            expires_at=expires,
        )
        lease = (await store.claim_acquisitions(
            owner="owner",
            session_id="session",
            session_generation=1,
            runtime_instance_id="runtime-a",
            build_identity={"build": "a"},
        ))[0]
        assert await store.renew_session_leases(
            session_id="session",
            session_generation=1,
            runtime_instance_id="runtime-a",
            build_identity={"build": "a"},
        ) == 1
        await store.upsert_companion_session(
            session_id="session",
            companion_id="companion",
            runtime_instance_id="runtime-b",
            build_identity={"build": "b"},
            providers=["ctrip"],
            scopes=["ctrip:lodging"],
            expires_at=expires,
        )
        assert not await store.heartbeat_acquisition(
            lease.acquisition_id,
            owner="owner",
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id="session",
            session_generation=1,
            runtime_instance_id="runtime-a",
            build_identity={"build": "a"},
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_wait_housekeeps_expired_max_attempts_without_a_new_claimer() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="1" * 64)
        submitted = await store.submit_consumer(
            _submission(max_attempts=1),
            consumer_id="wait-expired",
            tenant_id="tenant",
            tenant_partition="user",
        )
        await store.upsert_companion_session(
            session_id="wait-session",
            companion_id="companion",
            runtime_instance_id=None,
            build_identity=None,
            providers=["ctrip"],
            scopes=["ctrip:lodging"],
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        await store.claim_acquisitions(
            owner="wait-owner",
            session_id="wait-session",
            session_generation=1,
            lease_seconds=1,
        )
        await asyncio.sleep(1.1)
        terminal = await store.wait_consumer(
            submitted.consumer_id, tenant_id="tenant", timeout_seconds=0.2
        )
        assert terminal is not None
        assert terminal.acquisition_state == BrowserTaskState.FAILED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_wait_never_returns_a_nonterminal_snapshot_at_its_deadline() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="9" * 64)
        submitted = await store.submit_consumer(
            _submission(),
            consumer_id="wait-nonterminal",
            tenant_id="tenant",
            tenant_partition="user",
        )

        with pytest.raises(TimeoutError, match="terminal state"):
            await store.wait_consumer(
                submitted.consumer_id,
                tenant_id="tenant",
                timeout_seconds=0.01,
            )

        current = await store.get_consumer(submitted.consumer_id, tenant_id="tenant")
        assert current is not None
        assert current.acquisition_state == BrowserTaskState.QUEUED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_completion_outbox_rehydrates_after_lease_expiry_and_is_idempotent() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="2" * 64)
        submitted = await store.submit_consumer(
            _submission(), consumer_id="outbox-task", tenant_id="tenant", tenant_partition="user"
        )
        await store.upsert_companion_session(
            session_id="outbox-session",
            companion_id="companion",
            runtime_instance_id="runtime",
            build_identity={"build": "a"},
            providers=["ctrip"],
            scopes=["ctrip:lodging"],
            expires_at=datetime.now(UTC) + timedelta(minutes=2),
        )
        lease = (
            await store.claim_acquisitions(
                owner="companion",
                session_id="outbox-session",
                session_generation=1,
                runtime_instance_id="runtime",
                build_identity={"build": "a"},
                lease_seconds=1,
            )
        )[0]
        completion = BrowserTaskCompletion(
            state=BrowserTaskState.FAILED,
            failure=BrowserFailure(
                code=BrowserFailureCode.TIMEOUT,
                message="outbox",
                retryable=True,
                captured_at=datetime.now(UTC),
            ),
        )
        now = datetime.now(UTC)
        frozen = submitted.snapshot.model_copy(
            update={
                "state": BrowserTaskState.FAILED,
                "updated_at": now,
                "failure": completion.failure,
            }
        )
        digest = await store.prepare_acquisition_completion(
            lease.acquisition_id,
            tenant_id="tenant",
            owner=lease.owner,
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id=lease.session_id,
            session_generation=lease.session_generation,
            completion=completion,
            completion_snapshot=frozen,
            event_details={"frozen_at": now.isoformat(), "result_sha256": "x" * 64},
            runtime_instance_id=lease.runtime_instance_id,
            build_identity=lease.build_identity,
        )
        await asyncio.sleep(1.1)
        recovered = await store.get_claim_lease(
            submitted.consumer_id, tenant_id="tenant", claim_token=lease.claim_token
        )
        assert recovered is not None
        pending = await store.get_pending_completion(
            lease.acquisition_id, tenant_id="tenant"
        )
        assert pending is not None and pending[1].state == BrowserTaskState.FAILED
        published = await store.finalize_acquisition_completion(
            lease.acquisition_id, tenant_id="tenant", completion_sha256=digest
        )
        repeated = await store.finalize_acquisition_completion(
            lease.acquisition_id, tenant_id="tenant", completion_sha256=digest
        )
        assert published == repeated and repeated.state == BrowserTaskState.FAILED
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_terminal_reuse_returns_new_public_consumer_id() -> None:
    """A fresh ordinary consumer must not inherit the old terminal handle."""
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    try:
        store = DurableBrowserTaskStore(database, authority_partition_sha256="7" * 64)
        first = await store.submit_consumer(
            _submission(),
            consumer_id="terminal-old",
            tenant_id="tenant",
            tenant_partition="user",
        )
        await store.upsert_companion_session(
            session_id="terminal-session",
            companion_id="terminal-companion",
            runtime_instance_id="terminal-runtime",
            build_identity={"build": "terminal"},
            providers=[provider.value for provider in BrowserProvider],
            scopes=[],
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        lease = (
            await store.claim_acquisitions(
                owner="terminal-owner",
                session_id="terminal-session",
                session_generation=1,
                runtime_instance_id="terminal-runtime",
                build_identity={"build": "terminal"},
            )
        )[0]
        completion = BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(_terminal_quote(),),
        )
        await store.complete_acquisition(
            lease.acquisition_id,
            tenant_id="tenant",
            owner=lease.owner,
            generation=lease.generation,
            claim_token=lease.claim_token,
            session_id=lease.session_id,
            session_generation=lease.session_generation,
            completion=completion,
            runtime_instance_id=lease.runtime_instance_id,
            build_identity=lease.build_identity,
        )
        second = await store.submit_consumer(
            _submission(),
            consumer_id="terminal-new",
            tenant_id="tenant",
            tenant_partition="user",
        )
        assert second.snapshot.id == "terminal-new"
        assert second.snapshot.id != first.snapshot.id
        assert second.snapshot.reused_from_task_id == "terminal-old"
    finally:
        await database.dispose()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_postgres_formal_partition_and_concurrent_singleflight() -> None:
    url = os.environ.get("TRIPCHORD_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("TRIPCHORD_POSTGRES_TEST_URL is not configured")
    database = Database(url)
    await database.create_schema()
    try:
        a = DurableBrowserTaskStore(database, authority_partition_sha256="b" * 64)
        submission = _submission()
        same_capability = {"job_graph_sha256": "a" * 64, "attempt": 1}
        first, second = await asyncio.gather(
            a.submit_consumer(
                submission,
                consumer_id="pg-consumer-a",
                tenant_id="pg-browser-test",
                tenant_partition="pg-user",
                capability=same_capability,
            ),
            a.submit_consumer(
                submission,
                consumer_id="pg-consumer-b",
                tenant_id="pg-browser-test",
                tenant_partition="pg-user",
                capability=same_capability,
            ),
        )
        assert first.acquisition_id == second.acquisition_id
        other = await a.submit_consumer(
            submission,
            consumer_id="pg-consumer-c",
            tenant_id="pg-browser-test",
            tenant_partition="pg-user",
            capability={"job_graph_sha256": "b" * 64, "attempt": 1},
        )
        assert other.acquisition_id != first.acquisition_id
    finally:
        await database.dispose()
