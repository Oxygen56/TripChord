from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_system import LiveCoverageMode, LivePackageAgentSystem
from tripchord.planning.package import PackageDecisionState, PackageIntent
from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    LIVE_V5_BROWSER_PROVIDERS,
    BrowserClaimError,
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskCompletion,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    create_browser_bridge_app,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
START = date(2026, 8, 23)
END = date(2026, 8, 30)


def _submission(
    provider: BrowserProvider,
    *,
    timeout_seconds: int = 15,
    max_attempts: int = 2,
) -> BrowserTaskSubmission:
    return BrowserTaskSubmission(
        provider=provider,
        kind=BrowserVertical.LODGING,
        query=BrowserSearchQuery(
            destination="MLE",
            start_date=START,
            end_date=END,
            adults=2,
            rooms=1,
            destination_code="MLE",
        ),
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )


def _failed_completion() -> BrowserTaskCompletion:
    return BrowserTaskCompletion(
        state=BrowserTaskState.FAILED,
        failure=BrowserFailure(
            code=BrowserFailureCode.DOM_DRIFT,
            message="fixture page did not contain a known quote card",
            captured_at=NOW,
        ),
    )


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="cancel-live-hgh-mle",
        origin="HGH",
        destination="MLE",
        start_date=START,
        end_date=END,
        adults=2,
        rooms=1,
    )


def _query() -> BrowserSearchQuery:
    return BrowserSearchQuery(
        origin="HGH",
        destination="MLE",
        start_date=START,
        end_date=END,
        adults=2,
        rooms=1,
        origin_code="HGH",
        destination_code="MLE",
    )


class _TrackingBridge(BrowserTaskBridge):
    def __init__(self) -> None:
        super().__init__(now=lambda: NOW)
        self.submitted_ids: list[str] = []
        self.all_submitted = asyncio.Event()

    async def submit_many(
        self,
        submissions: Iterable[BrowserTaskSubmission],
    ) -> tuple[BrowserTaskSnapshot, ...]:
        snapshots = await super().submit_many(submissions)
        self.submitted_ids.extend(snapshot.id for snapshot in snapshots)
        if len(self.submitted_ids) >= 11:
            self.all_submitted.set()
        return snapshots


class _ImmediateTimeoutBridge(_TrackingBridge):
    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        del task_ids, timeout_seconds
        raise TimeoutError("fixture source timeout")


@pytest.mark.asyncio
async def test_cancel_many_makes_queued_tasks_terminal_and_unclaimable() -> None:
    bridge = BrowserTaskBridge(now=lambda: NOW)
    tasks = await bridge.submit_many(
        _submission(provider) for provider in LIVE_V5_BROWSER_PROVIDERS
    )

    cancelled = await bridge.cancel_many(
        (task.id for task in tasks),
        reason="caller stopped the live request",
    )

    assert [task.state for task in cancelled] == [BrowserTaskState.CANCELLED] * 3
    assert all(task.failure is not None for task in cancelled)
    assert {task.failure.code for task in cancelled if task.failure is not None} == {
        BrowserFailureCode.CANCELLED
    }
    assert {
        task.failure.details["previous_state"] for task in cancelled if task.failure is not None
    } == {BrowserTaskState.QUEUED.value}
    assert await bridge.claim("companion-after-cancel", limit=6) == ()
    waited = await bridge.wait_many(
        (task.id for task in tasks),
        timeout_seconds=0.01,
    )
    assert tuple(task.id for task in waited) == tuple(task.id for task in tasks)


@pytest.mark.asyncio
async def test_claimed_cancellation_rejects_stale_companion_completion_with_http_409() -> None:
    bridge = BrowserTaskBridge(now=lambda: NOW)
    (task,) = await bridge.submit_many((_submission(BrowserProvider.CTRIP),))
    (lease,) = await bridge.claim("paired-companion", limit=1)
    cancelled = (
        await bridge.cancel_many(
            (task.id,),
            reason="API request timed out",
        )
    )[0]

    assert cancelled.state == BrowserTaskState.CANCELLED
    assert cancelled.claimed_by == "paired-companion"
    assert cancelled.claimed_at == lease.claimed_at
    assert cancelled.created_at <= lease.claimed_at <= cancelled.updated_at
    assert cancelled.failure is not None
    assert cancelled.failure.details["previous_state"] == BrowserTaskState.CLAIMED.value

    token = "browser-cancellation-test-token-123456"
    app = create_browser_bridge_app(bridge, bridge_token=token)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/v1/tasks/{task.id}/complete",
            headers={BRIDGE_TOKEN_HEADER: token},
            json={
                "claim_token": lease.claim_token,
                "completion": _failed_completion().model_dump(mode="json"),
            },
        )

    assert response.status_code == 409
    assert (await bridge.get(task.id)).state == BrowserTaskState.CANCELLED
    assert await bridge.claim("different-companion", limit=6) == ()


@pytest.mark.asyncio
async def test_cancellation_is_safe_across_completion_and_lease_expiry_races() -> None:
    clock = [NOW]
    bridge = BrowserTaskBridge(now=lambda: clock[0])
    completed_task, expiring_task = await bridge.submit_many(
        (
            _submission(BrowserProvider.FLIGGY),
            _submission(BrowserProvider.QUNAR, max_attempts=2),
        )
    )
    first_lease, expiring_lease = await bridge.claim("paired-companion", limit=2)

    completed = await bridge.complete(
        completed_task.id,
        first_lease.claim_token,
        _failed_completion(),
    )
    unchanged = (
        await bridge.cancel_many(
            (completed_task.id,),
            reason="late controller cancellation",
        )
    )[0]
    assert unchanged == completed
    assert unchanged.state == BrowserTaskState.FAILED

    clock[0] += timedelta(seconds=16)
    cancelled = (
        await bridge.cancel_many(
            (expiring_task.id,),
            reason="controller timeout won the expiry race",
        )
    )[0]
    assert cancelled.state == BrowserTaskState.CANCELLED
    assert cancelled.attempt_count == 1
    assert cancelled.failure is not None
    assert cancelled.failure.details["previous_state"] == BrowserTaskState.QUEUED.value
    with pytest.raises(BrowserClaimError, match="active claim"):
        await bridge.complete(
            expiring_task.id,
            expiring_lease.claim_token,
            _failed_completion(),
        )


@pytest.mark.asyncio
async def test_first_six_browser_leases_are_provider_balanced_and_timestamped() -> None:
    tick = [NOW]

    def clock() -> datetime:
        value = tick[0]
        tick[0] += timedelta(milliseconds=1)
        return value

    bridge = BrowserTaskBridge(now=clock)
    await bridge.submit_many(
        _submission(provider) for provider in LIVE_V5_BROWSER_PROVIDERS for _ in range(5)
    )

    leases = await bridge.claim("paired-companion", limit=6)

    assert {
        provider: sum(lease.provider == provider for lease in leases)
        for provider in LIVE_V5_BROWSER_PROVIDERS
    } == {
        BrowserProvider.CTRIP: 3,
        BrowserProvider.QUNAR: 1,
        BrowserProvider.TONGCHENG: 2,
    }
    for lease in leases:
        snapshot = await bridge.get(lease.task_id)
        assert snapshot.claimed_at == lease.claimed_at
        assert snapshot.created_at <= lease.claimed_at <= snapshot.updated_at
    with pytest.raises(ValueError, match="between 1 and 6"):
        await bridge.claim("oversized-companion", limit=7)


@pytest.mark.asyncio
async def test_live_run_cancellation_invalidates_all_claimed_and_queued_tasks() -> None:
    bridge = _TrackingBridge()
    system = LivePackageAgentSystem(bridge, now=lambda: NOW)
    run_task = asyncio.create_task(
        system.run(
            _intent(),
            _query(),
            mode=LiveCoverageMode.STRICT,
            timeout_seconds=15,
        )
    )
    await asyncio.wait_for(bridge.all_submitted.wait(), timeout=2)
    leases = await bridge.claim("paired-companion", limit=6)
    assert len(leases) == 6

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    snapshots = await asyncio.gather(*(bridge.get(task_id) for task_id in bridge.submitted_ids))
    assert len(snapshots) == 11
    assert all(snapshot.state == BrowserTaskState.CANCELLED for snapshot in snapshots)
    assert sum(snapshot.claimed_at is not None for snapshot in snapshots) == 6
    assert await bridge.claim("late-companion", limit=6) == ()


@pytest.mark.asyncio
async def test_source_timeouts_cancel_bridge_tasks_before_degraded_decision() -> None:
    bridge = _ImmediateTimeoutBridge()
    system = LivePackageAgentSystem(bridge, now=lambda: NOW)

    run = await system.run(
        _intent(),
        _query(),
        mode=LiveCoverageMode.STRICT,
        timeout_seconds=15,
    )

    snapshots = await asyncio.gather(*(bridge.get(task_id) for task_id in bridge.submitted_ids))
    assert len(snapshots) == 11
    assert all(snapshot.state == BrowserTaskState.CANCELLED for snapshot in snapshots)
    assert run.decision.state == PackageDecisionState.HUMAN_BLOCK
    assert all(
        any("TimeoutError" in reason for reason in coverage.failure_reasons)
        for coverage in run.coverage
    )
    assert await bridge.claim("late-companion", limit=6) == ()
