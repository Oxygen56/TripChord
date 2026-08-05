from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.domain.itinerary import (
    ItineraryItem,
    PlanStatus,
    PlanVersion,
    Violation,
    ViolationCode,
)
from tripchord.domain.trip import TripSpec


class RepairDisposition(StrEnum):
    APPLIED = "applied"
    UNRESOLVED = "unresolved"


class RepairStrategy(StrEnum):
    NO_ACTION = "no_action"
    IN_PLACE_REPAIR = "in_place_repair"
    EXPAND_LOCAL_CANDIDATE_POOL = "expand_local_candidate_pool"
    GLOBAL_REPLAN = "global_replan"
    HUMAN_BLOCK = "human_block"


class RepairStepKind(StrEnum):
    PRESERVE = "preserve"
    MUTATE = "mutate"
    FETCH_CANDIDATES = "fetch_candidates"
    REVERIFY = "reverify"
    ESCALATE = "escalate"


class ItemChange(DomainModel):
    item_id: str
    changed_fields: tuple[str, ...]


class PlanDiff(DomainModel):
    added_item_ids: tuple[str, ...] = ()
    removed_item_ids: tuple[str, ...] = ()
    changed_items: tuple[ItemChange, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added_item_ids or self.removed_item_ids or self.changed_items)


class RepairAction(DomainModel):
    violation_code: ViolationCode
    disposition: RepairDisposition
    message: str
    item_ids: tuple[str, ...] = ()


class RepairStep(DomainModel):
    order: int = Field(ge=1)
    kind: RepairStepKind
    target_item_ids: tuple[str, ...] = ()
    dependency_item_ids: tuple[str, ...] = ()
    required_inputs: tuple[str, ...] = ()
    success_invariant: str


class StructuredRepairPlan(DomainModel):
    strategy: RepairStrategy
    trigger_violation_codes: tuple[ViolationCode, ...] = ()
    direct_item_ids: tuple[str, ...] = ()
    cascade_item_ids: tuple[str, ...] = ()
    preserve_item_ids: tuple[str, ...] = ()
    candidate_pool_expansion_required: bool = False
    requested_candidate_count: int = Field(default=0, ge=0)
    steps: tuple[RepairStep, ...] = ()
    fallback_strategy: RepairStrategy | None = None
    rationale: str


class RepairOutcome(DomainModel):
    plan: PlanVersion
    diff: PlanDiff
    actions: tuple[RepairAction, ...]
    unresolved: tuple[Violation, ...]
    repair_plan: StructuredRepairPlan


def diff_plans(before: PlanVersion, after: PlanVersion) -> PlanDiff:
    before_items = {item.id: item for item in before.items}
    after_items = {item.id: item for item in after.items}
    changed: list[ItemChange] = []
    for item_id in before_items.keys() & after_items.keys():
        before_dump = before_items[item_id].model_dump()
        after_dump = after_items[item_id].model_dump()
        fields = tuple(
            sorted(field for field in before_dump if before_dump[field] != after_dump[field])
        )
        if fields:
            changed.append(ItemChange(item_id=item_id, changed_fields=fields))
    return PlanDiff(
        added_item_ids=tuple(sorted(after_items.keys() - before_items.keys())),
        removed_item_ids=tuple(sorted(before_items.keys() - after_items.keys())),
        changed_items=tuple(sorted(changed, key=lambda item: item.item_id)),
    )


class RepairEngine:
    def repair(
        self,
        spec: TripSpec,
        plan: PlanVersion,
        violations: tuple[Violation, ...],
    ) -> RepairOutcome:
        items = {item.id: item for item in plan.items}
        actions: list[RepairAction] = []
        unresolved: list[Violation] = []
        budget_handled = False

        for violation in violations:
            if violation.code in {ViolationCode.OVERLAP, ViolationCode.TRAVEL_GAP}:
                action = self._repair_gap(items, violation)
            elif violation.code == ViolationCode.DAILY_WINDOW:
                action = self._repair_daily_window(spec, items, violation)
            elif violation.code == ViolationCode.BUDGET_EXCEEDED and not budget_handled:
                action = self._repair_budget(spec, items, violation)
                budget_handled = True
            elif violation.code == ViolationCode.DATE_OUT_OF_RANGE:
                action = self._remove_unlocked(items, violation, "removed out-of-range item")
            else:
                action = None

            if action is None:
                unresolved.append(violation)
                actions.append(
                    RepairAction(
                        violation_code=violation.code,
                        disposition=RepairDisposition.UNRESOLVED,
                        message="requires new sourced candidates or external revalidation",
                        item_ids=violation.item_ids,
                    )
                )
            else:
                actions.append(action)

        candidate = plan.model_copy(
            update={
                "id": f"{plan.trip_id}:plan:v{plan.version + 1}",
                "version": plan.version + 1,
                "status": PlanStatus.VERIFYING,
                "items": tuple(sorted(items.values(), key=lambda item: (item.starts_at, item.id))),
                "parent_version_id": plan.id,
            }
        )
        diff = diff_plans(plan, candidate)
        if not diff.changed:
            candidate = plan
        return RepairOutcome(
            plan=candidate,
            diff=diff,
            actions=tuple(actions),
            unresolved=tuple(unresolved),
            repair_plan=self._structured_plan(
                plan,
                violations,
                tuple(actions),
                tuple(unresolved),
            ),
        )

    def _structured_plan(
        self,
        plan: PlanVersion,
        violations: tuple[Violation, ...],
        actions: tuple[RepairAction, ...],
        unresolved: tuple[Violation, ...],
    ) -> StructuredRepairPlan:
        direct = tuple(
            sorted({item_id for violation in violations for item_id in violation.item_ids})
        )
        preserve = tuple(sorted({item.id for item in plan.items} - set(direct)))
        requires_candidates = bool(unresolved)
        global_codes = {
            ViolationCode.MUST_VISIT_MISSING,
            ViolationCode.CURRENCY_MISMATCH,
        }
        if any(item.code in global_codes for item in unresolved):
            strategy = RepairStrategy.GLOBAL_REPLAN
        elif requires_candidates:
            strategy = RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL
        elif any(action.disposition == RepairDisposition.APPLIED for action in actions):
            strategy = RepairStrategy.IN_PLACE_REPAIR
        else:
            strategy = RepairStrategy.NO_ACTION
        steps: list[RepairStep] = []
        if preserve:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.PRESERVE,
                    target_item_ids=preserve,
                    success_invariant="未受影响项目的全部字段保持逐值相等",
                )
            )
        if requires_candidates:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.FETCH_CANDIDATES,
                    target_item_ids=direct,
                    required_inputs=("带来源的新候选", "新鲜度", "可用性", "价格与条款"),
                    success_invariant="候选来自可追溯来源且能直接响应 Verifier 拒绝原因",
                )
            )
        elif direct:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.MUTATE,
                    target_item_ids=direct,
                    success_invariant="只修改违规项及其显式依赖项",
                )
            )
        if strategy != RepairStrategy.NO_ACTION:
            steps.append(
                RepairStep(
                    order=len(steps) + 1,
                    kind=RepairStepKind.REVERIFY,
                    target_item_ids=direct,
                    required_inputs=("修复后方案", "原方案 diff", "声明式不变量"),
                    success_invariant="确定性 Verifier 与异构不变量复核均通过",
                )
            )
        return StructuredRepairPlan(
            strategy=strategy,
            trigger_violation_codes=tuple(violation.code for violation in violations),
            direct_item_ids=direct,
            preserve_item_ids=preserve,
            candidate_pool_expansion_required=requires_candidates,
            requested_candidate_count=5 if requires_candidates else 0,
            steps=tuple(steps),
            fallback_strategy=(
                RepairStrategy.HUMAN_BLOCK
                if strategy
                in {
                    RepairStrategy.EXPAND_LOCAL_CANDIDATE_POOL,
                    RepairStrategy.GLOBAL_REPLAN,
                }
                else None
            ),
            rationale=(
                "现有候选无法修复硬错误，输出扩大候选池及失败升级信号"
                if requires_candidates
                else "现有证据足以执行有界局部修复"
            ),
        )

    def _repair_gap(
        self,
        items: dict[str, ItineraryItem],
        violation: Violation,
    ) -> RepairAction | None:
        if len(violation.item_ids) != 2:
            return None
        previous = items.get(violation.item_ids[0])
        current = items.get(violation.item_ids[1])
        if previous is None or current is None or current.locked:
            return None
        required = int(violation.details.get("required_minutes", 0))
        new_start = previous.ends_at + timedelta(minutes=required)
        duration = current.ends_at - current.starts_at
        items[current.id] = current.model_copy(
            update={"starts_at": new_start, "ends_at": new_start + duration}
        )
        return RepairAction(
            violation_code=violation.code,
            disposition=RepairDisposition.APPLIED,
            message=f"shifted {current.title} after the preceding item",
            item_ids=(current.id,),
        )

    def _repair_daily_window(
        self,
        spec: TripSpec,
        items: dict[str, ItineraryItem],
        violation: Violation,
    ) -> RepairAction | None:
        if not violation.item_ids:
            return None
        item = items.get(violation.item_ids[0])
        if item is None or item.locked:
            return None
        duration = item.ends_at - item.starts_at
        zone = item.starts_at.tzinfo
        day_start = datetime.combine(item.starts_at.date(), spec.daily_window.start, tzinfo=zone)
        day_end = datetime.combine(item.starts_at.date(), spec.daily_window.end, tzinfo=zone)
        if duration > day_end - day_start:
            return None
        new_start = max(item.starts_at, day_start)
        if new_start + duration > day_end:
            new_start = day_end - duration
        items[item.id] = item.model_copy(
            update={"starts_at": new_start, "ends_at": new_start + duration}
        )
        return RepairAction(
            violation_code=violation.code,
            disposition=RepairDisposition.APPLIED,
            message=f"moved {item.title} inside the daily window",
            item_ids=(item.id,),
        )

    def _repair_budget(
        self,
        spec: TripSpec,
        items: dict[str, ItineraryItem],
        violation: Violation,
    ) -> RepairAction | None:
        if spec.budget is None:
            return None
        total = sum(
            (
                item.cost.amount
                for item in items.values()
                if item.cost is not None and item.cost.currency == spec.budget.currency
            ),
            start=Decimal("0"),
        )
        removable = sorted(
            (
                item
                for item in items.values()
                if not item.locked
                and item.cost is not None
                and item.cost.currency == spec.budget.currency
            ),
            key=lambda item: (
                item.utility,
                -(item.cost.amount if item.cost is not None else Decimal("0")),
                item.id,
            ),
        )
        removed: list[str] = []
        for item in removable:
            if total <= spec.budget.amount:
                break
            cost = item.cost
            if cost is None:
                continue
            total -= cost.amount
            removed.append(item.id)
            items.pop(item.id, None)
        if total > spec.budget.amount or not removed:
            return None
        return RepairAction(
            violation_code=violation.code,
            disposition=RepairDisposition.APPLIED,
            message="removed the lowest-utility optional items until budget passed",
            item_ids=tuple(removed),
        )

    def _remove_unlocked(
        self,
        items: dict[str, ItineraryItem],
        violation: Violation,
        message: str,
    ) -> RepairAction | None:
        removed: list[str] = []
        for item_id in violation.item_ids:
            item = items.get(item_id)
            if item is None or item.locked:
                continue
            items.pop(item_id)
            removed.append(item_id)
        if not removed:
            return None
        return RepairAction(
            violation_code=violation.code,
            disposition=RepairDisposition.APPLIED,
            message=message,
            item_ids=tuple(removed),
        )
