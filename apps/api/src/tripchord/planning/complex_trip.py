"""Generic typed contracts and bounded solver for complex trips.

Providers populate ``OfferCatalog``; this module never contains a destination,
price, or URL.  Frozen catalogs belong to tests/replay providers.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any, Protocol

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    PackageCandidateGenerationResult,
    PackageIntent,
    PackageInventory,
    PackagePlanner,
    PackageVerifier,
    TravelPackageCandidate,
)


class PlaceRef(DomainModel):
    id: str
    name: str
    city: str
    kind: str = "city"


class TravelTopology(StrEnum):
    SINGLE_DESTINATION = "single_destination"
    MULTI_CITY = "multi_city"


class SourceState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_QUERIED = "not_queried"


class SourceStatus(DomainModel):
    source_id: str
    provider: str
    state: SourceState
    detail: str
    query_task_ids: tuple[str, ...] = ()
    captured_at: datetime


class TripWindow(DomainModel):
    start: datetime
    end: datetime


class TripAnchor(DomainModel):
    id: str
    name: str
    place_id: str
    start: datetime
    end: datetime
    traveler_count: int = Field(default=1, ge=1)
    provided_price_cny_cents: int | None = Field(default=None, ge=0)


class TripLegRequirement(DomainModel):
    id: str
    origin_place_id: str
    destination_place_id: str
    departure_date: date | None = None
    earliest_departure_date: date | None = None
    latest_departure_date: date | None = None


class TravelIntent(DomainModel):
    topology: TravelTopology = TravelTopology.MULTI_CITY
    travelers: int = Field(ge=1)
    origin: PlaceRef
    places: tuple[PlaceRef, ...]
    window: TripWindow
    route_legs: tuple[TripLegRequirement, ...]
    anchors: tuple[TripAnchor, ...] = ()
    minimum_anchor_buffer_minutes: int = Field(default=0, ge=0)
    preference_summary: str = ""
    unresolved_critical: tuple[str, ...] = ()

    @classmethod
    def from_package_interpretation(cls, interpretation: Any) -> TravelIntent:
        """Project one ready legacy parse into the authoritative domain intent."""
        window = interpretation.window
        template = interpretation.intent_template
        if window is None or template is None:
            raise ValueError("ready package interpretation requires window and template")
        origin = PlaceRef(id=window.origin, name=window.origin, city=window.origin)
        destination = PlaceRef(
            id=window.destination,
            name=window.destination,
            city=window.destination,
        )
        latest_return = (
            window.latest_return_date
            or window.latest_arrival_date
            or window.latest_departure + timedelta(days=window.max_nights)
        )
        earliest_return = window.earliest_departure + timedelta(days=window.min_nights)
        return_targets = window.return_date_targets
        return_exact = return_targets[0] if len(return_targets) == 1 else None
        return cls(
            topology=TravelTopology.SINGLE_DESTINATION,
            travelers=window.adults + window.children + window.infants,
            origin=origin,
            places=(destination,),
            window=TripWindow(
                start=datetime.combine(window.earliest_departure, datetime.min.time()),
                end=datetime.combine(latest_return, datetime.max.time()),
            ),
            route_legs=(
                TripLegRequirement(
                    id="leg:outbound",
                    origin_place_id=origin.id,
                    destination_place_id=destination.id,
                    departure_date=(
                        window.earliest_departure
                        if window.earliest_departure == window.latest_departure
                        else None
                    ),
                    earliest_departure_date=window.earliest_departure,
                    latest_departure_date=window.latest_departure,
                ),
                TripLegRequirement(
                    id="leg:return",
                    origin_place_id=destination.id,
                    destination_place_id=origin.id,
                    departure_date=return_exact,
                    earliest_departure_date=earliest_return,
                    latest_departure_date=latest_return,
                ),
            ),
            preference_summary="单目的地完整行程",
        )


class PriceContract(DomainModel):
    id: str
    currency: str = "CNY"
    total_for_party_cents: int = Field(ge=0)
    component_ids: tuple[str, ...]
    shared: bool = False
    taxes_and_fees_included: bool = True
    source: str = "current"


class TransportOffer(DomainModel):
    id: str
    provider: str
    origin_place_id: str
    destination_place_id: str
    departure: datetime
    arrival: datetime
    price_contract_id: str
    detail_url: str
    label: str


class StayOffer(DomainModel):
    id: str
    provider: str
    place_id: str
    check_in: date
    check_out: date
    price_contract_id: str
    detail_url: str
    label: str


class BundleOffer(DomainModel):
    id: str
    label: str
    component_offer_ids: tuple[str, ...]
    price_contract_id: str


class OfferCatalog(DomainModel):
    transports: tuple[TransportOffer, ...] = ()
    stays: tuple[StayOffer, ...] = ()
    bundles: tuple[BundleOffer, ...] = ()
    query_tasks: tuple[str, ...] = ()
    source_statuses: tuple[SourceStatus, ...] = ()
    source_mode: str = "current"


class ComplexOfferProvider(Protocol):
    def catalog_for(
        self, intent: TravelIntent
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        """Return a bounded catalog and its source-backed contracts."""


class RequirementGraph(DomainModel):
    required_place_ids: tuple[str, ...]
    route_legs: tuple[TripLegRequirement, ...]
    anchor_ids: tuple[str, ...]
    minimum_anchor_buffer_minutes: int


class OfferGraph(DomainModel):
    """Compiled time/location view of the bounded offer catalog."""

    transport_offer_ids: tuple[str, ...] = ()
    stay_offer_ids: tuple[str, ...] = ()
    price_contract_ids: tuple[str, ...] = ()


class PlanningProblem(DomainModel):
    intent: TravelIntent
    requirement_graph: RequirementGraph | None = None
    offer_graph: OfferGraph | None = None
    offer_catalog: OfferCatalog = OfferCatalog()
    price_contracts: tuple[PriceContract, ...] = ()
    legacy_intent: PackageIntent | None = None
    legacy_inventory: PackageInventory | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PlanComponent(DomainModel):
    kind: str
    offer_id: str
    label: str
    provider: str
    start: datetime | date
    end: datetime | date
    place_from: str | None = None
    place_to: str | None = None
    price_contract_id: str
    detail_url: str
    price_cny_cents: int | None = None
    shared_price_contract: bool = False


class PlanStatus(StrEnum):
    FEASIBLE = "feasible"
    OPTIMAL_IN_CATALOG = "optimal-in-catalog"
    NO_SOLUTION = "no-solution"


class PlanGraph(DomainModel):
    status: PlanStatus
    components: tuple[PlanComponent, ...] = ()
    total_cny_cents: int | None = None
    counted_price_contract_ids: tuple[str, ...] = ()
    price_contracts: tuple[PriceContract, ...] = ()
    checked_constraints: tuple[str, ...] = ()
    claim_boundary: str


class TripCardStatus(StrEnum):
    FINAL = "final"
    CANDIDATE = "candidate"
    SOURCE_GAP = "source_gap"
    NO_SOLUTION = "no_solution"


class TripCardProjection(DomainModel):
    """The sole public itinerary card for every topology."""

    status: TripCardStatus
    title: str
    start_date: date
    end_date: date
    city_order: tuple[str, ...]
    traveler_count: int = Field(ge=1)
    total_cny_cents: int | None = None
    components: tuple[PlanComponent, ...] = ()
    fixed_activities: tuple[PlanComponent, ...] = ()
    price_contracts: tuple[PriceContract, ...] = ()
    activity_price_included: bool = False
    unresolved_items: tuple[str, ...] = ()
    source_statuses: tuple[SourceStatus, ...] = ()
    query_captured_at: datetime
    source_boundary: str


class PlanSolver(Protocol):
    def solve(self, problem: PlanningProblem) -> PlanGraph:
        """Solve one compiled travel problem."""


class PlanningCompiler:
    """Compile a natural-language request into the authoritative intent."""

    def compile(self, text: str, *, reference_year: int | None = None) -> TravelIntent | None:
        if not is_complex_multi_city_request(text):
            return None
        return parse_complex_intent(text, reference_year=reference_year)

    def from_package_interpretation(self, interpretation: Any) -> TravelIntent:
        return TravelIntent.from_package_interpretation(interpretation)

    def compile_problem(
        self,
        intent: TravelIntent,
        *,
        offer_catalog: OfferCatalog | None = None,
        price_contracts: tuple[PriceContract, ...] = (),
        captured_at: datetime | None = None,
        legacy_intent: PackageIntent | None = None,
        legacy_inventory: PackageInventory | None = None,
    ) -> PlanningProblem:
        """Compile one intent and source catalog into the common solver input."""
        catalog = offer_catalog or OfferCatalog()
        return PlanningProblem(
            intent=intent,
            requirement_graph=RequirementGraph(
                required_place_ids=tuple(item.id for item in intent.places),
                route_legs=intent.route_legs,
                anchor_ids=tuple(item.id for item in intent.anchors),
                minimum_anchor_buffer_minutes=intent.minimum_anchor_buffer_minutes,
            ),
            offer_graph=OfferGraph(
                transport_offer_ids=tuple(item.id for item in catalog.transports),
                stay_offer_ids=tuple(item.id for item in catalog.stays),
                price_contract_ids=tuple(item.id for item in price_contracts),
            ),
            offer_catalog=catalog,
            price_contracts=price_contracts,
            legacy_intent=legacy_intent,
            legacy_inventory=legacy_inventory,
            captured_at=captured_at or datetime.now(UTC),
        )


class ComplexCatalogSolver:
    def solve(self, problem: PlanningProblem) -> PlanGraph:
        return solve_complex_catalog(problem)


class PackagePlannerAdapter:
    """Real adapter around the existing package planner and verifier."""

    def __init__(
        self,
        planner: PackagePlanner,
        verifier: PackageVerifier,
        *,
        now: Any | None = None,
    ) -> None:
        self.planner = planner
        self.verifier = verifier
        self._now = now

    def generate_verified(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> PackageCandidateGenerationResult:
        generation = self.planner.generate_bounded(intent, inventory)
        verified = tuple(
            candidate
            for candidate in generation.candidates
            if not self.verifier.errors(intent, candidate, now=self._now() if self._now else None)
        )
        return generation.model_copy(update={"candidates": verified})

    def generate_bounded(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> PackageCandidateGenerationResult:
        """Transparent live delegate; repair/risk stages retain every candidate."""
        return self.planner.generate_bounded(intent, inventory)

    def solve(self, problem: PlanningProblem) -> PlanGraph:
        if problem.legacy_intent is None or problem.legacy_inventory is None:
            raise ValueError("single-destination problem requires legacy package payload")
        generated = self.generate_verified(problem.legacy_intent, problem.legacy_inventory)
        if not generated.candidates:
            return PlanGraph(
                status=PlanStatus.NO_SOLUTION,
                claim_boundary="当前单目的地库存没有通过最终校验的完整方案",
            )
        winner = min(
            generated.candidates,
            key=lambda item: (item.declared_total_cents, item.id),
        )
        return _legacy_candidate_plan_graph(winner)


def _legacy_candidate_plan_graph(candidate: TravelPackageCandidate) -> PlanGraph:
    contract_id = f"legacy-package:{candidate.id}"
    components = (
        PlanComponent(
            kind="transport",
            offer_id=candidate.flight.id,
            label=f"{candidate.flight.origin}往返{candidate.flight.destination}",
            provider=candidate.flight.provider,
            start=candidate.flight.outbound_depart_at,
            end=candidate.flight.return_arrive_at,
            place_from=candidate.flight.origin,
            place_to=candidate.flight.destination,
            price_contract_id=contract_id,
            detail_url=_first_http_evidence(candidate.flight.evidence_refs),
            price_cny_cents=candidate.declared_total_cents,
            shared_price_contract=True,
        ),
        *(
            PlanComponent(
                kind="stay",
                offer_id=item.id,
                label=item.property_name,
                provider=item.provider,
                start=item.check_in,
                end=item.check_out,
                place_from=item.place_key,
                price_contract_id=contract_id,
                detail_url=_first_http_evidence(item.evidence_refs),
                shared_price_contract=True,
            )
            for item in candidate.lodgings
        ),
        *(
            PlanComponent(
                kind="transfer",
                offer_id=item.id,
                label=f"{item.origin_area.value}→{item.destination_area.value}",
                provider=item.provider,
                start=item.service_date,
                end=item.service_date,
                place_from=item.origin_place_key,
                place_to=item.destination_place_key,
                price_contract_id=contract_id,
                detail_url=item.detail_url,
                shared_price_contract=True,
            )
            for item in candidate.transfers
        ),
    )
    contract = PriceContract(
        id=contract_id,
        total_for_party_cents=candidate.declared_total_cents,
        component_ids=tuple(item.offer_id for item in components),
        shared=len(components) > 1,
        taxes_and_fees_included=True,
        source="legacy-package-planner",
    )
    return PlanGraph(
        status=PlanStatus.FEASIBLE,
        components=components,
        total_cny_cents=candidate.declared_total_cents,
        counted_price_contract_ids=(contract_id,),
        price_contracts=(contract,),
        checked_constraints=("PackagePlanner生成", "PackageVerifier无错误"),
        claim_boundary="单目的地候选由现有 PackagePlanner 生成并经 PackageVerifier 校验",
    )


def _first_http_evidence(references: tuple[str, ...]) -> str:
    return next(
        (item for item in references if item.startswith(("https://", "http://"))),
        "",
    )


def project_trip_card(
    intent: TravelIntent,
    graph: PlanGraph,
    contracts: tuple[PriceContract, ...],
    source_statuses: tuple[SourceStatus, ...],
    *,
    captured_at: datetime,
    execution_ready: bool = False,
    unresolved_items: tuple[str, ...] = (),
) -> TripCardProjection:
    activities = tuple(
        PlanComponent(
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
        )
        for anchor in intent.anchors
    )
    components = tuple(item for item in graph.components if item.kind != "anchor")
    activity_price_included = bool(intent.anchors) and all(
        item.provided_price_cny_cents is not None for item in intent.anchors
    )
    source_gap = graph.status == PlanStatus.NO_SOLUTION and not any(
        item.state == SourceState.SUCCEEDED for item in source_statuses
    )
    return TripCardProjection(
        status=(
            TripCardStatus.SOURCE_GAP
            if source_gap
            else TripCardStatus.NO_SOLUTION
            if graph.status == PlanStatus.NO_SOLUTION
            else TripCardStatus.FINAL
            if execution_ready
            else TripCardStatus.CANDIDATE
        ),
        title=(
            "多城市异地进出与固定活动方案"
            if intent.topology == TravelTopology.MULTI_CITY
            else "单目的地完整行程方案"
        ),
        start_date=intent.window.start.date(),
        end_date=intent.window.end.date(),
        city_order=tuple(item.name for item in intent.places),
        traveler_count=intent.travelers,
        total_cny_cents=graph.total_cny_cents,
        components=components,
        fixed_activities=activities,
        price_contracts=contracts,
        activity_price_included=activity_price_included,
        unresolved_items=(
            *intent.unresolved_critical,
            *unresolved_items,
            *(
                ("活动费用未提供，未计入总价。",)
                if intent.anchors and not activity_price_included
                else ()
            ),
        ),
        source_statuses=source_statuses,
        query_captured_at=captured_at,
        source_boundary=graph.claim_boundary,
    )


def project_package_result_trip_card(
    intent: TravelIntent,
    plan: Any,
    *,
    captured_at: datetime,
    execution_ready: bool,
) -> TripCardProjection:
    """Adapt the existing package projection through the shared graph/card gate."""
    components: list[PlanComponent] = []
    option_id = str(plan.option_id)
    contract_id = f"package-projection:{option_id}"
    flight = plan.flight
    if flight is not None:
        components.extend(
            (
                PlanComponent(
                    kind="transport",
                    offer_id=f"{option_id}:flight:outbound",
                    label="去程 " + "/".join(flight.outbound_flight_numbers),
                    provider=flight.provider,
                    start=flight.outbound_depart_at,
                    end=flight.outbound_arrive_at,
                    place_from=flight.origin,
                    place_to=flight.destination,
                    price_contract_id=contract_id,
                    detail_url=flight.official_view_url or "",
                    price_cny_cents=flight.total_for_party_cents,
                    shared_price_contract=True,
                ),
                PlanComponent(
                    kind="transport",
                    offer_id=f"{option_id}:flight:return",
                    label="返程 " + "/".join(flight.return_flight_numbers),
                    provider=flight.provider,
                    start=flight.return_depart_at,
                    end=flight.return_arrive_at,
                    place_from=flight.destination,
                    place_to=flight.origin,
                    price_contract_id=contract_id,
                    detail_url=flight.official_view_url or "",
                    shared_price_contract=True,
                ),
            )
        )
    for index, lodging in enumerate(plan.lodgings):
        components.append(
            PlanComponent(
                kind="stay",
                offer_id=f"{option_id}:stay:{index}",
                label=lodging.property_name,
                provider=lodging.provider,
                start=lodging.check_in,
                end=lodging.check_out,
                place_from=lodging.place_key or lodging.area,
                price_contract_id=contract_id,
                detail_url=lodging.official_view_url or "",
                price_cny_cents=(
                    lodging.total_for_party_cents or lodging.reference_cny_cents
                ),
                shared_price_contract=True,
            )
        )
    for index, transfer in enumerate(plan.transfers):
        components.append(
            PlanComponent(
                kind="transfer",
                offer_id=f"{option_id}:transfer:{index}",
                label=f"{transfer.origin_area}→{transfer.destination_area}",
                provider=transfer.provider,
                start=transfer.depart_at or transfer.service_date,
                end=transfer.arrive_at or transfer.service_date,
                place_from=transfer.origin_place_key or transfer.origin_area,
                place_to=transfer.destination_place_key or transfer.destination_area,
                price_contract_id=contract_id,
                detail_url=transfer.official_view_url or "",
                price_cny_cents=(
                    transfer.total_for_party_cents or transfer.reference_cny_cents
                ),
                shared_price_contract=True,
            )
        )
    total = plan.total_budget_cents or plan.estimated_total_cny_cents
    contract = (
        PriceContract(
            id=contract_id,
            total_for_party_cents=total,
            component_ids=tuple(item.offer_id for item in components),
            shared=len(components) > 1,
            taxes_and_fees_included=execution_ready,
            source="live-package-projection",
        )
        if total is not None and components
        else None
    )
    source_statuses = tuple(
        SourceStatus(
            source_id=source_id,
            provider=source_id.split(":", 1)[0],
            state=SourceState.SUCCEEDED,
            detail="本次查询已提供方案证据",
            captured_at=captured_at,
        )
        for source_id in plan.covered_source_ids
    ) + tuple(
        SourceStatus(
            source_id=source_id,
            provider=source_id.split(":", 1)[0],
            state=SourceState.FAILED,
            detail="本次查询未返回可用结果",
            captured_at=captured_at,
        )
        for source_id in plan.failed_source_ids
    )
    contracts = (contract,) if contract is not None else ()
    graph = PlanGraph(
        status=PlanStatus.FEASIBLE if components else PlanStatus.NO_SOLUTION,
        components=tuple(components),
        total_cny_cents=total,
        counted_price_contract_ids=(contract_id,) if contract is not None else (),
        price_contracts=contracts,
        checked_constraints=("单目的地方案投影合同复算",),
        claim_boundary=plan.claim_boundary,
    )
    validation_errors = validate_plan_graph(graph, contracts)
    validated_execution_ready = execution_ready and not validation_errors
    if validation_errors:
        graph = graph.model_copy(
            update={
                "claim_boundary": (
                    f"{graph.claim_boundary}最终校验未通过："
                    + "；".join(validation_errors)
                )
            }
        )
    return project_trip_card(
        intent,
        graph,
        contracts,
        source_statuses,
        captured_at=captured_at,
        execution_ready=validated_execution_ready,
        unresolved_items=tuple(plan.unresolved_items),
    )


def validate_plan_graph(
    plan: PlanGraph,
    contracts: tuple[PriceContract, ...],
    *,
    intent: TravelIntent | None = None,
    catalog: OfferCatalog | None = None,
) -> tuple[str, ...]:
    """Independent final checks; the solver is not trusted as its own proof."""
    by_id = {item.id: item for item in contracts}
    errors: list[str] = []
    if len(plan.counted_price_contract_ids) != len(set(plan.counted_price_contract_ids)):
        errors.append("价格合同重复计价")
    priced_components = tuple(
        component
        for component in plan.components
        if component.price_contract_id != "user-provided-not-priced"
    )
    referenced_contract_ids = {item.price_contract_id for item in priced_components}
    if referenced_contract_ids != set(plan.counted_price_contract_ids):
        errors.append("选中组件合同集合与计价合同集合不一致")
    for component in priced_components:
        if component.price_contract_id not in by_id:
            errors.append(f"组件缺少价格合同:{component.offer_id}")
            continue
        component_contract = by_id[component.price_contract_id]
        if component_contract.currency != "CNY":
            errors.append(f"价格合同非人民币:{component_contract.id}")
        if not component_contract.taxes_and_fees_included:
            errors.append(f"价格合同税费未闭合:{component_contract.id}")
    components_by_contract: dict[str, set[str]] = {}
    for component in priced_components:
        components_by_contract.setdefault(component.price_contract_id, set()).add(
            component.offer_id
        )
    for contract_id, component_ids in components_by_contract.items():
        coverage_contract: PriceContract | None = by_id.get(contract_id)
        if coverage_contract is None:
            continue
        if set(coverage_contract.component_ids) != component_ids:
            errors.append(f"价格合同组件覆盖不精确:{contract_id}")
        if len(component_ids) > 1 and not coverage_contract.shared:
            errors.append(f"多组件合同未声明共享:{contract_id}")
        if len(component_ids) == 1 and coverage_contract.shared:
            errors.append(f"单组件合同错误声明共享:{contract_id}")
        if len(component_ids) > 1 and catalog is not None:
            declared_bundles = tuple(
                bundle
                for bundle in catalog.bundles
                if bundle.price_contract_id == contract_id
            )
            if declared_bundles and not any(
                set(bundle.component_offer_ids) == component_ids
                for bundle in declared_bundles
            ):
                errors.append(f"Bundle组件覆盖不精确:{contract_id}")
    if intent is not None and catalog is not None:
        transports = tuple(item for item in plan.components if item.kind == "transport")
        if len(transports) != len(intent.route_legs):
            errors.append("交通段数量与需求不一致")
        offers_by_id = {item.id: item for item in catalog.transports}
        selected_transport_offers: list[TransportOffer] = []
        for index, requirement in enumerate(intent.route_legs):
            if index >= len(transports):
                break
            component = transports[index]
            offer = offers_by_id.get(component.offer_id)
            if offer is None:
                errors.append(f"交通报价不存在:{component.offer_id}")
                continue
            selected_transport_offers.append(offer)
            if (offer.origin_place_id, offer.destination_place_id) != (
                requirement.origin_place_id,
                requirement.destination_place_id,
            ):
                errors.append(f"交通地点不匹配:{component.offer_id}")
            if offer.arrival <= offer.departure:
                errors.append(f"交通段到达时间不得早于或等于出发:{component.offer_id}")
            departure_date = offer.departure.date()
            if requirement.departure_date and departure_date != requirement.departure_date:
                errors.append(f"交通日期不匹配:{component.offer_id}")
            if (
                requirement.earliest_departure_date
                and departure_date < requirement.earliest_departure_date
            ):
                errors.append(f"交通早于日期窗:{component.offer_id}")
            if (
                requirement.latest_departure_date
                and departure_date > requirement.latest_departure_date
            ):
                errors.append(f"交通晚于日期窗:{component.offer_id}")
        for previous, current in pairwise(selected_transport_offers):
            if previous.arrival > current.departure:
                errors.append(
                    f"相邻交通时间倒置:{previous.id}->{current.id}"
                )
        if transports and (
            transports[0].start < intent.window.start
            or transports[-1].end > intent.window.end
        ):
            errors.append("交通超出全程时间窗")
        stays = tuple(item for item in plan.components if item.kind == "stay")
        stays_by_place = {item.place_from: item for item in stays}
        stay_offers = {item.id: item for item in catalog.stays}
        for index, place in enumerate(intent.places):
            stay = stays_by_place.get(place.id)
            if stay is None or index + 1 >= len(transports):
                errors.append(f"住宿未覆盖:{place.id}")
                continue
            stay_offer = stay_offers.get(stay.offer_id)
            if stay_offer is None:
                errors.append(f"住宿报价不存在:{stay.offer_id}")
                continue
            arrival = offers_by_id[transports[index].offer_id].arrival.date()
            departure = offers_by_id[transports[index + 1].offer_id].departure.date()
            if stay_offer.check_in > arrival or (
                stay_offer.check_out < departure
            ):
                errors.append(f"住宿日期未覆盖:{stay.offer_id}")
        for anchor in intent.anchors:
            if anchor.end <= anchor.start:
                errors.append(f"固定活动结束时间不得早于或等于开始:{anchor.id}")
                continue
            anchor_index = next(
                (i for i, place in enumerate(intent.places) if place.id == anchor.place_id),
                None,
            )
            if anchor_index is None or anchor_index + 1 >= len(transports):
                errors.append(f"活动地点不存在:{anchor.place_id}")
                continue
            if (
                transports[anchor_index].end
                + timedelta(minutes=intent.minimum_anchor_buffer_minutes)
                > anchor.start
                or anchor.end + timedelta(minutes=intent.minimum_anchor_buffer_minutes)
                > transports[anchor_index + 1].start
            ):
                errors.append(f"活动缓冲不足:{anchor.id}")
        if intent.unresolved_critical:
            errors.extend(f"关键需求待确认:{item}" for item in intent.unresolved_critical)
    if plan.total_cny_cents is not None:
        calculated = sum(
            by_id[item].total_for_party_cents
            for item in plan.counted_price_contract_ids
            if item in by_id
        )
        if calculated != plan.total_cny_cents:
            errors.append("总价与价格合同不一致")
    return tuple(errors)


def is_complex_multi_city_request(text: str) -> bool:
    """Detect a multi-stop route with a fixed time anchor, without city names."""
    dates = re.findall(r"(?:\d{4}[-年])?\d{1,2}(?:[-月/]\d{1,2}日?)?", text)
    stops = re.findall(r"(?:出发到|到|去|→)([\u4e00-\u9fff]{2,12})", text)
    return_match = re.search(r"从([\u4e00-\u9fff]{2,12})返回([\u4e00-\u9fff]{2,12})", text)
    if return_match:
        stops.append(return_match.group(1))
    fixed_time = re.search(r"\d{1,2}:\d{2}", text)
    return len(dates) >= 3 and len(stops) >= 2 and fixed_time is not None


def parse_complex_intent(text: str, *, reference_year: int | None = None) -> TravelIntent | None:
    explicit_year = re.search(r"(\d{4})[-年]", text)
    year = int(explicit_year.group(1)) if explicit_year else reference_year
    if year is None:
        return None
    text = re.sub(r"(?<!\d)(\d{1,2})/(\d{1,2})", rf"{year}-\1-\2", text)
    text = re.sub(
        r"(?<!\d{4}年)(?<!\d)(\d{1,2})月(\d{1,2})日",
        rf"{year}年\1月\2日",
        text,
    )
    dates = re.findall(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})", text)
    if len(dates) < 3:
        return None
    parsed_dates = [date(int(y), int(m), int(d)) for y, m, d in dates]
    origin_match = re.search(
        r"(?:\d{4}[-年]\d{1,2}[-月]\d{1,2}日?\s*)"
        r"从?([\u4e00-\u9fff]{2,12}?)(?=出发)",
        text,
    ) or re.search(
        r"(?:^|[，,；。\s])(?:\d+\s*名?成人)?从?"
        r"([\u4e00-\u9fff]{2,12}?)(?=出发)",
        text,
    )
    if origin_match is None:
        return None
    origin = PlaceRef(
        id=origin_match.group(1), name=origin_match.group(1), city=origin_match.group(1)
    )
    stop_names = re.findall(r"(?:出发到|到|去|→)([\u4e00-\u9fff]{2,12})", text)
    return_match = re.search(r"从([\u4e00-\u9fff]{2,12})返回([\u4e00-\u9fff]{2,12})", text)
    if return_match:
        stop_names.append(return_match.group(1))
    else:
        short_return_place = re.search(
            r"从([\u4e00-\u9fff]{2,12}?)返(?:回)?[\u4e00-\u9fff]{1,8}", text
        )
        if short_return_place:
            stop_names.append(short_return_place.group(1))
    unique_stops = tuple(dict.fromkeys(stop_names))
    if len(unique_stops) < 2:
        return None
    places = tuple(PlaceRef(id=name, name=name, city=name) for name in unique_stops)
    route_places = (origin.id, *unique_stops, origin.id)
    route_event_dates = [
        date(int(y), int(m), int(d))
        for y, m, d, _verb, _place in re.findall(
            r"(\d{4})[-年](\d{1,2})[-月](\d{1,2}).{0,12}?"
            r"(出发到|到|去|从|→)([\u4e00-\u9fff]{2,12}?)(?=返回|[，；。]|$)",
            text,
        )
    ]
    return_short_match = re.search(
        r"(\d{4})[-年](\d{1,2})[-月](\d{1,2}).{0,10}?返(?:回)?[\u4e00-\u9fff]{1,8}",
        text,
    )
    if return_short_match and (
        not route_event_dates
        or route_event_dates[-1]
        != date(*(int(item) for item in return_short_match.groups()))
    ):
        route_event_dates.append(date(*(int(item) for item in return_short_match.groups())))
    route_dates: list[date | None] = list(route_event_dates[: len(route_places) - 1])
    if len(route_dates) == len(route_places) - 2 and route_dates:
        route_dates = [*route_dates[:-1], None, route_dates[-1]]
    route_legs = tuple(
        TripLegRequirement(
            id=f"leg:{index}",
            origin_place_id=route_places[index],
            destination_place_id=route_places[index + 1],
            departure_date=route_dates[index] if index < len(route_dates) else None,
            earliest_departure_date=(
                route_dates[index - 1]
                if index < len(route_dates) and route_dates[index] is None
                else None
            ),
            latest_departure_date=(
                route_dates[index + 1]
                if index + 1 < len(route_dates) and route_dates[index] is None
                else None
            ),
        )
        for index in range(min(len(route_places) - 1, len(route_dates)))
    )
    traveler_match = re.search(r"(\d+)\s*名?成人", text)
    traveler_count = int(traveler_match.group(1)) if traveler_match else 1
    anchor_pattern = (
        r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})[日 ]+"
        r"([0-2]?\d):([0-5]\d)-([0-2]?\d):([0-5]\d)"
        r".{0,20}?在([\u4e00-\u9fff]{2,12}?)有.{0,10}?(演唱会|会议|活动)"
    )
    anchor_match = re.search(anchor_pattern, text)
    anchors: tuple[TripAnchor, ...] = ()
    unresolved_critical: tuple[str, ...] = ()
    if anchor_match:
        y, m, d, h1, n1, h2, n2, place, kind = anchor_match.groups()
        start = datetime(int(y), int(m), int(d), int(h1), int(n1))
        end = datetime(int(y), int(m), int(d), int(h2), int(n2))
        anchors = (
            TripAnchor(
                id=f"anchor:{place}:{start.isoformat()}",
                name=f"已持有{kind}",
                place_id=place,
                start=start,
                end=end,
                traveler_count=traveler_count,
            ),
        )
    elif re.search(
        r"\d{4}[-年]\d{1,2}[-月]\d{1,2}[日 ]+"
        r"[0-2]?\d:[0-5]\d.{0,30}?(?:演唱会|会议|活动)",
        text,
    ):
        unresolved_critical = ("固定活动只有开始时间，结束时间待确认",)
    activity_price_match = re.search(
        r"(?:活动|演唱会|会议).{0,12}?(?:已支付)?(?:总)?(?:费用|票价)?"
        r"\s*(\d+(?:\.\d+)?)\s*(?:元|人民币)",
        text,
    )
    if anchors and activity_price_match is not None:
        anchors = (
            anchors[0].model_copy(
                update={
                    "provided_price_cny_cents": round(
                        float(activity_price_match.group(1)) * 100
                    )
                }
            ),
        )
    buffer_match = re.search(r"至少留(\d+)分钟", text)
    return TravelIntent(
        travelers=traveler_count,
        origin=origin,
        places=places,
        window=TripWindow(
            start=datetime.combine(min(parsed_dates), datetime.min.time()),
            end=datetime.combine(max(parsed_dates), datetime.max.time()),
        ),
        route_legs=route_legs,
        anchors=anchors,
        minimum_anchor_buffer_minutes=int(buffer_match.group(1)) if buffer_match else 0,
        preference_summary="交通和酒店总价尽量低",
        unresolved_critical=unresolved_critical,
    )


def solve_complex_catalog(problem: PlanningProblem) -> PlanGraph:
    """Solve the bounded offer graph directly with CP-SAT selection variables."""
    from ortools.sat.python import cp_model

    intent = problem.intent
    catalog = problem.offer_catalog
    if intent.unresolved_critical:
        return _no_solution_graph(
            problem.price_contracts,
            "关键需求待确认：" + "；".join(intent.unresolved_critical),
        )
    if any(anchor.end <= anchor.start for anchor in intent.anchors):
        return _no_solution_graph(
            problem.price_contracts,
            "固定活动结束时间必须晚于开始时间",
        )
    effective_contracts = list(problem.price_contracts)
    activity_contract_ids: list[str] = []
    for anchor in intent.anchors:
        if anchor.provided_price_cny_cents is None:
            continue
        contract_id = f"user-activity:{anchor.id}"
        activity_contract_ids.append(contract_id)
        effective_contracts.append(
            PriceContract(
                id=contract_id,
                total_for_party_cents=anchor.provided_price_cny_cents,
                component_ids=(anchor.id,),
                source="user-provided",
            )
        )
    contracts = tuple(effective_contracts)
    by_contract = {item.id: item for item in contracts}
    bundle_by_contract = {item.price_contract_id: item for item in catalog.bundles}

    def contract_for_offer(offer: TransportOffer | StayOffer) -> PriceContract | None:
        contract = by_contract.get(offer.price_contract_id)
        if contract is None or offer.id not in contract.component_ids:
            return None
        component_count = len(contract.component_ids)
        if component_count == 1 and contract.shared:
            return None
        if component_count > 1:
            bundle = bundle_by_contract.get(contract.id)
            if (
                not contract.shared
                or (
                    bundle is not None
                    and set(bundle.component_offer_ids) != set(contract.component_ids)
                )
            ):
                return None
        if contract.currency != "CNY" or not contract.taxes_and_fees_included:
            return None
        return contract

    model = cp_model.CpModel()
    transport_vars = {
        item.id: model.NewBoolVar(f"transport:{item.id}")  # type: ignore[attr-defined]
        for item in catalog.transports
    }
    stay_vars = {
        item.id: model.NewBoolVar(f"stay:{item.id}")  # type: ignore[attr-defined]
        for item in catalog.stays
    }
    offer_by_id: dict[str, TransportOffer | StayOffer] = {
        item.id: item for item in catalog.transports
    }
    offer_by_id.update({item.id: item for item in catalog.stays})
    contract_by_offer = {
        offer_id: contract_for_offer(offer)
        for offer_id, offer in offer_by_id.items()
    }
    for offer_id, contract in contract_by_offer.items():
        if contract is None:
            variable = (
                transport_vars[offer_id]
                if offer_id in transport_vars
                else stay_vars.get(offer_id)
            )
            if variable is not None:
                model.Add(variable == 0)  # type: ignore[attr-defined]

    leg_options: list[tuple[TransportOffer, ...]] = []
    for requirement in intent.route_legs:
        leg_slot = tuple(
            offer
            for offer in catalog.transports
            if (offer.origin_place_id, offer.destination_place_id)
            == (requirement.origin_place_id, requirement.destination_place_id)
        )
        if not leg_slot:
            return _no_solution_graph(contracts, "当前来源缺少至少一个必要交通段")
        leg_options.append(leg_slot)
        model.Add(sum(transport_vars[item.id] for item in leg_slot) == 1)  # type: ignore[attr-defined]
        for offer in leg_slot:
            departure_date = offer.departure.date()
            valid = (
                (requirement.departure_date is None or departure_date == requirement.departure_date)
                and (
                    requirement.earliest_departure_date is None
                    or departure_date >= requirement.earliest_departure_date
                )
                and (
                    requirement.latest_departure_date is None
                    or departure_date <= requirement.latest_departure_date
                )
                and offer.departure >= intent.window.start
                and offer.arrival <= intent.window.end
                and offer.arrival > offer.departure
            )
            if not valid:
                model.Add(transport_vars[offer.id] == 0)  # type: ignore[attr-defined]

    stay_options: list[tuple[StayOffer, ...]] = []
    for place in intent.places:
        stay_slot = tuple(item for item in catalog.stays if item.place_id == place.id)
        if not stay_slot:
            return _no_solution_graph(contracts, f"当前来源缺少{place.name}住宿")
        stay_options.append(stay_slot)
        model.Add(sum(stay_vars[item.id] for item in stay_slot) == 1)  # type: ignore[attr-defined]

    for left_options, right_options in pairwise(leg_options):
        for left in left_options:
            for right in right_options:
                if left.arrival > right.departure:
                    model.Add(  # type: ignore[attr-defined]
                        transport_vars[left.id] + transport_vars[right.id] <= 1
                    )
    for index, options in enumerate(stay_options):
        inbound_options = leg_options[index]
        outbound_options = leg_options[index + 1]
        for stay in options:
            for inbound in inbound_options:
                if stay.check_in > inbound.arrival.date():
                    model.Add(  # type: ignore[attr-defined]
                        stay_vars[stay.id] + transport_vars[inbound.id] <= 1
                    )
            for outbound in outbound_options:
                if stay.check_out < outbound.departure.date():
                    model.Add(  # type: ignore[attr-defined]
                        stay_vars[stay.id] + transport_vars[outbound.id] <= 1
                    )
    for anchor in intent.anchors:
        place_index = next(
            (index for index, place in enumerate(intent.places) if place.id == anchor.place_id),
            None,
        )
        if place_index is None:
            return _no_solution_graph(contracts, f"固定活动地点不在路线中:{anchor.place_id}")
        for inbound in leg_options[place_index]:
            if (
                inbound.arrival
                + timedelta(minutes=intent.minimum_anchor_buffer_minutes)
                > anchor.start
            ):
                model.Add(transport_vars[inbound.id] == 0)  # type: ignore[attr-defined]
        for outbound in leg_options[place_index + 1]:
            if (
                anchor.end + timedelta(minutes=intent.minimum_anchor_buffer_minutes)
                > outbound.departure
            ):
                model.Add(transport_vars[outbound.id] == 0)  # type: ignore[attr-defined]

    contract_vars = {
        contract.id: model.NewBoolVar(f"contract:{contract.id}")  # type: ignore[attr-defined]
        for contract in contracts
    }
    for contract in contracts:
        variable = contract_vars[contract.id]
        if contract.id in activity_contract_ids:
            model.Add(variable == 1)  # type: ignore[attr-defined]
            continue
        component_vars = [
            transport_vars[item]
            if item in transport_vars
            else stay_vars[item]
            for item in contract.component_ids
            if item in transport_vars or item in stay_vars
        ]
        if len(component_vars) != len(contract.component_ids):
            model.Add(variable == 0)  # type: ignore[attr-defined]
            continue
        for component_var in component_vars:
            model.Add(variable == component_var)  # type: ignore[attr-defined]
    model.Minimize(  # type: ignore[attr-defined]
        sum(
            contract_vars[item.id] * item.total_for_party_cents
            for item in contracts
        )
    )
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _no_solution_graph(contracts, "当前来源目录没有满足全部约束的连续方案")

    selected_transports = tuple(
        next(item for item in options if solver.Value(transport_vars[item.id]))
        for options in leg_options
    )
    selected_stays = tuple(
        next(item for item in options if solver.Value(stay_vars[item.id]))
        for options in stay_options
    )
    counted_contract_ids = tuple(
        item.id for item in contracts if solver.Value(contract_vars[item.id])
    )
    transport_components = tuple(
        _offer_component(item, by_contract[item.price_contract_id])
        for item in selected_transports
    )
    stay_components = tuple(
        _offer_component(item, by_contract[item.price_contract_id])
        for item in selected_stays
    )
    components = transport_components + stay_components + tuple(
        PlanComponent(
            kind="anchor",
            offer_id=item.id,
            label=item.name,
            provider="user-provided",
            start=item.start,
            end=item.end,
            place_from=item.place_id,
            price_contract_id=(
                f"user-activity:{item.id}"
                if item.provided_price_cny_cents is not None
                else "user-provided-not-priced"
            ),
            detail_url="",
            price_cny_cents=item.provided_price_cny_cents,
        )
        for item in intent.anchors
    )
    graph = PlanGraph(
        status=(
            PlanStatus.OPTIMAL_IN_CATALOG
            if status == cp_model.OPTIMAL
            else PlanStatus.FEASIBLE
        ),
        components=components,
        total_cny_cents=sum(
            by_contract[item].total_for_party_cents for item in counted_contract_ids
        ),
        counted_price_contract_ids=counted_contract_ids,
        price_contracts=contracts,
        checked_constraints=(
            "逐段地点与日期窗",
            "交通时间连续",
            "住宿覆盖",
            "固定活动缓冲",
            "Bundle与价格合同唯一计价",
        ),
        claim_boundary=(
            "仅对当前有界来源目录求得最优；不是实时全网最低价或锁价"
            if status == cp_model.OPTIMAL
            else "当前有界来源目录内找到可行解，但未证明目录内最优；不是锁价"
        ),
    )
    errors = validate_plan_graph(graph, contracts, intent=intent, catalog=catalog)
    if errors:
        return _no_solution_graph(contracts, "最终校验未通过：" + "；".join(errors))
    return graph


def _offer_component(
    offer: TransportOffer | StayOffer,
    contract: PriceContract,
) -> PlanComponent:
    shared = len(contract.component_ids) > 1
    if isinstance(offer, TransportOffer):
        return PlanComponent(
            kind="transport",
            offer_id=offer.id,
            label=offer.label,
            provider=offer.provider,
            start=offer.departure,
            end=offer.arrival,
            place_from=offer.origin_place_id,
            place_to=offer.destination_place_id,
            price_contract_id=contract.id,
            detail_url=offer.detail_url,
            price_cny_cents=None if shared else contract.total_for_party_cents,
            shared_price_contract=shared,
        )
    return PlanComponent(
        kind="stay",
        offer_id=offer.id,
        label=offer.label,
        provider=offer.provider,
        start=offer.check_in,
        end=offer.check_out,
        place_from=offer.place_id,
        price_contract_id=contract.id,
        detail_url=offer.detail_url,
        price_cny_cents=None if shared else contract.total_for_party_cents,
        shared_price_contract=shared,
    )


def _no_solution_graph(
    contracts: tuple[PriceContract, ...],
    boundary: str,
) -> PlanGraph:
    return PlanGraph(
        status=PlanStatus.NO_SOLUTION,
        price_contracts=contracts,
        claim_boundary=boundary,
    )
