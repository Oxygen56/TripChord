"""Production subprocess worker entry for live flexible-from-text jobs.

C-146 P0-1: the FastAPI persistent-job route wraps the REAL planning operation
in a ``LiveJobWorkerCommand`` pointing at this module. The registry spawns an
independent worker process that runs it, so the hard-stop watchdog can PROVE the
operation's external side effects permanently froze — SIGKILL of the whole
process group + waitpid + the permanent freeze of any external probe — instead
of assuming a coroutine inside the API process died on cancellation.

The worker reconstructs the durable request (payload JSON, request SHA-256,
tenant identity) and executes the SAME ``_execute_live_flexible_from_text``
production path the API uses, against the real app components. Progress /
checkpoint / model-trace observability cannot call the parent's in-process
reporters across the process boundary, so this module captures those events in a
local collector and returns them inside the result payload; the parent registry
replays them onto the durable job record before the terminal label is
published. The durable job identity, query / cancel / retry / cold-start
recovery are all owned by the parent registry, and the worker's stdout JSON
becomes the durable job ``result``.

This module intentionally does NOT import ``tripchord.main`` at module scope:
importing it would construct the job registry in this process (a concurrent
second writer of the same state file). The worker subprocess is spawned with
``TRIPCHORD_LIVE_WORKER_SUBPROCESS=1``, which makes ``main`` skip the registry
singleton; the import happens only when the entry actually runs.
"""

from __future__ import annotations

from typing import Any


class _WorkerObservability:
    """Cross-process progress / checkpoint / model-trace collector.

    The API process's ``LiveJobProgressReporter`` is an in-process object; the
    parent cannot receive reporter calls over the subprocess pipe. This object
    satisfies the same protocol and records every event so the parent registry
    can replay them onto the durable job after the worker exits.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.progress_events: list[list[Any]] = []
        self.pair_checkpoints: list[dict[str, Any]] = []
        self.model_trace_summary: dict[str, Any] | None = None
        self.source_terminal_events: list[dict[str, Any]] = []
        self.barrier_released_at: str | None = None

    async def ensure_active(self) -> None:
        return None

    async def __call__(self, stage: str, progress: int) -> None:
        self.progress_events.append([stage, progress])

    async def report_pair_checkpoint(self, checkpoint: Any) -> None:
        self.pair_checkpoints.append(checkpoint.model_dump(mode="json"))

    async def report_model_trace_summary(
        self,
        scope_id: str,
        scope_request_sha256: str,
        trace_count: int,
        success_count: int,
        failure_count: int,
    ) -> None:
        self.model_trace_summary = {
            "scope_id": scope_id,
            "scope_request_sha256": scope_request_sha256,
            "trace_count": trace_count,
            "success_count": success_count,
            "failure_count": failure_count,
        }

    async def report_source_terminal_events(self, events: tuple[Any, ...]) -> None:
        self.source_terminal_events.extend(
            event.model_dump(mode="json") for event in events
        )

    async def report_barrier_released(self, barrier_released_at: Any) -> None:
        self.barrier_released_at = barrier_released_at.isoformat()

    def as_payload(self) -> dict[str, Any]:
        return {
            "progress_events": self.progress_events,
            "pair_checkpoints": self.pair_checkpoints,
            "model_trace_summary": self.model_trace_summary,
            "source_terminal_events": self.source_terminal_events,
            "barrier_released_at": self.barrier_released_at,
        }


async def run_live_flexible_from_text(
    *,
    payload: dict[str, Any],
    request_digest: str,
    tenant_id: str,
    job_id: str | None = None,
    probe_path: str | None = None,
    runtime_bundle: dict[str, Any] | None = None,
    formal_execution_capability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Delayed import: only an actual worker execution pulls in ``tripchord.main``
    # (and, under TRIPCHORD_LIVE_WORKER_SUBPROCESS=1, no second job registry).
    from tripchord.api import LiveFlexibleFromTextPlanningRequest
    from tripchord.auth import Principal
    from tripchord.main import _execute_live_flexible_from_text, app, live_run_cache

    runtime_receipt: dict[str, Any] | None = None
    if runtime_bundle is not None:
        # C-146 P0-1 (RETURN 7de8cf3e): the API process handed this worker a
        # runtime bundle so the ready chain runs in THIS process against a REAL
        # browser-bridge composition built from the spec — never a
        # monkeypatched private runtime and never the deterministic stand-in.
        from tripchord.agents.live_flexible_worker_runtime import (
            install_runtime_bundle,
            shutdown_runtime_http,
        )

        runtime_receipt = install_runtime_bundle(app, runtime_bundle)

    request = LiveFlexibleFromTextPlanningRequest.model_validate(payload)
    observability = _WorkerObservability(job_id=job_id or "")
    source_authority = getattr(app.state, "formal_live_source_authority", None)
    if (source_authority is None) != (formal_execution_capability is None):
        raise RuntimeError(
            "worker formal source authority and execution capability must be paired"
        )

    async def execute() -> Any:
        return await _execute_live_flexible_from_text(
            request,
            target_app=app,
            cache=live_run_cache,
            principal=Principal(tenant_id=tenant_id, auth_mode="worker-subprocess"),
            report_progress=observability,
            report_pair_checkpoint=observability.report_pair_checkpoint,
            expected_request_sha256=request_digest,
            model_trace_scope_id=job_id or None,
            report_model_trace_summary=observability.report_model_trace_summary,
        )

    try:
        if source_authority is None:
            response = await execute()
        else:
            # The capability was issued and validated by the parent API before
            # activation, then injected by the registry at spawn time. Binding
            # it to this worker's ContextVar makes every real Browser/iCom call
            # pass the same frozen-graph guard while the shared ledger serializes
            # events across the API coordinator and its direct child executor.
            with source_authority.execution_scope(formal_execution_capability):
                response = await execute()
        result = response.model_dump(mode="json")
        # ``LiveRunCache`` is process-local. Return the EXACT entries addressed
        # by the worker response handles so the parent command can atomically
        # import those event-replan snapshots under NEW parent-owned handles.
        # Reconstructing this envelope from ``response.run.pair_runs`` would
        # silently substitute a different observable whenever the flexible
        # runner's published pair run and its cache snapshot diverged.
        worker_cache_runs: list[dict[str, Any]] = []
        for handle in response.cached_pair_runs:
            entry = await live_run_cache.get(handle.run_id, tenant_id)
            if entry is None:
                raise RuntimeError(
                    "live planning worker cache handle expired before handoff"
                )
            worker_cache_runs.append(
                {
                    "date_pair_id": handle.date_pair_id,
                    "run": entry.run.model_dump(mode="json"),
                }
            )
        result["_worker_cache_runs"] = worker_cache_runs
    finally:
        if runtime_bundle is not None:
            await shutdown_runtime_http(app)
    result["_worker_observability"] = observability.as_payload()
    if runtime_receipt is not None:
        result["worker_runtime_receipt"] = runtime_receipt
    return result
