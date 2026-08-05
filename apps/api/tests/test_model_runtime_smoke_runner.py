from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scripts.run_model_runtime_smoke import (
    build_parser,
    execute_from_arguments,
    main,
    validate_arguments,
)


def _arguments(tmp_path: Path) -> list[str]:
    return [
        "--ack-live-cost",
        "--provider",
        "openai_compatible",
        "--model",
        "mock-model",
        "--base-url",
        "https://model.example/v1",
        "--output",
        str(tmp_path / "model-smoke.json"),
    ]


def test_cli_without_cost_ack_exits_before_any_model_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "must-not-be-used")
    with pytest.raises(SystemExit):
        main(
            [
                "--provider",
                "openai_compatible",
                "--model",
                "mock-model",
                "--base-url",
                "https://unreachable.invalid/v1",
                "--output",
                str(tmp_path / "must-not-exist.json"),
            ]
        )
    assert not (tmp_path / "must-not-exist.json").exists()


@pytest.mark.asyncio
async def test_mock_smoke_emits_hashes_traces_and_no_secret_or_prompt_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "model-api-key-super-secret"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    call_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        call_bodies.append(json.loads(request.content))
        index = len(call_bodies)
        if index == 1:
            message: dict[str, object] = {"role": "assistant", "content": '{"status":"ok"}'}
            finish_reason = "stop"
        elif index == 2:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tool-smoke-1",
                        "type": "function",
                        "function": {"name": "inspect_quotes", "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        elif index == 3:
            message = {
                "role": "assistant",
                "content": '{"selected_quote_id":"q1","summary":"observed quote selected"}',
            }
            finish_reason = "stop"
        else:
            raise AssertionError("runner exceeded its three-call contract")
        return httpx.Response(
            200,
            json={
                "id": f"mock-response-{index}",
                "choices": [{"finish_reason": finish_reason, "message": message}],
                "usage": {"prompt_tokens": 10 * index, "completion_tokens": 3 * index},
            },
        )

    parser = build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    validate_arguments(parser, args)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await execute_from_arguments(args, http_client=client)

    serialized = json.dumps(report, ensure_ascii=False)
    assert len(call_bodies) == 3
    assert report["schema_version"] == "tripchord-model-runtime-smoke-v2"
    assert report["provider_adapter"] == "openai_compatible"
    assert report["model"] == "mock-model"
    assert report["structured_json"]["structured_output"] == {"status": "ok"}
    assert report["bounded_tool_loop"]["observed_tool_names"] == ["inspect_quotes"]
    assert report["bounded_tool_loop"]["selected_quote_id"] == "q1"
    assert report["aggregate"]["logical_model_calls"] == 3
    assert report["request_trace_hashes_match"] is True
    assert len(report["traces"]) == 3
    assert len(report["code_sha256"]) == 64
    assert all(len(item) == 64 for item in report["bounded_tool_loop"]["request_sha256"])
    assert secret not in serialized
    assert "observed quote selected" not in serialized
    assert "Report gateway health" not in serialized
    assert "Select one observed quote id" not in serialized


@pytest.mark.asyncio
async def test_execute_requires_key_and_cost_ack_before_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="missing API key"):
        await execute_from_arguments(args)

    args.ack_live_cost = False
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    with pytest.raises(RuntimeError, match="cost acknowledgement"):
        await execute_from_arguments(args)
