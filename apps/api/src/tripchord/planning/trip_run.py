"""Versioned state and scoped replanning for complex TripChord itineraries."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections import defaultdict, deque
from datetime import UTC, date, datetime, time
from enum import StrEnum
from itertools import pairwise
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.complex_trip import (
    ComplexCatalogSolver,
    OfferCatalog,
    PlanComponent,
    PlanGraph,
    PlanningCompiler,
    PlanRepresentativeKind,
    PlanStatus,
    PriceContract,
    SourceState,
    SourceStatus,
    StayRequirement,
    Traveler,
    TravelerGroup,
    TravelIntent,
    TripCardProjection,
    TripCardStatus,
    current_complex_plan_execution_ready,
    project_trip_card,
    validate_plan_graph,
)


class TripChangeKind(StrEnum):
    NATURAL_LANGUAGE = "natural_language"
    STAY_UNAVAILABLE = "stay_unavailable"
    PRICE_CHANGED = "price_changed"
    TRANSPORT_SCHEDULE_CHANGED = "transport_schedule_changed"
    TRAVELER_WITHDRAWN = "traveler_withdrawn"


class TripChangeStatus(StrEnum):
    APPLIED = "applied"
    NO_EFFECT = "no_effect"
    NEEDS_SCOPE_EXPANSION = "needs_scope_expansion"
    CLARIFICATION_REQUIRED = "clarification_required"


class ComplexDependencyKind(StrEnum):
    TEMPORAL = "temporal"
    LOCATION = "location"
    TRAVELER = "traveler"
    LODGING = "lodging"
    FIXED_ACTIVITY = "fixed_activity"
    PRICE_CONTRACT = "price_contract"


class ComplexPlanDependency(DomainModel):
    upstream_component_id: str
    downstream_component_id: str
    kind: ComplexDependencyKind
    participant_ids: tuple[str, ...] = ()


class ComplexImpactScope(DomainModel):
    direct_component_ids: tuple[str, ...] = ()
    affected_component_ids: tuple[str, ...] = ()
    unaffected_component_ids: tuple[str, ...] = ()
    traversed_dependency_kinds: tuple[ComplexDependencyKind, ...] = ()


class TripComponentReference(DomainModel):
    component_id: str
    offer_id: str
    name: str
    kind: str


class TripComponentReplacement(DomainModel):
    before: TripComponentReference
    after: TripComponentReference


class TravelerPlanDiff(DomainModel):
    traveler_id: str
    traveler_name: str
    status: str = "kept"
    kept_component_ids: tuple[str, ...] = ()
    replaced_component_ids: tuple[str, ...] = ()
    added_component_ids: tuple[str, ...] = ()
    removed_component_ids: tuple[str, ...] = ()


class TripPlanDiff(DomainModel):
    kept: tuple[TripComponentReference, ...] = ()
    replaced: tuple[TripComponentReplacement, ...] = ()
    added: tuple[TripComponentReference, ...] = ()
    removed: tuple[TripComponentReference, ...] = ()
    traveler_changes: tuple[TravelerPlanDiff, ...] = ()
    before_total_cny_cents: int | None = None
    after_total_cny_cents: int | None = None
    delta_cny_cents: int | None = None
    query_scope: tuple[str, ...] = ()
    external_query_count: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class ComplexPlanVersion(DomainModel):
    id: str
    run_id: str
    version: int = Field(ge=1)
    parent_version_id: str | None = None
    intent: TravelIntent
    selected_plan_graph: PlanGraph | None = None
    selected_trip_card: TripCardProjection | None = None
    candidate_plan_graphs: tuple[PlanGraph, ...] = ()
    candidate_trip_cards: tuple[TripCardProjection, ...] = ()
    graph_version: str = Field(pattern=r"^graph:[0-9a-f]{64}$")
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    offer_catalog: OfferCatalog
    source_contracts: tuple[PriceContract, ...]
    dependencies: tuple[ComplexPlanDependency, ...] = ()
    created_reason: str
    created_at: datetime
    diff_from_parent: TripPlanDiff | None = None


class TripChangeRecord(DomainModel):
    id: str
    kind: TripChangeKind
    status: TripChangeStatus
    request_text: str | None = None
    target_refs: tuple[str, ...] = ()
    message: str
    impact: ComplexImpactScope
    query_scope: tuple[str, ...] = ()
    external_query_count: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    created_at: datetime
    resulting_plan_version_id: str | None = None
    needs_scope_expansion: tuple[str, ...] = ()


class TripRun(DomainModel):
    id: str
    source_job_id: str | None = None
    original_intent: TravelIntent
    initial_catalog: OfferCatalog
    initial_price_contracts: tuple[PriceContract, ...]
    active_plan_version_id: str
    plan_versions: tuple[ComplexPlanVersion, ...]
    change_history: tuple[TripChangeRecord, ...] = ()
    initial_external_query_count: int = Field(default=0, ge=0)
    initial_planning_elapsed_ms: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    boundary: str = (
        "TripRun是复杂行程的唯一可变真相；历史PlanVersion不可覆盖，"
        "任何修改都必须从当前版本产生新版本或诚实保留原版本。"
    )

    @model_validator(mode="after")
    def active_version_exists(self) -> TripRun:
        ids = [item.id for item in self.plan_versions]
        if len(ids) != len(set(ids)):
            raise ValueError("TripRun plan version ids must be unique")
        if self.active_plan_version_id not in ids:
            raise ValueError("TripRun active plan version must exist")
        versions = [item.version for item in self.plan_versions]
        if versions != list(range(1, len(versions) + 1)):
            raise ValueError("TripRun plan versions must be sequential")
        return self

    def active_version(self) -> ComplexPlanVersion:
        return next(
            item for item in self.plan_versions if item.id == self.active_plan_version_id
        )


class NaturalLanguageTripModification(DomainModel):
    text: str = Field(min_length=1, max_length=1200)


class StructuredTripChangeEvent(DomainModel):
    id: str = Field(default_factory=lambda: f"trip-event-{uuid4()}")
    kind: TripChangeKind
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    target_offer_id: str | None = None
    traveler_id: str | None = None
    new_total_cny_cents: int | None = Field(default=None, ge=0)
    new_departure: datetime | None = None
    new_arrival: datetime | None = None
    source_ref: str | None = None


class TripRunMutationResult(DomainModel):
    status: TripChangeStatus
    trip_run: TripRun
    active_plan_version: ComplexPlanVersion
    diff: TripPlanDiff | None = None
    message: str
    needs_scope_expansion: tuple[str, ...] = ()
    external_query_count: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class ScopedStayOfferProvider(Protocol):
    def catalog_for_stays(
        self,
        intent: TravelIntent,
    ) -> (
        tuple[OfferCatalog, tuple[PriceContract, ...]]
        | object
    ):
        """Return only lodging observations for the supplied stay requirements."""


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _catalog_digest(catalog: OfferCatalog, contracts: tuple[PriceContract, ...]) -> str:
    return _canonical_digest(
        {
            "catalog": catalog.model_dump(mode="json"),
            "contracts": [item.model_dump(mode="json") for item in contracts],
        }
    )


def _next_graph_version(
    previous: str,
    intent: TravelIntent,
    catalog: OfferCatalog,
    graph: PlanGraph,
) -> str:
    return "graph:" + _canonical_digest(
        {
            "previous": previous,
            "intent": intent.model_dump(mode="json"),
            "catalog": catalog.model_dump(mode="json"),
            "graph": graph.model_dump(mode="json"),
        }
    )


def build_complex_plan_dependencies(
    intent: TravelIntent,
    graph: PlanGraph,
) -> tuple[ComplexPlanDependency, ...]:
    """Compile dependency views from the selected plan, never from prose."""

    components = _dependency_components(intent, graph)
    edges: dict[tuple[str, str, ComplexDependencyKind], ComplexPlanDependency] = {}
    by_traveler: dict[str, list[PlanComponent]] = defaultdict(list)
    for component in components:
        # Older aggregate-count inputs deliberately leave participant_ids empty.
        # Project that omission to the same anonymous traveler scope used by
        # the deterministic solver; otherwise the compatibility path silently
        # produces an empty dependency graph and cannot propagate a change.
        for traveler_id in _component_participant_scope(component, intent):
            by_traveler[traveler_id].append(component)
    for traveler_id, scoped in by_traveler.items():
        ordered = sorted(scoped, key=_component_time_key)
        for before, after in pairwise(ordered):
            for kind in (
                ComplexDependencyKind.TEMPORAL,
                ComplexDependencyKind.LOCATION,
                ComplexDependencyKind.TRAVELER,
            ):
                key = (_stable_component_id(before), _stable_component_id(after), kind)
                edges[key] = ComplexPlanDependency(
                    upstream_component_id=_stable_component_id(before),
                    downstream_component_id=_stable_component_id(after),
                    kind=kind,
                    participant_ids=(traveler_id,),
                )
    by_contract: dict[str, list[PlanComponent]] = defaultdict(list)
    for component in components:
        if component.price_contract_id != "user-provided-not-priced":
            by_contract[component.price_contract_id].append(component)
    for scoped in by_contract.values():
        for before in scoped:
            for after in scoped:
                if _stable_component_id(before) == _stable_component_id(after):
                    continue
                key = (
                    _stable_component_id(before),
                    _stable_component_id(after),
                    ComplexDependencyKind.PRICE_CONTRACT,
                )
                edges[key] = ComplexPlanDependency(
                    upstream_component_id=_stable_component_id(before),
                    downstream_component_id=_stable_component_id(after),
                    kind=ComplexDependencyKind.PRICE_CONTRACT,
                    participant_ids=tuple(
                        sorted(
                            set(_component_participant_scope(before, intent))
                            | set(_component_participant_scope(after, intent))
                        )
                    ),
                )
    stays = tuple(item for item in components if item.kind == "stay")
    anchors = tuple(item for item in components if item.kind == "anchor")
    for stay in stays:
        for anchor in anchors:
            stay_scope = set(_component_participant_scope(stay, intent))
            anchor_scope = set(_component_participant_scope(anchor, intent))
            if not stay_scope.intersection(anchor_scope):
                continue
            if _component_covers(stay, anchor):
                for kind in (
                    ComplexDependencyKind.LODGING,
                    ComplexDependencyKind.FIXED_ACTIVITY,
                ):
                    key = (
                        _stable_component_id(stay),
                        _stable_component_id(anchor),
                        kind,
                    )
                    edges[key] = ComplexPlanDependency(
                        upstream_component_id=_stable_component_id(stay),
                        downstream_component_id=_stable_component_id(anchor),
                        kind=kind,
                        participant_ids=tuple(
                            sorted(
                                stay_scope.intersection(anchor_scope)
                            )
                        ),
                    )
    return tuple(
        sorted(
            edges.values(),
            key=lambda item: (
                item.upstream_component_id,
                item.downstream_component_id,
                item.kind.value,
            ),
        )
    )


def _dependency_components(
    intent: TravelIntent,
    graph: PlanGraph,
) -> tuple[PlanComponent, ...]:
    """Return selected components plus explicit anchor nodes for dependency use.

    Some older projections keep user-owned activities only on the trip-card
    projection and omit them from ``PlanGraph.components``.  The dependency
    graph still needs a typed anchor node so a lodging or schedule change can
    propagate through the fixed activity.  These synthetic nodes are internal
    dependency vertices only; they are never published as selected offers.
    """

    components = list(graph.components)
    existing_ids = {_stable_component_id(item) for item in components}
    for anchor in intent.anchors:
        if anchor.id in existing_ids:
            continue
        components.append(
            PlanComponent(
                component_id=anchor.id,
                kind="anchor",
                offer_id=anchor.id,
                label=anchor.name,
                provider="user-provided",
                start=anchor.start,
                end=anchor.end,
                place_from=anchor.place_id,
                price_contract_id=(
                    f"user-activity:{anchor.id}"
                    if anchor.provided_price_cny_cents is not None
                    else "user-provided-not-priced"
                ),
                detail_url="",
                price_cny_cents=anchor.provided_price_cny_cents,
                participant_ids=anchor.participant_ids,
            )
        )
    return tuple(components)


def _component_participant_scope(
    component: PlanComponent,
    intent: TravelIntent,
) -> tuple[str, ...]:
    if component.participant_ids:
        return component.participant_ids
    if intent.traveler_profiles:
        return tuple(item.id for item in intent.traveler_profiles)
    return tuple(f"traveler:{index + 1}" for index in range(intent.travelers))


def build_initial_trip_run(
    *,
    run_id: str | None,
    source_job_id: str | None,
    intent: TravelIntent,
    catalog: OfferCatalog,
    source_contracts: tuple[PriceContract, ...],
    plan_graphs: tuple[PlanGraph, ...],
    trip_cards: tuple[TripCardProjection, ...],
    graph_version: str,
    catalog_digest: str,
    initial_planning_elapsed_ms: int,
    created_at: datetime | None = None,
) -> TripRun:
    resolved_id = run_id or f"trip-run-{uuid4()}"
    now = created_at or datetime.now(UTC)
    selected_graph = plan_graphs[0] if len(plan_graphs) == 1 else None
    selected_card = trip_cards[0] if len(trip_cards) == 1 else None
    version = ComplexPlanVersion(
        id=f"{resolved_id}:plan:v1",
        run_id=resolved_id,
        version=1,
        intent=intent,
        selected_plan_graph=selected_graph,
        selected_trip_card=selected_card,
        candidate_plan_graphs=plan_graphs,
        candidate_trip_cards=trip_cards,
        graph_version=graph_version,
        catalog_digest=catalog_digest,
        offer_catalog=catalog,
        source_contracts=source_contracts,
        dependencies=(
            build_complex_plan_dependencies(intent, selected_graph)
            if selected_graph is not None
            else ()
        ),
        created_reason="initial_plan",
        created_at=now,
    )
    return TripRun(
        id=resolved_id,
        source_job_id=source_job_id,
        original_intent=intent,
        initial_catalog=catalog,
        initial_price_contracts=source_contracts,
        active_plan_version_id=version.id,
        plan_versions=(version,),
        initial_external_query_count=len(catalog.query_tasks),
        initial_planning_elapsed_ms=initial_planning_elapsed_ms,
        created_at=now,
        updated_at=now,
    )


class ComplexTripRunReplanner:
    """Apply bounded changes while preserving the active version on failure."""

    async def modify(
        self,
        trip_run: TripRun,
        request: NaturalLanguageTripModification,
        *,
        provider: object,
    ) -> TripRunMutationResult:
        started = perf_counter()
        active = trip_run.active_version()
        if active.selected_plan_graph is None or active.selected_trip_card is None:
            return self._blocked(
                trip_run,
                TripChangeKind.NATURAL_LANGUAGE,
                request.text,
                "请先在代表方案中选定一个方案后再修改。",
                TripChangeStatus.CLARIFICATION_REQUIRED,
                elapsed_ms=_elapsed_ms(started),
            )
        traveler_name = _withdrawn_traveler_name(request.text)
        if traveler_name is not None:
            return await self._withdraw_traveler(
                trip_run,
                traveler_name,
                request_text=request.text,
                provider=provider,
                started=started,
            )
        stay_target = _explicit_stay_replacement_target(active, request.text)
        if stay_target is not None:
            component, requirement = stay_target
            return await self._replace_stays(
                trip_run,
                affected_requirements=(requirement,),
                target_offer_ids=(component.offer_id,),
                request_text=request.text,
                kind=TripChangeKind.NATURAL_LANGUAGE,
                provider=provider,
                started=started,
                created_reason="natural_language_stay_replacement",
            )
        return self._blocked(
            trip_run,
            TripChangeKind.NATURAL_LANGUAGE,
            request.text,
            "修改范围不够明确；未使用模型猜测要修改的组件。",
            TripChangeStatus.CLARIFICATION_REQUIRED,
            elapsed_ms=_elapsed_ms(started),
        )

    async def apply_event(
        self,
        trip_run: TripRun,
        event: StructuredTripChangeEvent,
        *,
        provider: object,
    ) -> TripRunMutationResult:
        started = perf_counter()
        active = trip_run.active_version()
        if active.selected_plan_graph is None or active.selected_trip_card is None:
            return self._blocked(
                trip_run,
                event.kind,
                None,
                "当前还没有选定可修改方案。",
                TripChangeStatus.CLARIFICATION_REQUIRED,
                target_refs=tuple(item for item in (event.target_offer_id,) if item),
                elapsed_ms=_elapsed_ms(started),
                record_id=event.id,
            )
        if event.kind == TripChangeKind.STAY_UNAVAILABLE:
            target = _selected_component(active, event.target_offer_id, kind="stay")
            requirement = _stay_requirement_for_component(active.intent, target)
            if target is None or requirement is None:
                return self._blocked(
                    trip_run,
                    event.kind,
                    None,
                    "变化事件没有命中当前住宿组件。",
                    TripChangeStatus.NO_EFFECT,
                    target_refs=tuple(item for item in (event.target_offer_id,) if item),
                    elapsed_ms=_elapsed_ms(started),
                    record_id=event.id,
                )
            return await self._replace_stays(
                trip_run,
                affected_requirements=(requirement,),
                target_offer_ids=(target.offer_id,),
                request_text=None,
                kind=event.kind,
                provider=provider,
                started=started,
                created_reason="connector_stay_unavailable",
                record_id=event.id,
            )
        if event.kind == TripChangeKind.TRAVELER_WITHDRAWN:
            traveler_name = _traveler_name_from_event(active.intent, event)
            if traveler_name is None:
                return self._blocked(
                    trip_run,
                    event.kind,
                    None,
                    "变化事件缺少当前行程中的旅行者。",
                    TripChangeStatus.CLARIFICATION_REQUIRED,
                    elapsed_ms=_elapsed_ms(started),
                    record_id=event.id,
                )
            return await self._withdraw_traveler(
                trip_run,
                traveler_name,
                request_text=None,
                provider=provider,
                started=started,
                record_id=event.id,
                kind=event.kind,
            )
        if event.kind in {
            TripChangeKind.PRICE_CHANGED,
            TripChangeKind.TRANSPORT_SCHEDULE_CHANGED,
        }:
            return self._apply_component_fact_change(
                trip_run,
                event,
                started=started,
            )
        return self._blocked(
            trip_run,
            event.kind,
            None,
            "当前变化类型未映射到复杂行程。",
            TripChangeStatus.CLARIFICATION_REQUIRED,
            elapsed_ms=_elapsed_ms(started),
            record_id=event.id,
        )

    async def _replace_stays(
        self,
        trip_run: TripRun,
        *,
        affected_requirements: tuple[StayRequirement, ...],
        target_offer_ids: tuple[str, ...],
        request_text: str | None,
        kind: TripChangeKind,
        provider: object,
        started: float,
        created_reason: str,
        record_id: str | None = None,
        replacement_intent: TravelIntent | None = None,
        require_different_property: bool = True,
        impact_override: ComplexImpactScope | None = None,
    ) -> TripRunMutationResult:
        active = trip_run.active_version()
        intent = replacement_intent or active.intent
        assert active.selected_plan_graph is not None
        impact = impact_override or _impact_for_targets(active, target_offer_ids, kind)
        scoped_intent = intent.model_copy(
            update={
                "route_legs": (),
                "stay_requirements": affected_requirements,
                "places": tuple(
                    place
                    for place in intent.places
                    if any(
                        requirement.place_id == place.id
                        for requirement in affected_requirements
                    )
                )
                or intent.places,
            }
        )
        queried, queried_contracts, query_error = await _query_stays(
            provider,
            scoped_intent,
        )
        query_count = len(queried.query_tasks)
        query_scope = queried.query_tasks
        if query_error is not None:
            return self._blocked(
                trip_run,
                kind,
                request_text,
                query_error,
                TripChangeStatus.NEEDS_SCOPE_EXPANSION,
                target_refs=target_offer_ids,
                impact=impact,
                query_scope=query_scope,
                external_query_count=query_count,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
                needs_scope_expansion=(
                    "同一住宿日期与地点的其他来源",
                    "相邻日期或接驳",
                    "全局行程",
                ),
            )
        excluded_property_names = (
            {
                _property_name(item.label)
                for item in active.selected_plan_graph.components
                if item.offer_id in target_offer_ids
            }
            if require_different_property
            else set()
        )
        alternatives = tuple(
            item
            for item in queried.stays
            if item.id not in target_offer_ids
            and _property_name(item.label) not in excluded_property_names
            and any(
                item.place_id == requirement.place_id
                and item.check_in <= requirement.check_in
                and item.check_out >= requirement.check_out
                and (
                    not item.participant_ids
                    or set(item.participant_ids)
                    == set(requirement.participant_ids)
                )
                for requirement in affected_requirements
            )
        )
        if not alternatives:
            return self._blocked(
                trip_run,
                kind,
                request_text,
                "受影响住宿段当前没有另一家费用完整的可用酒店。",
                TripChangeStatus.NEEDS_SCOPE_EXPANSION,
                target_refs=target_offer_ids,
                impact=impact,
                query_scope=query_scope,
                external_query_count=query_count,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
                needs_scope_expansion=(
                    "同一住宿日期与地点的其他来源",
                    "相邻日期或接驳",
                    "全局行程",
                ),
            )
        queried = queried.model_copy(update={"stays": alternatives})
        catalog, contracts = _locked_catalog_with_scoped_stays(
            active,
            intent,
            affected_requirements,
            target_offer_ids,
            queried,
            queried_contracts,
        )
        return self._solve_new_version(
            trip_run,
            intent=intent,
            catalog=catalog,
            contracts=contracts,
            kind=kind,
            request_text=request_text,
            target_refs=target_offer_ids,
            impact=impact,
            query_scope=query_scope,
            external_query_count=query_count,
            started=started,
            created_reason=created_reason,
            record_id=record_id,
        )

    async def _withdraw_traveler(
        self,
        trip_run: TripRun,
        traveler_name: str,
        *,
        request_text: str | None,
        provider: object,
        started: float,
        record_id: str | None = None,
        kind: TripChangeKind = TripChangeKind.NATURAL_LANGUAGE,
    ) -> TripRunMutationResult:
        active = trip_run.active_version()
        traveler = next(
            (
                item
                for item in active.intent.traveler_profiles
                if item.name == traveler_name or item.id == traveler_name
            ),
            None,
        )
        if traveler is None:
            return self._blocked(
                trip_run,
                kind,
                request_text,
                f"当前行程中没有旅行者{traveler_name}。",
                TripChangeStatus.NO_EFFECT,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
            )
        if active.intent.travelers <= 1:
            return self._blocked(
                trip_run,
                kind,
                request_text,
                "不能移除行程中最后一位旅行者。",
                TripChangeStatus.NEEDS_SCOPE_EXPANSION,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
            )
        new_intent, affected_requirements, affected_offer_ids = _intent_without_traveler(
            active,
            traveler,
        )
        if affected_requirements:
            return await self._replace_stays(
                trip_run,
                affected_requirements=affected_requirements,
                target_offer_ids=affected_offer_ids,
                request_text=request_text,
                kind=kind,
                provider=provider,
                started=started,
                created_reason=f"traveler_withdrawn:{traveler.id}",
                record_id=record_id,
                replacement_intent=new_intent,
                require_different_property=False,
                impact_override=_impact_for_traveler(active, traveler.id),
            )
        catalog, contracts = _locked_catalog_without_traveler(active, new_intent, traveler.id)
        return self._solve_new_version(
            trip_run,
            intent=new_intent,
            catalog=catalog,
            contracts=contracts,
            kind=kind,
            request_text=request_text,
            target_refs=(traveler.id,),
            impact=_impact_for_traveler(active, traveler.id),
            query_scope=(),
            external_query_count=0,
            started=started,
            created_reason=f"traveler_withdrawn:{traveler.id}",
            record_id=record_id,
        )

    def _apply_component_fact_change(
        self,
        trip_run: TripRun,
        event: StructuredTripChangeEvent,
        *,
        started: float,
    ) -> TripRunMutationResult:
        active = trip_run.active_version()
        target = _selected_component(active, event.target_offer_id)
        if target is None:
            return self._blocked(
                trip_run,
                event.kind,
                None,
                "变化事件没有命中当前组件。",
                TripChangeStatus.NO_EFFECT,
                target_refs=tuple(item for item in (event.target_offer_id,) if item),
                elapsed_ms=_elapsed_ms(started),
                record_id=event.id,
            )
        catalog = _selected_only_catalog(active)
        contracts = list(_selected_source_contracts(active))
        if event.kind == TripChangeKind.PRICE_CHANGED:
            if event.new_total_cny_cents is None or not event.source_ref:
                return self._blocked(
                    trip_run,
                    event.kind,
                    None,
                    "价格变化事件需要人民币同行总价和来源引用。",
                    TripChangeStatus.CLARIFICATION_REQUIRED,
                    target_refs=(target.offer_id,),
                    elapsed_ms=_elapsed_ms(started),
                    record_id=event.id,
                )
            contracts = [
                item.model_copy(
                    update={
                        "total_for_party_cents": event.new_total_cny_cents,
                        "source": event.source_ref,
                    }
                )
                if item.id == target.price_contract_id
                else item
                for item in contracts
            ]
        else:
            if (
                target.kind != "transport"
                or event.new_departure is None
                or event.new_arrival is None
                or not event.source_ref
            ):
                return self._blocked(
                    trip_run,
                    event.kind,
                    None,
                    "班次变化事件需要交通组件、新起降时间和来源引用。",
                    TripChangeStatus.CLARIFICATION_REQUIRED,
                    target_refs=(target.offer_id,),
                    elapsed_ms=_elapsed_ms(started),
                    record_id=event.id,
                )
            catalog = catalog.model_copy(
                update={
                    "transports": tuple(
                        item.model_copy(
                            update={
                                "departure": event.new_departure,
                                "arrival": event.new_arrival,
                            }
                        )
                        if item.id == target.offer_id
                        else item
                        for item in catalog.transports
                    )
                }
            )
        return self._solve_new_version(
            trip_run,
            intent=active.intent,
            catalog=catalog,
            contracts=tuple(contracts),
            kind=event.kind,
            request_text=None,
            target_refs=(target.offer_id,),
            impact=_impact_for_targets(active, (target.offer_id,), event.kind),
            query_scope=(),
            external_query_count=0,
            started=started,
            created_reason=f"connector_{event.kind.value}",
            record_id=event.id,
        )

    def _solve_new_version(
        self,
        trip_run: TripRun,
        *,
        intent: TravelIntent,
        catalog: OfferCatalog,
        contracts: tuple[PriceContract, ...],
        kind: TripChangeKind,
        request_text: str | None,
        target_refs: tuple[str, ...],
        impact: ComplexImpactScope,
        query_scope: tuple[str, ...],
        external_query_count: int,
        started: float,
        created_reason: str,
        record_id: str | None,
    ) -> TripRunMutationResult:
        active = trip_run.active_version()
        problem = PlanningCompiler().compile_problem(
            intent,
            offer_catalog=catalog,
            price_contracts=contracts,
            captured_at=max(
                (item.captured_at for item in catalog.source_statuses),
                default=datetime.now(UTC),
            ),
        )
        graph = ComplexCatalogSolver().solve(problem)
        errors = validate_plan_graph(graph, graph.price_contracts, intent=intent, catalog=catalog)
        if graph.status == PlanStatus.NO_SOLUTION or errors:
            detail = "局部候选无法通过整趟时间、地点、人员和费用复算。"
            if errors:
                detail += " " + "；".join(errors[:3])
            return self._blocked(
                trip_run,
                kind,
                request_text,
                detail,
                TripChangeStatus.NEEDS_SCOPE_EXPANSION,
                target_refs=target_refs,
                impact=impact,
                query_scope=query_scope,
                external_query_count=external_query_count,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
                needs_scope_expansion=(
                    "受影响组件的相邻日期或接驳",
                    "全局行程",
                ),
            )
        captured_at = max(
            (item.captured_at for item in catalog.source_statuses),
            default=datetime.now(UTC),
        )
        execution_ready = (
            current_complex_plan_execution_ready(intent, graph, catalog)
            if catalog.source_mode == "current"
            else active.selected_trip_card is not None
            and active.selected_trip_card.status == TripCardStatus.FINAL
            and not errors
        )
        card = project_trip_card(
            intent,
            graph,
            graph.price_contracts,
            catalog.source_statuses,
            captured_at=captured_at,
            execution_ready=execution_ready,
            representative_kind=PlanRepresentativeKind.PERSONALIZED,
            selection_reason=(
                "按本次明确修改保留未受影响安排，"
                "并在受影响的当前来源候选中选择总价更低的可行方案。"
            ),
        )
        if (
            active.selected_trip_card is not None
            and active.selected_plan_graph is not None
            and not _unaffected_components_preserved(
                active.selected_plan_graph,
                graph,
                set(impact.unaffected_component_ids),
            )
        ):
            return self._blocked(
                trip_run,
                kind,
                request_text,
                "局部重规划尝试改动未受影响组件，已保留原版本。",
                TripChangeStatus.NEEDS_SCOPE_EXPANSION,
                target_refs=target_refs,
                impact=impact,
                query_scope=query_scope,
                external_query_count=external_query_count,
                elapsed_ms=_elapsed_ms(started),
                record_id=record_id,
                needs_scope_expansion=("全局行程",),
            )
        elapsed_ms = _elapsed_ms(started)
        diff = build_trip_plan_diff(
            active,
            intent,
            graph,
            query_scope=query_scope,
            external_query_count=external_query_count,
            elapsed_ms=elapsed_ms,
        )
        version_number = len(trip_run.plan_versions) + 1
        version_id = f"{trip_run.id}:plan:v{version_number}"
        graph_version = _next_graph_version(
            active.graph_version,
            intent,
            catalog,
            graph,
        )
        version = ComplexPlanVersion(
            id=version_id,
            run_id=trip_run.id,
            version=version_number,
            parent_version_id=active.id,
            intent=intent,
            selected_plan_graph=graph,
            selected_trip_card=card,
            candidate_plan_graphs=(graph,),
            candidate_trip_cards=(card,),
            graph_version=graph_version,
            catalog_digest=_catalog_digest(catalog, contracts),
            offer_catalog=catalog,
            source_contracts=contracts,
            dependencies=build_complex_plan_dependencies(intent, graph),
            created_reason=created_reason,
            created_at=datetime.now(UTC),
            diff_from_parent=diff,
        )
        record = TripChangeRecord(
            id=record_id or f"trip-change-{uuid4()}",
            kind=kind,
            status=TripChangeStatus.APPLIED,
            request_text=request_text,
            target_refs=target_refs,
            message="已生成新版本并对整趟行程重新复算。",
            impact=impact,
            query_scope=query_scope,
            external_query_count=external_query_count,
            elapsed_ms=elapsed_ms,
            created_at=datetime.now(UTC),
            resulting_plan_version_id=version.id,
        )
        updated = trip_run.model_copy(
            update={
                "active_plan_version_id": version.id,
                "plan_versions": (*trip_run.plan_versions, version),
                "change_history": (*trip_run.change_history, record),
                "updated_at": datetime.now(UTC),
            }
        )
        return TripRunMutationResult(
            status=TripChangeStatus.APPLIED,
            trip_run=updated,
            active_plan_version=version,
            diff=diff,
            message=record.message,
            external_query_count=external_query_count,
            elapsed_ms=elapsed_ms,
        )

    def _blocked(
        self,
        trip_run: TripRun,
        kind: TripChangeKind,
        request_text: str | None,
        message: str,
        status: TripChangeStatus,
        *,
        target_refs: tuple[str, ...] = (),
        impact: ComplexImpactScope | None = None,
        query_scope: tuple[str, ...] = (),
        external_query_count: int = 0,
        elapsed_ms: int,
        record_id: str | None = None,
        needs_scope_expansion: tuple[str, ...] = (),
    ) -> TripRunMutationResult:
        active = trip_run.active_version()
        resolved_impact = impact or ComplexImpactScope(
            unaffected_component_ids=tuple(
                item.offer_id
                for item in (
                    active.selected_plan_graph.components
                    if active.selected_plan_graph is not None
                    else ()
                )
            )
        )
        record = TripChangeRecord(
            id=record_id or f"trip-change-{uuid4()}",
            kind=kind,
            status=status,
            request_text=request_text,
            target_refs=target_refs,
            message=message,
            impact=resolved_impact,
            query_scope=query_scope,
            external_query_count=external_query_count,
            elapsed_ms=elapsed_ms,
            created_at=datetime.now(UTC),
            needs_scope_expansion=needs_scope_expansion,
        )
        updated = trip_run.model_copy(
            update={
                "change_history": (*trip_run.change_history, record),
                "updated_at": datetime.now(UTC),
            }
        )
        return TripRunMutationResult(
            status=status,
            trip_run=updated,
            active_plan_version=active,
            message=message,
            needs_scope_expansion=needs_scope_expansion,
            external_query_count=external_query_count,
            elapsed_ms=elapsed_ms,
        )


async def _query_stays(
    provider: object,
    intent: TravelIntent,
) -> tuple[OfferCatalog, tuple[PriceContract, ...], str | None]:
    method = getattr(provider, "catalog_for_stays", None)
    if method is None:
        return OfferCatalog(), (), "当前来源不支持只查询受影响住宿段。"
    try:
        result = method(intent)
        if inspect.isawaitable(result):
            result = await result
        catalog, contracts = result
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return OfferCatalog(), (), f"受影响住宿段查询失败:{type(exc).__name__}"
    if not isinstance(catalog, OfferCatalog):
        return OfferCatalog(), (), "受影响住宿段来源返回无效。"
    if any(item.state != SourceState.SUCCEEDED for item in catalog.source_statuses):
        return catalog, tuple(contracts), "受影响住宿段当前来源未成功返回。"
    return catalog, tuple(contracts), None


def _selected_only_catalog(active: ComplexPlanVersion) -> OfferCatalog:
    assert active.selected_plan_graph is not None
    selected = {item.offer_id for item in active.selected_plan_graph.components}
    catalog = active.offer_catalog
    return catalog.model_copy(
        update={
            "transports": tuple(item for item in catalog.transports if item.id in selected),
            "stays": tuple(item for item in catalog.stays if item.id in selected),
            "bundles": tuple(
                item
                for item in catalog.bundles
                if set(item.component_offer_ids).issubset(selected)
            ),
        }
    )


def _selected_source_contracts(active: ComplexPlanVersion) -> tuple[PriceContract, ...]:
    assert active.selected_plan_graph is not None
    selected_ids = set(active.selected_plan_graph.counted_price_contract_ids)
    return tuple(item for item in active.source_contracts if item.id in selected_ids)


def _locked_catalog_with_scoped_stays(
    active: ComplexPlanVersion,
    intent: TravelIntent,
    affected_requirements: tuple[StayRequirement, ...],
    target_offer_ids: tuple[str, ...],
    queried: OfferCatalog,
    queried_contracts: tuple[PriceContract, ...],
) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
    assert active.selected_plan_graph is not None
    selected = {item.offer_id for item in active.selected_plan_graph.components}
    removed = set(target_offer_ids)
    transports = tuple(
        item for item in active.offer_catalog.transports if item.id in selected
    )
    stays = tuple(
        item
        for item in active.offer_catalog.stays
        if item.id in selected and item.id not in removed
    ) + queried.stays
    selected_contract_ids = {
        item.price_contract_id
        for item in active.selected_plan_graph.components
        if item.offer_id not in removed
        and item.price_contract_id != "user-provided-not-priced"
    }
    contracts = tuple(
        item for item in active.source_contracts if item.id in selected_contract_ids
    ) + tuple(
        item
        for item in queried_contracts
        if any(stay.price_contract_id == item.id for stay in queried.stays)
    )
    old_statuses = tuple(
        status
        for status in active.offer_catalog.source_statuses
        if not _status_matches_stay_requirements(status, affected_requirements)
    )
    old_tasks = tuple(
        task
        for task in active.offer_catalog.query_tasks
        if not _task_matches_stay_requirements(task, affected_requirements)
    )
    catalog = OfferCatalog(
        transports=transports,
        stays=stays,
        bundles=tuple(
            item
            for item in active.offer_catalog.bundles
            if set(item.component_offer_ids).issubset(selected - removed)
        ),
        query_tasks=tuple(dict.fromkeys((*old_tasks, *queried.query_tasks))),
        source_statuses=(*old_statuses, *queried.source_statuses),
        source_mode=active.offer_catalog.source_mode,
    )
    return catalog, tuple(dict((item.id, item) for item in contracts).values())


def _locked_catalog_without_traveler(
    active: ComplexPlanVersion,
    intent: TravelIntent,
    traveler_id: str,
) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
    assert active.selected_plan_graph is not None
    selected_components = tuple(
        item
        for item in active.selected_plan_graph.components
        if traveler_id not in item.participant_ids and item.kind != "anchor"
    )
    selected_ids = {item.offer_id for item in selected_components}
    selected_contract_ids = {item.price_contract_id for item in selected_components}
    catalog = active.offer_catalog.model_copy(
        update={
            "transports": tuple(
                item for item in active.offer_catalog.transports if item.id in selected_ids
            ),
            "stays": tuple(
                item for item in active.offer_catalog.stays if item.id in selected_ids
            ),
            "bundles": tuple(
                item
                for item in active.offer_catalog.bundles
                if set(item.component_offer_ids).issubset(selected_ids)
            ),
        }
    )
    contracts = tuple(
        item for item in active.source_contracts if item.id in selected_contract_ids
    )
    return catalog, contracts


def _intent_without_traveler(
    active: ComplexPlanVersion,
    traveler: Traveler,
) -> tuple[TravelIntent, tuple[StayRequirement, ...], tuple[str, ...]]:
    intent = active.intent
    remaining_profiles = tuple(
        item for item in intent.traveler_profiles if item.id != traveler.id
    )
    groups: list[TravelerGroup] = []
    for group in intent.traveler_groups:
        remaining = tuple(item for item in group.traveler_ids if item != traveler.id)
        if remaining:
            groups.append(group.model_copy(update={"traveler_ids": remaining}))
    route_legs = tuple(
        item.model_copy(
            update={
                "participant_ids": tuple(
                    person for person in item.participant_ids if person != traveler.id
                )
            }
        )
        for item in intent.route_legs
        if any(person != traveler.id for person in item.participant_ids)
    )
    updated_stays: list[StayRequirement] = []
    affected: list[StayRequirement] = []
    affected_offer_ids: list[str] = []
    for requirement in intent.stay_requirements:
        participants = tuple(
            person for person in requirement.participant_ids if person != traveler.id
        )
        if not participants:
            continue
        updated = requirement.model_copy(update={"participant_ids": participants})
        updated_stays.append(updated)
        if traveler.id in requirement.participant_ids:
            affected.append(updated)
            component = _component_for_stay_requirement(active, requirement)
            if component is not None:
                affected_offer_ids.append(component.offer_id)
    anchors = tuple(
        anchor.model_copy(
            update={
                "participant_ids": tuple(
                    person for person in anchor.participant_ids if person != traveler.id
                ),
                "traveler_count": len(
                    tuple(
                        person
                        for person in anchor.participant_ids
                        if person != traveler.id
                    )
                ),
            }
        )
        for anchor in intent.anchors
        if any(person != traveler.id for person in anchor.participant_ids)
    )
    new_intent = intent.model_copy(
        update={
            "travelers": len(remaining_profiles),
            "origin": remaining_profiles[0].origin,
            "traveler_profiles": remaining_profiles,
            "traveler_groups": tuple(groups),
            "route_legs": route_legs,
            "stay_requirements": tuple(updated_stays),
            "anchors": anchors,
        }
    )
    return new_intent, tuple(affected), tuple(affected_offer_ids)


def _explicit_stay_replacement_target(
    active: ComplexPlanVersion,
    text_value: str,
) -> tuple[PlanComponent, StayRequirement] | None:
    normalized = "".join(text_value.split())
    if "换" not in normalized or "酒店" not in normalized:
        return None
    if not any(marker in normalized for marker in ("保留所有车次", "保留车次", "只把")):
        return None
    date_range = _date_range_from_text(normalized, active.intent.window.start.year)
    if date_range is None:
        return None
    check_in, check_out = date_range
    assert active.selected_plan_graph is not None
    for component in active.selected_plan_graph.components:
        if component.kind != "stay":
            continue
        if _as_date(component.start) != check_in or _as_date(component.end) != check_out:
            continue
        if "共享" in normalized and not (
            len(component.participant_ids) >= 2
            or (
                not component.participant_ids
                and active.intent.travelers >= 2
            )
        ):
            continue
        requirement = _stay_requirement_for_component(active.intent, component)
        if requirement is not None:
            return component, requirement
    return None


def _date_range_from_text(value: str, year: int) -> tuple[date, date] | None:
    iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:至|到|~)(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if iso:
        return (
            date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))),
            date(int(iso.group(4)), int(iso.group(5)), int(iso.group(6))),
        )
    chinese = re.search(
        r"(\d{1,2})月(\d{1,2})日(?:至|到|~)(\d{1,2})月(\d{1,2})日",
        value,
    )
    if chinese:
        return (
            date(year, int(chinese.group(1)), int(chinese.group(2))),
            date(year, int(chinese.group(3)), int(chinese.group(4))),
        )
    return None


def _withdrawn_traveler_name(value: str) -> str | None:
    match = re.search(r"(?:旅行者)?([\w\u4e00-\u9fff]{1,12})退出行程", "".join(value.split()))
    return match.group(1) if match else None


def _traveler_name_from_event(
    intent: TravelIntent,
    event: StructuredTripChangeEvent,
) -> str | None:
    return next(
        (
            item.name
            for item in intent.traveler_profiles
            if item.id == event.traveler_id or item.name == event.traveler_id
        ),
        None,
    )


def _selected_component(
    active: ComplexPlanVersion,
    offer_id: str | None,
    *,
    kind: str | None = None,
) -> PlanComponent | None:
    if offer_id is None or active.selected_plan_graph is None:
        return None
    return next(
        (
            item
            for item in active.selected_plan_graph.components
            if item.offer_id == offer_id and (kind is None or item.kind == kind)
        ),
        None,
    )


def _stay_requirement_for_component(
    intent: TravelIntent,
    component: PlanComponent | None,
) -> StayRequirement | None:
    if component is None:
        return None
    found = next(
        (
            item
            for item in intent.stay_requirements
            if item.place_id == component.place_from
            and item.check_in == _as_date(component.start)
            and item.check_out == _as_date(component.end)
            and set(item.participant_ids) == set(component.participant_ids)
        ),
        None,
    )
    if found is not None:
        return found
    # The first multi-city compiler versions derive one stay slot per place
    # instead of materialising StayRequirement objects.  A versioned TripRun
    # still needs a typed slot to target that lodging component; derive only
    # dates/place/participants already present in the selected graph.
    return StayRequirement(
        id=_stable_component_id(component),
        place_id=component.place_from,
        check_in=_as_date(component.start),
        check_out=_as_date(component.end),
        participant_ids=component.participant_ids
        or tuple(f"traveler:{index + 1}" for index in range(intent.travelers)),
        room_count=1,
    )


def _component_for_stay_requirement(
    active: ComplexPlanVersion,
    requirement: StayRequirement,
) -> PlanComponent | None:
    if active.selected_plan_graph is None:
        return None
    return next(
        (
            item
            for item in active.selected_plan_graph.components
            if item.kind == "stay"
            and item.place_from == requirement.place_id
            and _as_date(item.start) == requirement.check_in
            and _as_date(item.end) == requirement.check_out
            and set(item.participant_ids) == set(requirement.participant_ids)
        ),
        None,
    )


def _status_matches_stay_requirements(
    status: SourceStatus,
    requirements: tuple[StayRequirement, ...],
) -> bool:
    return status.provider.lower() == "trip.com" and any(
        _task_matches_stay_requirements(task, requirements)
        for task in status.query_task_ids
    )


def _task_matches_stay_requirements(
    task: str,
    requirements: tuple[StayRequirement, ...],
) -> bool:
    return any(
        requirement.check_in.isoformat() in task
        and requirement.check_out.isoformat() in task
        for requirement in requirements
    )


def _impact_for_targets(
    active: ComplexPlanVersion,
    target_ids: tuple[str, ...],
    kind: TripChangeKind,
) -> ComplexImpactScope:
    target_set = set(target_ids)
    direct = {
        _stable_component_id(item)
        for item in (
            active.selected_plan_graph.components
            if active.selected_plan_graph is not None
            else ()
        )
        if item.offer_id in target_set or _stable_component_id(item) in target_set
    }
    allowed = (
        {ComplexDependencyKind.PRICE_CONTRACT, ComplexDependencyKind.LODGING}
        if kind in {TripChangeKind.NATURAL_LANGUAGE, TripChangeKind.STAY_UNAVAILABLE}
        else {ComplexDependencyKind.PRICE_CONTRACT}
        if kind == TripChangeKind.PRICE_CHANGED
        else {
            ComplexDependencyKind.TEMPORAL,
            ComplexDependencyKind.LOCATION,
            ComplexDependencyKind.TRAVELER,
            ComplexDependencyKind.FIXED_ACTIVITY,
            ComplexDependencyKind.PRICE_CONTRACT,
        }
    )
    adjacency: dict[str, list[ComplexPlanDependency]] = defaultdict(list)
    for edge in active.dependencies:
        adjacency[edge.upstream_component_id].append(edge)
        if edge.kind in {ComplexDependencyKind.PRICE_CONTRACT, ComplexDependencyKind.LODGING}:
            adjacency[edge.downstream_component_id].append(
                edge.model_copy(
                    update={
                        "upstream_component_id": edge.downstream_component_id,
                        "downstream_component_id": edge.upstream_component_id,
                    }
                )
            )
    affected = set(direct)
    traversed: set[ComplexDependencyKind] = set()
    queue: deque[str] = deque(direct)
    while queue:
        current = queue.popleft()
        for edge in adjacency[current]:
            if edge.kind not in allowed:
                continue
            traversed.add(edge.kind)
            if edge.downstream_component_id in affected:
                continue
            affected.add(edge.downstream_component_id)
            queue.append(edge.downstream_component_id)
    all_ids = {
        _stable_component_id(item)
        for item in (
            active.selected_plan_graph.components
            if active.selected_plan_graph is not None
            else ()
        )
    }
    return ComplexImpactScope(
        direct_component_ids=tuple(sorted(direct)),
        affected_component_ids=tuple(sorted(affected)),
        unaffected_component_ids=tuple(sorted(all_ids - affected)),
        traversed_dependency_kinds=tuple(sorted(traversed, key=lambda item: item.value)),
    )


def _impact_for_traveler(
    active: ComplexPlanVersion,
    traveler_id: str,
) -> ComplexImpactScope:
    direct = tuple(
        item.offer_id
        for item in (
            active.selected_plan_graph.components
            if active.selected_plan_graph is not None
            else ()
        )
        if traveler_id in item.participant_ids
    )
    return _impact_for_targets(active, direct, TripChangeKind.TRAVELER_WITHDRAWN)


def build_trip_plan_diff(
    before: ComplexPlanVersion,
    after_intent: TravelIntent,
    after_graph: PlanGraph,
    *,
    query_scope: tuple[str, ...],
    external_query_count: int,
    elapsed_ms: int,
) -> TripPlanDiff:
    assert before.selected_plan_graph is not None
    before_by_id = {
        _stable_component_id(item): item
        for item in before.selected_plan_graph.components
    }
    after_by_id = {_stable_component_id(item): item for item in after_graph.components}
    kept: list[TripComponentReference] = []
    replaced: list[TripComponentReplacement] = []
    removed_ids = set(before_by_id) - set(after_by_id)
    added_ids = set(after_by_id) - set(before_by_id)
    for component_id in set(before_by_id) & set(after_by_id):
        if _components_equivalent(before_by_id[component_id], after_by_id[component_id]):
            kept.append(_component_ref(before_by_id[component_id]))
        else:
            replaced.append(
                TripComponentReplacement(
                    before=_component_ref(before_by_id[component_id]),
                    after=_component_ref(after_by_id[component_id]),
                )
            )
    for before_id in tuple(sorted(removed_ids)):
        before_component = before_by_id[before_id]
        matching = next(
            (
                after_id
                for after_id in sorted(added_ids)
                if _same_component_slot(before_component, after_by_id[after_id])
            ),
            None,
        )
        if matching is None:
            continue
        replaced.append(
            TripComponentReplacement(
                before=_component_ref(before_component),
                after=_component_ref(after_by_id[matching]),
            )
        )
        removed_ids.remove(before_id)
        added_ids.remove(matching)
    before_total = before.selected_plan_graph.total_cny_cents
    after_total = after_graph.total_cny_cents
    traveler_changes = _traveler_diffs(
        before.intent,
        before.selected_plan_graph,
        after_intent,
        after_graph,
        replaced,
    )
    return TripPlanDiff(
        kept=tuple(sorted(kept, key=lambda item: item.component_id)),
        replaced=tuple(
            sorted(replaced, key=lambda item: item.before.component_id)
        ),
        added=tuple(
            _component_ref(after_by_id[item]) for item in sorted(added_ids)
        ),
        removed=tuple(
            _component_ref(before_by_id[item]) for item in sorted(removed_ids)
        ),
        traveler_changes=traveler_changes,
        before_total_cny_cents=before_total,
        after_total_cny_cents=after_total,
        delta_cny_cents=(
            after_total - before_total
            if before_total is not None and after_total is not None
            else None
        ),
        query_scope=query_scope,
        external_query_count=external_query_count,
        elapsed_ms=elapsed_ms,
    )


def _traveler_diffs(
    before_intent: TravelIntent,
    before_graph: PlanGraph,
    after_intent: TravelIntent,
    after_graph: PlanGraph,
    replacements: list[TripComponentReplacement],
) -> tuple[TravelerPlanDiff, ...]:
    before_names = {item.id: item.name for item in before_intent.traveler_profiles}
    after_names = {item.id: item.name for item in after_intent.traveler_profiles}
    replacement_before = {item.before.component_id for item in replacements}
    all_ids = tuple(sorted(set(before_names) | set(after_names)))
    rows: list[TravelerPlanDiff] = []
    for traveler_id in all_ids:
        before_components = {
            _stable_component_id(item)
            for item in before_graph.components
            if traveler_id in item.participant_ids
        }
        after_components = {
            _stable_component_id(item)
            for item in after_graph.components
            if traveler_id in item.participant_ids
        }
        status = (
            "removed"
            if traveler_id not in after_names
            else "added"
            if traveler_id not in before_names
            else "changed"
            if before_components != after_components
            else "kept"
        )
        rows.append(
            TravelerPlanDiff(
                traveler_id=traveler_id,
                traveler_name=after_names.get(
                    traveler_id,
                    before_names.get(traveler_id, traveler_id),
                ),
                status=status,
                kept_component_ids=tuple(sorted(before_components & after_components)),
                replaced_component_ids=tuple(
                    sorted(before_components & replacement_before)
                ),
                added_component_ids=tuple(sorted(after_components - before_components)),
                removed_component_ids=tuple(sorted(before_components - after_components)),
            )
        )
    return tuple(rows)


def _unaffected_components_preserved(
    before: PlanGraph,
    after: PlanGraph,
    unaffected_ids: set[str],
) -> bool:
    before_by_id = {_stable_component_id(item): item for item in before.components}
    after_by_id = {_stable_component_id(item): item for item in after.components}
    return all(
        before_by_id.get(item_id) is not None
        and after_by_id.get(item_id) is not None
        and _components_equivalent(before_by_id[item_id], after_by_id[item_id])
        for item_id in unaffected_ids
    )


def _components_equivalent(before: PlanComponent, after: PlanComponent) -> bool:
    """Compare plan components across typed and JSON persistence boundaries.

    A JSON round trip can parse a date-only lodging boundary as midnight
    ``datetime`` because ``PlanComponent`` intentionally accepts both dates
    and datetimes for transport/anchor components.  That representation change
    is not a travel change and must not create a false replacement or block a
    scoped replan after a process restart.
    """

    def canonical(component: PlanComponent) -> dict[str, object]:
        value = component.model_dump(mode="json")
        if component.kind == "stay":
            value["start"] = _as_date(component.start).isoformat()
            value["end"] = _as_date(component.end).isoformat()
        else:
            for field_name in ("start", "end"):
                raw = getattr(component, field_name)
                if isinstance(raw, datetime):
                    normalized = raw.astimezone(UTC) if raw.tzinfo is not None else raw
                    value[field_name] = normalized.isoformat()
                elif isinstance(raw, date):
                    value[field_name] = raw.isoformat()
        return value

    return canonical(before) == canonical(after)


def _component_ref(component: PlanComponent) -> TripComponentReference:
    return TripComponentReference(
        component_id=_stable_component_id(component),
        offer_id=component.offer_id,
        name=component.label,
        kind=component.kind,
    )


def _same_component_slot(before: PlanComponent, after: PlanComponent) -> bool:
    return (
        before.kind == after.kind
        and before.start == after.start
        and before.end == after.end
        and before.place_from == after.place_from
        and before.place_to == after.place_to
        and set(before.participant_ids) == set(after.participant_ids)
    )


def _component_time_key(component: PlanComponent) -> tuple[datetime, str]:
    value = component.start
    resolved = value if isinstance(value, datetime) else datetime.combine(value, time.min)
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    return resolved, component.offer_id


def _component_covers(stay: PlanComponent, anchor: PlanComponent) -> bool:
    return _as_date(stay.start) <= _as_date(anchor.start) < _as_date(stay.end)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _property_name(label: str) -> str:
    return label.split("｜", 1)[0].strip().casefold()


def _stable_component_id(component: PlanComponent) -> str:
    return component.component_id or component.offer_id


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


__all__ = [
    "ComplexDependencyKind",
    "ComplexImpactScope",
    "ComplexPlanDependency",
    "ComplexPlanVersion",
    "ComplexTripRunReplanner",
    "NaturalLanguageTripModification",
    "StructuredTripChangeEvent",
    "TripChangeKind",
    "TripChangeRecord",
    "TripChangeStatus",
    "TripPlanDiff",
    "TripRun",
    "TripRunMutationResult",
    "build_complex_plan_dependencies",
    "build_initial_trip_run",
    "build_trip_plan_diff",
]
