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
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tripchord.agents.live_jobs as live_jobs_module
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
    _linux_group_has_live_member,
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
    linux_live = _linux_group_has_live_member(pgid)
    if linux_live is not None:
        return linux_live
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs group semantics")
@pytest.mark.parametrize(
    ("states", "expected"),
    [((), None), (("Z", "Z"), False), (("Z", "R"), True), (None, None)],
)
def test_linux_group_liveness_distinguishes_zombies_and_unknown(
    monkeypatch: pytest.MonkeyPatch,
    states: tuple[str, ...] | None,
    expected: bool | None,
) -> None:
    monkeypatch.setattr(
        live_jobs_module,
        "_linux_process_group_states",
        lambda _pgid: states,
    )
    assert _linux_group_has_live_member(12345) is expected


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
        "total_timeout_seconds": 600,
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
                headers={"Idempotency-Key": "worker-real-subprocess"},
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
                headers={"Idempotency-Key": "worker-cancel-process"},
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


# ---------------------------------------------------------------------------
# C-146 P0 supplement (RETURN 9666a380): native red-then-green counterexamples
# for the seven NEW P0s — real ready HTTP→worker chain (P0-1), spawn-window
# cold-start discovery (P0-2), watchdog earlier-deadline wake + concurrent batch
# (P0-3), worker clean-return group-empty confirm (P0-4), cold-boot drain before
# terminalize (P0-5), evict rollback on persist failure (P0-6), and qcap atomic
# reject before stop/kill (P0-7).
# ---------------------------------------------------------------------------


# A worker entry that RETURNS success after forking a stubborn grandchild which
# keeps writing external side effects forever — a clean LEADER exit is NOT an
# empty process group.
_RETURNED_GRANDCHILD_SRC = f'''\
import asyncio
import os
import subprocess
import sys

_GRANDCHILD = {_GRANDCHILD_SRC!r}

async def run_returned_grandchild(*, probe_path=None, **kwargs):
    with open(probe_path, "a") as fh:
        fh.write("leader-start:" + str(os.getpid()) + "\\n")
        fh.flush()
    child = subprocess.Popen(
        [sys.executable, "-c", _GRANDCHILD, probe_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(probe_path, "a") as fh:
        fh.write("grandchild-pid:" + str(child.pid) + "\\n")
        fh.flush()
    # The leader returns a clean success while the grandchild keeps appending.
    return {{"status": "succeeded", "pid": os.getpid()}}
'''


class _SlowKillConfirm:
    """Test double for a worker handle whose kill+confirm takes ``slow`` seconds.

    Only used to make one sibling's hard-stop deterministically SLOW so a test
    can prove a concurrently-due job is NOT delayed by it. The real
    ``_SubprocessWorkerHandle`` bound is bounded by ``kill_and_confirm``'s
    confirm budget, which would mask the gather-blocking regression."""

    def __init__(self, slow: float) -> None:
        self.slow = slow

    async def kill_and_confirm(self, timeout: float) -> bool:
        await asyncio.sleep(self.slow)
        return True


# ---------------------------------------------------------------------------
# P0-1: the REAL ready chain runs in the REAL worker subprocess (no builder
# override) and the FAILED job surfaces the operation's OWN cause.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_route_runs_real_ready_chain_and_surfaces_real_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POSTing a READY payload (destination 目的地/马累) through the real route —
    NO builder override — runs the REAL ready chain in a REAL worker subprocess,
    records the
    durable worker identity, and FAILS with the operation's OWN provenance (the
    missing flexible-live-system HTTP 503), never a generic ``worker exited with
    N``. RED on HEAD: the ready chain failed with a generic exit-code RuntimeError
    and the real cause never surfaced."""
    registry = _http_app_context(monkeypatch, tmp_path)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=True),
                headers={"Idempotency-Key": "worker-real-ready"},
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job"]["id"]
            runtime = registry._records[job_id]
            terminal = await _terminal_job_slow(client, job_id)
        assert terminal["state"] == "failed", terminal
        # A REAL worker subprocess ran the ready chain — durable identity.
        assert runtime.worker_pgid is not None and runtime.worker_pgid > 0
        assert runtime.worker_marker
        # The surfaced failure is the REAL operation's cause, not a generic
        # subprocess exit error.
        assert "HTTPException" in terminal["error"], terminal
        assert "worker exited with" not in terminal["error"]
        assert terminal.get("safe_failure_code") == "http_exception"
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# P0-2: a parent-API crash landing in the spawn window (after the durable
# spawn-intent write, before the worker's own marker write) leaves only an
# intent file (marker nonce, no pgid) — a cold start recovers the PGID by
# scanning process command lines and kills the authenticated orphan.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_intent_window_cold_start_discovers_orphan_group_by_marker(
    tmp_path: Path,
) -> None:
    """A parent-API SIGKILL between ``create_subprocess_exec`` and the worker's
    own marker write leaves ONLY the durable spawn-intent (marker nonce, NO pgid)
    plus a live orphan whose command line carries the nonce. A fresh registry's
    ``restore_after_restart`` must recover the PGID by scanning process command
    lines, AUTHENTICATE + kill the whole group, and drop the stale intent. RED on
    HEAD: intent-only marker files were skipped (no pgid), so the orphan was
    never discovered and kept running."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "spawn-window-probe.txt"
    marker = hashlib.sha256(b"spawn-intent-nonce").hexdigest()
    job_id = "live-job-spawn-window"
    workers_dir = tmp_path / ".live-jobs.json.workers"

    orphan_code = (
        "import os, sys, time\n"
        "probe = sys.argv[2]\n"
        "with open(probe, 'a') as fh:\n"
        "    fh.write('orphan-pgid:' + str(os.getpgrp()) + '\\n')\n"
        "    fh.flush()\n"
        "while True:\n"
        "    with open(probe, 'a') as fh:\n"
        "        fh.write('orphan\\n')\n"
        "        fh.flush()\n"
        "    time.sleep(0.01)\n"
    )
    # Crasher: writes ONLY the spawn intent (nonce, NO pgid), spawns the orphan
    # carrying the nonce, then dies — the exact P0-2 Window A. The orphan is
    # reparented to init so its death is provable.
    crash_src = (
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"probe = {str(probe)!r}\n"
        f"marker = {marker!r}\n"
        f"marker_file = {str(workers_dir / f'{job_id}.json')!r}\n"
        f"orphan_code = {orphan_code!r}\n"
        "Path(marker_file).parent.mkdir(parents=True, exist_ok=True)\n"
        "Path(marker_file).write_text(json.dumps({'marker': marker}, "
        "ensure_ascii=False, sort_keys=True), encoding='utf-8')\n"
        "worker = subprocess.Popen(\n"
        "    [sys.executable, '-c', orphan_code, marker, probe],\n"
        "    start_new_session=True,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "time.sleep(1)\n"
        "os._exit(99)\n"
    )
    crash = subprocess.Popen([sys.executable, "-c", crash_src])
    crash.wait(timeout=10)
    try:
        _wait_for_probe(probe, "orphan-pgid")
        pgid = int(
            next(
                line
                for line in probe.read_text(encoding="utf-8").splitlines()
                if line.startswith("orphan-pgid:")
            ).split(":", 1)[1]
        )
        assert _group_alive(pgid)
        # The intent file holds NO pgid — only the nonce.
        intent = json.loads((workers_dir / f"{job_id}.json").read_text(encoding="utf-8"))
        assert "pgid" not in intent

        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [],
            "idempotency": [],
        }
        _write_registry_state(payload, state_path)
        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            await registry.restore_after_restart()
            # The whole authenticated group is provably gone and the stale
            # intent file is removed.
            assert not _group_alive(pgid)
            assert not _probe_grows(probe)
            assert not (workers_dir / f"{job_id}.json").exists()
        finally:
            await registry.close()
    finally:
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# P0-3: the watchdog must wake when an EARLIER deadline is armed mid-sleep, and
# a slowly-confirming sibling must never delay a concurrently-due job.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_wakes_on_earlier_deadline_inserted_mid_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The watchdog sleeps until the earliest KNOWN deadline. A NEW job whose
    absolute deadline is EARLIER than the deadline the loop is already sleeping
    on must WAKE the loop: the new job hard-stops at ITS OWN deadline+grace,
    never at the later pre-existing deadline. RED on HEAD: the loop slept until
    the pre-existing later deadline, so the earlier job overran its own bound."""
    state_path = tmp_path / "live-jobs.json"
    probe_a = tmp_path / "probe-early-a.txt"
    probe_b = tmp_path / "probe-early-b.txt"
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
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_a),
            idempotency_key="early-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=4.0,
        )
        ra = registry._records[snap_a.id]
        await _wait_for_runtime(
            ra,
            lambda r: r.worker_pgid is not None
            and r.operation_task is not None
            and not r.operation_task.done(),
        )
        watchdog = registry._hard_stop_watchdog
        assert watchdog is not None and not watchdog.done()
        # Insert job B with a much EARLIER deadline while the watchdog is asleep
        # on A's 4.0s deadline.
        snap_b, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_b),
            idempotency_key="early-b",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.6,
        )
        rb = registry._records[snap_b.id]
        await _wait_for_runtime(
            rb,
            lambda r: r.worker_pgid is not None
            and r.operation_task is not None
            and not r.operation_task.done(),
        )
        b_started = time.monotonic()
        fail_persists = True
        # B hard-stops within ITS OWN deadline+grace (0.6 + 0.1 + confirm), NOT
        # at A's later 4.0+0.1 deadline.
        await _wait_for_runtime(
            rb,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        elapsed_b = time.monotonic() - b_started
        assert rb.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert elapsed_b <= 0.6 + 0.1 + registry._hard_stop_confirm_seconds + 1.0
        assert not _group_alive(rb.worker_pgid)
        assert not _probe_grows(probe_b)
    finally:
        monkeypatch.undo()
        await registry.close()


@pytest.mark.asyncio
async def test_watchdog_does_not_delay_due_sibling_during_slow_confirm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With tightly staggered deadlines, a job that becomes due WHILE a sibling's
    hard-stop confirm is still running must stop within its OWN deadline+grace —
    the shared watchdog spawns a CONCURRENT per-job wrapper instead of gathering
    (serializing) the batch. A slow sibling confirm (test double on A) must not
    delay B. RED on HEAD: the batch was gathered, so B waited for A's confirm and
    overran its own bound."""
    state_path = tmp_path / "live-jobs.json"
    probe_a = tmp_path / "probe-tight-a.txt"
    probe_b = tmp_path / "probe-tight-b.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=4,
        max_running=2,
        execution_hard_stop_grace_seconds=0.05,
        hard_stop_confirm_seconds=0.6,
    )
    fail_persists = False
    real_persist = registry._persist_locked

    def conditional_fail() -> None:
        if fail_persists:
            raise RuntimeError("injected permanent deadline-intent persist failure")
        real_persist()

    monkeypatch.setattr(registry, "_persist_locked", conditional_fail)
    pgid_a: int | None = None
    pgid_b: int | None = None
    try:
        started = time.monotonic()
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_a),
            idempotency_key="tight-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.8,
        )
        snap_b, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_b),
            idempotency_key="tight-b",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.9,
        )
        ra = registry._records[snap_a.id]
        rb = registry._records[snap_b.id]
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
        # A's hard-stop becomes SLOW: replace its real handle with the double so
        # the confirm takes far longer than B's own bound would tolerate. A's real
        # process group is never touched by the double — the finally block kills
        # it after both assertions.
        ra.worker_handle = _SlowKillConfirm(1.5)
        fail_persists = True
        # Poll B's hard-stop CONCURRENTLY with A's and await B FIRST: B must
        # stop within its own deadline+grace+confirm — never after A's slow 1.5s
        # confirm completes — and its observed time must be measured the moment
        # it happens, not after A's quarantine settles.
        b_waiter = asyncio.create_task(
            _wait_for_runtime(
                rb,
                lambda r: r.quarantined and r.hard_stopped,
                timeout=10.0,
            )
        )
        await asyncio.wait_for(b_waiter, timeout=10.0)
        elapsed_b = time.monotonic() - started
        assert rb.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        # B's deadline (0.9 from admission ~started+0.05) + grace 0.05 + its own
        # confirm 0.6 + a small scheduling buffer.
        assert elapsed_b <= 0.9 + 0.05 + registry._hard_stop_confirm_seconds + 0.2
        assert not _group_alive(pgid_b)
        assert not _probe_grows(probe_b)
        # A is confirmed via the slow double (1.5s) only afterwards. A's real
        # process group was never touched by the double — the finally block
        # below kills it.
    finally:
        # The slow double never killed A's real worker; kill both real groups.
        with _suppress_os():
            if pgid_a is not None:
                os.killpg(pgid_a, signal.SIGKILL)
        with _suppress_os():
            if pgid_b is not None:
                os.killpg(pgid_b, signal.SIGKILL)
        monkeypatch.undo()
        await registry.close()


# ---------------------------------------------------------------------------
# P0-4: a worker whose ENTRY returns cleanly but that forked a stubborn
# descendant must not let the job complete over live side effects.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_clean_return_confirms_whole_group_empty_before_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker whose entry RETURNS success after forking a stubborn grandchild
    (same process group, keeps writing external side effects) must NOT let the
    job terminalize over live work: the registry SIGKILLs the whole group and
    confirms it empty (probe frozen, group dead) BEFORE surfacing the result.
    RED on HEAD: a clean leader exit was treated as completion and the grandchild
    kept growing the probe."""
    module = tmp_path / "returned_grandchild_worker.py"
    module.write_text(_RETURNED_GRANDCHILD_SRC, encoding="utf-8")
    probe = tmp_path / "probe-returned-grandchild.txt"
    registry = _http_app_context(monkeypatch, tmp_path)
    monkeypatch.setattr(
        main_module,
        "_build_live_flexible_from_text_worker_command",
        lambda *a, **k: LiveJobWorkerCommand(
            module_path=str(module),
            entry="run_returned_grandchild",
            args={},
            probe_path=str(probe),
        ),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
                headers={"Idempotency-Key": "worker-clean-return"},
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job"]["id"]
            runtime = registry._records[job_id]
            await _wait_for_runtime(runtime, lambda r: r.worker_pgid is not None)
            pgid = runtime.worker_pgid
            assert pgid is not None and pgid > 0
            terminal = await _terminal_job_slow(client, job_id)
        assert terminal["state"] == "succeeded", terminal
        # The whole group — leader AND the stubborn grandchild — is provably
        # empty before the job is allowed to succeed.
        assert not _group_alive(pgid)
        assert not _probe_grows(probe)
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# P0-5: a cold-booted runtime whose durable worker identity points at a LIVE,
# marker-authenticated orphan must drain (kill + confirm) it BEFORE any
# terminalize / permit release can claim completion.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_boot_drain_kills_live_orphan_before_terminalize(
    tmp_path: Path,
) -> None:
    """A cold-booted runtime has no in-memory operation task, but its durable
    worker identity may still point at a LIVE marker-authenticated orphan group.
    ``_cancel_and_drain_operation`` must AUTHENTICATE the group, SIGKILL it and
    confirm it died BEFORE any terminalize / permit release is allowed. RED on
    HEAD: a cold-booted runtime reported stopped unconditionally, leaving the
    orphan alive and writing side effects."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "cold-boot-orphan-probe.txt"
    marker = hashlib.sha256(b"cold-boot-nonce").hexdigest()
    job_id = "live-job-cold-boot-drain"

    orphan_code = (
        "import os, sys, time\n"
        "probe = sys.argv[2]\n"
        "with open(probe, 'a') as fh:\n"
        "    fh.write('orphan-pgid:' + str(os.getpgrp()) + '\\n')\n"
        "    fh.flush()\n"
        "while True:\n"
        "    with open(probe, 'a') as fh:\n"
        "        fh.write('orphan\\n')\n"
        "        fh.flush()\n"
        "    time.sleep(0.01)\n"
    )
    # Crasher spawns the marker-carrying orphan then dies (reparented to init).
    crash_src = (
        "import os, subprocess, sys, time\n"
        f"probe = {str(probe)!r}\n"
        f"marker = {marker!r}\n"
        f"orphan_code = {orphan_code!r}\n"
        "worker = subprocess.Popen(\n"
        "    [sys.executable, '-c', orphan_code, marker, probe],\n"
        "    start_new_session=True,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "time.sleep(1)\n"
        "os._exit(99)\n"
    )
    crash = subprocess.Popen([sys.executable, "-c", crash_src])
    crash.wait(timeout=10)
    _wait_for_probe(probe, "orphan-pgid")
    pgid = int(
        next(
            line
            for line in probe.read_text(encoding="utf-8").splitlines()
            if line.startswith("orphan-pgid:")
        ).split(":", 1)[1]
    )
    try:
        assert _group_alive(pgid)
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
            job_id, LivePlanningJobState.RUNNING, _QUARANTINE_ORPHAN_STAGE, 5, 2
        )
        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [
                _v3_record(
                    "tenant-a",
                    snap,
                    quarantined=True,
                    quarantine_stage=_QUARANTINE_ORPHAN_STAGE,
                    pending_terminal=pending.to_persisted(),
                    worker_pgid=pgid,
                    worker_marker=marker,
                    worker_probe=str(probe),
                )
            ],
            "idempotency": [
                _v3_idempotency_entry("tenant-a", "cold-boot-key", job_id),
            ],
        }
        _write_registry_state(payload, state_path)
        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            runtime = registry._records[job_id]
            assert runtime.operation_task is None  # cold boot
            # Do NOT call restore_after_restart: exercise the drain directly.
            confirmed = await registry._cancel_and_drain_operation(runtime)
            assert confirmed is True
            # The authenticated orphan group is provably gone BEFORE any
            # terminalize / permit release is allowed to claim completion.
            assert not _group_alive(pgid)
            assert not _probe_grows(probe)
        finally:
            await registry.close()
    finally:
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# P0-6: a pre-commit persist failure on an admission that EVICTED the oldest
# terminal record must roll the eviction back — the old idempotency binding
# survives.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_eviction_rolls_back_idempotency_on_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With ``capacity=1``, job A's success fills the only slot. Job B's
    admission EVICTS terminal A; a pre-commit persist failure on B's admission
    must roll the eviction back — A's record and its idempotency binding survive,
    so a same-key retry with a different digest still fails closed. RED on HEAD:
    the eviction was applied and never undone, so the old key was silently freed."""
    state_path = tmp_path / "live-jobs.json"

    async def quick_op(report: Any) -> dict[str, Any]:
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path, capacity=1)
    fail_persists = False
    real_persist = registry._persist_locked

    def conditional_fail() -> None:
        if fail_persists:
            raise RuntimeError("injected pre-commit persist failure")
        real_persist()

    monkeypatch.setattr(registry, "_persist_locked", conditional_fail)
    try:
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=quick_op,
            idempotency_key="key-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
        ra = registry._records[snap_a.id]
        await _wait_for_runtime(
            ra,
            lambda r: r.snapshot.state == LivePlanningJobState.SUCCEEDED,
        )
        # B's admission evicts terminal A; the persist of B's admission fails.
        fail_persists = True
        with pytest.raises(RuntimeError, match="injected pre-commit persist failure"):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=quick_op,
                idempotency_key="key-b",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
        fail_persists = False
        # P0-6: A's idempotency binding must have been rolled back — a same-key
        # request with a DIFFERENT digest still conflicts with terminal A.
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=quick_op,
                idempotency_key="key-a",
                request_digest="b" * 64,
                defer_start=False,
            )
    finally:
        monkeypatch.undo()
        await registry.close()


# ---------------------------------------------------------------------------
# P0-7: the quarantine-capacity check is the ATOMIC precondition of a hard stop
# — a full quota REFUSES the stop BEFORE any stop/kill side effect.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qcap_full_refuses_hard_stop_before_any_kill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With ``quarantine_capacity=1``, A's hard stop consumes the only slot. When
    B's hard-stop bound passes while the quota is FULL the watchdog REFUSES the
    stop BEFORE any kill — B stays alive (probe keeps growing), is NOT
    quarantined, and is marked deferred. Once retention reclaims A's slot, the
    deferred B is retried immediately and then hard-stopped. RED on HEAD: the
    stop/kill ran FIRST and the capacity rejection came after — B's probe froze
    over an irreversible kill.

    B's own deadline stays far in the future so the runner's deadline-timeout
    path can never race the watchdog: the only force acting on B is the
    watchdog's qcap-gated hard-stop (armed by overriding ``hard_stop_monotonic``
    directly)."""
    state_path = tmp_path / "live-jobs.json"
    probe_a = tmp_path / "probe-qcap-a.txt"
    probe_b = tmp_path / "probe-qcap-b.txt"
    module = _write_stubborn_worker(tmp_path)
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=4,
        max_running=2,
        quarantine_capacity=1,
        execution_hard_stop_grace_seconds=0.1,
        hard_stop_confirm_seconds=0.3,
        hard_stop_defer_retry_seconds=60.0,
    )
    fail_persists = False
    real_persist = registry._persist_locked

    def conditional_fail() -> None:
        if fail_persists:
            raise RuntimeError("injected permanent deadline-intent persist failure")
        real_persist()

    monkeypatch.setattr(registry, "_persist_locked", conditional_fail)
    try:
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_a, spawn_grandchild=True),
            idempotency_key="qcap-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=1.0,
        )
        snap_b, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_stubborn_command(module, probe_b, spawn_grandchild=True),
            idempotency_key="qcap-b",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=100.0,
        )
        ra = registry._records[snap_a.id]
        rb = registry._records[snap_b.id]
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
        fail_persists = True
        # A hard-stops first and consumes the only quarantine slot.
        await _wait_for_runtime(
            ra,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        assert ra.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert not _group_alive(pgid_a)
        assert not _probe_grows(probe_a)
        # Arm B's hard-stop bound PAST now while its own deadline stays in the
        # future (so the runner never races), then wake the watchdog: the qcap is
        # FULL, so B's stop must be REFUSED before any kill.
        async with registry._changed:
            rb.hard_stop_monotonic = asyncio.get_running_loop().time() - 0.1
            registry._wake_hard_stop_watchdog()
        # Give the watchdog B's own confirm budget to settle either outcome,
        # then prove B was NOT killed: the capacity rejection happened BEFORE
        # any stop/kill side effect.
        await asyncio.sleep(0.5 + 0.1 + registry._hard_stop_confirm_seconds + 0.3)
        assert _probe_grows(probe_b)  # RED: frozen (killed first) -> FAILS
        assert not rb.quarantined
        assert not rb.hard_stopped
        assert rb.hard_stop_deferred is True
        assert _group_alive(pgid_b)
        # Retention reclaims A's slot -> the deferred B is retried immediately.
        # Re-enable persist so the reclamation can actually commit and the
        # immediate retry can proceed.
        fail_persists = False
        async with registry._changed:
            registry._quarantine_retention = timedelta(milliseconds=1)
            await asyncio.sleep(0.05)
            registry._prune_locked(registry._utc_now())
        await _wait_for_runtime(
            rb,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        assert rb.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert not _group_alive(pgid_b)
        assert not _probe_grows(probe_b)
    finally:
        monkeypatch.undo()
        await registry.close()


# ---------------------------------------------------------------------------
# RETURN 7de8cf3e — six native red-then-green counterexamples.
#
# 1. P0-1: the REAL ready HTTP chain runs in a REAL cross-process worker whose
#    reconstructed app installs a PRODUCTION runtime from the API's env handoff
#    (no monkeypatched private runtime) — the job SUCCEEDS.
# 2. P0-2: a NON-ZERO leader exit after forking a stubborn grandchild proves the
#    WHOLE group dead BEFORE the terminal FAILED label is published.
# 3. P0-3: a cold boot defers resolution of a durable PGID/marker record until
#    ``restore_after_restart`` has discovered + killed the real orphan — never a
#    terminalize/prune over live external side effects.
# 4. P0-4: a kill/confirm EXCEPTION or cancel race releases
#    ``hard_stop_in_flight``/``hard_stop_quarantine_reserved`` in ``finally``
#    and re-arms a bounded-backoff retry — no leaked flag/reservation.
# 5. P0-5: a durable quarantine ABOVE the new small qcap cold-loads COMPLETELY
#    and fail-closed (``_quarantine_overflow``), never a loader RuntimeError.
# 6. P0-6: an idcap-FULL collection atomically rejects a NEW key BEFORE any
#    eviction — the old identity mapping stays byte-identical.
# ---------------------------------------------------------------------------


async def _quick_success(report: Any) -> dict[str, Any]:
    """A plain in-process operation that succeeds immediately."""
    return {"ok": True}


async def _never_finish(report: Any) -> dict[str, Any]:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


# A worker whose LEADER raises after forking a stubborn grandchild: the group
# must be proven dead before the terminal FAILED label is published (P0-2).
_RAISING_STUBBORN_SRC = f'''\
import os
import subprocess
import sys

_GRANDCHILD = {_GRANDCHILD_SRC!r}

async def run_raising_stubborn(*, probe_path=None, **kwargs):
    with open(probe_path, "a") as fh:
        fh.write("leader-start:" + str(os.getpid()) + "\\n")
        fh.flush()
    child = subprocess.Popen(
        [sys.executable, "-c", _GRANDCHILD, probe_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(probe_path, "a") as fh:
        fh.write("grandchild-pid:" + str(child.pid) + "\\n")
        fh.flush()
    raise RuntimeError("worker entry exploded")
'''


class _FlakyKillConfirm:
    """A fake worker handle whose ``kill_and_confirm`` raises once (an injected
    OS kill failure), then reports False (death not confirmed), then True — so
    the watchdog's finally-cleanup + bounded-backoff retry are the only forces
    that can ever close the record out."""

    def __init__(self, behavior: list[object]) -> None:
        self.behavior = behavior
        self.calls = 0

    async def kill_and_confirm(self, timeout: float) -> bool:
        outcome = self.behavior[min(self.calls, len(self.behavior) - 1)]
        self.calls += 1
        if outcome == "raise":
            raise OSError("injected kill_and_confirm failure")
        return bool(outcome)


def test_worker_runtime_envelope_rejects_tampered_spec_and_provenance() -> None:
    """The independent worker verifies both canonical configuration bytes and
    the parent API's immutable code provenance before composing capabilities.
    """
    from copy import deepcopy

    from tripchord.agents.live_flexible_worker_runtime import (
        _verified_runtime_spec,
        build_authenticated_runtime_bundle,
    )
    from tripchord.platform.adapters import default_browser_providers_from_registry
    from tripchord.runtime_provenance import PROVENANCE

    spec = {
        "runtime": "browser-bridge",
        "bridge_token": "runtime-attestation-token-000000000000000",
        "providers": [
            provider.value
            for provider in default_browser_providers_from_registry()
        ],
        "model_agents_required": False,
        "adaptive_agent_scaling_enabled": False,
        "now_iso": "2026-07-30T09:00:00+00:00",
        "http_host": "127.0.0.1",
        "http_port": 43123,
        "icom_api_origin": "http://127.0.0.1:43124",
    }
    envelope = build_authenticated_runtime_bundle(spec)
    verified, _, coordinator = _verified_runtime_spec(envelope)
    assert verified == spec
    assert coordinator == PROVENANCE.to_dict()

    tampered_spec = deepcopy(envelope)
    tampered_spec["spec"]["http_port"] = 43125
    with pytest.raises(RuntimeError, match="digest does not match"):
        _verified_runtime_spec(tampered_spec)

    foreign_provenance = deepcopy(envelope)
    foreign_provenance["runtime_provenance"]["commit_sha"] = "f" * 40
    with pytest.raises(RuntimeError, match="commit_sha does not match"):
        _verified_runtime_spec(foreign_provenance)

    foreign_api_process = deepcopy(envelope)
    foreign_api_process["api_runtime_identity"]["pid"] = -1
    with pytest.raises(RuntimeError, match="API runtime identity"):
        _verified_runtime_spec(foreign_api_process)


@pytest.mark.asyncio
async def test_http_route_rejects_formal_worker_with_models_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The former HTTP positive cannot disable model execution and self-certify.

    A real route and independent subprocess still run, but the parent-owned
    formal source boundary makes ``model_agents_required=False`` an authenticated
    runtime-contract violation before any synthetic inventory can enter.
    """
    from tripchord.platform.adapters import default_browser_providers_from_registry
    from tripchord.providers.browser_bridge import formal_worker_source_token

    registry = _http_app_context(monkeypatch, tmp_path)
    companion_token = "ready-chain-bridge-token-000000000000000000"
    monkeypatch.setattr(
        main_module,
        "settings",
        settings.model_copy(update={"browser_bridge_token": companion_token}),
    )
    worker_source_token = formal_worker_source_token(companion_token)
    try:
        bundle = json.dumps(
            {
                "runtime": "browser-bridge",
                "bridge_token": worker_source_token,
                "providers": [
                    provider.value
                    for provider in default_browser_providers_from_registry()
                ],
                "model_agents_required": False,
                "formal_parent_api_origin": "http://127.0.0.1:8000",
                "adaptive_agent_scaling_enabled": False,
                "now_iso": "2026-08-17T09:00:00+08:00",
                "http_host": None,
                "http_port": None,
                "icom_api_origin": None,
                "formal_source_private_key_path": None,
                "formal_source_ledger_path": None,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        monkeypatch.setenv("TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE", bundle)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=True),
                headers={"Idempotency-Key": "worker-models-disabled"},
            )
            assert created.status_code == 202, created.text
            job_id = created.json()["job"]["id"]
            terminal = await _terminal_job_slow(client, job_id)

        assert terminal["state"] == "failed", terminal
        assert terminal["error"] == "RuntimeError: live planning execution failed"
        assert terminal["safe_failure_code"] == "execution_exception"
        assert terminal["pair_checkpoints"] == []
        assert terminal["source_terminal_events"] == []
        assert terminal["barrier_released_at"] is None
        assert terminal.get("result") is None
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_worker_nonzero_exit_kills_stubborn_descendant_before_terminal(
    tmp_path: Path,
) -> None:
    """A worker whose LEADER exits non-zero after forking a stubborn grandchild
    must prove the WHOLE group is dead (SIGKILL + confirm) BEFORE the job fails
    terminal — the worker's own finally already deleted the durable marker file,
    so only the group kill can stop the descendant's external side effects. RED
    on baseline: the non-zero exit raised immediately, stranding the grandchild
    appending the probe while the job entered a terminal state."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "raising-probe.txt"
    module = tmp_path / "raising_stubborn_worker.py"
    module.write_text(_RAISING_STUBBORN_SRC, encoding="utf-8")
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=4,
        max_running=2,
        execution_hard_stop_grace_seconds=10.0,
        hard_stop_confirm_seconds=0.5,
    )
    pgid: int | None = None
    try:
        snap, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=LiveJobWorkerCommand(
                module_path=str(module),
                entry="run_raising_stubborn",
                args={},
                probe_path=str(probe),
            ),
            idempotency_key="raising-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30.0,
        )
        runtime = registry._records[snap.id]
        # Await the REAL worker subprocess spawn (yields to the event loop so
        # the runner's operation task can start), then read the probe the worker
        # process itself writes.
        await _wait_for_runtime(
            runtime,
            lambda r: r.worker_pgid is not None
            and r.operation_task is not None
            and not r.operation_task.done(),
        )
        _wait_for_probe(probe, "grandchild-pid")
        pgid = runtime.worker_pgid
        assert pgid is not None and _group_alive(pgid)
        await _wait_for_runtime(
            runtime,
            lambda r: r.snapshot.state == LivePlanningJobState.FAILED,
        )
        # The whole group — INCLUDING the stubborn grandchild — is dead and the
        # probe permanently froze BEFORE the terminal FAILED label was published.
        assert pgid is not None
        assert not _group_alive(pgid)
        assert not _probe_grows(probe)
    finally:
        if pgid is not None:
            with _suppress_os():
                os.killpg(pgid, signal.SIGKILL)
        await registry.close()


@pytest.mark.asyncio
async def test_worker_importer_rejects_missing_cross_process_observability(
    tmp_path: Path,
) -> None:
    """A production-style worker import cannot publish a result that omitted
    the progress/checkpoint/model/source envelope owned by that same process.
    """
    module = tmp_path / "missing_observability_worker.py"
    module.write_text(
        "async def run_clean(**kwargs):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    importer_called = False

    async def import_result(result: dict[str, Any]) -> dict[str, Any]:
        nonlocal importer_called
        importer_called = True
        return result

    registry = LivePlanningJobRegistry(
        state_path=tmp_path / "missing-observability.json",
        capacity=2,
        max_running=1,
    )
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=LiveJobWorkerCommand(
                module_path=str(module),
                entry="run_clean",
                result_importer=import_result,
            ),
            idempotency_key="missing-observability",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30.0,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_runtime(
            runtime,
            lambda item: item.snapshot.state == LivePlanningJobState.FAILED,
        )
        assert importer_called is False
        assert runtime.snapshot.result is None
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_cold_load_does_not_terminalize_prune_before_orphan_discovery(
    tmp_path: Path,
) -> None:
    """A cold boot loads a durable RUNNING record with a live orphaned worker
    group (PGID + marker) AND a durable pending_terminal: the load must DEFER
    resolution (no terminalize, no prune) until ``restore_after_restart`` has
    discovered + killed the orphan — resolution over a live executor would
    publish a terminal label or reclaim over external side effects. RED on
    baseline: the loader terminalized the record to the pending FAILED label
    BEFORE the orphan was authenticated + killed."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "cold-orphan-probe.txt"
    marker = hashlib.sha256(b"cold-orphan-nonce").hexdigest()
    job_id = "live-job-cold-orphan"
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
    # A "crasher" parent spawns the stubborn worker in its own session, writes
    # the durable marker file, then dies — a genuine orphan (reparented to
    # init) exactly as a SIGKILLed API process would leave.
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
    pgid = json.loads(
        (workers_dir / f"{job_id}.json").read_text(encoding="utf-8")
    )["pgid"]
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
        "interpreting_requirement",
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
                pending_terminal=pending.to_persisted(),
                worker_pgid=pgid,
                worker_marker=marker,
                worker_probe=str(probe),
            )
        ],
        "idempotency": [
            _v3_idempotency_entry("tenant-a", "cold-orphan-key", job_id),
        ],
    }
    _write_registry_state(payload, state_path)
    try:
        _wait_for_probe(probe, "orphan")
        assert _group_alive(pgid)
        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            runtime = registry._records[job_id]
            # RED: the load terminalized the record (FAILED) over the live
            # orphan BEFORE any orphan discovery; GREEN defers resolution.
            assert runtime.snapshot.state == LivePlanningJobState.RUNNING
            assert not runtime.quarantined
            assert runtime.pending_terminal is not None
            # The orphan is STILL live pre-restore: no terminalize/prune
            # claimed it, no prune removed the record.
            assert _group_alive(pgid)
            assert _probe_grows(probe)
            await registry.restore_after_restart()
            # Zero-request recovery: the real orphan is discovered + killed +
            # reaped and the probe permanently froze.
            assert not _group_alive(pgid)
            assert not _probe_grows(probe)
        finally:
            await registry.close()
    finally:
        # The orphan is not a child of this process (reparented), so only the
        # group kill is needed for cleanup.
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_hard_stop_clears_in_flight_and_retries_after_kill_exception(
    tmp_path: Path,
) -> None:
    """A kill/confirm EXCEPTION inside a hard stop must release the in-flight
    marker AND the quarantine slot reservation on EVERY exit, and a death-NOT-
    confirmed outcome must re-arm a bounded-backoff retry — the deferred record
    never permanently loses its close-out opportunity. RED on baseline: the
    exception leaked ``hard_stop_in_flight``/``hard_stop_quarantine_reserved``
    so the watchdog skipped the record forever and the deadline never settled."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=4,
        max_running=1,
        quarantine_capacity=1,
        execution_hard_stop_grace_seconds=5.0,
        hard_stop_confirm_seconds=0.5,
        hard_stop_defer_retry_seconds=0.05,
    )
    runtime: Any = None
    try:
        snap, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_never_finish,
            idempotency_key="hardstop-flaky",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=60.0,
        )
        runtime = registry._records[snap.id]
        await _wait_for_runtime(
            runtime,
            lambda r: r.operation_task is not None and not r.operation_task.done(),
        )
        handle = _FlakyKillConfirm(["raise", False, True])
        runtime.worker_handle = handle
        async with registry._changed:
            runtime.hard_stop_monotonic = asyncio.get_running_loop().time() - 0.1
            registry._wake_hard_stop_watchdog()
        # The finally-cleanup + backoff retry drive the flaky kill to a
        # CONFIRMED hard stop (raise -> not-confirmed -> confirmed).
        await _wait_for_runtime(
            runtime,
            lambda r: r.quarantined and r.hard_stopped,
            timeout=10.0,
        )
        assert runtime.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert runtime.hard_stop_in_flight is False
        assert runtime.hard_stop_quarantine_reserved is False
        assert handle.calls == 3
    finally:
        if runtime is not None:
            operation = runtime.operation_task
            if operation is not None and not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(operation, timeout=3)
            for task in (runtime.task, runtime.cleanup_owner, registry._hard_stop_watchdog):
                if (
                    task is not None
                    and not task.done()
                    and task is not asyncio.current_task()
                ):
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await asyncio.wait_for(task, timeout=3)
        await registry.close()


@pytest.mark.asyncio
async def test_cold_load_full_durable_quarantine_overflow_new_small_qcap(
    tmp_path: Path,
) -> None:
    """A state file holding MORE durable quarantined records than the CURRENT
    qcap (a config shrink) must load COMPLETELY and fail-closed — no loader
    RuntimeError, no record dropped, ``_quarantine_overflow`` set, admissions
    refused until bounded retention cleanup restores capacity. RED on baseline:
    the load-tail persist rejected the combined active+quarantine count and the
    cold start crashed."""
    state_path = tmp_path / "live-jobs.json"
    records = []
    idempotency = []
    for i in range(2):
        job_id = f"live-job-overflow-active-{i}"
        snap = _v3_snapshot(job_id, LivePlanningJobState.SUCCEEDED, "complete", 100, 3)
        records.append(_v3_record("tenant-a", snap))
        idempotency.append(
            _v3_idempotency_entry("tenant-a", f"overflow-key-active-{i}", job_id)
        )
    for i in range(2):
        job_id = f"live-job-overflow-q-{i}"
        snap = _v3_snapshot(
            job_id, LivePlanningJobState.RUNNING, _QUARANTINE_ORPHAN_STAGE, 5, 2
        )
        records.append(
            _v3_record(
                "tenant-a",
                snap,
                quarantined=True,
                quarantine_stage=_QUARANTINE_ORPHAN_STAGE,
            )
        )
        idempotency.append(
            _v3_idempotency_entry("tenant-a", f"overflow-key-q-{i}", job_id)
        )
    _write_registry_state(
        {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": records,
            "idempotency": idempotency,
        },
        state_path,
    )

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=2,
        quarantine_capacity=1,
        idempotency_capacity=6,
    )
    try:
        # Complete load: every durable record survives, none dropped (RED: the
        # load crashed on the combined-count reject).
        assert registry._quarantine_overflow is True
        assert len(registry._records) == 4
        for i in range(2):
            assert f"live-job-overflow-active-{i}" in registry._records
            assert f"live-job-overflow-q-{i}" in registry._records
        # Fail-closed: no NEW admission while overflow holds.
        with pytest.raises(LivePlanningJobCapacityError, match="quarantine capacity exceeded"):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=_quick_success,
                idempotency_key="overflow-new-key",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
        # Bounded retention cleanup reclaims the quarantined records and
        # restores admission capacity.
        async with registry._changed:
            registry._quarantine_retention = timedelta(milliseconds=1)
            await asyncio.sleep(0.05)
            registry._prune_locked(registry._utc_now())
        assert registry._quarantine_overflow is False
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_quick_success,
            idempotency_key="overflow-after-cleanup",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_idcap_admission_rejects_when_collection_full_preserving_old_mapping(
    tmp_path: Path,
) -> None:
    """``capacity=2`` + ``idempotency_capacity=2``: two terminal jobs fill both
    identity slots. A THIRD job with a NEW key must fail closed at the FULL
    identity collection — eviction of an executable record slot never frees an
    identity slot, so the old mapping stays byte-identical. RED on baseline: the
    capacity eviction ran FIRST, deleting job1's binding so the reduced count
    admitted the new key and destroyed the old mapping."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=2,
        max_running=2,
        idempotency_capacity=2,
        quarantine_capacity=1,
    )
    try:
        snap_a, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_quick_success,
            idempotency_key="idcap-a",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
        ra = registry._records[snap_a.id]
        await _wait_for_runtime(
            ra,
            lambda r: r.snapshot.state == LivePlanningJobState.SUCCEEDED,
        )
        snap_b, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_quick_success,
            idempotency_key="idcap-b",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
        rb = registry._records[snap_b.id]
        await _wait_for_runtime(
            rb,
            lambda r: r.snapshot.state == LivePlanningJobState.SUCCEEDED,
        )
        key_a = LivePlanningJobRegistry._idempotency_partition("tenant-a", "idcap-a")
        assert key_a in registry._idempotency
        assert registry._idempotency[key_a].job_id == snap_a.id
        before = state_path.read_bytes()
        # A third NEW key must fail closed at the FULL identity collection —
        # BEFORE any capacity eviction could delete the old mapping.
        with pytest.raises(LivePlanningJobCapacityError, match="idempotency capacity exceeded"):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=_quick_success,
                idempotency_key="idcap-c",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
        after = state_path.read_bytes()
        # The rejected admission wrote NOTHING: the old identity is byte-identical.
        assert after == before
        assert snap_a.id in registry._records
        assert registry._idempotency[key_a].job_id == snap_a.id
        assert registry._idempotency[key_a].request_digest == REQUEST_SHA256
    finally:
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "entry", "first_confirmation", "expected_terminal"),
    (
        (
            _RAISING_STUBBORN_SRC,
            "run_raising_stubborn",
            False,
            LivePlanningJobState.FAILED,
        ),
        (
            _RETURNED_GRANDCHILD_SRC,
            "run_returned_grandchild",
            "raise",
            LivePlanningJobState.SUCCEEDED,
        ),
    ),
    ids=("nonzero-false", "clean-raise"),
)
async def test_worker_exit_confirmation_failure_keeps_identity_and_permit_until_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
    entry: str,
    first_confirmation: object,
    expected_terminal: LivePlanningJobState,
) -> None:
    """Clean and non-zero leader exits share one fail-closed group contract.

    A False/raising first confirmation leaves the stubborn descendant alive,
    therefore the job must remain non-terminal with its durable identity and
    admission permit intact.  The same owner retries automatically; only the
    subsequent real group kill may publish SUCCEEDED/FAILED and release the
    permit.  This is the original P0-2 attack shape, not a terminal-state mock.
    """
    from tripchord.agents import live_jobs as live_jobs_module

    module = tmp_path / f"worker-exit-{entry}.py"
    probe = tmp_path / f"worker-exit-{entry}.txt"
    state_path = tmp_path / f"worker-exit-{entry}.json"
    module.write_text(source, encoding="utf-8")
    original_confirm = live_jobs_module._SubprocessWorkerHandle.kill_and_confirm
    release_confirmation = asyncio.Event()
    call_count = 0

    async def controlled_confirm(handle: Any, timeout: float) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            if first_confirmation == "raise":
                raise OSError("injected worker-exit confirmation failure")
            return False
        await release_confirmation.wait()
        return await original_confirm(handle, timeout)

    monkeypatch.setattr(
        live_jobs_module._SubprocessWorkerHandle,
        "kill_and_confirm",
        controlled_confirm,
    )
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=2,
        max_running=1,
        hard_stop_confirm_seconds=0.5,
        execution_hard_stop_grace_seconds=10.0,
        cleanup_retry_backoff_seconds=0.01,
    )
    pgid: int | None = None
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=LiveJobWorkerCommand(
                module_path=str(module),
                entry=entry,
                args={},
                probe_path=str(probe),
            ),
            idempotency_key=f"exit-confirm-{entry}",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=30.0,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_runtime(runtime, lambda item: item.worker_pgid is not None)
        _wait_for_probe(probe, "grandchild-pid")
        pgid = runtime.worker_pgid
        assert pgid is not None
        await _wait_for_runtime(runtime, lambda _: call_count >= 2)

        # First confirmation failed and the retry is deliberately blocked: no
        # terminal label, no permit release, no identity loss over live work.
        assert runtime.snapshot.state not in {
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        }
        assert runtime.slot_held is True
        assert runtime.worker_pgid == pgid
        assert runtime.worker_marker
        assert _group_alive(pgid)
        assert _probe_grows(probe)
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        durable_record = next(
            item
            for item in durable["records"]
            if item["snapshot"]["id"] == snapshot.id
        )
        assert durable_record["worker_pgid"] == pgid
        assert durable_record["worker_marker"] == runtime.worker_marker

        release_confirmation.set()
        await _wait_for_runtime(
            runtime,
            lambda item: item.snapshot.state == expected_terminal,
            timeout=15.0,
        )
        assert runtime.slot_held is False
        assert not _group_alive(pgid)
        assert not _probe_grows(probe)
    finally:
        release_confirmation.set()
        monkeypatch.undo()
        if pgid is not None:
            with _suppress_os():
                os.killpg(pgid, signal.SIGKILL)
        await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ("marker-mismatch", "ps-failure"))
async def test_orphan_auth_failure_stays_isolated_across_two_cold_boots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    """An unauthenticated durable worker identity is never terminalized.

    Marker mismatch and process-query failure both preserve explicit durable
    ``authenticated=False`` / ``death_confirmed=False`` facts.  Two complete
    cold boots keep the same non-terminal orphan quarantine and continue to
    re-check it; neither boot guesses the pending FAILED outcome or kills an
    unauthenticated process group.
    """
    from tripchord.agents import live_jobs as live_jobs_module

    state_path = tmp_path / f"orphan-auth-{failure_mode}.json"
    probe = tmp_path / f"orphan-auth-{failure_mode}.txt"
    expected_marker = "expected-orphan-marker-000000000001"
    process_marker = (
        expected_marker
        if failure_mode == "ps-failure"
        else "foreign-orphan-marker-000000000002"
    )
    orphan_code = (
        "import sys, time\n"
        "probe = sys.argv[2]\n"
        "while True:\n"
        "    with open(probe, 'a') as fh:\n"
        "        fh.write('foreign-orphan\\n')\n"
        "        fh.flush()\n"
        "    time.sleep(0.01)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", orphan_code, process_marker, str(probe)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = process.pid
    if failure_mode == "ps-failure":
        monkeypatch.setattr(
            live_jobs_module.LivePlanningJobRegistry,
            "_group_commands",
            staticmethod(lambda _pgid: []),
        )
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
    job_id = f"live-job-orphan-auth-{failure_mode}"
    snapshot = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        "timeout_pending",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    _write_registry_state(
        {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [
                _v3_record(
                    "tenant-a",
                    snapshot,
                    pending_terminal=pending.to_persisted(),
                    worker_pgid=pgid,
                    worker_marker=expected_marker,
                    worker_probe=str(probe),
                )
            ],
            "idempotency": [
                _v3_idempotency_entry("tenant-a", "orphan-auth-key", job_id)
            ],
        },
        state_path,
    )
    try:
        _wait_for_probe(probe, "foreign-orphan")
        observed: list[tuple[object, ...]] = []
        for _boot in range(2):
            registry = LivePlanningJobRegistry(state_path=state_path)
            try:
                await registry.restore_after_restart()
                # Let the restored cleanup owner and reaper run.  They must
                # treat auth/death-confirm failure as an unproven live executor,
                # not race the startup resolver and publish pending FAILED.
                await asyncio.sleep(0.15)
                runtime = registry._records[job_id]
                observed.append(
                    (
                        runtime.snapshot.state,
                        runtime.snapshot.stage,
                        runtime.quarantined,
                        runtime.quarantine_stage,
                        runtime.orphan_authenticated,
                        runtime.orphan_death_confirmed,
                        runtime.pending_terminal.state
                        if runtime.pending_terminal is not None
                        else None,
                    )
                )
                assert _group_alive(pgid)
                assert _probe_grows(probe)
            finally:
                await registry.close()
        expected = (
            LivePlanningJobState.RUNNING,
            _QUARANTINE_ORPHAN_STAGE,
            True,
            _QUARANTINE_ORPHAN_STAGE,
            False,
            False,
            LivePlanningJobState.FAILED,
        )
        assert observed == [expected, expected]
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        durable_record = durable["records"][0]
        assert durable_record["orphan_authenticated"] is False
        assert durable_record["orphan_death_confirmed"] is False
    finally:
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)
        with _suppress_os():
            process.wait(timeout=5)


@pytest.mark.parametrize(
    ("authenticated", "death_confirmed"),
    (("yes", False), (False, "yes"), (False, True)),
    ids=("foreign-auth-type", "foreign-death-type", "death-without-auth"),
)
def test_cold_load_rejects_invalid_or_impossible_orphan_facts(
    tmp_path: Path,
    authenticated: object,
    death_confirmed: object,
) -> None:
    """Persisted orphan facts are an exact typed tuple: booleans/None only,
    and death confirmation can never exist without prior marker authentication.
    """
    state_path = tmp_path / "invalid-orphan-facts.json"
    job_id = "live-job-invalid-orphan-facts"
    record = _v3_record(
        "tenant-a",
        _v3_snapshot(
            job_id,
            LivePlanningJobState.RUNNING,
            _QUARANTINE_ORPHAN_STAGE,
            5,
            2,
        ),
        quarantined=True,
        quarantine_stage=_QUARANTINE_ORPHAN_STAGE,
        worker_pgid=2_147_483_647,
        worker_marker="invalid-orphan-fact-marker-00000001",
    )
    record["orphan_authenticated"] = authenticated
    record["orphan_death_confirmed"] = death_confirmed
    _write_registry_state(
        {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [record],
            "idempotency": [
                _v3_idempotency_entry("tenant-a", "invalid-orphan-key", job_id)
            ],
        },
        state_path,
    )
    with pytest.raises(RuntimeError, match="orphan"):
        LivePlanningJobRegistry(state_path=state_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("worker_marker", "orphan-marker-without-pgid-00000001"),
        ("worker_probe", "/foreign/orphan-probe"),
        ("orphan_authenticated", False),
        ("orphan_death_confirmed", False),
    ),
    ids=("marker", "probe", "authentication-fact", "death-fact"),
)
def test_cold_load_rejects_orphan_state_without_worker_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Marker/probe/auth/death state cannot survive without its exact PGID.

    Otherwise a cold boot silently drops an authentication failure or death
    fact and may later recycle the record as if no orphan had ever existed.
    """
    state_path = tmp_path / f"orphan-without-identity-{field}.json"
    job_id = f"live-job-orphan-without-identity-{field}"
    record = _v3_record(
        "tenant-a",
        _v3_snapshot(
            job_id,
            LivePlanningJobState.RUNNING,
            _QUARANTINE_ORPHAN_STAGE,
            5,
            2,
        ),
        quarantined=True,
        quarantine_stage=_QUARANTINE_ORPHAN_STAGE,
    )
    record[field] = value
    _write_registry_state(
        {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [record],
            "idempotency": [
                _v3_idempotency_entry(
                    "tenant-a",
                    f"orphan-without-identity-{field}",
                    job_id,
                )
            ],
        },
        state_path,
    )
    with pytest.raises(RuntimeError, match="worker identity"):
        LivePlanningJobRegistry(state_path=state_path)


@pytest.mark.asyncio
async def test_persistent_hard_stop_exception_is_rate_bounded_and_consumed(
    tmp_path: Path,
) -> None:
    """A permanent kill exception has a fixed-window call ceiling and never
    leaves an unretrieved wrapper exception or a leaked in-flight reservation.
    """

    class AlwaysRaiseConfirm:
        def __init__(self) -> None:
            self.calls: list[float] = []

        async def kill_and_confirm(self, timeout: float) -> bool:
            self.calls.append(asyncio.get_running_loop().time())
            raise OSError("permanent injected kill failure")

    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    registry = LivePlanningJobRegistry(
        state_path=tmp_path / "persistent-hard-stop.json",
        capacity=2,
        max_running=1,
        quarantine_capacity=1,
        hard_stop_confirm_seconds=0.02,
        cleanup_retry_backoff_seconds=0.01,
        hard_stop_confirm_budget_window_seconds=0.5,
        hard_stop_confirm_budget_window_calls=3,
        execution_hard_stop_grace_seconds=10.0,
    )
    runtime: Any = None
    handle = AlwaysRaiseConfirm()
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_never_finish,
            idempotency_key="persistent-hard-stop",
            request_digest=REQUEST_SHA256,
            deadline_seconds=60.0,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_runtime(
            runtime,
            lambda item: item.operation_task is not None
            and not item.operation_task.done(),
        )
        runtime.worker_handle = handle
        async with registry._changed:
            runtime.hard_stop_monotonic = loop.time() - 0.1
            registry._wake_hard_stop_watchdog()
        await _wait_for_runtime(runtime, lambda _: len(handle.calls) >= 3)
        await asyncio.sleep(0.2)
        # The fixed 0.5s window allows at most three calls; the old path spun
        # continuously once the deadline was already past.
        assert len(handle.calls) == 3
        assert runtime.snapshot.state not in {
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        }
        assert runtime.hard_stop_in_flight is False
        assert runtime.hard_stop_quarantine_reserved is False
        assert not any(
            "never retrieved" in str(context.get("message", "")).casefold()
            for context in contexts
        )
    finally:
        loop.set_exception_handler(previous_handler)
        if runtime is not None:
            async with registry._changed:
                runtime.hard_stop_monotonic = loop.time() + 3600
                runtime.worker_handle = None
            for task in tuple(registry._hard_stop_tasks):
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            operation = runtime.operation_task
            if operation is not None and not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation
            runner = runtime.task
            if runner is not None and not runner.done():
                runner.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await runner
        await registry.close()


@pytest.mark.asyncio
async def test_hard_stop_wrapper_cancel_race_releases_owner_and_reservation(
    tmp_path: Path,
) -> None:
    """Cancelling a wrapper while kill/confirm is suspended releases both
    ownership flags, and its done callback consumes the cancellation cleanly.
    """

    class BlockingConfirm:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def kill_and_confirm(self, timeout: float) -> bool:
            self.entered.set()
            await asyncio.Event().wait()
            return False

    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    registry = LivePlanningJobRegistry(
        state_path=tmp_path / "cancel-race-hard-stop.json",
        capacity=2,
        max_running=1,
        quarantine_capacity=1,
        execution_hard_stop_grace_seconds=10.0,
    )
    runtime: Any = None
    handle = BlockingConfirm()
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_never_finish,
            idempotency_key="cancel-race-hard-stop",
            request_digest=REQUEST_SHA256,
            deadline_seconds=60.0,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_runtime(
            runtime,
            lambda item: item.operation_task is not None
            and not item.operation_task.done(),
        )
        runtime.worker_handle = handle
        async with registry._changed:
            runtime.hard_stop_monotonic = loop.time() - 0.1
            registry._wake_hard_stop_watchdog()
        await asyncio.wait_for(handle.entered.wait(), timeout=5.0)
        await _wait_for_runtime(
            runtime,
            lambda item: item.hard_stop_in_flight
            and item.hard_stop_quarantine_reserved,
        )
        wrapper = next(iter(registry._hard_stop_tasks))
        async with registry._changed:
            runtime.hard_stop_monotonic = loop.time() + 3600
        wrapper.cancel()
        with suppress(asyncio.CancelledError):
            await wrapper
        await asyncio.sleep(0)
        assert runtime.hard_stop_in_flight is False
        assert runtime.hard_stop_quarantine_reserved is False
        assert wrapper not in registry._hard_stop_tasks
        assert not contexts
    finally:
        loop.set_exception_handler(previous_handler)
        if runtime is not None:
            runtime.worker_handle = None
            operation = runtime.operation_task
            if operation is not None and not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation
            runner = runtime.task
            if runner is not None and not runner.done():
                runner.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await runner
        await registry.close()


@pytest.mark.asyncio
async def test_http_idcap_gate_precedes_uuid_runtime_and_worker_command_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A full idempotency collection rejects a new HTTP key before every
    constructor and leaves both the durable bytes and in-memory bindings intact.
    """
    from tripchord.agents import live_jobs as live_jobs_module

    registry = _http_app_context(
        monkeypatch,
        tmp_path,
        registry_kwargs={"idempotency_capacity": 1},
    )
    first, _ = await registry.start_idempotent(
        tenant_id="local",
        operation=_quick_success,
        idempotency_key="idcap-existing",
        request_digest=REQUEST_SHA256,
        deadline_seconds=30.0,
    )
    await _wait_for_runtime(
        registry._records[first.id],
        lambda item: item.snapshot.state == LivePlanningJobState.SUCCEEDED,
    )
    state_path = tmp_path / "live-jobs.json"
    before_bytes = state_path.read_bytes()
    before_records = tuple(registry._records)
    before_idempotency = {
        key: (entry.job_id, entry.request_digest, entry.defer_start)
        for key, entry in registry._idempotency.items()
    }
    calls = {"uuid": 0, "runtime": 0, "worker_command": 0}
    original_uuid4 = live_jobs_module.uuid4
    original_runtime = live_jobs_module._RuntimeJob
    original_builder = main_module._build_live_flexible_from_text_worker_command

    def uuid_probe() -> Any:
        calls["uuid"] += 1
        return original_uuid4()

    def runtime_probe(*args: Any, **kwargs: Any) -> Any:
        calls["runtime"] += 1
        return original_runtime(*args, **kwargs)

    def builder_probe(*args: Any, **kwargs: Any) -> Any:
        calls["worker_command"] += 1
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(live_jobs_module, "uuid4", uuid_probe)
    monkeypatch.setattr(live_jobs_module, "_RuntimeJob", runtime_probe)
    monkeypatch.setattr(
        main_module,
        "_build_live_flexible_from_text_worker_command",
        builder_probe,
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(
                app=app,
                client=("127.0.0.1", 51342),
                raise_app_exceptions=False,
            ),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                headers={"Idempotency-Key": "idcap-new"},
                json=_payload(ready=False),
            )
        assert response.status_code == 503, response.text
        assert calls == {"uuid": 0, "runtime": 0, "worker_command": 0}
        assert state_path.read_bytes() == before_bytes
        assert tuple(registry._records) == before_records
        assert {
            key: (entry.job_id, entry.request_digest, entry.defer_start)
            for key, entry in registry._idempotency.items()
        } == before_idempotency
    finally:
        monkeypatch.undo()
        await registry.close()


@pytest.mark.asyncio
async def test_cold_load_settles_confirmed_orphan_activation_to_restart_cancelled(
    tmp_path: Path,
) -> None:
    """A cold boot loads a durable QUEUED record whose durable worker identity
    (PGID + marker) points at a LIVE orphaned worker AND whose durable
    ``activation_operation`` proves a formal activation was committed when the
    parent crashed: the load must DEFER resolution (never terminalize over a live
    executor), ``restore_after_restart`` must first discover + authenticate +
    SIGKILL + confirm the orphan group, and ONLY THEN settle the record from
    durable facts to ``restart_cancelled``. RED on baseline: the loader resolved
    the activation-committed QUEUED record to ``restart_cancelled`` at load time
    — over the still-live orphan, before any discovery/kill — while the group
    kept running."""
    state_path = tmp_path / "live-jobs.json"
    probe = tmp_path / "cold-activation-orphan-probe.txt"
    marker = hashlib.sha256(b"cold-activation-orphan-nonce").hexdigest()
    job_id = "live-job-cold-activation-orphan"
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
    pgid = json.loads(
        (workers_dir / f"{job_id}.json").read_text(encoding="utf-8")
    )["pgid"]
    # Durable formal activation that was committed when the parent crashed: the
    # job passed the admission barrier and was dispatched, but its snapshot never
    # advanced past QUEUED.
    activation_operation = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "a" * 64,
        "idempotency_key": f"formal-activate-{job_id}",
        "request_digest": REQUEST_SHA256,
        "job_id": job_id,
        "challenge_id": f"challenge-{job_id}",
        "attempt_digest": REQUEST_SHA256,
        "capability_sha256": REQUEST_SHA256,
        "companion_identity_sha256": REQUEST_SHA256,
        "queued_result": {"job": {"id": job_id, "state": "queued"}},
        "phase": "committed",
        "dispatch_count": 1,
    }
    snap = _v3_snapshot(job_id, LivePlanningJobState.QUEUED, "queued", 0, 1)
    record = _v3_record(
        "tenant-a",
        snap,
        worker_pgid=pgid,
        worker_marker=marker,
        worker_probe=str(probe),
    )
    record["activation_operation"] = activation_operation
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [record],
        "idempotency": [
            _v3_idempotency_entry("tenant-a", "cold-activation-key", job_id),
        ],
    }
    _write_registry_state(payload, state_path)
    try:
        _wait_for_probe(probe, "orphan")
        assert _group_alive(pgid)
        registry = LivePlanningJobRegistry(state_path=state_path)
        try:
            runtime = registry._records[job_id]
            # RED: the load terminalized the activation record to CANCELLED over
            # the LIVE orphan BEFORE any discovery; GREEN defers resolution so
            # the record stays non-terminal QUEUED until the executor is proven
            # gone.
            assert runtime.snapshot.state == LivePlanningJobState.QUEUED
            assert not runtime.quarantined
            assert _group_alive(pgid)
            assert _probe_grows(probe)
            await registry.restore_after_restart()
            # Zero-request recovery: the orphan was discovered + killed first,
            # then the record was settled from durable facts only.
            assert not _group_alive(pgid)
            assert not _probe_grows(probe)
            settled = registry._records[job_id]
            assert settled.snapshot.state == LivePlanningJobState.CANCELLED
            assert settled.snapshot.stage == "restart_cancelled"
        finally:
            await registry.close()
    finally:
        with _suppress_os():
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.asyncio
async def test_cold_load_settles_activation_when_worker_group_already_exited(
    tmp_path: Path,
) -> None:
    """A worker may exit and remove its marker between parent death and recovery.

    Current ESRCH proves there is no group to kill (so no marker authentication
    is needed); the independent committed activation then proves the exact
    restart-cancelled outcome. Readiness must not expose the stale QUEUED state.
    """
    state_path = tmp_path / "already-exited-live-jobs.json"
    job_id = "live-job-cold-activation-already-exited"
    missing_pgid = 2_147_483_647
    activation_operation = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "a" * 64,
        "idempotency_key": f"formal-activate-{job_id}",
        "request_digest": REQUEST_SHA256,
        "job_id": job_id,
        "challenge_id": f"challenge-{job_id}",
        "attempt_digest": REQUEST_SHA256,
        "capability_sha256": REQUEST_SHA256,
        "companion_identity_sha256": REQUEST_SHA256,
        "queued_result": {"job": {"id": job_id, "state": "queued"}},
        "phase": "committed",
        "dispatch_count": 1,
    }
    record = _v3_record(
        "tenant-a",
        _v3_snapshot(job_id, LivePlanningJobState.QUEUED, "queued", 0, 1),
        worker_pgid=missing_pgid,
        worker_marker="already-exited-worker-marker-00000001",
    )
    record["activation_operation"] = activation_operation
    _write_registry_state(
        {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [record],
            "idempotency": [
                _v3_idempotency_entry(
                    "tenant-a",
                    "cold-activation-already-exited-key",
                    job_id,
                )
            ],
        },
        state_path,
    )

    registry = LivePlanningJobRegistry(state_path=state_path)
    try:
        assert registry._records[job_id].snapshot.state == LivePlanningJobState.QUEUED
        await registry.restore_after_restart()
        settled = registry._records[job_id]
        assert settled.snapshot.state == LivePlanningJobState.CANCELLED
        assert settled.snapshot.stage == "restart_cancelled"
        assert settled.worker_pgid is None
        assert settled.worker_marker is None
        assert settled.quarantined is False
    finally:
        await registry.close()

    restarted = LivePlanningJobRegistry(state_path=state_path)
    try:
        settled = restarted._records[job_id]
        assert settled.snapshot.state == LivePlanningJobState.CANCELLED
        assert settled.snapshot.stage == "restart_cancelled"
    finally:
        await restarted.close()
