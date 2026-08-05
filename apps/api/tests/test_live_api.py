from __future__ import annotations

import asyncio
import importlib
import json
import stat
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_monitor import (
    LiveMonitorCheck,
    LiveMonitorStatus,
    LiveQuoteMonitorRegistry,
)
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LiveDataProvider,
    LiveEventReplanRun,
    LiveFinalizationState,
    LivePackageAgentRun,
    LivePackageEvent,
    LiveRunPurpose,
    PlatformSearchCoverage,
)
from tripchord.agents.models import AgentRole, AgentTask, AgentTaskResult, TaskGraph
from tripchord.agents.persistent_memory import CorruptionPolicy
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.config import Settings
from tripchord.main import (
    LiveRunCache,
    LiveRunCacheLoadError,
    LiveRunCacheWriteError,
    _advance_cached_live_run,
    _build_live_run_cache,
    _install_browser_bridge,
    app,
    protect_mounted_browser_bridge,
    settings,
)
from tripchord.planning.package import (
    PackageDecision,
    PackageDecisionState,
    PackageEventKind,
    PackageIntent,
    PackageInventory,
)
from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    LIVE_V5_BROWSER_PROVIDERS,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserVertical,
)

main_module = importlib.import_module("tripchord.main")


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="hgh-mle-live-fixture",
        origin="HGH",
        destination="MLE",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        rooms=1,
        currency="CNY",
    )


def _search_query() -> BrowserSearchQuery:
    return BrowserSearchQuery(
        origin="HGH",
        destination="MLE",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        rooms=1,
        currency="CNY",
        origin_code="HGH",
        destination_code="MLE",
    )


def _source_ids() -> tuple[str, ...]:
    return tuple(
        source_id
        for provider in LIVE_V5_BROWSER_PROVIDERS
        for source_id in (
            f"source-{provider.value}-flight",
            *(
                (
                    f"source-{provider.value}-lodging-full",
                    f"source-{provider.value}-lodging-first",
                    f"source-{provider.value}-lodging-middle",
                    f"source-{provider.value}-lodging-last",
                )
                if provider in {BrowserProvider.CTRIP, BrowserProvider.QUNAR}
                else ()
            ),
        )
    )


def _scheduler() -> SchedulerOutcome:
    return SchedulerOutcome(
        graph=TaskGraph(tasks=()),
        results=(),
        trace=(),
        wall_time_seconds=0,
        max_parallel_tasks=0,
        succeeded=False,
    )


def _final_publication_scheduler() -> SchedulerOutcome:
    tasks = (
        AgentTask(
            id="orchestrate-travel-package",
            role=AgentRole.SAFETY_GATE,
            goal="fixture deterministic master decision",
        ),
        AgentTask(
            id="explain-final-decision",
            role=AgentRole.EXPLANATION,
            goal="fixture final explanation",
            dependencies=("orchestrate-travel-package",),
        ),
        AgentTask(
            id="curate-run-memory",
            role=AgentRole.MEMORY_CURATOR,
            goal="fixture memory candidate curation",
            dependencies=("explain-final-decision",),
        ),
        AgentTask(
            id="publish-live-run",
            role=AgentRole.SAFETY_GATE,
            goal="fixture deterministic publication gate",
            dependencies=("curate-run-memory",),
        ),
    )
    results = tuple(
        AgentTaskResult(
            task_id=task.id,
            agent_role=task.role,
            success=True,
            summary="fixture stage completed",
            output=(
                {"publication_gate_passed": True}
                if task.id == "publish-live-run"
                else {}
            ),
        )
        for task in tasks
    )
    return SchedulerOutcome(
        graph=TaskGraph(tasks=tasks),
        results=results,
        trace=(),
        wall_time_seconds=0,
        max_parallel_tasks=1,
        succeeded=True,
    )


def _blocked_live_run(
    *,
    mode: LiveCoverageMode = LiveCoverageMode.STRICT,
) -> LivePackageAgentRun:
    source_ids = _source_ids()
    coverage = tuple(
        PlatformSearchCoverage(
            provider=provider,
            failed_verticals=(BrowserVertical.FLIGHT, BrowserVertical.LODGING),
            failed_source_ids=tuple(
                source_id
                for source_id in source_ids
                if source_id.startswith(f"source-{provider.value}-")
            ),
            failure_reasons=("fixture exposes a structured blocked result only",),
            complete=False,
        )
        for provider in LIVE_V5_BROWSER_PROVIDERS
    )
    return LivePackageAgentRun(
        mode=mode,
        intent=_intent(),
        search_query=_search_query(),
        decision=PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="测试仅验证 API 编排，不代表真实平台报价可用",
        ),
        claim_boundary=("三平台十五路查询未在本测试执行；不得声称实时核价完成或形成可订方案。"),
        all_platforms_complete=False,
        coverage=coverage,
        inventory=PackageInventory(),
        normalization_results=(),
        package=None,
        scheduler=_final_publication_scheduler(),
        source_task_ids=source_ids,
    )


class _FakeLiveSystem:
    def __init__(self) -> None:
        self.run_calls = 0
        self.active_replans = 0
        self.max_active_replans = 0
        self.previous_runs: list[LivePackageAgentRun] = []

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode,
        timeout_seconds: int,
    ) -> LivePackageAgentRun:
        self.run_calls += 1
        assert intent == _intent()
        assert query == _search_query()
        assert timeout_seconds == 20
        return _blocked_live_run(mode=mode)

    async def replan_after_event(
        self,
        previous: LivePackageAgentRun,
        event: LivePackageEvent,
        *,
        timeout_seconds: int,
    ) -> LiveEventReplanRun:
        assert timeout_seconds == 20
        self.previous_runs.append(previous)
        self.active_replans += 1
        self.max_active_replans = max(self.max_active_replans, self.active_replans)
        try:
            await asyncio.sleep(0.01)
            return LiveEventReplanRun(
                event=event,
                decision=PackageDecision(
                    state=PackageDecisionState.HUMAN_BLOCK,
                    summary=f"event handled: {event.id}",
                ),
                claim_boundary=("事件测试未访问真实平台，仅证明同一 run 的重规划请求会串行裁决。"),
                inventory=previous.inventory,
                normalization_results=(),
                package=None,
                scheduler=_scheduler(),
                requeried_providers=(event.affected_provider,),
                source_task_ids=(f"event-source-{event.id}",),
            )
        finally:
            self.active_replans -= 1


def _live_request_payload() -> dict[str, object]:
    return {
        "intent": _intent().model_dump(mode="json"),
        "search_query": _search_query().model_dump(mode="json"),
        "coverage_mode": "strict",
        "timeout_seconds": 20,
    }


def test_browser_bridge_mount_requires_explicit_enablement() -> None:
    disabled_app = FastAPI()
    bridge, live_system = _install_browser_bridge(
        disabled_app,
        Settings(_env_file=None),
    )

    assert bridge is None
    assert live_system is None
    assert all(route.path != "/browser-bridge" for route in disabled_app.routes)


@pytest.mark.asyncio
async def test_mounted_browser_bridge_requires_loopback_and_token_even_for_health() -> None:
    token = "mounted-bridge-test-token-that-is-long-enough"
    mounted_app = FastAPI()
    bridge, live_system = _install_browser_bridge(
        mounted_app,
        Settings(
            _env_file=None,
            browser_bridge_enabled=True,
            browser_bridge_token=token,
        ),
    )
    mounted_app.middleware("http")(protect_mounted_browser_bridge)
    assert bridge is not None
    assert live_system is not None

    async with AsyncClient(
        transport=ASGITransport(app=mounted_app, client=("127.0.0.1", 51342)),
        base_url="http://127.0.0.1",
    ) as client:
        missing_token = await client.get("/browser-bridge/health")
        healthy = await client.get(
            "/browser-bridge/health",
            headers={BRIDGE_TOKEN_HEADER: token},
        )
        claimed = await client.post(
            "/browser-bridge/v1/tasks/claim",
            headers={BRIDGE_TOKEN_HEADER: token},
            json={"companion_id": "fixture-companion"},
        )
        companion_status = await client.get(
            "/browser-bridge/v1/companions/status",
            headers={BRIDGE_TOKEN_HEADER: token},
        )

    async with AsyncClient(
        transport=ASGITransport(app=mounted_app, client=("192.0.2.10", 51342)),
        base_url="http://127.0.0.1",
    ) as remote:
        remote_health = await remote.get(
            "/browser-bridge/health",
            headers={BRIDGE_TOKEN_HEADER: token},
        )
        remote_companion_status = await remote.get(
            "/browser-bridge/v1/companions/status",
            headers={BRIDGE_TOKEN_HEADER: token},
        )

    assert missing_token.status_code == 401
    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok", "scope": "local-read-only-browser"}
    assert claimed.status_code == 200
    assert claimed.json() == {"leases": []}
    assert companion_status.status_code == 200
    assert companion_status.json()["status"] == "connected"
    assert companion_status.json()["stale_after_seconds"] == 45
    assert remote_health.status_code == 403
    assert remote_companion_status.status_code == 403


@pytest.mark.asyncio
async def test_live_plan_is_explicitly_unavailable_when_bridge_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "live_package_agent_system", None)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-plan",
            json=_live_request_payload(),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://test",
    ) as remote:
        remote_response = await remote.post(
            "/api/v1/agents/live-plan",
            json=_live_request_payload(),
        )

    assert response.status_code == 503
    assert "实时浏览器核价未启用" in response.json()["detail"]
    assert remote_response.status_code == 403


@pytest.mark.asyncio
async def test_live_plan_and_event_replan_share_tenant_bound_serialized_run_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_system = _FakeLiveSystem()
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5))
    monkeypatch.setattr(app.state, "live_package_agent_system", fake_system)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        degraded_payload = {**_live_request_payload(), "coverage_mode": "degraded"}
        degraded = await client.post(
            "/api/v1/agents/live-plan",
            json=degraded_payload,
        )
        assert degraded.status_code == 422

        planned = await client.post(
            "/api/v1/agents/live-plan",
            json=_live_request_payload(),
        )
        assert planned.status_code == 200
        body = planned.json()
        run_id = body["run_id"]
        expires_at = body["expires_at"]
        assert len(body["run"]["source_task_ids"]) == 11
        assert body["run"]["decision"]["state"] == "human_block"

        async def replan(event_id: str) -> object:
            return await client.post(
                f"/api/v1/agents/live-plans/{run_id}/events/replan",
                json={
                    "event": {
                        "id": event_id,
                        "kind": "price_changed",
                        "target_component_id": "fixture-component",
                        "affected_provider": "ctrip",
                    },
                    "timeout_seconds": 20,
                },
            )

        replanned = await asyncio.gather(replan("event-1"), replan("event-2"))

    assert fake_system.run_calls == 1
    assert all(response.status_code == 200 for response in replanned)
    assert all(response.json()["run_id"] == run_id for response in replanned)
    assert all(response.json()["expires_at"] == expires_at for response in replanned)
    assert fake_system.max_active_replans == 1
    assert len(fake_system.previous_runs) == 2
    assert fake_system.previous_runs[0].decision.summary.startswith("测试仅验证")
    assert fake_system.previous_runs[1].decision.summary.startswith("event handled:")
    cached = await cache.get(run_id, "anonymous")
    assert cached is not None
    assert len(cached.run.source_task_ids) == 13


@pytest.mark.asyncio
async def test_exploration_only_run_cannot_enter_event_replanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_system = _FakeLiveSystem()
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5))
    final_run = _blocked_live_run()
    exploration_run = final_run.model_copy(
        update={
            "run_purpose": LiveRunPurpose.EXPLORATION_SELECTION,
            "finalization_state": LiveFinalizationState.EXPLORATION_SEALED,
            "deferred_stage_ids": (
                "explain-final-decision",
                "curate-run-memory",
                "publish-live-run",
            ),
            "exploration_seal_passed": True,
        }
    )
    event = LivePackageEvent(
        id="event-exploration-must-not-run",
        kind=PackageEventKind.PRICE_CHANGED,
        target_component_id="fixture-component",
        affected_provider=LiveDataProvider.CTRIP,
    )
    replanned = await fake_system.replan_after_event(
        final_run,
        event,
        timeout_seconds=20,
    )
    with pytest.raises(ValueError, match="final-published"):
        _advance_cached_live_run(exploration_run, replanned)

    fake_system.previous_runs.clear()
    run_id, _ = await cache.put("anonymous", exploration_run)
    monkeypatch.setattr(app.state, "live_package_agent_system", fake_system)
    monkeypatch.setattr(app.state, "live_run_cache", cache)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/events/replan",
            json={"event": event.model_dump(mode="json"), "timeout_seconds": 20},
        )
        monitor_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/monitor",
            json={
                "interval_seconds": 60,
                "max_checks": 1,
                "timeout_seconds": 20,
            },
        )

    assert response.status_code == 422
    assert "publication refresh first" in response.json()["detail"]
    assert monitor_response.status_code == 422
    assert "publication refresh first" in monitor_response.json()["detail"]
    assert fake_system.previous_runs == []


@pytest.mark.asyncio
async def test_live_monitor_api_is_opt_in_bounded_and_cancellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_system = _FakeLiveSystem()
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5))
    seen_tenants: list[str] = []

    async def revalidate_once(
        status: LiveMonitorStatus,
        tenant_id: str,
    ) -> LiveMonitorCheck:
        seen_tenants.append(tenant_id)
        monitor = status
        return LiveMonitorCheck(
            sequence=monitor.check_count + 1,
            checked_at=datetime.now(UTC),
            target_component_id="fixture-component",
            event_id="monitor-event-fixture",
            applied_disposition="local_repair",
            decision_state="human_block",
            package_changed=False,
            summary="fixture proves monitor lifecycle only; no OTA was queried",
        )

    registry = LiveQuoteMonitorRegistry(revalidate_once)
    monkeypatch.setattr(app.state, "live_package_agent_system", fake_system)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(main_module, "live_quote_monitor_registry", registry)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            planned = await client.post(
                "/api/v1/agents/live-plan",
                json=_live_request_payload(),
            )
            assert planned.status_code == 200
            run_id = planned.json()["run_id"]

            blocked_start = await client.post(
                f"/api/v1/agents/live-plans/{run_id}/monitor",
                json={
                    "interval_seconds": 60,
                    "max_checks": 2,
                    "timeout_seconds": 20,
                },
            )
            assert blocked_start.status_code == 409

            entry = await cache.get(run_id, "anonymous")
            assert entry is not None
            entry.run = entry.run.model_copy(
                update={
                    "decision": PackageDecision(
                        state=PackageDecisionState.ACCEPT,
                        summary="fixture accepted for monitor lifecycle test only",
                    )
                }
            )

            started = await client.post(
                f"/api/v1/agents/live-plans/{run_id}/monitor",
                json={
                    "interval_seconds": 60,
                    "max_checks": 2,
                    "timeout_seconds": 20,
                },
            )
            assert started.status_code == 201
            monitor_id = started.json()["monitor"]["id"]
            assert started.json()["monitor"]["state"] == "active"
            assert "不是供应商推送" in started.json()["monitor"]["boundary"]

            current = await client.get(f"/api/v1/agents/live-plans/{run_id}")
            assert current.status_code == 200
            assert current.json()["run_id"] == run_id
            assert current.json()["run"]["decision"]["state"] == "accept"

            checked = await client.post(
                f"/api/v1/agents/live-monitors/{monitor_id}/check-now"
            )
            assert checked.status_code == 200
            assert checked.json()["monitor"]["check_count"] == 1
            assert checked.json()["monitor"]["last_check"]["event_id"] == (
                "monitor-event-fixture"
            )

            fetched = await client.get(
                f"/api/v1/agents/live-monitors/{monitor_id}"
            )
            assert fetched.status_code == 200
            assert fetched.json()["monitor"]["state"] == "active"

            stopped = await client.delete(
                f"/api/v1/agents/live-monitors/{monitor_id}"
            )
            assert stopped.status_code == 200
            assert stopped.json()["monitor"]["state"] == "stopped"
    finally:
        await registry.close()

    assert seen_tenants == ["anonymous"]


@pytest.mark.asyncio
async def test_live_run_cache_has_fixed_ttl_lru_capacity_and_tenant_isolation() -> None:
    clock = [datetime(2026, 7, 30, 12, 0, tzinfo=UTC)]
    cache = LiveRunCache(
        capacity=2,
        ttl=timedelta(seconds=30),
        now=lambda: clock[0],
    )
    run = _blocked_live_run()
    first_id, first_expiry = await cache.put("tenant-a", run)
    clock[0] += timedelta(seconds=1)
    second_id, _ = await cache.put("tenant-a", run)

    assert await cache.get(first_id, "tenant-b") is None
    assert await cache.get(first_id, "tenant-a") is not None
    third_id, _ = await cache.put("tenant-a", run)

    assert await cache.get(second_id, "tenant-a") is None
    assert await cache.get(first_id, "tenant-a") is not None
    assert await cache.get(third_id, "tenant-a") is not None
    clock[0] = first_expiry
    assert await cache.get(first_id, "tenant-a") is None


def test_live_run_cache_builder_resolves_default_relative_path_and_supports_off(
    tmp_path: Path,
) -> None:
    enabled = _build_live_run_cache(Settings(_env_file=None), tmp_path)
    disabled = _build_live_run_cache(
        Settings(_env_file=None, live_run_cache_state_path=""),
        tmp_path,
    )

    assert enabled.state_path == tmp_path / ".runtime" / "live-run-cache.json"
    assert enabled.persistence_enabled is True
    assert disabled.state_path is None
    assert disabled.persistence_enabled is False


@pytest.mark.asyncio
async def test_live_run_cache_restart_restores_only_unexpired_tenant_partition(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    state_path = tmp_path / "runtime" / "live-run-cache.json"
    cache = LiveRunCache(
        capacity=4,
        ttl=timedelta(minutes=30),
        now=lambda: clock[0],
        state_path=state_path,
    )
    run_id, expires_at = await cache.put("tenant-a", _blocked_live_run())

    snapshot_text = state_path.read_text(encoding="utf-8")
    assert "tenant-a" not in snapshot_text
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    clock[0] += timedelta(minutes=5)
    restarted = LiveRunCache(
        capacity=4,
        ttl=timedelta(minutes=30),
        now=lambda: clock[0],
        state_path=state_path,
    )

    assert await restarted.get(run_id, "tenant-b") is None
    restored = await restarted.get(run_id, "tenant-a")
    assert restored is not None
    assert restored.expires_at == expires_at
    assert restored.run == _blocked_live_run()


@pytest.mark.asyncio
async def test_live_run_cache_restart_drops_expired_state_without_extending_ttl(
    tmp_path: Path,
) -> None:
    clock = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    state_path = tmp_path / "live-run-cache.json"
    cache = LiveRunCache(
        ttl=timedelta(seconds=30),
        now=lambda: clock[0],
        state_path=state_path,
    )
    run_id, expires_at = await cache.put("tenant-a", _blocked_live_run())

    clock[0] = expires_at
    restarted = LiveRunCache(
        ttl=timedelta(seconds=30),
        now=lambda: clock[0],
        state_path=state_path,
    )

    assert await restarted.get(run_id, "tenant-a") is None
    assert json.loads(state_path.read_text(encoding="utf-8"))["entries"] == []


@pytest.mark.asyncio
async def test_live_run_cache_retires_verified_previous_schema_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    cache = LiveRunCache(now=lambda: now, state_path=state_path)
    run_id, _ = await cache.put("tenant-a", _blocked_live_run())
    previous = json.loads(state_path.read_text(encoding="utf-8"))
    previous["schema_version"] = 1
    state_path.write_text(json.dumps(previous), encoding="utf-8")

    previous_bytes = state_path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated atomic replace failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(main_module.os, "replace", fail_replace)
        with pytest.raises(LiveRunCacheWriteError, match=r"atomic.*write failed"):
            LiveRunCache(now=lambda: now, state_path=state_path)
    assert state_path.read_bytes() == previous_bytes
    assert tuple(tmp_path.glob(".live-run-cache.json.tmp-*")) == ()

    restarted = LiveRunCache(now=lambda: now, state_path=state_path)
    rewritten = json.loads(state_path.read_text(encoding="utf-8"))

    assert await restarted.get(run_id, "tenant-a") is None
    assert rewritten["schema_version"] == main_module._LIVE_RUN_CACHE_SCHEMA_VERSION
    assert rewritten["entries"] == []
    assert rewritten["entries_sha256"] == LiveRunCache._entries_digest([])


def test_live_run_cache_retires_digest_valid_v1_without_parsing_legacy_runs(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    legacy_entries: list[object] = [
        {
            "legacy_contract": "pre-dag-seal",
            "run": {"intentionally": "invalid under the v2 model"},
        }
    ]
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": legacy_entries,
                "entries_sha256": LiveRunCache._entries_digest(legacy_entries),
            }
        ),
        encoding="utf-8",
    )

    restarted = LiveRunCache(now=lambda: now, state_path=state_path)
    rewritten = json.loads(state_path.read_text(encoding="utf-8"))

    assert restarted.persistence_enabled is True
    assert rewritten == {
        "entries": [],
        "entries_sha256": LiveRunCache._entries_digest([]),
        "schema_version": main_module._LIVE_RUN_CACHE_SCHEMA_VERSION,
    }


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "entries": []},
        {
            "schema_version": 1,
            "entries": {},
            "entries_sha256": LiveRunCache._entries_digest([]),
        },
    ],
)
def test_live_run_cache_v1_requires_a_valid_envelope_before_retirement(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    state_path.write_text(json.dumps(document), encoding="utf-8")
    previous_bytes = state_path.read_bytes()

    with pytest.raises(LiveRunCacheLoadError, match="envelope is incomplete"):
        LiveRunCache(now=lambda: now, state_path=state_path)

    assert state_path.read_bytes() == previous_bytes
    assert tuple(tmp_path.glob(".live-run-cache.json.tmp-*")) == ()


@pytest.mark.asyncio
async def test_live_run_cache_previous_schema_bad_checksum_still_fails_closed(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    cache = LiveRunCache(now=lambda: now, state_path=state_path)
    await cache.put("tenant-a", _blocked_live_run())
    previous = json.loads(state_path.read_text(encoding="utf-8"))
    previous["schema_version"] = 1
    previous["entries"][0]["run"]["decision"]["summary"] = "tampered v1"
    state_path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(LiveRunCacheLoadError, match="checksum mismatch"):
        LiveRunCache(now=lambda: now, state_path=state_path)


def test_live_run_cache_current_schema_invalid_entry_still_fails_closed(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    LiveRunCache(now=lambda: now, state_path=state_path)
    document = {
        "schema_version": main_module._LIVE_RUN_CACHE_SCHEMA_VERSION,
        "entries": [
            {
                "run_id": "live-run-invalid-current-contract",
                "tenant_partition_sha256": "0" * 64,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=30)).isoformat(),
                "run": {},
            }
        ],
    }
    document["entries_sha256"] = LiveRunCache._entries_digest(document["entries"])
    state_path.write_text(json.dumps(document), encoding="utf-8")
    previous_bytes = state_path.read_bytes()

    with pytest.raises(LiveRunCacheLoadError, match="invalid entry"):
        LiveRunCache(now=lambda: now, state_path=state_path)

    assert state_path.read_bytes() == previous_bytes
    assert tuple(tmp_path.glob(".live-run-cache.json.tmp-*")) == ()


@pytest.mark.parametrize("schema_version", [True, 999])
def test_live_run_cache_rejects_invalid_or_unknown_schema(
    tmp_path: Path,
    schema_version: object,
) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / f"live-run-cache-{schema_version}.json"
    document = {
        "schema_version": schema_version,
        "entries": [],
        "entries_sha256": LiveRunCache._entries_digest([]),
    }
    state_path.write_text(json.dumps(document), encoding="utf-8")
    previous_bytes = state_path.read_bytes()

    expected = "invalid" if schema_version is True else "unsupported"
    with pytest.raises(LiveRunCacheLoadError, match=expected):
        LiveRunCache(now=lambda: now, state_path=state_path)

    assert state_path.read_bytes() == previous_bytes
    assert tuple(tmp_path.glob(f".{state_path.name}.tmp-*")) == ()


@pytest.mark.asyncio
async def test_live_run_cache_checksum_tampering_fails_closed(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    cache = LiveRunCache(now=lambda: now, state_path=state_path)
    await cache.put("tenant-a", _blocked_live_run())
    document = json.loads(state_path.read_text(encoding="utf-8"))
    document["entries"][0]["run"]["decision"]["summary"] = "tampered"
    state_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LiveRunCacheLoadError, match="checksum mismatch"):
        LiveRunCache(now=lambda: now, state_path=state_path)


def test_live_run_cache_corrupt_snapshot_can_be_quarantined(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    state_path = tmp_path / "live-run-cache.json"
    state_path.write_text("not-json", encoding="utf-8")

    restarted = LiveRunCache(
        now=lambda: now,
        state_path=state_path,
        corruption_policy=CorruptionPolicy.QUARANTINE,
    )

    assert restarted.persistence_enabled is True
    assert not state_path.exists()
    quarantined = tuple(tmp_path.glob("live-run-cache.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "not-json"
    assert stat.S_IMODE(quarantined[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_event_replace_is_durable_and_keeps_original_expiry(tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    state_path = tmp_path / "live-run-cache.json"
    cache = LiveRunCache(now=lambda: clock[0], state_path=state_path)
    original = _blocked_live_run()
    run_id, expires_at = await cache.put("tenant-a", original)
    entry = await cache.get(run_id, "tenant-a")
    assert entry is not None
    updated = original.model_copy(
        update={
            "decision": original.decision.model_copy(
                update={"summary": "event handled and durably replaced"}
            )
        }
    )

    clock[0] += timedelta(minutes=2)
    replaced_expiry = await cache.replace(run_id, "tenant-a", entry, updated)
    restarted = LiveRunCache(now=lambda: clock[0], state_path=state_path)
    durable = await restarted.get(run_id, "tenant-a")

    assert replaced_expiry == expires_at
    assert durable is not None
    assert durable.expires_at == expires_at
    assert durable.run.decision.summary == "event handled and durably replaced"
