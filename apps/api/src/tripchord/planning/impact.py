from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from itertools import pairwise

from tripchord.domain.common import DomainModel
from tripchord.domain.events import EventKind, PlanEvent
from tripchord.domain.itinerary import PlanVersion
from tripchord.planning.verifier import VerificationContext


class DependencyKind(StrEnum):
    TEMPORAL = "temporal"
    TRAVEL = "travel"
    BOOKING = "booking"


class PlanDependency(DomainModel):
    upstream_item_id: str
    downstream_item_id: str
    kind: DependencyKind


class ImpactScope(DomainModel):
    event_id: str
    direct_item_ids: tuple[str, ...]
    downstream_item_ids: tuple[str, ...]
    affected_item_ids: tuple[str, ...]
    unaffected_item_ids: tuple[str, ...]


def build_plan_dependencies(
    plan: PlanVersion,
    context: VerificationContext | None = None,
) -> tuple[PlanDependency, ...]:
    verification_context = context or VerificationContext()
    explicit = {
        (requirement.from_item_id, requirement.to_item_id): PlanDependency(
            upstream_item_id=requirement.from_item_id,
            downstream_item_id=requirement.to_item_id,
            kind=DependencyKind.TRAVEL,
        )
        for requirement in verification_context.travel_requirements
    }
    ordered = sorted(plan.items, key=lambda item: (item.starts_at, item.id))
    for previous, current in pairwise(ordered):
        if previous.starts_at.date() != current.starts_at.date():
            continue
        explicit.setdefault(
            (previous.id, current.id),
            PlanDependency(
                upstream_item_id=previous.id,
                downstream_item_id=current.id,
                kind=DependencyKind.TEMPORAL,
            ),
        )
    return tuple(
        sorted(
            explicit.values(),
            key=lambda edge: (edge.upstream_item_id, edge.downstream_item_id, edge.kind),
        )
    )


class ImpactAnalyzer:
    def analyze(
        self,
        event: PlanEvent,
        plan: PlanVersion,
        dependencies: tuple[PlanDependency, ...],
    ) -> ImpactScope:
        direct = self._resolve_targets(event, plan)
        adjacency: dict[str, set[str]] = defaultdict(set)
        for edge in dependencies:
            adjacency[edge.upstream_item_id].add(edge.downstream_item_id)

        downstream: set[str] = set()
        queue: deque[str] = deque(direct)
        while queue:
            current = queue.popleft()
            for dependent in adjacency[current]:
                if dependent in direct or dependent in downstream:
                    continue
                downstream.add(dependent)
                queue.append(dependent)

        all_ids = {item.id for item in plan.items}
        affected = direct | downstream
        return ImpactScope(
            event_id=event.id,
            direct_item_ids=tuple(sorted(direct)),
            downstream_item_ids=tuple(sorted(downstream)),
            affected_item_ids=tuple(sorted(affected)),
            unaffected_item_ids=tuple(sorted(all_ids - affected)),
        )

    def _resolve_targets(self, event: PlanEvent, plan: PlanVersion) -> set[str]:
        refs = set(event.target_refs)
        if event.kind == EventKind.USER_CHANGED_REQUIREMENT and not refs:
            return {item.id for item in plan.items}
        return {
            item.id
            for item in plan.items
            if item.id in refs
            or (item.offer_id is not None and item.offer_id in refs)
            or bool(refs.intersection(item.source_refs))
        }
