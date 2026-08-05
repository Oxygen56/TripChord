from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import tripchord.main as main_module
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tripchord.agents.companion_control_tools import (
    BrowserCompanionBuildReconcileResponse,
    BrowserCompanionBuildReconcileTimings,
    BrowserCompanionRuntimeExecutorAgent,
    BrowserCompanionRuntimeSupervisor,
)
from tripchord.config import Settings
from tripchord.main import _install_browser_bridge, app
from tripchord.providers.browser_bridge import (
    CONTROL_TOKEN_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    BrowserCompanionReloadReasonCode,
)

CONTROL_TOKEN = "control-token-used-only-by-test-0001"
IDEMPOTENCY_KEY = "runtime-reconcile-api-0001"
NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


def _response() -> BrowserCompanionBuildReconcileResponse:
    return BrowserCompanionBuildReconcileResponse(
        request_id=None,
        companion_id="chrome-mv3-fixture-extension",
        state="already_current",
        old_build_sha256="a" * 64,
        target_build_sha256="a" * 64,
        old_runtime_instance_id="runtime-current-0001",
        new_runtime_instance_id="runtime-current-0001",
        timings=BrowserCompanionBuildReconcileTimings(
            requested_at=NOW,
            updated_at=NOW,
            elapsed_ms=0,
        ),
    )


class _RecordingRuntimeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[BrowserCompanionReloadReasonCode, str]] = []

    async def reconcile_build(
        self,
        reason_code: BrowserCompanionReloadReasonCode,
        *,
        idempotency_key: str,
    ) -> BrowserCompanionBuildReconcileResponse:
        self.calls.append((reason_code, idempotency_key))
        return _response()


@pytest.mark.asyncio
async def test_runtime_reconcile_api_fails_closed_when_control_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "browser_companion_runtime_agent", None)
    monkeypatch.setattr(app.state, "browser_bridge_control_token", None)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers={
                CONTROL_TOKEN_HEADER: CONTROL_TOKEN,
                IDEMPOTENCY_KEY_HEADER: IDEMPOTENCY_KEY,
            },
            json={"reason_code": "recovery"},
        )

    assert response.status_code == 503


@pytest.mark.asyncio
async def test_runtime_reconcile_api_returns_503_when_agent_exists_without_external_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_agent = _RecordingRuntimeAgent()
    monkeypatch.setattr(app.state, "browser_companion_runtime_agent", runtime_agent)
    monkeypatch.setattr(app.state, "browser_bridge_control_token", None)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers={
                CONTROL_TOKEN_HEADER: CONTROL_TOKEN,
                IDEMPOTENCY_KEY_HEADER: IDEMPOTENCY_KEY,
            },
            json={"reason_code": "recovery"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "browser companion runtime control is not enabled"
    )
    assert runtime_agent.calls == []


@pytest.mark.asyncio
async def test_runtime_reconcile_api_rejects_wrong_control_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_agent = _RecordingRuntimeAgent()
    monkeypatch.setattr(app.state, "browser_companion_runtime_agent", runtime_agent)
    monkeypatch.setattr(app.state, "browser_bridge_control_token", CONTROL_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers={
                CONTROL_TOKEN_HEADER: "wrong-control-token-that-is-long-enough",
                IDEMPOTENCY_KEY_HEADER: IDEMPOTENCY_KEY,
            },
            json={"reason_code": "recovery"},
        )

    assert response.status_code == 403
    assert runtime_agent.calls == []


@pytest.mark.asyncio
async def test_runtime_reconcile_api_invokes_agent_and_returns_only_sanitized_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_agent = _RecordingRuntimeAgent()
    monkeypatch.setattr(app.state, "browser_companion_runtime_agent", runtime_agent)
    monkeypatch.setattr(app.state, "browser_bridge_control_token", CONTROL_TOKEN)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers={
                CONTROL_TOKEN_HEADER: CONTROL_TOKEN,
                IDEMPOTENCY_KEY_HEADER: IDEMPOTENCY_KEY,
            },
            json={"reason_code": "operator_requested"},
        )

    assert response.status_code == 200
    assert runtime_agent.calls == [
        (BrowserCompanionReloadReasonCode.OPERATOR_REQUESTED, IDEMPOTENCY_KEY)
    ]
    assert response.json()["state"] == "already_current"
    serialized = response.text.lower()
    assert "control-token" not in serialized
    assert "bridge-token" not in serialized


@pytest.mark.asyncio
async def test_runtime_reconcile_api_rejects_extra_control_inputs_and_remote_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_agent = _RecordingRuntimeAgent()
    monkeypatch.setattr(app.state, "browser_companion_runtime_agent", runtime_agent)
    monkeypatch.setattr(app.state, "browser_bridge_control_token", CONTROL_TOKEN)
    headers = {
        CONTROL_TOKEN_HEADER: CONTROL_TOKEN,
        IDEMPOTENCY_KEY_HEADER: IDEMPOTENCY_KEY,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as local:
        extra_input = await local.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers=headers,
            json={"reason_code": "recovery", "target_build_sha256": "f" * 64},
        )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://127.0.0.1",
    ) as remote:
        remote_response = await remote.post(
            "/api/v1/agents/browser-companion/reconcile-build",
            headers=headers,
            json={"reason_code": "recovery"},
        )

    assert extra_input.status_code == 422
    assert remote_response.status_code == 403
    assert runtime_agent.calls == []


def test_install_browser_bridge_wires_opt_in_runtime_agent_and_supervisor() -> None:
    configured_app = FastAPI()
    bridge, live_system = _install_browser_bridge(
        configured_app,
        Settings(
            _env_file=None,
            browser_bridge_enabled=True,
            browser_bridge_token="bridge-token-used-only-by-test-0001",
            browser_companion_auto_reload_enabled=True,
        ),
    )

    assert bridge is not None
    assert live_system is not None
    assert isinstance(
        configured_app.state.browser_companion_runtime_agent,
        BrowserCompanionRuntimeExecutorAgent,
    )
    assert isinstance(
        configured_app.state.browser_companion_runtime_supervisor,
        BrowserCompanionRuntimeSupervisor,
    )
    assert configured_app.state.browser_bridge_control_enabled is False
    assert configured_app.state.browser_companion_auto_reload_enabled is True

    externally_controlled_app = FastAPI()
    _install_browser_bridge(
        externally_controlled_app,
        Settings(
            _env_file=None,
            browser_bridge_enabled=True,
            browser_bridge_token="bridge-token-used-only-by-test-0002",
            browser_bridge_control_token=CONTROL_TOKEN,
        ),
    )
    assert isinstance(
        externally_controlled_app.state.browser_companion_runtime_agent,
        BrowserCompanionRuntimeExecutorAgent,
    )
    assert externally_controlled_app.state.browser_companion_runtime_supervisor is None
    assert externally_controlled_app.state.browser_bridge_control_enabled is True
    assert externally_controlled_app.state.browser_companion_auto_reload_enabled is False

    disabled_app = FastAPI()
    _install_browser_bridge(disabled_app, Settings(_env_file=None))
    assert disabled_app.state.browser_companion_runtime_agent is None
    assert disabled_app.state.browser_companion_runtime_supervisor is None
    assert disabled_app.state.browser_bridge_control_enabled is False
    assert disabled_app.state.browser_companion_auto_reload_enabled is False


class _RecordingSupervisor:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_closes_companion_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_app = FastAPI()
    supervisor = _RecordingSupervisor()
    target_app.state.browser_companion_runtime_supervisor = supervisor
    create_schema = AsyncMock()
    recover = AsyncMock()
    close_rate_limiter = AsyncMock()
    dispose = AsyncMock()
    monkeypatch.setattr(main_module.database, "create_schema", create_schema)
    monkeypatch.setattr(main_module.job_runner, "recover", recover)
    monkeypatch.setattr(main_module.rate_limiter, "close", close_rate_limiter)
    monkeypatch.setattr(main_module.database, "dispose", dispose)

    async with main_module.lifespan(target_app):
        assert supervisor.started is True
        assert supervisor.closed is False

    assert supervisor.closed is True
    create_schema.assert_awaited_once()
    recover.assert_awaited_once()
    close_rate_limiter.assert_awaited_once()
    dispose.assert_awaited_once()
