from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from datetime import UTC, date, datetime

import pytest
from tripchord.agents.adaptive_control import (
    AdaptiveConcurrencyAudit,
    AdaptiveControlInput,
    ScaleDirective,
    derive_scale_directive,
)
from tripchord.agents.agent_budget import (
    AgentBudgetLedger,
    bind_agent_budget,
    current_agent_budget,
)
from tripchord.agents.agent_templates import AgentTemplateId, build_agent_template_plan
from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentSystem,
    FlexiblePairExecution,
)
from tripchord.agents.live_system import (
    CandidateShardAgentRecord,
    CandidateShardMergeAudit,
    LiveCoverageMode,
    LivePackageAgentRun,
    LivePackageAgentSystem,
    LiveRunPurpose,
)
from tripchord.agents.memory import MemoryAccessContext
from tripchord.agents.model_gateway import (
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelToolCall,
)
from tripchord.agents.models import AgentRole
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORMS,
    FlexibleDateExplorer,
    FlexibleTravelWindow,
)
from tripchord.planning.package import PackageIntent
from tripchord.providers.browser_bridge import BrowserSearchQuery

_accepted_run = importlib.import_module("apps.api.tests.test_flexible_live_system")._accepted_run


class _NeverRunLiveSystem:
    def __init__(self) -> None:
        self.call_count = 0

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        self.call_count += 1
        raise AssertionError((intent, query, mode, timeout_seconds, source_start_delays_ms))


class _FullUniverseLiveRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.peak_active = 0

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del timeout_seconds, source_start_delays_ms
        self.calls += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.001)
            return _accepted_run(
                intent,
                query,
                mode,
                total_cents=900_000,
                complete=True,
            )
        finally:
            self.active -= 1


class _AdaptiveQueryModel:
    provider = "adaptive-test"
    model = "adaptive-query-test"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.frontier_row_counts: list[int] = []
        self.active = 0
        self.max_active = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.005)
            tool_messages = tuple(message for message in request.messages if message.tool_results)
            if not tool_messages:
                return ModelResponse(
                    provider=self.provider,
                    model=self.model,
                    tool_calls=(
                        ModelToolCall(
                            id=f"inspect-{len(self.requests)}",
                            name="inspect_date_search_space",
                        ),
                    ),
                )
            envelope = json.loads(tool_messages[-1].tool_results[0].content)
            observation = envelope["tool_observation"]
            output = observation["tool_receipt"]["output"]
            rows = output["frontier_rows"]
            self.frontier_row_counts.append(len(rows))
            budget = int(output["exact_pair_budget"])
            selected = [str(row[0]) for row in rows[:budget]]
            return ModelResponse(
                provider=self.provider,
                model=self.model,
                text=json.dumps(
                    {
                        "summary": "按分片可见候选选择并交由最终合并 Agent 裁决",
                        "selected_pair_ids": selected,
                        "selection_reasons": ["分片内低价与覆盖锚点"],
                        "stop_condition": "达到确定性精查预算",
                        "query_budget_pairs": budget,
                    }
                ),
            )
        finally:
            self.active -= 1


def _candidate_ids_sha256(candidate_ids: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(list(candidate_ids), separators=(",", ":")).encode()
    ).hexdigest()


def _dynamic_candidate_audit() -> tuple[ScaleDirective, CandidateShardMergeAudit]:
    candidate_ids = tuple(f"nested-candidate-{index:03d}" for index in range(65))
    scopes = (
        candidate_ids[:32],
        candidate_ids[32:64],
        candidate_ids[64:],
    )
    directive = derive_scale_directive(AdaptiveControlInput(D=1, C=65, G=0, R=False, E=False))
    records = tuple(
        CandidateShardAgentRecord(
            shard_index=index,
            task_id=f"candidate-scout-{index:02d}",
            agent_template_id=("candidate_curator" if index == 0 else "candidate_shard"),
            candidate_ids=scope,
            scope_sha256=_candidate_ids_sha256(scope),
            nominated_candidate_ids=(scope[0],),
            model_proposal_applied=True,
            fallback_used=False,
        )
        for index, scope in enumerate(scopes)
    )
    frontier_ids = tuple(scope[0] for scope in scopes)
    return directive, CandidateShardMergeAudit(
        scale_state_fingerprint=directive.state_fingerprint,
        pool_candidate_count=65,
        requested_shard_count=3,
        completed_shard_count=3,
        max_model_concurrency=3,
        model_concurrency_audit=AdaptiveConcurrencyAudit(
            ceiling=3,
            initial_limit=2,
            final_limit=3,
            peak_in_flight=2,
            admitted_count=3,
            success_count=3,
            failure_count=0,
            additive_increase_count=1,
            multiplicative_decrease_count=0,
        ),
        complete_partition=True,
        shards=records,
        nominated_candidate_ids=frontier_ids,
        decision_frontier_candidate_ids=frontier_ids,
        pool_sha256=_candidate_ids_sha256(candidate_ids),
        frontier_sha256=_candidate_ids_sha256(frontier_ids),
        merger_agent_admitted=True,
    )


class _DynamicCandidateLiveRunner:
    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del timeout_seconds, source_start_delays_ms
        ledger = current_agent_budget()
        assert ledger is not None
        core_agents = (
            ("supervise-source-search", AgentRole.SEARCH_SUPERVISOR),
            ("analyze-live-evidence", AgentRole.EVIDENCE_ARBITER),
            ("curate-travel-candidates", AgentRole.CANDIDATE_CURATOR),
            ("criticize-travel-package", AgentRole.RISK_CRITIC),
            ("strategize-package-repair", AgentRole.REPAIR_STRATEGIST),
            ("recriticize-repaired-package", AgentRole.RECRITIC),
            ("recommend-final-decision", AgentRole.ORCHESTRATOR),
            ("explain-final-decision", AgentRole.EXPLANATION),
            ("curate-run-memory", AgentRole.MEMORY_CURATOR),
        )
        for task_id, role in core_agents:
            await ledger.admit(task_id, role)
        for index in range(3):
            await ledger.admit(
                f"candidate-scout-{index:02d}",
                AgentRole.CANDIDATE_CURATOR,
            )

        directive, shard_audit = _dynamic_candidate_audit()
        base = _accepted_run(
            request,
            search_query,
            mode,
            total_cents=900_000,
            complete=True,
        )
        payload = base.model_dump(mode="python")
        payload.update(
            {
                "candidate_scale_directive": directive,
                "candidate_shard_merge_audit": shard_audit,
                "agent_budget_audit": ledger.audit(),
            }
        )
        return LivePackageAgentRun.model_validate(payload)


class _PublicationBudgetLiveSystem(LivePackageAgentSystem):
    """Concrete-type fixture; Flexible owns the mocked publication refresh."""

    def __init__(self) -> None:
        self.exploration_call_count = 0

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        purpose: LiveRunPurpose = LiveRunPurpose.FINAL_PUBLICATION,
        model_agents_enabled: bool = True,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
        memory_access: MemoryAccessContext | None = None,
        allow_recent_quote_reuse: bool = True,
    ) -> LivePackageAgentRun:
        del (
            purpose,
            model_agents_enabled,
            timeout_seconds,
            source_start_delays_ms,
            memory_access,
            allow_recent_quote_reuse,
        )
        self.exploration_call_count += 1
        return _accepted_run(
            intent,
            query,
            mode,
            total_cents=900_000 + self.exploration_call_count,
            complete=True,
        )


_PUBLICATION_AGENT_ROLES = (
    AgentRole.EVIDENCE_ARBITER,
    AgentRole.CANDIDATE_CURATOR,
    AgentRole.RISK_CRITIC,
    AgentRole.REPAIR_STRATEGIST,
    AgentRole.RECRITIC,
    AgentRole.ORCHESTRATOR,
    AgentRole.EXPLANATION,
    AgentRole.MEMORY_CURATOR,
)


def _window() -> FlexibleTravelWindow:
    return FlexibleTravelWindow(
        origin="HGH",
        destination="Tokyo",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 6),
        min_nights=5,
        max_nights=8,
        max_pairs=24,
        adults=2,
        rooms=1,
    )


def _broad_window() -> FlexibleTravelWindow:
    return FlexibleTravelWindow(
        origin="HGH",
        destination="Tokyo",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 10, 1),
        min_nights=3,
        max_nights=60,
        max_pairs=400,
        adults=2,
        rooms=1,
    )


def _two_hundred_fifty_two_date_window() -> FlexibleTravelWindow:
    return FlexibleTravelWindow(
        origin="HGH",
        destination="Tokyo",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 10, 2),
        min_nights=5,
        max_nights=8,
        max_pairs=252,
        adults=2,
        rooms=1,
    )


@pytest.mark.asyncio
async def test_adaptive_full_66_pair_run_keeps_full_execution_and_fixed_pair_concurrency() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="Tokyo",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 11),
        min_nights=3,
        max_nights=8,
        max_pairs=66,
        adults=2,
        rooms=1,
    )
    assert window.universe_size == 66
    runner = _FullUniverseLiveRunner()
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        runner,
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        adaptive_agent_scaling_enabled=True,
    )

    result = await system.run(window, max_pairs=400)

    assert result.scale_directive is not None
    assert result.scale_directive.control_input.direct_final_pair_count == 8
    assert result.scale_directive.background_batches == 9
    assert len(result.pair_runs) == 66
    assert runner.calls == 66
    assert runner.peak_active == 3
    assert len(result.query_plan.selected_pair_ids) == 66
    assert result.query_plan.omitted_pair_ids == ()
    assert result.query_plan.sampled_not_exhaustive is False
    assert result.sampled_not_exhaustive is False
    assert result.query_agentic.model_call_count == 0
    assert result.agent_budget_audit is not None
    assert result.agent_budget_audit.admitted_count == 0
    assert result.agent_budget_audit.rejected_count == 0


@pytest.mark.asyncio
async def test_full_v4_window_runs_with_contiguous_acquisition_budget() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 11),
        min_nights=3,
        max_nights=8,
        max_pairs=66,
        adults=2,
        rooms=1,
    )
    runner = _FullUniverseLiveRunner()
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        runner,
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        adaptive_agent_scaling_enabled=True,
    )

    result = await system.run(window, max_pairs=400)

    assert len(result.pair_runs) == 66
    assert runner.calls == 66
    assert model.requests == []
    assert result.query_plan.unique_acquisition_count == 648
    assert max(item.scheduled_offset_ms for item in result.query_plan.tasks) == 290_000
    assert max(
        item.scheduled_offset_ms
        for execution in result.pair_runs
        for item in execution.query_tasks
    ) == 290_000


@pytest.mark.asyncio
async def test_lead_time_filtered_full_effective_universe_is_not_marked_sampled() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="Tokyo",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 10),
        min_nights=3,
        max_nights=8,
        max_pairs=400,
        adults=2,
        rooms=1,
    )
    runner = _FullUniverseLiveRunner()
    system = FlexibleLiveAgentSystem(
        runner,
        now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
        minimum_departure_lead_days=7,
        adaptive_agent_scaling_enabled=True,
    )

    result = await system.run(window, max_pairs=400)

    assert result.effective_window.earliest_departure == date(2026, 8, 8)
    assert len(result.exploration.candidates) == result.effective_window.universe_size
    assert len(result.pair_runs) == result.effective_window.universe_size
    assert len(result.query_plan.selected_pair_ids) == result.effective_window.universe_size
    assert result.query_plan.omitted_pair_ids == ()
    assert result.query_plan.sampled_not_exhaustive is False
    assert result.sampled_not_exhaustive is False


@pytest.mark.asyncio
async def test_adaptive_query_strategy_runs_independent_date_shards_then_merges() -> None:
    window = _window()
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(
        window,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )
    directive = system._adaptive_scale_directive(
        exploration,
        mode=LiveCoverageMode.STRICT,
    )

    assert directive is not None
    assert directive.date_shards == 2
    assert directive.health_adjusted_model_concurrency == 2
    proposal, agentic, reordered = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=2,
        memory_access=None,
        scale_directive=directive,
    )

    assert proposal is not None
    assert len(proposal.selected_pair_ids) == 2
    assert tuple(item.id for item in reordered.candidates[:2]) == proposal.selected_pair_ids
    assert agentic.stage_count == 3  # two date scouts plus one deterministic-scope merger
    assert agentic.logical_request_count == 6
    assert model.max_active == 2
    assert len(agentic.model_concurrency_audits) == 1
    assert agentic.model_concurrency_audits[0].peak_in_flight == 2
    assert agentic.model_concurrency_audits[0].ceiling == 2
    template_plan = build_agent_template_plan(directive)
    allocations = {item.template_id: item.instances for item in template_plan.allocations}
    assert allocations[AgentTemplateId.DATE_SHARD] == 1
    assert allocations[AgentTemplateId.DATE_MERGER] == 1
    assert directive.browser_concurrency == 6
    assert directive.date_pair_execution_concurrency == 1


@pytest.mark.asyncio
async def test_disabled_adaptive_scaling_keeps_the_single_query_agent_path() -> None:
    window = _window()
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(window)
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
    )

    assert (
        system._adaptive_scale_directive(
            exploration,
            mode=LiveCoverageMode.STRICT,
        )
        is None
    )
    _, agentic, _ = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=2,
        memory_access=None,
    )

    assert agentic.stage_count == 1
    assert agentic.logical_request_count == 2
    assert model.max_active == 1


@pytest.mark.asyncio
async def test_four_hundred_dates_use_bounded_tree_mergers_without_hiding_shards() -> None:
    window = _broad_window()
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(
        window,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert len(exploration.candidates) == 400
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )
    directive = system._adaptive_scale_directive(
        exploration,
        mode=LiveCoverageMode.STRICT,
        exact_pair_budget=2,
    )

    assert directive is not None
    assert directive.date_shards == 34
    assert directive.date_mergers == 4
    assert directive.logical_saturated is False
    proposal, agentic, reordered = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=2,
        memory_access=None,
        scale_directive=directive,
    )

    assert proposal is not None
    assert len(proposal.selected_pair_ids) == 2
    assert tuple(item.id for item in reordered.candidates[:2]) == proposal.selected_pair_ids
    assert agentic.stage_count == 38  # 34 scouts + 3 intermediate + 1 final merger
    assert agentic.logical_request_count == 76
    assert len(model.frontier_row_counts) == 38
    assert max(model.frontier_row_counts) <= 12
    assert len(agentic.model_concurrency_audits) == 2
    assert agentic.model_concurrency_audits[0].admitted_count == 34
    assert agentic.model_concurrency_audits[0].ceiling == 8
    assert agentic.model_concurrency_audits[1].admitted_count == 3
    assert model.max_active <= directive.health_adjusted_model_concurrency


@pytest.mark.asyncio
async def test_saturated_four_hundred_date_request_stops_before_model_or_browser() -> None:
    live = _NeverRunLiveSystem()
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        live,
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )

    with pytest.raises(ValueError, match="自适应 Agent 准入门拒绝"):
        await system.run(
            _broad_window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=8,
        )

    assert live.call_count == 0
    assert model.requests == []


@pytest.mark.asyncio
async def test_text_requirement_admission_reduces_remaining_planning_budget() -> None:
    live = _NeverRunLiveSystem()
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        live,
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )
    ledger = AgentBudgetLedger()
    with bind_agent_budget(ledger):
        await ledger.admit("interpret-package-requirements", AgentRole.CONTEXT)
        with pytest.raises(ValueError, match="全请求只剩 95/96"):
            await system.run(
                _two_hundred_fifty_two_date_window(),
                mode=LiveCoverageMode.STRICT,
                max_pairs=8,
                publication_refresh_minimum_options=2,
            )

    assert ledger.audit().admitted_count == 1
    assert live.call_count == 0
    assert model.requests == []


@pytest.mark.asyncio
async def test_flexible_run_reconciles_actual_candidate_scout_additions() -> None:
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        _DynamicCandidateLiveRunner(),
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )

    run = await system.run(
        _window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=1,
    )

    assert run.scale_directive is not None
    assert run.scale_directive.logical_agent_cap == 12
    assert run.agent_budget_audit is not None
    assert run.agent_budget_audit.admitted_count == 15
    assert len(run.dynamic_candidate_agent_additions) == 1
    addition = run.dynamic_candidate_agent_additions[0]
    assert addition.pool_candidate_count == 65
    assert addition.candidate_scout_count == 3
    assert addition.additional_model_agent_count == 3
    assert addition.merger_agent_template_id == "candidate_merger"
    assert addition.merger_agent_admitted


def _publication_budget_system(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FlexibleLiveAgentSystem, _PublicationBudgetLiveSystem, list[str]]:
    live = _PublicationBudgetLiveSystem()
    model = _AdaptiveQueryModel()
    system = FlexibleLiveAgentSystem(
        live,
        now=lambda: datetime(2026, 7, 1, tzinfo=UTC),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        adaptive_agent_scaling_enabled=True,
    )
    refresh_side_effects: list[str] = []

    async def failed_publication_refresh(
        previous: FlexiblePairExecution,
        *_args: object,
        **_kwargs: object,
    ) -> FlexiblePairExecution:
        attempt = len(refresh_side_effects) + 1
        refresh_side_effects.append(previous.date_pair.id)
        ledger = current_agent_budget()
        assert ledger is not None
        for index, role in enumerate(_PUBLICATION_AGENT_ROLES):
            await ledger.admit(f"publication-attempt-{attempt}-{index}", role)
        return previous.model_copy(
            update={
                "publication_refresh_failure_class": "RuntimeError",
                "publication_refresh_failure_message": "fixture publication rejection",
            }
        )

    monkeypatch.setattr(system, "_is_sealed_exploration", lambda _run: True)
    monkeypatch.setattr(
        system,
        "_refresh_execution_for_publication",
        failed_publication_refresh,
    )
    return system, live, refresh_side_effects


@pytest.mark.asyncio
async def test_publication_fallback_refreezes_directive_and_template_for_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, live, refresh_side_effects = _publication_budget_system(monkeypatch)

    run = await system.run(
        _window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        publication_refresh_minimum_options=1,
    )

    assert live.exploration_call_count == 3
    assert len(refresh_side_effects) == 3
    assert run.final_decision.state.value == "human_block"
    assert run.scale_directive is not None
    assert run.scale_directive.control_input.publication_pair_count == 3
    assert run.agent_template_plan is not None
    assert run.agent_template_plan.state_fingerprint == run.scale_directive.state_fingerprint
    assert run.agent_template_plan.logical_agent_count == run.scale_directive.raw_logical_agents
    assert run.agent_template_plan.deferred_instance_count == 0


@pytest.mark.asyncio
async def test_publication_fallback_budget_shortfall_stops_before_second_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _, refresh_side_effects = _publication_budget_system(monkeypatch)
    ledger = AgentBudgetLedger()

    with bind_agent_budget(ledger):
        for index in range(64):
            await ledger.admit(f"outer-agent-{index:02d}", AgentRole.CONTEXT)
        run = await system.run(
            _window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=3,
            publication_refresh_minimum_options=1,
        )

    assert len(refresh_side_effects) == 1
    assert run.final_decision.state.value == "human_block"
    assert "发布重搜预算不足" in run.final_decision.summary
    assert "浏览器和模型调用前停止" in run.final_decision.summary
    assert run.scale_directive is not None
    assert run.scale_directive.control_input.publication_pair_count == 1
    assert run.agent_template_plan is not None
    assert run.agent_template_plan.state_fingerprint == run.scale_directive.state_fingerprint
    assert ledger.audit().rejected_count == 0
