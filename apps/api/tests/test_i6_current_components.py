from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.auth import Principal, get_principal
from tripchord.main import app
from tripchord.planning.complex_trip import (
    ActivityOffer,
    ComplexCatalogSolver,
    OfferCatalog,
    PlanningCompiler,
    PlanStatus,
    PriceContract,
    SourceState,
    SourceStatus,
    StayOffer,
    TransportOffer,
    parse_complex_intent,
    validate_plan_graph,
)
from tripchord.providers.fx_reference import UsdCnyReferenceRate
from tripchord.providers.icom_complex import IComCurrentTransportSource
from tripchord.providers.icom_transfer import (
    IComAvailabilityStatus,
    IComPublishedBaseFare,
    IComTransferOption,
    IComTransferQuery,
    IComTransferSearchResult,
)

TEXT = (
    "2名成人，2026-08-30 从马累机场乘轮渡到马富施；"
    "2026-08-31 在马富施安排在线浮潜活动；"
    "2026-09-02 从马富施乘轮渡返回马累机场；住宿3晚，价格优先。"
)
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _intent() -> Any:
    result = parse_complex_intent(TEXT, reference_year=2026)
    assert result is not None
    return result


def _rate() -> UsdCnyReferenceRate:
    return UsdCnyReferenceRate(
        rate_date=date(2026, 8, 20),
        captured_at=NOW,
        usd_per_eur=Decimal("1"),
        cny_per_eur=Decimal("7"),
        response_sha256="a" * 64,
    )


def _icom_option(
    *,
    query: IComTransferQuery,
    trip_id: int,
    amount: str,
) -> IComTransferOption:
    departure = datetime(2026, 8, 30, 7 + trip_id % 2, 30, tzinfo=NOW.tzinfo)
    arrival = departure + timedelta(minutes=45)
    fare = IComPublishedBaseFare.model_construct(
        amount=Decimal(amount),
        currency="USD",
        basis="per_person",
        taxes_included=None,
        evidence=(),
    )
    return IComTransferOption.model_construct(
        trip_id=trip_id,
        schedule_id=trip_id,
        service_name="Fixture ferry",
        vessel_name="Fixture vessel",
        origin=query.origin,
        destination=query.destination,
        route=f"{query.origin.value} -> {query.destination.value}",
        departure_at=departure,
        arrival_at=arrival,
        capacity=20,
        remaining_capacity=20,
        stops=0,
        is_cancelled=False,
        availability_status=IComAvailabilityStatus.AVAILABLE,
        eligible_for_party=True,
        fare=fare,
        currency_policy_evidence=None,
        source_url="https://fixture.test/ferry",
        captured_at=NOW,
        evidence=(),
    )


class _DifferentFareProvider:
    async def search(
        self,
        query: IComTransferQuery,
        *,
        query_task_id: str | None = None,
    ) -> IComTransferSearchResult:
        del query_task_id
        return IComTransferSearchResult.model_construct(
            query=query,
            searched_at=NOW,
            options=(
                _icom_option(query=query, trip_id=1, amount="30"),
                _icom_option(query=query, trip_id=2, amount="45"),
            ),
            source_urls=(
                "https://fixture.test/schedules",
                "https://fixture.test/fare",
                "https://fixture.test/policy",
            ),
        )

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_each_ferry_option_gets_its_own_party_price_contract() -> None:
    intent = _intent()
    source = IComCurrentTransportSource(provider=_DifferentFareProvider())
    transports, contracts, statuses, _ = await source.catalog_for(
        intent,
        reference_rate=_rate(),
        fetch_reference=False,
    )

    assert statuses[0].state == SourceState.SUCCEEDED
    assert len(transports) == 4
    by_offer = {item.id: item for item in contracts}
    assert len({item.total_for_party_cents for item in contracts}) == 2
    for offer in transports:
        contract = by_offer[offer.price_contract_id]
        assert contract.component_ids == (offer.id,)
        assert contract.original_currency == "USD"
        assert contract.original_total_for_party_cents in {6_000, 9_000}
        assert contract.total_for_party_cents == contract.original_total_for_party_cents * 7


def _frozen_activity_catalog() -> tuple[Any, OfferCatalog, tuple[PriceContract, ...]]:
    intent = _intent()
    transport_specs = (
        (
            "f-out",
            "马累机场",
            "马富施",
            "2026-08-30T07:30:00+05:00",
            "2026-08-30T08:15:00+05:00",
            10_000,
        ),
        (
            "f-back",
            "马富施",
            "马累机场",
            "2026-09-02T06:30:00+05:00",
            "2026-09-02T07:15:00+05:00",
            11_000,
        ),
    )
    transports: list[TransportOffer] = []
    contracts: list[PriceContract] = []
    for offer_id, origin, destination, start, end, amount in transport_specs:
        contract_id = f"{offer_id}:price"
        transports.append(
            TransportOffer(
                id=offer_id,
                provider="fixture",
                origin_place_id=origin,
                destination_place_id=destination,
                departure=datetime.fromisoformat(start),
                arrival=datetime.fromisoformat(end),
                price_contract_id=contract_id,
                detail_url=f"https://fixture.test/{offer_id}",
                label=offer_id,
                participant_ids=("traveler:1", "traveler:2"),
                party_capacity_confirmed=True,
                available_units=2,
                mode="ferry",
            )
        )
        contracts.append(
            PriceContract(
                id=contract_id,
                total_for_party_cents=amount,
                component_ids=(offer_id,),
                currency="CNY",
                taxes_and_fees_included=True,
                source="fixture",
            )
        )
    stay = StayOffer(
        id="stay",
        provider="fixture",
        place_id="马富施",
        check_in=date(2026, 8, 30),
        check_out=date(2026, 9, 2),
        price_contract_id="stay:price",
        detail_url="https://fixture.test/stay",
        label="测试酒店",
        participant_ids=("traveler:1", "traveler:2"),
        confirmed_traveler_count=2,
        confirmed_room_count=1,
    )
    contracts.append(
        PriceContract(
            id="stay:price",
            total_for_party_cents=137_200,
            component_ids=("stay",),
            currency="CNY",
            taxes_and_fees_included=True,
            source="fixture",
        )
    )
    activity = ActivityOffer(
        id="activity-fish-tank",
        provider="fixture",
        place_id="马富施",
        start=datetime.fromisoformat("2026-08-31T09:30:00+05:00"),
        end=datetime.fromisoformat("2026-08-31T14:45:00+05:00"),
        price_contract_id="activity:price",
        detail_url="https://fixture.test/fish-tank",
        label="Fish Tank 浮潜",
        participant_ids=("traveler:1", "traveler:2"),
        party_capacity_confirmed=True,
        available_units=10,
        original_currency="USD",
        original_price_for_party_cents=14_000,
    )
    contracts.append(
        PriceContract(
            id="activity:price",
            total_for_party_cents=94_077,
            component_ids=(activity.id,),
            currency="CNY",
            taxes_and_fees_included=True,
            source="fixture",
        )
    )
    status = SourceStatus(
        source_id="fixture:i6",
        provider="fixture",
        state=SourceState.SUCCEEDED,
        detail="fixture current-shaped sources",
        query_task_ids=("ferry-out", "ferry-back", "stay", "activity"),
        captured_at=NOW,
    )
    catalog = OfferCatalog(
        transports=tuple(transports),
        stays=(stay,),
        activities=(activity,),
        query_tasks=status.query_task_ids,
        source_statuses=(status,),
        source_mode="current",
    )
    return intent, catalog, tuple(contracts)


@pytest.mark.asyncio
async def test_formal_entrypoint_preserves_activity_date_and_link() -> None:
    _intent, catalog, contracts = _frozen_activity_catalog()

    class FrozenProvider:
        def catalog_for(self, _intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return catalog, contracts

    previous = getattr(app.state, "complex_offer_provider", None)
    app.state.complex_offer_provider = FrozenProvider()
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="i6-frozen-e2e",
        auth_mode="static-token",
    )
    try:
        payload = {
            "requirement": {"text": TEXT, "reference_date": "2026-08-20"},
            "coverage_mode": "strict",
            "timeout_seconds": 60,
            "total_timeout_seconds": 120,
            "max_pairs": 1,
        }
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51371)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=payload,
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.state.complex_offer_provider = previous

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["interpretation"] is None
    assert body["model_trace_count"] == 0
    assert body["travel_intent"]["route_legs"] == [
        {
            "id": "leg:0",
            "origin_place_id": "马累机场",
            "destination_place_id": "马富施",
            "departure_date": "2026-08-30",
            "earliest_departure_date": None,
            "latest_departure_date": None,
            "participant_ids": [],
        },
        {
            "id": "leg:1",
            "origin_place_id": "马富施",
            "destination_place_id": "马累机场",
            "departure_date": "2026-09-02",
            "earliest_departure_date": None,
            "latest_departure_date": None,
            "participant_ids": [],
        },
    ]
    assert body["travel_intent"]["activity_requirements"][0]["activity_date"] == "2026-08-31"
    card = body["trip_card"]
    assert card["status"] == "final"
    assert card["total_cny_cents"] == 252_277
    assert [item["kind"] for item in card["components"]] == [
        "transport",
        "transport",
        "stay",
        "activity",
    ]
    activity_component = card["components"][-1]
    assert activity_component["start"].startswith("2026-08-31")
    assert activity_component["detail_url"] == "https://fixture.test/fish-tank"
    assert body["source_statuses"][0]["state"] == "succeeded"
    assert body["personalization"]["pareto_candidate_count"] == 1
    assert body["personalization"]["selection_mode"] == "single"
    assert {
        item["id"] for item in card["price_contracts"]
    } == {"activity:price", "f-back:price", "f-out:price", "stay:price"}


@pytest.mark.asyncio
async def test_formal_entrypoint_rejects_activity_before_inbound_arrival() -> None:
    intent, catalog, contracts = _frozen_activity_catalog()
    late_inbound = next(
        item for item in catalog.transports if item.id == "f-out"
    ).model_copy(
        update={
            "arrival": datetime.fromisoformat(
                "2026-08-31T12:00:00+05:00"
            )
        }
    )
    late_catalog = catalog.model_copy(
        update={
            "transports": tuple(
                late_inbound if item.id == "f-out" else item
                for item in catalog.transports
            )
        }
    )
    late_graph = ComplexCatalogSolver().solve(
        PlanningCompiler().compile_problem(
            intent,
            offer_catalog=late_catalog,
            price_contracts=contracts,
        )
    )
    assert late_graph.status == PlanStatus.NO_SOLUTION
    valid_graph = ComplexCatalogSolver().solve(
        PlanningCompiler().compile_problem(
            intent,
            offer_catalog=catalog,
            price_contracts=contracts,
        )
    )
    connectivity_errors = validate_plan_graph(
        valid_graph,
        valid_graph.price_contracts,
        intent=intent,
        catalog=late_catalog,
    )
    assert any("在线活动早于到达" in item for item in connectivity_errors)

    class LateInboundProvider:
        def catalog_for(
            self, _intent: Any
        ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
            return late_catalog, contracts

    previous = getattr(app.state, "complex_offer_provider", None)
    app.state.complex_offer_provider = LateInboundProvider()
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="i6-activity-connectivity-e2e",
        auth_mode="static-token",
    )
    try:
        payload = {
            "requirement": {"text": TEXT, "reference_date": "2026-08-20"},
            "coverage_mode": "strict",
            "timeout_seconds": 60,
            "total_timeout_seconds": 120,
            "max_pairs": 1,
        }
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51372)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=payload,
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
        app.state.complex_offer_provider = previous

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_trace_count"] == 0
    card = body["trip_card"]
    assert card["status"] == "no_solution"
    assert card["total_cny_cents"] is None
    assert "满足全部约束" in card["source_boundary"]
