from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    PackageArea,
    PackageIntent,
    PackageInventory,
    PackagePlaceKey,
    PackageVerificationHandoff,
    PackageVerificationPhase,
    PackageViolationCode,
    QuoteAvailability,
    TransferPriceGuarantee,
    TravelPackageCandidate,
)


class StayPlanSource(StrEnum):
    SYSTEM_FROZEN_LIVE_V4 = "system_frozen_live_v4"


class StayPlanId(StrEnum):
    MAAFUSHI_ICOM = "maafushi_icom"
    MAAFUSHI_SPLIT_HULHUMALE = "maafushi_split_hulhumale"
    HULHUMALE_CONTINUOUS = "hulhumale_continuous"


class StayDateAnchor(StrEnum):
    TRIP_START = "trip_start"
    TRIP_END = "trip_end"


class StayInventoryResultState(StrEnum):
    QUOTE_FOUND = "quote_found"
    CONFIRMED_EMPTY = "confirmed_empty"
    BOUNDED_NO_EXACT_QUOTE = "bounded_no_exact_quote"
    BOUNDED_PROVIDER_PENDING = "bounded_provider_pending"


class StayDateBoundary(DomainModel):
    anchor: StayDateAnchor
    offset_days: int = Field(default=0, ge=-30, le=30)

    def resolve(self, intent: PackageIntent) -> date:
        base = (
            intent.start_date
            if self.anchor == StayDateAnchor.TRIP_START
            else intent.end_date
        )
        return base + timedelta(days=self.offset_days)


class StaySegmentSpec(DomainModel):
    segment_id: str = Field(min_length=1)
    query_segment: str = Field(min_length=1)
    exact_place_key: PackagePlaceKey
    area: PackageArea
    check_in: StayDateBoundary
    check_out: StayDateBoundary

    @model_validator(mode="after")
    def validate_place_area(self) -> Self:
        expected = {
            PackagePlaceKey.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
            PackagePlaceKey.HULHUMALE: PackageArea.AIRPORT_ISLAND,
        }
        if self.exact_place_key not in expected:
            raise ValueError("stay segments may only target an exact lodging place")
        if expected[self.exact_place_key] != self.area:
            raise ValueError("stay segment place and package area do not match")
        return self


class RequiredTransferContract(DomainModel):
    contract_id: str = Field(min_length=1)
    origin_place_key: PackagePlaceKey
    destination_place_key: PackagePlaceKey
    origin_area: PackageArea
    destination_area: PackageArea
    service_date: StayDateBoundary
    required_provider: str | None = None
    allowed_price_guarantees: tuple[TransferPriceGuarantee, ...] = Field(min_length=1)
    requires_tax_inclusive_total: bool

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        expected = {
            PackagePlaceKey.VELANA_AIRPORT: PackageArea.AIRPORT,
            PackagePlaceKey.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
            PackagePlaceKey.HULHUMALE: PackageArea.AIRPORT_ISLAND,
        }
        if self.origin_place_key == self.destination_place_key:
            raise ValueError("transfer contract must move between two exact places")
        if expected[self.origin_place_key] != self.origin_area:
            raise ValueError("transfer contract origin place and area do not match")
        if expected[self.destination_place_key] != self.destination_area:
            raise ValueError("transfer contract destination place and area do not match")
        if self.requires_tax_inclusive_total and (
            self.allowed_price_guarantees != (TransferPriceGuarantee.ALL_IN_CONFIRMED,)
        ):
            raise ValueError("tax-inclusive transfer contracts require all-in confirmed pricing")
        return self


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class StayPlanCandidate(DomainModel):
    stay_plan_id: StayPlanId
    label_zh: str = Field(min_length=1)
    segments: tuple[StaySegmentSpec, ...] = Field(min_length=1)
    required_transfer_contracts: tuple[RequiredTransferContract, ...] = Field(min_length=1)
    scan_limit_per_platform: int = Field(ge=1, le=100)
    source: StayPlanSource
    source_ref: str = Field(min_length=1)
    candidate_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        segment_ids = tuple(item.segment_id for item in self.segments)
        query_segments = tuple(item.query_segment for item in self.segments)
        contract_ids = tuple(item.contract_id for item in self.required_transfer_contracts)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("stay plan segment ids must be unique")
        if len(query_segments) != len(set(query_segments)):
            raise ValueError("stay plan query segments must be unique")
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("stay plan transfer contract ids must be unique")
        if self.candidate_sha256 != self.computed_sha256():
            raise ValueError("stay plan candidate SHA does not match its canonical payload")
        return self

    def computed_sha256(self) -> str:
        return _sha256(
            self.model_dump(
                mode="json",
                exclude={"candidate_sha256"},
            )
        )


class StayPlanCandidateSet(DomainModel):
    schema_version: str = "tripchord-stay-plan-candidates-v4"
    gateway_destination: str = Field(min_length=1)
    frozen_at: datetime
    candidates: tuple[StayPlanCandidate, ...] = Field(min_length=2)
    source: StayPlanSource
    source_ref: str = Field(min_length=1)
    candidate_set_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("frozen_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("stay plan candidate set frozen_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_candidate_set(self) -> Self:
        ids = tuple(item.stay_plan_id for item in self.candidates)
        if len(ids) != len(set(ids)):
            raise ValueError("stay plan ids must be unique")
        required = {
            StayPlanId.MAAFUSHI_ICOM,
            StayPlanId.HULHUMALE_CONTINUOUS,
        }
        if not required <= set(ids):
            raise ValueError("live-v4 requires Maafushi+iCom and continuous Hulhumalé candidates")
        if any(item.source != self.source for item in self.candidates):
            raise ValueError("candidate and candidate-set sources must match")
        if self.candidate_set_sha256 != self.computed_sha256():
            raise ValueError("stay plan candidate-set SHA does not match its canonical payload")
        return self

    @property
    def stay_plan_ids(self) -> tuple[StayPlanId, ...]:
        return tuple(item.stay_plan_id for item in self.candidates)

    def candidate(self, stay_plan_id: StayPlanId | str) -> StayPlanCandidate:
        normalized = StayPlanId(stay_plan_id)
        return next(item for item in self.candidates if item.stay_plan_id == normalized)

    def computed_sha256(self) -> str:
        return _sha256(
            self.model_dump(
                mode="json",
                exclude={"candidate_set_sha256"},
            )
        )


def _candidate(payload: dict[str, object]) -> StayPlanCandidate:
    candidate_sha256 = _sha256(payload)
    return StayPlanCandidate.model_validate(
        {
            **payload,
            "candidate_sha256": candidate_sha256,
        }
    )


def _boundary(anchor: StayDateAnchor, offset_days: int = 0) -> dict[str, object]:
    return {"anchor": anchor.value, "offset_days": offset_days}


def _segment(
    segment_id: str,
    query_segment: str,
    place: PackagePlaceKey,
    area: PackageArea,
    check_in: dict[str, object],
    check_out: dict[str, object],
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "query_segment": query_segment,
        "exact_place_key": place.value,
        "area": area.value,
        "check_in": check_in,
        "check_out": check_out,
    }


def _transfer(
    contract_id: str,
    origin: PackagePlaceKey,
    destination: PackagePlaceKey,
    origin_area: PackageArea,
    destination_area: PackageArea,
    service_date: dict[str, object],
    *,
    provider: str | None,
    guarantee: TransferPriceGuarantee,
    tax_inclusive: bool,
) -> dict[str, object]:
    return {
        "contract_id": contract_id,
        "origin_place_key": origin.value,
        "destination_place_key": destination.value,
        "origin_area": origin_area.value,
        "destination_area": destination_area.value,
        "service_date": service_date,
        "required_provider": provider,
        "allowed_price_guarantees": [guarantee.value],
        "requires_tax_inclusive_total": tax_inclusive,
    }


def system_stay_plan_candidate_set(
    gateway_destination: str = "马累",
    *,
    frozen_at: datetime | None = None,
) -> StayPlanCandidateSet:
    """Build the immutable live-v4 candidate set before any provider result is read."""

    source = StayPlanSource.SYSTEM_FROZEN_LIVE_V4
    source_ref = "tripchord:maldives-free-travel:live-v4"
    start = _boundary(StayDateAnchor.TRIP_START)
    start_plus_one = _boundary(StayDateAnchor.TRIP_START, 1)
    end_minus_one = _boundary(StayDateAnchor.TRIP_END, -1)
    end = _boundary(StayDateAnchor.TRIP_END)
    published = TransferPriceGuarantee.PUBLISHED_BASE_FARE
    all_in = TransferPriceGuarantee.ALL_IN_CONFIRMED
    common = {
        "scan_limit_per_platform": 12,
        "source": source.value,
        "source_ref": source_ref,
    }
    maafushi = _candidate(
        {
            "stay_plan_id": StayPlanId.MAAFUSHI_ICOM.value,
            "label_zh": "Maafushi 连住 + iCom 官方公开快船",
            "segments": [
                _segment(
                    "maafushi-full",
                    "full",
                    PackagePlaceKey.MAAFUSHI,
                    PackageArea.DESTINATION_ISLAND,
                    start,
                    end,
                )
            ],
            "required_transfer_contracts": [
                _transfer(
                    "icom-continuous-outbound",
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackagePlaceKey.MAAFUSHI,
                    PackageArea.AIRPORT,
                    PackageArea.DESTINATION_ISLAND,
                    start,
                    provider="icom-public-transfer",
                    guarantee=published,
                    tax_inclusive=False,
                ),
                _transfer(
                    "icom-continuous-inbound",
                    PackagePlaceKey.MAAFUSHI,
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackageArea.DESTINATION_ISLAND,
                    PackageArea.AIRPORT,
                    end,
                    provider="icom-public-transfer",
                    guarantee=published,
                    tax_inclusive=False,
                ),
            ],
            **common,
        }
    )
    split = _candidate(
        {
            "stay_plan_id": StayPlanId.MAAFUSHI_SPLIT_HULHUMALE.value,
            "label_zh": "Hulhumalé 首末晚 + Maafushi 中段 + iCom 快船",
            "segments": [
                _segment(
                    "hulhumale-first",
                    "first",
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT_ISLAND,
                    start,
                    start_plus_one,
                ),
                _segment(
                    "maafushi-middle",
                    "middle",
                    PackagePlaceKey.MAAFUSHI,
                    PackageArea.DESTINATION_ISLAND,
                    start_plus_one,
                    end_minus_one,
                ),
                _segment(
                    "hulhumale-last",
                    "last",
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT_ISLAND,
                    end_minus_one,
                    end,
                ),
            ],
            "required_transfer_contracts": [
                _transfer(
                    "airport-hulhumale-first",
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    start,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
                _transfer(
                    "hulhumale-airport-next-day",
                    PackagePlaceKey.HULHUMALE,
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    PackageArea.AIRPORT,
                    start_plus_one,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
                _transfer(
                    "icom-split-outbound",
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackagePlaceKey.MAAFUSHI,
                    PackageArea.AIRPORT,
                    PackageArea.DESTINATION_ISLAND,
                    start_plus_one,
                    provider="icom-public-transfer",
                    guarantee=published,
                    tax_inclusive=False,
                ),
                _transfer(
                    "icom-split-inbound",
                    PackagePlaceKey.MAAFUSHI,
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackageArea.DESTINATION_ISLAND,
                    PackageArea.AIRPORT,
                    end_minus_one,
                    provider="icom-public-transfer",
                    guarantee=published,
                    tax_inclusive=False,
                ),
                _transfer(
                    "airport-hulhumale-last",
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    end_minus_one,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
                _transfer(
                    "hulhumale-airport-return-day",
                    PackagePlaceKey.HULHUMALE,
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    PackageArea.AIRPORT,
                    end,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
            ],
            **common,
        }
    )
    hulhumale = _candidate(
        {
            "stay_plan_id": StayPlanId.HULHUMALE_CONTINUOUS.value,
            "label_zh": "Hulhumalé 连住 + 双向机场接驳",
            "segments": [
                _segment(
                    "hulhumale-full",
                    "hulhumale-full",
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT_ISLAND,
                    start,
                    end,
                )
            ],
            "required_transfer_contracts": [
                _transfer(
                    "airport-hulhumale-continuous",
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackagePlaceKey.HULHUMALE,
                    PackageArea.AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    start,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
                _transfer(
                    "hulhumale-airport-continuous",
                    PackagePlaceKey.HULHUMALE,
                    PackagePlaceKey.VELANA_AIRPORT,
                    PackageArea.AIRPORT_ISLAND,
                    PackageArea.AIRPORT,
                    end,
                    provider=None,
                    guarantee=all_in,
                    tax_inclusive=True,
                ),
            ],
            **common,
        }
    )
    payload = {
        "schema_version": "tripchord-stay-plan-candidates-v4",
        "gateway_destination": gateway_destination,
        "frozen_at": (frozen_at or datetime(2026, 7, 31, tzinfo=UTC)).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "candidates": [
            item.model_dump(mode="json") for item in (maafushi, split, hulhumale)
        ],
        "source": source.value,
        "source_ref": source_ref,
    }
    return StayPlanCandidateSet.model_validate(
        {
            **payload,
            "candidate_set_sha256": _sha256(payload),
        }
    )


class StayPlanInventoryOutcome(DomainModel):
    source_task_id: str = Field(min_length=1)
    provider: str = Field(pattern="^(ctrip|fliggy|qunar|tongcheng)$")
    stay_plan_id: StayPlanId
    segment_id: str = Field(min_length=1)
    state: StayInventoryResultState
    exact_place_key: PackagePlaceKey
    scan_limit: int = Field(ge=1, le=100)
    scanned_count: int = Field(default=0, ge=0, le=100)
    quote_ids: tuple[str, ...] = ()
    normalization_result_refs: tuple[str, ...] = ()
    raw_snapshot_id: str | None = None
    raw_quote_evidence_sha256s: tuple[str, ...] = ()
    inventory_receipt_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    confirmed_exhaustive: bool = False
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result_state(self) -> Self:
        if self.scanned_count > self.scan_limit:
            raise ValueError("stay inventory scan cannot exceed its pre-frozen limit")
        if self.state == StayInventoryResultState.QUOTE_FOUND:
            if not self.quote_ids:
                raise ValueError("quote_found requires at least one exact quote id")
            if (
                len(self.normalization_result_refs) != len(self.quote_ids)
                or len(self.raw_quote_evidence_sha256s) != len(self.quote_ids)
                or len(set(self.normalization_result_refs))
                != len(self.normalization_result_refs)
                or len(set(self.raw_quote_evidence_sha256s))
                != len(self.raw_quote_evidence_sha256s)
                or self.raw_snapshot_id is None
                or not self.raw_snapshot_id
            ):
                raise ValueError(
                    "quote_found must crosslink each quote to one normalization result "
                    "and one unique raw quote in its source snapshot"
                )
            required_raw_refs = {
                f"browser:{self.provider}:sha256:{digest}"
                for digest in self.raw_quote_evidence_sha256s
            }
            if (
                any(
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    for digest in self.raw_quote_evidence_sha256s
                )
                or not required_raw_refs <= set(self.evidence_refs)
                or f"browser-task:{self.raw_snapshot_id}" not in self.evidence_refs
            ):
                raise ValueError(
                    "quote_found evidence refs must seal its raw snapshot and SHA-256 quotes"
                )
            if self.inventory_receipt_sha256 is not None:
                raise ValueError("quote_found cannot carry a failure inventory receipt")
            if self.confirmed_exhaustive:
                raise ValueError("quote_found cannot also claim confirmed empty inventory")
        elif self.quote_ids:
            raise ValueError("empty stay-inventory outcomes cannot contain quote ids")
        elif (
            self.normalization_result_refs
            or self.raw_snapshot_id is not None
            or self.raw_quote_evidence_sha256s
        ):
            raise ValueError(
                "failure inventory outcomes cannot carry successful quote crosslinks"
            )
        elif self.inventory_receipt_sha256 is None:
            raise ValueError(
                "empty stay-inventory outcomes require a sealed v1 inventory receipt"
            )
        elif (
            not any(
                reference.startswith("browser-task:")
                for reference in self.evidence_refs
            )
            or (
                f"inventory-receipt:sha256:{self.inventory_receipt_sha256}"
                not in self.evidence_refs
            )
        ):
            raise ValueError(
                "failure inventory outcomes must reference their source task and sealed receipt"
            )
        elif self.state == StayInventoryResultState.CONFIRMED_EMPTY:
            if not self.confirmed_exhaustive:
                raise ValueError("confirmed_empty requires exhaustive provider evidence")
        elif self.confirmed_exhaustive:
            raise ValueError("bounded_no_exact_quote must retain its bounded evidence claim")
        return self


def stay_plan_candidate_errors(
    plan: StayPlanCandidate,
    intent: PackageIntent,
    candidate: TravelPackageCandidate,
) -> tuple[str, ...]:
    # A date-pair return date is the searched departure date.  The stay plan
    # must follow the flight's actual airport arrival and return-departure
    # dates, otherwise a safe 2026-09-03 -> 2026-09-10 itinerary is compared
    # against the wrong 2026-09-03 hotel check-in boundary.
    bound_intent = intent.model_copy(
        update={
            "start_date": candidate.flight.outbound_arrive_at.date(),
            "end_date": candidate.flight.return_depart_at.date(),
        }
    )
    errors: list[str] = []
    for segment in plan.segments:
        check_in = segment.check_in.resolve(bound_intent)
        check_out = segment.check_out.resolve(bound_intent)
        lodging_matches = tuple(
            lodging
            for lodging in candidate.lodgings
            if lodging.area == segment.area
            and lodging.place_key == segment.exact_place_key
            and lodging.check_in == check_in
            and lodging.check_out == check_out
        )
        if len(lodging_matches) != 1:
            errors.append(
                "segment:"
                f"{segment.segment_id}:expected_one_exact_quote:"
                f"found_{len(lodging_matches)}"
            )
    if len(candidate.lodgings) != len(plan.segments):
        errors.append(
            f"lodging_segment_count:expected_{len(plan.segments)}:found_{len(candidate.lodgings)}"
        )
    for contract in plan.required_transfer_contracts:
        service_date = contract.service_date.resolve(bound_intent)
        transfer_matches = tuple(
            transfer
            for transfer in candidate.transfers
            if transfer.origin_area == contract.origin_area
            and transfer.destination_area == contract.destination_area
            and transfer.origin_place_key == contract.origin_place_key
            and transfer.destination_place_key == contract.destination_place_key
            and transfer.service_date == service_date
            and transfer.price_guarantee in contract.allowed_price_guarantees
            and (
                contract.required_provider is None
                or transfer.provider == contract.required_provider
            )
            and (
                not contract.requires_tax_inclusive_total
                or transfer.taxes_and_fees_included is True
            )
        )
        if len(transfer_matches) != 1:
            errors.append(
                "transfer:"
                f"{contract.contract_id}:expected_one_exact_contract:"
                f"found_{len(transfer_matches)}"
            )
    if len(candidate.transfers) != len(plan.required_transfer_contracts):
        errors.append(
            "transfer_contract_count:"
            f"expected_{len(plan.required_transfer_contracts)}:"
            f"found_{len(candidate.transfers)}"
        )
    return tuple(errors)


def stay_plan_for_candidate(
    candidate_set: StayPlanCandidateSet,
    intent: PackageIntent,
    candidate: TravelPackageCandidate,
) -> StayPlanId | None:
    matches = tuple(
        plan.stay_plan_id
        for plan in candidate_set.candidates
        if not stay_plan_candidate_errors(plan, intent, candidate)
    )
    if len(matches) > 1:
        raise ValueError("package candidate ambiguously matches multiple frozen stay plans")
    return matches[0] if matches else None


class StayPlanCandidateEvaluation(DomainModel):
    stay_plan_id: StayPlanId
    eligible_candidate_ids: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_evaluation(self) -> Self:
        if self.eligible_candidate_ids and self.rejection_reasons:
            raise ValueError("eligible stay plan cannot also carry rejection reasons")
        if not self.eligible_candidate_ids and not self.rejection_reasons:
            raise ValueError("ineligible stay plan requires a deterministic reason")
        return self


class StayPlanPlannerHandoff(DomainModel):
    candidate_set_sha256: str = Field(min_length=64, max_length=64)
    frozen_stay_plan_ids: tuple[StayPlanId, ...] = Field(min_length=2)
    evaluations: tuple[StayPlanCandidateEvaluation, ...] = Field(min_length=2)
    selected_stay_plan_id: StayPlanId | None = None
    selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        evaluation_ids = tuple(item.stay_plan_id for item in self.evaluations)
        if evaluation_ids != self.frozen_stay_plan_ids:
            raise ValueError("Planner evaluations must preserve frozen stay-plan order")
        if (self.selected_stay_plan_id is None) != (self.selected_candidate_id is None):
            raise ValueError("Planner must select stay plan and package candidate atomically")
        if self.selected_stay_plan_id is None:
            return self
        selected = next(
            item
            for item in self.evaluations
            if item.stay_plan_id == self.selected_stay_plan_id
        )
        if self.selected_candidate_id not in selected.eligible_candidate_ids:
            raise ValueError("Planner selected a candidate outside the frozen eligible stay plan")
        return self

    @classmethod
    def from_candidates(
        cls,
        candidate_set: StayPlanCandidateSet,
        intent: PackageIntent,
        candidates: tuple[TravelPackageCandidate, ...],
        selected_candidate_id: str | None,
        *,
        inventory: PackageInventory | None = None,
        inventory_outcomes: tuple[StayPlanInventoryOutcome, ...] = (),
    ) -> StayPlanPlannerHandoff:
        by_plan: dict[StayPlanId, list[str]] = {
            item.stay_plan_id: [] for item in candidate_set.candidates
        }
        rejected: dict[StayPlanId, list[str]] = {
            item.stay_plan_id: [] for item in candidate_set.candidates
        }
        for candidate in candidates:
            matched = stay_plan_for_candidate(candidate_set, intent, candidate)
            if matched is None:
                for plan in candidate_set.candidates:
                    rejected[plan.stay_plan_id].extend(
                        stay_plan_candidate_errors(plan, intent, candidate)
                    )
                continue
            by_plan[matched].append(candidate.id)
        evaluations = tuple(
            StayPlanCandidateEvaluation(
                stay_plan_id=plan.stay_plan_id,
                eligible_candidate_ids=tuple(by_plan[plan.stay_plan_id]),
                rejection_reasons=(
                    ()
                    if by_plan[plan.stay_plan_id]
                    else tuple(
                        dict.fromkeys(
                            (
                                *rejected[plan.stay_plan_id],
                                *(
                                    _stay_plan_inventory_rejection_reasons(
                                        plan,
                                        intent,
                                        inventory,
                                        inventory_outcomes,
                                    )
                                    if inventory is not None
                                    else ()
                                ),
                            )
                        )
                    )
                    or ("no_exact_inventory_and_transfer_candidate",)
                ),
            )
            for plan in candidate_set.candidates
        )
        selected_plan = None
        if selected_candidate_id is not None:
            selected_candidate = next(
                item for item in candidates if item.id == selected_candidate_id
            )
            selected_plan = stay_plan_for_candidate(
                candidate_set,
                intent,
                selected_candidate,
            )
            if selected_plan is None:
                raise ValueError("Planner cannot select a package outside the frozen stay plans")
        return cls(
            candidate_set_sha256=candidate_set.candidate_set_sha256,
            frozen_stay_plan_ids=candidate_set.stay_plan_ids,
            evaluations=evaluations,
            selected_stay_plan_id=selected_plan,
            selected_candidate_id=selected_candidate_id,
        )


def _stay_plan_inventory_rejection_reasons(
    plan: StayPlanCandidate,
    intent: PackageIntent,
    inventory: PackageInventory,
    inventory_outcomes: tuple[StayPlanInventoryOutcome, ...],
) -> tuple[str, ...]:
    """Return exact, fail-closed reasons why one frozen plan has no package.

    Published foreign-currency base fares remain valid evidence for contracts
    that explicitly allow them, but they are never converted or promoted to an
    all-in price.  Sealed empty/pending lodging outcomes explain missing input;
    they are not treated as usable inventory.
    """

    reasons: list[str] = []
    for segment in plan.segments:
        check_in = segment.check_in.resolve(intent)
        check_out = segment.check_out.resolve(intent)
        exact_lodgings = tuple(
            lodging
            for lodging in inventory.lodgings
            if lodging.availability == QuoteAvailability.AVAILABLE
            and lodging.area == segment.area
            and lodging.place_key == segment.exact_place_key
            and lodging.check_in == check_in
            and lodging.check_out == check_out
            and lodging.adults == intent.adults
            and lodging.rooms == intent.rooms
        )
        if exact_lodgings:
            continue
        reasons.append(
            f"segment:{segment.segment_id}:no_exact_normalized_lodging:"
            f"{segment.exact_place_key.value}:{check_in.isoformat()}:{check_out.isoformat()}"
        )
        segment_outcomes = tuple(
            outcome
            for outcome in inventory_outcomes
            if outcome.stay_plan_id == plan.stay_plan_id
            and outcome.segment_id == segment.segment_id
            and outcome.state != StayInventoryResultState.QUOTE_FOUND
        )
        reasons.extend(
            f"segment:{segment.segment_id}:source:{outcome.provider}:{outcome.state.value}"
            for outcome in sorted(
                segment_outcomes,
                key=lambda item: (item.provider, item.source_task_id),
            )
        )

    for contract in plan.required_transfer_contracts:
        service_date = contract.service_date.resolve(intent)
        exact_transfers = tuple(
            transfer
            for transfer in inventory.transfers
            if transfer.availability == QuoteAvailability.AVAILABLE
            and transfer.origin_area == contract.origin_area
            and transfer.destination_area == contract.destination_area
            and transfer.origin_place_key == contract.origin_place_key
            and transfer.destination_place_key == contract.destination_place_key
            and transfer.service_date == service_date
            and transfer.adults == intent.adults
            and transfer.price_guarantee in contract.allowed_price_guarantees
            and (
                transfer.price_guarantee
                != TransferPriceGuarantee.ALL_IN_CONFIRMED
                or transfer.currency == intent.currency
            )
            and (
                contract.required_provider is None
                or transfer.provider == contract.required_provider
            )
            and (
                not contract.requires_tax_inclusive_total
                or transfer.taxes_and_fees_included is True
            )
        )
        if not exact_transfers:
            reasons.append(
                f"transfer:{contract.contract_id}:no_exact_hard_contract:"
                f"{contract.origin_place_key.value}:{contract.destination_place_key.value}:"
                f"{service_date.isoformat()}"
            )

    if not reasons:
        reasons.append("candidate_pool_empty_after_frozen_lodging_transfer_join")
    return tuple(dict.fromkeys(reasons))


class StayPlanVerificationHandoff(DomainModel):
    candidate_set_sha256: str = Field(min_length=64, max_length=64)
    phase: PackageVerificationPhase
    stay_plan_id: StayPlanId
    candidate_id: str = Field(min_length=1)
    candidate_version: int = Field(ge=1)
    component_ids: tuple[str, ...] = Field(min_length=1)
    error_codes: tuple[PackageViolationCode, ...] = ()

    @classmethod
    def from_package_handoff(
        cls,
        *,
        candidate_set: StayPlanCandidateSet,
        stay_plan_id: StayPlanId,
        package_handoff: PackageVerificationHandoff,
    ) -> StayPlanVerificationHandoff:
        return cls(
            candidate_set_sha256=candidate_set.candidate_set_sha256,
            phase=package_handoff.phase,
            stay_plan_id=stay_plan_id,
            candidate_id=package_handoff.candidate_id,
            candidate_version=package_handoff.candidate_version,
            component_ids=package_handoff.component_ids,
            error_codes=tuple(item.code for item in package_handoff.errors),
        )


class StayPlanRepairHandoff(DomainModel):
    candidate_set_sha256: str = Field(min_length=64, max_length=64)
    rejected_stay_plan_id: StayPlanId
    rejected_candidate_id: str = Field(min_length=1)
    rejection_error_codes: tuple[PackageViolationCode, ...] = ()
    attempted: bool
    agent_strategy_applied: bool = False
    repaired_stay_plan_id: StayPlanId | None = None
    repaired_candidate_id: str | None = None

    @model_validator(mode="after")
    def validate_repair(self) -> Self:
        if bool(self.rejection_error_codes) != self.attempted:
            raise ValueError("stay-plan Repair attempt must exactly follow Verifier hard errors")
        if (self.repaired_stay_plan_id is None) != (self.repaired_candidate_id is None):
            raise ValueError("stay-plan Repair must bind plan and package candidate atomically")
        changed_by_repair = self.attempted or self.agent_strategy_applied
        if changed_by_repair and self.repaired_candidate_id == self.rejected_candidate_id:
            raise ValueError("stay-plan Repair cannot silently reuse the rejected package")
        if self.agent_strategy_applied and self.repaired_candidate_id is None:
            raise ValueError("applied Agent stay-plan repair requires a candidate")
        if not changed_by_repair and (
            self.repaired_candidate_id != self.rejected_candidate_id
            or self.repaired_stay_plan_id != self.rejected_stay_plan_id
        ):
            raise ValueError("no-op stay-plan Repair must preserve Planner selection")
        return self


class StayPlanPlanningHandoff(DomainModel):
    planner: StayPlanPlannerHandoff
    initial_verification: StayPlanVerificationHandoff
    repair: StayPlanRepairHandoff
    reverification: StayPlanVerificationHandoff | None

    @model_validator(mode="after")
    def validate_chain(self) -> Self:
        selected_plan = self.planner.selected_stay_plan_id
        selected_candidate = self.planner.selected_candidate_id
        if selected_plan is None or selected_candidate is None:
            raise ValueError("stay-plan handoff requires a Planner selection")
        hashes = {
            self.planner.candidate_set_sha256,
            self.initial_verification.candidate_set_sha256,
            self.repair.candidate_set_sha256,
        }
        if self.reverification is not None:
            hashes.add(self.reverification.candidate_set_sha256)
        if len(hashes) != 1:
            raise ValueError("Planner, Verifier, Repair and ReVerifier changed the frozen set")
        if (
            self.initial_verification.phase != PackageVerificationPhase.INITIAL
            or self.initial_verification.stay_plan_id != selected_plan
            or self.initial_verification.candidate_id != selected_candidate
        ):
            raise ValueError("Verifier did not verify the Planner-selected stay plan")
        if (
            self.repair.rejected_stay_plan_id != selected_plan
            or self.repair.rejected_candidate_id != selected_candidate
            or self.repair.rejection_error_codes
            != self.initial_verification.error_codes
        ):
            raise ValueError("Repair did not preserve Verifier rejection provenance")
        repaired_candidate = self.repair.repaired_candidate_id
        repaired_plan = self.repair.repaired_stay_plan_id
        if repaired_candidate is None or repaired_plan is None:
            if self.reverification is not None:
                raise ValueError("failed stay-plan Repair cannot be reverified")
            return self
        if self.reverification is None:
            raise ValueError("master cannot receive a stay-plan Repair without ReVerifier")
        if (
            self.reverification.phase != PackageVerificationPhase.REVERIFICATION
            or self.reverification.stay_plan_id != repaired_plan
            or self.reverification.candidate_id != repaired_candidate
        ):
            raise ValueError("ReVerifier did not verify the exact stay-plan Repair output")
        return self
