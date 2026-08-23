from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
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
from tripchord.main import (
    LiveRunCache,
    _flexible_total_timeout_seconds,
    _live_timeout_seconds,
    app,
    package_requirement_agent,
    settings,
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
            "evidence_refs": (
                "https://kaanihotels.com/stays/Beach-Hotel/book",
            ),
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
            evidence_refs=("https://sfs-api.icomtours.com/api/v1/public/ferry-fares/schedule-base-price",),
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
        inventory=PackageInventory(
            flights=(flight,), lodgings=(ctrip, kaani), transfers=transfers
        ),
        stay_plan_inventory_outcomes=(ctrip_outcome,),
        kaani_lodging_results=(kaani_result,),
        normalization_results=(
            NormalizedBrowserQuoteResult(
                provider="qunar", kind=BrowserVertical.FLIGHT,
                status=QuoteNormalizationStatus.USABLE, quote=flight,
            ),
            NormalizedBrowserQuoteResult(
                provider="ctrip", kind=BrowserVertical.LODGING,
                status=QuoteNormalizationStatus.USABLE, quote=ctrip,
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
            "icom_cny_reference_estimate": (
                estimate if ctrip_decision is not None else None
            ),
        }
    )
    return LivePackageAgentRun.model_validate(payload)


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
    assert {
        tuple(item["lodging_dates"])
        for item in candidates
    } == {("2026-09-04", "2026-09-09")}
    by_provider = {item["lodging_provider"]: item for item in candidates}
    assert by_provider["ctrip"]["selected"] is True
    assert by_provider["kaani_official"]["selected"] is False
    assert by_provider["ctrip"]["estimated_total_cny_cents"] == 1_187_147
    assert by_provider["kaani_official"]["estimated_total_cny_cents"] == 1_293_926
    assert by_provider["ctrip"]["flight_component_id"] == by_provider["kaani_official"][
        "flight_component_id"
    ]
    assert by_provider["ctrip"]["transfer_component_ids"] == by_provider["kaani_official"][
        "transfer_component_ids"
    ]
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
        for item in pair_run["exact_quote_comparison_coverage"]["segments"][0][
            "provider_evidence"
        ]
    } >= {"ctrip", "kaani_official"}

    return_departure_text = (
        "2026年9月3日从杭州出发去马尔代夫，回程最晚在2026年9月9日，"
        "2名成人，1间房。"
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
    assert return_departure["best_available_plan"]["flight"][
        "return_arrive_at"
    ].startswith("2026-09-10")

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
async def test_http_endpoint_does_not_rank_foreign_candidate_without_ecb_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _TwoCandidatePairRunner(kaani_reference_complete=False)
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
    candidates = {
        item["lodging_provider"]: item for item in body["decision_candidates"]
    }
    assert {
        tuple(item["lodging_dates"])
        for item in candidates.values()
    } == {("2026-09-04", "2026-09-09")}
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
    assert body["best_available_plan"]["flight"]["outbound_depart_at"].startswith(
        "2026-09-03"
    )
    assert body["best_available_plan"]["flight"]["return_depart_at"].startswith(
        "2026-09-09"
    )
    assert body["best_available_plan"]["lodgings"][0]["check_in"] == "2026-09-04"
    assert body["best_available_plan"]["lodgings"][0]["check_out"] == "2026-09-09"
    assert body["best_available_plan"]["estimated_total_cny_cents"] == 1_187_147
    pair_run = body["run"]["pair_runs"][0]["run"]
    kaani_evidence = next(
        item
        for item in pair_run["exact_quote_comparison_coverage"]["segments"][0][
            "provider_evidence"
        ]
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
                    text=(
                        "出发地：杭州，去程：2026年8月，玩5-8天，"
                        "人数：2名成人，酒店：1间房"
                    )
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
