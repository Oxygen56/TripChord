from __future__ import annotations

import httpx
import pytest
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.model_agent import ModelToolAgent
from tripchord.agents.model_gateway import (
    AnthropicMessagesClient,
    ModelGatewayError,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ModelUsage,
    ScriptedModelClient,
)
from tripchord.agents.models import AgentRole, AgentTask, ToolPermission
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec


@pytest.mark.asyncio
async def test_model_agent_uses_tool_result_before_final_decision() -> None:
    model = ScriptedModelClient(
        (
            ModelResponse(
                text="我先检查工具证据",
                reasoning_content="thinking-state-must-be-replayed",
                tool_calls=(
                    ModelToolCall(
                        id="call-1",
                        name="search_rail",
                        arguments={"origin": "上海", "destination": "北京"},
                    ),
                ),
                provider="placeholder",
                model="placeholder",
                usage=ModelUsage(input_tokens=10, output_tokens=3),
            ),
            ModelResponse(
                text='{"summary":"选择 G2，因为工具返回价格最低","offer_id":"rail-g2"}',
                provider="placeholder",
                model="placeholder",
                usage=ModelUsage(input_tokens=12, output_tokens=8),
            ),
        )
    )
    router = ModelRouter(
        {AgentRole.TRANSPORT: model},
        high_risk_client=model,
    )
    tools = ToolRegistry()

    async def search(_: ToolCall) -> dict[str, object]:
        return {"offers": [{"id": "rail-g2", "price": 553}]}

    tools.register(
        ToolSpec(
            name="search_rail",
            description="查询高铁候选",
            permission=ToolPermission.READ_ONLY_EXTERNAL,
            allowed_roles=(AgentRole.TRANSPORT,),
            input_schema={
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["origin", "destination"],
            },
        ),
        search,
    )
    agent = ModelToolAgent(
        AgentRole.TRANSPORT,
        router,
        system_prompt="基于证据选择交通，不得编造报价。",
    )

    result = await agent.execute(
        AgentTask(
            id="transport",
            role=AgentRole.TRANSPORT,
            goal="选择上海到北京交通",
            allowed_tools=("search_rail",),
        ),
        ContextEngine(EvidenceBlackboard()),
        tools,
    )

    assert result.success
    assert result.output["offer_id"] == "rail-g2"
    assert len(result.output["tool_receipts"]) == 1
    assert result.token_usage == 33
    assert "rail-g2" in model.requests[1].messages[-1].tool_results[0].content
    assistant_turn = model.requests[1].messages[-2]
    assert assistant_turn.content == "我先检查工具证据"
    assert assistant_turn.reasoning_content == "thinking-state-must-be-replayed"
    assert len(assistant_turn.tool_calls) == 1
    assert assistant_turn.tool_calls[0].name == "search_rail"


@pytest.mark.asyncio
async def test_model_router_falls_back_and_discloses_route() -> None:
    primary = ScriptedModelClient(())
    fallback = ScriptedModelClient(
        (ModelResponse(text="ok", provider="x", model="x"),),
        model="fallback",
    )
    router = ModelRouter(
        {AgentRole.POI: primary},
        high_risk_client=primary,
        fallback_client=fallback,
    )
    routed = await router.complete(ModelRequest(role=AgentRole.POI, system="x", messages=()))
    assert routed.response.text == "ok"
    assert routed.route.fallback_used
    assert routed.route.reason == "primary_failed"
    assert routed.route.primary_attempt_count == 1
    assert routed.route.fallback_attempt_count == 1
    assert routed.route.http_attempt_count == 2


@pytest.mark.asyncio
async def test_model_router_preserves_split_attempt_counts_when_fallback_also_fails() -> None:
    primary = ScriptedModelClient((), model="primary")
    fallback = ScriptedModelClient((), model="fallback")
    router = ModelRouter(
        {AgentRole.POI: primary},
        high_risk_client=primary,
        fallback_client=fallback,
    )

    with pytest.raises(ModelGatewayError) as raised:
        await router.complete(ModelRequest(role=AgentRole.POI, system="x", messages=()))

    assert raised.value.primary_attempt_count == 1
    assert raised.value.fallback_attempt_count == 1
    assert raised.value.attempt_count == 2


@pytest.mark.asyncio
async def test_anthropic_adapter_parses_text_tool_and_usage_without_live_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "test-key"
        assert request.url.path == "/v1/messages"
        assert b'"messages":[]' in request.content
        return httpx.Response(
            200,
            json={
                "id": "msg-test",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 5},
                "content": [
                    {"type": "text", "text": "先查询"},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "search",
                        "input": {"city": "北京"},
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AnthropicMessagesClient(
            api_key="test-key",
            model="claude-test",
            base_url="https://unit.test",
            http_client=http_client,
        )
        response = await client.complete(ModelRequest(role=AgentRole.POI, system="x", messages=()))
    assert response.text == "先查询"
    assert response.tool_calls[0].arguments == {"city": "北京"}
    assert response.usage.total_tokens == 12
