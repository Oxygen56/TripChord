from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.planning.flexible_dates import AuditableDatePair


class ExactDatePairObservation(DomainModel):
    date_pair_id: str = Field(min_length=1)
    total_budget_cents: int | None = Field(default=None, ge=0)
    recommendable: bool


class AdaptiveRefinementDecision(DomainModel):
    round: int = Field(ge=1)
    selected_pair_id: str | None = None
    remaining_budget_pairs: int = Field(ge=0)
    incumbent_total_cents: int | None = Field(default=None, ge=0)
    priority_score: Decimal | None = None
    stopped_early: bool = False
    reason: str = Field(min_length=1)


class SearchQualityEvaluation(DomainModel):
    universe_size: int = Field(ge=1)
    evaluated_pair_count: int = Field(ge=0)
    coverage: Decimal = Field(ge=0, le=1)
    recall_at_k: Decimal = Field(ge=0, le=1)
    selected_best_cents: int | None = Field(default=None, ge=0)
    oracle_best_cents: int = Field(ge=0)
    price_regret_cents: int | None = Field(default=None, ge=0)


class DatePairRefiner(Protocol):
    def next_pair(
        self,
        candidates: tuple[AuditableDatePair, ...],
        observations: tuple[ExactDatePairObservation, ...],
        *,
        exact_pair_budget: int,
    ) -> AdaptiveRefinementDecision: ...


class RankedTopKDateRefiner:
    """Deterministic bounded acquisition over the already-audited candidate order."""

    policy_id = "query_strategist_ranked_bounded_top_k_v1"

    def next_pair(
        self,
        candidates: tuple[AuditableDatePair, ...],
        observations: tuple[ExactDatePairObservation, ...],
        *,
        exact_pair_budget: int,
    ) -> AdaptiveRefinementDecision:
        if exact_pair_budget < 1:
            raise ValueError("exact_pair_budget must be positive")
        observed = {item.date_pair_id: item for item in observations}
        if len(observed) != len(observations):
            raise ValueError("exact observations must have unique pair ids")
        unknown = set(observed) - {candidate.id for candidate in candidates}
        if unknown:
            raise ValueError(f"observations reference unknown pairs: {sorted(unknown)}")
        remaining_budget = max(0, exact_pair_budget - len(observations))
        incumbent = min(
            (
                item.total_budget_cents
                for item in observations
                if item.recommendable and item.total_budget_cents is not None
            ),
            default=None,
        )
        if remaining_budget == 0:
            return AdaptiveRefinementDecision(
                round=len(observations) + 1,
                remaining_budget_pairs=0,
                incumbent_total_cents=incumbent,
                reason="bounded Top-K 精查预算已耗尽，停止新增平台查询",
            )
        unobserved = tuple(candidate for candidate in candidates if candidate.id not in observed)
        if not unobserved:
            return AdaptiveRefinementDecision(
                round=len(observations) + 1,
                remaining_budget_pairs=remaining_budget,
                incumbent_total_cents=incumbent,
                reason="Query Strategist 排序后的候选已全部精查",
            )
        selected = min(unobserved, key=lambda item: (item.rank, item.id))
        return AdaptiveRefinementDecision(
            round=len(observations) + 1,
            selected_pair_id=selected.id,
            remaining_budget_pairs=remaining_budget - 1,
            incumbent_total_cents=incumbent,
            priority_score=Decimal(1) / Decimal(max(1, selected.rank)),
            reason=(
                "按 Query Strategist 已校验的候选顺序执行 bounded Top-K；"
                "无可用模型时保留确定性粗排顺序。冻结 synthetic 基准仅支持该保守默认，"
                "不证明真实 OTA 上优于实验性 adaptive"
            ),
        )


class AdaptiveDateRefiner:
    """Allocates a bounded exact-search budget one observation at a time."""

    policy_id = "adaptive_experimental_v1"

    def __init__(
        self,
        *,
        minimum_observations_before_stop: int = 3,
        material_improvement_ratio: Decimal = Decimal("0.03"),
        uncertainty_buffer_ratio: Decimal = Decimal("0.10"),
    ) -> None:
        if minimum_observations_before_stop < 2:
            raise ValueError("adaptive early stop requires at least two observations")
        if not Decimal(0) <= material_improvement_ratio <= Decimal(1):
            raise ValueError("material improvement ratio must be between zero and one")
        if not Decimal(0) <= uncertainty_buffer_ratio <= Decimal(1):
            raise ValueError("uncertainty buffer ratio must be between zero and one")
        self._minimum_observations_before_stop = minimum_observations_before_stop
        self._material_improvement_ratio = material_improvement_ratio
        self._uncertainty_buffer_ratio = uncertainty_buffer_ratio

    def next_pair(
        self,
        candidates: tuple[AuditableDatePair, ...],
        observations: tuple[ExactDatePairObservation, ...],
        *,
        exact_pair_budget: int,
    ) -> AdaptiveRefinementDecision:
        if exact_pair_budget < 1:
            raise ValueError("exact_pair_budget must be positive")
        observed = {item.date_pair_id: item for item in observations}
        if len(observed) != len(observations):
            raise ValueError("exact observations must have unique pair ids")
        unknown = set(observed) - {candidate.id for candidate in candidates}
        if unknown:
            raise ValueError(f"observations reference unknown pairs: {sorted(unknown)}")
        remaining_budget = max(0, exact_pair_budget - len(observations))
        incumbent = min(
            (
                item.total_budget_cents
                for item in observations
                if item.recommendable and item.total_budget_cents is not None
            ),
            default=None,
        )
        if remaining_budget == 0:
            return AdaptiveRefinementDecision(
                round=len(observations) + 1,
                remaining_budget_pairs=0,
                incumbent_total_cents=incumbent,
                reason="精查预算已耗尽，停止新增平台查询",
            )
        unobserved = tuple(candidate for candidate in candidates if candidate.id not in observed)
        if not unobserved:
            return AdaptiveRefinementDecision(
                round=len(observations) + 1,
                remaining_budget_pairs=remaining_budget,
                incumbent_total_cents=incumbent,
                reason="粗排 shortlist 已全部精查，无剩余日期对",
            )
        if not observations:
            first = unobserved[0]
            return AdaptiveRefinementDecision(
                round=1,
                selected_pair_id=first.id,
                remaining_budget_pairs=remaining_budget - 1,
                priority_score=Decimal(1),
                reason="首轮利用粗价先验，精查排序第一的日期对",
            )

        observed_candidates = tuple(
            candidate for candidate in candidates if candidate.id in observed
        )
        calibrated = tuple(
            (
                candidate.median_total_for_party_cents,
                observed[candidate.id].total_budget_cents,
            )
            for candidate in observed_candidates
            if candidate.median_total_for_party_cents is not None
            and observed[candidate.id].total_budget_cents is not None
            and observed[candidate.id].recommendable
        )
        if (
            incumbent is not None
            and len(observations) >= self._minimum_observations_before_stop
            and len(calibrated) >= self._minimum_observations_before_stop
            and all(item.median_total_for_party_cents is not None for item in unobserved)
        ):
            residuals = tuple(
                exact - prior
                for prior, exact in calibrated
                if prior is not None and exact is not None
            )
            residual_floor = min(residuals)
            residual_spread = max(residuals) - residual_floor
            base_buffer = max(
                residual_spread,
                int(Decimal(incumbent) * self._uncertainty_buffer_ratio),
            )
            optimistic_totals = tuple(
                max(
                    0,
                    cast_prior
                    + residual_floor
                    - base_buffer
                    - int(
                        Decimal(incumbent)
                        * (Decimal(1) - candidate.platform_coverage)
                        * Decimal("0.20")
                    ),
                )
                for candidate in unobserved
                if (cast_prior := candidate.median_total_for_party_cents) is not None
            )
            material_threshold = int(
                Decimal(incumbent) * (Decimal(1) - self._material_improvement_ratio)
            )
            if optimistic_totals and min(optimistic_totals) >= material_threshold:
                return AdaptiveRefinementDecision(
                    round=len(observations) + 1,
                    remaining_budget_pairs=remaining_budget,
                    incumbent_total_cents=incumbent,
                    stopped_early=True,
                    reason=(
                        "已用已精查日期校准粗价与整包价残差；所有剩余日期的保守"
                        "乐观下界都无法带来至少 "
                        f"{self._material_improvement_ratio * 100:.1f}% 的价格改善，"
                        "提前停止外部查询"
                    ),
                )
        prior_values = tuple(
            candidate.median_total_for_party_cents
            for candidate in candidates
            if candidate.median_total_for_party_cents is not None
        )
        prior_min = min(prior_values, default=0)
        prior_max = max(prior_values, default=prior_min)
        prior_span = max(1, prior_max - prior_min)

        def priority(candidate: AuditableDatePair) -> Decimal:
            if candidate.median_total_for_party_cents is None:
                price_utility = Decimal("0.45")
            else:
                price_utility = Decimal(
                    prior_max - candidate.median_total_for_party_cents
                ) / Decimal(prior_span)
            uncertainty = Decimal(1) - candidate.platform_coverage
            diversity = min(
                (
                    Decimal(
                        abs((candidate.departure_date - item.departure_date).days)
                        + abs(candidate.night_count - item.night_count)
                    )
                    for item in observed_candidates
                ),
                default=Decimal(0),
            )
            diversity = min(Decimal(1), diversity / Decimal(14))
            failure_boost = Decimal("0.2") if not any(
                item.recommendable for item in observations
            ) else Decimal(0)
            improvement = Decimal(0)
            if incumbent is not None and candidate.median_total_for_party_cents is not None:
                improvement = max(
                    Decimal(0),
                    Decimal(incumbent - candidate.median_total_for_party_cents)
                    / Decimal(max(1, incumbent)),
                )
            return (
                Decimal("0.45") * price_utility
                + Decimal("0.25") * uncertainty
                + Decimal("0.20") * diversity
                + Decimal("0.10") * improvement
                + failure_boost
            )

        selected = max(
            unobserved,
            key=lambda item: (priority(item), -item.rank, item.id),
        )
        score = priority(selected)
        return AdaptiveRefinementDecision(
            round=len(observations) + 1,
            selected_pair_id=selected.id,
            remaining_budget_pairs=remaining_budget - 1,
            incumbent_total_cents=incumbent,
            priority_score=score,
            reason=(
                "根据低价潜力、先验不确定性、与已查日期的多样性及 incumbent 改进空间"
                "自适应分配下一次精查"
            ),
        )


def evaluate_search_quality(
    *,
    exact_totals_by_pair: dict[str, int],
    selected_pair_ids: tuple[str, ...],
    relevant_k: int,
) -> SearchQualityEvaluation:
    if not exact_totals_by_pair or relevant_k < 1:
        raise ValueError("ground truth must be non-empty and relevant_k positive")
    unknown = set(selected_pair_ids) - set(exact_totals_by_pair)
    if unknown:
        raise ValueError(f"selected ids missing from ground truth: {sorted(unknown)}")
    ordered = tuple(
        sorted(exact_totals_by_pair, key=lambda item: (exact_totals_by_pair[item], item))
    )
    oracle_relevant = set(ordered[: min(relevant_k, len(ordered))])
    selected = set(selected_pair_ids)
    recall = Decimal(len(selected & oracle_relevant)) / Decimal(len(oracle_relevant))
    oracle_best = exact_totals_by_pair[ordered[0]]
    selected_best = min(
        (exact_totals_by_pair[item] for item in selected_pair_ids),
        default=None,
    )
    return SearchQualityEvaluation(
        universe_size=len(exact_totals_by_pair),
        evaluated_pair_count=len(selected_pair_ids),
        coverage=Decimal(len(selected_pair_ids)) / Decimal(len(exact_totals_by_pair)),
        recall_at_k=recall,
        selected_best_cents=selected_best,
        oracle_best_cents=oracle_best,
        price_regret_cents=(selected_best - oracle_best if selected_best is not None else None),
    )


__all__ = [
    "AdaptiveDateRefiner",
    "AdaptiveRefinementDecision",
    "DatePairRefiner",
    "ExactDatePairObservation",
    "RankedTopKDateRefiner",
    "SearchQualityEvaluation",
    "evaluate_search_quality",
]
