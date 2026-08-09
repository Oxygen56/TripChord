from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelClientConfig,
    ModelGatewayError,
    ModelHTTPError,
    ModelMessage,
    ModelPricing,
    ModelProviderName,
    ModelRequest,
    ModelResponse,
    ModelRetryPolicy,
    ModelThinkingMode,
    ModelTool,
    ModelToolResult,
    OpenAICompatibleChatClient,
    RetryingModelClient,
    StructuredOutputError,
    build_model_client,
)
from tripchord.agents.models import AgentRole


@pytest.mark.asyncio
async def test_openai_compatible_client_enforces_schema_and_records_cost() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"decision":"accept"}'},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    sink = InMemoryModelTraceSink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            api_key="secret",
            model="test-model",
            base_url="https://llm.example/v1",
            http_client=http_client,
            pricing=ModelPricing(
                input_usd_per_million_tokens=2,
                output_usd_per_million_tokens=6,
            ),
            trace_sink=sink,
        )
        response = await client.complete(
            ModelRequest(
                role=AgentRole.ORCHESTRATOR,
                system="Return a decision",
                messages=(ModelMessage(role="user", content="decide"),),
                response_schema={
                    "type": "object",
                    "required": ["decision"],
                    "additionalProperties": False,
                    "properties": {"decision": {"type": "string", "enum": ["accept"]}},
                },
            )
        )

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"]["type"] == "json_schema"
    assert response.structured_output == {"decision": "accept"}
    assert response.estimated_cost_usd == pytest.approx(0.00032)
    assert response.trace_id is not None
    assert len(sink.records) == 1
    assert sink.records[0].request_digest != ""
    assert sink.records[0].success is True


@pytest.mark.asyncio
async def test_openai_compatible_client_rejects_schema_mismatch() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": '{"wrong":true}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="test-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
        )
        with pytest.raises(StructuredOutputError):
            await client.complete(
                ModelRequest(
                    role=AgentRole.CONTEXT,
                    system="strict json",
                    messages=(ModelMessage(role="user", content="parse"),),
                    response_schema={
                        "type": "object",
                        "required": ["value"],
                        "properties": {"value": {"type": "integer"}},
                    },
                )
            )


@pytest.mark.asyncio
async def test_deepseek_auto_mode_uses_json_object_and_client_side_schema_gate() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"status":"ok"}',
                            "reasoning_content": "ephemeral-provider-reasoning",
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        response = await OpenAICompatibleChatClient(
            api_key="test-only",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            http_client=http_client,
        ).complete(
            ModelRequest(
                role=AgentRole.EVENT_DIAGNOSER,
                system="Return JSON.",
                messages=(ModelMessage(role="user", content="status"),),
                response_schema={
                    "type": "object",
                    "required": ["status"],
                    "additionalProperties": False,
                    "properties": {"status": {"type": "string", "const": "ok"}},
                },
            )
        )

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    messages = body["messages"]
    assert isinstance(messages, list)
    assert "JSON Schema" in messages[0]["content"]
    assert response.structured_output == {"status": "ok"}
    assert response.reasoning_content == "ephemeral-provider-reasoning"
    assert "reasoning_content" not in response.model_dump(mode="json")


@pytest.mark.asyncio
async def test_deepseek_enabled_thinking_replays_reasoning_with_tool_turn() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body)
        if len(captured) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "must-survive-tool-round",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "inspect",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"status":"ok"}'},
                    }
                ]
            },
        )

    tool = ModelTool(name="inspect", description="inspect", input_schema={"type": "object"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            http_client=http_client,
            thinking_mode=ModelThinkingMode.ENABLED,
        )
        first = await client.complete(
            ModelRequest(
                role=AgentRole.RISK_CRITIC,
                system="inspect first",
                messages=(ModelMessage(role="user", content="check"),),
                tools=(tool,),
            )
        )
        final = await client.complete(
            ModelRequest(
                role=AgentRole.RISK_CRITIC,
                system="inspect first",
                messages=(
                    ModelMessage(role="user", content="check"),
                    ModelMessage(
                        role="assistant",
                        content=first.text,
                        reasoning_content=first.reasoning_content,
                        tool_calls=first.tool_calls,
                    ),
                    ModelMessage(
                        role="user",
                        tool_results=(
                            ModelToolResult(tool_call_id="call-1", content='{"ok":true}'),
                        ),
                    ),
                ),
                tools=(tool,),
                response_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string", "const": "ok"}},
                },
            )
        )

    assert captured[0]["thinking"] == {"type": "enabled"}
    second_messages = captured[1]["messages"]
    assert isinstance(second_messages, list)
    assistant = next(item for item in second_messages if item["role"] == "assistant")
    assert assistant["content"] == ""
    assert assistant["reasoning_content"] == "must-survive-tool-round"
    assert len(assistant["tool_calls"]) == 1
    assert final.structured_output == {"status": "ok"}


@pytest.mark.asyncio
async def test_structured_output_retry_adds_bounded_repair_without_replaying_bad_text() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        content = "malformed-untrusted-output" if len(bodies) == 1 else '{"status":"ok"}'
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = build_model_client(
            ModelClientConfig(
                provider=ModelProviderName.OPENAI_COMPATIBLE,
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                retry=ModelRetryPolicy(
                    max_attempts=2,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            http_client=http_client,
        )
        repair_tool = ModelTool(
            name="inspect_evidence",
            description="read only",
            input_schema={"type": "object"},
        )
        response = await client.complete(
            ModelRequest(
                role=AgentRole.EXPLANATION,
                system="Use only supplied evidence IDs.",
                messages=(ModelMessage(role="user", content='{"evidence_ids":["e1"]}'),),
                tools=(repair_tool,),
                response_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string", "const": "ok"}},
                },
            )
        )

    assert len(bodies) == 2
    assert bodies[0]["tools"] == bodies[1]["tools"]
    assert bodies[0]["max_tokens"] == 2048
    assert bodies[1]["max_tokens"] == 4096
    first_messages = bodies[0]["messages"]
    repaired_messages = bodies[1]["messages"]
    assert isinstance(first_messages, list) and isinstance(repaired_messages, list)
    assert first_messages[1] == repaired_messages[1]
    assert "Structured output repair attempt 1" in repaired_messages[0]["content"]
    assert "malformed-untrusted-output" not in json.dumps(bodies[1])
    assert response.structured_output == {"status": "ok"}
    assert response.metadata["attempt_count"] == 2
    assert response.metadata["structured_repair_count"] == 1


@pytest.mark.asyncio
async def test_structured_output_allows_only_one_correction_then_fails_closed() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"status":"still incomplete"'},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = build_model_client(
            ModelClientConfig(
                provider=ModelProviderName.OPENAI_COMPATIBLE,
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                retry=ModelRetryPolicy(
                    max_attempts=3,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            http_client=http_client,
        )
        with pytest.raises(
            StructuredOutputError,
            match="truncated at max_tokens",
        ) as captured:
            await client.complete(
                ModelRequest(
                    role=AgentRole.EXPLANATION,
                    system="Return one compact JSON object.",
                    messages=(ModelMessage(role="user", content="explain"),),
                    response_schema={
                        "type": "object",
                        "required": ["status"],
                        "additionalProperties": False,
                        "properties": {"status": {"type": "string"}},
                    },
                )
            )

    assert len(bodies) == 2
    assert bodies[0]["max_tokens"] == 2048
    assert bodies[1]["max_tokens"] == 4096
    assert "Structured output repair attempt 1" in bodies[1]["messages"][0]["content"]
    assert captured.value.attempt_count == 2


@pytest.mark.asyncio
async def test_provider_error_code_is_sanitized_without_body_or_secret_in_trace() -> None:
    sink = InMemoryModelTraceSink()
    secret_body_text = "sensitive-provider-message-and-secret"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid request / reasoning_content",
                    "type": "invalid_request_error",
                    "message": secret_body_text,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com",
            http_client=http_client,
            trace_sink=sink,
        )
        with pytest.raises(ModelHTTPError) as captured:
            await client.complete(
                ModelRequest(
                    role=AgentRole.RISK_CRITIC,
                    system="safe",
                    messages=(ModelMessage(role="user", content="check"),),
                )
            )

    assert captured.value.status_code == 400
    assert captured.value.provider_error_code == "invalid_request_reasoning_content"
    assert secret_body_text not in str(captured.value)
    assert len(sink.records) == 1
    trace = sink.records[0]
    assert trace.http_status_code == 400
    assert trace.provider_error_code == "invalid_request_reasoning_content"
    assert secret_body_text not in str(trace.model_dump(mode="json"))


class _FlakyClient:
    provider = "fixture"
    model = "fixture"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls < 3:
            raise ModelGatewayError("transient")
        return ModelResponse(text="ok", provider=self.provider, model=self.model)


@pytest.mark.asyncio
async def test_retry_wrapper_is_bounded_and_reports_attempt_count() -> None:
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    inner = _FlakyClient()
    client = RetryingModelClient(
        inner,
        ModelRetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=1),
        sleep=no_sleep,
    )
    response = await client.complete(
        ModelRequest(
            role=AgentRole.CONTEXT,
            system="test",
            messages=(ModelMessage(role="user", content="test"),),
        )
    )
    assert inner.calls == 3
    assert delays == [0.1, 0.2]
    assert response.metadata["attempt_count"] == 3


@pytest.mark.asyncio
async def test_retry_wrapper_reports_attempts_on_final_failure() -> None:
    class AlwaysFailingClient:
        provider = "fixture"
        model = "fixture"

        async def complete(self, _: ModelRequest) -> ModelResponse:
            raise ModelGatewayError("still unavailable")

    client = RetryingModelClient(
        AlwaysFailingClient(),
        ModelRetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
    )

    with pytest.raises(ModelGatewayError) as error:
        await client.complete(
            ModelRequest(
                role=AgentRole.CONTEXT,
                system="test",
                messages=(ModelMessage(role="user", content="test"),),
            )
        )

    assert error.value.attempt_count == 3


def test_provider_factory_builds_sdk_free_retrying_clients() -> None:
    anthropic = build_model_client(
        ModelClientConfig(
            provider=ModelProviderName.ANTHROPIC,
            api_key="test-key",
            model="claude-test",
            retry=ModelRetryPolicy(max_attempts=2),
        )
    )
    compatible = build_model_client(
        ModelClientConfig(
            provider=ModelProviderName.OPENAI_COMPATIBLE,
            model="local-model",
            base_url="http://127.0.0.1:9000/v1",
            retry=ModelRetryPolicy(max_attempts=1),
        )
    )
    assert anthropic.provider == "anthropic"
    assert compatible.provider == "openai_compatible"


@pytest.mark.asyncio
async def test_model_trace_scopes_isolate_concurrent_identical_requests() -> None:
    sink = InMemoryModelTraceSink(max_records=10)
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    request = ModelRequest(
        role=AgentRole.CONTEXT,
        system="bounded fixture",
        messages=(ModelMessage(role="user", content="same request"),),
    )
    request_sha256 = "a" * 64
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )

        async def invoke(scope_id: str) -> tuple[str, int]:
            with sink.trace_scope(request_sha256, scope_id=scope_id) as scope:
                await client.complete(request)
                summary = sink.scope_summary(scope)
                return summary.scope_id, summary.trace_count

        first = asyncio.create_task(invoke("live-job-first"))
        second = asyncio.create_task(invoke("live-job-second"))
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        summaries = await asyncio.gather(first, second)

    assert set(summaries) == {("live-job-first", 1), ("live-job-second", 1)}
    assert {item.scope_id for item in sink.records} == {
        "live-job-first",
        "live-job-second",
    }
    assert {item.scope_request_digest for item in sink.records} == {request_sha256}


@pytest.mark.asyncio
async def test_model_trace_scope_count_is_independent_of_bounded_record_deque() -> None:
    sink = InMemoryModelTraceSink(max_records=1)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )
        with sink.trace_scope("b" * 64, scope_id="live-job-bounded") as scope:
            await asyncio.gather(
                *(
                    client.complete(
                        ModelRequest(
                            role=AgentRole.CONTEXT,
                            system="bounded fixture",
                            messages=(ModelMessage(role="user", content=str(index)),),
                        )
                    )
                    for index in range(3)
                )
            )
            summary = sink.scope_summary(scope)

    assert len(sink.records) == 1
    assert summary.trace_count == 3
    assert summary.success_count == 3
    assert summary.failure_count == 0
    assert sink.records[0].scope_id == "live-job-bounded"


@pytest.mark.asyncio
async def test_model_trace_scope_reports_zero_and_failed_attempts_without_content() -> None:
    sink = InMemoryModelTraceSink()
    with sink.trace_scope("c" * 64, scope_id="live-job-zero") as zero_scope:
        zero = sink.scope_summary(zero_scope)

    secret_response = "provider-secret-response-body"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"type": "upstream_error", "message": secret_response}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAICompatibleChatClient(
            model="fixture-model",
            base_url="http://127.0.0.1:9000/v1",
            http_client=http_client,
            trace_sink=sink,
        )
        with sink.trace_scope("d" * 64, scope_id="live-job-failed") as failed_scope:
            with pytest.raises(ModelHTTPError):
                await client.complete(
                    ModelRequest(
                        role=AgentRole.CONTEXT,
                        system="secret-prompt-must-not-be-persisted",
                        messages=(ModelMessage(role="user", content="secret-user-text"),),
                    )
                )
            failed = sink.scope_summary(failed_scope)

    assert (zero.trace_count, zero.success_count, zero.failure_count) == (0, 0, 0)
    assert (failed.trace_count, failed.success_count, failed.failure_count) == (1, 0, 1)
    serialized = sink.records[-1].model_dump_json()
    assert "secret-prompt-must-not-be-persisted" not in serialized
    assert "secret-user-text" not in serialized
    assert secret_response not in serialized


@pytest.mark.asyncio
async def test_structured_output_locally_repairs_markdown_fenced_json_without_model_retry() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '```json\n{"status":"ok"}\n```',
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = build_model_client(
            ModelClientConfig(
                provider=ModelProviderName.OPENAI_COMPATIBLE,
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                retry=ModelRetryPolicy(
                    max_attempts=2,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            http_client=http_client,
        )
        response = await client.complete(
            ModelRequest(
                role=AgentRole.EXPLANATION,
                system="Return compact JSON.",
                messages=(ModelMessage(role="user", content="go"),),
                response_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string", "const": "ok"}},
                },
            )
        )

    # The fence is repaired locally; the model is never asked to retry.
    assert len(bodies) == 1
    assert "repair attempt" not in json.dumps(bodies[0])
    assert response.structured_output == {"status": "ok"}
    assert response.metadata["attempt_count"] == 1
    assert response.metadata["structured_repair_count"] == 0


@pytest.mark.asyncio
async def test_structured_output_locally_repairs_prose_wrapped_json_without_model_retry() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": 'Here is the result:\n{"status":"ok"}\nThat is all.',
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = build_model_client(
            ModelClientConfig(
                provider=ModelProviderName.OPENAI_COMPATIBLE,
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                retry=ModelRetryPolicy(
                    max_attempts=2,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            http_client=http_client,
        )
        response = await client.complete(
            ModelRequest(
                role=AgentRole.EXPLANATION,
                system="Return compact JSON.",
                messages=(ModelMessage(role="user", content="go"),),
                response_schema={
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string", "const": "ok"}},
                },
            )
        )

    assert len(bodies) == 1
    assert response.structured_output == {"status": "ok"}
    assert response.metadata["attempt_count"] == 1
    assert response.metadata["structured_repair_count"] == 0


@pytest.mark.asyncio
async def test_structured_output_archives_raw_text_when_local_repair_exhausted() -> None:
    malformed = 'prefix {"status": "truncated"'
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": malformed},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = build_model_client(
            ModelClientConfig(
                provider=ModelProviderName.OPENAI_COMPATIBLE,
                model="deepseek-v4-flash",
                base_url="https://api.deepseek.com",
                retry=ModelRetryPolicy(
                    max_attempts=2,
                    base_delay_seconds=0,
                    max_delay_seconds=0,
                ),
            ),
            http_client=http_client,
        )
        with pytest.raises(StructuredOutputError) as captured:
            await client.complete(
                ModelRequest(
                    role=AgentRole.EXPLANATION,
                    system="Return compact JSON.",
                    messages=(ModelMessage(role="user", content="go"),),
                    response_schema={
                        "type": "object",
                        "required": ["status"],
                        "properties": {"status": {"type": "string"}},
                    },
                )
            )

    # The model got one repair request, then failed closed with the raw text
    # archived on the exception so the evidence chain can seal the failure.
    assert len(bodies) == 2
    assert captured.value.raw_output == malformed
    assert captured.value.attempt_count == 2
