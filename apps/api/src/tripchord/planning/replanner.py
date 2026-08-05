from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from tripchord.domain.common import DomainModel, Money
from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanStatus, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.impact import (
    ImpactAnalyzer,
    ImpactScope,
    PlanDependency,
    build_plan_dependencies,
)
from tripchord.planning.repair import (
    PlanDiff,
    RepairStep,
    RepairStepKind,
    RepairStrategy,
    StructuredRepairPlan,
    diff_plans,
)
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowResult, WorkflowStatus


class ReplanStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    NO_EFFECT = "no_effect"


class LocalReplanResult(DomainModel):
    status: ReplanStatus
    event: PlanEvent
    impact: ImpactScope
    final_plan: PlanVersion
    diff: PlanDiff
    workflow: WorkflowResult | None = None
    repair_plan: StructuredRepairPlan
    overall_preservation_ratio: float
    unaffected_preservation_ratio: float
    message: str


class LocalReplanner:
    def __init__(self, *, max_repair_iterations: int = 3) -> None:
        self._analyzer = ImpactAnalyzer()
        self._max_iterations = max_repair_iterations

    def replan(
        self,
        spec: TripSpec,
        plan: PlanVersion,
        event: PlanEvent,
        context: VerificationContext | None = None,
        dependencies: tuple[PlanDependency, ...] | None = None,
        replacements: dict[str, ItineraryItem] | None = None,
    ) -> LocalReplanResult:
        verification_context = context or VerificationContext()
        graph = (
            dependencies
            if dependencies is not None
            else build_plan_dependencies(plan, verification_context)
        )
        impact = self._analyzer.analyze(event, plan, graph)
        if event.id in plan.applied_event_ids:
            return self._result(
                ReplanStatus.NO_EFFECT,
                event,
                impact,
                plan,
                None,
                "event was already applied to this plan lineage",
            )
        if event.trip_id != plan.trip_id:
            return self._result(
                ReplanStatus.BLOCKED,
                event,
                impact,
                plan,
                None,
                "event trip_id does not match the plan",
            )
        if not impact.direct_item_ids:
            return self._result(
                ReplanStatus.NO_EFFECT,
                event,
                impact,
                plan,
                None,
                "event targets do not match this plan version",
            )

        original = {item.id: item for item in plan.items}
        locked_targets = [item_id for item_id in impact.direct_item_ids if original[item_id].locked]
        if locked_targets:
            return self._result(
                ReplanStatus.BLOCKED,
                event,
                impact,
                plan,
                None,
                f"directly affected items are locked: {', '.join(locked_targets)}",
            )

        if event.kind == EventKind.PRICE_CHANGED:
            price_error, price_unchanged = self._validate_price_change(plan, impact, event)
            if price_error is not None:
                return self._result(
                    ReplanStatus.BLOCKED,
                    event,
                    impact,
                    plan,
                    None,
                    price_error,
                )
            if price_unchanged:
                return self._result(
                    ReplanStatus.NO_EFFECT,
                    event,
                    impact,
                    plan,
                    None,
                    "observed price equals the current value; no plan version was created",
                )

        event_plan, error = self._apply_event(plan, event, impact, replacements or {})
        if error is not None:
            return self._result(
                ReplanStatus.BLOCKED,
                event,
                impact,
                plan,
                None,
                error,
            )

        scoped_plan = self._lock_unaffected(event_plan, impact)
        workflow = PlanningWorkflow(max_repair_iterations=self._max_iterations).run(
            spec,
            scoped_plan,
            verification_context,
        )
        restored = self._restore_locks(workflow.final_plan, original)
        restored = restored.model_copy(
            update={
                "id": f"{plan.trip_id}:plan:v{plan.version + 1}",
                "version": plan.version + 1,
                "parent_version_id": plan.id,
            }
        )
        if not self._unaffected_preserved(plan, restored, impact):
            return self._result(
                ReplanStatus.BLOCKED,
                event,
                impact,
                plan,
                workflow,
                "local replan attempted to modify an unaffected item",
            )
        status = (
            ReplanStatus.READY if workflow.status == WorkflowStatus.READY else ReplanStatus.BLOCKED
        )
        message = (
            "event applied and affected subgraph reverified"
            if status == ReplanStatus.READY
            else "affected subgraph could not be repaired with available evidence"
        )
        return self._result(status, event, impact, restored, workflow, message, before=plan)

    def _apply_event(
        self,
        plan: PlanVersion,
        event: PlanEvent,
        impact: ImpactScope,
        replacements: dict[str, ItineraryItem],
    ) -> tuple[PlanVersion, str | None]:
        items = {item.id: item for item in plan.items}
        direct = set(impact.direct_item_ids)
        if event.kind == EventKind.PRICE_CHANGED:
            try:
                amount = Decimal(str(event.payload["new_amount"]))
            except (KeyError, InvalidOperation):
                return plan, "price change event requires a numeric new_amount"
            currency = str(event.payload.get("currency", "CNY"))
            for item_id in direct:
                items[item_id] = items[item_id].model_copy(
                    update={"cost": Money(amount=amount, currency=currency)}
                )
        elif event.kind == EventKind.TRANSPORT_DELAYED:
            raw_delay = event.payload.get("delay_minutes")
            if not isinstance(raw_delay, (str, int)) or isinstance(raw_delay, bool):
                return plan, "transport delay event requires integer delay_minutes"
            try:
                delay = int(raw_delay)
            except ValueError:
                return plan, "transport delay event requires integer delay_minutes"
            if delay < 0:
                return plan, "delay_minutes must not be negative"
            for item_id in impact.affected_item_ids:
                item = items[item_id]
                items[item_id] = item.model_copy(
                    update={
                        "starts_at": item.starts_at + timedelta(minutes=delay),
                        "ends_at": item.ends_at + timedelta(minutes=delay),
                    }
                )
        elif event.kind in {
            EventKind.SOLD_OUT,
            EventKind.PLACE_CLOSED,
            EventKind.WEATHER_ALERT,
        }:
            for item_id in direct:
                replacement = replacements.get(item_id)
                if replacement is None:
                    if items[item_id].kind in {ItemKind.TRANSPORT, ItemKind.LODGING}:
                        return (
                            plan,
                            "transport and lodging disruptions require a sourced replacement",
                        )
                    items.pop(item_id)
                else:
                    items.pop(item_id)
                    items[replacement.id] = replacement
        elif event.kind == EventKind.USER_CHANGED_REQUIREMENT:
            pass
        else:
            return plan, f"unsupported event kind: {event.kind}"

        candidate = plan.model_copy(
            update={
                "id": f"{plan.trip_id}:plan:v{plan.version + 1}",
                "version": plan.version + 1,
                "status": PlanStatus.VERIFYING,
                "parent_version_id": plan.id,
                "applied_event_ids": (*plan.applied_event_ids, event.id),
                "items": tuple(sorted(items.values(), key=lambda item: (item.starts_at, item.id))),
            }
        )
        return candidate, None

    def _validate_price_change(
        self,
        plan: PlanVersion,
        impact: ImpactScope,
        event: PlanEvent,
    ) -> tuple[str | None, bool]:
        try:
            new_amount = Decimal(str(event.payload["new_amount"]))
        except (KeyError, InvalidOperation):
            return "price change event requires a numeric new_amount", False
        currency = str(event.payload.get("currency", "CNY")).upper()
        old_amount_raw = event.payload.get("old_amount")
        try:
            declared_old = (
                Decimal(str(old_amount_raw)) if old_amount_raw is not None else None
            )
        except InvalidOperation:
            return "price change event old_amount must be numeric when provided", False
        items = {item.id: item for item in plan.items}
        current_costs = [items[item_id].cost for item_id in impact.direct_item_ids]
        if any(cost is None for cost in current_costs):
            return "price change cannot be verified for an item without a current price", False
        concrete_costs = [cost for cost in current_costs if cost is not None]
        if any(cost.currency != currency for cost in concrete_costs):
            return "price change currency does not match the current item price", False
        if declared_old is not None and any(
            cost.amount != declared_old for cost in concrete_costs
        ):
            return "price change old_amount is stale relative to the current plan", False
        return None, all(cost.amount == new_amount for cost in concrete_costs)

    def _lock_unaffected(self, plan: PlanVersion, impact: ImpactScope) -> PlanVersion:
        unaffected = set(impact.unaffected_item_ids)
        return plan.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"locked": True}) if item.id in unaffected else item
                    for item in plan.items
                )
            }
        )

    def _restore_locks(
        self,
        plan: PlanVersion,
        original: dict[str, ItineraryItem],
    ) -> PlanVersion:
        return plan.model_copy(
            update={
                "items": tuple(
                    item.model_copy(update={"locked": original[item.id].locked})
                    if item.id in original
                    else item
                    for item in plan.items
                )
            }
        )

    def _unaffected_preserved(
        self,
        before: PlanVersion,
        after: PlanVersion,
        impact: ImpactScope,
    ) -> bool:
        before_items = {item.id: item for item in before.items}
        after_items = {item.id: item for item in after.items}
        return all(
            after_items.get(item_id) == before_items[item_id]
            for item_id in impact.unaffected_item_ids
        )

    def _result(
        self,
        status: ReplanStatus,
        event: PlanEvent,
        impact: ImpactScope,
        plan: PlanVersion,
        workflow: WorkflowResult | None,
        message: str,
        *,
        before: PlanVersion | None = None,
    ) -> LocalReplanResult:
        original = before or plan
        diff = diff_plans(original, plan)
        before_items = {item.id: item for item in original.items}
        after_items = {item.id: item for item in plan.items}
        preserved = sum(after_items.get(item_id) == item for item_id, item in before_items.items())
        unaffected = impact.unaffected_item_ids
        unaffected_preserved = sum(
            after_items.get(item_id) == before_items[item_id] for item_id in unaffected
        )
        return LocalReplanResult(
            status=status,
            event=event,
            impact=impact,
            final_plan=plan,
            diff=diff,
            workflow=workflow,
            repair_plan=self._repair_plan(status, event, impact, message),
            overall_preservation_ratio=preserved / len(before_items) if before_items else 1.0,
            unaffected_preservation_ratio=(
                unaffected_preserved / len(unaffected) if unaffected else 1.0
            ),
            message=message,
        )

    def _repair_plan(
        self,
        status: ReplanStatus,
        event: PlanEvent,
        impact: ImpactScope,
        message: str,
    ) -> StructuredRepairPlan:
        if status == ReplanStatus.NO_EFFECT:
            strategy = RepairStrategy.NO_ACTION
        elif status == ReplanStatus.READY:
            strategy = RepairStrategy.IN_PLACE_REPAIR
        elif event.kind == EventKind.USER_CHANGED_REQUIREMENT:
            strategy = RepairStrategy.GLOBAL_REPLAN
        elif event.kind in {
            EventKind.SOLD_OUT,
            EventKind.PLACE_CLOSED,
            EventKind.WEATHER_ALERT,
        }:
            strategy = RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL
        else:
            strategy = RepairStrategy.HUMAN_BLOCK
        steps: list[RepairStep] = []
        if impact.unaffected_item_ids:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.PRESERVE,
                    target_item_ids=impact.unaffected_item_ids,
                    success_invariant="未受影响项目保持逐值相等",
                )
            )
        if strategy == RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.FETCH_CANDIDATES,
                    target_item_ids=impact.direct_item_ids,
                    dependency_item_ids=impact.downstream_item_ids,
                    required_inputs=("同类可用候选", "来源证据", "新鲜度", "兼容性字段"),
                    success_invariant="新候选与直接目标兼容且不会破坏下游依赖",
                )
            )
        elif strategy in {RepairStrategy.IN_PLACE_REPAIR, RepairStrategy.GLOBAL_REPLAN}:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.MUTATE,
                    target_item_ids=impact.direct_item_ids,
                    dependency_item_ids=impact.downstream_item_ids,
                    success_invariant="变更范围不超出显式影响子图",
                )
            )
        if strategy not in {RepairStrategy.NO_ACTION, RepairStrategy.HUMAN_BLOCK}:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.REVERIFY,
                    target_item_ids=impact.affected_item_ids,
                    required_inputs=("修复后方案", "方案差异", "声明式不变量"),
                    success_invariant="硬约束与级联依赖全部复核通过",
                )
            )
        return StructuredRepairPlan(
            strategy=strategy,
            direct_item_ids=impact.direct_item_ids,
            cascade_item_ids=impact.downstream_item_ids,
            preserve_item_ids=impact.unaffected_item_ids,
            candidate_pool_expansion_required=(
                strategy == RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL
            ),
            requested_candidate_count=(
                5 if strategy == RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL else 0
            ),
            steps=tuple(steps),
            fallback_strategy=(
                RepairStrategy.GLOBAL_REPLAN
                if strategy == RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL
                else RepairStrategy.HUMAN_BLOCK
                if strategy == RepairStrategy.GLOBAL_REPLAN
                else None
            ),
            rationale=message,
        )
