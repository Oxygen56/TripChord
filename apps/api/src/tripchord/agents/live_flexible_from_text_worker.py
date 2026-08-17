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

import hashlib
import json
from typing import Any

_MODEL_EXECUTION_RECEIPT_SCHEMA = "tripchord-model-execution-receipt-v1"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _formal_model_execution_receipt(
    *,
    trace_sink: Any,
    runtime_receipt: dict[str, Any],
    job_id: str,
    request_digest: str,
) -> dict[str, Any]:
    """Seal privacy-safe traces proving the production model actually ran."""

    identity = runtime_receipt.get("model_runtime_identity")
    if runtime_receipt.get("model_agents_required") is not True or not isinstance(
        identity,
        dict,
    ):
        raise RuntimeError("formal worker model runtime receipt is invalid")
    traces = tuple(
        trace
        for trace in trace_sink.records
        if trace.scope_id == job_id
        and trace.scope_request_digest == request_digest
    )
    if not traces:
        raise RuntimeError("formal worker completed without an actual model call")
    allowed_models = {identity.get("primary_model"), identity.get("fast_model")}
    provider = identity.get("provider")
    if any(
        trace.provider != provider or trace.model not in allowed_models
        for trace in traces
    ):
        raise RuntimeError("formal worker model trace identity is invalid")
    safe_traces = [
        {
            "id": trace.id,
            "provider": trace.provider,
            "model": trace.model,
            "role": trace.role.value,
            "request_digest": trace.request_digest,
            "scope_id": trace.scope_id,
            "scope_request_digest": trace.scope_request_digest,
            "response_schema_requested": trace.response_schema_requested,
            "tool_count": trace.tool_count,
            "started_at": trace.started_at.isoformat(),
            "finished_at": trace.finished_at.isoformat(),
            "success": trace.success,
            "usage": trace.usage.model_dump(mode="json"),
            "estimated_cost_usd": trace.estimated_cost_usd,
            "error_class": trace.error_class,
        }
        for trace in traces
    ]
    success_count = sum(1 for trace in traces if trace.success)
    unsigned = {
        "schema_version": _MODEL_EXECUTION_RECEIPT_SCHEMA,
        "job_id": job_id,
        "request_sha256": request_digest,
        "runtime_bundle_spec_sha256": runtime_receipt["spec_sha256"],
        "worker_runtime_identity_sha256": _canonical_sha256(
            runtime_receipt["worker_runtime_identity"]
        ),
        "model_runtime_identity": identity,
        "trace_count": len(safe_traces),
        "success_count": success_count,
        "failure_count": len(safe_traces) - success_count,
        "traces": safe_traces,
    }
    return {**unsigned, "receipt_sha256": _canonical_sha256(unsigned)}


def validate_model_execution_receipt(
    receipt: object,
    runtime_receipt: object,
    *,
    job_id: str,
    request_sha256: str,
    trace_count: int,
    success_count: int,
    failure_count: int,
) -> dict[str, Any]:
    """Revalidate a decoded worker receipt at every consuming boundary."""

    runtime_fields = {
        "schema_version",
        "runtime",
        "providers",
        "spec_sha256",
        "runtime_provenance",
        "api_runtime_identity_sha256",
        "worker_runtime_identity",
        "model_agents_required",
        "model_runtime_identity",
    }
    receipt_fields = {
        "schema_version",
        "job_id",
        "request_sha256",
        "runtime_bundle_spec_sha256",
        "worker_runtime_identity_sha256",
        "model_runtime_identity",
        "trace_count",
        "success_count",
        "failure_count",
        "traces",
        "receipt_sha256",
    }
    if (
        not isinstance(runtime_receipt, dict)
        or set(runtime_receipt) != runtime_fields
        or runtime_receipt.get("schema_version")
        != "tripchord-live-worker-runtime-receipt-v1"
        or runtime_receipt.get("model_agents_required") is not True
        or not isinstance(runtime_receipt.get("model_runtime_identity"), dict)
    ):
        raise ValueError("worker runtime receipt is invalid")
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
        raise ValueError("model execution receipt shape is invalid")
    traces = receipt.get("traces")
    identity = runtime_receipt["model_runtime_identity"]
    allowed_models = {identity.get("primary_model"), identity.get("fast_model")}
    if (
        receipt.get("schema_version") != _MODEL_EXECUTION_RECEIPT_SCHEMA
        or receipt.get("job_id") != job_id
        or receipt.get("request_sha256") != request_sha256
        or receipt.get("runtime_bundle_spec_sha256")
        != runtime_receipt.get("spec_sha256")
        or receipt.get("worker_runtime_identity_sha256")
        != _canonical_sha256(runtime_receipt["worker_runtime_identity"])
        or receipt.get("model_runtime_identity") != identity
        or receipt.get("trace_count") != trace_count
        or receipt.get("success_count") != success_count
        or receipt.get("failure_count") != failure_count
        or trace_count <= 0
        or success_count != trace_count
        or failure_count != 0
        or not isinstance(traces, list)
        or len(traces) != trace_count
        or receipt.get("receipt_sha256")
        != _canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    ):
        raise ValueError("model execution receipt binding is invalid")
    expected_trace_fields = {
        "id",
        "provider",
        "model",
        "role",
        "request_digest",
        "scope_id",
        "scope_request_digest",
        "response_schema_requested",
        "tool_count",
        "started_at",
        "finished_at",
        "success",
        "usage",
        "estimated_cost_usd",
        "error_class",
    }
    for trace in traces:
        if (
            not isinstance(trace, dict)
            or set(trace) != expected_trace_fields
            or trace.get("provider") != identity.get("provider")
            or trace.get("model") not in allowed_models
            or trace.get("scope_id") != job_id
            or trace.get("scope_request_digest") != request_sha256
            or trace.get("success") is not True
            or trace.get("error_class") is not None
            or not isinstance(trace.get("request_digest"), str)
            or len(trace["request_digest"]) != 64
        ):
            raise ValueError("model execution trace binding is invalid")
    return dict(receipt)


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

    def _merge_source_terminal_events(self, events: tuple[Any, ...]) -> None:
        positions = {
            event.get("source_task_id"): index
            for index, event in enumerate(self.source_terminal_events)
            if isinstance(event, dict)
        }
        for event in events:
            payload = (
                dict(event)
                if isinstance(event, dict)
                else event.model_dump(mode="json")
            )
            source_task_id = payload.get("source_task_id")
            if not isinstance(source_task_id, str) or not source_task_id:
                raise RuntimeError("worker source terminal event identity is invalid")
            index = positions.get(source_task_id)
            if index is None:
                positions[source_task_id] = len(self.source_terminal_events)
                self.source_terminal_events.append(payload)
            else:
                self.source_terminal_events[index] = payload

    async def report_live_source_terminal_events(
        self,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        self._merge_source_terminal_events(events)

    async def report_source_terminal_events(self, events: tuple[Any, ...]) -> None:
        self._merge_source_terminal_events(events)

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
    import tripchord.main as api_main
    from tripchord.api import LiveFlexibleFromTextPlanningRequest
    from tripchord.auth import Principal
    from tripchord.main import _execute_live_flexible_from_text, app, live_run_cache

    runtime_receipt: dict[str, Any] | None = None
    observability = _WorkerObservability(job_id=job_id or "")
    if runtime_bundle is not None:
        # C-146 P0-1 (RETURN 7de8cf3e): the API process handed this worker a
        # runtime bundle so the ready chain runs in THIS process against a REAL
        # browser-bridge composition built from the spec — never a
        # monkeypatched private runtime and never the deterministic stand-in.
        from tripchord.agents.live_flexible_worker_runtime import (
            install_runtime_bundle,
            shutdown_runtime_http,
            start_runtime_model_http,
        )

        runtime_receipt = install_runtime_bundle(
            app,
            runtime_bundle,
            formal_execution_capability=formal_execution_capability,
            source_terminal_reporter=(
                observability.report_live_source_terminal_events
            ),
        )
    try:
        if (
            runtime_receipt is not None
            and runtime_receipt.get("model_agents_required") is True
        ):
            await start_runtime_model_http(app)

        request = LiveFlexibleFromTextPlanningRequest.model_validate(payload)
        source_authority = getattr(app.state, "formal_live_source_authority", None)
        parent_source = getattr(
            app.state,
            "live_worker_parent_source_client",
            None,
        )
        if source_authority is not None and parent_source is not None:
            raise RuntimeError("worker cannot own and proxy the formal source authority")
        formal_source_installed = (
            source_authority is not None or parent_source is not None
        )
        if formal_source_installed != (formal_execution_capability is not None):
            raise RuntimeError(
                "worker formal source runtime and execution capability must be paired"
            )
    except Exception:
        # Validation and model-transport startup occur before the main execution
        # try/finally.  Close the parent TCP facade here as well so a rejected
        # handoff never survives until process teardown by accident.
        if runtime_bundle is not None:
            await shutdown_runtime_http(app)
        raise

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
        if (
            runtime_receipt is not None
            and runtime_receipt.get("model_agents_required") is True
        ):
            result["model_execution_receipt"] = _formal_model_execution_receipt(
                trace_sink=api_main.model_trace_sink,
                runtime_receipt=runtime_receipt,
                job_id=job_id or "",
                request_digest=request_digest,
            )
    finally:
        if runtime_bundle is not None:
            await shutdown_runtime_http(app)
    result["_worker_observability"] = observability.as_payload()
    if runtime_receipt is not None:
        result["worker_runtime_receipt"] = runtime_receipt
    return result
