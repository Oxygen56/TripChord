from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.stay_plans import (
    StayPlanCandidateSet,
    StayPlanId,
)


class TravelPlatform(StrEnum):
    CTRIP = "ctrip"
    FLIGGY = "fliggy"
    QUNAR = "qunar"
    TONGCHENG = "tongcheng"


LEGACY_V4_PLATFORMS: tuple[TravelPlatform, ...] = (
    TravelPlatform.CTRIP,
    TravelPlatform.FLIGGY,
    TravelPlatform.QUNAR,
)
LIVE_V5_PLATFORMS: tuple[TravelPlatform, ...] = (
    TravelPlatform.CTRIP,
    TravelPlatform.QUNAR,
    TravelPlatform.TONGCHENG,
)
EXPECTED_PLATFORMS: tuple[TravelPlatform, ...] = LEGACY_V4_PLATFORMS

# Provider capabilities are explicit rather than inferred from platform count.
# Tongcheng's public international-flight web surface is audited, while its
# overseas-lodging surface is intentionally disabled after repeated account
# security gates. Disabled capabilities are never scheduled or counted as a
# successful search.


class DateExplorationMode(StrEnum):
    FULL_CALENDAR_TOP_K = "full_calendar_top_k"
    FULL_UNIVERSE_NO_COMPLETE_PRIOR = "full_universe_no_complete_prior"
    SAMPLED_NOT_EXHAUSTIVE = "sampled_not_exhaustive"


class DatePairSource(StrEnum):
    FUSED_FARE_HINT = "fused_fare_hint"
    STRATIFIED_SAMPLE = "stratified_sample"


class DateSearchMetricStatus(StrEnum):
    FULL_WINDOW_EVALUABLE = "full_window_evaluable"
    PARTIAL_PRIOR_ONLY = "partial_prior_only"


class QueryTaskKind(StrEnum):
    FLIGHT = "flight"
    LODGING_FULL_STAY = "lodging_full_stay"
    LODGING_FIRST_NIGHT = "lodging_first_night"
    LODGING_MIDDLE_STAY = "lodging_middle_stay"
    LODGING_LAST_NIGHT = "lodging_last_night"
    LODGING_HULHUMALE_FULL_STAY = "lodging_hulhumale_full_stay"


LIVE_V5_PLATFORM_QUERY_KINDS = {
    TravelPlatform.CTRIP: frozenset(QueryTaskKind),
    TravelPlatform.QUNAR: frozenset(QueryTaskKind),
    TravelPlatform.TONGCHENG: frozenset({QueryTaskKind.FLIGHT}),
}


class LodgingZone(StrEnum):
    DESTINATION = "destination"
    AIRPORT_ISLAND = "airport_island"


class FlexibleTravelWindow(DomainModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    origin_code: str | None = None
    destination_code: str | None = None
    earliest_departure: date
    latest_departure: date
    min_nights: int = Field(ge=1, le=60)
    max_nights: int = Field(ge=1, le=60)
    max_pairs: int = Field(default=12, ge=1, le=400)
    adults: int = Field(default=2, ge=1, le=20)
    rooms: int = Field(default=1, ge=1, le=8)
    currency: str = Field(default="CNY", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("origin_code", "destination_code")
    @classmethod
    def normalize_iata_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
            raise ValueError("location codes must be three-letter IATA codes")
        return normalized

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.latest_departure < self.earliest_departure:
            raise ValueError("latest_departure must not be before earliest_departure")
        if self.max_nights < self.min_nights:
            raise ValueError("max_nights must not be less than min_nights")
        if (self.latest_departure - self.earliest_departure).days > 92:
            raise ValueError("flexible departure window cannot exceed 93 calendar days")
        return self

    @property
    def departure_day_count(self) -> int:
        return (self.latest_departure - self.earliest_departure).days + 1

    @property
    def universe_size(self) -> int:
        return self.departure_day_count * (self.max_nights - self.min_nights + 1)

    def all_date_pairs(self) -> tuple[tuple[date, date], ...]:
        return tuple(
            (
                self.earliest_departure + timedelta(days=departure_offset),
                self.earliest_departure + timedelta(days=departure_offset + night_count),
            )
            for departure_offset in range(self.departure_day_count)
            for night_count in range(self.min_nights, self.max_nights + 1)
        )

    def contains(self, departure_date: date, return_date: date) -> bool:
        nights = (return_date - departure_date).days
        return (
            self.earliest_departure <= departure_date <= self.latest_departure
            and self.min_nights <= nights <= self.max_nights
        )


class FareDateHint(DomainModel):
    departure_date: date
    return_date: date
    total_for_party_cents: int = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    evidence_ref: str = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_hint_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.return_date <= self.departure_date:
            raise ValueError("fare hint return date must be after departure date")
        return self

    @property
    def night_count(self) -> int:
        return (self.return_date - self.departure_date).days


class PlatformFareCalendar(DomainModel):
    platform: TravelPlatform
    hints: tuple[FareDateHint, ...]
    complete_for_window: bool = False
    captured_at: datetime
    expires_at: datetime

    @field_validator("captured_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("calendar timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_freshness_window(self) -> Self:
        if self.expires_at <= self.captured_at:
            raise ValueError("calendar expires_at must be after captured_at")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.captured_at <= reference < self.expires_at


class AuditableDatePair(DomainModel):
    id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    departure_date: date
    return_date: date
    night_count: int = Field(ge=1)
    source: DatePairSource
    supporting_platforms: tuple[TravelPlatform, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    consensus_count: int = Field(default=0, ge=0, le=3)
    platform_coverage: Decimal = Field(default=Decimal(0), ge=0, le=1)
    complete_calendar_support: int = Field(default=0, ge=0, le=3)
    best_total_for_party_cents: int | None = Field(default=None, ge=0)
    median_total_for_party_cents: int | None = Field(default=None, ge=0)
    oldest_hint_age_seconds: int | None = Field(default=None, ge=0)
    audit_reason: str = Field(min_length=1)


class DateSearchMetrics(DomainModel):
    universe_size: int = Field(ge=1)
    coarse_window_pair_count: int = Field(ge=1)
    prior_observed_pair_count: int = Field(ge=0)
    prior_coverage: Decimal = Field(ge=0, le=1)
    shortlist_pair_count: int = Field(ge=1)
    shortlist_coverage: Decimal = Field(gt=0, le=1)
    exact_search_budget_pairs: int = Field(default=0, ge=0)
    exact_search_coverage: Decimal = Field(default=Decimal(0), ge=0, le=1)
    recall_at_k: Decimal | None = Field(default=None, ge=0, le=1)
    price_regret_cents: int | None = Field(default=None, ge=0)
    metric_status: DateSearchMetricStatus
    evaluation_note: str = Field(min_length=1)


class DateExplorationResult(DomainModel):
    mode: DateExplorationMode
    sampled_not_exhaustive: bool
    universe_size: int = Field(ge=1)
    candidates: tuple[AuditableDatePair, ...] = Field(min_length=1)
    missing_platforms: tuple[TravelPlatform, ...] = ()
    stale_platforms: tuple[TravelPlatform, ...] = ()
    ignored_hint_count: int = Field(default=0, ge=0)
    search_metrics: DateSearchMetrics
    warnings: tuple[str, ...] = ()


class PlatformRatePolicy(DomainModel):
    platform: TravelPlatform
    minimum_interval_ms: int = Field(default=1_000, ge=0, le=60_000)
    max_tasks: int = Field(default=100, ge=1, le=10_000)


_LIVE_STAY_PLAN_PROVIDER_INTERVAL_FLOOR_MS = 40_000


def effective_platform_interval_ms(
    rate: PlatformRatePolicy,
    *,
    stay_plan_candidate_set: StayPlanCandidateSet | None,
) -> int:
    """Return the one audited provider interval used by planning and execution.

    A live stay-plan query opens several authenticated result surfaces on the
    same provider.  Both the global plan and each serially admitted date pair
    must therefore use the same anti-bot floor; otherwise the execution record
    no longer binds exactly to the frozen query plan even though task IDs match.
    """

    if stay_plan_candidate_set is None:
        return rate.minimum_interval_ms
    return max(
        rate.minimum_interval_ms,
        _LIVE_STAY_PLAN_PROVIDER_INTERVAL_FLOOR_MS,
    )


def _default_rate_policies() -> tuple[PlatformRatePolicy, ...]:
    return tuple(
        PlatformRatePolicy(platform=platform, minimum_interval_ms=1_000, max_tasks=100)
        for platform in EXPECTED_PLATFORMS
    )


class QueryPlanPolicy(DomainModel):
    include_split_stays: bool = True
    max_total_tasks: int = Field(default=150, ge=1, le=10_000)
    max_exact_pairs: int | None = Field(default=None, ge=1, le=100)
    platform_rates: tuple[PlatformRatePolicy, ...] = Field(default_factory=_default_rate_policies)

    @model_validator(mode="after")
    def validate_platforms(self) -> Self:
        platforms = tuple(item.platform for item in self.platform_rates)
        if len(platforms) != len(set(platforms)):
            raise ValueError("platform rate policies must be unique")
        if not platforms:
            raise ValueError("query plans require at least one provider platform")
        return self


class FlexibleQueryTask(DomainModel):
    id: str = Field(min_length=1)
    date_pair_id: str = Field(min_length=1)
    platform: TravelPlatform
    kind: QueryTaskKind
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    start_date: date
    end_date: date
    adults: int = Field(ge=1, le=20)
    rooms: int = Field(ge=1, le=8)
    currency: str = Field(min_length=3, max_length=3)
    lodging_zone: LodgingZone | None = None
    stay_plan_id: StayPlanId | None = None
    scheduled_offset_ms: int = Field(ge=0)


class FlexibleQueryPlan(DomainModel):
    tasks: tuple[FlexibleQueryTask, ...] = Field(min_length=1)
    selected_pair_ids: tuple[str, ...] = Field(min_length=1)
    omitted_pair_ids: tuple[str, ...] = ()
    total_task_count: int = Field(ge=1)
    task_count_by_platform: dict[str, int]
    sampled_not_exhaustive: bool
    search_metrics: DateSearchMetrics
    query_hash: str = Field(min_length=64, max_length=64)
    stay_plan_candidate_set_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    frozen_stay_plan_ids: tuple[StayPlanId, ...] = ()
    warnings: tuple[str, ...] = ()


class FlexibleDateExplorer:
    def __init__(
        self,
        platforms: tuple[TravelPlatform, ...] = EXPECTED_PLATFORMS,
    ) -> None:
        if not platforms or len(set(platforms)) != len(platforms):
            raise ValueError("date explorer requires at least one unique platform")
        self._platforms = platforms

    def explore(
        self,
        window: FlexibleTravelWindow,
        calendars: tuple[PlatformFareCalendar, ...] = (),
        *,
        now: datetime | None = None,
    ) -> DateExplorationResult:
        reference = now or datetime.now(UTC)
        fresh, stale = self._select_calendars(calendars, reference)
        missing = tuple(platform for platform in self._platforms if platform not in fresh)
        universe = window.all_date_pairs()
        universe_set = set(universe)
        platform_hints, ignored = self._normalize_hints(window, fresh)
        full_coverage = self._has_full_coverage(universe_set, fresh, platform_hints)
        ranked_hints = self._rank_hinted_pairs(
            window,
            fresh,
            platform_hints,
            reference,
        )

        if full_coverage:
            selected = ranked_hints[: window.max_pairs]
            mode = DateExplorationMode.FULL_CALENDAR_TOP_K
            sampled = False
        else:
            hinted_quota = (
                min(len(ranked_hints), max(1, window.max_pairs * 2 // 3)) if ranked_hints else 0
            )
            selected = list(ranked_hints[:hinted_quota])
            selected_keys = {
                (candidate.departure_date, candidate.return_date) for candidate in selected
            }
            sample_count = window.max_pairs - len(selected)
            selected.extend(
                self._stratified_samples(
                    window,
                    sample_count,
                    excluded=selected_keys,
                )
            )
            sampled = len(selected) < window.universe_size
            mode = (
                DateExplorationMode.SAMPLED_NOT_EXHAUSTIVE
                if sampled
                else DateExplorationMode.FULL_UNIVERSE_NO_COMPLETE_PRIOR
            )

        ranked = tuple(
            candidate.model_copy(update={"rank": index})
            for index, candidate in enumerate(selected, start=1)
        )
        warnings: list[str] = []
        if sampled:
            warnings.append("候选来自低价提示与分层抽样，未穷举完整月份，不得表述为全月最低价")
        elif not full_coverage:
            warnings.append(
                "已枚举完整日期组合，但粗价先验未覆盖完整三平台；"
                "只有进入精确查询预算的日期对可用于最终报价比较"
            )
        if missing:
            warnings.append("缺少部分平台低价日历；缺失平台仍会在固定日期对阶段执行精确查询")
        if stale:
            warnings.append("过期低价日历已忽略，不参与日期排序")
        if ignored:
            warnings.append("窗口外、币种不符或重复的粗报价提示已确定性忽略")
        observed_pairs = {
            (departure, return_date)
            for _, departure, return_date in platform_hints
        }
        prior_coverage = Decimal(len(observed_pairs)) / Decimal(window.universe_size)
        shortlist_coverage = Decimal(len(ranked)) / Decimal(window.universe_size)
        metrics = DateSearchMetrics(
            universe_size=window.universe_size,
            coarse_window_pair_count=window.universe_size,
            prior_observed_pair_count=len(observed_pairs),
            prior_coverage=prior_coverage,
            shortlist_pair_count=len(ranked),
            shortlist_coverage=shortlist_coverage,
            recall_at_k=Decimal(1) if full_coverage else None,
            price_regret_cents=0 if full_coverage else None,
            metric_status=(
                DateSearchMetricStatus.FULL_WINDOW_EVALUABLE
                if full_coverage
                else DateSearchMetricStatus.PARTIAL_PRIOR_ONLY
            ),
            evaluation_note=(
                "完整三平台日历覆盖下，Recall@K 与价格 regret 可按全窗口先验计算"
                if full_coverage
                else "粗先验未覆盖完整窗口；Recall@K 与价格 regret 必须用冻结真值集离线计算"
            ),
        )
        return DateExplorationResult(
            mode=mode,
            sampled_not_exhaustive=sampled,
            universe_size=window.universe_size,
            candidates=ranked,
            missing_platforms=missing,
            stale_platforms=stale,
            ignored_hint_count=ignored,
            search_metrics=metrics,
            warnings=tuple(warnings),
        )

    def _select_calendars(
        self,
        calendars: tuple[PlatformFareCalendar, ...],
        now: datetime,
    ) -> tuple[
        dict[TravelPlatform, PlatformFareCalendar],
        tuple[TravelPlatform, ...],
    ]:
        by_platform: dict[TravelPlatform, list[PlatformFareCalendar]] = {}
        for calendar in calendars:
            by_platform.setdefault(calendar.platform, []).append(calendar)
        fresh: dict[TravelPlatform, PlatformFareCalendar] = {}
        stale: list[TravelPlatform] = []
        for platform in self._platforms:
            candidates = by_platform.get(platform, [])
            usable = [item for item in candidates if item.is_fresh(now)]
            if usable:
                fresh[platform] = max(
                    usable,
                    key=lambda item: (
                        item.captured_at,
                        len(item.hints),
                        item.complete_for_window,
                    ),
                )
            elif candidates:
                stale.append(platform)
        return fresh, tuple(stale)

    def _normalize_hints(
        self,
        window: FlexibleTravelWindow,
        calendars: dict[TravelPlatform, PlatformFareCalendar],
    ) -> tuple[
        dict[tuple[TravelPlatform, date, date], FareDateHint],
        int,
    ]:
        normalized: dict[tuple[TravelPlatform, date, date], FareDateHint] = {}
        ignored = 0
        for platform in self._platforms:
            calendar = calendars.get(platform)
            if calendar is None:
                continue
            ordered = sorted(
                calendar.hints,
                key=lambda item: (
                    item.departure_date,
                    item.return_date,
                    item.total_for_party_cents,
                    item.evidence_ref,
                ),
            )
            for hint in ordered:
                if (
                    not window.contains(hint.departure_date, hint.return_date)
                    or hint.currency != window.currency
                ):
                    ignored += 1
                    continue
                key = (platform, hint.departure_date, hint.return_date)
                current = normalized.get(key)
                if current is None:
                    normalized[key] = hint
                elif (
                    hint.total_for_party_cents,
                    hint.evidence_ref,
                ) < (
                    current.total_for_party_cents,
                    current.evidence_ref,
                ):
                    normalized[key] = hint
                    ignored += 1
                else:
                    ignored += 1
        return normalized, ignored

    def _has_full_coverage(
        self,
        universe: set[tuple[date, date]],
        calendars: dict[TravelPlatform, PlatformFareCalendar],
        hints: dict[tuple[TravelPlatform, date, date], FareDateHint],
    ) -> bool:
        if set(calendars) != set(self._platforms):
            return False
        for platform in self._platforms:
            calendar = calendars[platform]
            if not calendar.complete_for_window:
                return False
            covered = {
                (departure, return_date)
                for hint_platform, departure, return_date in hints
                if hint_platform == platform
            }
            if not universe <= covered:
                return False
        return True

    def _rank_hinted_pairs(
        self,
        window: FlexibleTravelWindow,
        calendars: dict[TravelPlatform, PlatformFareCalendar],
        hints: dict[tuple[TravelPlatform, date, date], FareDateHint],
        now: datetime,
    ) -> list[AuditableDatePair]:
        grouped: dict[
            tuple[date, date],
            list[tuple[TravelPlatform, FareDateHint, PlatformFareCalendar]],
        ] = {}
        for (platform, departure, return_date), hint in hints.items():
            grouped.setdefault((departure, return_date), []).append(
                (platform, hint, calendars[platform])
            )
        candidates: list[AuditableDatePair] = []
        for (departure, return_date), entries in grouped.items():
            ordered_entries = sorted(entries, key=lambda item: item[0].value)
            prices = sorted(item[1].total_for_party_cents for item in ordered_entries)
            median = self._integer_median(prices)
            platforms = tuple(item[0] for item in ordered_entries)
            complete_support = sum(
                1 for _, _, calendar in ordered_entries if calendar.complete_for_window
            )
            oldest_age = max(
                max(0, int((now - calendar.captured_at).total_seconds()))
                for _, _, calendar in ordered_entries
            )
            evidence = tuple(dict.fromkeys(item[1].evidence_ref for item in ordered_entries))
            candidates.append(
                AuditableDatePair(
                    id=self._pair_id(window, departure, return_date),
                    rank=1,
                    departure_date=departure,
                    return_date=return_date,
                    night_count=(return_date - departure).days,
                    source=DatePairSource.FUSED_FARE_HINT,
                    supporting_platforms=platforms,
                    evidence_refs=evidence,
                    consensus_count=len(platforms),
                    platform_coverage=(Decimal(len(platforms)) / Decimal(len(self._platforms))),
                    complete_calendar_support=complete_support,
                    best_total_for_party_cents=prices[0],
                    median_total_for_party_cents=median,
                    oldest_hint_age_seconds=oldest_age,
                    audit_reason=(
                        f"{len(platforms)}个平台同日期提示；两人价中位数"
                        f"{median}分；完整日历支持{complete_support}"
                    ),
                )
            )
        candidates.sort(
            key=lambda item: (
                -item.consensus_count,
                item.median_total_for_party_cents
                if item.median_total_for_party_cents is not None
                else 10**18,
                -item.complete_calendar_support,
                item.oldest_hint_age_seconds
                if item.oldest_hint_age_seconds is not None
                else 10**18,
                item.departure_date,
                item.night_count,
                item.id,
            )
        )
        return candidates

    def _stratified_samples(
        self,
        window: FlexibleTravelWindow,
        count: int,
        *,
        excluded: set[tuple[date, date]],
    ) -> list[AuditableDatePair]:
        if count <= 0:
            return []
        universe = [pair for pair in window.all_date_pairs() if pair not in excluded]
        if not universe:
            return []
        midpoint_departure = window.earliest_departure + timedelta(
            days=(window.latest_departure - window.earliest_departure).days // 2
        )
        midpoint_nights = (window.min_nights + window.max_nights) // 2
        anchor_pairs = (
            (
                window.earliest_departure,
                window.earliest_departure + timedelta(days=window.min_nights),
            ),
            (
                window.latest_departure,
                window.latest_departure + timedelta(days=window.max_nights),
            ),
            (
                midpoint_departure,
                midpoint_departure + timedelta(days=midpoint_nights),
            ),
            (
                window.earliest_departure,
                window.earliest_departure + timedelta(days=window.max_nights),
            ),
            (
                window.latest_departure,
                window.latest_departure + timedelta(days=window.min_nights),
            ),
        )
        ordered: list[tuple[date, date]] = []
        for pair in anchor_pairs:
            if pair in universe and pair not in ordered:
                ordered.append(pair)
        target_count = min(count, len(universe))
        if target_count > len(ordered):
            slots = max(1, target_count - len(ordered))
            for index in range(slots):
                if slots == 1:
                    position = (len(universe) - 1) // 2
                else:
                    position = round(index * (len(universe) - 1) / (slots - 1))
                pair = universe[position]
                if pair not in ordered:
                    ordered.append(pair)
        if len(ordered) < target_count:
            departure_span = max(1, window.departure_day_count)
            bucketed = sorted(
                universe,
                key=lambda pair: (
                    min(
                        2,
                        ((pair[0] - window.earliest_departure).days * 3 // departure_span),
                    ),
                    (pair[1] - pair[0]).days,
                    pair[0],
                ),
            )
            for pair in bucketed:
                if pair not in ordered:
                    ordered.append(pair)
                if len(ordered) >= target_count:
                    break
        return [
            AuditableDatePair(
                id=self._pair_id(window, departure, return_date),
                rank=1,
                departure_date=departure,
                return_date=return_date,
                night_count=(return_date - departure).days,
                source=DatePairSource.STRATIFIED_SAMPLE,
                audit_reason=("无完整三平台日历覆盖；按出发月早/中/晚与停留时长分层抽样"),
            )
            for departure, return_date in ordered[:target_count]
        ]

    def _integer_median(self, values: list[int]) -> int:
        middle = len(values) // 2
        if len(values) % 2:
            return values[middle]
        return (values[middle - 1] + values[middle]) // 2

    def _pair_id(
        self,
        window: FlexibleTravelWindow,
        departure: date,
        return_date: date,
    ) -> str:
        # C-122 supervision 02:56 (round-19 continuation): the frozen live-v4
        # scenario's date-pair generation MUST route through the canonical
        # ``frozen_v4_pair_id`` helper, which enforces the frozen time contract
        # (2026-08 departure, return > departure, 5-8 nights) BEFORE the digest
        # is computed — a 2030 departure / reversed dates / 1/9/10-night pair
        # raises here at generation time, not only at acceptance.  Function-level
        # import avoids a module cycle: ``frozen_graph`` derives its canonical
        # sets FROM this module.
        from tripchord.planning.frozen_graph import (
            _is_frozen_v4_window,
            frozen_v4_pair_id,
        )

        if _is_frozen_v4_window(window):
            return frozen_v4_pair_id(departure, return_date)
        raw = (
            f"{window.origin}|{window.destination}|"
            f"{departure.isoformat()}|{return_date.isoformat()}|"
            f"{window.adults}|{window.rooms}|{window.currency}"
        )
        digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
        return f"date-pair:{departure.isoformat()}:{return_date.isoformat()}:{digest}"


class FlexibleQueryPlanBuilder:
    def __init__(
        self,
        platforms: tuple[TravelPlatform, ...] = EXPECTED_PLATFORMS,
    ) -> None:
        if not platforms or len(set(platforms)) != len(platforms):
            raise ValueError("query planner requires at least one unique platform")
        self._platforms = platforms

    def build(
        self,
        window: FlexibleTravelWindow,
        exploration: DateExplorationResult,
        policy: QueryPlanPolicy | None = None,
        *,
        stay_plan_candidate_set: StayPlanCandidateSet | None = None,
    ) -> FlexibleQueryPlan:
        effective_policy = policy or QueryPlanPolicy(
            platform_rates=tuple(
                PlatformRatePolicy(platform=platform) for platform in self._platforms
            )
        )
        if {item.platform for item in effective_policy.platform_rates} != set(self._platforms):
            raise ValueError("query policy must match the planner provider profile")
        rates = {item.platform: item for item in effective_policy.platform_rates}
        base_task_windows = self._task_windows(
            exploration.candidates[0],
            effective_policy.include_split_stays,
            stay_plan_candidate_set,
        ) if exploration.candidates else ()
        task_count_by_platform_per_pair = {
            platform: sum(
                1
                for kind, *_ in base_task_windows
                if kind in LIVE_V5_PLATFORM_QUERY_KINDS.get(
                    platform,
                    frozenset(QueryTaskKind),
                )
            )
            for platform in self._platforms
        }
        if stay_plan_candidate_set is not None and not effective_policy.include_split_stays:
            raise ValueError("live-v4 requires the frozen split-stay repair candidate")
        tasks_per_pair = sum(task_count_by_platform_per_pair.values())
        global_capacity = effective_policy.max_total_tasks // tasks_per_pair
        platform_capacity = min(
            rates[platform].max_tasks // task_count_by_platform_per_pair[platform]
            for platform in self._platforms
        )
        pair_capacity = min(global_capacity, platform_capacity)
        if effective_policy.max_exact_pairs is not None:
            pair_capacity = min(pair_capacity, effective_policy.max_exact_pairs)
        if pair_capacity < 1:
            raise ValueError(
                "query task limits are too small for one complete date pair "
                f"across {len(self._platforms)} provider(s)"
            )
        selected = exploration.candidates[:pair_capacity]
        omitted = exploration.candidates[pair_capacity:]
        counters = {platform: 0 for platform in self._platforms}
        tasks: list[FlexibleQueryTask] = []
        for pair in selected:
            for platform in self._platforms:
                for kind, start_date, end_date, zone in self._task_windows(
                    pair,
                    effective_policy.include_split_stays,
                    stay_plan_candidate_set,
                ):
                    if kind not in LIVE_V5_PLATFORM_QUERY_KINDS.get(
                        platform,
                        frozenset(QueryTaskKind),
                    ):
                        continue
                    stay_plan_id = self._stay_plan_id(kind, stay_plan_candidate_set)
                    # Live stay plans keep one provider lane paced while the
                    # provider lanes remain concurrent.  The shared helper is
                    # also used by the runtime's per-pair expansion so these
                    # offsets remain an exact, auditable execution contract.
                    interval_ms = effective_platform_interval_ms(
                        rates[platform],
                        stay_plan_candidate_set=stay_plan_candidate_set,
                    )
                    offset = counters[platform] * interval_ms
                    task = self._task(
                        window,
                        pair,
                        platform,
                        kind,
                        start_date,
                        end_date,
                        zone,
                        stay_plan_id,
                        offset,
                    )
                    tasks.append(task)
                    counters[platform] += 1
        query_hash = self._query_hash(
            window,
            effective_policy,
            tasks,
            stay_plan_candidate_set,
        )
        warnings = list(exploration.warnings)
        if omitted:
            warnings.append("部分日期对因全局任务上限或平台速率配额未进入精确查询计划")
        return FlexibleQueryPlan(
            tasks=tuple(tasks),
            selected_pair_ids=tuple(item.id for item in selected),
            omitted_pair_ids=tuple(item.id for item in omitted),
            total_task_count=len(tasks),
            task_count_by_platform={
                platform.value: counters[platform] for platform in self._platforms
            },
            sampled_not_exhaustive=exploration.sampled_not_exhaustive,
            search_metrics=exploration.search_metrics.model_copy(
                update={
                    "exact_search_budget_pairs": len(selected),
                    "exact_search_coverage": (
                        Decimal(len(selected)) / Decimal(window.universe_size)
                    ),
                }
            ),
            query_hash=query_hash,
            stay_plan_candidate_set_sha256=(
                stay_plan_candidate_set.candidate_set_sha256
                if stay_plan_candidate_set is not None
                else None
            ),
            frozen_stay_plan_ids=(
                stay_plan_candidate_set.stay_plan_ids if stay_plan_candidate_set is not None else ()
            ),
            warnings=tuple(warnings),
        )

    def _task_windows(
        self,
        pair: AuditableDatePair,
        include_split: bool,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> tuple[tuple[QueryTaskKind, date, date, LodgingZone | None], ...]:
        tasks: list[tuple[QueryTaskKind, date, date, LodgingZone | None]] = [
            (
                QueryTaskKind.FLIGHT,
                pair.departure_date,
                pair.return_date,
                None,
            ),
            (
                QueryTaskKind.LODGING_FULL_STAY,
                pair.departure_date,
                pair.return_date,
                LodgingZone.DESTINATION,
            ),
        ]
        if include_split:
            first_checkout = pair.departure_date + timedelta(days=1)
            last_checkin = pair.return_date - timedelta(days=1)
            tasks.extend(
                (
                    (
                        QueryTaskKind.LODGING_FIRST_NIGHT,
                        pair.departure_date,
                        first_checkout,
                        LodgingZone.AIRPORT_ISLAND,
                    ),
                    (
                        QueryTaskKind.LODGING_MIDDLE_STAY,
                        first_checkout,
                        last_checkin,
                        LodgingZone.DESTINATION,
                    ),
                    (
                        QueryTaskKind.LODGING_LAST_NIGHT,
                        last_checkin,
                        pair.return_date,
                        LodgingZone.AIRPORT_ISLAND,
                    ),
                )
            )
        if stay_plan_candidate_set is not None:
            tasks.append(
                (
                    QueryTaskKind.LODGING_HULHUMALE_FULL_STAY,
                    pair.departure_date,
                    pair.return_date,
                    LodgingZone.AIRPORT_ISLAND,
                )
            )
        return tuple(tasks)

    def _stay_plan_id(
        self,
        kind: QueryTaskKind,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> StayPlanId | None:
        if stay_plan_candidate_set is None or kind == QueryTaskKind.FLIGHT:
            return None
        mapping = {
            QueryTaskKind.LODGING_FULL_STAY: StayPlanId.MAAFUSHI_ICOM,
            QueryTaskKind.LODGING_FIRST_NIGHT: StayPlanId.MAAFUSHI_SPLIT_HULHUMALE,
            QueryTaskKind.LODGING_MIDDLE_STAY: StayPlanId.MAAFUSHI_SPLIT_HULHUMALE,
            QueryTaskKind.LODGING_LAST_NIGHT: StayPlanId.MAAFUSHI_SPLIT_HULHUMALE,
            QueryTaskKind.LODGING_HULHUMALE_FULL_STAY: StayPlanId.HULHUMALE_CONTINUOUS,
        }
        stay_plan_id = mapping[kind]
        if stay_plan_id not in stay_plan_candidate_set.stay_plan_ids:
            raise ValueError(f"query task references unfrozen stay plan: {stay_plan_id.value}")
        return stay_plan_id

    def _task(
        self,
        window: FlexibleTravelWindow,
        pair: AuditableDatePair,
        platform: TravelPlatform,
        kind: QueryTaskKind,
        start_date: date,
        end_date: date,
        zone: LodgingZone | None,
        stay_plan_id: StayPlanId | None,
        offset: int,
    ) -> FlexibleQueryTask:
        raw = (
            f"{pair.id}|{platform.value}|{kind.value}|"
            f"{start_date.isoformat()}|{end_date.isoformat()}|{zone or '-'}|"
            f"{stay_plan_id or '-'}"
        )
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return FlexibleQueryTask(
            id=f"query:{platform.value}:{kind.value}:{digest}",
            date_pair_id=pair.id,
            platform=platform,
            kind=kind,
            origin=window.origin,
            destination=window.destination,
            start_date=start_date,
            end_date=end_date,
            adults=window.adults,
            rooms=window.rooms,
            currency=window.currency,
            lodging_zone=zone,
            stay_plan_id=stay_plan_id,
            scheduled_offset_ms=offset,
        )

    def _query_hash(
        self,
        window: FlexibleTravelWindow,
        policy: QueryPlanPolicy,
        tasks: list[FlexibleQueryTask],
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> str:
        canonical = json.dumps(
            {
                "window": window.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in tasks],
                "stay_plan_candidate_set": (
                    stay_plan_candidate_set.model_dump(mode="json")
                    if stay_plan_candidate_set is not None
                    else None
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()
