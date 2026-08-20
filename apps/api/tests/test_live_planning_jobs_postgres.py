"""PostgreSQL process-boundary checks for the durable live-job control plane.

These tests intentionally use two independent Python processes.  They are skipped
unless ``TRIPCHORD_POSTGRES_TEST_URL`` points at a PostgreSQL instance (the CI
workflow provisions one); the normal local suite remains SQLite-only and fast.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import signal
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_jobs import (
    LiveJobWorkerCommand,
    LivePlanningJobRegistry,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
)
from tripchord.persistence.database import Database
from tripchord.persistence.live_planning_jobs import DurableLivePlanningJobStore

POSTGRES_URL = os.environ.get("TRIPCHORD_POSTGRES_TEST_URL")
REQUEST_SHA = "f" * 64
TENANT = "postgres-process-test"

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="TRIPCHORD_POSTGRES_TEST_URL is not configured"
)


def _group_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _snapshot(job_id: str) -> LivePlanningJobSnapshot:
    now = datetime.now(UTC)
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
        deadline_at=now + timedelta(minutes=5),
    )


def _checkpoint(sequence: int, pair_id: str) -> LivePlanningPairCheckpoint:
    departure = datetime(2026, 8, 20, tzinfo=UTC).date() + timedelta(days=sequence)
    return LivePlanningPairCheckpoint.create(
        sequence=sequence,
        request_sha256=REQUEST_SHA,
        date_pair_id=pair_id,
        departure_date=departure,
        return_date=departure + timedelta(days=3),
        state=LivePlanningPairCheckpointState.COMPLETED,
        query_task_ids=(f"query-{pair_id}",),
        captured_at=datetime.now(UTC),
        run_purpose="exploration",
        finalization_state="exploration_complete",
        decision_state="candidate",
        source_task_count=1,
        exploration_seal_passed=True,
        all_platforms_complete=True,
    )


async def _crashed_worker(url: str, job_id: str) -> None:
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    lease = await store.claim_with_identity(job_id, tenant_id=TENANT, lease_seconds=1)
    assert lease is not None
    for sequence in (1, 2):
        checkpoint = _checkpoint(sequence, f"pair-{sequence}")
        assert await store.store_pair_result(
            job_id,
            tenant_id=TENANT,
            checkpoint=checkpoint,
            execution={"date_pair_id": checkpoint.date_pair_id, "worker": "A"},
            execution_sha256=(str(sequence) * 64),
            owner=lease.owner,
            lease_generation=lease.generation,
        )
    # This is the process boundary under test: no graceful release or settle.
    os.kill(os.getpid(), signal.SIGKILL)


async def _recovery_worker(url: str, job_id: str) -> None:
    await asyncio.sleep(1.25)
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    lease = await store.claim_with_identity(job_id, tenant_id=TENANT, lease_seconds=5)
    assert lease is not None
    assert lease.generation == 2
    checkpoint = _checkpoint(3, "pair-3")
    assert await store.store_pair_result(
        job_id,
        tenant_id=TENANT,
        checkpoint=checkpoint,
        execution={"date_pair_id": checkpoint.date_pair_id, "worker": "B"},
        execution_sha256="3" * 64,
        owner=lease.owner,
        lease_generation=lease.generation,
    )
    settled = await store.settle(
        job_id,
        tenant_id=TENANT,
        state=LivePlanningJobState.SUCCEEDED,
        result={"pair_runs": ["pair-1", "pair-2", "pair-3"]},
        owner=lease.owner,
        lease_generation=lease.generation,
    )
    assert settled.state == LivePlanningJobState.SUCCEEDED


async def _create_idempotent_job(url: str, key: str, result_path: str) -> None:
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    job_id = f"postgres-race-{os.getpid()}"
    created = await store.create_or_get(
        tenant_id=TENANT,
        idempotency_key=key,
        request_sha256=REQUEST_SHA,
        snapshot=_snapshot(job_id),
        command_spec={"kind": "worker_command", "version": 1},
    )
    with open(result_path, "a", encoding="utf-8") as handle:
        handle.write(created.id + "\n")


def _run(coro: Any, *args: str) -> None:
    asyncio.run(coro(*args))


async def postgres_registry_worker_entry(
    *,
    request_digest: str,
    job_id: str,
    tenant_id: str,
    lease_owner: str,
    lease_generation: int,
    payload: dict[str, object] | None = None,
    **_: object,
) -> dict[str, object]:
    """Deterministic allowlisted worker used through the production subprocess path."""
    url = os.environ["TRIPCHORD_POSTGRES_TEST_URL"]
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    existing = await store.load_pair_results(job_id, tenant_id=tenant_id)
    next_sequence = len(existing) + 1
    while next_sequence <= 3:
        checkpoint = _checkpoint(next_sequence, f"pair-{next_sequence}")
        accepted = await store.store_pair_result(
            job_id,
            tenant_id=tenant_id,
            checkpoint=checkpoint,
            execution={
                "date_pair_id": checkpoint.date_pair_id,
                "worker_generation": lease_generation,
            },
            execution_sha256=(str(next_sequence) * 64),
            owner=lease_owner,
            lease_generation=lease_generation,
        )
        if not accepted:
            raise RuntimeError("worker lease was fenced")
        execution_log = os.environ.get("TRIPCHORD_POSTGRES_EXECUTION_LOG")
        if execution_log:
            with open(execution_log, "a", encoding="utf-8") as handle:
                handle.write(f"pair-{next_sequence}\t{lease_generation}\n")
        next_sequence += 1
        if lease_generation == 1 and next_sequence == 3:
            # Coordinator A is killed while this real worker remains orphaned.
            await asyncio.Event().wait()
    await database.dispose()
    return {"pair_runs": ["pair-1", "pair-2", "pair-3"], "worker_generation": lease_generation}


async def _registry_coordinator_a(
    url: str, state_path: str, result_path: str, tenant_id: str
) -> None:
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    registry = LivePlanningJobRegistry(
        durable_store=store, state_path=Path(state_path), durable_lease_seconds=1
    )
    command = LiveJobWorkerCommand(
        module_path=__file__,
        entry="postgres_registry_worker_entry",
        args={"payload": {}, "request_digest": REQUEST_SHA, "tenant_id": tenant_id},
    )
    created, replayed = await registry.start_idempotent(
        tenant_id=tenant_id,
        operation=command,
        idempotency_key=f"formal-postgres-recovery-{result_path}",
        request_digest=REQUEST_SHA,
        deadline_seconds=30,
    )
    assert not replayed
    Path(result_path).write_text(created.id, encoding="utf-8")
    while len(await store.load_pair_results(created.id, tenant_id=tenant_id)) < 2:
        await asyncio.sleep(0.02)
    runtime = registry._records[created.id]
    assert runtime.worker_handle is not None
    Path(result_path + ".pid").write_text(str(runtime.worker_handle.pid), encoding="utf-8")
    Path(result_path + ".ready").write_text("ready", encoding="utf-8")
    await asyncio.Event().wait()


async def _registry_coordinator_b(
    url: str, state_path: str, job_id: str, result_path: str, tenant_id: str
) -> None:
    database = Database(url)
    store = DurableLivePlanningJobStore(database)
    registry = LivePlanningJobRegistry(
        durable_store=store, state_path=Path(state_path), durable_lease_seconds=1
    )
    await asyncio.sleep(0.2)
    Path(result_path + ".started").write_text("started", encoding="utf-8")
    await registry.restore_after_restart()
    Path(result_path + ".first").write_text("restored-while-lease-valid", encoding="utf-8")

    def resolver(spec: dict[str, Any]) -> Any:
        if spec != {
            "kind": "worker_command",
            "module_path": __file__,
            "entry": "postgres_registry_worker_entry",
            "args": {"payload": {}, "request_digest": REQUEST_SHA, "tenant_id": tenant_id},
            "probe_path": None,
        }:
            raise AssertionError(repr(spec))
        assert spec == {
            "kind": "worker_command",
            "module_path": __file__,
            "entry": "postgres_registry_worker_entry",
            "args": {"payload": {}, "request_digest": REQUEST_SHA, "tenant_id": tenant_id},
            "probe_path": None,
        }
        return LiveJobWorkerCommand(
            module_path=__file__,
            entry="postgres_registry_worker_entry",
            args={"payload": {}, "request_digest": REQUEST_SHA, "tenant_id": tenant_id},
        )

    deadline = asyncio.get_running_loop().time() + 10
    recovered: tuple[str, ...] = ()
    while asyncio.get_running_loop().time() < deadline:
        await registry.restore_after_restart()
        recovered = await registry.recover_durable(tenant_id=tenant_id, command_resolver=resolver)
        if recovered:
            break
        await asyncio.sleep(0.05)
    assert recovered == (job_id,)
    while True:
        current = await registry.get(job_id, tenant_id)
        if current is not None and current.state == LivePlanningJobState.SUCCEEDED:
            assert current.result is not None
            assert current.result["pair_runs"] == ["pair-1", "pair-2", "pair-3"]
            assert current.result["worker_generation"] > 1
            Path(result_path).write_text("succeeded", encoding="utf-8")
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"recovered registry did not settle: {current!r}")
        await asyncio.sleep(0.05)
    await registry.close()
    await database.dispose()


@pytest.mark.integration
def test_postgres_registry_command_recovery_runs_missing_pair_and_public_get(tmp_path: Any) -> None:
    assert POSTGRES_URL is not None
    state_path = tmp_path / "live-jobs.json"
    result_path = tmp_path / "job-id.txt"
    tenant_id = f"postgres-process-{os.getpid()}-{time.monotonic_ns()}"
    execution_log = tmp_path / "execution.log"
    os.environ["TRIPCHORD_POSTGRES_EXECUTION_LOG"] = str(execution_log)
    context = multiprocessing.get_context("spawn")
    coordinator_a = context.Process(
        target=_run,
        args=(_registry_coordinator_a, POSTGRES_URL, str(state_path), str(result_path), tenant_id),
    )
    coordinator_b: multiprocessing.Process | None = None
    worker_pid: int | None = None
    coordinator_a.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not Path(str(result_path) + ".ready").exists():
            time.sleep(0.05)
        assert Path(str(result_path) + ".ready").exists()
        job_id = result_path.read_text(encoding="utf-8")
        coordinator_b = context.Process(
            target=_run,
            args=(
                _registry_coordinator_b,
                POSTGRES_URL,
                str(state_path),
                job_id,
                str(result_path) + ".done",
                tenant_id,
            ),
        )
        coordinator_b.start()
        first_deadline = time.monotonic() + 5
        first_marker = Path(str(result_path) + ".done.first")
        while time.monotonic() < first_deadline and not first_marker.exists():
            time.sleep(0.05)
        assert first_marker.exists()
        worker_pid = int(Path(str(result_path) + ".pid").read_text(encoding="utf-8"))
        os.kill(worker_pid, 0)
        coordinator_a.kill()
        coordinator_a.join(10)
        assert coordinator_a.exitcode == -signal.SIGKILL
        coordinator_b.join(15)
        assert coordinator_b.exitcode == 0
        assert Path(str(result_path) + ".done").read_text(encoding="utf-8") == "succeeded"
        executions = [
            line.split("\t")
            for line in execution_log.read_text(encoding="utf-8").splitlines()
        ]
        assert executions[:2] == [["pair-1", "1"], ["pair-2", "1"]]
        assert executions[2][0] == "pair-3"
        assert int(executions[2][1]) > 1
        workers_dir = state_path.parent / f".{state_path.name}.workers"
        assert not (workers_dir / f"{job_id}.json").exists()
        assert not (workers_dir / f"{job_id}.death-proof.json").exists()
        dead_deadline = time.monotonic() + 5
        while time.monotonic() < dead_deadline and _group_alive(worker_pid):
            time.sleep(0.05)
        assert not _group_alive(worker_pid)

        async def verify_generations() -> None:
            database = Database(POSTGRES_URL)
            results = await DurableLivePlanningJobStore(database).load_pair_results(
                job_id, tenant_id=tenant_id
            )
            assert [item["date_pair_id"] for item in results] == [
                "pair-1",
                "pair-2",
                "pair-3",
            ]
            generations = [item["worker_generation"] for item in results]
            assert generations[:2] == [1, 1]
            assert generations[2] > 1
            await database.dispose()

        asyncio.run(verify_generations())
    finally:
        # Cleanup is only a safety net after the assertions above. The success
        # path already proved the recovery chain removed the old process group;
        # this must not turn a failed death assertion into a false pass.
        if coordinator_a.is_alive():
            coordinator_a.kill()
        coordinator_a.join(5)
        if coordinator_b is not None:
            if coordinator_b.is_alive():
                coordinator_b.kill()
            coordinator_b.join(5)
        if worker_pid is not None and _group_alive(worker_pid):
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(worker_pid, signal.SIGKILL)
        os.environ.pop("TRIPCHORD_POSTGRES_EXECUTION_LOG", None)

    async def public_get() -> None:
        import tripchord.main as main_module
        from tripchord.auth import Principal, get_principal

        database = Database(POSTGRES_URL)
        registry = LivePlanningJobRegistry(durable_store=DurableLivePlanningJobStore(database))
        main_module.app.state.live_planning_job_registry = registry
        main_module.app.dependency_overrides[get_principal] = lambda: Principal(
            tenant_id=tenant_id, auth_mode="integration-test"
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main_module.app),
                base_url="http://testserver",
            ) as client:
                response = await client.get(
                    f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
                )
            assert response.status_code == 200
            payload = response.json()
            assert payload["state"] == "succeeded"
            assert payload["result"]["pair_runs"] == ["pair-1", "pair-2", "pair-3"]
        finally:
            main_module.app.dependency_overrides.pop(get_principal, None)
            await registry.close()
            await database.dispose()

    asyncio.run(public_get())


@pytest.mark.integration
def test_postgres_two_process_recovery_and_generation_fencing(tmp_path: Any) -> None:
    assert POSTGRES_URL is not None
    job_id = f"postgres-recovery-{os.getpid()}"

    async def prepare() -> None:
        database = Database(POSTGRES_URL)
        await DurableLivePlanningJobStore(database).create_or_get(
            tenant_id=TENANT,
            idempotency_key=f"recovery-{job_id}",
            request_sha256=REQUEST_SHA,
            snapshot=_snapshot(job_id),
            command_spec={"kind": "worker_command", "version": 1},
        )
        await database.dispose()

    asyncio.run(prepare())
    crashed = multiprocessing.get_context("spawn").Process(
        target=_run, args=(_crashed_worker, POSTGRES_URL, job_id)
    )
    crashed.start()
    crashed.join(10)
    assert crashed.exitcode == -signal.SIGKILL

    recovered = multiprocessing.get_context("spawn").Process(
        target=_run, args=(_recovery_worker, POSTGRES_URL, job_id)
    )
    recovered.start()
    recovered.join(10)
    assert recovered.exitcode == 0

    async def verify() -> None:
        database = Database(POSTGRES_URL)
        store = DurableLivePlanningJobStore(database, reap_lease_seconds=1)
        pair_results = await store.load_pair_results(job_id, tenant_id=TENANT)
        assert [item["date_pair_id"] for item in pair_results] == [
            "pair-1",
            "pair-2",
            "pair-3",
        ]
        final = await store.get(job_id, tenant_id=TENANT)
        assert final is not None and final.state == LivePlanningJobState.SUCCEEDED
        old_lease_write = await store.store_pair_result(
            job_id,
            tenant_id=TENANT,
            checkpoint=_checkpoint(1, "pair-1"),
            execution={"date_pair_id": "pair-1", "worker": "late-A"},
            execution_sha256="a" * 64,
            owner="worker:stale-A",
            lease_generation=1,
        )
        assert old_lease_write is False
        after = await store.get(job_id, tenant_id=TENANT)
        assert after == final
        await database.dispose()

    asyncio.run(verify())


@pytest.mark.integration
def test_postgres_two_process_idempotency_creates_one_job(tmp_path: Any) -> None:
    assert POSTGRES_URL is not None
    key = f"idempotency-{os.getpid()}"
    result_path = str(tmp_path / "ids.txt")

    async def prepare() -> None:
        database = Database(POSTGRES_URL)
        await database.dispose()

    asyncio.run(prepare())
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_run, args=(_create_idempotent_job, POSTGRES_URL, key, result_path))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    with open(result_path, encoding="utf-8") as handle:
        ids = [line.strip() for line in handle if line.strip()]
    assert len(ids) == 2
    assert len(set(ids)) == 1
@pytest.mark.integration
def test_postgres_orphan_reap_fence_blocks_claim_until_confirmed(tmp_path: Any) -> None:
    """A cleanup owner closes the check-to-kill claim race in PostgreSQL."""
    assert POSTGRES_URL is not None

    async def exercise() -> None:
        database_b = Database(POSTGRES_URL)
        database_c = Database(POSTGRES_URL)
        store_b = DurableLivePlanningJobStore(database_b, reap_lease_seconds=1)
        store_c = DurableLivePlanningJobStore(database_c, reap_lease_seconds=1)
        tenant = f"reap-fence-{os.getpid()}-{time.monotonic_ns()}"
        job_id = f"reap-fence-{time.monotonic_ns()}"
        await store_b.create_or_get(
            tenant_id=tenant,
            idempotency_key=job_id,
            request_sha256=REQUEST_SHA,
            snapshot=_snapshot(job_id),
            command_spec={"kind": "worker_command", "version": 1},
        )
        lease = await store_b.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1)
        assert lease is not None
        await asyncio.sleep(1.1)
        assert await store_b.authorize_orphan_reap(
            job_id, tenant_id=tenant, owner=lease.owner, generation=lease.generation,
            reaper_id="reaper-b",
        )
        # B is paused before authentication/kill. C cannot claim the fenced row.
        assert await store_c.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1) is None
        assert not await store_c.store_pair_result(
            job_id,
            tenant_id=tenant,
            checkpoint=_checkpoint(1, "pair-old"),
            execution={"date_pair_id": "pair-old"},
            execution_sha256="1" * 64,
            owner=lease.owner,
            lease_generation=lease.generation,
        )
        await asyncio.sleep(1.1)
        replacement_token = await store_c.authorize_orphan_reap(
            job_id, tenant_id=tenant, owner=lease.owner, generation=lease.generation,
            reaper_id="reaper-c",
        )
        assert replacement_token is not None
        assert not await store_c.complete_orphan_reap(
            job_id,
            tenant_id=tenant,
            owner=lease.owner,
            generation=lease.generation,
            reap_token=replacement_token,
        )
        assert not await store_c.record_orphan_reap_proof(
            job_id,
            tenant_id=tenant,
            owner=lease.owner,
            generation=lease.generation,
            reap_token="reaper:stale",
            pgid=12345,
            marker_digest="a" * 64,
        )
        assert not await store_b.complete_orphan_reap(
            job_id,
            tenant_id=tenant,
            owner=lease.owner,
            generation=lease.generation,
            reap_token="reaper:stale",
        )
        assert await store_c.record_orphan_reap_proof(
            job_id,
            tenant_id=tenant,
            owner=lease.owner,
            generation=lease.generation,
            reap_token=replacement_token,
            pgid=12345,
            marker_digest="a" * 64,
        )
        assert await store_c.complete_orphan_reap(
            job_id,
            tenant_id=tenant,
            owner=lease.owner,
            generation=lease.generation,
            reap_token=replacement_token,
        )
        first, second = await asyncio.gather(
            store_b.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1),
            store_c.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1),
        )
        assert (first is None) != (second is None)
        await database_b.dispose()
        await database_c.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_marker_only_esrch_completes_reap_after_reaper_crash(tmp_path: Any) -> None:
    """A marker-only restart may complete only a DB-backed reaping fence."""
    assert POSTGRES_URL is not None

    async def exercise() -> None:
        database = Database(POSTGRES_URL)
        store = DurableLivePlanningJobStore(database, reap_lease_seconds=1)
        tenant = f"marker-reap-{os.getpid()}-{time.monotonic_ns()}"
        job_id = f"live-job-{uuid.uuid4()}"
        await store.create_or_get(
            tenant_id=tenant,
            idempotency_key=job_id,
            request_sha256=REQUEST_SHA,
            snapshot=_snapshot(job_id),
            command_spec={"kind": "worker_command", "version": 1},
        )
        lease = await store.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1)
        assert lease is not None
        await asyncio.sleep(1.1)
        token = await store.authorize_orphan_reap(
            job_id, tenant_id=tenant, owner=lease.owner, generation=lease.generation,
            reaper_id="reaper-b",
        )
        assert token is not None
        assert (
            await store.authorize_orphan_reap(
                job_id,
                tenant_id=tenant,
                owner=lease.owner,
                generation=lease.generation,
                reaper_id="reaper-c",
            )
            is None
        )
        await asyncio.sleep(1.1)

        state_path = tmp_path / "live-jobs.json"
        workers_dir = state_path.parent / f".{state_path.name}.workers"
        workers_dir.mkdir(mode=0o700)
        marker = "marker-reaper-crash"
        receipt = {
            "schema": "tripchord.live-authenticated-cleanup.v1",
            "job_id": job_id,
            "pgid": 99999999,
            "marker": marker,
            "tenant_id": tenant,
            "lease_owner": lease.owner,
            "lease_generation": lease.generation,
            "authenticated_cleanup": True,
            "authenticated_at": datetime.now(UTC).isoformat(),
            "digest": "",
        }
        receipt["digest"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in receipt.items() if key != "digest"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        (workers_dir / f"{job_id}.json").write_text(json.dumps(receipt), encoding="utf-8")
        assert LivePlanningJobRegistry._validate_worker_receipt(receipt, job_id)
        registry = LivePlanningJobRegistry(
            durable_store=store, state_path=state_path, hard_stop_confirm_seconds=0.1
        )
        try:
            await registry.restore_after_restart()
            replacement = await store.claim_with_identity(
                job_id, tenant_id=tenant, lease_seconds=1
            )
            assert replacement is not None
            assert replacement.generation == 3
            assert not (workers_dir / f"{job_id}.json").exists()
        finally:
            await database.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgres_authenticated_receipt_crash_window_is_fenced(tmp_path: Any) -> None:
    """Production discovery keeps an authenticated receipt until DB complete."""
    assert POSTGRES_URL is not None

    async def exercise() -> None:
        database = Database(POSTGRES_URL)
        store = DurableLivePlanningJobStore(database, reap_lease_seconds=1)
        tenant = f"receipt-window-{os.getpid()}-{time.monotonic_ns()}"
        job_id = f"live-job-{uuid.uuid4()}"
        await store.create_or_get(
            tenant_id=tenant,
            idempotency_key=job_id,
            request_sha256=REQUEST_SHA,
            snapshot=_snapshot(job_id),
            command_spec={"kind": "worker_command", "version": 1},
        )
        lease = await store.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1)
        assert lease is not None
        state_path = tmp_path / "live-jobs.json"
        workers_dir = state_path.parent / f".{state_path.name}.workers"
        workers_dir.mkdir(mode=0o700)
        marker = "receipt-window-marker"
        marker_file = workers_dir / f"{job_id}.json"
        marker_file.write_text(
            json.dumps(
                {
                    "schema": "tripchord.live-authenticated-cleanup.v1",
                    "job_id": job_id,
                    "pid": 99999999,
                    "pgid": 99999999,
                    "marker": marker,
                    "probe_path": "",
                    "tenant_id": tenant,
                    "lease_owner": lease.owner,
                    "lease_generation": lease.generation,
                }
            ),
            encoding="utf-8",
        )
        await asyncio.sleep(1.1)
        registry = LivePlanningJobRegistry(
            durable_store=store, state_path=state_path, hard_stop_confirm_seconds=0.1
        )
        registry._group_commands = lambda _pgid: (f"worker --marker {marker}",)  # type: ignore[method-assign]
        real_killpg = os.killpg

        def crash_before_kill(pgid: int, sig: int) -> None:
            if pgid == 99999999:
                raise OSError("controlled reaper crash")
            real_killpg(pgid, sig)

        try:
            with patch("tripchord.agents.live_jobs.os.killpg", crash_before_kill):
                await registry.restore_after_restart()
            assert marker_file.exists()
            assert (
                json.loads(marker_file.read_text(encoding="utf-8"))["authenticated_cleanup"]
                is True
            )
            assert await store.claim_with_identity(job_id, tenant_id=tenant) is None
            await asyncio.sleep(1.1)
            registry._group_commands = lambda _pgid: ()  # type: ignore[method-assign]

            def prove_esrch(pgid: int, sig: int) -> None:
                raise ProcessLookupError(pgid)

            with patch("tripchord.agents.live_jobs.os.killpg", prove_esrch):
                await registry.restore_after_restart()
            replacement = await store.claim_with_identity(job_id, tenant_id=tenant)
            assert replacement is not None
            assert not marker_file.exists()
        finally:
            await registry.close()
            await database.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation,rehash",
    [
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(schema="evil"),
            True,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                job_id=f"live-job-{uuid.uuid4()}"
            ),
            True,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                authenticated_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat()
            ),
            True,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.pop("authenticated_at"),
            False,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                lease_generation="1"
            ),
            True,
        ),
        (lambda receipt, job_id, tenant, owner, generation: receipt.update(digest="0" * 64), False),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                tenant_id="wrong-tenant"
            ),
            True,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                lease_owner="wrong-owner"
            ),
            True,
        ),
        (
            lambda receipt, job_id, tenant, owner, generation: receipt.update(
                lease_generation=generation + 1
            ),
            True,
        ),
    ],
    ids=[
        "schema", "job_id", "future", "missing", "type", "digest", "tenant",
        "owner", "generation",
    ],
)
def test_postgres_invalid_authenticated_receipt_is_fail_closed(
    tmp_path: Any, mutation: Any, rehash: bool
) -> None:
    """Invalid receipts never authorize completion or a replacement claim."""
    assert POSTGRES_URL is not None

    async def exercise() -> None:
        database = Database(POSTGRES_URL)
        store = DurableLivePlanningJobStore(database, reap_lease_seconds=1)
        tenant = f"invalid-receipt-{os.getpid()}-{time.monotonic_ns()}"
        job_id = f"live-job-{uuid.uuid4()}"
        await store.create_or_get(
            tenant_id=tenant,
            idempotency_key=job_id,
            request_sha256=REQUEST_SHA,
            snapshot=_snapshot(job_id),
            command_spec={"kind": "worker_command", "version": 1},
        )
        lease = await store.claim_with_identity(job_id, tenant_id=tenant, lease_seconds=1)
        assert lease is not None
        await asyncio.sleep(1.1)
        state_path = tmp_path / f"{job_id}.json"
        workers_dir = state_path.parent / f".{state_path.name}.workers"
        workers_dir.mkdir(mode=0o700)
        receipt: dict[str, object] = {
            "schema": "tripchord.live-authenticated-cleanup.v1",
            "job_id": job_id,
            "pgid": 99999999,
            "marker": "invalid-receipt-marker",
            "tenant_id": tenant,
            "lease_owner": lease.owner,
            "lease_generation": lease.generation,
            "authenticated_cleanup": True,
            "authenticated_at": datetime.now(UTC).isoformat(),
            "digest": "",
        }
        mutation(receipt, job_id, tenant, lease.owner, lease.generation)
        if rehash:
            receipt["digest"] = LivePlanningJobRegistry._proof_digest(receipt)
        else:
            receipt["digest"] = "0" * 64
        marker_path = workers_dir / f"{job_id}.json"
        marker_path.write_text(json.dumps(receipt), encoding="utf-8")
        registry = LivePlanningJobRegistry(durable_store=store, state_path=state_path)
        try:
            await registry.restore_after_restart()
            assert await store.claim_with_identity(job_id, tenant_id=tenant) is None
            assert marker_path.exists()
        finally:
            await registry.close()
            await database.dispose()

    asyncio.run(exercise())
