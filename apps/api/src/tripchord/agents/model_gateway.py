from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict, deque
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

import httpx
from pydantic import Field, JsonValue, TypeAdapter, model_validator

from tripchord.agents.models import AgentRole
from tripchord.domain.common import DomainModel


class ModelTool(DomainModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    input_schema: dict[str, JsonValue]


class ModelToolCall(DomainModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ModelToolResult(DomainModel):
    tool_call_id: str = Field(min_length=1)
    content: str
    is_error: bool = False


class ModelMessage(DomainModel):
    role: str
    content: str = ""
    # DeepSeek V4 thinking-mode tool turns must replay this field verbatim.
    # It is ephemeral provider state, never copied into Agent outputs or traces.
    reasoning_content: str | None = Field(default=None, repr=False)
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_results: tuple[ModelToolResult, ...] = ()


class ModelUsage(DomainModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelPricing(DomainModel):
    """Optional price card used only for transparent cost estimates."""

    input_usd_per_million_tokens: float = Field(default=0, ge=0)
    output_usd_per_million_tokens: float = Field(default=0, ge=0)

    def estimate(self, usage: ModelUsage) -> float:
        return (
            usage.input_tokens * self.input_usd_per_million_tokens
            + usage.output_tokens * self.output_usd_per_million_tokens
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelTraceScope:
    """One execution instance bound to a canonical outer API request."""

    id: str
    request_digest: str


_CURRENT_MODEL_TRACE_SCOPE: ContextVar[ModelTraceScope | None] = ContextVar(
    "tripchord_model_trace_scope",
    default=None,
)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


@contextmanager
def _bind_model_trace_scope(scope: ModelTraceScope) -> Iterator[None]:
    token: Token[ModelTraceScope | None] = _CURRENT_MODEL_TRACE_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_MODEL_TRACE_SCOPE.reset(token)


class ModelCallTrace(DomainModel):
    """Privacy-preserving model trace: hashes prompts instead of storing them."""

    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    role: AgentRole
    request_digest: str = Field(min_length=64, max_length=64)
    scope_id: str | None = Field(default=None, min_length=1, max_length=120)
    scope_request_digest: str | None = Field(
        default=None,
        pattern="^[0-9a-f]{64}$",
    )
    response_schema_requested: bool
    tool_count: int = Field(ge=0)
    started_at: datetime
    finished_at: datetime
    success: bool
    usage: ModelUsage = Field(default_factory=ModelUsage)
    estimated_cost_usd: float = Field(default=0, ge=0)
    error_class: str | None = None
    error_message: str | None = None
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    provider_error_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_scope_binding(self) -> ModelCallTrace:
        if (self.scope_id is None) != (self.scope_request_digest is None):
            raise ValueError("model trace scope id and request digest must be set together")
        return self


class ModelTraceScopeSummary(DomainModel):
    """Bounded, content-free aggregate for one execution scope."""

    scope_id: str = Field(min_length=1, max_length=120)
    scope_request_digest: str = Field(pattern="^[0-9a-f]{64}$")
    trace_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> ModelTraceScopeSummary:
        if self.trace_count != self.success_count + self.failure_count:
            raise ValueError("model trace scope counts must add up")
        return self


class ModelTraceSink(Protocol):
    def record(self, trace: ModelCallTrace) -> None: ...


class InMemoryModelTraceSink:
    """Bounded-process trace sink. Production integrations may persist externally."""

    def __init__(
        self,
        *,
        max_records: int = 2_000,
        max_scope_summaries: int = 2_000,
    ) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if max_scope_summaries < 1:
            raise ValueError("max_scope_summaries must be positive")
        self._records: deque[ModelCallTrace] = deque(maxlen=max_records)
        self._max_scope_summaries = max_scope_summaries
        self._scope_summaries: dict[str, ModelTraceScopeSummary] = {}
        self._active_scope_ids: set[str] = set()
        self._lock = RLock()

    @contextmanager
    def trace_scope(
        self,
        request_digest: str,
        *,
        scope_id: str | None = None,
    ) -> Iterator[ModelTraceScope]:
        """Bind traces to one request without relying on global count deltas."""

        if not _valid_sha256(request_digest):
            raise ValueError("request_digest must be a lowercase SHA-256 hex digest")
        resolved_scope_id = scope_id or f"model-scope-{uuid4().hex}"
        if not resolved_scope_id.strip() or len(resolved_scope_id) > 120:
            raise ValueError("scope_id must contain 1 to 120 characters")
        scope = ModelTraceScope(
            id=resolved_scope_id,
            request_digest=request_digest,
        )
        with self._lock:
            if scope.id in self._scope_summaries:
                raise ValueError("model trace scope_id was already used")
            self._prune_scope_summaries_locked(reserve=1)
            self._scope_summaries[scope.id] = ModelTraceScopeSummary(
                scope_id=scope.id,
                scope_request_digest=scope.request_digest,
            )
            self._active_scope_ids.add(scope.id)
        try:
            with _bind_model_trace_scope(scope):
                yield scope
        finally:
            with self._lock:
                self._active_scope_ids.discard(scope.id)
                self._prune_scope_summaries_locked()

    def record(self, trace: ModelCallTrace) -> None:
        with self._lock:
            self._records.append(trace)
            if trace.scope_id is None or trace.scope_request_digest is None:
                return
            summary = self._scope_summaries.get(trace.scope_id)
            if summary is None:
                self._prune_scope_summaries_locked(reserve=1)
                summary = ModelTraceScopeSummary(
                    scope_id=trace.scope_id,
                    scope_request_digest=trace.scope_request_digest,
                )
            elif summary.scope_request_digest != trace.scope_request_digest:
                raise ValueError("model trace scope request digest changed within one scope")
            self._scope_summaries[trace.scope_id] = summary.model_copy(
                update={
                    "trace_count": summary.trace_count + 1,
                    "success_count": summary.success_count + int(trace.success),
                    "failure_count": summary.failure_count + int(not trace.success),
                }
            )

    @property
    def records(self) -> tuple[ModelCallTrace, ...]:
        with self._lock:
            return tuple(self._records)

    def scope_summary(self, scope: ModelTraceScope | str) -> ModelTraceScopeSummary:
        scope_id = scope.id if isinstance(scope, ModelTraceScope) else scope
        with self._lock:
            summary = self._scope_summaries.get(scope_id)
            if summary is None:
                raise LookupError(f"unknown model trace scope: {scope_id}")
            return summary

    def _prune_scope_summaries_locked(self, *, reserve: int = 0) -> None:
        target = self._max_scope_summaries - reserve
        while len(self._scope_summaries) > target:
            removable = next(
                (
                    scope_id
                    for scope_id in self._scope_summaries
                    if scope_id not in self._active_scope_ids
                ),
                None,
            )
            if removable is None:
                return
            self._scope_summaries.pop(removable, None)


class ModelRequest(DomainModel):
    role: AgentRole
    system: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelTool, ...] = ()
    response_schema: dict[str, JsonValue] | None = None
    temperature: float = Field(default=0, ge=0, le=1)
    max_tokens: int = Field(default=2048, ge=1, le=16384)
    risk_level: int = Field(default=0, ge=0, le=4)


class ModelResponse(DomainModel):
    text: str = ""
    # Keep provider reasoning only long enough to replay a thinking-mode tool
    # turn. Excluding it from serialization prevents accidental CoT persistence.
    reasoning_content: str | None = Field(default=None, exclude=True, repr=False)
    tool_calls: tuple[ModelToolCall, ...] = ()
    stop_reason: str | None = None
    provider: str
    model: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    latency_seconds: float = Field(default=0, ge=0)
    raw_id: str | None = None
    structured_output: JsonValue | None = None
    estimated_cost_usd: float = Field(default=0, ge=0)
    trace_id: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ModelClient(Protocol):
    provider: str
    model: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class ModelHTTPPostClient(Protocol):
    """Narrow transport contract used by the SDK-free model adapters."""

    async def post(
        self,
        url: str | httpx.URL,
        *,
        headers: Any = None,
        json: Any = None,
    ) -> httpx.Response: ...


class ModelGatewayError(RuntimeError):
    # RetryingModelClient overwrites this on the final raised instance.  The
    # field lets the router distinguish one logical Agent request from the
    # actual number of paid/provider HTTP attempts.
    attempt_count: int = 1
    # The router attaches the split counts before re-raising a routed failure.
    # They are annotations rather than class defaults so callers can distinguish
    # an un-routed client error and fall back to ``attempt_count``.
    primary_attempt_count: int
    fallback_attempt_count: int


class ModelHTTPError(ModelGatewayError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        retryable: bool,
        provider_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider_error_code = provider_error_code


class StructuredOutputError(ModelGatewayError):
    """A completion failed local JSON parse or schema validation.

    ``raw_output`` archives the exact untrusted model text that failed, so a
    caller (or the sealed exploration evidence) can audit the failure without
    replaying it into the next model request.  It is intentionally not part of
    the message: the message stays stable for programmatic callers while the
    archived text is available to the evidence chain.
    """

    def __init__(self, message: str, *, raw_output: str | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class ModelResponseFormatMode(StrEnum):
    AUTO = "auto"
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    PROMPT_ONLY = "prompt_only"


class ModelThinkingMode(StrEnum):
    AUTO = "auto"
    DISABLED = "disabled"
    ENABLED = "enabled"


def _safe_provider_error_code(response: httpx.Response) -> str | None:
    """Extract only a bounded provider code/type, never an error body/message."""

    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("code", "type"):
        raw = error.get(key)
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            continue
        sanitized = re.sub(r"[^A-Za-z0-9._:-]+", "_", str(raw)).strip("_")
        if sanitized:
            return sanitized[:120]
    return None


def _http_error_message(provider_label: str, status_code: int, code: str | None) -> str:
    message = f"{provider_label} request failed with HTTP {status_code}"
    if code is not None:
        message = f"{message} [provider_error_code={code}]"
    return message


def _request_digest(request: ModelRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _resolve_schema_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise StructuredOutputError(f"unsupported external schema reference: {ref}")
    value: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or key not in value:
            raise StructuredOutputError(f"unresolved schema reference: {ref}")
        value = value[key]
    if not isinstance(value, Mapping):
        raise StructuredOutputError(f"schema reference is not an object: {ref}")
    return value


def _matches_json_type(value: Any, expected: str) -> bool:
    return {
        "null": value is None,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected, True)


def _validate_json_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    root: Mapping[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate the JSON-Schema subset emitted by Pydantic model schemas.

    Native structured-output support remains enabled for OpenAI-compatible
    providers. This validator is the provider-independent fail-closed boundary.
    """

    root_schema = root or schema
    if ref := schema.get("$ref"):
        if not isinstance(ref, str):
            raise StructuredOutputError(f"{path}: invalid schema reference")
        _validate_json_schema(
            value,
            _resolve_schema_ref(root_schema, ref),
            root=root_schema,
            path=path,
        )
        return
    for keyword in ("anyOf", "oneOf"):
        choices = schema.get(keyword)
        if isinstance(choices, list):
            matched = 0
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                try:
                    _validate_json_schema(value, choice, root=root_schema, path=path)
                except StructuredOutputError:
                    continue
                matched += 1
            if matched == 0 or (keyword == "oneOf" and matched != 1):
                raise StructuredOutputError(f"{path}: value does not satisfy {keyword}")
            return
    if "const" in schema and value != schema["const"]:
        raise StructuredOutputError(f"{path}: value does not match const")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise StructuredOutputError(f"{path}: value is outside enum")
    expected = schema.get("type")
    expected_types = [expected] if isinstance(expected, str) else expected
    if isinstance(expected_types, list) and not any(
        isinstance(item, str) and _matches_json_type(value, item) for item in expected_types
    ):
        raise StructuredOutputError(f"{path}: unexpected JSON type")
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [item for item in required if isinstance(item, str) and item not in value]
            if missing:
                raise StructuredOutputError(f"{path}: missing required fields {missing}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, Mapping):
                    _validate_json_schema(
                        child,
                        child_schema,
                        root=root_schema,
                        path=f"{path}.{key}",
                    )
                elif schema.get("additionalProperties") is False:
                    raise StructuredOutputError(f"{path}: unexpected field {key}")
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            _validate_json_schema(item, item_schema, root=root_schema, path=f"{path}[{index}]")
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise StructuredOutputError(f"{path}: string shorter than minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise StructuredOutputError(f"{path}: string longer than maxLength")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise StructuredOutputError(f"{path}: number below minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise StructuredOutputError(f"{path}: number above maximum")


def _structured_output(text: str, schema: Mapping[str, Any] | None) -> JsonValue | None:
    if schema is None:
        return None
    try:
        value: Any = _parse_structured_json(text)
    except StructuredOutputError:
        raise
    _validate_json_schema(value, schema)
    return TypeAdapter(JsonValue).validate_python(value)


def _parse_structured_json(text: str) -> JsonValue:
    """Parse model output as JSON with bounded deterministic local repairs.

    The model is told to return one compact JSON object, but real completions
    can arrive wrapped in Markdown fences, carry a prose prefix/suffix, or
    contain a trailing comma.  A bounded set of local syntactic repairs runs
    BEFORE the single model-side correction request, so a recoverable
    malformation is repaired without spending the model's correction budget,
    and a hard failure archives the raw text on the raised error for sealed
    audit evidence.  No repair adds facts, tools, permissions, or authority;
    each candidate is derived only from the supplied text.
    """

    candidates: list[str] = [text]
    stripped = text.strip()
    if stripped != text:
        candidates.append(stripped)
    # Markdown code fences: ```json {…} ``` / ``` {…} ``` — take the first
    # fenced body and stop at the closing fence.
    for fence in ("```json", "```JSON", "```"):
        if fence in text:
            _, _, after = text.partition(fence)
            if "```" in after:
                body = after.split("```", 1)[0].strip()
                if body:
                    candidates.append(body)
    # Object/array extraction: the first opener to the last matching closer,
    # which drops a prose prefix and any trailing text after the JSON value.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise StructuredOutputError(
        "model did not return valid JSON",
        raw_output=text,
    ) from last_error


def _trace_success(
    *,
    sink: ModelTraceSink | None,
    pricing: ModelPricing,
    request: ModelRequest,
    provider: str,
    model: str,
    started_at: datetime,
    usage: ModelUsage,
) -> tuple[str, float]:
    trace_id = f"model-trace-{uuid4().hex}"
    estimate = pricing.estimate(usage)
    scope = _CURRENT_MODEL_TRACE_SCOPE.get()
    if sink is not None:
        sink.record(
            ModelCallTrace(
                id=trace_id,
                provider=provider,
                model=model,
                role=request.role,
                request_digest=_request_digest(request),
                scope_id=scope.id if scope is not None else None,
                scope_request_digest=(
                    scope.request_digest if scope is not None else None
                ),
                response_schema_requested=request.response_schema is not None,
                tool_count=len(request.tools),
                started_at=started_at,
                finished_at=datetime.now(UTC),
                success=True,
                usage=usage,
                estimated_cost_usd=estimate,
            )
        )
    return trace_id, estimate


def _trace_failure(
    *,
    sink: ModelTraceSink | None,
    request: ModelRequest,
    provider: str,
    model: str,
    started_at: datetime,
    error: Exception,
) -> None:
    if sink is None:
        return
    scope = _CURRENT_MODEL_TRACE_SCOPE.get()
    sink.record(
        ModelCallTrace(
            id=f"model-trace-{uuid4().hex}",
            provider=provider,
            model=model,
            role=request.role,
            request_digest=_request_digest(request),
            scope_id=scope.id if scope is not None else None,
            scope_request_digest=scope.request_digest if scope is not None else None,
            response_schema_requested=request.response_schema is not None,
            tool_count=len(request.tools),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            success=False,
            error_class=type(error).__name__,
            error_message=str(error)[:500],
            http_status_code=getattr(error, "status_code", None),
            provider_error_code=getattr(error, "provider_error_code", None),
        )
    )


class ScriptedModelClient:
    """Deterministic model double used for trace replay and offline evaluation."""

    provider = "scripted"

    def __init__(
        self,
        responses: Sequence[ModelResponse],
        *,
        model: str = "scripted-v1",
        delay_seconds: float = 0,
    ) -> None:
        self.model = model
        self._responses = deque(responses)
        self._delay_seconds = delay_seconds
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        if not self._responses:
            raise ModelGatewayError("scripted model has no remaining response")
        response = self._responses.popleft()
        return response.model_copy(update={"provider": self.provider, "model": self.model})


class AnthropicMessagesClient:
    """Thin async adapter for Anthropic Messages API with client-side tools."""

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 45,
        http_client: ModelHTTPPostClient | None = None,
        pricing: ModelPricing | None = None,
        trace_sink: ModelTraceSink | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = http_client
        self._pricing = pricing or ModelPricing()
        self._trace_sink = trace_sink

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started_at = datetime.now(UTC)
        system = request.system
        if request.response_schema is not None:
            system = (
                f"{system}\nReturn only one JSON value matching this JSON Schema exactly: "
                f"{json.dumps(request.response_schema, ensure_ascii=False, separators=(',', ':'))}"
            )
        body: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": [self._anthropic_message(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            body["tools"] = [tool.model_dump() for tool in request.tools]
        started = perf_counter()
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        owns_client = self._client is None
        try:
            response = await client.post(
                f"{self._base_url}/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            provider_error_code = _safe_provider_error_code(exc.response)
            error = ModelHTTPError(
                _http_error_message("Anthropic", status_code, provider_error_code),
                status_code=status_code,
                retryable=status_code in {408, 409, 425, 429} or status_code >= 500,
                provider_error_code=provider_error_code,
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=error,
            )
            raise error from exc
        except (httpx.RequestError, ValueError) as exc:
            error = ModelHTTPError(
                f"Anthropic request failed: {exc}",
                status_code=None,
                retryable=True,
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=error,
            )
            raise error from exc
        finally:
            if owns_client:
                assert isinstance(client, httpx.AsyncClient)
                await client.aclose()
        text_parts: list[str] = []
        calls: list[ModelToolCall] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                arguments = TypeAdapter(dict[str, JsonValue]).validate_python(
                    block.get("input", {})
                )
                calls.append(
                    ModelToolCall(
                        id=str(block["id"]),
                        name=str(block["name"]),
                        arguments=arguments,
                    )
                )
        usage = payload.get("usage", {})
        text = "\n".join(text_parts)
        model_usage = ModelUsage(
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
        try:
            structured = _structured_output(text, request.response_schema) if not calls else None
        except StructuredOutputError as exc:
            stop_reason = payload.get("stop_reason")
            reported_error = (
                StructuredOutputError("model JSON was truncated at max_tokens")
                if stop_reason == "max_tokens"
                else exc
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=reported_error,
            )
            if reported_error is exc:
                raise
            raise reported_error from exc
        trace_id, estimate = _trace_success(
            sink=self._trace_sink,
            pricing=self._pricing,
            request=request,
            provider=self.provider,
            model=self.model,
            started_at=started_at,
            usage=model_usage,
        )
        return ModelResponse(
            text=text,
            tool_calls=tuple(calls),
            stop_reason=payload.get("stop_reason"),
            provider=self.provider,
            model=self.model,
            usage=model_usage,
            latency_seconds=perf_counter() - started,
            raw_id=payload.get("id"),
            structured_output=structured,
            estimated_cost_usd=estimate,
            trace_id=trace_id,
        )

    def _anthropic_message(self, message: ModelMessage) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in message.tool_calls
        )
        blocks.extend(
            {
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.content,
                "is_error": result.is_error,
            }
            for result in message.tool_results
        )
        return {"role": message.role, "content": blocks}


class OpenAICompatibleChatClient:
    """SDK-free adapter for OpenAI-compatible ``/chat/completions`` APIs."""

    provider = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 45,
        http_client: ModelHTTPPostClient | None = None,
        pricing: ModelPricing | None = None,
        trace_sink: ModelTraceSink | None = None,
        response_format_mode: ModelResponseFormatMode = ModelResponseFormatMode.AUTO,
        thinking_mode: ModelThinkingMode = ModelThinkingMode.AUTO,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = http_client
        self._pricing = pricing or ModelPricing()
        self._trace_sink = trace_sink
        self._response_format_mode = response_format_mode
        self._thinking_mode = thinking_mode

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started_at = datetime.now(UTC)
        response_format_mode = self._resolved_response_format_mode()
        effective_request = request
        if (
            request.response_schema is not None
            and response_format_mode != ModelResponseFormatMode.JSON_SCHEMA
        ):
            schema_text = json.dumps(
                request.response_schema,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            effective_request = request.model_copy(
                update={
                    "system": (
                        f"{request.system}\nReturn only one JSON object matching this JSON "
                        "Schema exactly: "
                        f"{schema_text}"
                    )
                }
            )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(effective_request),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        resolved_thinking_mode = self._resolved_thinking_mode()
        if resolved_thinking_mode is not None:
            body["thinking"] = {"type": resolved_thinking_mode.value}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        if request.response_schema is not None:
            if response_format_mode == ModelResponseFormatMode.JSON_SCHEMA:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "tripchord_response",
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
            elif response_format_mode == ModelResponseFormatMode.JSON_OBJECT:
                body["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        started = perf_counter()
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        owns_client = self._client is None
        try:
            response = await client.post(self._endpoint(), headers=headers, json=body)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            provider_error_code = _safe_provider_error_code(exc.response)
            error = ModelHTTPError(
                _http_error_message(
                    "OpenAI-compatible",
                    status_code,
                    provider_error_code,
                ),
                status_code=status_code,
                retryable=status_code in {408, 409, 425, 429} or status_code >= 500,
                provider_error_code=provider_error_code,
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=error,
            )
            raise error from exc
        except (httpx.RequestError, ValueError, KeyError, IndexError) as exc:
            error = ModelHTTPError(
                f"OpenAI-compatible request failed: {exc}",
                status_code=None,
                retryable=not isinstance(exc, (KeyError, IndexError)),
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=error,
            )
            raise error from exc
        finally:
            if owns_client:
                assert isinstance(client, httpx.AsyncClient)
                await client.aclose()
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            payload_error = ModelGatewayError("OpenAI-compatible response has no assistant choice")
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=payload_error,
            )
            raise payload_error from exc
        text = str(message.get("content") or "")
        raw_reasoning = message.get("reasoning_content")
        reasoning_content = (
            raw_reasoning if isinstance(raw_reasoning, str) else None
        )
        calls: list[ModelToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
                validated = TypeAdapter(dict[str, JsonValue]).validate_python(arguments)
                calls.append(
                    ModelToolCall(
                        id=str(raw_call["id"]),
                        name=str(function["name"]),
                        arguments=validated,
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                raise StructuredOutputError("model returned an invalid tool call") from exc
        raw_usage = payload.get("usage") or {}
        usage = ModelUsage(
            input_tokens=int(raw_usage.get("prompt_tokens", 0)),
            output_tokens=int(raw_usage.get("completion_tokens", 0)),
        )
        try:
            structured = _structured_output(text, request.response_schema) if not calls else None
        except StructuredOutputError as exc:
            stop_reason = choice.get("finish_reason")
            reported_error = (
                StructuredOutputError("model JSON was truncated at max_tokens")
                if stop_reason == "length"
                else exc
            )
            _trace_failure(
                sink=self._trace_sink,
                request=request,
                provider=self.provider,
                model=self.model,
                started_at=started_at,
                error=reported_error,
            )
            if reported_error is exc:
                raise
            raise reported_error from exc
        trace_id, estimate = _trace_success(
            sink=self._trace_sink,
            pricing=self._pricing,
            request=request,
            provider=self.provider,
            model=self.model,
            started_at=started_at,
            usage=usage,
        )
        return ModelResponse(
            text=text,
            reasoning_content=reasoning_content,
            tool_calls=tuple(calls),
            stop_reason=choice.get("finish_reason"),
            provider=self.provider,
            model=self.model,
            usage=usage,
            latency_seconds=perf_counter() - started,
            raw_id=payload.get("id"),
            structured_output=structured,
            estimated_cost_usd=estimate,
            trace_id=trace_id,
        )

    def _endpoint(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    def _resolved_response_format_mode(self) -> ModelResponseFormatMode:
        if self._response_format_mode != ModelResponseFormatMode.AUTO:
            return self._response_format_mode
        return (
            ModelResponseFormatMode.JSON_OBJECT
            if self._uses_deepseek_contract()
            else ModelResponseFormatMode.JSON_SCHEMA
        )

    def _resolved_thinking_mode(self) -> ModelThinkingMode | None:
        if self._thinking_mode != ModelThinkingMode.AUTO:
            return self._thinking_mode
        # DeepSeek V4 defaults thinking on. TripChord's safe default explicitly
        # disables it so tool loops cannot depend on hidden provider state. Users
        # may opt in; enabled mode is fully replayed by ``_messages`` below.
        if self._uses_deepseek_contract():
            return ModelThinkingMode.DISABLED
        return None

    def _uses_deepseek_contract(self) -> bool:
        normalized_model = self.model.casefold()
        return "deepseek.com" in self._base_url.casefold() or normalized_model.startswith(
            ("deepseek-v4", "deepseek-chat", "deepseek-reasoner")
        )

    def _messages(self, request: ModelRequest) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for message in request.messages:
            if message.tool_results:
                messages.extend(
                    {
                        "role": "tool",
                        "tool_call_id": result.tool_call_id,
                        "content": result.content,
                    }
                    for result in message.tool_results
                )
                continue
            payload: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.reasoning_content is not None:
                payload["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(payload)
        return messages


class ModelProviderName(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"


class ModelRetryPolicy(DomainModel):
    max_attempts: int = Field(default=3, ge=1, le=8)
    base_delay_seconds: float = Field(default=0.25, ge=0, le=30)
    max_delay_seconds: float = Field(default=4, ge=0, le=60)


class ModelClientConfig(DomainModel):
    provider: ModelProviderName
    model: str = Field(min_length=1)
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=45, gt=0, le=300)
    retry: ModelRetryPolicy = Field(default_factory=ModelRetryPolicy)
    pricing: ModelPricing = Field(default_factory=ModelPricing)
    response_format_mode: ModelResponseFormatMode = ModelResponseFormatMode.AUTO
    thinking_mode: ModelThinkingMode = ModelThinkingMode.AUTO


def _structured_repair_request(
    request: ModelRequest,
    *,
    repair_attempt: int,
) -> ModelRequest:
    """Retry a schema failure without replaying untrusted malformed output.

    The original messages, tools, permissions, IDs, and response schema remain
    byte-for-byte represented by the same typed request. Only a bounded reminder
    is appended to the trusted system instruction. This lets the model repair
    syntax/shape without gaining new facts or authority.
    """

    # The round-3 DeepSeek evidence showed a repeatable failure mode where a
    # syntactically incomplete explanation was retried with the exact same
    # 2k output ceiling.  That only repeats a likely truncation.  A structured
    # correction gets one larger, still-bounded output window; it does not get
    # more context, tools, facts, IDs, or authority.
    repair_max_tokens = min(16_384, max(4_096, request.max_tokens * 2))
    return request.model_copy(
        update={
            "system": (
                f"{request.system}\n"
                f"Structured output repair attempt {repair_attempt}: the previous "
                "response failed local JSON/schema validation. Return one compact, "
                "complete JSON object matching the already supplied schema. Do not use "
                "Markdown fences or any text before or after the object. Close every "
                "array and object before the output limit. Reuse only IDs and facts "
                "present in the original request; do not add tools, facts, permissions, "
                "or actions."
            ),
            "max_tokens": repair_max_tokens,
        }
    )


class RetryingModelClient:
    """Bounded exponential retry wrapper; non-retryable HTTP errors fail fast."""

    def __init__(
        self,
        inner: ModelClient,
        policy: ModelRetryPolicy,
        *,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._sleep = sleep
        self.provider = inner.provider
        self.model = inner.model

    async def complete(self, request: ModelRequest) -> ModelResponse:
        last_error: ModelGatewayError | None = None
        current_request = request
        structured_repair_count = 0
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                response = await self._inner.complete(current_request)
                metadata = dict(response.metadata)
                metadata["attempt_count"] = attempt
                metadata["structured_repair_count"] = structured_repair_count
                return response.model_copy(update={"metadata": metadata})
            except ModelGatewayError as exc:
                last_error = exc
                exc.attempt_count = attempt
                retryable = getattr(exc, "retryable", True)
                if not retryable or attempt >= self._policy.max_attempts:
                    raise
                if isinstance(exc, StructuredOutputError):
                    # A malformed/schema-invalid completion is not a transient
                    # transport failure.  Permit exactly one explicit correction
                    # request; a second invalid completion remains a hard failure.
                    if structured_repair_count >= 1:
                        raise
                    structured_repair_count += 1
                    current_request = _structured_repair_request(
                        request,
                        repair_attempt=structured_repair_count,
                    )
                delay = min(
                    self._policy.max_delay_seconds,
                    self._policy.base_delay_seconds * (2 ** (attempt - 1)),
                )
                await self._sleep(delay)
        assert last_error is not None
        raise last_error


def build_model_client(
    config: ModelClientConfig,
    *,
    http_client: ModelHTTPPostClient | None = None,
    trace_sink: ModelTraceSink | None = None,
) -> ModelClient:
    """Build a provider client without importing a provider-specific SDK."""

    if config.provider == ModelProviderName.ANTHROPIC:
        if not config.api_key:
            raise ValueError("Anthropic requires an API key")
        inner: ModelClient = AnthropicMessagesClient(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url or "https://api.anthropic.com",
            timeout_seconds=config.timeout_seconds,
            http_client=http_client,
            pricing=config.pricing,
            trace_sink=trace_sink,
        )
    else:
        inner = OpenAICompatibleChatClient(
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url or "https://api.openai.com/v1",
            timeout_seconds=config.timeout_seconds,
            http_client=http_client,
            pricing=config.pricing,
            trace_sink=trace_sink,
            response_format_mode=config.response_format_mode,
            thinking_mode=config.thinking_mode,
        )
    if config.retry.max_attempts == 1:
        return inner
    return RetryingModelClient(inner, config.retry)


class ModelRoute(DomainModel):
    provider: str
    model: str
    reason: str
    fallback_used: bool = False
    primary_attempt_count: int = Field(default=1, ge=1)
    fallback_attempt_count: int = Field(default=0, ge=0)

    @property
    def http_attempt_count(self) -> int:
        return self.primary_attempt_count + self.fallback_attempt_count


class RoutedModelResponse(DomainModel):
    response: ModelResponse
    route: ModelRoute


class ModelRouter:
    """Routes by role/risk and records explicit fallbacks instead of hiding them."""

    def __init__(
        self,
        primary_by_role: Mapping[AgentRole, ModelClient],
        *,
        high_risk_client: ModelClient,
        fallback_client: ModelClient | None = None,
    ) -> None:
        self._primary_by_role = dict(primary_by_role)
        self._high_risk_client = high_risk_client
        self._fallback_client = fallback_client
        self.route_counts: dict[str, int] = defaultdict(int)

    async def complete(self, request: ModelRequest) -> RoutedModelResponse:
        client = (
            self._high_risk_client
            if request.risk_level >= 2
            or request.role in {AgentRole.ORCHESTRATOR, AgentRole.EVIDENCE_ARBITER}
            else self._primary_by_role.get(request.role, self._high_risk_client)
        )
        reason = "high_risk_or_control" if client is self._high_risk_client else "role_specialist"
        try:
            response = await client.complete(request)
            route = ModelRoute(
                provider=client.provider,
                model=client.model,
                reason=reason,
                primary_attempt_count=self._response_attempt_count(response),
            )
        except ModelGatewayError as primary_error:
            if self._fallback_client is None or self._fallback_client is client:
                primary_error.primary_attempt_count = max(1, primary_error.attempt_count)
                primary_error.fallback_attempt_count = 0
                raise
            try:
                response = await self._fallback_client.complete(request)
            except ModelGatewayError as fallback_error:
                fallback_error.primary_attempt_count = max(1, primary_error.attempt_count)
                fallback_error.fallback_attempt_count = max(1, fallback_error.attempt_count)
                # Preserve the legacy aggregate for callers that only know the
                # original exception contract.
                fallback_error.attempt_count = (
                    fallback_error.primary_attempt_count
                    + fallback_error.fallback_attempt_count
                )
                raise
            route = ModelRoute(
                provider=self._fallback_client.provider,
                model=self._fallback_client.model,
                reason="primary_failed",
                fallback_used=True,
                primary_attempt_count=max(1, primary_error.attempt_count),
                fallback_attempt_count=self._response_attempt_count(response),
            )
        self.route_counts[f"{route.provider}:{route.model}"] += 1
        return RoutedModelResponse(response=response, route=route)

    @staticmethod
    def _response_attempt_count(response: ModelResponse) -> int:
        raw = response.metadata.get("attempt_count", 1)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1 else 1


def compact_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
