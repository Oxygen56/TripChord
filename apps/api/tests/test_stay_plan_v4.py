from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tripchord.agents import live_done_gate_v4
from tripchord.agents.flexible_live_system import FlexiblePairState
from tripchord.agents.live_done_gate import LiveDoneGateCheck
from tripchord.agents.live_done_gate_v4 import (
    _check_all_recommended_publication_closures,
    _check_inventory_outcome_contract,
    _check_recommendable_options,
    _check_stage_aware_run_contracts,
    _check_v4_source_graph,
    _inventory_outcome_evidence_errors,
    _v4_terminal_outcome_is_verified,
)
from tripchord.agents.live_system import (
    LiveEvidenceScope,
    LiveFinalizationState,
    LivePackageAgentSystem,
    LiveRunPurpose,
    _RunState,
)
from tripchord.agents.stay_area import system_stay_area_search_profile
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORMS,
    FlexibleDateExplorer,
    FlexibleQueryPlanBuilder,
    FlexibleTravelWindow,
    QueryTaskKind,
)
from tripchord.planning.frozen_graph import (
    _FROZEN_V4_TRAVEL_WINDOW,
    FROZEN_V4_REFERENCE_DATE,
)
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageCandidateKind,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
    PackagePlaceKey,
    PackagePlanner,
    PackageRepairer,
    PackageVerificationHandoff,
    PackageVerificationPhase,
    PackageViolation,
    PackageViolationCode,
    PackageViolationSeverity,
    QuoteAvailability,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
)
from tripchord.planning.stay_plans import (
    StayInventoryResultState,
    StayPlanCandidateSet,
    StayPlanId,
    StayPlanInventoryOutcome,
    StayPlanPlannerHandoff,
    StayPlanPlanningHandoff,
    StayPlanRepairHandoff,
    StayPlanVerificationHandoff,
    stay_plan_for_candidate,
    system_stay_plan_candidate_set,
)
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    LodgingInventoryConfirmedQuery,
    LodgingInventoryReceipt,
    LodgingInventoryReceiptState,
    lodging_inventory_receipt_sha256,
    parse_historical_lodging_inventory_receipt,
    qunar_detail_seed_selection,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(days=60)
MALDIVES = timezone(timedelta(hours=5))


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="trip:v4",
        origin="杭州",
        destination="马累",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        destination_place_key=None,
    )


def _flight(intent: PackageIntent) -> NormalizedFlightQuote:
    return NormalizedFlightQuote(
        id="flight:v4",
        provider="ctrip",
        total_for_party_cents=1_100_000,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=EXPIRES,
        availability=QuoteAvailability.AVAILABLE,
        evidence_refs=("evidence:flight:v4",),
        origin=intent.origin,
        destination=intent.destination,
        adults=intent.adults,
        outbound_depart_at=datetime(2026, 8, 12, 1, 0, tzinfo=UTC),
        outbound_arrive_at=datetime(2026, 8, 12, 10, 0, tzinfo=MALDIVES),
        return_depart_at=datetime(2026, 8, 18, 18, 0, tzinfo=MALDIVES),
        return_arrive_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )


def _lodging(
    *,
    quote_id: str,
    place: PackagePlaceKey,
    area: PackageArea,
    total_cents: int,
) -> NormalizedLodgingQuote:
    return NormalizedLodgingQuote(
        id=quote_id,
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=EXPIRES,
        availability=QuoteAvailability.AVAILABLE,
        evidence_refs=(f"evidence:{quote_id}",),
        property_name=f"{place.value} exact stay",
        area=area,
        check_in=date(2026, 8, 12),
        check_out=date(2026, 8, 18),
        adults=2,
        rooms=1,
        place_key=place,
    )


def _transfer(
    *,
    transfer_id: str,
    provider: str,
    origin: PackagePlaceKey,
    destination: PackagePlaceKey,
    origin_area: PackageArea,
    destination_area: PackageArea,
    service_date: date,
    depart_hour: int,
    arrive_hour: int,
    guarantee: TransferPriceGuarantee,
) -> TransferOption:
    return TransferOption(
        id=transfer_id,
        provider=provider,
        total_for_party_cents=(
            25_000 if guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED else 6_400
        ),
        currency=("CNY" if guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED else "USD"),
        taxes_and_fees_included=(
            True if guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED else None
        ),
        captured_at=NOW,
        expires_at=EXPIRES,
        availability=QuoteAvailability.AVAILABLE,
        evidence_refs=(f"evidence:{transfer_id}",),
        origin_area=origin_area,
        destination_area=destination_area,
        origin_place_key=origin,
        destination_place_key=destination,
        adults=2,
        service_date=service_date,
        schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
        duration_minutes=60,
        depart_at=datetime.combine(
            service_date,
            datetime.min.time(),
            tzinfo=MALDIVES,
        ).replace(hour=depart_hour),
        arrive_at=datetime.combine(
            service_date,
            datetime.min.time(),
            tzinfo=MALDIVES,
        ).replace(hour=arrive_hour),
        operates_24_hours=False,
        requires_reservation=True,
        price_scope=TransferPriceScope.ONE_WAY,
        price_contract_id=f"price:{transfer_id}",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        price_guarantee=guarantee,
        contract_evidence_text=f"{origin.value} -> {destination.value}",
        detail_url="https://example.com/read-only-transfer",
    )


def _inventory(intent: PackageIntent) -> PackageInventory:
    airport = PackagePlaceKey.VELANA_AIRPORT
    maafushi = PackagePlaceKey.MAAFUSHI
    hulhumale = PackagePlaceKey.HULHUMALE
    return PackageInventory(
        flights=(_flight(intent),),
        lodgings=(
            _lodging(
                quote_id="lodging:maafushi",
                place=maafushi,
                area=PackageArea.DESTINATION_ISLAND,
                total_cents=320_000,
            ),
            _lodging(
                quote_id="lodging:hulhumale",
                place=hulhumale,
                area=PackageArea.AIRPORT_ISLAND,
                total_cents=280_000,
            ),
        ),
        transfers=(
            _transfer(
                transfer_id="icom:out",
                provider="icom-public-transfer",
                origin=airport,
                destination=maafushi,
                origin_area=PackageArea.AIRPORT,
                destination_area=PackageArea.DESTINATION_ISLAND,
                service_date=intent.start_date,
                depart_hour=13,
                arrive_hour=14,
                guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
            ),
            _transfer(
                transfer_id="icom:back",
                provider="icom-public-transfer",
                origin=maafushi,
                destination=airport,
                origin_area=PackageArea.DESTINATION_ISLAND,
                destination_area=PackageArea.AIRPORT,
                service_date=intent.end_date,
                depart_hour=13,
                arrive_hour=14,
                guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
            ),
            _transfer(
                transfer_id="hulhumale:out",
                provider="airport-transfer-fixture",
                origin=airport,
                destination=hulhumale,
                origin_area=PackageArea.AIRPORT,
                destination_area=PackageArea.AIRPORT_ISLAND,
                service_date=intent.start_date,
                depart_hour=11,
                arrive_hour=12,
                guarantee=TransferPriceGuarantee.ALL_IN_CONFIRMED,
            ),
            _transfer(
                transfer_id="hulhumale:back",
                provider="airport-transfer-fixture",
                origin=hulhumale,
                destination=airport,
                origin_area=PackageArea.AIRPORT_ISLAND,
                destination_area=PackageArea.AIRPORT,
                service_date=intent.end_date,
                depart_hour=15,
                arrive_hour=16,
                guarantee=TransferPriceGuarantee.ALL_IN_CONFIRMED,
            ),
        ),
    )


def test_candidate_set_is_prefrozen_and_tamper_evident() -> None:
    candidate_set = system_stay_plan_candidate_set()

    assert candidate_set.stay_plan_ids == (
        StayPlanId.MAAFUSHI_ICOM,
        StayPlanId.MAAFUSHI_SPLIT_HULHUMALE,
        StayPlanId.HULHUMALE_CONTINUOUS,
    )
    assert candidate_set.candidate_set_sha256 == candidate_set.computed_sha256()
    assert all(item.candidate_sha256 == item.computed_sha256() for item in candidate_set.candidates)

    damaged = copy.deepcopy(candidate_set.model_dump(mode="json"))
    damaged["candidates"][0]["scan_limit_per_platform"] = 99
    with pytest.raises(ValidationError, match="candidate SHA"):
        StayPlanCandidateSet.model_validate(damaged)


def test_v5_query_plan_uses_flight_only_tongcheng_capability() -> None:
    window = FlexibleTravelWindow(
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        earliest_departure=date(2026, 8, 12),
        latest_departure=date(2026, 8, 12),
        min_nights=6,
        max_nights=6,
        max_pairs=1,
        adults=2,
        rooms=1,
    )
    candidate_set = system_stay_plan_candidate_set()
    exploration = FlexibleDateExplorer().explore(window, now=NOW)
    plan = FlexibleQueryPlanBuilder(platforms=LIVE_V5_PLATFORMS).build(
        window,
        exploration,
        stay_plan_candidate_set=candidate_set,
    )

    assert plan.total_task_count == 13
    assert plan.task_count_by_platform == {"ctrip": 6, "qunar": 6, "tongcheng": 1}
    assert plan.stay_plan_candidate_set_sha256 == candidate_set.candidate_set_sha256
    assert sum(item.kind == QueryTaskKind.LODGING_HULHUMALE_FULL_STAY for item in plan.tasks) == 2
    assert {item.stay_plan_id for item in plan.tasks if item.stay_plan_id is not None} == set(
        candidate_set.stay_plan_ids
    )


def test_v4_default_query_plan_freezes_each_provider_lane_at_40_seconds() -> None:
    window = FlexibleTravelWindow(
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        earliest_departure=date(2026, 8, 12),
        latest_departure=date(2026, 8, 14),
        min_nights=6,
        max_nights=6,
        max_pairs=3,
        adults=2,
        rooms=1,
    )
    candidate_set = system_stay_plan_candidate_set()
    exploration = FlexibleDateExplorer().explore(window, now=NOW)

    plan = FlexibleQueryPlanBuilder(platforms=LIVE_V5_PLATFORMS).build(
        window,
        exploration,
        stay_plan_candidate_set=candidate_set,
    )

    assert plan.total_task_count == 39
    for platform in LIVE_V5_PLATFORMS:
        offsets = tuple(
            task.scheduled_offset_ms for task in plan.tasks if task.platform == platform
        )
        assert offsets == tuple(index * 40_000 for index in range(len(offsets)))


def _v4_source_graph_fixture() -> tuple[SimpleNamespace, StayPlanCandidateSet]:
    # C-122 R44 (canonical pair-set authority): the producer check now requires
    # the run to seal the CANONICAL frozen ordered trio (derived from the frozen
    # window + committed reference_date + selection algorithm), so the passing
    # fixture must explore the FROZEN window exactly as the API does — not an
    # arbitrary synthetic window whose pair ids would be rejected as foreign.
    frozen = _FROZEN_V4_TRAVEL_WINDOW
    effective_earliest = max(
        frozen.earliest_departure,
        FROZEN_V4_REFERENCE_DATE + timedelta(days=7),
    )
    window = frozen.model_copy(
        update={"earliest_departure": effective_earliest, "max_pairs": 3}
    )
    reference = datetime(
        FROZEN_V4_REFERENCE_DATE.year,
        FROZEN_V4_REFERENCE_DATE.month,
        FROZEN_V4_REFERENCE_DATE.day,
        tzinfo=UTC,
    )
    candidate_set = system_stay_plan_candidate_set()
    exploration = FlexibleDateExplorer().explore(window, now=reference)
    plan = FlexibleQueryPlanBuilder(platforms=LIVE_V5_PLATFORMS).build(
        window,
        exploration,
        stay_plan_candidate_set=candidate_set,
    )
    pair_by_id = {item.id: item for item in exploration.candidates}
    suffix_by_kind = {
        QueryTaskKind.FLIGHT: "flight",
        QueryTaskKind.LODGING_FULL_STAY: "lodging-full",
        QueryTaskKind.LODGING_FIRST_NIGHT: "lodging-first",
        QueryTaskKind.LODGING_MIDDLE_STAY: "lodging-middle",
        QueryTaskKind.LODGING_LAST_NIGHT: "lodging-last",
        QueryTaskKind.LODGING_HULHUMALE_FULL_STAY: "lodging-hulhumale-full",
    }
    public_transfer_ids = tuple(
        sorted(
            {
                f"public-transfer-icom-{contract.contract_id.removeprefix('icom-')}"
                for stay_plan in candidate_set.candidates
                for contract in stay_plan.required_transfer_contracts
                if contract.required_provider == "icom-public-transfer"
            }
        )
    )
    pair_runs: list[SimpleNamespace] = []
    for pair_id in plan.selected_pair_ids:
        pair_tasks = tuple(task for task in plan.tasks if task.date_pair_id == pair_id)
        source_ids = tuple(
            f"source-{task.platform.value}-{suffix_by_kind[task.kind]}" for task in pair_tasks
        )
        graph_ids = (*source_ids, *public_transfer_ids)
        live_run = SimpleNamespace(
            source_task_ids=source_ids,
            public_transfer_task_ids=public_transfer_ids,
            scheduler=SimpleNamespace(
                graph=SimpleNamespace(
                    tasks=tuple(SimpleNamespace(id=task_id) for task_id in graph_ids)
                )
            ),
        )
        pair_runs.append(
            SimpleNamespace(
                date_pair=pair_by_id[pair_id],
                query_tasks=pair_tasks,
                state=FlexiblePairState.COMPLETED,
                run=live_run,
            )
        )
    assert len(plan.tasks) == 39
    assert len(pair_runs) == 3
    return (
        SimpleNamespace(
            pair_runs=tuple(pair_runs),
            query_plan=plan,
        ),
        candidate_set,
    )


def _replace_pair_execution(
    root_run: SimpleNamespace,
    index: int,
    **updates: object,
) -> SimpleNamespace:
    executions = list(root_run.pair_runs)
    executions[index] = SimpleNamespace(
        **{
            **vars(executions[index]),
            **updates,
        }
    )
    return SimpleNamespace(
        **{
            **vars(root_run),
            "pair_runs": tuple(executions),
        }
    )


def test_v4_source_graph_requires_three_unique_fully_bound_date_pairs() -> None:
    run, candidate_set = _v4_source_graph_fixture()
    assert _check_v4_source_graph(run, candidate_set).passed

    first_two_ids = run.query_plan.selected_pair_ids[:2]
    first_two_tasks = tuple(
        task for task in run.query_plan.tasks if task.date_pair_id in first_two_ids
    )
    two_pair_run = SimpleNamespace(
        pair_runs=run.pair_runs[:2],
        query_plan=run.query_plan.model_copy(
            update={
                "tasks": first_two_tasks,
                "selected_pair_ids": first_two_ids,
                "total_task_count": len(first_two_tasks),
            }
        ),
    )
    assert not _check_v4_source_graph(two_pair_run, candidate_set).passed

    duplicate_pair_run = _replace_pair_execution(
        run,
        1,
        date_pair=run.pair_runs[0].date_pair,
    )
    assert not _check_v4_source_graph(
        duplicate_pair_run,
        candidate_set,
    ).passed

    mismatched_selected_ids = run.query_plan.model_copy(
        update={
            "selected_pair_ids": (
                run.query_plan.selected_pair_ids[0],
                run.query_plan.selected_pair_ids[1],
                "date-pair:forged",
            )
        }
    )
    assert not _check_v4_source_graph(
        SimpleNamespace(
            pair_runs=run.pair_runs,
            query_plan=mismatched_selected_ids,
        ),
        candidate_set,
    ).passed

    first_execution = run.pair_runs[0]
    tampered_tasks = list(first_execution.query_tasks)
    tampered_tasks[0] = tampered_tasks[0].model_copy(
        update={
            "scheduled_offset_ms": tampered_tasks[0].scheduled_offset_ms + 1,
        }
    )
    mismatched_execution = _replace_pair_execution(
        run,
        0,
        query_tasks=tuple(tampered_tasks),
    )
    mismatch_check = _check_v4_source_graph(mismatched_execution, candidate_set)
    assert not mismatch_check.passed
    assert "execution 查询任务未精确绑定 query_plan 子集" in mismatch_check.summary


def test_pending_lodging_inventory_receipt_crossvalidates_as_its_own_state() -> None:
    candidate_set = system_stay_plan_candidate_set()
    intent = _intent()
    captured_at = NOW + timedelta(seconds=31)
    observed_duration_ms = 28_000
    plan = candidate_set.candidate(StayPlanId.MAAFUSHI_SPLIT_HULHUMALE)
    segment = next(item for item in plan.segments if item.query_segment == "first")
    task_id = "source-qunar-lodging-first"
    options = {
        "expected_lodging_place_key": segment.exact_place_key.value,
        "expected_package_area": segment.area.value,
        "segment": segment.query_segment,
    }
    page_url = "https://hotel.qunar.com/city/i-hulhumale/"
    raw_receipt = {
        "schema_version": "tripchord-lodging-inventory-receipt-v1",
        "parser_version": "tripchord-visible-dom-v3",
        "provider": "qunar",
        "state": "bounded_provider_pending",
        "confirmed_query": {
            "destination": "Hulhumalé",
            "start_date": segment.check_in.resolve(intent).isoformat(),
            "end_date": segment.check_out.resolve(intent).isoformat(),
            "adults": intent.adults,
            "rooms": intent.rooms,
            "options": options,
        },
        "confirmation_scope": "confirmed_visible_search",
        "scan_limit": plan.scan_limit_per_platform,
        "scanned_count": 0,
        "candidate_summaries": [],
        "explicit_empty_evidence": None,
        "provider_pending_evidence": {
            "contract_version": "qunar-visible-search-pending-v1",
            "result_count_text": "共 家酒店满足条件",
            "pending_message": "请稍等,您查询的结果正在实时搜索中...",
            "observed_duration_ms": observed_duration_ms,
        },
        "page_url": page_url,
        "captured_at": captured_at.isoformat(),
    }
    receipt_sha = lodging_inventory_receipt_sha256(raw_receipt)
    snapshot = BrowserTaskSnapshot.model_validate(
        {
            "id": "browser-task-pending-v4-contract",
            "provider": "qunar",
            "kind": "lodging",
            "query": {
                "origin": intent.origin,
                "destination": "Hulhumalé",
                "start_date": segment.check_in.resolve(intent).isoformat(),
                "end_date": segment.check_out.resolve(intent).isoformat(),
                "adults": intent.adults,
                "rooms": intent.rooms,
                "currency": "CNY",
                "options": options,
            },
            "state": "failed",
            "created_at": NOW.isoformat(),
            "updated_at": (captured_at + timedelta(milliseconds=20)).isoformat(),
            "attempt_count": 1,
            "claimed_by": "chrome-companion-pending-v4",
            "claimed_at": NOW.isoformat(),
            "failure": {
                "code": "extraction_error",
                "message": "去哪儿可见结果壳仍在实时搜索",
                "retryable": False,
                "page_url": page_url,
                "captured_at": captured_at.isoformat(),
                "details": {
                    "inventory_result_state": "bounded_provider_pending",
                    "confirmed_exhaustive": False,
                    "scanned_count": 0,
                    "bounded_pending_observed_ms": observed_duration_ms,
                    "inventory_receipt": raw_receipt,
                    "inventory_receipt_sha256": receipt_sha,
                },
            },
        }
    )
    outcome = StayPlanInventoryOutcome(
        source_task_id=task_id,
        provider="qunar",
        stay_plan_id=plan.stay_plan_id,
        segment_id=segment.segment_id,
        state=StayInventoryResultState.BOUNDED_PROVIDER_PENDING,
        exact_place_key=segment.exact_place_key,
        scan_limit=plan.scan_limit_per_platform,
        scanned_count=0,
        inventory_receipt_sha256=receipt_sha,
        evidence_refs=(
            f"browser-task:{snapshot.id}",
            f"inventory-receipt:sha256:{receipt_sha}",
        ),
        reason="平台在有界等待后仍显示实时搜索中，不冒充空库存或报价",
    )
    result = SimpleNamespace(
        task_id=task_id,
        output={"snapshot": snapshot.model_dump(mode="json")},
    )
    run = SimpleNamespace(
        source_task_ids=(task_id,),
        scheduler=SimpleNamespace(results=(result,)),
        stay_plan_inventory_outcomes=(outcome,),
        normalization_results=(),
        intent=intent,
    )

    def run_with(
        candidate_snapshot: BrowserTaskSnapshot,
        candidate_outcome: StayPlanInventoryOutcome = outcome,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            source_task_ids=(task_id,),
            scheduler=SimpleNamespace(
                results=(
                    SimpleNamespace(
                        task_id=task_id,
                        output={"snapshot": candidate_snapshot.model_dump(mode="json")},
                    ),
                )
            ),
            stay_plan_inventory_outcomes=(candidate_outcome,),
            normalization_results=(),
            intent=intent,
        )

    def sealed_run(
        candidate_receipt: dict[str, object],
        *,
        surface_duration_ms: int = observed_duration_ms,
        claimed_at: datetime = NOW,
    ) -> SimpleNamespace:
        candidate_sha = lodging_inventory_receipt_sha256(candidate_receipt)
        assert snapshot.failure is not None
        candidate_snapshot = snapshot.model_copy(
            update={
                "claimed_at": claimed_at,
                "failure": snapshot.failure.model_copy(
                    update={
                        "details": {
                            **snapshot.failure.details,
                            "bounded_pending_observed_ms": surface_duration_ms,
                            "inventory_receipt": candidate_receipt,
                            "inventory_receipt_sha256": candidate_sha,
                        }
                    }
                ),
            }
        )
        candidate_outcome = outcome.model_copy(
            update={
                "inventory_receipt_sha256": candidate_sha,
                "evidence_refs": (
                    f"browser-task:{snapshot.id}",
                    f"inventory-receipt:sha256:{candidate_sha}",
                ),
            }
        )
        return run_with(candidate_snapshot, candidate_outcome)

    assert not _inventory_outcome_evidence_errors(
        run,
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )
    assert _v4_terminal_outcome_is_verified(
        SimpleNamespace(
            stay_plan_inventory_outcomes=(outcome,),
            flight_search_outcomes=(),
        ),
        snapshot,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )

    forged_receipt = copy.deepcopy(raw_receipt)
    forged_receipt["provider_pending_evidence"]["observed_duration_ms"] = 1
    assert snapshot.failure is not None
    forged_snapshot = snapshot.model_copy(
        update={
            "failure": snapshot.failure.model_copy(
                update={
                    "details": {
                        **snapshot.failure.details,
                        "inventory_receipt": forged_receipt,
                    }
                }
            )
        }
    )
    forged_run = SimpleNamespace(
        **{
            **vars(run),
            "scheduler": SimpleNamespace(
                results=(
                    SimpleNamespace(
                        task_id=task_id,
                        output={"snapshot": forged_snapshot.model_dump(mode="json")},
                    ),
                )
            ),
        }
    )
    assert _inventory_outcome_evidence_errors(
        forged_run,
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )

    wrong_state_run = run_with(
        snapshot,
        outcome.model_copy(update={"state": StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE}),
    )
    assert _inventory_outcome_evidence_errors(
        wrong_state_run,
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )

    sealed_duration_mismatch = copy.deepcopy(raw_receipt)
    pending_evidence = sealed_duration_mismatch["provider_pending_evidence"]
    assert isinstance(pending_evidence, dict)
    pending_evidence["observed_duration_ms"] = observed_duration_ms + 1_000
    assert _inventory_outcome_evidence_errors(
        sealed_run(sealed_duration_mismatch),
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )

    assert _inventory_outcome_evidence_errors(
        sealed_run(
            copy.deepcopy(raw_receipt),
            claimed_at=captured_at - timedelta(seconds=1),
        ),
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )

    sealed_query_mismatch = copy.deepcopy(raw_receipt)
    confirmed_query = sealed_query_mismatch["confirmed_query"]
    assert isinstance(confirmed_query, dict)
    confirmed_query["destination"] = "Malé"
    assert _inventory_outcome_evidence_errors(
        sealed_run(sealed_query_mismatch),
        candidate_set,
        now=captured_at,
        maximum_quote_age=timedelta(minutes=15),
    )


@pytest.mark.parametrize("declared_total", [53, 55])
def test_v4_source_graph_rejects_non_54_declared_task_total(
    declared_total: int,
) -> None:
    run, candidate_set = _v4_source_graph_fixture()
    damaged_plan = run.query_plan.model_copy(update={"total_task_count": declared_total})
    assert not _check_v4_source_graph(
        SimpleNamespace(
            pair_runs=run.pair_runs,
            query_plan=damaged_plan,
        ),
        candidate_set,
    ).passed


def test_v4_source_graph_rejects_duplicate_query_and_source_ids() -> None:
    run, candidate_set = _v4_source_graph_fixture()
    tasks = list(run.query_plan.tasks)
    tasks[1] = tasks[1].model_copy(update={"id": tasks[0].id})
    duplicate_query_plan = run.query_plan.model_copy(update={"tasks": tuple(tasks)})
    assert not _check_v4_source_graph(
        SimpleNamespace(
            pair_runs=run.pair_runs,
            query_plan=duplicate_query_plan,
        ),
        candidate_set,
    ).passed

    first_live = run.pair_runs[0].run
    source_ids = first_live.source_task_ids
    duplicate_source_live = SimpleNamespace(
        **{
            **vars(first_live),
            "source_task_ids": (*source_ids[:-1], source_ids[0]),
        }
    )
    duplicate_source_run = _replace_pair_execution(
        run,
        0,
        run=duplicate_source_live,
    )
    assert not _check_v4_source_graph(
        duplicate_source_run,
        candidate_set,
    ).passed

    duplicate_public_live = SimpleNamespace(
        **{
            **vars(first_live),
            "public_transfer_task_ids": (
                *first_live.public_transfer_task_ids,
                first_live.public_transfer_task_ids[0],
            ),
        }
    )
    duplicate_public_run = _replace_pair_execution(
        run,
        0,
        run=duplicate_public_live,
    )
    assert not _check_v4_source_graph(
        duplicate_public_run,
        candidate_set,
    ).passed


def test_v4_source_graph_rejects_scenario_drift_from_canonical_frozen_graph() -> None:
    """C-122 round-19 (supervision 17:03 Block 1): the producer must derive its
    expected member sets from the SAME canonical frozen graph the layer-6
    validator compares against.  A candidate set that drifts from the canonical
    graph (here: an extra iCom-public-transfer contract) fails closed instead of
    sealing a graph whose members the validator would reject as foreign."""
    run, candidate_set = _v4_source_graph_fixture()
    drifted_candidates: list[SimpleNamespace] = []
    for index, plan in enumerate(candidate_set.candidates):
        if index == 0:
            extra_contract = SimpleNamespace(
                contract_id="icom-evil-extra",
                required_provider="icom-public-transfer",
            )
            plan = SimpleNamespace(
                **{
                    **vars(plan),
                    "required_transfer_contracts": (
                        *plan.required_transfer_contracts,
                        extra_contract,
                    ),
                }
            )
        drifted_candidates.append(plan)
    drifted_candidate_set = SimpleNamespace(
        **{
            **vars(candidate_set),
            "candidates": tuple(drifted_candidates),
        }
    )
    drifted_check = _check_v4_source_graph(run, drifted_candidate_set)
    assert not drifted_check.passed
    assert "与规范冻结图不一致" in drifted_check.summary


_STAGE_AWARE_DECISION_DEPENDENCIES = {
    "plan-travel-package": ("normalize-browser-quotes",),
    "prepare-candidate-decision-frontier": ("plan-travel-package",),
    "analyze-live-evidence": ("prepare-candidate-decision-frontier",),
    "curate-travel-candidates": ("analyze-live-evidence",),
    "verify-travel-package": ("curate-travel-candidates",),
    "criticize-travel-package": ("verify-travel-package",),
    "strategize-package-repair": ("criticize-travel-package",),
    "repair-travel-package": ("strategize-package-repair",),
    "reverify-travel-package": ("repair-travel-package",),
    "recriticize-repaired-package": ("reverify-travel-package",),
    "recommend-final-decision": ("recriticize-repaired-package",),
    "orchestrate-travel-package": ("recommend-final-decision",),
}
_STAGE_AWARE_DEFERRED = (
    "explain-final-decision",
    "curate-run-memory",
    "publish-live-run",
)


def _stage_scheduler(*, exploration: bool) -> SimpleNamespace:
    dependencies = dict(_STAGE_AWARE_DECISION_DEPENDENCIES)
    terminal = "seal-exploration-run" if exploration else "publish-live-run"
    if exploration:
        dependencies[terminal] = ("orchestrate-travel-package",)
    else:
        dependencies.update(
            {
                "explain-final-decision": ("orchestrate-travel-package",),
                "curate-run-memory": ("explain-final-decision",),
                "publish-live-run": ("curate-run-memory",),
            }
        )
    tasks = tuple(
        SimpleNamespace(id=task_id, dependencies=task_dependencies)
        for task_id, task_dependencies in dependencies.items()
    )
    results = tuple(
        SimpleNamespace(
            task_id=task.id,
            success=True,
            output=(
                {
                    "exploration_seal_passed": True,
                    "decision_present": True,
                    "model_required_failed": False,
                    "memory_persisted": False,
                    "deferred_stage_ids": list(_STAGE_AWARE_DEFERRED),
                }
                if task.id == "seal-exploration-run"
                else {"publication_gate_passed": True}
                if task.id == "publish-live-run"
                else {}
            ),
        )
        for task in tasks
    )
    return SimpleNamespace(
        graph=SimpleNamespace(tasks=tasks),
        results=results,
        succeeded=True,
    )


def _stage_lifecycle_run(
    *,
    exploration: bool,
    pair_id: str,
    start_date: date,
    end_date: date,
    search_query: SimpleNamespace | None = None,
) -> SimpleNamespace:
    bound_query = search_query or SimpleNamespace(
        start_date=start_date,
        end_date=end_date,
    )
    return SimpleNamespace(
        evidence_scope=(
            LiveEvidenceScope.FULL_SEARCH
            if exploration
            else LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH
        ),
        run_purpose=(
            LiveRunPurpose.EXPLORATION_SELECTION
            if exploration
            else LiveRunPurpose.FINAL_PUBLICATION
        ),
        finalization_state=(
            LiveFinalizationState.EXPLORATION_SEALED
            if exploration
            else LiveFinalizationState.FINAL_PUBLISHED
        ),
        deferred_stage_ids=_STAGE_AWARE_DEFERRED if exploration else (),
        exploration_seal_passed=exploration,
        explanation=None if exploration else SimpleNamespace(summary="published"),
        memory_candidates=None if exploration else SimpleNamespace(candidates=()),
        intent=SimpleNamespace(trip_id=f"flexible:{pair_id}"),
        search_query=bound_query,
        scheduler=_stage_scheduler(exploration=exploration),
        agentic=SimpleNamespace(stages=()),
    )


def _stage_aware_done_gate_fixture() -> SimpleNamespace:
    pair_runs = []
    recommended_ids = []
    for index in range(1, 4):
        pair_id = f"pair:{index}"
        start_date = date(2026, 8, index)
        end_date = start_date + timedelta(days=5)
        pair = SimpleNamespace(
            id=pair_id,
            departure_date=start_date,
            return_date=end_date,
        )
        exploration = _stage_lifecycle_run(
            exploration=True,
            pair_id=pair_id,
            start_date=start_date,
            end_date=end_date,
        )
        if index <= 2:
            option_id = f"option:{index}"
            recommended_ids.append(option_id)
            pair_runs.append(
                SimpleNamespace(
                    date_pair=pair,
                    run=_stage_lifecycle_run(
                        exploration=False,
                        pair_id=pair_id,
                        start_date=start_date,
                        end_date=end_date,
                        search_query=exploration.search_query,
                    ),
                    exploration_run=exploration,
                    publication_refresh_audit=SimpleNamespace(
                        binding_passed=True,
                        refreshed_option_id=option_id,
                    ),
                )
            )
        else:
            pair_runs.append(
                SimpleNamespace(
                    date_pair=pair,
                    run=exploration,
                    exploration_run=None,
                    publication_refresh_audit=None,
                )
            )
    return SimpleNamespace(
        pair_runs=tuple(pair_runs),
        publication_refresh_minimum_options=2,
        recommended_option_ids=tuple(recommended_ids),
    )


def test_stage_aware_gate_requires_three_sealed_explorations_and_two_publications() -> None:
    check = _check_stage_aware_run_contracts(_stage_aware_done_gate_fixture())

    assert check.passed
    assert check.evidence["exploration_count"] == 3
    assert check.evidence["publication_count"] == 2
    assert check.evidence["publication_option_ids"] == ["option:1", "option:2"]


def test_stage_aware_gate_rejects_deferred_stage_forged_as_success() -> None:
    run = _stage_aware_done_gate_fixture()
    first = run.pair_runs[0]
    exploration = first.exploration_run
    forged = SimpleNamespace(
        task_id="explain-final-decision",
        success=True,
        output={"deferred": True},
    )
    damaged_exploration = SimpleNamespace(
        **{
            **vars(exploration),
            "scheduler": SimpleNamespace(
                **{
                    **vars(exploration.scheduler),
                    "results": (*exploration.scheduler.results, forged),
                }
            ),
        }
    )
    damaged_first = SimpleNamespace(**{**vars(first), "exploration_run": damaged_exploration})
    damaged = SimpleNamespace(**{**vars(run), "pair_runs": (damaged_first, *run.pair_runs[1:])})

    check = _check_stage_aware_run_contracts(damaged)

    assert not check.passed
    assert "不得冒充成功执行" in check.summary


@pytest.mark.parametrize("damage", ["unsealed_exploration", "incomplete_publication"])
def test_stage_aware_gate_rejects_unsealed_or_incomplete_terminal_chain(
    damage: str,
) -> None:
    run = _stage_aware_done_gate_fixture()
    first = run.pair_runs[0]
    if damage == "unsealed_exploration":
        exploration = first.exploration_run
        damaged_run = SimpleNamespace(**{**vars(exploration), "exploration_seal_passed": False})
        damaged_first = SimpleNamespace(**{**vars(first), "exploration_run": damaged_run})
    else:
        publication = first.run
        damaged_results = tuple(
            SimpleNamespace(**{**vars(result), "success": False})
            if result.task_id == "curate-run-memory"
            else result
            for result in publication.scheduler.results
        )
        damaged_publication = SimpleNamespace(
            **{
                **vars(publication),
                "scheduler": SimpleNamespace(
                    **{
                        **vars(publication.scheduler),
                        "results": damaged_results,
                    }
                ),
            }
        )
        damaged_first = SimpleNamespace(**{**vars(first), "run": damaged_publication})
    damaged = SimpleNamespace(**{**vars(run), "pair_runs": (damaged_first, *run.pair_runs[1:])})

    assert not _check_stage_aware_run_contracts(damaged).passed


def _recommendable_fixture() -> SimpleNamespace:
    run, _ = _v4_source_graph_fixture()
    options = tuple(
        SimpleNamespace(
            rank=index,
            date_pair_id=execution.date_pair.id,
            departure_date=execution.date_pair.departure_date,
            return_date=execution.date_pair.return_date,
            decision_state=PackageDecisionState.ACCEPT,
            recommendable=True,
            total_budget_cents=1_500_000 + index,
            evidence_completeness=Decimal("1"),
            all_platforms_complete=True,
            stay_plan_id=StayPlanId.MAAFUSHI_ICOM,
            option_id=(f"{execution.date_pair.id}:{StayPlanId.MAAFUSHI_ICOM.value}"),
        )
        for index, execution in enumerate(run.pair_runs, start=1)
    )
    return SimpleNamespace(
        **{
            **vars(run),
            "ranked_options": options,
            "recommended_option_ids": tuple(option.option_id for option in options),
        }
    )


def test_recommendable_gate_counts_distinct_real_date_pairs_only() -> None:
    run = _recommendable_fixture()
    assert _check_recommendable_options(run, 2).passed

    duplicate_option_run = SimpleNamespace(
        **{
            **vars(run),
            "ranked_options": (
                run.ranked_options[0],
                run.ranked_options[0],
                run.ranked_options[2],
            ),
            "recommended_option_ids": (
                run.ranked_options[0].option_id,
                run.ranked_options[0].option_id,
                run.ranked_options[2].option_id,
            ),
        }
    )
    assert not _check_recommendable_options(
        duplicate_option_run,
        2,
    ).passed

    first_pair = run.pair_runs[0].date_pair
    second_pair = run.pair_runs[1].date_pair
    duplicate_date_pair = SimpleNamespace(
        **{
            **vars(second_pair),
            "departure_date": first_pair.departure_date,
            "return_date": first_pair.return_date,
        }
    )
    duplicate_date_run = _replace_pair_execution(
        run,
        1,
        date_pair=duplicate_date_pair,
    )
    second_option = run.ranked_options[1]
    duplicate_date_option = SimpleNamespace(
        **{
            **vars(second_option),
            "departure_date": first_pair.departure_date,
            "return_date": first_pair.return_date,
        }
    )
    duplicate_date_run = SimpleNamespace(
        **{
            **vars(duplicate_date_run),
            "ranked_options": (
                run.ranked_options[0],
                duplicate_date_option,
                run.ranked_options[2],
            ),
        }
    )
    assert not _check_recommendable_options(
        duplicate_date_run,
        2,
    ).passed

    mismatched_recommendations = SimpleNamespace(
        **{
            **vars(run),
            "recommended_option_ids": run.recommended_option_ids[:-1],
        }
    )
    assert not _check_recommendable_options(
        mismatched_recommendations,
        2,
    ).passed


def test_two_publication_options_are_rechecked_against_600_second_ttl_after_event() -> None:
    base = _recommendable_fixture()
    options = base.ranked_options[:2]
    refreshed_executions = []
    for index, (execution, option) in enumerate(
        zip(base.pair_runs[:2], options, strict=True),
        start=1,
    ):
        flight = _flight(_intent()).model_copy(
            update={
                "id": f"flight:publication:{index}",
                "expires_at": NOW + timedelta(seconds=600),
            }
        )
        lodging = _lodging(
            quote_id=f"lodging:publication:{index}",
            place=PackagePlaceKey.MAAFUSHI,
            area=PackageArea.DESTINATION_ISLAND,
            total_cents=300_000,
        ).model_copy(update={"expires_at": NOW + timedelta(seconds=600)})
        source_task_ids = (f"publication-source-{index}",)
        publication_run = SimpleNamespace(
            evidence_scope=LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH,
            package=SimpleNamespace(
                final_candidate=SimpleNamespace(
                    flight=flight,
                    lodgings=(lodging,),
                    transfers=(),
                )
            ),
            source_task_ids=source_task_ids,
        )
        refreshed_executions.append(
            SimpleNamespace(
                **{
                    **vars(execution),
                    "run": publication_run,
                    "exploration_run": SimpleNamespace(
                        evidence_scope=LiveEvidenceScope.FULL_SEARCH
                    ),
                    "publication_refresh_audit": SimpleNamespace(
                        binding_passed=True,
                        refreshed_option_id=option.option_id,
                        source_task_ids=source_task_ids,
                    ),
                }
            )
        )
    run = SimpleNamespace(
        **{
            **vars(base),
            "pair_runs": tuple(refreshed_executions),
            "ranked_options": options,
            "recommended_option_ids": tuple(item.option_id for item in options),
            "publication_refresh_minimum_options": 2,
        }
    )

    post_event_gate = _check_recommendable_options(
        run,
        2,
        now=NOW + timedelta(seconds=599),
        maximum_quote_age=timedelta(minutes=15),
    )
    assert post_event_gate.passed
    assert post_event_gate.evidence["freshness_ttl_seconds"] == 600
    assert len(post_event_gate.evidence["freshness_by_option"]) == 2

    expired_gate = _check_recommendable_options(
        run,
        2,
        now=NOW + timedelta(seconds=600),
        maximum_quote_age=timedelta(minutes=15),
    )
    assert not expired_gate.passed


def test_all_recommended_publication_options_receive_equal_deep_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    option_ids = ("option:one", "option:two")
    publications = (
        SimpleNamespace(
            marker="one",
            evidence_scope=LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH,
        ),
        SimpleNamespace(
            marker="two",
            evidence_scope=LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH,
        ),
    )
    observed: list[tuple[str, str]] = []

    def planner_check(publication: SimpleNamespace) -> LiveDoneGateCheck:
        observed.append((publication.marker, "planner"))
        return LiveDoneGateCheck(id="planner", passed=True, summary="passed")

    def budget_check(
        publication: SimpleNamespace,
        *,
        now: datetime,
    ) -> LiveDoneGateCheck:
        assert now == NOW
        observed.append((publication.marker, "budget"))
        return LiveDoneGateCheck(id="budget", passed=True, summary="passed")

    def public_transfer_check(
        exploration: SimpleNamespace,
        publication: SimpleNamespace,
        *,
        now: datetime,
        maximum_quote_age: timedelta,
    ) -> live_done_gate_v4.LiveV4DoneGateCheck:
        assert exploration.marker == publication.marker
        assert now == NOW
        assert maximum_quote_age == timedelta(seconds=600)
        observed.append((publication.marker, "icom"))
        return live_done_gate_v4.LiveV4DoneGateCheck(
            name="icom",
            passed=True,
            summary="passed",
        )

    monkeypatch.setattr(
        live_done_gate_v4,
        "_check_planner_verifier_repair",
        planner_check,
    )
    monkeypatch.setattr(
        live_done_gate_v4,
        "_check_budget_and_evidence",
        budget_check,
    )
    monkeypatch.setattr(
        live_done_gate_v4,
        "_check_v4_public_transfer_evidence",
        public_transfer_check,
    )
    run = SimpleNamespace(
        publication_refresh_minimum_options=2,
        recommended_option_ids=option_ids,
        ranked_options=(
            SimpleNamespace(option_id=option_ids[0], date_pair_id="pair:one"),
            SimpleNamespace(option_id=option_ids[1], date_pair_id="pair:two"),
        ),
        pair_runs=(
            SimpleNamespace(
                date_pair=SimpleNamespace(id="pair:one"),
                run=publications[0],
                exploration_run=publications[0],
            ),
            SimpleNamespace(
                date_pair=SimpleNamespace(id="pair:two"),
                run=publications[1],
                exploration_run=publications[1],
            ),
        ),
    )

    check = _check_all_recommended_publication_closures(run, now=NOW)

    assert check.passed
    assert observed == [
        ("one", "planner"),
        ("one", "budget"),
        ("one", "icom"),
        ("two", "planner"),
        ("two", "budget"),
        ("two", "icom"),
    ]
    assert set(check.evidence["options"]) == set(option_ids)


def test_live_v4_adds_exact_hulhumale_full_source_without_touching_v3_shape() -> None:
    system = LivePackageAgentSystem(BrowserTaskBridge())
    profile = system_stay_area_search_profile("马累")
    assert profile is not None
    candidate_set = system_stay_plan_candidate_set()
    query = BrowserSearchQuery(
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        options={
            "gateway_destination": "马累",
            "stay_area_search_profile": profile.model_dump(mode="json"),
            "stay_plan_candidate_set": candidate_set.model_dump(mode="json"),
        },
    )

    tasks = system._provider_source_tasks(BrowserProvider.CTRIP, query, 120)
    assert len(tasks) == 6
    hulhumale = next(item for item in tasks if item.id.endswith("lodging-hulhumale-full"))
    submission = BrowserTaskSubmission.model_validate(hulhumale.input["submission"])
    assert submission.query.destination == "Hulhumalé"
    assert submission.query.options["expected_lodging_place_key"] == "hulhumale"
    assert submission.query.options["expected_package_area"] == "airport_island"

    v3_query = query.model_copy(
        update={
            "options": {
                "gateway_destination": "马累",
                "stay_area_search_profile": profile.model_dump(mode="json"),
            }
        }
    )
    assert len(system._provider_source_tasks(BrowserProvider.CTRIP, v3_query, 120)) == 5


def test_v4_icom_tasks_are_derived_from_frozen_transfer_contracts() -> None:
    intent = _intent()
    candidate_set = system_stay_plan_candidate_set()
    tasks = LivePackageAgentSystem(BrowserTaskBridge())._icom_source_tasks(
        intent,
        candidate_set,
    )

    assert tuple(item.id for item in tasks) == (
        "public-transfer-icom-continuous-outbound",
        "public-transfer-icom-split-outbound",
        "public-transfer-icom-split-inbound",
        "public-transfer-icom-continuous-inbound",
    )
    assert {
        (
            item.input["icom_query"]["travel_date"],
            item.input["icom_query"]["origin"],
            item.input["icom_query"]["destination"],
        )
        for item in tasks
    } == {
        ("2026-08-12", "Airport", "Maafushi"),
        ("2026-08-13", "Airport", "Maafushi"),
        ("2026-08-17", "Maafushi", "Airport"),
        ("2026-08-18", "Maafushi", "Airport"),
    }


def _canonical_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bounded_receipt_snapshot(
    *,
    receipt_updates: dict[str, object] | None = None,
    receipt_sha256: str | None = None,
) -> BrowserTaskSnapshot:
    task_id = "source-ctrip-lodging-full"
    options = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    query = BrowserSearchQuery(
        origin="杭州",
        destination="Maafushi",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        options=options,
    )
    receipt: dict[str, object] = {
        "schema_version": "tripchord-lodging-inventory-receipt-v1",
        "parser_version": "tripchord-visible-dom-v3",
        "provider": "ctrip",
        "state": "bounded_no_exact_quote",
        "confirmed_query": {
            "destination": "Maafushi",
            "start_date": "2026-08-12",
            "end_date": "2026-08-18",
            "adults": 2,
            "rooms": 1,
            "options": options,
        },
        "confirmation_scope": "confirmed_visible_search",
        "scan_limit": 12,
        "scanned_count": 3,
        "candidate_summaries": [
            {
                "candidate_index": index,
                "title": f"Candidate {index}",
                "area_evidence": None,
                "room_evidence": None,
                "price_evidence": None,
                "price_basis": "unknown",
                "price_finality": "unknown",
            }
            for index in range(3)
        ],
        "explicit_empty_evidence": None,
        "page_url": "https://hotels.ctrip.com/hotels/list",
        "captured_at": NOW.isoformat(),
    }
    if receipt_updates:
        receipt.update(receipt_updates)
    digest = receipt_sha256 or _canonical_sha256(receipt)
    return BrowserTaskSnapshot(
        id=task_id,
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.LODGING,
        query=query,
        state=BrowserTaskState.FAILED,
        created_at=NOW,
        updated_at=NOW,
        attempt_count=1,
        claimed_by="edge-companion-v4",
        claimed_at=NOW,
        failure=BrowserFailure(
            code=BrowserFailureCode.EXTRACTION_ERROR,
            message="detail quotes were not verifiable",
            page_url="https://hotels.ctrip.com/hotels/list",
            captured_at=NOW,
            details={
                "inventory_receipt": receipt,
                "inventory_receipt_sha256": digest,
            },
        ),
    )


def _inventory_outcomes_for_snapshot(
    snapshot: BrowserTaskSnapshot,
) -> tuple[StayPlanInventoryOutcome, ...]:
    task_id = snapshot.id
    state = _RunState(
        source_task_ids=(task_id,),
        stay_plan_candidate_set=system_stay_plan_candidate_set(),
        snapshots={task_id: snapshot},
    )
    return LivePackageAgentSystem(BrowserTaskBridge())._stay_plan_inventory_outcomes(state)


def test_v1_bounded_inventory_receipt_is_sha_sealed_and_crosslinked() -> None:
    snapshot = _bounded_receipt_snapshot()
    outcomes = _inventory_outcomes_for_snapshot(snapshot)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.state == StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE
    assert outcome.scanned_count == 3
    assert outcome.inventory_receipt_sha256 is not None
    assert f"inventory-receipt:sha256:{outcome.inventory_receipt_sha256}" in outcome.evidence_refs
    assert snapshot.failure is not None
    raw = snapshot.failure.details["inventory_receipt"]
    assert isinstance(raw, dict)
    receipt = LodgingInventoryReceipt.model_validate(raw)
    assert receipt.provider == BrowserProvider.CTRIP
    assert lodging_inventory_receipt_sha256(raw) == outcome.inventory_receipt_sha256


def test_frozen_preselection_keeps_terminal_empty_separate_from_usable_quotes() -> None:
    snapshot = _bounded_receipt_snapshot()
    outcomes = _inventory_outcomes_for_snapshot(snapshot)
    task_id = snapshot.id
    state = _RunState(
        source_task_ids=(task_id,),
        stay_plan_candidate_set=system_stay_plan_candidate_set(),
        snapshots={task_id: snapshot},
        stay_plan_inventory_outcomes=outcomes,
    )

    coverage = LivePackageAgentSystem(BrowserTaskBridge())._coverage(state)
    ctrip = next(item for item in coverage if item.provider == BrowserProvider.CTRIP)

    assert task_id in ctrip.terminal_outcome_source_ids
    assert task_id not in ctrip.usable_quote_source_ids
    assert task_id in ctrip.terminal_without_usable_quote_source_ids
    assert BrowserVertical.LODGING not in ctrip.successful_verticals
    assert ctrip.complete is False


def _qunar_confirmed_empty_v2_receipt(
    options: dict[str, str],
) -> tuple[dict[str, object], str]:
    def child(captured_at: datetime) -> dict[str, object]:
        return {
            "schema_version": "tripchord-lodging-inventory-receipt-v1",
            "parser_version": "tripchord-visible-dom-v3",
            "provider": "qunar",
            "state": "confirmed_empty",
            "confirmed_query": {
                "destination": "Maafushi",
                "start_date": "2026-08-12",
                "end_date": "2026-08-18",
                "adults": 2,
                "rooms": 1,
                "options": options,
            },
            "confirmation_scope": "confirmed_visible_search",
            "scan_limit": 12,
            "scanned_count": 0,
            "candidate_summaries": [],
            "explicit_empty_evidence": {
                "contract_version": "qunar-visible-zero-inventory-v1",
                "result_count_text": "共 0 家酒店满足条件",
                "empty_message": "很抱歉，没有找到相关的酒店",
            },
            "provider_pending_evidence": None,
            "page_url": "https://hotel.qunar.com/city/i-ka_maafushi/",
            "captured_at": captured_at.isoformat(),
        }

    first = child(NOW - timedelta(seconds=2))
    second = child(NOW)
    query_fingerprint = _canonical_sha256(first["confirmed_query"])
    confirmed_query = LodgingInventoryConfirmedQuery.model_validate(first["confirmed_query"])
    seed_offset, target_property_ids = qunar_detail_seed_selection(confirmed_query)
    lineage = {
        "schema_version": "tripchord-browser-lineage-hash-v1",
        "isolation_scope": "companion_owned_unfocused_normal_window_active_tab",
        "runtime_lineage_sha256": "1" * 64,
        "window_lineage_sha256": "2" * 64,
        "tab_lineage_sha256": "3" * 64,
    }
    chain = {
        "schema_version": "tripchord-qunar-empty-observation-chain-v1",
        "query_fingerprint_sha256": query_fingerprint,
        "observations": [
            {
                "ordinal": ordinal,
                "receipt": child_receipt,
                "receipt_sha256": _canonical_sha256(child_receipt),
                "captured_at": child_receipt["captured_at"],
                "query_fingerprint_sha256": query_fingerprint,
                "lineage": copy.deepcopy(lineage),
            }
            for ordinal, child_receipt in enumerate((first, second), start=1)
        ],
        "observed_interval_ms": 2_000,
        "detail_fallback": {
            "contract_version": "tripchord-qunar-detail-fallback-summary-v2",
            "attempted": True,
            "target_limit": 2,
            "seed_selection_policy": "query-fingerprint-rotation-v1",
            "seed_selection_offset": seed_offset,
            "target_property_ids": list(target_property_ids),
            "observed_results": [
                {
                    "property_id": property_id,
                    "state": "failed",
                    "verified_quote_count": 0,
                }
                for property_id in target_property_ids
            ],
            "verified_quote_count": 0,
        },
        "sealed_at": NOW.isoformat(),
    }
    parent = {
        **second,
        "schema_version": "tripchord-lodging-inventory-receipt-v2",
        "observation_chain": chain,
    }
    return parent, _canonical_sha256(parent)


def test_qunar_rotating_detail_fallback_is_bound_to_confirmed_query() -> None:
    options = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    receipt, _digest = _qunar_confirmed_empty_v2_receipt(options)
    chain = receipt["observation_chain"]
    assert isinstance(chain, dict)
    chain["detail_fallback"] = {
        "contract_version": "tripchord-qunar-detail-fallback-summary-v2",
        "attempted": True,
        "target_limit": 2,
        "seed_selection_policy": "query-fingerprint-rotation-v1",
        "seed_selection_offset": 4,
        "target_property_ids": ["2075", "2142"],
        "observed_results": [
            {"property_id": "2075", "state": "failed", "verified_quote_count": 0},
            {"property_id": "2142", "state": "failed", "verified_quote_count": 0},
        ],
        "verified_quote_count": 0,
    }

    parsed = LodgingInventoryReceipt.model_validate(receipt)
    assert parsed.observation_chain is not None
    assert parsed.observation_chain.detail_fallback.seed_selection_offset == 4

    tampered = copy.deepcopy(receipt)
    tampered_chain = tampered["observation_chain"]
    assert isinstance(tampered_chain, dict)
    tampered_fallback = tampered_chain["detail_fallback"]
    assert isinstance(tampered_fallback, dict)
    tampered_fallback.update(
        {
            "seed_selection_offset": 5,
            "target_property_ids": ["2142", "2112"],
            "observed_results": [
                {
                    "property_id": "2142",
                    "state": "failed",
                    "verified_quote_count": 0,
                },
                {
                    "property_id": "2112",
                    "state": "failed",
                    "verified_quote_count": 0,
                },
            ],
        }
    )
    with pytest.raises(
        ValidationError,
        match="detail fallback seed selection does not match the confirmed query",
    ):
        LodgingInventoryReceipt.model_validate(tampered)


def test_legacy_qunar_fallback_requires_explicit_historical_parser() -> None:
    options = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    receipt, _digest = _qunar_confirmed_empty_v2_receipt(options)
    chain = receipt["observation_chain"]
    assert isinstance(chain, dict)
    chain["detail_fallback"] = {
        "contract_version": "tripchord-qunar-detail-fallback-summary-v1",
        "attempted": True,
        "target_limit": 2,
        "target_property_ids": ["2112", "2055"],
        "observed_results": [
            {"property_id": "2112", "state": "failed", "verified_quote_count": 0},
            {"property_id": "2055", "state": "failed", "verified_quote_count": 0},
        ],
        "verified_quote_count": 0,
    }

    with pytest.raises(
        ValidationError,
        match="requires the explicit historical parsing path",
    ):
        LodgingInventoryReceipt.model_validate(receipt)
    historical = parse_historical_lodging_inventory_receipt(receipt)
    assert historical.observation_chain is not None
    assert historical.observation_chain.detail_fallback.contract_version.endswith("-v1")


def test_qunar_confirmed_empty_receipt_is_typed_sha_sealed_and_exhaustive() -> None:
    options = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    receipt, digest = _qunar_confirmed_empty_v2_receipt(options)
    snapshot = BrowserTaskSnapshot(
        id="source-qunar-lodging-full",
        provider=BrowserProvider.QUNAR,
        kind=BrowserVertical.LODGING,
        query=BrowserSearchQuery(
            origin="杭州",
            destination="Maafushi",
            start_date=date(2026, 8, 12),
            end_date=date(2026, 8, 18),
            adults=2,
            rooms=1,
            options=options,
        ),
        state=BrowserTaskState.FAILED,
        created_at=NOW,
        updated_at=NOW,
        attempt_count=1,
        claimed_by="edge-companion-v4",
        claimed_at=NOW,
        failure=BrowserFailure(
            code=BrowserFailureCode.NO_INVENTORY,
            message="exact query returned zero hotels",
            page_url="https://hotel.qunar.com/city/i-ka_maafushi/",
            captured_at=NOW,
            details={
                "inventory_result_state": "confirmed_empty",
                "confirmed_exhaustive": True,
                "scanned_count": 0,
                "inventory_receipt": receipt,
                "inventory_receipt_sha256": digest,
                "inventory_observation_chain_schema_version": (
                    "tripchord-qunar-empty-observation-chain-v1"
                ),
                "detail_orchestration": {
                    "state": "stable_empty_no_verified_detail_quote",
                    "verified_quote_count": 0,
                },
            },
        ),
    )

    outcomes = _inventory_outcomes_for_snapshot(snapshot)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.state == StayInventoryResultState.CONFIRMED_EMPTY
    assert outcome.confirmed_exhaustive is True
    assert outcome.scanned_count == 0
    assert outcome.inventory_receipt_sha256 == digest
    assert (
        LodgingInventoryReceipt.model_validate(receipt).state
        == LodgingInventoryReceiptState.CONFIRMED_EMPTY
    )
    candidate_set = system_stay_plan_candidate_set()
    run = SimpleNamespace(
        source_task_ids=(snapshot.id,),
        scheduler=SimpleNamespace(
            results=(
                SimpleNamespace(
                    task_id=snapshot.id,
                    output={"snapshot": snapshot.model_dump(mode="json")},
                ),
            )
        ),
        stay_plan_inventory_outcomes=(outcome,),
        normalization_results=(),
        intent=_intent(),
    )
    assert not _inventory_outcome_evidence_errors(
        run,
        candidate_set,
        now=NOW,
        maximum_quote_age=timedelta(minutes=15),
    )

    tampered_receipt = copy.deepcopy(receipt)
    tampered_chain = tampered_receipt["observation_chain"]
    assert isinstance(tampered_chain, dict)
    observations = tampered_chain["observations"]
    assert isinstance(observations, list)
    observations[1]["lineage"]["runtime_lineage_sha256"] = "9" * 64
    tampered_sha = _canonical_sha256(tampered_receipt)
    assert snapshot.failure is not None
    tampered_snapshot = snapshot.model_copy(
        update={
            "failure": snapshot.failure.model_copy(
                update={
                    "details": {
                        **snapshot.failure.details,
                        "inventory_receipt": tampered_receipt,
                        "inventory_receipt_sha256": tampered_sha,
                    }
                }
            )
        }
    )
    tampered_outcome = outcome.model_copy(
        update={
            "inventory_receipt_sha256": tampered_sha,
            "evidence_refs": (
                f"browser-task:{snapshot.id}",
                f"inventory-receipt:sha256:{tampered_sha}",
            ),
        }
    )
    tampered_run = SimpleNamespace(
        **{
            **vars(run),
            "scheduler": SimpleNamespace(
                results=(
                    SimpleNamespace(
                        task_id=snapshot.id,
                        output={"snapshot": tampered_snapshot.model_dump(mode="json")},
                    ),
                )
            ),
            "stay_plan_inventory_outcomes": (tampered_outcome,),
        }
    )
    assert _inventory_outcome_evidence_errors(
        tampered_run,
        candidate_set,
        now=NOW,
        maximum_quote_age=timedelta(minutes=15),
    )


@pytest.mark.parametrize(
    "legacy_details",
    [
        {
            "capture_code": "expected_place_preview_not_found",
            "candidate_summaries": [{"state": "unverified"}],
        },
        {
            "inventory_result_state": "bounded_no_exact_quote",
            "confirmed_exhaustive": False,
            "scanned_count": 3,
            "candidate_summaries": [{"state": "unverified"}] * 3,
        },
        {
            "parser_version": "tripchord-visible-dom-v3",
            "dom_diagnostics": {
                "scope": "visible_candidate_cards_only",
                "max_candidates": 6,
                "candidates": [{"price_anchor_hits": 1}] * 4,
                "truncated": False,
            },
            "stage_trace": [
                {"stage": "prepare_search", "status": "completed"},
                {"stage": "trigger_search", "status": "completed"},
                {"stage": "list_extraction", "status": "completed"},
            ],
        },
    ],
)
def test_legacy_diagnostics_cannot_prove_inventory(
    legacy_details: dict[str, object],
) -> None:
    snapshot = _bounded_receipt_snapshot()
    assert snapshot.failure is not None
    legacy = snapshot.model_copy(
        update={"failure": snapshot.failure.model_copy(update={"details": legacy_details})}
    )
    assert _inventory_outcomes_for_snapshot(legacy) == ()


@pytest.mark.parametrize(
    ("receipt_updates", "receipt_sha256"),
    [
        ({}, "0" * 64),
        ({"parser_version": "tripchord-visible-dom-v2"}, None),
        ({"confirmation_scope": "trusted_exact_search_url"}, None),
        (
            {
                "scanned_count": 0,
                "candidate_summaries": [],
                "explicit_empty_evidence": None,
            },
            None,
        ),
        ({"explicit_empty_evidence": {"visible_text": "暂无结果"}}, None),
        ({"state": "confirmed_empty"}, None),
        (
            {
                "candidate_summaries": [
                    {
                        "candidate_index": 0,
                        "title": "forged",
                        "area_evidence": None,
                        "room_evidence": None,
                        "price_evidence": None,
                        "price_basis": "unknown",
                        "price_finality": "unknown",
                    }
                ]
            },
            None,
        ),
        (
            {
                "candidate_summaries": [
                    {
                        "candidate_index": index + 1,
                        "title": f"Candidate {index}",
                        "area_evidence": None,
                        "room_evidence": None,
                        "price_evidence": None,
                        "price_basis": "unknown",
                        "price_finality": "unknown",
                    }
                    for index in range(3)
                ]
            },
            None,
        ),
        (
            {
                "candidate_summaries": [
                    {
                        "candidate_index": index,
                        "title": None,
                        "area_evidence": None,
                        "room_evidence": None,
                        "price_evidence": None,
                        "price_basis": "unknown",
                        "price_finality": "unknown",
                    }
                    for index in range(3)
                ]
            },
            None,
        ),
        (
            {
                "candidate_summaries": [
                    {
                        "candidate_index": index,
                        "title": f"Candidate {index}",
                        "area_evidence": None,
                        "room_evidence": None,
                        "price_evidence": None,
                        "price_basis": "per_person",
                        "price_finality": "unknown",
                    }
                    for index in range(3)
                ]
            },
            None,
        ),
        (
            {
                "candidate_summaries": [
                    {
                        "candidate_index": index,
                        "title": f"Candidate {index}",
                        "area_evidence": None,
                        "room_evidence": None,
                        "price_evidence": None,
                        "price_basis": "unknown",
                        "price_finality": "exact_candidate",
                        "extra": "forged",
                    }
                    for index in range(3)
                ]
            },
            None,
        ),
        (
            {
                "confirmed_query": {
                    "destination": "Hulhumalé",
                    "start_date": "2026-08-12",
                    "end_date": "2026-08-18",
                    "adults": 2,
                    "rooms": 1,
                    "options": {
                        "expected_lodging_place_key": "maafushi",
                        "expected_package_area": "destination_island",
                        "segment": "full",
                    },
                }
            },
            None,
        ),
    ],
)
def test_v1_inventory_receipt_fails_closed_on_unproven_or_tampered_evidence(
    receipt_updates: dict[str, object],
    receipt_sha256: str | None,
) -> None:
    snapshot = _bounded_receipt_snapshot(
        receipt_updates=receipt_updates,
        receipt_sha256=receipt_sha256,
    )
    assert _inventory_outcomes_for_snapshot(snapshot) == ()


def test_receipt_hash_rejects_mutation_after_sealing() -> None:
    snapshot = _bounded_receipt_snapshot()
    assert snapshot.failure is not None
    details = copy.deepcopy(snapshot.failure.details)
    receipt = details["inventory_receipt"]
    assert isinstance(receipt, dict)
    receipt["scanned_count"] = 2
    tampered = snapshot.model_copy(
        update={"failure": snapshot.failure.model_copy(update={"details": details})}
    )
    assert _inventory_outcomes_for_snapshot(tampered) == ()


def test_hulhumale_event_requery_keeps_the_exact_full_stay_segment() -> None:
    intent = _intent()
    lodging = _lodging(
        quote_id="lodging:hulhumale:event",
        place=PackagePlaceKey.HULHUMALE,
        area=PackageArea.AIRPORT_ISLAND,
        total_cents=280_000,
    )
    query = BrowserSearchQuery(
        origin=intent.origin,
        destination=intent.destination,
        start_date=intent.start_date,
        end_date=intent.end_date,
        adults=intent.adults,
        rooms=intent.rooms,
    )

    assert (
        LivePackageAgentSystem(BrowserTaskBridge())._segment_name(
            intent,
            query,
            lodging=lodging,
        )
        == "hulhumale-full"
    )


def test_planner_can_choose_only_packages_matching_the_frozen_set() -> None:
    intent = _intent()
    candidate_set = system_stay_plan_candidate_set()
    candidates = PackagePlanner().generate(intent, _inventory(intent))
    matched = {
        stay_plan_for_candidate(candidate_set, intent, candidate) for candidate in candidates
    }

    assert StayPlanId.MAAFUSHI_ICOM in matched
    assert StayPlanId.HULHUMALE_CONTINUOUS in matched
    handoff = StayPlanPlannerHandoff.from_candidates(
        candidate_set,
        intent,
        candidates,
        candidates[0].id,
    )
    assert handoff.selected_stay_plan_id == StayPlanId.HULHUMALE_CONTINUOUS
    assert handoff.selected_candidate_id == candidates[0].id


def test_empty_raw_like_inventory_explains_exact_lodging_and_transfer_gaps() -> None:
    intent = _intent()
    airport = PackagePlaceKey.VELANA_AIRPORT
    maafushi = PackagePlaceKey.MAAFUSHI
    first = _lodging(
        quote_id="lodging:split:first",
        place=PackagePlaceKey.HULHUMALE,
        area=PackageArea.AIRPORT_ISLAND,
        total_cents=80_000,
    ).model_copy(update={"check_out": intent.start_date + timedelta(days=1)})
    middle = _lodging(
        quote_id="lodging:split:middle",
        place=maafushi,
        area=PackageArea.DESTINATION_ISLAND,
        total_cents=280_000,
    ).model_copy(
        update={
            "check_in": intent.start_date + timedelta(days=1),
            "check_out": intent.end_date - timedelta(days=1),
        }
    )
    last = _lodging(
        quote_id="lodging:split:last",
        place=PackagePlaceKey.HULHUMALE,
        area=PackageArea.AIRPORT_ISLAND,
        total_cents=90_000,
    ).model_copy(update={"check_in": intent.end_date - timedelta(days=1)})
    transfers = (
        _transfer(
            transfer_id="icom:continuous:out",
            provider="icom-public-transfer",
            origin=airport,
            destination=maafushi,
            origin_area=PackageArea.AIRPORT,
            destination_area=PackageArea.DESTINATION_ISLAND,
            service_date=intent.start_date,
            depart_hour=13,
            arrive_hour=14,
            guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        ),
        _transfer(
            transfer_id="icom:split:out",
            provider="icom-public-transfer",
            origin=airport,
            destination=maafushi,
            origin_area=PackageArea.AIRPORT,
            destination_area=PackageArea.DESTINATION_ISLAND,
            service_date=intent.start_date + timedelta(days=1),
            depart_hour=13,
            arrive_hour=14,
            guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        ),
        _transfer(
            transfer_id="icom:split:back",
            provider="icom-public-transfer",
            origin=maafushi,
            destination=airport,
            origin_area=PackageArea.DESTINATION_ISLAND,
            destination_area=PackageArea.AIRPORT,
            service_date=intent.end_date - timedelta(days=1),
            depart_hour=13,
            arrive_hour=14,
            guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        ),
        _transfer(
            transfer_id="icom:continuous:back",
            provider="icom-public-transfer",
            origin=maafushi,
            destination=airport,
            origin_area=PackageArea.DESTINATION_ISLAND,
            destination_area=PackageArea.AIRPORT,
            service_date=intent.end_date,
            depart_hour=13,
            arrive_hour=14,
            guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
        ),
    )
    inventory = PackageInventory(
        flights=(_flight(intent),),
        lodgings=(first, middle, last),
        transfers=transfers,
    )

    generation = PackagePlanner().generate_bounded(intent, inventory)

    assert generation.audit.raw_structural_candidate_upper_bound > 0
    assert generation.candidates == ()
    assert generation.audit.policy_version == "package-candidate-beam-v3"
    assert "global:no_candidate_after_hard_contract_join" in (generation.audit.rejection_reasons)
    assert any(
        reason.startswith("continuous_island:lodging:destination_island:")
        for reason in generation.audit.rejection_reasons
    )
    assert any(
        reason.startswith("split_airport_island:transfer:airport:airport_island:")
        for reason in generation.audit.rejection_reasons
    )
    # USD iCom published base fares remain supplemental contracts.  The audit
    # must not invent FX/taxes or mislabel those exact legs as absent.
    assert not any(
        "airport:destination_island" in reason and "no_compatible_hard_contract" in reason
        for reason in generation.audit.rejection_reasons
    )

    handoff = StayPlanPlannerHandoff.from_candidates(
        system_stay_plan_candidate_set(),
        intent,
        (),
        None,
        inventory=inventory,
    )
    evaluations = {item.stay_plan_id: item for item in handoff.evaluations}
    maafushi_reasons = evaluations[StayPlanId.MAAFUSHI_ICOM].rejection_reasons
    split_reasons = evaluations[StayPlanId.MAAFUSHI_SPLIT_HULHUMALE].rejection_reasons
    assert any("maafushi-full:no_exact_normalized_lodging" in item for item in maafushi_reasons)
    assert not any("icom-continuous" in item for item in maafushi_reasons)
    assert any("airport-hulhumale-first:no_exact_hard_contract" in item for item in split_reasons)
    assert any(
        "hulhumale-airport-return-day:no_exact_hard_contract" in item for item in split_reasons
    )


def test_fragility_repair_can_choose_continuous_hulhumale_from_frozen_pool() -> None:
    intent = _intent()
    candidates = PackagePlanner().generate(intent, _inventory(intent))
    rejected = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    hulhumale = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND
    )
    violation = PackageViolation(
        code=PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
        severity=PackageViolationSeverity.ERROR,
        message="fixture structured fragility",
        component_ids=(rejected.flight.id,),
    )

    repaired = PackageRepairer().repair_from_rejection(
        intent,
        rejected,
        (rejected, hulhumale),
        (violation,),
    )

    assert repaired.candidate is not None
    assert repaired.candidate.kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND
    assert repaired.candidate.parent_candidate_id == rejected.id
    assert repaired.diff is not None and repaired.diff.changed


def test_stay_plan_handoff_rejects_master_bypass_or_plan_swap() -> None:
    intent = _intent()
    candidate_set = system_stay_plan_candidate_set()
    candidates = PackagePlanner().generate(intent, _inventory(intent))
    selected = candidates[0]
    selected_plan = stay_plan_for_candidate(candidate_set, intent, selected)
    assert selected_plan is not None
    planner = StayPlanPlannerHandoff.from_candidates(
        candidate_set,
        intent,
        candidates,
        selected.id,
    )
    initial_package = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.INITIAL,
        candidate=selected,
        violations=(),
        verified_at=NOW,
    )
    reverified_package = PackageVerificationHandoff.from_candidate(
        phase=PackageVerificationPhase.REVERIFICATION,
        candidate=selected,
        violations=(),
        verified_at=NOW,
    )
    initial = StayPlanVerificationHandoff.from_package_handoff(
        candidate_set=candidate_set,
        stay_plan_id=selected_plan,
        package_handoff=initial_package,
    )
    reverified = StayPlanVerificationHandoff.from_package_handoff(
        candidate_set=candidate_set,
        stay_plan_id=selected_plan,
        package_handoff=reverified_package,
    )
    repair = StayPlanRepairHandoff(
        candidate_set_sha256=candidate_set.candidate_set_sha256,
        rejected_stay_plan_id=selected_plan,
        rejected_candidate_id=selected.id,
        attempted=False,
        repaired_stay_plan_id=selected_plan,
        repaired_candidate_id=selected.id,
    )
    handoff = StayPlanPlanningHandoff(
        planner=planner,
        initial_verification=initial,
        repair=repair,
        reverification=reverified,
    )
    assert handoff.reverification is not None

    damaged = handoff.model_dump(mode="json")
    damaged["reverification"]["stay_plan_id"] = StayPlanId.MAAFUSHI_ICOM.value
    with pytest.raises(ValidationError, match="exact stay-plan Repair output"):
        StayPlanPlanningHandoff.model_validate(damaged)

    without_reverification = handoff.model_dump(mode="json")
    without_reverification["reverification"] = None
    with pytest.raises(ValidationError, match="without ReVerifier"):
        StayPlanPlanningHandoff.model_validate(without_reverification)


@pytest.mark.parametrize(
    ("state", "quote_ids", "confirmed_exhaustive"),
    [
        (StayInventoryResultState.QUOTE_FOUND, ("quote:1",), False),
        (StayInventoryResultState.CONFIRMED_EMPTY, (), True),
        (StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE, (), False),
    ],
)
def test_inventory_result_states_preserve_claim_boundaries(
    state: StayInventoryResultState,
    quote_ids: tuple[str, ...],
    confirmed_exhaustive: bool,
) -> None:
    raw_sha = "b" * 64
    receipt_sha = "a" * 64
    task_id = "source-ctrip-lodging-full"
    outcome = StayPlanInventoryOutcome(
        source_task_id=task_id,
        provider="ctrip",
        stay_plan_id=StayPlanId.MAAFUSHI_ICOM,
        segment_id="maafushi-full",
        state=state,
        exact_place_key=PackagePlaceKey.MAAFUSHI,
        scan_limit=12,
        scanned_count=10,
        quote_ids=quote_ids,
        normalization_result_refs=(
            (f"normalization-result:{task_id}:quote:1",)
            if state == StayInventoryResultState.QUOTE_FOUND
            else ()
        ),
        raw_snapshot_id=(task_id if state == StayInventoryResultState.QUOTE_FOUND else None),
        raw_quote_evidence_sha256s=(
            (raw_sha,) if state == StayInventoryResultState.QUOTE_FOUND else ()
        ),
        inventory_receipt_sha256=(
            None if state == StayInventoryResultState.QUOTE_FOUND else receipt_sha
        ),
        evidence_refs=(
            (
                f"browser-task:{task_id}",
                f"browser:ctrip:sha256:{raw_sha}",
            )
            if state == StayInventoryResultState.QUOTE_FOUND
            else (
                f"browser-task:{task_id}",
                f"inventory-receipt:sha256:{receipt_sha}",
            )
        ),
        confirmed_exhaustive=confirmed_exhaustive,
        reason="fixture",
    )
    assert outcome.state == state


def test_bounded_empty_cannot_be_upgraded_to_confirmed_empty() -> None:
    task_id = "source-ctrip-lodging-full"
    receipt_sha = "a" * 64
    with pytest.raises(ValidationError, match="bounded_no_exact_quote"):
        StayPlanInventoryOutcome(
            source_task_id=task_id,
            provider="ctrip",
            stay_plan_id=StayPlanId.MAAFUSHI_ICOM,
            segment_id="maafushi-full",
            state=StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE,
            exact_place_key=PackagePlaceKey.MAAFUSHI,
            scan_limit=12,
            scanned_count=12,
            inventory_receipt_sha256=receipt_sha,
            evidence_refs=(
                f"browser-task:{task_id}",
                f"inventory-receipt:sha256:{receipt_sha}",
            ),
            confirmed_exhaustive=True,
            reason="fixture",
        )


def _inventory_gate_fixture(
    *,
    selected_provider_states: dict[str, StayInventoryResultState],
) -> tuple[StayPlanCandidateSet, SimpleNamespace]:
    candidate_set = system_stay_plan_candidate_set()
    selected = StayPlanId.HULHUMALE_CONTINUOUS
    outcomes: list[StayPlanInventoryOutcome] = []
    for plan in candidate_set.candidates:
        for segment in plan.segments:
            for provider in ("ctrip", "qunar"):
                state = (
                    selected_provider_states[provider]
                    if plan.stay_plan_id == selected
                    else StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE
                )
                exact = state == StayInventoryResultState.QUOTE_FOUND
                task_id = f"source-{provider}-lodging-{segment.query_segment}"
                quote_id = f"quote:{provider}:{plan.stay_plan_id.value}:{segment.segment_id}"
                raw_sha = hashlib.sha256(quote_id.encode()).hexdigest()
                receipt_sha = hashlib.sha256(
                    f"{task_id}:{plan.stay_plan_id.value}:{state.value}".encode()
                ).hexdigest()
                outcomes.append(
                    StayPlanInventoryOutcome(
                        source_task_id=task_id,
                        provider=provider,
                        stay_plan_id=plan.stay_plan_id,
                        segment_id=segment.segment_id,
                        state=state,
                        exact_place_key=segment.exact_place_key,
                        scan_limit=plan.scan_limit_per_platform,
                        scanned_count=1,
                        quote_ids=((quote_id,) if exact else ()),
                        normalization_result_refs=(
                            (f"normalization-result:{task_id}:{quote_id}",) if exact else ()
                        ),
                        raw_snapshot_id=task_id if exact else None,
                        raw_quote_evidence_sha256s=((raw_sha,) if exact else ()),
                        inventory_receipt_sha256=(None if exact else receipt_sha),
                        evidence_refs=(
                            (
                                f"browser-task:{task_id}",
                                f"browser:{provider}:sha256:{raw_sha}",
                            )
                            if exact
                            else (
                                f"browser-task:{task_id}",
                                f"inventory-receipt:sha256:{receipt_sha}",
                            )
                        ),
                        confirmed_exhaustive=(state == StayInventoryResultState.CONFIRMED_EMPTY),
                        reason="fixture",
                    )
                )
    run = SimpleNamespace(
        pair_runs=(
            SimpleNamespace(
                date_pair=SimpleNamespace(id="date-pair:v4"),
                run=SimpleNamespace(
                    stay_plan_inventory_outcomes=tuple(outcomes),
                    selected_stay_plan_id=selected,
                ),
            ),
        )
    )

    return candidate_set, run


def test_inventory_gate_requires_exactly_two_providers_for_each_segment() -> None:
    candidate_set, run = _inventory_gate_fixture(
        selected_provider_states={
            "ctrip": StayInventoryResultState.QUOTE_FOUND,
            "qunar": StayInventoryResultState.QUOTE_FOUND,
        }
    )

    check = _check_inventory_outcome_contract(
        run,
        candidate_set,
        minimum_exact_providers_per_selected_segment=2,
    )
    assert check.passed
    assert check.name == "stay_inventory_four_state_contract"
    assert all(
        state in check.summary
        for state in (
            "exact_quote",
            "confirmed_empty",
            "bounded_no_exact_quote",
            "bounded_provider_pending",
        )
    )


@pytest.mark.parametrize(
    "second_provider_state",
    [
        StayInventoryResultState.CONFIRMED_EMPTY,
        StayInventoryResultState.BOUNDED_PROVIDER_PENDING,
    ],
)
def test_inventory_gate_rejects_one_exact_quote_plus_nonquote_state(
    second_provider_state: StayInventoryResultState,
) -> None:
    candidate_set, run = _inventory_gate_fixture(
        selected_provider_states={
            "ctrip": StayInventoryResultState.QUOTE_FOUND,
            "qunar": second_provider_state,
        }
    )

    check = _check_inventory_outcome_contract(
        run,
        candidate_set,
        minimum_exact_providers_per_selected_segment=2,
    )

    assert not check.passed
    assert check.name == "stay_inventory_four_state_contract"
    assert "精确报价不足 2 家" in check.summary
    assert "=1家" in check.summary


@pytest.mark.parametrize("threshold", [1, 3])
def test_inventory_gate_rejects_non_strict_exact_provider_threshold(
    threshold: int,
) -> None:
    candidate_set, run = _inventory_gate_fixture(
        selected_provider_states={
            "ctrip": StayInventoryResultState.QUOTE_FOUND,
            "qunar": StayInventoryResultState.QUOTE_FOUND,
        }
    )

    with pytest.raises(ValueError, match="threshold is frozen at 2"):
        _check_inventory_outcome_contract(
            run,
            candidate_set,
            minimum_exact_providers_per_selected_segment=threshold,
        )


def test_strict_done_gate_rejects_one_exact_provider_configuration() -> None:
    with pytest.raises(ValueError, match="threshold is frozen at 2"):
        live_done_gate_v4.evaluate_live_v4_done_gate(
            SimpleNamespace(),
            expected_candidate_set=system_stay_plan_candidate_set(),
            minimum_exact_providers_per_selected_segment=1,
        )


def _timeout_without_receipt_snapshot(
    *,
    provider: BrowserProvider = BrowserProvider.QUNAR,
    segment: str = "full",
) -> BrowserTaskSnapshot:
    task_id = f"source-{provider.value}-lodging-{segment}"
    options = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": segment,
    }
    query = BrowserSearchQuery(
        origin="杭州",
        destination="Maafushi",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        options=options,
    )
    return BrowserTaskSnapshot(
        id=task_id,
        provider=provider,
        kind=BrowserVertical.LODGING,
        query=query,
        state=BrowserTaskState.FAILED,
        created_at=NOW,
        updated_at=NOW,
        attempt_count=1,
        claimed_by="edge-companion-v4",
        claimed_at=NOW,
        failure=BrowserFailure(
            code=BrowserFailureCode.TIMEOUT,
            message="browser companion did not complete the task before its lease expired",
            page_url=None,
            captured_at=NOW,
            details={},
        ),
    )


def test_counterexample_timeout_without_receipt_drops_four_state_row() -> None:
    """Counter-example for the layer-6 defect this round fixes.

    A Qunar lodging task whose lease expired mid-search produced no terminal
    receipt; _stay_plan_inventory_outcomes therefore emits NO four-state row
    for that provider/segment, which fails the stay_inventory_four_state_contract
    (missing outcome).  The frozen 120s lease is kept; the closure is that a
    fast-failed extraction preserves the exact result tab and the retry reuses
    it for a full-budget extraction that seals a bounded receipt
    (quote/empty/pending) — never a silent lease bump.
    """
    snapshot = _timeout_without_receipt_snapshot()
    outcomes = _inventory_outcomes_for_snapshot(snapshot)

    assert outcomes == ()
    assert snapshot.failure is not None
    assert snapshot.failure.code == BrowserFailureCode.TIMEOUT
    assert "inventory_receipt" not in snapshot.failure.details


def test_lodging_source_tasks_keep_frozen_request_lease() -> None:
    """Regression guard for the frozen 120s single-task lease.

    Lodging source submissions must keep the exact request-level timeout
    (120s) — the C-98 bump that raised lodging leases to 240/300s violated the
    frozen contract and must not regress.  Flight source submissions also keep
    the exact request lease.
    """
    system = LivePackageAgentSystem(BrowserTaskBridge())
    profile = system_stay_area_search_profile("马累")
    assert profile is not None
    query = BrowserSearchQuery(
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        options={
            "gateway_destination": "马累",
            "stay_area_search_profile": profile.model_dump(mode="json"),
            "stay_plan_candidate_set": system_stay_plan_candidate_set().model_dump(mode="json"),
        },
    )

    for provider in (BrowserProvider.CTRIP, BrowserProvider.QUNAR):
        for segment in ("full", "first", "middle", "last", "hulhumale-full"):
            task = system._source_task(
                provider,
                BrowserVertical.LODGING,
                query,
                120,
                segment=segment,
            )
            submission = BrowserTaskSubmission.model_validate(task.input["submission"])
            assert submission.timeout_seconds == 120, (
                f"{provider.value}/{segment} got lease "
                f"{submission.timeout_seconds}, expected the frozen 120s"
            )

    flight_task = system._source_task(
        BrowserProvider.CTRIP,
        BrowserVertical.FLIGHT,
        query,
        120,
    )
    flight_submission = BrowserTaskSubmission.model_validate(
        flight_task.input["submission"]
    )
    assert flight_submission.timeout_seconds == 120


def test_counterexample_preserved_tab_triggers_retry_reuse() -> None:
    """Counter-example for the 120s closure.

    A Qunar lodging task that fast-failed (retryable timeout) and preserved its
    exact result tab must mark the retry for tab reuse; the companion then skips
    landing/trigger and spends the full fresh budget on extraction.  Without the
    preserved tab no reuse is triggered and the four-state row stays missing.
    """
    from tripchord.agents.live_system import (
        _should_reuse_lodging_result_tab,
        _with_reuse_lodging_result_tab,
    )

    system = LivePackageAgentSystem(BrowserTaskBridge())
    profile = system_stay_area_search_profile("马累")
    assert profile is not None
    query = BrowserSearchQuery(
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        start_date=date(2026, 8, 12),
        end_date=date(2026, 8, 18),
        adults=2,
        rooms=1,
        options={
            "gateway_destination": "马累",
            "stay_area_search_profile": profile.model_dump(mode="json"),
            "stay_plan_candidate_set": system_stay_plan_candidate_set().model_dump(mode="json"),
        },
    )
    task = system._source_task(
        BrowserProvider.QUNAR,
        BrowserVertical.LODGING,
        query,
        120,
        segment="full",
    )
    submission = BrowserTaskSubmission.model_validate(task.input["submission"])
    assert submission.timeout_seconds == 120

    preserved = _timeout_without_receipt_snapshot(provider=BrowserProvider.QUNAR)
    preserved = preserved.model_copy(
        update={
            "failure": BrowserFailure(
                code=BrowserFailureCode.TIMEOUT,
                message="stage_timeout: remaining lease below extraction minimum budget",
                page_url="https://touch.qunar.com/hotel/list",
                captured_at=NOW,
                retryable=True,
                details={
                    "inventory_result_state": "bounded_provider_pending",
                    "preserved_exact_result_tab": {
                        "provider": "qunar",
                        "kind": "lodging",
                        "tab_id": 42,
                        "url": "https://touch.qunar.com/hotel/list",
                    },
                },
            )
        }
    )
    assert _should_reuse_lodging_result_tab(preserved, submission) is True

    reused = _with_reuse_lodging_result_tab(submission)
    assert reused.query.options.get("__tripchord_reuse_exact_result_tab") is True
    assert reused.timeout_seconds == 120

    # Without the preserved tab the same failure is not eligible for reuse.
    bare = _timeout_without_receipt_snapshot(provider=BrowserProvider.QUNAR)
    assert _should_reuse_lodging_result_tab(bare, submission) is False
