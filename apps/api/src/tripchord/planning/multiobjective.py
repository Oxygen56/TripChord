from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from math import prod

from pydantic import Field

from tripchord.domain.common import DomainModel


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


class ObjectiveWeight(DomainModel):
    name: str = Field(min_length=1)
    direction: ObjectiveDirection = ObjectiveDirection.MAXIMIZE
    weight: Decimal = Field(ge=0, le=1)


class DecisionVector(DomainModel):
    candidate_id: str = Field(min_length=1)
    values: dict[str, Decimal]
    diversity_tags: tuple[str, ...] = ()
    feasible: bool = True


class ParetoRankedCandidate(DomainModel):
    candidate_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    pareto_front: int = Field(ge=1)
    weighted_score: Decimal
    diversity_penalty: Decimal = Field(ge=0)
    normalized_values: dict[str, Decimal]
    dominated_by: tuple[str, ...] = ()
    explanation: str = Field(min_length=1)


class ParetoTopKSelector:
    """Deterministic non-dominated sorting plus diversity-aware weighted ranking."""

    def select(
        self,
        candidates: Sequence[DecisionVector],
        objectives: Sequence[ObjectiveWeight],
        *,
        top_k: int | None = None,
        diversity_strength: Decimal = Decimal("0.08"),
    ) -> tuple[ParetoRankedCandidate, ...]:
        if not candidates:
            return ()
        if top_k is not None and top_k < 1:
            raise ValueError("top_k must be positive")
        declared_names = tuple(objective.name for objective in objectives)
        if not declared_names or len(declared_names) != len(set(declared_names)):
            raise ValueError("objectives must be non-empty and uniquely named")
        active_objectives = tuple(objective for objective in objectives if objective.weight > 0)
        total_weight = sum(
            (objective.weight for objective in active_objectives),
            start=Decimal(0),
        )
        if total_weight <= 0:
            raise ValueError("at least one objective weight must be positive")
        if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
            raise ValueError("candidate ids must be unique")
        for candidate in candidates:
            if set(candidate.values) != set(declared_names):
                raise ValueError("every candidate must define every declared objective")

        # A zero-weight objective is explicitly outside the user's decision
        # function. It must therefore be excluded from Pareto dominance as well
        # as the weighted sum; otherwise an "indifferent" preference can still
        # move a candidate onto another Pareto front.
        active_names = tuple(objective.name for objective in active_objectives)
        directions = {item.name: item.direction for item in active_objectives}
        normalized = self._normalize(candidates, objectives)
        dominators = {
            candidate.candidate_id: tuple(
                sorted(
                    other.candidate_id
                    for other in candidates
                    if other.feasible
                    and (
                        not candidate.feasible
                        or self._dominates(other, candidate, directions)
                    )
                )
            )
            for candidate in candidates
        }
        fronts = self._fronts(candidates, directions)
        front_by_id = {
            candidate_id: front
            for front, candidate_ids in enumerate(fronts, start=1)
            for candidate_id in candidate_ids
        }
        # Treat supplied weights as ratios. Normalizing them makes the fixed
        # diversity penalty comparable across requests and keeps ranking
        # invariant when every user weight is scaled by the same factor.
        weights = {
            item.name: item.weight / total_weight for item in active_objectives
        }
        base_score = {
            candidate.candidate_id: sum(
                (
                    normalized[candidate.candidate_id][name] * weights[name]
                    for name in active_names
                ),
                start=Decimal(0),
            )
            for candidate in candidates
        }
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        remaining = set(by_id)
        selected: list[ParetoRankedCandidate] = []
        limit = min(top_k or len(candidates), len(candidates))
        while remaining and len(selected) < limit:
            scored: list[tuple[int, Decimal, Decimal, str]] = []
            for candidate_id in remaining:
                candidate = by_id[candidate_id]
                shared = max(
                    (
                        self._tag_similarity(
                            candidate.diversity_tags,
                            by_id[item.candidate_id].diversity_tags,
                        )
                        for item in selected
                    ),
                    default=Decimal(0),
                )
                penalty = diversity_strength * shared
                scored.append(
                    (
                        front_by_id[candidate_id],
                        -(base_score[candidate_id] - penalty),
                        penalty,
                        candidate_id,
                    )
                )
            front, neg_score, penalty, candidate_id = min(scored)
            remaining.remove(candidate_id)
            selected.append(
                ParetoRankedCandidate(
                    candidate_id=candidate_id,
                    rank=len(selected) + 1,
                    pareto_front=front,
                    weighted_score=-neg_score,
                    diversity_penalty=penalty,
                    normalized_values=normalized[candidate_id],
                    dominated_by=dominators[candidate_id],
                    explanation=(
                        f"Pareto 第 {front} 层；归一化加权分 {-neg_score:.4f}；"
                        f"多样性扣分 {penalty:.4f}"
                    ),
                )
            )
        return tuple(selected)

    def _normalize(
        self,
        candidates: Sequence[DecisionVector],
        objectives: Sequence[ObjectiveWeight],
    ) -> dict[str, dict[str, Decimal]]:
        result: dict[str, dict[str, Decimal]] = {
            candidate.candidate_id: {} for candidate in candidates
        }
        for objective in objectives:
            values = [candidate.values[objective.name] for candidate in candidates]
            minimum = min(values)
            maximum = max(values)
            span = maximum - minimum
            for candidate in candidates:
                value = candidate.values[objective.name]
                if span == 0:
                    normalized = Decimal(1)
                elif objective.direction == ObjectiveDirection.MAXIMIZE:
                    normalized = (value - minimum) / span
                else:
                    normalized = (maximum - value) / span
                result[candidate.candidate_id][objective.name] = normalized
        return result

    def _fronts(
        self,
        candidates: Sequence[DecisionVector],
        directions: Mapping[str, ObjectiveDirection],
    ) -> tuple[tuple[str, ...], ...]:
        remaining = {candidate.candidate_id: candidate for candidate in candidates}
        fronts: list[tuple[str, ...]] = []
        while remaining:
            front = tuple(
                sorted(
                    candidate_id
                    for candidate_id, candidate in remaining.items()
                    if not any(
                        other.feasible
                        and (
                            not candidate.feasible
                            or self._dominates(other, candidate, directions)
                        )
                        for other_id, other in remaining.items()
                        if other_id != candidate_id
                    )
                )
            )
            if not front:
                raise RuntimeError("Pareto front extraction made no progress")
            fronts.append(front)
            for candidate_id in front:
                remaining.pop(candidate_id)
        return tuple(fronts)

    def _dominates(
        self,
        left: DecisionVector,
        right: DecisionVector,
        directions: Mapping[str, ObjectiveDirection],
    ) -> bool:
        comparisons = tuple(
            (
                left.values[name] >= right.values[name]
                if direction == ObjectiveDirection.MAXIMIZE
                else left.values[name] <= right.values[name]
            )
            for name, direction in directions.items()
        )
        strict = tuple(
            (
                left.values[name] > right.values[name]
                if direction == ObjectiveDirection.MAXIMIZE
                else left.values[name] < right.values[name]
            )
            for name, direction in directions.items()
        )
        return all(comparisons) and any(strict)

    def _tag_similarity(self, left: tuple[str, ...], right: tuple[str, ...]) -> Decimal:
        union = set(left) | set(right)
        if not union:
            return Decimal(0)
        return Decimal(len(set(left) & set(right))) / Decimal(len(union))


class CombinationChoice(DomainModel):
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    cost_cents: int = Field(ge=0)
    utility: Decimal = Field(ge=0)
    excludes: tuple[str, ...] = ()


class PrunedCombination(DomainModel):
    choice_ids: tuple[str, ...] = Field(min_length=1)
    total_cost_cents: int = Field(ge=0)
    total_utility: Decimal = Field(ge=0)


class CombinationPruningResult(DomainModel):
    theoretical_combination_count: int = Field(ge=0)
    evaluated_leaf_count: int = Field(ge=0)
    budget_pruned_branch_count: int = Field(ge=0)
    bound_pruned_branch_count: int = Field(ge=0)
    incompatibility_pruned_branch_count: int = Field(ge=0)
    combinations: tuple[PrunedCombination, ...]


class CombinationPruner:
    """Exact branch-and-bound search for one choice per component group."""

    def prune(
        self,
        groups: Mapping[str, Sequence[CombinationChoice]],
        *,
        budget_cents: int,
        top_k: int,
    ) -> CombinationPruningResult:
        if budget_cents < 0 or top_k < 1:
            raise ValueError("budget must be non-negative and top_k positive")
        ordered_groups = tuple(sorted(groups))
        if any(not groups[group] for group in ordered_groups):
            return CombinationPruningResult(
                theoretical_combination_count=0,
                evaluated_leaf_count=0,
                budget_pruned_branch_count=0,
                bound_pruned_branch_count=0,
                incompatibility_pruned_branch_count=0,
                combinations=(),
            )
        for group in ordered_groups:
            if any(choice.group != group for choice in groups[group]):
                raise ValueError("choice group must match its mapping key")
        choices = {
            group: tuple(
                sorted(groups[group], key=lambda item: (-item.utility, item.cost_cents, item.id))
            )
            for group in ordered_groups
        }
        suffix_min_cost = [0] * (len(ordered_groups) + 1)
        suffix_max_utility = [Decimal(0)] * (len(ordered_groups) + 1)
        for index in range(len(ordered_groups) - 1, -1, -1):
            group = ordered_groups[index]
            suffix_min_cost[index] = suffix_min_cost[index + 1] + min(
                item.cost_cents for item in choices[group]
            )
            suffix_max_utility[index] = suffix_max_utility[index + 1] + max(
                item.utility for item in choices[group]
            )

        results: list[PrunedCombination] = []
        evaluated = budget_pruned = bound_pruned = incompatible_pruned = 0

        def visit(
            index: int,
            selected: tuple[CombinationChoice, ...],
            cost: int,
            utility: Decimal,
        ) -> None:
            nonlocal evaluated, budget_pruned, bound_pruned, incompatible_pruned
            if cost + suffix_min_cost[index] > budget_cents:
                budget_pruned += 1
                return
            if len(results) >= top_k:
                worst = min(result.total_utility for result in results)
                if utility + suffix_max_utility[index] < worst:
                    bound_pruned += 1
                    return
            if index == len(ordered_groups):
                evaluated += 1
                results.append(
                    PrunedCombination(
                        choice_ids=tuple(item.id for item in selected),
                        total_cost_cents=cost,
                        total_utility=utility,
                    )
                )
                results.sort(
                    key=lambda item: (-item.total_utility, item.total_cost_cents, item.choice_ids)
                )
                del results[top_k:]
                return
            for choice in choices[ordered_groups[index]]:
                selected_ids = {item.id for item in selected}
                if selected_ids.intersection(choice.excludes) or any(
                    choice.id in item.excludes for item in selected
                ):
                    incompatible_pruned += 1
                    continue
                visit(
                    index + 1,
                    (*selected, choice),
                    cost + choice.cost_cents,
                    utility + choice.utility,
                )

        visit(0, (), 0, Decimal(0))
        return CombinationPruningResult(
            theoretical_combination_count=prod(len(choices[group]) for group in ordered_groups),
            evaluated_leaf_count=evaluated,
            budget_pruned_branch_count=budget_pruned,
            bound_pruned_branch_count=bound_pruned,
            incompatibility_pruned_branch_count=incompatible_pruned,
            combinations=tuple(results),
        )


__all__ = [
    "CombinationChoice",
    "CombinationPruner",
    "CombinationPruningResult",
    "DecisionVector",
    "ObjectiveDirection",
    "ObjectiveWeight",
    "ParetoRankedCandidate",
    "ParetoTopKSelector",
    "PrunedCombination",
]
