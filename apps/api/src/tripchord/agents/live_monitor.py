from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field, field_validator

from tripchord.domain.common import DomainModel


class LiveMonitorState(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class LiveMonitorCheck(DomainModel):
    sequence: int = Field(ge=1)
    checked_at: datetime
    target_component_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    applied_disposition: str | None = None
    decision_state: str = Field(min_length=1)
    package_changed: bool
    summary: str = Field(min_length=1)

    _validate_checked_at = field_validator("checked_at")(
        lambda value: _timezone_aware(value, "checked_at")
    )


class LiveMonitorStatus(DomainModel):
    id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    state: LiveMonitorState
    interval_seconds: int = Field(ge=1, le=86_400)
    max_checks: int = Field(ge=1, le=10_000)
    timeout_seconds: int = Field(default=120, ge=15, le=300)
    check_count: int = Field(default=0, ge=0)
    next_check_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_check: LiveMonitorCheck | None = None
    last_error: str | None = None
    boundary: str = (
        "用户显式开启后的本机周期性只读重核价；每轮只重查一个当前整包组件，"
        "不是供应商推送、库存锁定、后台常驻服务或自动下单。进程重启后监控需重新开启。"
    )

    _validate_created_at = field_validator("created_at")(
        lambda value: _timezone_aware(value, "created_at")
    )
    _validate_updated_at = field_validator("updated_at")(
        lambda value: _timezone_aware(value, "updated_at")
    )
    _validate_next_check_at = field_validator("next_check_at")(
        lambda value: None if value is None else _timezone_aware(value, "next_check_at")
    )


MonitorCheckCallback = Callable[[LiveMonitorStatus, str], Awaitable[LiveMonitorCheck]]


class _RuntimeMonitor:
    def __init__(
        self,
        *,
        tenant_id: str,
        tenant_partition: str,
        status: LiveMonitorStatus,
    ) -> None:
        self.tenant_id = tenant_id
        self.tenant_partition = tenant_partition
        self.status = status
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()
        self.lock = asyncio.Lock()


class LiveQuoteMonitorRegistry:
    """Process-local lifecycle manager for opt-in periodic live revalidation."""

    def __init__(
        self,
        callback: MonitorCheckCallback,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_monitors: int = 64,
    ) -> None:
        if max_monitors < 1:
            raise ValueError("max_monitors must be positive")
        self._callback = callback
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep
        self._max_monitors = max_monitors
        self._records: dict[str, _RuntimeMonitor] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        run_id: str,
        tenant_id: str,
        interval_seconds: int,
        max_checks: int,
        timeout_seconds: int = 120,
    ) -> LiveMonitorStatus:
        if not 1 <= interval_seconds <= 86_400:
            raise ValueError("interval_seconds must be between 1 and 86400")
        if not 1 <= max_checks <= 10_000:
            raise ValueError("max_checks must be between 1 and 10000")
        if not 15 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 15 and 300")
        now = self._utc_now()
        monitor_id = f"live-monitor-{secrets.token_urlsafe(16)}"
        status = LiveMonitorStatus(
            id=monitor_id,
            run_id=run_id,
            state=LiveMonitorState.ACTIVE,
            interval_seconds=interval_seconds,
            max_checks=max_checks,
            timeout_seconds=timeout_seconds,
            next_check_at=now + timedelta(seconds=interval_seconds),
            created_at=now,
            updated_at=now,
        )
        runtime = _RuntimeMonitor(
            tenant_id=tenant_id,
            tenant_partition=self._tenant_partition(tenant_id),
            status=status,
        )
        async with self._lock:
            active = sum(
                item.status.state == LiveMonitorState.ACTIVE
                for item in self._records.values()
            )
            if active >= self._max_monitors:
                raise RuntimeError("live quote monitor capacity exceeded")
            self._records[monitor_id] = runtime
            runtime.task = asyncio.create_task(
                self._run(runtime),
                name=f"tripchord:{monitor_id}",
            )
        return runtime.status

    async def get(self, monitor_id: str, tenant_id: str) -> LiveMonitorStatus | None:
        runtime = await self._owned(monitor_id, tenant_id)
        return runtime.status if runtime is not None else None

    async def stop(self, monitor_id: str, tenant_id: str) -> LiveMonitorStatus | None:
        runtime = await self._owned(monitor_id, tenant_id)
        if runtime is None:
            return None
        runtime.stop_event.set()
        task = runtime.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with runtime.lock:
            now = self._utc_now()
            if runtime.status.state == LiveMonitorState.ACTIVE:
                runtime.status = runtime.status.model_copy(
                    update={
                        "state": LiveMonitorState.STOPPED,
                        "next_check_at": None,
                        "updated_at": now,
                    }
                )
            return runtime.status

    async def check_now(
        self,
        monitor_id: str,
        tenant_id: str,
    ) -> LiveMonitorStatus | None:
        runtime = await self._owned(monitor_id, tenant_id)
        if runtime is None:
            return None
        await self._check_once(runtime)
        return runtime.status

    async def close(self) -> None:
        async with self._lock:
            records = tuple(self._records.values())
        for runtime in records:
            runtime.stop_event.set()
            if runtime.task is not None:
                runtime.task.cancel()
        await asyncio.gather(
            *(runtime.task for runtime in records if runtime.task is not None),
            return_exceptions=True,
        )

    async def _run(self, runtime: _RuntimeMonitor) -> None:
        try:
            while runtime.status.state == LiveMonitorState.ACTIVE:
                await self._sleep(runtime.status.interval_seconds)
                if runtime.stop_event.is_set():
                    return
                await self._check_once(runtime)
        except asyncio.CancelledError:
            raise

    async def _check_once(self, runtime: _RuntimeMonitor) -> None:
        async with runtime.lock:
            if runtime.status.state != LiveMonitorState.ACTIVE:
                return
            try:
                check = await self._callback(runtime.status, runtime.tenant_id)
            except Exception as exc:
                now = self._utc_now()
                runtime.status = runtime.status.model_copy(
                    update={
                        "state": LiveMonitorState.FAILED,
                        "next_check_at": None,
                        "updated_at": now,
                        "last_error": f"{type(exc).__name__}: {str(exc)[:500]}",
                    }
                )
                return
            count = runtime.status.check_count + 1
            complete = count >= runtime.status.max_checks
            now = self._utc_now()
            runtime.status = runtime.status.model_copy(
                update={
                    "state": (
                        LiveMonitorState.COMPLETED if complete else LiveMonitorState.ACTIVE
                    ),
                    "check_count": count,
                    "next_check_at": (
                        None
                        if complete
                        else now + timedelta(seconds=runtime.status.interval_seconds)
                    ),
                    "updated_at": now,
                    "last_check": check,
                    "last_error": None,
                }
            )

    async def _owned(
        self,
        monitor_id: str,
        tenant_id: str,
    ) -> _RuntimeMonitor | None:
        partition = self._tenant_partition(tenant_id)
        async with self._lock:
            runtime = self._records.get(monitor_id)
            if runtime is None or not secrets.compare_digest(
                runtime.tenant_partition,
                partition,
            ):
                return None
            return runtime

    def _utc_now(self) -> datetime:
        return _timezone_aware(self._now(), "monitor clock").astimezone(UTC)

    @staticmethod
    def _tenant_partition(tenant_id: str) -> str:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


__all__ = [
    "LiveMonitorCheck",
    "LiveMonitorState",
    "LiveMonitorStatus",
    "LiveQuoteMonitorRegistry",
]
