from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    NormalizedLodgingQuote,
    PackageArea,
    PackageCandidateKind,
    PackageDiff,
    PackageIntent,
    PackagePlaceKey,
    PackageQuote,
    QuoteAvailability,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TravelPackageCandidate,
)


class PackageInvariantCode(StrEnum):
    UNIQUE_COMPONENT_IDS = "unique_component_ids"
    VERSION_LINEAGE = "version_lineage"
    DECLARED_DIFF_MATCHES = "declared_diff_matches"
    UNAFFECTED_COMPONENTS_PRESERVED = "unaffected_components_preserved"
    INTENT_DATE_PARTY_ROOMS = "intent_date_party_rooms"
    HARD_PREFERENCES = "hard_preferences"
    LODGING_KIND_STRUCTURE = "lodging_kind_structure"
    LODGING_NIGHT_COVERAGE = "lodging_night_coverage"
    TRANSFER_PRICE_CONTRACTS = "transfer_price_contracts"
    TOTAL_ARITHMETIC_AND_BUDGET = "total_arithmetic_and_budget"
    QUOTE_TRUST_AND_FRESHNESS = "quote_trust_and_freshness"
    QUOTE_CAPTURE_SKEW = "quote_capture_skew"
    TRANSFER_CHAIN_AND_CONNECTIONS = "transfer_chain_and_connections"


class PackageInvariantCheck(DomainModel):
    code: PackageInvariantCode
    passed: bool
    message: str = Field(min_length=1)
    component_ids: tuple[str, ...] = ()
    details: dict[str, str | int | bool] = Field(default_factory=dict)


class PackageReverificationReport(DomainModel):
    engine: str = "declarative-package-invariants-v1"
    semantics_boundary: str = (
        "共享业务语义的异构确定性重算，不调用 PackageVerifier，也不是形式化证明"
    )
    before_candidate_id: str = Field(min_length=1)
    after_candidate_id: str = Field(min_length=1)
    audited_at: datetime
    checks: tuple[PackageInvariantCheck, ...]

    @field_validator("audited_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("package reverification audit time must be timezone-aware")
        return value

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_codes(self) -> tuple[PackageInvariantCode, ...]:
        return tuple(check.code for check in self.checks if not check.passed)


class DeclarativePackageReVerifier:
    """Heterogeneous deterministic audit over a repaired package.

    The implementation deliberately does not call ``PackageVerifier`` or
    ``diff_packages``.  It recomputes a second set of invariants from the
    serialized intent, package components, and declared repair receipt.  Some
    business semantics necessarily overlap the primary verifier, so this is an
    independent implementation and failure-containment layer, not a formal
    proof that the primary verifier is correct.
    """

    def audit(
        self,
        intent: PackageIntent,
        before: TravelPackageCandidate,
        after: TravelPackageCandidate,
        diff: PackageDiff | None,
        *,
        now: datetime | None = None,
    ) -> PackageReverificationReport:
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("package reverification reference time must be timezone-aware")
        checks = (
            self._unique_component_ids(after),
            self._version_lineage(intent, before, after, diff),
            self._declared_diff(before, after, diff),
            self._unaffected_components_preserved(before, after, diff),
            self._intent_date_party_rooms(intent, after),
            self._hard_preferences(intent, after),
            self._lodging_kind_structure(intent, after),
            self._lodging_night_coverage(intent, after),
            self._transfer_price_contracts(after),
            self._total_arithmetic_and_budget(intent, after),
            self._quote_trust_and_freshness(after, reference),
            self._quote_capture_skew(intent, after),
            self._transfer_chain_and_connections(intent, after),
        )
        return PackageReverificationReport(
            before_candidate_id=before.id,
            after_candidate_id=after.id,
            audited_at=reference,
            checks=checks,
        )

    def _unique_component_ids(
        self,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        ids = candidate.component_ids
        duplicates = tuple(sorted(item_id for item_id, count in Counter(ids).items() if count > 1))
        expected_count = 1 + len(candidate.lodgings) + len(candidate.transfers)
        return self._check(
            PackageInvariantCode.UNIQUE_COMPONENT_IDS,
            not duplicates and len(ids) == expected_count,
            "航班、住宿和接驳组件 ID 必须全局唯一且数量与组件结构一致",
            duplicates,
            expected_component_count=expected_count,
            actual_component_count=len(ids),
        )

    def _version_lineage(
        self,
        intent: PackageIntent,
        before: TravelPackageCandidate,
        after: TravelPackageCandidate,
        diff: PackageDiff | None,
    ) -> PackageInvariantCheck:
        unchanged = after == before
        if unchanged:
            passed = diff is None and after.trip_id == intent.trip_id
        else:
            passed = bool(
                diff is not None
                and diff.changed
                and before.trip_id == intent.trip_id
                and after.trip_id == intent.trip_id
                and after.id != before.id
                and after.version == before.version + 1
                and after.parent_candidate_id == before.id
            )
        return self._check(
            PackageInvariantCode.VERSION_LINEAGE,
            passed,
            "实质修复必须生成直接子版本；无操作复核必须逐值保持原候选",
            (before.id, after.id),
            before_version=before.version,
            after_version=after.version,
            changed=not unchanged,
        )

    def _declared_diff(
        self,
        before: TravelPackageCandidate,
        after: TravelPackageCandidate,
        diff: PackageDiff | None,
    ) -> PackageInvariantCheck:
        before_quotes = self._quote_map(before)
        after_quotes = self._quote_map(after)
        before_ids = set(before_quotes)
        after_ids = set(after_quotes)
        actual_removed = tuple(
            item_id for item_id in before.component_ids if item_id not in after_ids
        )
        actual_added = tuple(
            item_id for item_id in after.component_ids if item_id not in before_ids
        )
        actual_changed = tuple(
            item_id
            for item_id in before.component_ids
            if item_id in after_quotes and before_quotes[item_id] != after_quotes[item_id]
        )
        actual_preserved = tuple(
            item_id for item_id in before.component_ids if item_id in after_ids
        )
        actual_ratio = (
            Decimal(len(actual_preserved)) / Decimal(len(before.component_ids))
            if before.component_ids
            else Decimal(1)
        )
        actual_material_change = bool(actual_removed or actual_added or actual_changed)
        if diff is None:
            passed = not actual_material_change and after == before
        else:
            passed = (
                diff.before_candidate_id == before.id
                and diff.after_candidate_id == after.id
                and diff.removed_component_ids == actual_removed
                and diff.added_component_ids == actual_added
                and diff.changed_component_ids == actual_changed
                and diff.preserved_component_ids == actual_preserved
                and diff.preservation_ratio == actual_ratio
                and diff.changed == actual_material_change
            )
        affected = tuple(dict.fromkeys((*actual_removed, *actual_added, *actual_changed)))
        return self._check(
            PackageInvariantCode.DECLARED_DIFF_MATCHES,
            passed,
            "Repair 声明的 before/after/diff 必须与独立逐组件重算完全一致",
            affected,
            material_component_change=actual_material_change,
            diff_present=diff is not None,
        )

    def _unaffected_components_preserved(
        self,
        before: TravelPackageCandidate,
        after: TravelPackageCandidate,
        diff: PackageDiff | None,
    ) -> PackageInvariantCheck:
        before_quotes = self._quote_map(before)
        after_quotes = self._quote_map(after)
        declared_affected = (
            {
                *diff.removed_component_ids,
                *diff.added_component_ids,
                *diff.changed_component_ids,
            }
            if diff is not None
            else set()
        )
        unexpected = tuple(
            item_id
            for item_id, before_quote in before_quotes.items()
            if item_id not in declared_affected and after_quotes.get(item_id) != before_quote
        )
        return self._check(
            PackageInvariantCode.UNAFFECTED_COMPONENTS_PRESERVED,
            not unexpected,
            "diff 之外的组件必须逐字段保持不变",
            unexpected,
        )

    def _intent_date_party_rooms(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        invalid: list[str] = []
        flight = candidate.flight
        if (
            candidate.trip_id != intent.trip_id
            or candidate.currency != intent.currency
        ):
            invalid.append(candidate.id)
        if (
            flight.origin != intent.origin
            or flight.destination != intent.destination
            or flight.outbound_depart_at.date() != intent.start_date
            or flight.return_depart_at.date() != intent.end_date
            or flight.adults != intent.adults
            or not flight.party_availability_confirmed
            or flight.currency != intent.currency
        ):
            invalid.append(flight.id)
        invalid.extend(
            lodging.id
            for lodging in candidate.lodgings
            if lodging.adults != intent.adults
            or lodging.rooms != intent.rooms
            or lodging.currency != intent.currency
        )
        invalid.extend(
            transfer.id
            for transfer in candidate.transfers
            if transfer.adults != intent.adults
            or (
                transfer.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
                and transfer.currency != intent.currency
            )
        )
        return self._check(
            PackageInvariantCode.INTENT_DATE_PARTY_ROOMS,
            not invalid,
            "候选的路线、旅行日期、成人数、房间数与已确认币种必须忠实于用户意图",
            tuple(dict.fromkeys(invalid)),
            expected_adults=intent.adults,
            expected_rooms=intent.rooms,
        )

    def _lodging_night_coverage(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        counts = {
            intent.start_date + timedelta(days=offset): 0
            for offset in range(intent.night_count)
        }
        outside: list[str] = []
        for lodging in candidate.lodgings:
            night = lodging.check_in
            while night < lodging.check_out:
                if night not in counts:
                    outside.append(f"{lodging.id}@{night.isoformat()}")
                else:
                    counts[night] += 1
                night += timedelta(days=1)
        invalid_nights = tuple(
            night.isoformat() for night, count in counts.items() if count != 1
        )
        passed = not invalid_nights and not outside
        return self._check(
            PackageInvariantCode.LODGING_NIGHT_COVERAGE,
            passed,
            "住宿必须逐晚且仅一次覆盖完整行程，不得覆盖行程外夜晚",
            tuple(item.id for item in candidate.lodgings) if not passed else (),
            invalid_nights=",".join(invalid_nights),
            outside_nights=",".join(outside),
        )

    def _hard_preferences(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        invalid: set[str] = set()
        if intent.require_checked_baggage is True:
            baggage = candidate.flight.checked_baggage_per_adult_kg
            if baggage is None or baggage <= 0:
                invalid.add(candidate.flight.id)
        if intent.allow_connections is False:
            outbound = candidate.flight.outbound_flight_numbers
            returning = candidate.flight.return_flight_numbers
            if not outbound or not returning or len(outbound) != 1 or len(returning) != 1:
                invalid.add(candidate.flight.id)
        if intent.require_breakfast is not None:
            invalid.update(
                lodging.id
                for lodging in candidate.lodgings
                if lodging.breakfast_included is not intent.require_breakfast
            )
        return self._check(
            PackageInvariantCode.HARD_PREFERENCES,
            not invalid,
            "显式托运行李、拒绝中转和早餐硬偏好必须由报价字段直接证明",
            tuple(sorted(invalid)),
            checked_baggage_required=intent.require_checked_baggage is True,
            direct_flight_required=intent.allow_connections is False,
            breakfast_constraint_present=intent.require_breakfast is not None,
        )

    def _lodging_kind_structure(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        if candidate.kind == PackageCandidateKind.CONTINUOUS_ISLAND:
            expected = Counter(
                {(PackageArea.DESTINATION_ISLAND, intent.start_date, intent.end_date): 1}
            )
        elif candidate.kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND:
            expected = Counter(
                {(PackageArea.AIRPORT_ISLAND, intent.start_date, intent.end_date): 1}
            )
        else:
            first_checkout = intent.start_date + timedelta(days=1)
            last_checkin = intent.end_date - timedelta(days=1)
            expected = Counter(
                {
                    (PackageArea.AIRPORT_ISLAND, intent.start_date, first_checkout): 1,
                    (
                        PackageArea.DESTINATION_ISLAND,
                        first_checkout,
                        last_checkin,
                    ): 1,
                    (PackageArea.AIRPORT_ISLAND, last_checkin, intent.end_date): 1,
                }
            )
        actual = Counter(
            (lodging.area, lodging.check_in, lodging.check_out)
            for lodging in candidate.lodgings
        )
        place_mismatches = tuple(
            lodging.id
            for lodging in candidate.lodgings
            if (
                intent.destination_place_key == PackagePlaceKey.MAAFUSHI
                and lodging.area == PackageArea.DESTINATION_ISLAND
                and lodging.place_key != PackagePlaceKey.MAAFUSHI
            )
            or (
                intent.destination_place_key == PackagePlaceKey.HULHUMALE
                and lodging.area == PackageArea.AIRPORT_ISLAND
                and lodging.place_key != PackagePlaceKey.HULHUMALE
            )
        )
        passed = actual == expected and not place_mismatches
        return self._check(
            PackageInvariantCode.LODGING_KIND_STRUCTURE,
            passed,
            "住宿区域、完整连住或首中末分段必须与候选类型和明确地点一致",
            place_mismatches or (() if passed else tuple(item.id for item in candidate.lodgings)),
            expected_segment_count=sum(expected.values()),
            actual_segment_count=len(candidate.lodgings),
        )

    def _transfer_price_contracts(
        self,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        by_contract: dict[str, list[TransferOption]] = {}
        for transfer in candidate.transfers:
            by_contract.setdefault(transfer.price_contract_id, []).append(transfer)
        invalid: set[str] = set()
        for group in by_contract.values():
            first = group[0]
            terms_match = all(
                first.price_scope == transfer.price_scope
                and first.purchase_scope == transfer.purchase_scope
                and first.price_guarantee == transfer.price_guarantee
                and first.bound_lodging_id == transfer.bound_lodging_id
                and first.provider == transfer.provider
                and first.currency == transfer.currency
                and first.total_for_party_cents == transfer.total_for_party_cents
                and first.taxes_and_fees_included == transfer.taxes_and_fees_included
                and first.adults == transfer.adults
                for transfer in group[1:]
            )
            if len(group) == 1:
                if not terms_match:
                    invalid.add(first.id)
                continue
            reciprocal = bool(
                len(group) == 2
                and all(
                    transfer.price_scope == TransferPriceScope.ROUND_TRIP
                    for transfer in group
                )
                and group[0].origin_area == group[1].destination_area
                and group[0].destination_area == group[1].origin_area
                and group[0].origin_place_key == group[1].destination_place_key
                and group[0].destination_place_key == group[1].origin_place_key
            )
            if not terms_match or not reciprocal:
                invalid.update(item.id for item in group)
        return self._check(
            PackageInvariantCode.TRANSFER_PRICE_CONTRACTS,
            not invalid,
            "共享往返价格合同必须且只能覆盖两条条款一致的互补去返程腿",
            tuple(sorted(invalid)),
            price_contract_count=len(by_contract),
            transfer_count=len(candidate.transfers),
        )

    def _total_arithmetic_and_budget(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        contract_terms: dict[
            str,
            tuple[int, str, TransferPriceScope, TransferPurchaseScope, bool | None],
        ] = {}
        invalid_contracts: set[str] = set()
        transfer_total = 0
        known_same_currency_base_fare_total = 0
        for transfer in candidate.transfers:
            if transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE:
                if (
                    transfer.currency == candidate.currency
                    and transfer.price_contract_id not in contract_terms
                ):
                    contract_terms[transfer.price_contract_id] = (
                        transfer.total_for_party_cents,
                        transfer.currency,
                        transfer.price_scope,
                        transfer.purchase_scope,
                        transfer.taxes_and_fees_included,
                    )
                    known_same_currency_base_fare_total += transfer.total_for_party_cents
                continue
            terms = (
                transfer.total_for_party_cents,
                transfer.currency,
                transfer.price_scope,
                transfer.purchase_scope,
                transfer.taxes_and_fees_included,
            )
            previous = contract_terms.get(transfer.price_contract_id)
            if previous is None:
                contract_terms[transfer.price_contract_id] = terms
                if transfer.currency == candidate.currency:
                    transfer_total += transfer.total_for_party_cents
            elif previous != terms or transfer.price_scope != TransferPriceScope.ROUND_TRIP:
                invalid_contracts.add(transfer.price_contract_id)
        independently_computed = (
            candidate.flight.total_for_party_cents
            + sum(item.total_for_party_cents for item in candidate.lodgings)
            + transfer_total
        )
        minimum_known_total = (
            independently_computed + known_same_currency_base_fare_total
        )
        over_budget = bool(
            intent.budget_cents is not None
            and minimum_known_total > intent.budget_cents
        )
        passed = (
            not invalid_contracts
            and candidate.declared_total_cents == independently_computed
            and not over_budget
        )
        return self._check(
            PackageInvariantCode.TOTAL_ARITHMETIC_AND_BUDGET,
            passed,
            "声明小计按整数分重算；同币种公开基础价还必须进入预算最低下界",
            tuple(sorted(invalid_contracts)) or (candidate.id,) if not passed else (),
            declared_total_cents=candidate.declared_total_cents,
            independently_computed_cents=independently_computed,
            known_same_currency_base_fare_cents=known_same_currency_base_fare_total,
            minimum_known_total_cents=minimum_known_total,
            budget_cents=intent.budget_cents if intent.budget_cents is not None else -1,
            over_budget=over_budget,
        )

    def _quote_trust_and_freshness(
        self,
        candidate: TravelPackageCandidate,
        now: datetime,
    ) -> PackageInvariantCheck:
        invalid: list[str] = []
        for quote in self._quotes(candidate):
            timestamps_aware = (
                quote.captured_at.tzinfo is not None
                and quote.expires_at.tzinfo is not None
            )
            fresh = bool(
                timestamps_aware
                and quote.captured_at <= now < quote.expires_at
            )
            evidence_complete = bool(quote.evidence_refs) and all(
                bool(ref.strip()) for ref in quote.evidence_refs
            )
            taxes_complete = quote.taxes_and_fees_included is True
            if (
                isinstance(quote, TransferOption)
                and quote.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
            ):
                taxes_complete = quote.taxes_and_fees_included is not True
            if (
                not timestamps_aware
                or not fresh
                or quote.availability != QuoteAvailability.AVAILABLE
                or not evidence_complete
                or not taxes_complete
            ):
                invalid.append(quote.id)
        return self._check(
            PackageInvariantCode.QUOTE_TRUST_AND_FRESHNESS,
            not invalid,
            "每个报价必须有时区、有效 TTL、可售状态、非空证据和与价格口径一致的税费状态",
            tuple(invalid),
            audited_at=now.isoformat(),
        )

    def _quote_capture_skew(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        quotes = self._quotes(candidate)
        aware = all(item.captured_at.tzinfo is not None for item in quotes)
        skew_seconds = (
            int(
                (
                    max(item.captured_at for item in quotes)
                    - min(item.captured_at for item in quotes)
                ).total_seconds()
            )
            if quotes and aware
            else -1
        )
        allowed_seconds = intent.maximum_quote_capture_skew_minutes * 60
        return self._check(
            PackageInvariantCode.QUOTE_CAPTURE_SKEW,
            aware and 0 <= skew_seconds <= allowed_seconds,
            "跨平台组件抓取时间差不得超过用户意图规定的核价一致性窗口",
            (
                tuple(item.id for item in quotes)
                if skew_seconds > allowed_seconds or not aware
                else ()
            ),
            capture_skew_seconds=skew_seconds,
            maximum_capture_skew_seconds=allowed_seconds,
        )

    def _transfer_chain_and_connections(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> PackageInvariantCheck:
        required = self._required_transfer_legs(intent, candidate.kind)
        available = Counter(
            (item.origin_area, item.destination_area, item.service_date)
            for item in candidate.transfers
        )
        required_counts = Counter(required)
        invalid: set[str] = set()
        structure_mismatch = available != required_counts
        if structure_mismatch:
            invalid.update(item.id for item in candidate.transfers)

        by_leg: dict[tuple[PackageArea, PackageArea, date], TransferOption] = {}
        for transfer in candidate.transfers:
            leg = (transfer.origin_area, transfer.destination_area, transfer.service_date)
            if leg in by_leg:
                invalid.update((by_leg[leg].id, transfer.id))
            by_leg[leg] = transfer
            if not self._binding_matches(transfer, candidate.lodgings):
                invalid.add(transfer.id)
            if not self._published_place_matches(intent, transfer, candidate.lodgings):
                invalid.add(transfer.id)

        arrival_leg = required[0]
        arrival = by_leg.get(arrival_leg)
        if arrival is not None:
            required_buffer = (
                intent.minimum_arrival_to_boat_minutes
                if candidate.kind == PackageCandidateKind.CONTINUOUS_ISLAND
                else 0
            )
            not_before = candidate.flight.outbound_arrive_at + timedelta(
                minutes=required_buffer
            )
            if not self._has_feasible_departure(arrival, not_before=not_before):
                invalid.add(arrival.id)

        return_leg = required[-1]
        returning = by_leg.get(return_leg)
        if returning is not None:
            arrive_by = candidate.flight.return_depart_at - timedelta(
                minutes=intent.minimum_airport_buffer_minutes
            )
            if not self._has_feasible_departure(returning, arrive_by=arrive_by):
                invalid.add(returning.id)

        if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND:
            for first_leg, second_leg in ((required[1], required[2]), (required[3], required[4])):
                first = by_leg.get(first_leg)
                second = by_leg.get(second_leg)
                if first is None or second is None:
                    continue
                if (
                    first.service_date != second.service_date
                    or first.destination_area != second.origin_area
                    or self._earliest_arrival(first)
                    + timedelta(minutes=intent.minimum_transfer_connection_minutes)
                    > self._latest_departure(second)
                ):
                    invalid.update((first.id, second.id))

        return self._check(
            PackageInvariantCode.TRANSFER_CHAIN_AND_CONNECTIONS,
            not structure_mismatch and not invalid,
            "接驳腿必须精确覆盖方案类型，并满足酒店绑定、地点身份、航班缓冲和同日换乘窗口",
            tuple(sorted(invalid)),
            required_transfer_count=len(required),
            actual_transfer_count=len(candidate.transfers),
            structure_mismatch=structure_mismatch,
        )

    @staticmethod
    def _quotes(candidate: TravelPackageCandidate) -> tuple[PackageQuote, ...]:
        return (candidate.flight, *candidate.lodgings, *candidate.transfers)

    @classmethod
    def _quote_map(
        cls,
        candidate: TravelPackageCandidate,
    ) -> dict[str, PackageQuote]:
        return {quote.id: quote for quote in cls._quotes(candidate)}

    @staticmethod
    def _required_transfer_legs(
        intent: PackageIntent,
        kind: PackageCandidateKind,
    ) -> tuple[tuple[PackageArea, PackageArea, date], ...]:
        if kind == PackageCandidateKind.CONTINUOUS_ISLAND:
            return (
                (PackageArea.AIRPORT, PackageArea.DESTINATION_ISLAND, intent.start_date),
                (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT, intent.end_date),
            )
        if kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND:
            return (
                (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, intent.start_date),
                (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, intent.end_date),
            )
        first_checkout = intent.start_date + timedelta(days=1)
        last_checkin = intent.end_date - timedelta(days=1)
        return (
            (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, intent.start_date),
            (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, first_checkout),
            (PackageArea.AIRPORT, PackageArea.DESTINATION_ISLAND, first_checkout),
            (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT, last_checkin),
            (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, last_checkin),
            (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, intent.end_date),
        )

    @staticmethod
    def _binding_matches(
        transfer: TransferOption,
        lodgings: tuple[NormalizedLodgingQuote, ...],
    ) -> bool:
        if transfer.purchase_scope == TransferPurchaseScope.PUBLIC_INDEPENDENT:
            return transfer.bound_lodging_id is None
        lodging = next(
            (item for item in lodgings if item.id == transfer.bound_lodging_id),
            None,
        )
        if lodging is None:
            return False
        arrives = (
            transfer.destination_area == lodging.area
            and transfer.service_date == lodging.check_in
        )
        leaves = (
            transfer.origin_area == lodging.area
            and transfer.service_date == lodging.check_out
        )
        return arrives or leaves

    @staticmethod
    def _published_place_matches(
        intent: PackageIntent,
        transfer: TransferOption,
        lodgings: tuple[NormalizedLodgingQuote, ...],
    ) -> bool:
        if transfer.price_guarantee != TransferPriceGuarantee.PUBLISHED_BASE_FARE:
            return True
        if intent.destination_place_key not in {None, PackagePlaceKey.MAAFUSHI}:
            return False
        destination_lodgings = tuple(
            item for item in lodgings if item.area == PackageArea.DESTINATION_ISLAND
        )
        if not destination_lodgings or any(
            item.place_key != PackagePlaceKey.MAAFUSHI for item in destination_lodgings
        ):
            return False
        expected = {
            PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
            PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
        }
        return (
            transfer.origin_place_key == expected.get(transfer.origin_area)
            and transfer.destination_place_key == expected.get(transfer.destination_area)
        )

    @staticmethod
    def _earliest_departure(transfer: TransferOption) -> datetime:
        if transfer.depart_at is not None:
            return transfer.depart_at
        assert transfer.service_window_start_at is not None
        return transfer.service_window_start_at

    @staticmethod
    def _latest_departure(transfer: TransferOption) -> datetime:
        if transfer.depart_at is not None:
            return transfer.depart_at
        assert transfer.service_window_end_at is not None
        return transfer.service_window_end_at

    @classmethod
    def _earliest_arrival(cls, transfer: TransferOption) -> datetime:
        if transfer.arrive_at is not None:
            return transfer.arrive_at
        return cls._earliest_departure(transfer) + timedelta(
            minutes=transfer.duration_minutes
        )

    @classmethod
    def _has_feasible_departure(
        cls,
        transfer: TransferOption,
        *,
        not_before: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> bool:
        earliest = cls._earliest_departure(transfer)
        latest = cls._latest_departure(transfer)
        if not_before is not None:
            earliest = max(earliest, not_before)
        if arrive_by is not None:
            latest = min(
                latest,
                arrive_by - timedelta(minutes=transfer.duration_minutes),
            )
        return earliest <= latest

    @staticmethod
    def _check(
        code: PackageInvariantCode,
        passed: bool,
        message: str,
        component_ids: tuple[str, ...] = (),
        **details: str | int | bool,
    ) -> PackageInvariantCheck:
        return PackageInvariantCheck(
            code=code,
            passed=passed,
            message=message,
            component_ids=component_ids,
            details=details,
        )
