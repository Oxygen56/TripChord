from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from tripchord.agents.context_budget import BudgetedAgentContextBuilder
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
)
from tripchord.agents.memory import MemoryAccessContext, MemoryStore
from tripchord.agents.model_gateway import (
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ModelUsage,
    ScriptedModelClient,
)
from tripchord.agents.models import AgentRole
from tripchord.agents.rag import EvidenceRagRetriever
from tripchord.planning.adaptive_dates import (
    ExactDatePairObservation,
    RankedTopKDateRefiner,
)
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORMS,
    FlexibleDateExplorer,
    FlexibleTravelWindow,
)
from tripchord.planning.package import PackageIntent
from tripchord.providers.browser_bridge import BrowserSearchQuery


class _NeverRunLiveSystem:
    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        raise AssertionError((intent, query, mode, timeout_seconds, source_start_delays_ms))


@pytest.mark.asyncio
async def test_query_agent_reorders_wider_coarse_pool_but_cannot_expand_budget() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 31),
        min_nights=5,
        max_nights=8,
        max_pairs=12,
        adults=2,
        rooms=1,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(
        window,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    selected = (
        exploration.candidates[7].id,
        exploration.candidates[2].id,
        exploration.candidates[5].id,
    )
    model = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-dates",
                        name="inspect_date_search_space",
                    ),
                ),
                usage=ModelUsage(input_tokens=20, output_tokens=2),
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps(
                    {
                        "summary": "在低价、覆盖不确定性和日期多样性之间分配精查预算",
                        "selected_pair_ids": list(selected[:2]),
                        "selection_reasons": ["保留低价先验", "探索不同周次"],
                        "stop_condition": "两次精查完成后停止",
                        "query_budget_pairs": 2,
                        "uncertainty_flags": ["缺少完整三平台月历"],
                    }
                ),
                usage=ModelUsage(input_tokens=30, output_tokens=20),
            ),
        ),
        model="query-strategist-fixture",
    )
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
    )

    proposal, agentic, reordered = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=2,
        memory_access=None,
    )

    assert proposal is not None
    assert proposal.selected_pair_ids == selected[:2]
    assert proposal.query_budget_pairs == 2
    assert tuple(item.id for item in reordered.candidates[:2]) == selected[:2]
    refiner = RankedTopKDateRefiner()
    first = refiner.next_pair(reordered.candidates, (), exact_pair_budget=2)
    second = refiner.next_pair(
        reordered.candidates,
        (
            ExactDatePairObservation(
                date_pair_id=first.selected_pair_id or "missing",
                recommendable=False,
            ),
        ),
        exact_pair_budget=2,
    )
    assert (first.selected_pair_id, second.selected_pair_id) == selected[:2]
    assert len(reordered.candidates) == 12
    assert agentic.stage_count == 1
    assert agentic.logical_request_count == 2
    assert agentic.http_attempt_count == 2
    assert agentic.model_call_count == 2
    assert agentic.total_token_usage == 72
    assert agentic.stages[0].tool_names == ("inspect_date_search_space",)


@pytest.mark.asyncio
async def test_required_query_agent_repairs_hallucinated_date_id_once() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 3),
        min_nights=5,
        max_nights=5,
        max_pairs=3,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(window)
    valid_id = exploration.candidates[0].id
    model = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-dates",
                        name="inspect_date_search_space",
                    ),
                ),
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps(
                    {
                        "summary": "尝试选择一个日期",
                        "selected_pair_ids": ["date-pair:not-from-tool"],
                        "stop_condition": "精查完成",
                        "query_budget_pairs": 1,
                    }
                )
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps(
                    {
                        "summary": "改为工具可见日期",
                        "selected_pair_ids": [valid_id],
                        "selection_reasons": ["使用可见候选"],
                        "stop_condition": "一个日期对精查完成后停止",
                        "query_budget_pairs": 1,
                    }
                ),
            ),
        )
    )
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
    )

    proposal, agentic, _ = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=1,
        memory_access=None,
    )

    assert proposal is not None
    assert proposal.selected_pair_ids == (valid_id,)
    assert agentic.logical_request_count == 3
    assert agentic.stages[0].proposal_repair_count == 1


@pytest.mark.asyncio
async def test_query_agent_observes_complete_compact_frontier_at_month_scale() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 11),
        latest_departure=date(2026, 8, 31),
        min_nights=5,
        max_nights=8,
        max_pairs=84,
        adults=2,
        rooms=1,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(
        window,
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )
    selected = tuple(item.id for item in exploration.candidates[:3])
    model = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-month-frontier",
                        name="inspect_date_search_space",
                    ),
                ),
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps(
                    {
                        "summary": "在完整可见前沿中选择早末中三个分层日期",
                        "selected_pair_ids": list(selected),
                        "selection_reasons": ["日期分层", "停留时长分层"],
                        "stop_condition": "三个日期对精查完成后停止",
                        "query_budget_pairs": 3,
                        "uncertainty_flags": ["无完整三平台月历"],
                    }
                ),
            ),
        )
    )
    store = MemoryStore()
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
        context_builder=BudgetedAgentContextBuilder(EvidenceRagRetriever(store)),
        memory_store=store,
    )

    proposal, agentic, _ = await system._query_strategy(
        window,
        exploration,
        exact_pair_budget=3,
        memory_access=MemoryAccessContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            trip_id="trip-a",
        ),
    )

    assert len(exploration.candidates) == 84
    assert proposal is not None
    assert proposal.selected_pair_ids == selected
    trace = agentic.stages[0]
    assert trace.context_token_budget == 2_400
    assert trace.truncated_tool_observations == 0
    assert trace.tool_observation_tokens >= 400


def test_query_frontier_stays_bounded_at_maximum_exact_budget() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 31),
        min_nights=5,
        max_nights=8,
        max_pairs=124,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(window)

    frontier = FlexibleLiveAgentSystem._query_strategy_frontier(
        exploration.candidates,
        exact_pair_budget=8,
    )

    assert len(exploration.candidates) == 124
    assert len(frontier) == 12
    assert frontier[:8] == exploration.candidates[:8]
    assert frontier[-1] == exploration.candidates[-1]


@pytest.mark.asyncio
async def test_required_query_agent_rejects_persistent_budget_shrink() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 3),
        min_nights=5,
        max_nights=5,
        max_pairs=3,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(window)
    ids = tuple(item.id for item in exploration.candidates)
    model = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-dates",
                        name="inspect_date_search_space",
                    ),
                ),
            ),
            *(
                ModelResponse(
                    provider="ignored",
                    model="ignored",
                    text=json.dumps(
                        {
                            "summary": "错误缩小硬预算",
                            "selected_pair_ids": list(ids[:2]),
                            "selection_reasons": ["错误地少查一个"],
                            "stop_condition": "提前停止",
                            "query_budget_pairs": 2,
                        }
                    ),
                )
                for _ in range(2)
            ),
        )
    )
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
    )

    with pytest.raises(ValueError) as raised:
        await system._query_strategy(
            window,
            exploration,
            exact_pair_budget=3,
            memory_access=None,
        )

    diagnostic = str(raised.value)
    assert "proposal_policy:query_strategy_frontier_and_budget_v1" in diagnostic
    assert "proposal_repairs=1" in diagnostic


@pytest.mark.asyncio
async def test_required_query_agent_exposes_bounded_structured_failure_diagnostic() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 3),
        min_nights=5,
        max_nights=5,
        max_pairs=3,
    )
    exploration = FlexibleDateExplorer(LIVE_V5_PLATFORMS).explore(window)
    model = ScriptedModelClient(
        (
            ModelResponse(
                provider="ignored",
                model="ignored",
                tool_calls=(
                    ModelToolCall(
                        id="inspect-dates",
                        name="inspect_date_search_space",
                    ),
                ),
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps({"summary": "缺少必填字段"}),
            ),
            ModelResponse(
                provider="ignored",
                model="ignored",
                text=json.dumps({"summary": "修复后仍缺少必填字段"}),
            ),
        )
    )
    system = FlexibleLiveAgentSystem(
        _NeverRunLiveSystem(),
        model_router=ModelRouter(
            {AgentRole.QUERY_STRATEGIST: model},
            high_risk_client=model,
        ),
        model_agents_required=True,
    )

    with pytest.raises(ValueError) as raised:
        await system._query_strategy(
            window,
            exploration,
            exact_pair_budget=1,
            memory_access=None,
        )

    diagnostic = str(raised.value)
    assert "必需的查询策略 Agent 未能完成结构化决策" in diagnostic
    assert "ValidationError" in diagnostic
    assert "logical_requests=3" in diagnostic
    assert "proposal_repairs=1" in diagnostic
    assert "tool_protocol_repairs=0" in diagnostic
    assert len(diagnostic) < 700
