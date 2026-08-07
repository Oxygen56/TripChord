from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tripchord.agents.live_monitor import (
    LiveMonitorCheck,
    LiveMonitorState,
    LiveMonitorStatus,
    LiveQuoteMonitorRegistry,
)
from tripchord.persistence.database import Database
from tripchord.persistence.live_monitors import (
    DbLiveMonitorStore,
    LiveMonitorNotFoundError,
)

NOW = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)


def _status(monitor_id: str, tenant_id: str) -> LiveMonitorStatus:
    return LiveMonitorStatus(
        id=monitor_id,
        run_id="run-fixture",
        state=LiveMonitorState.ACTIVE,
        interval_seconds=3600,
        max_checks=3,
        timeout_seconds=120,
        next_check_at=NOW + timedelta(seconds=3600),
        created_at=NOW,
        updated_at=NOW,
    )


async def _check(status: LiveMonitorStatus, tenant_id: str) -> LiveMonitorCheck:
    assert tenant_id == "tenant-a"
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


@pytest.mark.asyncio
async def test_monitor_status_and_checks_round_trip() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DbLiveMonitorStore(database)
    try:
        status = _status("live-monitor-repo-1", "tenant-a")
        await store.save_status("tenant-a", status)
        check = await _check(status, "tenant-a")
        await store.append_check("tenant-a", status.id, check)

        loaded = await store.get_status("tenant-a", status.id)
        assert loaded.state == LiveMonitorState.ACTIVE
        assert loaded.run_id == "run-fixture"
        assert loaded.check_count == 0  # status check_count is independent of history
        assert loaded.last_check is not None
        assert loaded.last_check.sequence == 1
        assert "不是供应商推送" in loaded.boundary

        with pytest.raises(LiveMonitorNotFoundError):
            await store.get_status("tenant-b", status.id)

        active = await store.list_active()
        assert [tenant for tenant, _ in active] == ["tenant-a"]
        assert active[0][1].id == status.id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_registry_persists_start_check_and_stop() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DbLiveMonitorStore(database)
    registry = LiveQuoteMonitorRegistry(_check, now=lambda: NOW, store=store)
    try:
        status = await registry.start(
            run_id="run-fixture",
            tenant_id="tenant-a",
            interval_seconds=3600,
            max_checks=2,
        )
        persisted = await store.get_status("tenant-a", status.id)
        assert persisted.state == LiveMonitorState.ACTIVE
        assert persisted.boundary == status.boundary

        first = await registry.check_now(status.id, "tenant-a")
        assert first is not None
        assert first.check_count == 1
        persisted2 = await store.get_status("tenant-a", status.id)
        assert persisted2.check_count == 1
        assert persisted2.last_check is not None
        assert persisted2.last_check.sequence == 1

        stopped = await registry.stop(status.id, "tenant-a")
        assert stopped is not None
        assert stopped.state == LiveMonitorState.STOPPED
        persisted3 = await store.get_status("tenant-a", status.id)
        assert persisted3.state == LiveMonitorState.STOPPED
    finally:
        await registry.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_recover_resumes_resolvable_monitor() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DbLiveMonitorStore(database)
    status = _status("live-monitor-recover-1", "tenant-a")
    await store.save_status("tenant-a", status)

    registry = LiveQuoteMonitorRegistry(_check, now=lambda: NOW, store=store)
    try:
        # A fresh registry (simulating a restarted process) rehydrates the
        # ACTIVE monitor whose run context is still resolvable.
        async def resolvable(tenant_id: str, monitor: LiveMonitorStatus) -> bool:
            del tenant_id, monitor
            return True

        recovered = await registry.recover(resolvable)
        assert recovered == 1
        got = await registry.get(status.id, "tenant-a")
        assert got is not None
        assert got.state == LiveMonitorState.ACTIVE
        assert got.check_count == 0
    finally:
        await registry.close()
        await database.dispose()


@pytest.mark.asyncio
async def test_recover_marks_unresolvable_monitor_failed_honestly() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    store = DbLiveMonitorStore(database)
    status = _status("live-monitor-recover-2", "tenant-a")
    await store.save_status("tenant-a", status)

    registry = LiveQuoteMonitorRegistry(_check, now=lambda: NOW, store=store)
    try:

        async def unresolvable(tenant_id: str, monitor: LiveMonitorStatus) -> bool:
            del tenant_id, monitor
            return False

        recovered = await registry.recover(unresolvable)
        assert recovered == 0
        failed = await store.get_status("tenant-a", status.id)
        assert failed.state == LiveMonitorState.FAILED
        assert failed.next_check_at is None
        assert "not recoverable" in failed.last_error
    finally:
        await registry.close()
        await database.dispose()
