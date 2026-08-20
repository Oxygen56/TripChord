from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from tripchord.planning.flexible_dates import (
    EXPECTED_PLATFORMS,
    DateExplorationMode,
    DatePairSource,
    FareDateHint,
    FlexibleDateExplorer,
    FlexibleQueryPlanBuilder,
    FlexibleTravelWindow,
    PlatformFareCalendar,
    PlatformRatePolicy,
    QueryPlanPolicy,
    QueryTaskKind,
    TravelPlatform,
    canonical_acquisition_fingerprint,
)
from tripchord.planning.stay_plans import system_stay_plan_candidate_set

CAPTURED = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
EXPIRES = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def august_window(*, max_pairs: int = 6) -> FlexibleTravelWindow:
    return FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 31),
        min_nights=5,
        max_nights=8,
        max_pairs=max_pairs,
        adults=2,
        rooms=1,
    )


def hint(
    departure: date,
    nights: int,
    total_cents: int,
    ref: str,
) -> FareDateHint:
    return FareDateHint(
        departure_date=departure,
        return_date=departure + timedelta(days=nights),
        total_for_party_cents=total_cents,
        evidence_ref=ref,
    )


def calendar(
    platform: TravelPlatform,
    hints: tuple[FareDateHint, ...],
    *,
    complete: bool = False,
    captured_at: datetime = CAPTURED,
    expires_at: datetime = EXPIRES,
) -> PlatformFareCalendar:
    return PlatformFareCalendar(
        platform=platform,
        hints=hints,
        complete_for_window=complete,
        captured_at=captured_at,
        expires_at=expires_at,
    )


def policy(*, max_total_tasks: int = 90) -> QueryPlanPolicy:
    return QueryPlanPolicy(
        include_split_stays=True,
        max_total_tasks=max_total_tasks,
        platform_rates=(
            PlatformRatePolicy(
                platform=TravelPlatform.CTRIP,
                minimum_interval_ms=1_000,
                max_tasks=30,
            ),
            PlatformRatePolicy(
                platform=TravelPlatform.FLIGGY,
                minimum_interval_ms=1_500,
                max_tasks=30,
            ),
            PlatformRatePolicy(
                platform=TravelPlatform.QUNAR,
                minimum_interval_ms=2_000,
                max_tasks=30,
            ),
        ),
    )


def test_august_five_to_eight_nights_uses_bounded_stratified_query_plan() -> None:
    window = august_window(max_pairs=6)

    exploration = FlexibleDateExplorer().explore(window, now=NOW)

    assert exploration.mode == DateExplorationMode.SAMPLED_NOT_EXHAUSTIVE
    assert exploration.sampled_not_exhaustive is True
    assert exploration.universe_size == 31 * 4
    assert exploration.search_metrics.coarse_window_pair_count == 31 * 4
    assert exploration.search_metrics.prior_observed_pair_count == 0
    assert exploration.search_metrics.prior_coverage == 0
    assert exploration.search_metrics.recall_at_k is None
    assert exploration.search_metrics.price_regret_cents is None
    assert len(exploration.candidates) == 6
    assert exploration.missing_platforms == (
        TravelPlatform.CTRIP,
        TravelPlatform.FLIGGY,
        TravelPlatform.QUNAR,
    )
    assert all(
        date(2026, 8, 1) <= item.departure_date <= date(2026, 8, 31)
        and 5 <= item.night_count <= 8
        and item.return_date == item.departure_date + timedelta(days=item.night_count)
        and item.source == DatePairSource.STRATIFIED_SAMPLE
        for item in exploration.candidates
    )
    selected_boundaries = {
        (item.departure_date, item.night_count) for item in exploration.candidates
    }
    assert (date(2026, 8, 1), 5) in selected_boundaries
    assert (date(2026, 8, 31), 8) in selected_boundaries
    assert (date(2026, 8, 16), 6) in selected_boundaries
    assert any("不得表述为全月最低价" in warning for warning in exploration.warnings)

    plan = FlexibleQueryPlanBuilder().build(window, exploration, policy())

    assert plan.total_task_count == 90
    assert plan.search_metrics.exact_search_budget_pairs == 6
    assert plan.search_metrics.exact_search_coverage == Decimal(6) / Decimal(31 * 4)
    assert plan.task_count_by_platform == {
        "ctrip": 30,
        "fliggy": 30,
        "qunar": 30,
    }
    for pair_id in plan.selected_pair_ids:
        pair_tasks = [item for item in plan.tasks if item.date_pair_id == pair_id]
        assert len(pair_tasks) == 15
        for platform in EXPECTED_PLATFORMS:
            platform_tasks = [item for item in pair_tasks if item.platform == platform]
        assert {item.kind for item in platform_tasks} == {
                QueryTaskKind.FLIGHT,
                QueryTaskKind.LODGING_FULL_STAY,
                QueryTaskKind.LODGING_FIRST_NIGHT,
                QueryTaskKind.LODGING_MIDDLE_STAY,
                QueryTaskKind.LODGING_LAST_NIGHT,
            }
    for platform, interval in {
        TravelPlatform.CTRIP: 1_000,
        TravelPlatform.FLIGGY: 1_500,
        TravelPlatform.QUNAR: 2_000,
    }.items():
        offsets = [item.scheduled_offset_ms for item in plan.tasks if item.platform == platform]
        assert offsets == [index * interval for index in range(len(offsets))]


def test_august_coarse_stage_can_enumerate_all_124_pairs_without_claiming_price_coverage() -> None:
    exploration = FlexibleDateExplorer().explore(
        august_window(max_pairs=31 * 4),
        now=NOW,
    )

    assert exploration.mode == DateExplorationMode.FULL_UNIVERSE_NO_COMPLETE_PRIOR
    assert exploration.sampled_not_exhaustive is False
    assert len(exploration.candidates) == 31 * 4
    assert exploration.search_metrics.shortlist_coverage == Decimal(1)
    assert exploration.search_metrics.prior_coverage == 0
    assert exploration.search_metrics.recall_at_k is None
    assert any("完整日期组合" in warning for warning in exploration.warnings)


def test_consensus_beats_single_platform_price_and_missing_platform_degrades() -> None:
    window = august_window(max_pairs=4)
    consensus_date = date(2026, 8, 23)
    cheaper_single_date = date(2026, 8, 10)
    calendars = (
        calendar(
            TravelPlatform.CTRIP,
            (
                hint(consensus_date, 7, 938_400, "ctrip:consensus"),
                hint(cheaper_single_date, 7, 850_000, "ctrip:single-cheap"),
            ),
        ),
        calendar(
            TravelPlatform.FLIGGY,
            (hint(consensus_date, 7, 971_600, "fliggy:consensus"),),
        ),
    )

    result = FlexibleDateExplorer().explore(window, calendars, now=NOW)

    assert result.mode == DateExplorationMode.SAMPLED_NOT_EXHAUSTIVE
    assert result.sampled_not_exhaustive is True
    assert result.missing_platforms == (TravelPlatform.QUNAR,)
    assert result.candidates[0].departure_date == consensus_date
    assert result.candidates[0].consensus_count == 2
    assert result.candidates[0].median_total_for_party_cents == 955_000
    assert result.candidates[0].platform_coverage == Decimal(2) / Decimal(3)
    assert cheaper_single_date in {
        item.departure_date
        for item in result.candidates
        if item.source == DatePairSource.FUSED_FARE_HINT
    }

    plan = FlexibleQueryPlanBuilder().build(window, result, policy(max_total_tasks=60))
    assert any(item.platform == TravelPlatform.QUNAR for item in plan.tasks)
    assert all(
        {
            item.platform
            for item in plan.tasks
            if item.date_pair_id == pair_id and item.kind == QueryTaskKind.FLIGHT
        }
        == set(EXPECTED_PLATFORMS)
        for pair_id in plan.selected_pair_ids
    )


def test_query_hash_is_stable_across_calendar_and_hint_input_order() -> None:
    window = august_window(max_pairs=3)
    first = hint(date(2026, 8, 23), 7, 938_400, "ctrip:first")
    second = hint(date(2026, 8, 16), 6, 910_000, "ctrip:second")
    ctrip_a = calendar(TravelPlatform.CTRIP, (first, second))
    ctrip_b = calendar(TravelPlatform.CTRIP, (second, first))
    fliggy = calendar(
        TravelPlatform.FLIGGY,
        (
            hint(date(2026, 8, 23), 7, 950_000, "fliggy:first"),
            hint(date(2026, 8, 16), 6, 920_000, "fliggy:second"),
        ),
    )

    result_a = FlexibleDateExplorer().explore(
        window,
        (ctrip_a, fliggy),
        now=NOW,
    )
    result_b = FlexibleDateExplorer().explore(
        window,
        (fliggy, ctrip_b),
        now=NOW,
    )
    plan_a = FlexibleQueryPlanBuilder().build(
        window,
        result_a,
        policy(max_total_tasks=45),
    )
    plan_b = FlexibleQueryPlanBuilder().build(
        window,
        result_b,
        policy(max_total_tasks=45),
    )

    assert result_a.candidates == result_b.candidates
    assert plan_a.tasks == plan_b.tasks
    assert plan_a.query_hash == plan_b.query_hash
    assert len(plan_a.query_hash) == 64


def test_complete_calendars_require_every_pair_before_exhaustive_label() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 23),
        latest_departure=date(2026, 8, 24),
        min_nights=5,
        max_nights=6,
        max_pairs=3,
    )
    all_hints = tuple(
        hint(departure, (return_date - departure).days, 900_000, f"{departure}:{return_date}")
        for departure, return_date in window.all_date_pairs()
    )
    complete = tuple(calendar(platform, all_hints, complete=True) for platform in TravelPlatform)
    incomplete = (
        complete[0],
        complete[1],
        calendar(
            TravelPlatform.QUNAR,
            all_hints[:-1],
            complete=True,
        ),
    )

    exhaustive = FlexibleDateExplorer().explore(window, complete, now=NOW)
    degraded = FlexibleDateExplorer().explore(window, incomplete, now=NOW)

    assert exhaustive.mode == DateExplorationMode.FULL_CALENDAR_TOP_K
    assert exhaustive.sampled_not_exhaustive is False
    assert exhaustive.search_metrics.prior_coverage == 1
    assert exhaustive.search_metrics.recall_at_k == 1
    assert exhaustive.search_metrics.price_regret_cents == 0
    assert degraded.mode == DateExplorationMode.SAMPLED_NOT_EXHAUSTIVE
    assert degraded.sampled_not_exhaustive is True


def test_invalid_window_and_too_small_task_budget_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_nights"):
        FlexibleTravelWindow(
            origin="HGH",
            destination="MLE",
            earliest_departure=date(2026, 8, 1),
            latest_departure=date(2026, 8, 31),
            min_nights=8,
            max_nights=5,
        )
    window = august_window(max_pairs=2)
    result = FlexibleDateExplorer().explore(window, now=NOW)
    tiny_policy = QueryPlanPolicy(
        max_total_tasks=14,
        platform_rates=(
            PlatformRatePolicy(platform=TravelPlatform.CTRIP, max_tasks=5),
            PlatformRatePolicy(platform=TravelPlatform.FLIGGY, max_tasks=5),
            PlatformRatePolicy(platform=TravelPlatform.QUNAR, max_tasks=5),
        ),
    )
    with pytest.raises(ValueError, match="too small"):
        FlexibleQueryPlanBuilder().build(window, result, tiny_policy)


def test_exact_pair_budget_prunes_shortlist_without_hiding_coverage() -> None:
    window = august_window(max_pairs=6)
    exploration = FlexibleDateExplorer().explore(window, now=NOW)
    limited = policy(max_total_tasks=150).model_copy(update={"max_exact_pairs": 2})

    plan = FlexibleQueryPlanBuilder().build(window, exploration, limited)

    assert len(exploration.candidates) == 6
    assert len(plan.selected_pair_ids) == 2
    assert len(plan.omitted_pair_ids) == 4
    assert plan.search_metrics.shortlist_pair_count == 6
    assert plan.search_metrics.exact_search_budget_pairs == 2
    assert plan.search_metrics.exact_search_coverage == Decimal(2) / Decimal(124)


def _policy_for_platforms(platforms: tuple[TravelPlatform, ...]) -> QueryPlanPolicy:
    return QueryPlanPolicy(
        include_split_stays=True,
        max_total_tasks=90,
        platform_rates=tuple(
            PlatformRatePolicy(platform=platform, minimum_interval_ms=1_000, max_tasks=30)
            for platform in platforms
        ),
    )


_SYNTHETIC_TASKS_PER_FULL_PLATFORM_PER_PAIR = 5
_TONGCHENG_FLIGHT_ONLY_TASKS_PER_PAIR = 1


def _expected_tasks_per_pair(platforms: tuple[TravelPlatform, ...]) -> int:
    total = 0
    for platform in platforms:
        total += (
            _TONGCHENG_FLIGHT_ONLY_TASKS_PER_PAIR
            if platform is TravelPlatform.TONGCHENG
            else _SYNTHETIC_TASKS_PER_FULL_PLATFORM_PER_PAIR
        )
    return total


@pytest.mark.parametrize(
    "platforms",
    [
        (TravelPlatform.CTRIP,),
        (TravelPlatform.CTRIP, TravelPlatform.QUNAR),
        (TravelPlatform.CTRIP, TravelPlatform.QUNAR, TravelPlatform.TONGCHENG),
        (
            TravelPlatform.CTRIP,
            TravelPlatform.QUNAR,
            TravelPlatform.TONGCHENG,
            TravelPlatform.FLIGGY,
        ),
    ],
)
def test_dynamic_provider_count_builds_correct_task_set(
    platforms: tuple[TravelPlatform, ...],
) -> None:
    """v0.2 exit gate: 1/2/3/4 provider replays build a correct task DAG."""
    window = august_window(max_pairs=2)
    explorer = FlexibleDateExplorer(platforms)
    exploration = explorer.explore(window, now=NOW)
    assert exploration.candidates, "expected at least one date pair candidate"
    plan = FlexibleQueryPlanBuilder(platforms).build(
        window, exploration, _policy_for_platforms(platforms)
    )
    assert plan.tasks
    assert set(plan.task_count_by_platform) == {platform.value for platform in platforms}
    expected_per_pair = _expected_tasks_per_pair(platforms)
    for pair_id in plan.selected_pair_ids:
        pair_tasks = [item for item in plan.tasks if item.date_pair_id == pair_id]
        assert len(pair_tasks) == expected_per_pair
    assert all(item.platform in platforms for item in plan.tasks)


def test_zero_provider_query_plan_refuses_cleanly() -> None:
    """v0.2 exit gate: zero eligible scopes refuse before any task is built."""
    with pytest.raises(ValueError):
        FlexibleDateExplorer(())
    with pytest.raises(ValueError):
        FlexibleQueryPlanBuilder(())


def test_deadline_targets_prioritize_latest_safe_return_in_bounded_sampling() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 20),
        latest_departure=date(2026, 9, 7),
        min_nights=3,
        max_nights=7,
        latest_return_date=date(2026, 9, 10),
        latest_arrival_date=date(2026, 9, 10),
        return_date_targets=(date(2026, 9, 9), date(2026, 9, 10)),
    )

    exploration = FlexibleDateExplorer().explore(window, now=NOW)

    assert exploration.candidates[0].departure_date == date(2026, 9, 3)
    assert exploration.candidates[0].return_date == date(2026, 9, 10)


def test_v4_plan_deduplicates_acquisitions_without_reducing_date_coverage() -> None:
    window = FlexibleTravelWindow(
        origin="HGH",
        destination="MLE",
        earliest_departure=date(2026, 8, 1),
        latest_departure=date(2026, 8, 11),
        min_nights=3,
        max_nights=8,
        max_pairs=66,
        adults=2,
        rooms=1,
    )
    platforms = (TravelPlatform.CTRIP, TravelPlatform.QUNAR, TravelPlatform.TONGCHENG)
    exploration = FlexibleDateExplorer(platforms).explore(window, now=NOW)
    plan = FlexibleQueryPlanBuilder(platforms).build(
        window,
        exploration,
        QueryPlanPolicy(
            max_exact_pairs=66,
            platform_rates=tuple(
                PlatformRatePolicy(platform=item) for item in platforms
            ),
        ),
        stay_plan_candidate_set=system_stay_plan_candidate_set(),
    )
    assert plan.selected_pair_ids and plan.omitted_pair_ids == ()
    assert plan.logical_task_count == 858
    assert plan.unique_acquisition_count == 648
    assert plan.deduplicated_task_count == 210
    assert max(item.scheduled_offset_ms for item in plan.tasks) == 290_000
    assert len({canonical_acquisition_fingerprint(item) for item in plan.tasks}) == 648
