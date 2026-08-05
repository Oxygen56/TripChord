from __future__ import annotations

from datetime import date

import pytest
from tripchord.agents.model_gateway import (
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ScriptedModelClient,
)
from tripchord.agents.models import (
    AgentRole,
    DecisionState,
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
    ToolPermission,
)
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec
from tripchord.agents.travel_runtime import TravelMultiAgentSystem
from tripchord.domain.common import Money
from tripchord.domain.trip import TripSpec
from tripchord.planning.problem import ActivityAvailability, ActivityCandidate, PlanningProblem
from tripchord.planning.verifier import PlanVerifier


def problem() -> PlanningProblem:
    trip_date = date(2026, 10, 2)
    return PlanningProblem(
        trip=TripSpec(
            origin="上海",
            destinations=("北京",),
            start_date=trip_date,
            end_date=trip_date,
            budget=Money(amount="500", currency="CNY"),
            must_visit=("故宫博物院",),
        ),
        activities=(
            ActivityCandidate(
                id="forbidden-city",
                title="故宫博物院",
                duration_minutes=120,
                cost_cents=6000,
                utility=300,
                must_visit=True,
                availability=(
                    ActivityAvailability(
                        date=trip_date,
                        start_minute=9 * 60,
                        end_minute=20 * 60,
                    ),
                ),
                source_refs=("replay:poi:forbidden-city",),
            ),
            ActivityCandidate(
                id="temple",
                title="天坛公园",
                duration_minutes=90,
                cost_cents=3400,
                utility=180,
                availability=(
                    ActivityAvailability(
                        date=trip_date,
                        start_minute=9 * 60,
                        end_minute=20 * 60,
                    ),
                ),
                source_refs=("replay:poi:temple",),
            ),
        ),
    )


def specialist_client(tool_name: str) -> ScriptedModelClient:
    return ScriptedModelClient(
        (
            ModelResponse(
                tool_calls=(ModelToolCall(id=f"call-{tool_name}", name=tool_name),),
                provider="x",
                model="x",
            ),
            ModelResponse(text='{"summary":"已基于工具证据完成查询"}', provider="x", model="x"),
        ),
        model=f"specialist-{tool_name}",
    )


def build_system(
    *,
    neural_text: str,
    orchestrator_candidate: str,
    hotel_breakfast: bool = True,
) -> tuple[TravelMultiAgentSystem, dict[AgentRole, ScriptedModelClient]]:
    clients = {
        AgentRole.TRANSPORT: specialist_client("search_transport"),
        AgentRole.LODGING: specialist_client("search_lodging"),
        AgentRole.POI: specialist_client("search_poi"),
        AgentRole.WEATHER: specialist_client("search_weather"),
        AgentRole.NEURAL_PLANNER: ScriptedModelClient(
            (ModelResponse(text=neural_text, provider="x", model="x"),),
            model="neural-planner",
        ),
        AgentRole.CRITIC: ScriptedModelClient(
            (
                ModelResponse(
                    text='{"recommendation":"compare deterministic findings","reasons":[]}',
                    provider="x",
                    model="x",
                ),
            ),
            model="heterogeneous-critic",
        ),
    }
    control = ScriptedModelClient(
        (
            ModelResponse(
                text=(
                    '{"summary":"回放来源口径一致","usable_evidence_refs":[],'
                    '"conflicts":[],"non_comparable":[]}'
                ),
                provider="x",
                model="x",
            ),
            ModelResponse(
                text=(
                    f'{{"selected_candidate_id":"{orchestrator_candidate}",'
                    '"summary":"主控综合证据完成裁决"}'
                ),
                provider="x",
                model="x",
            ),
        ),
        model="strong-control",
    )
    router = ModelRouter(clients, high_risk_client=control)
    tools = ToolRegistry()
    role_by_tool = {
        "search_transport": AgentRole.TRANSPORT,
        "search_lodging": AgentRole.LODGING,
        "search_poi": AgentRole.POI,
        "search_weather": AgentRole.WEATHER,
    }

    async def query(call: ToolCall) -> dict[str, object]:
        output: dict[str, object] = {
            "source_mode": "replay",
            "query": call.tool_name,
            "items": [{"id": f"fixture:{call.tool_name}", "status": "available"}],
        }
        if call.tool_name == "search_lodging":
            output["hotel_breakfast"] = hotel_breakfast
        return output

    for name, role in role_by_tool.items():
        tools.register(
            ToolSpec(
                name=name,
                description=f"只读回放查询 {name}",
                permission=ToolPermission.READ_ONLY_EXTERNAL,
                allowed_roles=(role,),
            ),
            query,
        )
    return TravelMultiAgentSystem(router, tools), clients


@pytest.mark.asyncio
async def test_multi_agent_vertical_loop_runs_parallel_dual_planners_and_accepts() -> None:
    system, clients = build_system(
        neural_text=('{"selected_activity_ids":["forbidden-city"],"summary":"神经规划保留必去项"}'),
        orchestrator_candidate="candidate:cp-sat",
    )
    preferences = PreferenceConstitution(
        rules=(
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.REQUIRED,
                source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                weight=1,
                reason="用户明确要求早餐",
            ),
        )
    )

    outcome = await system.run(problem(), preferences)

    assert outcome.decision.state == DecisionState.ACCEPT
    assert outcome.selected_candidate_id == "candidate:cp-sat"
    assert outcome.final_plan is not None
    assert PlanVerifier().verify(problem().trip, outcome.final_plan) == ()
    assert outcome.scheduler.max_parallel_tasks == 4
    assert {record.topic for record in outcome.evidence} >= {
        "transport",
        "lodging",
        "poi",
        "weather",
        "preference",
        "arbitration",
        "candidate",
        "critique",
    }
    assert clients[AgentRole.CRITIC].requests
    assert outcome.preferences.effective("hotel_breakfast") is not None


@pytest.mark.asyncio
async def test_orchestrator_rejects_bad_neural_candidate_then_repairs_and_redecides() -> None:
    system, _ = build_system(
        neural_text=(
            '{"selected_activity_ids":["forbidden-city"],'
            '"shift_first_minutes":720,"summary":"故意制造晚间越界以测试修复"}'
        ),
        orchestrator_candidate="candidate:neural",
    )

    outcome = await system.run(problem())

    task_ids = [task.id for task in outcome.scheduler.graph.tasks]
    assert "repair-selected" in task_ids
    assert "orchestrator-final" in task_ids
    assert outcome.decision.state == DecisionState.ACCEPT
    assert outcome.selected_candidate_id == "candidate:repaired"
    assert outcome.final_plan is not None
    assert outcome.final_plan.version == 2
    assert PlanVerifier().verify(problem().trip, outcome.final_plan) == ()
    trace_kinds = [event.kind for event in outcome.scheduler.trace]
    assert trace_kinds.count("task_spawned") == 2


@pytest.mark.asyncio
async def test_explicit_required_preference_cannot_be_silently_overridden() -> None:
    system, _ = build_system(
        neural_text='{"selected_activity_ids":["forbidden-city"],"summary":"候选"}',
        orchestrator_candidate="candidate:cp-sat",
        hotel_breakfast=False,
    )
    preferences = PreferenceConstitution(
        rules=(
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.REQUIRED,
                source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                weight=1,
                reason="用户本人明确要求早餐",
            ),
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.WEIGHTED,
                source=PreferenceSource.INFERRED_CURRENT_CONTEXT,
                weight=0.1,
                reason="Agent 认为早餐风险较低",
            ),
        )
    )

    outcome = await system.run(problem(), preferences)

    assert outcome.decision.state == DecisionState.REPLAN_OR_BLOCK
    assert outcome.decision.verifier_violations == ("preference:hotel_breakfast",)
    assert "偏好" in outcome.decision.summary
    assert "repair-selected" not in [task.id for task in outcome.scheduler.graph.tasks]
