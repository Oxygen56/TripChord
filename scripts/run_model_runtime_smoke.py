from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit

import httpx
import tripchord.agents.model_gateway as model_gateway_module
from pydantic import JsonValue, TypeAdapter
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelClient,
    ModelClientConfig,
    ModelMessage,
    ModelPricing,
    ModelProviderName,
    ModelRequest,
    ModelResponse,
    ModelResponseFormatMode,
    ModelRetryPolicy,
    ModelTool,
    ModelToolResult,
    build_model_client,
    compact_json,
)
from tripchord.agents.models import AgentRole, ToolPermission
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec

SCHEMA_VERSION = "tripchord-model-runtime-smoke-v2"
MAX_LOGICAL_MODEL_CALLS = 3
CLAIM_BOUNDARY = (
    "This runner verifies one live structured-output request and one bounded two-call "
    "tool loop through the TripChord model gateway. It does not prove full OTA end-to-end "
    "quality, architecture superiority, production reliability, or provider availability."
)

STRUCTURED_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["status"],
    "additionalProperties": False,
    "properties": {"status": {"type": "string", "const": "ok"}},
}
TOOL_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}
TOOL_FINAL_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "required": ["selected_quote_id", "summary"],
    "additionalProperties": False,
    "properties": {
        "selected_quote_id": {"type": "string", "const": "q1"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 200},
    },
}


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _HashingModelClient:
    """Record hashes of exact logical requests/responses without retaining plaintext."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.request_sha256: list[str] = []
        self.response_sha256: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.request_sha256.append(_canonical_sha256(request.model_dump(mode="json")))
        response = await self._inner.complete(request)
        self.response_sha256.append(_canonical_sha256(response.model_dump(mode="json")))
        return response


def _usage(responses: Sequence[ModelResponse]) -> dict[str, int | float]:
    return {
        "input_tokens": sum(item.usage.input_tokens for item in responses),
        "output_tokens": sum(item.usage.output_tokens for item in responses),
        "total_tokens": sum(item.usage.total_tokens for item in responses),
        "estimated_cost_usd": sum(item.estimated_cost_usd for item in responses),
    }


def _trace_report(sink: InMemoryModelTraceSink) -> list[dict[str, JsonValue]]:
    return [
        TypeAdapter(dict[str, JsonValue]).validate_python(
            {
                "id": item.id,
                "provider": item.provider,
                "model": item.model,
                "role": item.role.value,
                "request_sha256": item.request_digest,
                "response_schema_requested": item.response_schema_requested,
                "tool_count": item.tool_count,
                "started_at": item.started_at.isoformat(),
                "finished_at": item.finished_at.isoformat(),
                "success": item.success,
                "usage": item.usage.model_dump(mode="json"),
                "estimated_cost_usd": item.estimated_cost_usd,
                "error_class": item.error_class,
            }
        )
        for item in sink.records
    ]


def _code_hashes() -> tuple[dict[str, str], str]:
    repository = Path(__file__).resolve().parents[1]
    gateway_path = Path(model_gateway_module.__file__).resolve()
    paths = (Path(__file__).resolve(), gateway_path)
    hashes = {
        (
            str(path.relative_to(repository))
            if path.is_relative_to(repository)
            else f"installed:{path.name}"
        ): _file_sha256(path)
        for path in paths
    }
    return hashes, _canonical_sha256(hashes)


async def run_smoke(
    client: ModelClient,
    *,
    trace_sink: InMemoryModelTraceSink,
    endpoint_host: str,
    key_environment_variable: str,
) -> dict[str, JsonValue]:
    audited = _HashingModelClient(client)
    started_at = datetime.now(UTC)
    wall_started = perf_counter()

    structured_started = perf_counter()
    structured_response = await audited.complete(
        ModelRequest(
            role=AgentRole.EVENT_DIAGNOSER,
            system="Return only the requested machine-readable health result.",
            messages=(ModelMessage(role="user", content="Report gateway health."),),
            response_schema=STRUCTURED_SCHEMA,
            max_tokens=64,
        )
    )
    structured_latency = perf_counter() - structured_started
    if structured_response.structured_output != {"status": "ok"}:
        raise RuntimeError("structured-output smoke did not return the required status")

    tool = ModelTool(
        name="inspect_quotes",
        description="Read a bounded fixture quote set before selecting an observed quote id.",
        input_schema=TOOL_INPUT_SCHEMA,
    )
    tool_request = ModelRequest(
        role=AgentRole.EVIDENCE_ARBITER,
        system=(
            "You are a bounded evidence arbiter. You must call inspect_quotes exactly once "
            "before selecting a quote. Do not invent quote ids."
        ),
        messages=(ModelMessage(role="user", content="Select one observed quote id."),),
        tools=(tool,),
        max_tokens=256,
    )
    tool_loop_started = perf_counter()
    tool_choice_response = await audited.complete(tool_request)
    if len(tool_choice_response.tool_calls) != 1:
        raise RuntimeError("bounded tool smoke requires exactly one tool call")
    selected_call = tool_choice_response.tool_calls[0]
    if selected_call.name != tool.name:
        raise RuntimeError("bounded tool smoke selected an undeclared tool")

    registry = ToolRegistry()

    async def inspect_quotes(_: ToolCall) -> dict[str, JsonValue]:
        return {
            "quotes": [
                {
                    "id": "q1",
                    "provider": "fixture-provider",
                    "total_minor_units": 12345,
                    "currency": "CNY",
                }
            ]
        }

    registry.register(
        ToolSpec(
            name=tool.name,
            description=tool.description,
            permission=ToolPermission.READ_ONLY_EXTERNAL,
            allowed_roles=(AgentRole.EVIDENCE_ARBITER,),
            input_schema=TOOL_INPUT_SCHEMA,
        ),
        inspect_quotes,
    )
    receipt = await registry.invoke(
        ToolCall(
            id=selected_call.id,
            tool_name=selected_call.name,
            task_id="model-runtime-smoke",
            agent_role=AgentRole.EVIDENCE_ARBITER,
            arguments=selected_call.arguments,
        )
    )
    # The final turn deliberately omits the tool declaration: the model has
    # already observed the receipt in the message history, so redeclaring the
    # tool only invites a second tool call instead of the structured verdict.
    # (Observed on deepseek-v4-flash, which re-called inspect_quotes instead of
    # answering.) The bounded client retry still covers malformed/wrong-value
    # completions without relaxing the fixed three-call contract.
    final_response = await audited.complete(
        ModelRequest(
            role=AgentRole.EVIDENCE_ARBITER,
            system=(
                "Use only the tool receipt. Return the selected observed quote id and a short "
                "summary as one JSON object."
            ),
            messages=(
                ModelMessage(
                    role="user",
                    content="Select one observed quote id.",
                ),
                ModelMessage(
                    role="assistant",
                    content=tool_choice_response.text,
                    reasoning_content=tool_choice_response.reasoning_content,
                    tool_calls=tool_choice_response.tool_calls,
                ),
                ModelMessage(
                    role="user",
                    tool_results=(
                        ModelToolResult(
                            tool_call_id=selected_call.id,
                            content=compact_json({"tool_result": receipt.model_dump(mode="json")}),
                        ),
                    ),
                ),
            ),
            response_schema=TOOL_FINAL_SCHEMA,
            max_tokens=256,
        )
    )
    tool_loop_latency = perf_counter() - tool_loop_started
    expected_tool_result = {"selected_quote_id": "q1"}
    final_output = final_response.structured_output
    if not isinstance(final_output, dict) or any(
        final_output.get(key) != value for key, value in expected_tool_result.items()
    ):
        raise RuntimeError("bounded tool smoke did not select the observed quote id")
    final_summary = str(final_output["summary"])
    if len(audited.request_sha256) != MAX_LOGICAL_MODEL_CALLS:
        raise RuntimeError("model smoke exceeded its fixed logical-call contract")

    responses = (structured_response, tool_choice_response, final_response)
    code_file_sha256, code_sha256 = _code_hashes()
    traces = _trace_report(trace_sink)
    request_hashes = audited.request_sha256
    response_hashes = audited.response_sha256
    trace_request_hashes = [str(item["request_sha256"]) for item in traces]
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "schema_version": SCHEMA_VERSION,
            "executed_at": started_at.isoformat(),
            "provider_adapter": audited.provider,
            "model": audited.model,
            "endpoint_host": endpoint_host,
            "credential_contract": {
                "api_key_source": f"environment:{key_environment_variable}",
                "api_key_persisted": False,
                "api_key_or_prompt_plaintext_in_report": False,
            },
            "safety_contract": {
                "paid_call_acknowledged": True,
                "max_logical_model_calls": MAX_LOGICAL_MODEL_CALLS,
                "observed_logical_model_calls": len(request_hashes),
                "prompts_persisted": False,
            },
            "structured_json": {
                "passed": True,
                "structured_output": structured_response.structured_output,
                "schema_sha256": _canonical_sha256(STRUCTURED_SCHEMA),
                "request_sha256": request_hashes[0],
                "response_sha256": response_hashes[0],
                "trace_id": structured_response.trace_id,
                "usage": _usage((structured_response,)),
                "latency_seconds": structured_latency,
            },
            "bounded_tool_loop": {
                "passed": True,
                "required_tool": tool.name,
                "observed_tool_names": [selected_call.name],
                "selected_quote_id": final_output["selected_quote_id"],
                "structured_output_redacted": {
                    "selected_quote_id": final_output["selected_quote_id"],
                    "summary_sha256": hashlib.sha256(final_summary.encode("utf-8")).hexdigest(),
                    "summary_length": len(final_summary),
                },
                "tool_input_schema_sha256": _canonical_sha256(TOOL_INPUT_SCHEMA),
                "final_schema_sha256": _canonical_sha256(TOOL_FINAL_SCHEMA),
                "request_sha256": request_hashes[1:],
                "response_sha256": response_hashes[1:],
                "trace_ids": [tool_choice_response.trace_id, final_response.trace_id],
                "usage": _usage((tool_choice_response, final_response)),
                "latency_seconds": tool_loop_latency,
            },
            "request_trace_hashes_match": request_hashes == trace_request_hashes,
            "traces": traces,
            "aggregate": {
                "usage": _usage(responses),
                "wall_latency_seconds": perf_counter() - wall_started,
                "logical_model_calls": len(responses),
            },
            "code_file_sha256": code_file_sha256,
            "code_sha256": code_sha256,
            "claim_boundary": CLAIM_BOUNDARY,
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the bounded, privacy-preserving TripChord live-model smoke.",
    )
    parser.add_argument("--ack-live-cost", action="store_true")
    parser.add_argument("--provider", choices=tuple(item.value for item in ModelProviderName))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", default="MODEL_API_KEY")
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--input-usd-per-million", type=float, default=0)
    parser.add_argument("--output-usd-per-million", type=float, default=0)
    parser.add_argument("--response-format-mode", default="auto")
    parser.add_argument("--output", type=Path)
    return parser


def validate_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.ack_live_cost:
        parser.error("live model smoke requires --ack-live-cost")
    if not args.provider or not args.model or not args.base_url:
        parser.error("live model smoke requires --provider, --model and --base-url")
    if args.output is None:
        parser.error("live model smoke requires --output")
    if not 0 < args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 0 and 300")
    if args.input_usd_per_million < 0 or args.output_usd_per_million < 0:
        parser.error("model price fields must be non-negative")
    try:
        ModelResponseFormatMode(args.response_format_mode)
    except ValueError:
        parser.error("unsupported --response-format-mode")


async def execute_from_arguments(
    args: argparse.Namespace,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, JsonValue]:
    if not args.ack_live_cost:
        raise RuntimeError("live model smoke requires explicit cost acknowledgement")
    api_key = os.getenv(args.api_key_env)
    if not api_key:
        raise RuntimeError(f"missing API key in environment variable {args.api_key_env}")
    trace_sink = InMemoryModelTraceSink(max_records=MAX_LOGICAL_MODEL_CALLS)
    client = build_model_client(
        ModelClientConfig(
            provider=ModelProviderName(args.provider),
            model=args.model,
            api_key=api_key,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            # Retry transient model deviations (a schema-invalid completion is
            # repaired via one explicit bounded re-request). The strict logical
            # call contract below still counts one logical call per stage, so a
            # repair does not relax the fixed three-call bound.
            retry=ModelRetryPolicy(max_attempts=3),
            pricing=ModelPricing(
                input_usd_per_million_tokens=args.input_usd_per_million,
                output_usd_per_million_tokens=args.output_usd_per_million,
            ),
            response_format_mode=ModelResponseFormatMode(args.response_format_mode),
        ),
        http_client=http_client,
        trace_sink=trace_sink,
    )
    endpoint_host = urlsplit(args.base_url).hostname or "invalid"
    return await run_smoke(
        client,
        trace_sink=trace_sink,
        endpoint_host=endpoint_host,
        key_environment_variable=args.api_key_env,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_arguments(parser, args)
    report = asyncio.run(execute_from_arguments(args))
    output = TypeAdapter(Path).validate_python(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(output),
                "provider": report["provider_adapter"],
                "model": report["model"],
                "code_sha256": report["code_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
