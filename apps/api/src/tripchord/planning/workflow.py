from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.domain.itinerary import PlanStatus, PlanVersion, Violation, ViolationSeverity
from tripchord.domain.trip import TripSpec
from tripchord.planning.repair import (
    PlanDiff,
    RepairAction,
    RepairEngine,
    StructuredRepairPlan,
)
from tripchord.planning.reverification import (
    DeclarativePlanReVerifier,
    PlanReverificationReport,
)
from tripchord.planning.verifier import PlanVerifier, VerificationContext


class WorkflowStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


class WorkflowTrace(DomainModel):
    iteration: int = Field(ge=1)
    before_plan_id: str
    after_plan_id: str
    violations_before: tuple[Violation, ...]
    actions: tuple[RepairAction, ...]
    repair_plan: StructuredRepairPlan
    diff: PlanDiff
    violations_after: tuple[Violation, ...]
    reverification: PlanReverificationReport


class WorkflowResult(DomainModel):
    status: WorkflowStatus
    final_plan: PlanVersion
    remaining_violations: tuple[Violation, ...]
    traces: tuple[WorkflowTrace, ...]
    final_reverification: PlanReverificationReport | None = None


class PlanningWorkflow:
    def __init__(
        self,
        verifier: PlanVerifier | None = None,
        repair_engine: RepairEngine | None = None,
        reverifier: DeclarativePlanReVerifier | None = None,
        *,
        max_repair_iterations: int = 3,
    ) -> None:
        self._verifier = verifier or PlanVerifier()
        self._repair = repair_engine or RepairEngine()
        self._reverifier = reverifier or DeclarativePlanReVerifier()
        self._max_iterations = max_repair_iterations

    def run(
        self,
        spec: TripSpec,
        initial_plan: PlanVersion,
        context: VerificationContext | None = None,
    ) -> WorkflowResult:
        verification_context = context or VerificationContext()
        current = initial_plan
        traces: list[WorkflowTrace] = []
        violations = self._verifier.verify(spec, current, verification_context)
        if not self._errors(violations):
            return self._ready(current, violations, traces)

        for iteration in range(1, self._max_iterations + 1):
            outcome = self._repair.repair(spec, current, self._errors(violations))
            if not outcome.diff.changed:
                return WorkflowResult(
                    status=WorkflowStatus.BLOCKED,
                    final_plan=current,
                    remaining_violations=violations,
                    traces=tuple(traces),
                )
            after = self._verifier.verify(spec, outcome.plan, verification_context)
            reverification = self._reverifier.verify(
                spec,
                current,
                outcome.plan,
                outcome.diff,
                verification_context,
            )
            traces.append(
                WorkflowTrace(
                    iteration=iteration,
                    before_plan_id=current.id,
                    after_plan_id=outcome.plan.id,
                    violations_before=violations,
                    actions=outcome.actions,
                    repair_plan=outcome.repair_plan,
                    diff=outcome.diff,
                    violations_after=after,
                    reverification=reverification,
                )
            )
            current = outcome.plan
            violations = after
            if not reverification.passed:
                return WorkflowResult(
                    status=WorkflowStatus.BLOCKED,
                    final_plan=current,
                    remaining_violations=violations,
                    traces=tuple(traces),
                    final_reverification=reverification,
                )
            if not self._errors(violations):
                return self._ready(
                    current,
                    violations,
                    traces,
                    reverification=reverification,
                )

        return WorkflowResult(
            status=WorkflowStatus.EXHAUSTED,
            final_plan=current,
            remaining_violations=violations,
            traces=tuple(traces),
            final_reverification=(traces[-1].reverification if traces else None),
        )

    def _ready(
        self,
        plan: PlanVersion,
        violations: tuple[Violation, ...],
        traces: list[WorkflowTrace],
        *,
        reverification: PlanReverificationReport | None = None,
    ) -> WorkflowResult:
        return WorkflowResult(
            status=WorkflowStatus.READY,
            final_plan=plan.model_copy(update={"status": PlanStatus.READY}),
            remaining_violations=violations,
            traces=tuple(traces),
            final_reverification=reverification,
        )

    def _errors(self, violations: tuple[Violation, ...]) -> tuple[Violation, ...]:
        return tuple(
            violation for violation in violations if violation.severity == ViolationSeverity.ERROR
        )
