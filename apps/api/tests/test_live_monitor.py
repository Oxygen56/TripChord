from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from tripchord.agents.live_monitor import (
    LiveMonitorCheck,
    LiveMonitorState,
    LiveMonitorStatus,
    LiveQuoteMonitorRegistry,
)
from tripchord.agents.live_system import LivePackageAgentRun
from tripchord.main import _maximum_safe_live_monitor_interval_seconds

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_monitor_interval_must_complete_a_component_rotation_before_expiry() -> None:
    component = lambda provider: SimpleNamespace(  # noqa: E731
        provider=provider,
        expires_at=NOW + timedelta(minutes=10),
    )
    run = cast(
        LivePackageAgentRun,
        SimpleNamespace(
            package=SimpleNamespace(
                final_candidate=SimpleNamespace(
                    flight=component("ctrip"),
                    lodgings=(component("qunar"),),
                    transfers=(),
                )
            )
        ),
    )

    # (10 minutes - 30-second safety margin) / two round-robin components.
    assert _maximum_safe_live_monitor_interval_seconds(run, now=NOW) == 285


@pytest.mark.asyncio
async def test_opt_in_monitor_is_tenant_isolated_and_completes_at_bound() -> None:
    seen: list[LiveMonitorStatus] = []

    async def check(status: LiveMonitorStatus, tenant_id: str) -> LiveMonitorCheck:
        assert tenant_id == "tenant-a"
        seen.append(status)
        sequence = status.check_count + 1
        return LiveMonitorCheck(
            sequence=sequence,
            checked_at=NOW,
            target_component_id=f"component-{sequence}",
            event_id=f"event-{sequence}",
            applied_disposition="refresh",
            decision_state="accept",
            package_changed=False,
            summary="只读重核价未发现语义变化",
        )

    registry = LiveQuoteMonitorRegistry(check, now=lambda: NOW)
    status = await registry.start(
        run_id="live-run-fixture",
        tenant_id="tenant-a",
        interval_seconds=3600,
        max_checks=2,
    )
    try:
        assert await registry.get(status.id, "tenant-b") is None
        first = await registry.check_now(status.id, "tenant-a")
        assert first is not None
        assert first.state == LiveMonitorState.ACTIVE
        assert first.check_count == 1
        second = await registry.check_now(status.id, "tenant-a")
        assert second is not None
        assert second.state == LiveMonitorState.COMPLETED
        assert second.check_count == 2
        assert second.next_check_at is None
        assert [item.check_count for item in seen] == [0, 1]
        assert "不是供应商推送" in second.boundary
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_monitor_failure_is_explicit_and_does_not_retry_forever() -> None:
    async def fail(_: LiveMonitorStatus, __: str) -> LiveMonitorCheck:
        raise RuntimeError("fixture bridge unavailable")

    registry = LiveQuoteMonitorRegistry(fail, now=lambda: NOW)
    status = await registry.start(
        run_id="live-run-fixture",
        tenant_id="tenant-a",
        interval_seconds=3600,
        max_checks=3,
    )
    try:
        failed = await registry.check_now(status.id, "tenant-a")
        assert failed is not None
        assert failed.state == LiveMonitorState.FAILED
        assert failed.check_count == 0
        assert failed.last_error == "RuntimeError: fixture bridge unavailable"
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_monitor_can_be_revoked() -> None:
    async def check(_: LiveMonitorStatus, __: str) -> LiveMonitorCheck:
        raise AssertionError("long interval monitor must not run before revocation")

    registry = LiveQuoteMonitorRegistry(check, now=lambda: NOW)
    status = await registry.start(
        run_id="live-run-fixture",
        tenant_id="tenant-a",
        interval_seconds=3600,
        max_checks=3,
    )
    stopped = await registry.stop(status.id, "tenant-a")
    assert stopped is not None
    assert stopped.state == LiveMonitorState.STOPPED
    assert stopped.next_check_at is None
    await registry.close()
