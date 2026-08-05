from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
import tripchord.main as main_module
from fastapi import FastAPI


class _RecordingResource:
    def __init__(
        self,
        events: list[str],
        failures: set[str],
        *,
        close_event: str,
    ) -> None:
        self._events = events
        self._failures = failures
        self._close_event = close_event

    async def close(self) -> None:
        await self._record_close()

    async def aclose(self) -> None:
        await self._record_close()

    async def _record_close(self) -> None:
        self._events.append(self._close_event)
        if self._close_event in self._failures:
            raise RuntimeError(f"{self._close_event} failed")


class _RecordingCompanion(_RecordingResource):
    def start(self) -> None:
        self._events.append("companion-start")


class _RecordingModelRuntime(_RecordingResource):
    async def start(self) -> None:
        self._events.append("model-start")


def _install_lifecycle_resources(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: set[str],
) -> tuple[FastAPI, list[str]]:
    events: list[str] = []
    target_app = FastAPI()
    target_app.state.model_router = object()
    target_app.state.model_http_runtime = _RecordingModelRuntime(
        events,
        failures,
        close_event="model-close",
    )
    target_app.state.browser_companion_runtime_supervisor = _RecordingCompanion(
        events,
        failures,
        close_event="companion-close",
    )
    target_app.state.live_planning_job_registry = _RecordingResource(
        events,
        failures,
        close_event="jobs-close",
    )
    target_app.state.icom_transfer_provider = _RecordingResource(
        events,
        failures,
        close_event="provider-close",
    )
    target_app.state.live_quote_monitor_registry = _RecordingResource(
        events,
        failures,
        close_event="monitors-close",
    )

    monkeypatch.setattr(main_module.database, "create_schema", AsyncMock())
    monkeypatch.setattr(main_module.job_runner, "recover", AsyncMock())

    def finalizer(event: str) -> Callable[[], Awaitable[None]]:
        async def close() -> None:
            events.append(event)
            if event in failures:
                raise RuntimeError(f"{event} failed")

        return close

    monkeypatch.setattr(main_module.rate_limiter, "close", finalizer("rate-close"))
    monkeypatch.setattr(main_module.database, "dispose", finalizer("database-close"))
    return target_app, events


@pytest.mark.asyncio
async def test_lifespan_attempts_every_shutdown_step_after_an_early_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app, events = _install_lifecycle_resources(
        monkeypatch,
        failures={"companion-close"},
    )

    with pytest.raises(RuntimeError, match="companion-close failed") as error:
        async with main_module.lifespan(target_app):
            assert events == ["model-start", "companion-start"]

    assert events == [
        "model-start",
        "companion-start",
        "companion-close",
        "jobs-close",
        "provider-close",
        "monitors-close",
        "model-close",
        "rate-close",
        "database-close",
    ]
    assert error.value.__notes__ == [
        "TripChord lifespan resource failed to close: browser_companion_supervisor"
    ]


@pytest.mark.asyncio
async def test_lifespan_aggregates_ordered_shutdown_failures_after_full_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app, events = _install_lifecycle_resources(
        monkeypatch,
        failures={"jobs-close", "model-close", "database-close"},
    )

    with pytest.raises(ExceptionGroup) as error:
        async with main_module.lifespan(target_app):
            pass

    assert events[-7:] == [
        "companion-close",
        "jobs-close",
        "provider-close",
        "monitors-close",
        "model-close",
        "rate-close",
        "database-close",
    ]
    assert [str(item) for item in error.value.exceptions] == [
        "jobs-close failed",
        "model-close failed",
        "database-close failed",
    ]
