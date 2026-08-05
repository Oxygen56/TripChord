from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from random import Random

from tripchord.agents.flexible_live_system import (
    FlexibleObjectiveWeights,
    FlexiblePackageConstraints,
)
from tripchord.agents.models import PreferenceMode
from tripchord.planning.adaptive_dates import (
    AdaptiveDateRefiner,
    ExactDatePairObservation,
    RankedTopKDateRefiner,
    evaluate_search_quality,
)
from tripchord.planning.flexible_dates import (
    AuditableDatePair,
    DatePairSource,
)
from tripchord.planning.multiobjective import (
    CombinationChoice,
    CombinationPruner,
    DecisionVector,
    ObjectiveDirection,
    ObjectiveWeight,
    ParetoTopKSelector,
)


def pair(
    index: int,
    *,
    prior_cents: int | None,
    coverage: str,
) -> AuditableDatePair:
    departure = date(2026, 8, 1) + timedelta(days=index * 7)
    return AuditableDatePair(
        id=f"pair-{index}",
        rank=index + 1,
        departure_date=departure,
        return_date=departure + timedelta(days=5 + index % 2),
        night_count=5 + index % 2,
        source=DatePairSource.FUSED_FARE_HINT,
        platform_coverage=Decimal(coverage),
        median_total_for_party_cents=prior_cents,
        audit_reason="frozen coarse prior",
    )


def test_pareto_top_k_preserves_tradeoffs_and_exposes_scores() -> None:
    candidates = (
        DecisionVector(
            candidate_id="cheap-split",
            values={
                "price": Decimal(800),
                "evidence": Decimal("0.95"),
                "convenience": Decimal("0.4"),
            },
            diversity_tags=("week-1", "split"),
        ),
        DecisionVector(
            candidate_id="balanced-direct",
            values={
                "price": Decimal(900),
                "evidence": Decimal("0.9"),
                "convenience": Decimal("0.9"),
            },
            diversity_tags=("week-2", "direct"),
        ),
        DecisionVector(
            candidate_id="dominated",
            values={
                "price": Decimal(950),
                "evidence": Decimal("0.8"),
                "convenience": Decimal("0.3"),
            },
            diversity_tags=("week-1", "split"),
        ),
    )
    objectives = (
        ObjectiveWeight(
            name="price",
            direction=ObjectiveDirection.MINIMIZE,
            weight=Decimal("0.55"),
        ),
        ObjectiveWeight(name="evidence", weight=Decimal("0.25")),
        ObjectiveWeight(name="convenience", weight=Decimal("0.20")),
    )

    ranked = ParetoTopKSelector().select(candidates, objectives, top_k=3)

    by_id = {item.candidate_id: item for item in ranked}
    assert by_id["cheap-split"].pareto_front == 1
    assert by_id["balanced-direct"].pareto_front == 1
    assert by_id["dominated"].pareto_front == 2
    assert by_id["dominated"].dominated_by == (
        "balanced-direct",
        "cheap-split",
    )
    assert all(item.explanation.startswith("Pareto") for item in ranked)


def test_pareto_weight_ratios_are_scale_invariant_with_diversity_penalty() -> None:
    candidates = (
        DecisionVector(
            candidate_id="best-x",
            values={"x": Decimal(100), "y": Decimal(0)},
            diversity_tags=("same-week",),
        ),
        DecisionVector(
            candidate_id="balanced-same-week",
            values={"x": Decimal(75), "y": Decimal(20)},
            diversity_tags=("same-week",),
        ),
        DecisionVector(
            candidate_id="best-y-other-week",
            values={"x": Decimal(0), "y": Decimal(100)},
            diversity_tags=("other-week",),
        ),
    )
    full_scale = (
        ObjectiveWeight(name="x", weight=Decimal("0.6")),
        ObjectiveWeight(name="y", weight=Decimal("0.4")),
    )
    tenth_scale = (
        ObjectiveWeight(name="x", weight=Decimal("0.06")),
        ObjectiveWeight(name="y", weight=Decimal("0.04")),
    )

    full_ranked = ParetoTopKSelector().select(candidates, full_scale)
    scaled_ranked = ParetoTopKSelector().select(candidates, tenth_scale)

    assert [item.candidate_id for item in full_ranked] == [
        item.candidate_id for item in scaled_ranked
    ]
    assert [item.weighted_score for item in full_ranked] == [
        item.weighted_score for item in scaled_ranked
    ]
    assert full_ranked[1].diversity_penalty > 0


def test_zero_weight_objective_neither_rewards_nor_changes_pareto_front() -> None:
    candidates = (
        DecisionVector(
            candidate_id="cheap-room-only",
            values={"price": Decimal(800), "breakfast": Decimal(0)},
        ),
        DecisionVector(
            candidate_id="expensive-breakfast",
            values={"price": Decimal(900), "breakfast": Decimal(1)},
        ),
    )
    objectives = (
        ObjectiveWeight(
            name="price",
            direction=ObjectiveDirection.MINIMIZE,
            weight=Decimal(1),
        ),
        ObjectiveWeight(name="breakfast", weight=Decimal(0)),
    )

    ranked = ParetoTopKSelector().select(candidates, objectives)

    assert [item.candidate_id for item in ranked] == [
        "cheap-room-only",
        "expensive-breakfast",
    ]
    assert ranked[1].pareto_front == 2
    assert ranked[1].dominated_by == ("cheap-room-only",)


def test_flexible_preference_modes_resolve_to_the_user_weight_contract() -> None:
    indifferent = FlexiblePackageConstraints(
        require_checked_baggage=False,
        require_breakfast=None,
        breakfast_preference_mode=PreferenceMode.INDIFFERENT,
        breakfast_preference_weight=0,
    )
    weighted = FlexiblePackageConstraints(
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.87,
    )
    required = FlexiblePackageConstraints(
        require_checked_baggage=True,
        require_breakfast=True,
        breakfast_preference_mode=PreferenceMode.REQUIRED,
        breakfast_preference_weight=1,
    )

    indifferent_specs = {item.name: item for item in indifferent.objective_specs()}
    weighted_specs = {item.name: item for item in weighted.objective_specs()}
    required_specs = {item.name: item for item in required.objective_specs()}

    assert indifferent_specs["breakfast"].weight == 0
    assert indifferent_specs["baggage"].weight == 0
    assert weighted_specs["breakfast"].weight == Decimal("0.87")
    assert required_specs["breakfast"].weight == 1
    assert required_specs["baggage"].weight == 1


def test_explicit_high_breakfast_weight_changes_a_real_tradeoff() -> None:
    configured = FlexibleObjectiveWeights(
        price=Decimal("0.45"),
        evidence=Decimal("0.20"),
        robustness=Decimal("0.15"),
        convenience=Decimal("0.10"),
        schedule_quality=Decimal("0.05"),
        breakfast=Decimal("0.025"),
        baggage=Decimal("0.025"),
    )
    indifferent = FlexiblePackageConstraints(objective_weights=configured)
    breakfast_matters = FlexiblePackageConstraints(
        breakfast_preference_mode=PreferenceMode.WEIGHTED,
        breakfast_preference_weight=0.9,
        objective_weights=configured,
    )
    candidates = (
        DecisionVector(
            candidate_id="cheap-room-only",
            values={
                "price": Decimal(800),
                "evidence": Decimal(1),
                "robustness": Decimal(1),
                "convenience": Decimal(1),
                "schedule_quality": Decimal(1),
                "breakfast": Decimal(0),
                "baggage": Decimal(0),
            },
        ),
        DecisionVector(
            candidate_id="costlier-with-breakfast",
            values={
                "price": Decimal(900),
                "evidence": Decimal(1),
                "robustness": Decimal(1),
                "convenience": Decimal(1),
                "schedule_quality": Decimal(1),
                "breakfast": Decimal(1),
                "baggage": Decimal(0),
            },
        ),
    )

    indifferent_ranked = ParetoTopKSelector().select(
        candidates,
        indifferent.objective_specs(),
    )
    weighted_ranked = ParetoTopKSelector().select(
        candidates,
        breakfast_matters.objective_specs(),
    )

    assert indifferent_ranked[0].candidate_id == "cheap-room-only"
    assert weighted_ranked[0].candidate_id == "costlier-with-breakfast"


def test_combination_pruner_matches_exact_top_k_without_enumerating_all_leaves() -> None:
    groups = {
        "flight": tuple(
            CombinationChoice(
                id=f"flight-{index}",
                group="flight",
                cost_cents=100_000 + index * 20_000,
                utility=Decimal(100 - index),
            )
            for index in range(6)
        ),
        "hotel": tuple(
            CombinationChoice(
                id=f"hotel-{index}",
                group="hotel",
                cost_cents=80_000 + index * 10_000,
                utility=Decimal(60 - index),
            )
            for index in range(6)
        ),
        "transfer": tuple(
            CombinationChoice(
                id=f"transfer-{index}",
                group="transfer",
                cost_cents=10_000 + index * 5_000,
                utility=Decimal(20 - index),
            )
            for index in range(6)
        ),
    }

    result = CombinationPruner().prune(groups, budget_cents=230_000, top_k=3)

    assert result.theoretical_combination_count == 216
    assert result.evaluated_leaf_count < result.theoretical_combination_count
    assert result.budget_pruned_branch_count + result.bound_pruned_branch_count > 0
    assert result.combinations[0].choice_ids == (
        "flight-0",
        "hotel-0",
        "transfer-0",
    )
    assert len(result.combinations) == 3


def test_adaptive_refiner_uses_prior_then_reallocates_budget_after_failure() -> None:
    candidates = (
        pair(0, prior_cents=800_000, coverage="1"),
        pair(1, prior_cents=820_000, coverage="0.33"),
        pair(2, prior_cents=900_000, coverage="0"),
    )
    refiner = AdaptiveDateRefiner()

    first = refiner.next_pair(candidates, (), exact_pair_budget=2)
    second = refiner.next_pair(
        candidates,
        (
            ExactDatePairObservation(
                date_pair_id="pair-0",
                recommendable=False,
            ),
        ),
        exact_pair_budget=2,
    )

    assert first.selected_pair_id == "pair-0"
    assert second.selected_pair_id in {"pair-1", "pair-2"}
    assert second.selected_pair_id != first.selected_pair_id
    assert second.remaining_budget_pairs == 0
    assert "自适应" in second.reason


def test_ranked_top_k_refiner_follows_audited_order_and_hard_budget() -> None:
    candidates = (
        pair(0, prior_cents=800_000, coverage="1").model_copy(update={"rank": 2}),
        pair(1, prior_cents=820_000, coverage="0.33").model_copy(update={"rank": 1}),
        pair(2, prior_cents=900_000, coverage="0").model_copy(update={"rank": 3}),
    )
    refiner = RankedTopKDateRefiner()
    first = refiner.next_pair(candidates, (), exact_pair_budget=2)
    observation = ExactDatePairObservation(
        date_pair_id=first.selected_pair_id or "missing",
        total_budget_cents=910_000,
        recommendable=True,
    )
    second = refiner.next_pair(candidates, (observation,), exact_pair_budget=2)
    stopped = refiner.next_pair(
        candidates,
        (
            observation,
            ExactDatePairObservation(
                date_pair_id=second.selected_pair_id or "missing",
                total_budget_cents=900_000,
                recommendable=True,
            ),
        ),
        exact_pair_budget=2,
    )

    assert first.selected_pair_id == "pair-1"
    assert second.selected_pair_id == "pair-0"
    assert stopped.selected_pair_id is None
    assert stopped.remaining_budget_pairs == 0
    assert "Query Strategist" in first.reason


def test_adaptive_refiner_stops_when_calibrated_optimistic_bound_cannot_improve() -> None:
    candidates = (
        pair(0, prior_cents=800_000, coverage="1"),
        pair(1, prior_cents=820_000, coverage="1"),
        pair(2, prior_cents=840_000, coverage="1"),
        pair(3, prior_cents=900_000, coverage="1"),
    )
    observations = (
        ExactDatePairObservation(
            date_pair_id="pair-0",
            total_budget_cents=1_000_000,
            recommendable=True,
        ),
        ExactDatePairObservation(
            date_pair_id="pair-1",
            total_budget_cents=1_020_000,
            recommendable=True,
        ),
        ExactDatePairObservation(
            date_pair_id="pair-2",
            total_budget_cents=1_040_000,
            recommendable=True,
        ),
    )

    decision = AdaptiveDateRefiner().next_pair(
        candidates,
        observations,
        exact_pair_budget=4,
    )

    assert decision.selected_pair_id is None
    assert decision.stopped_early
    assert decision.remaining_budget_pairs == 1
    assert "提前停止" in decision.reason


def test_search_quality_reports_coverage_recall_and_price_regret() -> None:
    evaluation = evaluate_search_quality(
        exact_totals_by_pair={
            "pair-a": 800_000,
            "pair-b": 820_000,
            "pair-c": 900_000,
            "pair-d": 950_000,
        },
        selected_pair_ids=("pair-b", "pair-d"),
        relevant_k=2,
    )

    assert evaluation.coverage == Decimal("0.5")
    assert evaluation.recall_at_k == Decimal("0.5")
    assert evaluation.oracle_best_cents == 800_000
    assert evaluation.selected_best_cents == 820_000
    assert evaluation.price_regret_cents == 20_000


def test_combination_pruning_property_matches_bruteforce_for_seeded_inventories() -> None:
    for seed in range(12):
        random = Random(seed)
        groups = {
            group: tuple(
                CombinationChoice(
                    id=f"{group}-{index}",
                    group=group,
                    cost_cents=random.randint(10, 90),
                    utility=Decimal(random.randint(1, 100)),
                )
                for index in range(4)
            )
            for group in ("flight", "hotel", "transfer")
        }
        budget = 160
        expected = sorted(
            (
                (
                    sum(choice.utility for choice in combination),
                    sum(choice.cost_cents for choice in combination),
                    tuple(choice.id for choice in combination),
                )
                for combination in product(*(groups[group] for group in sorted(groups)))
                if sum(choice.cost_cents for choice in combination) <= budget
            ),
            key=lambda item: (-item[0], item[1], item[2]),
        )[:3]

        actual = CombinationPruner().prune(groups, budget_cents=budget, top_k=3)

        assert [
            (item.total_utility, item.total_cost_cents, item.choice_ids)
            for item in actual.combinations
        ] == expected
