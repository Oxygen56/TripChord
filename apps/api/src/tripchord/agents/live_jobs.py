from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, Self
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from tripchord.domain.common import DomainModel


class LivePlanningJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_LIVE_PLANNING_JOB_STATES = frozenset(
    {
        LivePlanningJobState.SUCCEEDED,
        LivePlanningJobState.FAILED,
        LivePlanningJobState.CANCELLED,
    }
)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


_PAIR_CHECKPOINT_BOUNDARY = (
    "有界进度摘要，不是完整 Evidence Bundle；仅包含日期范围、Source task ids、"
    "类型化状态与摘要哈希，不含其他搜索参数、报价、URL、浏览器回执、cookie、"
    "授权 token、凭证或原始失败正文。"
)
_CheckpointId = Annotated[str, Field(min_length=1, max_length=200)]
_CheckpointKind = Annotated[str, Field(min_length=1, max_length=80)]
_Sha256 = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
_SAFE_EXCEPTION_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_SAFE_VALIDATION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
_SAFE_VALIDATION_LOC_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
_SAFE_VALIDATION_MODEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,119}$")
_MAX_SAFE_VALIDATION_ERRORS = 32
_MAX_SAFE_VALIDATION_LOC_DEPTH = 16


class LivePlanningPairCheckpointState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class LivePlanningSafeFailureCode(StrEnum):
    """Stable, non-secret failure categories exposed by the live-job control plane."""

    PYDANTIC_VALIDATION_ERROR = "pydantic_validation_error"
    DOMAIN_VALUE_ERROR = "domain_value_error"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    TIMEOUT_ERROR = "timeout_error"
    HTTP_EXCEPTION = "http_exception"
    EXECUTION_EXCEPTION = "execution_exception"


class LivePlanningSafeValidationError(DomainModel):
    """Pydantic error metadata with messages, inputs, contexts and URLs removed."""

    type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    loc: tuple[str | int, ...] = Field(default=(), max_length=_MAX_SAFE_VALIDATION_LOC_DEPTH)
    message_sha256: _Sha256


class LivePlanningSafeFailureDetails(DomainModel):
    """Allowlisted diagnostic metadata; never stores an exception message."""

    exception_class: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    message_sha256: _Sha256 | None = None
    validation_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    )
    validation_errors: tuple[LivePlanningSafeValidationError, ...] = Field(
        default=(),
        max_length=_MAX_SAFE_VALIDATION_ERRORS,
    )


@dataclass(frozen=True)
class _SafeFailureDiagnostic:
    code: LivePlanningSafeFailureCode
    details: LivePlanningSafeFailureDetails
    details_digest: str


def _safe_exception_class(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _SAFE_EXCEPTION_CLASS_PATTERN.fullmatch(name) else "Exception"


def _safe_validation_loc_component(value: object) -> str | int:
    if type(value) is int and 0 <= value <= 1_000_000:
        return value
    if isinstance(value, str) and _SAFE_VALIDATION_LOC_PATTERN.fullmatch(value):
        return value
    return "redacted"


def _safe_validation_errors(exc: ValidationError) -> tuple[LivePlanningSafeValidationError, ...]:
    safe_errors: list[LivePlanningSafeValidationError] = []
    for raw_error in exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:_MAX_SAFE_VALIDATION_ERRORS]:
        raw_type = raw_error.get("type")
        error_type = (
            raw_type
            if isinstance(raw_type, str)
            and _SAFE_VALIDATION_TYPE_PATTERN.fullmatch(raw_type)
            else "unknown_error_type"
        )
        raw_loc = raw_error.get("loc")
        loc_values = raw_loc if isinstance(raw_loc, (tuple, list)) else ()
        raw_message = raw_error.get("msg")
        message = raw_message if isinstance(raw_message, str) else "unknown validation message"
        safe_errors.append(
            LivePlanningSafeValidationError(
                type=error_type,
                loc=tuple(
                    _safe_validation_loc_component(item)
                    for item in loc_values[:_MAX_SAFE_VALIDATION_LOC_DEPTH]
                ),
                message_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(safe_errors)


def _diagnostic_exception(exc: BaseException) -> BaseException:
    """Prefer an explicitly chained validation/value cause beneath an HTTP wrapper."""

    current: BaseException | None = exc
    seen: set[int] = set()
    chain: list[BaseException] = []
    value_error: ValueError | None = None
    timeout_error: TimeoutError | None = None
    while current is not None and id(current) not in seen and len(chain) < 12:
        seen.add(id(current))
        chain.append(current)
        if isinstance(current, ValidationError):
            return current
        if isinstance(current, ValueError):
            value_error = current
        if isinstance(current, TimeoutError):
            timeout_error = current
        current = current.__cause__
    return value_error or timeout_error or chain[0]


def _safe_failure_details_digest(
    code: LivePlanningSafeFailureCode,
    details: LivePlanningSafeFailureDetails,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "safe_failure_code": code.value,
                "details": details.model_dump(mode="json"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _safe_failure_diagnostic(
    exc: BaseException,
    *,
    code_override: LivePlanningSafeFailureCode | None = None,
) -> _SafeFailureDiagnostic:
    target = exc if code_override is not None else _diagnostic_exception(exc)
    validation_errors: tuple[LivePlanningSafeValidationError, ...] = ()
    message_sha256: str | None = None
    validation_model: str | None = None
    if code_override is not None:
        code = code_override
    elif isinstance(target, ValidationError):
        code = LivePlanningSafeFailureCode.PYDANTIC_VALIDATION_ERROR
    elif isinstance(target, ValueError):
        code = LivePlanningSafeFailureCode.DOMAIN_VALUE_ERROR
    elif isinstance(target, TimeoutError):
        code = LivePlanningSafeFailureCode.TIMEOUT_ERROR
    elif _safe_exception_class(target) == "HTTPException":
        code = LivePlanningSafeFailureCode.HTTP_EXCEPTION
    else:
        code = LivePlanningSafeFailureCode.EXECUTION_EXCEPTION
    if isinstance(target, ValidationError):
        validation_errors = _safe_validation_errors(target)
        raw_title = target.title
        validation_model = (
            raw_title
            if isinstance(raw_title, str)
            and _SAFE_VALIDATION_MODEL_PATTERN.fullmatch(raw_title)
            else "ValidationModel"
        )
    elif isinstance(target, ValueError):
        message_sha256 = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    details = LivePlanningSafeFailureDetails(
        exception_class=_safe_exception_class(target),
        message_sha256=message_sha256,
        validation_model=validation_model,
        validation_errors=validation_errors,
    )
    return _SafeFailureDiagnostic(
        code=code,
        details=details,
        details_digest=_safe_failure_details_digest(code, details),
    )


class LivePlanningPairCheckpoint(DomainModel):
    """Small, non-evidentiary progress record for one exact-date execution."""

    schema_version: str = "live-pair-checkpoint-v1"
    request_sha256: _Sha256
    sequence: int = Field(ge=1, le=8)
    date_pair_id: _CheckpointId
    departure_date: date
    return_date: date
    state: LivePlanningPairCheckpointState
    query_task_ids: tuple[_CheckpointId, ...] = Field(min_length=1, max_length=18)
    run_purpose: _CheckpointKind | None = None
    finalization_state: _CheckpointKind | None = None
    decision_state: _CheckpointKind | None = None
    source_task_count: int | None = Field(default=None, ge=1, le=64)
    exploration_seal_passed: bool | None = None
    all_platforms_complete: bool | None = None
    failure_class: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern="^[A-Za-z_][A-Za-z0-9_.]*$",
    )
    run_summary_sha256: _Sha256
    checkpoint_sha256: _Sha256
    captured_at: datetime
    boundary: str = _PAIR_CHECKPOINT_BOUNDARY

    _validate_captured_at = field_validator("captured_at")(
        lambda value: _aware(value, "captured_at")
    )

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        request_sha256: str,
        date_pair_id: str,
        departure_date: date,
        return_date: date,
        state: LivePlanningPairCheckpointState,
        query_task_ids: tuple[str, ...],
        captured_at: datetime,
        run_purpose: str | None = None,
        finalization_state: str | None = None,
        decision_state: str | None = None,
        source_task_count: int | None = None,
        exploration_seal_passed: bool | None = None,
        all_platforms_complete: bool | None = None,
        failure_class: str | None = None,
    ) -> Self:
        values: dict[str, Any] = {
            "schema_version": "live-pair-checkpoint-v1",
            "request_sha256": request_sha256,
            "sequence": sequence,
            "date_pair_id": date_pair_id,
            "departure_date": departure_date,
            "return_date": return_date,
            "state": state,
            "query_task_ids": query_task_ids,
            "run_purpose": run_purpose,
            "finalization_state": finalization_state,
            "decision_state": decision_state,
            "source_task_count": source_task_count,
            "exploration_seal_passed": exploration_seal_passed,
            "all_platforms_complete": all_platforms_complete,
            "failure_class": failure_class,
            "captured_at": captured_at,
            "boundary": _PAIR_CHECKPOINT_BOUNDARY,
        }
        values["run_summary_sha256"] = cls._digest(cls._run_summary(values))
        values["checkpoint_sha256"] = cls._digest(cls._checkpoint_summary(values))
        return cls.model_validate(values)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        if self.schema_version != "live-pair-checkpoint-v1":
            raise ValueError("unsupported live pair checkpoint schema")
        if self.boundary != _PAIR_CHECKPOINT_BOUNDARY:
            raise ValueError("live pair checkpoint must retain its evidence boundary")
        if self.return_date <= self.departure_date:
            raise ValueError("checkpoint return date must be after departure date")
        if len(set(self.query_task_ids)) != len(self.query_task_ids):
            raise ValueError("checkpoint query task ids must be unique")
        completed_fields = (
            self.run_purpose,
            self.finalization_state,
            self.decision_state,
            self.source_task_count,
            self.exploration_seal_passed,
            self.all_platforms_complete,
        )
        if self.state == LivePlanningPairCheckpointState.COMPLETED:
            if any(value is None for value in completed_fields) or self.failure_class is not None:
                raise ValueError("completed pair checkpoint requires only a run summary")
        elif any(value is not None for value in completed_fields) or self.failure_class is None:
            raise ValueError("failed pair checkpoint requires only a typed failure class")
        values = self.model_dump(mode="python")
        if self.run_summary_sha256 != self._digest(self._run_summary(values)):
            raise ValueError("live pair run summary SHA-256 does not match its fields")
        if self.checkpoint_sha256 != self._digest(self._checkpoint_summary(values)):
            raise ValueError("live pair checkpoint SHA-256 does not match its fields")
        return self

    @staticmethod
    def _run_summary(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": LivePlanningPairCheckpointState(values["state"]).value,
            "run_purpose": values.get("run_purpose"),
            "finalization_state": values.get("finalization_state"),
            "decision_state": values.get("decision_state"),
            "source_task_count": values.get("source_task_count"),
            "exploration_seal_passed": values.get("exploration_seal_passed"),
            "all_platforms_complete": values.get("all_platforms_complete"),
            "failure_class": values.get("failure_class"),
        }

    @classmethod
    def _checkpoint_summary(cls, values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": values["schema_version"],
            "request_sha256": values["request_sha256"],
            "sequence": values["sequence"],
            "date_pair_id": values["date_pair_id"],
            "departure_date": values["departure_date"],
            "return_date": values["return_date"],
            "state": LivePlanningPairCheckpointState(values["state"]).value,
            "query_task_ids": values["query_task_ids"],
            "run_summary_sha256": values["run_summary_sha256"],
            "captured_at": values["captured_at"],
        }

    @staticmethod
    def _digest(value: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                default=lambda item: (
                    item.isoformat()
                    if isinstance(item, (date, datetime))
                    else str(item)
                ),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()


class LiveSourceTerminalEvent(DomainModel):
    """A typed terminal state for one source task, surfaced before the barrier.

    Before the settle barrier releases, the SSE stream is only allowed to carry
    progress/terminal events — never partial quotes or plans.  Each event binds
    one source task id to its typed terminal state so the UI can show
    per-platform/vertical progress and reasons without leaking intermediate
    prices.
    """

    schema_version: str = "live-source-terminal-event-v1"
    source_task_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=64)
    vertical: str = Field(min_length=1, max_length=40)
    terminal_state: str = Field(min_length=1, max_length=40)
    occurred_at: datetime
    detail: str | None = Field(default=None, max_length=400)

    _validate_occurred_at = field_validator("occurred_at")(
        lambda value: _aware(value, "occurred_at")
    )

    @model_validator(mode="after")
    def validate_terminal_event(self) -> Self:
        if self.schema_version != "live-source-terminal-event-v1":
            raise ValueError("unsupported live source terminal event schema")
        return self


class LivePlanningJobSnapshot(DomainModel):
    id: str = Field(min_length=1)
    state: LivePlanningJobState
    stage: str = Field(min_length=1, max_length=80)
    progress: int = Field(ge=0, le=100)
    cancellation_requested: bool = False
    cancel_pending: bool = False
    revision: int = Field(ge=1)
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=200)
    safe_failure_code: LivePlanningSafeFailureCode | None = None
    safe_failure_details: LivePlanningSafeFailureDetails | None = None
    safe_failure_details_digest: _Sha256 | None = None
    request_sha256: _Sha256 | None = None
    model_trace_scope_sha256: _Sha256 | None = None
    model_trace_count: int = Field(default=0, ge=0)
    model_trace_success_count: int = Field(default=0, ge=0)
    model_trace_failure_count: int = Field(default=0, ge=0)
    pair_checkpoints: tuple[LivePlanningPairCheckpoint, ...] = Field(
        default=(),
        max_length=8,
    )
    source_terminal_events: tuple[LiveSourceTerminalEvent, ...] = Field(
        default=(),
        max_length=64,
    )
    barrier_released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime
    expires_at: datetime | None = None
    boundary: str = (
        "本机进程内、单服务进程的长任务控制面；最多并行执行有限个任务，"
        "终态按容量和 TTL 有界保存。进程重启不恢复任务，不能视为持久化生产队列。"
    )

    _validate_created_at = field_validator("created_at")(
        lambda value: _aware(value, "created_at")
    )
    _validate_updated_at = field_validator("updated_at")(
        lambda value: _aware(value, "updated_at")
    )
    _validate_deadline_at = field_validator("deadline_at")(
        lambda value: _aware(value, "deadline_at")
    )
    _validate_expires_at = field_validator("expires_at")(
        lambda value: None if value is None else _aware(value, "expires_at")
    )
    _validate_barrier_released_at = field_validator("barrier_released_at")(
        lambda value: None if value is None else _aware(value, "barrier_released_at")
    )

    @model_validator(mode="after")
    def validate_bound_evidence(self) -> Self:
        if self.deadline_at <= self.created_at:
            raise ValueError("live job deadline must be after admission")
        if self.model_trace_count != (
            self.model_trace_success_count + self.model_trace_failure_count
        ):
            raise ValueError("live job model trace counts must add up")
        if (self.request_sha256 is None) != (self.model_trace_scope_sha256 is None):
            raise ValueError("live job request and model trace scope SHA-256 must be set together")
        if (
            self.request_sha256 is not None
            and self.model_trace_scope_sha256 != self.request_sha256
        ):
            raise ValueError("live job model trace scope SHA-256 must match its request")
        if tuple(item.sequence for item in self.pair_checkpoints) != tuple(
            range(1, len(self.pair_checkpoints) + 1)
        ):
            raise ValueError("live pair checkpoints must be contiguous and ordered")
        pair_ids = tuple(item.date_pair_id for item in self.pair_checkpoints)
        if len(set(pair_ids)) != len(pair_ids):
            raise ValueError("live pair checkpoints must bind unique date pairs")
        if self.pair_checkpoints and self.request_sha256 is None:
            raise ValueError("live pair checkpoints require a bound job request SHA-256")
        if any(
            item.request_sha256 != self.request_sha256 for item in self.pair_checkpoints
        ):
            raise ValueError("live pair checkpoint request SHA-256 must match its job")
        safe_failure_fields = (
            self.safe_failure_code,
            self.safe_failure_details,
            self.safe_failure_details_digest,
        )
        if any(value is not None for value in safe_failure_fields) and not all(
            value is not None for value in safe_failure_fields
        ):
            raise ValueError("live job safe failure diagnostics must be set together")
        if self.safe_failure_code is not None:
            if self.state != LivePlanningJobState.FAILED:
                raise ValueError("live job safe failure diagnostics require a failed state")
            assert self.safe_failure_details is not None
            assert self.safe_failure_details_digest is not None
            if self.safe_failure_details_digest != _safe_failure_details_digest(
                self.safe_failure_code,
                self.safe_failure_details,
            ):
                raise ValueError("live job safe failure details digest does not match")
            has_message_digest = self.safe_failure_details.message_sha256 is not None
            has_validation_model = self.safe_failure_details.validation_model is not None
            has_validation_errors = bool(self.safe_failure_details.validation_errors)
            if self.safe_failure_code == LivePlanningSafeFailureCode.PYDANTIC_VALIDATION_ERROR:
                if has_message_digest or not has_validation_model or not has_validation_errors:
                    raise ValueError(
                        "Pydantic safe failure details require a model and typed "
                        "validation metadata"
                    )
            elif self.safe_failure_code == LivePlanningSafeFailureCode.DOMAIN_VALUE_ERROR:
                if not has_message_digest or has_validation_model or has_validation_errors:
                    raise ValueError(
                        "domain ValueError safe failure details require only a message digest"
                    )
            elif has_message_digest or has_validation_model or has_validation_errors:
                raise ValueError(
                    "non-validation safe failure details cannot include validation metadata"
                )
        return self


class LivePlanningJobCapacityError(RuntimeError):
    pass


class LivePlanningJobIdempotencyConflictError(RuntimeError):
    pass


class LivePlanningJobInactiveError(RuntimeError):
    """Raised when detached work tries to mutate a terminal job generation."""

    pass


class LivePlanningJobRegistryPostCommitError(RuntimeError):
    """Raised when a registry persist fails after ``os.replace`` has committed.

    The disk already carries the newly written state, so callers must NOT roll
    the in-memory record back to its pre-persist value — the memory was mutated
    before the persist and therefore already matches the committed disk state.
    The failure is surfaced as an explicit indeterminate terminal outcome.

    ``job_id`` (when set by the raising call site) carries the committed identity
    so a production entry point can hand the caller a recoverable handle to the
    real task instead of failing closed without a trace."""

    def __init__(
        self,
        message: str = (
            "live planning job registry state was committed but could not be "
            "finalized; the on-disk record is authoritative"
        ),
    ) -> None:
        super().__init__(message)
        self.job_id: str | None = None


class LiveJobProgressReporter(Protocol):
    @property
    def job_id(self) -> str: ...

    async def ensure_active(self) -> None: ...

    async def __call__(self, stage: str, progress: int) -> None: ...

    async def report_pair_checkpoint(
        self,
        checkpoint: LivePlanningPairCheckpoint,
    ) -> None: ...

    async def report_model_trace_summary(
        self,
        scope_id: str,
        scope_request_sha256: str,
        trace_count: int,
        success_count: int,
        failure_count: int,
    ) -> None: ...

    async def report_source_terminal_events(
        self,
        events: tuple[LiveSourceTerminalEvent, ...],
    ) -> None: ...

    async def report_barrier_released(self, barrier_released_at: datetime) -> None: ...


LiveJobOperation = Callable[[LiveJobProgressReporter], Awaitable[dict[str, Any]]]


class _RegistryProgressReporter:
    def __init__(
        self,
        registry: LivePlanningJobRegistry,
        runtime: _RuntimeJob,
        generation: int,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._generation = generation

    @property
    def job_id(self) -> str:
        return self._runtime.snapshot.id

    async def ensure_active(self) -> None:
        await self._registry._ensure_active(self._runtime, self._generation)

    async def __call__(self, stage: str, progress: int) -> None:
        await self._registry._update_running(
            self._runtime,
            stage,
            progress,
            generation=self._generation,
        )

    async def report_pair_checkpoint(
        self,
        checkpoint: LivePlanningPairCheckpoint,
    ) -> None:
        await self._registry._update_pair_checkpoint(
            self._runtime,
            checkpoint,
            generation=self._generation,
        )

    async def report_model_trace_summary(
        self,
        scope_id: str,
        scope_request_sha256: str,
        trace_count: int,
        success_count: int,
        failure_count: int,
    ) -> None:
        await self._registry._update_model_trace_summary(
            self._runtime,
            scope_id=scope_id,
            scope_request_sha256=scope_request_sha256,
            trace_count=trace_count,
            success_count=success_count,
            failure_count=failure_count,
            generation=self._generation,
        )

    async def report_source_terminal_events(
        self,
        events: tuple[LiveSourceTerminalEvent, ...],
    ) -> None:
        await self._registry._update_source_terminal_events(
            self._runtime,
            events,
            generation=self._generation,
        )

    async def report_barrier_released(self, barrier_released_at: datetime) -> None:
        await self._registry._mark_barrier_released(
            self._runtime,
            barrier_released_at,
            generation=self._generation,
        )


class _RuntimeJob:
    def __init__(
        self,
        *,
        tenant_partition: str,
        snapshot: LivePlanningJobSnapshot,
        deadline_monotonic: float,
        operation: LiveJobOperation,
        prepared: bool = False,
    ) -> None:
        self.tenant_partition = tenant_partition
        self.snapshot = snapshot
        self.deadline_monotonic = deadline_monotonic
        self.operation = operation
        self.prepared = prepared
        self.activation_operation: dict[str, Any] | None = None
        self.generation = 1
        self.task: asyncio.Task[None] | None = None
        self.operation_task: asyncio.Task[dict[str, Any]] | None = None
        self.model_trace_summary_reported = False
        # P0-1 bounded-cleanup bookkeeping: set once a cancellation is in flight
        # so repeated cancels join the same cleanup, and records whether the
        # operation coroutine actually stopped within the budget.
        self.cancel_pending = False
        self.cancel_future: asyncio.Future[LivePlanningJobSnapshot | None] | None = None
        self.cancel_drain_succeeded: bool | None = None


class _IdempotencyEntry:
    def __init__(
        self,
        *,
        job_id: str,
        request_digest: str,
        defer_start: bool | None = None,
        legacy_isolated: bool = False,
    ) -> None:
        self.job_id = job_id
        self.request_digest = request_digest
        # P0-3: the stable execution mode is bound into the idempotency identity
        # so a same-key request cannot silently switch between a prepared
        # (defer_start=True) and an immediate execution of the same payload.
        self.defer_start = defer_start
        # P0-2: a legacy (v1/v2) binding whose execution mode cannot be proven is
        # isolated — it is never replayed under any mode and always conflicts.
        self.legacy_isolated = legacy_isolated


class LivePlanningJobRegistry:
    """Bounded control plane with fail-closed restart tombstones when persisted."""

    def __init__(
        self,
        *,
        capacity: int = 16,
        max_running: int = 1,
        terminal_ttl: timedelta = timedelta(minutes=30),
        cancel_wait_seconds: float = 10,
        now: Callable[[], datetime] | None = None,
        state_path: Path | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if not 1 <= max_running <= capacity:
            raise ValueError("max_running must be between one and capacity")
        if terminal_ttl <= timedelta(0):
            raise ValueError("terminal_ttl must be positive")
        if cancel_wait_seconds <= 0:
            raise ValueError("cancel_wait_seconds must be positive")
        self._capacity = capacity
        self._terminal_ttl = terminal_ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._cancel_wait_seconds = cancel_wait_seconds
        self._slots = asyncio.Semaphore(max_running)
        self._records: dict[str, _RuntimeJob] = {}
        self._idempotency: dict[str, _IdempotencyEntry] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)
        self._closed = False
        self._state_path = state_path
        if self._state_path is not None:
            self._load_state()

    @staticmethod
    async def _unrecoverable_operation(
        _report: LiveJobProgressReporter,
    ) -> dict[str, Any]:
        raise LivePlanningJobInactiveError(
            "live planning job operation cannot continue after a process restart"
        )

    def _load_state(self) -> None:
        path = self._state_path
        assert path is not None
        if not path.is_absolute():
            raise RuntimeError("live planning job registry state path must be absolute")
        self._validate_state_parent(path.parent)
        try:
            path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RuntimeError("live planning job registry state is unavailable") from exc
        self._validate_state_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("live planning job registry state is unreadable") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "records",
            "idempotency",
        }:
            raise RuntimeError("live planning job registry state has an invalid shape")
        schema_version = payload["schema_version"]
        if schema_version not in {
            "tripchord-live-job-registry-v1",
            "tripchord-live-job-registry-v2",
            "tripchord-live-job-registry-v3",
        }:
            raise RuntimeError("live planning job registry state schema is invalid")
        records = payload["records"]
        idempotency = payload["idempotency"]
        if (
            not isinstance(records, list)
            or len(records) > self._capacity
            or not isinstance(idempotency, list)
        ):
            raise RuntimeError("live planning job registry state exceeds its bounds")
        for item in records:
            expected_record_fields = {
                "tenant_partition",
                "snapshot",
                "prepared",
            }
            if schema_version in {
                "tripchord-live-job-registry-v2",
                "tripchord-live-job-registry-v3",
            }:
                expected_record_fields.add("activation_operation")
            if not isinstance(item, dict) or set(item) != expected_record_fields:
                raise RuntimeError("live planning job registry record is invalid")
            tenant_partition = item["tenant_partition"]
            prepared = item["prepared"]
            if (
                not isinstance(tenant_partition, str)
                or re.fullmatch(r"[0-9a-f]{64}", tenant_partition) is None
                or type(prepared) is not bool
            ):
                raise RuntimeError("live planning job registry record identity is invalid")
            try:
                snapshot = LivePlanningJobSnapshot.model_validate(item["snapshot"])
            except ValidationError as exc:
                raise RuntimeError("live planning job registry snapshot is invalid") from exc
            if snapshot.id in self._records:
                raise RuntimeError("live planning job registry record is duplicated")
            if prepared and snapshot.state != LivePlanningJobState.QUEUED:
                raise RuntimeError("live planning prepared record is inconsistent")
            runtime = _RuntimeJob(
                tenant_partition=tenant_partition,
                snapshot=snapshot,
                deadline_monotonic=0.0,
                operation=self._unrecoverable_operation,
                prepared=prepared,
            )
            if schema_version in {
                "tripchord-live-job-registry-v2",
                "tripchord-live-job-registry-v3",
            }:
                activation_operation = item["activation_operation"]
                if activation_operation is not None:
                    runtime.activation_operation = self._validate_activation_operation(
                        activation_operation,
                        expected_job_id=snapshot.id,
                    )
            self._records[snapshot.id] = runtime
        for item in idempotency:
            expected_idempotency_fields = {
                "partition",
                "job_id",
                "request_digest",
            }
            legacy_isolated_field = "legacy_isolated"
            if schema_version == "tripchord-live-job-registry-v3":
                expected_idempotency_fields.add("defer_start")
            # The v3 loader accepts the optional legacy-isolation marker on top
            # of the required field set (older v3 files omit it entirely).
            if not isinstance(item, dict) or set(item) not in {
                frozenset(expected_idempotency_fields),
                frozenset(expected_idempotency_fields | {legacy_isolated_field}),
            }:
                raise RuntimeError("live planning idempotency record is invalid")
            partition = item["partition"]
            job_id = item["job_id"]
            request_digest = item["request_digest"]
            if (
                not isinstance(partition, str)
                or re.fullmatch(r"[0-9a-f]{64}", partition) is None
                or partition in self._idempotency
                or not isinstance(job_id, str)
                or not isinstance(request_digest, str)
                or not self._valid_request_digest(request_digest)
            ):
                raise RuntimeError("live planning idempotency identity is invalid")
            defer_start: bool | None = None
            legacy_isolated = False
            if schema_version == "tripchord-live-job-registry-v3":
                defer_start = item["defer_start"]
                if type(defer_start) is not bool:
                    raise RuntimeError("live planning idempotency execution mode is invalid")
                legacy_isolated = item.get(legacy_isolated_field, False)
                if type(legacy_isolated) is not bool:
                    raise RuntimeError("live planning idempotency isolation is invalid")
            runtime = self._records.get(job_id)
            if (
                runtime is None
                or runtime.snapshot.request_sha256 != request_digest
            ):
                raise RuntimeError("live planning idempotency binding is invalid")
            if defer_start is None:
                # P0-2: legacy v1/v2 records carry no execution mode. Derive the
                # provable mode from the surviving durable facts; when the facts
                # are insufficient, fail closed into an isolated binding that is
                # never replayed (the derived bool is persisted, never null).
                derived = self._derive_legacy_execution_mode(
                    runtime,
                    schema_version=schema_version,
                )
                if derived is None:
                    legacy_isolated = True
                    defer_start = False
                else:
                    defer_start = derived
            self._idempotency[partition] = _IdempotencyEntry(
                job_id=job_id,
                request_digest=request_digest,
                defer_start=defer_start,
                legacy_isolated=legacy_isolated,
            )
        for runtime in self._records.values():
            if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES:
                runtime.prepared = False
                self._terminalize_locked(
                    runtime,
                    LivePlanningJobState.CANCELLED,
                    stage="restart_cancelled",
                    error="live planning job cannot continue after process restart",
                    cancellation_requested=True,
                )
        self._prune_locked(self._utc_now())
        self._persist_locked()

    def _derive_legacy_execution_mode(
        self,
        runtime: _RuntimeJob,
        *,
        schema_version: str,
    ) -> bool | None:
        """Derive the provable execution mode for a legacy (v1/v2) idempotency
        binding, or return None when the legacy facts are insufficient.

        Returns True for a prepared (defer_start=True) job, False for an
        immediate (defer_start=False) job, and None to fail closed into a
        legacy-isolated binding that is never replayed under any mode.
        """
        if schema_version == "tripchord-live-job-registry-v1":
            # v1 predates the activation_operation field entirely, so only an
            # un-activated prepared record (prepared=True, QUEUED) is provably
            # prepared; every other v1 record is ambiguous.
            return True if runtime.prepared else None
        # v2/v3 records carry the activation_operation, which is the durable
        # proof that a prepared job reached (or passed) activation intent.
        if runtime.activation_operation is not None:
            return True
        if runtime.prepared:
            return True
        if runtime.snapshot.state == LivePlanningJobState.CANCELLED:
            # A terminal cancelled job without an activation operation could be
            # an immediate job cancelled mid-flight OR a prepared job cancelled
            # before any intent was persisted — indistinguishable, so fail closed.
            return None
        return False

    @staticmethod
    def _validate_state_parent(parent: Path) -> None:
        try:
            info = parent.lstat()
        except OSError as exc:
            raise RuntimeError("live planning job registry parent is unavailable") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError("live planning job registry parent is not owner-only")

    @staticmethod
    def _validate_state_file(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise RuntimeError("live planning job registry state is unavailable") from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise RuntimeError("live planning job registry state is not an owner-only file")

    def _persist_locked(self) -> None:
        path = self._state_path
        if path is None:
            return
        self._validate_state_parent(path.parent)
        try:
            path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError("live planning job registry state is unavailable") from exc
        else:
            self._validate_state_file(path)
        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [
                {
                    "tenant_partition": runtime.tenant_partition,
                    "snapshot": runtime.snapshot.model_dump(mode="json"),
                    "prepared": runtime.prepared,
                    "activation_operation": runtime.activation_operation,
                }
                for runtime in sorted(
                    self._records.values(), key=lambda item: item.snapshot.id
                )
            ],
            "idempotency": [
                {
                    "partition": partition,
                    "job_id": entry.job_id,
                    "request_digest": entry.request_digest,
                    "defer_start": entry.defer_start,
                    "legacy_isolated": entry.legacy_isolated,
                }
                for partition, entry in sorted(self._idempotency.items())
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        descriptor = -1
        commit = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as target:
                descriptor = -1
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())
            self._validate_state_file(temporary)
            os.replace(temporary, path)
            commit = True
            if (
                os.environ.get("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")
                == "post_replace_validation"
            ):
                raise RuntimeError("injected post-replace validation failure")
            self._validate_state_file(path)
            if (
                os.environ.get("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")
                == "post_replace_dir_fsync"
            ):
                raise OSError("injected post-replace directory fsync failure")
            parent_descriptor = os.open(
                path.parent,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if commit:
                raise LivePlanningJobRegistryPostCommitError(
                    "live planning job registry state was committed but could "
                    "not be finalized; the on-disk record is authoritative"
                ) from exc
            with suppress(OSError):
                temporary.unlink()
            raise RuntimeError("live planning job registry state write failed") from exc
        except Exception as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if commit:
                raise LivePlanningJobRegistryPostCommitError(
                    "live planning job registry state was committed but could "
                    "not be finalized; the on-disk record is authoritative"
                ) from exc
            with suppress(OSError):
                temporary.unlink()
            raise

    async def start(
        self,
        *,
        tenant_id: str,
        operation: LiveJobOperation,
        request_digest: str | None = None,
        deadline_seconds: float = 3600,
    ) -> LivePlanningJobSnapshot:
        job, _ = await self.start_idempotent(
            tenant_id=tenant_id,
            operation=operation,
            request_digest=request_digest,
            deadline_seconds=deadline_seconds,
        )
        return job

    async def start_idempotent(
        self,
        *,
        tenant_id: str,
        operation: LiveJobOperation,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        deadline_seconds: float = 3600,
        defer_start: bool = False,
    ) -> tuple[LivePlanningJobSnapshot, bool]:
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be a finite positive number")
        idempotency_partition: str | None = None
        if request_digest is not None and not self._valid_request_digest(request_digest):
            raise ValueError("request_digest must be a lowercase SHA-256 hex digest")
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 200:
                raise ValueError("idempotency key must contain 1 to 200 characters")
            if request_digest is None:
                raise ValueError("request_digest must be a lowercase SHA-256 hex digest")
            idempotency_partition = self._idempotency_partition(tenant_id, idempotency_key)

        now = self._utc_now()
        deadline_at = now + timedelta(seconds=deadline_seconds)
        # Canonical UUID ids remain globally unique without the mixed-case
        # random runs that resemble bare credentials in committed evidence.
        job_id = f"live-job-{uuid4()}"
        runtime = _RuntimeJob(
            tenant_partition=self._tenant_partition(tenant_id),
            deadline_monotonic=asyncio.get_running_loop().time() + deadline_seconds,
            snapshot=LivePlanningJobSnapshot(
                id=job_id,
                state=LivePlanningJobState.QUEUED,
                stage="queued",
                progress=0,
                revision=1,
                request_sha256=request_digest,
                model_trace_scope_sha256=request_digest,
                created_at=now,
                updated_at=now,
                deadline_at=deadline_at,
            ),
            operation=operation,
            prepared=defer_start,
        )
        async with self._changed:
            if self._closed:
                raise RuntimeError("live planning job registry is closed")
            self._prune_locked(now)
            if idempotency_partition is not None:
                existing = self._idempotency.get(idempotency_partition)
                if existing is not None:
                    existing_runtime = self._records.get(existing.job_id)
                    if existing_runtime is None:
                        self._idempotency.pop(idempotency_partition, None)
                    elif not secrets.compare_digest(existing.request_digest, request_digest or ""):
                        raise LivePlanningJobIdempotencyConflictError(
                            "idempotency key was already used with a different request"
                        )
                    elif existing.legacy_isolated:
                        # P0-2: an ambiguous legacy v1/v2 binding is isolated —
                        # never replayed under any execution mode.
                        raise LivePlanningJobIdempotencyConflictError(
                            "idempotency key is bound to an isolated legacy record"
                        )
                    elif (
                        existing.defer_start is not None
                        and existing.defer_start != defer_start
                    ):
                        # P0-3: the execution mode is part of the identity. A
                        # same-key request that switches between a prepared and an
                        # immediate execution must fail closed — never reuse the
                        # old receipt under a different executor mode.
                        raise LivePlanningJobIdempotencyConflictError(
                            "idempotency key was already used with a different execution mode"
                        )
                    else:
                        # P0-1: a cancel_pending record whose executor has already
                        # stopped can be terminalized idempotently here, so a
                        # same-key retry never reuses a dead running executor as an
                        # active job.
                        if existing_runtime.snapshot.cancel_pending and (
                            existing_runtime.task is None
                            or existing_runtime.task.done()
                        ):
                            try:
                                self._complete_cancel_terminalize_locked(
                                    existing_runtime
                                )
                            except LivePlanningJobRegistryPostCommitError as exc:
                                exc.job_id = existing_runtime.snapshot.id
                                raise
                        return existing_runtime.snapshot, True
            self._make_capacity_locked()
            self._records[job_id] = runtime
            if idempotency_partition is not None:
                assert request_digest is not None
                self._idempotency[idempotency_partition] = _IdempotencyEntry(
                    job_id=job_id,
                    request_digest=request_digest,
                    defer_start=defer_start,
                )
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError as exc:
                # The new record was already committed to disk. A committed
                # non-prepared record promises an executor, so create it now (durable
                # start) before surfacing the indeterminate create; a prepared record
                # stays prepared for a later activation. Never leave a committed
                # "queued, prepared=false, task=None" job that a same-key retry
                # reports as reused but that never executes.
                exc.job_id = job_id
                if not defer_start:
                    runtime.task = asyncio.create_task(
                        self._run(runtime, operation),
                        name=f"tripchord:{job_id}",
                    )
                self._changed.notify_all()
                raise
            except Exception:
                self._remove_locked(job_id)
                raise
            if not defer_start:
                runtime.task = asyncio.create_task(
                    self._run(runtime, operation),
                    name=f"tripchord:{job_id}",
                )
            self._changed.notify_all()
            return runtime.snapshot, False

    async def activate(
        self,
        job_id: str,
        tenant_id: str,
        *,
        operation_id: str | None = None,
    ) -> LivePlanningJobSnapshot | None:
        """Start one explicitly prepared job exactly once.

        Formal evidence uses this split so its signed challenge can bind the
        already allocated terminal job id before any provider or Companion
        event is allowed to occur.
        """

        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            if runtime is None:
                return None
            activation = runtime.activation_operation
            if activation is not None:
                if operation_id != activation["operation_id"]:
                    raise LivePlanningJobInactiveError(
                        "live planning job uses a foreign activation operation"
                    )
                if activation["phase"] == "cancelled":
                    raise LivePlanningJobInactiveError(
                        "live planning activation operation was cancelled"
                    )
                if activation["phase"] in {"dispatched", "committed"}:
                    if runtime.snapshot.state == LivePlanningJobState.CANCELLED:
                        raise LivePlanningJobInactiveError(
                            "live planning job was cancelled"
                        )
                    return runtime.snapshot
                if activation["phase"] != "intent" or activation["dispatch_count"] != 0:
                    raise LivePlanningJobInactiveError(
                        "live planning activation operation is inconsistent"
                    )
            elif operation_id is not None:
                raise LivePlanningJobInactiveError(
                    "live planning activation operation has no durable intent"
                )
            if not runtime.prepared and runtime.task is not None:
                return runtime.snapshot
            if not runtime.prepared or runtime.task is not None:
                raise LivePlanningJobInactiveError(
                    "live planning job is not an unactivated prepared job"
                )
            if runtime.snapshot.state != LivePlanningJobState.QUEUED:
                raise LivePlanningJobInactiveError(
                    "prepared live planning job is no longer queued"
                )
            runtime.prepared = False
            if activation is not None:
                # Reassign a fresh operation object instead of mutating in place so
                # the original body stays intact for a pre-commit rollback. After the
                # replace commits, the disk already matches this new memory and must
                # NOT be rolled back.
                runtime.activation_operation = {
                    **activation,
                    "phase": "dispatched",
                    "dispatch_count": 1,
                }
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError as exc:
                # The dispatched state was already committed to disk; a committed
                # "dispatched" operation must have a real executor, so complete the
                # dispatch by creating the task before surfacing the indeterminate
                # outcome. Never keep a fake dispatched record with no runner.
                exc.job_id = job_id
                runtime.task = asyncio.create_task(
                    self._run(runtime, runtime.operation),
                    name=f"tripchord:{job_id}",
                )
                self._changed.notify_all()
                raise
            except Exception:
                runtime.prepared = True
                if activation is not None:
                    runtime.activation_operation = activation
                raise
            if (
                os.environ.get("TRIPCHORD_TEST_FORMAL_ACTIVATION_FAILPOINT")
                == "exit_after_registry_dispatch_persist"
            ):
                os._exit(86)
            runtime.task = asyncio.create_task(
                self._run(runtime, runtime.operation),
                name=f"tripchord:{job_id}",
            )
            self._changed.notify_all()
            return runtime.snapshot

    @staticmethod
    def _validate_activation_operation(
        value: object,
        *,
        expected_job_id: str,
        allow_intent_shape: bool = False,
    ) -> dict[str, Any]:
        immutable_fields = {
            "schema_version",
            "operation_id",
            "idempotency_key",
            "request_digest",
            "job_id",
            "challenge_id",
            "attempt_digest",
            "capability_sha256",
            "companion_identity_sha256",
            "queued_result",
        }
        fields = immutable_fields if allow_intent_shape else immutable_fields | {
            "phase",
            "dispatch_count",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise RuntimeError("live planning activation operation has an invalid shape")
        if value["schema_version"] != "tripchord-live-activation-operation-v1":
            raise RuntimeError("live planning activation operation schema is invalid")
        if value["job_id"] != expected_job_id:
            raise RuntimeError("live planning activation operation targets a foreign job")
        for field_name in (
            "operation_id",
            "request_digest",
            "attempt_digest",
            "capability_sha256",
            "companion_identity_sha256",
        ):
            if not isinstance(value[field_name], str) or re.fullmatch(
                r"[0-9a-f]{64}", value[field_name]
            ) is None:
                raise RuntimeError(
                    f"live planning activation operation {field_name} is invalid"
                )
        for field_name in ("idempotency_key", "challenge_id"):
            if (
                not isinstance(value[field_name], str)
                or not value[field_name].strip()
                or len(value[field_name]) > 200
            ):
                raise RuntimeError(
                    f"live planning activation operation {field_name} is invalid"
                )
        queued_result = value["queued_result"]
        if (
            not isinstance(queued_result, dict)
            or set(queued_result) != {"job"}
            or not isinstance(queued_result["job"], dict)
            or queued_result["job"].get("id") != expected_job_id
            or queued_result["job"].get("state") != LivePlanningJobState.QUEUED.value
        ):
            raise RuntimeError("live planning activation queued result is invalid")
        if not allow_intent_shape:
            phase = value["phase"]
            dispatch_count = value["dispatch_count"]
            if phase not in {"intent", "dispatched", "committed", "cancelled"}:
                raise RuntimeError("live planning activation operation phase is invalid")
            expected_counts = (
                (0,) if phase == "intent" else (0, 1) if phase == "cancelled" else (1,)
            )
            if type(dispatch_count) is not int or dispatch_count not in expected_counts:
                raise RuntimeError("live planning activation dispatch count is invalid")
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    async def prepare_activation_intent(
        self,
        job_id: str,
        tenant_id: str,
        *,
        intent: object,
    ) -> dict[str, Any]:
        """Persist one immutable formal activation identity before dispatch."""

        checked = self._validate_activation_operation(
            intent,
            expected_job_id=job_id,
            allow_intent_shape=True,
        )
        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            if runtime is None:
                raise LivePlanningJobInactiveError(
                    "live planning activation intent targets an unavailable job"
                )
            existing = runtime.activation_operation
            if existing is not None:
                existing_intent = {
                    key: existing[key]
                    for key in checked
                }
                if existing_intent != checked:
                    raise LivePlanningJobInactiveError(
                        "live planning activation intent differs from the durable intent"
                    )
                return json.loads(json.dumps(existing, sort_keys=True))
            if (
                not runtime.prepared
                or runtime.task is not None
                or runtime.snapshot.state != LivePlanningJobState.QUEUED
                or checked["queued_result"]["job"]
                != runtime.snapshot.model_dump(mode="json")
            ):
                raise LivePlanningJobInactiveError(
                    "live planning activation intent requires the exact prepared job"
                )
            runtime.activation_operation = {
                **checked,
                "phase": "intent",
                "dispatch_count": 0,
            }
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError:
                raise
            except Exception:
                runtime.activation_operation = None
                raise
            self._changed.notify_all()
            return json.loads(json.dumps(runtime.activation_operation, sort_keys=True))

    async def activation_operation(
        self,
        job_id: str,
        tenant_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            operation = runtime.activation_operation if runtime is not None else None
            if operation is None or operation.get("operation_id") != operation_id:
                raise LivePlanningJobInactiveError(
                    "live planning job uses a foreign activation operation"
                )
            return json.loads(json.dumps(operation, sort_keys=True))

    async def commit_activation(
        self,
        job_id: str,
        tenant_id: str,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            operation = runtime.activation_operation if runtime is not None else None
            if operation is None or operation.get("operation_id") != operation_id:
                raise LivePlanningJobInactiveError(
                    "live planning job uses a foreign activation operation"
                )
            if operation["phase"] == "cancelled":
                raise LivePlanningJobInactiveError(
                    "live planning activation operation was cancelled"
                )
            if runtime is not None and runtime.snapshot.state == LivePlanningJobState.CANCELLED:
                raise LivePlanningJobInactiveError(
                    "live planning job was cancelled"
                )
            if operation["phase"] == "intent":
                raise LivePlanningJobInactiveError(
                    "live planning activation operation was not dispatched"
                )
            if operation["phase"] == "dispatched":
                committed_operation = {**operation, "phase": "committed"}
                runtime.activation_operation = committed_operation
                try:
                    self._persist_locked()
                except LivePlanningJobRegistryPostCommitError:
                    raise
                except Exception:
                    # A pre-commit persist failure must restore the pre-commit
                    # operation body (including every nested field) so memory and
                    # disk agree. Post-commit failures keep the committed memory —
                    # the disk already carries it.
                    runtime.activation_operation = operation
                    raise
                self._changed.notify_all()
                return json.loads(json.dumps(committed_operation, sort_keys=True))
            return json.loads(json.dumps(operation, sort_keys=True))

    async def is_prepared(
        self,
        job_id: str,
        tenant_id: str,
        *,
        request_sha256: str,
    ) -> bool:
        async with self._lock:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            return bool(
                runtime is not None
                and runtime.prepared
                and runtime.task is None
                and runtime.snapshot.state == LivePlanningJobState.QUEUED
                and runtime.snapshot.request_sha256 == request_sha256
            )

    async def get(
        self,
        job_id: str,
        tenant_id: str,
    ) -> LivePlanningJobSnapshot | None:
        async with self._lock:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            return runtime.snapshot if runtime is not None else None

    async def cancel(
        self,
        job_id: str,
        tenant_id: str,
    ) -> LivePlanningJobSnapshot | None:
        """Cancel a live job with a bounded, fail-closed cleanup protocol.

        The final CANCELLED label is NEVER published before both the real
        ``operation_task`` and the registry ``task`` are confirmed done. A first
        cancellation flips the externally visible ``cancel_pending`` state, then
        requests the operation to stop and waits within the bounded
        ``_cancel_wait_seconds`` budget. If the operation swallows CancelledError
        and keeps running past the budget, the job FAILS CLOSED into a
        non-terminal ``cancel_timed_out`` state instead of faking a clean cancel.
        Repeated cancels idempotently join the same in-flight cleanup and never
        repeat its side effects.
        """
        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            if runtime is None:
                return None
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return runtime.snapshot
            if runtime.snapshot.cancel_pending:
                pending_future = runtime.cancel_future
                pending_runtime = runtime
                future: asyncio.Future[LivePlanningJobSnapshot | None] | None = None
                self._changed.notify_all()
            else:
                pending_future = None
                pending_runtime = None
                # Capture the full pre-call record so a pre-commit persist
                # failure can restore it byte-identically to the untouched disk
                # file — the transient cancel_pending/cancelling marker must never
                # leak into memory when no durable write succeeded.
                pre_call_snapshot = runtime.snapshot
                pre_call_generation = runtime.generation
                pre_call_prepared = runtime.prepared
                pre_call_activation_operation = runtime.activation_operation
                pre_call_cancel_pending = runtime.cancel_pending
                pre_call_cancel_future = runtime.cancel_future
                pre_call_cancel_drain_succeeded = runtime.cancel_drain_succeeded
                runtime.cancel_pending = True
                runtime.snapshot = runtime.snapshot.model_copy(
                    update={
                        "cancel_pending": True,
                        "cancellation_requested": True,
                        "stage": "cancelling",
                        "updated_at": self._utc_now(),
                    }
                )
                task = runtime.task
                operation_task = runtime.operation_task
                runtime.cancel_future = asyncio.get_running_loop().create_future()
                future = runtime.cancel_future
                # P0-1: persist the cancellation intent durably BEFORE stopping
                # the real executor, so a later persist failure can never strand
                # a stopped executor under an active/RUNNING record. Only once the
                # disk records cancel_pending do we touch the real work; if this
                # write fails pre-commit the executor is still untouched and the
                # record rolls back to the truthful RUNNING state.
                try:
                    self._persist_locked()
                except LivePlanningJobRegistryPostCommitError:
                    # The intent is already committed on disk; proceed to stop
                    # the executor. The final terminalize below settles (and may
                    # surface) the indeterminate write.
                    pass
                except Exception:
                    runtime.snapshot = pre_call_snapshot
                    runtime.generation = pre_call_generation
                    runtime.prepared = pre_call_prepared
                    runtime.activation_operation = pre_call_activation_operation
                    runtime.cancel_pending = pre_call_cancel_pending
                    runtime.cancel_future = pre_call_cancel_future
                    runtime.cancel_drain_succeeded = pre_call_cancel_drain_succeeded
                    if future is not None and not future.done():
                        future.set_result(pre_call_snapshot)
                    raise
                self._changed.notify_all()
        if pending_runtime is not None:
            # A cancellation is already in flight. Join the same bounded cleanup —
            # no repeated side effects — and settle the outcome from that run.
            if pending_future is not None and not pending_future.done():
                return await pending_future
            operation_task = pending_runtime.operation_task
            if operation_task is None or operation_task.done():
                # The real operation has since stopped, so the pending
                # cancellation can now complete safely.
                async with self._lock:
                    pending_current = self._owned_locked(job_id, tenant_id)
                if pending_current is not None:
                    await self._finish(
                        pending_current,
                        LivePlanningJobState.CANCELLED,
                        stage="cancelled",
                        cancellation_requested=True,
                    )
                    return await self.get(job_id, tenant_id)
                return None
            return pending_runtime.snapshot
        # First cancellation: request the real work to stop, then wait for it
        # within the bounded budget. _run's CancelledError handler drains the
        # operation_task and records whether it actually stopped.
        if operation_task is not None and not operation_task.done():
            operation_task.cancel()
        task_done = task is None or task is asyncio.current_task() or task.done()
        if not task_done:
            task.cancel()
            done_tasks, _ = await asyncio.wait(
                (task,),
                timeout=self._cancel_wait_seconds + 0.1,
            )
            task_done = task in done_tasks
        post_commit_error: LivePlanningJobRegistryPostCommitError | None = None
        async with self._lock:
            current = self._owned_locked(job_id, tenant_id)
            if current is None:
                outcome: LivePlanningJobSnapshot | None = None
                drained = True
            elif not task_done:
                # The runner itself did not stop within the budget — fail closed
                # rather than publish a terminal label over running work.
                drained = False
            else:
                drained = current.cancel_drain_succeeded
                if drained is None:
                    # No runner existed (e.g. a prepared job): nothing to drain.
                    drained = True
        if current is None:
            outcome = None
        else:
            try:
                if drained:
                    await self._finish(
                        current,
                        LivePlanningJobState.CANCELLED,
                        stage="cancelled",
                        cancellation_requested=True,
                    )
                else:
                    await self._mark_cancel_stuck(current)
            except LivePlanningJobRegistryPostCommitError as exc:
                exc.job_id = job_id
                post_commit_error = exc
            except Exception:
                # P0-1: a pre-commit failure on the final terminalize must NOT
                # roll back to the pre-call RUNNING state — the real executor is
                # already stopped, so RUNNING would be a lie over a dead executor.
                # _finish / _mark_cancel_stuck already restored the durably
                # persisted cancel_pending snapshot; keep that recoverable
                # non-active isolation state and surface the write failure so a
                # same-key retry or cold restart completes the terminalization.
                outcome = current.snapshot
                if future is not None and not future.done():
                    future.set_result(outcome)
                raise
            outcome = current.snapshot
        if future is not None and not future.done():
            future.set_result(outcome)
        if post_commit_error is not None:
            raise post_commit_error
        return outcome

    async def wait_for_change(
        self,
        job_id: str,
        tenant_id: str,
        *,
        after_revision: int,
        timeout_seconds: float = 15,
    ) -> LivePlanningJobSnapshot | None:
        if after_revision < 0:
            raise ValueError("after_revision cannot be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        async def wait() -> LivePlanningJobSnapshot | None:
            async with self._changed:
                while True:
                    self._prune_locked(self._utc_now())
                    runtime = self._owned_locked(job_id, tenant_id)
                    if runtime is None:
                        return None
                    if runtime.snapshot.revision > after_revision:
                        return runtime.snapshot
                    await self._changed.wait()

        try:
            async with asyncio.timeout(timeout_seconds):
                return await wait()
        except TimeoutError:
            return await self.get(job_id, tenant_id)

    async def close(self) -> None:
        async with self._changed:
            if self._closed:
                return
            self._closed = True
            active = tuple(
                runtime
                for runtime in self._records.values()
                if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
            )
            tasks = tuple(runtime.task for runtime in active if runtime.task is not None)
            operation_tasks = tuple(
                runtime.operation_task
                for runtime in active
                if runtime.operation_task is not None
            )
            for runtime in active:
                self._terminalize_locked(
                    runtime,
                    LivePlanningJobState.CANCELLED,
                    stage="cancelled",
                    cancellation_requested=True,
                )
            self._persist_locked()
            self._changed.notify_all()
        for operation_task in operation_tasks:
            if not operation_task.done():
                operation_task.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=self._cancel_wait_seconds + 0.1)

    async def _run(self, runtime: _RuntimeJob, operation: LiveJobOperation) -> None:
        acquired_slot = False
        operation_task: asyncio.Task[dict[str, Any]] | None = None
        generation = runtime.generation
        try:
            remaining = runtime.deadline_monotonic - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await self._slots.acquire()
            acquired_slot = True
            await self._update_running(
                runtime,
                "interpreting_requirement",
                5,
                generation=generation,
            )
            report = _RegistryProgressReporter(self, runtime, generation)

            async def invoke_operation() -> dict[str, Any]:
                return await operation(report)

            operation_task = asyncio.create_task(
                invoke_operation(),
                name=f"tripchord:{runtime.snapshot.id}:operation",
            )
            runtime.operation_task = operation_task
            operation_task.add_done_callback(self._consume_task_result)
            remaining = runtime.deadline_monotonic - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait((operation_task,), timeout=remaining)
            if operation_task not in done:
                raise TimeoutError
            result = operation_task.result()
        except asyncio.CancelledError:
            # P0-1 bounded cleanup: request the operation to stop and wait within
            # the budget BEFORE any terminal label is published. The final
            # CANCELLED is owned by cancel(), which reads `cancel_drain_succeeded`
            # and only then terminalizes (or fails closed). Recording the drain
            # result here and re-raising keeps this task genuinely cancelled so
            # the real work and the registry record never disagree.
            runtime.cancel_drain_succeeded = await self._cancel_and_drain_operation(
                operation_task
            )
            raise
        except TimeoutError as exc:
            failure = _safe_failure_diagnostic(
                exc,
                code_override=LivePlanningSafeFailureCode.DEADLINE_EXCEEDED,
            )
            await self._finish(
                runtime,
                LivePlanningJobState.FAILED,
                stage="deadline_exceeded",
                error="TimeoutError: live planning job deadline exceeded",
                safe_failure=failure,
                expected_generation=generation,
            )
            await self._cancel_and_drain_operation(operation_task)
        except Exception as exc:
            # Exception messages may contain a raw user prompt, URL, quote or provider
            # payload. Expose only the class and a stable generic description.
            failure = _safe_failure_diagnostic(exc)
            await self._finish(
                runtime,
                LivePlanningJobState.FAILED,
                stage="failed",
                error=f"{type(exc).__name__}: live planning execution failed",
                safe_failure=failure,
                expected_generation=generation,
            )
        else:
            await self._finish(
                runtime,
                LivePlanningJobState.SUCCEEDED,
                stage="complete",
                result=result,
                expected_generation=generation,
            )
        finally:
            if acquired_slot:
                self._slots.release()

    async def _cancel_and_drain_operation(
        self,
        operation_task: asyncio.Task[dict[str, Any]] | None,
    ) -> bool:
        """Cancel the operation and wait within the bounded budget.

        Returns True when the operation is confirmed stopped (already done, or
        done within the budget) and False when it swallowed CancelledError and is
        still alive past the budget — the caller must then fail closed rather than
        publish a fake terminal label over running work."""
        if operation_task is None or operation_task.done():
            return True
        operation_task.cancel()
        done, _ = await asyncio.wait(
            (operation_task,),
            timeout=self._cancel_wait_seconds,
        )
        return operation_task in done

    @staticmethod
    def _consume_task_result(task: asyncio.Task[dict[str, Any]]) -> None:
        with suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _update_running(
        self,
        runtime: _RuntimeJob,
        stage: str,
        progress: int,
        *,
        generation: int,
    ) -> None:
        if not stage.strip() or len(stage) > 80:
            raise ValueError("live job stage must contain 1 to 80 characters")
        if not 0 <= progress < 100:
            raise ValueError("non-terminal live job progress must be between 0 and 99")
        async with self._changed:
            self._ensure_active_locked(runtime, generation)
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "state": LivePlanningJobState.RUNNING,
                    "stage": stage,
                    "progress": max(runtime.snapshot.progress, progress),
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            self._changed.notify_all()

    async def _update_pair_checkpoint(
        self,
        runtime: _RuntimeJob,
        checkpoint: LivePlanningPairCheckpoint,
        *,
        generation: int,
    ) -> None:
        validated = LivePlanningPairCheckpoint.model_validate(checkpoint)
        async with self._changed:
            self._ensure_active_locked(runtime, generation)
            existing = runtime.snapshot.pair_checkpoints
            if runtime.snapshot.request_sha256 is None or not secrets.compare_digest(
                validated.request_sha256,
                runtime.snapshot.request_sha256,
            ):
                raise ValueError("live pair checkpoint request SHA-256 does not match its job")
            if len(existing) >= 8:
                raise ValueError("live pair checkpoint capacity exceeded")
            if validated.sequence != len(existing) + 1:
                raise ValueError("live pair checkpoint sequence is not contiguous")
            if any(item.date_pair_id == validated.date_pair_id for item in existing):
                raise ValueError("live pair checkpoint date pair was already reported")
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "pair_checkpoints": (*existing, validated),
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            self._changed.notify_all()

    async def _update_model_trace_summary(
        self,
        runtime: _RuntimeJob,
        *,
        scope_id: str,
        scope_request_sha256: str,
        trace_count: int,
        success_count: int,
        failure_count: int,
        generation: int,
    ) -> None:
        if not secrets.compare_digest(scope_id, runtime.snapshot.id):
            raise ValueError("model trace scope id does not match its live job")
        if not self._valid_request_digest(scope_request_sha256):
            raise ValueError("model trace scope must be a lowercase request SHA-256")
        if min(trace_count, success_count, failure_count) < 0:
            raise ValueError("model trace counts cannot be negative")
        if trace_count != success_count + failure_count:
            raise ValueError("model trace success and failure counts must add up")
        async with self._changed:
            self._ensure_active_locked(runtime, generation)
            if runtime.model_trace_summary_reported:
                raise ValueError("model trace summary was already reported for this live job")
            if runtime.snapshot.request_sha256 is None or not secrets.compare_digest(
                scope_request_sha256,
                runtime.snapshot.request_sha256,
            ):
                raise ValueError("model trace scope SHA-256 does not match its job request")
            if trace_count < runtime.snapshot.model_trace_count:
                raise ValueError("model trace count cannot decrease within one job")
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "model_trace_scope_sha256": scope_request_sha256,
                    "model_trace_count": trace_count,
                    "model_trace_success_count": success_count,
                    "model_trace_failure_count": failure_count,
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            runtime.model_trace_summary_reported = True
            self._changed.notify_all()

    async def _update_source_terminal_events(
        self,
        runtime: _RuntimeJob,
        events: tuple[LiveSourceTerminalEvent, ...],
        *,
        generation: int,
    ) -> None:
        if not events:
            return
        validated = tuple(
            LiveSourceTerminalEvent.model_validate(event) for event in events
        )
        if len(validated) > 64:
            raise ValueError("live source terminal events exceed the capacity bound")
        async with self._changed:
            self._ensure_active_locked(runtime, generation)
            existing = runtime.snapshot.source_terminal_events
            existing_by_id = {item.source_task_id for item in existing}
            if any(item.source_task_id in existing_by_id for item in validated):
                raise ValueError("live source terminal events must be unique per source task")
            merged = (*existing, *validated)
            if len(merged) > 64:
                raise ValueError("live source terminal events exceed the capacity bound")
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "source_terminal_events": merged,
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            self._changed.notify_all()

    async def _mark_barrier_released(
        self,
        runtime: _RuntimeJob,
        barrier_released_at: datetime,
        *,
        generation: int,
    ) -> None:
        _aware(barrier_released_at, "barrier_released_at")
        async with self._changed:
            self._ensure_active_locked(runtime, generation)
            if runtime.snapshot.barrier_released_at is not None:
                return
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "barrier_released_at": barrier_released_at,
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            self._changed.notify_all()

    async def _finish(
        self,
        runtime: _RuntimeJob,
        state: LivePlanningJobState,
        *,
        stage: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        safe_failure: _SafeFailureDiagnostic | None = None,
        expected_generation: int | None = None,
        cancellation_requested: bool | None = None,
    ) -> None:
        async with self._changed:
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return
            if expected_generation is not None and runtime.generation != expected_generation:
                return
            if runtime.snapshot.cancellation_requested and state == LivePlanningJobState.SUCCEEDED:
                state = LivePlanningJobState.CANCELLED
                stage = "cancelled"
                result = None
            previous_snapshot = runtime.snapshot
            previous_generation = runtime.generation
            previous_prepared = runtime.prepared
            previous_activation_operation = runtime.activation_operation
            self._terminalize_locked(
                runtime,
                state,
                stage=stage,
                result=result,
                error=error,
                safe_failure=safe_failure,
                cancellation_requested=cancellation_requested,
            )
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError:
                # The terminalized state was already committed to disk; the
                # in-memory record matches it, so keep it and surface the
                # indeterminate terminal outcome to the caller.
                raise
            except Exception:
                # A pre-commit persist failure must leave the whole mutable record —
                # snapshot, generation, the prepared flag and the full
                # activation_operation body including every nested field —
                # byte-identical to the untouched disk file, so a same-process
                # retry and a cold restart observe the same facts.
                runtime.snapshot = previous_snapshot
                runtime.generation = previous_generation
                runtime.prepared = previous_prepared
                runtime.activation_operation = previous_activation_operation
                raise
            self._changed.notify_all()

    async def _mark_cancel_stuck(self, runtime: _RuntimeJob) -> None:
        """P0-1 fail-closed outcome when the operation did not stop within the
        bounded cancellation budget.

        The job stays NON-terminal with an externally visible ``cancel_pending``
        state and an explicit ``cancel_timed_out`` stage, so a caller is never
        told the job is cleanly cancelled while the operation may still be
        running. The generation bump isolates the still-alive operation from any
        further registry writes, and a cold restart fail-closes the record to
        ``restart_cancelled`` — the only terminal state that is guaranteed to
        appear after the operation's process is actually gone.
        """
        async with self._changed:
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return
            previous_snapshot = runtime.snapshot
            previous_generation = runtime.generation
            previous_prepared = runtime.prepared
            previous_activation_operation = runtime.activation_operation
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "cancel_pending": True,
                    "stage": "cancel_timed_out",
                    "error": (
                        "live planning operation did not stop within the bounded "
                        "cancellation budget; the job stays non-terminal and the "
                        "operation is isolated"
                    ),
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            runtime.generation += 1
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError:
                raise
            except Exception:
                runtime.snapshot = previous_snapshot
                runtime.generation = previous_generation
                runtime.prepared = previous_prepared
                runtime.activation_operation = previous_activation_operation
                raise
            self._changed.notify_all()

    def _complete_cancel_terminalize_locked(self, runtime: _RuntimeJob) -> None:
        """P0-1: idempotently terminalize a cancel_pending job whose executor has
        already stopped, without re-acquiring the registry lock.

        Called from the idempotent retry path while holding ``self._lock``. A
        pre-commit persist failure restores the durable cancel_pending snapshot
        (never the pre-cancel RUNNING state), so a later retry completes the same
        terminalization and the job is never reported as active over a dead
        executor."""
        if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            return
        previous_snapshot = runtime.snapshot
        previous_generation = runtime.generation
        previous_prepared = runtime.prepared
        previous_activation_operation = runtime.activation_operation
        self._terminalize_locked(
            runtime,
            LivePlanningJobState.CANCELLED,
            stage="cancelled",
            cancellation_requested=True,
        )
        try:
            self._persist_locked()
        except LivePlanningJobRegistryPostCommitError:
            raise
        except Exception:
            runtime.snapshot = previous_snapshot
            runtime.generation = previous_generation
            runtime.prepared = previous_prepared
            runtime.activation_operation = previous_activation_operation
            raise

    async def _ensure_active(self, runtime: _RuntimeJob, generation: int) -> None:
        async with self._lock:
            self._ensure_active_locked(runtime, generation)

    def _ensure_active_locked(self, runtime: _RuntimeJob, generation: int) -> None:
        if (
            runtime.generation == generation
            and runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
            and not runtime.snapshot.cancellation_requested
            and asyncio.get_running_loop().time() >= runtime.deadline_monotonic
        ):
            raise TimeoutError("live planning job deadline exceeded")
        if (
            runtime.generation != generation
            or runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES
            or runtime.snapshot.cancellation_requested
        ):
            raise LivePlanningJobInactiveError(
                "live planning job generation is no longer active"
            )

    def _terminalize_locked(
        self,
        runtime: _RuntimeJob,
        state: LivePlanningJobState,
        *,
        stage: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        safe_failure: _SafeFailureDiagnostic | None = None,
        cancellation_requested: bool | None = None,
    ) -> None:
        if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            return
        now = self._utc_now()
        updates: dict[str, Any] = {
            "state": state,
            "stage": stage,
            "progress": 100,
            "result": result,
            "error": error,
            "safe_failure_code": safe_failure.code if safe_failure is not None else None,
            "safe_failure_details": (
                safe_failure.details if safe_failure is not None else None
            ),
            "safe_failure_details_digest": (
                safe_failure.details_digest if safe_failure is not None else None
            ),
            "expires_at": now + self._terminal_ttl,
            "revision": runtime.snapshot.revision + 1,
            "updated_at": now,
            "cancel_pending": False,
        }
        if cancellation_requested is not None:
            updates["cancellation_requested"] = cancellation_requested
        runtime.snapshot = runtime.snapshot.model_copy(update=updates)
        runtime.cancel_pending = False
        runtime.generation += 1
        # A terminalized job can no longer be an un-activated prepared record; the
        # on-disk invariant requires a prepared record to be QUEUED, so clearing
        # this here keeps every persist (cancel / close / restore) loadable.
        runtime.prepared = False
        if (
            state == LivePlanningJobState.CANCELLED
            and runtime.activation_operation is not None
            and runtime.activation_operation.get("phase")
            not in {"committed", "cancelled"}
        ):
            # Reassign a fresh operation object instead of mutating the existing
            # dict in place so a caller that snapshots the mutable record before
            # this call can restore the original body on a failed persist.
            runtime.activation_operation = {
                **runtime.activation_operation,
                "phase": "cancelled",
            }

    def _make_capacity_locked(self) -> None:
        if len(self._records) < self._capacity:
            return
        terminal = [
            item
            for item in self._records.values()
            if item.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES
        ]
        if terminal:
            oldest = min(terminal, key=lambda item: item.snapshot.updated_at)
            self._remove_locked(oldest.snapshot.id)
        if len(self._records) >= self._capacity:
            raise LivePlanningJobCapacityError("live planning job capacity exceeded")

    def _prune_locked(self, now: datetime) -> None:
        expired = tuple(
            job_id
            for job_id, runtime in self._records.items()
            if (
                runtime.snapshot.expires_at is not None
                and runtime.snapshot.expires_at <= now
            )
            or (
                runtime.prepared
                and runtime.task is None
                and runtime.snapshot.deadline_at <= now
            )
        )
        for job_id in expired:
            self._remove_locked(job_id)
        if expired:
            self._persist_locked()

    def _remove_locked(self, job_id: str) -> None:
        self._records.pop(job_id, None)
        stale_keys = tuple(
            key for key, entry in self._idempotency.items() if entry.job_id == job_id
        )
        for key in stale_keys:
            self._idempotency.pop(key, None)

    def _owned_locked(self, job_id: str, tenant_id: str) -> _RuntimeJob | None:
        runtime = self._records.get(job_id)
        if runtime is None:
            return None
        supplied = self._tenant_partition(tenant_id)
        if not secrets.compare_digest(runtime.tenant_partition, supplied):
            return None
        return runtime

    @staticmethod
    def _tenant_partition(tenant_id: str) -> str:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()

    @classmethod
    def _idempotency_partition(cls, tenant_id: str, idempotency_key: str) -> str:
        tenant_partition = cls._tenant_partition(tenant_id)
        return hashlib.sha256(
            f"{tenant_partition}\0{idempotency_key}".encode()
        ).hexdigest()

    @staticmethod
    def _valid_request_digest(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    def _utc_now(self) -> datetime:
        value = self._now()
        return _aware(value, "now").astimezone(UTC)
