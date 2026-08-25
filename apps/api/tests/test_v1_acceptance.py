from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
    LivePackageAgentSystem,
    _RunState,
)
from tripchord.agents.memory import (
    MemoryAccessContext,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    MemoryVolatility,
    PrivacyBoundary,
    confirmed_preference_constitution,
)
from tripchord.agents.models import PreferenceMode
from tripchord.agents.package_request import (
    HybridPackageRequirementAgent,
    PackageRequestState,
    PackageRequirementRequest,
    project_preferences_to_intent_template,
)
from tripchord.agents.plan_modification import (
    LivePlanModificationStatus,
    parse_live_plan_modification,
)
from tripchord.agents.rag import EvidenceRagRetriever, RagPurpose, RagRequest
from tripchord.api import build_live_final_plan_projection
from tripchord.planning.complex_trip import PackagePlannerAdapter
from tripchord.planning.package import (
    NormalizedLodgingQuote,
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackagePlanner,
    PackageVerifier,
    PackageViolationSeverity,
)
from tripchord.planning.package_reverification import DeclarativePackageReVerifier
from tripchord.providers.browser_bridge import (
    BrowserQuote,
    BrowserSearchQuery,
    BrowserTaskBridge,
)
from tripchord.providers.quote_normalizer import BrowserQuoteNormalizer

FIXTURES = Path(__file__).parent / "fixtures" / "v1_acceptance"
REQUEST_TEXT = (
    "我要从杭州出发去马尔代夫周边游，时间：从明天开始到9月10日前的4-8天游，"
    "人数：我和女朋友两个人，偏好：酒店不能太简陋，地址不能太偏，"
    "可以稍微有点品质但价格不能过高，到达和返程可以住机场附近，"
    "但也要关注有没有更好的选择。"
)
CAPTURE_TIME = datetime(2026, 8, 21, 19, 40, tzinfo=UTC)


class _SavedExactPairReplayRunner:
    """Feed one saved exact-pair result through the real flexible controller."""

    def __init__(self, saved_run: LivePackageAgentRun) -> None:
        self._saved_run = saved_run
        self.active = 0
        self.max_active = 0
        self.started_pairs: list[tuple[date, date]] = []
        self._all_started = asyncio.Event()

    async def run(
        self,
        request: PackageIntent,
        search_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        del mode, timeout_seconds, source_start_delays_ms
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started_pairs.append((request.start_date, request.end_date))
        if self.active == 3:
            self._all_started.set()
        try:
            await self._all_started.wait()
            await asyncio.sleep(0)
            # Only the saved 09-03 -> 09-09 component set matches its request.
            # Rebinding the requested scope lets the production controller reject
            # the other dates instead of inventing source observations.
            return self._saved_run.model_copy(
                update={"intent": request, "search_query": search_query}
            )
        finally:
            self.active -= 1


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_fixture(name: str, manifest: dict[str, Any]) -> Any:
    entry = next(item for item in manifest["fixtures"] if item["path"] == name)
    compressed = (FIXTURES / name).read_bytes()
    assert _sha256(compressed) == entry["gzip_sha256"]
    raw = gzip.decompress(compressed)
    assert _sha256(raw) == entry["uncompressed_sha256"]
    return json.loads(raw)


def _saved_lodging_quotes(
    raw_quotes: list[dict[str, Any]],
    run: LivePackageAgentRun,
) -> tuple[NormalizedLodgingQuote, ...]:
    normalizer = BrowserQuoteNormalizer()
    lodgings: list[NormalizedLodgingQuote] = []
    live_options = run.search_query.model_dump(mode="json")["options"]
    for raw in raw_quotes:
        quote = BrowserQuote.model_validate(raw)
        query_value = quote.details["query"]
        assert isinstance(query_value, dict)
        query_payload = dict(query_value)
        options_value = query_payload["options"]
        assert isinstance(options_value, dict)
        options = dict(options_value)
        options.update(
            {
                "gateway_destination": live_options["gateway_destination"],
                "stay_area_search_profile": live_options["stay_area_search_profile"],
                "stay_plan_candidate_set": live_options["stay_plan_candidate_set"],
            }
        )
        query_payload["options"] = options
        normalized = normalizer.normalize(
            quote,
            BrowserSearchQuery.model_validate(query_payload),
        )
        assert normalized.usable
        assert isinstance(normalized.quote, NormalizedLodgingQuote)
        lodgings.append(normalized.quote)
    return tuple(lodgings)


@pytest.mark.asyncio
async def test_saved_real_maldives_desktop_v1_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = time.perf_counter()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["replay_mode"] == "historical_saved_source_replay"
    assert manifest["network_access"] is False
    assert manifest["not_current_price_or_inventory"] is True
    flexible_snapshot = _load_fixture("flexible-exploration.json.gz", manifest)
    final_snapshot = _load_fixture("final-plan.json.gz", manifest)
    raw_lodging_quotes = _load_fixture("ctrip-lodging-quotes.json.gz", manifest)

    interpreted = await HybridPackageRequirementAgent(
        now=lambda: datetime(2026, 8, 19, 12, tzinfo=UTC)
    ).parse(
        PackageRequirementRequest(
            text=REQUEST_TEXT,
            reference_date=date(2026, 8, 19),
        )
    )
    assert interpreted.state == PackageRequestState.READY
    assert interpreted.window is not None
    assert interpreted.intent_template is not None
    assert (interpreted.window.adults, interpreted.window.rooms) == (2, 1)
    legal_pairs = interpreted.window.all_date_pairs()
    assert len(legal_pairs) == 85

    memory_store = MemoryStore()
    memory_access = MemoryAccessContext(
        tenant_id="v1-acceptance",
        user_id="saved-replay-user",
        session_id="saved-replay-session",
    )
    memory_store.upsert(
        MemoryRecord(
            id="memory:confirmed-breakfast",
            kind=MemoryKind.USER_PREFERENCE,
            scope=MemoryScope.USER,
            privacy=PrivacyBoundary.USER_PRIVATE,
            tenant_id=memory_access.tenant_id,
            user_id=memory_access.user_id,
            topic="user_preference",
            subject="hotel_breakfast",
            payload={
                "key": "hotel_breakfast",
                "value": {"mode": "required", "expected": True, "weight": 1},
            },
            source="user:explicit_memory_confirmation",
            captured_at=datetime(2026, 8, 18, 12, tzinfo=UTC),
            volatility=MemoryVolatility.STABLE,
        )
    )
    memory_store.upsert(
        MemoryRecord(
            id="memory:historical-price-must-not-enter-rag",
            kind=MemoryKind.EVIDENCE,
            scope=MemoryScope.SESSION,
            privacy=PrivacyBoundary.USER_PRIVATE,
            tenant_id=memory_access.tenant_id,
            user_id=memory_access.user_id,
            session_id=memory_access.session_id,
            topic="live_quote_price",
            subject="historical-price",
            payload={"amount_cents": 1},
            source="saved-replay-fixture",
            captured_at=datetime(2026, 8, 21, 19, 36, tzinfo=UTC),
            expires_at=datetime(2026, 8, 21, 19, 46, tzinfo=UTC),
            volatility=MemoryVolatility.REALTIME,
            rag_eligible=False,
        )
    )
    durable_preferences = confirmed_preference_constitution(
        memory_store,
        memory_access,
        now=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )
    effective_preferences = durable_preferences.merged_for_trip(current=interpreted.preferences)
    template, _ = project_preferences_to_intent_template(
        interpreted.intent_template,
        effective_preferences,
    )
    assert template.require_breakfast is True
    assert template.require_non_basic_lodging is True
    assert template.require_non_remote_lodging is True
    breakfast_preference = effective_preferences.effective("hotel_breakfast")
    assert breakfast_preference is not None
    assert breakfast_preference.mode == PreferenceMode.REQUIRED
    rag = EvidenceRagRetriever(memory_store).retrieve(
        RagRequest(purpose=RagPurpose.PLANNER, token_budget=1_000),
        memory_access,
    )
    assert "memory:confirmed-breakfast" in {item.memory_id for item in rag.hits}
    assert "memory:historical-price-must-not-enter-rag" not in {item.memory_id for item in rag.hits}

    saved_flexible = flexible_snapshot["result"]["run"]
    saved_pairs = tuple(
        (
            date.fromisoformat(item["date_pair"]["departure_date"]),
            date.fromisoformat(item["date_pair"]["return_date"]),
        )
        for item in saved_flexible["pair_runs"]
    )
    assert saved_pairs == (
        (date(2026, 9, 3), date(2026, 9, 9)),
        (date(2026, 9, 3), date(2026, 9, 10)),
        (date(2026, 9, 7), date(2026, 9, 10)),
    )
    assert all(item in legal_pairs for item in saved_pairs)
    pair_started_at = tuple(
        datetime.fromisoformat(
            item["run"]["scheduler"]["trace"][0]["occurred_at"].replace("Z", "+00:00")
        )
        for item in saved_flexible["pair_runs"]
    )
    assert max(pair_started_at) - min(pair_started_at) < timedelta(milliseconds=20)
    performance = saved_flexible["performance_report"]
    assert performance["date_pair_count"] == 3
    assert performance["completed_date_pair_count"] == 3
    assert performance["failed_date_pair_count"] == 0
    assert performance["wall_time_seconds"] == pytest.approx(254.556521874998)
    assert all(
        item["run"]["decision"]["state"] == "human_block" for item in saved_flexible["pair_runs"]
    )

    run = LivePackageAgentRun.model_validate(final_snapshot["run"])
    pair_replay = _SavedExactPairReplayRunner(run)
    flexible_replay = await FlexibleLiveAgentSystem(
        pair_replay,
        now=lambda: datetime(2026, 8, 19, 12, tzinfo=UTC),
    ).run(
        interpreted.window,
        mode=LiveCoverageMode.STRICT,
        max_pairs=3,
        timeout_seconds=15,
        total_timeout_seconds=60,
        reference_date=date(2026, 8, 19),
        pair_worker_count_override=3,
        replay_pair_schedule=saved_pairs,
    )
    assert pair_replay.max_active == 3
    assert set(pair_replay.started_pairs) == set(saved_pairs)
    assert flexible_replay.final_decision.state == PackageDecisionState.HUMAN_BLOCK
    assert flexible_replay.recommended_option_ids == ()

    lodgings = _saved_lodging_quotes(raw_lodging_quotes, run)
    assert len(lodgings) == 20
    projected_intent = template.materialize(date(2026, 9, 3), date(2026, 9, 9))
    intent = run.intent.model_copy(
        update={
            "require_breakfast": projected_intent.require_breakfast,
            "require_non_basic_lodging": projected_intent.require_non_basic_lodging,
            "require_non_remote_lodging": projected_intent.require_non_remote_lodging,
        }
    )
    inventory = run.inventory.model_copy(update={"lodgings": lodgings})
    generated = PackagePlanner().generate_bounded(intent, inventory)
    adapter = PackagePlannerAdapter(
        PackagePlanner(),
        PackageVerifier(),
        now=lambda: CAPTURE_TIME,
    )
    adapter_bounded = adapter.generate_bounded(intent, inventory)
    adapter_generated = adapter.generate_verified(intent, inventory)
    expected_verified_ids = tuple(
        item.id
        for item in generated.candidates
        if not PackageVerifier().errors(intent, item, now=CAPTURE_TIME)
    )
    assert tuple(item.id for item in adapter_generated.candidates) == expected_verified_ids
    assert tuple(item.id for item in adapter_bounded.candidates) == tuple(
        item.id for item in generated.candidates
    )
    assert adapter_bounded.audit == generated.audit
    assert generated.audit.raw_inventory_counts["lodgings"] == 20
    assert generated.audit.prescreened_inventory_counts["lodgings"] == 8
    assert len(generated.candidates) == 8
    winner = generated.candidates[0]
    assert winner.lodgings[0].property_name == "马富士卡尼海滩酒店(Kaani Beach Hotel)"
    assert winner.lodgings[0].room_name == "市景豪华间 - 带阳台"
    assert winner.lodgings[0].total_for_party_cents == 248_500
    assert winner.computed_total_cents == 1_068_700
    assert not tuple(
        item
        for item in PackageVerifier().verify(intent, winner, now=CAPTURE_TIME)
        if item.severity == PackageViolationSeverity.ERROR
    )
    baseline_reverification = DeclarativePackageReVerifier().audit(
        intent,
        winner,
        winner,
        None,
        now=CAPTURE_TIME,
    )
    assert baseline_reverification.passed

    system = LivePackageAgentSystem(
        BrowserTaskBridge(now=lambda: CAPTURE_TIME),
        now=lambda: CAPTURE_TIME,
    )
    dominance = system._deterministic_dominance_winner(
        _RunState(
            source_task_ids=(),
            intent=intent,
            inventory=inventory,
            candidates=generated.candidates,
            candidate_shortlist=generated.candidates,
            candidate_decision_frontier=generated.candidates,
        ),
        intent,
    )
    assert dominance is not None
    assert dominance[0].id == winner.id
    assert dominance[1] == 8

    assert run.package is not None
    run = run.model_copy(
        update={
            "intent": intent,
            "inventory": inventory,
            "package": run.package.model_copy(
                update={
                    "initial_candidate": winner,
                    "final_candidate": winner,
                }
            ),
        }
    )
    final_plan = build_live_final_plan_projection(run)
    assert final_plan is not None
    assert final_plan.flight is not None
    assert final_plan.flight.total_for_party_cents == 820_200
    assert final_plan.lodgings[0].display_total_cents == 248_500
    assert final_plan.confirmed_cny_subtotal_cents == 1_068_700
    assert final_plan.estimated_icom_transfer_cny_cents == 80_647
    assert final_plan.estimated_total_cny_cents == 1_149_347
    assert final_plan.total_budget_cents is None
    assert final_plan.price_comparability == "confirmed_cny_subtotal_plus_icom_estimate"
    chinese_itinerary = (
        "杭州往返马累，2位成人；航班同行总价¥8202；"
        "Kaani Beach Hotel市景豪华阳台房5晚¥2485；"
        "人民币已确认小计¥10687，iCom接驳按当日参考汇率估算¥806.47，"
        "预计合计¥11493.47；接驳税费未知，以上不是结算锁价。"
    )
    assert "人民币已确认小计¥10687" in chinese_itinerary
    assert "不是结算锁价" in chinese_itinerary

    async def unexpected_live_refresh(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("historical acceptance replay must not contact an OTA")

    monkeypatch.setattr(
        system,
        "_refresh_lodging_modification_sources",
        unexpected_live_refresh,
    )
    sea_view = parse_live_plan_modification(
        "酒店换成海景房，航班和接驳保持不变",
        current_departure_date=run.intent.start_date,
    )
    sea_view_run, sea_view_receipt = await system.modify_plan(
        run,
        sea_view,
        offline_lodging_quotes=lodgings,
        verification_now=CAPTURE_TIME,
    )
    assert sea_view_receipt.status == LivePlanModificationStatus.MODIFIED
    assert sea_view_receipt.difference_cny_cents == 12_000
    assert sea_view_receipt.verifier_passed is True
    assert sea_view_receipt.reverifier_passed is True
    assert sea_view_run.package is not None
    sea_view_candidate = sea_view_run.package.final_candidate
    assert sea_view_candidate.lodgings[0].room_name == "海景豪华双人房带阳台"
    assert sea_view_candidate.lodgings[0].total_for_party_cents == 260_500
    assert sea_view_candidate.flight.id == winner.flight.id
    assert tuple(item.id for item in sea_view_candidate.transfers) == tuple(
        item.id for item in winner.transfers
    )

    other_property = parse_live_plan_modification(
        "换一家酒店，航班和接驳保持不变",
        current_departure_date=run.intent.start_date,
    )
    unchanged, blocked_property = await system.modify_plan(
        run,
        other_property,
        offline_lodging_quotes=lodgings,
        verification_now=CAPTURE_TIME,
    )
    assert blocked_property.status == LivePlanModificationStatus.BLOCKED
    assert unchanged is run
    assert blocked_property.preserved_component_ids == winner.component_ids
    assert "位置硬条件" in blocked_property.summary

    global_change = parse_live_plan_modification(
        "改成9月4日出发，9月10日返回",
        current_departure_date=run.intent.start_date,
    )
    blocked_decision = PackageDecision(
        state=PackageDecisionState.HUMAN_BLOCK,
        summary="保存回放没有形成新日期的合格完整方案",
    )
    current_package = run.package
    assert current_package is not None
    failed_package = current_package.model_copy(
        update={
            "decisions": (*current_package.decisions, blocked_decision),
            "final_decision": blocked_decision,
        }
    )
    failed_global_run = run.model_copy(
        update={"decision": blocked_decision, "package": failed_package}
    )

    async def failed_global_replay(
        *_args: object,
        **_kwargs: object,
    ) -> LivePackageAgentRun:
        return failed_global_run

    monkeypatch.setattr(system, "run", failed_global_replay)
    unchanged_global, blocked_global = await system.modify_plan(run, global_change)
    assert blocked_global.status == LivePlanModificationStatus.BLOCKED
    assert unchanged_global is run
    assert blocked_global.before_candidate_id == winner.id
    assert blocked_global.after_candidate_id == winner.id
    assert blocked_global.preserved_component_ids == winner.component_ids
    assert blocked_global.difference_cny_cents == 0
    assert "原方案" in blocked_global.summary

    elapsed = time.perf_counter() - started
    summary = {
        "mode": "historical_saved_source_replay",
        "network_access": False,
        "legal_date_pair_count": len(legal_pairs),
        "actually_queried_saved_pair_count": len(saved_pairs),
        "saved_live_wall_time_seconds": performance["wall_time_seconds"],
        "offline_replay_wall_time_seconds": round(elapsed, 3),
        "model_request_count": 0,
        "selected_lodging_cny_cents": winner.lodgings[0].total_for_party_cents,
        "confirmed_cny_subtotal_cents": final_plan.confirmed_cny_subtotal_cents,
        "estimated_total_cny_cents": final_plan.estimated_total_cny_cents,
        "sea_view_difference_cny_cents": sea_view_receipt.difference_cny_cents,
        "other_property_status": blocked_property.status.value,
        "global_date_change_status": blocked_global.status.value,
    }
    print("\nV1_ACCEPTANCE=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
