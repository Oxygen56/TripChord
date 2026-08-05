import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.models import (
    AgentDecision,
    AgentRole,
    AgentTask,
    AgentTaskResult,
    DecisionState,
    EvidenceRecord,
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
    TaskGraph,
    ToolPermission,
)
from tripchord.agents.runtime import AgentRegistry, DynamicTaskScheduler, FunctionAgent
from tripchord.agents.tools import (
    ApprovalRequiredError,
    InvalidApprovalError,
    ToolCall,
    ToolForbiddenError,
    ToolRegistry,
    ToolSpec,
)


def evidence(
    evidence_id: str,
    topic: str,
    subject: str,
    *,
    version: int = 1,
    confidence: float = 1,
    expired: bool = False,
) -> EvidenceRecord:
    captured = datetime.now(UTC) - timedelta(minutes=10 if expired else 1)
    return EvidenceRecord(
        id=evidence_id,
        topic=topic,
        subject=subject,
        payload={"value": evidence_id},
        source="test",
        captured_at=captured,
        expires_at=captured + timedelta(minutes=5),
        confidence=confidence,
        owner_agent=AgentRole.CONTEXT,
        version=version,
    )


def test_preference_constitution_explicit_trip_rule_wins_agent_inference() -> None:
    preferences = PreferenceConstitution(
        rules=(
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.WEIGHTED,
                weight=0.2,
                source=PreferenceSource.INFERRED_CURRENT_CONTEXT,
            ),
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.REQUIRED,
                weight=1,
                source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                reason="同行有老人",
            ),
        )
    )

    selected = preferences.effective("hotel_breakfast")

    assert selected is not None
    assert selected.mode == PreferenceMode.REQUIRED
    assert selected.reason == "同行有老人"
    assert selected.mode.chinese_label == "必须满足"


def test_three_state_decision_requires_user_confirmation_for_exception() -> None:
    with pytest.raises(ValidationError):
        AgentDecision(
            state=DecisionState.ACCEPT_WITH_EXCEPTION,
            summary="超预算但继续",
            exception_reasons=("超预算 150 元",),
        )

    decision = AgentDecision(
        state=DecisionState.ACCEPT_WITH_EXCEPTION,
        summary="用户确认后采用",
        exception_reasons=("超预算 150 元",),
        verifier_violations=("budget_exceeded",),
        requires_user_confirmation=True,
    )
    assert decision.state.chinese_label == "确认例外后接受"


def test_context_pack_uses_latest_fresh_evidence_and_respects_budget() -> None:
    blackboard = EvidenceBlackboard()
    blackboard.add(evidence("hotel-v1", "lodging", "hotel-a", confidence=0.5))
    blackboard.add(evidence("hotel-v2", "lodging", "hotel-a", version=2, confidence=0.9))
    blackboard.add(evidence("hotel-old", "lodging", "hotel-old", expired=True))
    blackboard.add(evidence("weather", "weather", "beijing", confidence=0.8))
    engine = ContextEngine(blackboard)
    task = AgentTask(
        id="lodging",
        role=AgentRole.LODGING,
        goal="选择酒店",
        context_topics=("lodging",),
    )

    packed = engine.build_pack(task, token_budget=10_000)
    tiny = engine.build_pack(task, token_budget=1)

    assert packed.evidence_refs == ("hotel-v2",)
    assert "hotel-old" not in packed.evidence_refs
    assert tiny.evidence == ()
    assert tiny.omitted_evidence_refs == ("hotel-v2",)


@pytest.mark.asyncio
async def test_tool_permissions_require_approval_and_forbid_disallowed_actions() -> None:
    registry = ToolRegistry()

    async def handler(call: ToolCall) -> dict[str, str]:
        return {"received": call.id}

    registry.register(
        ToolSpec(
            name="search_hotels",
            description="只读搜索酒店",
            permission=ToolPermission.READ_ONLY_EXTERNAL,
            allowed_roles=(AgentRole.LODGING,),
        ),
        handler,
    )
    registry.register(
        ToolSpec(
            name="book_hotel",
            description="预订酒店",
            permission=ToolPermission.HIGH_IMPACT,
            allowed_roles=(AgentRole.EXECUTOR,),
        ),
        handler,
    )
    registry.register(
        ToolSpec(
            name="bypass_captcha",
            description="绕过验证码",
            permission=ToolPermission.FORBIDDEN,
            allowed_roles=(AgentRole.BROWSER_RESEARCH,),
        ),
        handler,
    )

    receipt = await registry.invoke(
        ToolCall(
            id="call-search",
            tool_name="search_hotels",
            task_id="hotel-task",
            agent_role=AgentRole.LODGING,
        )
    )
    assert receipt.success
    with pytest.raises(ApprovalRequiredError):
        await registry.invoke(
            ToolCall(
                id="call-book",
                tool_name="book_hotel",
                task_id="book-task",
                agent_role=AgentRole.EXECUTOR,
            )
        )
    book_call = ToolCall(
        id="call-book",
        tool_name="book_hotel",
        task_id="book-task",
        agent_role=AgentRole.EXECUTOR,
    )
    preview = registry.preview(book_call, summary="将预订北京酒店，预计支付 1320 元")
    grant = registry.approve(preview.id, approved_by="user-123")
    approved = await registry.invoke(book_call, approval_token=grant.token)
    assert approved.approval_token_used
    assert approved.approved_by == "user-123"
    assert registry.verify_receipt(book_call, approved)
    with pytest.raises(InvalidApprovalError, match="already used"):
        await registry.invoke(book_call, approval_token=grant.token)
    other_call = book_call.model_copy(update={"id": "call-other"})
    other_preview = registry.preview(other_call, summary="另一个预订")
    other_grant = registry.approve(other_preview.id, approved_by="user-123")
    changed_call = other_call.model_copy(update={"arguments": {"price": 9999}})
    with pytest.raises(InvalidApprovalError, match="does not match"):
        await registry.invoke(changed_call, approval_token=other_grant.token)
    with pytest.raises(ToolForbiddenError):
        await registry.invoke(
            ToolCall(
                id="call-forbidden",
                tool_name="bypass_captcha",
                task_id="browser-task",
                agent_role=AgentRole.BROWSER_RESEARCH,
            )
        )


def test_task_graph_rejects_cycles() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        TaskGraph(
            tasks=(
                AgentTask(
                    id="a",
                    role=AgentRole.TRANSPORT,
                    goal="a",
                    dependencies=("b",),
                ),
                AgentTask(
                    id="b",
                    role=AgentRole.LODGING,
                    goal="b",
                    dependencies=("a",),
                ),
            )
        )


@pytest.mark.asyncio
async def test_scheduler_runs_independent_agents_in_parallel_and_accepts_dynamic_tasks() -> None:
    registry = AgentRegistry()
    specialists_started: set[str] = set()
    both_specialists_started = asyncio.Event()

    async def specialist(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        specialists_started.add(task.id)
        if {"transport", "lodging"} <= specialists_started:
            both_specialists_started.set()
        # This is a deterministic concurrency assertion: a serial scheduler
        # would leave the first specialist waiting until this timeout instead
        # of relying on a load-sensitive wall-clock threshold.
        await asyncio.wait_for(both_specialists_started.wait(), timeout=0.5)
        await asyncio.sleep(0.01)
        spawned = (
            (
                AgentTask(
                    id="arbiter",
                    role=AgentRole.EVIDENCE_ARBITER,
                    goal="解决交通与酒店证据冲突",
                    dependencies=("transport", "lodging"),
                ),
            )
            if task.id == "transport"
            else ()
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=task.role,
            success=True,
            summary=f"{task.id} complete",
            spawned_tasks=spawned,
        )

    async def arbiter(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        return AgentTaskResult(
            task_id=task.id,
            agent_role=task.role,
            success=True,
            summary="evidence reconciled",
        )

    registry.register(FunctionAgent(AgentRole.TRANSPORT, specialist))
    registry.register(FunctionAgent(AgentRole.LODGING, specialist))
    registry.register(FunctionAgent(AgentRole.EVIDENCE_ARBITER, arbiter))
    graph = TaskGraph(
        tasks=(
            AgentTask(id="transport", role=AgentRole.TRANSPORT, goal="搜索交通"),
            AgentTask(id="lodging", role=AgentRole.LODGING, goal="搜索酒店"),
        )
    )
    scheduler = DynamicTaskScheduler(registry, max_concurrency=4)
    context = ContextEngine(EvidenceBlackboard())

    outcome = await scheduler.run(graph, context, ToolRegistry())

    assert outcome.succeeded
    assert outcome.max_parallel_tasks == 2
    assert both_specialists_started.is_set()
    assert [result.task_id for result in outcome.results] == [
        "transport",
        "lodging",
        "arbiter",
    ]
    assert any(event.kind == "task_spawned" for event in outcome.trace)


@pytest.mark.asyncio
async def test_scheduler_retries_transient_agent_failure_with_bounded_attempts() -> None:
    registry = AgentRegistry()
    attempts = 0

    async def flaky(
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("temporary provider timeout")
        return AgentTaskResult(
            task_id=task.id,
            agent_role=task.role,
            success=True,
            summary="recovered",
        )

    registry.register(FunctionAgent(AgentRole.WEATHER, flaky))
    outcome = await DynamicTaskScheduler(registry).run(
        TaskGraph(
            tasks=(
                AgentTask(
                    id="weather",
                    role=AgentRole.WEATHER,
                    goal="query weather",
                    max_attempts=2,
                ),
            )
        ),
        ContextEngine(EvidenceBlackboard()),
        ToolRegistry(),
    )

    assert outcome.succeeded
    assert attempts == 2
    failures = [event for event in outcome.trace if event.kind == "task_attempt_failed"]
    assert len(failures) == 1
    assert failures[0].details["will_retry"] is True
