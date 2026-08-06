from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
    PlatformSearchCoverage,
)
from tripchord.agents.models import AgentRole, AgentTask, AgentTaskResult, TaskGraph
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.main import (
    LiveRunCache,
    _flexible_total_timeout_seconds,
    app,
    settings,
)
from tripchord.planning.package import (
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
)
from tripchord.providers.browser_bridge import (
    LIVE_V5_BROWSER_PROVIDERS,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserVertical,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def _source_ids() -> tuple[str, ...]:
    return tuple(
        f"source-{provider.value}-{suffix}"
        for provider in LIVE_V5_BROWSER_PROVIDERS
        for suffix in (
            "flight",
            *((
                "lodging-full",
                "lodging-first",
                "lodging-middle",
                "lodging-last",
            ) if provider != BrowserProvider.TONGCHENG else ()),
        )
    )


def _blocked_run(
    request: PackageIntent,
    query: BrowserSearchQuery,
    mode: LiveCoverageMode,
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
            failure_reasons=("fixture does not access a real browser",),
            complete=False,
        )
        for provider in LIVE_V5_BROWSER_PROVIDERS
    )
    final_tasks = (
        AgentTask(
            id="orchestrate-travel-package",
            role=AgentRole.SAFETY_GATE,
            goal="fixture deterministic decision",
        ),
        AgentTask(
            id="explain-final-decision",
            role=AgentRole.EXPLANATION,
            goal="fixture explanation",
            dependencies=("orchestrate-travel-package",),
        ),
        AgentTask(
            id="curate-run-memory",
            role=AgentRole.MEMORY_CURATOR,
            goal="fixture memory curation",
            dependencies=("explain-final-decision",),
        ),
        AgentTask(
            id="publish-live-run",
            role=AgentRole.SAFETY_GATE,
            goal="fixture publication gate",
            dependencies=("curate-run-memory",),
        ),
    )
    return LivePackageAgentRun(
        mode=mode,
        intent=request,
        search_query=query,
        decision=PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="fixture browser search is intentionally blocked",
        ),
        claim_boundary="fixture only; no live platform coverage claim",
        all_platforms_complete=False,
        coverage=coverage,
        inventory=PackageInventory(),
        normalization_results=(),
        package=None,
        scheduler=SchedulerOutcome(
            graph=TaskGraph(tasks=final_tasks),
            results=tuple(
                AgentTaskResult(
                    task_id=task.id,
                    agent_role=task.role,
                    success=True,
                    summary="fixture stage complete",
                    output={"publication_gate_passed": True}
                    if task.id == "publish-live-run"
                    else {},
                )
                for task in final_tasks
            ),
            trace=(),
            wall_time_seconds=0,
            max_parallel_tasks=15,
            succeeded=True,
        ),
        source_task_ids=source_ids,
    )


class _FakePairRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        request: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        self.calls += 1
        assert timeout_seconds == 20
        assert source_start_delays_ms is not None
        assert len(source_start_delays_ms) == 11
        return _blocked_run(request, query, mode)


def _payload(*, coverage_mode: str = "strict") -> dict[str, object]:
    return {
        "window": {
            "origin": "HGH",
            "destination": "MLE",
            "earliest_departure": "2026-08-23",
            "latest_departure": "2026-08-23",
            "min_nights": 7,
            "max_nights": 7,
            "max_pairs": 3,
            "adults": 2,
            "rooms": 1,
            "currency": "CNY",
        },
        "coverage_mode": coverage_mode,
        "timeout_seconds": 20,
        "total_timeout_seconds": 120,
        "max_pairs": 1,
    }


def test_flexible_http_timeout_cannot_exceed_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    assert _flexible_total_timeout_seconds(None) == 120
    assert _flexible_total_timeout_seconds(1800) == 120
    assert _flexible_total_timeout_seconds(60) == 60


@pytest.mark.asyncio
async def test_flexible_live_plan_is_disabled_and_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        disabled = await client.post(
            "/api/v1/agents/live-flexible-plan",
            json=_payload(),
        )

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://test",
    ) as remote:
        forbidden = await remote.post(
            "/api/v1/agents/live-flexible-plan",
            json=_payload(),
        )

    assert disabled.status_code == 503
    assert "灵活日期实时核价未启用" in disabled.json()["detail"]
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_flexible_live_plan_enforces_strict_server_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _FakePairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan",
            json=_payload(coverage_mode="degraded"),
        )

    assert response.status_code == 422
    assert "strict full-coverage mode" in response.json()["detail"]
    assert pair_runner.calls == 0


@pytest.mark.asyncio
async def test_flexible_live_success_caches_each_pair_for_event_replanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _FakePairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5), now=lambda: NOW)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan",
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["query_plan"]["total_task_count"] == 11
    assert len(body["run"]["pair_runs"]) == 1
    assert body["run"]["sampled_not_exhaustive"] is False
    assert "不得声称全月最低价" in body["run"]["claim_boundary"]
    assert body["run"]["final_decision"]["state"] == "human_block"
    assert len(body["cached_pair_runs"]) == 1
    handle = body["cached_pair_runs"][0]
    assert handle["date_pair_id"] == body["run"]["pair_runs"][0]["date_pair"]["id"]
    assert await cache.get(handle["run_id"], "anonymous") is not None
    assert await cache.get(handle["run_id"], "another-tenant") is None
    assert pair_runner.calls == 1
