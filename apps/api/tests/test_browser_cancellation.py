from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentSystem,
    _record_scope_cancellation,
    _RunState,
)
from tripchord.agents.models import AgentRole, AgentTask
from tripchord.agents.tools import ToolCall
from tripchord.planning.package import PackageDecisionState, PackageIntent
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
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


class _ControlledSleep:
    """Deterministic sleep that pauses until the test records the tombstone."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        del seconds
        self.started.set()
        await self.release.wait()


def _qunar_lodging_task(*, task_id: str, start_delay_ms: int | None = None) -> AgentTask:
    submission = _submission(BrowserProvider.QUNAR)
    input_: dict = {
        "submission": submission.model_dump(mode="json"),
        "__tripchord_attempt_generation": 0,
    }
    if start_delay_ms is not None:
        input_["start_delay_ms"] = start_delay_ms
    return AgentTask(
        id=task_id,
        role=AgentRole.LODGING,
        goal="only-read qunar lodging quotes",
        allowed_tools=("browser_bridge_search",),
        input=input_,
        max_attempts=1,
    )


@pytest.mark.asyncio
async def test_scope_cancelled_during_start_delay_never_invokes_browser() -> None:
    """Counter-example: a scope cancelled while a source sleeps in its start
    delay must NOT invoke the browser after the delay.

    A delayed source passes the task-start tombstone check (the scope was live
    when it was submitted), then sleeps.  If the user closes the scope during
    that sleep, the post-delay re-check must suppress the tool invoke:
    zero browser/model/network access after cancellation.
    """
    bridge = _TrackingBridge()
    sleep = _ControlledSleep()
    system = LivePackageAgentSystem(bridge, now=lambda: NOW, sleep=sleep)
    state = _RunState(source_task_ids=("delayed-qunar-lodging",))
    tools = system._tool_registry(state, source_task_count=1)
    task = _qunar_lodging_task(
        task_id="delayed-qunar-lodging",
        start_delay_ms=120_000,
    )
    executor = system._source_executor(state)
    run_task = asyncio.create_task(executor(task, None, tools))
    await asyncio.wait_for(sleep.started.wait(), timeout=2)

    # The user closes the lodging scope while the source is still delayed.
    _record_scope_cancellation(
        state,
        ProviderScopeKey(provider="qunar", vertical=ProviderVertical.LODGING),
        generation=0,
        reason="user closed the lodging scope during the start delay",
    )
    sleep.release.set()

    result = await run_task
    assert result.success is True
    assert result.output["scope_cancelled"] is True
    assert result.output["external_tool_called"] is False
    # Zero browser access after the cancellation.
    assert bridge.submitted_ids == []


class _PreservedFailureThenCancelBridge(_TrackingBridge):
    """Attempt 0 fails retryably with a preserved result tab; the scope is
    cancelled while attempt 0 is in flight (recorded before wait_many returns).
    """

    def __init__(self, state: _RunState) -> None:
        super().__init__()
        self._state = state
        self._cancellation_recorded = False

    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        del timeout_seconds
        if not self._cancellation_recorded:
            self._cancellation_recorded = True
            # Simulate the user closing the scope / source timeout landing
            # while attempt 0 is still in flight: the retry decision must
            # observe the tombstone.
            _record_scope_cancellation(
                self._state,
                ProviderScopeKey(provider="qunar", vertical=ProviderVertical.LODGING),
                generation=0,
                reason="source timeout cancelled the lodging scope while attempt 0 was in flight",
            )
        task_id = next(iter(task_ids))
        return (
            BrowserTaskSnapshot(
                id=task_id,
                provider=BrowserProvider.QUNAR,
                kind=BrowserVertical.LODGING,
                query=_submission(BrowserProvider.QUNAR).query,
                state=BrowserTaskState.FAILED,
                created_at=NOW,
                updated_at=NOW,
                attempt_count=1,
                failure=BrowserFailure(
                    code=BrowserFailureCode.TIMEOUT,
                    message=(
                        "browser companion preserved the result tab but could "
                        "not finish extraction before the lease expired"
                    ),
                    retryable=True,
                    captured_at=NOW,
                    details={
                        "preserved_exact_result_tab": {
                            "provider": "qunar",
                            "kind": "lodging",
                            "tab_id": 25,
                            "url": "https://hotel.qunar.com/intl/search.jsp",
                        },
                    },
                ),
            ),
        )


@pytest.mark.asyncio
async def test_preserved_result_tab_retry_is_suppressed_when_scope_cancelled_in_flight() -> None:
    """Counter-example: a preserved-result-tab retry must NOT revive the browser
    after the scope is cancelled while attempt 0 is in flight.

    The search tool's retry (attempt 1, reusing the preserved exact result tab)
    is a real access that must be forbidden after cancellation: it must observe
    the tombstone and suppress the retry submission, so exactly one submission
    reaches the bridge and the caller sees the suppression flag.
    """
    state = _RunState(source_task_ids=("qunar-lodging-retry",))
    bridge = _PreservedFailureThenCancelBridge(state)
    system = LivePackageAgentSystem(bridge, now=lambda: NOW)
    tools = system._tool_registry(state, source_task_count=1)
    submission = _submission(BrowserProvider.QUNAR)
    call = ToolCall(
        id="call:qunar-lodging-retry",
        tool_name="browser_bridge_search",
        task_id="qunar-lodging-retry",
        agent_role=AgentRole.LODGING,
        arguments={
            "submission": submission.model_dump(mode="json"),
            "__tripchord_attempt_generation": 0,
        },
    )

    receipt = await tools.invoke(call)

    assert receipt.success is True
    assert receipt.output["retry_suppressed_by_scope_cancellation"] is True
    # Only attempt 0 reached the bridge — the reuse retry was suppressed.
    assert len(bridge.submitted_ids) == 1
    assert len(receipt.output["attempt_snapshots"]) == 1
