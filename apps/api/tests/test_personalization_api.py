from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import tripchord.main as main_module
from httpx import ASGITransport, AsyncClient
from tripchord.agents.models import AgentRole
from tripchord.agents.persistent_memory import PersistentMemoryStore
from tripchord.auth import Principal, get_principal
from tripchord.main import LiveRunCache, app
from tripchord.planning.complex_trip import (
    OfferCatalog,
    PlanningCompiler,
    PlanPreferenceMode,
    PriceContract,
    SourceState,
    SourceStatus,
    StayOffer,
    TransportOffer,
)
from tripchord.planning.personalization import (
    AgentContextManifest,
    AgentNeedRouter,
    AgentProposalResult,
    AgentSelectionProposal,
    personalize_complex_problem,
    validate_agent_selection_proposal,
)

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
BASE_TEXT = (
    "2名成人，2026-08-30 杭州出发到南京，2026-09-01 去上海，"
    "2026-09-04 从上海返回杭州；2026-08-31 19:00-21:30 "
    "在南京有已持有活动，活动费用未提供；活动前至少留90分钟缓冲。"
)


def _payload(text: str) -> dict[str, object]:
    return {
        "requirement": {"text": text, "reference_date": "2026-08-26"},
        "coverage_mode": "strict",
        "timeout_seconds": 300,
        "total_timeout_seconds": 600,
        "max_pairs": 1,
    }


class CountingTradeoffProvider:
    def __init__(self) -> None:
        self.calls = 0

    def catalog_for(
        self,
        intent: Any,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        self.calls += 1
        transports: list[TransportOffer] = []
        contracts: list[PriceContract] = []
        first_leg_options = (
            ("saver", "05:30", "09:30", 20_000),
            ("balanced", "08:00", "10:30", 25_000),
            ("experience", "09:30", "11:00", 32_000),
        )
        for suffix, departure_time, arrival_time, amount in first_leg_options:
            offer_id = f"rail:first:{suffix}"
            contract_id = f"contract:first:{suffix}"
            transports.append(
                TransportOffer(
                    id=offer_id,
                    provider="frozen-rail",
                    origin_place_id=intent.route_legs[0].origin_place_id,
                    destination_place_id=intent.route_legs[0].destination_place_id,
                    departure=datetime.fromisoformat(
                        f"2026-08-30T{departure_time}:00+08:00"
                    ),
                    arrival=datetime.fromisoformat(
                        f"2026-08-30T{arrival_time}:00+08:00"
                    ),
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"首段交通 {suffix}",
                    party_capacity_confirmed=True,
                    available_units=2,
                    transfer_count=1 if suffix == "saver" else 0,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=amount,
                    component_ids=(offer_id,),
                    source=f"fixture://price/{suffix}",
                )
            )
        for index, (departure, arrival, amount) in enumerate(
            (
                (
                    "2026-09-01T10:00:00+08:00",
                    "2026-09-01T11:00:00+08:00",
                    30_000,
                ),
                (
                    "2026-09-04T10:00:00+08:00",
                    "2026-09-04T11:00:00+08:00",
                    20_000,
                ),
            ),
            start=1,
        ):
            requirement = intent.route_legs[index]
            offer_id = f"rail:fixed:{index}"
            contract_id = f"contract:fixed:{index}"
            transports.append(
                TransportOffer(
                    id=offer_id,
                    provider="frozen-rail",
                    origin_place_id=requirement.origin_place_id,
                    destination_place_id=requirement.destination_place_id,
                    departure=datetime.fromisoformat(departure),
                    arrival=datetime.fromisoformat(arrival),
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"固定交通段{index}",
                    party_capacity_confirmed=True,
                    available_units=2,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=amount,
                    component_ids=(offer_id,),
                    source=f"fixture://price/fixed/{index}",
                )
            )
        stays: list[StayOffer] = []
        for index, (place, check_in, check_out, amount) in enumerate(
            (
                ("南京", date(2026, 8, 30), date(2026, 9, 1), 40_000),
                ("上海", date(2026, 9, 1), date(2026, 9, 4), 50_000),
            )
        ):
            offer_id = f"stay:{index}"
            contract_id = f"contract:stay:{index}"
            stays.append(
                StayOffer(
                    id=offer_id,
                    provider="frozen-hotel",
                    place_id=place,
                    check_in=check_in,
                    check_out=check_out,
                    price_contract_id=contract_id,
                    detail_url=f"fixture://{offer_id}",
                    label=f"{place}住宿",
                    confirmed_traveler_count=2,
                    confirmed_room_count=1,
                )
            )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    total_for_party_cents=amount,
                    component_ids=(offer_id,),
                    source=f"fixture://price/stay/{index}",
                )
            )
        tasks = tuple(
            [f"fixture:leg:{item.id}" for item in intent.route_legs]
            + ["fixture:stay:南京", "fixture:stay:上海"]
        )
        catalog = OfferCatalog(
            transports=tuple(transports),
            stays=tuple(stays),
            query_tasks=tasks,
            source_statuses=tuple(
                SourceStatus(
                    source_id=task,
                    provider="frozen-fixture",
                    state=SourceState.SUCCEEDED,
                    detail="同一冻结来源快照已返回",
                    query_task_ids=(task,),
                    captured_at=NOW,
                )
                for task in tasks
            ),
            source_mode="frozen_fixture",
        )
        return catalog, tuple(contracts)


class ScriptedBoundedAgent:
    def __init__(self, *, stale: bool = False) -> None:
        self.manifests: list[AgentContextManifest] = []
        self.stale = stale

    def propose(self, manifest: AgentContextManifest) -> AgentProposalResult:
        self.manifests.append(manifest)
        selected = min(
            manifest.candidates,
            key=lambda item: (
                item.metrics.schedule_inconvenience_minutes,
                item.metrics.transport_duration_minutes,
                item.metrics.total_cny_cents,
            ),
        )
        return AgentProposalResult(
            proposal=AgentSelectionProposal(
                graph_version=(
                    "graph:" + "0" * 64 if self.stale else manifest.graph_version
                ),
                role=manifest.role,
                candidate_id=selected.candidate_id,
                reason="仅在有界候选中选择更少赶早且耗时更短的方案",
                source_refs=selected.source_refs,
                skill_id=manifest.skill_id,
                skill_version=manifest.skill_version,
            ),
            model="scripted-fixture-agent",
            token_usage=96,
            latency_ms=8,
        )


class UniqueWinnerProvider(CountingTradeoffProvider):
    def catalog_for(
        self,
        intent: Any,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        catalog, contracts = super().catalog_for(intent)
        allowed_offer_ids = {
            "rail:first:saver",
            "rail:fixed:1",
            "rail:fixed:2",
            "stay:0",
            "stay:1",
        }
        allowed_contract_ids = {
            item.price_contract_id
            for item in (*catalog.transports, *catalog.stays)
            if item.id in allowed_offer_ids
        }
        return (
            catalog.model_copy(
                update={
                    "transports": tuple(
                        item for item in catalog.transports if item.id in allowed_offer_ids
                    ),
                    "stays": tuple(
                        item for item in catalog.stays if item.id in allowed_offer_ids
                    ),
                }
            ),
            tuple(item for item in contracts if item.id in allowed_contract_ids),
        )


@pytest.mark.asyncio
async def test_formal_entry_reuses_one_catalog_for_distinct_pareto_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CountingTradeoffProvider()
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.delattr(app.state, "personalization_agent", raising=False)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=12, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="personalization-three-card-user",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52001)),
            base_url="http://test",
        ) as client:
            unspecified = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT),
            )
            price_first = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "这次价格优先。"),
            )
            experience_first = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "这次舒适优先。"),
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert unspecified.status_code == 200, unspecified.text
    assert price_first.status_code == 200, price_first.text
    assert experience_first.status_code == 200, experience_first.text
    assert provider.calls == 3

    unspecified_body = unspecified.json()
    cards = unspecified_body["trip_cards"]
    assert unspecified_body["trip_card"] is None
    assert [item["representative_kind"] for item in cards] == [
        "saver",
        "balanced",
        "experience",
    ]
    assert len({tuple(c["offer_id"] for c in item["components"]) for item in cards}) == 3
    assert {
        datetime.fromisoformat(item["query_captured_at"].replace("Z", "+00:00"))
        for item in cards
    } == {NOW}
    assert unspecified_body["personalization"]["provider_query_count"] == 1
    assert unspecified_body["personalization"]["pareto_candidate_count"] == 3
    assert unspecified_body["personalization"]["model_call_count"] == 0
    assert {
        response.json()["personalization"]["catalog_digest"]
        for response in (unspecified, price_first, experience_first)
    } == {unspecified_body["personalization"]["catalog_digest"]}
    assert len(
        {
            response.json()["personalization"]["graph_version"]
            for response in (unspecified, price_first, experience_first)
        }
    ) == 3

    price_card = price_first.json()["trip_card"]
    experience_card = experience_first.json()["trip_card"]
    assert price_card["representative_kind"] == "personalized"
    assert experience_card["representative_kind"] == "personalized"
    assert price_card["total_cny_cents"] == 160_000
    assert experience_card["total_cny_cents"] == 172_000
    assert price_card["decision_metrics"]["schedule_inconvenience_minutes"] > 0
    assert experience_card["decision_metrics"]["schedule_inconvenience_minutes"] == 0
    assert (
        experience_card["decision_metrics"]["transport_duration_minutes"]
        < price_card["decision_metrics"]["transport_duration_minutes"]
    )


@pytest.mark.asyncio
async def test_unique_winner_skips_model_and_matches_no_agent_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = UniqueWinnerProvider()
    agent = ScriptedBoundedAgent()
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.setattr(app.state, "personalization_agent", agent, raising=False)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=4, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="personalization-unique-user",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52003)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "价格和舒适都重要。"),
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trip_card"]["total_cny_cents"] == 160_000
    assert body["personalization"]["pareto_candidate_count"] == 1
    assert body["personalization"]["model_call_count"] == 0
    assert agent.manifests == []


@pytest.mark.asyncio
async def test_confirmed_elder_skill_applies_then_current_request_overrides_and_revoke_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CountingTradeoffProvider()
    agent = ScriptedBoundedAgent()
    store = PersistentMemoryStore(tmp_path / "preferences.json")
    monkeypatch.setattr(app.state, "complex_offer_provider", provider)
    monkeypatch.setattr(app.state, "personalization_agent", agent, raising=False)
    monkeypatch.setattr(main_module, "memory_store", store)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=12, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="personalization-user",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 52002)),
            base_url="http://test",
        ) as client:
            confirmed = await client.post(
                "/api/v1/agents/memory/preferences/confirm",
                json={
                    "key": " elder_trip_comfort ",
                    "value": {
                        "mode": "weighted",
                        "expected": {
                            "condition": "traveling_with_elders",
                            "avoid_transfers": True,
                            "avoid_departures_before": "08:00",
                            "max_comfort_premium_cny_cents": 20_000,
                        },
                        "weight": 1,
                    },
                },
            )
            elder = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "其中一位是长辈。"),
            )
            current_override = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "其中一位是长辈，这次价格优先。"),
            )
            record_id = confirmed.json()["record"]["id"]
            revoked = await client.delete(f"/api/v1/agents/memory/{record_id}")
            after_revoke = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(BASE_TEXT + "本轮一位是长辈。"),
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["record"]["subject"] == "elder_trip_comfort"
    stored = confirmed.json()["record"]["payload"]["value"]
    assert set(stored["expected"]) == {
        "condition",
        "avoid_transfers",
        "avoid_departures_before",
        "max_comfort_premium_cny_cents",
    }
    assert "price" not in str(stored).lower()
    assert elder.status_code == 200, elder.text
    elder_body = elder.json()
    assert elder_body["trip_card"]["total_cny_cents"] == 172_000
    assert elder_body["trip_card"]["participating_agent_roles"] == [
        AgentRole.EXPERIENCE_SPECIALIST.value
    ]
    assert elder_body["trip_card"]["applied_skill_ids"] == [
        "elder-comfort-travel"
    ]
    assert elder_body["personalization"]["model_call_count"] == 1
    assert elder_body["model_trace_count"] == 1
    assert elder_body["model_trace_success_count"] == 1
    manifest = agent.manifests[0]
    assert manifest.skill_id == "elder-comfort-travel"
    assert manifest.skill_version == "1.0.0"
    assert "不保存价格" in (manifest.skill_rule_boundary or "")
    assert len(manifest.candidates) == 3
    assert manifest.allowed_tools == ("inspect_personalization_candidates",)

    assert current_override.status_code == 200, current_override.text
    override_body = current_override.json()
    assert override_body["trip_card"]["total_cny_cents"] == 160_000
    assert override_body["personalization"]["model_call_count"] == 0
    assert revoked.status_code == 200
    assert after_revoke.status_code == 200, after_revoke.text
    assert after_revoke.json()["trip_card"] is None
    assert len(after_revoke.json()["trip_cards"]) == 3


def test_stale_agent_proposal_is_rejected_without_changing_program_result() -> None:
    intent = PlanningCompiler().compile(BASE_TEXT, reference_year=2026)
    assert intent is not None
    provider = CountingTradeoffProvider()
    catalog, contracts = provider.catalog_for(intent)
    problem = PlanningCompiler().compile_problem(
        intent.model_copy(
            update={
                "preference_policy": intent.preference_policy.model_copy(
                    update={"mode": PlanPreferenceMode.AMBIGUOUS}
                )
            }
        ),
        offer_catalog=catalog,
        price_contracts=contracts,
        captured_at=NOW,
    )
    stale_agent = ScriptedBoundedAgent(stale=True)
    result = personalize_complex_problem(problem, agent=stale_agent)
    assert result.summary.model_call_count == 1
    assert result.summary.agent_runs[0].applied is False
    assert result.summary.agent_runs[0].rejected_reason == "stale_graph_version"
    manifest = stale_agent.manifests[0]
    valid = ScriptedBoundedAgent().propose(manifest).proposal
    assert validate_agent_selection_proposal(manifest, valid) is None
    assert (
        validate_agent_selection_proposal(
            manifest,
            valid.model_copy(update={"candidate_id": "pareto:not-in-manifest"}),
        )
        == "candidate_outside_manifest"
    )
    assert (
        validate_agent_selection_proposal(
            manifest,
            valid.model_copy(update={"source_refs": ()}),
        )
        == "missing_source_references"
    )
    assert len(valid.source_refs) > 1
    assert (
        validate_agent_selection_proposal(
            manifest,
            valid.model_copy(update={"source_refs": valid.source_refs[:-1]}),
        )
        == "candidate_source_reference_mismatch"
    )
    unrelated_manifest_ref = next(
        item for item in manifest.source_refs if item not in valid.source_refs
    )
    assert (
        validate_agent_selection_proposal(
            manifest,
            valid.model_copy(
                update={"source_refs": (*valid.source_refs, unrelated_manifest_ref)}
            ),
        )
        == "candidate_source_reference_mismatch"
    )
    assert (
        validate_agent_selection_proposal(
            manifest,
            valid.model_copy(update={"source_refs": ("fixture://invented-source",)}),
        )
        == "source_reference_outside_manifest"
    )
    assert result.plans[0].candidate.candidate_id in {
        item.candidate.candidate_id
        for item in personalize_complex_problem(
            problem,
            agent=None,
        ).plans
    }


def test_agent_need_router_does_not_scale_roles_with_query_volume() -> None:
    intent = PlanningCompiler().compile(BASE_TEXT, reference_year=2026)
    assert intent is not None
    provider = CountingTradeoffProvider()
    catalog, contracts = provider.catalog_for(intent)
    problem = PlanningCompiler().compile_problem(
        intent,
        offer_catalog=catalog,
        price_contracts=contracts,
        captured_at=NOW,
    )
    frontier = personalize_complex_problem(problem).plans
    candidates = tuple(item.candidate for item in frontier)
    assert candidates
    router = AgentNeedRouter()
    assert router.route(intent.preference_policy, candidates * 8) == ()
    ambiguous_policy = intent.preference_policy.model_copy(
        update={"mode": PlanPreferenceMode.AMBIGUOUS}
    )
    roles = router.route(ambiguous_policy, candidates * 8)
    assert roles == ((AgentRole.DECISION_AGENT, "ambiguous_tradeoff"),)
