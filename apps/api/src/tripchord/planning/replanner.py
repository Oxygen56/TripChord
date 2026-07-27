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
from tripchord.planning.repair import PlanDiff, diff_plans
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
            overall_preservation_ratio=preserved / len(before_items) if before_items else 1.0,
            unaffected_preservation_ratio=(
                unaffected_preserved / len(unaffected) if unaffected else 1.0
            ),
            message=message,
        )
