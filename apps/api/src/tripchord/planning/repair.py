from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

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


class RepairOutcome(DomainModel):
    plan: PlanVersion
    diff: PlanDiff
    actions: tuple[RepairAction, ...]
    unresolved: tuple[Violation, ...]


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
