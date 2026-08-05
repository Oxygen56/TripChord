from __future__ import annotations

import json

from pydantic import JsonValue, TypeAdapter

from tripchord.agents.model_gateway import (
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ScriptedModelClient,
)
from tripchord.agents.models import AgentRole, ToolPermission
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec
from tripchord.agents.travel_runtime import TravelMultiAgentSystem
from tripchord.domain.offers import TravelOffer
from tripchord.package_data import read_replay_offers
from tripchord.planning.problem import PlanningProblem


def _response(text: str = "") -> ModelResponse:
    return ModelResponse(text=text, provider="replay", model="agent-demo-v1")


def _specialist(tool_name: str) -> ScriptedModelClient:
    return ScriptedModelClient(
        (
            ModelResponse(
                tool_calls=(ModelToolCall(id=f"demo-{tool_name}", name=tool_name),),
                provider="replay",
                model="agent-demo-v1",
            ),
            _response('{"summary":"已读取回放工具证据；未声称为实时库存"}'),
        ),
        model=f"replay-{tool_name}",
    )


def build_replay_agent_system(problem: PlanningProblem) -> TravelMultiAgentSystem:
    selected_ids = [item.id for item in problem.activities[:2]]
    clients = {
        AgentRole.TRANSPORT: _specialist("search_transport"),
        AgentRole.LODGING: _specialist("search_lodging"),
        AgentRole.POI: _specialist("search_poi"),
        AgentRole.WEATHER: _specialist("search_weather"),
        AgentRole.NEURAL_PLANNER: ScriptedModelClient(
            (
                _response(
                    json.dumps(
                        {
                            "selected_activity_ids": selected_ids,
                            "summary": "回放神经规划候选",
                        },
                        ensure_ascii=False,
                    )
                ),
            ),
            model="replay-neural-planner",
        ),
        AgentRole.CRITIC: ScriptedModelClient(
            (_response('{"recommendation":"candidate:cp-sat","reasons":[]}'),),
            model="replay-critic",
        ),
    }
    control = ScriptedModelClient(
        (
            _response(
                '{"summary":"回放证据口径已仲裁","conflicts":[],'
                '"non_comparable":["回放报价不可视为实时可订价格"]}'
            ),
            _response(
                '{"selected_candidate_id":"candidate:cp-sat",'
                '"summary":"主控选择通过确定性验证的 CP-SAT 候选"}'
            ),
        ),
        model="replay-strong-control",
    )
    tools = ToolRegistry()
    offers = TypeAdapter(tuple[TravelOffer, ...]).validate_json(
        read_replay_offers()
    )

    async def search(call: ToolCall) -> dict[str, JsonValue]:
        if call.tool_name == "search_transport":
            payload: object = {
                "source_mode": "replay",
                "offers": [
                    item.model_dump(mode="json")
                    for item in offers
                    if item.kind.value in {"flight", "rail"}
                ],
            }
        elif call.tool_name == "search_lodging":
            lodging = [item for item in offers if item.kind.value == "lodging"]
            payload = {
                "source_mode": "replay",
                "hotel_breakfast": any(
                    "不含" not in (item.terms.meal_summary or "") for item in lodging
                ),
                "offers": [item.model_dump(mode="json") for item in lodging],
            }
        elif call.tool_name == "search_poi":
            payload = {
                "source_mode": "replay",
                "activities": [item.model_dump(mode="json") for item in problem.activities],
            }
        else:
            payload = {
                "source_mode": "replay",
                "forecast": "回放环境未提供实时天气；出发前需重新查询",
            }
        return TypeAdapter(dict[str, JsonValue]).validate_python(payload)

    for name, role in {
        "search_transport": AgentRole.TRANSPORT,
        "search_lodging": AgentRole.LODGING,
        "search_poi": AgentRole.POI,
        "search_weather": AgentRole.WEATHER,
    }.items():
        tools.register(
            ToolSpec(
                name=name,
                description=f"回放只读工具 {name}",
                permission=ToolPermission.READ_ONLY_EXTERNAL,
                allowed_roles=(role,),
            ),
            search,
        )
    return TravelMultiAgentSystem(ModelRouter(clients, high_risk_client=control), tools)
