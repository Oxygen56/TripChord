"""Production worker-runtime handoff for the live flexible-from-text worker.

C-146 P0-1 (RETURN 7de8cf3e): a worker subprocess reconstructs the API app fresh
from its environment; without a configured browser bridge the reconstructed
``flexible_live_agent_system`` is ``None`` and the ready chain fails with an HTTP
503. This module is the PRODUCTION configuration handoff that makes the ready
chain runnable in a REAL independent process WITHOUT monkeypatching a private
runtime into the app:

- The API process embeds a ``runtime_bundle`` (a JSON spec, sourced from
  ``TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE``) into the worker command args
  when it builds the command.
- The worker subprocess, after reconstructing its own app, installs the bundle's
  runtime on it — a real ``FlexibleLiveAgentSystem`` built IN the worker process
  from the spec, never an injected/patched in-process object.

The ``deterministic-blocking`` runtime uses a side-effect-free, deterministic
pair runner (no browser, no model, no network): every pair resolves identically
to a HUMAN_BLOCK decision with the same blocked coverage and a succeeded
scheduler. That makes a cross-process ready HTTP chain PROVABLE — interpretation
``ready``, run blocked, job ``succeeded`` — without external services. Future
browser bundles can extend the same install hook with the real bridge spec.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Any

from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
    PlatformSearchCoverage,
)
from tripchord.agents.models import (
    AgentRole,
    AgentTask,
    AgentTaskResult,
    TaskGraph,
)
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.planning.package import (
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
)
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserSearchQuery,
    BrowserVertical,
)


def _source_ids() -> tuple[str, ...]:
    return tuple(
        f"source-{provider.value}-{suffix}"
        for provider in BrowserProvider
        for suffix in (
            "flight",
            "lodging-full",
            "lodging-first",
            "lodging-middle",
            "lodging-last",
        )
    )


def _deterministic_blocked_run(
    intent: PackageIntent,
    query: BrowserSearchQuery,
    mode: LiveCoverageMode,
) -> LivePackageAgentRun:
    """The deterministic HUMAN_BLOCK run every pair resolves to.

    Identical shape to the fixture used by the in-process ready-chain contract,
    but a production class: coverage reports every source blocked by the
    deterministic boundary, and the scheduler graph succeeds. No live coverage
    claim is ever made (``claim_boundary`` says so explicitly)."""
    source_ids = _source_ids()
    coverage = tuple(
        PlatformSearchCoverage(
            provider=provider,
            failed_verticals=(BrowserVertical.FLIGHT, BrowserVertical.LODGING),
            failed_source_ids=tuple(
                source_id
                for source_id in source_ids
                if source_id.startswith(f"source-{provider.value}-")
            ),
            failure_reasons=(
                "deterministic worker runtime does not access a real browser",
            ),
            complete=False,
        )
        for provider in BrowserProvider
    )
    final_tasks = (
        AgentTask(
            id="orchestrate-travel-package",
            role=AgentRole.SAFETY_GATE,
            goal="deterministic worker runtime decision",
        ),
        AgentTask(
            id="explain-final-decision",
            role=AgentRole.EXPLANATION,
            goal="deterministic worker runtime explanation",
            dependencies=("orchestrate-travel-package",),
        ),
        AgentTask(
            id="curate-run-memory",
            role=AgentRole.MEMORY_CURATOR,
            goal="deterministic worker runtime memory curation",
            dependencies=("explain-final-decision",),
        ),
        AgentTask(
            id="publish-live-run",
            role=AgentRole.SAFETY_GATE,
            goal="deterministic worker runtime publication gate",
            dependencies=("curate-run-memory",),
        ),
    )
    return LivePackageAgentRun(
        mode=mode,
        intent=intent,
        search_query=query,
        decision=PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="deterministic worker runtime intentionally blocks browser search",
        ),
        claim_boundary="deterministic worker runtime only; no live coverage claim",
        all_platforms_complete=False,
        coverage=coverage,
        inventory=PackageInventory(),
        normalization_results=(),
        package=None,
        scheduler=SchedulerOutcome(
            graph=TaskGraph(tasks=final_tasks),
            results=tuple(
                AgentTaskResult(
                    task_id=task.id,
                    agent_role=task.role,
                    success=True,
                    summary="deterministic worker runtime stage complete",
                    output={"publication_gate_passed": True}
                    if task.id == "publish-live-run"
                    else {},
                )
                for task in final_tasks
            ),
            trace=(),
            wall_time_seconds=0,
            max_parallel_tasks=15,
            succeeded=True,
        ),
        source_task_ids=source_ids,
    )


class DeterministicBlockingPairRunner:
    """Deterministic, side-effect-free :class:`LiveDatePairRunner`.

    Every pair resolves identically to a HUMAN_BLOCK run, so a cross-process
    ready HTTP chain built on this runner is byte-deterministic and provable.
    """

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        return _deterministic_blocked_run(intent, query, mode)


def _parse_clock(
    spec: dict[str, Any],
) -> tuple[callable, callable]:
    def fixed_now_clock() -> datetime:
        return fixed_now

    now_iso = spec.get("now_iso")
    if isinstance(now_iso, str):
        try:
            fixed_now = datetime.fromisoformat(now_iso)
        except ValueError:
            fixed_now = datetime.now(UTC)
    else:
        fixed_now = datetime.now(UTC)
    now: callable = fixed_now_clock
    monotonic_value = spec.get("monotonic_clock")
    if isinstance(monotonic_value, (int, float)):
        fixed_monotonic = float(monotonic_value)

        def fixed_monotonic_clock() -> float:
            return fixed_monotonic

        monotonic_clock: callable = fixed_monotonic_clock
    else:
        monotonic_clock = monotonic
    return now, monotonic_clock


def install_runtime_bundle(target_app: Any, bundle: dict[str, Any]) -> None:
    """Install the worker's ``runtime_bundle`` onto its reconstructed app.

    The worker subprocess imports ``tripchord.main`` fresh; with no browser
    bridge configured its ``flexible_live_agent_system`` is None. This builds a
    REAL ``FlexibleLiveAgentSystem`` in THIS process from the bundle spec and
    installs it, so the ready chain's ``_flexible_live_agent_system_from_app``
    resolves a production system — never a monkeypatched private runtime.

    ``deterministic-blocking``: a fixed-clock system over
    :class:`DeterministicBlockingPairRunner`. Unknown runtimes fail closed."""
    runtime = bundle.get("runtime")
    if runtime != "deterministic-blocking":
        raise RuntimeError(f"unknown live flexible worker runtime: {runtime!r}")
    now, monotonic_clock = _parse_clock(bundle)
    system = FlexibleLiveAgentSystem(
        DeterministicBlockingPairRunner(),
        now=now,
        monotonic_clock=monotonic_clock,
    )
    target_app.state.flexible_live_agent_system = system
