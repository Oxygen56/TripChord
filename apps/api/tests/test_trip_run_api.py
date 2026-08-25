from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.auth import Principal, get_principal
from tripchord.main import LiveRunCache, app
from tripchord.persistence.trip_runs import InMemoryTripRunStore
from tripchord.planning.complex_trip import (
    OfferCatalog,
    PlanGraph,
    PriceContract,
    SourceState,
    SourceStatus,
    StayOffer,
    TransportOffer,
)
from tripchord.planning.trip_run import (
    ComplexDependencyKind,
    TripRun,
    build_complex_plan_dependencies,
)

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
BASE_TEXT = (
    "2名成人，2026-08-30 杭州出发到南京，2026-09-01 去上海，"
    "2026-09-04 从上海返回杭州；2026-08-31 19:00-21:30 "
    "在南京有已持有活动，活动费用未提供；交通和酒店总价尽量低，"
    "活动前至少留90分钟缓冲；这次价格优先。"
)


def _payload(text: str) -> dict[str, Any]:
    return {
        "requirement": {"text": text, "reference_date": "2026-08-26"},
        "coverage_mode": "strict",
        "timeout_seconds": 60,
        "total_timeout_seconds": 120,
        "max_pairs": 1,
    }


class TripRunFixtureProvider:
    def __init__(self) -> None:
        self.full_calls = 0
        self.stay_calls = 0
        self.fail_stay_replacement = False
        self._base_stays: tuple[StayOffer, ...] = ()

    def _catalog(self, intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        transports: list[TransportOffer] = []
        contracts: list[PriceContract] = []
        for index, requirement in enumerate(intent.route_legs):
            departure = datetime.combine(
                requirement.departure_date,
                datetime.min.time(),
                tzinfo=UTC,
            ).replace(hour=8 + index)
            offer_id = f"fixture:transport:{index}"
            contract_id = f"fixture:transport-contract:{index}"
            transports.append(
                TransportOffer(
                    id=offer_id,
                    provider="fixture-rail",
                    origin_place_id=requirement.origin_place_id,
                    destination_place_id=requirement.destination_place_id,
                    departure=departure,
                    arrival=departure + timedelta(hours=1),
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"交通段{index + 1}",
                    party_capacity_confirmed=True,
                    available_units=2,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=10_000 + index * 1_000,
                    component_ids=(offer_id,),
                    source=f"fixture://price/{offer_id}",
                )
            )

        stays: list[StayOffer] = []
        for index, place in enumerate(intent.places):
            check_in = date(2026, 8, 30) if index == 0 else date(2026, 9, 1)
            check_out = date(2026, 9, 1) if index == 0 else date(2026, 9, 4)
            offer_id = f"fixture:stay:{place.id}"
            contract_id = f"fixture:stay-contract:{place.id}"
            stays.append(
                StayOffer(
                    id=offer_id,
                    provider="fixture-hotel",
                    place_id=place.id,
                    check_in=check_in,
                    check_out=check_out,
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"{place.name}共享酒店",
                    confirmed_traveler_count=2,
                    confirmed_room_count=1,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=40_000 + index * 10_000,
                    component_ids=(offer_id,),
                    source=f"fixture://price/{offer_id}",
                )
            )
        query_tasks = tuple(
            [f"fixture:transport:{item.id}" for item in intent.route_legs]
            + [f"fixture:stay:{item.id}" for item in intent.places]
        )
        statuses = tuple(
            SourceStatus(
                source_id=task,
                provider="fixture-current",
                state=SourceState.SUCCEEDED,
                detail="冻结当前来源快照",
                query_task_ids=(task,),
                captured_at=NOW,
            )
            for task in query_tasks
        )
        catalog = OfferCatalog(
            transports=tuple(transports),
            stays=tuple(stays),
            query_tasks=query_tasks,
            source_statuses=statuses,
            source_mode="current",
        )
        self._base_stays = tuple(stays)
        return catalog, tuple(contracts)

    def catalog_for(self, intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        self.full_calls += 1
        return self._catalog(intent)

    def catalog_for_stays(
        self,
        intent: Any,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        self.stay_calls += 1
        if self.fail_stay_replacement:
            return OfferCatalog(
                query_tasks=("fixture:scoped-stays",),
                source_statuses=(
                    SourceStatus(
                        source_id="fixture:scoped-stays",
                        provider="fixture-current",
                        state=SourceState.SUCCEEDED,
                        detail="当前没有替代住宿",
                        query_task_ids=("fixture:scoped-stays",),
                        captured_at=NOW,
                    ),
                ),
                source_mode="current",
            ), ()
        requirements = tuple(intent.stay_requirements)
        stays: list[StayOffer] = []
        contracts: list[PriceContract] = []
        for original in self._base_stays:
            if requirements and not any(
                item.place_id == original.place_id
                and item.check_in <= original.check_in
                and item.check_out >= original.check_out
                for item in requirements
            ):
                continue
            offer_id = f"{original.id}:replacement"
            contract_id = f"{original.price_contract_id}:replacement"
            stays.append(
                original.model_copy(
                    update={
                        "id": offer_id,
                        "price_contract_id": contract_id,
                        "label": original.label + "替代",
                        "detail_url": f"fixture://{offer_id}",
                    }
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=35_000,
                    component_ids=(offer_id,),
                    source=f"fixture://price/{offer_id}",
                )
            )
        task = "fixture:scoped-stays"
        return (
            OfferCatalog(
                stays=tuple(stays),
                query_tasks=(task,),
                source_statuses=(
                    SourceStatus(
                        source_id=task,
                        provider="fixture-current",
                        state=SourceState.SUCCEEDED,
                        detail="仅查询受影响住宿段",
                        query_task_ids=(task,),
                        captured_at=NOW,
                    ),
                ),
                source_mode="current",
            ),
            tuple(contracts),
        )


class GroupTripRunFixtureProvider:
    def __init__(self) -> None:
        self.stay_calls = 0
        self._catalog: tuple[OfferCatalog, tuple[PriceContract, ...]] | None = None

    def catalog_for(self, intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        transports: list[TransportOffer] = []
        stays: list[StayOffer] = []
        contracts: list[PriceContract] = []
        query_tasks: list[str] = []
        for index, requirement in enumerate(intent.route_legs):
            assert requirement.departure_date is not None
            offer_id = f"group:transport:{index}"
            contract_id = f"group:transport-contract:{index}"
            departure = datetime.combine(
                requirement.departure_date,
                datetime.min.time(),
                tzinfo=UTC,
            ).replace(hour=8 + index)
            transports.append(
                TransportOffer(
                    id=offer_id,
                    provider="fixture-rail",
                    origin_place_id=requirement.origin_place_id,
                    destination_place_id=requirement.destination_place_id,
                    departure=departure,
                    arrival=departure + timedelta(hours=1),
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"{requirement.origin_place_id}→{requirement.destination_place_id}",
                    participant_ids=requirement.participant_ids,
                    party_capacity_confirmed=True,
                    available_units=2,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=10_000,
                    component_ids=(offer_id,),
                    covered_traveler_ids=requirement.participant_ids,
                    source=f"fixture://price/{offer_id}",
                )
            )
            query_tasks.append(f"group:transport:{requirement.id}")
        for index, requirement in enumerate(intent.stay_requirements):
            offer_id = f"group:stay:{index}"
            contract_id = f"group:stay-contract:{index}"
            stays.append(
                StayOffer(
                    id=offer_id,
                    provider="fixture-hotel",
                    place_id=requirement.place_id,
                    check_in=requirement.check_in,
                    check_out=requirement.check_out,
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"上海共享酒店{index + 1}",
                    participant_ids=requirement.participant_ids,
                    confirmed_traveler_count=len(requirement.participant_ids),
                    confirmed_room_count=1,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=30_000,
                    component_ids=(offer_id,),
                    covered_traveler_ids=requirement.participant_ids,
                    shared_between_travelers=len(requirement.participant_ids) > 1,
                    source=f"fixture://price/{offer_id}",
                )
            )
            query_tasks.append(f"group:stay:{requirement.id}")
        statuses = tuple(
            SourceStatus(
                source_id=task,
                provider="fixture-group-current",
                state=SourceState.SUCCEEDED,
                detail="冻结多人来源快照",
                query_task_ids=(task,),
                captured_at=NOW,
            )
            for task in query_tasks
        )
        catalog = OfferCatalog(
            transports=tuple(transports),
            stays=tuple(stays),
            query_tasks=tuple(query_tasks),
            source_statuses=statuses,
            source_mode="current",
        )
        self._catalog = catalog, tuple(contracts)
        return self._catalog

    def catalog_for_stays(self, intent: Any) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        self.stay_calls += 1
        assert self._catalog is not None
        original_catalog, _ = self._catalog
        stays: list[StayOffer] = []
        contracts: list[PriceContract] = []
        for index, requirement in enumerate(intent.stay_requirements):
            offer_id = f"group:replacement-stay:{index}:{requirement.place_id}"
            contract_id = f"group:replacement-contract:{index}"
            stays.append(
                StayOffer(
                    id=offer_id,
                    provider="fixture-hotel",
                    place_id=requirement.place_id,
                    check_in=requirement.check_in,
                    check_out=requirement.check_out,
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label="上海重新报价酒店",
                    participant_ids=requirement.participant_ids,
                    confirmed_traveler_count=len(requirement.participant_ids),
                    confirmed_room_count=1,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=25_000,
                    component_ids=(offer_id,),
                    covered_traveler_ids=requirement.participant_ids,
                    shared_between_travelers=len(requirement.participant_ids) > 1,
                    source=f"fixture://price/{offer_id}",
                )
            )
        task = "group:scoped-stays"
        status = SourceStatus(
            source_id=task,
            provider="fixture-group-current",
            state=SourceState.SUCCEEDED,
            detail="只重查受影响共享住宿",
            query_task_ids=(task,),
            captured_at=NOW,
        )
        return (
            OfferCatalog(
                stays=tuple(stays),
                query_tasks=(task,),
                source_statuses=(status,),
                source_mode=original_catalog.source_mode,
            ),
            tuple(contracts),
        )


@pytest.mark.asyncio
async def test_trip_run_create_modify_event_and_failed_change_keep_active_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TripRunFixtureProvider()
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.setattr(app.state, "trip_run_store", InMemoryTripRunStore())
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5)),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="trip-run-e2e", auth_mode="static-token"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52101)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT),
            )
            assert created.status_code == 200, created.text
            initial = created.json()
            assert initial["trip_card"]["status"] == "final"
            assert initial["trip_run"]["active_plan_version_id"].endswith(":v1")
            run_id = initial["trip_run"]["id"]
            assert provider.full_calls == 1
            initial_dependencies = initial["trip_run"]["plan_versions"][0][
                "dependencies"
            ]
            assert {
                item["kind"] for item in initial_dependencies
            } >= {
                "temporal",
                "location",
                "traveler",
                "lodging",
                "fixed_activity",
            }

            fetched = await client.get(f"/api/v1/trip-runs/{run_id}")
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["active_plan_version_id"].endswith(":v1")

            modified = await client.post(
                f"/api/v1/trip-runs/{run_id}/modify",
                json={
                    "text": (
                        "保留所有车次，只把2026-08-30至2026-09-01的共享酒店"
                        "换成另一家当前可用且价格最低的酒店"
                    )
                },
            )
            assert modified.status_code == 200, modified.text
            changed = modified.json()
            assert changed["status"] == "applied"
            assert len(changed["trip_run"]["plan_versions"]) == 2
            assert changed["diff"]["external_query_count"] == 1
            assert changed["diff"]["delta_cny_cents"] == -5_000
            assert {
                item["kind"] for item in changed["diff"]["kept"]
            } >= {"transport", "anchor"}
            assert "lodging" in set(
                changed["trip_run"]["change_history"][-1]["impact"][
                    "traversed_dependency_kinds"
                ]
            )
            assert any(
                item["kind"] == "fixed_activity"
                for item in changed["trip_run"]["plan_versions"][0][
                    "dependencies"
                ]
            )
            assert provider.stay_calls == 1
            active_v2 = changed["trip_run"]["active_plan_version_id"]

            selected_stay = next(
                item
                for item in changed["active_plan_version"]["selected_plan_graph"]["components"]
                if item["kind"] == "stay" and item["place_from"] == "南京"
            )
            provider.fail_stay_replacement = True
            unavailable = await client.post(
                f"/api/v1/trip-runs/{run_id}/events",
                json={
                    "kind": "stay_unavailable",
                    "target_offer_id": selected_stay["offer_id"],
                    "source_ref": "fixture://connector",
                },
            )
            assert unavailable.status_code == 200, unavailable.text
            failed = unavailable.json()
            assert failed["status"] == "needs_scope_expansion"
            assert failed["trip_run"]["active_plan_version_id"] == active_v2
            assert len(failed["trip_run"]["plan_versions"]) == 2
            assert failed["trip_run"]["change_history"][-1]["status"] == (
                "needs_scope_expansion"
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)


@pytest.mark.asyncio
async def test_anonymous_dependencies_include_shared_contract_and_anchor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anonymous aggregate travelers still propagate shared-price changes."""

    provider = TripRunFixtureProvider()
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.setattr(app.state, "trip_run_store", InMemoryTripRunStore())
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5)),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="trip-run-dependency", auth_mode="static-token"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52103)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT),
            )
            assert created.status_code == 200, created.text
            trip_run = TripRun.model_validate(created.json()["trip_run"])
            graph = trip_run.active_version().selected_plan_graph
            assert graph is not None
            components = list(graph.components)
            assert len(components) >= 2
            shared_contract_id = "fixture:shared-transport-contract"
            components[0] = components[0].model_copy(
                update={"price_contract_id": shared_contract_id}
            )
            components[1] = components[1].model_copy(
                update={"price_contract_id": shared_contract_id}
            )
            shared_graph = PlanGraph.model_validate(
                graph.model_copy(update={"components": tuple(components)})
            )
            dependencies = build_complex_plan_dependencies(
                trip_run.original_intent,
                shared_graph,
            )
            assert any(
                item.kind == ComplexDependencyKind.PRICE_CONTRACT
                for item in dependencies
            )
            assert any(
                item.kind == ComplexDependencyKind.FIXED_ACTIVITY
                for item in dependencies
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)


@pytest.mark.asyncio
async def test_trip_run_withdraws_one_traveler_and_reprices_shared_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = GroupTripRunFixtureProvider()
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.setattr(app.state, "trip_run_store", InMemoryTripRunStore())
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5)),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="trip-run-withdraw", auth_mode="static-token"
    )
    text = (
        "旅行者甲从杭州、旅行者乙从南京分别于2026-08-30出发到上海汇合，共住一间房；"
        "甲参加2026-08-31 19:00-21:00在上海的已持有活动，乙不参加；"
        "乙2026-09-02从上海返回南京，甲2026-09-04从上海返回杭州。"
        "两人均为成人，交通和酒店总价尽量低，活动费用未提供。"
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52102)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(text),
            )
            assert created.status_code == 200, created.text
            initial = created.json()
            assert initial["trip_card"]["status"] == "final"
            run_id = initial["trip_run"]["id"]
            withdrawn = await client.post(
                f"/api/v1/trip-runs/{run_id}/modify",
                json={"text": "乙退出行程"},
            )
            assert withdrawn.status_code == 200, withdrawn.text
            body = withdrawn.json()
            assert body["status"] == "applied"
            card = body["active_plan_version"]["selected_trip_card"]
            assert card["status"] == "final"
            assert card["traveler_count"] == 1
            assert provider.stay_calls == 1
            assert body["diff"]["delta_cny_cents"] is not None
            assert any(
                item["traveler_name"] == "乙" and item["status"] == "removed"
                for item in body["diff"]["traveler_changes"]
            )
            assert all(
                item["traveler_name"] == "甲"
                or item["status"] == "removed"
                for item in body["diff"]["traveler_changes"]
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
