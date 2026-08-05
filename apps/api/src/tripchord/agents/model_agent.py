from __future__ import annotations

import json
from typing import Any

from pydantic import JsonValue, TypeAdapter

from tripchord.agents.context import ContextEngine
from tripchord.agents.model_gateway import (
    ModelMessage,
    ModelRequest,
    ModelRouter,
    ModelTool,
    ModelToolResult,
    compact_json,
)
from tripchord.agents.models import AgentRole, AgentTask, AgentTaskResult
from tripchord.agents.tools import ToolCall, ToolRegistry


class ModelToolAgent:
    """A bounded model/tool loop. The model chooses tools and observes their receipts."""

    def __init__(
        self,
        role: AgentRole,
        router: ModelRouter,
        *,
        system_prompt: str,
        max_tool_rounds: int = 4,
    ) -> None:
        self.role = role
        self._router = router
        self._system_prompt = system_prompt
        self._max_tool_rounds = max_tool_rounds

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        tool_registry: ToolRegistry,
    ) -> AgentTaskResult:
        context = context_engine.build_pack(task)
        messages = [
            ModelMessage(
                role="user",
                content=compact_json(
                    {
                        "task": task.model_dump(mode="json"),
                        "evidence": [item.model_dump(mode="json") for item in context.evidence],
                    }
                ),
            )
        ]
        specs = tuple(
            ModelTool(
                name=name,
                description=tool_registry.spec(name).description,
                input_schema=tool_registry.spec(name).input_schema,
            )
            for name in task.allowed_tools
        )
        total_tokens = 0
        provider: str | None = None
        model: str | None = None
        receipts: list[dict[str, JsonValue]] = []
        raw_risk = task.input.get("risk_level", 0)
        risk_level = raw_risk if isinstance(raw_risk, int) else 0
        for round_index in range(self._max_tool_rounds + 1):
            routed = await self._router.complete(
                ModelRequest(
                    role=self.role,
                    system=self._system_prompt,
                    messages=tuple(messages),
                    tools=specs,
                    risk_level=risk_level,
                )
            )
            response = routed.response
            total_tokens += response.usage.total_tokens
            provider, model = response.provider, response.model
            if not response.tool_calls:
                output = self._parse_final_output(response.text)
                output["tool_receipts"] = TypeAdapter(JsonValue).validate_python(receipts)
                output["model_route"] = routed.route.model_dump(mode="json")
                return AgentTaskResult(
                    task_id=task.id,
                    agent_role=self.role,
                    success=True,
                    summary=str(output.get("summary", response.text or "模型任务完成")),
                    output=output,
                    model_provider=provider,
                    model_name=model,
                    token_usage=total_tokens,
                )
            if round_index >= self._max_tool_rounds:
                break
            undeclared = [
                call.name
                for call in response.tool_calls
                if call.name not in task.allowed_tools
            ]
            if undeclared:
                raise PermissionError(f"model selected undeclared tool: {undeclared[0]}")
            round_tool_results: list[ModelToolResult] = []
            for call in response.tool_calls:
                receipt = await tool_registry.invoke(
                    ToolCall(
                        id=call.id,
                        tool_name=call.name,
                        task_id=task.id,
                        agent_role=self.role,
                        arguments=call.arguments,
                    )
                )
                serialized = receipt.model_dump(mode="json")
                receipts.append(serialized)
                round_tool_results.append(
                    ModelToolResult(
                        tool_call_id=call.id,
                        content=compact_json({"tool_result": serialized}),
                    )
                )
            messages.extend(
                (
                    ModelMessage(
                        role="assistant",
                        content=response.text,
                        reasoning_content=response.reasoning_content,
                        tool_calls=response.tool_calls,
                    ),
                    ModelMessage(
                        role="user",
                        tool_results=tuple(round_tool_results),
                    ),
                )
            )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=False,
            summary="模型超过允许的工具调用轮数",
            failure_class="tool_loop_exhausted",
            model_provider=provider,
            model_name=model,
            token_usage=total_tokens,
        )

    def _parse_final_output(self, text: str) -> dict[str, JsonValue]:
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            return {"summary": text}
        if not isinstance(parsed, dict):
            return {"summary": text, "value": TypeAdapter(JsonValue).validate_python(parsed)}
        return TypeAdapter(dict[str, JsonValue]).validate_python(parsed)
