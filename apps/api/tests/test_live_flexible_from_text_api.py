from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LivePackageAgentRun,
    PlatformSearchCoverage,
)
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelHTTPError,
    ModelMessage,
    ModelRequest,
    OpenAICompatibleChatClient,
)
from tripchord.agents.models import (
    AgentRole,
    AgentTask,
    AgentTaskResult,
    PreferenceMode,
    TaskGraph,
)
from tripchord.agents.runtime import SchedulerOutcome
from tripchord.main import (
    LiveRunCache,
    _flexible_total_timeout_seconds,
    _live_timeout_seconds,
    app,
    package_requirement_agent,
    settings,
)
from tripchord.planning.package import (
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackageInventory,
)
from tripchord.planning.stay_plans import system_stay_plan_candidate_set
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserSearchQuery,
    BrowserVertical,
)

NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
ORIGINAL_REQUEST = """出发地：杭州
目的地：马累
去程：2026-8月
返程：玩5-8天
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算、早餐无要求、星级无要求、无行李、接受中转"""


def _source_ids() -> tuple[str, ...]:
    return tuple(
        f"source-{provider.value}-{suffix}"
        for provider in BrowserProvider
        for suffix in (
            "flight",
            "lodging-full",
            "lodging-first",
            "lodging-middle",
            "lodging-last",
        )
    )


def _blocked_run(
    intent: PackageIntent,
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
            failure_reasons=("API contract fixture does not access a real browser",),
            complete=False,
        )
        for provider in BrowserProvider
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
        intent=intent,
        search_query=query,
        decision=PackageDecision(
            state=PackageDecisionState.HUMAN_BLOCK,
            summary="fixture browser search is intentionally blocked",
        ),
        claim_boundary="API contract fixture only; no live coverage claim",
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


class _RecordingPairRunner:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                PackageIntent,
                BrowserSearchQuery,
                LiveCoverageMode,
                int,
                dict[str, int] | None,
            ]
        ] = []

    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun:
        self.calls.append((intent, query, mode, timeout_seconds, source_start_delays_ms))
        return _blocked_run(intent, query, mode)


def _payload(
    *,
    text: str = ORIGINAL_REQUEST,
    coverage_mode: str = "strict",
    max_pairs: int = 2,
) -> dict[str, object]:
    return {
        "requirement": {
            "text": text,
            "reference_date": "2026-07-30",
        },
        "coverage_mode": coverage_mode,
        "timeout_seconds": 300,
        "total_timeout_seconds": 1800,
        "max_pairs": max_pairs,
    }


@pytest.mark.asyncio
async def test_ready_text_maps_constraints_runs_flexible_search_and_caches_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    interpretation = body["interpretation"]
    assert interpretation["state"] == "ready"
    assert interpretation["window"]["min_nights"] == 4
    assert interpretation["window"]["max_nights"] == 7
    assert interpretation["window"]["origin_code"] == "HGH"
    assert interpretation["window"]["destination_code"] == "MLE"
    assert interpretation["intent_template"]["require_checked_baggage"] is False
    assert interpretation["intent_template"]["require_breakfast"] is None
    assert interpretation["intent_template"]["breakfast_preference_mode"] == "indifferent"
    assert interpretation["intent_template"]["breakfast_preference_weight"] == 0
    assert body["model_enhancement_enabled"] is False
    assert len(body["model_trace_scope_sha256"]) == 64
    assert body["model_trace_count"] == 0
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 0
    assert "模型增强未启用" in body["execution_boundary"]
    assert "不是用户原话，可改" in body["execution_boundary"]
    assert "不是用户原话，可改" in interpretation["claim_boundary"]
    assert body["run"]["sampled_not_exhaustive"] is True
    assert "不得声称全月最低价" in body["run"]["claim_boundary"]
    assert "不是用户原话，可改" in body["run"]["claim_boundary"]
    profile = body["run"]["stay_area_search_profile"]
    assert profile == {
        "gateway_destination": "马累",
        "destination_island_lodging_search_term": "Maafushi",
        "airport_island_lodging_search_term": "Hulhumalé",
        "source": "system_derived_golden",
        "assumption_zh": (
            "系统生成的可比较自由行场景，不是用户原话，可改：马累/MLE 作为航班"
            "门户，整段及中段住宿搜索 Maafushi，首晚及末晚住宿搜索 Hulhumalé。"
        ),
    }
    frozen = system_stay_plan_candidate_set()
    assert body["run"]["stay_plan_candidate_set"]["candidate_set_sha256"] == (
        frozen.candidate_set_sha256
    )
    assert body["run"]["query_plan"]["stay_plan_candidate_set_sha256"] == (
        frozen.candidate_set_sha256
    )
    assert len(body["run"]["pair_runs"]) == 2
    assert len(body["cached_pair_runs"]) == 2
    assert len(pair_runner.calls) == 2
    for intent, query, mode, timeout_seconds, delays in pair_runner.calls:
        assert mode == LiveCoverageMode.STRICT
        assert timeout_seconds == 60
        assert intent.destination == "马累"
        assert intent.destination_place_key is None
        assert intent.require_checked_baggage is False
        assert intent.require_breakfast is None
        assert intent.breakfast_preference_mode == PreferenceMode.INDIFFERENT
        assert intent.breakfast_preference_weight == 0
        assert intent.budget_cents is None
        assert query.destination == "马累"
        assert query.origin_code == "HGH"
        assert query.destination_code == "MLE"
        assert query.options["gateway_destination"] == "马累"
        query_profile = query.options["stay_area_search_profile"]
        assert isinstance(query_profile, dict)
        assert query_profile["source"] == "system_derived_golden"
        query_candidate_set = query.options["stay_plan_candidate_set"]
        assert isinstance(query_candidate_set, dict)
        assert query_candidate_set["candidate_set_sha256"] == frozen.candidate_set_sha256
        assert delays is not None and len(delays) == 13
    for handle in body["cached_pair_runs"]:
        assert await cache.get(handle["run_id"], "anonymous") is not None
        assert await cache.get(handle["run_id"], "another-tenant") is None


@pytest.mark.asyncio
async def test_structured_breakfast_weight_reaches_every_pair_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        pair_runner,
        now=lambda: NOW,
        monotonic_clock=lambda: 100.0,
    )
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(
        app.state,
        "live_run_cache",
        LiveRunCache(capacity=8, ttl=timedelta(minutes=5), now=lambda: NOW),
    )
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)
    payload = _payload(max_pairs=1)
    requirement = payload["requirement"]
    assert isinstance(requirement, dict)
    requirement["breakfast_mode"] = "weighted"
    requirement["breakfast_weight"] = 0.87

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=payload,
        )

    assert response.status_code == 200
    assert len(pair_runner.calls) == 1
    pair_intent = pair_runner.calls[0][0]
    assert pair_intent.require_breakfast is None
    assert pair_intent.breakfast_preference_mode == PreferenceMode.WEIGHTED
    assert pair_intent.breakfast_preference_weight == 0.87


@pytest.mark.asyncio
async def test_human_block_returns_interpretation_without_resolving_live_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    payload = _payload(text=("出发地：杭州，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房"))
    requirement = payload["requirement"]
    assert isinstance(requirement, dict)
    requirement["breakfast_mode"] = "weighted"
    requirement["breakfast_weight"] = 0.9

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=payload,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert body["cached_pair_runs"] == []
    assert len(body["model_trace_scope_sha256"]) == 64
    assert body["model_trace_count"] == 0
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 0
    assert "destination" in {item["field"] for item in body["interpretation"]["unresolved"]}
    application_issue = next(
        item
        for item in body["interpretation"]["unresolved"]
        if item["field"] == "preference_application:hotel_breakfast"
    )
    assert "尚未启动实时报价与 Planner" in application_issue["reason"]


@pytest.mark.asyncio
async def test_human_block_response_binds_successful_model_trace_to_this_request(
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

        class TracingRequirementAgent:
            async def parse(self, request: Any) -> Any:
                await model_client.complete(
                    ModelRequest(
                        role=AgentRole.CONTEXT,
                        system="bounded requirement fixture",
                        messages=(ModelMessage(role="user", content=request.text),),
                    )
                )
                return await package_requirement_agent.parse(request)

        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(app.state, "package_requirement_agent", TracingRequirementAgent())
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(
                    text=(
                        "出发地：杭州，去程：2026年8月，玩5-8天，"
                        "人数：2名成人，酒店：1间房"
                    )
                ),
            )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert body["model_trace_count"] == 1
    assert body["model_trace_success_count"] == 1
    assert body["model_trace_failure_count"] == 0
    trace = sink.records[0]
    assert trace.scope_request_digest == body["model_trace_scope_sha256"]
    assert trace.scope_id is not None and trace.scope_id.startswith("model-scope-")
    assert "出发地：杭州" not in trace.model_dump_json()


@pytest.mark.asyncio
async def test_model_failure_is_not_misreported_as_successful_enhancement_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = InMemoryModelTraceSink()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"type": "temporarily_unavailable", "message": "private"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        model_client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        class FailureTolerantRequirementAgent:
            async def parse(self, request: Any) -> Any:
                with pytest.raises(ModelHTTPError):
                    await model_client.complete(
                        ModelRequest(
                            role=AgentRole.CONTEXT,
                            system="bounded failing fixture",
                            messages=(ModelMessage(role="user", content=request.text),),
                        )
                    )
                return await package_requirement_agent.parse(request)

        monkeypatch.setattr(app.state, "model_trace_sink", sink)
        monkeypatch.setattr(app.state, "model_router", object())
        monkeypatch.setattr(
            app.state,
            "package_requirement_agent",
            FailureTolerantRequirementAgent(),
        )
        monkeypatch.setattr(app.state, "flexible_live_agent_system", None)
        monkeypatch.setattr(app.state, "live_run_cache", LiveRunCache())
        monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(text="出发地：杭州，2026年8月出发，玩5晚，2名成人，1间房"),
            )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["model_trace_count"] == 1
    assert body["model_trace_success_count"] == 0
    assert body["model_trace_failure_count"] == 1


@pytest.mark.asyncio
async def test_unknown_city_iata_blocks_before_any_live_pair_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(pair_runner, now=lambda: NOW)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(
                text=("出发地：杭州，目的地：曼谷，2026年8月出发，玩5晚，2名成人，1间房")
            ),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["interpretation"]["state"] == "human_block"
    assert body["run"] is None
    assert not pair_runner.calls
    unresolved = {item["field"]: item for item in body["interpretation"]["unresolved"]}
    assert unresolved["destination_code"]["critical"] is True
    assert "避免模型猜测或伪造机场代码" in unresolved["destination_code"]["reason"]


@pytest.mark.asyncio
async def test_from_text_endpoint_enforces_strict_policy_before_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair_runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(pair_runner, now=lambda: NOW)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(coverage_mode="degraded"),
        )

    assert response.status_code == 422
    assert "strict three-platform coverage" in response.json()["detail"]
    assert not pair_runner.calls


@pytest.mark.asyncio
async def test_from_text_endpoint_is_loopback_only() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 51342)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-flexible-plan-from-text",
            json=_payload(),
        )

    assert response.status_code == 403


def test_from_text_timeouts_are_capped_by_server_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)

    assert _live_timeout_seconds(300) == 60
    assert _flexible_total_timeout_seconds(1800) == 120
