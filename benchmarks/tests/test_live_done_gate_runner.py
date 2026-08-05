from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from benchmarks import run_live_done_gate

SCENARIO_PATH = Path(__file__).parents[1] / "scenarios" / "live-hgh-mle-aug-2026.json"


def _request_body() -> dict[str, Any]:
    return TypeAdapter(dict[str, Any]).validate_python(
        json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    )


def _valid_response() -> dict[str, Any]:
    return {
        "interpretation": {
            "state": "ready",
            "window": {
                "origin": "杭州",
                "destination": "马累",
                "earliest_departure": "2026-08-01",
                "latest_departure": "2026-08-31",
                "min_nights": 4,
                "max_nights": 7,
                "adults": 2,
                "rooms": 1,
            },
            "intent_template": {
                "origin": "杭州",
                "destination": "马累",
                "adults": 2,
                "rooms": 1,
                "require_checked_baggage": False,
                "require_breakfast": None,
            },
            "unresolved": [],
            "conflicts": [],
        },
        "run": {},
        "cached_pair_runs": [],
        "model_enhancement_enabled": False,
        "execution_boundary": "模型增强未启用；仅在关键字段完整时执行实时搜索。",
    }


def _replace_nested(
    payload: dict[str, Any],
    path: tuple[str, ...],
    value: object,
) -> None:
    current = payload
    for field in path[:-1]:
        current = current[field]
    current[path[-1]] = value


def test_scenario_preserves_original_requirement_and_reference_date() -> None:
    request_body = _request_body()

    run_live_done_gate._validate_request_contract(request_body)
    assert run_live_done_gate._EVIDENCE_SCHEMA_VERSION == "tripchord-live-evidence-v3"
    assert request_body["requirement"]["text"] == run_live_done_gate._EXPECTED_REQUIREMENT_TEXT
    assert (
        request_body["requirement"]["reference_date"] == run_live_done_gate._EXPECTED_REFERENCE_DATE
    )


@pytest.mark.asyncio
async def test_runner_posts_to_natural_language_flexible_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_valid_response())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tripchord.test",
    ) as client:
        (
            payload,
            interpretation,
            run_payload,
            boundary,
        ) = await run_live_done_gate._request_from_text_plan(
            client,
            "http://tripchord.test",
            _request_body(),
        )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/agents/live-flexible-plan-from-text"
    assert json.loads(requests[0].content) == _request_body()
    assert payload["model_enhancement_enabled"] is False
    assert interpretation["state"] == "ready"
    assert run_payload == {}
    assert "模型增强未启用" in boundary


@pytest.mark.asyncio
async def test_runner_requires_fresh_three_provider_companion_before_live_plan() -> None:
    requests: list[httpx.Request] = []
    token = "fixture-bridge-token-that-is-long-enough"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "connected",
                "server_time": "2026-07-30T12:00:00Z",
                "stale_after_seconds": 45,
                "companions": [
                    {
                        "companion_id": "chrome-mv3-fixture",
                    "providers": ["ctrip", "qunar", "tongcheng"],
                        "last_seen": "2026-07-30T11:59:59Z",
                        "age_seconds": 1.0,
                        "is_fresh": True,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tripchord.test",
    ) as client:
        payload = await run_live_done_gate._preflight_companion(
            client,
            "http://tripchord.test",
            token,
        )

    assert payload["status"] == "connected"
    assert run_live_done_gate._COMPANION_PREFLIGHT_TIMEOUT_SECONDS == 5.0
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/browser-bridge/v1/companions/status"
    assert requests[0].headers["X-TripChord-Bridge-Token"] == token


@pytest.mark.parametrize(
    "companions",
    [
        [],
        [
            {
                "companion_id": "chrome-mv3-stale",
                "providers": ["ctrip", "qunar", "tongcheng"],
                "last_seen": "2026-07-30T11:58:00Z",
                "age_seconds": 120.0,
                "is_fresh": False,
            }
        ],
        [
            {
                "companion_id": "chrome-mv3-partial",
                "providers": ["ctrip", "qunar"],
                "last_seen": "2026-07-30T11:59:59Z",
                "age_seconds": 1.0,
                "is_fresh": True,
            }
        ],
    ],
)
@pytest.mark.asyncio
async def test_runner_fails_fast_without_fresh_three_provider_companion(
    companions: list[dict[str, Any]],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "disconnected",
                "server_time": "2026-07-30T12:00:00Z",
                "stale_after_seconds": 45,
                "companions": companions,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tripchord.test",
    ) as client:
        with pytest.raises(
            RuntimeError,
            match="没有发现同时声明携程、去哪儿、同程且仍新鲜",
        ):
            await run_live_done_gate._preflight_companion(
                client,
                "http://tripchord.test",
                "fixture-bridge-token-that-is-long-enough",
            )


@pytest.mark.asyncio
async def test_runner_rejects_missing_bridge_token_without_http_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tripchord.test",
    ) as client:
        with pytest.raises(RuntimeError, match="缺少至少 32 字符"):
            await run_live_done_gate._preflight_companion(
                client,
                "http://tripchord.test",
                "",
            )

    assert requests == []


@pytest.mark.asyncio
async def test_run_does_not_submit_live_plan_when_companion_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "disconnected",
                "server_time": "2026-07-30T12:00:00Z",
                "stale_after_seconds": 45,
                "companions": [],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        run_live_done_gate.httpx,
        "AsyncClient",
        lambda **_: client,
    )
    args = SimpleNamespace(
        request=SCENARIO_PATH,
        output=tmp_path / "must-not-exist.json",
        api_base="http://tripchord.test",
        api_token="",
        bridge_token="fixture-bridge-token-that-is-long-enough",
        request_timeout_seconds=1_000,
        maximum_quote_age_minutes=15,
    )

    with pytest.raises(
        RuntimeError,
        match="没有发现同时声明携程、去哪儿、同程且仍新鲜",
    ):
        await run_live_done_gate._run(args)

    assert [request.url.path for request in requests] == ["/browser-bridge/v1/companions/status"]
    assert not args.output.exists()


def test_main_reports_runtime_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_: object) -> int:
        raise RuntimeError("Companion 未连接")

    monkeypatch.setattr(run_live_done_gate, "_arguments", lambda: object())
    monkeypatch.setattr(run_live_done_gate, "_run", fail)

    with pytest.raises(SystemExit) as exit_info:
        run_live_done_gate.main()

    captured = capsys.readouterr()
    assert exit_info.value.code == 2
    assert captured.err == "Companion 未连接\n"
    assert captured.out == ""


@pytest.mark.parametrize(
    ("path", "damaged_value"),
    [
        (("interpretation", "state"), "human_block"),
        (("interpretation", "window", "origin"), "上海"),
        (("interpretation", "window", "max_nights"), 8),
        (
            ("interpretation", "unresolved"),
            [{"field": "destination", "critical": True}],
        ),
        (("model_enhancement_enabled",), True),
    ],
)
@pytest.mark.asyncio
async def test_runner_fails_closed_on_damaged_interpretation(
    path: tuple[str, ...],
    damaged_value: object,
) -> None:
    response_payload = copy.deepcopy(_valid_response())
    _replace_nested(response_payload, path, damaged_value)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://tripchord.test",
    ) as client:
        with pytest.raises(
            RuntimeError,
            match="natural-language interpretation preflight failed",
        ):
            await run_live_done_gate._request_from_text_plan(
                client,
                "http://tripchord.test",
                _request_body(),
            )
