"""Production subprocess worker entry for live flexible-from-text jobs.

C-146 P0-1: the FastAPI persistent-job route wraps the REAL planning operation
in a ``LiveJobWorkerCommand`` pointing at this module. The registry spawns an
independent worker process that runs it, so the hard-stop watchdog can PROVE the
operation's external side effects permanently froze — SIGKILL of the whole
process group + waitpid + the permanent freeze of any external probe — instead
of assuming a coroutine inside the API process died on cancellation.

The worker reconstructs the durable request (payload JSON, request SHA-256,
tenant identity) and executes the SAME ``_execute_live_flexible_from_text``
production path the API uses, against the real app components. Progress
reporting cannot cross the process boundary (the reporter is in-process), so the
operation runs with ``report_progress=None``; the durable job identity, query /
cancel / retry / cold-start recovery are all owned by the parent registry, and
the worker's stdout JSON becomes the durable job ``result``.

This module intentionally does NOT import ``tripchord.main`` at module scope:
importing it would construct the job registry in this process (a concurrent
second writer of the same state file). The worker subprocess is spawned with
``TRIPCHORD_LIVE_WORKER_SUBPROCESS=1``, which makes ``main`` skip the registry
singleton; the import happens only when the entry actually runs.
"""

from __future__ import annotations

from typing import Any


async def run_live_flexible_from_text(
    *,
    payload: dict[str, Any],
    request_digest: str,
    tenant_id: str,
    probe_path: str | None = None,
) -> dict[str, Any]:
    # Delayed import: only an actual worker execution pulls in ``tripchord.main``
    # (and, under TRIPCHORD_LIVE_WORKER_SUBPROCESS=1, no second job registry).
    from tripchord.api import LiveFlexibleFromTextPlanningRequest
    from tripchord.auth import Principal
    from tripchord.main import _execute_live_flexible_from_text, app, live_run_cache

    request = LiveFlexibleFromTextPlanningRequest.model_validate(payload)
    response = await _execute_live_flexible_from_text(
        request,
        target_app=app,
        cache=live_run_cache,
        principal=Principal(tenant_id=tenant_id, auth_mode="worker-subprocess"),
        report_progress=None,
        report_pair_checkpoint=None,
        expected_request_sha256=request_digest,
        model_trace_scope_id=None,
        report_model_trace_summary=None,
    )
    return response.model_dump(mode="json")
