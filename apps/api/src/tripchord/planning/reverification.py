from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from tripchord.domain.common import DomainModel
from tripchord.domain.itinerary import ItemKind, ItineraryItem, PlanVersion
from tripchord.domain.trip import TripSpec
from tripchord.planning.repair import PlanDiff
from tripchord.planning.verifier import VerificationContext


class PlanInvariantCode(StrEnum):
    UNIQUE_ITEM_IDS = "unique_item_ids"
    VERSION_LINEAGE = "version_lineage"
    DECLARED_DIFF_MATCHES = "declared_diff_matches"
    UNAFFECTED_ITEMS_PRESERVED = "unaffected_items_preserved"
    POSITIVE_INTERVALS = "positive_intervals"
    NO_TEMPORAL_OVERLAP = "no_temporal_overlap"
    BUDGET_ARITHMETIC = "budget_arithmetic"
    REQUIRED_PROVENANCE = "required_provenance"
    TRAVEL_GAPS = "travel_gaps"
    PROTECTED_COMPONENTS_PRESERVED = "protected_components_preserved"


class PlanInvariantCheck(DomainModel):
    code: PlanInvariantCode
    passed: bool
    message: str
    item_ids: tuple[str, ...] = ()


class PlanReverificationReport(DomainModel):
    engine: str = "declarative-plan-invariants-v1"
    before_plan_id: str
    after_plan_id: str
    checks: tuple[PlanInvariantCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_codes(self) -> tuple[PlanInvariantCode, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


class DeclarativePlanReVerifier:
    """Independent invariants; deliberately does not call PlanVerifier."""

    def verify(
        self,
        spec: TripSpec,
        before: PlanVersion,
        after: PlanVersion,
        diff: PlanDiff,
        context: VerificationContext | None = None,
    ) -> PlanReverificationReport:
        verification_context = context or VerificationContext()
        checks = (
            self._unique_ids(after),
            self._lineage(before, after, diff),
            self._declared_diff(before, after, diff),
            self._unaffected_preserved(before, after, diff),
            self._positive_intervals(after),
            self._no_overlaps(after),
            self._budget(spec, after),
            self._provenance(after),
            self._travel_gaps(after, verification_context),
            self._protected_components(before, after, diff, verification_context),
        )
        return PlanReverificationReport(
            before_plan_id=before.id,
            after_plan_id=after.id,
            checks=checks,
        )

    def _unique_ids(self, plan: PlanVersion) -> PlanInvariantCheck:
        ids = tuple(item.id for item in plan.items)
        duplicates = tuple(sorted({item_id for item_id in ids if ids.count(item_id) > 1}))
        return self._check(
            PlanInvariantCode.UNIQUE_ITEM_IDS,
            not duplicates,
            "方案项目 ID 必须唯一",
            duplicates,
        )

    def _lineage(
        self,
        before: PlanVersion,
        after: PlanVersion,
        diff: PlanDiff,
    ) -> PlanInvariantCheck:
        expected_change = diff.changed
        passed = (
            before.trip_id == after.trip_id
            and (
                (
                    after.version == before.version + 1
                    and after.parent_version_id == before.id
                )
                if expected_change
                else after == before
            )
        )
        return self._check(
            PlanInvariantCode.VERSION_LINEAGE,
            passed,
            "发生实质变化时版本必须单调递增并引用直接父版本",
        )

    def _declared_diff(
        self,
        before: PlanVersion,
        after: PlanVersion,
        diff: PlanDiff,
    ) -> PlanInvariantCheck:
        before_items = {item.id: item for item in before.items}
        after_items = {item.id: item for item in after.items}
        actual_added = tuple(sorted(set(after_items) - set(before_items)))
        actual_removed = tuple(sorted(set(before_items) - set(after_items)))
        actual_changed = tuple(
            sorted(
                item_id
                for item_id in set(before_items) & set(after_items)
                if before_items[item_id] != after_items[item_id]
            )
        )
        declared_changed = tuple(sorted(item.item_id for item in diff.changed_items))
        passed = (
            actual_added == diff.added_item_ids
            and actual_removed == diff.removed_item_ids
            and actual_changed == declared_changed
        )
        affected = tuple(sorted(set(actual_added) | set(actual_removed) | set(actual_changed)))
        return self._check(
            PlanInvariantCode.DECLARED_DIFF_MATCHES,
            passed,
            "声明的 plan diff 必须与独立重算结果一致",
            affected,
        )

    def _unaffected_preserved(
        self,
        before: PlanVersion,
        after: PlanVersion,
        diff: PlanDiff,
    ) -> PlanInvariantCheck:
        affected = {
            *diff.added_item_ids,
            *diff.removed_item_ids,
            *(item.item_id for item in diff.changed_items),
        }
        after_items = {item.id: item for item in after.items}
        changed_unexpectedly = tuple(
            sorted(
                item.id
                for item in before.items
                if item.id not in affected and after_items.get(item.id) != item
            )
        )
        return self._check(
            PlanInvariantCode.UNAFFECTED_ITEMS_PRESERVED,
            not changed_unexpectedly,
            "diff 外项目必须逐值保持不变",
            changed_unexpectedly,
        )

    def _positive_intervals(self, plan: PlanVersion) -> PlanInvariantCheck:
        invalid = tuple(
            sorted(item.id for item in plan.items if item.ends_at <= item.starts_at)
        )
        return self._check(
            PlanInvariantCode.POSITIVE_INTERVALS,
            not invalid,
            "每个项目必须具有正时长",
            invalid,
        )

    def _no_overlaps(self, plan: PlanVersion) -> PlanInvariantCheck:
        ordered = sorted(
            (item for item in plan.items if item.kind != ItemKind.LODGING),
            key=lambda item: (item.starts_at, item.id),
        )
        overlaps: set[str] = set()
        active: list[ItineraryItem] = []
        for current in ordered:
            active = [item for item in active if item.ends_at > current.starts_at]
            for previous in active:
                overlaps.update((previous.id, current.id))
            active.append(current)
        return self._check(
            PlanInvariantCode.NO_TEMPORAL_OVERLAP,
            not overlaps,
            "非住宿项目不得发生时间重叠",
            tuple(sorted(overlaps)),
        )

    def _budget(self, spec: TripSpec, plan: PlanVersion) -> PlanInvariantCheck:
        if spec.budget is None:
            return self._check(
                PlanInvariantCode.BUDGET_ARITHMETIC,
                True,
                "用户未设置预算，不执行预算上限不变量",
            )
        mismatched = tuple(
            sorted(
                item.id
                for item in plan.items
                if item.cost is not None and item.cost.currency != spec.budget.currency
            )
        )
        total = sum(
            (
                item.cost.amount
                for item in plan.items
                if item.cost is not None and item.cost.currency == spec.budget.currency
            ),
            start=Decimal(0),
        )
        return self._check(
            PlanInvariantCode.BUDGET_ARITHMETIC,
            not mismatched and total <= spec.budget.amount,
            "同币种已知成本独立求和后不得超过预算",
            mismatched,
        )

    def _provenance(self, plan: PlanVersion) -> PlanInvariantCheck:
        missing = tuple(
            sorted(
                item.id
                for item in plan.items
                if item.kind in {ItemKind.TRANSPORT, ItemKind.LODGING}
                and not item.source_refs
                and item.offer_id is None
            )
        )
        return self._check(
            PlanInvariantCode.REQUIRED_PROVENANCE,
            not missing,
            "交通和住宿必须保留来源或报价引用",
            missing,
        )

    def _travel_gaps(
        self,
        plan: PlanVersion,
        context: VerificationContext,
    ) -> PlanInvariantCheck:
        items = {item.id: item for item in plan.items}
        invalid: set[str] = set()
        for requirement in context.travel_requirements:
            previous = items.get(requirement.from_item_id)
            current = items.get(requirement.to_item_id)
            if previous is None or current is None:
                invalid.update(
                    item_id
                    for item_id in (requirement.from_item_id, requirement.to_item_id)
                    if item_id not in items
                )
                continue
            actual = int((current.starts_at - previous.ends_at).total_seconds() // 60)
            if actual < requirement.minimum_minutes:
                invalid.update((previous.id, current.id))
        return self._check(
            PlanInvariantCode.TRAVEL_GAPS,
            not invalid,
            "显式交通间隔必须在独立重算后仍满足",
            tuple(sorted(invalid)),
        )

    def _protected_components(
        self,
        before: PlanVersion,
        after: PlanVersion,
        diff: PlanDiff,
        context: VerificationContext,
    ) -> PlanInvariantCheck:
        """v0.6 invariant: no protected component may be removed or changed
        without an explicitly applied override."""
        ledger = context.booking_ledger
        if ledger is None:
            return self._check(
                PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED,
                True,
                "没有已预订约束，保护不变量通过",
            )
        protected_ids = {fact.component_id for fact in ledger.facts}
        if not protected_ids:
            return self._check(
                PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED,
                True,
                "无已预订组件，保护不变量通过",
            )
        changed_ids = {item.item_id for item in diff.changed_items}
        touched = set(diff.removed_item_ids) | changed_ids
        after_ids = {item.id for item in after.items}
        # An applied override explicitly un-protects a component for this round.
        applied_override_ids = {
            override.component_id
            for override in ledger.overrides
            if override.state.value == "applied"
        }
        violations: set[str] = set()
        for component_id in sorted(protected_ids):
            if component_id in applied_override_ids:
                continue
            if component_id in touched or component_id not in after_ids:
                violations.add(component_id)
        if violations:
            return self._check(
                PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED,
                False,
                "已预订组件被删除或修改而未应用解除保护: "
                + ", ".join(sorted(violations)),
                tuple(sorted(violations)),
            )
        return self._check(
            PlanInvariantCode.PROTECTED_COMPONENTS_PRESERVED,
            True,
            "所有已预订组件均被保留且未静默修改",
            tuple(sorted(protected_ids)),
        )

    def _check(
        self,
        code: PlanInvariantCode,
        passed: bool,
        message: str,
        item_ids: tuple[str, ...] = (),
    ) -> PlanInvariantCheck:
        return PlanInvariantCheck(
            code=code,
            passed=passed,
            message=message,
            item_ids=item_ids,
        )


__all__ = [
    "DeclarativePlanReVerifier",
    "PlanInvariantCheck",
    "PlanInvariantCode",
    "PlanReverificationReport",
]
