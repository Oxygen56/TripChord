from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import tripchord.main as main_module
from httpx import ASGITransport, AsyncClient
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_jobs import (
    LivePlanningJobInactiveError,
    LivePlanningJobRegistry,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
    LiveSourceTerminalEvent,
)
from tripchord.agents.live_system import LivePackageAgentSystem
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleChatClient,
)
from tripchord.agents.models import AgentRole
from tripchord.api import LiveFlexibleFromTextPlanningRequest
from tripchord.main import (
    LiveRunCache,
    _cache_flexible_pair_runs,
    _live_flexible_from_text_request_sha256,
    app,
    package_requirement_agent,
    settings,
)
from tripchord.providers.browser_bridge import (
    BrowserTaskBridge,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def _payload(*, ready: bool) -> dict[str, object]:
    destination = "，目的地：马累" if ready else ""
    return {
        "requirement": {
            "text": (
                f"出发地：杭州{destination}，2026年8月出发，玩5晚，"
                "2名成人，1间房，无行李，接受中转"
            ),
            "reference_date": "2026-07-30",
        },
        "coverage_mode": "strict",
        "timeout_seconds": 300,
        "total_timeout_seconds": 1800,
        "max_pairs": 1,
    }


async def _terminal_job(
    client: AsyncClient,
    job_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0)
    raise AssertionError("live job did not reach a terminal state")


def _in_process_flexible_operation(
    payload: Any,
    *,
    request_digest: str,
    target_app: Any,
    cache: Any,
    principal: Any,
) -> Any:
    """Build the in-process operation these HTTP contract tests exercise.

    C-146 P0-1: the production route now wraps the real operation in a
    ``LiveJobWorkerCommand`` that runs in an INDEPENDENT subprocess. The HTTP
    contract tests in this file drive the route with in-process test doubles
    (blocking requirement agents, fake model clients, in-memory sinks) that
    cannot cross a process boundary, so this seam restores the pre-worker
    in-process operation — the SAME ``_execute_live_flexible_from_text`` call
    the production worker entry runs, but as a coroutine inside the API
    process. Assertions stay byte-identical; the REAL subprocess worker path is
    covered by the counterexample tests in ``test_live_job_worker_http.py``.
    """

    async def operation(report: Any) -> dict[str, Any]:
        response = await main_module._execute_live_flexible_from_text(
            payload,
            target_app=target_app,
            cache=cache,
            principal=principal,
            report_progress=report,
            report_pair_checkpoint=report.report_pair_checkpoint,
            expected_request_sha256=request_digest,
            model_trace_scope_id=report.job_id,
            report_model_trace_summary=report.report_model_trace_summary,
        )
        return response.model_dump(mode="json")

    return operation


@pytest.fixture(autouse=True)
def _in_process_flexible_operation_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route /jobs through the in-process operation for HTTP contract tests."""
    monkeypatch.setattr(
        main_module,
        "_build_live_flexible_from_text_worker_command",
        _in_process_flexible_operation,
    )


@pytest.mark.asyncio
async def test_async_live_job_returns_202_then_exposes_result_and_status_only_sse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = LivePlanningJobRegistry(capacity=4)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=False),
        )
        assert created.status_code == 202
        created_job = created.json()["job"]
        job_id = created_job["id"]
        admitted_at = datetime.fromisoformat(created_job["created_at"])
        deadline_at = datetime.fromisoformat(created_job["deadline_at"])
        assert (deadline_at - admitted_at).total_seconds() == 1800
        assert created.json()["status_url"].endswith(job_id)
        terminal = await _terminal_job(client, job_id)
        assert terminal["state"] == "succeeded"
        assert terminal["result"]["interpretation"]["state"] == "human_block"
        assert terminal["model_trace_scope_sha256"] == terminal["request_sha256"]
        assert terminal["model_trace_count"] == 0
        assert terminal["model_trace_success_count"] == 0
        assert terminal["model_trace_failure_count"] == 0
        assert terminal["result"]["model_trace_scope_sha256"] == terminal["request_sha256"]
        assert terminal["result"]["model_trace_count"] == 0
        assert "重启不恢复" in terminal["boundary"]

        streamed = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}/events"
        )
        assert streamed.status_code == 200
        assert "event: job" in streamed.text
        assert '"state": "succeeded"' in streamed.text
        assert '"result"' not in streamed.text
    await registry.close()


@pytest.mark.asyncio
async def test_cache_pair_runs_checks_job_generation_before_real_cache_write() -> None:
    class RecordingCache:
        def __init__(self) -> None:
            self.put_called = False

        async def put(self, *_: Any) -> tuple[str, datetime]:
            self.put_called = True
            return "must-not-exist", NOW

    cache = RecordingCache()
    run = SimpleNamespace(
        pair_runs=(
            SimpleNamespace(
                run=object(),
                date_pair=SimpleNamespace(id="date-pair:1"),
            ),
        )
    )

    registry = LivePlanningJobRegistry(cancel_wait_seconds=0.01)
    captured_report: list[Any] = []
    started = asyncio.Event()

    async def blocked(report: Any) -> dict[str, Any]:
        captured_report.append(report)
        started.set()
        await asyncio.Event().wait()
        return {}

    job = await registry.start(tenant_id="tenant-a", operation=blocked)
    await started.wait()
    cancelled = await registry.cancel(job.id, "tenant-a")
    assert cancelled is not None and cancelled.state.value == "cancelled"

    with pytest.raises(LivePlanningJobInactiveError, match="no longer active"):
        await _cache_flexible_pair_runs(
            run,
            cache,  # type: ignore[arg-type]
            "tenant-a",
            ensure_active=captured_report[0].ensure_active,
        )
    assert cache.put_called is False
    await registry.close()


@pytest.mark.asyncio
async def test_runtime_status_returns_effective_flexible_timeout_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 3600)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/agents/runtime")
    assert response.status_code == 200
    assert response.json()["effective_flexible_timeout_seconds"] == 3600


@pytest.mark.asyncio
async def test_failed_async_job_retains_request_bound_model_trace_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = InMemoryModelTraceSink()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        model_client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        class FailingRequirementAgent:
            async def parse(self, request: Any) -> Any:
                await model_client.complete(
                    ModelRequest(
                        role=AgentRole.CONTEXT,
                        system="bounded job failure fixture",
                        messages=(ModelMessage(role="user", content=request.text),),
                    )
                )
                raise RuntimeError("raw prompt and provider details must not escape")

        registry = LivePlanningJobRegistry(capacity=2)
        monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(app.state, "package_requirement_agent", FailingRequirementAgent())
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
            )
            assert created.status_code == 202
            initial = created.json()["job"]
            terminal = await _terminal_job(client, initial["id"])

        assert terminal["state"] == "failed"
        assert terminal["result"] is None
        assert terminal["request_sha256"] == initial["request_sha256"]
        assert terminal["model_trace_scope_sha256"] == terminal["request_sha256"]
        assert terminal["model_trace_count"] == 1
        assert terminal["model_trace_success_count"] == 1
        assert terminal["model_trace_failure_count"] == 0
        assert terminal["error"] == "RuntimeError: live planning execution failed"
        assert terminal["safe_failure_code"] == "execution_exception"
        assert terminal["safe_failure_details"]["exception_class"] == "RuntimeError"
        assert terminal["safe_failure_details"]["message_sha256"] is None
        assert terminal["safe_failure_details"]["validation_model"] is None
        assert terminal["safe_failure_details"]["validation_errors"] == []
        assert len(terminal["safe_failure_details_digest"]) == 64
        assert sink.records[0].scope_id == terminal["id"]
        assert sink.records[0].scope_request_digest == terminal["request_sha256"]
        assert "raw prompt" not in str(terminal)
        await registry.close()


@pytest.mark.asyncio
async def test_concurrent_identical_jobs_keep_model_trace_counts_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = InMemoryModelTraceSink(max_records=10)
    release = asyncio.Event()
    both_entered = asyncio.Event()
    assignment_lock = asyncio.Lock()
    assigned = 0

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        model_client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        class ConcurrentRequirementAgent:
            async def parse(self, request: Any) -> Any:
                nonlocal assigned
                async with assignment_lock:
                    assigned += 1
                    call_count = assigned
                    if assigned == 2:
                        both_entered.set()
                await release.wait()
                for _ in range(call_count):
                    await model_client.complete(
                        ModelRequest(
                            role=AgentRole.CONTEXT,
                            system="bounded concurrent fixture",
                            messages=(ModelMessage(role="user", content=request.text),),
                        )
                    )
                return await package_requirement_agent.parse(request)

        registry = LivePlanningJobRegistry(capacity=4, max_running=2)
        monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(app.state, "package_requirement_agent", ConcurrentRequirementAgent())
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            first = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
            )
            second = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=False),
            )
            assert first.status_code == second.status_code == 202
            await asyncio.wait_for(both_entered.wait(), timeout=1)
            release.set()
            terminals = await asyncio.gather(
                _terminal_job(client, first.json()["job"]["id"]),
                _terminal_job(client, second.json()["job"]["id"]),
            )

        assert {item["model_trace_count"] for item in terminals} == {1, 2}
        assert len({item["id"] for item in terminals}) == 2
        assert len({item["request_sha256"] for item in terminals}) == 1
        for terminal in terminals:
            job_traces = tuple(item for item in sink.records if item.scope_id == terminal["id"])
            assert terminal["state"] == "succeeded"
            assert terminal["model_trace_scope_sha256"] == terminal["request_sha256"]
            assert terminal["model_trace_count"] == len(job_traces)
            assert terminal["model_trace_success_count"] == len(job_traces)
            assert terminal["model_trace_failure_count"] == 0
        await registry.close()


@pytest.mark.asyncio
async def test_async_timeout_keeps_completed_pair_checkpoint_visible_and_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = asyncio.Event()
    release = asyncio.Event()

    class CheckpointThenTimeoutSystem(FlexibleLiveAgentSystem):
        def __init__(self) -> None:
            # The overridden run is the whole fixture; the base runner is never invoked.
            super().__init__(object(), now=lambda: NOW)  # type: ignore[arg-type]

        async def run(self, *_: Any, **kwargs: Any) -> Any:
            reporter = kwargs["pair_checkpoint_reporter"]
            request_sha256 = kwargs["checkpoint_request_sha256"]
            assert reporter is not None
            await reporter(
                LivePlanningPairCheckpoint.create(
                    sequence=1,
                    request_sha256=request_sha256,
                    date_pair_id="date-pair:2026-08-20:2026-08-25",
                    departure_date=date(2026, 8, 20),
                    return_date=date(2026, 8, 25),
                    state=LivePlanningPairCheckpointState.COMPLETED,
                    query_task_ids=tuple(f"query-safe-{index}" for index in range(11)),
                    run_purpose="exploration_selection",
                    finalization_state="exploration_sealed",
                    decision_state="reject",
                    source_task_count=20,
                    exploration_seal_passed=True,
                    all_platforms_complete=False,
                    captured_at=NOW,
                )
            )
            reported.set()
            await release.wait()
            raise TimeoutError(
                "Bearer leaked-token; Cookie: session=raw-cookie; exact quote 12345"
            )

    registry = LivePlanningJobRegistry(capacity=2)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(
        app.state,
        "flexible_live_agent_system",
        CheckpointThenTimeoutSystem(),
    )
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "auth_tokens", {"token-a": "tenant-a", "token-b": "tenant-b"})

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=True),
            headers={"Authorization": "Bearer token-a"},
        )
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]
        request_sha256 = created.json()["job"]["request_sha256"]
        assert len(request_sha256) == 64
        assert created.json()["job"]["model_trace_scope_sha256"] == request_sha256
        await asyncio.wait_for(reported.wait(), timeout=1)

        visible = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
            headers={"Authorization": "Bearer token-a"},
        )
        assert visible.status_code == 200
        assert visible.json()["state"] == "running"
        assert visible.json()["pair_checkpoints"][0]["state"] == "completed"
        assert visible.json()["pair_checkpoints"][0]["request_sha256"] == request_sha256
        hidden = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
            headers={"Authorization": "Bearer token-b"},
        )
        assert hidden.status_code == 404

        release.set()
        terminal = await _terminal_job(
            client,
            job_id,
            headers={"Authorization": "Bearer token-a"},
        )
        assert terminal["state"] == "failed"
        assert len(terminal["pair_checkpoints"]) == 1
        assert terminal["model_trace_scope_sha256"] == request_sha256
        assert terminal["model_trace_count"] == 0
        serialized = str(terminal)
        assert "leaked-token" not in serialized
        assert "raw-cookie" not in serialized
        assert "12345" not in serialized
    await registry.close()


@pytest.mark.asyncio
async def test_async_live_job_cancel_reaches_running_operation_and_is_repeat_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingFlexibleSystem:
        async def run(self, *_: Any, **__: Any) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    registry = LivePlanningJobRegistry(capacity=2)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", BlockingFlexibleSystem())
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        async with asyncio.timeout(1):
            created = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text/jobs",
                json=_payload(ready=True),
            )
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]
        await asyncio.wait_for(started.wait(), timeout=1)

        stopped = await client.delete(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
        )
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "cancelled"
        assert stopped.json()["cancellation_requested"] is True
        assert cancelled.is_set()

        repeated = await client.delete(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
        )
        assert repeated.status_code == 200
        assert repeated.json() == stopped.json()
    await registry.close()


@pytest.mark.asyncio
async def test_http_cancel_propagates_through_flexible_run_to_browser_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingBridge(BrowserTaskBridge):
        def __init__(self) -> None:
            super().__init__(now=lambda: NOW)
            self.submitted_ids: list[str] = []
            self.first_submitted = asyncio.Event()

        async def submit_many(
            self,
            submissions: Iterable[BrowserTaskSubmission],
        ) -> tuple[BrowserTaskSnapshot, ...]:
            snapshots = await super().submit_many(submissions)
            self.submitted_ids.extend(item.id for item in snapshots)
            self.first_submitted.set()
            return snapshots

    bridge = TrackingBridge()
    live_system = LivePackageAgentSystem(bridge, now=lambda: NOW)
    flexible_system = FlexibleLiveAgentSystem(
        live_system,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    registry = LivePlanningJobRegistry(capacity=2)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible_system)
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=True),
        )
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]
        await asyncio.wait_for(bridge.first_submitted.wait(), timeout=2)
        leases = await bridge.claim("test-companion", limit=1)
        assert len(leases) == 1

        stopped = await client.delete(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}"
        )
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "cancelled"

    snapshots = await asyncio.gather(*(bridge.get(task_id) for task_id in bridge.submitted_ids))
    assert snapshots
    assert all(item.state == BrowserTaskState.CANCELLED for item in snapshots)
    assert any(item.claimed_at is not None for item in snapshots)
    assert await bridge.claim("late-companion", limit=6) == ()
    await registry.close()


@pytest.mark.asyncio
async def test_async_live_job_api_is_tenant_isolated_and_capacity_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = asyncio.Event()

    class BlockingFlexibleSystem:
        async def run(self, *_: Any, **__: Any) -> Any:
            await blocker.wait()

    registry = LivePlanningJobRegistry(capacity=1)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", BlockingFlexibleSystem())
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "auth_tokens", {"token-a": "tenant-a", "token-b": "tenant-b"})

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=True),
            headers={"Authorization": "Bearer token-a"},
        )
        assert created.status_code == 202
        job_id = created.json()["job"]["id"]

        hidden = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
            headers={"Authorization": "Bearer token-b"},
        )
        assert hidden.status_code == 404
        full = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=True),
            headers={"Authorization": "Bearer token-b"},
        )
        assert full.status_code == 503
        assert full.headers["retry-after"] == "30"

        stopped = await client.delete(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
            headers={"Authorization": "Bearer token-a"},
        )
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "cancelled"
    await registry.close()


@pytest.mark.asyncio
async def test_async_live_job_creation_remains_loopback_only() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text/jobs",
            json=_payload(ready=False),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_async_post_idempotency_prevents_duplicate_live_search_and_is_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    two_started = asyncio.Event()

    class CountingBlockingFlexibleSystem:
        async def run(self, *_: Any, **__: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                two_started.set()
            await asyncio.Event().wait()

    registry = LivePlanningJobRegistry(capacity=4, max_running=2)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(
        app.state,
        "flexible_live_agent_system",
        CountingBlockingFlexibleSystem(),
    )
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "auth_tokens", {"token-a": "tenant-a", "token-b": "tenant-b"})
    path = "/api/v1/agents/live-flexible-plan-from-text/jobs"
    key_header = {"Authorization": "Bearer token-a", "Idempotency-Key": "network-retry-1"}

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        first = await client.post(path, json=_payload(ready=True), headers=key_header)
        assert first.status_code == 202
        first_id = first.json()["job"]["id"]
        assert first.json()["replayed"] is False
        for _ in range(100):
            if calls == 1:
                break
            await asyncio.sleep(0)
        assert calls == 1

        replay = await client.post(path, json=_payload(ready=True), headers=key_header)
        assert replay.status_code == 202
        assert replay.json()["job"]["id"] == first_id
        assert replay.json()["replayed"] is True
        assert calls == 1

        changed = _payload(ready=True)
        changed["max_pairs"] = 2
        conflict = await client.post(path, json=changed, headers=key_header)
        assert conflict.status_code == 409
        assert calls == 1

        other = await client.post(
            path,
            json=_payload(ready=True),
            headers={
                "Authorization": "Bearer token-b",
                "Idempotency-Key": "network-retry-1",
            },
        )
        assert other.status_code == 202
        assert other.json()["replayed"] is False
        assert other.json()["job"]["id"] != first_id
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert calls == 2

        for job_id, token in (
            (first_id, "token-a"),
            (other.json()["job"]["id"], "token-b"),
        ):
            stopped = await client.delete(
                f"{path}/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert stopped.status_code == 200
            assert stopped.json()["state"] == "cancelled"
    await registry.close()


@pytest.mark.asyncio
async def test_sse_stream_gates_result_until_after_barrier_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = LivePlanningJobRegistry(capacity=4)
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    released_at = datetime(2026, 7, 30, 9, 2, tzinfo=UTC)

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_source_terminal_events(
            (
                LiveSourceTerminalEvent(
                    source_task_id="source-ctrip-flight",
                    provider="ctrip",
                    vertical="flight",
                    terminal_state="quote_found",
                    occurred_at=datetime(2026, 7, 30, 9, 1, tzinfo=UTC),
                ),
            )
        )
        await report.report_barrier_released(released_at)
        return {"done": True}

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        # Admit a job directly against the same registry the endpoint reads.
        job = await registry.start(
            tenant_id="anonymous",
            operation=operation,
            request_digest="d" * 64,
        )
        await _terminal_job(client, job.id)
        streamed = await client.get(
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job.id}/events"
        )
        assert streamed.status_code == 200
        assert "event: barrier" in streamed.text
        assert f'"barrier_released_at": "{released_at.isoformat()}"' in streamed.text
        assert "event: source_terminal" not in streamed.text  # not a distinct SSE type
        assert '"source_terminal_events"' in streamed.text
        assert '"done": true' in streamed.text
        assert "event: result" in streamed.text
    await registry.close()


@pytest.mark.asyncio
async def test_start_post_commit_persist_failure_returns_recoverable_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143 P0-2 counter-example: when a production persistent task entry's
    post-commit persist fails (the record was already committed to disk and the
    real task is running), the start endpoint must return a recoverable committed
    job identity instead of a bare 500. A same-key retry must retrieve the same
    job, and query/cancel must act on that same task — no duplicate dispatch."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingFlexibleSystem:
        async def run(self, *_: Any, **__: Any) -> Any:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    registry = LivePlanningJobRegistry(
        state_path=tmp_path / "live-jobs.json",
        capacity=4,
    )
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", BlockingFlexibleSystem())
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    path = "/api/v1/agents/live-flexible-plan-from-text/jobs"
    key_header = {"Idempotency-Key": "post-commit-recoverable-1"}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        monkeypatch.setenv(
            "TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT",
            "post_replace_dir_fsync",
        )
        created = await client.post(path, json=_payload(ready=True), headers=key_header)
        monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")
        # The committed identity is recoverable — not a bare 500.
        assert created.status_code == 202
        created_job = created.json()["job"]
        job_id = created_job["id"]
        assert created.json()["replayed"] is False
        assert created.json()["status_url"].endswith(job_id)
        # The real task is genuinely running behind the lost response envelope.
        await asyncio.wait_for(started.wait(), timeout=2)

        # The committed record is durably on disk with the same committed state.
        disk_payload = json.loads(
            (tmp_path / "live-jobs.json").read_text(encoding="utf-8")
        )
        disk_record = next(
            record
            for record in disk_payload["records"]
            if record["snapshot"]["id"] == job_id
        )
        assert disk_record["snapshot"]["id"] == job_id

        # Query and same-key retry act on the same committed task.
        queried = await client.get(f"{path}/{job_id}")
        assert queried.status_code == 200
        assert queried.json()["id"] == job_id
        replay = await client.post(path, json=_payload(ready=True), headers=key_header)
        assert replay.status_code == 202
        assert replay.json()["job"]["id"] == job_id
        assert replay.json()["replayed"] is True

        # Cancel acts on the same task; no duplicate dispatch.
        stopped = await client.delete(f"{path}/{job_id}")
        assert stopped.status_code == 200
        assert stopped.json()["id"] == job_id
        assert cancelled.is_set()
        terminal = await _terminal_job(client, job_id)
        assert terminal["state"] == "cancelled"
    await registry.close()


@pytest.mark.asyncio
async def test_cancellation_pending_same_key_retry_returns_conflict_not_500(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143 P0-2: a same-key retry while the real operation swallows the cancel
    must return a stable, machine-decidable 409 (never a bare 500), keep the
    original job identity unchanged and queryable, and only expose the terminal
    state after the operation truly stops — never a new job, never a false
    success. A full cold start reads the same terminal facts."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    monkeypatch.setattr(app.state, "live_planning_job_registry", registry)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "auth_tokens", {"token-a": "tenant-a"})

    stop = asyncio.Event()
    started = asyncio.Event()
    side_effects = 0

    async def stubborn(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        started.set()
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"ok": True}

    payload = _payload(ready=True)
    request_digest = _live_flexible_from_text_request_sha256(
        LiveFlexibleFromTextPlanningRequest(**payload)
    )
    snap, _replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=stubborn,
        idempotency_key="api-retry-while-pending",
        request_digest=request_digest,
        deadline_seconds=30,
    )
    runtime = registry._records[snap.id]
    for _ in range(1000):
        if started.is_set():
            break
        await asyncio.sleep(0)
    assert started.is_set()

    path = "/api/v1/agents/live-flexible-plan-from-text/jobs"
    key_header = {
        "Authorization": "Bearer token-a",
        "Idempotency-Key": "api-retry-while-pending",
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            # Drive the cancellation through the real DELETE endpoint; the
            # operation swallows it and stays alive (cancel_pending).
            cancelled = await client.delete(
                f"{path}/{snap.id}",
                headers={"Authorization": "Bearer token-a"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["cancel_pending"] is True
            assert (
                runtime.operation_task is not None
                and not runtime.operation_task.done()
            )

            retry = await client.post(path, json=payload, headers=key_header)
            # RED on HEAD: the unmapped RuntimeError surfaces as a bare 500 (or
            # propagates through ASGITransport). After the fix: a stable 409
            # with the original identity.
            assert retry.status_code != 500
            assert retry.status_code == 409
            body = retry.json()["detail"]
            assert body["job_id"] == snap.id
            assert body["state"] == "cancellation_pending"
            assert body["retryable"] is True
            assert body["status_url"].endswith(f"/jobs/{snap.id}")
            # The retry must NOT create a new job.
            assert len(registry._records) == 1

            # Identity unchanged and queryable while the operation is alive.
            query = await client.get(
                f"{path}/{snap.id}",
                headers={"Authorization": "Bearer token-a"},
            )
            assert query.status_code == 200
            assert query.json()["id"] == snap.id
            assert query.json()["cancel_pending"] is True

            # Once the operation truly stops, the same-key retry returns the
            # terminal state idempotently.
            stop.set()
            await asyncio.wait_for(runtime.operation_task, timeout=3)
            final = await client.post(path, json=payload, headers=key_header)
            assert final.status_code == 202
            final_body = final.json()
            assert final_body["replayed"] is True
            assert final_body["job"]["id"] == snap.id
            assert final_body["job"]["state"] == "cancelled"

        # A full cold start reads the same terminal facts.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        cold = await reloaded.get(snap.id, "tenant-a")
        assert cold is not None and cold.state == "cancelled"
        await reloaded.close()
    finally:
        # On the native-red path the assertion fails before stop.set(); the
        # stubborn operation would otherwise survive close()'s bounded drain and
        # hang pytest-asyncio's teardown. Settle it boundedly before closing.
        stop.set()
        if runtime is not None:
            operation_task = runtime.operation_task
            if operation_task is not None and not operation_task.done():
                operation_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(operation_task, timeout=3)
        await registry.close()
