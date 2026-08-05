from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any, Protocol

from pydantic import Field, JsonValue, TypeAdapter
from tripchord.agents.model_gateway import (
    ModelClient,
    ModelClientConfig,
    ModelGatewayError,
    ModelMessage,
    ModelPricing,
    ModelProviderName,
    ModelRequest,
    ModelResponse,
    ModelRetryPolicy,
    ModelTool,
    ModelToolCall,
    ModelToolResult,
    ModelUsage,
    StructuredOutputError,
    build_model_client,
)
from tripchord.agents.models import AgentRole
from tripchord.domain.common import DomainModel

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "agent-architecture-v1.jsonl"


class ArchitectureName(str):
    SINGLE = "single_agent"
    MULTI = "multi_agent"


class Requirements(DomainModel):
    max_budget_cents: int = Field(gt=0)
    max_stops: int = Field(ge=0)
    breakfast_required: bool = False
    overnight_forbidden: bool = False
    fresh_evidence_required: bool = True


class Candidate(DomainModel):
    id: str = Field(min_length=1)
    total_cost_cents: int = Field(ge=0)
    stops: int = Field(ge=0)
    breakfast_included: bool
    overnight: bool
    available: bool
    evidence_fresh: bool
    utility: int


class InventoryEvent(DomainModel):
    candidate_id: str = Field(min_length=1)
    patch: dict[str, JsonValue]


class ArchitectureScenario(DomainModel):
    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    requirements: Requirements
    candidates: tuple[Candidate, ...] = Field(min_length=2)
    event: InventoryEvent | None = None
    expected_repair: bool = False


class CandidateCheck(DomainModel):
    candidate_id: str | None
    hard_valid: bool
    violations: tuple[str, ...] = ()


class AgentProposal(DomainModel):
    summary: str = Field(min_length=1)
    selected_candidate_id: str | None = None
    decision: str = Field(pattern="^(accept|replan_or_block)$")
    repair_required: bool = False
    reasons: tuple[str, ...] = ()


class FairBudget(DomainModel):
    max_model_calls: int = Field(default=16, ge=1)
    max_tool_calls: int = Field(default=16, ge=1)
    max_total_tokens: int = Field(default=12_000, ge=256)
    max_output_tokens_per_call: int = Field(default=1_024, ge=64)


class BudgetExceeded(ModelGatewayError):
    pass


@dataclass
class UsageLedger:
    budget: FairBudget
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0
    reported_model_latency_seconds: float = 0
    budget_breached: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def before_model_call(self) -> None:
        if self.model_calls >= self.budget.max_model_calls:
            self.budget_breached = True
            raise BudgetExceeded("model_call_budget_exhausted")
        if self.total_tokens >= self.budget.max_total_tokens:
            self.budget_breached = True
            raise BudgetExceeded("token_budget_exhausted")
        self.model_calls += 1

    def after_model_call(self, response: ModelResponse) -> None:
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.estimated_cost_usd += response.estimated_cost_usd
        self.reported_model_latency_seconds += response.latency_seconds
        if self.total_tokens > self.budget.max_total_tokens:
            self.budget_breached = True
            raise BudgetExceeded("token_budget_exceeded_by_response")

    def before_tool_call(self) -> None:
        if self.tool_calls >= self.budget.max_tool_calls:
            self.budget_breached = True
            raise BudgetExceeded("tool_call_budget_exhausted")
        self.tool_calls += 1


class BudgetedClient:
    def __init__(self, inner: ModelClient, ledger: UsageLedger) -> None:
        self._inner = inner
        self._ledger = ledger
        self.provider = inner.provider
        self.model = inner.model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._ledger.before_model_call()
        remaining = max(1, self._ledger.budget.max_total_tokens - self._ledger.total_tokens)
        bounded = request.model_copy(
            update={
                "max_tokens": min(
                    request.max_tokens,
                    self._ledger.budget.max_output_tokens_per_call,
                    remaining,
                )
            }
        )
        response = await self._inner.complete(bounded)
        self._ledger.after_model_call(response)
        return response


class SyntheticPolicyClient:
    """Deterministic harness double; it is not evidence about LLM quality."""

    provider = "scripted"
    model = "shared-policy-fixture-v1"

    def __init__(self, *, delay_seconds: float = 0) -> None:
        self._delay_seconds = delay_seconds

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = perf_counter()
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        tool_payloads = _tool_payloads(request.messages)
        if "requirements" not in tool_payloads or "candidates" not in tool_payloads:
            return ModelResponse(
                tool_calls=(
                    ModelToolCall(
                        id="fixture-inspect-requirements",
                        name="inspect_requirements",
                    ),
                    ModelToolCall(
                        id="fixture-inspect-candidates",
                        name="inspect_candidates",
                    ),
                ),
                provider=self.provider,
                model=self.model,
                usage=ModelUsage(input_tokens=120, output_tokens=18),
                latency_seconds=perf_counter() - started,
                estimated_cost_usd=0.00002,
            )
        requirements = tool_payloads["requirements"].get("requirements", {})
        candidates = tool_payloads["candidates"].get("candidates", [])
        valid = [
            item
            for item in candidates
            if isinstance(item, dict)
            and isinstance(requirements, dict)
            and _synthetic_candidate_valid(item, requirements)
        ]
        valid.sort(
            key=lambda item: (
                -int(item.get("utility", 0)),
                int(item.get("total_cost_cents", 0)),
                str(item.get("id", "")),
            )
        )
        selected = str(valid[0]["id"]) if valid else None
        verification = tool_payloads.get("verification")
        if selected is not None and (
            verification is None or verification.get("candidate_id") != selected
        ):
            return ModelResponse(
                tool_calls=(
                    ModelToolCall(
                        id=f"fixture-verify-{selected}",
                        name="verify_candidate",
                        arguments={"candidate_id": selected},
                    ),
                ),
                provider=self.provider,
                model=self.model,
                usage=ModelUsage(input_tokens=150, output_tokens=24),
                latency_seconds=perf_counter() - started,
                estimated_cost_usd=0.00003,
            )
        verified = bool(verification and verification.get("hard_valid"))
        proposal = AgentProposal(
            summary="冻结策略从原始约束和候选计算选择，并主动调用验证工具复核",
            selected_candidate_id=selected if verified else None,
            decision="accept" if selected and verified else "replan_or_block",
            repair_required=not bool(selected and verified),
            reasons=("scripted_harness_only",),
        )
        text = proposal.model_dump_json()
        return ModelResponse(
            text=text,
            provider=self.provider,
            model=self.model,
            usage=ModelUsage(input_tokens=180, output_tokens=72),
            latency_seconds=perf_counter() - started,
            structured_output=TypeAdapter(JsonValue).validate_python(
                proposal.model_dump(mode="json")
            ),
            estimated_cost_usd=0.00005,
        )


def _tool_payloads(messages: Sequence[ModelMessage]) -> dict[str, dict[str, Any]]:
    payloads: dict[str, dict[str, Any]] = {}
    for message in messages:
        for result in message.tool_results:
            try:
                parsed: Any = json.loads(result.content)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get("tool_name"), str)
                and isinstance(parsed.get("tool_result"), dict)
            ):
                name = str(parsed["tool_name"])
                payloads[
                    "requirements"
                    if name == "inspect_requirements"
                    else "candidates"
                    if name == "inspect_candidates"
                    else "verification"
                ] = dict(parsed["tool_result"])
    return payloads


def _synthetic_candidate_valid(
    candidate: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> bool:
    return bool(
        candidate.get("available") is True
        and isinstance(candidate.get("total_cost_cents"), int)
        and int(candidate["total_cost_cents"]) <= int(requirements["max_budget_cents"])
        and isinstance(candidate.get("stops"), int)
        and int(candidate["stops"]) <= int(requirements["max_stops"])
        and (
            not bool(requirements.get("breakfast_required"))
            or candidate.get("breakfast_included") is True
        )
        and (
            not bool(requirements.get("overnight_forbidden"))
            or candidate.get("overnight") is False
        )
        and (
            not bool(requirements.get("fresh_evidence_required"))
            or candidate.get("evidence_fresh") is True
        )
    )


_TOOLS = (
    ModelTool(
        name="inspect_requirements",
        description="读取本任务已经确认的硬约束，不执行任何外部操作",
        input_schema={"type": "object", "additionalProperties": False},
    ),
    ModelTool(
        name="inspect_candidates",
        description="读取当前版本原始候选字段，不提供答案或预计算违规标签",
        input_schema={"type": "object", "additionalProperties": False},
    ),
    ModelTool(
        name="verify_candidate",
        description="对一个候选执行同一套确定性硬约束校验",
        input_schema={
            "type": "object",
            "required": ["candidate_id"],
            "additionalProperties": False,
            "properties": {"candidate_id": {"type": "string"}},
        },
    ),
)


def _tool_contract_sha256() -> str:
    payload = [item.model_dump(mode="json") for item in _TOOLS]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ScenarioToolbox:
    def __init__(self, scenario: ArchitectureScenario, ledger: UsageLedger) -> None:
        self.scenario = scenario
        self.ledger = ledger
        self._inventory = {item.id: item for item in scenario.candidates}
        self.inventory_version = 1

    def apply_event(self) -> None:
        event = self.scenario.event
        if event is None:
            return
        candidate = self._inventory.get(event.candidate_id)
        if candidate is None:
            raise ValueError(f"event references unknown candidate: {event.candidate_id}")
        self._inventory[event.candidate_id] = Candidate.model_validate(
            {**candidate.model_dump(mode="python"), **event.patch}
        )
        self.inventory_version += 1

    def verify(self, candidate_id: str | None) -> CandidateCheck:
        candidate = self._inventory.get(candidate_id or "")
        if candidate is None:
            return CandidateCheck(
                candidate_id=candidate_id,
                hard_valid=False,
                violations=("unknown_or_missing_candidate",),
            )
        rules = self.scenario.requirements
        violations: list[str] = []
        if not candidate.available:
            violations.append("unavailable")
        if candidate.total_cost_cents > rules.max_budget_cents:
            violations.append("budget")
        if candidate.stops > rules.max_stops:
            violations.append("stops")
        if rules.breakfast_required and not candidate.breakfast_included:
            violations.append("breakfast")
        if rules.overnight_forbidden and candidate.overnight:
            violations.append("overnight")
        if rules.fresh_evidence_required and not candidate.evidence_fresh:
            violations.append("stale_evidence")
        return CandidateCheck(
            candidate_id=candidate.id,
            hard_valid=not violations,
            violations=tuple(violations),
        )

    def candidate(self, candidate_id: str | None) -> Candidate | None:
        return self._inventory.get(candidate_id or "")

    def invoke(self, call: ModelToolCall) -> dict[str, JsonValue]:
        self.ledger.before_tool_call()
        if call.name == "inspect_requirements":
            return TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "inventory_version": self.inventory_version,
                    "requirements": self.scenario.requirements.model_dump(mode="json"),
                }
            )
        if call.name == "inspect_candidates":
            return TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "inventory_version": self.inventory_version,
                    "candidates": [
                        candidate.model_dump(mode="json")
                        for candidate in self._inventory.values()
                    ],
                }
            )
        if call.name == "verify_candidate":
            candidate_id = call.arguments.get("candidate_id")
            if not isinstance(candidate_id, str):
                raise StructuredOutputError("verify_candidate requires candidate_id")
            return TypeAdapter(dict[str, JsonValue]).validate_python(
                self.verify(candidate_id).model_dump(mode="json")
            )
        raise PermissionError(f"undeclared benchmark tool: {call.name}")


def _agent_prompt(
    *,
    architecture: str,
    stage: str,
    scenario: ArchitectureScenario,
    current_check: CandidateCheck | None,
) -> str:
    return json.dumps(
        {
            "architecture": architecture,
            "stage": stage,
            "scenario_id": scenario.id,
            "category": scenario.category,
            "current_check": (
                current_check.model_dump(mode="json") if current_check is not None else None
            ),
            "instructions": [
                "先调用至少一个只读工具，再提交提案",
                "只能选择工具回执中存在的 candidate_id",
                "硬约束失败时不得 accept",
                "最终仅输出符合 response_schema 的 JSON",
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


_SYSTEM_PROMPTS = {
    "single_plan": (
        "你是唯一的旅行规划 Agent，同时承担检索检查、候选取舍、验证与解释。"
        "你必须使用工具观察当前状态，并对自己的候选负责。"
    ),
    "single_repair": (
        "你是同一个旅行规划 Agent。上一个候选已被确定性校验或事件证伪；"
        "根据当前工具回执自行诊断并修复。"
    ),
    "planner": "你是 Planner Agent。使用工具提出当前最优候选，不得宣布硬约束已经通过。",
    "verifier": (
        "你是 Verifier Agent。使用工具独立复核当前候选；发现硬约束问题时必须拒绝。"
    ),
    "repair": "你是 Repair Agent。使用工具查找能修复当前失败的替代候选。",
    "orchestrator": (
        "你是主控 Agent。使用工具复核候选与分工结果，提出最终接受或阻断建议；"
        "确定性发布门拥有最后否决权。"
    ),
}


async def _invoke_agent(
    client: BudgetedClient,
    toolbox: ScenarioToolbox,
    *,
    architecture: str,
    role: AgentRole,
    stage: str,
    current_check: CandidateCheck | None,
) -> AgentProposal:
    messages = [
        ModelMessage(
            role="user",
            content=_agent_prompt(
                architecture=architecture,
                stage=stage,
                scenario=toolbox.scenario,
                current_check=current_check,
            ),
        )
    ]
    used_tool = False
    schema = TypeAdapter(dict[str, JsonValue]).validate_python(
        AgentProposal.model_json_schema()
    )
    for round_index in range(4):
        response = await client.complete(
            ModelRequest(
                role=role,
                system=_SYSTEM_PROMPTS[stage],
                messages=tuple(messages),
                tools=_TOOLS,
                response_schema=schema,
                temperature=0,
                max_tokens=toolbox.ledger.budget.max_output_tokens_per_call,
                risk_level=2 if stage in {"verifier", "orchestrator"} else 1,
            )
        )
        if response.tool_calls:
            if round_index == 3:
                raise BudgetExceeded("agent_tool_loop_exhausted")
            for call in response.tool_calls:
                if call.name not in {item.name for item in _TOOLS}:
                    raise PermissionError(f"model selected undeclared tool: {call.name}")
                payload = toolbox.invoke(call)
                used_tool = True
                messages.extend(
                    (
                        ModelMessage(role="assistant", tool_calls=(call,)),
                        ModelMessage(
                            role="user",
                            tool_results=(
                                ModelToolResult(
                                    tool_call_id=call.id,
                                    content=json.dumps(
                                        {"tool_name": call.name, "tool_result": payload},
                                        ensure_ascii=False,
                                        separators=(",", ":"),
                                    ),
                                ),
                            ),
                        ),
                    )
                )
            continue
        if not used_tool:
            messages.append(
                ModelMessage(
                    role="user",
                    content="尚未调用工具。必须先调用三个只读工具中的至少一个。",
                )
            )
            continue
        raw: Any = response.structured_output
        if raw is None:
            try:
                raw = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise StructuredOutputError("agent did not return JSON") from exc
        return AgentProposal.model_validate(raw)
    raise BudgetExceeded("agent_round_budget_exhausted")


@dataclass
class ArmRun:
    architecture: str
    provider: str
    model: str
    ledger: UsageLedger
    selected_candidate_id: str | None = None
    accepted: bool = False
    valid_candidate_found: bool = False
    proposed_hard_violations: int = 0
    released_hard_violations: int = 0
    repair_attempted: bool = False
    repair_succeeded: bool = False
    latency_seconds: float = 0
    failure: str | None = None

    def as_dict(self) -> dict[str, JsonValue]:
        return TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                "architecture": self.architecture,
                "provider": self.provider,
                "model": self.model,
                "selected_candidate_id": self.selected_candidate_id,
                "accepted": self.accepted,
                "valid_candidate_found": self.valid_candidate_found,
                "proposed_hard_constraint_violation_count": self.proposed_hard_violations,
                "released_hard_constraint_violation_count": self.released_hard_violations,
                "repair_attempted": self.repair_attempted,
                "repair_succeeded": self.repair_succeeded,
                "latency_seconds": self.latency_seconds,
                "model_reported_latency_seconds": self.ledger.reported_model_latency_seconds,
                "model_call_count": self.ledger.model_calls,
                "tool_call_count": self.ledger.tool_calls,
                "input_tokens": self.ledger.input_tokens,
                "output_tokens": self.ledger.output_tokens,
                "total_tokens": self.ledger.total_tokens,
                "estimated_cost_usd": self.ledger.estimated_cost_usd,
                "budget_breached": self.ledger.budget_breached,
                "failure": self.failure,
            }
        )


def _count_bad_accept(proposal: AgentProposal, check: CandidateCheck) -> int:
    return int(proposal.decision == "accept" and not check.hard_valid)


class ReleaseGate(Protocol):
    def __call__(self, proposal: AgentProposal, check: CandidateCheck) -> bool: ...


def deterministic_release_gate(proposal: AgentProposal, check: CandidateCheck) -> bool:
    return proposal.decision == "accept" and check.hard_valid


def _independent_release_audit(
    requirements: Requirements,
    candidate: Candidate | None,
) -> tuple[str, ...]:
    """Independently recompute release violations without using CandidateCheck."""

    if candidate is None:
        return ("missing_released_candidate",)
    violations: list[str] = []
    if not candidate.available:
        violations.append("unavailable")
    if candidate.total_cost_cents > requirements.max_budget_cents:
        violations.append("budget")
    if candidate.stops > requirements.max_stops:
        violations.append("stops")
    if requirements.breakfast_required and not candidate.breakfast_included:
        violations.append("breakfast")
    if requirements.overnight_forbidden and candidate.overnight:
        violations.append("overnight")
    if requirements.fresh_evidence_required and not candidate.evidence_fresh:
        violations.append("stale_evidence")
    return tuple(violations)


def _record_release(
    run: ArmRun,
    toolbox: ScenarioToolbox,
    proposal: AgentProposal,
    check: CandidateCheck,
    release_gate: ReleaseGate,
) -> None:
    released = release_gate(proposal, check)
    run.accepted = released
    if not released:
        run.released_hard_violations = 0
        return
    audit_violations = _independent_release_audit(
        toolbox.scenario.requirements,
        toolbox.candidate(proposal.selected_candidate_id),
    )
    run.released_hard_violations = int(bool(audit_violations))


async def _run_single(
    scenario: ArchitectureScenario,
    client: ModelClient,
    budget: FairBudget,
    release_gate: ReleaseGate,
) -> ArmRun:
    ledger = UsageLedger(budget)
    bounded = BudgetedClient(client, ledger)
    toolbox = ScenarioToolbox(scenario, ledger)
    run = ArmRun(
        architecture=ArchitectureName.SINGLE,
        provider=client.provider,
        model=client.model,
        ledger=ledger,
    )
    started = perf_counter()
    try:
        proposal = await _invoke_agent(
            bounded,
            toolbox,
            architecture=ArchitectureName.SINGLE,
            role=AgentRole.ORCHESTRATOR,
            stage="single_plan",
            current_check=None,
        )
        initial_check = toolbox.verify(proposal.selected_candidate_id)
        run.proposed_hard_violations += _count_bad_accept(proposal, initial_check)
        if scenario.event is not None:
            toolbox.apply_event()
        current_check = toolbox.verify(proposal.selected_candidate_id)
        needs_repair = not current_check.hard_valid
        if needs_repair:
            run.repair_attempted = True
            proposal = await _invoke_agent(
                bounded,
                toolbox,
                architecture=ArchitectureName.SINGLE,
                role=AgentRole.ORCHESTRATOR,
                stage="single_repair",
                current_check=current_check,
            )
            current_check = toolbox.verify(proposal.selected_candidate_id)
            run.proposed_hard_violations += _count_bad_accept(proposal, current_check)
            run.repair_succeeded = current_check.hard_valid and proposal.decision == "accept"
        run.selected_candidate_id = proposal.selected_candidate_id
        run.valid_candidate_found = current_check.hard_valid
        _record_release(run, toolbox, proposal, current_check, release_gate)
    except (ModelGatewayError, PermissionError, StructuredOutputError, ValueError) as exc:
        run.failure = f"{type(exc).__name__}:{exc}"
    finally:
        run.latency_seconds = perf_counter() - started
    return run


async def _run_multi(
    scenario: ArchitectureScenario,
    client: ModelClient,
    budget: FairBudget,
    release_gate: ReleaseGate,
) -> ArmRun:
    ledger = UsageLedger(budget)
    bounded = BudgetedClient(client, ledger)
    toolbox = ScenarioToolbox(scenario, ledger)
    run = ArmRun(
        architecture=ArchitectureName.MULTI,
        provider=client.provider,
        model=client.model,
        ledger=ledger,
    )
    started = perf_counter()
    try:
        planner = await _invoke_agent(
            bounded,
            toolbox,
            architecture=ArchitectureName.MULTI,
            role=AgentRole.NEURAL_PLANNER,
            stage="planner",
            current_check=None,
        )
        selected_id = planner.selected_candidate_id
        current_check = toolbox.verify(selected_id)
        run.proposed_hard_violations += _count_bad_accept(planner, current_check)
        verifier = await _invoke_agent(
            bounded,
            toolbox,
            architecture=ArchitectureName.MULTI,
            role=AgentRole.HARD_VERIFIER,
            stage="verifier",
            current_check=current_check,
        )
        run.proposed_hard_violations += _count_bad_accept(verifier, current_check)
        if scenario.event is not None:
            toolbox.apply_event()
            current_check = toolbox.verify(selected_id)
        if not current_check.hard_valid or verifier.repair_required:
            run.repair_attempted = True
            repaired = await _invoke_agent(
                bounded,
                toolbox,
                architecture=ArchitectureName.MULTI,
                role=AgentRole.REPAIR_STRATEGIST,
                stage="repair",
                current_check=current_check,
            )
            selected_id = repaired.selected_candidate_id
            current_check = toolbox.verify(selected_id)
            run.proposed_hard_violations += _count_bad_accept(repaired, current_check)
            run.repair_succeeded = current_check.hard_valid and repaired.decision == "accept"
        orchestrator = await _invoke_agent(
            bounded,
            toolbox,
            architecture=ArchitectureName.MULTI,
            role=AgentRole.ORCHESTRATOR,
            stage="orchestrator",
            current_check=current_check,
        )
        if orchestrator.selected_candidate_id is not None:
            selected_id = orchestrator.selected_candidate_id
            current_check = toolbox.verify(selected_id)
        run.proposed_hard_violations += _count_bad_accept(orchestrator, current_check)
        run.selected_candidate_id = selected_id
        run.valid_candidate_found = current_check.hard_valid
        final_proposal = (
            orchestrator
            if orchestrator.selected_candidate_id is not None
            else orchestrator.model_copy(update={"selected_candidate_id": selected_id})
        )
        _record_release(run, toolbox, final_proposal, current_check, release_gate)
    except (ModelGatewayError, PermissionError, StructuredOutputError, ValueError) as exc:
        run.failure = f"{type(exc).__name__}:{exc}"
    finally:
        run.latency_seconds = perf_counter() - started
    return run


class ClientFactory(Protocol):
    def __call__(self) -> ModelClient: ...


def _aggregate(rows: Sequence[dict[str, JsonValue]], architecture: str) -> dict[str, JsonValue]:
    arm_rows = [row[architecture] for row in rows]
    typed = [item for item in arm_rows if isinstance(item, dict)]
    repair_rows = [item for row, item in zip(rows, typed, strict=True) if row["expected_repair"]]

    def number(item: Mapping[str, JsonValue], key: str) -> float:
        raw = item.get(key, 0)
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0

    def rate(key: str) -> float:
        return mean(bool(item.get(key)) for item in typed) if typed else 0

    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "scenario_count": len(typed),
            "valid_plan_rate": rate("accepted"),
            "valid_candidate_found_rate": rate("valid_candidate_found"),
            "proposed_hard_constraint_violation_count": sum(
                number(item, "proposed_hard_constraint_violation_count") for item in typed
            ),
            "released_hard_constraint_violation_count": sum(
                number(item, "released_hard_constraint_violation_count") for item in typed
            ),
            "repair_success_rate": (
                mean(bool(item.get("repair_succeeded")) for item in repair_rows)
                if repair_rows
                else 0
            ),
            "mean_latency_seconds": mean(number(item, "latency_seconds") for item in typed),
            "mean_model_calls": mean(number(item, "model_call_count") for item in typed),
            "mean_tool_calls": mean(number(item, "tool_call_count") for item in typed),
            "mean_total_tokens": mean(number(item, "total_tokens") for item in typed),
            "total_estimated_cost_usd": sum(
                number(item, "estimated_cost_usd") for item in typed
            ),
            "budget_breach_count": sum(bool(item.get("budget_breached")) for item in typed),
            "failure_count": sum(item.get("failure") is not None for item in typed),
        }
    )


def load_scenarios(path: Path = SCENARIOS) -> tuple[ArchitectureScenario, ...]:
    return tuple(
        ArchitectureScenario.model_validate_json(line)
        for line in path.read_text().splitlines()
        if line.strip()
    )


async def evaluate_architectures(
    *,
    path: Path = SCENARIOS,
    client_factory: ClientFactory | None = None,
    budget: FairBudget | None = None,
    limit: int | None = None,
    mode: str = "scripted",
    release_gate: ReleaseGate = deterministic_release_gate,
) -> dict[str, JsonValue]:
    scenarios = load_scenarios(path)
    if limit is not None:
        scenarios = scenarios[:limit]
    if not scenarios:
        raise ValueError("architecture benchmark requires at least one scenario")
    effective_budget = budget or FairBudget()
    factory = client_factory or (lambda: SyntheticPolicyClient())
    rows: list[dict[str, JsonValue]] = []
    identities: dict[str, set[str]] = {
        ArchitectureName.SINGLE: set(),
        ArchitectureName.MULTI: set(),
    }
    for index, scenario in enumerate(scenarios):
        single_client = factory()
        multi_client = factory()
        # Alternating AB/BA order reduces a fixed arm-order bias in live pilots.
        if index % 2 == 0:
            single = await _run_single(
                scenario, single_client, effective_budget, release_gate
            )
            multi = await _run_multi(scenario, multi_client, effective_budget, release_gate)
            run_order = [ArchitectureName.SINGLE, ArchitectureName.MULTI]
        else:
            multi = await _run_multi(scenario, multi_client, effective_budget, release_gate)
            single = await _run_single(
                scenario, single_client, effective_budget, release_gate
            )
            run_order = [ArchitectureName.MULTI, ArchitectureName.SINGLE]
        identities[ArchitectureName.SINGLE].add(f"{single.provider}:{single.model}")
        identities[ArchitectureName.MULTI].add(f"{multi.provider}:{multi.model}")
        rows.append(
            TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "scenario_id": scenario.id,
                    "category": scenario.category,
                    "expected_repair": scenario.expected_repair,
                    "run_order": run_order,
                    ArchitectureName.SINGLE: single.as_dict(),
                    ArchitectureName.MULTI: multi.as_dict(),
                }
            )
        )
    single_identities = sorted(identities[ArchitectureName.SINGLE])
    multi_identities = sorted(identities[ArchitectureName.MULTI])
    same_model = single_identities == multi_identities and len(single_identities) == 1
    evidence_tier = (
        "scripted_harness_validation"
        if mode == "scripted"
        else "live_model_on_frozen_synthetic_tasks_pilot"
    )
    result = {
        "suite": "agent-architecture-v1",
        "evidence_tier": evidence_tier,
        "scenario_count": len(scenarios),
        "category_count": len({item.category for item in scenarios}),
        "scenario_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "fairness_contract": {
            "same_task_set": True,
            "same_model_identity": same_model,
            "single_model_identities": single_identities,
            "multi_model_identities": multi_identities,
            "same_tool_contract": True,
            "tool_contract_sha256": _tool_contract_sha256(),
            "same_total_budget": True,
            "released_candidate_audit": "independent_recomputation_from_raw_candidate_fields",
            "budget": effective_budget.model_dump(mode="json"),
            "temperature": 0,
            "arm_order": "deterministic_alternating_ab_ba",
        },
        "metrics": {
            ArchitectureName.SINGLE: _aggregate(rows, ArchitectureName.SINGLE),
            ArchitectureName.MULTI: _aggregate(rows, ArchitectureName.MULTI),
        },
        "claim_boundary": {
            "winner_claim_allowed": False,
            "reason": (
                "scripted/synthetic 仅验证基准与安全门；不能证明多 Agent 优于单 Agent。"
                if mode == "scripted"
                else (
                    "真实模型小样本只属于冻结合成任务 pilot；不能外推到线上 OTA、"
                    "生产 SLA 或总体优势。"
                )
            ),
            "required_for_stronger_claim": [
                "预注册样本量与统计检验",
                "多个随机种子和至少两个模型家族",
                "未见真实任务与人工盲评",
                "真实供应商链路的独立线上证据",
            ],
        },
        "rows": rows,
    }
    return TypeAdapter(dict[str, JsonValue]).validate_python(result)


def _live_factory(args: argparse.Namespace) -> Callable[[], ModelClient]:
    api_key = os.getenv(args.api_key_env)
    if args.provider == ModelProviderName.ANTHROPIC.value and not api_key:
        raise ValueError(f"missing API key in environment variable {args.api_key_env}")
    config = ModelClientConfig(
        provider=ModelProviderName(args.provider),
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
        retry=ModelRetryPolicy(max_attempts=2),
        pricing=ModelPricing(
            input_usd_per_million_tokens=args.input_usd_per_million,
            output_usd_per_million_tokens=args.output_usd_per_million,
        ),
    )
    return lambda: build_model_client(config)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fair single-LLM-Agent vs multi-Agent architecture benchmark"
    )
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--provider", choices=tuple(item.value for item in ModelProviderName))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", default="TRIPCHORD_MODEL_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--input-usd-per-million", type=float, default=0)
    parser.add_argument("--output-usd-per-million", type=float, default=0)
    parser.add_argument("--ack-live-cost", action="store_true")
    parser.add_argument("--max-model-calls", type=int, default=16)
    parser.add_argument("--max-tool-calls", type=int, default=16)
    parser.add_argument("--max-total-tokens", type=int, default=12_000)
    args = parser.parse_args()
    factory: ClientFactory | None = None
    if args.mode == "live":
        if not args.ack_live_cost:
            parser.error("--mode live requires --ack-live-cost")
        if not args.provider or not args.model:
            parser.error("--mode live requires --provider and --model")
        if args.limit is None:
            parser.error("--mode live requires an explicit --limit")
        factory = _live_factory(args)
    result = asyncio.run(
        evaluate_architectures(
            path=args.scenarios,
            client_factory=factory,
            budget=FairBudget(
                max_model_calls=args.max_model_calls,
                max_tool_calls=args.max_tool_calls,
                max_total_tokens=args.max_total_tokens,
            ),
            limit=args.limit,
            mode=args.mode,
        )
    )
    body = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    print(body, end="")


if __name__ == "__main__":
    main()
