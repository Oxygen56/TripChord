from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import JsonValue, TypeAdapter

from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.model_agent import ModelToolAgent
from tripchord.agents.model_gateway import ModelMessage, ModelRequest, ModelRouter, compact_json
from tripchord.agents.models import (
    AgentDecision,
    AgentRole,
    AgentTask,
    AgentTaskResult,
    DecisionState,
    EvidenceRecord,
    PreferenceConstitution,
    TaskGraph,
)
from tripchord.agents.runtime import AgentRegistry, DynamicTaskScheduler, SchedulerOutcome
from tripchord.agents.tools import ToolRegistry
from tripchord.domain.common import DomainModel
from tripchord.domain.itinerary import PlanVersion, ViolationSeverity
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.problem import PlanningProblem
from tripchord.planning.verifier import PlanVerifier
from tripchord.planning.workflow import PlanningWorkflow, WorkflowStatus


class CandidateEnvelope(DomainModel):
    id: str
    generator: str
    plan: PlanVersion
    objective_value: float
    total_utility: int


class TravelAgentRun(DomainModel):
    decision: AgentDecision
    selected_candidate_id: str | None
    final_plan: PlanVersion | None
    preferences: PreferenceConstitution
    scheduler: SchedulerOutcome
    evidence: tuple[EvidenceRecord, ...]


def _json_dict(value: object) -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_python(value)


def _parse_json_object(text: str) -> dict[str, JsonValue]:
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError:
        return {"summary": text}
    if not isinstance(value, dict):
        return {"summary": text}
    return _json_dict(value)


def _evidence(
    *,
    task: AgentTask,
    topic: str,
    subject: str,
    payload: dict[str, JsonValue],
    source: str,
    confidence: float = 1,
) -> EvidenceRecord:
    now = datetime.now(UTC)
    return EvidenceRecord(
        id=f"{task.id}:evidence:v1",
        topic=topic,
        subject=subject,
        payload=payload,
        source=source,
        captured_at=now,
        expires_at=now + timedelta(minutes=30),
        confidence=confidence,
        owner_agent=task.role,
    )


class GroundedSpecialistAgent:
    def __init__(
        self,
        role: AgentRole,
        router: ModelRouter,
        *,
        topic: str,
        system_prompt: str,
    ) -> None:
        self.role = role
        self._topic = topic
        self._inner = ModelToolAgent(role, router, system_prompt=system_prompt)

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        tool_registry: ToolRegistry,
    ) -> AgentTaskResult:
        result = await self._inner.execute(task, context_engine, tool_registry)
        if not result.success:
            return result
        record = _evidence(
            task=task,
            topic=self._topic,
            subject=str(task.input.get("subject", task.id)),
            payload=result.output,
            source=f"{result.model_provider}:{result.model_name}",
            confidence=0.9,
        )
        return result.model_copy(update={"evidence": (record,)})


class EvidenceArbiterAgent:
    role = AgentRole.EVIDENCE_ARBITER

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        _: ToolRegistry,
    ) -> AgentTaskResult:
        pack = context_engine.build_pack(task)
        routed = await self._router.complete(
            ModelRequest(
                role=self.role,
                system=(
                    "你是证据仲裁 Agent。比较来源、时间、价格口径与冲突；只输出 JSON，"
                    "列出可用 evidence_refs、冲突和不可比较项，不得把回放数据说成实时价格。"
                ),
                messages=(
                    ModelMessage(
                        role="user",
                        content=compact_json(
                            {"evidence": [item.model_dump(mode="json") for item in pack.evidence]}
                        ),
                    ),
                ),
                risk_level=1,
            )
        )
        output = _parse_json_object(routed.response.text)
        output["input_evidence_refs"] = TypeAdapter(JsonValue).validate_python(
            list(pack.evidence_refs)
        )
        record = _evidence(
            task=task,
            topic="arbitration",
            subject="travel_sources",
            payload=output,
            source=f"{routed.response.provider}:{routed.response.model}",
            confidence=0.9,
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary=str(output.get("summary", "证据仲裁完成")),
            output=output,
            evidence=(record,),
            model_provider=routed.response.provider,
            model_name=routed.response.model,
            token_usage=routed.response.usage.total_tokens,
        )


class PreferenceGuardAgent:
    role = AgentRole.PREFERENCE_GUARD

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        _: ToolRegistry,
    ) -> AgentTaskResult:
        preferences = PreferenceConstitution.model_validate(task.input["preferences"])
        pack = context_engine.build_pack(task)
        observations: dict[str, list[JsonValue]] = {}
        for record in pack.evidence:
            self._collect(record.payload, observations)
        violations: list[dict[str, JsonValue]] = []
        weighted_score = 0.0
        weighted_total = 0.0
        evaluated: list[dict[str, JsonValue]] = []
        for rule in preferences.effective_rules():
            values = observations.get(rule.key, [])
            expected = True if rule.expected is None else rule.expected
            matches = any(value == expected for value in values)
            if rule.mode.value == "required" and not matches:
                violations.append(
                    {
                        "key": rule.key,
                        "mode": rule.mode.value,
                        "reason": "缺少满足用户明确必选偏好的证据",
                        "observed": values,
                        "expected": expected,
                    }
                )
            elif rule.mode.value == "forbidden" and matches:
                violations.append(
                    {
                        "key": rule.key,
                        "mode": rule.mode.value,
                        "reason": "候选命中用户明确禁止项",
                        "observed": values,
                        "expected": expected,
                    }
                )
            elif rule.mode.value == "weighted":
                weighted_total += rule.weight
                if matches:
                    weighted_score += rule.weight
            evaluated.append(
                {
                    "key": rule.key,
                    "mode": rule.mode.value,
                    "source": rule.source.value,
                    "matched": matches,
                    "observed": values,
                    "expected": expected,
                }
            )
        output = _json_dict(
            {
                "hard_violations": violations,
                "evaluated": evaluated,
                "weighted_score": weighted_score,
                "weighted_total": weighted_total,
                "effective_rule_count": len(preferences.effective_rules()),
            }
        )
        record = _evidence(
            task=task,
            topic="preference",
            subject="constitution_compliance",
            payload=output,
            source="preference-constitution-guard",
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary=("偏好宪章存在硬冲突" if violations else "偏好宪章校验通过"),
            output=output,
            evidence=(record,),
        )

    def _collect(
        self,
        value: JsonValue,
        observations: dict[str, list[JsonValue]],
    ) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                observations.setdefault(key, []).append(child)
                self._collect(child, observations)
        elif isinstance(value, list):
            for child in value:
                self._collect(child, observations)


class CpSatPlannerAgent:
    role = AgentRole.CP_SAT_PLANNER

    async def execute(
        self,
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        problem = PlanningProblem.model_validate(task.input["problem"])
        optimizer = ItineraryOptimizer()
        solved = optimizer.solve(problem)
        candidate = CandidateEnvelope(
            id="candidate:cp-sat",
            generator="cp_sat",
            plan=optimizer.to_plan(
                solved,
                problem,
                trip_id=str(task.input.get("trip_id", "agent-trip")),
                plan_id="agent-trip:cp-sat:v1",
            ),
            objective_value=solved.objective_value,
            total_utility=solved.total_utility,
        )
        output = _json_dict(candidate.model_dump(mode="json"))
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary="CP-SAT 候选已生成",
            output=output,
            evidence=(
                _evidence(
                    task=task,
                    topic="candidate",
                    subject=candidate.id,
                    payload=output,
                    source="ortools-cp-sat",
                ),
            ),
        )


class NeuralPlannerAgent:
    role = AgentRole.NEURAL_PLANNER

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        _: ToolRegistry,
    ) -> AgentTaskResult:
        problem = PlanningProblem.model_validate(task.input["problem"])
        pack = context_engine.build_pack(task)
        routed = await self._router.complete(
            ModelRequest(
                role=self.role,
                system=(
                    "你是神经行程规划 Agent。根据候选、用户偏好和已仲裁证据选择活动。"
                    "只输出 JSON: {selected_activity_ids:[...], summary:string, "
                    "shift_first_minutes?:integer}。不得编造候选。"
                ),
                messages=(
                    ModelMessage(
                        role="user",
                        content=compact_json(
                            {
                                "problem": problem.model_dump(mode="json"),
                                "evidence": [
                                    item.model_dump(mode="json") for item in pack.evidence
                                ],
                                "preferences": task.input.get("preferences", {}),
                            }
                        ),
                    ),
                ),
            )
        )
        selected_output = _parse_json_object(routed.response.text)
        raw_ids = selected_output.get("selected_activity_ids", [])
        selected_ids = (
            {item for item in raw_ids if isinstance(item, str)}
            if isinstance(raw_ids, list)
            else set()
        )
        selected_ids.update(item.id for item in problem.activities if item.must_visit)
        known_ids = {item.id for item in problem.activities}
        if not selected_ids or not selected_ids <= known_ids:
            return AgentTaskResult(
                task_id=task.id,
                agent_role=self.role,
                success=False,
                summary="神经规划器返回未知或空候选",
                failure_class="invalid_neural_candidate",
                model_provider=routed.response.provider,
                model_name=routed.response.model,
                token_usage=routed.response.usage.total_tokens,
            )
        restricted = problem.model_copy(
            update={
                "activities": tuple(item for item in problem.activities if item.id in selected_ids)
            }
        )
        optimizer = ItineraryOptimizer()
        solved = optimizer.solve(restricted)
        plan = optimizer.to_plan(
            solved,
            restricted,
            trip_id=str(task.input.get("trip_id", "agent-trip")),
            plan_id="agent-trip:neural:v1",
        )
        shift = selected_output.get("shift_first_minutes", 0)
        if isinstance(shift, int) and shift and plan.items:
            first = plan.items[0]
            shifted = first.model_copy(
                update={
                    "starts_at": first.starts_at + timedelta(minutes=shift),
                    "ends_at": first.ends_at + timedelta(minutes=shift),
                }
            )
            plan = plan.model_copy(update={"items": (shifted, *plan.items[1:])})
        candidate = CandidateEnvelope(
            id="candidate:neural",
            generator="neural_plus_cp_sat_projection",
            plan=plan,
            objective_value=solved.objective_value,
            total_utility=solved.total_utility,
        )
        output = _json_dict(candidate.model_dump(mode="json"))
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary=str(selected_output.get("summary", "神经候选已生成")),
            output=output,
            evidence=(
                _evidence(
                    task=task,
                    topic="candidate",
                    subject=candidate.id,
                    payload=output,
                    source=f"{routed.response.provider}:{routed.response.model}",
                    confidence=0.85,
                ),
            ),
            model_provider=routed.response.provider,
            model_name=routed.response.model,
            token_usage=routed.response.usage.total_tokens,
        )


class CriticAgent:
    role = AgentRole.CRITIC

    def __init__(self, router: ModelRouter) -> None:
        self._router = router
        self._verifier = PlanVerifier()

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        _: ToolRegistry,
    ) -> AgentTaskResult:
        problem = PlanningProblem.model_validate(task.input["problem"])
        candidates = [
            CandidateEnvelope.model_validate(record.payload)
            for record in context_engine.build_pack(task).evidence
            if record.topic == "candidate"
        ]
        findings = {
            candidate.id: [
                violation.model_dump(mode="json")
                for violation in self._verifier.verify(problem.trip, candidate.plan)
            ]
            for candidate in candidates
        }
        routed = await self._router.complete(
            ModelRequest(
                role=self.role,
                system=(
                    "你是异构批评 Agent。确定性校验结果不可删除；补充体验、证据和鲁棒性批评。"
                    "只输出 JSON，包含 recommendation 和 reasons。"
                ),
                messages=(
                    ModelMessage(
                        role="user",
                        content=compact_json(
                            {
                                "candidates": [item.model_dump(mode="json") for item in candidates],
                                "deterministic_findings": findings,
                            }
                        ),
                    ),
                ),
                risk_level=1,
            )
        )
        output = _parse_json_object(routed.response.text)
        output["deterministic_findings"] = TypeAdapter(JsonValue).validate_python(findings)
        record = _evidence(
            task=task,
            topic="critique",
            subject="candidate_pool",
            payload=output,
            source=f"deterministic-verifier+{routed.response.provider}:{routed.response.model}",
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary=str(output.get("summary", "候选批评完成")),
            output=output,
            evidence=(record,),
            model_provider=routed.response.provider,
            model_name=routed.response.model,
            token_usage=routed.response.usage.total_tokens,
        )


class RepairAgent:
    role = AgentRole.REPAIR

    async def execute(
        self,
        task: AgentTask,
        _: ContextEngine,
        __: ToolRegistry,
    ) -> AgentTaskResult:
        problem = PlanningProblem.model_validate(task.input["problem"])
        plan = PlanVersion.model_validate(task.input["plan"])
        outcome = PlanningWorkflow(max_repair_iterations=3).run(problem.trip, plan)
        output = _json_dict(
            {
                "workflow_status": outcome.status.value,
                "plan": outcome.final_plan.model_dump(mode="json"),
                "remaining_violations": [
                    item.model_dump(mode="json") for item in outcome.remaining_violations
                ],
                "repair_iterations": len(outcome.traces),
            }
        )
        next_task = AgentTask(
            id="orchestrator-final",
            role=AgentRole.ORCHESTRATOR,
            goal="对修复后的候选做最终三态裁决",
            dependencies=(task.id,),
            input={"problem": task.input["problem"], "repaired": output},
            context_topics=("candidate", "critique", "preference"),
            priority=100,
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary="自主修复完成" if outcome.status == WorkflowStatus.READY else "修复后仍有阻塞",
            output=output,
            spawned_tasks=(next_task,),
        )


class OrchestratorAgent:
    role = AgentRole.ORCHESTRATOR

    def __init__(self, router: ModelRouter) -> None:
        self._router = router
        self._verifier = PlanVerifier()

    async def execute(
        self,
        task: AgentTask,
        context_engine: ContextEngine,
        _: ToolRegistry,
    ) -> AgentTaskResult:
        problem = PlanningProblem.model_validate(task.input["problem"])
        if "repaired" in task.input:
            pack = context_engine.build_pack(task)
            repaired = TypeAdapter(dict[str, JsonValue]).validate_python(task.input["repaired"])
            plan = PlanVersion.model_validate(repaired["plan"])
            violations = self._verifier.verify(problem.trip, plan)
            errors = tuple(item for item in violations if item.severity == ViolationSeverity.ERROR)
            decision = AgentDecision(
                state=DecisionState.ACCEPT if not errors else DecisionState.REPLAN_OR_BLOCK,
                summary="修复方案通过最终校验" if not errors else "修复耗尽后仍有硬约束冲突",
                verifier_violations=tuple(item.code.value for item in violations),
                evidence_refs=pack.evidence_refs,
            )
            return AgentTaskResult(
                task_id=task.id,
                agent_role=self.role,
                success=True,
                summary=decision.summary,
                output=_json_dict(
                    {
                        "decision": decision.model_dump(mode="json"),
                        "selected_candidate_id": "candidate:repaired",
                        "plan": plan.model_dump(mode="json"),
                    }
                ),
            )

        pack = context_engine.build_pack(task)
        candidates = [
            CandidateEnvelope.model_validate(record.payload)
            for record in pack.evidence
            if record.topic == "candidate"
        ]
        preference_violations: list[JsonValue] = []
        for record in pack.evidence:
            raw_violations = record.payload.get("hard_violations", [])
            if record.topic == "preference" and isinstance(raw_violations, list):
                preference_violations.extend(raw_violations)
        if not candidates:
            decision = AgentDecision(
                state=DecisionState.REPLAN_OR_BLOCK,
                summary="没有可裁决的候选方案",
            )
            return AgentTaskResult(
                task_id=task.id,
                agent_role=self.role,
                success=True,
                summary=decision.summary,
                output={"decision": decision.model_dump(mode="json")},
            )
        routed = await self._router.complete(
            ModelRequest(
                role=self.role,
                system=(
                    "你是主控 Agent，拥有最终裁决权。综合候选、确定性校验、偏好和批评，"
                    "只输出 JSON: {selected_candidate_id, summary}。不得隐藏硬约束冲突。"
                ),
                messages=(
                    ModelMessage(
                        role="user",
                        content=compact_json(
                            {"evidence": [item.model_dump(mode="json") for item in pack.evidence]}
                        ),
                    ),
                ),
                risk_level=3,
            )
        )
        proposed = _parse_json_object(routed.response.text)
        requested_id = proposed.get("selected_candidate_id")
        selected = next((item for item in candidates if item.id == requested_id), None)
        fallback_reason: str | None = None
        if selected is None:
            selected = max(candidates, key=lambda item: item.total_utility)
            fallback_reason = "模型选择了不存在的候选，主控改选效用最高的已知候选"
        violations = self._verifier.verify(problem.trip, selected.plan)
        errors = tuple(item for item in violations if item.severity == ViolationSeverity.ERROR)
        spawned: tuple[AgentTask, ...]
        if preference_violations:
            decision = AgentDecision(
                state=DecisionState.REPLAN_OR_BLOCK,
                summary="主控拒绝覆盖用户明确偏好的候选；需要补充满足偏好的来源或候选",
                verifier_violations=tuple(
                    f"preference:{item.get('key', 'unknown')}"
                    for item in preference_violations
                    if isinstance(item, dict)
                ),
                evidence_refs=pack.evidence_refs,
            )
            spawned = ()
        elif errors:
            decision = AgentDecision(
                state=DecisionState.REPLAN_OR_BLOCK,
                summary="主控拒绝带硬约束冲突的候选并启动自主修复",
                verifier_violations=tuple(item.code.value for item in violations),
                evidence_refs=pack.evidence_refs,
            )
            repair_task = AgentTask(
                id="repair-selected",
                role=AgentRole.REPAIR,
                goal="修复主控选中方案的硬约束冲突",
                dependencies=(task.id,),
                input={
                    "problem": task.input["problem"],
                    "plan": selected.plan.model_dump(mode="json"),
                },
                priority=100,
            )
            spawned = (repair_task,)
        else:
            warnings = tuple(
                item for item in violations if item.severity == ViolationSeverity.WARNING
            )
            decision = AgentDecision(
                state=(DecisionState.ACCEPT_WITH_EXCEPTION if warnings else DecisionState.ACCEPT),
                summary=str(proposed.get("summary", "主控已完成候选裁决")),
                verifier_violations=tuple(item.code.value for item in violations),
                exception_reasons=tuple(item.message for item in warnings),
                evidence_refs=pack.evidence_refs,
                requires_user_confirmation=bool(warnings),
            )
            spawned = ()
        output = _json_dict(
            {
                "decision": decision.model_dump(mode="json"),
                "requested_candidate_id": requested_id,
                "selected_candidate_id": selected.id,
                "fallback_reason": fallback_reason,
                "plan": selected.plan.model_dump(mode="json"),
            }
        )
        return AgentTaskResult(
            task_id=task.id,
            agent_role=self.role,
            success=True,
            summary=decision.summary,
            output=output,
            spawned_tasks=spawned,
            model_provider=routed.response.provider,
            model_name=routed.response.model,
            token_usage=routed.response.usage.total_tokens,
        )


class TravelMultiAgentSystem:
    def __init__(
        self,
        router: ModelRouter,
        tool_registry: ToolRegistry,
        *,
        max_concurrency: int = 8,
    ) -> None:
        self._router = router
        self._tools = tool_registry
        self._max_concurrency = max_concurrency

    async def run(
        self,
        problem: PlanningProblem,
        preferences: PreferenceConstitution | None = None,
        *,
        official_research_urls: tuple[str, ...] = (),
    ) -> TravelAgentRun:
        constitution = preferences or PreferenceConstitution()
        registry = AgentRegistry()
        specialist_specs = [
            (
                AgentRole.TRANSPORT,
                "transport",
                "查询并比较可追溯交通报价，不得声称回放价是实时价。",
            ),
            (AgentRole.LODGING, "lodging", "查询酒店并核对房型、日期、早餐和取消条款。"),
            (AgentRole.POI, "poi", "查询景点开放、预约、位置和游览时长证据。"),
            (AgentRole.WEATHER, "weather", "查询天气证据并标注时间与预报不确定性。"),
        ]
        if official_research_urls and self._tools.has("research_official_page"):
            specialist_specs.append(
                (
                    AgentRole.BROWSER_RESEARCH,
                    "official_page",
                    "读取行程相关的官方公开页面并保留内容哈希和抓取时间。",
                )
            )
        for role, topic, prompt in specialist_specs:
            registry.register(
                GroundedSpecialistAgent(role, self._router, topic=topic, system_prompt=prompt)
            )
        registry.register(EvidenceArbiterAgent(self._router))
        registry.register(PreferenceGuardAgent())
        registry.register(CpSatPlannerAgent())
        registry.register(NeuralPlannerAgent(self._router))
        registry.register(CriticAgent(self._router))
        registry.register(RepairAgent())
        registry.register(OrchestratorAgent(self._router))

        tool_for_role = {
            AgentRole.TRANSPORT: "search_transport",
            AgentRole.LODGING: "search_lodging",
            AgentRole.POI: "search_poi",
            AgentRole.WEATHER: "search_weather",
            AgentRole.BROWSER_RESEARCH: "research_official_page",
        }
        common_input = {
            "origin": problem.trip.origin,
            "destination": problem.trip.destinations[0],
            "start_date": problem.trip.start_date.isoformat(),
            "end_date": problem.trip.end_date.isoformat(),
            "subject": problem.trip.destinations[0],
        }
        source_tasks = tuple(
            AgentTask(
                id=f"source-{role.value}",
                role=role,
                goal=prompt,
                allowed_tools=(tool_for_role[role],),
                input=(
                    {**common_input, "urls": list(official_research_urls)}
                    if role == AgentRole.BROWSER_RESEARCH
                    else common_input
                ),
            )
            for role, _, prompt in specialist_specs
        )
        source_ids = tuple(task.id for task in source_tasks)
        problem_json: JsonValue = TypeAdapter(JsonValue).validate_python(
            problem.model_dump(mode="json")
        )
        preference_json: JsonValue = TypeAdapter(JsonValue).validate_python(
            constitution.model_dump(mode="json")
        )
        graph = TaskGraph(
            tasks=(
                *source_tasks,
                AgentTask(
                    id="preference-guard",
                    role=AgentRole.PREFERENCE_GUARD,
                    goal="执行用户偏好宪章并拒绝静默覆盖",
                    context_topics=(
                        "transport",
                        "lodging",
                        "poi",
                        "weather",
                        "official_page",
                    ),
                    dependencies=source_ids,
                    input={"preferences": preference_json},
                    priority=30,
                ),
                AgentTask(
                    id="evidence-arbiter",
                    role=AgentRole.EVIDENCE_ARBITER,
                    goal="解决跨来源价格、时效和口径冲突",
                    context_topics=(
                        "transport",
                        "lodging",
                        "poi",
                        "weather",
                        "official_page",
                        "preference",
                    ),
                    dependencies=(*source_ids, "preference-guard"),
                    priority=20,
                ),
                AgentTask(
                    id="planner-neural",
                    role=AgentRole.NEURAL_PLANNER,
                    goal="生成神经规划候选",
                    context_topics=("arbitration",),
                    dependencies=("evidence-arbiter",),
                    input={"problem": problem_json, "preferences": preference_json},
                    priority=10,
                ),
                AgentTask(
                    id="planner-cp-sat",
                    role=AgentRole.CP_SAT_PLANNER,
                    goal="生成 CP-SAT 候选",
                    dependencies=("evidence-arbiter",),
                    input={"problem": problem_json},
                    priority=10,
                ),
                AgentTask(
                    id="critic",
                    role=AgentRole.CRITIC,
                    goal="对双路候选做确定性验证与异构批评",
                    context_topics=("candidate",),
                    dependencies=("planner-neural", "planner-cp-sat"),
                    input={"problem": problem_json},
                ),
                AgentTask(
                    id="orchestrator",
                    role=AgentRole.ORCHESTRATOR,
                    goal="综合证据并作最终三态裁决",
                    context_topics=("candidate", "critique", "preference"),
                    dependencies=("critic",),
                    input={"problem": problem_json},
                ),
            )
        )
        blackboard = EvidenceBlackboard()
        scheduler = await DynamicTaskScheduler(
            registry,
            max_concurrency=self._max_concurrency,
        ).run(graph, ContextEngine(blackboard), self._tools)
        orchestrator_results = [
            result
            for result in scheduler.results
            if result.agent_role == AgentRole.ORCHESTRATOR and "decision" in result.output
        ]
        if not orchestrator_results:
            decision = AgentDecision(
                state=DecisionState.REPLAN_OR_BLOCK,
                summary="运行未产出主控裁决",
            )
            return TravelAgentRun(
                decision=decision,
                selected_candidate_id=None,
                final_plan=None,
                preferences=constitution,
                scheduler=scheduler,
                evidence=blackboard.records,
            )
        final = orchestrator_results[-1]
        decision = AgentDecision.model_validate(final.output["decision"])
        selected_id = final.output.get("selected_candidate_id")
        plan_payload = final.output.get("plan")
        return TravelAgentRun(
            decision=decision,
            selected_candidate_id=selected_id if isinstance(selected_id, str) else None,
            final_plan=PlanVersion.model_validate(plan_payload) if plan_payload else None,
            preferences=constitution,
            scheduler=scheduler,
            evidence=blackboard.records,
        )
