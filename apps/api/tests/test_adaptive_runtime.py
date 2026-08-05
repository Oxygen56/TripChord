import pytest
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.models import AgentRole, AgentTask, AgentTaskResult, TaskGraph
from tripchord.agents.runtime import (
    AgentRegistry,
    DynamicTaskScheduler,
    FunctionAgent,
    SchedulerControlState,
    SpawnValidation,
)
from tripchord.agents.tools import ToolRegistry


def _context() -> ContextEngine:
    return ContextEngine(EvidenceBlackboard())


def _success(task: AgentTask, *, spawned_tasks: tuple[AgentTask, ...] = ()) -> AgentTaskResult:
    return AgentTaskResult(
        task_id=task.id,
        agent_role=task.role,
        success=True,
        summary=f"{task.id} completed",
        spawned_tasks=spawned_tasks,
    )


@pytest.mark.asyncio
async def test_batch_advisor_executes_only_selected_runnable_subset_each_round() -> None:
    registry = AgentRegistry()
    executed: list[str] = []
    advisor_states: list[SchedulerControlState] = []

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        executed.append(task.id)
        return _success(task)

    def advisor(
        runnable: tuple[AgentTask, ...],
        state: SchedulerControlState,
    ) -> tuple[str, ...]:
        advisor_states.append(state)
        return (runnable[-1].id,)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    graph = TaskGraph(
        tasks=tuple(
            AgentTask(id=task_id, role=AgentRole.TRANSPORT, goal=task_id)
            for task_id in ("a", "b", "c")
        )
    )

    outcome = await DynamicTaskScheduler(
        registry,
        max_concurrency=3,
        batch_advisor=advisor,
    ).run(graph, _context(), ToolRegistry())

    assert outcome.succeeded
    assert executed == ["c", "b", "a"]
    assert outcome.max_parallel_tasks == 1
    assert [state.round_number for state in advisor_states] == [1, 2, 3]
    assert [state.runnable_task_ids for state in advisor_states] == [
        ("a", "b", "c"),
        ("a", "b"),
        ("a",),
    ]
    selected_events = [
        event for event in outcome.trace if event.kind == "batch_advisor_selected"
    ]
    assert [event.details["selected_task_ids"] for event in selected_events] == [
        ["c"],
        ["b"],
        ["a"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [(), ("not-runnable",), ("a", "a")])
async def test_invalid_or_empty_batch_advice_fails_closed_without_looping(
    selection: tuple[str, ...],
) -> None:
    registry = AgentRegistry()
    executions = 0

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        nonlocal executions
        executions += 1
        return _success(task)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    outcome = await DynamicTaskScheduler(
        registry,
        batch_advisor=lambda _runnable, _state: selection,
    ).run(
        TaskGraph(tasks=(AgentTask(id="a", role=AgentRole.TRANSPORT, goal="a"),)),
        _context(),
        ToolRegistry(),
    )

    assert not outcome.succeeded
    assert executions == 0
    assert outcome.results[0].failure_class == "batch_advisor_blocked"
    assert sum(event.kind == "batch_advisor_rejected" for event in outcome.trace) == 1
    assert sum(event.kind == "task_blocked_by_advisor" for event in outcome.trace) == 1


@pytest.mark.asyncio
async def test_spawn_validator_rejects_optional_child_and_records_reason() -> None:
    registry = AgentRegistry()
    executed: list[str] = []

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        executed.append(task.id)
        if task.id != "root":
            return _success(task)
        return _success(
            task,
            spawned_tasks=(
                AgentTask(id="allowed", role=AgentRole.TRANSPORT, goal="allowed"),
                AgentTask(
                    id="denied",
                    role=AgentRole.TRANSPORT,
                    goal="denied",
                    input={"spawn_required": False},
                ),
            ),
        )

    def validator(
        _parent: AgentTask,
        spawned: AgentTask,
        _state: SchedulerControlState,
    ) -> SpawnValidation:
        return SpawnValidation(
            allowed=spawned.id == "allowed",
            reason="template is not allowlisted",
        )

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    outcome = await DynamicTaskScheduler(
        registry,
        spawn_validator=validator,
    ).run(
        TaskGraph(tasks=(AgentTask(id="root", role=AgentRole.TRANSPORT, goal="root"),)),
        _context(),
        ToolRegistry(),
    )

    assert outcome.succeeded
    assert executed == ["root", "allowed"]
    assert [task.id for task in outcome.graph.tasks] == ["root", "allowed"]
    rejected = [event for event in outcome.trace if event.kind == "task_spawn_rejected"]
    assert len(rejected) == 1
    assert rejected[0].task_id == "denied"
    assert rejected[0].details["reason"] == "template is not allowlisted"


@pytest.mark.asyncio
async def test_dynamic_and_per_parent_limits_bound_recursive_spawn_growth() -> None:
    registry = AgentRegistry()

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        generation = int(task.input.get("generation", 0))
        children = (
            AgentTask(
                id=f"child-{generation + 1}",
                role=AgentRole.TRANSPORT,
                goal="continue recursive expansion",
                input={"generation": generation + 1, "spawn_required": False},
            ),
            AgentTask(
                id=f"sibling-{generation + 1}",
                role=AgentRole.TRANSPORT,
                goal="exceed the per-parent quota",
                input={"spawn_required": False},
            ),
        )
        return _success(task, spawned_tasks=children)

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    outcome = await DynamicTaskScheduler(
        registry,
        max_dynamic_tasks=3,
        max_spawned_tasks_per_parent=1,
    ).run(
        TaskGraph(tasks=(AgentTask(id="root", role=AgentRole.TRANSPORT, goal="root"),)),
        _context(),
        ToolRegistry(),
    )

    assert outcome.succeeded
    assert [task.id for task in outcome.graph.tasks] == [
        "root",
        "child-1",
        "child-2",
        "child-3",
    ]
    rejected_reasons = [
        event.details["reason"]
        for event in outcome.trace
        if event.kind == "task_spawn_rejected"
    ]
    assert rejected_reasons.count("maximum spawned tasks per parent reached") == 2
    assert rejected_reasons.count("maximum dynamic task count reached") == 3


@pytest.mark.asyncio
async def test_controlled_spawn_rejects_unknown_dependencies_without_corrupting_graph() -> None:
    registry = AgentRegistry()

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _success(
            task,
            spawned_tasks=(
                AgentTask(
                    id="unsafe",
                    role=AgentRole.TRANSPORT,
                    goal="invalid dependency",
                    dependencies=("missing",),
                    input={"spawn_required": False},
                ),
            ),
        )

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    outcome = await DynamicTaskScheduler(
        registry,
        max_dynamic_tasks=1,
    ).run(
        TaskGraph(tasks=(AgentTask(id="root", role=AgentRole.TRANSPORT, goal="root"),)),
        _context(),
        ToolRegistry(),
    )

    assert outcome.succeeded
    assert [task.id for task in outcome.graph.tasks] == ["root"]
    rejected = [event for event in outcome.trace if event.kind == "task_spawn_rejected"]
    assert len(rejected) == 1
    assert "unknown dependencies" in rejected[0].details["reason"]


@pytest.mark.asyncio
async def test_required_spawn_rejection_fails_the_scheduler_outcome() -> None:
    registry = AgentRegistry()

    async def worker(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return _success(
            task,
            spawned_tasks=(
                AgentTask(id="required-child", role=AgentRole.REPAIR, goal="required"),
            ),
        )

    registry.register(FunctionAgent(AgentRole.TRANSPORT, worker))
    outcome = await DynamicTaskScheduler(
        registry,
        max_dynamic_tasks=0,
    ).run(
        TaskGraph(tasks=(AgentTask(id="root", role=AgentRole.TRANSPORT, goal="root"),)),
        _context(),
        ToolRegistry(),
    )

    assert not outcome.succeeded
    assert outcome.required_spawn_rejection_count == 1
    rejected = [event for event in outcome.trace if event.kind == "task_spawn_rejected"]
    assert rejected[0].details["spawn_required"] is True
