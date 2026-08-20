from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentSystem,
    _RunState,
)
from tripchord.agents.models import AgentRole, AgentTask
from tripchord.agents.tools import ToolRegistry
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageCandidateKind,
    PackageDecisionState,
    PackageDiff,
    PackageIntent,
    PackagePlaceKey,
    PackagePlannerHandoff,
    PackageRepairHandoff,
    PackageRepairOutcome,
    PackageVerificationHandoff,
    PackageVerificationPhase,
    PackageVerifier,
    PackageViolationCode,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
    TravelPackageCandidate,
    diff_packages,
)
from tripchord.planning.package_reverification import (
    DeclarativePackageReVerifier,
    PackageInvariantCode,
)
from tripchord.platform.booking import BookingLedger
from tripchord.platform.booking_gate import BookingService
from tripchord.providers.browser_bridge import BrowserTaskBridge

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
MALDIVES = timezone(timedelta(hours=5))
START = date(2026, 8, 23)
END = date(2026, 8, 30)


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="audit-trip",
        origin="HGH",
        destination="MLE",
        start_date=START,
        end_date=END,
        adults=2,
        rooms=1,
        budget_cents=1_500_000,
        minimum_arrival_to_boat_minutes=120,
        minimum_airport_buffer_minutes=180,
    )


def _flight() -> NormalizedFlightQuote:
    return NormalizedFlightQuote(
        id="flight:v1",
        provider="ctrip",
        total_for_party_cents=900_000,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        evidence_refs=("evidence:flight",),
        origin="HGH",
        destination="MLE",
        adults=2,
        party_availability_confirmed=True,
        outbound_depart_at=datetime(2026, 8, 23, 8, 30, tzinfo=timezone(timedelta(hours=8))),
        outbound_arrive_at=datetime(2026, 8, 23, 18, 35, tzinfo=MALDIVES),
        return_depart_at=datetime(2026, 8, 30, 10, 45, tzinfo=MALDIVES),
        return_arrive_at=datetime(2026, 8, 31, 15, 40, tzinfo=timezone(timedelta(hours=8))),
    )


def _lodging(quote_id: str, total_cents: int) -> NormalizedLodgingQuote:
    return NormalizedLodgingQuote(
        id=quote_id,
        provider="qunar",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        evidence_refs=(f"evidence:{quote_id}",),
        property_name="Airport Island Hotel",
        area=PackageArea.AIRPORT_ISLAND,
        check_in=START,
        check_out=END,
        adults=2,
        rooms=1,
    )


def _transfer(
    quote_id: str,
    origin: PackageArea,
    destination: PackageArea,
    service_date: date,
) -> TransferOption:
    window_start = datetime.combine(service_date, datetime.min.time(), tzinfo=MALDIVES)
    return TransferOption(
        id=quote_id,
        provider="hotel-transfer",
        total_for_party_cents=20_000,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        evidence_refs=(f"evidence:{quote_id}",),
        origin_area=origin,
        destination_area=destination,
        adults=2,
        service_date=service_date,
        schedule_mode=TransferScheduleMode.SERVICE_WINDOW,
        duration_minutes=20,
        service_window_start_at=window_start,
        service_window_end_at=window_start.replace(hour=23, minute=59),
        operates_24_hours=True,
        requires_reservation=True,
        price_scope=TransferPriceScope.ROUND_TRIP,
        price_contract_id="transfer-contract:round-trip",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        contract_evidence_text="往返机场与机场岛酒店，24 小时服务，两人含税总价 CNY 200.00",
        detail_url="https://example.com/transfer-contract",
    )


def _candidates() -> tuple[TravelPackageCandidate, TravelPackageCandidate, PackageDiff]:
    flight = _flight()
    transfers = (
        _transfer(
            "transfer:outbound",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            START,
        ),
        _transfer(
            "transfer:return",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            END,
        ),
    )
    before = TravelPackageCandidate(
        id="audit-trip:package:airport:v1",
        trip_id="audit-trip",
        kind=PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
        flight=flight,
        lodgings=(_lodging("lodging:before", 400_000),),
        transfers=transfers,
        declared_total_cents=1_320_000,
    )
    after = TravelPackageCandidate(
        id="audit-trip:package:alternative:v2",
        trip_id="audit-trip",
        version=2,
        parent_candidate_id=before.id,
        kind=PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
        flight=flight,
        lodgings=(_lodging("lodging:after", 390_000),),
        transfers=transfers,
        declared_total_cents=1_310_000,
    )
    return before, after, diff_packages(before, after)


def _ledger_with_protected(component_id: str = "lodging:before") -> BookingLedger:
    service = BookingService(BookingLedger(plan_version="audit-trip"), now=NOW)
    ledger, _ = service.acknowledge_component(
        plan_version="audit-trip",
        component_id=component_id,
        checklist_id="checklist-1",
        acknowledgement_id="ack-1",
        user_token_sha256="a" * 64,
    )
    return ledger


def test_reverifier_blocks_removal_of_protected_component() -> None:
    before, after, _ = _candidates()
    removed_lodging = after.model_copy(
        update={
            "lodgings": (),
            "declared_total_cents": (
                after.flight.total_for_party_cents
                + sum(item.total_for_party_cents for item in after.transfers)
            ),
        }
    )

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        removed_lodging,
        diff_packages(before, removed_lodging),
        now=NOW,
        booking_ledger=_ledger_with_protected("lodging:before"),
    )

    check = next(
        c
        for c in report.checks
        if c.code is PackageInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is False
    assert check.component_ids == ("lodging:before",)
    assert not report.passed


def test_reverifier_blocks_change_of_protected_component() -> None:
    before, _, _ = _candidates()
    changed_lodging = before.lodgings[0].model_copy(update={"property_name": "renamed"})
    changed_before = before.model_copy(update={"lodgings": (changed_lodging,)})

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        changed_before,
        diff_packages(before, changed_before),
        now=NOW,
        booking_ledger=_ledger_with_protected("lodging:before"),
    )

    check = next(
        c
        for c in report.checks
        if c.code is PackageInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is False
    assert "lodging:before" in check.component_ids


def test_reverifier_passes_when_protected_component_preserved() -> None:
    before, after, diff = _candidates()

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        after,
        diff,
        now=NOW,
        booking_ledger=_ledger_with_protected("flight:v1"),
    )

    check = next(
        c
        for c in report.checks
        if c.code is PackageInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is True
    assert report.passed


def test_reverifier_passes_when_applied_override_unprotects() -> None:
    before, after, diff = _candidates()
    ledger = _ledger_with_protected("lodging:before")
    service = BookingService(ledger, now=NOW)
    updated, _ = service.request_override(
        plan_version="audit-trip",
        component_id="lodging:before",
        requested_by_token_sha256="b" * 64,
        reason="user wants a different hotel",
        request_id="override-1",
    )
    updated, _ = service.resolve_override("override-1", apply=True, resolved_at=NOW)

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        after,
        diff,
        now=NOW,
        booking_ledger=updated,
    )

    check = next(
        c
        for c in report.checks
        if c.code is PackageInvariantCode.PROTECTED_COMPONENTS_PRESERVED
    )
    assert check.passed is True
    assert report.passed


def test_heterogeneous_package_reverifier_accepts_consistent_repair() -> None:
    before, after, diff = _candidates()

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        after,
        diff,
        now=NOW,
    )

    assert report.passed
    assert len(report.checks) == 14
    assert report.failed_codes == ()
    assert "不调用 PackageVerifier" in report.semantics_boundary
    assert "形式化证明" in report.semantics_boundary


def test_reverifier_uses_actual_arrival_dates_and_keeps_foreign_lodging_separate() -> None:
    intent = _intent()
    actual_arrival = datetime(2026, 8, 24, 18, 35, tzinfo=MALDIVES)
    flight = _flight().model_copy(update={"outbound_arrive_at": actual_arrival})
    transfers = (
        _transfer(
            "transfer:actual-arrival",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            actual_arrival.date(),
        ),
        _transfer(
            "transfer:actual-return",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            END,
        ),
    )
    before_lodging = _lodging("lodging:usd-before", 105_826).model_copy(
        update={
            "check_in": actual_arrival.date(),
            "currency": "USD",
        }
    )
    after_lodging = before_lodging.model_copy(
        update={
            "id": "lodging:usd-after",
            "property_name": "Airport Island Hotel (updated capture)",
        }
    )
    before = TravelPackageCandidate(
        id="audit-trip:package:actual-dates:v1",
        trip_id=intent.trip_id,
        kind=PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
        flight=flight,
        lodgings=(before_lodging,),
        transfers=transfers,
        declared_total_cents=920_000,
    )
    after = before.model_copy(
        update={
            "id": "audit-trip:package:actual-dates:v2",
            "version": 2,
            "parent_candidate_id": before.id,
            "lodgings": (after_lodging,),
        }
    )

    report = DeclarativePackageReVerifier().audit(
        intent,
        before,
        after,
        diff_packages(before, after),
        now=NOW,
    )

    assert report.passed
    assert next(
        check for check in report.checks
        if check.code is PackageInvariantCode.LODGING_NIGHT_COVERAGE
    ).passed
    assert next(
        check for check in report.checks
        if check.code is PackageInvariantCode.TOTAL_ARITHMETIC_AND_BUDGET
    ).passed


@pytest.mark.parametrize(
    "intent_update",
    (
        {"allow_connections": False},
        {"require_checked_baggage": True},
        {"require_breakfast": True},
        {"require_breakfast": False},
    ),
)
def test_independent_audit_recomputes_explicit_hard_preferences(
    intent_update: dict[str, bool],
) -> None:
    before, after, diff = _candidates()

    report = DeclarativePackageReVerifier().audit(
        _intent().model_copy(update=intent_update),
        before,
        after,
        diff,
        now=NOW,
    )

    assert not report.passed
    assert PackageInvariantCode.HARD_PREFERENCES in report.failed_codes


def test_missing_all_required_transfers_fails_closed_in_both_verifiers() -> None:
    before, after, _ = _candidates()
    before = before.model_copy(
        update={
            "transfers": (),
            "declared_total_cents": (
                before.flight.total_for_party_cents
                + sum(item.total_for_party_cents for item in before.lodgings)
            ),
        }
    )
    after = after.model_copy(
        update={
            "transfers": (),
            "declared_total_cents": (
                after.flight.total_for_party_cents
                + sum(item.total_for_party_cents for item in after.lodgings)
            ),
        }
    )

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        after,
        diff_packages(before, after),
        now=NOW,
    )
    primary_codes = {
        item.code for item in PackageVerifier().errors(_intent(), after, now=NOW)
    }

    assert PackageInvariantCode.TRANSFER_CHAIN_AND_CONNECTIONS in report.failed_codes
    assert PackageViolationCode.TRANSFER_CHAIN_INCOMPLETE in primary_codes


def test_kind_lodging_structure_is_enforced_by_contract_and_both_verifiers() -> None:
    before, after, _ = _candidates()
    before = before.model_copy(
        update={
            "lodgings": tuple(
                item.model_copy(update={"area": PackageArea.DESTINATION_ISLAND})
                for item in before.lodgings
            )
        }
    )
    after = after.model_copy(
        update={
            "lodgings": tuple(
                item.model_copy(update={"area": PackageArea.DESTINATION_ISLAND})
                for item in after.lodgings
            )
        }
    )

    with pytest.raises(ValueError, match="lodging segments"):
        TravelPackageCandidate.model_validate(after.model_dump())

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        after,
        diff_packages(before, after),
        now=NOW,
    )
    primary_codes = {
        item.code for item in PackageVerifier().errors(_intent(), after, now=NOW)
    }

    assert PackageInvariantCode.LODGING_KIND_STRUCTURE in report.failed_codes
    assert PackageViolationCode.LODGING_STRUCTURE_MISMATCH in primary_codes


def test_round_trip_contract_cannot_be_reused_for_more_than_reciprocal_pair() -> None:
    before, after, _ = _candidates()
    duplicate = after.transfers[0].model_copy(update={"id": "transfer:third-free-leg"})
    malformed = after.model_copy(update={"transfers": (*after.transfers, duplicate)})

    with pytest.raises(ValueError, match="exactly two legs"):
        TravelPackageCandidate.model_validate(malformed.model_dump())

    report = DeclarativePackageReVerifier().audit(
        _intent(),
        before,
        malformed,
        diff_packages(before, malformed),
        now=NOW,
    )
    primary_codes = {
        item.code for item in PackageVerifier().errors(_intent(), malformed, now=NOW)
    }

    assert PackageInvariantCode.TRANSFER_PRICE_CONTRACTS in report.failed_codes
    assert PackageViolationCode.TRANSFER_PRICE_CONTRACT_INVALID in primary_codes


def _published_direct_candidate(
    candidate: TravelPackageCandidate,
) -> TravelPackageCandidate:
    lodging = candidate.lodgings[0].model_copy(
        update={
            "area": PackageArea.DESTINATION_ISLAND,
            "place_key": PackagePlaceKey.MAAFUSHI,
        }
    )
    transfers: list[TransferOption] = []
    for index, transfer in enumerate(candidate.transfers):
        outbound = index == 0
        transfers.append(
            transfer.model_copy(
                update={
                    "origin_area": (
                        PackageArea.AIRPORT
                        if outbound
                        else PackageArea.DESTINATION_ISLAND
                    ),
                    "destination_area": (
                        PackageArea.DESTINATION_ISLAND
                        if outbound
                        else PackageArea.AIRPORT
                    ),
                    "origin_place_key": (
                        PackagePlaceKey.VELANA_AIRPORT
                        if outbound
                        else PackagePlaceKey.MAAFUSHI
                    ),
                    "destination_place_key": (
                        PackagePlaceKey.MAAFUSHI
                        if outbound
                        else PackagePlaceKey.VELANA_AIRPORT
                    ),
                    "price_guarantee": TransferPriceGuarantee.PUBLISHED_BASE_FARE,
                    "taxes_and_fees_included": None,
                }
            )
        )
    confirmed_subtotal = (
        candidate.flight.total_for_party_cents + lodging.total_for_party_cents
    )
    return candidate.model_copy(
        update={
            "kind": PackageCandidateKind.CONTINUOUS_ISLAND,
            "lodgings": (lodging,),
            "transfers": tuple(transfers),
            "declared_total_cents": confirmed_subtotal,
        }
    )


def test_same_currency_published_base_fare_enters_hard_budget_lower_bound() -> None:
    original_before, original_after, _ = _candidates()
    before = _published_direct_candidate(original_before)
    after = _published_direct_candidate(original_after)
    known_base_fare = after.transfers[0].total_for_party_cents
    intent = _intent().model_copy(
        update={
            "destination_place_key": PackagePlaceKey.MAAFUSHI,
            "budget_cents": after.declared_total_cents + known_base_fare - 1,
        }
    )
    validated = TravelPackageCandidate.model_validate(after.model_dump())

    report = DeclarativePackageReVerifier().audit(
        intent,
        before,
        validated,
        diff_packages(before, validated),
        now=NOW,
    )
    primary_codes = {
        item.code for item in PackageVerifier().errors(intent, validated, now=NOW)
    }

    assert PackageInvariantCode.TOTAL_ARITHMETIC_AND_BUDGET in report.failed_codes
    assert PackageViolationCode.BUDGET_EXCEEDED in primary_codes


class _AlwaysPassPackageVerifier:
    def verify(self, *_: object, **__: object) -> tuple[()]:
        return ()


@pytest.mark.parametrize(
    ("tamper", "expected_code"),
    (
        ("amount", PackageInvariantCode.TOTAL_ARITHMETIC_AND_BUDGET),
        ("lineage", PackageInvariantCode.VERSION_LINEAGE),
        ("diff", PackageInvariantCode.DECLARED_DIFF_MATCHES),
    ),
)
@pytest.mark.asyncio
async def test_independent_audit_blocks_even_when_primary_verifier_is_stubbed_to_pass(
    tamper: str,
    expected_code: PackageInvariantCode,
) -> None:
    before, valid_after, valid_diff = _candidates()
    if tamper == "amount":
        after = valid_after.model_copy(update={"declared_total_cents": 1})
        diff = valid_diff
    elif tamper == "lineage":
        after = valid_after.model_copy(
            update={"version": before.version, "parent_candidate_id": None}
        )
        diff = valid_diff
    else:
        after = valid_after
        diff = valid_diff.model_copy(
            update={"added_component_ids": ("lodging:forged",)}
        )

    initial_verification = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=before,
        violations=(),
        verified_at=NOW,
    )
    repair_handoff = PackageRepairHandoff(
        rejected_candidate_id=before.id,
        attempted=False,
        agent_strategy_applied=True,
        outcome=PackageRepairOutcome(
            candidate=after,
            diff=diff,
            message="stubbed primary verifier allowed a repaired candidate",
        ),
    )
    state = _RunState(
        source_task_ids=(),
        planner_handoff=PackagePlannerHandoff(
            candidates=(before,),
            selected_candidate_id=before.id,
        ),
        initial_candidate=before,
        initial_verification_handoff=initial_verification,
        repair=repair_handoff.outcome,
        repair_handoff=repair_handoff,
    )
    system = LivePackageAgentSystem(
        BrowserTaskBridge(now=lambda: NOW),
        now=lambda: NOW,
    )
    system._verifier = _AlwaysPassPackageVerifier()  # type: ignore[assignment]
    context = ContextEngine(EvidenceBlackboard())
    tools = ToolRegistry()

    reverify_result = await system._verifier_executor(state, _intent())(
        AgentTask(
            id="reverify-travel-package",
            role=AgentRole.HARD_VERIFIER,
            goal="counterexample reverification",
        ),
        context,
        tools,
    )

    assert reverify_result.output["hard_error_count"] == 0
    assert reverify_result.output["independent_audit_passed"] is False
    assert state.reverification_handoff is not None
    assert state.reverification_handoff.errors == ()
    assert state.package_reverification_audit is not None
    assert expected_code in state.package_reverification_audit.failed_codes

    orchestrator_result = await system._orchestrator_executor(
        state,
        _intent(),
        LiveCoverageMode.DEGRADED,
    )(
        AgentTask(
            id="orchestrate-travel-package",
            role=AgentRole.ORCHESTRATOR,
            goal="counterexample safety gate",
        ),
        context,
        tools,
    )

    assert state.decision is not None
    assert state.decision.state == PackageDecisionState.HUMAN_BLOCK
    assert "异构确定性 ReVerifier" in state.decision.summary
    assert orchestrator_result.output["decision"] == PackageDecisionState.HUMAN_BLOCK.value
    assert orchestrator_result.output["independent_audit_passed"] is False
