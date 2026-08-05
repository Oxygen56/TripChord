from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import TypeAdapter
from tripchord.agents.flexible_live_system import FlexibleLiveAgentRun
from tripchord.agents.live_done_gate import evaluate_live_done_gate
from tripchord.agents.live_system import LiveEventReplanRun, LivePackageAgentRun
from tripchord.providers.browser_bridge import BRIDGE_TOKEN_HEADER

_FROM_TEXT_ENDPOINT = "/api/v1/agents/live-flexible-plan-from-text"
_COMPANION_STATUS_ENDPOINT = "/browser-bridge/v1/companions/status"
_COMPANION_PREFLIGHT_TIMEOUT_SECONDS = 5.0
_MAX_INITIAL_LIVE_ATTEMPTS = 2
_REQUIRED_BROWSER_PROVIDERS = frozenset({"ctrip", "qunar", "tongcheng"})
_EXPECTED_REQUIREMENT_TEXT = """出发地：杭州
目的地：马累
去程：2026-8月
返程：玩5-8天
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算、早餐无要求、星级无要求、无行李、接受中转"""
_EXPECTED_REFERENCE_DATE = "2026-07-30"
_EVIDENCE_SCHEMA_VERSION = "tripchord-live-evidence-v3"
_MISSING = object()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the authenticated TripChord live-v3 Done-Gate.",
    )
    parser.add_argument(
        "--request",
        type=Path,
        required=True,
        help=f"JSON body for POST {_FROM_TEXT_ENDPOINT}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Evidence bundle destination",
    )
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--api-token", default="")
    parser.add_argument(
        "--bridge-token",
        default=os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", ""),
        help=("Local Browser Bridge pairing token; defaults to TRIPCHORD_BROWSER_BRIDGE_TOKEN"),
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=1_000)
    parser.add_argument("--maximum-quote-age-minutes", type=int, default=15)
    return parser.parse_args()


def _headers(api_token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    return headers


def _response_json(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} returned non-JSON HTTP {response.status_code}") from exc
    if response.is_error:
        raise RuntimeError(f"{label} failed with HTTP {response.status_code}: {payload}")
    return TypeAdapter(dict[str, Any]).validate_python(payload)


def _nested_value(payload: dict[str, Any], path: tuple[str, ...]) -> object:
    current: object = payload
    for field in path:
        if not isinstance(current, dict) or field not in current:
            return _MISSING
        current = current[field]
    return current


def _same_json_value(actual: object, expected: object) -> bool:
    if expected is None:
        return actual is None
    return type(actual) is type(expected) and actual == expected


def _display_value(value: object) -> str:
    if value is _MISSING:
        return "<missing>"
    return repr(value)


def _validate_request_contract(request_body: dict[str, Any]) -> None:
    checks = (
        (("requirement", "text"), _EXPECTED_REQUIREMENT_TEXT),
        (("requirement", "reference_date"), _EXPECTED_REFERENCE_DATE),
        (("coverage_mode",), "strict"),
    )
    mismatches = [
        f"{'.'.join(path)} expected {_display_value(expected)}, got {_display_value(actual)}"
        for path, expected in checks
        if not _same_json_value(
            actual := _nested_value(request_body, path),
            expected,
        )
    ]
    if mismatches:
        raise RuntimeError(
            "live Done-Gate request contract failed before search: " + "; ".join(mismatches)
        )


def _preflight_from_text_response(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    checks: tuple[tuple[tuple[str, ...], object], ...] = (
        (("interpretation", "state"), "ready"),
        (("interpretation", "window", "origin"), "杭州"),
        (("interpretation", "window", "destination"), "马累"),
        (("interpretation", "window", "earliest_departure"), "2026-08-01"),
        (("interpretation", "window", "latest_departure"), "2026-08-31"),
        (("interpretation", "window", "min_nights"), 4),
        (("interpretation", "window", "max_nights"), 7),
        (("interpretation", "window", "adults"), 2),
        (("interpretation", "window", "rooms"), 1),
        (("interpretation", "intent_template", "origin"), "杭州"),
        (("interpretation", "intent_template", "destination"), "马累"),
        (("interpretation", "intent_template", "adults"), 2),
        (("interpretation", "intent_template", "rooms"), 1),
        (("interpretation", "intent_template", "require_checked_baggage"), False),
        (("interpretation", "intent_template", "require_breakfast"), None),
        (("interpretation", "unresolved"), []),
        (("interpretation", "conflicts"), []),
        (("model_enhancement_enabled",), False),
    )
    mismatches = [
        f"{'.'.join(path)} expected {_display_value(expected)}, got {_display_value(actual)}"
        for path, expected in checks
        if not _same_json_value(
            actual := _nested_value(payload, path),
            expected,
        )
    ]
    execution_boundary = payload.get("execution_boundary")
    if not isinstance(execution_boundary, str) or not execution_boundary.strip():
        mismatches.append("execution_boundary must be a non-empty string")
    run_payload = payload.get("run")
    if not isinstance(run_payload, dict):
        mismatches.append("ready interpretation must include an executable run object")
    interpretation_payload = payload.get("interpretation")
    if not isinstance(interpretation_payload, dict):
        mismatches.append("interpretation must be an object")
    if mismatches:
        raise RuntimeError(
            "natural-language interpretation preflight failed before event injection: "
            + "; ".join(mismatches)
        )
    assert isinstance(interpretation_payload, dict)
    assert isinstance(run_payload, dict)
    assert isinstance(execution_boundary, str)
    return interpretation_payload, run_payload, execution_boundary


async def _preflight_companion(
    client: httpx.AsyncClient,
    base: str,
    bridge_token: str,
) -> dict[str, Any]:
    if len(bridge_token) < 32:
        raise RuntimeError(
            "Browser Companion preflight failed / 浏览器 Companion 预检失败："
            "缺少至少 32 字符的本地桥配对令牌；请传入 --bridge-token 或设置 "
            "TRIPCHORD_BROWSER_BRIDGE_TOKEN。实时搜索尚未提交。"
        )
    try:
        response = await client.get(
            f"{base}{_COMPANION_STATUS_ENDPOINT}",
            headers={BRIDGE_TOKEN_HEADER: bridge_token},
            timeout=_COMPANION_PREFLIGHT_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            "Browser Companion preflight failed / 浏览器 Companion 预检失败："
            f"状态端点在 {_COMPANION_PREFLIGHT_TIMEOUT_SECONDS:g} 秒内未响应；"
            "实时搜索尚未提交。"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            "Browser Companion preflight failed / 浏览器 Companion 预检失败："
            f"无法连接本地状态端点（{exc}）；实时搜索尚未提交。"
        ) from exc

    payload = _response_json(response, "Browser Companion preflight / 浏览器 Companion 预检")
    stale_after = payload.get("stale_after_seconds")
    companions = payload.get("companions")
    if type(stale_after) is not int or stale_after <= 0 or not isinstance(companions, list):
        raise RuntimeError(
            "Browser Companion preflight failed / 浏览器 Companion 预检失败："
            "状态响应结构无效；实时搜索尚未提交。"
        )

    fresh_covering = []
    for companion in companions:
        if not isinstance(companion, dict):
            continue
        providers = companion.get("providers")
        age_seconds = companion.get("age_seconds")
        if (
            companion.get("is_fresh") is True
            and isinstance(providers, list)
            and _REQUIRED_BROWSER_PROVIDERS.issubset(
                provider for provider in providers if isinstance(provider, str)
            )
            and isinstance(age_seconds, int | float)
            and not isinstance(age_seconds, bool)
            and 0 <= age_seconds <= stale_after
        ):
            fresh_covering.append(companion)
    if not fresh_covering:
        raise RuntimeError(
            "Browser Companion preflight failed / 浏览器 Companion 预检失败："
                "没有发现同时声明携程、去哪儿、同程且仍新鲜的已连接 Companion；"
            f"心跳超过 {stale_after} 秒即过期。请打开扩展并确认“已连接，只读轮询中”。"
            "实时搜索尚未提交。"
        )
    return payload


async def _request_from_text_plan(
    client: httpx.AsyncClient,
    base: str,
    request_body: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    response = await client.post(
        f"{base}{_FROM_TEXT_ENDPOINT}",
        json=request_body,
    )
    payload = _response_json(response, "natural-language flexible live plan")
    interpretation, run_payload, execution_boundary = _preflight_from_text_response(payload)
    return payload, interpretation, run_payload, execution_boundary


def _selected_pair(
    flexible: FlexibleLiveAgentRun,
) -> tuple[str, LivePackageAgentRun]:
    if not flexible.recommended_option_ids:
        raise RuntimeError("flexible live run did not produce a recommendable date pair")
    selected_id = flexible.recommended_option_ids[0]
    for execution in flexible.pair_runs:
        if execution.date_pair.id == selected_id and execution.run is not None:
            return selected_id, execution.run
    raise RuntimeError("recommended date pair has no completed live run")


def _event_target(initial: LivePackageAgentRun) -> tuple[str, str]:
    if initial.package is None:
        raise RuntimeError("selected date pair has no accepted package")
    candidate = initial.package.final_candidate
    if candidate.lodgings:
        target = max(candidate.lodgings, key=lambda item: item.night_count)
        return target.id, target.provider
    return candidate.flight.id, candidate.flight.provider


async def _run(args: argparse.Namespace) -> int:
    request_body = TypeAdapter(dict[str, Any]).validate_python(
        json.loads(args.request.read_text(encoding="utf-8"))
    )
    _validate_request_contract(request_body)
    headers = _headers(args.api_token)
    timeout = httpx.Timeout(args.request_timeout_seconds)
    base = args.api_base.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        companion_preflight = await _preflight_companion(
            client,
            base,
            args.bridge_token,
        )
        initial_search_attempts: list[dict[str, Any]] = []
        best_attempt: tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, Any],
            str,
            FlexibleLiveAgentRun,
        ] | None = None
        for attempt_number in range(1, _MAX_INITIAL_LIVE_ATTEMPTS + 1):
            (
                flexible_payload,
                interpretation,
                run_payload,
                execution_boundary,
            ) = await _request_from_text_plan(client, base, request_body)
            flexible = FlexibleLiveAgentRun.model_validate(run_payload)
            initial_search_attempts.append(
                {
                    "attempt_number": attempt_number,
                    "recommended_option_ids": list(flexible.recommended_option_ids),
                    "final_decision": flexible.final_decision.model_dump(mode="json"),
                    "ranked_options": [
                        {
                            "date_pair_id": item.date_pair_id,
                            "decision_state": item.decision_state.value,
                            "evidence_completeness": str(item.evidence_completeness),
                            "all_platforms_complete": item.all_platforms_complete,
                        }
                        for item in flexible.ranked_options
                    ],
                }
            )
            if (
                best_attempt is None
                or len(flexible.recommended_option_ids)
                > len(best_attempt[4].recommended_option_ids)
            ):
                best_attempt = (
                    flexible_payload,
                    interpretation,
                    run_payload,
                    execution_boundary,
                    flexible,
                )
            if len(flexible.recommended_option_ids) >= 2:
                break
        if best_attempt is None:
            raise RuntimeError("flexible live search did not return an attempt")
        (
            flexible_payload,
            interpretation,
            run_payload,
            execution_boundary,
            flexible,
        ) = best_attempt
        selected_pair_id, initial = _selected_pair(flexible)
        handles = TypeAdapter(list[dict[str, Any]]).validate_python(
            flexible_payload.get("cached_pair_runs", [])
        )
        run_id = next(
            (
                str(item["run_id"])
                for item in handles
                if item.get("date_pair_id") == selected_pair_id
            ),
            None,
        )
        if run_id is None:
            raise RuntimeError("selected date pair was not cached for event replanning")
        target_id, provider = _event_target(initial)
        event_body = {
            "event": {
                "id": f"live-gate-price-change-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
                "kind": "price_changed",
                "target_component_id": target_id,
                "affected_provider": provider,
            }
        }
        event_response = await client.post(
            f"{base}/api/v1/agents/live-plans/{run_id}/events/replan",
            json=event_body,
        )
        event_payload = _response_json(event_response, "event replan")
        event = LiveEventReplanRun.model_validate(event_payload["run"])

    evaluated_at = datetime.now(UTC)
    report = evaluate_live_done_gate(
        initial,
        event,
        flexible=flexible,
        evaluated_at=evaluated_at,
        maximum_quote_age=timedelta(minutes=args.maximum_quote_age_minutes),
    )
    bundle = {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "captured_at": evaluated_at.isoformat(),
        "request": request_body,
        "companion_preflight": companion_preflight,
        "interpretation": interpretation,
        "model_enhancement_enabled": False,
        "execution_boundary": execution_boundary,
        "initial_search_attempts": initial_search_attempts,
        "flexible_run": flexible.model_dump(mode="json"),
        "selected_pair_id": selected_pair_id,
        "selected_run_id": run_id,
        "initial_run": initial.model_dump(mode="json"),
        "injected_event": event_body,
        "event_run": event.model_dump(mode="json"),
        "done_gate": report.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "passed": report.passed,
                "output": str(args.output),
                "bundle_sha256": report.bundle_sha256,
                "failed_checks": [check.id for check in report.checks if not check.passed],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.passed else 2


def main() -> None:
    try:
        exit_code = asyncio.run(_run(_arguments()))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
