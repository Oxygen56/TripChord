from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from time import perf_counter
from typing import Protocol

from pydantic import Field, JsonValue

from tripchord.agents.context import ContextEngine
from tripchord.agents.models import (
    AgentRole,
    AgentTask,
    AgentTaskResult,
    DependencyPolicy,
    TaskGraph,
    TraceEvent,
)
from tripchord.agents.tools import ToolRegistry
from tripchord.domain.common import DomainModel


def _dependency_met(result: AgentTaskResult, policy: DependencyPolicy) -> bool:
    """Whether one dependency result releases a task under a dependency policy."""
    if policy is DependencyPolicy.ALL_TERMINAL:
        return result.terminal
    return result.success


class AgentExecutor(Protocol):
    role: AgentRole

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        tool_registry: ToolRegistry,
    ) -> AgentTaskResult: ...


AgentFunction = Callable[
    [AgentTask, ContextEngine, ToolRegistry],
    Awaitable[AgentTaskResult],
]


class SchedulerControlState(DomainModel):
    """Immutable scheduler state exposed to bounded control callbacks."""

    round_number: int = Field(ge=1)
    initial_task_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    dynamic_task_count: int = Field(ge=0)
    completed_results: tuple[AgentTaskResult, ...] = ()
    runnable_task_ids: tuple[str, ...] = ()
    remaining_dynamic_task_capacity: int | None = Field(default=None, ge=0)


class SpawnValidation(DomainModel):
    allowed: bool
    reason: str | None = None


class RunnableBatchAdvisor(Protocol):
    def __call__(
        self,
        runnable: tuple[AgentTask, ...],
        state: SchedulerControlState,
    ) -> Sequence[str] | Awaitable[Sequence[str]]: ...


class DynamicSpawnValidator(Protocol):
    def __call__(
        self,
        parent: AgentTask,
        spawned: AgentTask,
        state: SchedulerControlState,
    ) -> bool | SpawnValidation | Awaitable[bool | SpawnValidation]: ...


class FunctionAgent:
    def __init__(self, role: AgentRole, function: AgentFunction) -> None:
        self.role = role
        self._function = function

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        tool_registry: ToolRegistry,
    ) -> AgentTaskResult:
        return await self._function(task, context_engine, tool_registry)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[AgentRole, AgentExecutor] = {}

    def register(self, agent: AgentExecutor) -> None:
        if agent.role in self._agents:
            raise ValueError(f"agent role {agent.role} is already registered")
        self._agents[agent.role] = agent

    def get(self, role: AgentRole) -> AgentExecutor:
        agent = self._agents.get(role)
        if agent is None:
            raise LookupError(f"no agent registered for role {role}")
        return agent


class SchedulerOutcome(DomainModel):
    graph: TaskGraph
    results: tuple[AgentTaskResult, ...]
    trace: tuple[TraceEvent, ...]
    wall_time_seconds: float = Field(ge=0)
    max_parallel_tasks: int = Field(ge=0)
    required_spawn_rejection_count: int = Field(default=0, ge=0)
    succeeded: bool


class DynamicTaskScheduler:
    def __init__(
        self,
        registry: AgentRegistry,
        *,
        max_concurrency: int = 8,
        batch_advisor: RunnableBatchAdvisor | None = None,
        spawn_validator: DynamicSpawnValidator | None = None,
        max_dynamic_tasks: int | None = None,
        max_spawned_tasks_per_parent: int | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if max_dynamic_tasks is not None and max_dynamic_tasks < 0:
            raise ValueError("max_dynamic_tasks must be non-negative")
        if (
            max_spawned_tasks_per_parent is not None
            and max_spawned_tasks_per_parent < 0
        ):
            raise ValueError("max_spawned_tasks_per_parent must be non-negative")
        self._registry = registry
        self._max_concurrency = max_concurrency
        self._batch_advisor = batch_advisor
        self._spawn_validator = spawn_validator
        self._max_dynamic_tasks = max_dynamic_tasks
        self._max_spawned_tasks_per_parent = max_spawned_tasks_per_parent
        self._controlled_spawning = any(
            option is not None
            for option in (
                batch_advisor,
                spawn_validator,
                max_dynamic_tasks,
                max_spawned_tasks_per_parent,
            )
        )

    async def run(
        self,
        graph: TaskGraph,
        context_engine: ContextEngine,
        tool_registry: ToolRegistry,
    ) -> SchedulerOutcome:
        started = perf_counter()
        tasks = {task.id: task for task in graph.tasks}
        results: dict[str, AgentTaskResult] = {}
        trace: list[TraceEvent] = []
        sequence = 0
        max_parallel = 0
        semaphore = asyncio.Semaphore(self._max_concurrency)
        initial_task_count = len(tasks)
        dynamic_task_count = 0
        required_spawn_rejection_count = 0
        spawned_by_parent: dict[str, int] = {}
        round_number = 0

        def record(
            kind: str,
            task: AgentTask | None = None,
            **details: JsonValue,
        ) -> None:
            nonlocal sequence
            sequence += 1
            trace.append(
                TraceEvent(
                    sequence=sequence,
                    kind=kind,
                    task_id=task.id if task else None,
                    agent_role=task.role if task else None,
                    details=details,
                )
            )

        def control_state(
            *,
            current_round: int,
            runnable_task_ids: tuple[str, ...] | None = None,
        ) -> SchedulerControlState:
            ordered_results = tuple(
                results[task_id] for task_id in tasks if task_id in results
            )
            if runnable_task_ids is None:
                runnable_task_ids = tuple(
                    task.id
                    for task_id, task in tasks.items()
                    if task_id not in results
                    and all(
                        dependency in results
                        and _dependency_met(results[dependency], task.dependency_policy)
                        for dependency in task.dependencies
                    )
                )
            remaining_capacity = (
                None
                if self._max_dynamic_tasks is None
                else max(self._max_dynamic_tasks - dynamic_task_count, 0)
            )
            return SchedulerControlState(
                round_number=current_round,
                initial_task_count=initial_task_count,
                task_count=len(tasks),
                dynamic_task_count=dynamic_task_count,
                completed_results=ordered_results,
                runnable_task_ids=runnable_task_ids,
                remaining_dynamic_task_capacity=remaining_capacity,
            )

        async def select_batch(
            runnable: tuple[AgentTask, ...],
            *,
            current_round: int,
        ) -> tuple[tuple[AgentTask, ...], str | None]:
            if self._batch_advisor is None:
                return runnable, None
            state = control_state(
                current_round=current_round,
                runnable_task_ids=tuple(task.id for task in runnable),
            )
            try:
                advised = self._batch_advisor(runnable, state)
                if isinstance(advised, Awaitable):
                    advised = await advised
                if isinstance(advised, str):
                    raise TypeError("batch advisor must return a sequence of task ids")
                selected_ids = tuple(advised)
                if any(not isinstance(task_id, str) for task_id in selected_ids):
                    raise TypeError("batch advisor task ids must be strings")
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                record(
                    "batch_advisor_rejected",
                    round_number=current_round,
                    reason=reason,
                    runnable_task_ids=[task.id for task in runnable],
                )
                return (), reason

            if len(selected_ids) != len(set(selected_ids)):
                reason = "batch advisor returned duplicate task ids"
                record(
                    "batch_advisor_rejected",
                    round_number=current_round,
                    reason=reason,
                    runnable_task_ids=[task.id for task in runnable],
                    selected_task_ids=list(selected_ids),
                )
                return (), reason
            runnable_by_id = {task.id: task for task in runnable}
            outside_runnable = [
                task_id for task_id in selected_ids if task_id not in runnable_by_id
            ]
            if outside_runnable:
                reason = "batch advisor selected tasks outside the runnable set"
                outside_runnable_details: list[JsonValue] = [
                    task_id for task_id in outside_runnable
                ]
                record(
                    "batch_advisor_rejected",
                    round_number=current_round,
                    reason=reason,
                    runnable_task_ids=[task.id for task in runnable],
                    selected_task_ids=list(selected_ids),
                    invalid_task_ids=outside_runnable_details,
                )
                return (), reason
            if not selected_ids:
                reason = "batch advisor selected no tasks while runnable work remained"
                record(
                    "batch_advisor_rejected",
                    round_number=current_round,
                    reason=reason,
                    runnable_task_ids=[task.id for task in runnable],
                    selected_task_ids=[],
                )
                return (), reason

            selected = tuple(runnable_by_id[task_id] for task_id in selected_ids)
            record(
                "batch_advisor_selected",
                round_number=current_round,
                runnable_task_ids=[task.id for task in runnable],
                selected_task_ids=list(selected_ids),
                runnable_count=len(runnable),
                selected_count=len(selected),
            )
            return selected, None

        async def spawn_rejection_reason(
            parent: AgentTask,
            spawned: AgentTask,
            *,
            current_round: int,
        ) -> str | None:
            if spawned.id in tasks:
                return "dynamic task id already exists"
            missing = set(spawned.dependencies) - set(tasks)
            if missing:
                return f"unknown dependencies: {sorted(missing)}"
            if (
                self._max_dynamic_tasks is not None
                and dynamic_task_count >= self._max_dynamic_tasks
            ):
                return "maximum dynamic task count reached"
            parent_spawn_count = spawned_by_parent.get(parent.id, 0)
            if (
                self._max_spawned_tasks_per_parent is not None
                and parent_spawn_count >= self._max_spawned_tasks_per_parent
            ):
                return "maximum spawned tasks per parent reached"
            if self._spawn_validator is None:
                return None
            try:
                validation = self._spawn_validator(
                    parent,
                    spawned,
                    control_state(current_round=current_round),
                )
                if isinstance(validation, Awaitable):
                    validation = await validation
            except Exception as exc:
                return f"spawn validator error: {type(exc).__name__}: {exc}"
            if isinstance(validation, SpawnValidation):
                if validation.allowed:
                    return None
                return validation.reason or "spawn validator rejected task"
            if isinstance(validation, bool):
                return None if validation else "spawn validator rejected task"
            return "spawn validator returned an invalid response"

        def block_pending_after_advisor_failure(reason: str) -> None:
            for task_id, task in tasks.items():
                if task_id in results:
                    continue
                results[task_id] = AgentTaskResult(
                    task_id=task.id,
                    agent_role=task.role,
                    success=False,
                    summary="task blocked because the batch advisor made no safe progress",
                    failure_class="batch_advisor_blocked",
                    output={"reason": reason},
                )
                record("task_blocked_by_advisor", task, reason=reason)

        async def execute(task: AgentTask) -> AgentTaskResult:
            async with semaphore:
                result: AgentTaskResult | None = None
                for attempt in range(1, task.max_attempts + 1):
                    record("task_started", task, attempt=attempt)
                    try:
                        result = await self._registry.get(task.role).execute(
                            task,
                            context_engine,
                            tool_registry,
                        )
                    except Exception as exc:
                        result = AgentTaskResult(
                            task_id=task.id,
                            agent_role=task.role,
                            success=False,
                            summary=str(exc),
                            failure_class=type(exc).__name__,
                        )
                    if result.success:
                        break
                    record(
                        "task_attempt_failed",
                        task,
                        attempt=attempt,
                        failure_class=result.failure_class,
                        will_retry=attempt < task.max_attempts,
                    )
                assert result is not None
                record(
                    "task_finished",
                    task,
                    success=result.success,
                    spawned_tasks=len(result.spawned_tasks),
                )
                return result

        record("run_started", task_count=len(tasks))
        while len(results) < len(tasks):
            round_number += 1
            pending = [task for task_id, task in tasks.items() if task_id not in results]
            runnable = [
                task
                for task in pending
                if all(
                    dependency in results
                    and _dependency_met(results[dependency], task.dependency_policy)
                    for dependency in task.dependencies
                )
            ]
            if not runnable:
                blocked = [task for task in pending if task.id not in results]
                for task in blocked:
                    failed_dependencies = [
                        dependency
                        for dependency in task.dependencies
                        if dependency in results
                        and not _dependency_met(results[dependency], task.dependency_policy)
                    ]
                    result = AgentTaskResult(
                        task_id=task.id,
                        agent_role=task.role,
                        success=False,
                        summary="task blocked by failed or unresolved dependencies",
                        failure_class="dependency_blocked",
                        output={"failed_dependencies": failed_dependencies},
                    )
                    results[task.id] = result
                    record("task_blocked", task, failed_dependencies=len(failed_dependencies))
                break
            runnable.sort(key=lambda task: (-task.priority, task.id))
            selected, advisor_failure = await select_batch(
                tuple(runnable),
                current_round=round_number,
            )
            if advisor_failure is not None:
                block_pending_after_advisor_failure(advisor_failure)
                break
            max_parallel = max(max_parallel, min(len(selected), self._max_concurrency))
            settled = await asyncio.gather(*(execute(task) for task in selected))
            for result in settled:
                if result.task_id in results:
                    raise RuntimeError(f"task {result.task_id} produced more than one result")
                results[result.task_id] = result
                context_engine.add_agent_evidence(result.evidence)
                for spawned in result.spawned_tasks:
                    if not self._controlled_spawning:
                        if spawned.id in tasks:
                            raise ValueError(f"dynamic task id already exists: {spawned.id}")
                        missing = set(spawned.dependencies) - set(tasks)
                        if missing:
                            raise ValueError(
                                f"dynamic task {spawned.id} has unknown dependencies: "
                                f"{sorted(missing)}"
                            )
                    else:
                        parent = tasks[result.task_id]
                        rejection_reason = await spawn_rejection_reason(
                            parent,
                            spawned,
                            current_round=round_number,
                        )
                        if rejection_reason is not None:
                            spawn_required = spawned.input.get("spawn_required", True) is not False
                            record(
                                "task_spawn_rejected",
                                spawned,
                                parent_task=result.task_id,
                                reason=rejection_reason,
                                spawn_required=spawn_required,
                                dynamic_task_count=dynamic_task_count,
                                parent_spawn_count=spawned_by_parent.get(result.task_id, 0),
                            )
                            if spawn_required:
                                required_spawn_rejection_count += 1
                            continue
                    tasks[spawned.id] = spawned
                    dynamic_task_count += 1
                    spawned_by_parent[result.task_id] = (
                        spawned_by_parent.get(result.task_id, 0) + 1
                    )
                    record("task_spawned", spawned, parent_task=result.task_id)

        ordered_tasks = tuple(tasks.values())
        final_graph = TaskGraph(tasks=ordered_tasks)
        succeeded = (
            bool(results)
            and all(result.success for result in results.values())
            and required_spawn_rejection_count == 0
        )
        record("run_finished", success=succeeded, result_count=len(results))
        return SchedulerOutcome(
            graph=final_graph,
            results=tuple(results[task.id] for task in ordered_tasks),
            trace=tuple(trace),
            wall_time_seconds=perf_counter() - started,
            max_parallel_tasks=max_parallel,
            required_spawn_rejection_count=required_spawn_rejection_count,
            succeeded=succeeded,
        )
