from __future__ import annotations

from tripchord.domain.common import DomainModel
from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import ItineraryItem, PlanStatus, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.impact import PlanDependency
from tripchord.planning.optimizer import ItineraryOptimizer
from tripchord.planning.policy import (
    ReplanCandidateMetrics,
    ReplanMode,
    ReplanPolicyDecision,
    ReplanPolicySelector,
    ReplanPreference,
)
from tripchord.planning.problem import PlanningInfeasible, PlanningProblem
from tripchord.planning.repair import PlanDiff, diff_plans
from tripchord.planning.replanner import LocalReplanner, LocalReplanResult, ReplanStatus
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowStatus


class AdaptiveReplanResult(DomainModel):
    status: ReplanStatus
    event: PlanEvent
    preference: ReplanPreference
    selected_mode: ReplanMode
    policy: ReplanPolicyDecision
    candidates: tuple[ReplanCandidateMetrics, ...]
    final_plan: PlanVersion
    diff: PlanDiff
    overall_preservation_ratio: float
    unaffected_preservation_ratio: float
    message: str


class AdaptiveReplanner:
    def __init__(
        self,
        selector: ReplanPolicySelector,
        *,
        max_repair_iterations: int = 3,
    ) -> None:
        self._selector = selector
        self._max_iterations = max_repair_iterations

    def replan(
        self,
        spec: TripSpec,
        plan: PlanVersion,
        event: PlanEvent,
        preference: ReplanPreference,
        problem: PlanningProblem | None,
        context: VerificationContext | None = None,
        dependencies: tuple[PlanDependency, ...] | None = None,
        replacements: dict[str, ItineraryItem] | None = None,
    ) -> AdaptiveReplanResult:
        verification_context = context or VerificationContext()
        local = LocalReplanner(max_repair_iterations=self._max_iterations).replan(
            spec,
            plan,
            event,
            verification_context,
            dependencies,
            replacements,
        )
        initial_utility = sum(item.utility for item in plan.items)
        local_metrics = self._metrics(
            ReplanMode.LOCAL,
            local.final_plan,
            plan,
            local.status == ReplanStatus.READY,
            initial_utility,
        )
        global_plan = self._global_candidate(
            problem,
            plan,
            event,
            local,
            verification_context,
        )
        global_metrics = (
            self._metrics(
                ReplanMode.GLOBAL,
                global_plan,
                plan,
                True,
                initial_utility,
            )
            if global_plan is not None
            else None
        )
        decision = self._selector.select(preference, local_metrics, global_metrics)
        selected = (
            global_plan
            if decision.selected_mode == ReplanMode.GLOBAL and global_plan is not None
            else local.final_plan
        )
        selected_metrics = global_metrics if selected is global_plan else local_metrics
        assert selected_metrics is not None
        status = ReplanStatus.READY if selected_metrics.hard_valid else local.status
        unaffected = local.impact.unaffected_item_ids
        before_items = {item.id: item for item in plan.items}
        after_items = {item.id: item for item in selected.items}
        unaffected_preserved = sum(
            after_items.get(item_id) == before_items[item_id] for item_id in unaffected
        )
        return AdaptiveReplanResult(
            status=status,
            event=event,
            preference=preference,
            selected_mode=decision.selected_mode,
            policy=decision,
            candidates=tuple(
                candidate for candidate in (local_metrics, global_metrics) if candidate is not None
            ),
            final_plan=selected,
            diff=diff_plans(plan, selected),
            overall_preservation_ratio=selected_metrics.preservation_ratio,
            unaffected_preservation_ratio=(
                unaffected_preserved / len(unaffected) if unaffected else 1.0
            ),
            message=(
                f"selected {decision.selected_mode.value} replanning after deterministic "
                f"verification for {preference.value} preference"
            ),
        )

    def _global_candidate(
        self,
        problem: PlanningProblem | None,
        plan: PlanVersion,
        event: PlanEvent,
        local: LocalReplanResult,
        context: VerificationContext,
    ) -> PlanVersion | None:
        if problem is None or local.status != ReplanStatus.READY:
            return None
        if event.kind not in {
            EventKind.PLACE_CLOSED,
            EventKind.SOLD_OUT,
            EventKind.WEATHER_ALERT,
        }:
            return None
        candidate_ids = {candidate.id for candidate in problem.activities}
        removed_ids = {
            item_id.removeprefix("activity:")
            for item_id in local.impact.direct_item_ids
            if item_id.removeprefix("activity:") in candidate_ids
        }
        if not removed_ids:
            return None
        global_problem = problem.model_copy(
            update={
                "activities": tuple(
                    item for item in problem.activities if item.id not in removed_ids
                )
            }
        )
        try:
            optimizer = ItineraryOptimizer()
            solved = optimizer.solve(global_problem)
        except PlanningInfeasible:
            return None
        draft = optimizer.to_plan(
            solved,
            global_problem,
            trip_id=plan.trip_id,
            plan_id=f"{plan.trip_id}:plan:v{plan.version + 1}",
            version=plan.version + 1,
        ).model_copy(
            update={
                "parent_version_id": plan.id,
                "applied_event_ids": (*plan.applied_event_ids, event.id),
                "status": PlanStatus.VERIFYING,
            }
        )
        workflow = PlanningWorkflow(max_repair_iterations=self._max_iterations).run(
            problem.trip,
            draft,
            context,
        )
        if workflow.status != WorkflowStatus.READY:
            return None
        return workflow.final_plan.model_copy(
            update={
                "id": f"{plan.trip_id}:plan:v{plan.version + 1}",
                "version": plan.version + 1,
                "parent_version_id": plan.id,
                "applied_event_ids": (*plan.applied_event_ids, event.id),
            }
        )

    def _metrics(
        self,
        mode: ReplanMode,
        candidate: PlanVersion,
        before: PlanVersion,
        hard_valid: bool,
        initial_utility: int,
    ) -> ReplanCandidateMetrics:
        before_items = {item.id: item for item in before.items}
        after_items = {item.id: item for item in candidate.items}
        preserved = sum(after_items.get(item_id) == item for item_id, item in before_items.items())
        utility = sum(item.utility for item in candidate.items)
        return ReplanCandidateMetrics(
            mode=mode,
            hard_valid=hard_valid,
            preservation_ratio=preserved / len(before_items) if before_items else 1.0,
            utility_retention=utility / initial_utility if initial_utility else 1.0,
        )
