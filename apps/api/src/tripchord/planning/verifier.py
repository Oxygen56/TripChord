from collections import defaultdict
from datetime import date
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.domain.itinerary import (
    ItemKind,
    ItineraryItem,
    PlanVersion,
    Violation,
    ViolationCode,
    ViolationSeverity,
)
from tripchord.domain.offers import TravelOffer
from tripchord.domain.trip import TripSpec


class VerificationMode(StrEnum):
    DRAFT = "draft"
    CONFIRMATION = "confirmation"


class TravelRequirement(DomainModel):
    from_item_id: str
    to_item_id: str
    minimum_minutes: int = Field(ge=0, le=1440)


class VerificationContext(DomainModel):
    mode: VerificationMode = VerificationMode.DRAFT
    offers: tuple[TravelOffer, ...] = ()
    travel_requirements: tuple[TravelRequirement, ...] = ()


class PlanVerifier:
    """Deterministic checks that must never be delegated to an LLM judge."""

    def verify(
        self,
        spec: TripSpec,
        plan: PlanVersion,
        context: VerificationContext | None = None,
    ) -> tuple[Violation, ...]:
        verification_context = context or VerificationContext()
        violations: list[Violation] = []
        violations.extend(self._check_trip_dates(spec, plan))
        violations.extend(self._check_daily_windows(spec, plan))
        violations.extend(self._check_overlaps(plan))
        violations.extend(self._check_budget(spec, plan))
        violations.extend(self._check_provenance(plan))
        violations.extend(self._check_must_visit(spec, plan))
        violations.extend(self._check_travel_gaps(plan, verification_context))
        violations.extend(self._check_offers(plan, verification_context))
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
            if (
                item.kind in {ItemKind.TRANSPORT, ItemKind.LODGING}
                and not item.source_refs
                and item.offer_id is None
            ):
                result.append(
                    Violation(
                        code=ViolationCode.MISSING_PROVENANCE,
                        severity=ViolationSeverity.ERROR,
                        message=f"{item.title} has no traceable source",
                        item_ids=(item.id,),
                    )
                )
        return result

    def _check_must_visit(self, spec: TripSpec, plan: PlanVersion) -> list[Violation]:
        searchable = " ".join(f"{item.title} {item.location_name or ''}" for item in plan.items)
        return [
            Violation(
                code=ViolationCode.MUST_VISIT_MISSING,
                severity=ViolationSeverity.ERROR,
                message=f"must-visit item is missing: {term}",
                details={"term": term},
            )
            for term in spec.must_visit
            if term not in searchable
        ]

    def _check_travel_gaps(
        self,
        plan: PlanVersion,
        context: VerificationContext,
    ) -> list[Violation]:
        items = {item.id: item for item in plan.items}
        result: list[Violation] = []
        for requirement in context.travel_requirements:
            previous = items.get(requirement.from_item_id)
            current = items.get(requirement.to_item_id)
            if previous is None or current is None:
                continue
            actual = int((current.starts_at - previous.ends_at).total_seconds() / 60)
            if actual < requirement.minimum_minutes:
                result.append(
                    Violation(
                        code=ViolationCode.TRAVEL_GAP,
                        severity=ViolationSeverity.ERROR,
                        message=(
                            f"{previous.title} to {current.title} needs "
                            f"{requirement.minimum_minutes} travel minutes"
                        ),
                        item_ids=(previous.id, current.id),
                        details={
                            "required_minutes": requirement.minimum_minutes,
                            "actual_minutes": actual,
                        },
                    )
                )
        return result

    def _check_offers(
        self,
        plan: PlanVersion,
        context: VerificationContext,
    ) -> list[Violation]:
        offers = {offer.id: offer for offer in context.offers}
        severity = (
            ViolationSeverity.ERROR
            if context.mode == VerificationMode.CONFIRMATION
            else ViolationSeverity.WARNING
        )
        result: list[Violation] = []
        for item in plan.items:
            if item.offer_id is None:
                continue
            offer = offers.get(item.offer_id)
            if offer is None:
                result.append(
                    Violation(
                        code=ViolationCode.MISSING_PROVENANCE,
                        severity=ViolationSeverity.ERROR,
                        message=f"{item.title} references an unavailable offer",
                        item_ids=(item.id,),
                    )
                )
                continue
            if offer.requires_revalidation or not offer.source.is_fresh():
                result.append(
                    Violation(
                        code=ViolationCode.STALE_OR_UNVERIFIED_OFFER,
                        severity=severity,
                        message=f"{item.title} must be repriced before confirmation",
                        item_ids=(item.id,),
                        details={"offer_id": offer.id},
                    )
                )
        return result
