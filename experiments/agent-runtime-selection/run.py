from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import inspect
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
CASE_PATH = ROOT / "docs/case-studies/maldives-2026-08-19/case-bundle.json"
sys.path.insert(0, str(ROOT / "apps/api/src"))


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str
    selected_candidate_ids: list[str]
    replaced_component_id: str | None = None
    preserved_component_ids: list[str]
    constraint_conflicts: list[str]
    evidence_refs: list[str]
    summary: str = Field(min_length=1)


def load_case() -> dict[str, Any]:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


def scenario(case: dict[str, Any], name: str) -> dict[str, Any]:
    if name == "baseline":
        return {"name": name, "event": None}
    if name == "controlled_recovery":
        return {"name": name, "event": case["controlled_recovery"]}
    return {
        "name": "invalid_or_ambiguous",
        "event": {"flight_taxes": "unknown", "two_seat_inventory": "unknown"},
    }


class DomainTools:
    """The one tool contract shared by every adapter."""

    def __init__(self, case: dict[str, Any], current: dict[str, Any]):
        self.case = case
        self.current = current
        self.calls: list[dict[str, Any]] = []

    def _record(
        self, name: str, arguments: dict[str, Any], output: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"tool_name": name, "arguments": arguments, "success": True})
        return output

    def inspect_requirements(self) -> dict[str, Any]:
        return self._record(
            "inspect_requirements", {}, {"user_fact_lock": self.case["user_fact_lock"]}
        )

    def inspect_candidate(self, candidate_id: str = "main") -> dict[str, Any]:
        return self._record(
            "inspect_candidate",
            {"candidate_id": candidate_id},
            {"main_candidate": self.case["main_candidate"]},
        )

    def verify_candidate(self, candidate_id: str = "main") -> dict[str, Any]:
        event = self.current["event"]
        conflicts = ["historical_case_human_block"]
        if event and event.get("target"):
            return self._record(
                "verify_candidate",
                {"candidate_id": candidate_id},
                {
                    "valid": False,
                    "decision": "replan",
                    "replace": event["target"],
                    "replacement": event.get("replacement_trip_id"),
                    "preserved_ratio": 0.75,
                    "conflicts": conflicts,
                },
            )
        return self._record(
            "verify_candidate",
            {"candidate_id": candidate_id},
            {
                "valid": False,
                "decision": "human_block",
                "conflicts": conflicts,
            },
        )

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        if name == "inspect_requirements":
            return self.inspect_requirements()
        if name == "inspect_candidate":
            return self.inspect_candidate(**args)
        if name == "verify_candidate":
            return self.verify_candidate(**args)
        raise ValueError(f"unknown tool: {name}")


SCRIPTED_OUTPUT = {
    "decision": "human_block",
    "selected_candidate_ids": ["main"],
    "replaced_component_id": None,
    "preserved_component_ids": [],
    "constraint_conflicts": ["historical_case_human_block"],
    "evidence_refs": ["main_candidate_manifest", "official_flight_manifest"],
    "summary": "历史证据存在未决冲突，保持人工阻断。",
}


def expected(name: str) -> dict[str, Any]:
    value = dict(SCRIPTED_OUTPUT)
    if name == "controlled_recovery":
        value.update(
            {
                "decision": "replan",
                "replaced_component_id": "icom:trip:7989:maafushi-v3-2026-09-03-09-09",
                "preserved_component_ids": ["flight", "Arena lodging", "iCom 8564"],
            }
        )
    return value


async def custom_run(
    case: dict[str, Any], current: dict[str, Any], tools: DomainTools
) -> dict[str, Any]:
    import ortools  # noqa: F401
    from tripchord.agents.context import ContextEngine, EvidenceBlackboard
    from tripchord.agents.model_agent import ModelToolAgent
    from tripchord.agents.model_gateway import (
        ModelResponse,
        ModelRouter,
        ModelToolCall,
        ScriptedModelClient,
    )
    from tripchord.agents.models import AgentRole, AgentTask
    from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec

    scripted = ScriptedModelClient(
        (
            ModelResponse(
                tool_calls=(ModelToolCall(id="call-1", name="inspect_requirements"),),
                provider="scripted",
                model="fixture",
            ),
            ModelResponse(
                tool_calls=(ModelToolCall(id="call-2", name="inspect_candidate"),),
                provider="scripted",
                model="fixture",
            ),
            ModelResponse(
                tool_calls=(ModelToolCall(id="call-3", name="verify_candidate"),),
                provider="scripted",
                model="fixture",
            ),
            ModelResponse(
                text=json.dumps(expected(current["name"]), ensure_ascii=False),
                provider="scripted",
                model="fixture",
            ),
        ),
        model="fixture",
    )
    router = ModelRouter({AgentRole.EVIDENCE_ARBITER: scripted}, high_risk_client=scripted)
    registry = ToolRegistry()

    async def handler(call: ToolCall) -> dict[str, Any]:
        return tools.call(call.tool_name, call.arguments)

    for name in ("inspect_requirements", "inspect_candidate", "verify_candidate"):
        registry.register(
            ToolSpec(
                name=name,
                description=name,
                permission=1,
                allowed_roles=(AgentRole.EVIDENCE_ARBITER,),
            ),
            handler,
        )
    task = AgentTask(
        id="framework-selection",
        role=AgentRole.EVIDENCE_ARBITER,
        goal="Use read-only tools and produce the proposal.",
        allowed_tools=("inspect_requirements", "inspect_candidate", "verify_candidate"),
    )
    result = await ModelToolAgent(
        AgentRole.EVIDENCE_ARBITER, router, system_prompt="Return JSON only.", max_tool_rounds=4
    ).execute(task, ContextEngine(EvidenceBlackboard()), registry)
    if not result.success:
        raise RuntimeError(result.failure_class or "custom agent failed")
    return {k: v for k, v in result.output.items() if k in Proposal.model_fields}


async def pydanticai_run(
    case: dict[str, Any], current: dict[str, Any], tools: DomainTools
) -> dict[str, Any]:
    mod = importlib.import_module("pydantic_ai")
    Agent = mod.Agent
    agent = Agent("test", output_type=Proposal)

    @agent.tool_plain
    def inspect_requirements() -> dict[str, Any]:
        return tools.call("inspect_requirements")

    @agent.tool_plain
    def inspect_candidate(candidate_id: str = "main") -> dict[str, Any]:
        return tools.call("inspect_candidate", {"candidate_id": candidate_id})

    @agent.tool_plain
    def verify_candidate(candidate_id: str = "main") -> dict[str, Any]:
        return tools.call("verify_candidate", {"candidate_id": candidate_id})

    from pydantic_ai.models.test import TestModel

    class OrderedTestModel(TestModel):
        def __init__(self) -> None:
            super().__init__(call_tools=[], custom_output_args=expected(current["name"]))
            self._step = 0

        def _request(self, messages, model_settings, model_request_parameters):
            names = ["inspect_requirements", "inspect_candidate", "verify_candidate"]
            from pydantic_ai.messages import ModelResponse, ToolCallPart

            if self._step < 3:
                name = names[self._step]
                self._step += 1
                return ModelResponse(
                    parts=[ToolCallPart(name, {}, tool_call_id=f"call-{self._step}")],
                    model_name="ordered-test",
                )
            return super()._request(messages, model_settings, model_request_parameters)

    result = await agent.run(
        "Use the read-only tools, then produce the historical-case decision.",
        model=OrderedTestModel(),
    )
    return result.output.model_dump()


async def langgraph_run(
    case: dict[str, Any], current: dict[str, Any], tools: DomainTools
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import MessagesState
    from langgraph.prebuilt import ToolNode

    @tool
    def inspect_requirements() -> dict[str, Any]:
        """Read the fixed user requirements."""
        return tools.call("inspect_requirements")

    @tool
    def inspect_candidate(candidate_id: str = "main") -> dict[str, Any]:
        """Read the historical candidate."""
        return tools.call("inspect_candidate", {"candidate_id": candidate_id})

    @tool
    def verify_candidate(candidate_id: str = "main") -> dict[str, Any]:
        """Run the deterministic candidate audit."""
        return tools.call("verify_candidate", {"candidate_id": candidate_id})

    declared = [inspect_requirements, inspect_candidate, verify_candidate]

    async def scripted_model(state):
        tool_count = sum(
            1 for message in state["messages"] if getattr(message, "type", "") == "tool"
        )
        if tool_count >= 3:
            return {
                "messages": [
                    AIMessage(content=json.dumps(expected(current["name"]), ensure_ascii=False))
                ]
            }
        name = ["inspect_requirements", "inspect_candidate", "verify_candidate"][tool_count]
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": {},
                            "id": f"call-{tool_count + 1}",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }

    def route(state) -> str:
        return "tools" if getattr(state["messages"][-1], "tool_calls", []) else END

    graph = StateGraph(MessagesState)
    graph.add_node("model", scripted_model)
    graph.add_node("tools", ToolNode(declared))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route)
    graph.add_edge("tools", "model")
    result = await graph.compile().ainvoke(
        {"messages": [HumanMessage(content="offline scripted case")]}
    )
    return json.loads(result["messages"][-1].content)


async def openai_agents_run(
    case: dict[str, Any], current: dict[str, Any], tools: DomainTools
) -> dict[str, Any]:
    from agents import Agent, Model, RunConfig, Runner, function_tool
    from agents.items import ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    class ScriptedModel(Model):
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id,
            conversation_id,
            prompt,
        ):
            self.calls += 1
            if self.calls <= 3:
                name = ["inspect_requirements", "inspect_candidate", "verify_candidate"][
                    self.calls - 1
                ]
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name=name,
                        arguments="{}",
                        call_id=f"call-{self.calls}",
                        id=f"call-{self.calls}",
                        status="completed",
                    )
                ]
            else:
                output = [
                    ResponseOutputMessage(
                        type="message",
                        id="msg-final",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text",
                                text=json.dumps(expected(current["name"]), ensure_ascii=False),
                                annotations=[],
                                logprobs=None,
                            )
                        ],
                    )
                ]
            return ModelResponse(
                output=output, usage=Usage(requests=1), response_id=f"response-{self.calls}"
            )

        async def stream_response(self, *args, **kwargs):
            if False:
                yield None

    scripted = ScriptedModel()

    @function_tool
    def inspect_requirements() -> dict[str, Any]:
        """Read the fixed user requirements."""
        return tools.call("inspect_requirements")

    @function_tool
    def inspect_candidate(candidate_id: str = "main") -> dict[str, Any]:
        """Read the historical candidate."""
        return tools.call("inspect_candidate", {"candidate_id": candidate_id})

    @function_tool
    def verify_candidate(candidate_id: str = "main") -> dict[str, Any]:
        """Run the deterministic candidate audit."""
        return tools.call("verify_candidate", {"candidate_id": candidate_id})

    agent = Agent(
        name="tripchord_selection",
        instructions="Use every read-only tool, then return JSON.",
        model=scripted,
        tools=[inspect_requirements, inspect_candidate, verify_candidate],
        output_type=Proposal,
    )
    result = await Runner.run(
        agent,
        "offline scripted case",
        max_turns=6,
        run_config=RunConfig(tracing_disabled=True),
    )
    if result.final_output is None:
        raise RuntimeError("openai-agents produced no final output")
    return (
        result.final_output.model_dump()
        if isinstance(result.final_output, Proposal)
        else json.loads(result.final_output)
    )


async def google_adk_run(
    case: dict[str, Any], current: dict[str, Any], tools: DomainTools
) -> dict[str, Any]:
    mod = importlib.import_module("google.adk.agents")
    LlmAgent = mod.LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    class ScriptedLlm(BaseLlm):
        async def generate_content_async(self, llm_request: Any, stream: bool = False):
            tool_count = sum(
                1
                for content in llm_request.contents
                for part in content.parts or []
                if part.function_response
            )
            if tool_count >= 3:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                text=json.dumps(expected(current["name"]), ensure_ascii=False)
                            )
                        ],
                    )
                )
            else:
                name = ["inspect_requirements", "inspect_candidate", "verify_candidate"][tool_count]
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name=name, args={"candidate_id": "main"} if tool_count else {}
                                )
                            )
                        ],
                    )
                )

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai.types import Content, Part

    agent = LlmAgent(
        name="tripchord_selection",
        model=ScriptedLlm(model="scripted"),
        instruction="Use tools and return JSON.",
        tools=[tools.inspect_requirements, tools.inspect_candidate, tools.verify_candidate],
        output_schema=Proposal,
    )
    sessions = InMemorySessionService()
    session = await sessions.create_session(
        app_name="tripchord_selection", user_id="harness", session_id="session"
    )
    runner = Runner(app_name="tripchord_selection", agent=agent, session_service=sessions)
    final_text = None
    async for event in runner.run_async(
        user_id="harness",
        session_id=session.id,
        new_message=Content(role="user", parts=[Part(text="offline scripted case")]),
    ):
        if (
            getattr(event, "is_final_response", lambda: False)()
            and event.content
            and event.content.parts
        ):
            final_text = event.content.parts[0].text
    if not final_text:
        raise RuntimeError("google-adk scripted run produced no final response")
    return json.loads(final_text)


ADAPTERS = {
    "custom": custom_run,
    "pydanticai": pydanticai_run,
    "langgraph": langgraph_run,
    "openai_agents": openai_agents_run,
    "google_adk": google_adk_run,
}


async def one(framework: str, name: str, case: dict[str, Any]) -> dict[str, Any]:
    async def invoke() -> tuple[DomainTools, str, str | None, Any, float]:
        tools = DomainTools(case, scenario(case, name))
        started = time.perf_counter()
        status = "success"
        error = None
        output = None
        try:
            output = await ADAPTERS[framework](case, scenario(case, name), tools)
        except Exception as exc:
            status, error = "failure", f"{type(exc).__name__}: {exc}"
        finally:
            if framework == "custom":
                for module_name in tuple(sys.modules):
                    if module_name == "tripchord" or module_name.startswith("tripchord."):
                        sys.modules.pop(module_name, None)
        return tools, status, error, output, time.perf_counter() - started

    warmup = await invoke()
    runs = [await invoke() for _ in range(3)]
    elapsed = sorted(run[4] for run in runs)[1]
    expected_tools = ["inspect_requirements", "inspect_candidate", "verify_candidate"]

    def audit(run: tuple[DomainTools, str, str | None, Any, float]) -> dict[str, Any]:
        run_tools, run_status, run_error, run_output, _ = run
        output_correct = False
        audit_error = run_error
        if run_output is not None:
            try:
                Proposal.model_validate(run_output)
                output_correct = run_output == expected(name)
                if not output_correct:
                    audit_error = "output did not match the shared deterministic audit"
            except Exception as exc:
                audit_error = f"invalid_output: {exc}"
        elif audit_error is None:
            audit_error = "missing output"
        tool_order = [call["tool_name"] for call in run_tools.calls]
        tool_contract_ok = tool_order == expected_tools
        if not tool_contract_ok:
            audit_error = audit_error or (
                "tool contract mismatch: expected the three tools in fixed order"
            )
        return {
            "status": run_status if audit_error is None else "failure",
            "error": audit_error,
            "output_correct": output_correct,
            "tool_contract_ok": tool_contract_ok,
            "tool_call_order": tool_order,
            "tool_calls": len(tool_order),
        }

    warmup_audit = audit(warmup)
    measurement_audits = [audit(run) for run in runs]
    final_audit = measurement_audits[-1]
    valid = final_audit["output_correct"]
    contract_ok = final_audit["tool_contract_ok"]
    if not all(item["status"] == "success" for item in [warmup_audit, *measurement_audits]):
        status, error = "failure", "one or more warmup or measured runs failed audit"
    else:
        status, error = "success", None
    adapter_lines = len(inspect.getsourcelines(ADAPTERS[framework])[0])
    return {
        "framework": framework,
        "scenario": name,
        "status": status,
        "output_correct": valid,
        "tool_contract_ok": contract_ok,
        "warmup_status": warmup_audit["status"],
        "warmup_error": warmup_audit["error"],
        "measurement_statuses": [item["status"] for item in measurement_audits],
        "measurement_errors": [item["error"] for item in measurement_audits],
        "tool_call_order": final_audit["tool_call_order"],
        "tool_calls": final_audit["tool_calls"],
        "run_audits": [{"kind": "warmup", **warmup_audit}]
        + [
            {"kind": "measurement", "index": index, **item}
            for index, item in enumerate(measurement_audits, 1)
        ],
        "wall_time_seconds_median_warm": elapsed,
        "latency_used_for_selection": False,
        "error": error,
        "adapter_loc": adapter_lines,
        "framework_version": framework_version(framework),
    }


def framework_version(name: str) -> str | None:
    package = {
        "custom": None,
        "pydanticai": "pydantic_ai",
        "langgraph": "langgraph",
        "openai_agents": "agents",
        "google_adk": "google.adk",
    }[name]
    if not package:
        return "TripChord custom baseline"
    try:
        return importlib.metadata.version(
            {
                "pydantic_ai": "pydantic-ai",
                "langgraph": "langgraph",
                "agents": "openai-agents",
                "google.adk": "google-adk",
            }[package]
        )
    except Exception:
        return None


async def main(output_path: Path) -> None:
    case = load_case()
    results = [
        await one(f, s, case)
        for f in ADAPTERS
        for s in ("baseline", "controlled_recovery", "invalid_or_ambiguous")
    ]
    payload = {
        "schema_version": "tripchord.agent-runtime-framework-selection.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "evidence_tier": "framework_harness_validation",
        "winner_claim_allowed": False,
        "case_path": str(CASE_PATH.relative_to(ROOT)),
        "case_bundle_sha256": case["bundle_canonical_sha256"],
        "canonical_digest_source": (
            "case_bundle.bundle_canonical_sha256 (declared historical artifact digest; "
            "no whole-file recomputation claim)"
        ),
        "frameworks": list(ADAPTERS),
        "scenarios": ["baseline", "controlled_recovery", "invalid_or_ambiguous"],
        "all_contracts_passed": all(
            audit_item["status"] == "success"
            and audit_item["output_correct"]
            and audit_item["tool_contract_ok"]
            for result in results
            for audit_item in result["run_audits"]
        ),
        "results": results,
        "limitations": [
            "historical replay only",
            "offline scripted harness; no model quality claim",
            "latency is reported as warm median but excluded from selection",
            "no OTA/network/booking",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not payload["all_contracts_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output))
