from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import pairwise

from tripchord.domain.itinerary import (
    ItemKind,
    ItineraryItem,
    PlanVersion,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from tripchord.domain.trip import TripSpec


class PlanVerifier:
    """Deterministic checks that must never be delegated to an LLM judge."""

    def verify(self, spec: TripSpec, plan: PlanVersion) -> tuple[Violation, ...]:
        violations: list[Violation] = []
        violations.extend(self._check_trip_dates(spec, plan))
        violations.extend(self._check_daily_windows(spec, plan))
        violations.extend(self._check_overlaps(plan))
        violations.extend(self._check_budget(spec, plan))
        violations.extend(self._check_provenance(plan))
        return tuple(violations)

    def _check_trip_dates(self, spec: TripSpec, plan: PlanVersion) -> list[Violation]:
        result: list[Violation] = []
        for item in plan.items:
            if item.starts_at.date() < spec.start_date or item.ends_at.date() > spec.end_date:
                result.append(
                    Violation(
                        code=ViolationCode.DATE_OUT_OF_RANGE,
                        severity=ViolationSeverity.ERROR,
                        message=f"{item.title} falls outside the trip dates",
                        item_ids=(item.id,),
                    )
                )
        return result

    def _check_daily_windows(self, spec: TripSpec, plan: PlanVersion) -> list[Violation]:
        result: list[Violation] = []
        for item in plan.items:
            if item.kind in {ItemKind.LODGING, ItemKind.TRANSPORT}:
                continue
            if (
                item.starts_at.timetz().replace(tzinfo=None) < spec.daily_window.start
                or item.ends_at.timetz().replace(tzinfo=None) > spec.daily_window.end
            ):
                result.append(
                    Violation(
                        code=ViolationCode.DAILY_WINDOW,
                        severity=ViolationSeverity.ERROR,
                        message=f"{item.title} is outside the preferred daily window",
                        item_ids=(item.id,),
                    )
                )
        return result

    def _check_overlaps(self, plan: PlanVersion) -> list[Violation]:
        by_date: dict[date, list[ItineraryItem]] = defaultdict(list)
        for item in plan.items:
            if item.kind != ItemKind.LODGING:
                by_date[item.starts_at.date()].append(item)

        result: list[Violation] = []
        for items in by_date.values():
            ordered = sorted(items, key=lambda candidate: candidate.starts_at)
            for previous, current in pairwise(ordered):
                if current.starts_at < previous.ends_at:
                    result.append(
                        Violation(
                            code=ViolationCode.OVERLAP,
                            severity=ViolationSeverity.ERROR,
                            message=f"{previous.title} overlaps {current.title}",
                            item_ids=(previous.id, current.id),
                        )
                    )
        return result

    def _check_budget(self, spec: TripSpec, plan: PlanVersion) -> list[Violation]:
        if spec.budget is None:
            return []
        amounts: list[Decimal] = []
        for item in plan.items:
            if item.cost is None:
                continue
            if item.cost.currency != spec.budget.currency:
                return [
                    Violation(
                        code=ViolationCode.CURRENCY_MISMATCH,
                        severity=ViolationSeverity.ERROR,
                        message="plan costs use a currency different from the trip budget",
                        item_ids=(item.id,),
                    )
                ]
            amounts.append(item.cost.amount)
        total = sum(amounts, start=Decimal("0"))
        if total <= spec.budget.amount:
            return []
        return [
            Violation(
                code=ViolationCode.BUDGET_EXCEEDED,
                severity=ViolationSeverity.ERROR,
                message="known plan costs exceed the trip budget",
                details={"total": float(total), "budget": float(spec.budget.amount)},
            )
        ]

    def _check_provenance(self, plan: PlanVersion) -> list[Violation]:
        result: list[Violation] = []
        for item in plan.items:
            if item.kind in {ItemKind.TRANSPORT, ItemKind.LODGING} and not item.source_refs:
                result.append(
                    Violation(
                        code=ViolationCode.MISSING_PROVENANCE,
                        severity=ViolationSeverity.ERROR,
                        message=f"{item.title} has no traceable source",
                        item_ids=(item.id,),
                    )
                )
        return result
