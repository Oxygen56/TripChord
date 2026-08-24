from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime

import pytest
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentSystem,
    _browser_wait_timeout_seconds,
    _remaining_absolute_delay_ms,
)
from tripchord.planning.package import PackageIntent
from tripchord.providers.browser_bridge import (
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskSnapshot,
    BrowserTaskSubmission,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
START = date(2026, 8, 23)
END = date(2026, 8, 30)


class _WaveBudgetProbeBridge(BrowserTaskBridge):
    def __init__(self) -> None:
        super().__init__(now=lambda: NOW)
        self.positions: dict[str, int] = {}
        self.wait_budgets: dict[str, float] = {}
        self.lease_by_id: dict[str, int] = {}
        self.all_submitted = asyncio.Event()

    async def submit_many(
        self,
        submissions: Iterable[BrowserTaskSubmission],
    ) -> tuple[BrowserTaskSnapshot, ...]:
        snapshots = await super().submit_many(submissions)
        for snapshot in snapshots:
            self.positions[snapshot.id] = len(self.positions)
        for submission, snapshot in zip(submissions, snapshots, strict=True):
            self.lease_by_id[snapshot.id] = submission.timeout_seconds
        # Flight-first scheduling admits the three flight sources before any
        # lodging source can become runnable.
        if len(self.positions) == 3:
            self.all_submitted.set()
        return snapshots

    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        ids = tuple(task_ids)
        assert len(ids) == 1
        await asyncio.wait_for(self.all_submitted.wait(), timeout=2)
        task_id = ids[0]
        self.wait_budgets[task_id] = timeout_seconds
        # Production waits for one public task handle, but its acquisition may
        # still be queued behind earlier graph sources before receiving a lease.
        minimum_budget = float(self.lease_by_id[task_id] + 1)
        if timeout_seconds < minimum_budget:
            raise TimeoutError(f"task requires {minimum_budget:g} seconds")
        return await self.cancel_many(
            ids,
            reason="wave-budget probe reached a bounded terminal result",
        )


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="wave-budget-hgh-mle",
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


@pytest.mark.parametrize(
    ("source_task_count", "expected_seconds"),
    [(1, 16.0), (6, 16.0), (7, 32.0), (15, 48.0)],
)
def test_browser_wait_budget_covers_all_bounded_waves(
    source_task_count: int,
    expected_seconds: float,
) -> None:
    assert (
        _browser_wait_timeout_seconds(
            15,
            source_task_count=source_task_count,
        )
        == expected_seconds
    )


@pytest.mark.parametrize(
    ("configured_ms", "elapsed_seconds", "expected_remaining_ms"),
    [
        (40_000, 0, 40_000),
        (80_000, 55, 25_000),
        (120_000, 155, 0),
    ],
)
def test_source_offsets_remain_absolute_across_supervisor_waves(
    configured_ms: int,
    elapsed_seconds: float,
    expected_remaining_ms: int,
) -> None:
    remaining, elapsed_ms = _remaining_absolute_delay_ms(
        configured_ms,
        schedule_started_monotonic=100,
        current_monotonic=100 + elapsed_seconds,
    )

    assert remaining == expected_remaining_ms
    assert elapsed_ms == int(elapsed_seconds * 1000)


@pytest.mark.asyncio
async def test_admitted_flight_sources_keep_the_full_graph_queue_budget() -> None:
    bridge = _WaveBudgetProbeBridge()
    system = LivePackageAgentSystem(bridge, now=lambda: NOW)

    run = await system.run(
        _intent(),
        _query(),
        mode=LiveCoverageMode.STRICT,
        timeout_seconds=15,
    )

    # Flight-first scheduling starts with the flight sources and may retry
    # their bounded attempts before lodging becomes runnable.  Every admitted
    # attempt keeps a caller wait that covers the complete source graph, while
    # each claimed browser attempt retains its frozen 15s lease.
    assert len(bridge.wait_budgets) >= 3
    assert set(bridge.wait_budgets.values()) == {
        _browser_wait_timeout_seconds(
            15,
            source_task_count=len(run.source_task_ids),
        )
    }
    assert all(
        all("TimeoutError" not in reason for reason in coverage.failure_reasons)
        for coverage in run.coverage
    )


@pytest.mark.asyncio
async def test_full_window_screen_submits_flights_without_ota_lodging() -> None:
    bridge = _WaveBudgetProbeBridge()
    system = LivePackageAgentSystem(bridge, now=lambda: NOW)
    query = _query().model_copy(
        update={
            "options": {
                "__tripchord_full_window_flight_screen": True,
                "__tripchord_browser_wait_source_count": 90,
            }
        }
    )

    run = await system.run(
        _intent(),
        query,
        mode=LiveCoverageMode.STRICT,
        timeout_seconds=15,
        model_agents_enabled=False,
    )

    assert len(run.source_task_ids) == 3
    assert run.evidence_scope.value == "full_window_baseline_screen"
    assert all(source_id.endswith("-flight") for source_id in run.source_task_ids)
    assert set(bridge.wait_budgets.values()) == {
        _browser_wait_timeout_seconds(15, source_task_count=90)
    }
