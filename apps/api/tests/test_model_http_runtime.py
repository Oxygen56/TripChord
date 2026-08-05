from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest
import tripchord.main as main_module
from fastapi import FastAPI
from pydantic import ValidationError
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelRequest,
)
from tripchord.agents.model_http_runtime import (
    ManagedModelHTTPRuntime,
    ModelHTTPRuntimeError,
    ModelHTTPRuntimeState,
)
from tripchord.agents.models import AgentRole
from tripchord.config import Settings


def _runtime_factory(
    handler: Callable[[httpx.Request], httpx.Response],
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]],
) -> Callable[..., httpx.AsyncClient]:
    def factory(
        *,
        http2: bool,
        limits: httpx.Limits,
        timeout: httpx.Timeout,
    ) -> httpx.AsyncClient:
        observations.append((http2, limits, timeout))
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


@pytest.mark.asyncio
async def test_runtime_is_fail_closed_and_start_close_are_idempotent() -> None:
    requests: list[httpx.Request] = []
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    runtime = ManagedModelHTTPRuntime(
        http2=True,
        client_factory=_runtime_factory(handler, observations),
    )

    with pytest.raises(ModelHTTPRuntimeError, match="not started"):
        await runtime.post("https://model.test/check")

    await runtime.start()
    await runtime.start()
    assert runtime.state == ModelHTTPRuntimeState.STARTED
    assert len(observations) == 1
    assert observations[0][0] is True
    assert observations[0][1].max_connections == 12
    assert observations[0][1].max_keepalive_connections == 12
    assert observations[0][2].connect == 45

    response = await runtime.post("https://model.test/check")
    assert response.status_code == 200
    assert len(requests) == 1

    await runtime.aclose()
    await runtime.aclose()
    assert runtime.state == ModelHTTPRuntimeState.CLOSED
    with pytest.raises(ModelHTTPRuntimeError, match="already closed"):
        await runtime.post("https://model.test/check")
    with pytest.raises(ModelHTTPRuntimeError, match="after shutdown"):
        await runtime.start()


@pytest.mark.asyncio
async def test_close_waits_for_an_in_flight_request() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    runtime = ManagedModelHTTPRuntime(
        client_factory=_runtime_factory(handler, observations),
    )
    await runtime.start()
    request = asyncio.create_task(runtime.post("https://model.test/slow"))
    await entered.wait()
    close = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)

    assert runtime.state == ModelHTTPRuntimeState.CLOSING
    assert runtime.active_requests == 1
    assert not close.done()

    release.set()
    assert (await request).status_code == 200
    await close
    assert runtime.state == ModelHTTPRuntimeState.CLOSED
    assert runtime.active_requests == 0


@pytest.mark.asyncio
async def test_runtime_enforces_the_process_wide_twelve_request_cap() -> None:
    entered = 0
    peak = 0
    release = asyncio.Event()
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        await release.wait()
        entered -= 1
        return httpx.Response(200, json={"ok": True})

    runtime = ManagedModelHTTPRuntime(
        max_connections=24,
        max_keepalive_connections=24,
        max_in_flight=12,
        client_factory=_runtime_factory(handler, observations),
    )
    await runtime.start()
    requests = tuple(
        asyncio.create_task(runtime.post(f"https://model.test/{index}"))
        for index in range(20)
    )
    for _ in range(100):
        if runtime.active_requests == 12:
            break
        await asyncio.sleep(0)
    assert runtime.active_requests == 12
    assert peak == 12
    assert runtime.peak_active_requests == 12
    release.set()
    assert all(response.status_code == 200 for response in await asyncio.gather(*requests))
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancelled_close_caller_does_not_strand_runtime_in_closing() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    runtime = ManagedModelHTTPRuntime(
        client_factory=_runtime_factory(handler, observations),
    )
    await runtime.start()
    request = asyncio.create_task(runtime.post("https://model.test/slow"))
    await entered.wait()
    first_close = asyncio.create_task(runtime.aclose())
    await asyncio.sleep(0)
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    release.set()
    await request
    await runtime.aclose()
    assert runtime.state == ModelHTTPRuntimeState.CLOSED


@pytest.mark.asyncio
async def test_router_primary_and_fast_models_share_the_runtime_client() -> None:
    observations: list[tuple[bool, httpx.Limits, httpx.Timeout]] = []
    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        requested_models.append(payload["model"])
        return httpx.Response(
            200,
            json={
                "id": "model-response",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    runtime = ManagedModelHTTPRuntime(
        client_factory=_runtime_factory(handler, observations),
    )
    settings = Settings(
        _env_file=None,
        model_provider="openai_compatible",
        model_base_url="https://model.test/v1",
        model_name="control-model",
        model_fast_name="fast-model",
        model_max_attempts=1,
    )
    router = main_module._build_model_router(
        settings,
        InMemoryModelTraceSink(),
        http_client=runtime.http_client,
    )
    assert router is not None
    await runtime.start()
    try:
        await router.complete(
            ModelRequest(role=AgentRole.CONTEXT, system="test", messages=())
        )
        await router.complete(
            ModelRequest(role=AgentRole.ORCHESTRATOR, system="test", messages=())
        )
    finally:
        await runtime.aclose()

    assert requested_models == ["fast-model", "control-model"]
    assert len(observations) == 1


def test_model_http_settings_are_bounded_and_keepalive_cannot_exceed_connections() -> None:
    configured = Settings(
        _env_file=None,
        model_http2_enabled=True,
        model_http_max_connections=24,
        model_http_max_keepalive_connections=10,
    )
    assert configured.model_http2_enabled is True
    assert configured.model_http_max_connections == 24
    assert configured.model_http_max_keepalive_connections == 10
    assert configured.model_http_max_in_flight == 12

    with pytest.raises(ValidationError, match="cannot exceed"):
        Settings(
            _env_file=None,
            model_http_max_connections=4,
            model_http_max_keepalive_connections=5,
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_http_max_in_flight=13)


@pytest.mark.asyncio
async def test_lifespan_starts_runtime_and_closes_it_after_live_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class RecordingRuntime:
        async def start(self) -> None:
            events.append("model-start")

        async def aclose(self) -> None:
            events.append("model-close")

    class RecordingJobs:
        async def close(self) -> None:
            events.append("jobs-close")

    class RecordingMonitors:
        async def close(self) -> None:
            events.append("monitors-close")

    target_app = FastAPI()
    target_app.state.model_router = object()
    target_app.state.model_http_runtime = RecordingRuntime()
    target_app.state.live_planning_job_registry = RecordingJobs()
    target_app.state.live_quote_monitor_registry = RecordingMonitors()
    monkeypatch.setattr(main_module.database, "create_schema", AsyncMock())
    monkeypatch.setattr(main_module.job_runner, "recover", AsyncMock())
    monkeypatch.setattr(main_module.rate_limiter, "close", AsyncMock())
    monkeypatch.setattr(main_module.database, "dispose", AsyncMock())

    async with main_module.lifespan(target_app):
        assert events == ["model-start"]

    assert events == [
        "model-start",
        "jobs-close",
        "monitors-close",
        "model-close",
    ]
