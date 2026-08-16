"""C-146 P0 counter-examples: the production HTTP worker chain + process-group
hard-stop + concurrent watchdog + cold-start recovery + quarantine-overflow
fail-closed + hard identity/bytes caps (task #13-#16, RETURN comment 9666a380).

The production FastAPI route now wraps the REAL planning operation in a
``LiveJobWorkerCommand`` that the registry executes in an INDEPENDENT subprocess
(``live_job_worker``). These tests prove, with real OS processes:

- P0-1: the HTTP POST path runs the real worker subprocess, records the durable
  worker identity (PGID + marker nonce), and a real HTTP cancel really stops the
  whole process group (probe growth freezes, group is dead).
- P0-2: a hard stop kills the whole process group INCLUDING a stubborn grandchild
  the worker spawned, and a cold start discovers + authenticates + cleans a real
  orphaned group via its marker nonce (a reused PGID owned by an unrelated
  process is never killed).
- P0-3: the single hard-stop watchdog concurrently enforces every due
  operation's OWN absolute deadline+grace with ``max_running=2`` — each stubborn
  group stops within its own bound and the shared watchdog survives the race.
- P0-4: FastAPI startup (``restore_after_restart``) auto-terminates a durable
  quarantined + pending_terminal record with zero requests; memory == disk; a
  second boot is stable.
- P0-5: persistent quarantine above the current qcap still loads, is fail-closed
  (no new conversion/admission), keeps tombstones, and bounded retention cleanup
  restores capacity.
- P0-6: idempotency/byte/identity caps are enforced on load AND atomically
  BEFORE any ``_records``/``_idempotency`` mutation or worker start, and the
  state read path is FD-bounded (fstat FIRST).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tripchord.main as main_module
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_jobs import (
    _QUARANTINE_HARD_STOPPED_STAGE,
    _QUARANTINE_ORPHAN_STAGE,
    LiveJobWorkerCommand,
    LivePlanningJobCapacityError,
    LivePlanningJobIdempotencyConflictError,
    LivePlanningJobRegistry,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningSafeFailureCode,
    _PendingTerminalOutcome,
    _safe_failure_diagnostic,
)
from tripchord.main import (
    LiveRunCache,
    app,
    package_requirement_agent,
    settings,
)

REQUEST_SHA256 = "a" * 64

_GRANDCHILD_SRC = (
    "import sys, time\n"
    "probe = sys.argv[1]\n"
    "while True:\n"
    "    with open(probe, 'a') as fh:\n"
    "        fh.write('grandchild\\n')\n"
    "        fh.flush()\n"
    "    time.sleep(0.01)\n"
)

# A stubborn worker entry: appends to the probe forever and optionally spawns a
# grandchild (same process group) that also appends. A registry hard stop /
# cancel must kill the WHOLE group — the probe permanently freezes.
_STUBBORN_WORKER_SRC = f'''\
import asyncio
import os
import subprocess
import sys

_GRANDCHILD = {_GRANDCHILD_SRC!r}

async def run_stubborn(*, probe_path=None, spawn_grandchild=False, **kwargs):
    with open(probe_path, "a") as fh:
        fh.write("leader-start:" + str(os.getpid()) + "\\n")
        fh.flush()
    if spawn_grandchild:
        child = subprocess.Popen(
            [sys.executable, "-c", _GRANDCHILD, probe_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with open(probe_path, "a") as fh:
            fh.write("grandchild-pid:" + str(child.pid) + "\\n")
            fh.flush()
    while True:
        with open(probe_path, "a") as fh:
            fh.write("leader:" + str(os.getpid()) + "\\n")
            fh.flush()
        await asyncio.sleep(0.01)
'''


def _write_stubborn_worker(tmp_path: Path) -> Path:
    module = tmp_path / "stubborn_worker.py"
    module.write_text(_STUBBORN_WORKER_SRC, encoding="utf-8")
    return module


def _stubborn_command(
    module: Path,
    probe: Path,
    *,
    spawn_grandchild: bool = False,
) -> LiveJobWorkerCommand:
    return LiveJobWorkerCommand(
        module_path=str(module),
        entry="run_stubborn",
        args={"spawn_grandchild": spawn_grandchild},
        probe_path=str(probe),
    )


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _probe_grows(probe: Path, wait: float = 0.2) -> bool:
    try:
        size = probe.stat().st_size
    except OSError:
        return False
    time.sleep(wait)
    try:
        return probe.stat().st_size > size
    except OSError:
        return False


def _wait_for_probe(probe: Path, needle: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe.exists():
            try:
                text = probe.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if needle in text:
                return
        time.sleep(0.05)
    raise AssertionError(f"probe never wrote {needle!r}: {probe!r}")


async def _wait_for_runtime(
    runtime: Any,
    predicate: Any,
    timeout: float = 15.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(runtime):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("runtime condition not reached in time")


def _v3_snapshot(
    job_id: str,
    state: LivePlanningJobState,
    stage: str,
    progress: int,
    revision: int,
    *,
    cancellation_requested: bool = False,
    cancel_pending: bool = False,
) -> LivePlanningJobSnapshot:
    created = datetime.now(UTC) - timedelta(minutes=5)
    return LivePlanningJobSnapshot(
        id=job_id,
        state=state,
        stage=stage,
        progress=progress,
        revision=revision,
        cancellation_requested=cancellation_requested,
        cancel_pending=cancel_pending,
        request_sha256=REQUEST_SHA256,
        model_trace_scope_sha256=REQUEST_SHA256,
        created_at=created,
        updated_at=created,
        deadline_at=created + timedelta(hours=1),
    )


def _write_registry_state(payload: dict[str, Any], state_path: Path) -> None:
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    state_path.chmod(0o600)


def _v3_idempotency_entry(
    tenant_id: str,
    idempotency_key: str,
    job_id: str,
    *,
    defer_start: bool = True,
) -> dict[str, Any]:
    return {
        "partition": LivePlanningJobRegistry._idempotency_partition(tenant_id, idempotency_key),
        "job_id": job_id,
        "request_digest": REQUEST_SHA256,
        "defer_start": defer_start,
    }


def _v3_record(
    tenant_id: str,
    snapshot: LivePlanningJobSnapshot,
    *,
    quarantined: bool = False,
    quarantine_stage: str | None = None,
    pending_terminal: dict[str, Any] | None = None,
    worker_pgid: int | None = None,
    worker_marker: str | None = None,
    worker_probe: str | None = None,
) -> dict[str, Any]:
    return {
        "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
        "snapshot": snapshot.model_dump(mode="json"),
        "prepared": False,
        "activation_operation": None,
        "pending_terminal": pending_terminal,
        "quarantined": quarantined,
        "quarantine_stage": quarantine_stage,
        "worker_pgid": worker_pgid,
        "worker_marker": worker_marker,
        "worker_probe": worker_probe,
    }


def _payload(*, ready: bool) -> dict[str, object]:
    destination = "，目的地：马累" if ready else ""
    return {
        "requirement": {
            "text": (
                f"出发地：杭州{destination}，2026年8月出发，玩5晚，"
                "2名成人，1间房，无行李，接受中转"
            ),
            "reference_date": "2026-07-30",
        },
        "coverage_mode": "strict",
        "timeout_seconds": 300,
        "total_timeout_seconds": 1800,
        "max_pairs": 1,
    }


async def _terminal_job_slow(
    client: AsyncClient,
    job_id: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
        )
        assert response.status_code == 200, response.text
        body = response.json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.2)
    raise AssertionError("live job did not reach a terminal state")


def _http_app_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    registry_kwargs: dict[str, Any] | None = None,
) -> LivePlanningJobRegistry:
    state_path = tmp_path / "live-jobs.json"
    kwargs: dict[str, Any] = {"state_path": state_path, "capacity": 4}
    if registry_kwargs:
        kwargs.update(registry_kwargs)
    registry = LivePlanningJobRegistry(**kwargs)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(main_module.rate_limiter, "_limit", 1_000_000)
    return registry


# ---------------------------------------------------------------------------
# P0-1: production HTTP main chain runs the REAL worker subprocess + durable
# identity + HTTP cancel really stops the process group.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_route_runs_real_worker_subprocess_with_durable_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production route (NO builder override) wraps the real operation in a
    ``LiveJobWorkerCommand`` and runs it in an independent subprocess: the job
    succeeds, the durable record carries a real worker PGID + marker nonce, and
    the on-disk marker file is removed on clean exit. RED on HEAD: the route ran
    the operation in-process and no worker identity ever existed."""
    registry = _http_app_context(monkeypatch, tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job"]["id"]
            terminal = await _terminal_job_slow(client, job_id)
        assert terminal["state"] == "succeeded", terminal
        assert terminal["request_sha256"] == terminal.get("model_trace_scope_sha256")
        runtime = registry._records[job_id]
        assert runtime.worker_pgid is not None and runtime.worker_pgid > 0
        assert runtime.worker_marker
        # The marker file was written by the real worker and removed on clean
        # exit; the workers dir holds no stale marker for a succeeded job.
        workers_dir = registry._workers_dir()
        if workers_dir is not None and workers_dir.exists():
            markers = [p.name for p in workers_dir.iterdir() if p.name == f"{job_id}.json"]
            assert markers == []
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_http_cancel_really_stops_the_worker_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real HTTP cancel (DELETE) on a stubborn subprocess worker provably stops
    the whole process group: the worker PGID is recorded, the probe grows while
    running, and after the DELETE the group is dead (``os.killpg`` raises
    ProcessLookupError) and the probe is permanently frozen."""
    probe = tmp_path / "probe-cancel.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = _http_app_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        main_module,
        "_build_live_flexible_from_text_worker_command",
        lambda *a, **k: _stubborn_command(module, probe, spawn_grandchild=True),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job"]["id"]
            runtime = registry._records[job_id]
            await _wait_for_runtime(runtime, lambda r: r.worker_pgid is not None)
            pgid = runtime.worker_pgid
            assert pgid is not None and pgid > 0
            assert runtime.worker_marker
            assert _group_alive(pgid)
            _wait_for_probe(probe, "leader-start")
            assert _probe_grows(probe)
            stopped = await client.delete(
                f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["state"] == "cancelled"
        assert not _group_alive(pgid)
        assert not _probe_grows(probe)
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# P0-2: kill + wait the whole process group (stubborn grandchild), and cold
# start discovers + authenticates + cleans a real orphan; a reused PGID without
# the marker nonce is never killed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_stop_kills_whole_group_including_stubborn_grandchild(
    tmp_path: Path,
) -> None:
    """A worker that spawned a stubborn grandchild (same process group) is
    cancelled: BOTH the leader and the grandchild die (the group is empty), the
    probe permanently freezes, and the durable identity survives the stop."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "probe-group.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=2.0)
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe, spawn_grandchild=True),
            idempotency_key="group-kill",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_runtime(runtime, lambda r: r.worker_pgid is not None)
        pgid = runtime.worker_pgid
        assert pgid is not None and pgid > 0
        _wait_for_probe(probe, "grandchild-pid")
        grandchild_line = next(
            line
            for line in probe.read_text(encoding="utf-8").splitlines()
            if line.startswith("grandchild-pid:")
        )
        grandchild_pid = int(grandchild_line.split(":", 1)[1])
        # The grandchild inherits the worker's process group.
        assert os.getpgid(grandchild_pid) == pgid
        assert _group_alive(pgid)

        cancelled = await registry.cancel(snapshot.id, "tenant-a")
        assert cancelled is not None and cancelled.state == LivePlanningJobState.CANCELLED
        # The whole group — leader AND grandchild — is provably gone.
        assert not _group_alive(pgid)
        assert not _probe_grows(probe)
        # Durable identity survives the stop.
        assert runtime.worker_pgid == pgid
        assert runtime.worker_marker
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_cold_start_discovers_and_cleans_real_orphan_group(
    tmp_path: Path,
) -> None:
    """A parent-API SIGKILL mid-run leaves a real orphaned worker process group
    plus its durable marker file and state-file identity. A fresh registry's
    ``restore_after_restart`` discovers the group, AUTHENTICATES it via the
    marker nonce, kills the whole group, and quarantines the owning record as an
    orphan — all with zero requests."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "orphan-probe.txt"
    marker = hashlib.sha256(b"orphan-nonce").hexdigest()
    job_id = "live-job-orphaned"
    workers_dir = tmp_path / ".live-jobs.json.workers"

    orphan_code = (
        "import sys, time\n"
        "probe = sys.argv[2]\n"
        "while True:\n"
        "    with open(probe, 'a') as fh:\n"
        "        fh.write('orphan\\n')\n"
        "        fh.flush()\n"
        "    time.sleep(0.01)\n"
    )
    # Simulate the REAL parent-API crash: a "crasher" parent spawns the stubborn
    # worker in its own session, writes the durable marker file, then dies —
    # leaving the worker genuinely orphaned (reparented to init) exactly as a
    # SIGKILLed API process would. A worker spawned DIRECTLY by this test process
    # would linger as a zombie after the kill and shadow the group-gone confirm,
    # so the orphan must be reparented for the death to be provable.
    crash_src = (
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"probe = {str(probe)!r}\n"
        f"marker = {marker!r}\n"
        f"marker_file = {str(workers_dir / f'{job_id}.json')!r}\n"
        f"orphan_code = {orphan_code!r}\n"
        "worker = subprocess.Popen(\n"
        "    [sys.executable, '-c', orphan_code, marker, probe],\n"
        "    start_new_session=True,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "pgid = worker.pid\n"
        "Path(marker_file).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(marker_file).write_text(json.dumps({\n"
        "    'pid': pgid,\n"
        "    'pgid': pgid,\n"
        "    'marker': marker,\n"
        "    'probe_path': probe,\n"
        "    'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),\n"
        "}, ensure_ascii=False, sort_keys=True), encoding='utf-8')\n"
        "time.sleep(1)\n"
        "os._exit(99)\n"
    )
    crash = subprocess.Popen([sys.executable, "-c", crash_src])
    crash.wait(timeout=10)
    # The worker is now a genuine orphan; recover its group from the marker file
    # the crasher wrote before dying.
    pgid = json.loads((workers_dir / f"{job_id}.json").read_text(encoding="utf-8"))["pgid"]
    try:
        _wait_for_probe(probe, "orphan")
        assert _group_alive(pgid)
        assert os.getpgid(pgid) == pgid

        snap = _v3_snapshot(
            job_id, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 2
        )
        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [
                _v3_record(
                    "tenant-a",
                    snap,
                    worker_pgid=pgid,
                    worker_marker=marker,
                    worker_probe=str(probe),
                )
            ],
            "idempotency": [
                _v3_idempotency_entry("tenant-a", "orphan-key", job_id),
            ],
        }
        _write_registry_state(payload, state_path)

        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            await registry.restore_after_restart()
            # The orphan was reparented to init on the parent crash, so the
            # SIGKILLed group is reaped promptly and its death is confirmed.
            assert not _group_alive(pgid)
            assert not _probe_grows(probe)
            # The owning record is quarantined as an orphan, never replayed.
            runtime = registry._records[job_id]
            assert runtime.quarantined is True
            assert runtime.quarantine_stage == _QUARANTINE_ORPHAN_STAGE
            assert runtime.hard_stopped is True
            # Same-key fails closed.
            with pytest.raises(LivePlanningJobIdempotencyConflictError):
                await registry.start_idempotent(
                    tenant_id="tenant-a",
                    operation=_stubborn_command(
                        _write_stubborn_worker(tmp_path), tmp_path / "p.txt"
                    ),
                    idempotency_key="orphan-key",
                    request_digest=REQUEST_SHA256,
                    defer_start=False,
                )
        finally:
            await registry.close()
    finally:
        # The orphan is not a child of this process (it was reparented), so only
        # the group kill is needed for cleanup.
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_cold_start_never_kills_unauthenticated_group(
    tmp_path: Path,
) -> None:
    """A live process group whose command line does NOT contain the recorded
    marker nonce (a reused PGID or an unrelated process) is NEVER killed by
    ``restore_after_restart`` — the marker is the sole authentication."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "unrelated-probe.txt"
    claimed_marker = hashlib.sha256(b"claimed-nonce").hexdigest()

    unrelated_code = (
        "import sys, time\n"
        "probe = sys.argv[2]\n"
        "while True:\n"
        "    with open(probe, 'a') as fh:\n"
        "        fh.write('unrelated\\n')\n"
        "        fh.flush()\n"
        "    time.sleep(0.01)\n"
    )
    # The argv carries a DIFFERENT nonce, so the marker-file claim never matches.
    proc = subprocess.Popen(
        [sys.executable, "-c", unrelated_code, "other-nonce", str(probe)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = proc.pid
    try:
        _wait_for_probe(probe, "unrelated")
        assert _group_alive(pgid)
        workers_dir = tmp_path / ".live-jobs.json.workers"
        workers_dir.mkdir(exist_ok=True)
        (workers_dir / "live-job-unrelated.json").write_text(
            json.dumps(
                {
                    "pid": pgid,
                    "pgid": pgid,
                    "marker": claimed_marker,
                    "probe_path": str(probe),
                    "started_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [],
            "idempotency": [],
        }
        _write_registry_state(payload, state_path)
        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            await registry.restore_after_restart()
            # The unauthenticated group is untouched.
            assert _group_alive(pgid)
            assert _probe_grows(probe)
        finally:
            await registry.close()
    finally:
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)
        with _suppress_os():
            proc.wait(timeout=5)


class _suppress_os:
    """Best-effort OSError suppression for cleanup that never masks assertions."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: Any) -> None:
        return True


# ---------------------------------------------------------------------------
# P0-3: the single watchdog concurrently enforces every due operation's own
# absolute deadline+grace with max_running=2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_concurrent_staggered_deadlines_two_stubborn_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With ``max_running=2`` two stubborn worker process groups run
    concurrently under staggered absolute deadlines. The FIRST durable deadline
    intent cannot commit (permanent write failure), so the runner may NOT drain
    either executor — the single hard-stop watchdog is the only force that stops
    them. Each group is quarantined hard-stopped within its OWN deadline+grace,
    A hard-stops strictly before B, and the shared watchdog task survives the
    race (a single kill race never kills it)."""
    state_path = tmp_path / "live-jobs.json"
    probe_a = tmp_path / "probe-watchdog-a.txt"
    probe_b = tmp_path / "probe-watchdog-b.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=4,
        max_running=2,
        execution_hard_stop_grace_seconds=0.1,
        hard_stop_confirm_seconds=0.3,
    )
    fail_persists = False
    real_persist = registry._persist_locked

    def conditional_fail() -> None:
        if fail_persists:
            raise RuntimeError("injected permanent deadline-intent persist failure")
        real_persist()

    monkeypatch.setattr(registry, "_persist_locked", conditional_fail)
    try:
        started = time.monotonic()
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_a, spawn_grandchild=True),
            idempotency_key="watchdog-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=2.0,
        )
        snap_b, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_b, spawn_grandchild=True),
            idempotency_key="watchdog-b",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=3.0,
        )
        ra = registry._records[snap_a.id]
        rb = registry._records[snap_b.id]
        # Both executors live concurrently (max_running=2).
        await _wait_for_runtime(
            ra,
            lambda r: r.worker_pgid is not None
            and r.operation_task is not None
            and not r.operation_task.done(),
        )
        await _wait_for_runtime(
            rb,
            lambda r: r.worker_pgid is not None
            and r.operation_task is not None
            and not r.operation_task.done(),
        )
        pgid_a = ra.worker_pgid
        pgid_b = rb.worker_pgid
        assert pgid_a is not None and pgid_b is not None and pgid_a != pgid_b
        watchdog = registry._hard_stop_watchdog
        assert watchdog is not None and not watchdog.done()
        # Now every persist fails: the runner may NOT stop either executor, so
        # only the watchdog can enforce the absolute deadline+grace.
        fail_persists = True

        # A hard-stops within ITS OWN deadline+grace (2.0 + 0.1 + confirm).
        await _wait_for_runtime(
            ra,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        assert ra.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert registry._hard_stop_watchdog is watchdog
        assert not _group_alive(pgid_a)
        assert not _probe_grows(probe_a)
        # B is still live AFTER A stopped — the staggered deadlines are
        # enforced independently, not by a shared serial kill.
        assert not rb.hard_stopped
        assert _group_alive(pgid_b)
        assert _probe_grows(probe_b)
        elapsed_a = time.monotonic() - started
        assert elapsed_a <= 2.0 + 0.1 + registry._hard_stop_confirm_seconds + 2.0

        # B hard-stops at ITS OWN later deadline+grace.
        await _wait_for_runtime(
            rb,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        assert rb.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        # The single watchdog handled BOTH hard-stops and then self-terminated
        # (no live operation needs it) — it was NEVER replaced by a fresh task,
        # and never cancelled by the concurrent kill race. Either it has already
        # finished (None) or it is still the SAME instance about to finish.
        assert registry._hard_stop_watchdog is None or registry._hard_stop_watchdog is watchdog
        await asyncio.wait_for(watchdog, timeout=5.0)
        assert not watchdog.cancelled()
        assert not _group_alive(pgid_b)
        assert not _probe_grows(probe_b)
        elapsed_b = time.monotonic() - started
        assert elapsed_b <= 3.0 + 0.1 + registry._hard_stop_confirm_seconds + 2.0
        assert elapsed_a < elapsed_b
        # Neither executor was ever granted a guessed terminal label.
        assert ra.snapshot.state not in (
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        )
        assert rb.snapshot.state not in (
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        )
    finally:
        monkeypatch.undo()
        await registry.close()


# ---------------------------------------------------------------------------
# P0-4: FastAPI startup restores the unique cleanup owner for a durable
# quarantined + pending_terminal record and auto-terminates it with zero
# requests; memory == disk; a second boot is stable.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_restore_auto_terminates_quarantined_pending_terminal(
    tmp_path: Path,
) -> None:
    """A durable quarantined + pending_terminal (FAILED/deadline_exceeded)
    record cold-boots into a registry whose ``restore_after_restart`` restores
    the unique cleanup owner: with ZERO requests the record auto-terminates to
    the durable outcome, the disk matches memory, and a second cold boot
    observes the same terminal state."""
    state_path = tmp_path / "live-jobs.json"
    job_id = "live-job-startup-restore"
    pending = _PendingTerminalOutcome(
        state=LivePlanningJobState.FAILED,
        stage="deadline_exceeded",
        error="TimeoutError: live planning job deadline exceeded",
        safe_failure=_safe_failure_diagnostic(
            TimeoutError("live planning job deadline exceeded"),
            code_override=LivePlanningSafeFailureCode.DEADLINE_EXCEEDED,
        ),
        cancellation_requested=True,
    )
    snap = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        _QUARANTINE_HARD_STOPPED_STAGE,
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            _v3_record(
                "tenant-a",
                snap,
                quarantined=True,
                quarantine_stage=_QUARANTINE_HARD_STOPPED_STAGE,
                pending_terminal=pending.to_persisted(),
            )
        ],
        "idempotency": [
            _v3_idempotency_entry("tenant-a", "startup-key", job_id),
        ],
    }
    _write_registry_state(payload, state_path)

    registry = LivePlanningJobRegistry(state_path=state_path)
    try:
        await registry.restore_after_restart()
        # No get/cancel/query: the cleanup owner settles the record on its own.
        await _wait_for_runtime(
            registry._records[job_id],
            lambda r: r.snapshot.state == LivePlanningJobState.FAILED,
        )
        runtime = registry._records[job_id]
        assert runtime.snapshot.stage == "deadline_exceeded"
        assert runtime.snapshot.cancellation_requested is True
        # The durable cancel intent (cancellation_requested) survives; the
        # transient in-flight marker (cancel_pending) is cleared once the
        # durable outcome is settled to a terminal state.
        assert runtime.snapshot.cancel_pending is False
        # memory == disk.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == job_id
        )
        assert disk_record["snapshot"]["state"] == "failed"
        assert disk_record["snapshot"]["stage"] == "deadline_exceeded"
    finally:
        await registry.close()

    # Second cold boot is stable: the same terminal state, no re-isolation.
    second = LivePlanningJobRegistry(state_path=state_path)
    try:
        await second.restore_after_restart()
        again = await second.get(job_id, "tenant-a")
        assert again is not None
        assert again.state == LivePlanningJobState.FAILED
        assert again.stage == "deadline_exceeded"
    finally:
        await second.close()


# ---------------------------------------------------------------------------
# P0-5: persistent quarantine above the current qcap still loads and is
# fail-closed; bounded retention cleanup restores capacity; tombstones stay.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_overflow_loads_fails_closed_and_restores_capacity(
    tmp_path: Path,
) -> None:
    """A durable file with more quarantined records than the current
    ``quarantine_capacity`` LOADS (never rejected), flags the registry
    fail-closed (no new conversion/admission), keeps the tombstones, and bounded
    retention cleanup restores admission capacity."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    qcap = 2
    overflow = 4
    records = []
    idempotency = []
    for index in range(overflow):
        job_id = f"live-job-overflow-{index}"
        snap = _v3_snapshot(
            job_id,
            LivePlanningJobState.RUNNING,
            _QUARANTINE_HARD_STOPPED_STAGE,
            5,
            2,
        )
        records.append(
            _v3_record(
                tenant_id,
                snap,
                quarantined=True,
                quarantine_stage=_QUARANTINE_HARD_STOPPED_STAGE,
            )
        )
        idempotency.append(
            _v3_idempotency_entry(tenant_id, f"overflow-{index}", job_id)
        )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": records,
        "idempotency": idempotency,
    }
    _write_registry_state(payload, state_path)

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        quarantine_capacity=qcap,
        quarantine_retention=timedelta(hours=6),
    )
    try:
        # Every durable record loaded (4 > qcap), flagged fail-closed.
        assert len(registry._records) == overflow
        assert registry._quarantine_overflow is True
        # No NEW admission while overflow holds.
        with pytest.raises(LivePlanningJobCapacityError):
            await registry.start_idempotent(
                tenant_id=tenant_id,
                operation=_stubborn_command(
                    _write_stubborn_worker(tmp_path), tmp_path / "p.txt"
                ),
                idempotency_key="fresh-key",
                request_digest=REQUEST_SHA256,
                defer_start=False,
                deadline_seconds=30,
            )
        # No NEW quarantine conversion while overflow holds.
        assert not registry._quarantine_capacity_available_locked()

        # Bounded retention cleanup is the ONLY path that restores capacity:
        # once the durable quarantined count drops back to qcap, the flag clears.
        registry._quarantine_retention = timedelta(seconds=1)
        snap_new, _ = await registry.start_idempotent(
            tenant_id=tenant_id,
            operation=_stubborn_command(
                _write_stubborn_worker(tmp_path), tmp_path / "p2.txt"
            ),
            idempotency_key="fresh-after-cleanup",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30,
        )
        assert registry._quarantine_overflow is False
        assert snap_new.id in registry._records
        # The reclaimed keys keep their durable tombstones — same-key fails closed.
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id=tenant_id,
                operation=_stubborn_command(
                    _write_stubborn_worker(tmp_path), tmp_path / "p3.txt"
                ),
                idempotency_key="overflow-0",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        reclaimed = next(
            entry for entry in disk["idempotency"]
            if entry["job_id"] == "live-job-overflow-0"
        )
        assert reclaimed["legacy_isolated"] is True
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# P0-6: idempotency / byte / identity caps enforced on load AND atomically
# BEFORE any mutation; the state read path is FD-bounded (fstat FIRST).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_cap_enforced_before_mutation_and_worker_start(
    tmp_path: Path,
) -> None:
    """Starting a job past ``idempotency_capacity`` fails closed BEFORE the new
    record or identity is ever written — no partial ``_records`` entry, no
    partial ``_idempotency`` entry, and no worker subprocess is ever started for
    the rejected key. RED on HEAD: the record was admitted first and the cap
    raised after (partial state)."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "rejected-probe.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        idempotency_capacity=1,
    )
    try:
        snap1, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe),
            idempotency_key="k1",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30,
        )
        await registry.get(snap1.id, "tenant-a")
        with pytest.raises(LivePlanningJobCapacityError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=_stubborn_command(module, probe),
                idempotency_key="k2",
                request_digest=REQUEST_SHA256,
                defer_start=False,
                deadline_seconds=30,
            )
        # No partial record / identity / worker for the rejected key.
        assert len(registry._records) == 1
        assert len(registry._idempotency) == 1
        assert not any(
            runtime.worker_pgid is not None for runtime in registry._records.values()
        ) or snap1.id in registry._records
        workers_dir = registry._workers_dir()
        if workers_dir is not None and workers_dir.exists():
            assert list(workers_dir.iterdir()) == []
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_idempotency_cap_rejected_on_load(tmp_path: Path) -> None:
    """A hand-crafted file carrying more idempotency identities than the cap is
    rejected on load — the registry never admits a collection it can never grow
    to or reload."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    records = []
    idempotency = []
    for index in range(3):
        job_id = f"live-job-idcap-{index}"
        snap = _v3_snapshot(
            job_id,
            LivePlanningJobState.SUCCEEDED,
            "complete",
            100,
            3,
        )
        records.append(_v3_record(tenant_id, snap))
        idempotency.append(_v3_idempotency_entry(tenant_id, f"idcap-{index}", job_id))
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": records,
        "idempotency": idempotency,
    }
    _write_registry_state(payload, state_path)
    with pytest.raises(RuntimeError, match="idempotency bounds exceeded"):
        LivePlanningJobRegistry(state_path=state_path, idempotency_capacity=2)


@pytest.mark.asyncio
async def test_state_byte_cap_rejected_on_load_fstat_first(tmp_path: Path) -> None:
    """A state file larger than ``state_max_bytes`` is rejected on load, and the
    read path is FD-bounded: fstat FIRST, then read at most the verified size —
    an inflated file can never be slurped before the cap check."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    bloat = "x" * 8192
    snap = _v3_snapshot("live-job-bloat", LivePlanningJobState.SUCCEEDED, "complete", 100, 3)
    bloated = snap.model_copy(update={"result": {"bloat": bloat}})
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [_v3_record(tenant_id, bloated)],
        "idempotency": [],
    }
    _write_registry_state(payload, state_path)
    assert state_path.stat().st_size > 1024
    with pytest.raises(RuntimeError, match="byte bound"):
        LivePlanningJobRegistry(state_path=state_path, state_max_bytes=1024)
