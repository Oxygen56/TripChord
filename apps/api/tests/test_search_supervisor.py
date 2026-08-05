from __future__ import annotations

import json

import pytest
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.live_advisory import StructuredLiveModelAgent, proposal_from_result
from tripchord.agents.model_gateway import (
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ScriptedModelClient,
)
from tripchord.agents.models import AgentRole, AgentTask, ToolPermission
from tripchord.agents.search_supervisor import (
    SearchCacheDisposition,
    SearchScheduleSafetyError,
    SearchScheduleWave,
    SearchSupervisorProposal,
    SearchTaskCapability,
    apply_search_supervisor_proposal,
    materialize_search_schedule,
)
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec

_INSPECT_SEARCH_CAPABILITIES_TOOL = "inspect_search_capabilities"


def _capability(
    task_id: str,
    *,
    provider: str | None = None,
    vertical: str | None = None,
    current_start_delay_ms: int = 0,
    required: bool = True,
    tenant_authorized: bool = True,
    permission: ToolPermission = ToolPermission.READ_ONLY_EXTERNAL,
) -> SearchTaskCapability:
    return SearchTaskCapability(
        task_id=task_id,
        provider=provider or task_id.split("-")[0],
        vertical=vertical or ("flight" if "flight" in task_id else "lodging"),
        required=required,
        tenant_authorized=tenant_authorized,
        permission=permission,
        cache_disposition=SearchCacheDisposition.RECENT_REUSE_ALLOWED,
        current_start_delay_ms=current_start_delay_ms,
        capability_version="test-readonly-v1",
    )


def _task(task_id: str) -> AgentTask:
    return AgentTask(
        id=task_id,
        role=AgentRole.TRANSPORT,
        goal=f"read-only search {task_id}",
        allowed_tools=("browser_bridge_search",),
        max_attempts=1,
    )


@pytest.mark.asyncio
async def test_model_agent_proposal_changes_real_source_order_and_waves() -> None:
    capabilities = tuple(_capability(task_id) for task_id in ("a-flight", "b-flight", "c-flight"))
    payload = {
        "summary": "prioritize b, then fan out a and c",
        "waves": [
            {"id": "priority", "task_ids": ["b-flight"]},
            {"id": "fanout", "task_ids": ["c-flight", "a-flight"]},
        ],
        "skipped_task_ids": [],
        "declared_budget_units": 3,
        "strategy_reasons": ["fresh evidence first"],
        "uncertainty_flags": [],
    }
    client = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-search-envelope",
                        name=_INSPECT_SEARCH_CAPABILITIES_TOOL,
                    ),
                ),
            ),
            ModelResponse(provider="ignored", model="ignored", text=json.dumps(payload)),
        ),
        model="search-supervisor-fixture",
    )
    router = ModelRouter(
        {AgentRole.SEARCH_SUPERVISOR: client},
        high_risk_client=client,
    )
    agent = StructuredLiveModelAgent(
        AgentRole.SEARCH_SUPERVISOR,
        router,
        system_prompt="Only schedule allowed read-only task IDs.",
        output_model=SearchSupervisorProposal,
        required=True,
    )
    supervisor_task = AgentTask(
        id="supervise",
        role=AgentRole.SEARCH_SUPERVISOR,
        goal="schedule allowed searches",
        allowed_tools=(_INSPECT_SEARCH_CAPABILITIES_TOOL,),
        input={
            "allowed_source_tasks": [item.model_dump(mode="json") for item in capabilities],
            "hard_budget_units": 3,
        },
        max_attempts=1,
    )

    tools = ToolRegistry()

    async def inspect(_: ToolCall) -> dict[str, object]:
        return {
            "allowed_source_tasks": [item.model_dump(mode="json") for item in capabilities],
            "hard_budget_units": 3,
        }

    tools.register(
        ToolSpec(
            name=_INSPECT_SEARCH_CAPABILITIES_TOOL,
            description="Inspect deterministic read-only search envelope",
            permission=ToolPermission.PURE_COMPUTE,
            allowed_roles=(AgentRole.SEARCH_SUPERVISOR,),
        ),
        inspect,
    )
    result = await agent.execute(
        supervisor_task,
        ContextEngine(EvidenceBlackboard()),
        tools,
    )
    proposal = proposal_from_result(result, SearchSupervisorProposal)
    assert isinstance(proposal, SearchSupervisorProposal)
    trace = result.output["agentic_trace"]
    assert isinstance(trace, dict)
    assert trace["tool_names"] == [_INSPECT_SEARCH_CAPABILITIES_TOOL]
    schedule = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="strict",
        hard_budget_units=3,
        max_browser_tasks_per_wave=3,
        browser_companion_lease_cap=1,
    )
    materialized = materialize_search_schedule(
        tuple(_task(item.task_id) for item in capabilities),
        schedule,
        supervisor_task_id="supervise",
    )

    assert schedule.proposal_source == "model_agent"
    assert schedule.ordered_task_ids == ("b-flight", "c-flight", "a-flight")
    assert tuple(task.id for task in materialized) == schedule.ordered_task_ids
    assert materialized[0].dependencies == ("supervise",)
    assert materialized[1].dependencies == ("b-flight",)
    assert materialized[2].dependencies == ("b-flight",)
    assert materialized[0].input["search_schedule_wave_id"] == "priority"


@pytest.mark.asyncio
async def test_search_supervisor_rejects_unknown_model_tool() -> None:
    client = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(ModelToolCall(id="evil", name="read_browser_cookies"),),
            ),
        ),
        model="malicious-search-supervisor-fixture",
    )
    router = ModelRouter(
        {AgentRole.SEARCH_SUPERVISOR: client},
        high_risk_client=client,
    )
    agent = StructuredLiveModelAgent(
        AgentRole.SEARCH_SUPERVISOR,
        router,
        system_prompt="Only inspect the declared pure-compute capability tool.",
        output_model=SearchSupervisorProposal,
        required=True,
    )
    task = AgentTask(
        id="supervise",
        role=AgentRole.SEARCH_SUPERVISOR,
        goal="schedule allowed searches",
        allowed_tools=(_INSPECT_SEARCH_CAPABILITIES_TOOL,),
        max_attempts=1,
    )

    async def inspect(_: ToolCall) -> dict[str, object]:
        return {"allowed_source_tasks": []}

    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name=_INSPECT_SEARCH_CAPABILITIES_TOOL,
            description="Inspect deterministic read-only search envelope",
            permission=ToolPermission.PURE_COMPUTE,
            allowed_roles=(AgentRole.SEARCH_SUPERVISOR,),
        ),
        inspect,
    )

    result = await agent.execute(
        task,
        ContextEngine(EvidenceBlackboard()),
        tools,
    )

    assert result.output["agent_required_failed"] is True
    trace = result.output["agentic_trace"]
    assert isinstance(trace, dict)
    assert str(trace["failure"]).startswith("PermissionError:")
    assert "undeclared tool" in str(trace["failure"])


def test_unknown_duplicate_and_over_budget_proposal_is_atomically_rejected() -> None:
    capabilities = tuple(_capability(task_id) for task_id in ("a-flight", "b-flight"))
    proposal = SearchSupervisorProposal(
        summary="malicious schedule",
        waves=(
            SearchScheduleWave(
                id="bad",
                task_ids=("a-flight", "a-flight", "a-flight", "unknown-flight"),
            ),
        ),
        skipped_task_ids=("b-flight",),
        declared_budget_units=3,
    )

    schedule = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="strict",
        hard_budget_units=2,
        max_browser_tasks_per_wave=2,
    )

    assert not schedule.proposal_accepted
    assert schedule.proposal_source == "scripted_fallback"
    assert schedule.ordered_task_ids == ("a-flight", "b-flight")
    assert any(reason.startswith("unknown_task_ids") for reason in schedule.rejected_reasons)
    assert "duplicate_scheduled_task_ids" in schedule.rejected_reasons
    assert "hard_budget_exceeded:3>2" in schedule.rejected_reasons
    assert "strict_mode_cannot_skip_tasks" in schedule.rejected_reasons


@pytest.mark.parametrize(
    ("tenant_authorized", "permission"),
    (
        (False, ToolPermission.READ_ONLY_EXTERNAL),
        (True, ToolPermission.HIGH_IMPACT),
    ),
)
def test_unsafe_allow_list_fails_closed_before_model_output(
    tenant_authorized: bool,
    permission: ToolPermission,
) -> None:
    capabilities = (
        _capability(
            "a-flight",
            tenant_authorized=tenant_authorized,
            permission=permission,
        ),
    )

    with pytest.raises(SearchScheduleSafetyError, match="unauthorized or non-read-only"):
        apply_search_supervisor_proposal(
            capabilities,
            None,
            coverage_mode="strict",
            hard_budget_units=1,
            max_browser_tasks_per_wave=1,
        )


def test_degraded_can_skip_only_optional_tasks_but_strict_cannot() -> None:
    capabilities = (
        _capability("required-flight", required=True),
        _capability("optional-lodging", required=False),
    )
    proposal = SearchSupervisorProposal(
        summary="skip optional split-stay read",
        waves=(SearchScheduleWave(id="minimum", task_ids=("required-flight",)),),
        skipped_task_ids=("optional-lodging",),
        declared_budget_units=1,
    )

    degraded = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="degraded",
        hard_budget_units=2,
        max_browser_tasks_per_wave=2,
    )
    strict = apply_search_supervisor_proposal(
        tuple(item.model_copy(update={"required": True}) for item in capabilities),
        proposal,
        coverage_mode="strict",
        hard_budget_units=2,
        max_browser_tasks_per_wave=2,
    )
    materialized = materialize_search_schedule(
        tuple(_task(item.task_id) for item in capabilities),
        degraded,
        supervisor_task_id="supervise",
    )

    assert degraded.proposal_accepted
    assert degraded.skipped_task_ids == ("optional-lodging",)
    assert materialized[-1].input["search_supervisor_skipped"] is True
    assert not strict.proposal_accepted
    assert strict.skipped_task_ids == ()
    assert set(strict.ordered_task_ids) == {"required-flight", "optional-lodging"}


def test_missing_model_uses_auditable_scripted_fallback() -> None:
    capabilities = tuple(_capability(task_id) for task_id in ("a-flight", "b-flight"))

    schedule = apply_search_supervisor_proposal(
        capabilities,
        None,
        coverage_mode="strict",
        hard_budget_units=2,
        max_browser_tasks_per_wave=2,
    )

    assert schedule.proposal_source == "scripted_fallback"
    assert schedule.rejected_reasons == ("model_proposal_unavailable",)
    assert schedule.ordered_task_ids == ("a-flight", "b-flight")


def _v4_browser_capabilities() -> tuple[SearchTaskCapability, ...]:
    capabilities: list[SearchTaskCapability] = []
    for provider in ("ctrip", "qunar"):
        for index, delay_ms in enumerate(range(0, 240_000, 40_000)):
            capabilities.append(
                _capability(
                    f"{provider}-source-{index}",
                    provider=provider,
                    current_start_delay_ms=delay_ms,
                )
            )
    capabilities.append(
        _capability(
            "tongcheng-flight",
            provider="tongcheng",
            current_start_delay_ms=0,
        )
    )
    return tuple(capabilities)


def test_thirteen_single_task_barrier_waves_are_rejected_and_fall_back() -> None:
    capabilities = _v4_browser_capabilities()
    proposal = SearchSupervisorProposal(
        summary="serialize every browser source",
        waves=tuple(
            SearchScheduleWave(id=f"serial-{index}", task_ids=(item.task_id,))
            for index, item in enumerate(capabilities, start=1)
        ),
        declared_budget_units=13,
    )

    schedule = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="strict",
        hard_budget_units=13,
        max_browser_tasks_per_wave=13,
        browser_companion_lease_cap=6,
    )

    assert not schedule.proposal_accepted
    assert schedule.proposal_source == "scripted_fallback"
    assert schedule.rejected_reasons == ("browser_barrier_batches_exceeded:13>3",)
    assert len(schedule.waves) == 1
    assert schedule.ordered_task_ids == tuple(item.task_id for item in capabilities)
    assert schedule.minimum_browser_lease_batches == 3
    assert schedule.applied_browser_barrier_batches == 3


def test_provider_delay_regression_is_rejected_and_falls_back_in_original_order() -> None:
    capabilities = (
        _capability(
            "ctrip-flight",
            provider="ctrip",
            current_start_delay_ms=0,
        ),
        _capability(
            "ctrip-lodging",
            provider="ctrip",
            current_start_delay_ms=40_000,
        ),
        _capability(
            "qunar-flight",
            provider="qunar",
            current_start_delay_ms=0,
        ),
    )
    proposal = SearchSupervisorProposal(
        summary="put the delayed Ctrip source before its immediate source",
        waves=(
            SearchScheduleWave(
                id="reversed",
                task_ids=("ctrip-lodging", "ctrip-flight", "qunar-flight"),
            ),
        ),
        declared_budget_units=3,
    )

    schedule = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="strict",
        hard_budget_units=3,
        max_browser_tasks_per_wave=3,
        browser_companion_lease_cap=2,
    )

    assert not schedule.proposal_accepted
    assert schedule.proposal_source == "scripted_fallback"
    assert schedule.ordered_task_ids == (
        "ctrip-flight",
        "ctrip-lodging",
        "qunar-flight",
    )
    assert schedule.rejected_reasons == (
        "provider_delay_order_regression:ctrip:ctrip-lodging@40000>ctrip-flight@0",
    )


def test_safe_concurrent_v4_proposal_is_preserved_without_rewriting() -> None:
    capabilities = _v4_browser_capabilities()
    first_wave = tuple(item.task_id for item in capabilities[:6])
    second_wave = tuple(item.task_id for item in capabilities[6:])
    proposal = SearchSupervisorProposal(
        summary="two concurrent waves with the minimum three browser lease batches",
        waves=(
            SearchScheduleWave(id="ctrip-concurrent", task_ids=first_wave),
            SearchScheduleWave(id="remaining-concurrent", task_ids=second_wave),
        ),
        declared_budget_units=13,
    )

    schedule = apply_search_supervisor_proposal(
        capabilities,
        proposal,
        coverage_mode="strict",
        hard_budget_units=13,
        max_browser_tasks_per_wave=13,
        browser_companion_lease_cap=6,
    )

    assert schedule.proposal_accepted
    assert schedule.proposal_source == "model_agent"
    assert schedule.waves == proposal.waves
    assert schedule.ordered_task_ids == (*first_wave, *second_wave)
    assert schedule.rejected_reasons == ()
    assert schedule.minimum_browser_lease_batches == 3
    assert schedule.applied_browser_barrier_batches == 3
