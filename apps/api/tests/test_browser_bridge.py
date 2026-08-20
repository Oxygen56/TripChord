from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    CONTROL_TOKEN_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    LIVE_V4_BROWSER_PROVIDERS,
    BrowserBridgeError,
    BrowserClaimError,
    BrowserCompanionBuildIdentity,
    BrowserCompanionControlError,
    BrowserCompanionControlState,
    BrowserCompanionReloadReasonCode,
    BrowserCompanionReloadReceipt,
    BrowserCompanionReloadReceiptState,
    BrowserCompanionReloadRequestBody,
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserQuote,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskCompletion,
    BrowserTaskNotFoundError,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    JsonFileBrowserBridgeStateStore,
    QuotePriceBasis,
    create_browser_bridge_app,
    ctrip_trusted_flight_search_url,
    fliggy_trusted_flight_search_url,
    qunar_trusted_flight_search_url,
    tongcheng_trusted_flight_search_url,
    tongcheng_trusted_lodging_search_url,
)

OLD_BUILD_SHA256 = "1" * 64
TARGET_BUILD_SHA256 = "2" * 64
OLD_RUNTIME_INSTANCE_ID = "runtime-instance-old-0001"
NEW_RUNTIME_INSTANCE_ID = "runtime-instance-new-0002"


def companion_build(build_sha256: str) -> BrowserCompanionBuildIdentity:
    return BrowserCompanionBuildIdentity(
        manifest_version="0.1.0",
        build_sha256=build_sha256,
        content_runtime_version="2026-08-04.1",
    )


def reload_request_body() -> BrowserCompanionReloadRequestBody:
    return BrowserCompanionReloadRequestBody(
        expected_current_build_sha256=OLD_BUILD_SHA256,
        target_build_sha256=TARGET_BUILD_SHA256,
        reason_code=BrowserCompanionReloadReasonCode.COMPANION_BUILD_CHANGED,
        expires_in_seconds=120,
        max_drain_seconds=90,
    )


def submission(
    provider: BrowserProvider,
    kind: BrowserVertical,
    *,
    timeout_seconds: int = 120,
    max_attempts: int = 2,
    reuse_partition_sha256: str | None = None,
) -> BrowserTaskSubmission:
    return BrowserTaskSubmission(
        provider=provider,
        kind=kind,
        query=BrowserSearchQuery(
            origin="杭州" if kind == BrowserVertical.FLIGHT else None,
            destination="马累",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 30),
            adults=2,
            rooms=1,
            origin_code="HGH",
            destination_code="MLE",
        ),
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        reuse_partition_sha256=reuse_partition_sha256,
    )


def reuse_partition(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def quote(
    provider: BrowserProvider,
    kind: BrowserVertical,
    *,
    amount: str = "4692",
) -> BrowserQuote:
    domain = {
        BrowserProvider.CTRIP: "flights.ctrip.com",
        BrowserProvider.FLIGGY: "sjipiao.fliggy.com",
        BrowserProvider.QUNAR: "flight.qunar.com",
    }[provider]
    query_details = {
        "origin": "杭州" if kind == BrowserVertical.FLIGHT else None,
        "destination": "马累",
        "start_date": "2026-08-23",
        "end_date": "2026-08-30",
        "adults": 2,
        "rooms": 1,
        "currency": "CNY",
        "origin_code": "HGH" if kind == BrowserVertical.FLIGHT else None,
        "destination_code": "MLE",
        "search_url": None,
    }
    typed_details = (
        {
            "origin": "杭州",
            "destination": "马累",
            "adults": 2,
            "outbound_departure_at": "2026-08-23T08:30:00+08:00",
            "outbound_arrival_at": "2026-08-23T18:35:00+05:00",
            "return_departure_at": "2026-08-30T10:45:00+05:00",
            "return_arrival_at": "2026-08-31T09:10:00+08:00",
            "carrier_text": "香港航空",
            "connection_text": "香港中转",
            "baggage_text": "无免费托运行李",
            "workflow_kind": (
                "combined_roundtrip_card"
                if provider == BrowserProvider.QUNAR
                else "staged_outbound_return"
            ),
            "combination_status": "round_trip_complete",
            "combination_id": f"{provider.value}-fixture-outbound-return",
            "journey_price_scope": "round_trip",
            "price_finality": "final_for_combination",
            "price_basis_evidence": "页面可见的每人往返价",
            "tax_evidence": "页面可见含税",
            "party_availability_status": (
                "comparison_only"
                if provider == BrowserProvider.FLIGGY
                else "confirmed_for_party"
            ),
            "selection_evidence": (
                "组合卡同时包含去返两程"
                if provider == BrowserProvider.QUNAR
                else "已选去程摘要与返程列表一致"
            ),
            "action_trace": (
                [{"action": "search"}]
                if provider == BrowserProvider.QUNAR
                else [{"action": "search"}, {"action": "select_outbound"}]
            ),
        }
        if kind == BrowserVertical.FLIGHT
        else {
            "destination": "马累",
            "check_in": "2026-08-23",
            "check_out": "2026-08-30",
            "adults": 2,
            "rooms": 1,
            "room_text": "标准大床房",
            "area_text": "胡鲁马累",
            "breakfast_text": "含早餐",
            "cancellation_text": "可免费取消",
            "transfer_text": "机场接送 US$15",
        }
    )
    return BrowserQuote(
        provider=provider,
        kind=kind,
        page_url=f"https://{domain}/search/results",
        captured_at=datetime.now(UTC),
        parser_version="tripchord-visible-dom-v3",
        visible_evidence="{}",
        evidence_sha256="a" * 64,
        currency="cny",
        amount=Decimal(amount),
        price_basis=(
            QuotePriceBasis.PER_PERSON
            if kind == BrowserVertical.FLIGHT
            else QuotePriceBasis.PER_NIGHT
        ),
        taxes_included=True,
        title="杭州往返马累" if kind == BrowserVertical.FLIGHT else "马累示例酒店",
        details={
            "fixture": True,
            "read_only": True,
            "query": query_details,
            "driver": {
                "mode": "fixture",
                "triggered": True,
                "confirmed_query": {"destination": "马累"},
                "confirmation_scope": "fixture",
            },
            "price_text": f"¥{amount}",
            "visible_terms": ["含税"],
            "extraction": "visible_dom",
            **typed_details,
        },
    )


@pytest.mark.asyncio
async def test_claims_six_platform_vertical_tasks_in_one_batch() -> None:
    bridge = BrowserTaskBridge()
    tasks = await bridge.submit_many(
        submission(provider, kind)
        for provider in LIVE_V4_BROWSER_PROVIDERS
        for kind in BrowserVertical
    )

    leases = await bridge.claim("chrome-profile-companion", limit=6)

    assert len(tasks) == 6
    assert len(leases) == 6
    assert {(lease.provider, lease.kind) for lease in leases} == {
        (provider, kind) for provider in LIVE_V4_BROWSER_PROVIDERS for kind in BrowserVertical
    }
    assert len({lease.claim_token for lease in leases}) == 6
    snapshots = [await bridge.get(lease.task_id) for lease in leases]
    assert all(snapshot.state == "claimed" for snapshot in snapshots)
    assert all(snapshot.claimed_at is not None for snapshot in snapshots)


@pytest.mark.asyncio
async def test_large_claim_batch_applies_qunar_lodging_backpressure_before_lease() -> None:
    bridge = BrowserTaskBridge()
    await bridge.submit_many(
        submission(provider, BrowserVertical.LODGING)
        for provider in LIVE_V4_BROWSER_PROVIDERS
        for _ in range(5)
    )

    leases = await bridge.claim("chrome-profile-companion", limit=6)

    assert len(leases) == 6
    assert {
        provider: sum(lease.provider == provider for lease in leases)
        for provider in LIVE_V4_BROWSER_PROVIDERS
    } == {
        BrowserProvider.CTRIP: 3,
        BrowserProvider.FLIGGY: 2,
        BrowserProvider.QUNAR: 1,
    }

    concurrent = await bridge.claim("second-companion", limit=6)
    assert all(
        not (
            lease.provider == BrowserProvider.QUNAR
            and lease.kind == BrowserVertical.LODGING
        )
        for lease in concurrent
    )

    await bridge.cancel_many(
        (lease.task_id for lease in leases),
        reason="test batch completed",
    )
    after_release = await bridge.claim("chrome-profile-companion", limit=6)
    assert sum(
        lease.provider == BrowserProvider.QUNAR
        and lease.kind == BrowserVertical.LODGING
        for lease in after_release
    ) == 1


@pytest.mark.asyncio
async def test_empty_claim_records_privacy_minimal_heartbeat_and_expires() -> None:
    clock = [datetime(2026, 7, 30, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])

    assert (
        await bridge.claim(
            "chrome-mv3-fixture-extension",
            providers=tuple(BrowserProvider),
        )
        == ()
    )
    connected = await bridge.companion_status()
    heartbeat = connected.companions[0]

    assert connected.status == "connected"
    assert connected.stale_after_seconds == 45
    assert heartbeat.companion_id == "chrome-mv3-fixture-extension"
    assert heartbeat.providers == tuple(BrowserProvider)
    assert heartbeat.last_seen == clock[0]
    assert heartbeat.age_seconds == 0
    assert heartbeat.is_fresh is True
    assert set(heartbeat.model_dump()) == {
        "companion_id",
        "providers",
        "last_seen",
        "age_seconds",
        "is_fresh",
        "authorized_scope_keys",
        "adapter_version",
        "contract_version",
        "build_identity",
        "runtime_instance_id",
    }

    clock[0] += timedelta(seconds=46)
    stale = await bridge.companion_status()

    assert stale.status == "disconnected"
    assert stale.companions[0].age_seconds == 46
    assert stale.companions[0].is_fresh is False

    refreshed = await bridge.heartbeat(
        "chrome-mv3-fixture-extension",
        providers=tuple(BrowserProvider),
    )
    assert refreshed.last_seen == clock[0]
    assert refreshed.age_seconds == 0
    assert refreshed.is_fresh is True
    assert (await bridge.companion_status()).status == "connected"


@pytest.mark.asyncio
async def test_completion_requires_matching_lease_and_quote_scope() -> None:
    bridge = BrowserTaskBridge()
    (task,) = await bridge.submit_many((submission(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),))
    (lease,) = await bridge.claim("companion")

    with pytest.raises(BrowserClaimError, match="claim token"):
        await bridge.complete(
            task.id,
            "wrong-token-value-long-enough",
            BrowserTaskCompletion(
                state=BrowserTaskState.SUCCEEDED,
                quotes=(quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),),
            ),
        )

    with pytest.raises(BrowserClaimError, match="provider or kind"):
        await bridge.complete(
            task.id,
            lease.claim_token,
            BrowserTaskCompletion(
                state=BrowserTaskState.SUCCEEDED,
                quotes=(quote(BrowserProvider.FLIGGY, BrowserVertical.FLIGHT),),
            ),
        )

    completed = await bridge.complete(
        task.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),),
        ),
    )

    assert completed.state == BrowserTaskState.SUCCEEDED
    assert completed.quotes[0].currency == "CNY"
    assert completed.quotes[0].amount == Decimal("4692")
    assert completed.quotes[0].taxes_included is True
    assert completed.quotes[0].details["read_only"] is True
    assert completed.quotes[0].details["query"]["destination"] == "马累"
    assert completed.quotes[0].details["driver"]["triggered"] is True
    assert completed.quotes[0].details["outbound_departure_at"] == ("2026-08-23T08:30:00+08:00")


@pytest.mark.asyncio
async def test_exact_lodging_quote_reuse_is_explicit_bounded_and_audited() -> None:
    clock = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    base = submission(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        reuse_partition_sha256=reuse_partition("tenant-a|user-a"),
    )
    opted_in = base.model_copy(
        update={
            "query": base.query.model_copy(
                update={
                    "options": {
                        "segment": "full",
                        "expected_package_area": "destination_island",
                        "expected_lodging_place_key": "maafushi",
                        "__tripchord_allow_recent_quote_reuse": True,
                    }
                }
            )
        }
    )
    (source,) = await bridge.submit_many((opted_in,))
    (lease,) = await bridge.claim("companion")
    source_quote = quote(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
    ).model_copy(update={"captured_at": clock[0]})
    await bridge.complete(
        source.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(source_quote,),
        ),
    )

    clock[0] += timedelta(minutes=9, seconds=59)
    retry = opted_in.model_copy(
        update={
            "query": opted_in.query.model_copy(
                update={
                    "options": {
                        **opted_in.query.options,
                        "__tripchord_reuse_exact_result_tab": True,
                    }
                }
            )
        }
    )
    (reused,) = await bridge.submit_many((retry,))
    assert reused.state == BrowserTaskState.SUCCEEDED
    assert reused.attempt_count == 0
    assert reused.reused_from_task_id == source.id
    assert reused.reuse_age_seconds == 599
    assert reused.quotes == (source_quote,)

    clock[0] += timedelta(seconds=1)
    (expired,) = await bridge.submit_many((retry,))
    assert expired.state == BrowserTaskState.QUEUED
    assert expired.reused_from_task_id is None


@pytest.mark.asyncio
async def test_exact_quote_reuse_is_partitioned_and_event_refresh_can_bypass_it() -> None:
    clock = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    base = submission(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        reuse_partition_sha256=reuse_partition("tenant-a|user-a"),
    )
    reusable_query = base.query.model_copy(
        update={
            "options": {
                "segment": "full",
                "expected_package_area": "destination_island",
                "expected_lodging_place_key": "maafushi",
                "__tripchord_allow_recent_quote_reuse": True,
            }
        }
    )
    source_submission = base.model_copy(update={"query": reusable_query})
    (source,) = await bridge.submit_many((source_submission,))
    (lease,) = await bridge.claim("companion")
    source_quote = quote(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
    ).model_copy(update={"captured_at": clock[0]})
    await bridge.complete(
        source.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(source_quote,),
        ),
    )
    clock[0] += timedelta(minutes=1)

    cross_user = source_submission.model_copy(
        update={"reuse_partition_sha256": reuse_partition("tenant-a|user-b")}
    )
    (isolated,) = await bridge.submit_many((cross_user,))
    assert isolated.state == BrowserTaskState.QUEUED
    assert isolated.reused_from_task_id is None

    event_refresh = source_submission.model_copy(
        update={
            "query": reusable_query.model_copy(
                update={
                    "options": {
                        **reusable_query.options,
                        "__tripchord_allow_recent_quote_reuse": False,
                    }
                }
            )
        }
    )
    (fresh_task,) = await bridge.submit_many((event_refresh,))
    assert fresh_task.state == BrowserTaskState.QUEUED
    assert fresh_task.reused_from_task_id is None


@pytest.mark.asyncio
async def test_identical_partitioned_inflight_queries_use_single_flight() -> None:
    bridge = BrowserTaskBridge()
    base = submission(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        reuse_partition_sha256=reuse_partition("tenant-a|user-a"),
    )
    reusable = base.model_copy(
        update={
            "query": base.query.model_copy(
                update={"options": {"__tripchord_allow_recent_quote_reuse": True}}
            )
        }
    )

    (first,) = await bridge.submit_many((reusable,))
    (second,) = await bridge.submit_many((reusable,))

    assert first.id == second.id
    assert second.inflight_coalesced is True
    (lease,) = await bridge.claim("single-flight-companion")
    assert lease.task_id == first.id
    assert await bridge.claim("single-flight-companion-2") == ()
    terminal = await bridge.complete(
        first.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(quote(BrowserProvider.CTRIP, BrowserVertical.LODGING),),
        ),
    )
    (observed,) = await bridge.wait_many((second.id,), timeout_seconds=1)

    assert terminal.state == BrowserTaskState.SUCCEEDED
    assert observed.quotes == terminal.quotes


@pytest.mark.asyncio
async def test_single_flight_is_tenant_isolated_and_one_waiter_cannot_cancel_others() -> None:
    bridge = BrowserTaskBridge()
    base = submission(BrowserProvider.CTRIP, BrowserVertical.LODGING)

    def reusable(partition_key: str) -> BrowserTaskSubmission:
        return base.model_copy(
            update={
                "reuse_partition_sha256": reuse_partition(partition_key),
                "query": base.query.model_copy(
                    update={"options": {"__tripchord_allow_recent_quote_reuse": True}}
                ),
            }
        )

    (first,) = await bridge.submit_many((reusable("tenant-a|user-a"),))
    (same_partition,) = await bridge.submit_many((reusable("tenant-a|user-a"),))
    (other_partition,) = await bridge.submit_many((reusable("tenant-b|user-b"),))

    assert same_partition.id == first.id
    assert other_partition.id != first.id
    (still_running,) = await bridge.cancel_many((first.id,), reason="one waiter timed out")
    assert still_running.state == BrowserTaskState.QUEUED
    (cancelled,) = await bridge.cancel_many((first.id,), reason="last waiter timed out")
    assert cancelled.state == BrowserTaskState.CANCELLED


@pytest.mark.asyncio
async def test_quote_reuse_without_authenticated_partition_fails_closed() -> None:
    clock = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    base = submission(BrowserProvider.CTRIP, BrowserVertical.LODGING)
    query_with_reuse = base.query.model_copy(
        update={"options": {"__tripchord_allow_recent_quote_reuse": True}}
    )
    unsafe_unpartitioned = base.model_copy(update={"query": query_with_reuse})
    (source,) = await bridge.submit_many((unsafe_unpartitioned,))
    (lease,) = await bridge.claim("companion")
    await bridge.complete(
        source.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(
                quote(
                    BrowserProvider.CTRIP,
                    BrowserVertical.LODGING,
                ).model_copy(update={"captured_at": clock[0]}),
            ),
        ),
    )

    (retry,) = await bridge.submit_many((unsafe_unpartitioned,))

    assert retry.state == BrowserTaskState.QUEUED
    assert retry.reused_from_task_id is None


@pytest.mark.asyncio
async def test_multi_quote_cache_rejects_batch_when_any_quote_is_expired() -> None:
    clock = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    base = submission(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        reuse_partition_sha256=reuse_partition("tenant-a|user-a"),
    )
    reusable = base.model_copy(
        update={
            "query": base.query.model_copy(
                update={"options": {"__tripchord_allow_recent_quote_reuse": True}}
            )
        }
    )
    (source,) = await bridge.submit_many((reusable,))
    (lease,) = await bridge.claim("companion")
    fresh = quote(BrowserProvider.CTRIP, BrowserVertical.LODGING).model_copy(
        update={"captured_at": clock[0]}
    )
    stale = quote(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        amount="5692",
    ).model_copy(update={"captured_at": clock[0] - timedelta(seconds=600)})
    await bridge.complete(
        source.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(fresh, stale),
        ),
    )

    (retry,) = await bridge.submit_many((reusable,))

    assert retry.state == BrowserTaskState.QUEUED
    assert retry.reused_from_task_id is None


def test_reuse_fingerprint_covers_every_price_affecting_query_field() -> None:
    base = submission(BrowserProvider.CTRIP, BrowserVertical.LODGING).query.model_copy(
        update={
            "options": {
                "segment": "full",
                "expected_package_area": "destination_island",
                "expected_lodging_place_key": "maafushi",
                "fare_family": "refundable",
                "__tripchord_allow_recent_quote_reuse": True,
            }
        }
    )
    canonical_payload = base.model_dump(mode="json")
    canonical_payload["options"] = {
        key: value
        for key, value in canonical_payload["options"].items()
        if not key.startswith("__tripchord_")
    }
    expected = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    original = BrowserTaskBridge._reuse_fingerprint(base)

    assert original == expected
    variants = (
        base.model_copy(update={"origin": "上海"}),
        base.model_copy(update={"destination": "胡鲁马累"}),
        base.model_copy(update={"start_date": date(2026, 8, 24)}),
        base.model_copy(update={"end_date": date(2026, 8, 31)}),
        base.model_copy(update={"adults": 3}),
        base.model_copy(update={"rooms": 2}),
        base.model_copy(update={"currency": "USD"}),
        base.model_copy(update={"origin_code": "PVG"}),
        base.model_copy(update={"destination_code": "DXB"}),
        base.model_copy(update={"search_url": "https://hotels.ctrip.com/hotels/list"}),
        base.model_copy(update={"options": {**base.options, "segment": "middle"}}),
        base.model_copy(
            update={"options": {**base.options, "fare_family": "non_refundable"}}
        ),
    )
    assert all(BrowserTaskBridge._reuse_fingerprint(item) != original for item in variants)

    control_only_change = base.model_copy(
        update={
            "options": {
                **base.options,
                "__tripchord_allow_recent_quote_reuse": False,
                "__tripchord_reuse_exact_result_tab": True,
            }
        }
    )
    assert BrowserTaskBridge._reuse_fingerprint(control_only_change) == original


@pytest.mark.asyncio
async def test_captcha_login_and_dom_drift_are_structured_results() -> None:
    bridge = BrowserTaskBridge()
    tasks = await bridge.submit_many(
        (
            submission(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),
            submission(BrowserProvider.FLIGGY, BrowserVertical.FLIGHT),
            submission(BrowserProvider.QUNAR, BrowserVertical.FLIGHT),
        )
    )
    leases = await bridge.claim("companion", limit=3)
    now = datetime.now(UTC)
    completions = (
        BrowserTaskCompletion(
            state=BrowserTaskState.BLOCKED,
            failure=BrowserFailure(
                code=BrowserFailureCode.CAPTCHA_REQUIRED,
                message="平台要求用户完成验证码",
                captured_at=now,
            ),
        ),
        BrowserTaskCompletion(
            state=BrowserTaskState.BLOCKED,
            failure=BrowserFailure(
                code=BrowserFailureCode.LOGIN_REQUIRED,
                message="当前标签页登录状态已失效",
                captured_at=now,
            ),
        ),
        BrowserTaskCompletion(
            state=BrowserTaskState.FAILED,
            failure=BrowserFailure(
                code=BrowserFailureCode.DOM_DRIFT,
                message="没有找到已知报价卡片",
                retryable=False,
                captured_at=now,
            ),
        ),
    )

    results = [
        await bridge.complete(task.id, lease.claim_token, completion)
        for task, lease, completion in zip(tasks, leases, completions, strict=True)
    ]

    assert [result.state for result in results] == ["blocked", "blocked", "failed"]
    assert [result.failure.code for result in results if result.failure] == [
        "captcha_required",
        "login_required",
        "dom_drift",
    ]


@pytest.mark.asyncio
async def test_expired_claim_requeues_once_then_fails() -> None:
    clock = [datetime(2026, 7, 30, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    (task,) = await bridge.submit_many(
        (
            submission(
                BrowserProvider.QUNAR,
                BrowserVertical.LODGING,
                timeout_seconds=15,
                max_attempts=2,
            ),
        )
    )

    await bridge.claim("companion-a")
    clock[0] += timedelta(seconds=16)
    assert (await bridge.get(task.id)).state == BrowserTaskState.QUEUED

    await bridge.claim("companion-b")
    clock[0] += timedelta(seconds=16)
    failed = await bridge.get(task.id)

    assert failed.state == BrowserTaskState.FAILED
    assert failed.attempt_count == 2
    assert failed.failure is not None
    assert failed.failure.code == BrowserFailureCode.TIMEOUT


@pytest.mark.asyncio
async def test_housekeeping_terminal_transition_wakes_wait_many() -> None:
    clock = [datetime(2026, 7, 30, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    (task,) = await bridge.submit_many(
        (
            submission(
                BrowserProvider.QUNAR,
                BrowserVertical.LODGING,
                timeout_seconds=15,
                max_attempts=1,
            ),
        )
    )
    await bridge.claim("companion-a")
    waiter = asyncio.create_task(bridge.wait_many((task.id,), timeout_seconds=5))
    await asyncio.sleep(0)

    clock[0] += timedelta(seconds=16)
    assert await bridge.claim("companion-b") == ()
    (terminal,) = await asyncio.wait_for(waiter, timeout=0.5)

    assert terminal.state == BrowserTaskState.FAILED
    assert terminal.failure is not None
    assert terminal.failure.code == BrowserFailureCode.TIMEOUT


@pytest.mark.asyncio
async def test_expired_completion_housekeeping_wakes_wait_many() -> None:
    clock = [datetime(2026, 7, 30, 12, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    (task,) = await bridge.submit_many(
        (
            submission(
                BrowserProvider.QUNAR,
                BrowserVertical.LODGING,
                timeout_seconds=15,
                max_attempts=1,
            ),
        )
    )
    (lease,) = await bridge.claim("companion")
    waiter = asyncio.create_task(bridge.wait_many((task.id,), timeout_seconds=5))
    await asyncio.sleep(0)

    clock[0] += timedelta(seconds=16)
    with pytest.raises(BrowserClaimError, match="active claim"):
        await bridge.complete(
            task.id,
            lease.claim_token,
            BrowserTaskCompletion(
                state=BrowserTaskState.FAILED,
                failure=BrowserFailure(
                    code=BrowserFailureCode.DOM_DRIFT,
                    message="late fixture completion",
                    captured_at=clock[0],
                ),
            ),
        )
    (terminal,) = await asyncio.wait_for(waiter, timeout=0.5)

    assert terminal.state == BrowserTaskState.FAILED
    assert terminal.failure is not None
    assert terminal.failure.code == BrowserFailureCode.TIMEOUT


@pytest.mark.asyncio
async def test_wait_many_self_wakes_at_nearest_lease_expiry() -> None:
    bridge = BrowserTaskBridge()
    (task,) = await bridge.submit_many(
        (
            submission(
                BrowserProvider.QUNAR,
                BrowserVertical.LODGING,
                timeout_seconds=15,
                max_attempts=1,
            ),
        )
    )
    await bridge.claim("companion")
    async with bridge._changed:
        bridge._records[task.id].lease_expires_at = datetime.now(UTC) + timedelta(milliseconds=50)

    (terminal,) = await bridge.wait_many((task.id,), timeout_seconds=0.5)

    assert terminal.state == BrowserTaskState.FAILED
    assert terminal.failure is not None
    assert terminal.failure.code == BrowserFailureCode.TIMEOUT


@pytest.mark.asyncio
async def test_optional_file_store_requeues_claim_without_persisting_lease_token(
    tmp_path: Path,
) -> None:
    state_path = (tmp_path / "bridge-state.json").resolve()
    store = JsonFileBrowserBridgeStateStore(state_path)
    bridge = BrowserTaskBridge(state_store=store)
    (task,) = await bridge.submit_many(
        (submission(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),)
    )
    (first_lease,) = await bridge.claim("companion-before-restart")

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    serialized = state_path.read_text(encoding="utf-8")
    for private_lease_field in (
        "claim_token",
        "claimed_by",
        "claimed_at",
        "lease_expires_at",
    ):
        assert private_lease_field not in serialized
    assert persisted["tasks"][0]["state"] == "claimed"
    assert state_path.stat().st_mode & 0o777 == 0o600

    recovered = BrowserTaskBridge(state_store=store)
    snapshot = await recovered.get(task.id)
    assert snapshot.state == BrowserTaskState.QUEUED
    assert snapshot.attempt_count == 1
    assert snapshot.claimed_by is None
    assert snapshot.claimed_at is None

    (second_lease,) = await recovered.claim("companion-after-restart")
    assert second_lease.task_id == first_lease.task_id
    assert second_lease.claim_token != first_lease.claim_token


@pytest.mark.asyncio
async def test_succeeded_exact_quote_cache_survives_restart_with_partition(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 3, 10, 0, tzinfo=UTC)]
    state_path = (tmp_path / "bridge-success-state.json").resolve()
    store = JsonFileBrowserBridgeStateStore(state_path)
    partition = reuse_partition("tenant-a|user-a")
    base = submission(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
        reuse_partition_sha256=partition,
    )
    reusable = base.model_copy(
        update={
            "query": base.query.model_copy(
                update={"options": {"__tripchord_allow_recent_quote_reuse": True}}
            )
        }
    )
    bridge = BrowserTaskBridge(state_store=store, now=lambda: clock[0])
    (source,) = await bridge.submit_many((reusable,))
    (lease,) = await bridge.claim("companion-before-restart")
    source_quote = quote(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
    ).model_copy(update={"captured_at": clock[0]})
    await bridge.complete(
        source.id,
        lease.claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(source_quote,),
        ),
    )

    clock[0] += timedelta(minutes=3)
    recovered = BrowserTaskBridge(state_store=store, now=lambda: clock[0])
    (cached,) = await recovered.submit_many((reusable,))

    assert cached.state == BrowserTaskState.SUCCEEDED
    assert cached.reused_from_task_id == source.id
    assert cached.reuse_age_seconds == 180
    assert cached.quotes == (source_quote,)
    serialized = state_path.read_text(encoding="utf-8")
    assert partition in serialized
    assert "tenant-a|user-a" not in serialized


@pytest.mark.asyncio
async def test_restart_after_final_claim_fails_instead_of_replaying_it(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 3, 10, 0, tzinfo=UTC)]
    state_path = (tmp_path / "bridge-final-claim.json").resolve()
    store = JsonFileBrowserBridgeStateStore(state_path)
    bridge = BrowserTaskBridge(state_store=store, now=lambda: clock[0])
    (task,) = await bridge.submit_many(
        (
            submission(
                BrowserProvider.CTRIP,
                BrowserVertical.FLIGHT,
                max_attempts=1,
            ),
        )
    )
    await bridge.claim("companion-before-crash")

    recovered = BrowserTaskBridge(state_store=store, now=lambda: clock[0])
    failed = await recovered.get(task.id)

    assert failed.state == BrowserTaskState.FAILED
    assert failed.failure is not None
    assert failed.failure.code == BrowserFailureCode.TIMEOUT
    assert failed.failure.details == {"recovered_after_restart": True}
    assert await recovered.claim("companion-after-restart") == ()


def test_corrupted_persisted_bridge_state_fails_closed(tmp_path: Path) -> None:
    state_path = (tmp_path / "bridge-corrupt.json").resolve()
    state_path.write_text('{"schema_version":"wrong","tasks":[', encoding="utf-8")

    with pytest.raises(BrowserBridgeError, match="browser bridge state cannot be loaded"):
        BrowserTaskBridge(state_store=JsonFileBrowserBridgeStateStore(state_path))


@pytest.mark.asyncio
async def test_reload_control_runs_drain_accept_apply_closed_loop() -> None:
    bridge = BrowserTaskBridge()
    companion_id = "chrome-mv3-control-fixture"
    (task,) = await bridge.submit_many(
        (submission(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),)
    )
    claimed = await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    assert len(claimed.leases) == 1
    requested = await bridge.request_reload(
        companion_id,
        idempotency_key="reload-build-0001",
        request=reload_request_body(),
    )
    assert requested.state == BrowserCompanionControlState.DRAINING

    draining = await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    assert draining.leases == ()
    assert draining.control is None

    await bridge.complete(
        task.id,
        claimed.leases[0].claim_token,
        BrowserTaskCompletion(
            state=BrowserTaskState.SUCCEEDED,
            quotes=(quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT),),
        ),
    )
    dispatch = await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    assert dispatch.leases == ()
    assert dispatch.control is not None
    assert dispatch.control.action.value == "reload_extension"
    assert dispatch.control.expected_runtime_instance_id == OLD_RUNTIME_INSTANCE_ID
    assert dispatch.control.target_build_sha256 == TARGET_BUILD_SHA256

    accepted = await bridge.record_reload_receipt(
        BrowserCompanionReloadReceipt(
            companion_id=companion_id,
            request_id=requested.id,
            receipt_token=dispatch.control.receipt_token,
            delivery_generation=dispatch.control.delivery_generation,
            state=BrowserCompanionReloadReceiptState.ACCEPTED,
            build_identity=companion_build(OLD_BUILD_SHA256),
            runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
        )
    )
    assert accepted.state == BrowserCompanionControlState.ACCEPTED

    applied = await bridge.record_reload_receipt(
        BrowserCompanionReloadReceipt(
            companion_id=companion_id,
            request_id=requested.id,
            receipt_token=dispatch.control.receipt_token,
            delivery_generation=dispatch.control.delivery_generation,
            state=BrowserCompanionReloadReceiptState.APPLIED,
            build_identity=companion_build(TARGET_BUILD_SHA256),
            runtime_instance_id=NEW_RUNTIME_INSTANCE_ID,
            previous_runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
        )
    )
    assert applied.state == BrowserCompanionControlState.APPLIED
    assert applied.observed_build_sha256 == TARGET_BUILD_SHA256
    assert applied.observed_runtime_instance_id == NEW_RUNTIME_INSTANCE_ID


@pytest.mark.asyncio
async def test_reload_receipts_reject_wrong_token_same_runtime_and_wrong_build() -> None:
    bridge = BrowserTaskBridge()
    companion_id = "chrome-mv3-receipt-fixture"
    await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    requested = await bridge.request_reload(
        companion_id,
        idempotency_key="reload-receipt-0001",
        request=reload_request_body(),
    )
    dispatch = await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    assert dispatch.control is not None
    base = BrowserCompanionReloadReceipt(
        companion_id=companion_id,
        request_id=requested.id,
        receipt_token=dispatch.control.receipt_token,
        delivery_generation=dispatch.control.delivery_generation,
        state=BrowserCompanionReloadReceiptState.ACCEPTED,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    with pytest.raises(BrowserCompanionControlError, match="token or delivery"):
        await bridge.record_reload_receipt(
            base.model_copy(update={"receipt_token": "x" * 40})
        )
    await bridge.record_reload_receipt(base)
    with pytest.raises(BrowserCompanionControlError, match="new runtime"):
        await bridge.record_reload_receipt(
            base.model_copy(
                update={
                    "state": BrowserCompanionReloadReceiptState.APPLIED,
                    "build_identity": companion_build(TARGET_BUILD_SHA256),
                }
            )
        )
    with pytest.raises(BrowserCompanionControlError, match="reload target"):
        await bridge.record_reload_receipt(
            base.model_copy(
                update={
                    "state": BrowserCompanionReloadReceiptState.APPLIED,
                    "build_identity": companion_build("3" * 64),
                    "runtime_instance_id": NEW_RUNTIME_INSTANCE_ID,
                }
            )
        )


@pytest.mark.asyncio
async def test_reload_idempotency_and_persistence_never_store_plain_receipt_token(
    tmp_path: Path,
) -> None:
    state_path = (tmp_path / "bridge-control-state.json").resolve()
    store = JsonFileBrowserBridgeStateStore(state_path)
    bridge = BrowserTaskBridge(state_store=store)
    companion_id = "chrome-mv3-persist-fixture"
    await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    first = await bridge.request_reload(
        companion_id,
        idempotency_key="reload-persist-0001",
        request=reload_request_body(),
    )
    repeated = await bridge.request_reload(
        companion_id,
        idempotency_key="reload-persist-0001",
        request=reload_request_body(),
    )
    assert repeated.id == first.id
    with pytest.raises(BrowserCompanionControlError, match="different reload"):
        await bridge.request_reload(
            companion_id,
            idempotency_key="reload-persist-0001",
            request=reload_request_body().model_copy(
                update={"target_build_sha256": "4" * 64}
            ),
        )
    dispatch = await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    assert dispatch.control is not None
    await bridge.record_reload_receipt(
        BrowserCompanionReloadReceipt(
            companion_id=companion_id,
            request_id=first.id,
            receipt_token=dispatch.control.receipt_token,
            delivery_generation=dispatch.control.delivery_generation,
            state=BrowserCompanionReloadReceiptState.ACCEPTED,
            build_identity=companion_build(OLD_BUILD_SHA256),
            runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
        )
    )
    serialized = state_path.read_text(encoding="utf-8")
    assert dispatch.control.receipt_token not in serialized
    assert "receipt_token_sha256" in serialized
    assert json.loads(serialized)["schema_version"] == "tripchord-browser-bridge-state-v2"

    recovered = BrowserTaskBridge(state_store=store)
    applied = await recovered.record_reload_receipt(
        BrowserCompanionReloadReceipt(
            companion_id=companion_id,
            request_id=first.id,
            receipt_token=dispatch.control.receipt_token,
            delivery_generation=dispatch.control.delivery_generation,
            state=BrowserCompanionReloadReceiptState.APPLIED,
            build_identity=companion_build(TARGET_BUILD_SHA256),
            runtime_instance_id=NEW_RUNTIME_INSTANCE_ID,
            previous_runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
        )
    )
    assert applied.state == BrowserCompanionControlState.APPLIED


@pytest.mark.asyncio
async def test_reload_http_control_auth_and_receipt_boundary() -> None:
    bridge_token = "bridge-token-for-control-tests-123456"
    control_token = "separate-control-token-for-tests-123456"
    companion_id = "chrome-mv3-http-control"
    bridge = BrowserTaskBridge()
    await bridge.claim_response(
        companion_id,
        build_identity=companion_build(OLD_BUILD_SHA256),
        runtime_instance_id=OLD_RUNTIME_INSTANCE_ID,
    )
    app = create_browser_bridge_app(
        bridge,
        bridge_token=bridge_token,
        control_token=control_token,
    )
    payload = reload_request_body().model_dump(mode="json")
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        bridge_headers = {BRIDGE_TOKEN_HEADER: bridge_token}
        denied = await client.post(
            f"/v1/companions/{companion_id}/reload-requests",
            headers={
                **bridge_headers,
                IDEMPOTENCY_KEY_HEADER: "reload-http-0001",
            },
            json=payload,
        )
        created = await client.post(
            f"/v1/companions/{companion_id}/reload-requests",
            headers={
                **bridge_headers,
                CONTROL_TOKEN_HEADER: control_token,
                IDEMPOTENCY_KEY_HEADER: "reload-http-0001",
            },
            json=payload,
        )
        dispatched = await client.post(
            "/v1/tasks/claim",
            headers=bridge_headers,
            json={
                "companion_id": companion_id,
                "build_identity": companion_build(OLD_BUILD_SHA256).model_dump(
                    mode="json"
                ),
                "runtime_instance_id": OLD_RUNTIME_INSTANCE_ID,
            },
        )
        control = dispatched.json()["control"]
        accepted = await client.post(
            "/v1/companions/control/receipt",
            headers=bridge_headers,
            json={
                "companion_id": companion_id,
                "request_id": created.json()["id"],
                "receipt_token": control["receipt_token"],
                "delivery_generation": control["delivery_generation"],
                "state": "accepted",
                "build_identity": companion_build(OLD_BUILD_SHA256).model_dump(
                    mode="json"
                ),
                "runtime_instance_id": OLD_RUNTIME_INSTANCE_ID,
            },
        )

    assert denied.status_code == 403
    assert created.status_code == 200
    assert dispatched.status_code == 200
    assert dispatched.json()["leases"] == []
    assert control["action"] == "reload_extension"
    assert accepted.status_code == 200
    assert accepted.json()["state"] == "accepted"


def test_browser_bridge_factory_rejects_collapsed_control_credential() -> None:
    shared_token = "shared-token-that-must-not-cross-boundaries-0001"
    with pytest.raises(ValueError, match="must be distinct"):
        create_browser_bridge_app(
            bridge_token=shared_token,
            control_token=shared_token,
        )


@pytest.mark.asyncio
async def test_terminal_records_are_bounded_and_age_pruned() -> None:
    clock = [datetime(2026, 8, 3, 10, 0, tzinfo=UTC)]
    bridge = BrowserTaskBridge(
        max_terminal_records=2,
        terminal_retention_seconds=600,
        now=lambda: clock[0],
    )
    completed_ids: list[str] = []
    for provider in (
        BrowserProvider.CTRIP,
        BrowserProvider.QUNAR,
        BrowserProvider.CTRIP,
    ):
        (task,) = await bridge.submit_many((submission(provider, BrowserVertical.FLIGHT),))
        (lease,) = await bridge.claim("companion")
        await bridge.complete(
            task.id,
            lease.claim_token,
            BrowserTaskCompletion(
                state=BrowserTaskState.SUCCEEDED,
                quotes=(
                    quote(provider, BrowserVertical.FLIGHT).model_copy(
                        update={"captured_at": clock[0]}
                    ),
                ),
            ),
        )
        completed_ids.append(task.id)
        clock[0] += timedelta(seconds=1)

    with pytest.raises(BrowserTaskNotFoundError):
        await bridge.get(completed_ids[0])
    assert (await bridge.get(completed_ids[1])).state == BrowserTaskState.SUCCEEDED

    clock[0] += timedelta(seconds=601)
    await bridge.submit_many((submission(BrowserProvider.CTRIP, BrowserVertical.LODGING),))
    with pytest.raises(BrowserTaskNotFoundError):
        await bridge.get(completed_ids[1])


def test_quote_and_search_urls_must_match_the_selected_provider() -> None:
    normalized_codes = BrowserSearchQuery(
        origin="杭州",
        destination="马累",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        origin_code=" hgh ",
        destination_code="mle",
    )
    assert normalized_codes.origin_code == "HGH"
    assert normalized_codes.destination_code == "MLE"
    for provider, builder in (
        (BrowserProvider.CTRIP, ctrip_trusted_flight_search_url),
        (BrowserProvider.FLIGGY, fliggy_trusted_flight_search_url),
        (BrowserProvider.QUNAR, qunar_trusted_flight_search_url),
        (BrowserProvider.TONGCHENG, tongcheng_trusted_flight_search_url),
    ):
        trusted_query = normalized_codes.model_copy(
            update={"search_url": builder(normalized_codes)}
        )
        accepted = BrowserTaskSubmission(
            provider=provider,
            kind=BrowserVertical.FLIGHT,
            query=trusted_query,
        )
        assert accepted.query.search_url == builder(normalized_codes)
        with pytest.raises(ValidationError, match="audited provider search contract"):
            BrowserTaskSubmission(
                provider=provider,
                kind=BrowserVertical.FLIGHT,
                query=trusted_query.model_copy(
                    update={"search_url": f"{trusted_query.search_url}&tracking=unexpected"}
                ),
            )
        with pytest.raises(ValidationError, match="selected provider"):
            BrowserTaskSubmission(
                provider=provider,
                kind=BrowserVertical.FLIGHT,
                query=trusted_query.model_copy(
                    update={"search_url": f"{trusted_query.search_url}&coupon=secret"}
                ),
            )
    with pytest.raises(ValidationError, match="three-letter IATA"):
        BrowserSearchQuery(
            origin="杭州",
            destination="马累",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 30),
            origin_code="杭州",
        )
    with pytest.raises(ValidationError, match="selected provider"):
        BrowserTaskSubmission(
            provider=BrowserProvider.CTRIP,
            kind=BrowserVertical.FLIGHT,
            query=BrowserSearchQuery(
                origin="杭州",
                destination="马累",
                start_date=date(2026, 8, 23),
                end_date=date(2026, 8, 30),
                search_url="https://flight.qunar.com/site/roundtrip_list.htm",
            ),
        )
    with pytest.raises(ValidationError, match="page_url"):
        BrowserQuote.model_validate(
            {
                **quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT).model_dump(),
                "page_url": "https://evil.example/collect",
            }
        )
    with pytest.raises(ValidationError, match="browser secrets"):
        BrowserSearchQuery(
            destination="马累",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 30),
            options={"coupon_code": "forbidden"},
        )
    with pytest.raises(ValidationError, match="selected provider"):
        BrowserTaskSubmission(
            provider=BrowserProvider.CTRIP,
            kind=BrowserVertical.FLIGHT,
            query=BrowserSearchQuery(
                origin="杭州",
                destination="马累",
                start_date=date(2026, 8, 23),
                end_date=date(2026, 8, 30),
                search_url="https://flights.ctrip.com/order/create",
            ),
        )


def test_fliggy_official_domain_family_accepts_hk_but_not_other_alibaba_hosts() -> None:
    source = quote(
        BrowserProvider.FLIGGY,
        BrowserVertical.FLIGHT,
    ).model_dump(mode="python")
    accepted = BrowserQuote.model_validate(
        {
            **source,
            "page_url": "https://www.fliggy.hk/",
        }
    )
    assert accepted.page_url == "https://www.fliggy.hk/"

    accepted_subdomain = BrowserQuote.model_validate(
        {
            **source,
            "page_url": "https://flight.fliggy.hk/results",
        }
    )
    assert accepted_subdomain.page_url == "https://flight.fliggy.hk/results"

    for outside_url in (
        "https://fliggy.hk.evil.example/collect",
        "https://www.taobao.com/travel",
        "https://travel.alibaba.com/",
    ):
        with pytest.raises(ValidationError, match="page_url"):
            BrowserQuote.model_validate(
                {
                    **source,
                    "page_url": outside_url,
                }
            )


def test_lodging_checkout_date_is_not_misclassified_as_transaction_checkout() -> None:
    fliggy_source = quote(
        BrowserProvider.FLIGGY,
        BrowserVertical.LODGING,
    ).model_dump(mode="python")
    fliggy_url = (
        "https://hotel.fliggy.com/hotel_list3.htm"
        "?city=933081&checkIn=2026-08-01&checkOut=2026-08-05"
    )
    accepted_fliggy = BrowserQuote.model_validate(
        {**fliggy_source, "page_url": fliggy_url}
    )
    assert accepted_fliggy.page_url == fliggy_url

    fliggy_detail_url = (
        "https://hotel.fliggy.com/hotel_detail2.htm"
        "?shid=50420706&city=933081&checkIn=2026-08-01"
        "&checkOut=2026-08-05&roomNum=1&aNum_1=2&cNum_1=0"
    )
    accepted_fliggy_detail = BrowserQuote.model_validate(
        {**fliggy_source, "page_url": fliggy_detail_url}
    )
    assert accepted_fliggy_detail.page_url == fliggy_detail_url

    ctrip_source = quote(
        BrowserProvider.CTRIP,
        BrowserVertical.LODGING,
    ).model_dump(mode="python")
    ctrip_url = (
        "https://hotels.ctrip.com/hotels/detail/"
        "?hotelId=6210622&checkIn=2026-08-01&checkOut=2026-08-05"
    )
    accepted_ctrip = BrowserQuote.model_validate(
        {**ctrip_source, "page_url": ctrip_url}
    )
    assert accepted_ctrip.page_url == ctrip_url

    for rejected in (
        "https://hotel.fliggy.com/hotel_list3.htm?checkOut=2026-08-05",
        (
            "https://hotel.fliggy.com/hotel_list3.htm"
            "?checkIn=2026-08-05&checkOut=2026-08-01"
        ),
        (
            "https://hotel.fliggy.com/checkout"
            "?checkIn=2026-08-01&checkOut=2026-08-05"
        ),
        (
            "https://hotel.fliggy.com/hotel_list3.htm"
            "?checkIn=2026-08-01&checkOut=2026-08-05&order=create"
        ),
    ):
        with pytest.raises(ValidationError, match="page_url"):
            BrowserQuote.model_validate(
                {**fliggy_source, "page_url": rejected}
            )


def test_qunar_trusted_flight_url_uses_audited_names_and_exact_percent_encoding() -> None:
    canonical = BrowserSearchQuery(
        origin="Hangzhou",
        destination="Malé",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 5),
        adults=2,
        origin_code="HGH",
        destination_code="MLE",
    )

    assert qunar_trusted_flight_search_url(canonical) == (
        "https://flight.qunar.com/twell/flight/Search.jsp"
        "?from=flight_int_search&showTotalPr=0&searchType=RoundTripFlight"
        "&fromCity=%E6%9D%AD%E5%B7%9E&toCity=%E9%A9%AC%E7%B4%AF"
        "&adultNum=2&childNum=0&fromDate=2026-08-01&toDate=2026-08-05"
    )
    assert qunar_trusted_flight_search_url(
        canonical.model_copy(update={"destination": "马尔代夫"})
    ) == (
        "https://flight.qunar.com/twell/flight/Search.jsp"
        "?from=flight_int_search&showTotalPr=0&searchType=RoundTripFlight"
        "&fromCity=%E6%9D%AD%E5%B7%9E&toCity=%E9%A9%AC%E7%B4%AF"
        "&adultNum=2&childNum=0&fromDate=2026-08-01&toDate=2026-08-05"
    )
    for update in (
        {"origin": "上海"},
        {"destination": "东京"},
        {"origin_code": "PVG"},
        {"destination_code": "NRT"},
    ):
        with pytest.raises(ValueError, match="audited"):
            qunar_trusted_flight_search_url(canonical.model_copy(update=update))


def test_tongcheng_trusted_flight_url_encodes_official_book1_search_contract() -> None:
    canonical = BrowserSearchQuery(
        origin="Hangzhou",
        destination="Malé",
        start_date=date(2026, 8, 19),
        end_date=date(2026, 8, 25),
        adults=2,
        origin_code="HGH",
        destination_code="MLE",
    )

    assert tongcheng_trusted_flight_search_url(canonical) == (
        "https://www.ly.com/eliflight/book1.html"
        "?para=HGH*MLE*2026-08-19*2026-08-25*RT*2_0_0*Y%7CS%7CC%7CF"
        "&departureCity=%E6%9D%AD%E5%B7%9E"
        "&arrivalCity=%E9%A9%AC%E7%B4%AF"
    )
    for update in (
        {"origin": "上海"},
        {"destination": "东京"},
        {"origin_code": "PVG"},
        {"destination_code": "NRT"},
    ):
        with pytest.raises(ValueError, match="audited"):
            tongcheng_trusted_flight_search_url(canonical.model_copy(update=update))


def test_tongcheng_trusted_lodging_url_is_exact_and_submission_allows_it() -> None:
    canonical = BrowserSearchQuery(
        destination="胡鲁马累",
        start_date=date(2026, 9, 4),
        end_date=date(2026, 9, 6),
        adults=2,
        rooms=1,
        options={
            "expected_package_area": "airport_island",
            "expected_lodging_place_key": "hulhumale",
            "segment": "first",
        },
    )
    expected = (
        "https://www.ly.com/hotel/hotellist"
        "?city=110018578&inDate=2026-09-04&outDate=2026-09-06"
        "&adultsNumber=2&roomNum=1&intl=1"
    )
    assert tongcheng_trusted_lodging_search_url(canonical) == expected
    accepted = BrowserTaskSubmission(
        provider=BrowserProvider.TONGCHENG,
        kind=BrowserVertical.LODGING,
        query=canonical.model_copy(update={"search_url": expected}),
    )
    assert accepted.query.search_url == expected
    with pytest.raises(ValidationError, match="audited provider search contract"):
        BrowserTaskSubmission(
            provider=BrowserProvider.TONGCHENG,
            kind=BrowserVertical.LODGING,
            query=canonical.model_copy(
                update={"search_url": f"{expected}&tracking=unexpected"}
            ),
        )


def test_success_requires_quote_and_blocked_requires_gate_failure() -> None:
    with pytest.raises(ValidationError, match="at least one quote"):
        BrowserTaskCompletion(state=BrowserTaskState.SUCCEEDED)
    with pytest.raises(ValidationError, match="reserved"):
        BrowserTaskCompletion(
            state=BrowserTaskState.BLOCKED,
            failure=BrowserFailure(
                code=BrowserFailureCode.DOM_DRIFT,
                message="页面结构变化",
                captured_at=datetime.now(UTC),
            ),
        )
    with pytest.raises(ValidationError, match="missing required fields"):
        BrowserQuote(
            provider=BrowserProvider.CTRIP,
            kind=BrowserVertical.FLIGHT,
            page_url="https://flights.ctrip.com/results",
            captured_at=datetime.now(UTC),
            parser_version="tripchord-visible-dom-v3",
            visible_evidence="{}",
            evidence_sha256="a" * 64,
            currency="CNY",
            amount=Decimal("1000"),
            price_basis=QuotePriceBasis.PER_PERSON,
            taxes_included=None,
            title="不完整报价",
            details={},
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("outbound_departure_at", None, "non-empty ISO timestamp"),
        ("outbound_arrival_at", "", "non-empty ISO timestamp"),
        ("return_departure_at", "2026-08-30T10:45:00", "explicit timezone"),
        ("return_arrival_at", "not-a-time", "ISO timestamp"),
    ),
)
def test_flight_quote_requires_four_non_empty_timezone_aware_timestamps(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT).model_dump(mode="python")
    raw["details"][field] = value

    with pytest.raises(ValidationError, match=message):
        BrowserQuote.model_validate(raw)


@pytest.mark.parametrize(
    ("provider", "workflow", "party_status", "actions"),
    (
        (
            BrowserProvider.CTRIP,
            "staged_outbound_return",
            "confirmed_for_party",
            [{"action": "search"}, {"action": "select_outbound"}],
        ),
        (
            BrowserProvider.FLIGGY,
            "staged_outbound_return",
            "comparison_only",
            [{"action": "search"}, {"action": "select_outbound"}],
        ),
        (
            BrowserProvider.QUNAR,
            "combined_roundtrip_card",
            "confirmed_for_party",
            [{"action": "search"}],
        ),
    ),
)
def test_flight_quote_enforces_provider_workflow_contract(
    provider: BrowserProvider,
    workflow: str,
    party_status: str,
    actions: list[dict[str, str]],
) -> None:
    result = quote(provider, BrowserVertical.FLIGHT)

    assert result.details["workflow_kind"] == workflow
    assert result.details["party_availability_status"] == party_status
    assert result.details["action_trace"] == actions


def test_observed_party_context_is_valid_availability_without_price_proof() -> None:
    raw = quote(BrowserProvider.QUNAR, BrowserVertical.FLIGHT).model_dump(mode="python")
    raw["details"]["party_availability_status"] = "observed_party_context"

    accepted = BrowserQuote.model_validate(raw)

    assert accepted.details["party_availability_status"] == "observed_party_context"


@pytest.mark.parametrize(
    ("provider", "field", "value", "message"),
    (
        (
            BrowserProvider.CTRIP,
            "workflow_kind",
            "combined_roundtrip_card",
            "staged_outbound_return",
        ),
        (
            BrowserProvider.QUNAR,
            "workflow_kind",
            "staged_outbound_return",
            "combined_roundtrip_card",
        ),
        (
            BrowserProvider.FLIGGY,
            "party_availability_status",
            "confirmed_for_party",
            "comparison_only",
        ),
        (
            BrowserProvider.QUNAR,
            "action_trace",
            [{"action": "search"}, {"action": "select_outbound"}],
            "must not select",
        ),
    ),
)
def test_flight_quote_rejects_cross_provider_workflow_claims(
    provider: BrowserProvider,
    field: str,
    value: object,
    message: str,
) -> None:
    raw = quote(provider, BrowserVertical.FLIGHT).model_dump(mode="python")
    raw["details"][field] = value

    with pytest.raises(ValidationError, match=message):
        BrowserQuote.model_validate(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("combination_status", "outbound_preview", "round_trip_complete"),
        ("journey_price_scope", "outbound", "round_trip"),
        ("price_finality", "preview_only", "final_for_combination"),
        (
            "action_trace",
            [{"action": "search"}, {"action": "book"}],
            "read-only allowlist",
        ),
        (
            "action_trace",
            [{"action": "search"}, {"action": "select_outbound", "label": "预订"}],
            "transaction or account",
        ),
        (
            "action_trace",
            [{"action": "search"}] * 9,
            "between one and eight",
        ),
    ),
)
def test_flight_quote_rejects_preview_or_transaction_action(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT).model_dump(mode="python")
    raw["details"][field] = value

    with pytest.raises(ValidationError, match=message):
        BrowserQuote.model_validate(raw)


def test_browser_quote_requires_exact_production_parser_v3() -> None:
    raw = quote(BrowserProvider.QUNAR, BrowserVertical.FLIGHT).model_dump(mode="python")
    raw["parser_version"] = "tripchord-visible-dom-v2"

    with pytest.raises(ValidationError, match="production visible-DOM parser"):
        BrowserQuote.model_validate(raw)


@pytest.mark.asyncio
async def test_local_http_bridge_requires_pairing_token() -> None:
    token = "test-bridge-token-that-is-long-enough-123"
    app = create_browser_bridge_app(bridge_token=token)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        unauthorized = await client.post(
            "/v1/tasks/claim",
            json={"companion_id": "fixture-companion"},
        )
        authorized = await client.post(
            "/v1/tasks/claim",
            headers={BRIDGE_TOKEN_HEADER: token},
            json={"companion_id": "fixture-companion"},
        )
        unauthorized_status = await client.get("/v1/companions/status")
        authorized_status = await client.get(
            "/v1/companions/status",
            headers={BRIDGE_TOKEN_HEADER: token},
        )
        unauthorized_heartbeat = await client.post(
            "/v1/companions/heartbeat",
            json={
                "companion_id": "fixture-companion",
                "providers": ["ctrip", "fliggy", "qunar"],
            },
        )
        authorized_heartbeat = await client.post(
            "/v1/companions/heartbeat",
            headers={BRIDGE_TOKEN_HEADER: token},
            json={
                "companion_id": "fixture-companion",
                "providers": ["ctrip", "fliggy", "qunar"],
            },
        )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {"leases": []}
    assert unauthorized_status.status_code == 401
    assert authorized_status.status_code == 200
    assert authorized_status.json()["status"] == "connected"
    assert authorized_status.json()["companions"][0]["providers"] == [
        provider.value for provider in BrowserProvider
    ]
    assert unauthorized_heartbeat.status_code == 401
    assert authorized_heartbeat.status_code == 200
    assert authorized_heartbeat.json()["is_fresh"] is True
    serialized = authorized_status.text.lower()
    for forbidden in ("cookie", "profile", "account", "tab_url", "page_url"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_local_http_bridge_cors_only_allows_extension_and_loopback_origins() -> None:
    token = "test-bridge-token-that-is-long-enough-cors"
    app = create_browser_bridge_app(bridge_token=token)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        allowed = await client.options(
            "/v1/tasks/claim",
            headers={
                "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": BRIDGE_TOKEN_HEADER,
            },
        )
        rejected = await client.options(
            "/v1/tasks/claim",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": BRIDGE_TOKEN_HEADER,
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"].startswith("chrome-extension://")
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers


@pytest.mark.asyncio
async def test_local_http_bridge_round_trip_and_remote_rejection() -> None:
    token = "test-bridge-token-that-is-long-enough-456"
    app = create_browser_bridge_app(bridge_token=token)
    headers = {BRIDGE_TOKEN_HEADER: token}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        submitted = await client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "tasks": [
                    submission(
                        BrowserProvider.CTRIP,
                        BrowserVertical.FLIGHT,
                    ).model_dump(mode="json")
                ]
            },
        )
        claimed = await client.post(
            "/v1/tasks/claim",
            headers=headers,
            json={"companion_id": "fixture-companion"},
        )
        lease = claimed.json()["leases"][0]
        completed = await client.post(
            f"/v1/tasks/{lease['task_id']}/complete",
            headers=headers,
            json={
                "claim_token": lease["claim_token"],
                "completion": {
                    "state": "succeeded",
                    "quotes": [
                        quote(
                            BrowserProvider.CTRIP,
                            BrowserVertical.FLIGHT,
                        ).model_dump(mode="json")
                    ],
                },
            },
        )

    assert submitted.status_code == 200
    assert claimed.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["state"] == "succeeded"
    assert completed.json()["quotes"][0]["details"]["connection_text"] == "香港中转"

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://127.0.0.1",
    ) as remote:
        rejected = await remote.get("/health")
        rejected_status = await remote.get(
            "/v1/companions/status",
            headers=headers,
        )

    assert rejected.status_code == 403
    assert rejected_status.status_code == 403
