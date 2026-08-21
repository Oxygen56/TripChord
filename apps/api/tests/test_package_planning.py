from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import cast

import pytest
from tripchord.agents.models import PreferenceMode
from tripchord.planning.package import (
    FlightGroundTransferContract,
    NormalizedFlightQuote,
    NormalizedFlightSegment,
    NormalizedLodgingQuote,
    PackageArea,
    PackageCandidateKind,
    PackageDecisionState,
    PackageEvent,
    PackageEventKind,
    PackageEventPlanningHandoff,
    PackageEventRepairHandoff,
    PackageIntent,
    PackageInventory,
    PackageOrchestrator,
    PackagePlaceKey,
    PackagePlanner,
    PackagePlannerHandoff,
    PackagePlanningHandoff,
    PackagePreferenceApplicationState,
    PackageRepairer,
    PackageRepairHandoff,
    PackageRepairOutcome,
    PackageRepairPlanStrategy,
    PackageVerificationHandoff,
    PackageVerificationPhase,
    PackageVerifier,
    PackageViolation,
    PackageViolationCode,
    PackageViolationSeverity,
    QuoteAvailability,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
    TravelPackageCandidate,
)

MALDIVES = timezone(timedelta(hours=5))
CAPTURED = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
VERIFY_AT = datetime(2026, 7, 30, 9, 30, tzinfo=UTC)
_UNSET_PLACE_KEY = object()


def test_flight_publishability_requires_party_total_and_airport_segments() -> None:
    outbound_depart = datetime(2026, 8, 23, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    outbound_arrive = datetime(2026, 8, 23, 18, 35, tzinfo=MALDIVES)
    return_depart = datetime(2026, 8, 30, 10, 45, tzinfo=MALDIVES)
    return_arrive = datetime(2026, 8, 31, 15, 40, tzinfo=timezone(timedelta(hours=8)))
    complete = flight().model_copy(
        update={
            "origin_airport_code": "HGH",
            "destination_airport_code": "MLE",
            "outbound_flight_numbers": ("MU501",),
            "return_flight_numbers": ("MU502",),
            "outbound_segments": (
                NormalizedFlightSegment(
                    flight_number="MU501",
                    departure_airport_code="HGH",
                    arrival_airport_code="MLE",
                    departure_at=outbound_depart,
                    arrival_at=outbound_arrive,
                ),
            ),
            "return_segments": (
                NormalizedFlightSegment(
                    flight_number="MU502",
                    departure_airport_code="MLE",
                    arrival_airport_code="HGH",
                    departure_at=return_depart,
                    arrival_at=return_arrive,
                ),
            ),
        },
    )
    assert complete.has_publishable_execution_contract is True
    assert complete.model_copy(
        update={"party_total_known": False, "price_basis": "comparison_only"}
    ).has_publishable_execution_contract is False
    assert complete.model_copy(
        update={"outbound_segments": (), "return_segments": ()}
    ).has_publishable_execution_contract is False
    cross_airport = complete.model_copy(
        update={
            "outbound_flight_numbers": ("MU501", "MU503"),
            "outbound_segments": (
                    complete.outbound_segments[0].model_copy(
                        update={
                            "arrival_airport_code": "PEK",
                            "arrival_at": datetime(
                                2026,
                                8,
                                23,
                                10,
                                30,
                                tzinfo=timezone(timedelta(hours=8)),
                            ),
                        }
                    ),
                    NormalizedFlightSegment(
                        flight_number="MU503",
                        departure_airport_code="PKX",
                        arrival_airport_code="MLE",
                        departure_at=datetime(
                            2026,
                            8,
                            23,
                            11,
                            0,
                            tzinfo=timezone(timedelta(hours=8)),
                        ),
                        arrival_at=outbound_arrive,
                    ),
                ),
        }
    )
    assert cross_airport.has_publishable_execution_contract is False
    assert cross_airport.model_copy(
        update={
            "outbound_ground_transfers": (
                FlightGroundTransferContract(
                    from_airport_code="PEK",
                    to_airport_code="PKX",
                    mode="audited ground transfer",
                    minimum_buffer_minutes=30,
                    actual_buffer_minutes=30,
                    baggage_recheck_required=True,
                    through_ticket_protected=True,
                    evidence_refs=("evidence:ground-transfer",),
                ),
            )
        }
    ).has_publishable_execution_contract is True


def intent(
    *,
    budget_cents: int | None = 1_600_000,
    require_checked_baggage: bool | None = False,
    require_breakfast: bool | None = None,
    breakfast_preference_mode: PreferenceMode | None = None,
    breakfast_preference_weight: float | None = None,
    destination_place_key: PackagePlaceKey | None = None,
    allow_connections: bool | None = None,
    require_non_basic_lodging: bool = False,
    maximum_quote_capture_skew_minutes: int = 20,
) -> PackageIntent:
    return PackageIntent(
        trip_id="hgh-mle-20260823",
        origin="HGH",
        destination="MLE",
        destination_place_key=destination_place_key,
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        rooms=1,
        budget_cents=budget_cents,
        require_checked_baggage=require_checked_baggage,
        allow_connections=allow_connections,
        require_breakfast=require_breakfast,
        require_non_basic_lodging=require_non_basic_lodging,
        breakfast_preference_mode=breakfast_preference_mode,
        breakfast_preference_weight=breakfast_preference_weight,
        minimum_arrival_to_boat_minutes=120,
        minimum_airport_buffer_minutes=180,
        maximum_quote_capture_skew_minutes=maximum_quote_capture_skew_minutes,
    )


def flight(
    *,
    quote_id: str = "ctrip:flight:hgh-mle:v1",
    provider: str = "ctrip",
    total_cents: int = 938_400,
    adults: int = 2,
    party_availability_confirmed: bool = True,
    taxes_included: bool = True,
    baggage_kg: int | None = 0,
    expires_at: datetime = EXPIRES,
) -> NormalizedFlightQuote:
    return NormalizedFlightQuote(
        id=quote_id,
        provider=provider,
        total_for_party_cents=total_cents,
        taxes_and_fees_included=taxes_included,
        captured_at=CAPTURED,
        expires_at=expires_at,
        evidence_refs=(f"evidence:{quote_id}",),
        origin="HGH",
        destination="MLE",
        adults=adults,
        party_availability_confirmed=party_availability_confirmed,
        outbound_depart_at=datetime(2026, 8, 23, 8, 30, tzinfo=timezone(timedelta(hours=8))),
        outbound_arrive_at=datetime(2026, 8, 23, 18, 35, tzinfo=MALDIVES),
        return_depart_at=datetime(2026, 8, 30, 10, 45, tzinfo=MALDIVES),
        return_arrive_at=datetime(
            2026,
            8,
            31,
            15,
            40,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        checked_baggage_per_adult_kg=baggage_kg,
    )


def lodging(
    quote_id: str,
    property_name: str,
    area: PackageArea,
    check_in: date,
    check_out: date,
    total_cents: int,
    *,
    taxes_included: bool = True,
    breakfast: bool | None = True,
    adults: int = 2,
    expires_at: datetime = EXPIRES,
    availability: QuoteAvailability = QuoteAvailability.AVAILABLE,
    place_key: PackagePlaceKey | object | None = _UNSET_PLACE_KEY,
    room_name: str | None = None,
) -> NormalizedLodgingQuote:
    default_place_key = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
        PackageArea.AIRPORT_ISLAND: PackagePlaceKey.HULHUMALE,
        PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
    }[area]
    return NormalizedLodgingQuote(
        id=quote_id,
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=taxes_included,
        captured_at=CAPTURED,
        expires_at=expires_at,
        availability=availability,
        evidence_refs=(f"evidence:{quote_id}",),
        property_name=property_name,
        area=area,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        rooms=1,
        breakfast_included=breakfast,
        room_name=room_name,
        place_key=(
            default_place_key
            if place_key is _UNSET_PLACE_KEY
            else cast(PackagePlaceKey | None, place_key)
        ),
    )


def transfer(
    quote_id: str,
    origin: PackageArea,
    destination: PackageArea,
    depart_at: datetime,
    arrive_at: datetime,
    total_cents: int,
    *,
    currency: str = "CNY",
    taxes_and_fees_included: bool | None = True,
    price_guarantee: TransferPriceGuarantee = (TransferPriceGuarantee.ALL_IN_CONFIRMED),
    price_contract_id: str | None = None,
    price_scope: TransferPriceScope = TransferPriceScope.ONE_WAY,
    origin_place_key: PackagePlaceKey | None = None,
    destination_place_key: PackagePlaceKey | None = None,
) -> TransferOption:
    duration_minutes = int((arrive_at - depart_at).total_seconds() // 60)
    place_by_area = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
        PackageArea.AIRPORT_ISLAND: PackagePlaceKey.HULHUMALE,
        PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
    }
    return TransferOption(
        id=quote_id,
        provider="local-transfer",
        currency=currency,
        total_for_party_cents=total_cents,
        taxes_and_fees_included=taxes_and_fees_included,
        captured_at=CAPTURED,
        expires_at=EXPIRES,
        evidence_refs=(f"evidence:{quote_id}",),
        origin_area=origin,
        destination_area=destination,
        origin_place_key=origin_place_key or place_by_area[origin],
        destination_place_key=destination_place_key or place_by_area[destination],
        adults=2,
        service_date=depart_at.date(),
        schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
        duration_minutes=duration_minutes,
        depart_at=depart_at,
        arrive_at=arrive_at,
        operates_24_hours=False,
        requires_reservation=True,
        price_scope=price_scope,
        price_contract_id=price_contract_id or f"price:{quote_id}",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        price_guarantee=price_guarantee,
        contract_evidence_text=(
            f"单程 {origin.value} → {destination.value}，"
            f"{duration_minutes}分钟，"
            + (
                f"含税总价 {currency} {total_cents / 100:.2f}"
                if price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
                else (f"公开基础价 {currency} {total_cents / 100:.2f}，税费未知")
            )
        ),
        detail_url="https://hotels.ctrip.com/hotels/detail/transfer-fixture",
    )


def window_transfer(
    quote_id: str,
    origin: PackageArea,
    destination: PackageArea,
    service_date: date,
    total_cents: int,
    *,
    duration_minutes: int,
    price_contract_id: str,
) -> TransferOption:
    start = datetime.combine(
        service_date,
        datetime.min.time(),
        tzinfo=MALDIVES,
    )
    evidence = (
        f"往返 {origin.value} ↔ {destination.value}，24小时服务 UTC+05:00，"
        f"单程{duration_minutes}分钟，需提前预约，含税总价 CNY "
        f"{total_cents / 100:.2f}"
    )
    place_by_area = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
        PackageArea.AIRPORT_ISLAND: PackagePlaceKey.HULHUMALE,
        PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
    }
    return TransferOption(
        id=quote_id,
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=CAPTURED,
        expires_at=EXPIRES,
        evidence_refs=(f"evidence:{quote_id}",),
        origin_area=origin,
        destination_area=destination,
        origin_place_key=place_by_area[origin],
        destination_place_key=place_by_area[destination],
        adults=2,
        service_date=service_date,
        schedule_mode=TransferScheduleMode.SERVICE_WINDOW,
        duration_minutes=duration_minutes,
        service_window_start_at=start,
        service_window_end_at=start.replace(hour=23, minute=59),
        operates_24_hours=True,
        requires_reservation=True,
        price_scope=TransferPriceScope.ROUND_TRIP,
        price_contract_id=price_contract_id,
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        contract_evidence_text=evidence,
        detail_url="https://hotels.ctrip.com/hotels/detail/transfer-window-fixture",
    )


def published_maafushi_transfer(
    quote_id: str,
    origin: PackageArea,
    destination: PackageArea,
    depart_at: datetime,
    arrive_at: datetime,
    *,
    price_contract_id: str | None = None,
    price_scope: TransferPriceScope = TransferPriceScope.ONE_WAY,
) -> TransferOption:
    place_by_area = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
        PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
    }
    return transfer(
        quote_id,
        origin,
        destination,
        depart_at,
        arrive_at,
        6_000,
        currency="USD",
        taxes_and_fees_included=None,
        price_guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        price_contract_id=price_contract_id,
        price_scope=price_scope,
        origin_place_key=place_by_area[origin],
        destination_place_key=place_by_area[destination],
    )


def golden_inventory(
    *,
    middle_replacement: NormalizedLodgingQuote | None = None,
) -> PackageInventory:
    start = date(2026, 8, 23)
    end = date(2026, 8, 30)
    first_checkout = start + timedelta(days=1)
    last_checkin = end - timedelta(days=1)
    lodgings = [
        lodging(
            "ctrip:kaani:direct",
            "Kaani Village & Spa",
            PackageArea.DESTINATION_ISLAND,
            start,
            end,
            471_100,
        ),
        lodging(
            "ctrip:terminal27:first",
            "Terminal 27",
            PackageArea.AIRPORT_ISLAND,
            start,
            first_checkout,
            39_600,
        ),
        lodging(
            "ctrip:kaani:middle",
            "Kaani Village & Spa",
            PackageArea.DESTINATION_ISLAND,
            first_checkout,
            last_checkin,
            336_500,
        ),
        lodging(
            "ctrip:terminal27:last",
            "Terminal 27",
            PackageArea.AIRPORT_ISLAND,
            last_checkin,
            end,
            39_600,
        ),
    ]
    if middle_replacement is not None:
        lodgings.append(middle_replacement)
    transfers = (
        transfer(
            "transfer:direct-out",
            PackageArea.AIRPORT,
            PackageArea.DESTINATION_ISLAND,
            datetime(2026, 8, 23, 19, 20, tzinfo=MALDIVES),
            datetime(2026, 8, 23, 20, 5, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "transfer:direct-back",
            PackageArea.DESTINATION_ISLAND,
            PackageArea.AIRPORT,
            datetime(2026, 8, 30, 7, 30, tzinfo=MALDIVES),
            datetime(2026, 8, 30, 8, 15, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "transfer:airport-hotel",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            datetime(2026, 8, 23, 19, 20, tzinfo=MALDIVES),
            datetime(2026, 8, 23, 19, 40, tzinfo=MALDIVES),
            10_800,
        ),
        transfer(
            "transfer:first-hotel-airport",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            datetime(2026, 8, 24, 6, 40, tzinfo=MALDIVES),
            datetime(2026, 8, 24, 7, 0, tzinfo=MALDIVES),
            10_800,
        ),
        transfer(
            "transfer:airport-destination-next-day",
            PackageArea.AIRPORT,
            PackageArea.DESTINATION_ISLAND,
            datetime(2026, 8, 24, 7, 30, tzinfo=MALDIVES),
            datetime(2026, 8, 24, 8, 15, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "transfer:destination-airport-day-before",
            PackageArea.DESTINATION_ISLAND,
            PackageArea.AIRPORT,
            datetime(2026, 8, 29, 16, 0, tzinfo=MALDIVES),
            datetime(2026, 8, 29, 16, 45, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "transfer:airport-last-hotel",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            datetime(2026, 8, 29, 17, 30, tzinfo=MALDIVES),
            datetime(2026, 8, 29, 17, 50, tzinfo=MALDIVES),
            10_800,
        ),
        transfer(
            "transfer:hotel-airport",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            datetime(2026, 8, 30, 6, 50, tzinfo=MALDIVES),
            datetime(2026, 8, 30, 7, 10, tzinfo=MALDIVES),
            10_800,
        ),
    )
    return PackageInventory(
        flights=(flight(),),
        lodgings=tuple(lodgings),
        transfers=transfers,
    )


def breakfast_ranking_inventory(
    *lodging_quotes: NormalizedLodgingQuote,
) -> PackageInventory:
    return PackageInventory(
        flights=(flight(total_cents=800_000),),
        lodgings=lodging_quotes,
        transfers=(
            transfer(
                "transfer:breakfast-rank-out",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
                datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
                36_000,
            ),
            transfer(
                "transfer:breakfast-rank-back",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
                36_000,
            ),
        ),
    )


def breakfast_stay(
    quote_id: str,
    *,
    total_cents: int,
    breakfast: bool | None,
) -> NormalizedLodgingQuote:
    return lodging(
        quote_id,
        quote_id,
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 23),
        date(2026, 8, 30),
        total_cents,
        breakfast=breakfast,
    )


def test_golden_case_verifier_rejects_then_repair_and_orchestrator_accepts() -> None:
    request = intent()
    inventory = golden_inventory()
    candidates = PackagePlanner().generate(request, inventory)
    direct = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )

    initial_codes = {item.code for item in PackageVerifier().verify(request, direct, now=VERIFY_AT)}
    assert initial_codes == {
        PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
        PackageViolationCode.EARLY_DEPARTURE_BUFFER,
    }

    result = PackageOrchestrator().execute(
        request,
        direct,
        inventory,
        now=VERIFY_AT,
    )

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.ACCEPT,
    ]
    assert result.final_decision.state == PackageDecisionState.ACCEPT
    assert result.final_candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
    assert result.final_candidate.flight.id == direct.flight.id
    assert [item.night_count for item in result.final_candidate.lodgings] == [1, 5, 1]
    assert result.final_violations == ()
    assert result.diff is not None
    assert direct.flight.id in result.diff.preserved_component_ids
    assert result.final_candidate.version == 2
    assert result.final_candidate.parent_candidate_id == direct.id
    assert result.budget.total_cents == 1_469_300
    assert result.budget.total_cents == result.final_candidate.computed_total_cents
    assert result.budget.formula == (
        "航班 ¥9384.00 + 住宿 ¥4157.00 + 接驳 ¥1152.00 = ¥14693.00（2名成人）"
    )
    assert result.evidence_refs == result.final_candidate.evidence_refs
    assert len(result.evidence_refs) == 10


def test_verifier_rejects_return_flight_that_arrives_after_home_deadline() -> None:
    request = intent().model_copy(update={"latest_arrival_date": date(2026, 8, 30)})
    inventory = golden_inventory()
    candidate = next(
        item
        for item in PackagePlanner().generate(request, inventory)
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )

    violations = PackageVerifier().verify(request, candidate, now=VERIFY_AT)

    assert any(
        item.code == PackageViolationCode.DATE_MISMATCH
        and "回杭边界" in item.message
        for item in violations
    )


def test_live_candidate_generation_is_bounded_audited_and_deterministic_under_pressure() -> None:
    request = intent()
    base = golden_inventory()
    seed_flight = base.flights[0]
    direct_out = next(item for item in base.transfers if item.id == "transfer:direct-out")
    direct_back = next(item for item in base.transfers if item.id == "transfer:direct-back")
    stressed_transfer_options = tuple(
        source.model_copy(
            update={
                "id": f"stress-{source.id}-{index:03d}",
                "provider": ("ctrip", "qunar", "tongcheng")[index % 3],
                "depart_at": source.depart_at + timedelta(minutes=index),
                "arrive_at": source.arrive_at + timedelta(minutes=index),
                "price_contract_id": f"stress-contract-{source.id}-{index:03d}",
                "total_for_party_cents": source.total_for_party_cents + index,
                "evidence_refs": (f"evidence:stress-{source.id}-{index:03d}",),
            }
        )
        for source in (direct_out, direct_back)
        for index in range(100)
    )
    stressed = base.model_copy(
        update={
            "flights": tuple(
                seed_flight.model_copy(
                    update={
                        "id": f"stress-flight-{index:04d}",
                        "provider": ("ctrip", "qunar", "tongcheng")[index % 3],
                        "provider_itinerary_id": f"itinerary-{index:04d}",
                        "total_for_party_cents": (
                            seed_flight.total_for_party_cents + index
                        ),
                        "evidence_refs": (f"evidence:stress-flight-{index:04d}",),
                    }
                )
                for index in range(1_000)
            ),
            "transfers": (*base.transfers, *stressed_transfer_options),
        }
    )
    planner = PackagePlanner()

    first = planner.generate_bounded(request, stressed, candidate_cap=10)
    second = planner.generate_bounded(request, stressed, candidate_cap=10)
    reordered = planner.generate_bounded(
        request,
        stressed.model_copy(
            update={
                "flights": tuple(reversed(stressed.flights)),
                "lodgings": tuple(reversed(stressed.lodgings)),
                "transfers": tuple(reversed(stressed.transfers)),
            }
        ),
        candidate_cap=10,
    )

    assert len(first.candidates) == 10
    assert first.audit.generated_candidate_count == 10
    assert first.audit.generation_candidate_cap == 10
    assert first.audit.transfer_beam_width == planner.LIVE_TRANSFER_BEAM_WIDTH
    assert first.audit.policy_version == "package-candidate-beam-v4"
    assert first.audit.selection_policy_version == "provider-flight-kind-reservation-v1"
    assert (
        first.audit.transfer_limit_per_contract_bucket
        == planner.LIVE_TRANSFER_LIMIT_PER_CONTRACT_BUCKET
    )
    assert first.audit.raw_inventory_counts["flights"] == 1_000
    assert first.audit.prescreened_inventory_counts["flights"] == planner.LIVE_FLIGHT_LIMIT
    assert first.audit.raw_inventory_counts["transfers"] > 200
    assert (
        first.audit.prescreened_inventory_counts["transfers"]
        < first.audit.raw_inventory_counts["transfers"]
    )
    assert (
        first.audit.raw_structural_candidate_upper_bound
        > first.audit.prescreened_structural_candidate_upper_bound
        > first.audit.generation_candidate_cap
    )
    assert first.audit.input_prescreen_pruned
    assert first.audit.generation_stopped_at_cap
    assert first.audit.prescreen_structure_scan_completed
    assert first.audit.structurally_joined_candidate_count > len(first.candidates)
    assert not first.audit.transfer_combinations_exhaustively_enumerated
    assert not first.audit.full_enumeration_claimed
    assert first.audit.generated_candidate_ids == tuple(
        item.id for item in first.candidates
    )
    assert "不得声称" in first.audit.omitted_scope
    assert first.audit.generation_proof_sha256 == second.audit.generation_proof_sha256
    assert first.audit.generation_proof_sha256 == reordered.audit.generation_proof_sha256
    assert first.audit.raw_inventory_ids_sha256 == reordered.audit.raw_inventory_ids_sha256
    assert (
        first.audit.prescreened_inventory_ids_sha256
        == reordered.audit.prescreened_inventory_ids_sha256
    )
    assert [item.id for item in first.candidates] == [item.id for item in second.candidates]
    assert [item.id for item in first.candidates] == [
        item.id for item in reordered.candidates
    ]
    assert len({item.flight.id for item in first.candidates}) == len(first.candidates)
    assert {item.flight.provider for item in first.candidates} == {
        "ctrip",
        "qunar",
        "tongcheng",
    }
    assert len({item.kind for item in first.candidates}) >= 2

    tampered = first.candidates[0].model_copy(
        update={
            "declared_total_cents": first.candidates[0].declared_total_cents + 1,
        }
    )
    assert PackageViolationCode.TOTAL_MISMATCH in {
        item.code for item in PackageVerifier().verify(request, tampered, now=VERIFY_AT)
    }


def test_candidate_generation_does_not_misreport_an_exact_size_pool_as_cap_pruned() -> None:
    request = intent()
    inventory = golden_inventory()
    planner = PackagePlanner()
    complete = planner.generate_bounded(request, inventory, candidate_cap=2_000)

    assert complete.candidates
    exact = planner.generate_bounded(
        request,
        inventory,
        candidate_cap=len(complete.candidates),
    )

    assert len(exact.candidates) == exact.audit.generation_candidate_cap
    assert not exact.audit.generation_stopped_at_cap
    assert exact.audit.prescreen_structure_scan_completed
    assert not exact.audit.transfer_combinations_exhaustively_enumerated
    assert not exact.audit.full_enumeration_claimed


def test_small_candidate_cap_reserves_platform_flight_and_kind_diversity() -> None:
    request = intent()
    base = golden_inventory()
    seed = base.flights[0]
    flights = tuple(
        seed.model_copy(
            update={
                "id": f"diverse-flight-{index}",
                "provider": provider,
                "provider_itinerary_id": f"diverse-itinerary-{index}",
                "total_for_party_cents": seed.total_for_party_cents + index * 100,
                "evidence_refs": (f"evidence:diverse-flight-{index}",),
            }
        )
        for index, provider in enumerate(("ctrip", "qunar", "tongcheng"))
    )
    inventory = base.model_copy(update={"flights": flights})
    planner = PackagePlanner()

    first = planner.generate_bounded(request, inventory, candidate_cap=3)
    complete = planner.generate_bounded(request, inventory, candidate_cap=2_000)
    reordered = planner.generate_bounded(
        request,
        inventory.model_copy(
            update={
                "flights": tuple(reversed(inventory.flights)),
                "lodgings": tuple(reversed(inventory.lodgings)),
                "transfers": tuple(reversed(inventory.transfers)),
            }
        ),
        candidate_cap=3,
    )

    assert len(first.candidates) == 3
    assert first.candidates[0].id == complete.candidates[0].id
    assert {candidate.id for candidate in first.candidates} <= {
        candidate.id for candidate in complete.candidates
    }
    assert len({candidate.flight.id for candidate in first.candidates}) == 3
    assert {candidate.flight.provider for candidate in first.candidates} == {
        "ctrip",
        "qunar",
        "tongcheng",
    }
    assert len({candidate.kind for candidate in first.candidates}) == 2
    assert first.audit.structurally_joined_candidate_count == 6
    assert first.audit.generation_stopped_at_cap
    assert first.audit.prescreen_structure_scan_completed
    assert first.audit.generated_candidate_ids == reordered.audit.generated_candidate_ids
    assert first.audit.generation_proof_sha256 == reordered.audit.generation_proof_sha256


def test_planner_can_mix_flight_and_lodging_from_different_platforms() -> None:
    request = intent()
    base = golden_inventory()
    ctrip_middle = next(
        stay for stay in base.lodgings if stay.id == "ctrip:kaani:middle"
    )
    qunar_middle = ctrip_middle.model_copy(
        update={
            "id": "qunar:kaani:middle",
            "provider": "qunar",
            "total_for_party_cents": ctrip_middle.total_for_party_cents - 25_000,
            "evidence_refs": ("evidence:qunar:kaani:middle",),
        }
    )
    inventory = base.model_copy(
        update={"lodgings": (*base.lodgings, qunar_middle)}
    )

    candidates = PackagePlanner().generate(request, inventory)
    selected = next(
        candidate
        for candidate in candidates
        if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
        and qunar_middle.id in candidate.component_ids
    )

    assert selected.flight.provider == "ctrip"
    assert {stay.provider for stay in selected.lodgings} == {"ctrip", "qunar"}
    assert PackageVerifier().errors(request, selected, now=VERIFY_AT) == ()
    assert selected.computed_total_cents == 1_444_300


def _golden_structured_handoff(
    *,
    reverification_violations: tuple[PackageViolation, ...] = (),
) -> tuple[PackageIntent, PackagePlanningHandoff]:
    request = intent()
    inventory = golden_inventory()
    candidates = PackagePlanner().generate(request, inventory)
    direct = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    initial_violations = PackageVerifier().verify(
        request,
        direct,
        now=VERIFY_AT,
    )
    repaired = PackageRepairer().repair_from_rejection(
        request,
        direct,
        candidates,
        initial_violations,
    )
    assert repaired.candidate is not None
    planner_handoff = PackagePlannerHandoff(
        candidates=candidates,
        selected_candidate_id=direct.id,
    )
    initial_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=direct,
        violations=initial_violations,
        verified_at=VERIFY_AT,
    )
    repair_handoff = PackageRepairHandoff(
        rejected_candidate_id=direct.id,
        rejection_error_codes=tuple(violation.code for violation in initial_handoff.errors),
        attempted=True,
        outcome=repaired,
    )
    reverify_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.REVERIFICATION,
        candidate=repaired.candidate,
        violations=reverification_violations,
        verified_at=VERIFY_AT,
    )
    return request, PackagePlanningHandoff(
        planner=planner_handoff,
        initial_verification=initial_handoff,
        repair=repair_handoff,
        reverification=reverify_handoff,
    )


def test_repair_handoff_cannot_silently_drop_or_bypass_verifier_rejection() -> None:
    request = intent()
    candidates = PackagePlanner().generate(request, golden_inventory())
    direct = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    violations = PackageVerifier().verify(request, direct, now=VERIFY_AT)
    initial_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=direct,
        violations=violations,
        verified_at=VERIFY_AT,
    )

    with pytest.raises(ValueError, match="exactly match verifier hard errors"):
        PackagePlanningHandoff(
            planner=PackagePlannerHandoff(
                candidates=candidates,
                selected_candidate_id=direct.id,
            ),
            initial_verification=initial_handoff,
            repair=PackageRepairHandoff(
                rejected_candidate_id=direct.id,
                rejection_error_codes=(),
                attempted=False,
                outcome=PackageRepairOutcome(
                    candidate=direct,
                    diff=None,
                    message="恶意跳过拒绝",
                ),
            ),
            reverification=PackageVerificationHandoff.from_candidate(
                phase=PackageVerificationPhase.REVERIFICATION,
                candidate=direct,
                violations=(),
                verified_at=VERIFY_AT,
            ),
        )

    with pytest.raises(ValueError, match="silently reuse a rejected candidate"):
        PackageRepairHandoff(
            rejected_candidate_id=direct.id,
            rejection_error_codes=tuple(violation.code for violation in initial_handoff.errors),
            attempted=True,
            outcome=PackageRepairOutcome(
                candidate=direct,
                diff=None,
                message="伪造已修复",
            ),
        )


def test_master_consumes_reverification_handoff_without_rerunning_verifier() -> None:
    class ExplodingVerifier(PackageVerifier):
        def verify(
            self,
            intent: PackageIntent,
            candidate: TravelPackageCandidate,
            *,
            now: datetime | None = None,
        ) -> tuple[PackageViolation, ...]:
            raise AssertionError("master must not rerun verifier")

    request, accepted_handoff = _golden_structured_handoff()
    orchestrator = PackageOrchestrator(verifier=ExplodingVerifier())

    accepted = orchestrator.decide_from_handoff(request, accepted_handoff)

    assert accepted.final_decision.state == PackageDecisionState.ACCEPT
    assert accepted.planning_handoff == accepted_handoff
    assert accepted.final_candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND

    injected_rejection = PackageViolation(
        code=PackageViolationCode.BUDGET_EXCEEDED,
        severity=PackageViolationSeverity.ERROR,
        message="ReVerifier 注入的反例硬错误",
        component_ids=accepted.final_candidate.component_ids,
    )
    _, blocked_handoff = _golden_structured_handoff(
        reverification_violations=(injected_rejection,),
    )

    blocked = orchestrator.decide_from_handoff(request, blocked_handoff)

    assert blocked.final_candidate.component_ids == accepted.final_candidate.component_ids
    assert blocked.final_decision.state == PackageDecisionState.HUMAN_BLOCK
    assert blocked.final_decision.violation_codes == (PackageViolationCode.BUDGET_EXCEEDED,)
    assert "ReVerifier" in blocked.final_decision.summary


def test_weighted_breakfast_preference_changes_only_soft_candidate_ranking() -> None:
    inventory = breakfast_ranking_inventory(
        breakfast_stay(
            "stay:room-only",
            total_cents=300_000,
            breakfast=False,
        ),
        breakfast_stay(
            "stay:breakfast",
            total_cents=350_000,
            breakfast=True,
        ),
    )
    high_weight = intent(
        budget_cents=2_000_000,
        require_breakfast=None,
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.8,
    )
    low_weight = high_weight.model_copy(update={"breakfast_preference_weight": 0.2})

    high_candidates = PackagePlanner().generate(high_weight, inventory)
    low_candidates = PackagePlanner().generate(low_weight, inventory)

    assert high_candidates[0].lodgings[0].id == "stay:breakfast"
    assert low_candidates[0].lodgings[0].id == "stay:room-only"
    result = PackageOrchestrator().execute(
        high_weight,
        high_candidates[0],
        inventory,
        now=VERIFY_AT,
    )
    application = result.preference_applications[0]
    assert result.final_decision.state == PackageDecisionState.ACCEPT
    assert application.state == PackagePreferenceApplicationState.APPLIED
    assert application.mode == PreferenceMode.WEIGHTED
    assert application.weight == 0.8
    assert application.comparable_candidate_count == 2
    assert application.selected_breakfast_coverage == Decimal(1)
    assert application.selected_breakfast_evidence_complete


def test_non_basic_lodging_is_hard_filtered_and_equal_price_prefers_sea_view() -> None:
    no_window = lodging(
        "stay:no-window",
        "Island Hotel",
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 23),
        date(2026, 8, 30),
        338_000,
        room_name="标准双人房（无窗）",
    )
    city_view = lodging(
        "stay:family-city-balcony",
        "Island Hotel",
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 23),
        date(2026, 8, 30),
        493_500,
        room_name="豪华家庭城景阳台房",
    )
    sea_view = lodging(
        "stay:double-sea-balcony",
        "Island Hotel",
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 23),
        date(2026, 8, 30),
        493_500,
        room_name="豪华双人海景阳台房",
    )
    inventory = breakfast_ranking_inventory(no_window, city_view, sea_view)
    required = intent(
        budget_cents=2_000_000,
        require_non_basic_lodging=True,
    )

    candidates = PackagePlanner().generate(required, inventory)

    assert [item.lodgings[0].id for item in candidates] == [
        sea_view.id,
        city_view.id,
    ]
    unsafe = next(
        item
        for item in PackagePlanner().generate(
            required.model_copy(update={"require_non_basic_lodging": False}),
            inventory,
        )
        if item.lodgings[0].id == no_window.id
    )
    assert PackageViolationCode.LODGING_QUALITY_PREFERENCE in {
        item.code for item in PackageVerifier().errors(required, unsafe, now=VERIFY_AT)
    }


def test_weighted_breakfast_never_promotes_incomplete_foreign_total_over_complete_cny() -> None:
    inventory = breakfast_ranking_inventory(
        breakfast_stay("stay:subtotal", total_cents=250_000, breakfast=True),
        breakfast_stay("stay:complete", total_cents=300_000, breakfast=False),
    )
    request = intent(
        budget_cents=2_000_000,
        require_breakfast=None,
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.9,
    )
    candidates = PackagePlanner().generate(request, inventory)
    incomplete = candidates[0].model_copy(
        update={
            "id": "candidate:foreign-subtotal",
            "transfers": tuple(
                transfer.model_copy(
                    update={"currency": "USD", "taxes_and_fees_included": None}
                )
                for transfer in candidates[0].transfers
            ),
        }
    )
    complete = candidates[1].model_copy(update={"id": "candidate:complete-cny"})

    ranked = PackagePlanner().rank_candidates(request, (incomplete, complete))

    assert ranked[0].id == "candidate:complete-cny"


def test_unknown_breakfast_gets_no_soft_preference_reward() -> None:
    inventory = breakfast_ranking_inventory(
        breakfast_stay(
            "stay:unknown-breakfast",
            total_cents=300_000,
            breakfast=None,
        ),
        breakfast_stay(
            "stay:confirmed-breakfast",
            total_cents=350_000,
            breakfast=True,
        ),
    )
    request = intent(
        budget_cents=2_000_000,
        require_breakfast=None,
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.9,
    )

    candidates = PackagePlanner().generate(request, inventory)

    assert candidates[0].lodgings[0].id == "stay:confirmed-breakfast"
    assert candidates[1].lodgings[0].breakfast_included is None


def test_weighted_breakfast_reports_not_applied_without_a_comparison() -> None:
    inventory = breakfast_ranking_inventory(
        breakfast_stay(
            "stay:only-option",
            total_cents=300_000,
            breakfast=True,
        )
    )
    request = intent(
        budget_cents=2_000_000,
        require_breakfast=None,
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.9,
    )
    candidate = PackagePlanner().generate(request, inventory)[0]

    result = PackageOrchestrator().execute(
        request,
        candidate,
        inventory,
        now=VERIFY_AT,
    )

    application = result.preference_applications[0]
    assert application.state == PackagePreferenceApplicationState.NOT_APPLIED
    assert application.comparable_candidate_count == 1
    assert "只有一个可比候选" in application.reason


def test_forbidden_breakfast_is_hard_and_unknown_does_not_satisfy_it() -> None:
    inventory = breakfast_ranking_inventory(
        breakfast_stay(
            "stay:unknown",
            total_cents=280_000,
            breakfast=None,
        ),
        breakfast_stay(
            "stay:included",
            total_cents=290_000,
            breakfast=True,
        ),
        breakfast_stay(
            "stay:confirmed-room-only",
            total_cents=320_000,
            breakfast=False,
        ),
    )
    request = intent(
        budget_cents=2_000_000,
        require_breakfast=False,
        breakfast_preference_mode=PreferenceMode.FORBIDDEN,
        breakfast_preference_weight=1,
    )
    initial = PackagePlanner().generate(request, inventory)[0]

    initial_errors = PackageVerifier().errors(
        request,
        initial,
        now=VERIFY_AT,
    )
    assert initial.lodgings[0].breakfast_included is None
    assert {item.code for item in initial_errors} == {PackageViolationCode.BREAKFAST_PREFERENCE}

    result = PackageOrchestrator().execute(
        request,
        initial,
        inventory,
        now=VERIFY_AT,
    )

    assert result.final_decision.state == PackageDecisionState.ACCEPT
    assert result.final_candidate.lodgings[0].id == "stay:confirmed-room-only"
    assert (
        result.preference_applications[0].state == PackagePreferenceApplicationState.HARD_CONSTRAINT
    )


def test_breakfast_mode_cannot_smuggle_soft_weight_into_a_hard_constraint() -> None:
    with pytest.raises(ValueError, match="must not create a hard constraint"):
        intent(
            require_breakfast=True,
            breakfast_preference_mode=PreferenceMode.WEIGHTED,
            breakfast_preference_weight=1,
        )


def test_verifier_checks_party_tax_freshness_nights_preferences_and_budget() -> None:
    request = intent(
        budget_cents=1_000_000,
        require_checked_baggage=True,
        require_breakfast=True,
    )
    inventory = golden_inventory()
    direct = next(
        item
        for item in PackagePlanner().generate(intent(), inventory)
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    broken_flight = flight(
        adults=1,
        taxes_included=False,
        baggage_kg=0,
        expires_at=datetime(2026, 7, 30, 9, 15, tzinfo=UTC),
    )
    broken_lodging = lodging(
        "ctrip:kaani:broken",
        "Kaani Village & Spa",
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 23),
        date(2026, 8, 29),
        471_100,
        taxes_included=False,
        breakfast=False,
    )
    broken = direct.model_copy(
        update={
            "flight": broken_flight,
            "lodgings": (broken_lodging,),
            "declared_total_cents": 1,
        }
    )

    codes = {item.code for item in PackageVerifier().verify(request, broken, now=VERIFY_AT)}

    assert {
        PackageViolationCode.PARTY_MISMATCH,
        PackageViolationCode.TOTAL_MISMATCH,
        PackageViolationCode.TAXES_INCOMPLETE,
        PackageViolationCode.STALE_QUOTE,
        PackageViolationCode.LODGING_NIGHT_COVERAGE,
        PackageViolationCode.BAGGAGE_PREFERENCE,
        PackageViolationCode.BREAKFAST_PREFERENCE,
        PackageViolationCode.BUDGET_EXCEEDED,
    } <= codes


def test_unknown_baggage_only_blocks_when_user_explicitly_requires_checked_baggage() -> None:
    inventory = golden_inventory()
    direct = next(
        item
        for item in PackagePlanner().generate(intent(), inventory)
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    unknown = direct.model_copy(
        update={"flight": direct.flight.model_copy(update={"checked_baggage_per_adult_kg": None})}
    )
    explicit = direct.model_copy(
        update={"flight": direct.flight.model_copy(update={"checked_baggage_per_adult_kg": 23})}
    )

    optional_codes = {
        item.code for item in PackageVerifier().verify(intent(), unknown, now=VERIFY_AT)
    }
    required_unknown_codes = {
        item.code
        for item in PackageVerifier().verify(
            intent(require_checked_baggage=True),
            unknown,
            now=VERIFY_AT,
        )
    }
    required_explicit_codes = {
        item.code
        for item in PackageVerifier().verify(
            intent(require_checked_baggage=True),
            explicit,
            now=VERIFY_AT,
        )
    }

    assert PackageViolationCode.BAGGAGE_PREFERENCE not in optional_codes
    assert PackageViolationCode.BAGGAGE_PREFERENCE in required_unknown_codes
    assert PackageViolationCode.BAGGAGE_PREFERENCE not in required_explicit_codes


def test_no_connection_preference_fails_closed_on_missing_or_multi_segment_evidence() -> None:
    request = intent(allow_connections=False)
    direct = next(
        item
        for item in PackagePlanner().generate(intent(), golden_inventory())
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    missing_codes = {
        item.code for item in PackageVerifier().verify(request, direct, now=VERIFY_AT)
    }
    connecting = direct.model_copy(
        update={
            "flight": direct.flight.model_copy(
                update={
                    "outbound_flight_numbers": ("CZ123", "CZ456"),
                    "return_flight_numbers": ("CZ789",),
                }
            )
        }
    )
    connecting_codes = {
        item.code for item in PackageVerifier().verify(request, connecting, now=VERIFY_AT)
    }
    nonstop = direct.model_copy(
        update={
            "flight": direct.flight.model_copy(
                update={
                    "outbound_flight_numbers": ("CZ123",),
                    "return_flight_numbers": ("CZ789",),
                }
            )
        }
    )
    nonstop_codes = {
        item.code for item in PackageVerifier().verify(request, nonstop, now=VERIFY_AT)
    }

    assert PackageViolationCode.CONNECTION_PREFERENCE in missing_codes
    assert PackageViolationCode.CONNECTION_PREFERENCE in connecting_codes
    assert PackageViolationCode.CONNECTION_PREFERENCE not in nonstop_codes


def test_cross_platform_package_rejects_excessive_capture_time_skew() -> None:
    request = intent(maximum_quote_capture_skew_minutes=20)
    direct = next(
        item
        for item in PackagePlanner().generate(request, golden_inventory())
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    late_capture = CAPTURED + timedelta(minutes=21)
    shifted_lodging = direct.lodgings[0].model_copy(
        update={
            "captured_at": late_capture,
            "expires_at": late_capture + timedelta(hours=1),
        }
    )
    skewed = direct.model_copy(update={"lodgings": (shifted_lodging,)})

    violations = PackageVerifier().verify(request, skewed, now=VERIFY_AT)
    capture_violation = next(
        item for item in violations if item.code == PackageViolationCode.QUOTE_CAPTURE_SKEW
    )

    assert capture_violation.details["capture_skew_seconds"] == 21 * 60
    assert capture_violation.details["maximum_capture_skew_seconds"] == 20 * 60


def test_24h_round_trip_repairs_fragile_direct_without_double_charge() -> None:
    request = intent()
    inventory = golden_inventory()
    first_checkout = request.start_date + timedelta(days=1)
    last_checkin = request.end_date - timedelta(days=1)
    first_airport_price = "transfer-price:first-airport-hotel-round-trip"
    last_airport_price = "transfer-price:last-airport-hotel-round-trip"
    island_price = "transfer-price:airport-island-destination-round-trip"
    window_transfers = (
        window_transfer(
            "window:airport-to-airport-island",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            request.start_date,
            10_800,
            duration_minutes=20,
            price_contract_id=first_airport_price,
        ),
        window_transfer(
            "window:first-hotel-to-airport",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            first_checkout,
            10_800,
            duration_minutes=20,
            price_contract_id=first_airport_price,
        ),
        window_transfer(
            "window:airport-to-destination",
            PackageArea.AIRPORT,
            PackageArea.DESTINATION_ISLAND,
            first_checkout,
            36_000,
            duration_minutes=45,
            price_contract_id=island_price,
        ),
        window_transfer(
            "window:destination-to-airport",
            PackageArea.DESTINATION_ISLAND,
            PackageArea.AIRPORT,
            last_checkin,
            36_000,
            duration_minutes=45,
            price_contract_id=island_price,
        ),
        window_transfer(
            "window:airport-to-last-hotel",
            PackageArea.AIRPORT,
            PackageArea.AIRPORT_ISLAND,
            last_checkin,
            10_800,
            duration_minutes=20,
            price_contract_id=last_airport_price,
        ),
        window_transfer(
            "window:last-hotel-to-airport",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.AIRPORT,
            request.end_date,
            10_800,
            duration_minutes=20,
            price_contract_id=last_airport_price,
        ),
    )
    enriched = inventory.model_copy(update={"transfers": (*inventory.transfers, *window_transfers)})
    candidates = PackagePlanner().generate(request, enriched)
    direct = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )

    assert {item.code for item in PackageVerifier().errors(request, direct, now=VERIFY_AT)} == {
        PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
        PackageViolationCode.EARLY_DEPARTURE_BUFFER,
    }

    result = PackageOrchestrator().execute(
        request,
        direct,
        enriched,
        now=VERIFY_AT,
    )

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.ACCEPT,
    ]
    assert result.final_candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
    assert result.final_violations == ()
    assert {item.price_contract_id for item in result.final_candidate.transfers} == {
        first_airport_price,
        island_price,
        last_airport_price,
    }
    assert result.budget.transfer_cents == 57_600
    assert result.budget.total_cents == 1_411_700


def test_price_event_replaces_only_affected_middle_stay_and_preserves_nine_tenths() -> None:
    request = intent()
    replacement = lodging(
        "fliggy:kaani:middle:repriced",
        "Kaani Village & Spa",
        PackageArea.DESTINATION_ISLAND,
        date(2026, 8, 24),
        date(2026, 8, 29),
        356_500,
    )
    inventory = golden_inventory(middle_replacement=replacement)
    initial_candidates = PackagePlanner().generate(request, inventory)
    before = next(
        item
        for item in initial_candidates
        if item.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
        and any(stay.id == "ctrip:kaani:middle" for stay in item.lodgings)
    )
    event = PackageEvent(
        id="price-change-kaani-1",
        kind=PackageEventKind.PRICE_CHANGED,
        target_component_id="ctrip:kaani:middle",
        replacement_component_id=replacement.id,
    )

    result = PackageOrchestrator().replan_after_event(
        request,
        before,
        event,
        inventory,
        now=VERIFY_AT,
    )

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.ACCEPT,
    ]
    assert result.final_violations == ()
    assert result.diff is not None
    assert result.diff.removed_component_ids == ("ctrip:kaani:middle",)
    assert result.diff.added_component_ids == (replacement.id,)
    assert result.preservation_ratio == Decimal("0.9")
    assert result.final_candidate.flight == before.flight
    assert result.final_candidate.transfers == before.transfers
    assert result.final_candidate.lodgings[0] == before.lodgings[0]
    assert result.final_candidate.lodgings[2] == before.lodgings[2]
    assert result.final_candidate.applied_event_ids == ("price-change-kaani-1",)
    assert result.budget.total_cents == before.computed_total_cents + 20_000

    repair_outcome = PackageRepairer().repair_event(before, event, inventory)
    repair_plan = repair_outcome.repair_plan
    assert repair_plan is not None
    assert repair_plan.strategy == PackageRepairPlanStrategy.LOCAL_REPAIR
    assert repair_plan.target_component_ids == ("ctrip:kaani:middle",)
    assert repair_plan.preserve_component_ids == tuple(
        component_id
        for component_id in before.component_ids
        if component_id != "ctrip:kaani:middle"
    )
    assert repair_plan.steps[-1].action == "independent_reverification"


def test_event_repair_requests_pool_expansion_when_replacement_is_missing() -> None:
    request = intent()
    inventory = golden_inventory()
    before = next(
        item
        for item in PackagePlanner().generate(request, inventory)
        if item.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
        and any(stay.id == "ctrip:kaani:middle" for stay in item.lodgings)
    )
    event = PackageEvent(
        id="sold-out-kaani-1",
        kind=PackageEventKind.SOLD_OUT,
        target_component_id="ctrip:kaani:middle",
        replacement_component_id="missing:compatible:lodging",
    )

    outcome = PackageRepairer().repair_event(before, event, inventory)

    assert outcome.candidate is None
    assert outcome.diff is None
    assert outcome.repair_plan is not None
    assert outcome.repair_plan.strategy == PackageRepairPlanStrategy.EXPAND_CANDIDATE_POOL
    assert outcome.repair_plan.candidate_pool_expansion_required is True
    assert outcome.repair_plan.requested_candidate_count == 5
    assert outcome.repair_plan.fallback_strategy == PackageRepairPlanStrategy.GLOBAL_REPLAN
    assert [step.action for step in outcome.repair_plan.steps] == [
        "preserve_unaffected_components",
        "expand_compatible_candidate_pool",
        "independent_reverification",
    ]


def test_non_fragile_hard_preferences_repair_to_cheapest_fully_valid_candidate() -> None:
    request = intent(
        budget_cents=2_000_000,
        require_checked_baggage=True,
        require_breakfast=True,
    )
    cheap_flight = flight(
        quote_id="flight:cheap:no-baggage",
        total_cents=800_000,
        baggage_kg=0,
    )
    valid_flight = flight(
        quote_id="flight:valid:baggage",
        total_cents=850_000,
        baggage_kg=23,
    )
    cheap_stay = lodging(
        "stay:cheap:no-breakfast",
        "Room Only",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        300_000,
        breakfast=False,
    )
    valid_stay = lodging(
        "stay:valid:breakfast",
        "Breakfast Included",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        350_000,
        breakfast=True,
    )
    safe_transfers = (
        transfer(
            "transfer:safe-out",
            PackageArea.AIRPORT,
            PackageArea.DESTINATION_ISLAND,
            datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
            datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "transfer:safe-back",
            PackageArea.DESTINATION_ISLAND,
            PackageArea.AIRPORT,
            datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
            datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
            36_000,
        ),
    )
    inventory = PackageInventory(
        flights=(cheap_flight, valid_flight),
        lodgings=(cheap_stay, valid_stay),
        transfers=safe_transfers,
    )
    initial = PackagePlanner().generate(request, inventory)[0]

    assert initial.flight.id == cheap_flight.id
    assert initial.lodgings[0].id == cheap_stay.id
    assert {item.code for item in PackageVerifier().errors(request, initial, now=VERIFY_AT)} == {
        PackageViolationCode.BAGGAGE_PREFERENCE,
        PackageViolationCode.BREAKFAST_PREFERENCE,
    }

    result = PackageOrchestrator().execute(
        request,
        initial,
        inventory,
        now=VERIFY_AT,
    )

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.ACCEPT,
    ]
    assert result.final_candidate.flight.id == valid_flight.id
    assert result.final_candidate.lodgings[0].id == valid_stay.id
    assert result.final_violations == ()


def _party_availability_inventory(
    *flight_quotes: NormalizedFlightQuote,
) -> PackageInventory:
    request = intent(budget_cents=2_000_000)
    stay = lodging(
        "stay:party-availability",
        "Availability Test Stay",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        300_000,
    )
    return PackageInventory(
        flights=flight_quotes,
        lodgings=(stay,),
        transfers=(
            transfer(
                "transfer:party-availability-out",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
                datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
                36_000,
            ),
            transfer(
                "transfer:party-availability-back",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
                36_000,
            ),
        ),
    )


def test_unconfirmed_fliggy_low_price_is_rejected_and_repaired_to_confirmed_flight() -> None:
    request = intent(budget_cents=2_000_000)
    fliggy_comparison_only = flight(
        quote_id="fliggy:flight:comparison-only",
        provider="fliggy",
        total_cents=700_000,
        party_availability_confirmed=False,
    )
    ctrip_confirmed = flight(
        quote_id="ctrip:flight:party-confirmed",
        provider="ctrip",
        total_cents=750_000,
        party_availability_confirmed=True,
    )
    inventory = _party_availability_inventory(
        fliggy_comparison_only,
        ctrip_confirmed,
    )
    planner = PackagePlanner()
    verifier = PackageVerifier()
    candidates = planner.generate(request, inventory)
    initial = candidates[0]

    assert initial.flight == fliggy_comparison_only
    initial_violations = verifier.verify(request, initial, now=VERIFY_AT)
    assert tuple(item.code for item in initial_violations) == (
        PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED,
    )
    initial_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=initial,
        violations=initial_violations,
        verified_at=VERIFY_AT,
    )

    repair = PackageRepairer(planner=planner, verifier=verifier).repair_from_rejection(
        request,
        initial,
        candidates,
        initial_violations,
    )
    assert repair.candidate is not None
    assert repair.candidate.flight == ctrip_confirmed
    assert repair.candidate.flight.party_availability_confirmed
    final_violations = verifier.verify(request, repair.candidate, now=VERIFY_AT)
    assert final_violations == ()
    repair_handoff = PackageRepairHandoff(
        rejected_candidate_id=initial.id,
        rejection_error_codes=tuple(item.code for item in initial_handoff.errors),
        attempted=True,
        outcome=repair,
    )
    reverification_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.REVERIFICATION,
        candidate=repair.candidate,
        violations=final_violations,
        verified_at=VERIFY_AT,
    )
    planning_handoff = PackagePlanningHandoff(
        planner=PackagePlannerHandoff(
            candidates=candidates,
            selected_candidate_id=initial.id,
        ),
        initial_verification=initial_handoff,
        repair=repair_handoff,
        reverification=reverification_handoff,
    )

    result = PackageOrchestrator().decide_from_handoff(request, planning_handoff)

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.ACCEPT,
    ]
    assert result.final_candidate.flight == ctrip_confirmed
    assert result.final_candidate.flight.party_availability_confirmed
    assert result.final_violations == ()
    assert result.planning_handoff == planning_handoff


def test_all_comparison_only_flights_force_master_human_block() -> None:
    request = intent(budget_cents=2_000_000)
    inventory = _party_availability_inventory(
        flight(
            quote_id="fliggy:flight:comparison-only",
            provider="fliggy",
            total_cents=700_000,
            party_availability_confirmed=False,
        ),
        flight(
            quote_id="qunar:flight:comparison-only",
            provider="qunar",
            total_cents=720_000,
            party_availability_confirmed=False,
        ),
    )
    planner = PackagePlanner()
    verifier = PackageVerifier()
    candidates = planner.generate(request, inventory)
    initial = candidates[0]
    initial_violations = verifier.verify(request, initial, now=VERIFY_AT)
    initial_handoff = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=initial,
        violations=initial_violations,
        verified_at=VERIFY_AT,
    )
    repair = PackageRepairer(planner=planner, verifier=verifier).repair_from_rejection(
        request,
        initial,
        candidates,
        initial_violations,
    )

    assert repair.candidate is None
    planning_handoff = PackagePlanningHandoff(
        planner=PackagePlannerHandoff(
            candidates=candidates,
            selected_candidate_id=initial.id,
        ),
        initial_verification=initial_handoff,
        repair=PackageRepairHandoff(
            rejected_candidate_id=initial.id,
            rejection_error_codes=tuple(item.code for item in initial_handoff.errors),
            attempted=True,
            outcome=repair,
        ),
        reverification=None,
    )

    result = PackageOrchestrator().decide_from_handoff(request, planning_handoff)

    assert [item.state for item in result.decisions] == [
        PackageDecisionState.REJECT_AND_REPLAN,
        PackageDecisionState.HUMAN_BLOCK,
    ]
    assert result.final_candidate == initial
    assert result.final_decision.violation_codes == (
        PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED,
    )


@pytest.mark.parametrize(
    "phase",
    [
        PackageVerificationPhase.INITIAL,
        PackageVerificationPhase.REVERIFICATION,
        PackageVerificationPhase.EVENT_REVERIFICATION,
    ],
)
def test_unconfirmed_party_availability_is_a_hard_error_in_every_phase(
    phase: PackageVerificationPhase,
) -> None:
    request = intent(budget_cents=2_000_000)
    inventory = _party_availability_inventory(
        flight(
            quote_id=f"fliggy:flight:{phase.value}",
            provider="fliggy",
            total_cents=700_000,
            party_availability_confirmed=False,
        )
    )
    candidate = PackagePlanner().generate(request, inventory)[0]
    violations = PackageVerifier().verify(request, candidate, now=VERIFY_AT)
    handoff = PackageVerificationHandoff.from_candidate(
        phase=phase,
        candidate=candidate,
        violations=violations,
        verified_at=VERIFY_AT,
    )

    assert tuple(item.code for item in handoff.errors) == (
        PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED,
    )


def test_event_reverification_blocks_unconfirmed_flight_replacement() -> None:
    request = intent(budget_cents=2_000_000)
    confirmed = flight(
        quote_id="ctrip:flight:confirmed-before-event",
        provider="ctrip",
        total_cents=750_000,
        party_availability_confirmed=True,
    )
    comparison_only = flight(
        quote_id="fliggy:flight:event-comparison-only",
        provider="fliggy",
        total_cents=700_000,
        party_availability_confirmed=False,
    )
    inventory = _party_availability_inventory(confirmed, comparison_only)
    current = next(
        candidate
        for candidate in PackagePlanner().generate(
            request,
            _party_availability_inventory(confirmed),
        )
        if candidate.flight == confirmed
    )
    event = PackageEvent(
        id="flight-price-change",
        kind=PackageEventKind.PRICE_CHANGED,
        target_component_id=confirmed.id,
        replacement_component_id=comparison_only.id,
    )
    repair = PackageRepairer().repair_event(current, event, inventory)
    assert repair.candidate is not None
    violations = PackageVerifier().verify(request, repair.candidate, now=VERIFY_AT)
    event_handoff = PackageEventPlanningHandoff(
        repair=PackageEventRepairHandoff(
            event=event,
            current_candidate_id=current.id,
            current_candidate_version=current.version,
            current_component_ids=current.component_ids,
            outcome=repair,
        ),
        reverification=PackageVerificationHandoff.from_candidate(
            phase=PackageVerificationPhase.EVENT_REVERIFICATION,
            candidate=repair.candidate,
            violations=violations,
            verified_at=VERIFY_AT,
        ),
    )

    result = PackageOrchestrator().decide_event_from_handoff(
        request,
        current,
        event_handoff,
    )

    assert result.final_decision.state == PackageDecisionState.HUMAN_BLOCK
    assert result.final_decision.violation_codes == (
        PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED,
    )


def test_hotel_bound_transfer_cannot_cross_lodgings_or_survive_hotel_event() -> None:
    request = intent(budget_cents=2_000_000)
    hotel_a = lodging(
        "stay:bound:a",
        "Hotel A",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        400_000,
    )
    hotel_b = lodging(
        "stay:bound:b",
        "Hotel B",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        350_000,
    )
    public_out = transfer(
        "transfer:bound-out",
        PackageArea.AIRPORT,
        PackageArea.DESTINATION_ISLAND,
        datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
        datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
        36_000,
    )
    public_back = transfer(
        "transfer:bound-back",
        PackageArea.DESTINATION_ISLAND,
        PackageArea.AIRPORT,
        datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
        datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
        36_000,
    )
    bound_transfers = tuple(
        item.model_copy(
            update={
                "purchase_scope": TransferPurchaseScope.HOTEL_BOUND,
                "bound_lodging_id": hotel_a.id,
            }
        )
        for item in (public_out, public_back)
    )
    inventory = PackageInventory(
        flights=(flight(),),
        lodgings=(hotel_a, hotel_b),
        transfers=bound_transfers,
    )
    continuous = tuple(
        item
        for item in PackagePlanner().generate(request, inventory)
        if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )

    assert len(continuous) == 1
    assert continuous[0].lodgings[0].id == hotel_a.id
    mismatched = continuous[0].model_copy(
        update={
            "lodgings": (hotel_b,),
            "declared_total_cents": (
                continuous[0].flight.total_for_party_cents
                + hotel_b.total_for_party_cents
                + sum(item.total_for_party_cents for item in bound_transfers)
            ),
        }
    )
    assert PackageViolationCode.TRANSFER_BINDING_MISMATCH in {
        item.code for item in PackageVerifier().errors(request, mismatched, now=VERIFY_AT)
    }

    event = PackageEvent(
        id="hotel-a-sold-out",
        kind=PackageEventKind.SOLD_OUT,
        target_component_id=hotel_a.id,
        replacement_component_id=hotel_b.id,
    )
    result = PackageOrchestrator().replan_after_event(
        request,
        continuous[0],
        event,
        inventory,
        now=VERIFY_AT,
    )

    assert result.final_decision.state == PackageDecisionState.HUMAN_BLOCK
    assert result.final_candidate == continuous[0]
    assert "接驳" in result.final_decision.summary


def test_published_usd_base_fare_is_supplemental_not_cny_all_in_total() -> None:
    request = intent(
        budget_cents=1_500_000,
        destination_place_key=PackagePlaceKey.MAAFUSHI,
    )
    stay = lodging(
        "stay:maafushi",
        "Maafushi Hotel",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        400_000,
        place_key=PackagePlaceKey.MAAFUSHI,
    )
    outbound = published_maafushi_transfer(
        "icom:outbound",
        PackageArea.AIRPORT,
        PackageArea.DESTINATION_ISLAND,
        datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
        datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
    )
    inbound = published_maafushi_transfer(
        "icom:inbound",
        PackageArea.DESTINATION_ISLAND,
        PackageArea.AIRPORT,
        datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
        datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
    )
    inventory = PackageInventory(
        flights=(flight(),),
        lodgings=(stay,),
        transfers=(outbound, inbound),
    )

    candidate = PackagePlanner().generate(request, inventory)[0]
    violations = PackageVerifier().verify(request, candidate, now=VERIFY_AT)
    codes = {item.code for item in violations}

    assert candidate.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    assert len(candidate.transfers) == 2
    assert candidate.declared_total_cents == 1_338_400
    assert candidate.computed_total_cents == 1_338_400
    assert PackageViolationCode.CURRENCY_MISMATCH not in codes
    assert PackageViolationCode.TAXES_INCOMPLETE not in codes
    assert codes == {
        PackageViolationCode.PUBLISHED_BASE_FARE_NOT_ALL_IN,
        PackageViolationCode.BUDGET_NOT_FULLY_VERIFIED,
    }
    assert all(item.severity == PackageViolationSeverity.WARNING for item in violations)

    result = PackageOrchestrator().execute(
        request,
        candidate,
        inventory,
        now=VERIFY_AT,
    )
    assert result.final_decision.state == PackageDecisionState.ACCEPT
    assert set(result.final_decision.violation_codes) == codes
    assert "未完全验证" in result.final_decision.summary
    assert result.budget.total_cents == 1_338_400
    assert result.budget.confirmed_subtotal_cents == 1_338_400
    assert result.budget.transfer_cents == 0
    assert result.budget.budget_compliance_fully_verified is False
    assert result.budget.is_all_in_total is False
    assert len(result.budget.supplemental_published_base_fares) == 1
    supplemental = result.budget.supplemental_published_base_fares[0]
    assert supplemental.currency == "USD"
    assert supplemental.total_for_party_cents == 12_000
    assert len(supplemental.price_contract_ids) == 2
    assert supplemental.taxes_and_fees_included is None
    assert "USD 120.00" in result.budget.formula
    assert "税费未知" in result.budget.formula
    assert "已确认小计" in result.budget.formula


def test_published_round_trip_contract_is_deduplicated_but_keeps_both_legs() -> None:
    request = intent(
        budget_cents=None,
        destination_place_key=PackagePlaceKey.MAAFUSHI,
    )
    stay = lodging(
        "stay:maafushi:round-trip",
        "Maafushi Hotel",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        400_000,
        place_key=PackagePlaceKey.MAAFUSHI,
    )
    shared_contract = "published:maafushi:round-trip"
    outbound = published_maafushi_transfer(
        "icom:round-trip:out",
        PackageArea.AIRPORT,
        PackageArea.DESTINATION_ISLAND,
        datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
        datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
        price_contract_id=shared_contract,
        price_scope=TransferPriceScope.ROUND_TRIP,
    )
    inbound = published_maafushi_transfer(
        "icom:round-trip:back",
        PackageArea.DESTINATION_ISLAND,
        PackageArea.AIRPORT,
        datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
        datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
        price_contract_id=shared_contract,
        price_scope=TransferPriceScope.ROUND_TRIP,
    )
    candidate = PackagePlanner().generate(
        request,
        PackageInventory(
            flights=(flight(),),
            lodgings=(stay,),
            transfers=(outbound, inbound),
        ),
    )[0]

    supplemental = candidate.model_copy()
    budget = (
        PackageOrchestrator()
        .execute(
            request,
            supplemental,
            PackageInventory(
                flights=(flight(),),
                lodgings=(stay,),
                transfers=(outbound, inbound),
            ),
            now=VERIFY_AT,
        )
        .budget
    )
    assert budget.supplemental_published_base_fares[0].total_for_party_cents == 6_000
    assert budget.supplemental_published_base_fares[0].price_contract_ids == (shared_contract,)
    assert set(budget.supplemental_published_base_fares[0].transfer_ids) == {
        outbound.id,
        inbound.id,
    }


def test_icom_transfer_never_pairs_with_unconfirmed_destination_island() -> None:
    request = intent(destination_place_key=PackagePlaceKey.MAAFUSHI)
    unconfirmed_island_stay = lodging(
        "stay:unknown-island",
        "Unknown Island Hotel",
        PackageArea.DESTINATION_ISLAND,
        request.start_date,
        request.end_date,
        300_000,
        place_key=None,
    )
    inventory = PackageInventory(
        flights=(flight(),),
        lodgings=(unconfirmed_island_stay,),
        transfers=(
            published_maafushi_transfer(
                "icom:unknown-island:out",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                datetime(2026, 8, 23, 20, 40, tzinfo=MALDIVES),
                datetime(2026, 8, 23, 21, 25, tzinfo=MALDIVES),
            ),
            published_maafushi_transfer(
                "icom:unknown-island:back",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 30, 6, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 30, 7, 15, tzinfo=MALDIVES),
            ),
        ),
    )

    assert PackagePlanner().generate(request, inventory) == ()


def test_split_uses_six_real_airport_hub_legs_and_rejects_virtual_direct() -> None:
    request = intent()
    inventory = golden_inventory()
    split = next(
        candidate
        for candidate in PackagePlanner().generate(request, inventory)
        if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
    )
    assert tuple((item.origin_area, item.destination_area) for item in split.transfers) == (
        (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND),
        (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT),
        (PackageArea.AIRPORT, PackageArea.DESTINATION_ISLAND),
        (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT),
        (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND),
        (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT),
    )

    virtual_transfers = (
        transfer(
            "virtual:hulhumale-maafushi",
            PackageArea.AIRPORT_ISLAND,
            PackageArea.DESTINATION_ISLAND,
            datetime(2026, 8, 24, 10, 0, tzinfo=MALDIVES),
            datetime(2026, 8, 24, 10, 45, tzinfo=MALDIVES),
            36_000,
        ),
        transfer(
            "virtual:maafushi-hulhumale",
            PackageArea.DESTINATION_ISLAND,
            PackageArea.AIRPORT_ISLAND,
            datetime(2026, 8, 29, 16, 0, tzinfo=MALDIVES),
            datetime(2026, 8, 29, 16, 45, tzinfo=MALDIVES),
            36_000,
        ),
    )
    legacy_ids = {
        "transfer:direct-out",
        "transfer:direct-back",
        "transfer:airport-hotel",
        "transfer:hotel-airport",
    }
    legacy_inventory = inventory.model_copy(
        update={
            "transfers": (
                *(item for item in inventory.transfers if item.id in legacy_ids),
                *virtual_transfers,
            )
        }
    )
    assert not any(
        candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
        for candidate in PackagePlanner().generate(request, legacy_inventory)
    )


def test_split_selects_connectable_schedule_and_rejects_only_too_early_one() -> None:
    request = intent()
    inventory = golden_inventory()
    too_early = transfer(
        "transfer:airport-destination-too-early",
        PackageArea.AIRPORT,
        PackageArea.DESTINATION_ISLAND,
        datetime(2026, 8, 24, 7, 20, tzinfo=MALDIVES),
        datetime(2026, 8, 24, 8, 5, tzinfo=MALDIVES),
        30_000,
    )
    with_alternative = inventory.model_copy(update={"transfers": (*inventory.transfers, too_early)})
    selected = next(
        candidate
        for candidate in PackagePlanner().generate(request, with_alternative)
        if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
    )
    assert "transfer:airport-destination-next-day" in selected.component_ids
    assert too_early.id not in selected.component_ids

    without_connectable = inventory.model_copy(
        update={
            "transfers": tuple(
                too_early if item.id == "transfer:airport-destination-next-day" else item
                for item in inventory.transfers
            )
        }
    )
    invalid = next(
        candidate
        for candidate in PackagePlanner().generate(request, without_connectable)
        if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
    )
    assert PackageViolationCode.TRANSFER_CONNECTION_INFEASIBLE in {
        item.code for item in PackageVerifier().errors(request, invalid, now=VERIFY_AT)
    }


def test_platform_input_order_does_not_change_deterministic_result() -> None:
    """v0.4 exit gate: shuffling provider return order leaves the comparison identical."""
    request = intent()
    base = golden_inventory()

    def selected_key(inventory: PackageInventory) -> tuple[str, ...]:
        candidates = PackagePlanner().generate(request, inventory)
        best = min(candidates, key=lambda c: c.computed_total_cents)
        return (best.flight.provider, *tuple(sorted(s.provider for s in best.lodgings)))

    forward = selected_key(base)
    # Reverse the provider order of the lodging list (provider order must not matter).
    reversed_inventory = base.model_copy(
        update={"lodgings": tuple(reversed(base.lodgings))}
    )
    backward = selected_key(reversed_inventory)
    assert forward == backward
    assert forward[0] == "ctrip"


def test_price_tax_scope_mismatch_is_not_silently_compared() -> None:
    """v0.4 exit gate: offers with different tax scope must not be mixed as equal."""
    request = intent()
    base = golden_inventory()
    # Take one lodging and mark its taxes as NOT included — an incomparable scope.
    lodgings = list(base.lodgings)
    altered = lodgings[0].model_copy(update={"taxes_and_fees_included": False})
    lodgings[0] = altered
    inventory = base.model_copy(update={"lodgings": tuple(lodgings)})

    candidates = PackagePlanner().generate(request, inventory)
    for candidate in candidates:
        tax_mismatch = len(
            {stay.taxes_and_fees_included for stay in candidate.lodgings}
        ) > 1
        if tax_mismatch:
            # The verifier must surface the incomparable tax scope rather than
            # treating per-component totals as equivalent.
            violations = PackageVerifier().errors(request, candidate, now=VERIFY_AT)
            assert violations, "mixing tax-included and tax-excluded offers must fail"
