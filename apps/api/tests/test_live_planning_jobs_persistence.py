import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tripchord.agents.live_jobs import (
    LiveJobWorkerCommand,
    LivePlanningJobCancellationPendingError,
    LivePlanningJobInactiveError,
    LivePlanningJobRegistry,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
)
from tripchord.agents.live_system import LiveCoverageMode
from tripchord.persistence.database import Database
from tripchord.persistence.live_planning_jobs import (
    DurableLivePlanningJobConflict,
    DurableLivePlanningJobStore,
)
from tripchord.persistence.live_planning_jobs import (
    DurableLivePlanningJobRepository as JobRepository,
)

REQUEST_SHA = "a" * 64


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def recovery_fixture_entry(**_kwargs: object) -> dict[str, str]:
    return {"status": "recovered"}


def snapshot(job_id: str = "live-1") -> LivePlanningJobSnapshot:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    return LivePlanningJobSnapshot(
        id=job_id,
        state=LivePlanningJobState.QUEUED,
        stage="queued",
        progress=0,
        revision=1,
        request_sha256=REQUEST_SHA,
        model_trace_scope_sha256=REQUEST_SHA,
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_live_job_repository_idempotency_claim_restart_and_terminal_guard() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    initial = snapshot()
    async with database.sessions() as session:
        repository = JobRepository(session)
        created = await repository.create_or_get(
            idempotency_key="request-1",
            request_sha256=REQUEST_SHA,
            snapshot=initial,
        )
        repeated = await repository.create_or_get(
            idempotency_key="request-1",
            request_sha256=REQUEST_SHA,
            snapshot=initial,
        )
        assert repeated.id == created.id
        lease = await repository.claim_with_identity(created.id)
        assert lease is not None
        claimed = lease.snapshot
        assert claimed.state == LivePlanningJobState.RUNNING
        assert await repository.claim(created.id) is None
        with pytest.raises(DurableLivePlanningJobConflict, match="stale live job lease"):
            await repository.replace_snapshot(
                created.id,
                claimed.model_copy(update={"revision": claimed.revision + 1}),
                owner="old-worker",
                lease_generation=0,
            )
        with pytest.raises(DurableLivePlanningJobConflict, match="stale or cancelled"):
            await repository.settle(
                created.id,
                state=LivePlanningJobState.SUCCEEDED,
                result={"late": True},
                owner="old-worker",
                lease_generation=0,
            )
        checkpoint = LivePlanningPairCheckpoint.create(
            sequence=1,
            request_sha256=REQUEST_SHA,
            date_pair_id="pair-1",
            departure_date=date(2026, 8, 21),
            return_date=date(2026, 8, 24),
            state=LivePlanningPairCheckpointState.FAILED,
            query_task_ids=("query-1",),
            failure_class="TestFailure",
            captured_at=datetime.now(UTC),
        )
        with pytest.raises(DurableLivePlanningJobConflict, match="stale or cancelled"):
            await repository.append_checkpoint(
                created.id,
                checkpoint,
                owner="old-worker",
                lease_generation=0,
            )
        await repository.cancel(created.id)
        late = await repository.settle(
            created.id,
            state=LivePlanningJobState.SUCCEEDED,
            result={"late": True},
            owner=lease.owner,
            lease_generation=lease.generation,
        )
        assert late.state == LivePlanningJobState.CANCELLED

    # A new repository/session reads the same authoritative row after the
    # original control object has gone away; no process-local registry is used.
    async with database.sessions() as session:
        restored = await JobRepository(session).get(initial.id)
        assert restored is not None
        assert restored.state == LivePlanningJobState.CANCELLED
    await database.dispose()


@pytest.mark.asyncio
async def test_registry_production_entry_publishes_running_and_terminal_to_database() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)

    async def operation(report: object) -> dict[str, str]:
        await report("collecting", 20)  # type: ignore[operator]
        return {"status": "ok"}

    registry = LivePlanningJobRegistry(durable_store=store)
    created, reused = await registry.start_idempotent(
        tenant_id="anonymous",
        operation=operation,
        idempotency_key="registry-entry-1",
        request_digest=REQUEST_SHA,
    )
    assert not reused
    finished = None
    for _ in range(20):
        finished = await registry.get(created.id, "anonymous")
        if finished is not None and finished.state == LivePlanningJobState.SUCCEEDED:
            break
        await asyncio.sleep(0.01)
    assert finished is not None
    assert finished.state == LivePlanningJobState.SUCCEEDED
    restored = await LivePlanningJobRegistry(durable_store=store).get(
        created.id, "anonymous"
    )
    assert restored is not None
    assert restored.state == LivePlanningJobState.SUCCEEDED
    await registry.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_pair_result_is_fenced_and_same_digest_is_idempotent() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    now = datetime(2026, 8, 21, tzinfo=UTC)
    initial = snapshot("pair-job")
    async with database.sessions() as session:
        repository = JobRepository(session, "tenant-a")
        await repository.create_or_get(
            idempotency_key="pair-key", request_sha256=REQUEST_SHA, snapshot=initial
        )
        lease = await repository.claim_with_identity(initial.id)
        assert lease is not None
        checkpoint = LivePlanningPairCheckpoint.create(
            sequence=1,
            request_sha256=REQUEST_SHA,
            date_pair_id="pair-1",
            departure_date=now.date(),
            return_date=now.date() + timedelta(days=3),
            state=LivePlanningPairCheckpointState.FAILED,
            query_task_ids=("query-1",),
            failure_class="TestFailure",
            captured_at=now,
        )
        assert await repository.store_pair_result(
            initial.id,
            checkpoint=checkpoint,
            execution={"date_pair": "pair-1"},
            execution_sha256="b" * 64,
            owner=lease.owner,
            lease_generation=lease.generation,
        )
        assert await repository.store_pair_result(
            initial.id,
            checkpoint=checkpoint,
            execution={"date_pair": "pair-1"},
            execution_sha256="b" * 64,
            owner=lease.owner,
            lease_generation=lease.generation,
        )
        assert not await repository.store_pair_result(
            initial.id,
            checkpoint=checkpoint,
            execution={"late": True},
            execution_sha256="c" * 64,
            owner="old-worker",
            lease_generation=lease.generation,
        )
    await database.dispose()


@pytest.mark.asyncio
async def test_registry_recover_durable_rebuilds_allowlisted_worker_command(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recovery-command.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    command = LiveJobWorkerCommand(
        module_path=__file__,
        entry="recovery_fixture_entry",
        args={},
    )
    first = LivePlanningJobRegistry(durable_store=store, worker_module="")
    second = LivePlanningJobRegistry(durable_store=store, worker_module="")

    def resolver(spec: dict[str, object]) -> LiveJobWorkerCommand:
        assert spec["kind"] == "worker_command"
        assert spec["module_path"] == __file__
        return LiveJobWorkerCommand(
            module_path=str(spec["module_path"]),
            entry=str(spec["entry"]),
            args=dict(spec.get("args", {})),
        )

    try:
        created, replayed = await first.start_idempotent(
            tenant_id="tenant-recovery",
            operation=command,
            idempotency_key="recover-command",
            request_digest=REQUEST_SHA,
            defer_start=True,
        )
        assert not replayed

        recovered = await second.recover_durable(
            tenant_id="tenant-recovery", command_resolver=resolver
        )
        assert recovered == (created.id,)
        # The first registry never claimed the row.  Closing it while the
        # second registry owns the recovered worker must be a detach/no-op and
        # must not request cancellation or write a stale revision.
        await first.close()
        peer_view = await second.get(created.id, "tenant-recovery")
        assert peer_view is not None
        assert peer_view.cancel_pending is False
        # Await the actual recovered worker task rather than polling a second
        # registry snapshot.  This makes a stuck subprocess/terminal barrier
        # fail at the owning task, instead of looking like an unexplained
        # RUNNING record under a loaded CI event loop.
        recovered_runtime = second._records[created.id]
        assert recovered_runtime.task is not None
        await asyncio.wait_for(recovered_runtime.task, timeout=5)
        current = await second.get(created.id, "tenant-recovery")
        assert current is not None
        assert current.state == LivePlanningJobState.SUCCEEDED
        assert current.result == {"status": "recovered"}
    finally:
        # ``first`` still holds the pre-claim queued snapshot while ``second``
        # owns the authoritative terminal revision.  Suspend the prepared
        # registry without trying to persist a stale cancellation over it.
        await first.suspend_for_restart()
        await second.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_stale_registry_close_stops_local_executor_without_touching_new_lease() -> None:
    """A stale registry must stop only its own operation after lease handoff."""
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    first = LivePlanningJobRegistry(durable_store=store, cancel_wait_seconds=0.05)
    second = LivePlanningJobRegistry(durable_store=store, cancel_wait_seconds=0.05)
    started = asyncio.Event()
    effects: list[int] = []

    async def operation(_report: object) -> dict[str, str]:
        started.set()
        while True:
            effects.append(len(effects))
            await asyncio.sleep(0.005)

    try:
        created, replayed = await first.start_idempotent(
            tenant_id="tenant-stale",
            operation=operation,
            idempotency_key="stale-close",
            request_digest=REQUEST_SHA,
        )
        assert not replayed
        await asyncio.wait_for(started.wait(), timeout=3)
        runtime = first._records[created.id]
        assert runtime.durable_lease_owner is not None
        assert runtime.durable_lease_generation is not None
        if runtime.lease_heartbeat_task is not None:
            runtime.lease_heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.lease_heartbeat_task
            runtime.lease_heartbeat_task = None
        assert await store.release_lease(
            created.id,
            tenant_id="tenant-stale",
            owner=runtime.durable_lease_owner,
            lease_generation=runtime.durable_lease_generation,
        )
        replacement = await store.claim_with_identity(
            created.id, tenant_id="tenant-stale"
        )
        assert replacement is not None
        b_owner = replacement.owner
        b_generation = replacement.generation
        before = await store.get(created.id, tenant_id="tenant-stale")
        assert before is not None
        assert await store.lease_matches(
            created.id,
            tenant_id="tenant-stale",
            owner=b_owner,
            generation=b_generation,
        )
        await first.close()
        effects_at_return = len(effects)
        await asyncio.sleep(0.04)
        after = await store.get(created.id, tenant_id="tenant-stale")
        assert after == before
        assert len(effects) == effects_at_return
        assert await store.lease_matches(
            created.id,
            tenant_id="tenant-stale",
            owner=b_owner,
            generation=b_generation,
        )
        assert runtime.task is not None and runtime.task.done()
        assert runtime.operation_task is not None and runtime.operation_task.done()
        assert first._closed is True
    finally:
        with suppress(BaseException):
            await first.suspend_for_restart()
        with suppress(BaseException):
            await second.suspend_for_restart()
        await database.dispose()


@pytest.mark.asyncio
async def test_stubborn_stale_registry_close_fails_closed_until_executor_stops() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    first = LivePlanningJobRegistry(durable_store=store, cancel_wait_seconds=0.02)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def stubborn(_report: object) -> dict[str, str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.005)
        return {"status": "stopped"}

    try:
        created, _ = await first.start_idempotent(
            tenant_id="tenant-stubborn",
            operation=stubborn,
            idempotency_key="stubborn-stale-close",
            request_digest=REQUEST_SHA,
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        runtime = first._records[created.id]
        assert runtime.durable_lease_owner is not None
        assert runtime.durable_lease_generation is not None
        if runtime.lease_heartbeat_task is not None:
            runtime.lease_heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await runtime.lease_heartbeat_task
            runtime.lease_heartbeat_task = None
        assert await store.release_lease(
            created.id,
            tenant_id="tenant-stubborn",
            owner=runtime.durable_lease_owner,
            lease_generation=runtime.durable_lease_generation,
        )
        replacement = await store.claim_with_identity(
            created.id, tenant_id="tenant-stubborn"
        )
        assert replacement is not None
        b_owner = replacement.owner
        b_generation = replacement.generation
        before = await store.get(created.id, tenant_id="tenant-stubborn")
        assert before is not None
        assert await store.lease_matches(
            created.id,
            tenant_id="tenant-stubborn",
            owner=b_owner,
            generation=b_generation,
        )
        with pytest.raises(LivePlanningJobCancellationPendingError):
            await first.close()
        assert first._closed is False
        assert await store.get(created.id, tenant_id="tenant-stubborn") == before
        assert await store.lease_matches(
            created.id,
            tenant_id="tenant-stubborn",
            owner=b_owner,
            generation=b_generation,
        )
        stop.set()
        await first.close()
        assert first._closed is True
        assert await store.get(created.id, tenant_id="tenant-stubborn") == before
        assert await store.lease_matches(
            created.id,
            tenant_id="tenant-stubborn",
            owner=b_owner,
            generation=b_generation,
        )
        assert runtime.task is not None and runtime.task.done()
        assert runtime.operation_task is not None and runtime.operation_task.done()
    finally:
        stop.set()
        with suppress(BaseException):
            await first.suspend_for_restart()
        await database.dispose()


@pytest.mark.asyncio
async def test_peer_registry_cancel_requests_owner_drain(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'peer-cancel.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def operation(_report: object) -> dict[str, str]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise
        return {"ok": "unexpected"}

    owner = LivePlanningJobRegistry(durable_store=store)
    peer = LivePlanningJobRegistry(durable_store=store)
    created, _ = await owner.start_idempotent(
        tenant_id="tenant-peer-cancel",
        operation=operation,
        idempotency_key="peer-cancel-key",
        request_digest=REQUEST_SHA,
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    requested = await peer.cancel(created.id, "tenant-peer-cancel")
    assert requested is not None and requested.cancellation_requested is True
    await asyncio.wait_for(stopped.wait(), timeout=4)
    await asyncio.sleep(0.25)
    final = await store.get(created.id, tenant_id="tenant-peer-cancel")
    assert final is not None and final.state == LivePlanningJobState.CANCELLED
    await owner.close()
    await peer.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_durable_registry_ignores_legacy_json_state(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'durable.db'}")
    await database.create_schema()
    legacy = tmp_path / "legacy.json"
    legacy.write_text("not-json", encoding="utf-8")
    registry = LivePlanningJobRegistry(
        durable_store=DurableLivePlanningJobStore(database),
        state_path=legacy,
    )
    assert registry._records == {}
    assert registry._idempotency == {}
    await registry.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_forged_empty_death_proof_is_ignored(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'proof.db'}")
    await database.create_schema()
    registry = LivePlanningJobRegistry(
        durable_store=DurableLivePlanningJobStore(database),
        state_path=tmp_path / "markers.json",
    )
    workers_dir = registry._workers_dir()
    assert workers_dir is not None
    workers_dir.mkdir(parents=True, exist_ok=True)
    forged = workers_dir / (
        "live-job-00000000-0000-4000-8000-000000000001.death-proof.json"
    )
    forged.write_text("", encoding="utf-8")
    assert registry._load_death_proofs() == {}
    await registry.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_death_proof_is_content_bound_and_survives_marker_reap(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'proof-valid.db'}")
    await database.create_schema()
    registry = LivePlanningJobRegistry(
        durable_store=DurableLivePlanningJobStore(database),
        state_path=tmp_path / "markers.json",
    )
    job_id = "live-job-00000000-0000-4000-8000-000000000001"
    marker = "tripchord-marker-nonce"
    registry._write_death_proof(
        job_id,
        marker,
        12345,
        tenant_id="tenant-proof",
        lease_owner="worker-proof",
        lease_generation=1,
    )
    proof = registry._death_proof_file_for(job_id)
    assert proof is not None and proof.exists()
    assert set(registry._load_death_proofs()) == {job_id}
    registry._reap_stale_marker_files()
    assert proof.exists()
    proof.write_text("{}", encoding="utf-8")
    assert registry._load_death_proofs() == {}
    await registry.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_death_proof_tenant_owner_generation_tampering_is_rejected(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tamper.db'}")
    await database.create_schema()
    registry = LivePlanningJobRegistry(
        durable_store=DurableLivePlanningJobStore(database),
        state_path=tmp_path / "markers.json",
    )
    job_id = "live-job-00000000-0000-4000-8000-000000000005"
    registry._write_death_proof(
        job_id,
        "marker-tamper",
        12345,
        tenant_id="tenant-proof",
        lease_owner="worker-proof",
        lease_generation=1,
    )
    proof = registry._death_proof_file_for(job_id)
    assert proof is not None
    original = json.loads(proof.read_text(encoding="utf-8"))
    for field, value in (
        ("tenant_id", "tenant-other"),
        ("lease_owner", "worker-other"),
        ("lease_generation", 2),
    ):
        tampered = dict(original)
        tampered[field] = value
        proof.write_text(json.dumps(tampered), encoding="utf-8")
        assert registry._load_death_proofs() == {}
    await registry.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_old_generation_proof_cannot_consume_new_cancel_target(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'generation-proof.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    tenant_id = "tenant-generation-proof"
    job_id = "live-job-00000000-0000-4000-8000-000000000006"
    await store.create_or_get(
        tenant_id=tenant_id,
        idempotency_key="generation-proof",
        request_sha256=REQUEST_SHA,
        snapshot=snapshot(job_id),
        command_spec={},
    )
    lease = await store.claim_with_identity(job_id, tenant_id=tenant_id, lease_seconds=30)
    assert lease is not None
    await store.request_cancel(job_id, tenant_id=tenant_id)
    rejected_owner = await store.consume_orphan_death_proof(
        job_id,
        tenant_id=tenant_id,
        proof_owner="wrong-owner",
        proof_generation=lease.generation,
    )
    rejected_generation = await store.consume_orphan_death_proof(
        job_id,
        tenant_id=tenant_id,
        proof_owner=lease.owner,
        proof_generation=lease.generation + 1,
    )
    assert rejected_owner is None
    assert rejected_generation is None
    current = await store.get(job_id, tenant_id=tenant_id)
    assert current is not None and current.state != LivePlanningJobState.CANCELLED
    await database.dispose()


@pytest.mark.asyncio
async def test_unclaimed_queued_peer_cancel_is_terminal_without_proof(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'queued-cancel.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    tenant_id = "tenant-queued-cancel"
    job_id = "live-job-00000000-0000-4000-8000-000000000007"
    await store.create_or_get(
        tenant_id=tenant_id,
        idempotency_key="queued-cancel",
        request_sha256=REQUEST_SHA,
        snapshot=snapshot(job_id),
        command_spec={},
    )
    cancelled = await store.request_cancel(job_id, tenant_id=tenant_id)
    assert cancelled.state == LivePlanningJobState.CANCELLED
    assert cancelled.cancel_pending is False
    await database.dispose()


@pytest.mark.asyncio
async def test_peer_cancel_blocks_prepared_registry_activation(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'activate-cancel.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    calls = 0

    async def operation(_report: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    owner = LivePlanningJobRegistry(durable_store=store)
    peer = LivePlanningJobRegistry(durable_store=store)
    snapshot_a, _ = await owner.start_idempotent(
        tenant_id="tenant-activate-cancel",
        operation=operation,
        idempotency_key="activate-cancel",
        request_digest=REQUEST_SHA,
        defer_start=True,
    )
    cancelled = await peer.cancel(snapshot_a.id, "tenant-activate-cancel")
    assert cancelled is not None and cancelled.state == LivePlanningJobState.CANCELLED
    with pytest.raises(LivePlanningJobInactiveError):
        await owner.activate(snapshot_a.id, "tenant-activate-cancel")
    assert calls == 0
    await owner.close()
    await peer.close()
    await database.dispose()


@pytest.mark.asyncio
async def test_real_orphan_proof_retries_cancel_after_restart(tmp_path, monkeypatch) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'orphan.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    state_path = tmp_path / "markers.json"
    tenant_id = "tenant-orphan"
    job_id = "live-job-00000000-0000-4000-8000-000000000002"
    pending = snapshot(job_id).model_copy(
        update={"state": LivePlanningJobState.QUEUED, "stage": "queued"}
    )
    await store.create_or_get(
        tenant_id=tenant_id,
        idempotency_key="orphan-retry",
        request_sha256=REQUEST_SHA,
        snapshot=pending,
        command_spec={},
    )
    original_lease = await store.claim_with_identity(job_id, tenant_id=tenant_id, lease_seconds=1)
    assert original_lease is not None
    await store.request_cancel(job_id, tenant_id=tenant_id)

    marker = "tripchord-real-orphan-marker"
    workers_dir = tmp_path / ".markers.json.workers"
    workers_dir.mkdir(mode=0o700)
    orphan_src = (
        "import os,subprocess,sys,time; "
        "worker=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)',sys.argv[1],"
        "],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "print(worker.pid,flush=True); time.sleep(.2); os._exit(0)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", orphan_src, marker],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        child_pid = int(await asyncio.to_thread(child.stdout.readline))
        child.wait(timeout=2)
        pgid = os.getpgid(child_pid)
        marker_path = workers_dir / f"{job_id}.json"
        marker_path.write_text(
            json.dumps(
                {
                    "marker": marker,
                    "pgid": pgid,
                    "tenant_id": tenant_id,
                    "lease_owner": original_lease.owner,
                    "lease_generation": original_lease.generation,
                }
            ),
            encoding="utf-8",
        )
        first = LivePlanningJobRegistry(
            durable_store=store,
            state_path=state_path,
            hard_stop_confirm_seconds=1.0,
        )
        assert any(marker in line for line in first._group_commands(pgid))
        await first.restore_after_restart()
        assert not _group_alive(pgid)
        assert job_id in first._confirmed_stopped_job_ids
        proof = first._death_proof_file_for(job_id)
        assert proof is not None and proof.exists()

        original_consume = store.consume_orphan_death_proof
        calls = 0

        async def fail_once(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected first cancel persistence failure")
            return await original_consume(*args, **kwargs)

        monkeypatch.setattr(store, "consume_orphan_death_proof", fail_once)
        with pytest.raises(OSError, match="injected"):
            await first.recover_durable(
                tenant_id=tenant_id, command_resolver=lambda _: None  # type: ignore[arg-type]
            )
        assert proof.exists()
        await first.close()

        second = LivePlanningJobRegistry(
            durable_store=store,
            state_path=state_path,
            hard_stop_confirm_seconds=1.0,
        )
        await second.restore_after_restart()
        recovered = await second.recover_durable(
            tenant_id=tenant_id, command_resolver=lambda _: None  # type: ignore[arg-type]
        )
        assert recovered == ()
        final = await store.get(job_id, tenant_id=tenant_id)
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert not proof.exists()
        await second.close()

        third = LivePlanningJobRegistry(
            durable_store=store,
            state_path=state_path,
        )
        await third.restore_after_restart()
        assert await store.get(job_id, tenant_id=tenant_id) == final
        await third.close()
    finally:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
        with suppress(Exception):
            child.wait(timeout=2)
    await database.dispose()


@pytest.mark.asyncio
async def test_wrong_nonce_or_live_unverified_group_is_not_killed(tmp_path) -> None:
    registry = LivePlanningJobRegistry(state_path=tmp_path / "markers.json")
    workers_dir = registry._workers_dir()
    assert workers_dir is not None
    workers_dir.mkdir(mode=0o700)
    actual = "tripchord-actual-marker"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; print(sys.argv[1], flush=True); time.sleep(30)",
            actual,
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert child.stdout is not None
        await asyncio.to_thread(child.stdout.readline)
        pgid = os.getpgid(child.pid)
        marker_path = workers_dir / "live-job-00000000-0000-4000-8000-000000000003.json"
        marker_path.write_text(
            json.dumps({"marker": "wrong-nonce", "pgid": pgid}), encoding="utf-8"
        )
        await registry.restore_after_restart()
        assert child.poll() is None
        assert not registry._confirmed_stopped_job_ids
        assert not any(workers_dir.glob("*.death-proof.json"))
    finally:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        with suppress(Exception):
            child.wait(timeout=2)
        await registry.close()


@pytest.mark.asyncio
async def test_valid_durable_lease_prevents_peer_orphan_reap(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
    await database.create_schema()
    store = DurableLivePlanningJobStore(database)
    tenant_id = "tenant-live-lease"
    job_id = "live-job-00000000-0000-4000-8000-000000000004"
    await store.create_or_get(
        tenant_id=tenant_id,
        idempotency_key="live-lease",
        request_sha256=REQUEST_SHA,
        snapshot=snapshot(job_id),
        command_spec={},
    )
    lease = await store.claim_with_identity(job_id, tenant_id=tenant_id, lease_seconds=30)
    assert lease is not None
    state_path = tmp_path / "markers.json"
    workers_dir = tmp_path / ".markers.json.workers"
    workers_dir.mkdir(mode=0o700)
    marker = "tripchord-valid-lease-marker"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", marker],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pgid = os.getpgid(child.pid)
        (workers_dir / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "marker": marker,
                    "pgid": pgid,
                    "tenant_id": tenant_id,
                    "lease_owner": lease.owner,
                    "lease_generation": lease.generation,
                }
            ),
            encoding="utf-8",
        )
        peer = LivePlanningJobRegistry(
            durable_store=store,
            state_path=state_path,
            hard_stop_confirm_seconds=0.2,
        )
        await peer.restore_after_restart()
        assert child.poll() is None
        assert not any(workers_dir.glob("*.death-proof.json"))
        await peer.close()
    finally:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        with suppress(Exception):
            child.wait(timeout=2)
        await database.dispose()


@pytest.mark.asyncio
async def test_real_three_pair_recovery_runs_only_missing_pair() -> None:
    from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem

    from apps.api.tests.test_flexible_adaptive_scaling import _FullUniverseLiveRunner
    from apps.api.tests.test_flexible_live_system import REQUEST_SHA256 as RUN_SHA
    from apps.api.tests.test_flexible_live_system import window

    runner_a = _FullUniverseLiveRunner()
    system_a = FlexibleLiveAgentSystem(runner_a)
    seen: list[object] = []

    async def crash_after_two(execution: object) -> None:
        seen.append(execution)
        if len(seen) == 2:
            raise RuntimeError("controlled worker crash")

    with pytest.raises(BaseExceptionGroup):
        await system_a.run(
            window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=3,
            pair_execution_reporter=crash_after_two,
            checkpoint_request_sha256=RUN_SHA,
            reference_date=date(2026, 7, 30),
            pair_worker_count_override=1,
        )
    assert len(seen) == 2
    runner_b = _FullUniverseLiveRunner()
    recovered = await FlexibleLiveAgentSystem(runner_b).run(
        window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        recovered_pair_executions=tuple(seen),  # type: ignore[arg-type]
        reference_date=date(2026, 7, 30),
        pair_worker_count_override=1,
    )
    assert runner_b.calls == 1
    assert len(recovered.pair_runs) == 3
    assert len({item.date_pair.id for item in recovered.pair_runs}) == 3
