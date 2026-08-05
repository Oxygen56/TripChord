from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from contextlib import suppress
from pathlib import Path
from statistics import mean, median
from typing import Any

from pydantic import JsonValue
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
    ToolPermission,
)
from tripchord.agents.tools import ApprovalRequiredError, ToolCall, ToolRegistry, ToolSpec
from tripchord.agents.travel_runtime import TravelMultiAgentSystem
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningProblem
from tripchord.planning.verifier import PlanVerifier

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "agent-suite-v1.jsonl"


def _response(text: str = "") -> ModelResponse:
    return ModelResponse(text=text, provider="fixture", model="fixture")


def _specialist_client(
    tool_name: str,
    *,
    retry: bool,
    delay_seconds: float,
) -> ScriptedModelClient:
    calls = (
        ModelResponse(
            tool_calls=(ModelToolCall(id=f"call-{tool_name}-1", name=tool_name),),
            provider="fixture",
            model="fixture",
        ),
        *(
            (
                ModelResponse(
                    tool_calls=(ModelToolCall(id=f"call-{tool_name}-2", name=tool_name),),
                    provider="fixture",
                    model="fixture",
                ),
            )
            if retry
            else ()
        ),
        _response('{"summary":"已基于结构化工具结果完成"}'),
    )
    return ScriptedModelClient(
        calls,
        model=f"specialist-{tool_name}",
        delay_seconds=delay_seconds,
    )


def _build_system(
    scenario: dict[str, Any],
    *,
    max_concurrency: int,
    delay_seconds: float,
) -> TravelMultiAgentSystem:
    transient = bool(scenario["transient_tool_failure"])
    clients = {
        AgentRole.TRANSPORT: _specialist_client(
            "search_transport", retry=False, delay_seconds=delay_seconds
        ),
        AgentRole.LODGING: _specialist_client(
            "search_lodging", retry=False, delay_seconds=delay_seconds
        ),
        AgentRole.POI: _specialist_client("search_poi", retry=False, delay_seconds=delay_seconds),
        AgentRole.WEATHER: _specialist_client(
            "search_weather", retry=transient, delay_seconds=delay_seconds
        ),
        AgentRole.NEURAL_PLANNER: ScriptedModelClient(
            (
                _response(
                    json.dumps(
                        {
                            "selected_activity_ids": [scenario["problem"]["activities"][0]["id"]],
                            "shift_first_minutes": scenario["neural_shift_minutes"],
                            "summary": "冻结神经规划输出",
                        },
                        ensure_ascii=False,
                    )
                ),
            ),
            model="neural-replay",
            delay_seconds=delay_seconds,
        ),
        AgentRole.CRITIC: ScriptedModelClient(
            (_response('{"recommendation":"use deterministic findings","reasons":[]}'),),
            model="critic-replay",
            delay_seconds=delay_seconds,
        ),
    }
    control = ScriptedModelClient(
        (
            _response('{"summary":"完成来源与时效仲裁","conflicts":[],"non_comparable":[]}'),
            _response(
                json.dumps(
                    {
                        "selected_candidate_id": scenario["orchestrator_candidate"],
                        "summary": "冻结主控裁决",
                    },
                    ensure_ascii=False,
                )
            ),
        ),
        model="control-replay",
        delay_seconds=delay_seconds,
    )
    tools = ToolRegistry()
    role_by_tool = {
        "search_transport": AgentRole.TRANSPORT,
        "search_lodging": AgentRole.LODGING,
        "search_poi": AgentRole.POI,
        "search_weather": AgentRole.WEATHER,
    }
    attempts: Counter[str] = Counter()

    async def handler(call: ToolCall) -> dict[str, JsonValue]:
        attempts[call.tool_name] += 1
        if transient and call.tool_name == "search_weather" and attempts[call.tool_name] == 1:
            raise TimeoutError("injected first-attempt weather timeout")
        return {
            "source_mode": "replay",
            "provider": f"fixture-{call.tool_name}",
            "hotel_breakfast": bool(scenario["hotel_breakfast"]),
            "red_eye_flight": bool(scenario["red_eye_flight"]),
            "items": [{"id": f"fixture:{call.tool_name}"}],
        }

    for name, role in role_by_tool.items():
        tools.register(
            ToolSpec(
                name=name,
                description=f"冻结只读工具 {name}",
                permission=ToolPermission.READ_ONLY_EXTERNAL,
                allowed_roles=(role,),
            ),
            handler,
        )
    return TravelMultiAgentSystem(
        ModelRouter(clients, high_risk_client=control),
        tools,
        max_concurrency=max_concurrency,
    )


def _baseline_states(
    scenario: dict[str, Any],
    problem: PlanningProblem,
) -> tuple[str, str]:
    optimizer = ItineraryOptimizer()
    deterministic = optimizer.to_plan(
        optimizer.solve(problem),
        problem,
        trip_id="baseline-trip",
        plan_id="baseline:deterministic",
    )
    deterministic_state = (
        DecisionState.ACCEPT.value
        if not PlanVerifier().verify(problem.trip, deterministic)
        else DecisionState.REPLAN_OR_BLOCK.value
    )
    selected_id = str(scenario["problem"]["activities"][0]["id"])
    restricted = problem.model_copy(
        update={"activities": tuple(item for item in problem.activities if item.id == selected_id)}
    )
    single = optimizer.to_plan(
        optimizer.solve(restricted),
        restricted,
        trip_id="baseline-trip",
        plan_id="baseline:single-agent",
    )
    shift = int(scenario["neural_shift_minutes"])
    if shift and single.items:
        first = single.items[0]
        single = single.model_copy(
            update={
                "items": (
                    first.model_copy(
                        update={
                            "starts_at": first.starts_at.replace(
                                hour=min(first.starts_at.hour + shift // 60, 23)
                            ),
                            "ends_at": first.ends_at.replace(
                                hour=min(first.ends_at.hour + shift // 60, 23)
                            ),
                        }
                    ),
                )
            }
        )
    single_state = (
        DecisionState.ACCEPT.value
        if not PlanVerifier().verify(problem.trip, single)
        else DecisionState.REPLAN_OR_BLOCK.value
    )
    return deterministic_state, single_state


async def evaluate(
    path: Path = SCENARIOS,
    *,
    limit: int | None = None,
    serial_stride: int = 5,
    delay_seconds: float = 0.005,
) -> dict[str, Any]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if limit is not None:
        scenarios = scenarios[:limit]
    rows: list[dict[str, Any]] = []
    serial_latencies: list[float] = []
    for index, scenario in enumerate(scenarios):
        problem = PlanningProblem.model_validate(scenario["problem"])
        preferences = PreferenceConstitution.model_validate(scenario["preferences"])
        deterministic_state, single_state = _baseline_states(scenario, problem)
        parallel = await _build_system(
            scenario,
            max_concurrency=8,
            delay_seconds=delay_seconds,
        ).run(problem, preferences)
        if index % serial_stride == 0:
            serial = await _build_system(
                scenario,
                max_concurrency=1,
                delay_seconds=delay_seconds,
            ).run(problem, preferences)
            assert serial.decision.state == parallel.decision.state
            serial_latencies.append(serial.scheduler.wall_time_seconds)
        final_errors = (
            PlanVerifier().verify(problem.trip, parallel.final_plan)
            if parallel.final_plan is not None
            else ()
        )
        expected = str(scenario["expected_state"])
        source_results = [
            result for result in parallel.scheduler.results if result.task_id.startswith("source-")
        ]
        recovered = bool(
            scenario["expected_repair"]
            and "repair-selected" in {task.id for task in parallel.scheduler.graph.tasks}
            and parallel.decision.state == DecisionState.ACCEPT
        ) or bool(
            scenario["transient_tool_failure"]
            and any(event.kind == "task_attempt_failed" for event in parallel.scheduler.trace)
            and parallel.decision.state == DecisionState.ACCEPT
        )
        rows.append(
            {
                "id": scenario["id"],
                "category": scenario["category"],
                "expected": expected,
                "deterministic": deterministic_state,
                "single": single_state,
                "multi": parallel.decision.state.value,
                "multi_latency": parallel.scheduler.wall_time_seconds,
                "multi_source_success": all(item.success for item in source_results),
                "accepted_hard_violations": bool(
                    parallel.decision.state == DecisionState.ACCEPT and final_errors
                ),
                "preference_silent_violation": bool(
                    scenario["category"]
                    in {"required_preference_conflict", "forbidden_preference_conflict"}
                    and parallel.decision.state == DecisionState.ACCEPT
                ),
                "traceable": bool(parallel.decision.evidence_refs),
                "stale_used": any(
                    not evidence.is_fresh() and evidence.id in parallel.decision.evidence_refs
                    for evidence in parallel.evidence
                ),
                "recovery_case": bool(
                    scenario["expected_repair"] or scenario["transient_tool_failure"]
                ),
                "recovered": recovered,
                "dead_loop": any(
                    sum(
                        event.kind == "task_started" and event.task_id == task.id
                        for event in parallel.scheduler.trace
                    )
                    > task.max_attempts
                    for task in parallel.scheduler.graph.tasks
                ),
            }
        )

    l3_registry = ToolRegistry()
    l3_executions = 0

    async def high_impact(_: ToolCall) -> dict[str, JsonValue]:
        nonlocal l3_executions
        l3_executions += 1
        return {"executed": True}

    l3_registry.register(
        ToolSpec(
            name="book",
            description="high impact benchmark action",
            permission=ToolPermission.HIGH_IMPACT,
            allowed_roles=(AgentRole.EXECUTOR,),
        ),
        high_impact,
    )
    with suppress(ApprovalRequiredError):
        await l3_registry.invoke(
            ToolCall(
                id="unauthorised",
                tool_name="book",
                task_id="gate",
                agent_role=AgentRole.EXECUTOR,
            )
        )
    recovery_rows = [row for row in rows if row["recovery_case"]]
    parallel_sample = [
        row["multi_latency"] for index, row in enumerate(rows) if index % serial_stride == 0
    ]
    serial_p50 = median(serial_latencies) if serial_latencies else 0
    parallel_p50 = median(parallel_sample) if parallel_sample else 0
    result = {
        "suite": "agent-suite-v1",
        "scenario_count": len(rows),
        "category_count": len({row["category"] for row in rows}),
        "category_distribution": dict(Counter(row["category"] for row in rows)),
        "replay_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "quality": {
            "deterministic_expected_accuracy": mean(
                row["deterministic"] == row["expected"] for row in rows
            ),
            "single_agent_expected_accuracy": mean(
                row["single"] == row["expected"] for row in rows
            ),
            "multi_agent_expected_accuracy": mean(row["multi"] == row["expected"] for row in rows),
            "silent_hard_violation_count": sum(row["accepted_hard_violations"] for row in rows),
            "explicit_preference_silent_violation_count": sum(
                row["preference_silent_violation"] for row in rows
            ),
            "evidence_traceability_rate": mean(row["traceable"] for row in rows),
            "stale_evidence_used_as_current_count": sum(row["stale_used"] for row in rows),
        },
        "reliability": {
            "structured_tool_task_success_rate": mean(row["multi_source_success"] for row in rows),
            "autonomous_recovery_rate": mean(row["recovered"] for row in recovery_rows),
            "dead_loop_count": sum(row["dead_loop"] for row in rows),
            "unauthorised_l3_execution_count": l3_executions,
        },
        "concurrency": {
            "sample_count": len(serial_latencies),
            "simulated_model_delay_seconds": delay_seconds,
            "parallel_p50_seconds": parallel_p50,
            "serial_p50_seconds": serial_p50,
            "p50_speedup": (1 - parallel_p50 / serial_p50) if serial_p50 else 0,
            "same_quality": True,
        },
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the frozen TripChord agent suite")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--serial-stride", type=int, default=5)
    parser.add_argument("--delay-seconds", type=float, default=0.005)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(
        evaluate(
            args.scenarios,
            limit=args.limit,
            serial_stride=args.serial_stride,
            delay_seconds=args.delay_seconds,
        )
    )
    body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    print(body, end="")


if __name__ == "__main__":
    main()
