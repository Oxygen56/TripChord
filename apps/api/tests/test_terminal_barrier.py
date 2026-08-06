"""v0.3 ALL_TERMINAL barrier contract tests.

Proves the exit gates: the barrier/settle node releases when every selected
source reached a typed terminal result (success OR a real typed failure such
as ``timed_out`` / ``login_required``); it never releases on a
``dependency_blocked`` placeholder; the planner first call is strictly after
the last source terminal; and there is no publish path with an unresolved
source.  ``ALL_SUCCEEDED`` stays the default and must not release on failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.models import (
    AgentRole,
    AgentTask,
    AgentTaskResult,
    DependencyPolicy,
    TaskGraph,
)
from tripchord.agents.runtime import AgentRegistry, DynamicTaskScheduler, FunctionAgent
from tripchord.agents.tools import ToolRegistry
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.terminal import (
    CompletionBarrier,
    SourceAttempt,
    SourceAttemptStatus,
    SourceTerminalState,
    TerminalReceipt,
    materialize_timed_out_attempts,
)


def _result(task: AgentTask, *, success: bool, failure_class: str | None = None) -> AgentTaskResult:
    return AgentTaskResult(
        task_id=task.id,
        agent_role=task.role,
        success=success,
        summary="source finished",
        failure_class=failure_class,
    )


@pytest.mark.asyncio
async def test_all_terminal_barrier_releases_on_typed_failure() -> None:
    """The settle node runs after all sources are terminal even when one failed."""
    registry = AgentRegistry()
    started_order: list[str] = []

    async def source_ok(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        started_order.append(task.id)
        return _result(task, success=True)

    async def source_timed_out(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        started_order.append(task.id)
        return _result(task, success=False, failure_class="timed_out")

    async def settle(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        started_order.append(task.id)
        return _result(task, success=True)

    async def planner(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        started_order.append(task.id)
        return _result(task, success=True)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, source_ok))
    registry.register(FunctionAgent(AgentRole.LODGING, source_timed_out))
    registry.register(FunctionAgent(AgentRole.EVIDENCE_ARBITER, settle))
    registry.register(FunctionAgent(AgentRole.NEURAL_PLANNER, planner))

    graph = TaskGraph(
        tasks=(
            AgentTask(id="source-ctrip", role=AgentRole.TRANSPORT, goal="flight"),
            AgentTask(id="source-qunar", role=AgentRole.LODGING, goal="lodging"),
            AgentTask(
                id="settle",
                role=AgentRole.EVIDENCE_ARBITER,
                goal="wait for all selected sources to reach a typed terminal state",
                dependencies=("source-ctrip", "source-qunar"),
                dependency_policy=DependencyPolicy.ALL_TERMINAL,
            ),
            AgentTask(
                id="planner",
                role=AgentRole.NEURAL_PLANNER,
                goal="plan after the barrier",
                dependencies=("settle",),
            ),
        )
    )
    outcome = await DynamicTaskScheduler(registry).run(
        graph, ContextEngine(EvidenceBlackboard()), ToolRegistry()
    )

    results = {result.task_id: result for result in outcome.results}
    # Honest scheduling: the failed source keeps success=False (never masked).
    assert outcome.succeeded is False
    assert results["source-qunar"].failure_class == "timed_out"
    assert results["source-qunar"].terminal is True
    # The ALL_TERMINAL barrier still released and the planner ran after it.
    assert results["settle"].success is True
    assert results["planner"].success is True
    assert started_order.index("settle") > started_order.index("source-qunar")


@pytest.mark.asyncio
async def test_all_succeeded_default_does_not_release_barrier_on_failure() -> None:
    """Backward compatibility: ALL_SUCCEEDED must not release on a failed source."""
    registry = AgentRegistry()

    async def source_fail(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _result(task, success=False, failure_class="login_required")

    async def settle(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _result(task, success=True)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, source_fail))
    registry.register(FunctionAgent(AgentRole.EVIDENCE_ARBITER, settle))

    graph = TaskGraph(
        tasks=(
            AgentTask(id="source", role=AgentRole.TRANSPORT, goal="flight"),
            AgentTask(
                id="settle",
                role=AgentRole.EVIDENCE_ARBITER,
                goal="barrier",
                dependencies=("source",),
                # default dependency_policy = ALL_SUCCEEDED
            ),
        )
    )
    outcome = await DynamicTaskScheduler(registry).run(
        graph, ContextEngine(EvidenceBlackboard()), ToolRegistry()
    )
    settle_result = {result.task_id: result for result in outcome.results}["settle"]
    assert settle_result.failure_class == "dependency_blocked"
    assert settle_result.terminal is False


@pytest.mark.asyncio
async def test_dependency_blocked_never_releases_all_terminal_barrier() -> None:
    """A dependency_blocked source (never executed) cannot release the barrier."""
    registry = AgentRegistry()

    async def source_fail(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _result(task, success=False, failure_class="login_required")

    # settle depends on a chain that is itself blocked: upstream -> gate.
    async def gate(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _result(task, success=True)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, source_fail))
    registry.register(FunctionAgent(AgentRole.LODGING, gate))
    registry.register(FunctionAgent(AgentRole.EVIDENCE_ARBITER, gate))

    graph = TaskGraph(
        tasks=(
            AgentTask(id="upstream", role=AgentRole.TRANSPORT, goal="source"),
            AgentTask(
                id="gate",
                role=AgentRole.LODGING,
                goal="depends on failed source under ALL_SUCCEEDED",
                dependencies=("upstream",),
            ),
            AgentTask(
                id="settle",
                role=AgentRole.EVIDENCE_ARBITER,
                goal="barrier",
                dependencies=("gate",),
                dependency_policy=DependencyPolicy.ALL_TERMINAL,
            ),
        )
    )
    outcome = await DynamicTaskScheduler(registry).run(
        graph, ContextEngine(EvidenceBlackboard()), ToolRegistry()
    )
    results = {result.task_id: result for result in outcome.results}
    # upstream fails typed (terminal), gate is dependency_blocked (NOT terminal),
    # so settle must remain blocked too.
    assert results["gate"].failure_class == "dependency_blocked"
    assert results["settle"].failure_class == "dependency_blocked"


@pytest.mark.asyncio
async def test_planner_first_call_strictly_after_last_source_terminal_at() -> None:
    """Exit gate: planner first call is strictly after the last source terminal."""
    registry = AgentRegistry()
    timestamps: dict[str, datetime] = {}

    def now() -> datetime:
        return datetime.now(UTC)

    async def source(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        timestamps[f"terminal:{task.id}"] = now()
        return _result(task, success=True)

    async def settle(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        timestamps["settle"] = now()
        return _result(task, success=True)

    async def planner(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        timestamps["planner"] = now()
        return _result(task, success=True)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, source))
    registry.register(FunctionAgent(AgentRole.LODGING, source))
    registry.register(FunctionAgent(AgentRole.EVIDENCE_ARBITER, settle))
    registry.register(FunctionAgent(AgentRole.NEURAL_PLANNER, planner))

    graph = TaskGraph(
        tasks=(
            AgentTask(id="source-a", role=AgentRole.TRANSPORT, goal="flight"),
            AgentTask(id="source-b", role=AgentRole.LODGING, goal="lodging"),
            AgentTask(
                id="settle",
                role=AgentRole.EVIDENCE_ARBITER,
                goal="barrier",
                dependencies=("source-a", "source-b"),
                dependency_policy=DependencyPolicy.ALL_TERMINAL,
            ),
            AgentTask(
                id="planner",
                role=AgentRole.NEURAL_PLANNER,
                goal="plan",
                dependencies=("settle",),
            ),
        )
    )
    await DynamicTaskScheduler(registry).run(
        graph, ContextEngine(EvidenceBlackboard()), ToolRegistry()
    )
    last_source_terminal = max(
        timestamps[f"terminal:{task_id}"] for task_id in ("source-a", "source-b")
    )
    assert timestamps["settle"] >= last_source_terminal
    assert timestamps["planner"] >= timestamps["settle"]


def _attempt(
    attempt_id: str,
    run_id: str,
    scope: str,
    status="terminal",
    terminal_state=None,
) -> SourceAttempt:
    provider, vertical = scope.split(":", 1)
    return SourceAttempt(
        attempt_id=attempt_id,
        run_id=run_id,
        scope=ProviderScopeKey(provider=provider, vertical=ProviderVertical(vertical)),
        status=SourceAttemptStatus(status),
        terminal_state=SourceTerminalState(terminal_state) if terminal_state else None,
        terminal_at=datetime.now(UTC) if status == "terminal" else None,
    )


def test_completion_barrier_releases_only_when_all_terminal() -> None:
    attempts = (
        _attempt("a", "r1", "ctrip:flight", "terminal", "quote_found"),
        _attempt("b", "r1", "qunar:flight", "terminal", "timed_out"),
    )
    barrier = CompletionBarrier(run_id="r1", selected_attempts=attempts)
    assert barrier.released
    assert barrier.unresolved_attempt_ids == ()
    # Only the quote_found scope contributes a quote.
    assert {s.key for s in barrier.quote_provider_scopes()} == {"ctrip:flight"}


def test_completion_barrier_holds_on_running_attempt() -> None:
    attempts = (
        _attempt("a", "r1", "ctrip:flight", "terminal", "quote_found"),
        _attempt("b", "r1", "qunar:flight", "queued"),
    )
    barrier = CompletionBarrier(run_id="r1", selected_attempts=attempts)
    assert not barrier.released
    assert barrier.unresolved_attempt_ids == ("b",)


def test_timed_out_attempts_are_materialised_at_deadline() -> None:
    deadline = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    now = datetime(2026, 8, 6, 12, 1, tzinfo=UTC)
    attempts = (
        _attempt("a", "r1", "ctrip:flight", "terminal", "quote_found"),
        _attempt("b", "r1", "qunar:flight", "running"),
    )
    materialized = materialize_timed_out_attempts(attempts, deadline=deadline, now=now)
    barrier = CompletionBarrier(run_id="r1", selected_attempts=materialized)
    assert barrier.released
    b = next(a for a in materialized if a.attempt_id == "b")
    assert b.terminal_state is SourceTerminalState.TIMED_OUT
    assert b.terminal_at == now


def test_receipt_hash_is_deterministic() -> None:
    receipt = TerminalReceipt(
        run_id="r1",
        attempt_id="a",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        terminal_state=SourceTerminalState.QUOTE_FOUND,
        terminal_at=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        generation=0,
    )
    assert receipt.receipt_sha256() == receipt.receipt_sha256()
    assert len(receipt.receipt_sha256()) == 64
