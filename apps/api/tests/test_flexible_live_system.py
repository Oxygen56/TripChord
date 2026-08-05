from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexibleLiveAgentSystem,
    FlexiblePairState,
)
from tripchord.agents.live_done_gate import _check_flexible_ranked_options
from tripchord.agents.live_jobs import (
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
)
from tripchord.agents.live_system import (
    ExactQuoteComparisonCoverage,
    LiveCoverageMode,
    LivePackageAgentRun,
    LodgingProviderQuoteEvidence,
    LodgingSegmentQuoteComparisonCoverage,
    PlatformSearchCoverage,
)
from tripchord.agents.models import AgentRole, AgentTask, AgentTaskResult, TaskGraph
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.agents.stay_area import system_stay_area_search_profile
from tripchord.planning.adaptive_dates import (
    AdaptiveRefinementDecision,
    ExactDatePairObservation,
)
from tripchord.planning.flexible_dates import AuditableDatePair, FlexibleTravelWindow
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageCandidateKind,
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
    PackageOrchestrator,
    PackagePlaceKey,
    PackagePlanner,
    TransferOption,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
    TravelPackageCandidate,
)
from tripchord.planning.stay_plans import (
    StayInventoryResultState,
    system_stay_plan_candidate_set,
)
from tripchord.providers.browser_bridge import (
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskCompletion,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(hours=1)
MALDIVES = timezone(timedelta(hours=5))
CHINA = timezone(timedelta(hours=8))
REQUEST_SHA256 = "1" * 64


def window() -> FlexibleTravelWindow:
    return FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        origin_code="HGH",
        destination_code="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 3),
        min_nights=5,
        max_nights=5,
        max_pairs=3,
        adults=2,
        rooms=1,
    )


class ExpansionProbeRefiner:
    def next_pair(
        self,
        candidates: tuple[AuditableDatePair, ...],
        observations: tuple[ExactDatePairObservation, ...],
        *,
        exact_pair_budget: int,
    ) -> AdaptiveRefinementDecision:
        selected = candidates[0] if not observations else candidates[-1]
        return AdaptiveRefinementDecision(
            round=len(observations) + 1,
            selected_pair_id=selected.id,
            remaining_budget_pairs=exact_pair_budget - len(observations) - 1,
            reason="测试真实结果后从完整未查询日期池动态扩展",
        )


def _source_ids(provider: BrowserProvider) -> tuple[str, ...]:
    return tuple(
        f"source-{provider.value}-{suffix}"
        for suffix in (
            "flight",
            "lodging-full",
            "lodging-first",
            "lodging-middle",
            "lodging-last",
        )
    )


def _flight(request: PackageIntent, total_cents: int) -> NormalizedFlightQuote:
    return NormalizedFlightQuote(
        id=f"ctrip:flight:{request.start_date}",
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=EXPIRES,
        evidence_refs=(f"evidence:flight:{request.start_date}",),
        origin=request.origin,
        destination=request.destination,
        adults=request.adults,
        outbound_depart_at=datetime.combine(
            request.start_date,
            datetime.min.time(),
            tzinfo=CHINA,
        ).replace(hour=8, minute=30),
        outbound_arrive_at=datetime.combine(
            request.start_date,
            datetime.min.time(),
            tzinfo=MALDIVES,
        ).replace(hour=18, minute=35),
        return_depart_at=datetime.combine(
            request.end_date,
            datetime.min.time(),
            tzinfo=MALDIVES,
        ).replace(hour=10, minute=45),
        return_arrive_at=datetime.combine(
            request.end_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=CHINA,
        ).replace(hour=9, minute=10),
        checked_baggage_per_adult_kg=0,
    )


def _lodging(
    request: PackageIntent,
    suffix: str,
    area: PackageArea,
    check_in: date,
    check_out: date,
    total_cents: int,
) -> NormalizedLodgingQuote:
    return NormalizedLodgingQuote(
        id=f"ctrip:lodging:{request.start_date}:{suffix}",
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=EXPIRES,
        evidence_refs=(f"evidence:lodging:{request.start_date}:{suffix}",),
        property_name=f"{suffix} stay",
        area=area,
        check_in=check_in,
        check_out=check_out,
        adults=request.adults,
        rooms=request.rooms,
        breakfast_included=False,
        place_key=(
            PackagePlaceKey.HULHUMALE
            if area == PackageArea.AIRPORT_ISLAND
            else PackagePlaceKey.MAAFUSHI
        ),
    )


def _transfer(
    request: PackageIntent,
    suffix: str,
    origin: PackageArea,
    destination: PackageArea,
    travel_date: date,
    depart_hour: int,
    depart_minute: int,
    arrive_hour: int,
    arrive_minute: int,
    total_cents: int,
) -> TransferOption:
    depart_at = datetime.combine(
        travel_date,
        datetime.min.time(),
        tzinfo=MALDIVES,
    ).replace(hour=depart_hour, minute=depart_minute)
    arrive_at = datetime.combine(
        travel_date,
        datetime.min.time(),
        tzinfo=MALDIVES,
    ).replace(hour=arrive_hour, minute=arrive_minute)
    duration_minutes = int((arrive_at - depart_at).total_seconds() // 60)
    return TransferOption(
        id=f"ctrip:transfer:{request.start_date}:{suffix}",
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=EXPIRES,
        evidence_refs=(f"evidence:transfer:{request.start_date}:{suffix}",),
        origin_area=origin,
        destination_area=destination,
        adults=request.adults,
        service_date=travel_date,
        schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
        duration_minutes=duration_minutes,
        depart_at=depart_at,
        arrive_at=arrive_at,
        operates_24_hours=False,
        requires_reservation=True,
        price_scope=TransferPriceScope.ONE_WAY,
        price_contract_id=f"price:{request.start_date}:{suffix}",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        contract_evidence_text=(
            f"单程 {origin.value} → {destination.value}，"
            f"{duration_minutes}分钟，含税总价 CNY {total_cents / 100:.2f}"
        ),
        detail_url="https://hotels.ctrip.com/hotels/detail/transfer-fixture",
    )


def _inventory(request: PackageIntent, total_cents: int) -> PackageInventory:
    first_checkout = request.start_date + timedelta(days=1)
    last_checkin = request.end_date - timedelta(days=1)
    return PackageInventory(
        flights=(_flight(request, total_cents),),
        lodgings=(
            _lodging(
                request,
                "direct",
                PackageArea.DESTINATION_ISLAND,
                request.start_date,
                request.end_date,
                350_000,
            ),
            _lodging(
                request,
                "first",
                PackageArea.AIRPORT_ISLAND,
                request.start_date,
                first_checkout,
                39_600,
            ),
            _lodging(
                request,
                "middle",
                PackageArea.DESTINATION_ISLAND,
                first_checkout,
                last_checkin,
                336_500,
            ),
            _lodging(
                request,
                "last",
                PackageArea.AIRPORT_ISLAND,
                last_checkin,
                request.end_date,
                39_600,
            ),
        ),
        transfers=(
            _transfer(
                request,
                "direct-out",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                request.start_date,
                19,
                20,
                20,
                5,
                36_000,
            ),
            _transfer(
                request,
                "direct-back",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                request.end_date,
                7,
                30,
                8,
                15,
                36_000,
            ),
            _transfer(
                request,
                "airport-hotel",
                PackageArea.AIRPORT,
                PackageArea.AIRPORT_ISLAND,
                request.start_date,
                19,
                20,
                19,
                40,
                10_800,
            ),
            _transfer(
                request,
                "first-hotel-airport",
                PackageArea.AIRPORT_ISLAND,
                PackageArea.AIRPORT,
                first_checkout,
                6,
                40,
                7,
                0,
                10_800,
            ),
            _transfer(
                request,
                "airport-destination-next-day",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                first_checkout,
                7,
                30,
                8,
                15,
                36_000,
            ),
            _transfer(
                request,
                "destination-airport-day-before",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                last_checkin,
                16,
                0,
                16,
                45,
                36_000,
            ),
            _transfer(
                request,
                "airport-last-hotel",
                PackageArea.AIRPORT,
                PackageArea.AIRPORT_ISLAND,
                last_checkin,
                17,
                30,
                17,
                50,
                10_800,
            ),
            _transfer(
                request,
                "hotel-airport",
                PackageArea.AIRPORT_ISLAND,
                PackageArea.AIRPORT,
                request.end_date,
                6,
                50,
                7,
                10,
                10_800,
            ),
        ),
    )


def _coverage(*, complete: bool) -> tuple[PlatformSearchCoverage, ...]:
    result: list[PlatformSearchCoverage] = []
    for provider in BrowserProvider:
        source_ids = _source_ids(provider)
        provider_complete = complete or provider != BrowserProvider.QUNAR
        successful = source_ids if provider_complete else source_ids[:-1]
        failed = () if provider_complete else (source_ids[-1],)
        result.append(
            PlatformSearchCoverage(
                provider=provider,
                successful_verticals=(
                    (BrowserVertical.FLIGHT, BrowserVertical.LODGING)
                    if provider_complete
                    else (BrowserVertical.FLIGHT,)
                ),
                failed_verticals=(() if provider_complete else (BrowserVertical.LODGING,)),
                successful_source_ids=successful,
                failed_source_ids=failed,
                failure_reasons=(() if provider_complete else ("qunar last lodging unavailable",)),
                complete=provider_complete,
            )
        )
    return tuple(result)


def _exact_quote_comparison_coverage(
    candidate: TravelPackageCandidate,
    *,
    complete: bool = True,
) -> ExactQuoteComparisonCoverage:
    lodging = candidate.lodgings[0]
    provider_evidence = (
        LodgingProviderQuoteEvidence(
            provider=BrowserProvider.CTRIP,
            source_task_id="source-ctrip-lodging-full",
            inventory_state=StayInventoryResultState.QUOTE_FOUND,
            quote_ids=(f"fixture:ctrip:{lodging.id}",),
            evidence_refs=(f"fixture-evidence:ctrip:{lodging.id}",),
            source_execution_terminal=True,
        ),
        LodgingProviderQuoteEvidence(
            provider=BrowserProvider.QUNAR,
            source_task_id="source-qunar-lodging-full",
            inventory_state=(
                StayInventoryResultState.QUOTE_FOUND
                if complete
                else StayInventoryResultState.CONFIRMED_EMPTY
            ),
            quote_ids=((f"fixture:qunar:{lodging.id}",) if complete else ()),
            evidence_refs=(f"fixture-evidence:qunar:{lodging.id}",),
            source_execution_terminal=True,
        ),
    )
    segment = LodgingSegmentQuoteComparisonCoverage(
        segment_id="fixture-full",
        exact_place_key=lodging.place_key,
        check_in=lodging.check_in,
        check_out=lodging.check_out,
        provider_evidence=provider_evidence,
        distinct_exact_quote_provider_count=(2 if complete else 1),
        complete=complete,
    )
    return ExactQuoteComparisonCoverage(
        segments=(segment,),
        complete=complete,
        partial_evidence_only=not complete,
    )


def _accepted_run(
    request: PackageIntent,
    search_query: BrowserSearchQuery,
    mode: LiveCoverageMode,
    *,
    total_cents: int,
    complete: bool,
    exact_quote_comparison_complete: bool = True,
) -> LivePackageAgentRun:
    inventory = _inventory(request, total_cents)
    candidates = PackagePlanner().generate(request, inventory)
    direct = next(
        item for item in candidates if item.kind == PackageCandidateKind.CONTINUOUS_ISLAND
    )
    package = PackageOrchestrator().execute(
        request,
        direct,
        inventory,
        now=NOW,
    )
    assert package.final_decision.state == PackageDecisionState.ACCEPT
    if not exact_quote_comparison_complete:
        blocking = PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="fixture single-source lodging is partial evidence only",
            evidence_refs=package.final_candidate.evidence_refs,
        )
        package = package.model_copy(
            update={
                "decisions": (*package.decisions, blocking),
                "final_decision": blocking,
            }
        )
    coverage = _coverage(complete=complete)
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
    final_results = tuple(
        AgentTaskResult(
            task_id=task.id,
            agent_role=task.role,
            success=True,
            summary="fixture stage complete",
            output={
                "publication_gate_passed": True,
            }
            if task.id == "publish-live-run"
            else {},
        )
        for task in final_tasks
    )
    return LivePackageAgentRun(
        mode=mode,
        intent=request,
        search_query=search_query,
        decision=package.final_decision,
        claim_boundary="fixture live run",
        all_platforms_complete=all(item.complete for item in coverage),
        exact_quote_comparison_coverage=_exact_quote_comparison_coverage(
            package.final_candidate,
            complete=exact_quote_comparison_complete,
        ),
        coverage=coverage,
        inventory=inventory,
        normalization_results=(),
        package=package,
        scheduler=SchedulerOutcome(
            graph=TaskGraph(tasks=final_tasks),
            results=final_results,
            trace=(),
            wall_time_seconds=0,
            max_parallel_tasks=15,
            succeeded=True,
        ),
        source_task_ids=tuple(
            source_id for provider in BrowserProvider for source_id in _source_ids(provider)
        ),
    )


class FakeLiveRunner:
    def __init__(
        self,
        *,
        failing_date: date | None = None,
        complete: bool = True,
        exact_quote_comparison_complete: bool = True,
    ) -> None:
        self.failing_date = failing_date
        self.complete = complete
        self.exact_quote_comparison_complete = exact_quote_comparison_complete
        self.calls: list[tuple[PackageIntent, dict[str, int]]] = []
        self.queries: list[BrowserSearchQuery] = []

    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del timeout_seconds
        delays = source_start_delays_ms or {}
        self.calls.append((request, delays))
        self.queries.append(search_query)
        if request.start_date == self.failing_date:
            raise RuntimeError("fixture provider batch failed")
        variable_flight_total = 900_000 + request.start_date.day * 10_000
        return _accepted_run(
            request,
            search_query,
            mode,
            total_cents=variable_flight_total,
            complete=self.complete,
            exact_quote_comparison_complete=(
                self.exact_quote_comparison_complete
            ),
        )


class ConcurrencyProbeLiveRunner:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.started_dates: list[date] = []
        self.completed_dates: list[date] = []
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del timeout_seconds, source_start_delays_ms
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started_dates.append(request.start_date)
        if self.active == 3:
            self.all_started.set()
        try:
            await self.release.wait()
            await asyncio.sleep((4 - request.start_date.day) * 0.01)
            self.completed_dates.append(request.start_date)
            return _accepted_run(
                request,
                search_query,
                mode,
                total_cents=900_000,
                complete=True,
            )
        finally:
            self.active -= 1


class BlockingLiveRunner:
    def __init__(self) -> None:
        self.active = 0
        self.all_started = asyncio.Event()
        self.cancelled_dates: set[date] = set()
        self.block = asyncio.Event()

    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del search_query, mode, timeout_seconds, source_start_delays_ms
        self.active += 1
        if self.active == 1:
            self.all_started.set()
        try:
            await self.block.wait()
        except asyncio.CancelledError:
            self.cancelled_dates.add(request.start_date)
            raise
        raise AssertionError("blocking fixture must be cancelled")


class V4GlobalLeaseProbeLiveRunner:
    def __init__(self) -> None:
        self.bridge = BrowserTaskBridge(now=lambda: NOW)
        self.active_pairs = 0
        self.max_active_pairs = 0
        self.submitted = 0
        self.completed = 0
        self.claim_wave_sizes: list[int] = []

    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del timeout_seconds, source_start_delays_ms
        self.active_pairs += 1
        self.max_active_pairs = max(self.max_active_pairs, self.active_pairs)

        try:
            submissions = tuple(
                BrowserTaskSubmission(
                    provider=tuple(BrowserProvider)[index % 3],
                    kind=BrowserVertical.LODGING,
                    query=search_query,
                    timeout_seconds=15,
                    max_attempts=1,
                )
                for index in range(13)
            )
            snapshots = await self.bridge.submit_many(submissions)
            self.submitted += len(snapshots)
            terminal = await self.bridge.wait_many(
                (item.id for item in snapshots),
                timeout_seconds=2,
            )
            self.completed += sum(item.state.terminal for item in terminal)
            return _accepted_run(
                request,
                search_query,
                mode,
                total_cents=900_000,
                complete=True,
            )
        finally:
            self.active_pairs -= 1

    async def serve_all_sources(self, expected: int) -> None:
        while self.completed < expected:
            leases = await self.bridge.claim(
                "edge-six-lease-probe",
                providers=tuple(BrowserProvider),
                limit=6,
            )
            if not leases:
                await asyncio.sleep(0)
                continue
            self.claim_wave_sizes.append(len(leases))
            await asyncio.gather(
                *(
                    self.bridge.complete(
                        lease.task_id,
                        lease.claim_token,
                        BrowserTaskCompletion(
                            state=BrowserTaskState.FAILED,
                            failure=BrowserFailure(
                                code=BrowserFailureCode.DOM_DRIFT,
                                message="bounded lease probe completion",
                                captured_at=NOW,
                            ),
                        ),
                    )
                    for lease in leases
                )
            )


@pytest.mark.asyncio
async def test_three_date_pairs_are_admitted_serially_to_preserve_quote_freshness() -> None:
    fake = ConcurrencyProbeLiveRunner()
    checkpoints: list[LivePlanningPairCheckpoint] = []

    async def report(checkpoint: LivePlanningPairCheckpoint) -> None:
        checkpoints.append(checkpoint)

    system = FlexibleLiveAgentSystem(
        fake,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )

    fake.release.set()
    result = await asyncio.wait_for(
        system.run(
            window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=3,
            timeout_seconds=15,
            pair_checkpoint_reporter=report,
            checkpoint_request_sha256=REQUEST_SHA256,
        ),
        timeout=2,
    )

    expected_pair_ids = result.query_plan.selected_pair_ids
    assert fake.max_active == 1
    assert fake.completed_dates == [
        item.date_pair.departure_date for item in result.pair_runs
    ]
    assert tuple(item.date_pair.id for item in result.pair_runs) == expected_pair_ids
    assert {item.date_pair_id for item in result.ranked_options} == set(expected_pair_ids)
    assert result.ranked_options[1].departure_date == date(2026, 8, 3)
    assert result.ranked_options[1].diversity_tags != result.ranked_options[0].diversity_tags
    assert all(item.pareto_front == 1 for item in result.ranked_options)
    assert result.query_plan.total_task_count == 33
    assert [
        item.source_start_delays_ms["source-ctrip-flight"] for item in result.pair_runs
    ] == [0, 5_000, 10_000]
    assert [
        item.source_start_delays_ms["source-qunar-lodging-last"]
        for item in result.pair_runs
    ] == [4_000, 9_000, 14_000]
    assert tuple(item.sequence for item in checkpoints) == (1, 2, 3)
    assert tuple(item.date_pair_id for item in checkpoints) == expected_pair_ids
    assert all(item.state == LivePlanningPairCheckpointState.COMPLETED for item in checkpoints)
    assert all(len(item.query_task_ids) == 11 for item in checkpoints)
    assert all(item.run_summary_sha256 != item.checkpoint_sha256 for item in checkpoints)
    serialized = "".join(item.model_dump_json() for item in checkpoints)
    assert "detail_url" not in serialized
    assert "visible_evidence" not in serialized
    assert "total_cents" not in serialized


@pytest.mark.asyncio
async def test_adaptive_refiner_can_expand_beyond_initial_exact_shortlist() -> None:
    expanded_window = window().model_copy(
        update={
            "latest_departure": date(2026, 8, 6),
            "max_pairs": 6,
        }
    )
    result = await FlexibleLiveAgentSystem(
        FakeLiveRunner(),
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
        date_refiner=ExpansionProbeRefiner(),
    ).run(
        expanded_window,
        mode=LiveCoverageMode.STRICT,
        max_pairs=2,
        timeout_seconds=15,
    )

    initial_shortlist = {item.id for item in result.exploration.candidates[:2]}
    second_id = result.refinement_trace[1].selected_pair_id
    assert second_id == result.exploration.candidates[-1].id
    assert second_id not in initial_shortlist
    assert result.query_plan.selected_pair_ids == tuple(
        item.date_pair.id for item in result.pair_runs
    )
    assert second_id in result.query_plan.selected_pair_ids


@pytest.mark.asyncio
async def test_execution_time_lead_window_excludes_near_term_and_spans_month() -> None:
    broad_window = window().model_copy(
        update={
            "earliest_departure": date(2026, 8, 1),
            "latest_departure": date(2026, 8, 31),
        }
    )
    result = await FlexibleLiveAgentSystem(
        FakeLiveRunner(),
        now=lambda: datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        monotonic_clock=lambda: 100.0,
        minimum_departure_lead_days=7,
    ).run(
        broad_window,
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        timeout_seconds=15,
    )

    assert result.requested_window.earliest_departure == date(2026, 8, 1)
    assert result.effective_window.earliest_departure == date(2026, 8, 8)
    assert len(result.exploration.candidates) == result.effective_window.universe_size
    by_id = {item.id: item for item in result.exploration.candidates}
    assert [by_id[item].departure_date for item in result.query_plan.selected_pair_ids] == [
        date(2026, 8, 8),
        date(2026, 8, 31),
        date(2026, 8, 19),
    ]
    assert any("提前期不足 7 天" in warning for warning in result.exploration.warnings)


@pytest.mark.asyncio
async def test_v4_admission_completes_all_39_sources_with_six_global_leases() -> None:
    fake = V4GlobalLeaseProbeLiveRunner()
    system = FlexibleLiveAgentSystem(
        fake,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    v4_window = window().model_copy(
        update={
            "origin": "杭州",
            "destination": "马累",
        }
    )

    result, _ = await asyncio.gather(
        system.run(
            v4_window,
            mode=LiveCoverageMode.STRICT,
            max_pairs=3,
            timeout_seconds=15,
            stay_plan_candidate_set=system_stay_plan_candidate_set(),
        ),
        fake.serve_all_sources(39),
    )

    assert result.query_plan.total_task_count == 39
    assert fake.submitted == fake.completed == 39
    # Qunar lodging is admitted one lease at a time, so each 13-source pair
    # drains as 6 + 5 + 1 + 1 instead of pre-leasing two same-domain searches.
    assert fake.claim_wave_sizes == [6, 5, 1, 1] * 3
    assert fake.max_active_pairs == 1
    assert len(result.pair_runs) == 3
    for execution in result.pair_runs:
        planned_pair_tasks = tuple(
            task
            for task in result.query_plan.tasks
            if task.date_pair_id == execution.date_pair.id
        )
        assert execution.query_tasks == planned_pair_tasks
        assert execution.source_start_delays_ms["source-ctrip-flight"] == 0
        assert execution.source_start_delays_ms["source-tongcheng-flight"] == 0
        assert (
            execution.source_start_delays_ms["source-qunar-lodging-hulhumale-full"]
            == 200_000
        )
    assert [
        execution.query_tasks[0].scheduled_offset_ms
        for execution in result.pair_runs
    ] == [0, 240_000, 480_000]
    assert [
        next(
            task.scheduled_offset_ms
            for task in execution.query_tasks
            if task.platform.value == "tongcheng"
        )
        for execution in result.pair_runs
    ] == [0, 40_000, 80_000]


@pytest.mark.asyncio
async def test_one_date_failure_is_isolated_and_other_repaired_options_are_ranked() -> None:
    fake = FakeLiveRunner(failing_date=date(2026, 8, 1))
    system = FlexibleLiveAgentSystem(
        fake,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )

    result = await system.run(
        window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        timeout_seconds=15,
    )

    assert result.query_plan.total_task_count == 33
    assert result.query_plan.task_count_by_platform == {
        "ctrip": 15,
        "tongcheng": 3,
        "qunar": 15,
    }
    assert len(result.pair_runs) == 3
    failed = next(
        item for item in result.pair_runs if item.date_pair.departure_date == date(2026, 8, 1)
    )
    assert failed.state == FlexiblePairState.FAILED
    assert failed.failure_class == "RuntimeError"
    completed = tuple(
        item for item in result.pair_runs if item.state == FlexiblePairState.COMPLETED
    )
    assert len(completed) == 2
    assert all(
        item.run is not None
        and item.run.package is not None
        and [decision.state for decision in item.run.package.decisions]
        == [
            PackageDecisionState.REJECT_AND_REPLAN,
            PackageDecisionState.ACCEPT,
        ]
        and item.run.package.final_candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
        for item in completed
    )
    assert result.final_decision.state == PackageDecisionState.ACCEPT
    assert "实际完成的 2 个精确日期对" in result.final_decision.summary
    assert len(result.recommended_option_ids) == 2
    assert result.ranked_options[0].departure_date == date(2026, 8, 2)
    assert result.ranked_options[1].departure_date == date(2026, 8, 3)
    assert result.ranked_options[2].recommendable is False
    assert not result.sampled_not_exhaustive
    assert len(result.refinement_trace) == 3
    assert result.refinement_trace[0].selected_pair_id == result.query_plan.selected_pair_ids[0]
    assert len(
        {
            item.selected_pair_id
            for item in result.refinement_trace
            if item.selected_pair_id is not None
        }
    ) == 3
    assert "Query Strategist" in result.refinement_trace[1].reason
    assert "保守 fallback" in result.claim_boundary
    assert "不证明真实 OTA" in result.claim_boundary
    assert "不得声称全月最低价" in result.claim_boundary
    assert "不是用户原话，可改" in result.claim_boundary
    assert result.stay_area_search_profile is not None
    assert result.stay_area_search_profile.gateway_destination == "MLE"
    assert result.stay_area_search_profile.destination_island_lodging_search_term == "Maafushi"
    assert result.stay_area_search_profile.airport_island_lodging_search_term == "Hulhumalé"
    assert len(fake.calls) == 3
    assert all(
        request.destination_place_key == PackagePlaceKey.MAAFUSHI for request, _ in fake.calls
    )
    assert all(query.destination == "MLE" for query in fake.queries)
    assert all(query.origin_code == "HGH" for query in fake.queries)
    assert all(query.destination_code == "MLE" for query in fake.queries)
    assert all(query.options["gateway_destination"] == "MLE" for query in fake.queries)
    gate = _check_flexible_ranked_options(result)
    assert not gate.passed
    assert gate.evidence["recommendable_count"] == 2
    assert gate.evidence["recommended_pairs_have_icom_4_of_4"] is False
    first_delays = fake.calls[0][1]
    second_delays = fake.calls[1][1]
    assert first_delays["source-ctrip-flight"] == 0
    assert first_delays["source-ctrip-lodging-last"] == 4_000
    assert second_delays["source-ctrip-flight"] == 5_000
    assert second_delays["source-qunar-lodging-last"] == 9_000


@pytest.mark.asyncio
async def test_publication_refresh_fails_closed_when_exploration_has_fewer_than_two_options(
) -> None:
    constrained_window = window().model_copy(
        update={"latest_departure": date(2026, 8, 2), "max_pairs": 2}
    )
    system = FlexibleLiveAgentSystem(
        FakeLiveRunner(failing_date=date(2026, 8, 1)),
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )

    result = await system.run(
        constrained_window,
        mode=LiveCoverageMode.STRICT,
        max_pairs=2,
        timeout_seconds=15,
        publication_refresh_minimum_options=2,
    )

    assert result.recommended_option_ids == ()
    assert result.publication_refreshed_option_ids == ()
    assert result.final_decision.state == PackageDecisionState.HUMAN_BLOCK
    # Protocol fakes cannot mint the concrete system's deterministic exploration
    # seal, so neither the failed date nor the otherwise ACCEPT fake is eligible
    # for publication refresh.
    assert "只有 0 个" in result.final_decision.summary
    assert "少于冻结下限 2" in result.final_decision.summary


def test_two_publication_options_cannot_share_a_browser_task_id() -> None:
    option_ids = ("pair:one:maafushi_icom", "pair:two:maafushi_icom")

    def fixture(second_browser_task_id: str) -> SimpleNamespace:
        audits = (
            SimpleNamespace(
                binding_passed=True,
                refreshed_option_id=option_ids[0],
                browser_task_ids=("browser-task:one",),
            ),
            SimpleNamespace(
                binding_passed=True,
                refreshed_option_id=option_ids[1],
                browser_task_ids=(second_browser_task_id,),
            ),
        )
        return SimpleNamespace(
            publication_refresh_minimum_options=2,
            pair_runs=tuple(
                SimpleNamespace(publication_refresh_audit=audit) for audit in audits
            ),
            ranked_options=tuple(
                SimpleNamespace(option_id=option_id) for option_id in option_ids
            ),
            publication_refreshed_option_ids=option_ids,
            recommended_option_ids=option_ids,
            final_decision=SimpleNamespace(state=PackageDecisionState.ACCEPT),
        )

    distinct = fixture("browser-task:two")
    assert FlexibleLiveAgentRun.validate_publication_refresh(distinct) is distinct

    with pytest.raises(ValueError, match="globally distinct browser tasks"):
        FlexibleLiveAgentRun.validate_publication_refresh(fixture("browser-task:one"))


def test_publication_shortfall_distinguishes_binding_from_final_accept() -> None:
    summary = FlexibleLiveAgentSystem._publication_refresh_shortfall_summary(
        binding_passed_count=2,
        recommendable_count=0,
        minimum_options=2,
    )

    assert "2 个独立日期方案通过新鲜证据绑定审计" in summary
    assert "0 个完成最终 ACCEPT" in summary
    assert "少于冻结下限 2" in summary


@pytest.mark.asyncio
async def test_programming_error_is_not_misreported_as_an_isolated_date_failure() -> None:
    class ProgrammingErrorLiveRunner(FakeLiveRunner):
        async def run(
            self,
            request: PackageIntent,
            search_query: BrowserSearchQuery,
            *,
            mode: LiveCoverageMode = LiveCoverageMode.STRICT,
            timeout_seconds: int = 120,
            source_start_delays_ms: dict[str, int] | None = None,
        ) -> LivePackageAgentRun:
            del request, search_query, mode, timeout_seconds, source_start_delays_ms
            raise TypeError("fixture programming defect")

    system = FlexibleLiveAgentSystem(
        ProgrammingErrorLiveRunner(),
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )

    with pytest.raises(TypeError, match="programming defect"):
        await system.run(
            window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=1,
            timeout_seconds=15,
        )


@pytest.mark.asyncio
async def test_one_pair_timeout_is_isolated_without_cancelling_other_pairs() -> None:
    class OnePairTimeoutRunner(FakeLiveRunner):
        async def run(
            self,
            request: PackageIntent,
            search_query: BrowserSearchQuery,
            *,
            mode: LiveCoverageMode = LiveCoverageMode.STRICT,
            timeout_seconds: int = 120,
            source_start_delays_ms: dict[str, int] | None = None,
        ) -> LivePackageAgentRun:
            if request.start_date == date(2026, 8, 2):
                raise TimeoutError("fixture date-pair timeout")
            return await super().run(
                request,
                search_query,
                mode=mode,
                timeout_seconds=timeout_seconds,
                source_start_delays_ms=source_start_delays_ms,
            )

    checkpoints: list[LivePlanningPairCheckpoint] = []

    async def report(checkpoint: LivePlanningPairCheckpoint) -> None:
        checkpoints.append(checkpoint)

    result = await FlexibleLiveAgentSystem(
        OnePairTimeoutRunner(),
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    ).run(
        window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        timeout_seconds=15,
        pair_checkpoint_reporter=report,
        checkpoint_request_sha256=REQUEST_SHA256,
    )

    timed_out = next(
        item
        for item in result.pair_runs
        if item.date_pair.departure_date == date(2026, 8, 2)
    )
    assert timed_out.state == FlexiblePairState.FAILED
    assert timed_out.failure_class == "TimeoutError"
    assert timed_out.failure_message == "fixture date-pair timeout"
    assert sum(item.state == FlexiblePairState.COMPLETED for item in result.pair_runs) == 2
    assert len(result.recommended_option_ids) == 2
    assert result.final_decision.state == PackageDecisionState.ACCEPT
    timed_out_checkpoint = next(
        item for item in checkpoints if item.departure_date == date(2026, 8, 2)
    )
    assert timed_out_checkpoint.state == LivePlanningPairCheckpointState.FAILED
    assert timed_out_checkpoint.failure_class == "TimeoutError"
    assert "fixture date-pair timeout" not in timed_out_checkpoint.model_dump_json()


@pytest.mark.asyncio
async def test_outer_timeout_cancels_the_admitted_date_pair() -> None:
    fake = BlockingLiveRunner()
    run_task = asyncio.create_task(
        FlexibleLiveAgentSystem(
            fake,
            now=lambda: NOW,
            monotonic_clock=lambda: 100.0,
        ).run(
            window(),
            mode=LiveCoverageMode.STRICT,
            max_pairs=3,
            timeout_seconds=15,
        )
    )
    await asyncio.wait_for(fake.all_started.wait(), timeout=1)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await run_task

    assert len(fake.cancelled_dates) == 1


@pytest.mark.asyncio
async def test_other_destination_keeps_query_and_has_no_golden_stay_profile() -> None:
    fake = FakeLiveRunner()
    other_window = window().model_copy(update={"destination": "Tokyo", "destination_code": None})

    result = await FlexibleLiveAgentSystem(
        fake,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    ).run(
        other_window,
        mode=LiveCoverageMode.STRICT,
        max_pairs=1,
        timeout_seconds=15,
    )

    assert result.stay_area_search_profile is None
    assert "不是用户原话，可改" not in result.claim_boundary
    assert len(fake.queries) == 1
    assert fake.queries[0].destination == "Tokyo"
    assert fake.queries[0].destination_code is None
    assert fake.queries[0].options == {}
    assert fake.calls[0][0].destination_place_key is None


@pytest.mark.parametrize("destination", ["马累", "Malé", "MLE"])
def test_golden_profile_recognizes_supported_male_gateway_aliases(
    destination: str,
) -> None:
    profile = system_stay_area_search_profile(destination)

    assert profile is not None
    assert profile.gateway_destination == destination
    assert profile.destination_island_lodging_search_term == "Maafushi"
    assert profile.airport_island_lodging_search_term == "Hulhumalé"


@pytest.mark.asyncio
async def test_strict_mode_refuses_accept_label_without_three_platform_five_of_five() -> None:
    fake = FakeLiveRunner(complete=False)
    result = await FlexibleLiveAgentSystem(
        fake,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    ).run(
        window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=2,
        timeout_seconds=15,
    )

    assert all(item.run is not None for item in result.pair_runs)
    assert all(
        option.decision_state == PackageDecisionState.ACCEPT
        and not option.recommendable
        and not option.all_platforms_complete
        for option in result.ranked_options
    )
    assert result.recommended_option_ids == ()
    assert result.final_decision.state == PackageDecisionState.HUMAN_BLOCK


@pytest.mark.asyncio
async def test_single_source_package_stays_visible_but_never_enters_recommended_ids() -> None:
    result = await FlexibleLiveAgentSystem(
        FakeLiveRunner(exact_quote_comparison_complete=False),
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    ).run(
        window(),
        mode=LiveCoverageMode.STRICT,
        max_pairs=1,
        timeout_seconds=15,
    )

    execution = result.pair_runs[0]
    assert execution.run is not None
    assert execution.run.package is not None
    comparison = execution.run.exact_quote_comparison_coverage
    assert comparison is not None and comparison.partial_evidence_only
    assert result.ranked_options[0].total_budget_cents is not None
    assert not result.ranked_options[0].recommendable
    assert result.recommended_option_ids == ()
    assert result.final_decision.state == PackageDecisionState.HUMAN_BLOCK


@pytest.mark.asyncio
async def test_flexible_live_pair_bound_is_enforced() -> None:
    with pytest.raises(ValueError, match="one to eight"):
        await FlexibleLiveAgentSystem(
            FakeLiveRunner(),
            now=lambda: NOW,
        ).run(
            window(),
            max_pairs=9,
        )


def test_live_done_gate_rejects_missing_flexible_run() -> None:
    gate = _check_flexible_ranked_options(None)

    assert not gate.passed
    assert gate.id == "flexible_ranked_options"
