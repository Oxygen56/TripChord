from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import product
from typing import Any

import httpx
import pytest
import tripchord.main as main_module
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
    LivePackageAgentSystem,
    PlatformSearchCoverage,
    _RunState,
)
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelHTTPError,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleChatClient,
)
from tripchord.agents.models import (
    AgentRole,
    AgentTask,
    AgentTaskResult,
    PreferenceMode,
    TaskGraph,
)
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.config import Settings
from tripchord.main import (
    LiveRunCache,
    _flexible_total_timeout_seconds,
    _install_browser_bridge,
    _install_current_complex_offer_provider,
    _live_timeout_seconds,
    app,
    package_requirement_agent,
    settings,
)
from tripchord.planning.complex_trip import (
    BundleOffer,
    ComplexCatalogSolver,
    OfferCatalog,
    PlanningCompiler,
    PriceContract,
    SourceState,
    SourceStatus,
    StayOffer,
    TransportOffer,
    parse_complex_intent,
    validate_plan_graph,
)
from tripchord.planning.package import (
    LodgingLocationConvenience,
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
    PackagePlaceKey,
    QuoteAvailability,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
)
from tripchord.planning.stay_plans import (
    StayInventoryResultState,
    StayPlanId,
    StayPlanInventoryOutcome,
    system_stay_plan_candidate_set,
)
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserSearchQuery,
    BrowserVertical,
)
from tripchord.providers.current_complex import CurrentComplexOfferProvider
from tripchord.providers.icom_transfer import IComCnyReferenceEstimate
from tripchord.providers.quote_normalizer import (
    NormalizedBrowserQuoteResult,
    QuoteNormalizationStatus,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
ORIGINAL_REQUEST = """出发地：杭州
目的地：马累
去程：2026-8月
返程：玩5-8天
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算、早餐无要求、星级无要求、无行李、接受中转"""
FIXED_DATE_REQUEST = """出发地：杭州
目的地：马累
去程：2026-09-03
返程：2026-09-09
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算"""


def _fixture_icom_cny_reference_estimate() -> IComCnyReferenceEstimate:
    return IComCnyReferenceEstimate(
        rate_date=date(2026, 8, 22),
        captured_at=NOW,
        usd_per_eur=Decimal("1"),
        cny_per_eur=Decimal("6.720583"),
        usd_to_cny_reference_rate=Decimal("6.720583"),
        source_usd_base_fare_cents=12_000,
        estimated_cny_cents=80_647,
        price_contract_ids=("icom-contract-outbound", "icom-contract-return"),
        transfer_ids=("icom-outbound", "icom-return"),
        response_sha256="c" * 64,
    )


def _source_ids() -> tuple[str, ...]:
    return tuple(
        f"source-{provider.value}-{suffix}"
        for provider in BrowserProvider
        for suffix in (
            "flight",
            "lodging-full",
            "lodging-first",
            "lodging-middle",
            "lodging-last",
        )
    )


def _blocked_run(
    intent: PackageIntent,
    query: BrowserSearchQuery,
    mode: LiveCoverageMode,
) -> LivePackageAgentRun:
    source_ids = _source_ids()
    coverage = tuple(
        PlatformSearchCoverage(
            provider=provider,
            failed_verticals=(BrowserVertical.FLIGHT, BrowserVertical.LODGING),
            failed_source_ids=tuple(
                source_id
                for source_id in source_ids
                if source_id.startswith(f"source-{provider.value}-")
            ),
            failure_reasons=("API contract fixture does not access a real browser",),
            complete=False,
        )
        for provider in BrowserProvider
    )
    final_tasks = (
        AgentTask(
            id="orchestrate-travel-package",
            role=AgentRole.SAFETY_GATE,
            goal="fixture deterministic decision",
        ),
        AgentTask(
            id="explain-final-decision",
            role=AgentRole.EXPLANATION,
            goal="fixture explanation",
            dependencies=("orchestrate-travel-package",),
        ),
        AgentTask(
            id="curate-run-memory",
            role=AgentRole.MEMORY_CURATOR,
            goal="fixture memory curation",
            dependencies=("explain-final-decision",),
        ),
        AgentTask(
            id="publish-live-run",
            role=AgentRole.SAFETY_GATE,
            goal="fixture publication gate",
            dependencies=("curate-run-memory",),
        ),
    )
    return LivePackageAgentRun(
        mode=mode,
        intent=intent,
        search_query=query,
        decision=PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="fixture browser search is intentionally blocked",
        ),
        claim_boundary="API contract fixture only; no live coverage claim",
        all_platforms_complete=False,
        coverage=coverage,
        inventory=PackageInventory(),
        normalization_results=(),
        package=None,
        scheduler=SchedulerOutcome(
            graph=TaskGraph(tasks=final_tasks),
            results=tuple(
                AgentTaskResult(
                    task_id=task.id,
                    agent_role=task.role,
                    success=True,
                    summary="fixture stage complete",
                    output={"publication_gate_passed": True}
                    if task.id == "publish-live-run"
                    else {},
                )
                for task in final_tasks
            ),
            trace=(),
            wall_time_seconds=0,
            max_parallel_tasks=15,
            succeeded=True,
        ),
        source_task_ids=source_ids,
    )


class _RecordingPairRunner:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                PackageIntent,
                BrowserSearchQuery,
                LiveCoverageMode,
                int,
                dict[str, int] | None,
            ]
        ] = []

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        self.calls.append((intent, query, mode, timeout_seconds, source_start_delays_ms))
        return _blocked_run(intent, query, mode)


def _two_candidate_live_run(
    intent: PackageIntent,
    query: BrowserSearchQuery,
    mode: LiveCoverageMode,
    *,
    kaani_reference_complete: bool = True,
) -> LivePackageAgentRun:
    """Build two complete, non-executable candidates for the public API test."""

    captured_at = datetime(2026, 8, 22, 22, 48, tzinfo=UTC)
    flight = NormalizedFlightQuote(
        id="flight-qunar-jd5907-jd455-jd456-jd5908",
        provider="qunar",
        currency="CNY",
        total_for_party_cents=846_000,
        display_amount_cents=846_000,
        taxes_and_fees_included=True,
        captured_at=captured_at,
        expires_at=captured_at + timedelta(minutes=10),
        availability=QuoteAvailability.COMPARISON_ONLY,
        evidence_refs=("flight-party-comparison:sha256:" + "a" * 64,),
        origin="杭州",
        destination="马累",
        adults=2,
        party_availability_confirmed=False,
        party_total_known=True,
        price_basis="comparison_only",
        outbound_depart_at=datetime(2026, 9, 3, 21, 45, tzinfo=UTC),
        outbound_arrive_at=datetime(2026, 9, 4, 12, 20, tzinfo=UTC),
        return_depart_at=datetime(2026, 9, 9, 23, 10, tzinfo=UTC),
        return_arrive_at=datetime(2026, 9, 10, 9, 25, tzinfo=UTC),
        outbound_flight_numbers=("JD5907", "JD455"),
        return_flight_numbers=("JD456", "JD5908"),
        origin_airport_code="HGH",
        destination_airport_code="MLE",
    )

    ctrip = NormalizedLodgingQuote(
        id="lodging-ctrip-maafushi",
        provider="ctrip",
        currency="CNY",
        total_for_party_cents=260_500,
        taxes_and_fees_included=True,
        captured_at=captured_at,
        expires_at=captured_at + timedelta(minutes=10),
        availability=QuoteAvailability.AVAILABLE,
        evidence_refs=("https://hotels.ctrip.com/hotels/detail/?hotelId=47330536",),
        property_name="Kaani Beach Hotel",
        area=PackageArea.DESTINATION_ISLAND,
        place_key=PackagePlaceKey.MAAFUSHI,
        check_in=date(2026, 9, 4),
        check_out=date(2026, 9, 9),
        adults=2,
        rooms=1,
        room_name="海景豪华双人房带阳台",
        breakfast_included=True,
        cancellation_policy="免费取消",
        location_address="Aabaadhee Hingun Road, Maafushi",
        nearby_location_evidence=("near the main ferry jetty",),
        location_convenience=LodgingLocationConvenience.CONFIRMED_NOT_REMOTE,
    )
    kaani_payload = {
        **ctrip.model_dump(mode="python"),
        **{
            "id": "lodging-kaani-official-maafushi",
            "provider": "kaani_official",
            "currency": "USD",
            "total_for_party_cents": 54_650,
            "evidence_refs": ("https://kaanihotels.com/stays/Beach-Hotel/book",),
            "reference_total_cents": 367_279,
            "reference_currency": "CNY",
            "reference_rate_source": "European Central Bank",
            "reference_rate_date": date(2026, 8, 22),
            "reference_usd_to_cny": Decimal("6.720583"),
            "reference_rate_response_sha256": "b" * 64,
            "reference_rate_captured_at": captured_at,
        },
    }
    if not kaani_reference_complete:
        kaani_payload["reference_rate_response_sha256"] = None
    kaani = NormalizedLodgingQuote.model_validate(kaani_payload)

    def transfer(
        *,
        transfer_id: str,
        contract_id: str,
        origin_area: PackageArea,
        destination_area: PackageArea,
        origin_place_key: PackagePlaceKey,
        destination_place_key: PackagePlaceKey,
        service_date: date,
        depart_at: datetime,
        arrive_at: datetime,
    ) -> TransferOption:
        return TransferOption(
            id=transfer_id,
            provider="icom-public-transfer",
            currency="USD",
            total_for_party_cents=6_000,
            taxes_and_fees_included=None,
            captured_at=captured_at,
            expires_at=captured_at + timedelta(minutes=10),
            availability=QuoteAvailability.AVAILABLE,
            evidence_refs=(
                "https://sfs-api.icomtours.com/api/v1/public/ferry-fares/schedule-base-price",
            ),
            origin_area=origin_area,
            destination_area=destination_area,
            origin_place_key=origin_place_key,
            destination_place_key=destination_place_key,
            adults=2,
            service_date=service_date,
            schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
            duration_minutes=90,
            depart_at=depart_at,
            arrive_at=arrive_at,
            operates_24_hours=False,
            requires_reservation=True,
            price_scope=TransferPriceScope.ROUND_TRIP,
            price_contract_id=contract_id,
            purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
            price_guarantee=TransferPriceGuarantee.PUBLISHED_BASE_FARE,
            contract_evidence_text="公开 USD 基础价，税费未知",
            detail_url="https://sfs-api.icomtours.com/api/v1/public/ferry-fares/schedule-base-price",
        )

    transfers = (
        transfer(
            transfer_id="icom-outbound",
            contract_id="icom-contract-outbound",
            origin_area=PackageArea.AIRPORT,
            destination_area=PackageArea.DESTINATION_ISLAND,
            origin_place_key=PackagePlaceKey.VELANA_AIRPORT,
            destination_place_key=PackagePlaceKey.MAAFUSHI,
            service_date=date(2026, 9, 4),
            depart_at=datetime(2026, 9, 4, 14, 30, tzinfo=UTC),
            arrive_at=datetime(2026, 9, 4, 16, 0, tzinfo=UTC),
        ),
        transfer(
            transfer_id="icom-return",
            contract_id="icom-contract-return",
            origin_area=PackageArea.DESTINATION_ISLAND,
            destination_area=PackageArea.AIRPORT,
            origin_place_key=PackagePlaceKey.MAAFUSHI,
            destination_place_key=PackagePlaceKey.VELANA_AIRPORT,
            service_date=date(2026, 9, 9),
            depart_at=datetime(2026, 9, 9, 18, 0, tzinfo=UTC),
            arrive_at=datetime(2026, 9, 9, 19, 30, tzinfo=UTC),
        ),
    )

    stay_plan_set = system_stay_plan_candidate_set(frozen_at=captured_at)
    ctrip_outcome = StayPlanInventoryOutcome(
        source_task_id="source-ctrip-lodging-full",
        provider="ctrip",
        stay_plan_id=StayPlanId.MAAFUSHI_ICOM,
        segment_id="maafushi-full",
        state=StayInventoryResultState.QUOTE_FOUND,
        exact_place_key=PackagePlaceKey.MAAFUSHI,
        scan_limit=12,
        scanned_count=1,
        quote_ids=(ctrip.id,),
        normalization_result_refs=("normalization-ctrip-maafushi",),
        raw_snapshot_id="snapshot-ctrip-maafushi",
        raw_quote_evidence_sha256s=("d" * 64,),
        evidence_refs=(
            "browser-task:snapshot-ctrip-maafushi",
            "browser:ctrip:sha256:" + "d" * 64,
        ),
        reason="fixture quote found",
    )
    kaani_result = NormalizedBrowserQuoteResult(
        provider="kaani_official",
        kind=BrowserVertical.LODGING,
        status=QuoteNormalizationStatus.USABLE,
        quote=kaani,
    )
    state = _RunState(
        source_task_ids=("source-ctrip-lodging-full", "source-kaani-official-lodging"),
        intent=intent,
        mode=mode,
        stay_plan_candidate_set=stay_plan_set,
        inventory=PackageInventory(flights=(flight,), lodgings=(ctrip, kaani), transfers=transfers),
        stay_plan_inventory_outcomes=(ctrip_outcome,),
        kaani_lodging_results=(kaani_result,),
        normalization_results=(
            NormalizedBrowserQuoteResult(
                provider="qunar",
                kind=BrowserVertical.FLIGHT,
                status=QuoteNormalizationStatus.USABLE,
                quote=flight,
            ),
            NormalizedBrowserQuoteResult(
                provider="ctrip",
                kind=BrowserVertical.LODGING,
                status=QuoteNormalizationStatus.USABLE,
                quote=ctrip,
            ),
            kaani_result,
        ),
    )
    comparison_system = LivePackageAgentSystem(bridge=None)  # type: ignore[arg-type]
    candidate_set = comparison_system._build_decision_only_candidate_set(state)
    ctrip_decision = (
        next(
            item
            for item in candidate_set.candidates
            if item.candidate.lodgings[0].provider == "ctrip"
        )
        if candidate_set is not None
        else None
    )
    exact_coverage = (
        comparison_system._candidate_exact_quote_comparison_coverage(
            state,
            intent,
            ctrip_decision.candidate,
        )
        if ctrip_decision is not None
        else None
    )
    estimate = IComCnyReferenceEstimate(
        rate_date=date(2026, 8, 22),
        captured_at=captured_at,
        usd_per_eur=Decimal("1"),
        cny_per_eur=Decimal("6.720583"),
        usd_to_cny_reference_rate=Decimal("6.720583"),
        source_usd_base_fare_cents=12_000,
        estimated_cny_cents=80_647,
        price_contract_ids=("icom-contract-outbound", "icom-contract-return"),
        transfer_ids=("icom-outbound", "icom-return"),
        response_sha256="c" * 64,
    )
    payload = _blocked_run(intent, query, mode).model_dump(mode="python")
    payload.update(
        {
            "inventory": PackageInventory(
                flights=(flight,), lodgings=(ctrip, kaani), transfers=transfers
            ),
            "exact_quote_comparison_coverage": exact_coverage,
            "selected_stay_plan_id": StayPlanId.MAAFUSHI_ICOM,
            "decision_only_candidate": ctrip_decision,
            "decision_only_candidate_set": candidate_set,
            "icom_cny_reference_estimate": (estimate if ctrip_decision is not None else None),
        }
    )
    return LivePackageAgentRun.model_validate(payload)


def test_decision_only_set_compares_lodgings_within_one_flight() -> None:
    intent = PackageIntent(
        trip_id="decision-only-multi-flight",
        origin="杭州",
        destination="马累",
        start_date=date(2026, 9, 3),
        end_date=date(2026, 9, 9),
        adults=2,
        rooms=1,
    )
    query = BrowserSearchQuery(
        origin="杭州",
        destination="马累",
        start_date=intent.start_date,
        end_date=intent.end_date,
        adults=2,
        rooms=1,
        origin_code="HGH",
        destination_code="MLE",
    )
    base = _two_candidate_live_run(intent, query, LiveCoverageMode.DEGRADED)
    first_flight = base.inventory.flights[0]
    second_flight = first_flight.model_copy(
        update={
            "id": "flight-qunar-second-itinerary",
            "total_for_party_cents": first_flight.total_for_party_cents + 10_000,
            "display_amount_cents": first_flight.display_amount_cents + 10_000,
        }
    )
    state = _RunState(
        source_task_ids=base.source_task_ids,
        intent=intent,
        mode=LiveCoverageMode.DEGRADED,
        inventory=base.inventory.model_copy(update={"flights": (first_flight, second_flight)}),
        normalization_results=(
            NormalizedBrowserQuoteResult(
                provider="qunar",
                kind=BrowserVertical.FLIGHT,
                status=QuoteNormalizationStatus.USABLE,
                quote=first_flight,
            ),
            NormalizedBrowserQuoteResult(
                provider="qunar",
                kind=BrowserVertical.FLIGHT,
                status=QuoteNormalizationStatus.USABLE,
                quote=second_flight,
            ),
        ),
    )

    candidate_set = LivePackageAgentSystem(bridge=None)._build_decision_only_candidate_set(  # type: ignore[arg-type]
        state
    )

    assert candidate_set is not None
    assert {item.candidate.flight.id for item in candidate_set.candidates} == {first_flight.id}


class _TwoCandidatePairRunner(_RecordingPairRunner):
    def __init__(self, *, kaani_reference_complete: bool = True) -> None:
        super().__init__()
        self.kaani_reference_complete = kaani_reference_complete

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        self.calls.append((intent, query, mode, timeout_seconds, source_start_delays_ms))
        return _two_candidate_live_run(
            intent,
            query,
            mode,
            kaani_reference_complete=self.kaani_reference_complete,
        )


def _payload(
    *,
    text: str = ORIGINAL_REQUEST,
    coverage_mode: str = "strict",
    max_pairs: int = 2,
) -> dict[str, object]:
    return {
        "requirement": {
            "text": text,
            "reference_date": "2026-07-30",
        },
        "coverage_mode": coverage_mode,
        "timeout_seconds": 300,
        "total_timeout_seconds": 600,
        "max_pairs": max_pairs,
    }


@pytest.mark.asyncio
async def test_http_endpoint_projects_and_ranks_two_complete_decision_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public route must expose both candidates, not only a helper result."""

    pair_runner = _TwoCandidatePairRunner()
    fixture_icom_estimate = _fixture_icom_cny_reference_estimate()

    async def fixture_fetch_icom_estimate(**_: object) -> IComCnyReferenceEstimate:
        return fixture_icom_estimate

    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 300)
    monkeypatch.setattr(
        main_module,
        "fetch_icom_cny_reference_estimate",
        fixture_fetch_icom_estimate,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51351)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=FIXED_DATE_REQUEST, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    date_pair = body["run"]["pair_runs"][0]["date_pair"]
    assert date_pair["departure_date"] == "2026-09-03"
    assert date_pair["return_date"] == "2026-09-09"
    assert body["interpretation"]["window"]["latest_arrival_date"] is None
    candidates = body["decision_candidates"]
    assert {item["lodging_provider"] for item in candidates} == {
        "ctrip",
        "kaani_official",
    }
    assert {tuple(item["lodging_dates"]) for item in candidates} == {("2026-09-04", "2026-09-09")}
    by_provider = {item["lodging_provider"]: item for item in candidates}
    assert by_provider["ctrip"]["selected"] is True
    assert by_provider["kaani_official"]["selected"] is False
    assert by_provider["ctrip"]["estimated_total_cny_cents"] == 1_187_147
    assert by_provider["kaani_official"]["estimated_total_cny_cents"] == 1_293_926
    assert (
        by_provider["ctrip"]["flight_component_id"]
        == by_provider["kaani_official"]["flight_component_id"]
    )
    assert (
        by_provider["ctrip"]["transfer_component_ids"]
        == by_provider["kaani_official"]["transfer_component_ids"]
    )
    assert by_provider["ctrip"]["is_all_in"] is False
    assert by_provider["kaani_official"]["is_all_in"] is False
    assert by_provider["kaani_official"]["foreign_lodging_currency"] == "USD"
    assert by_provider["kaani_official"]["foreign_lodging_total_cents"] == 54_650
    assert by_provider["kaani_official"]["lodging_reference_cny_cents"] == 367_279
    assert by_provider["kaani_official"]["icom_usd_base_cents"] == 12_000
    assert by_provider["kaani_official"]["icom_reference_cny_cents"] == 80_647

    best = body["best_available_plan"]
    assert best["departure_date"] == "2026-09-03"
    assert best["return_date"] == "2026-09-09"
    assert best["flight"]["outbound_depart_at"].startswith("2026-09-03")
    assert best["flight"]["return_depart_at"].startswith("2026-09-09")
    assert best["flight"]["return_arrive_at"].startswith("2026-09-10")
    assert best["lodgings"][0]["check_in"] == "2026-09-04"
    assert best["lodgings"][0]["check_out"] == "2026-09-09"
    assert best["lodging_source_comparisons"]
    assert best["lodging_source_comparisons"][0]["provider"] == "ctrip"
    assert best["estimated_total_cny_cents"] == 1_187_147
    assert best["flight"]["provider"] == "qunar"
    assert best["flight"]["party_availability_confirmed"] is False
    assert body["final_plan"] is None
    pair_run = body["run"]["pair_runs"][0]["run"]
    assert {
        item["provider"]
        for item in pair_run["exact_quote_comparison_coverage"]["segments"][0]["provider_evidence"]
    } >= {"ctrip", "kaani_official"}

    return_departure_text = (
        "2026年9月3日从杭州出发去马尔代夫，回程最晚在2026年9月9日，2名成人，1间房。"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51352)),
        base_url="http://test",
    ) as client:
        return_departure_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=return_departure_text, max_pairs=1),
        )

    assert return_departure_response.status_code == 200, return_departure_response.text
    return_departure = return_departure_response.json()
    assert return_departure["interpretation"]["window"]["latest_arrival_date"] is None
    return_pair = return_departure["run"]["pair_runs"][0]["date_pair"]
    assert return_pair["departure_date"] == "2026-09-03"
    assert return_pair["return_date"] == "2026-09-09"
    assert return_pair["night_count"] == 6
    assert return_departure["best_available_plan"]["flight"]["return_arrive_at"].startswith(
        "2026-09-10"
    )

    arrival_boundary_text = (
        "2026年9月3日从杭州出发去马尔代夫，2026年9月9日返程，"
        "最晚在2026年9月9日回到杭州，2名成人，1间房。"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51353)),
        base_url="http://test",
    ) as client:
        blocked_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=arrival_boundary_text, max_pairs=1),
        )

    assert blocked_response.status_code == 200, blocked_response.text
    blocked = blocked_response.json()
    assert blocked["interpretation"]["window"]["latest_arrival_date"] == "2026-09-09"
    assert blocked["decision_candidates"] == []
    assert blocked["best_available_plan"] is None


@pytest.mark.asyncio
async def test_http_endpoint_solves_multi_city_anchor_from_same_text_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "2名成人，2026-10-02 杭州出发到大阪，2026-10-05 去京都，"
        "2026-10-08 从东京返回杭州；2026-10-03 19:00-21:30 在大阪有已持有演唱会，"
        "活动费用未提供；交通和酒店总价尽量低，活动前至少留90分钟缓冲。"
    )
    fixture_intent = parse_complex_intent(text)
    assert fixture_intent is not None
    short_intent = parse_complex_intent(
        "2名成人从杭州出发，2026-10-02 到大阪，10/5 去京都，10/8 从东京返回杭州；"
        "10/3 19:00-21:30 在大阪有演唱会，活动前至少留90分钟缓冲。"
    )
    assert short_intent is not None
    assert short_intent.origin.id == "杭州"
    assert short_intent.route_legs[-1].destination_place_id == "杭州"
    assert short_intent.route_legs[-1].departure_date == date(2026, 10, 8)
    fixture_contracts = tuple(
        PriceContract(
            id=cid,
            total_for_party_cents=amount,
            component_ids=(component,),
            source="frozen_fixture",
        )
        for cid, amount, component in (
            ("pc-hgh-osa", 148_000, "tr-hgh-osa"),
            ("pc-osa-kyo", 44_000, "tr-osa-kyo"),
            ("pc-kyo-bundle", 184_000, "tr-kyo-tyo"),
            ("pc-tyo-hgh", 176_000, "tr-tyo-hgh"),
            ("pc-osa-stay", 168_000, "stay-osa"),
            ("pc-tyo-stay", 132_000, "stay-tyo"),
        )
    )
    fixture_contracts = tuple(
        item.model_copy(
            update={
                "component_ids": ("tr-kyo-tyo", "stay-kyo"),
                "shared": True,
            }
        )
        if item.id == "pc-kyo-bundle"
        else item
        for item in fixture_contracts
    )

    def transport(
        offer_id: str,
        origin: str,
        destination: str,
        departure: str,
        arrival: str,
        contract: str,
        label: str,
    ) -> TransportOffer:
        return TransportOffer(
            id=offer_id,
            provider="frozen-fixture",
            origin_place_id=origin,
            destination_place_id=destination,
            departure=datetime.fromisoformat(departure),
            arrival=datetime.fromisoformat(arrival),
            price_contract_id=contract,
            detail_url=f"fixture://{offer_id}",
            label=label,
        )

    fixture_catalog = OfferCatalog(
        source_mode="frozen_fixture",
        bundles=(
            BundleOffer(
                id="bundle-kyo-rail-stay",
                label="京都交通住宿组合",
                component_offer_ids=("tr-kyo-tyo", "stay-kyo"),
                price_contract_id="pc-kyo-bundle",
            ),
        ),
        query_tasks=(
            "query:transport:杭州-大阪",
            "query:transport:大阪-京都",
            "query:transport:京都-东京",
            "query:transport:东京-杭州",
            "query:stay:大阪",
            "query:stay:京都",
            "query:stay:东京",
        ),
        transports=(
            transport(
                "tr-hgh-osa",
                "杭州",
                "大阪",
                "2026-10-02T09:00:00",
                "2026-10-02T13:00:00",
                "pc-hgh-osa",
                "杭州→大阪",
            ),
            transport(
                "tr-osa-kyo",
                "大阪",
                "京都",
                "2026-10-05T10:00:00",
                "2026-10-05T11:00:00",
                "pc-osa-kyo",
                "大阪→京都",
            ),
            transport(
                "tr-kyo-tyo",
                "京都",
                "东京",
                "2026-10-06T10:00:00",
                "2026-10-06T12:30:00",
                "pc-kyo-bundle",
                "京都→东京",
            ),
            transport(
                "tr-tyo-hgh",
                "东京",
                "杭州",
                "2026-10-08T14:00:00",
                "2026-10-08T18:00:00",
                "pc-tyo-hgh",
                "东京→杭州",
            ),
        ),
        stays=(
            StayOffer(
                id="stay-osa",
                provider="frozen-fixture",
                place_id="大阪",
                check_in=date(2026, 10, 2),
                check_out=date(2026, 10, 5),
                price_contract_id="pc-osa-stay",
                detail_url="fixture://stay-osa",
                label="大阪住宿",
            ),
            StayOffer(
                id="stay-kyo",
                provider="frozen-fixture",
                place_id="京都",
                check_in=date(2026, 10, 5),
                check_out=date(2026, 10, 6),
                price_contract_id="pc-kyo-bundle",
                detail_url="fixture://stay-kyo",
                label="京都住宿",
            ),
            StayOffer(
                id="stay-kyo-invalid",
                provider="frozen-fixture",
                place_id="京都",
                check_in=date(2026, 10, 5),
                check_out=date(2026, 10, 5),
                price_contract_id="pc-kyo-bundle",
                detail_url="fixture://stay-kyo-invalid",
                label="京都住宿（日期不足，不应入选）",
            ),
            StayOffer(
                id="stay-tyo",
                provider="frozen-fixture",
                place_id="东京",
                check_in=date(2026, 10, 6),
                check_out=date(2026, 10, 8),
                price_contract_id="pc-tyo-stay",
                detail_url="fixture://stay-tyo",
                label="东京住宿",
            ),
        ),
    )
    # Small exhaustive oracle: CP-SAT must match the exact minimum without
    # using this Cartesian enumeration in production.
    contracts_by_id = {item.id: item for item in fixture_contracts}
    oracle_totals: list[int] = []
    stay_slots = tuple(
        tuple(item for item in fixture_catalog.stays if item.place_id == place)
        for place in ("大阪", "京都", "东京")
    )
    for stays in product(*stay_slots):
        transport_dates = (
            fixture_catalog.transports[0].arrival.date(),
            fixture_catalog.transports[1].arrival.date(),
            fixture_catalog.transports[2].arrival.date(),
        )
        departure_dates = (
            fixture_catalog.transports[1].departure.date(),
            fixture_catalog.transports[2].departure.date(),
            fixture_catalog.transports[3].departure.date(),
        )
        if not all(
            stay.check_in <= arrival and stay.check_out >= departure
            for stay, arrival, departure in zip(
                stays, transport_dates, departure_dates, strict=True
            )
        ):
            continue
        contract_ids = {
            *(item.price_contract_id for item in fixture_catalog.transports),
            *(item.price_contract_id for item in stays),
        }
        oracle_totals.append(
            sum(contracts_by_id[item].total_for_party_cents for item in contract_ids)
        )
    assert min(oracle_totals) == 852_000

    class FrozenComplexProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return fixture_catalog, fixture_contracts

    class ExplodingLegacyParser:
        async def parse(self, _request: Any) -> Any:
            raise AssertionError("complex requests must not call the legacy parser")

    monkeypatch.setattr(app.state, "complex_offer_provider", FrozenComplexProvider(), raising=False)
    monkeypatch.setattr(app.state, "package_requirement_agent", ExplodingLegacyParser())
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51354)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"] is None
    assert "complex_plan" not in body
    intent = body["travel_intent"]
    card = body["trip_card"]
    assert card["status"] == "candidate"
    assert card["total_cny_cents"] == 852_000
    assert card["city_order"] == ["大阪", "京都", "东京"]
    assert card["traveler_count"] == 2
    assert intent["window"]["start"].startswith("2026-10-02")
    assert intent["window"]["end"].startswith("2026-10-08")
    assert intent["anchors"][0]["traveler_count"] == 2
    assert len(card["components"]) == 7
    assert len(card["fixed_activities"]) == 1
    assert "stay-kyo-invalid" not in {
        item["offer_id"] for item in card["components"]
    }
    bundle_contract = next(
        item for item in card["price_contracts"] if item["id"] == "pc-kyo-bundle"
    )
    assert bundle_contract["shared"] is True
    assert set(bundle_contract["component_ids"]) == {"tr-kyo-tyo", "stay-kyo"}
    assert body["source_statuses"][0]["state"] == "succeeded"
    assert "报价目录" in body["execution_boundary"]

    provided_activity_text = text.replace(
        "活动费用未提供", "活动已支付总费用1000元"
    )
    arrow_text = (
        "2名成人从杭州出发，2026-10-02 杭州→大阪，2026-10-05 去京都，"
        "2026-10-08 从东京返杭；2026-10-03 19:00-21:30 在大阪有演唱会，"
        "活动费用未提供；活动前至少留90分钟缓冲。"
    )
    shorthand_text = (
        "2名成人从杭州出发，10/2 到大阪，10/5 去京都，"
        "10/8 从东京返回杭州；10/3 19:00-21:30 在大阪有演唱会，"
        "活动费用未提供；活动前至少留90分钟缓冲。"
    )
    no_space_chinese_date_text = (
        "2名成人，2026年10月2日杭州出发到大阪，2026年10月5日去京都，"
        "2026年10月8日从东京返回杭州；2026年10月3日19:00-21:30在大阪有演唱会，"
        "活动费用未提供；活动前至少留90分钟缓冲。"
    )
    chinese_short_date_text = (
        "2名成人，10月2日杭州出发到大阪，10月5日去京都，"
        "10月8日从东京返回杭州；10月3日19:00-21:30在大阪有演唱会，"
        "活动费用未提供；活动前至少留90分钟缓冲。"
    )
    reversed_activity_text = text.replace("19:00-21:30", "21:30-19:00")
    start_only_activity_text = text.replace("19:00-21:30", "19:00")
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51356)),
        base_url="http://test",
    ) as client:
        provided_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=provided_activity_text, max_pairs=1),
        )
        arrow_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=arrow_text, max_pairs=1),
        )
        start_only_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=start_only_activity_text, max_pairs=1),
        )
        shorthand_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=shorthand_text, max_pairs=1),
        )
        no_space_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=no_space_chinese_date_text, max_pairs=1),
        )
        chinese_short_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=chinese_short_date_text, max_pairs=1),
        )
        reversed_activity_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=reversed_activity_text, max_pairs=1),
        )
    assert provided_response.status_code == 200, provided_response.text
    provided_card = provided_response.json()["trip_card"]
    assert provided_card["total_cny_cents"] == 952_000
    assert provided_card["activity_price_included"] is True
    assert any(
        item["id"].startswith("user-activity:")
        for item in provided_card["price_contracts"]
    )
    assert arrow_response.status_code == 200, arrow_response.text
    arrow_intent = arrow_response.json()["travel_intent"]
    assert arrow_intent["route_legs"][-1]["origin_place_id"] == "东京"
    assert arrow_intent["route_legs"][-1]["destination_place_id"] == "杭州"
    assert start_only_response.status_code == 200, start_only_response.text
    start_only_card = start_only_response.json()["trip_card"]
    assert start_only_card["total_cny_cents"] is None
    assert start_only_card["activity_price_included"] is False
    assert any("结束时间待确认" in item for item in start_only_card["unresolved_items"])
    assert shorthand_response.status_code == 200, shorthand_response.text
    shorthand_body = shorthand_response.json()
    assert shorthand_body["interpretation"] is None
    assert shorthand_body["trip_card"]["total_cny_cents"] == 852_000
    assert shorthand_body["travel_intent"]["window"]["start"].startswith("2026-10-02")
    assert shorthand_body["travel_intent"]["route_legs"][-1][
        "departure_date"
    ] == "2026-10-08"
    for chinese_date_response in (no_space_response, chinese_short_response):
        assert chinese_date_response.status_code == 200, chinese_date_response.text
        chinese_date_body = chinese_date_response.json()
        assert chinese_date_body["interpretation"] is None
        assert chinese_date_body["trip_card"]["total_cny_cents"] == 852_000
        assert chinese_date_body["travel_intent"]["origin"]["id"] == "杭州"
    assert reversed_activity_response.status_code == 200
    reversed_activity_card = reversed_activity_response.json()["trip_card"]
    assert reversed_activity_card["status"] == "no_solution"
    assert reversed_activity_card["total_cny_cents"] is None
    assert len(reversed_activity_card["fixed_activities"]) == 1

    invalid_return = next(
        item for item in fixture_catalog.transports if item.id == "tr-tyo-hgh"
    ).model_copy(
        update={
            "departure": datetime.fromisoformat("2026-10-09T14:00:00"),
            "arrival": datetime.fromisoformat("2026-10-09T18:00:00"),
        }
    )
    invalid_catalog = fixture_catalog.model_copy(
        update={
            "transports": tuple(
                invalid_return if item.id == "tr-tyo-hgh" else item
                for item in fixture_catalog.transports
            )
        }
    )

    class OutOfWindowProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return invalid_catalog, fixture_contracts

    monkeypatch.setattr(app.state, "complex_offer_provider", OutOfWindowProvider())
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51357)),
        base_url="http://test",
    ) as client:
        invalid_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )
    assert invalid_response.status_code == 200, invalid_response.text
    invalid_card = invalid_response.json()["trip_card"]
    assert invalid_card["status"] == "no_solution"
    assert invalid_card["total_cny_cents"] is None
    assert len(invalid_card["fixed_activities"]) == 1
    assert "满足全部约束" in invalid_card["source_boundary"]

    reversed_transport = next(
        item for item in fixture_catalog.transports if item.id == "tr-kyo-tyo"
    ).model_copy(update={"arrival": datetime.fromisoformat("2026-10-06T09:00:00")})
    reversed_transport_catalog = fixture_catalog.model_copy(
        update={
            "transports": tuple(
                reversed_transport if item.id == "tr-kyo-tyo" else item
                for item in fixture_catalog.transports
            )
        }
    )
    overlapping_transport = next(
        item for item in fixture_catalog.transports if item.id == "tr-kyo-tyo"
    ).model_copy(
        update={
            "departure": datetime.fromisoformat("2026-10-08T10:00:00"),
            "arrival": datetime.fromisoformat("2026-10-08T15:00:00"),
        }
    )
    overlapping_catalog = fixture_catalog.model_copy(
        update={
            "transports": tuple(
                overlapping_transport if item.id == "tr-kyo-tyo" else item
                for item in fixture_catalog.transports
            )
        }
    )

    class ReversedTransportProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return reversed_transport_catalog, fixture_contracts

    class OverlappingTransportProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return overlapping_catalog, fixture_contracts

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51358)),
        base_url="http://test",
    ) as client:
        monkeypatch.setattr(app.state, "complex_offer_provider", ReversedTransportProvider())
        reversed_transport_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )
        monkeypatch.setattr(app.state, "complex_offer_provider", OverlappingTransportProvider())
        overlapping_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )
    assert reversed_transport_response.status_code == 200
    assert reversed_transport_response.json()["trip_card"]["status"] == "no_solution"
    assert overlapping_response.status_code == 200
    assert overlapping_response.json()["trip_card"]["status"] == "no_solution"

    compiler = PlanningCompiler()
    successful_graph = ComplexCatalogSolver().solve(
        compiler.compile_problem(
            fixture_intent,
            offer_catalog=fixture_catalog,
            price_contracts=fixture_contracts,
        )
    )
    reverse_errors = validate_plan_graph(
        successful_graph,
        fixture_contracts,
        intent=fixture_intent,
        catalog=reversed_transport_catalog,
    )
    overlap_errors = validate_plan_graph(
        successful_graph,
        fixture_contracts,
        intent=fixture_intent,
        catalog=overlapping_catalog,
    )
    reversed_activity_intent = parse_complex_intent(reversed_activity_text)
    assert reversed_activity_intent is not None
    activity_errors = validate_plan_graph(
        successful_graph,
        fixture_contracts,
        intent=reversed_activity_intent,
        catalog=fixture_catalog,
    )
    assert any("到达时间" in item for item in reverse_errors)
    assert any("相邻交通时间倒置" in item for item in overlap_errors)
    assert any("活动结束时间" in item for item in activity_errors)

    roundtrip_contract = PriceContract(
        id="pc-roundtrip",
        total_for_party_cents=324_000,
        component_ids=("tr-hgh-osa", "tr-tyo-hgh"),
        shared=True,
        source="frozen_fixture",
    )
    roundtrip_contracts = (
        *(
            item
            for item in fixture_contracts
            if item.id not in {"pc-hgh-osa", "pc-tyo-hgh"}
        ),
        roundtrip_contract,
    )
    roundtrip_catalog = fixture_catalog.model_copy(
        update={
            "transports": tuple(
                item.model_copy(update={"price_contract_id": "pc-roundtrip"})
                if item.id in {"tr-hgh-osa", "tr-tyo-hgh"}
                else item
                for item in fixture_catalog.transports
            )
        }
    )

    class SharedRoundtripProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return roundtrip_catalog, roundtrip_contracts

    monkeypatch.setattr(app.state, "complex_offer_provider", SharedRoundtripProvider())
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51359)),
        base_url="http://test",
    ) as client:
        roundtrip_response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )
    assert roundtrip_response.status_code == 200, roundtrip_response.text
    roundtrip_card = roundtrip_response.json()["trip_card"]
    assert roundtrip_card["status"] == "candidate"
    assert roundtrip_card["total_cny_cents"] == 852_000
    projected_roundtrip = next(
        item for item in roundtrip_card["price_contracts"] if item["id"] == "pc-roundtrip"
    )
    assert projected_roundtrip["shared"] is True
    assert set(projected_roundtrip["component_ids"]) == {"tr-hgh-osa", "tr-tyo-hgh"}


@pytest.mark.asyncio
async def test_default_http_composition_runs_current_complex_provider_without_browser_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public HTTP sources remain installed when the optional Chrome bridge is off."""

    current_app = FastAPI()
    installed_provider = _install_current_complex_offer_provider(current_app)
    bridge, live_system = _install_browser_bridge(
        current_app,
        Settings(_env_file=None),
    )
    assert bridge is None
    assert live_system is None
    assert current_app.state.complex_offer_provider is installed_provider

    query_tasks = (
        "12306:2026-08-30:HGH-NKH",
        "12306:2026-09-01:NKH-AOH",
        "12306:2026-09-04:AOH-HGH",
        "trip.com:hotel:12:2026-08-30:2026-09-01:2a",
        "trip.com:hotel:2:2026-09-01:2026-09-04:2a",
    )

    def transport(
        offer_id: str,
        origin: str,
        destination: str,
        departure: str,
        arrival: str,
        amount: int,
    ) -> tuple[TransportOffer, PriceContract]:
        contract_id = f"{offer_id}:price"
        return (
            TransportOffer(
                id=offer_id,
                provider="12306",
                origin_place_id=origin,
                destination_place_id=destination,
                departure=datetime.fromisoformat(departure),
                arrival=datetime.fromisoformat(arrival),
                price_contract_id=contract_id,
                detail_url=f"https://www.12306.cn/{offer_id}",
                label=offer_id,
                party_capacity_confirmed=True,
                available_units=2,
            ),
            PriceContract(
                id=contract_id,
                total_for_party_cents=amount,
                component_ids=(offer_id,),
                source="current:12306:test-fixture",
            ),
        )

    transports_and_contracts = (
        transport(
            "rail-hgh-nkh",
            "杭州",
            "南京",
            "2026-08-30T09:00:00+08:00",
            "2026-08-30T10:30:00+08:00",
            20_000,
        ),
        transport(
            "rail-nkh-aoh",
            "南京",
            "上海",
            "2026-09-01T09:00:00+08:00",
            "2026-09-01T10:30:00+08:00",
            30_000,
        ),
        transport(
            "rail-aoh-hgh",
            "上海",
            "杭州",
            "2026-09-04T09:00:00+08:00",
            "2026-09-04T10:30:00+08:00",
            20_000,
        ),
    )
    stays = (
        StayOffer(
            id="stay-nanjing",
            provider="trip.com",
            place_id="南京",
            check_in=date(2026, 8, 30),
            check_out=date(2026, 9, 1),
            price_contract_id="stay-nanjing:price",
            detail_url="https://www.trip.com/hotels/nanjing",
            label="南京酒店",
            confirmed_traveler_count=2,
            confirmed_room_count=1,
        ),
        StayOffer(
            id="stay-shanghai",
            provider="trip.com",
            place_id="上海",
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 4),
            price_contract_id="stay-shanghai:price",
            detail_url="https://www.trip.com/hotels/shanghai",
            label="上海酒店",
            confirmed_traveler_count=2,
            confirmed_room_count=1,
        ),
    )
    contracts = (
        *(item[1] for item in transports_and_contracts),
        PriceContract(
            id="stay-nanjing:price",
            total_for_party_cents=40_000,
            component_ids=("stay-nanjing",),
            source="current:trip.com:test-fixture",
        ),
        PriceContract(
            id="stay-shanghai:price",
            total_for_party_cents=50_000,
            component_ids=("stay-shanghai",),
            source="current:trip.com:test-fixture",
        ),
    )
    catalog = OfferCatalog(
        transports=tuple(item[0] for item in transports_and_contracts),
        stays=stays,
        query_tasks=query_tasks,
        source_statuses=tuple(
            SourceStatus(
                source_id=f"source:{index}",
                provider="12306" if index < 3 else "trip.com",
                state=SourceState.SUCCEEDED,
                detail="当前人民币同行总价已返回",
                query_task_ids=(task_id,),
                captured_at=NOW,
            )
            for index, task_id in enumerate(query_tasks)
        ),
        source_mode="current",
    )

    async def fixture_catalog_for(
        _self: CurrentComplexOfferProvider,
        _intent: Any,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        return catalog, contracts

    monkeypatch.setattr(CurrentComplexOfferProvider, "catalog_for", fixture_catalog_for)
    monkeypatch.setattr(app.state, "complex_offer_provider", installed_provider)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    text = (
        "2名成人，2026-08-30 杭州出发到南京，2026-09-01 去上海，"
        "2026-09-04 从上海返回杭州；2026-08-31 19:00-21:30 "
        "在南京有已持有活动，活动费用未提供；交通和酒店总价尽量低，"
        "活动前至少留90分钟缓冲。"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51360)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"] is None
    assert body["trip_card"]["status"] == "final"
    assert body["trip_card"]["total_cny_cents"] == 160_000
    assert len(body["trip_card"]["components"]) == 5
    assert {item["state"] for item in body["source_statuses"]} == {"succeeded"}
    assert "生成完整方案" in body["execution_boundary"]


@pytest.mark.asyncio
async def test_http_endpoint_solves_group_merge_split_and_reconciles_shared_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "旅行者甲从杭州、旅行者乙从北京分别于2026-10-02出发到大阪汇合，共住一间房；"
        "甲参加2026-10-03 19:00-21:30在大阪的已持有演唱会，乙不参加；"
        "乙2026-10-06从大阪返回北京，甲2026-10-08从大阪返回杭州。"
        "两人均为成人，交通和酒店总价尽量低，活动前至少留90分钟缓冲，活动费用未提供。"
    )
    compiler = PlanningCompiler()
    intent = compiler.compile(text, reference_year=2026)
    assert intent is not None
    assert intent.topology.value == "group_multi_origin"
    assert [item.name for item in intent.traveler_profiles] == ["甲", "乙"]
    assert [len(item.participant_ids) for item in intent.stay_requirements] == [2, 1]

    transports: list[TransportOffer] = []
    stays: list[StayOffer] = []
    contracts: list[PriceContract] = []
    base_transport_prices = (120_000, 130_000, 140_000, 110_000)
    for index, requirement in enumerate(intent.route_legs):
        assert requirement.departure_date is not None
        for option, surcharge in enumerate((0, 20_000)):
            offer_id = f"fixture:transport:{index}:{option}"
            contract_id = f"fixture:contract:transport:{index}:{option}"
            departure = datetime.combine(
                requirement.departure_date,
                datetime.min.time(),
            ).replace(hour=9 + option)
            transports.append(
                TransportOffer(
                    id=offer_id,
                    provider="frozen-fixture",
                    origin_place_id=requirement.origin_place_id,
                    destination_place_id=requirement.destination_place_id,
                    departure=departure,
                    arrival=departure + timedelta(hours=3),
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=(
                        f"{requirement.origin_place_id}→"
                        f"{requirement.destination_place_id} 备选{option + 1}"
                    ),
                    participant_ids=requirement.participant_ids,
                    party_capacity_confirmed=True,
                    available_units=2,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=base_transport_prices[index] + surcharge,
                    component_ids=(offer_id,),
                    covered_traveler_ids=requirement.participant_ids,
                    source="frozen_fixture",
                )
            )
    base_stay_prices = (200_000, 100_000)
    for index, requirement in enumerate(intent.stay_requirements):
        for option, surcharge in enumerate((0, 50_000)):
            offer_id = f"fixture:stay:{index}:{option}"
            contract_id = f"fixture:contract:stay:{index}:{option}"
            stays.append(
                StayOffer(
                    id=offer_id,
                    provider="frozen-fixture",
                    place_id=requirement.place_id,
                    check_in=requirement.check_in,
                    check_out=requirement.check_out,
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"大阪住宿段{index + 1}备选{option + 1}",
                    participant_ids=requirement.participant_ids,
                    confirmed_traveler_count=len(requirement.participant_ids),
                    confirmed_room_count=requirement.room_count,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=base_stay_prices[index] + surcharge,
                    component_ids=(offer_id,),
                    covered_traveler_ids=requirement.participant_ids,
                    shared_between_travelers=len(requirement.participant_ids) > 1,
                    source="frozen_fixture",
                )
            )
    query_tasks = tuple(
        [f"fixture:query:leg:{item.id}" for item in intent.route_legs]
        + [f"fixture:query:stay:{item.id}" for item in intent.stay_requirements]
    )
    catalog = OfferCatalog(
        transports=tuple(transports),
        stays=tuple(stays),
        query_tasks=query_tasks,
        source_statuses=tuple(
            SourceStatus(
                source_id=task_id,
                provider="frozen-fixture",
                state=SourceState.SUCCEEDED,
                detail="冻结正式来源合同已返回",
                query_task_ids=(task_id,),
                captured_at=NOW,
            )
            for task_id in query_tasks
        ),
        source_mode="frozen_fixture",
    )
    frozen_contracts = tuple(contracts)

    # Small exhaustive oracle proves that shared lodging is charged once.
    contract_by_id = {item.id: item for item in frozen_contracts}
    slots = tuple(
        tuple(
            offer
            for offer in transports
            if offer.origin_place_id == requirement.origin_place_id
            and offer.destination_place_id == requirement.destination_place_id
            and offer.participant_ids == requirement.participant_ids
        )
        for requirement in intent.route_legs
    ) + tuple(
        tuple(
            offer
            for offer in stays
            if offer.check_in == requirement.check_in
            and offer.check_out == requirement.check_out
            and offer.participant_ids == requirement.participant_ids
        )
        for requirement in intent.stay_requirements
    )
    oracle_total = min(
        sum(
            contract_by_id[contract_id].total_for_party_cents
            for contract_id in {item.price_contract_id for item in selected}
        )
        for selected in product(*slots)
    )
    assert oracle_total == 800_000

    class FrozenGroupProvider:
        def catalog_for(
            self,
            _intent: Any,
        ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return catalog, frozen_contracts

    class ExplodingLegacyParser:
        async def parse(self, _request: Any) -> Any:
            raise AssertionError("group requests must not call the legacy parser")

    monkeypatch.setattr(app.state, "complex_offer_provider", FrozenGroupProvider())
    monkeypatch.setattr(app.state, "package_requirement_agent", ExplodingLegacyParser())
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51361)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"] is None
    assert body["travel_intent"]["topology"] == "group_multi_origin"
    card = body["trip_card"]
    assert card["status"] == "candidate"
    assert card["total_cny_cents"] == oracle_total
    assert len(card["components"]) == 6
    assert len(card["shared_components"]) == 1
    assert card["shared_cost_cny_cents"] == 200_000
    assert sum(
        item["attributable_total_cny_cents"] for item in card["traveler_costs"]
    ) == card["total_cny_cents"]
    costs = {item["traveler_name"]: item for item in card["traveler_costs"]}
    assert costs["甲"]["attributable_total_cny_cents"] == 460_000
    assert costs["乙"]["attributable_total_cny_cents"] == 340_000
    itineraries = {
        item["traveler_name"]: item["components"]
        for item in card["traveler_itineraries"]
    }
    assert any(item["kind"] == "anchor" for item in itineraries["甲"])
    assert not any(item["kind"] == "anchor" for item in itineraries["乙"])
    assert [item["kind"] for item in itineraries["甲"]] == [
        "transport",
        "stay",
        "anchor",
        "stay",
        "transport",
    ]
    assert [item["kind"] for item in itineraries["乙"]] == [
        "transport",
        "stay",
        "transport",
    ]
    assert len(
        [item for item in itineraries["甲"] if item["kind"] == "stay"]
    ) == 2
    assert len(
        [item for item in itineraries["乙"] if item["kind"] == "stay"]
    ) == 1
    assert len({item["id"] for item in card["price_contracts"]}) == 6

    capacity_catalog = catalog.model_copy(
        update={
            "stays": tuple(
                item.model_copy(update={"confirmed_traveler_count": 1})
                if len(item.participant_ids) == 2
                else item
                for item in catalog.stays
            )
        }
    )
    capacity_graph = ComplexCatalogSolver().solve(
        compiler.compile_problem(
            intent,
            offer_catalog=capacity_catalog,
            price_contracts=frozen_contracts,
        )
    )
    assert capacity_graph.status.value == "no-solution"
    missing_return_catalog = catalog.model_copy(
        update={
            "transports": tuple(
                item
                for item in catalog.transports
                if not (
                    item.origin_place_id == "大阪"
                    and item.destination_place_id == "北京"
                )
            )
        }
    )
    missing_return_graph = ComplexCatalogSolver().solve(
        compiler.compile_problem(
            intent,
            offer_catalog=missing_return_catalog,
            price_contracts=frozen_contracts,
        )
    )
    assert missing_return_graph.status.value == "no-solution"


@pytest.mark.asyncio
async def test_http_endpoint_reports_source_gap_without_frozen_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = (
        "2名成人，2026-10-02 杭州出发到大阪，2026-10-05 去京都，"
        "2026-10-08 从东京返回杭州；2026-10-03 19:00-21:30 在大阪有演唱会，"
        "活动费用未提供；活动前至少留90分钟缓冲。"
    )
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51355)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"] is None
    assert "complex_plan" not in body
    assert body["trip_card"]["status"] == "source_gap"
    assert body["trip_card"]["total_cny_cents"] is None
    assert body["trip_card"]["activity_price_included"] is False
    assert len(body["trip_card"]["fixed_activities"]) == 1
    assert body["trip_card"]["fixed_activities"][0]["label"] == "已持有演唱会"
    assert {item["provider"] for item in body["source_statuses"]} == {
        "12306",
        "trip.com",
    }
    assert {item["state"] for item in body["source_statuses"]} == {"not_queried"}
    assert "当前来源未形成可查询" in body["execution_boundary"]


@pytest.mark.asyncio
async def test_http_endpoint_does_not_rank_foreign_candidate_without_ecb_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _TwoCandidatePairRunner(kaani_reference_complete=False)
    fixture_icom_estimate = _fixture_icom_cny_reference_estimate()

    async def fixture_fetch_icom_estimate(**_: object) -> IComCnyReferenceEstimate:
        return fixture_icom_estimate

    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 300)
    monkeypatch.setattr(
        main_module,
        "fetch_icom_cny_reference_estimate",
        fixture_fetch_icom_estimate,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51352)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=FIXED_DATE_REQUEST, max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    date_pair = body["run"]["pair_runs"][0]["date_pair"]
    assert date_pair["departure_date"] == "2026-09-03"
    assert date_pair["return_date"] == "2026-09-09"
    candidates = {item["lodging_provider"]: item for item in body["decision_candidates"]}
    assert {tuple(item["lodging_dates"]) for item in candidates.values()} == {
        ("2026-09-04", "2026-09-09")
    }
    assert candidates["ctrip"]["selected"] is True
    assert candidates["ctrip"]["estimated_total_cny_cents"] == 1_187_147
    assert candidates["kaani_official"]["selected"] is False
    assert candidates["kaani_official"]["lodging_reference_cny_cents"] is None
    assert candidates["kaani_official"]["lodging_reference_source"] is None
    assert candidates["kaani_official"]["lodging_reference_date"] is None
    assert candidates["kaani_official"]["lodging_reference_sha256"] is None
    assert candidates["kaani_official"]["lodging_reference_captured_at"] is None
    assert candidates["kaani_official"]["estimated_total_cny_cents"] is None
    assert body["best_available_plan"]["lodgings"][0]["provider"] == "ctrip"
    assert body["best_available_plan"]["departure_date"] == "2026-09-03"
    assert body["best_available_plan"]["return_date"] == "2026-09-09"
    assert body["best_available_plan"]["flight"]["outbound_depart_at"].startswith("2026-09-03")
    assert body["best_available_plan"]["flight"]["return_depart_at"].startswith("2026-09-09")
    assert body["best_available_plan"]["lodgings"][0]["check_in"] == "2026-09-04"
    assert body["best_available_plan"]["lodgings"][0]["check_out"] == "2026-09-09"
    assert body["best_available_plan"]["estimated_total_cny_cents"] == 1_187_147
    pair_run = body["run"]["pair_runs"][0]["run"]
    kaani_evidence = next(
        item
        for item in pair_run["exact_quote_comparison_coverage"]["segments"][0]["provider_evidence"]
        if item["provider"] == "kaani_official"
    )
    assert kaani_evidence["quote_ids"]
    assert kaani_evidence["eligible_quote_ids"] == []


@pytest.mark.asyncio
async def test_ready_text_maps_constraints_does_not_cache_without_publishable_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    interpretation = body["interpretation"]
    assert interpretation["state"] == "ready"
    assert interpretation["window"]["min_nights"] == 4
    assert interpretation["window"]["max_nights"] == 7
    assert interpretation["window"]["origin_code"] == "HGH"
    assert interpretation["window"]["destination_code"] == "MLE"
    assert interpretation["intent_template"]["require_checked_baggage"] is False
    assert interpretation["intent_template"]["require_breakfast"] is None
    assert interpretation["intent_template"]["breakfast_preference_mode"] == "indifferent"
    assert interpretation["intent_template"]["breakfast_preference_weight"] == 0
    assert body["model_enhancement_enabled"] is False
    assert len(body["model_trace_scope_sha256"]) == 64
    assert body["model_trace_count"] == 0
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 0
    assert "模型增强未启用" in body["execution_boundary"]
    assert "不是用户原话，可改" in body["execution_boundary"]
    assert "不是用户原话，可改" in interpretation["claim_boundary"]
    assert body["run"]["sampled_not_exhaustive"] is True
    assert "不得声称全月最低价" in body["run"]["claim_boundary"]
    assert "不是用户原话，可改" in body["run"]["claim_boundary"]
    profile = body["run"]["stay_area_search_profile"]
    assert profile == {
        "gateway_destination": "马累",
        "destination_island_lodging_search_term": "Maafushi",
        "airport_island_lodging_search_term": "Hulhumalé",
        "source": "system_derived_golden",
        "assumption_zh": (
            "系统生成的可比较自由行场景，不是用户原话，可改：马累/MLE 作为航班"
            "门户，整段及中段住宿搜索 Maafushi，首晚及末晚住宿搜索 Hulhumalé。"
        ),
    }
    frozen = system_stay_plan_candidate_set()
    assert body["run"]["stay_plan_candidate_set"]["candidate_set_sha256"] == (
        frozen.candidate_set_sha256
    )
    assert body["run"]["query_plan"]["stay_plan_candidate_set_sha256"] == (
        frozen.candidate_set_sha256
    )
    assert len(body["run"]["pair_runs"]) == 2
    assert body["cached_pair_runs"] == []
    assert len(pair_runner.calls) == 2
    for intent, query, mode, timeout_seconds, delays in pair_runner.calls:
        assert mode == LiveCoverageMode.STRICT
        assert timeout_seconds == 60
        assert intent.destination == "马累"
        assert intent.destination_place_key is None
        assert intent.require_checked_baggage is False
        assert intent.require_breakfast is None
        assert intent.breakfast_preference_mode == PreferenceMode.INDIFFERENT
        assert intent.breakfast_preference_weight == 0
        assert intent.budget_cents is None
        assert query.destination == "马累"
        assert query.origin_code == "HGH"
        assert query.destination_code == "MLE"
        assert query.options["gateway_destination"] == "马累"
        query_profile = query.options["stay_area_search_profile"]
        assert isinstance(query_profile, dict)
        assert query_profile["source"] == "system_derived_golden"
        query_candidate_set = query.options["stay_plan_candidate_set"]
        assert isinstance(query_candidate_set, dict)
        assert query_candidate_set["candidate_set_sha256"] == frozen.candidate_set_sha256
        assert delays is not None and len(delays) == 13


@pytest.mark.asyncio
async def test_verbatim_maldives_text_binds_male_gateway_profile_before_live_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW)
    text = (
        "我要从杭州出发去马尔代夫周边游，时间：从明天开始到9月10日前的4-8天游，"
        "人数：我和女朋友两个人，偏好：住宿不能太简陋，地址不能太偏，可以稍微有点品质但价格不能太高，"
        "到达和返程可以住机场附近，但也要关注有没有更好的选择。"
    )
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51343)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(text=text, coverage_mode="strict", max_pairs=1),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"]["window"]["destination"] == "马尔代夫"
    assert body["interpretation"]["window"]["return_date_targets"] == [
        "2026-09-09",
        "2026-09-10",
    ]
    assert body["interpretation"]["intent_template"]["require_non_basic_lodging"] is True
    assert body["interpretation"]["intent_template"]["require_non_remote_lodging"] is True
    assert pair_runner.calls[0][0].destination == "马累"
    assert pair_runner.calls[0][0].require_non_basic_lodging is True
    assert pair_runner.calls[0][0].require_non_remote_lodging is True
    assert pair_runner.calls[0][1].destination == "马累"
    assert pair_runner.calls[0][1].options["gateway_destination"] == "马累"
    assert "stay_plan_candidate_set" in pair_runner.calls[0][1].options


@pytest.mark.asyncio
async def test_structured_breakfast_weight_reaches_every_pair_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)
    payload = _payload(max_pairs=1)
    requirement = payload["requirement"]
    assert isinstance(requirement, dict)
    requirement["breakfast_mode"] = "weighted"
    requirement["breakfast_weight"] = 0.87

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=payload,
        )

    assert response.status_code == 200
    assert len(pair_runner.calls) == 1
    pair_intent = pair_runner.calls[0][0]
    assert pair_intent.require_breakfast is None
    assert pair_intent.breakfast_preference_mode == PreferenceMode.WEIGHTED
    assert pair_intent.breakfast_preference_weight == 0.87


@pytest.mark.asyncio
async def test_human_block_returns_interpretation_without_resolving_live_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    payload = _payload(text=("出发地：杭州，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房"))
    requirement = payload["requirement"]
    assert isinstance(requirement, dict)
    requirement["breakfast_mode"] = "weighted"
    requirement["breakfast_weight"] = 0.9

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert body["cached_pair_runs"] == []
    assert len(body["model_trace_scope_sha256"]) == 64
    assert body["model_trace_count"] == 0
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 0
    assert "destination" in {item["field"] for item in body["interpretation"]["unresolved"]}
    application_issue = next(
        item
        for item in body["interpretation"]["unresolved"]
        if item["field"] == "preference_application:hotel_breakfast"
    )
    assert "尚未启动实时报价与 Planner" in application_issue["reason"]


@pytest.mark.asyncio
async def test_human_block_response_binds_successful_model_trace_to_this_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = InMemoryModelTraceSink()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        model_client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        class TracingRequirementAgent:
            async def parse(self, request: Any) -> Any:
                await model_client.complete(
                    ModelRequest(
                        role=AgentRole.CONTEXT,
                        system="bounded requirement fixture",
                        messages=(ModelMessage(role="user", content=request.text),),
                    )
                )
                return await package_requirement_agent.parse(request)

        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(app.state, "package_requirement_agent", TracingRequirementAgent())
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(
                    text=("出发地：杭州，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房")
                ),
            )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert body["model_trace_count"] == 1
    assert body["model_trace_success_count"] == 1
    assert body["model_trace_failure_count"] == 0
    trace = sink.records[0]
    assert trace.scope_request_digest == body["model_trace_scope_sha256"]
    assert trace.scope_id is not None and trace.scope_id.startswith("model-scope-")
    assert "出发地：杭州" not in trace.model_dump_json()


@pytest.mark.asyncio
async def test_model_failure_is_not_misreported_as_successful_enhancement_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = InMemoryModelTraceSink()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"type": "temporarily_unavailable", "message": "private"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        model_client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        class FailureTolerantRequirementAgent:
            async def parse(self, request: Any) -> Any:
                with pytest.raises(ModelHTTPError):
                    await model_client.complete(
                        ModelRequest(
                            role=AgentRole.CONTEXT,
                            system="bounded failing fixture",
                            messages=(ModelMessage(role="user", content=request.text),),
                        )
                    )
                return await package_requirement_agent.parse(request)

        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(
            app.state,
            "package_requirement_agent",
            FailureTolerantRequirementAgent(),
        )
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(text="出发地：杭州，2026年8月出发，玩5晚，2名成人，1间房"),
            )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["model_trace_count"] == 1
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 1


@pytest.mark.asyncio
async def test_unknown_city_iata_blocks_before_any_live_pair_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(pair_runner, now=lambda: NOW)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(
                text=("出发地：杭州，目的地：曼谷，2026年8月出发，玩5晚，2名成人，1间房")
            ),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert not pair_runner.calls
    unresolved = {item["field"]: item for item in body["interpretation"]["unresolved"]}
    assert unresolved["destination_code"]["critical"] is True
    assert "避免模型猜测或伪造机场代码" in unresolved["destination_code"]["reason"]


@pytest.mark.asyncio
async def test_from_text_endpoint_enforces_strict_policy_before_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(pair_runner, now=lambda: NOW)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(coverage_mode="degraded"),
        )

    assert response.status_code == 422
    assert "strict full-coverage mode" in response.json()["detail"]
    assert not pair_runner.calls


@pytest.mark.asyncio
async def test_from_text_endpoint_is_loopback_only() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(),
        )

    assert response.status_code == 403


def test_from_text_timeouts_are_capped_by_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    assert _live_timeout_seconds(300) == 60
    assert _flexible_total_timeout_seconds(1800) == 120
