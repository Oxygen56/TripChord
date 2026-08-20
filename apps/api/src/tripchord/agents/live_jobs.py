from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, Self, cast
from uuid import uuid4

from pydantic import Field, ValidationError, field_validator, model_validator

from tripchord.domain.common import DomainModel

logger = logging.getLogger(__name__)


def _linux_process_group_pids(pgid: int) -> tuple[int, ...]:
    """Return the current PIDs in a Linux process group."""
    if sys.platform != "linux":
        return ()
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return ()
    pids: list[int] = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="ascii")
            _, remainder = stat_text.split(") ", 1)
            fields = remainder.split()
            if len(fields) >= 3 and int(fields[2]) == pgid:
                pids.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            continue
    return tuple(pids)


def _linux_group_has_live_member(pgid: int) -> bool | None:
    """Return whether a PGID contains an executable member.

    Linux keeps unreaped SIGKILLed children visible to ``killpg(pgid, 0)`` as
    zombies.  They cannot produce side effects and must not block terminal
    cleanup. ``None`` means procfs gave no authoritative answer; callers then
    fail closed using the signal probe.
    """
    if sys.platform != "linux":
        return None
    states = _linux_process_group_states(pgid)
    if states is None or not states:
        return None
    return any(state != "Z" for state in states)


def _linux_process_group_states(pgid: int) -> tuple[str, ...] | None:
    """Read every visible member state for one PGID in a single procfs pass."""
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return None
    states: list[str] = []
    try:
        entries = tuple(proc_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="ascii")
            _, remainder = stat_text.split(") ", 1)
            fields = remainder.split()
            if len(fields) < 3:
                return None
            if int(fields[2]) == pgid:
                states.append(fields[0])
        except FileNotFoundError:
            continue
        except (PermissionError, OSError, ValueError):
            return None
    return tuple(states)


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

# C-145 P0 supplement: a DURABLE pending outcome is the terminal intent of a
# failed-closed cancel/close/deadline cleanup — the caller only ever chooses
# CANCELLED/cancelled or FAILED/deadline_exceeded. SUCCEEDED (or any
# non-terminal value) would terminalize a live record to a label the caller
# never chose, so the loader rejects it fail-closed instead.
_PENDING_TERMINAL_ALLOWED_STATES = frozenset(
    {
        LivePlanningJobState.CANCELLED,
        LivePlanningJobState.FAILED,
    }
)

# The EXACT field set ``to_persisted`` writes for every durable pending
# outcome. The decoder accepts only this precise set — a missing field or any
# unknown/extra field is corruption and is rejected fail-closed, never patched.
_PENDING_TERMINAL_PERSISTED_FIELDS = frozenset(
    {
        "state",
        "stage",
        "result",
        "error",
        "safe_failure_code",
        "safe_failure_details",
        "safe_failure_details_digest",
        "cancellation_requested",
    }
)

# C-145 P0 supplement: the explicit stage of a non-terminal record quarantined
# by an earlier cold start. It must stay quarantined across close()/cancel()/
# same-key paths and consecutive cold boots — never guessed to CANCELLED/FAILED.
_ISOLATED_AMBIGUOUS_CANCEL_STAGE = "isolated_ambiguous_cancel"

# C-146 P0 supplement (third P0): the explicit observable stage of a deadline
# record whose FIRST durable FAILED/deadline_exceeded intent could not be
# committed (every bounded pre-commit attempt failed). The executor and the
# admission slot stay untouched until the intent commits; the bounded cleanup
# owner re-commits it, then drains the operation and terminalizes.
_DEADLINE_INTENT_PERSIST_PENDING_STAGE = "deadline_intent_persist_pending"

# C-146 P0 supplement (fourth P0): the explicit NON-terminal quarantine stage a
# record enters once the bounded STATE budget (attempts / total wall-clock) for
# the first durable intent is exhausted under a permanent storage failure. The
# in-memory intent and isolation stay recoverable, but the registry stops the
# burst retry — no FAILED/CANCELLED is ever written or claimed from memory, and
# the record no longer counts against executable active capacity. Store
# recovery auto-reconciles it (persist quarantine + target facts, then release
# quotas).
_QUARANTINE_INTENT_UNCOMMITTED_STAGE = "quarantine_intent_uncommitted"

# C-146 P0 supplement (fourth P0): the explicit NON-terminal quarantine stage a
# record enters once the EXECUTION budget (absolute deadline + grace) is
# reached and the real operation did not provably stop. The executor is
# hard-stopped/detached within that absolute bound — its registry-facing side
# effects are isolated by the generation bump, the admission slot is released
# only in a bounded way, and the orphan is counted against the quarantine
# quota. Never a fabricated FAILED/CANCELLED label.
_QUARANTINE_HARD_STOPPED_STAGE = "quarantine_hard_stopped"

# C-146 hard-stop gate (12e35d45 门 1): the explicit NON-terminal quarantine
# stage for an IN-PROCESS operation that cannot be proven dead. A worker
# subprocess is SIGKILLed and waitpid-confirmed, so its death (and the freeze of
# any external probe it was writing) is a hard fact and it lands on
# ``quarantine_hard_stopped``. An in-process coroutine that swallows
# ``CancelledError`` past the bounded budget can NOT be proven dead — calling
# that a "hard stop" would be a lie — so it lands here instead, keeps
# ``hard_stopped`` False, keeps its admission slot, and only ever releases it
# (or settles) once the real task is confirmed done.
_QUARANTINE_ORPHAN_STAGE = "quarantine_orphan_in_process"

# Every explicit quarantine stage. Records in these stages are NON-terminal,
# excluded from executable active capacity, and governed by the bounded
# quarantine quota + retention.
_QUARANTINE_STAGES = frozenset(
    {
        _ISOLATED_AMBIGUOUS_CANCEL_STAGE,
        _QUARANTINE_INTENT_UNCOMMITTED_STAGE,
        _QUARANTINE_HARD_STOPPED_STAGE,
        _QUARANTINE_ORPHAN_STAGE,
    }
)


def _default_worker_module() -> str:
    """Absolute path of the ``live_job_worker`` script the registry spawns.

    The worker runs by file path (not ``-m``), so it needs no import machinery
    of its own; it loads the command's entry module by path in turn."""
    try:
        import tripchord.agents.live_job_worker as worker_module
    except ImportError:
        return "tripchord.agents.live_job_worker"
    return worker_module.__file__ or "tripchord.agents.live_job_worker"


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


@dataclass(frozen=True)
class _PendingTerminalOutcome:
    """C-145 P1/P0: the durable terminal intent of a failed-closed cleanup.

    Recorded ATOMICALLY WITH the stuck isolation so the target outcome survives
    a pre-commit failure (neither is on disk) and a cold restart (both are). Once
    the real operation stops on its own, the cleanup owner terminalizes the
    record to exactly this outcome — never a guessed CANCELLED/FAILED label —
    without an extra retry/close/cold start. The caller (cancel/close/deadline)
    supplies the unambiguous target: cancel/close → CANCELLED/cancelled,
    deadline → FAILED/deadline_exceeded with the safe-failure diagnostic."""

    state: LivePlanningJobState
    stage: str
    result: dict[str, Any] | None = None
    error: str | None = None
    safe_failure: _SafeFailureDiagnostic | None = None
    cancellation_requested: bool | None = None

    def to_persisted(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "stage": self.stage,
            "result": self.result,
            "error": self.error,
            "safe_failure_code": (
                self.safe_failure.code.value if self.safe_failure is not None else None
            ),
            "safe_failure_details": (
                self.safe_failure.details.model_dump(mode="json")
                if self.safe_failure is not None
                else None
            ),
            "safe_failure_details_digest": (
                self.safe_failure.details_digest if self.safe_failure is not None else None
            ),
            "cancellation_requested": self.cancellation_requested,
        }

    @classmethod
    def from_persisted(
        cls,
        raw: Any,
        *,
        legacy_v3_none_cancellation: bool = False,
    ) -> _PendingTerminalOutcome:
        """Strictly decode a durable pending outcome, or raise ``ValueError``.

        ``legacy_v3_none_cancellation`` is set ONLY by the v3 loader for the
        exact historical combination the pre-P0 ``_mark_cancel_stuck`` producer
        wrote on disk (a stuck isolation with ``cancel_pending`` and a pending
        outcome whose ``cancellation_requested`` defaulted to ``None``). That
        bounded migration accepts the ``None`` and converts it to the explicit
        ``True`` semantics; every new-schema ``None`` / foreign / tampered value
        is rejected fail-closed (the new producer always writes exactly
        ``True``).

        The decoder accepts ONLY the exact field set the producer writes
        (``to_persisted``), forbids a SUCCEEDED target, and precisely binds the
        legal state/stage/result/error/safe_failure/cancellation combination:
        cancel/close → CANCELLED/cancelled with no result/error/safe failure;
        deadline → FAILED/deadline_exceeded with a complete safe-failure whose
        digest recomputes consistently from the stored code + details. A
        malformed payload (unknown state, wrong stage, missing/extra fields,
        dangling or tampered safe-failure fields, illegal result/error/cancel
        flag) is treated as corruption — the loader rejects it fail-closed
        instead of silently rewriting the record to a guessed label."""
        if not isinstance(raw, dict):
            raise ValueError("pending terminal outcome is not an object")
        if set(raw) != _PENDING_TERMINAL_PERSISTED_FIELDS:
            raise ValueError("pending terminal outcome has an invalid field set")
        state_raw = raw["state"]
        stage = raw["stage"]
        if not isinstance(state_raw, str) or not isinstance(stage, str) or not stage:
            raise ValueError("pending terminal outcome is missing its target")
        try:
            state = LivePlanningJobState(state_raw)
        except ValueError as exc:
            raise ValueError("pending terminal outcome has an unknown state") from exc
        # C-145 P0 supplement: a durable pending outcome can only target an
        # ALLOWED terminal state — SUCCEEDED (or any non-terminal value) would
        # terminalize a live record to a label the caller never chose, so the
        # loader rejects it fail-closed instead.
        if state not in _PENDING_TERMINAL_ALLOWED_STATES:
            raise ValueError("pending terminal outcome has an unforgeable state")
        # C-145 P0 supplement: the stage is part of the contract. A CANCELLED
        # intent must carry exactly stage ``cancelled`` and a FAILED intent
        # exactly ``deadline_exceeded``; any other stage is corruption and is
        # rejected fail-closed, never patched.
        expected_stage = (
            "cancelled" if state is LivePlanningJobState.CANCELLED else "deadline_exceeded"
        )
        if stage != expected_stage:
            raise ValueError("pending terminal outcome has an inconsistent stage")
        result = raw["result"]
        if result is not None:
            # A durable terminal intent never carries a success result — only a
            # real SUCCEEDED run publishes one, and SUCCEEDED is never a pending
            # target. Any non-null result here is corruption.
            raise ValueError("pending terminal outcome has an illegal result")
        error = raw["error"]
        if state is LivePlanningJobState.CANCELLED:
            # cancel/close intents carry no error.
            if error is not None:
                raise ValueError("pending terminal outcome has an illegal error")
        else:
            # A FAILED/deadline_exceeded intent ALWAYS carries the timeout error.
            if not isinstance(error, str) or not error:
                raise ValueError("pending terminal outcome is missing its failure error")
        cancellation_requested = raw["cancellation_requested"]
        if cancellation_requested is not True:
            # C-146 P0 supplement (P0-4 / b119): a REAL old-v3 file may carry
            # ``None`` here from the historical ``_mark_cancel_stuck`` default
            # path. ONLY the loader's exact legacy combination (same field set +
            # the old producer's stuck isolation + untampered identity) may
            # migrate the None to the explicit True semantics; every new-schema
            # None or foreign value is corruption and rejected fail-closed.
            if cancellation_requested is None and legacy_v3_none_cancellation:
                cancellation_requested = True
            else:
                raise ValueError("pending terminal outcome has an invalid cancellation flag")
        safe_failure: _SafeFailureDiagnostic | None = None
        code_raw = raw["safe_failure_code"]
        details_raw = raw["safe_failure_details"]
        digest_raw = raw["safe_failure_details_digest"]
        if state is LivePlanningJobState.CANCELLED:
            # cancel/close intents never carry a safe-failure diagnostic; any
            # dangling details/digest here is corruption.
            if code_raw is not None or details_raw is not None or digest_raw is not None:
                raise ValueError("pending terminal outcome has dangling safe failure fields")
        else:
            # A FAILED/deadline_exceeded intent ALWAYS carries a complete,
            # consistent safe-failure diagnostic — the digest must recompute
            # from the stored code + details, never be accepted from a
            # hand-written blob.
            if (
                not isinstance(code_raw, str)
                or not isinstance(digest_raw, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest_raw) is None
                or not isinstance(details_raw, dict)
            ):
                raise ValueError("pending terminal outcome has an invalid safe failure")
            try:
                code = LivePlanningSafeFailureCode(code_raw)
                details = LivePlanningSafeFailureDetails.model_validate(details_raw)
            except (ValueError, ValidationError) as exc:
                raise ValueError("pending terminal outcome has an invalid safe failure") from exc
            if _safe_failure_details_digest(code, details) != digest_raw:
                raise ValueError("pending terminal outcome safe failure digest is inconsistent")
            safe_failure = _SafeFailureDiagnostic(
                code=code,
                details=details,
                details_digest=digest_raw,
            )
        return cls(
            state=state,
            stage=stage,
            result=result,
            error=error,
            safe_failure=safe_failure,
            cancellation_requested=cancellation_requested,
        )


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
            if isinstance(raw_type, str) and _SAFE_VALIDATION_TYPE_PATTERN.fullmatch(raw_type)
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
            if isinstance(raw_title, str) and _SAFE_VALIDATION_MODEL_PATTERN.fullmatch(raw_title)
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
    # Keep the item boundary aligned with the durable snapshot capacity; the
    # registry additionally enforces the same cap at the lifecycle boundary.
    sequence: int = Field(ge=1, le=400)
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
                    item.isoformat() if isinstance(item, (date, datetime)) else str(item)
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


def _without_result(snapshot: LivePlanningJobSnapshot) -> dict[str, Any]:
    """Durable snapshot with a live-process terminal ``result`` excluded.

    See the C-146 P0-6 comment in ``_persist_locked``: the byte cap guards the
    bounded identity/metadata of a record, never the unbounded user-facing
    planning payload. Only a NON-None ``result`` (the live-process output) is
    excluded — a ``None`` result serializes exactly as before, so non-terminal
    records keep the byte-identical persisted shape. Serializing a copy (rather
    than mutating the live snapshot in place) keeps the status endpoint's view
    of ``result`` intact.
    """
    payload = snapshot.model_dump(mode="json")
    if payload.get("result") is not None:
        payload.pop("result", None)
    return payload


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
        # Flexible-date runs persist one checkpoint per executed date pair;
        # the public runner accepts up to 400 pairs, so the durable snapshot
        # must not silently cap the internal 66-pair benchmark at eight.
        max_length=400,
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

    _validate_created_at = field_validator("created_at")(lambda value: _aware(value, "created_at"))
    _validate_updated_at = field_validator("updated_at")(lambda value: _aware(value, "updated_at"))
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
        if self.request_sha256 is not None and self.model_trace_scope_sha256 != self.request_sha256:
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
        if any(item.request_sha256 != self.request_sha256 for item in self.pair_checkpoints):
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


class LivePlanningJobCancellationPendingError(RuntimeError):
    """Raised when an idempotent retry hits a cancel_pending job whose real
    operation has not yet stopped. The cancellation is still in flight, so the
    retry must fail closed instead of reusing a running executor as an active
    job or terminalizing cancelled over live work. Carries the original
    ``job_id`` so the HTTP layer can return a queryable/retryable conflict
    instead of a bare 500 (P0-2)."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.job_id: str | None = None


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


@dataclass(frozen=True)
class LiveJobWorkerCommand:
    """An operation that must run inside a real subprocess worker.

    The registry executes this in a fresh OS process (``live_job_worker``) so
    the hard-stop watchdog can PROVE the operation's external side effects
    permanently froze: SIGKILL + waitpid confirms the real PID is dead, and any
    external probe the worker was appending stops growing. ``module_path`` is
    the absolute path of the module defining ``entry`` (loaded by file path, so
    any self-contained module-level function can run here), ``entry`` is the
    callable qualname, ``args`` its keyword arguments, and ``probe_path`` — when
    set — is injected into the call so the worker can append an unobservable,
    registry-independent side-effect probe. In-process coroutines that swallow
    cancellation can never be proven dead; a worker subprocess can.
    """

    module_path: str
    entry: str
    args: dict[str, Any] = field(default_factory=dict)
    probe_path: str | None = None
    # Parent-only import hook. It is NEVER serialized into worker argv or the
    # durable registry: after the worker returns validated JSON, the parent uses
    # this to atomically import process-local artifacts (notably live-run cache
    # entries) and replace worker-local handles before publishing the result.
    result_importer: Callable[
        [dict[str, Any]], Awaitable[dict[str, Any]]
    ] | None = field(default=None, repr=False, compare=False)


class _SubprocessWorkerHandle:
    """An owned worker subprocess (real PID / PGID) plus death-confirmation state.

    The worker runs as the leader of its OWN process group (``os.setsid()`` in
    the script and ``start_new_session=True`` at spawn), so any grandchild it
    forks shares the group. ``kill_and_confirm`` SIGKILLs the WHOLE GROUP and —
    after the leader is reaped via ``wait()`` — confirms the group has no
    executable member within a bounded budget. On Linux an unreaped zombie can
    keep ``killpg(pgid, 0)`` successful even though it cannot run; procfs state
    distinguishes that case. A dead parent worker therefore never leaves a live
    grandchild behind, and the registry PROVES the operation's external side
    effects froze BEFORE it releases any permit / opens new admission / writes a
    terminal label.

    Durable identity: the handle also knows the unique marker nonce and the
    registry-side marker file path, so a cold start can re-discover an orphaned
    worker (parent API SIGKILLed) and authenticate the PGID before cleaning it.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        probe_path: str | None = None,
        marker: str = "",
        marker_file: Path | None = None,
    ) -> None:
        self.process = process
        self.probe_path = probe_path
        self.pid = process.pid
        # ``start_new_session`` makes the worker a session leader: PGID == PID.
        self.pgid = process.pid
        self.marker = marker
        self.marker_file = marker_file
        self.death_confirmed = False

    def group_alive(self) -> bool:
        """True while an executable process remains in the worker's group."""
        linux_live = _linux_group_has_live_member(self.pgid)
        if linux_live is not None:
            return linux_live
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # A group we cannot signal still exists — treat it as alive.
            return True
        return True

    async def kill_and_confirm(self, timeout: float) -> bool:
        """SIGKILL the whole group and confirm every member is dead in ``timeout``.

        Returns True only when the leader is reaped and no executable group
        member remains. The executor (and any grandchild it forked) is then
        provably unable to produce further external side effects."""
        if self.process.returncode is not None and not self.group_alive():
            self.death_confirmed = True
            return True
        if self.group_alive():
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pgid, signal.SIGKILL)  # whole tree — no grandchild escape
        deadline = asyncio.get_running_loop().time() + timeout
        # Reap the leader (waitpid) within the budget.
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(self.process.wait(), timeout=remaining)
        except TimeoutError:
            return False
        # Confirm every descendant stopped executing. Linux may retain a dead
        # descendant as a zombie until its external parent reaps it.
        while asyncio.get_running_loop().time() < deadline:
            if not self.group_alive():
                self.death_confirmed = True
                return True
            await asyncio.sleep(0.01)
        return False


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
        operation: LiveJobOperation | LiveJobWorkerCommand,
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
        # P0-1 admission binding: whether THIS runtime currently owns an
        # admission permit. True from the moment the runner acquires the
        # semaphore until the REAL operation task is confirmed done (or the
        # runner never handed the permit to an operation). This is what ties
        # capacity to the live operation lifecycle instead of the runner's exit.
        self.slot_held = False
        # C-145 P1: the unique, waitable cleanup owner of this runtime's pending
        # terminal outcome. Set once a cancel/close/deadline cleanup fails
        # closed; the owner (joined by the operation done-callback) auto-collects
        # the record to a terminal state when the real operation stops on its
        # own — without an extra retry/close/cold start.
        self.cleanup_owner: asyncio.Task[None] | None = None
        self.pending_terminal: _PendingTerminalOutcome | None = None
        # C-145 P0: bounded-retry bookkeeping for the cleanup owner. A single
        # owner invocation retries the terminal persist ``budget`` times with a
        # short backoff; when the budget is exhausted it keeps the DURABLE
        # pending outcome and arms the registry reaper, which re-spawns the
        # owner after ``cleanup_next_retry_monotonic`` until the terminal commit
        # succeeds or the process shuts down.
        self.cleanup_retry_round = 0
        self.cleanup_next_retry_monotonic = 0.0
        # C-146 P0 supplement (third P0): the deadline handler created the
        # FAILED/deadline_exceeded durable intent IN MEMORY but could not commit
        # it (every bounded pre-commit attempt failed). While this flag is set,
        # the real operation and the admission slot are left untouched — the
        # cleanup owner re-commits the intent before any cancel/drain.
        self.intent_persist_pending = False
        # C-146 P0 supplement (fourth P0): bounded STATE budget bookkeeping for
        # the first durable intent. The total attempts and the wall-clock since
        # the first attempt bound the burst retry; when either budget is
        # exhausted the record is quarantined non-terminal and the burst stops.
        self.intent_persist_attempts = 0
        self.intent_persist_started_monotonic = 0.0
        # C-146 P0 supplement (fourth P0): EXECUTION budget. The absolute bound
        # at which the real executor must be hard-stopped/quarantined regardless
        # of storage recovery is ``deadline_monotonic + grace``. The single
        # hard-stop watchdog isolates a live operation once this passes.
        self.hard_stop_monotonic = 0.0
        self.hard_stopped = False
        # C-146 P0-3: True while the watchdog's concurrent hard-stop wrapper is
        # actively stopping this runtime, so the re-scan never double-processes a
        # due operation. Cleared (and the watchdog re-awoken) when the wrapper
        # finishes — success, deferral or exception alike.
        self.hard_stop_in_flight = False
        # C-146 P0-7: the watchdog reached this runtime's deadline but the
        # quarantine quota was full, so the stop was REFUSED before any
        # irreversible kill. The record stays non-quarantined and running;
        # ``hard_stop_next_attempt_monotonic`` bounds the retry so the watchdog
        # re-attempts once capacity frees (or the backoff elapses) instead of
        # busy-spinning every scan.
        self.hard_stop_deferred = False
        self.hard_stop_next_attempt_monotonic = 0.0
        # C-146 P0-4 (RETURN 7de8cf3e): SATURATING + fixed-window-bounded retry
        # bookkeeping for a hard-stop death-confirmation that KEEPS failing
        # (kill/confirm exception or death-not-confirmed). The retry round
        # drives an exponential backoff capped at 0.5s; the per-window call
        # count is a hard upper bound so a persistently failing confirm can
        # never hot-loop the watchdog. ``hard_stop_next_attempt_monotonic`` is
        # the wake time the watchdog loop sleeps on.
        self.hard_stop_confirm_retry_round = 0
        self.hard_stop_confirm_window_start_monotonic = 0.0
        self.hard_stop_confirm_window_calls = 0
        # C-146 P0-7: a quarantine slot RESERVED atomically (same lock domain as
        # the conversion) BEFORE any stop/kill side effect, so a capacity
        # rejection never happens after an irreversible action and a concurrent
        # sibling hard-stop can never consume the last slot between the check
        # and the conversion. Counted by ``_quarantine_capacity_available_locked``
        # alongside ``quarantined`` and released when the record converts or
        # terminalizes.
        self.hard_stop_quarantine_reserved = False
        # C-146 P0 supplement (fourth P0) / b119: quarantine membership. A
        # quarantined record is NON-terminal, does NOT count against executable
        # active capacity, is governed by the bounded quarantine quota +
        # retention, and keeps a minimal durable tombstone after reclamation so
        # a same-key request still fails closed.
        self.quarantined = False
        self.quarantine_stage: str | None = None
        # C-146 P0 supplement (fourth P0): has the durable quarantine fact been
        # committed? For a quarantined record WITHOUT a pending terminal outcome
        # (hard-stopped / ambiguous-isolated) the reconcile is complete exactly
        # once the quarantine facts are on disk — no terminal settlement exists.
        self.quarantine_reconciled = False
        # C-146 hard-stop gate (12e35d45 门 1/门 2): the real worker subprocess
        # this runtime owns when its operation is a ``LiveJobWorkerCommand``.
        # Death is provable via ``kill_and_confirm`` (SIGKILL + waitpid); an
        # in-process coroutine has no such proof and is never called a hard
        # stop.
        self.worker_handle: _SubprocessWorkerHandle | None = None
        # C-146 hard-stop gate (12e35d45 门 2): the DURABLE worker identity —
        # the process-group id, the unique marker nonce and the probe path — is
        # persisted when the worker starts so a cold start / parent-API crash
        # can re-discover, authenticate and clean a real orphan even after the
        # in-memory handle is gone and even after a PID/PGID was reused.
        self.worker_pgid: int | None = None
        self.worker_marker: str | None = None
        self.worker_probe: str | None = None
        # Parent-validated, signed formal execution capability handed to a real
        # worker only at the prepared-job activation boundary. It is never part
        # of the initial command (which is built before challenge issuance), and
        # is not persisted because a parent restart kills/isolates the old worker
        # rather than resuming its process-local execution scope.
        self.worker_execution_capability: dict[str, Any] | None = None
        # C-146 P0-3: durable per-identity orphan facts persisted across cold
        # starts. ``orphan_authenticated`` records whether a cold boot ever
        # AUTHENTICATED this durable worker group via its marker nonce (False
        # covers both "marker not found in the group" AND "the ps query itself
        # failed"); ``orphan_death_confirmed`` records whether the authenticated
        # group was ever CONFIRMED dead (whole group ESRCH within the confirm
        # budget). None = this identity has never been cold-boot checked. A
        # cold start may settle a record ONLY when BOTH are provably True;
        # auth/ps failure keeps the record isolated (orphan quarantine) so
        # consecutive cold starts can re-check the group.
        self.orphan_authenticated: bool | None = None
        self.orphan_death_confirmed: bool | None = None
        # C-146 P0-2: worker-EXIT confirmation retry state (in-memory). When
        # the worker leader exits (clean OR non-zero) but the whole process
        # group cannot yet be proven empty, the confirmation AUTO-RETRIES on a
        # SATURATING + fixed-window-bounded backoff while the job stays
        # non-terminal, keeps its durable worker identity and its admission
        # permit. These counters mirror the hard-stop confirm budget windows.
        self.worker_exit_confirm_retry_round: int = 0
        self.worker_exit_confirm_window_start_monotonic: float = 0.0
        self.worker_exit_confirm_window_calls: int = 0


class _IdempotencyEntry:
    def __init__(
        self,
        *,
        job_id: str,
        request_digest: str,
        defer_start: bool | None = None,
        legacy_isolated: bool = False,
        updated_at: datetime | None = None,
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
        # C-146 hard-stop gate (12e35d45 门 5): the tombstone/identity
        # ``updated_at``. Set when the binding is created and when a quarantined
        # record's reclamation promotes it to a durable isolated tombstone. A
        # missing value (pre-gate legacy file) is preserved as None so the
        # bounded tombstone-TTL sweep NEVER reclaims it — old tombstones stay
        # bounded by ``idempotency_capacity`` instead.
        self.updated_at = updated_at


class LivePlanningJobRegistry:
    """Bounded control plane with fail-closed restart tombstones when persisted."""

    def __init__(
        self,
        *,
        capacity: int = 16,
        max_running: int = 1,
        terminal_ttl: timedelta = timedelta(minutes=30),
        cancel_wait_seconds: float = 10,
        cancel_isolation_persist_attempts: int = 3,
        cleanup_retry_backoff_seconds: float = 0.02,
        # C-146 P0 supplement (fourth P0): the absolute EXECUTION budget is the
        # deadline plus this grace. The single hard-stop watchdog quarantines a
        # live operation once the bound passes, regardless of storage recovery.
        execution_hard_stop_grace_seconds: float = 5.0,
        # C-146 P0 supplement (fourth P0): the bounded STATE budget for the
        # first durable intent. A burst of persist attempts beyond either cap
        # stops and the record is quarantined non-terminal. Defaults are
        # deliberately generous so a short transient write failure never
        # quarantines a record whose intent commits a moment later.
        intent_persist_budget_attempts: int = 30,
        intent_persist_wallclock_budget_seconds: float = 5.0,
        # C-146 P0 supplement (fourth P0) / b119: the bounded quarantine quota
        # and retention. Quarantined (non-terminal) records are NOT counted
        # against executable active capacity; they are governed by their own
        # quota and are reclaimed after the retention window, leaving a minimal
        # durable tombstone so a same-key request still fails closed.
        quarantine_capacity: int = 8,
        quarantine_retention: timedelta = timedelta(hours=6),
        # C-146 hard-stop gate (12e35d45 门 1/门 2): the bounded budget for
        # confirming a real executor is dead AFTER SIGKILL / task-cancel. The
        # watchdog's per-operation hard-stop latency is bounded by this, so one
        # stubborn operation can never delay another past its own deadline+grace.
        hard_stop_confirm_seconds: float = 1.0,
        # C-146 hard-stop gate (12e35d45 门 1): the interpreter and worker module
        # used to run a ``LiveJobWorkerCommand``. Tests override these to pin the
        # exact python and the absolute worker script path.
        worker_python: str = "",
        worker_module: str = "",
        # C-146 hard-stop gate (12e35d45 门 5): triple hard caps for the state
        # file, the idempotency-identity collection and the durable tombstones.
        # idempotency_capacity bounds the identity/tombstone cardinality;
        # state_max_bytes bounds the serialized file; tombstone_ttl bounds how
        # long a dangling isolated tombstone survives before the bounded,
        # memory=disk reclamation sweep reclaims it.
        idempotency_capacity: int = 256,
        state_max_bytes: int = 1_048_576,
        tombstone_ttl: timedelta = timedelta(days=30),
        now: Callable[[], datetime] | None = None,
        state_path: Path | None = None,
        # C-146 P0-7: bounded backoff between two hard-stop attempts when the
        # stop was REFUSED for a full quarantine quota. Never a busy-spin.
        hard_stop_defer_retry_seconds: float = 1.0,
        # C-146 P0-4 (RETURN 7de8cf3e): SATURATING + fixed-window-bounded
        # retry for a hard-stop death-confirmation that KEEPS failing
        # (kill/confirm exception or death-not-confirmed). The exponential
        # backoff saturates (capped); the per-window call-count is a hard upper
        # bound so a persistently failing confirm can never hot-loop the
        # watchdog. Both are test-controllable.
        hard_stop_confirm_budget_window_seconds: float = 2.0,
        hard_stop_confirm_budget_window_calls: int = 8,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if not 1 <= max_running <= capacity:
            raise ValueError("max_running must be between one and capacity")
        if terminal_ttl <= timedelta(0):
            raise ValueError("terminal_ttl must be positive")
        if cancel_wait_seconds <= 0:
            raise ValueError("cancel_wait_seconds must be positive")
        if cancel_isolation_persist_attempts < 1:
            raise ValueError("cancel_isolation_persist_attempts must be at least one")
        if cleanup_retry_backoff_seconds <= 0:
            raise ValueError("cleanup_retry_backoff_seconds must be positive")
        if execution_hard_stop_grace_seconds < 0:
            raise ValueError("execution_hard_stop_grace_seconds must be non-negative")
        if intent_persist_budget_attempts < 1:
            raise ValueError("intent_persist_budget_attempts must be at least one")
        if intent_persist_wallclock_budget_seconds <= 0:
            raise ValueError("intent_persist_wallclock_budget_seconds must be positive")
        if quarantine_capacity < 1:
            raise ValueError("quarantine_capacity must be positive")
        if quarantine_retention <= timedelta(0):
            raise ValueError("quarantine_retention must be positive")
        if hard_stop_confirm_seconds <= 0:
            raise ValueError("hard_stop_confirm_seconds must be positive")
        if idempotency_capacity < 1:
            raise ValueError("idempotency_capacity must be positive")
        if state_max_bytes < 1024:
            raise ValueError("state_max_bytes must be at least 1024")
        if tombstone_ttl <= timedelta(0):
            raise ValueError("tombstone_ttl must be positive")
        if hard_stop_confirm_budget_window_seconds <= 0:
            raise ValueError("hard_stop_confirm_budget_window_seconds must be positive")
        if hard_stop_confirm_budget_window_calls < 1:
            raise ValueError("hard_stop_confirm_budget_window_calls must be at least one")
        self._capacity = capacity
        self._terminal_ttl = terminal_ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._cancel_wait_seconds = cancel_wait_seconds
        self._cancel_isolation_persist_attempts = cancel_isolation_persist_attempts
        self._cleanup_retry_backoff_seconds = cleanup_retry_backoff_seconds
        self._execution_hard_stop_grace_seconds = execution_hard_stop_grace_seconds
        self._intent_persist_budget_attempts = intent_persist_budget_attempts
        self._intent_persist_wallclock_budget_seconds = intent_persist_wallclock_budget_seconds
        self._quarantine_capacity = quarantine_capacity
        self._quarantine_retention = quarantine_retention
        # C-146 hard-stop gate (12e35d45 门 5): set when a DURABLE state file
        # carries more quarantined records than the CURRENT qcap. The system
        # still loads every record (own/old-version files are never rejected),
        # isolates the overflow fail-closed, rejects NEW quarantine conversions
        # and admissions, and clears the flag once bounded retention reclaims
        # enough to fit the quota again.
        self._quarantine_overflow = False
        self._hard_stop_confirm_seconds = hard_stop_confirm_seconds
        self._hard_stop_defer_retry_seconds = hard_stop_defer_retry_seconds
        self._hard_stop_confirm_budget_window_seconds = hard_stop_confirm_budget_window_seconds
        self._hard_stop_confirm_budget_window_calls = hard_stop_confirm_budget_window_calls
        self._worker_python = worker_python or sys.executable
        self._worker_module = worker_module or _default_worker_module()
        self._idempotency_capacity = idempotency_capacity
        self._state_max_bytes = state_max_bytes
        self._tombstone_ttl = tombstone_ttl
        self._slots = asyncio.Semaphore(max_running)
        self._records: dict[str, _RuntimeJob] = {}
        self._idempotency: dict[str, _IdempotencyEntry] = {}
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)
        self._closed = False
        # C-145 P0: the single, bounded cleanup reaper. Spawned on demand when a
        # cleanup owner exhausts its per-round persist budget; it re-spawns the
        # owner after a bounded backoff until the terminal commit succeeds, and
        # self-terminates when no pending outcome needs a retry.
        self._reaper_task: asyncio.Task[None] | None = None
        # C-146 P0 supplement (fourth P0): the single, bounded hard-stop
        # watchdog. Spawned lazily once a live operation's absolute execution
        # bound can be reached; it quarantines the executor past the bound and
        # self-terminates when no operation needs it.
        self._hard_stop_watchdog: asyncio.Task[None] | None = None
        # C-146 P0-3: wake event for the single hard-stop watchdog. Set whenever
        # (a) a hard-stop wrapper finishes so the loop re-scans immediately for
        # newly-due siblings (never delayed past their OWN deadline by a slow
        # confirm), (b) a new operation arms an EARLIER deadline than the one the
        # loop is currently sleeping on, or (c) quarantine retention frees a slot
        # so a P0-7-deferred hard-stop can be retried. Created lazily inside the
        # watchdog loop; ``_wake_hard_stop_watchdog`` no-ops when it is absent.
        self._hard_stop_wake: asyncio.Event | None = None
        # C-146 P0-3: strong refs to in-flight hard-stop wrapper tasks so the
        # event loop never garbage-collects a running wrapper.
        self._hard_stop_tasks: set[asyncio.Task[None]] = set()
        # C-146 hard-stop gate (12e35d45 门 3): runtimes that loaded as
        # ``quarantined + pending_terminal`` but whose cleanup owner could not be
        # spawned inside ``__init__`` (no running event loop, e.g. construction
        # at import time). They are spawned lazily from the first async entry
        # point / ``close()``, so a cold boot never leaves a durable
        # quarantined+pending-terminal record as a permanent dangling entry with
        # no owner or reaper.
        self._deferred_cleanup_owners: list[_RuntimeJob] = []
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
        # C-146 hard-stop gate (12e35d45 门 5): the whole state file has a hard
        # byte bound, rejected on load so an attacker's inflated file is never
        # admitted — the same cap is enforced before every persist below.
        # C-146 P0-6: the read path is FD-bounded (fstat FIRST, then read at most
        # the verified size) — never an unbounded ``read_text``/``read`` that
        # slurps an arbitrarily large file before checking the cap.
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError as exc:
            raise RuntimeError("live planning job registry state is unavailable") from exc
        try:
            try:
                file_size = os.fstat(fd).st_size
            except OSError as exc:
                raise RuntimeError("live planning job registry state is unavailable") from exc
            if file_size > self._state_max_bytes:
                raise RuntimeError("live planning job registry state exceeds its byte bound")
            # Read exactly the verified size (never more): a file that grows
            # between fstat and read would leave unread trailing bytes, which the
            # strict size check below rejects instead of admitting.
            try:
                with os.fdopen(fd, "rb") as handle:
                    raw = handle.read(file_size)
            except OSError as exc:
                raise RuntimeError("live planning job registry state is unreadable") from exc
            fd = -1
            if len(raw) != file_size:
                raise RuntimeError("live planning job registry state is unreadable")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("live planning job registry state is unreadable") from exc
        finally:
            if fd != -1:
                os.close(fd)
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
        if not isinstance(records, list) or not isinstance(idempotency, list):
            raise RuntimeError("live planning job registry state has an invalid shape")
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
            # C-145 P0: the durable pending terminal outcome is an OPTIONAL v3
            # field. Old v3 files omit it entirely; new files write it (possibly
            # null) for every record.
            if schema_version == "tripchord-live-job-registry-v3":
                expected_record_fields.add("pending_terminal")
            # C-146 P0 supplement (P0-4): ``quarantined``/``quarantine_stage`` are
            # OPTIONAL v3 fields. Old v3 files omit them entirely; new files
            # write them for every record (null stage when not quarantined).
            if schema_version == "tripchord-live-job-registry-v3":
                expected_record_fields.add("quarantined")
                expected_record_fields.add("quarantine_stage")
            # C-145 P0 supplement: the v3 loader explicitly accepts BOTH the
            # new-v3 field set (with ``pending_terminal``, possibly null) AND the
            # old-v3 field set (without the key at all — migrated to a null
            # intent via ``item.get`` below). Every OTHER missing or unknown
            # field is still rejected fail-closed; nothing is silently patched.
            #
            # C-146 hard-stop gate (12e35d45 门 2): the durable worker identity
            # fields (worker_pgid/worker_marker/worker_probe) are OPTIONAL new-v3
            # fields on top of the required set — old files omit them entirely.
            if not isinstance(item, dict):
                raise RuntimeError("live planning job registry record is invalid")
            if schema_version == "tripchord-live-job-registry-v3":
                minimal_v3 = frozenset(
                    expected_record_fields
                    - {"pending_terminal", "quarantined", "quarantine_stage"}
                )
                full_v3 = frozenset(
                    expected_record_fields
                    | {
                        "worker_pgid",
                        "worker_marker",
                        "worker_probe",
                        # C-146 P0-3: durable per-identity orphan facts.
                        "orphan_authenticated",
                        "orphan_death_confirmed",
                    }
                )
                if not minimal_v3.issubset(set(item)) or not set(item).issubset(full_v3):
                    raise RuntimeError("live planning job registry record is invalid")
            elif set(item) != expected_record_fields:
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
            if schema_version == "tripchord-live-job-registry-v3":
                # ``get`` migrates an old-v3 record (no key) to a null intent.
                raw_pending = item.get("pending_terminal")
                if raw_pending is not None:
                    # C-146 P0 supplement (P0-4 / b119): the historical
                    # ``cancellation_requested: None`` is migrated to the
                    # explicit True semantics ONLY for the exact old-producer
                    # combination — a pre-this-change v3 record (no durable
                    # quarantine marker) whose snapshot carries the OLD stuck
                    # isolation (``cancel_pending`` at the old ``cancel_timed_out``
                    # stage, the shape the pre-P0 producer's default path wrote).
                    # The identity/digest were already validated untampered above.
                    # Every new-schema None / foreign / mixed / wrong-stage value
                    # is rejected fail-closed.
                    legacy_v3_none_cancellation = (
                        "quarantined" not in item
                        and snapshot.cancel_pending
                        and snapshot.stage == "cancel_timed_out"
                    )
                    try:
                        runtime.pending_terminal = _PendingTerminalOutcome.from_persisted(
                            raw_pending,
                            legacy_v3_none_cancellation=legacy_v3_none_cancellation,
                        )
                    except (ValueError, KeyError, TypeError, ValidationError) as exc:
                        raise RuntimeError(
                            "live planning job registry pending terminal is invalid"
                        ) from exc
                # C-146 P0 supplement (P0-4): quarantine membership is durable.
                # ``get`` migrates an old-v3 record (no key) to non-quarantined.
                quarantined = item.get("quarantined", False)
                quarantine_stage = item.get("quarantine_stage")
                if type(quarantined) is not bool:
                    raise RuntimeError("live planning job registry quarantine flag is invalid")
                if quarantined:
                    if quarantine_stage not in _QUARANTINE_STAGES:
                        raise RuntimeError("live planning job registry quarantine stage is invalid")
                    runtime.quarantined = True
                    runtime.quarantine_stage = quarantine_stage
                elif quarantine_stage is not None:
                    raise RuntimeError("live planning job registry quarantine stage is invalid")
                # C-146 hard-stop gate (12e35d45 门 2): the durable worker
                # identity (process-group id + marker nonce + probe path). It is
                # OPTIONAL; when present it must be well-typed and consistent so
                # a cold start can re-discover and clean a real orphan.
                raw_pgid = item.get("worker_pgid")
                raw_marker = item.get("worker_marker")
                raw_probe = item.get("worker_probe")
                raw_orphan_auth = item.get("orphan_authenticated")
                raw_orphan_death = item.get("orphan_death_confirmed")
                if raw_pgid is None and any(
                    value is not None
                    for value in (
                        raw_marker,
                        raw_probe,
                        raw_orphan_auth,
                        raw_orphan_death,
                    )
                ):
                    raise RuntimeError(
                        "live planning job orphan facts lack a worker identity"
                    )
                if raw_pgid is not None:
                    if type(raw_pgid) is not int or raw_pgid <= 0:
                        raise RuntimeError("live planning job registry worker pgid is invalid")
                    if not isinstance(raw_marker, str) or not raw_marker:
                        raise RuntimeError("live planning job registry worker marker is invalid")
                    if raw_probe is not None and not isinstance(raw_probe, str):
                        raise RuntimeError("live planning job registry worker probe is invalid")
                    runtime.worker_pgid = raw_pgid
                    runtime.worker_marker = raw_marker
                    runtime.worker_probe = raw_probe
                    # C-146 P0-3: durable per-identity auth/death-confirm facts
                    # (optional; absent in pre-P0-3 v3 files → None, meaning the
                    # identity has never been cold-boot checked).
                    if raw_orphan_auth is not None and type(raw_orphan_auth) is not bool:
                        raise RuntimeError(
                            "live planning job orphan authentication fact is invalid"
                        )
                    runtime.orphan_authenticated = raw_orphan_auth
                    if raw_orphan_death is not None and type(raw_orphan_death) is not bool:
                        raise RuntimeError(
                            "live planning job orphan death-confirmation fact is invalid"
                        )
                    if raw_orphan_death is True and raw_orphan_auth is not True:
                        raise RuntimeError(
                            "live planning job orphan death confirmation lacks authentication"
                        )
                    runtime.orphan_death_confirmed = raw_orphan_death
            self._records[snapshot.id] = runtime
        # C-146 P0 supplement (P0-4) / b119: active record capacity and
        # quarantine capacity are validated INDEPENDENTLY. A legal file may hold
        # ``capacity`` ordinary executable records PLUS up to
        # ``quarantine_capacity`` isolated/quarantined records — the latter never
        # occupy executable active capacity. Each quota is enforced separately so
        # neither a ghost quarantine nor an overflowing active set can hide
        # behind the other.
        active_records = sum(1 for item in self._records.values() if not item.quarantined)
        quarantined_records = len(self._records) - active_records
        if active_records > self._capacity:
            raise RuntimeError("live planning job registry state exceeds its bounds")
        if quarantined_records > self._quarantine_capacity:
            # C-146 P0-5: persistent quarantine may exceed the CURRENT quarantine
            # capacity (qcap). qcap is a NEW-conversion/admission bound, NOT a
            # loader reject bound: the loader must load every durable record
            # (including overflow), flag the registry fail-closed so no new
            # conversion/admission happens while overflow holds, and only restore
            # capacity after bounded retention cleanup reclaims quarantine space.
            self._quarantine_overflow = True
        for item in idempotency:
            expected_idempotency_fields = {
                "partition",
                "job_id",
                "request_digest",
            }
            legacy_isolated_field = "legacy_isolated"
            if schema_version == "tripchord-live-job-registry-v3":
                expected_idempotency_fields.add("defer_start")
            # The v3 loader accepts the optional legacy-isolation marker AND the
            # optional ``updated_at`` (12e35d45 门 5 tombstone TTL) on top of the
            # required field set. Older v3 files omit either or both entirely.
            if not isinstance(item, dict):
                raise RuntimeError("live planning idempotency record is invalid")
            if schema_version == "tripchord-live-job-registry-v3":
                required = frozenset(expected_idempotency_fields)
                optional = frozenset({legacy_isolated_field, "updated_at"})
                if not required.issubset(set(item)) or not set(item).issubset(required | optional):
                    raise RuntimeError("live planning idempotency record is invalid")
            elif set(item) != expected_idempotency_fields:
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
            updated_at: datetime | None = None
            if schema_version == "tripchord-live-job-registry-v3":
                defer_start = item["defer_start"]
                if type(defer_start) is not bool:
                    raise RuntimeError("live planning idempotency execution mode is invalid")
                legacy_isolated = item.get(legacy_isolated_field, False)
                if type(legacy_isolated) is not bool:
                    raise RuntimeError("live planning idempotency isolation is invalid")
                raw_updated_at = item.get("updated_at")
                if raw_updated_at is not None:
                    if not isinstance(raw_updated_at, str):
                        raise RuntimeError("live planning idempotency updated_at is invalid")
                    try:
                        updated_at = _aware(
                            datetime.fromisoformat(raw_updated_at),
                            "updated_at",
                        )
                    except (ValueError, TypeError):
                        raise RuntimeError(
                            "live planning idempotency updated_at is invalid"
                        ) from None
            loaded_runtime = self._records.get(job_id)
            if loaded_runtime is None:
                # C-146 P0 supplement (P0-4)/b119: a minimal durable tombstone
                # keeps the idempotency binding after its quarantined record is
                # reclaimed, so a same-key request always fails closed and the
                # key is never silently reused. ONLY an isolated v3 binding is a
                # valid tombstone — a non-isolated binding pointing at a missing
                # record is corrupt, and a legacy v1/v2 binding can never be a
                # tombstone (quarantine only exists in v3).
                if (
                    not legacy_isolated
                    or defer_start is None
                    or schema_version != "tripchord-live-job-registry-v3"
                ):
                    raise RuntimeError("live planning idempotency binding is invalid")
            elif loaded_runtime.snapshot.request_sha256 != request_digest:
                raise RuntimeError("live planning idempotency binding is invalid")
            if defer_start is None:
                # P0-2: legacy v1/v2 records carry no execution mode. Derive the
                # provable mode from the surviving durable facts; when the facts
                # are insufficient, fail closed into an isolated binding that is
                # never replayed (the derived bool is persisted, never null).
                assert loaded_runtime is not None
                derived = self._derive_legacy_execution_mode(
                    loaded_runtime,
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
                updated_at=updated_at,
            )
        # C-146 hard-stop gate (12e35d45 门 5): the idempotency identity /
        # tombstone collection has a hard cardinality bound, enforced on load so
        # a hand-crafted file cannot admit more identities than the registry can
        # ever grow to.
        if len(self._idempotency) > self._idempotency_capacity:
            raise RuntimeError("live planning job registry idempotency bounds exceeded")
        for runtime in self._records.values():
            if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES:
                if runtime.worker_pgid is not None and runtime.worker_marker:
                    # C-146 P0-3 (RETURN 7de8cf3e): a DURABLE worker identity
                    # means a real executor process may still be alive (the
                    # parent API was SIGKILLed mid-run and the worker was
                    # orphaned). Resolution must NOT run at load time — a
                    # terminalize/prune before the orphan is authenticated +
                    # killed + reaped would publish a terminal label or reclaim
                    # over live external side effects. The record is resolved in
                    # ``restore_after_restart`` AFTER
                    # ``_discover_and_stop_orphan_workers`` provably handled the
                    # group.
                    continue
                self._resolve_cold_booted_record_locked(runtime)
        # C-146 P0 supplement (P0-4) / b119: the resolution loop above may have
        # quarantined newly-isolated ambiguous records. Re-validate the two
        # INDEPENDENT quotas AFTER resolution so a hand-crafted file whose
        # quarantine exceeds its own bounded quota is rejected fail-closed
        # before anything is persisted.
        active_records = sum(1 for item in self._records.values() if not item.quarantined)
        if active_records > self._capacity:
            raise RuntimeError("live planning job registry state exceeds its bounds")
        if len(self._records) - active_records > self._quarantine_capacity:
            # C-146 P0-5: persistent quarantine above the CURRENT qcap —
            # including records the resolution loop newly isolated — ALWAYS
            # loads. qcap is a NEW-conversion/admission bound, NOT a loader
            # reject bound: the registry goes fail-closed (no new
            # conversion/admission) while overflow holds, and only bounded
            # retention cleanup restores capacity.
            self._quarantine_overflow = True
        # C-146 hard-stop gate (12e35d45 门 3): a durable ``quarantined +
        # pending_terminal`` record must NEVER be left as a permanent dangling
        # entry with no owner/reaper. The process boundary that ran it is
        # provably gone, so restore its unique cleanup owner; the owner
        # reconciles the durable quarantine fact, then settles the durable
        # pending terminal once (memory=disk, idempotent across restarts). When
        # no event loop is running yet (construction at import time) the spawn
        # is deferred to the first async entry point / ``close()``.
        for runtime in self._records.values():
            if runtime.quarantined and runtime.pending_terminal is not None:
                self._defer_cleanup_owner_spawn(runtime)
        self._prune_locked(self._utc_now())
        self._persist_locked()

    def _resolve_cold_booted_record_locked(self, runtime: _RuntimeJob) -> None:
        """Settle a cold-booted non-terminal record from DURABLE facts only.

        C-146 P0-3 (RETURN 7de8cf3e): extracted from the ``_load_state``
        resolution loop so ``restore_after_restart`` runs the SAME resolution for
        a record whose durable worker identity pointed at an orphan group that
        was only just discovered + killed + reaped. Resolution is honest ONLY
        when the executor is provably gone — the caller must guarantee that
        (``_load_state`` skips records with a durable worker identity; the post-
        discovery ``restore_after_restart`` path runs after
        ``_discover_and_stop_orphan_workers``).
        """
        # C-146 P0 supplement (P0-4): a record that loaded as ``prepared=True``
        # is provably never-executed (the on-disk invariant forbids a prepared
        # record in any state but QUEUED, and activation is the only way a
        # prepared job ever starts). ``prepared`` is reset below, so capture the
        # durable flag before the reset: restart_cancelled is honest ONLY for
        # this shape — never for an admitted immediate (prepared=False) record
        # whose operation may already have been executing.
        was_prepared = runtime.prepared
        runtime.prepared = False
        if runtime.quarantined:
            # C-146 P0 supplement (P0-4): a quarantined record stays
            # quarantined NON-terminal across a cold boot — never terminalized
            # from a memory-only intent, never re-isolated, never unquarantined.
            # The quarantine facts (stage, error, updated_at) are durable and
            # idempotent, so a second cold boot observes exactly the same state.
            #
            # C-146 P0-3 (RETURN 7de8cf3e) supplement: the ONE exception is an
            # ORPHAN quarantine set by this boot's ``_discover_and_stop_orphan_workers``
            # after it AUTHENTICATED the durable worker group, SIGKILLed it and
            # CONFIRMED every member died (``hard_stopped``). That proves the
            # executor is provably gone, so the loader's OWN resolution may settle
            # the record from durable facts — e.g. a formal activation interrupted
            # by the parent crash (``activation_operation`` phase committed +
            # snapshot QUEUED) to ``restart_cancelled``. An orphan group that could
            # NOT be confirmed dead stays quarantined for the next startup; any
            # other quarantine stage stays non-terminal by the P0-4 contract.
            if not (
                runtime.quarantine_stage == _QUARANTINE_ORPHAN_STAGE
                and runtime.hard_stopped
            ):
                return
            was_orphan_confirmed = True
            runtime.quarantined = False
            runtime.quarantine_stage = None
        else:
            was_orphan_confirmed = False
        pending = runtime.pending_terminal
        # C-146 P0-3 supplement (regression guard): an orphan-confirmed record
        # may ONLY be settled when the durable facts prove a terminal outcome.
        # A RUNNING record that died mid-run (no durable cancel intent, no
        # activation, deadline not passed) has no provable terminal label — the
        # honest outcome is to KEEP the confirmed-orphan quarantine (the
        # strongest durable fact: the executor was authenticated + SIGKILLed +
        # confirmed dead), never downgrade it to ``isolated_ambiguous_cancel``
        # and never guess a terminal label over live work.
        provable_terminal = (
            (pending is not None and runtime.snapshot.cancel_pending)
            or (
                was_prepared
                and runtime.snapshot.state == LivePlanningJobState.QUEUED
            )
            or (runtime.snapshot.deadline_at <= self._utc_now())
            or (
                runtime.activation_operation is not None
                and runtime.activation_operation.get("phase")
                in {"intent", "dispatched", "committed"}
                and runtime.snapshot.state == LivePlanningJobState.QUEUED
            )
        )
        if was_orphan_confirmed and not provable_terminal:
            runtime.quarantined = True
            runtime.quarantine_stage = _QUARANTINE_ORPHAN_STAGE
            runtime.hard_stopped = True
            return
        if pending is not None and runtime.snapshot.cancel_pending:
            # C-145 P0: a DURABLE retry intent survives a restart. The real
            # operation's process is provably gone, so continue the cleanup to
            # the intended terminal outcome now — never guess a cancelled/failed
            # label and never treat an unknown state as success. A cancel_pending
            # guard keeps an inconsistent (corrupted) outcome fail-closed to
            # restart_cancelled.
            self._terminalize_locked(
                runtime,
                pending.state,
                stage=pending.stage,
                result=pending.result,
                error=pending.error,
                safe_failure=pending.safe_failure,
                cancellation_requested=pending.cancellation_requested,
            )
        elif runtime.snapshot.stage == _ISOLATED_AMBIGUOUS_CANCEL_STAGE:
            # C-145 P0 supplement: a record quarantined by an earlier cold start
            # stays quarantined — re-isolating is idempotent, so two consecutive
            # cold starts never drift it into a guessed CANCELLED/FAILED label.
            # C-146 P0 supplement (P0-4): old v3 files lack the durable
            # ``quarantined`` marker, so a record at this stage is upgraded
            # atomically to the explicit quarantine flag (persisted on the next
            # write).
            runtime.quarantined = True
            runtime.quarantine_stage = _ISOLATED_AMBIGUOUS_CANCEL_STAGE
        elif pending is not None or runtime.snapshot.cancel_pending:
            # C-145 P0 supplement: a non-terminal record whose durable cancel
            # intent (pending=true) has NO provable terminal outcome — or
            # carries an outcome inconsistent with its snapshot — gives no
            # unforgeable fact to prove whether the intended label was CANCELLED
            # or FAILED. We never guess: the record is isolated (never replayed)
            # and the ambiguity is surfaced explicitly.
            self._isolate_ambiguous_cancel_locked(runtime)
        elif was_prepared and runtime.snapshot.state == LivePlanningJobState.QUEUED:
            # C-146 P0 supplement (P0-3): a record that never passed the
            # admission barrier provably never executed — the operation closure
            # cannot have started, so a CANCELLED label guesses nothing about
            # live work. restart_cancelled is the honest, provable tombstone for
            # a never-started job. P0-4 narrows this to ``was_prepared`` records
            # ONLY: a prepared record is the one shape whose durable flag proves
            # it never executed.
            self._terminalize_locked(
                runtime,
                LivePlanningJobState.CANCELLED,
                stage="restart_cancelled",
                error="live planning job cannot continue after process restart",
                cancellation_requested=True,
            )
        elif runtime.snapshot.deadline_at <= self._utc_now():
            # C-146 P0 supplement (P0-3/P0-4): recover FAILED/deadline_exceeded
            # ONLY from durable unforgeable deadline provenance. An admitted
            # record with NO cancel intent whose deadline provably passed before
            # the crash — including one that died in the
            # deadline-intent-persist-pending window, where the FIRST FAILED
            # intent never committed — carries the deadline as its single
            # provable explanation. We never fake a deadline and never guess
            # restart_cancelled over it.
            self._terminalize_locked(
                runtime,
                LivePlanningJobState.FAILED,
                stage="deadline_exceeded",
                error="TimeoutError: live planning job deadline exceeded",
                safe_failure=_safe_failure_diagnostic(
                    TimeoutError("live planning job deadline exceeded"),
                    code_override=LivePlanningSafeFailureCode.DEADLINE_EXCEEDED,
                ),
                cancellation_requested=True,
            )
        elif (
            runtime.activation_operation is not None
            and runtime.activation_operation.get("phase")
            in {"intent", "dispatched", "committed"}
            and runtime.snapshot.state == LivePlanningJobState.QUEUED
        ):
            # C-143 (91648931) / C-146 P0-4: a FORMAL activation that was
            # interrupted by the process death. ``activate`` resets ``prepared``
            # (so the was_prepared branch above cannot fire) and the durable
            # activation_operation proves the job passed the admission barrier
            # and was activating (intent/dispatched) or already committed
            # (committed) while its snapshot never advanced past QUEUED. With the
            # deadline NOT yet passed (the branch above would otherwise have
            # proved deadline provenance) and the executor gone with the process
            # boundary, restart_cancelled is the contract-allowed, provable
            # outcome — the same-key retry fails closed and the dispatched
            # operation is never replayed. Distinct from a plain immediate
            # (prepared=False, no activation) QUEUED record, whose operation may
            # have been executing when the process died and which stays
            # quarantined ambiguous.
            self._terminalize_locked(
                runtime,
                LivePlanningJobState.CANCELLED,
                stage="restart_cancelled",
                error="live planning job cannot continue after process restart",
                cancellation_requested=True,
            )
        else:
            # C-146 P0 supplement (P0-3/P0-4): an admitted record whose deadline
            # did not pass and that carries no durable cancel intent gives
            # insufficient unforgeable facts to prove ANY terminal outcome. This
            # includes a QUEUED immediate record (prepared=False): its operation
            # may already have been executing when the process died (RUNNING is
            # never a durable write), so restart_cancelled would guess a
            # CANCELLED label over live work. No terminal label is fabricated:
            # the record is quarantined as ambiguous instead.
            self._isolate_ambiguous_cancel_locked(runtime)
        runtime.pending_terminal = None

    def _isolate_ambiguous_cancel_locked(self, runtime: _RuntimeJob) -> None:
        """Quarantine a non-terminal record whose durable cancel intent has no
        provable terminal outcome (C-145 P0 supplement / C-146 P0-4/b119).

        A record that was mid-cancel at the crash (``snapshot.cancel_pending``)
        but carries no durable ``pending_terminal`` gives no unforgeable fact to
        prove whether the intended outcome was CANCELLED or FAILED. We never
        guess: the record stays NON-terminal with an explicit
        ``isolated_ambiguous_cancel`` marker and a clear error, and every
        idempotency binding to it is marked ``legacy_isolated`` so a same-key
        request fails closed instead of replaying or re-admitting over an
        unknown outcome. The clear ``cancel_pending`` flag keeps a later
        cancel()/close() from joining a phantom in-flight cancel and guessing a
        label for it."""
        runtime.snapshot = runtime.snapshot.model_copy(
            update={
                "cancel_pending": False,
                "stage": _ISOLATED_AMBIGUOUS_CANCEL_STAGE,
                "error": (
                    "cancel was pending at restart without a provable terminal "
                    "outcome; the job is isolated and never replayed"
                ),
                "revision": runtime.snapshot.revision + 1,
                "updated_at": self._utc_now(),
            }
        )
        runtime.cancel_pending = False
        # C-146 P0 supplement (P0-4): a quarantined record is non-terminal but
        # never occupies executable active capacity — the quarantine has its own
        # bounded quota and retention.
        runtime.quarantined = True
        runtime.quarantine_stage = _ISOLATED_AMBIGUOUS_CANCEL_STAGE
        for entry in self._idempotency.values():
            if entry.job_id == runtime.snapshot.id:
                entry.legacy_isolated = True

    def _derive_legacy_execution_mode(
        self,
        runtime: _RuntimeJob,
        *,
        schema_version: str,
    ) -> bool | None:
        """Derive the provable execution mode for a legacy (v1/v2) idempotency
        binding, or return None when the legacy facts are insufficient.

        Returns True only for a job with a durable activation operation or a
        prepared flag. Returns None (never False) for every shape that lacks an
        unforgeable proof of being immediate — a formal prepared job could have
        activated with operation_id=None, so "missing activation" is not
        evidence of immediate execution. None fails closed into a legacy-isolated
        binding that is never replayed under any mode.
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
        # 硬门 A: a non-cancelled record without an activation operation and
        # without a prepared flag is NOT provably immediate. The old v2 API
        # allowed a formal defer_start=true prepared job to activate successfully
        # with operation_id=None, leaving exactly this shape (RUNNING,
        # prepared=false, activation_operation=None). Only unforgeable,
        # durably-recheckable facts prove the execution mode; this shape proves
        # none, so it fails closed into a legacy-isolated binding that is never
        # replayed under any mode.
        return None

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

    def _persist_locked(
        self,
        *,
        snapshot_overrides: dict[str, LivePlanningJobSnapshot] | None = None,
    ) -> None:
        """Persist the state file.

        ``snapshot_overrides`` lets one caller (the worker-identity persist at
        spawn time) write a record whose serialized snapshot differs from the
        live in-memory snapshot — used to keep the DURABLE state at QUEUED while
        the live snapshot has advanced to RUNNING (see ``_run_worker_command``).
        """
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
        # C-146 hard-stop gate (12e35d45 门 6): fail-fast BEFORE serialization.
        # The cardinality bounds are enforced at admission, so a state that
        # reaches this point already fits the quotas; this pre-check makes the
        # byte-boundary failure happen before the full blob is built, never after
        # an unbounded list was materialized on disk.
        #
        # C-146 P0-5 (RETURN 7de8cf3e): the two quotas are INDEPENDENT — active
        # records vs ``capacity`` and quarantined records vs ``quarantine_capacity``
        # — never the old combined sum. A legitimately-loaded durable quarantine
        # that exceeds the CURRENT qcap (a config shrink) must load AND persist
        # fail-closed (``_quarantine_overflow`` set, no new conversion/admission);
        # only bounded retention cleanup restores capacity. The combined-count
        # reject would crash the cold start instead of preserving the overflow.
        active_records = sum(1 for item in self._records.values() if not item.quarantined)
        quarantined_records = len(self._records) - active_records
        if active_records > self._capacity or (
            quarantined_records > self._quarantine_capacity and not self._quarantine_overflow
        ):
            raise RuntimeError("live planning job registry record bounds exceeded")
        if len(self._idempotency) > self._idempotency_capacity:
            raise RuntimeError("live planning job registry idempotency bounds exceeded")
        payload = {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [
                {
                    "tenant_partition": runtime.tenant_partition,
                    # C-146 P0-6 (supplement): the terminal ``result`` is a
                    # LIVE-PROCESS payload, not part of the bounded durable
                    # identity. A successful live planning response (the full run
                    # + scheduler/exploration traces) can legitimately exceed the
                    # state-file byte cap; the cap guards the identity/metadata
                    # (records, idempotency, quarantine, worker ownership), never
                    # the unbounded user output. Excluding ``result`` here keeps
                    # the on-disk record within the bound while the live process
                    # still serves it via the status endpoint; a cold restart
                    # loads the record with ``result=None`` (the run's durable
                    # data lives in the search-run store, not in the registry
                    # state file).
                    "snapshot": _without_result(
                        snapshot_overrides.get(runtime.snapshot.id, runtime.snapshot)
                        if snapshot_overrides is not None
                        else runtime.snapshot
                    ),
                    "prepared": runtime.prepared,
                    "activation_operation": runtime.activation_operation,
                    "pending_terminal": (
                        runtime.pending_terminal.to_persisted()
                        if runtime.pending_terminal is not None
                        else None
                    ),
                    # C-146 P0 supplement (P0-4): quarantine membership is durable
                    # so a cold restart never treats a quarantined record as
                    # active, never fabricates a terminal label for it, and never
                    # silently reuses its key.
                    "quarantined": runtime.quarantined,
                    "quarantine_stage": runtime.quarantine_stage,
                    # C-146 hard-stop gate (12e35d45 门 2): the durable worker
                    # identity so a cold start can clean a real orphan even after
                    # a parent-API crash and a PGID/PID reuse.
                    "worker_pgid": runtime.worker_pgid,
                    "worker_marker": runtime.worker_marker,
                    "worker_probe": runtime.worker_probe,
                    # C-146 P0-3: durable per-identity auth/death-confirm facts so
                    # a cold start can prove it may settle a record and never
                    # guesses over an unauthenticated/unconfirmed executor.
                    "orphan_authenticated": runtime.orphan_authenticated,
                    "orphan_death_confirmed": runtime.orphan_death_confirmed,
                }
                for runtime in sorted(self._records.values(), key=lambda item: item.snapshot.id)
            ],
            "idempotency": [
                {
                    "partition": partition,
                    "job_id": entry.job_id,
                    "request_digest": entry.request_digest,
                    "defer_start": entry.defer_start,
                    "legacy_isolated": entry.legacy_isolated,
                    "updated_at": (
                        entry.updated_at.isoformat() if entry.updated_at is not None else None
                    ),
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
        # C-146 hard-stop gate (12e35d45 门 5): the whole state file has a hard
        # byte bound, enforced BEFORE any temporary file is written — a state
        # that cannot serialize within the bound must never reach disk (and the
        # caller's rollback keeps memory byte-identical to the untouched disk).
        if len(encoded) > self._state_max_bytes:
            raise RuntimeError("live planning job registry state exceeds its byte bound")
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
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
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
        operation: LiveJobOperation | LiveJobWorkerCommand | None = None,
        operation_factory: Callable[
            [], LiveJobOperation | LiveJobWorkerCommand
        ]
        | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        deadline_seconds: float = 3600,
        defer_start: bool = False,
    ) -> tuple[LivePlanningJobSnapshot, bool]:
        self._spawn_deferred_cleanup_owners()
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be a finite positive number")
        if (operation is None) == (operation_factory is None):
            raise ValueError("exactly one live planning operation source is required")
        idempotency_partition: str | None = None
        if request_digest is not None and not self._valid_request_digest(request_digest):
            raise ValueError("request_digest must be a lowercase SHA-256 hex digest")
        if idempotency_key is not None:
            if not idempotency_key.strip() or len(idempotency_key) > 200:
                raise ValueError("idempotency key must contain 1 to 200 characters")
            if request_digest is None:
                raise ValueError("request_digest must be a lowercase SHA-256 hex digest")
            idempotency_partition = self._idempotency_partition(tenant_id, idempotency_key)
        async with self._changed:
            if self._closed:
                raise RuntimeError("live planning job registry is closed")
            now = self._utc_now()
            self._prune_locked(now)
            if idempotency_partition is not None:
                existing = self._idempotency.get(idempotency_partition)
                if existing is not None:
                    existing_runtime = self._records.get(existing.job_id)
                    if existing_runtime is None:
                        # C-146 P0 supplement (P0-4) / b119: a durable idempotency
                        # binding whose record is gone is a minimal tombstone
                        # (quarantine reclamation / cold-start load of a reclaimed
                        # record). It ALWAYS fails closed — the key is never
                        # silently deleted and reused, never re-admitted, never
                        # replayed. Runtime-missing + close/cancel/prune/retention
                        # must never clear it.
                        raise LivePlanningJobIdempotencyConflictError(
                            "idempotency key is bound to a reclaimed or isolated "
                            "record and cannot be reused"
                        )
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
                    elif existing.defer_start is not None and existing.defer_start != defer_start:
                        # P0-3: the execution mode is part of the identity. A
                        # same-key request that switches between a prepared and an
                        # immediate execution must fail closed — never reuse the
                        # old receipt under a different executor mode.
                        raise LivePlanningJobIdempotencyConflictError(
                            "idempotency key was already used with a different execution mode"
                        )
                    else:
                        # P0-1 / 硬门 B: a cancel_pending record may be
                        # terminalized idempotently here only when BOTH the
                        # registry task AND the real operation task are truly
                        # stopped — never while the operation could still be
                        # writing side effects. If the operation is still running,
                        # the retry fails closed: it must not report reused
                        # success over a live executor nor terminalize CANCELLED
                        # over live work.
                        if existing_runtime.snapshot.cancel_pending:
                            if self._executors_stopped(existing_runtime):
                                try:
                                    self._complete_cancel_terminalize_locked(existing_runtime)
                                except LivePlanningJobRegistryPostCommitError as exc:
                                    exc.job_id = existing_runtime.snapshot.id
                                    raise
                            else:
                                cancellation_pending_error = (
                                    LivePlanningJobCancellationPendingError(
                                        "idempotency key is bound to a cancellation "
                                        "still in progress; retry after the "
                                        "operation stops"
                                    )
                                )
                                # P0-2: the HTTP layer must be able to surface
                                # the original identity and a status query
                                # location instead of a bare 500.
                                cancellation_pending_error.job_id = existing_runtime.snapshot.id
                                raise cancellation_pending_error
                        return existing_runtime.snapshot, True
            # C-146 hard-stop gate (12e35d45 门 5): the idempotency-identity /
            # tombstone collection has a hard CARDINALITY bound, enforced
            # ATOMICALLY BEFORE the new record is admitted to ``_records`` or
            # ``_idempotency``. An attacker issuing many unique keys can never
            # grow this beyond the configured cap, and an over-cap identity can
            # never leave a partial record behind. The loader enforces the same
            # bound on read, so a file this function writes is always
            # reloadable. Existing recoverable identities are never evicted — a
            # full collection fails closed.
            #
            # C-146 P0-6 (RETURN 7de8cf3e): the idcap check runs BEFORE any
            # capacity eviction. Eviction frees an executable RECORD slot, never
            # an identity slot — checking the count AFTER eviction would let a
            # full idempotency collection wrongly admit a NEW key by first
            # deleting the oldest terminal record's binding (the eviction
            # shrinks the count below the cap), silently destroying the old
            # mapping and breaking idempotent replay/audit. A full collection
            # must fail closed with the old identity byte-identical.
            if (
                idempotency_partition is not None
                and len(self._idempotency) >= self._idempotency_capacity
            ):
                raise LivePlanningJobCapacityError(
                    "live planning job idempotency capacity exceeded"
                )
            # C-146 P0-5 (RETURN 7de8cf3e): the identity-capacity check above is
            # the ATOMIC authority.  Only after it accepts the new key do we
            # invoke the lazy operation factory (the HTTP worker-command
            # builder), mint a UUID, construct ``_RuntimeJob``, or evict an
            # executable record.  Keeping every step under this SAME lock closes
            # the old pre-check/build/re-check race: two concurrent new keys can
            # never both build commands for one remaining identity slot. A full
            # collection therefore rejects with zero constructor calls and
            # byte-identical memory/disk state.
            resolved_operation = (
                operation_factory() if operation_factory is not None else operation
            )
            assert resolved_operation is not None
            deadline_at = now + timedelta(seconds=deadline_seconds)
            # Canonical UUID ids remain globally unique without the mixed-case
            # random runs that resemble bare credentials in committed evidence.
            job_id = f"live-job-{uuid4()}"
            runtime = _RuntimeJob(
                tenant_partition=self._tenant_partition(tenant_id),
                deadline_monotonic=(
                    asyncio.get_running_loop().time() + deadline_seconds
                ),
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
                operation=resolved_operation,
                prepared=defer_start,
            )
            # C-146 P0 supplement (fourth P0): the absolute EXECUTION bound.
            # Once this monotonic time passes, the hard-stop watchdog must
            # quarantine a still-live operation regardless of storage recovery.
            runtime.hard_stop_monotonic = (
                runtime.deadline_monotonic
                + self._execution_hard_stop_grace_seconds
            )
            evicted_runtime, evicted_entries = self._make_capacity_locked()
            self._records[job_id] = runtime
            if idempotency_partition is not None:
                assert request_digest is not None
                self._idempotency[idempotency_partition] = _IdempotencyEntry(
                    job_id=job_id,
                    request_digest=request_digest,
                    defer_start=defer_start,
                    updated_at=now,
                )
            # C-146 P0-3: a NEW job may arm an EARLIER execution bound than the
            # one the watchdog is currently sleeping on. Wake it only now that the
            # record is actually visible in ``_records`` — a wake issued before
            # insertion would be consumed by a re-scan that cannot see the new
            # deadline, and the loop would sleep straight past it.
            self._wake_hard_stop_watchdog()
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
                        self._run(runtime, resolved_operation),
                        name=f"tripchord:{job_id}",
                    )
                self._changed.notify_all()
                raise
            except Exception:
                # C-146 P0-6: a pre-commit persist failure must roll back BOTH the
                # just-added record AND any capacity eviction that made room for
                # it — otherwise the old terminal record's idempotency mapping is
                # silently destroyed by a failed admission.
                self._remove_locked(job_id)
                self._restore_capacity_locked(evicted_runtime, evicted_entries)
                raise
            if not defer_start:
                runtime.task = asyncio.create_task(
                    self._run(runtime, resolved_operation),
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
        worker_execution_capability: object | None = None,
    ) -> LivePlanningJobSnapshot | None:
        """Start one explicitly prepared job exactly once.

        Formal evidence uses this split so its signed challenge can bind the
        already allocated terminal job id before any provider or Companion
        event is allowed to occur.
        """
        self._spawn_deferred_cleanup_owners()

        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            if runtime is None:
                return None
            activation = runtime.activation_operation
            if activation is not None and operation_id != activation["operation_id"]:
                raise LivePlanningJobInactiveError(
                    "live planning job uses a foreign activation operation"
                )
            if runtime.quarantined:
                # C-146 P0 supplement (P0-4 / b119): a quarantined record is
                # NON-terminal and never actionable — it must not be started,
                # replayed, committed or reported as an in-flight dispatch. The
                # quarantine is explicit and stable; fail closed, never guess a
                # label for it.
                raise LivePlanningJobInactiveError(
                    "live planning job is quarantined and cannot be activated"
                )
            if activation is not None:
                if activation["phase"] == "cancelled":
                    raise LivePlanningJobInactiveError(
                        "live planning activation operation was cancelled"
                    )
                if activation["phase"] in {"dispatched", "committed"}:
                    if runtime.snapshot.state == LivePlanningJobState.CANCELLED:
                        raise LivePlanningJobInactiveError("live planning job was cancelled")
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
                raise LivePlanningJobInactiveError("prepared live planning job is no longer queued")
            previous_worker_capability = runtime.worker_execution_capability
            if worker_execution_capability is not None:
                if not isinstance(runtime.operation, LiveJobWorkerCommand) or activation is None:
                    raise LivePlanningJobInactiveError(
                        "formal execution capability requires a prepared worker activation"
                    )
                try:
                    canonical_capability = json.dumps(
                        worker_execution_capability,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                except (TypeError, ValueError) as exc:
                    raise LivePlanningJobInactiveError(
                        "formal worker execution capability is not canonical JSON"
                    ) from exc
                if not secrets.compare_digest(
                    hashlib.sha256(canonical_capability).hexdigest(),
                    str(activation["capability_sha256"]),
                ):
                    raise LivePlanningJobInactiveError(
                        "formal worker execution capability differs from activation"
                    )
                runtime.worker_execution_capability = json.loads(canonical_capability)
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
                runtime.worker_execution_capability = previous_worker_capability
                if activation is not None:
                    runtime.activation_operation = activation
                raise
            if os.environ.get("TRIPCHORD_TEST_FORMAL_ACTIVATION_FAILPOINT") in {
                "exit_after_registry_dispatch_persist",
                "exit_after_registry_dispatch",
            }:
                # The real-HTTP failpoint exits only after the Companion ack
                # response has been observably written.  Leave the durable
                # dispatch without an executor during that response flush: the
                # old immediate ``os._exit`` never yielded the event loop after
                # task creation either, and letting the task advance for the
                # flush interval would change the interrupted durable facts.
                self._changed.notify_all()
                return runtime.snapshot
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
        fields = (
            immutable_fields
            if allow_intent_shape
            else immutable_fields
            | {
                "phase",
                "dispatch_count",
            }
        )
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
            if (
                not isinstance(value[field_name], str)
                or re.fullmatch(r"[0-9a-f]{64}", value[field_name]) is None
            ):
                raise RuntimeError(f"live planning activation operation {field_name} is invalid")
        for field_name in ("idempotency_key", "challenge_id"):
            if (
                not isinstance(value[field_name], str)
                or not value[field_name].strip()
                or len(value[field_name]) > 200
            ):
                raise RuntimeError(f"live planning activation operation {field_name} is invalid")
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
        return cast(dict[str, Any], json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ))

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
                existing_intent = {key: existing[key] for key in checked}
                if existing_intent != checked:
                    raise LivePlanningJobInactiveError(
                        "live planning activation intent differs from the durable intent"
                    )
                return cast(dict[str, Any], json.loads(json.dumps(existing, sort_keys=True)))
            if (
                not runtime.prepared
                or runtime.task is not None
                or runtime.snapshot.state != LivePlanningJobState.QUEUED
                or checked["queued_result"]["job"] != runtime.snapshot.model_dump(mode="json")
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
            return cast(
                dict[str, Any],
                json.loads(json.dumps(runtime.activation_operation, sort_keys=True)),
            )

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
            return cast(dict[str, Any], json.loads(json.dumps(operation, sort_keys=True)))

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
            if runtime is None:
                raise LivePlanningJobInactiveError(
                    "live planning job uses a foreign activation operation"
                )
            if runtime.quarantined:
                # C-146 P0 supplement (P0-4 / b119): a quarantined record's
                # activation is never committed — fail closed, keep the durable
                # facts untouched.
                raise LivePlanningJobInactiveError(
                    "live planning job is quarantined and cannot be committed"
                )
            if operation["phase"] == "cancelled":
                raise LivePlanningJobInactiveError(
                    "live planning activation operation was cancelled"
                )
            if runtime.snapshot.state == LivePlanningJobState.CANCELLED:
                raise LivePlanningJobInactiveError("live planning job was cancelled")
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
                return cast(
                    dict[str, Any],
                    json.loads(json.dumps(committed_operation, sort_keys=True)),
                )
            return cast(dict[str, Any], json.loads(json.dumps(operation, sort_keys=True)))

    async def is_prepared(
        self,
        job_id: str,
        tenant_id: str,
        *,
        request_sha256: str,
    ) -> bool:
        self._spawn_deferred_cleanup_owners()
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
        self._spawn_deferred_cleanup_owners()
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
        self._spawn_deferred_cleanup_owners()
        async with self._changed:
            self._prune_locked(self._utc_now())
            runtime = self._owned_locked(job_id, tenant_id)
            if runtime is None:
                return None
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return runtime.snapshot
            if runtime.quarantined:
                # C-145 P0 supplement / C-146 P0 supplement (P0-4): a quarantined
                # record has no provable terminal outcome — an explicit cancel
                # must NOT guess CANCELLED. Return the quarantined snapshot
                # unchanged (idempotent, fail-closed) so a later cold start sees
                # the same quarantine and the same-key path keeps failing closed.
                return runtime.snapshot
            if runtime.intent_persist_pending:
                # C-146 P0 supplement (P0-3): a deadline record whose FIRST
                # durable intent is still uncommitted must not be cancelled by a
                # caller — the intent commits first, then the executor is stopped
                # to the committed FAILED/deadline_exceeded outcome. An explicit
                # cancel joins nothing and guesses nothing: return the observable
                # retry-state snapshot unchanged (idempotent, fail-closed).
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
                pre_call_pending_terminal = runtime.pending_terminal
                runtime.cancel_pending = True
                runtime.snapshot = runtime.snapshot.model_copy(
                    update={
                        "cancel_pending": True,
                        "cancellation_requested": True,
                        "stage": "cancelling",
                        "updated_at": self._utc_now(),
                    }
                )
                # C-145 P0 supplement: a cancel's FIRST durable intent carries
                # the full CANCELLED/cancelled outcome in the SAME atomic commit
                # as the cancel_pending isolation — so a crash right after that
                # commit (before any drain/mark) cold-starts straight to the
                # unambiguous cancel outcome, never a guessed label.
                runtime.pending_terminal = _PendingTerminalOutcome(
                    state=LivePlanningJobState.CANCELLED,
                    stage="cancelled",
                    cancellation_requested=True,
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
                    runtime.pending_terminal = pre_call_pending_terminal
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
                    await self._join_pending_cleanup(
                        pending_current,
                        fallback_state=LivePlanningJobState.CANCELLED,
                        fallback_stage="cancelled",
                        fallback_cancellation_requested=True,
                    )
                    return await self.get(job_id, tenant_id)
                return None
            return pending_runtime.snapshot
        # First cancellation: request the real work to stop, then wait for it
        # within the bounded budget. _run's CancelledError handler drains the
        # operation_task and records whether it actually stopped. A worker
        # subprocess is SIGKILLed (its PID death is the provable stop); an
        # in-process task is cancelled.
        if operation_task is not None and not operation_task.done():
            worker = runtime.worker_handle
            if worker is not None:
                await worker.kill_and_confirm(self._cancel_wait_seconds)
            else:
                operation_task.cancel()
        task_done = task is None or task is asyncio.current_task() or task.done()
        if task is not None and not task_done:
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
                # 硬门 B: the shared terminalize predicate — BOTH the registry
                # task and the real operation task must be confirmed done before
                # a final CANCELLED is published.
                drained = self._executors_stopped(current)
        if current is None:
            outcome = None
        else:
            try:
                if drained:
                    await self._join_pending_cleanup(
                        current,
                        fallback_state=LivePlanningJobState.CANCELLED,
                        fallback_stage="cancelled",
                        fallback_cancellation_requested=True,
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
        self._spawn_deferred_cleanup_owners()
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

    async def restore_after_restart(self) -> None:
        """Cold-start / parent-API-crash recovery (C-146 P0-2/P0-4).

        The FastAPI lifespan calls this exactly once on startup, BEFORE any
        request can reach a durable job:

        1. ``_discover_and_stop_orphan_workers`` — find every real orphaned
           worker process group (state-file identity + on-disk marker files),
           AUTHENTICATE each via its unique marker nonce so a reused PGID owned
           by an unrelated process is never killed, SIGKILL the whole group and
           confirm every member died, then quarantine the owning record as an
           orphan so it is isolated, never replayed, and reclaimed only by
           bounded retention.
        2. ``_reap_stale_marker_files`` — drop marker files for groups that are
           already dead (best effort).
        3. Restore the unique cleanup owner/reaper for every durable
           quarantined + pending_terminal record (including ones deferred from
           the loop-less construction in ``__init__``), so a cold boot
           auto-terminates them without waiting for a key request/query/close.

        Idempotent and safe to call again: a second call only re-scans for
        workers that (re)appeared after the first boot.
        """
        await self._discover_and_stop_orphan_workers()
        self._reap_stale_marker_files()
        self._spawn_deferred_cleanup_owners()
        async with self._lock:
            runtimes = list(self._records.values())
        for runtime in runtimes:
            if runtime.pending_terminal is not None or runtime.quarantined:
                # C-146 P0-3 (RETURN 7de8cf3e): a record whose durable worker
                # identity was deferred at load was QUARANTINED as an orphan by
                # ``_discover_and_stop_orphan_workers`` above. When that discovery
                # authenticated + killed + CONFIRMED the group dead, the executor
                # is provably gone and the loader's OWN resolution may settle the
                # record from durable facts (a formal activation interrupted by
                # the crash -> restart_cancelled); otherwise (unconfirmed death or
                # a non-orphan quarantine) the record stays quarantined and the
                # bounded cleanup reconcile owns it.
                if (
                    runtime.quarantined
                    and runtime.quarantine_stage == _QUARANTINE_ORPHAN_STAGE
                    and runtime.hard_stopped
                    and runtime.orphan_authenticated is True
                    and runtime.orphan_death_confirmed is True
                    and runtime.worker_pgid is not None
                    and runtime.worker_marker
                    and runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
                ):
                    async with self._lock:
                        self._resolve_cold_booted_record_locked(runtime)
                self._ensure_cleanup_owner(runtime)
            elif runtime.worker_pgid is not None and runtime.worker_marker:
                if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES:
                    # C-146 P0-3 (RETURN 7de8cf3e): this record's durable worker
                    # identity was deferred at load (resolution must not run over
                    # a possibly-live orphan). Its orphan group has NOW been
                    # discovered + killed (or was already gone), so the executor
                    # is provably dead and the record can be settled with the
                    # loader's OWN resolution — a cold-booted durable PGID/marker
                    # is never terminalized/pruned before zero-request orphan
                    # recovery.
                    #
                    # C-146 P0-3 supplement: resolution is honest ONLY when the
                    # durable per-identity facts PROVE the group was both
                    # authenticated (the marker nonce was observed in its command
                    # lines / the ps query succeeded) AND death-confirmed (the
                    # whole group returned ESRCH within the confirm budget). An
                    # orphan that was never authenticated (auth failure or ps-query
                    # failure) has an executor whose death is NOT provable — it may
                    # still be live. The terminal resolver is never called to guess
                    # a label over it: the record is quarantined as an orphan
                    # (isolated, non-terminal) and stays durable so consecutive
                    # cold starts keep re-checking the same group.
                    if runtime.orphan_authenticated and runtime.orphan_death_confirmed:
                        async with self._lock:
                            self._resolve_cold_booted_record_locked(runtime)
                    else:
                        async with self._lock:
                            current = self._records.get(runtime.snapshot.id)
                            if (
                                current is not None
                                and current.snapshot.state
                                not in TERMINAL_LIVE_PLANNING_JOB_STATES
                            ):
                                current.quarantined = True
                                current.quarantine_stage = _QUARANTINE_ORPHAN_STAGE
                                current.hard_stopped = bool(
                                    current.orphan_death_confirmed
                                )
                                current.generation += 1
                                current.snapshot = current.snapshot.model_copy(
                                    update={
                                        "stage": _QUARANTINE_ORPHAN_STAGE,
                                        "error": (
                                            "live planning job executor was orphaned by "
                                            "a parent crash without confirmed death"
                                        ),
                                        "updated_at": self._utc_now(),
                                    }
                                )
                                with suppress(Exception):
                                    self._persist_locked()
                                self._changed.notify_all()
                        self._ensure_cleanup_owner(runtime)

    async def close(self) -> None:
        """Close the registry, reusing the exact durable drain state machine as a
        single-job cancel (P0-4).

        The final CANCELLED label is NEVER published before both the real
        ``operation_task`` and the registry ``task`` of every still-active record
        are confirmed stopped. The cancel intent is persisted durably FIRST;
        then both executors are physically cancelled and truly awaited within the
        bounded budget. If an operation swallows CancelledError and keeps running
        past the budget, the job fails closed into a non-terminal
        ``cancel_pending`` state (``closing``/``cancel_timed_out``) and the
        cleanup stays owned — a repeated ``close()`` joins the same cleanup and
        a cold restart fail-closes the record to ``restart_cancelled``. The
        ``operation_task`` reference is never dropped while the operation lives.
        """
        self._spawn_deferred_cleanup_owners()
        async with self._changed:
            # C-145 P0 supplement / C-146 P0 supplement (P0-4): a quarantined
            # record has NO provable terminal outcome — close() must never guess
            # a CANCELLED label for it. Every quarantine stage
            # (``isolated_ambiguous_cancel``, ``quarantine_intent_uncommitted``,
            # ``quarantine_hard_stopped``) is excluded from the active set so the
            # closing isolation, the durable CANCELLED intent and the executor
            # settle all leave it untouched; a later cold start still sees the
            # same quarantine.
            active = tuple(
                runtime
                for runtime in self._records.values()
                if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
                and not runtime.quarantined
            )
            if not active:
                self._closed = True
                return
            if not self._closed:
                # P0-4: persist the cancellation intent durably BEFORE stopping
                # any real executor. ``_closed`` is TRANSACTIONAL with that
                # durable intent: it flips to True only once the isolation has
                # been committed for every still-active record, so it never means
                # "close() was called" but always "every record needing cleanup
                # has a durable owner". A pre-commit failure (even after the
                # bounded retries) rolls the in-memory markers AND ``_closed``
                # back and leaves every executor untouched; a post-commit failure
                # means the intent is already on disk, so proceed to stop the
                # executors. A later close() then re-persists the intent because
                # ``_closed`` is still False.
                previous_snapshots = [runtime.snapshot for runtime in active]
                previous_pending_terminals = [runtime.pending_terminal for runtime in active]
                for runtime in active:
                    if not runtime.snapshot.cancel_pending:
                        runtime.snapshot = runtime.snapshot.model_copy(
                            update={
                                "cancel_pending": True,
                                "cancellation_requested": True,
                                "stage": "closing",
                                "updated_at": self._utc_now(),
                            }
                        )
                    # C-145 P0 supplement: close()'s FIRST durable intent carries
                    # the full CANCELLED/cancelled outcome in the SAME atomic
                    # commit as the closing isolation — a crash right after this
                    # commit cold-starts straight to the cancel outcome. A record
                    # that already holds a DURABLE intent (e.g. a deadline cleanup)
                    # keeps it: the first intent wins, never overwritten.
                    if runtime.pending_terminal is None:
                        runtime.pending_terminal = _PendingTerminalOutcome(
                            state=LivePlanningJobState.CANCELLED,
                            stage="cancelled",
                            cancellation_requested=True,
                        )
                try:
                    await self._persist_locked_with_bounded_retry()
                except Exception:
                    for runtime, previous_snapshot in zip(active, previous_snapshots, strict=True):
                        runtime.snapshot = previous_snapshot
                    for runtime, previous_pending in zip(
                        active, previous_pending_terminals, strict=True
                    ):
                        runtime.pending_terminal = previous_pending
                    raise
                self._closed = True
                self._changed.notify_all()
        # Physically cancel BOTH executors of every still-active record FIRST —
        # before any settle releases a slot — so a queued job can never start
        # running mid-close and trip the cancellation_requested gate. Then truly
        # await all of them within the bounded budget, and settle each one:
        # publish CANCELLED only when both executors are confirmed stopped,
        # otherwise fail closed into the non-terminal cancel_pending isolation.
        for runtime in active:
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                continue
            operation_task = runtime.operation_task
            worker = runtime.worker_handle
            if operation_task is not None and not operation_task.done():
                if worker is not None:
                    # SIGKILL + waitpid — the provable stop for a worker.
                    await worker.kill_and_confirm(self._cancel_wait_seconds)
                else:
                    operation_task.cancel()
            task = runtime.task
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        pending_tasks = tuple(
            runtime.task
            for runtime in active
            if runtime.task is not None and not runtime.task.done()
        )
        if pending_tasks:
            await asyncio.wait(
                pending_tasks,
                timeout=self._cancel_wait_seconds + 0.1,
            )
        for runtime in active:
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                continue
            async with self._lock:
                current = self._records.get(runtime.snapshot.id)
            if current is None:
                continue
            if self._executors_stopped(current):
                await self._join_pending_cleanup(
                    current,
                    fallback_state=LivePlanningJobState.CANCELLED,
                    fallback_stage="cancelled",
                    fallback_cancellation_requested=True,
                )
            else:
                await self._mark_cancel_stuck(current)

    async def _run(
        self,
        runtime: _RuntimeJob,
        operation: LiveJobOperation | LiveJobWorkerCommand,
    ) -> None:
        operation_task: asyncio.Task[dict[str, Any]] | None = None
        generation = runtime.generation
        try:
            remaining = runtime.deadline_monotonic - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await self._slots.acquire()
            # P0-1: the admission permit now belongs to THIS runtime until the
            # REAL operation task is done — never released while a stubborn
            # operation could still be writing side effects.
            runtime.slot_held = True
            await self._update_running(
                runtime,
                "interpreting_requirement",
                5,
                generation=generation,
            )
            report = _RegistryProgressReporter(self, runtime, generation)

            async def invoke_operation() -> dict[str, Any]:
                # C-146 hard-stop gate (12e35d45 门 1): a LiveJobWorkerCommand
                # runs in a REAL subprocess (owned PID), so the watchdog can
                # prove its death via SIGKILL + waitpid and the freeze of any
                # external probe it was writing. A plain callable stays
                # in-process and can never be proven dead past cancellation.
                if isinstance(operation, LiveJobWorkerCommand):
                    return await self._run_worker_command(runtime, operation)
                return await operation(report)

            operation_task = asyncio.create_task(
                invoke_operation(),
                name=f"tripchord:{runtime.snapshot.id}:operation",
            )
            runtime.operation_task = operation_task
            # C-146 P0 supplement (fourth P0): the single hard-stop watchdog is
            # armed once a real executor is live. Past the absolute deadline +
            # grace it quarantines the operation even under a permanent storage
            # failure, so the executor and its side effects have a hard bound.
            self._ensure_hard_stop_watchdog()

            def _on_operation_done(task: asyncio.Task[dict[str, Any]]) -> None:
                # Consume any un-retrieved exception, then release the admission
                # permit only now that the real operation is confirmed done.
                # This is the NORMAL release path: a stubborn operation that
                # swallows CancelledError and later stops (via a bounded
                # cancel/close or its own natural end) releases here, never from
                # the runner's finally.
                self._consume_task_result(task)
                self._maybe_release_slot(runtime)

            operation_task.add_done_callback(_on_operation_done)
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
            # CANCELLED is owned by cancel()/close(), which use the shared
            # _executors_stopped predicate (registry task AND operation task both
            # done) and only then terminalize (or fail closed). Recording the
            # drain result here and re-raising keeps this task genuinely
            # cancelled so the real work and the registry record never disagree.
            runtime.cancel_drain_succeeded = await self._cancel_and_drain_operation(runtime)
            raise
        except TimeoutError as exc:
            failure = _safe_failure_diagnostic(
                exc,
                code_override=LivePlanningSafeFailureCode.DEADLINE_EXCEEDED,
            )
            # 硬门 C: the final FAILED label is NEVER published before the real
            # operation is confirmed stopped. Persist a durable
            # timeout/cancel-pending isolation FIRST, then cancel + drain the
            # operation; publish FAILED only when it is truly stopped. If the
            # operation swallows CancelledError past the budget, fail closed into
            # a non-terminal timeout/cancel-pending state and keep cleanup
            # ownership (a retry or close joins it).
            intent_committed = False
            async with self._lock:
                if runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES:
                    runtime.snapshot = runtime.snapshot.model_copy(
                        update={
                            "cancel_pending": True,
                            "cancellation_requested": True,
                            "stage": "timeout_pending",
                            "updated_at": self._utc_now(),
                        }
                    )
                    # C-145 P0 supplement: the FIRST durable intent of a deadline
                    # entry carries the full FAILED/deadline_exceeded outcome —
                    # pending state, stage, error and the safe-failure diagnostic —
                    # in the SAME atomic commit as the timeout isolation. Only a
                    # successful commit ever permits the cancel/drain below.
                    runtime.pending_terminal = _PendingTerminalOutcome(
                        state=LivePlanningJobState.FAILED,
                        stage="deadline_exceeded",
                        error="TimeoutError: live planning job deadline exceeded",
                        safe_failure=failure,
                        cancellation_requested=True,
                    )
                    try:
                        await self._persist_locked_with_bounded_retry()
                        intent_committed = True
                    except Exception:
                        # C-146 P0 supplement (P0-3): the FIRST durable intent is
                        # the HARD PRECONDITION for stopping the executor. When
                        # every bounded pre-commit attempt failed, the unique
                        # owner, the real operation and the capacity lease stay
                        # untouched — NO cancel, NO drain, NO slot release, NO
                        # terminal claim. The in-memory isolation and the
                        # FAILED/deadline_exceeded intent are the observable
                        # recoverable owner; the stage flips to the explicit
                        # retry state and the cleanup owner re-commits the intent
                        # before ANY stop/drain. The runner NEVER restores the
                        # pre-timeout RUNNING snapshot over the live executor.
                        runtime.intent_persist_pending = True
                        # C-146 P0 supplement (fourth P0): start the bounded
                        # STATE budget clock. The bounded burst inside
                        # ``_persist_locked_with_bounded_retry`` already
                        # consumed its attempts; the owner continues counting so
                        # the total stays bounded by attempts and wall-clock.
                        loop_now = asyncio.get_running_loop().time()
                        if runtime.intent_persist_started_monotonic == 0.0:
                            runtime.intent_persist_started_monotonic = loop_now
                        runtime.intent_persist_attempts += self._cancel_isolation_persist_attempts
                        runtime.snapshot = runtime.snapshot.model_copy(
                            update={
                                "stage": _DEADLINE_INTENT_PERSIST_PENDING_STAGE,
                                "updated_at": self._utc_now(),
                            }
                        )
                else:
                    # A concurrent terminalize already settled the record; the
                    # drain path below is a harmless no-op.
                    intent_committed = True
            if not intent_committed:
                # C-146 P0 supplement (P0-3): the bounded cleanup owner
                # auto-continues the persistence (same saturating backoff), and
                # only after the intent commits does it stop/drain the executor
                # and terminalize. close()/cancel()/same-key join this ordering
                # and never create a second owner.
                self._ensure_cleanup_owner(runtime)
                return
            drained = await self._cancel_and_drain_operation(runtime)
            operation_stopped = operation_task is None or operation_task.done()
            if drained and operation_stopped:
                with suppress(Exception):
                    # The terminal persist failed too (same permanent write
                    # failure). The in-memory timeout/cancel-pending isolation is
                    # retained as the observable owner and the runner exits
                    # without restoring a pre-timeout RUNNING snapshot over the
                    # already-stopped operation.
                    await self._finish(
                        runtime,
                        LivePlanningJobState.FAILED,
                        stage="deadline_exceeded",
                        error="TimeoutError: live planning job deadline exceeded",
                        safe_failure=failure,
                        expected_generation=generation,
                    )
            else:
                with suppress(Exception):
                    # Same permanent write failure: keep the in-memory
                    # timeout/cancel-pending isolation as the recoverable owner;
                    # the still-alive operation stays drained and the runner
                    # exits without abandoning it or restoring RUNNING.
                    # C-145 P0: the durable pending outcome is FAILED /
                    # deadline_exceeded with the safe-failure diagnostic — never
                    # a guessed CANCELLED label — so a late-stop auto-collect (or
                    # a cold restart) lands on the true failure semantics.
                    await self._mark_cancel_stuck(
                        runtime,
                        stage="timeout_pending",
                        error=(
                            "live planning operation did not stop within the deadline "
                            "cleanup budget; the job stays non-terminal and the "
                            "operation is isolated"
                        ),
                        pending_state=LivePlanningJobState.FAILED,
                        pending_stage="deadline_exceeded",
                        pending_error="TimeoutError: live planning job deadline exceeded",
                        pending_safe_failure=failure,
                        pending_cancellation_requested=True,
                    )
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
            # P0-1: never release while the real operation may still be alive.
            # The finally is only the safety net for paths where the permit was
            # acquired but no operation was ever started (e.g. _update_running
            # failed pre-start); the operation done-callback owns the release
            # once the real executor is confirmed done.
            self._maybe_release_slot(runtime)

    def _workers_dir(self) -> Path | None:
        """The sibling directory holding durable worker-orphan marker files.

        C-146 hard-stop gate (12e35d45 门 2): marker files live next to the
        state file (``.``<name>``.workers/``) so a cold start can find and
        authenticate an orphaned worker even when the API process was SIGKILLed
        before it could persist anything. Returns None when the registry has no
        durable state path (pure in-memory registry — no orphan tracking)."""
        if self._state_path is None:
            return None
        return self._state_path.parent / f".{self._state_path.name}.workers"

    def _marker_file_for(self, job_id: str) -> Path | None:
        workers_dir = self._workers_dir()
        if workers_dir is None:
            return None
        return workers_dir / f"{job_id}.json"

    @staticmethod
    def _group_commands(pgid: int) -> list[str]:
        """The command lines of every process in group ``pgid`` via ``ps``.

        Used to AUTHENTICATE an orphan worker before killing it: the group is
        only ever killed when its command line provably contains the unique
        marker nonce the registry handed that job — a reused PGID owned by an
        unrelated process is never touched. ``ps`` is POSIX-standard on both
        Linux and macOS."""
        if sys.platform == "linux":
            commands: list[str] = []
            for pid in _linux_process_group_pids(pgid):
                try:
                    command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                    if command:
                        commands.append(command.replace(b"\0", b" ").decode("utf-8", "replace"))
                except (FileNotFoundError, PermissionError, OSError):
                    continue
            if commands:
                return commands
            # procfs can briefly race a just-reparented process.  The procps
            # all-process listing is a bounded fallback for that window.
            try:
                completed = subprocess.run(
                    ["ps", "-e", "-o", "pgid=,command="],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError:
                return []
            if completed.returncode != 0:
                return []
            prefix = str(pgid)
            return [
                line.split(None, 1)[1]
                for line in completed.stdout.splitlines()
                if line.startswith(prefix) and len(line.split(None, 1)) == 2
            ]
        try:
            # procps ``ps -g`` means real *user* group on Linux, unlike the
            # BSD/macOS spelling where it selects process groups.  Using the
            # explicit procps selector is required for cold-boot orphan
            # authentication on Linux; an empty result must never be treated
            # as proof that the durable worker identity is safe to kill.
            group_selector = "--pgrp" if sys.platform == "linux" else "-g"
            completed = subprocess.run(
                ["ps", "-o", "command=", group_selector, str(pgid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []
        if completed.returncode != 0:
            return []
        return [line for line in completed.stdout.splitlines() if line]

    @staticmethod
    def _find_pgid_by_marker(marker: str) -> int | None:
        """Recover the PGID of a live process whose command line carries ``marker``.

        C-146 P0-2: a parent-API crash can land between
        ``create_subprocess_exec`` and the worker's own atomic marker-file
        write. In that window the ONLY durable trace of the orphan is the
        spawn-intent marker nonce (no PGID). Scanning every process command line
        for the 32-hex nonce — which never appears in an unrelated argv —
        recovers the process group so it can be authenticated and cleaned."""
        if sys.platform == "linux":
            proc_dir = Path("/proc")
            if proc_dir.is_dir():
                for entry in proc_dir.iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        command = (entry / "cmdline").read_bytes()
                        if marker not in command.decode("utf-8", "replace"):
                            continue
                        stat_text = (entry / "stat").read_text(encoding="ascii")
                        _, remainder = stat_text.split(") ", 1)
                        return int(remainder.split()[2])
                    except (FileNotFoundError, PermissionError, OSError, ValueError):
                        continue
            return None
        try:
            completed = subprocess.run(
                ["ps", "-e", "-o", "pid=,pgid=,command="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        for line in completed.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            _pid_str, pgid_str, command = parts
            if marker in command:
                try:
                    return int(pgid_str)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _write_spawn_intent(marker_file: Path, marker: str) -> None:
        """Atomically write the durable spawn-intent marker (nonce, NO pgid).

        C-146 P0-2: written BEFORE the worker subprocess exists so a parent-API
        crash in the spawn window leaves a recoverable record of the orphan. The
        worker overwrites this same path atomically with its pid/pgid on
        startup, so a cold start can authenticate + kill the real group either
        from the full marker file or, when only the intent survived, by scanning
        process command lines for the nonce."""
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker_file.parent / f".{marker_file.name}.{os.getpid()}.intent.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                {"marker": marker},
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker_file)

    def _wake_hard_stop_watchdog(self) -> None:
        """Wake the single hard-stop watchdog so it re-scans immediately.

        C-146 P0-3: the watchdog sleeps until the earliest known deadline or an
        explicit wake. Arming a new (earlier) execution bound, a hard-stop
        wrapper completing, or quarantine retention freeing a slot all set this
        so the loop never sleeps past a deadline that became due while it
        waited. No-op when the loop is not currently sleeping on the event."""
        wake = self._hard_stop_wake
        if wake is not None:
            wake.set()

    async def _discover_and_stop_orphan_workers(self) -> None:
        """Cold-start / parent-API-crash recovery: stop real orphaned workers.

        C-146 hard-stop gate (12e35d45 门 2): the API process may be SIGKILLed
        mid-run, orphaning a live worker process group whose durable record
        (state file worker identity + on-disk marker file) survives. This scans
        both sources, AUTHENTICATES each group via its marker nonce (so a reused
        PGID owned by an unrelated process is never killed), SIGKILLs the whole
        authenticated group and confirms every member died, then quarantines the
        owning record as an orphan so it is isolated, never replayed, and
        reclaimed only by bounded retention. Returns without blocking on any
        live operation; a group that cannot be confirmed dead stays quarantined
        and is re-attempted by the next startup.

        C-146 P0-3: the per-identity auth and exit-confirmation are DURABLY
        persisted (``orphan_authenticated`` / ``orphan_death_confirmed``) and are
        monotonic — a re-check never downgrades an earlier authenticated /
        death-confirmed observation. A group that cannot be AUTHENTICATED (marker
        not found in its command lines, or the ``ps`` query itself failed) is
        never killed and its record is still quarantined as an orphan so
        consecutive cold starts keep re-checking the same group; no terminal
        resolver ever runs over an executor whose death was never confirmed.
        If a separate no-signal probe returns ESRCH, there is no live group to
        authenticate or kill; the stale process identity is discarded and only
        independent durable job facts may resolve the record."""
        candidates: list[tuple[int, str, Path | None, _RuntimeJob | None]] = []
        if self._state_path is not None:
            workers_dir = self._workers_dir()
            if workers_dir is not None and workers_dir.is_dir():
                for marker_path in workers_dir.iterdir():
                    if not marker_path.is_file() or marker_path.suffix != ".json":
                        continue
                    try:
                        info = json.loads(marker_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    pgid = info.get("pgid")
                    marker = info.get("marker")
                    if not isinstance(marker, str) or not marker:
                        continue
                    if not isinstance(pgid, int) or pgid <= 0:
                        # C-146 P0-2: spawn-intent only — the parent crashed
                        # before the worker could write its pid/pgid. Recover the
                        # real group by scanning every process command line for
                        # the nonce; when no live process carries it, the spawn
                        # never happened (or the worker exited without ever
                        # writing a full marker) and the stale intent is dropped.
                        pgid = self._find_pgid_by_marker(marker)
                        if pgid is None:
                            with suppress(OSError):
                                marker_path.unlink(missing_ok=True)
                            continue
                    candidates.append((pgid, marker, marker_path, None))
        async with self._lock:
            runtimes = list(self._records.values())
        for runtime in runtimes:
            if (
                runtime.worker_pgid is not None
                and runtime.worker_marker
                and runtime.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
            ):
                candidates.append(
                    (runtime.worker_pgid, runtime.worker_marker, None, runtime)
                )
        # De-duplicate by (pgid, marker). A marker file AND a live registry
        # record can describe the SAME authenticated orphan group; prefer the
        # candidate that carries a runtime record so the owning job is
        # quarantined as an orphan (a marker-file-only candidate has no record
        # to quarantine), but keep the marker file path for cleanup.
        seen: dict[tuple[int, str], int] = {}
        unique: list[tuple[int, str, Path | None, _RuntimeJob | None]] = []
        for (
            candidate_pgid,
            candidate_marker,
            candidate_marker_path,
            candidate_runtime,
        ) in candidates:
            key = (candidate_pgid, candidate_marker)
            index = seen.get(key)
            if index is None:
                seen[key] = len(unique)
                unique.append(
                    (candidate_pgid, candidate_marker, candidate_marker_path, candidate_runtime)
                )
            elif unique[index][3] is None and candidate_runtime is not None:
                unique[index] = (
                    candidate_pgid,
                    candidate_marker,
                    unique[index][2],
                    candidate_runtime,
                )
        for unique_pgid, unique_marker, unique_marker_path, unique_runtime in unique:
            commands = self._group_commands(unique_pgid)
            authenticated = any(unique_marker in line for line in commands)
            # Kill the whole authenticated group; confirm every member died.
            # An unauthenticated group (marker not found in its command lines,
            # or the ps query itself failed) is NEVER killed — a reused PGID
            # owned by an unrelated process is never touched.
            confirmed = False
            group_absent = False
            if authenticated:
                with suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(unique_pgid, signal.SIGKILL)
                    deadline = asyncio.get_running_loop().time() + self._hard_stop_confirm_seconds
                    while asyncio.get_running_loop().time() < deadline:
                        linux_live = _linux_group_has_live_member(unique_pgid)
                        if linux_live is False:
                            confirmed = True
                            break
                        try:
                            os.killpg(unique_pgid, 0)
                        except ProcessLookupError:
                            confirmed = True
                            break
                        except PermissionError:
                            # macOS can briefly answer EPERM for a reparented orphan
                            # that is dying/being reaped (its group still exists but
                            # is no longer signalable). That is transient — the group
                            # becomes ESRCH within the confirm budget — so keep
                            # polling instead of giving up. The deadline still bounds
                            # the wait; an EPERM that persists until it expires means
                            # "cannot prove dead" and the group stays quarantined for
                            # the next startup.
                            pass
                        await asyncio.sleep(0.01)
            elif unique_runtime is not None:
                # A marker-free command scan can mean either a failed ``ps`` /
                # reused live PGID OR that the old worker exited and removed its
                # marker before this boot. Probe existence without signalling.
                # ESRCH proves there is currently no process group to authenticate
                # or kill; a live/EPERM group remains isolated. When an earlier
                # boot authenticated this exact identity, ESRCH completes the
                # durable authenticated-death proof. Otherwise the same current
                # absence lets this boot discard the stale worker identity and
                # resolve only from the record's independent durable facts.
                try:
                    os.killpg(unique_pgid, 0)
                except ProcessLookupError:
                    if unique_runtime.orphan_authenticated is True:
                        confirmed = True
                    else:
                        group_absent = True
                except (PermissionError, OSError):
                    pass
            if unique_runtime is not None:
                # C-146 P0-3: quarantine EVERY cold-booted record with a durable
                # worker identity — authenticated OR not. An unauthenticated
                # group that still exists (marker not found in its command lines,
                # or the ps query itself failed) is never killed, and its record
                # stays ISOLATED with the durable per-identity auth fact persisted.
                # A separately-proven ESRCH group takes the branch below instead.
                async with self._lock:
                    current = self._records.get(unique_runtime.snapshot.id)
                    if (
                        current is not None
                        and current.snapshot.state not in TERMINAL_LIVE_PLANNING_JOB_STATES
                    ):
                        if group_absent:
                            # The old group is already ESRCH, so no unrelated
                            # process is signalled and marker authentication is
                            # unnecessary. Clear the stale process identity, then
                            # run the SAME cold-boot resolver used for records that
                            # never carried a worker identity. Formal activation,
                            # durable cancel intent, or deadline facts decide the
                            # result; an ambiguous immediate job is still
                            # quarantined rather than guessed terminal. Persist
                            # before readiness so no request observes stale QUEUED.
                            current.worker_pgid = None
                            current.worker_marker = None
                            current.worker_probe = None
                            current.worker_execution_capability = None
                            current.orphan_authenticated = None
                            current.orphan_death_confirmed = None
                            current.quarantined = False
                            current.quarantine_stage = None
                            current.hard_stopped = False
                            self._resolve_cold_booted_record_locked(current)
                            self._persist_locked()
                            self._changed.notify_all()
                            if unique_marker_path is not None:
                                with suppress(OSError):
                                    unique_marker_path.unlink(missing_ok=True)
                            continue
                        # C-146 P0-3: the durable auth/death-confirm facts are
                        # MONOTONIC per identity — a cold start only ever STRENGTHENS
                        # them (a later boot's re-check that fails to authenticate,
                        # e.g. a reused PGID / ps failure / an already-ESRCH group,
                        # never downgrades an earlier authenticated + death-confirmed
                        # observation). A death-confirmed record is already provably
                        # settled and needs no re-quarantine.
                        if current.orphan_death_confirmed:
                            self._changed.notify_all()
                        else:
                            # A legacy pre-P0-3 file proves a confirmed kill via
                            # an ORPHAN-stage ``hard_stopped=True`` alone; that
                            # durable proof also proves the old recovery path had
                            # authenticated the marker before killing. Migrate the
                            # pair together so a new file can never carry the
                            # impossible death=True/authenticated=False shape.
                            legacy_confirmed = (
                                current.orphan_authenticated is None
                                and current.orphan_death_confirmed is None
                                and current.quarantine_stage
                                == _QUARANTINE_ORPHAN_STAGE
                                and current.hard_stopped
                            )
                            authenticated_so_far = bool(
                                current.orphan_authenticated
                                or authenticated
                                or legacy_confirmed
                            )
                            confirmed_so_far = (
                                legacy_confirmed
                                or current.orphan_death_confirmed
                                or confirmed
                            )
                            current.quarantined = True
                            current.quarantine_stage = _QUARANTINE_ORPHAN_STAGE
                            current.hard_stopped = confirmed_so_far
                            current.worker_pgid = unique_pgid
                            current.worker_marker = unique_marker
                            current.orphan_authenticated = authenticated_so_far
                            current.orphan_death_confirmed = confirmed_so_far
                            current.generation += 1
                            current.snapshot = current.snapshot.model_copy(
                                update={
                                    "stage": _QUARANTINE_ORPHAN_STAGE,
                                    "error": (
                                        "live planning job executor was orphaned by a parent crash"
                                    ),
                                    "updated_at": self._utc_now(),
                                }
                            )
                            with suppress(Exception):
                                self._persist_locked()
                            self._changed.notify_all()
            if unique_marker_path is not None and (authenticated or confirmed):
                with suppress(OSError):
                    unique_marker_path.unlink(missing_ok=True)

    async def _kill_orphan_group(self, pgid: int, marker: str) -> bool:
        """SIGKILL a cold-booted orphan group and confirm it died in budget.

        C-146 P0-5: shared with ``_cancel_and_drain_operation`` for a cold-booted
        runtime whose durable worker identity points at a LIVE, marker-authenticated
        process group. Returns True only when the whole group is gone within the
        hard-stop confirm budget; False means the group could not be proven dead
        and the caller must fail closed (never terminalize / release a permit over
        live external side effects)."""
        confirmed = False
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
            deadline = asyncio.get_running_loop().time() + self._hard_stop_confirm_seconds
            while asyncio.get_running_loop().time() < deadline:
                linux_live = _linux_group_has_live_member(pgid)
                if linux_live is False:
                    confirmed = True
                    break
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    confirmed = True
                    break
                except PermissionError:
                    # macOS can briefly answer EPERM for a reparented orphan that
                    # is dying/being reaped — transient; keep polling within the
                    # bounded budget.
                    pass
                await asyncio.sleep(0.01)
        return confirmed

    def _reap_stale_marker_files(self) -> None:
        """Remove marker files for groups that are already dead (best effort).

        Called from startup after orphan discovery, so a marker file whose
        worker exited cleanly between process death and this boot never lingers.
        Marker files for a LIVE authenticated group are left for the explicit
        ``_discover_and_stop_orphan_workers`` kill path."""
        if self._state_path is None:
            return
        workers_dir = self._workers_dir()
        if workers_dir is None or not workers_dir.is_dir():
            return
        for marker_path in workers_dir.iterdir():
            if not marker_path.is_file() or marker_path.suffix != ".json":
                continue
            try:
                info = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                with suppress(OSError):
                    marker_path.unlink(missing_ok=True)
                continue
            pgid = info.get("pgid")
            if not isinstance(pgid, int):
                with suppress(OSError):
                    marker_path.unlink(missing_ok=True)
                continue
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                with suppress(OSError):
                    marker_path.unlink(missing_ok=True)
            except PermissionError:
                continue

    @staticmethod
    def _parse_worker_failure(stderr: bytes) -> BaseException | None:
        """Reconstruct a typed failure from the worker's structured marker.

        C-146 P0-1: the worker writes a single bounded, sanitized line
        ``TRIPCHORD_WORKER_FAILURE:{"class": "...", "status_code": N}`` when its
        entry raises. Reconstruct the matching exception type so the job's
        safe-failure diagnostic reflects the REAL operation's cause (an HTTP 503
        from the ready chain, a domain ValueError, ...) rather than a generic
        exit-code error. Returns None when no marker is present."""
        decoded_stderr = stderr.decode("utf-8", "replace")
        for line in decoded_stderr.splitlines():
            if not line.startswith("TRIPCHORD_WORKER_FAILURE:"):
                continue
            try:
                payload = json.loads(line.split(":", 1)[1])
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            klass = payload.get("class")
            if klass == "HTTPException":
                try:
                    from starlette.exceptions import HTTPException
                except ImportError:  # pragma: no cover - starlette is a dependency
                    from fastapi import HTTPException
                status_code = 500
                try:
                    status_code = int(payload.get("status_code") or 500)
                except (TypeError, ValueError):
                    status_code = 500
                return HTTPException(
                    status_code=status_code,
                    detail="live planning operation reported an HTTP failure",
                )
            if isinstance(klass, str) and klass:
                return RuntimeError(f"live planning job worker reported: {klass}")
        return None

    async def _confirm_worker_group_exit(self, runtime: _RuntimeJob) -> None:
        """C-146 P0-2: prove the worker's WHOLE process tree exited.

        Called on BOTH worker exit paths (clean leader exit AND non-zero exit)
        BEFORE any terminal label may be written. ``kill_and_confirm`` SIGKILLs
        any survivor of the worker's process group and confirms the group is
        empty within the bounded budget. When confirmation returns False, times
        out, or raises, the job KEEPS its non-terminal state, its durable worker
        identity and its admission permit, and the confirmation AUTO-RETRIES on
        a saturating + fixed-window-bounded backoff — a terminal state is never
        written while any group member may still be writing external side
        effects. The job's deadline (bounded by ``_run``'s wait) is the terminal
        bound, and even then the timeout path only terminalizes after a
        confirmed drain. Returns once the group is provably empty."""
        if runtime.worker_handle is None:
            return
        logged = False
        while True:
            confirmed = False
            try:
                confirmed = await runtime.worker_handle.kill_and_confirm(
                    self._hard_stop_confirm_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A persistent confirmation EXCEPTION is the same "cannot prove
                # dead" outcome: never surface an unhandled task exception, keep
                # the record non-terminal and keep retrying.
                if not logged:
                    logger.warning(
                        "worker-exit group confirm raised for job %s: %s",
                        runtime.snapshot.id,
                        type(exc).__name__,
                    )
                    logged = True
                confirmed = False
            if confirmed:
                # This process owns the handle/marker that was durably attached
                # to this runtime, so a successful whole-group confirmation is
                # also the strongest possible per-identity auth/death fact. Keep
                # it on the runtime so the ensuing terminal persist survives a
                # crash/reload without reclassifying a proven-dead executor as
                # an unauthenticated orphan.
                runtime.orphan_authenticated = True
                runtime.orphan_death_confirmed = True
                return
            now = asyncio.get_running_loop().time()
            wait_until = self._worker_exit_next_attempt_locked(runtime, now)
            # The registry runner owns the deadline timeout concurrently.  Do
            # NOT clamp this sleep to the (possibly already-expired) job
            # deadline: doing so turns a persistent False/exception into a hot
            # loop exactly when the deadline passes.  This confirmation owner
            # keeps its own saturating/fixed-window cadence until cancellation
            # or a provable group death; the timeout path remains fail-closed.
            await asyncio.sleep(max(0.001, wait_until - now))

    async def _run_worker_command(
        self,
        runtime: _RuntimeJob,
        command: LiveJobWorkerCommand,
    ) -> dict[str, Any]:
        """Execute a ``LiveJobWorkerCommand`` in a real subprocess worker.

        C-146 hard-stop gate (12e35d45 门 1): the operation runs in a fresh OS
        process with an owned PID/PGID (``runtime.worker_handle``), so the
        hard-stop watchdog can PROVE its death via SIGKILL of the WHOLE process
        group + waitpid and the permanent freeze of any external probe it was
        writing. The worker script is spawned by absolute path; the entry module
        is loaded by path by the worker itself. ``probe_path`` (when set) is
        injected into the entry call so a test operation can append to an
        external, registry-independent side-effect file that only the process
        death freezes.

        C-146 hard-stop gate (12e35d45 门 2): the worker is spawned with
        ``start_new_session=True`` so it is the leader of its OWN session /
        process group (PGID == PID) even before ``os.setsid()`` in the script.
        A unique marker nonce is passed as an argv flag and durably recorded
        (best-effort persist under the lock + an on-disk marker file), so a
        parent-API crash can be recovered by authenticating the group via its
        command line before cleaning the real orphan."""
        env = dict(os.environ)
        pythonpath = [entry for entry in sys.path if entry]
        existing_pythonpath = env.get("PYTHONPATH")
        if existing_pythonpath:
            pythonpath = [existing_pythonpath, *pythonpath]
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        # C-146 hard-stop gate (12e35d45 门 1): the worker subprocess must never
        # construct the job registry. It only runs the operation; the API process
        # owns the durable registry, and a second instance would load (and under
        # old-v3 migration, rewrite) the same state file — a concurrent second
        # writer. The flag makes ``tripchord.main`` skip the registry singleton.
        env["TRIPCHORD_LIVE_WORKER_SUBPROCESS"] = "1"
        marker = ""
        marker_file = self._marker_file_for(runtime.snapshot.id)
        if marker_file is not None:
            marker = secrets.token_hex(16)
            # C-146 P0-2: write the durable SPAWN INTENT (unique marker nonce,
            # NO pgid yet) BEFORE the subprocess exists. A parent-API crash
            # landing between ``create_subprocess_exec`` and the worker's own
            # atomic marker-file write otherwise leaves an orphan whose only
            # durable trace is this intent file; a cold start recovers the real
            # group by scanning process command lines for the nonce. The worker
            # overwrites this same path atomically with its pid/pgid on startup.
            self._write_spawn_intent(marker_file, marker)
        # C-146 P0-1 (RETURN 7de8cf3e): the worker needs the durable job identity
        # to bind its model-trace scope / checkpoint request digest to the job the
        # parent registry owns (the same ``job_id`` the in-process reporter
        # exposes). The command is built before ``start_idempotent`` minted the
        # job id, so the registry injects it here at spawn time.
        worker_args = dict(command.args)
        worker_args["job_id"] = runtime.snapshot.id
        if runtime.worker_execution_capability is not None:
            if "formal_execution_capability" in worker_args:
                raise RuntimeError("worker command already carries a formal capability")
            worker_args["formal_execution_capability"] = (
                runtime.worker_execution_capability
            )
        argv = [
            self._worker_python,
            self._worker_module,
            "--module-path",
            command.module_path,
            "--entry",
            command.entry,
            "--args-json",
            json.dumps(worker_args, ensure_ascii=False),
            "--probe-path",
            command.probe_path or "",
            "--marker",
            marker,
            "--marker-file",
            str(marker_file) if marker_file is not None else "",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except BaseException:
            # Spawn failed: remove the stale spawn intent so a cold start never
            # chases a process that does not exist (the intent was written before
            # the spawn attempt).
            if marker_file is not None:
                with suppress(OSError):
                    marker_file.unlink(missing_ok=True)
            raise
        runtime.worker_handle = _SubprocessWorkerHandle(
            process,
            probe_path=command.probe_path,
            marker=marker,
            marker_file=marker_file,
        )
        # Durable worker identity: persisted best-effort so a cold start can
        # re-discover + authenticate + clean the real orphan even if this API
        # process is SIGKILLed mid-run. The marker file on disk is the
        # authoritative orphan record; a transient persist failure here never
        # aborts the live operation.
        runtime.worker_pgid = runtime.worker_handle.pgid
        runtime.worker_marker = marker
        runtime.worker_probe = command.probe_path
        # C-146 P0-3: this is a NEW worker identity; reset the durable orphan
        # facts so a stale authenticated/death-confirmed result from a PREVIOUS
        # worker never settles a record whose current executor has not been
        # cold-boot checked.
        runtime.orphan_authenticated = None
        runtime.orphan_death_confirmed = None
        if marker_file is not None:
            try:
                async with self._lock:
                    if self._records.get(runtime.snapshot.id) is runtime:
                        # C-146 hard-stop gate (12e35d45 门 2): persist the durable
                        # worker identity WITHOUT advancing the durable snapshot to
                        # RUNNING. RUNNING is a memory-only live observation; the
                        # cold-start recovery (``restart_cancelled`` for a formal
                        # activation) is honest only because a formal job's snapshot
                        # never durably advanced past QUEUED. A durable RUNNING here
                        # would turn an interrupted formal job into a quarantine
                        # orphan instead of its provable restart_cancelled tombstone.
                        durable_snapshot = runtime.snapshot.model_copy(
                            update={"state": LivePlanningJobState.QUEUED}
                        )
                        self._persist_locked(
                            snapshot_overrides={runtime.snapshot.id: durable_snapshot}
                        )
            except Exception:
                pass
        stdout, stderr = await process.communicate()
        # C-146 P0-2 (RETURN 7de8cf3e): BOTH exit paths — a NON-ZERO leader exit
        # AND a clean leader exit — prove the WHOLE process group is gone BEFORE
        # any terminal label is written. The worker's own finally already deleted
        # the durable marker file, so only the group kill can stop a stubborn
        # descendant's external side effects; a non-zero leader exit is NOT proof
        # the group is empty (the entry may have forked a descendant before
        # raising), and a clean exit is NOT proof either. A group that cannot be
        # confirmed empty keeps the job NON-terminal (identity + permit held) and
        # AUTO-RETRIES the confirmation on a saturating + fixed-window-bounded
        # backoff inside ``_confirm_worker_group_exit`` — the failure/result is
        # only surfaced AFTER the executor is provably gone.
        await self._confirm_worker_group_exit(runtime)
        if process.returncode != 0:
            # C-146 P0-1: surface the REAL operation's failure provenance. The
            # worker emits a structured marker (exception CLASS + HTTP status
            # only — never the raw message) when its entry raises; reconstruct a
            # typed exception so the job fails with the operation's OWN cause
            # (e.g. the ready chain's 503) instead of a generic exit-code error.
            # A missing/unparseable marker falls back to the generic failure.
            failure = self._parse_worker_failure(stderr)
            if failure is not None:
                raise failure
            raise RuntimeError(f"live planning job worker exited with {process.returncode}")
        text = stdout.decode("utf-8")
        result = json.loads(text) if text.strip() else {}
        self._validate_formal_worker_execution_receipts(runtime, command, result)
        # C-146 P0-1 (RETURN 7de8cf3e): the worker captured its progress /
        # checkpoint / model-trace observability in a cross-process collector;
        # replay it onto the durable job record NOW (still inside the operation,
        # before any terminal label) so the parent API's job GET observes the
        # full ready-chain progress, per-pair checkpoints and model trace. The
        # internal envelope is removed from the stored result.
        await self._replay_worker_observability(
            runtime,
            result,
            strict=command.result_importer is not None,
        )
        if command.result_importer is not None:
            result = await command.result_importer(result)
        return result

    @staticmethod
    def _validate_formal_worker_execution_receipts(
        runtime: _RuntimeJob,
        command: LiveJobWorkerCommand,
        result: dict[str, Any],
    ) -> None:
        """Fail closed unless a formal ready result proves real model execution."""

        bundle = command.args.get("runtime_bundle")
        if not isinstance(bundle, dict):
            return
        spec = bundle.get("spec")
        if (
            not isinstance(spec, dict)
            or spec.get("formal_parent_api_origin") is None
        ):
            return

        def digest(value: object) -> str:
            return hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()

        runtime_receipt = result.get("worker_runtime_receipt")
        expected_runtime_fields = {
            "schema_version",
            "runtime",
            "providers",
            "spec_sha256",
            "runtime_provenance",
            "api_runtime_identity_sha256",
            "worker_runtime_identity",
            "model_agents_required",
            "model_runtime_identity",
        }
        if (
            not isinstance(runtime_receipt, dict)
            or set(runtime_receipt) != expected_runtime_fields
            or runtime_receipt.get("schema_version")
            != "tripchord-live-worker-runtime-receipt-v1"
            or runtime_receipt.get("runtime") != "browser-bridge"
            or runtime_receipt.get("providers") != spec.get("providers")
            or runtime_receipt.get("spec_sha256") != bundle.get("spec_sha256")
            or runtime_receipt.get("runtime_provenance")
            != bundle.get("runtime_provenance")
            or runtime_receipt.get("model_agents_required") is not True
            or runtime_receipt.get("model_runtime_identity")
            != spec.get("model_runtime_identity")
        ):
            raise RuntimeError("formal worker runtime receipt is invalid")
        receipt = result.get("model_execution_receipt")
        expected_receipt_fields = {
            "schema_version",
            "job_id",
            "request_sha256",
            "runtime_bundle_spec_sha256",
            "worker_runtime_identity_sha256",
            "model_runtime_identity",
            "trace_count",
            "success_count",
            "failure_count",
            "traces",
            "receipt_sha256",
        }
        if not isinstance(receipt, dict) or set(receipt) != expected_receipt_fields:
            raise RuntimeError("formal worker model execution receipt is missing")
        traces = receipt.get("traces")
        expected_trace_fields = {
            "id",
            "provider",
            "model",
            "role",
            "request_digest",
            "scope_id",
            "scope_request_digest",
            "response_schema_requested",
            "tool_count",
            "started_at",
            "finished_at",
            "success",
            "usage",
            "estimated_cost_usd",
            "error_class",
        }
        request_sha256 = runtime.snapshot.request_sha256
        if request_sha256 is None:
            raise RuntimeError("formal worker job request SHA-256 is missing")
        model_identity = spec.get("model_runtime_identity")
        allowed_models = (
            {
                model_identity.get("primary_model"),
                model_identity.get("fast_model"),
            }
            if isinstance(model_identity, dict)
            else set()
        )
        if (
            receipt.get("schema_version")
            != "tripchord-model-execution-receipt-v1"
            or receipt.get("job_id") != runtime.snapshot.id
            or receipt.get("request_sha256") != request_sha256
            or receipt.get("runtime_bundle_spec_sha256")
            != bundle.get("spec_sha256")
            or receipt.get("worker_runtime_identity_sha256")
            != digest(runtime_receipt["worker_runtime_identity"])
            or receipt.get("model_runtime_identity") != model_identity
            or type(receipt.get("trace_count")) is not int
            or receipt.get("trace_count", 0) <= 0
            or receipt.get("success_count") != receipt.get("trace_count")
            or receipt.get("failure_count") != 0
            or not isinstance(traces, list)
            or len(traces) != receipt.get("trace_count")
            or receipt.get("receipt_sha256")
            != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
            or result.get("model_trace_count") != receipt.get("trace_count")
            or result.get("model_trace_success_count") != receipt.get("success_count")
            or result.get("model_trace_failure_count") != 0
        ):
            raise RuntimeError("formal worker model execution receipt is invalid")
        provider = model_identity.get("provider") if isinstance(model_identity, dict) else None
        for trace in traces:
            if (
                not isinstance(trace, dict)
                or set(trace) != expected_trace_fields
                or trace.get("provider") != provider
                or trace.get("model") not in allowed_models
                or trace.get("scope_id") != runtime.snapshot.id
                or trace.get("scope_request_digest") != request_sha256
                or trace.get("success") is not True
                or trace.get("error_class") is not None
                or not isinstance(trace.get("role"), str)
                or not trace.get("role")
                or not isinstance(trace.get("request_digest"), str)
                or len(trace["request_digest"]) != 64
                or type(trace.get("tool_count")) is not int
                or not isinstance(trace.get("usage"), dict)
            ):
                raise RuntimeError("formal worker model trace is invalid")
            try:
                started_at = datetime.fromisoformat(str(trace["started_at"]))
                finished_at = datetime.fromisoformat(str(trace["finished_at"]))
            except ValueError as exc:
                raise RuntimeError("formal worker model trace time is invalid") from exc
            if (
                started_at.tzinfo is None
                or finished_at.tzinfo is None
                or finished_at < started_at
            ):
                raise RuntimeError("formal worker model trace time is invalid")
        observability = result.get("_worker_observability")
        expected_observability_fields = {
            "progress_events",
            "pair_checkpoints",
            "model_trace_summary",
            "source_terminal_events",
            "barrier_released_at",
        }
        if (
            not isinstance(observability, dict)
            or set(observability) != expected_observability_fields
        ):
            raise RuntimeError("formal worker observability is missing or malformed")
        if observability.get("progress_events") != [
            ["interpreting_requirement", 10],
            ["searching_live_sources", 25],
            ["caching_pair_runs", 90],
            ["assembling_result", 95],
        ]:
            raise RuntimeError("formal worker progress history is not exact")

        from tripchord.api import LiveFlexibleFromTextPlanningResponse
        from tripchord.main import _source_terminal_events_from_run

        public_result = {
            key: value
            for key, value in result.items()
            if key
            not in {
                "_worker_observability",
                "_worker_cache_runs",
                "worker_runtime_receipt",
                "model_execution_receipt",
            }
        }
        response = LiveFlexibleFromTextPlanningResponse.model_validate(public_result)
        if response.run is None:
            raise RuntimeError("formal worker returned no executed flexible run")
        raw_barrier = observability.get("barrier_released_at")
        if not isinstance(raw_barrier, str):
            raise RuntimeError("formal worker barrier receipt is missing")
        try:
            barrier = datetime.fromisoformat(raw_barrier)
        except ValueError as exc:
            raise RuntimeError("formal worker barrier receipt time is invalid") from exc
        if barrier.tzinfo is None or barrier.utcoffset() is None:
            raise RuntimeError("formal worker barrier receipt time is invalid")
        expected_source_events = [
            item.model_dump(mode="json")
            for item in _source_terminal_events_from_run(response.run, barrier)
        ]
        if (
            not expected_source_events
            or observability.get("source_terminal_events")
            != expected_source_events
        ):
            raise RuntimeError(
                "formal worker source/barrier observability differs from its run"
            )
        raw_checkpoints = observability.get("pair_checkpoints")
        if not isinstance(raw_checkpoints, list):
            raise RuntimeError("formal worker pair checkpoints are missing")
        checkpoints = tuple(
            LivePlanningPairCheckpoint.model_validate(item)
            for item in raw_checkpoints
        )
        executions = response.run.pair_runs
        LivePlanningJobRegistry._validate_formal_pair_checkpoint_alignment(
            checkpoints,
            executions,
            request_sha256=request_sha256,
        )
        capability = runtime.worker_execution_capability
        if capability is not None and (
            capability.get("terminal_job_id") != receipt["job_id"]
            or capability.get("request_sha256") != receipt["request_sha256"]
        ):
            raise RuntimeError("formal worker model receipt is bound to a foreign capability")

    @staticmethod
    def _validate_formal_pair_checkpoint_alignment(
        checkpoints: tuple[LivePlanningPairCheckpoint, ...],
        executions: Any,
        *,
        request_sha256: str,
    ) -> None:
        """Bind parallel checkpoint completion records to planned pairs.

        The durable pair list is intentionally stable plan order, whereas
        checkpoint records arrive in worker completion order.  Sequence and
        uniqueness are checked on the checkpoint stream; immutable pair ids
        bind it to the execution stream without requiring the two orders to
        match.
        """
        execution_ids = tuple(execution.date_pair.id for execution in executions)
        checkpoint_ids = tuple(checkpoint.date_pair_id for checkpoint in checkpoints)
        executions_by_id = {execution.date_pair.id: execution for execution in executions}
        if (
            len(checkpoints) != len(executions)
            or tuple(checkpoint.sequence for checkpoint in checkpoints)
            != tuple(range(1, len(checkpoints) + 1))
            or len(set(checkpoint_ids)) != len(checkpoint_ids)
            or len(set(execution_ids)) != len(execution_ids)
            or set(checkpoint_ids) != set(execution_ids)
            or any(
                checkpoint.request_sha256 != request_sha256
                or checkpoint.departure_date
                != executions_by_id[checkpoint.date_pair_id].date_pair.departure_date
                or checkpoint.return_date
                != executions_by_id[checkpoint.date_pair_id].date_pair.return_date
                for checkpoint in checkpoints
            )
        ):
            raise RuntimeError("formal worker pair checkpoints differ from its executed pairs")

    async def _replay_worker_observability(
        self,
        runtime: _RuntimeJob,
        result: dict[str, Any],
        *,
        strict: bool = False,
    ) -> None:
        """Replay a worker's collected observability onto the durable job record.

        The worker subprocess cannot call the parent's in-process reporters, so
        ``run_live_flexible_from_text`` returns a ``_worker_observability``
        envelope inside its result. This replays each progress stage, each pair
        checkpoint and the model-trace summary through the SAME registry update
        methods the in-process reporters use, then removes the envelope so the
        stored job result stays clean. Replay happens inside the operation task
        under the current generation, so the updates are identical to what an
        in-process run would have persisted.
        """
        observability = result.pop("_worker_observability", None)
        if not isinstance(observability, dict):
            if strict:
                raise RuntimeError("live planning worker observability is missing")
            return
        expected_fields = {
            "progress_events",
            "pair_checkpoints",
            "model_trace_summary",
            "source_terminal_events",
            "barrier_released_at",
        }
        if strict and set(observability) != expected_fields:
            raise RuntimeError("live planning worker observability shape is invalid")
        generation = runtime.generation
        progress_events = observability.get("progress_events", [])
        if strict and (
            not isinstance(progress_events, list)
            or not progress_events
            or not all(
                isinstance(entry, list)
                and len(entry) == 2
                and isinstance(entry[0], str)
                and bool(entry[0])
                and type(entry[1]) is int
                for entry in progress_events
            )
        ):
            raise RuntimeError("live planning worker progress observability is invalid")
        for entry in progress_events:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            stage, progress = entry
            if isinstance(stage, str) and isinstance(progress, int):
                await self._update_running(
                    runtime,
                    stage,
                    progress,
                    generation=generation,
                )
        pair_checkpoints = observability.get("pair_checkpoints", [])
        if strict and (
            not isinstance(pair_checkpoints, list)
            or not all(isinstance(checkpoint, dict) for checkpoint in pair_checkpoints)
        ):
            raise RuntimeError("live planning worker checkpoint observability is invalid")
        for checkpoint in pair_checkpoints:
            if isinstance(checkpoint, dict):
                await self._update_pair_checkpoint(
                    runtime,
                    LivePlanningPairCheckpoint.model_validate(checkpoint),
                    generation=generation,
                )
        source_events = observability.get("source_terminal_events")
        if strict and (
            not isinstance(source_events, list)
            or not all(isinstance(event, dict) for event in source_events)
        ):
            raise RuntimeError("live planning worker source observability is invalid")
        if isinstance(source_events, list) and all(
            isinstance(event, dict) for event in source_events
        ):
            await self._update_source_terminal_events(
                runtime,
                tuple(source_events),
                generation=generation,
            )
        barrier_released_at = observability.get("barrier_released_at")
        if strict and barrier_released_at is not None and not isinstance(
            barrier_released_at,
            str,
        ):
            raise RuntimeError("live planning worker barrier observability is invalid")
        if isinstance(barrier_released_at, str):
            try:
                parsed_barrier = datetime.fromisoformat(barrier_released_at)
            except ValueError as exc:
                raise ValueError(
                    "worker barrier release timestamp is invalid"
                ) from exc
            await self._mark_barrier_released(
                runtime,
                parsed_barrier,
                generation=generation,
            )
        trace_summary = observability.get("model_trace_summary")
        expected_trace_fields = {
            "scope_id",
            "scope_request_sha256",
            "trace_count",
            "success_count",
            "failure_count",
        }
        if strict and (
            not isinstance(trace_summary, dict)
            or set(trace_summary) != expected_trace_fields
            or not isinstance(trace_summary.get("scope_id"), str)
            or not trace_summary.get("scope_id")
            or not isinstance(trace_summary.get("scope_request_sha256"), str)
            or any(
                type(trace_summary.get(field)) is not int
                for field in ("trace_count", "success_count", "failure_count")
            )
        ):
            raise RuntimeError("live planning worker model observability is invalid")
        ready_result = result.get("run") is not None
        if strict and ready_result and (
            not pair_checkpoints
            or not source_events
            or not isinstance(barrier_released_at, str)
        ):
            raise RuntimeError("live planning worker ready observability is incomplete")
        if strict and isinstance(trace_summary, dict) and (
            result.get("model_trace_scope_sha256")
            != trace_summary["scope_request_sha256"]
            or result.get("model_trace_count") != trace_summary["trace_count"]
            or result.get("model_trace_success_count")
            != trace_summary["success_count"]
            or result.get("model_trace_failure_count")
            != trace_summary["failure_count"]
        ):
            raise RuntimeError("live planning worker model result binding is invalid")
        if isinstance(trace_summary, dict):
            await self._update_model_trace_summary(
                runtime,
                scope_id=str(trace_summary.get("scope_id", "")),
                scope_request_sha256=str(
                    trace_summary.get("scope_request_sha256", "")
                ),
                trace_count=int(trace_summary.get("trace_count", 0)),
                success_count=int(trace_summary.get("success_count", 0)),
                failure_count=int(trace_summary.get("failure_count", 0)),
                generation=generation,
            )

    def _maybe_release_slot(self, runtime: _RuntimeJob) -> None:
        """Release the admission permit only once the REAL operation task is done.

        P0-1: capacity must bind to the live operation lifecycle, never to the
        runner's exit. A stubborn operation that swallowed CancelledError keeps
        holding its permit, so a new-key request can never start over it; the
        permit is released here only after ``operation_task`` is confirmed done
        (or never existed). Safe to call from the runner's ``finally`` AND from
        the operation done-callback: the ``slot_held`` flag makes a
        double-release impossible.

        C-145 P1: releasing the permit is NOT the end of the cleanup. A runtime
        whose cancel/close/deadline cleanup failed closed keeps a pending
        terminal outcome; the operation done-callback joins that state machine
        here, so the record auto-collects to its terminal state without an extra
        retry/close/cold start.

        C-145 P0 supplement: the operation done-callback must NEVER spawn a
        competing cleanup owner while a cancel()/close() caller is actively
        settling this record (its cancel future is still live). The settling
        caller's own join owns the terminalize — and must be the one to surface
        an indeterminate post-commit write. Once the caller resolves the future
        (or the operation stops on its own after cancel() returns), this
        re-enters and spawns the owner as usual."""
        if not runtime.slot_held:
            # Still join the cleanup state machine — a pending terminal outcome
            # may need a (re)spawned owner even when an earlier call already
            # released the permit.
            if not self._cancel_settler_active(runtime):
                self._ensure_cleanup_owner(runtime)
            return
        if runtime.operation_task is not None and not runtime.operation_task.done():
            return
        runtime.slot_held = False
        self._slots.release()
        if not self._cancel_settler_active(runtime):
            self._ensure_cleanup_owner(runtime)

    def _intent_persist_budget_exhausted_locked(self, runtime: _RuntimeJob) -> bool:
        """C-146 P0 supplement (P0-4): has the bounded STATE budget for the first
        durable intent been exhausted?

        The budget is a pair of caps: total attempts AND total wall-clock since
        the first attempt. Either cap reached means the burst retry must stop and
        the record must be quarantined non-terminal — the store is (or appears)
        permanently broken, and hammering it further cannot make the in-memory
        intent durable. Callers hold ``self._lock`` (or ``self._changed``)."""
        if runtime.intent_persist_attempts >= self._intent_persist_budget_attempts:
            return True
        started = runtime.intent_persist_started_monotonic
        return (
            started > 0.0
            and asyncio.get_running_loop().time() - started
            >= self._intent_persist_wallclock_budget_seconds
        )

    def _quarantine_capacity_available_locked(self) -> bool:
        """C-146 hard-stop gate (12e35d45 门 4): is a quarantine slot free?

        The quarantine conversion atomically reserves the slot in the SAME lock
        domain as the conversion (and its persist), so the quota can never be
        exceeded and a state the loader would reject can never be written.
        Callers hold ``self._lock``/``self._changed``.

        C-146 P0-5: while persistent quarantine overflow is set (the loader
        found more durable quarantined records than the current qcap) the
        registry is fail-closed — NO new conversion or admission is allowed
        until bounded retention cleanup reclaims quarantine space and clears
        the overflow flag."""
        if self._quarantine_overflow:
            return False
        return (
            sum(
                1
                for item in self._records.values()
                if item.quarantined or item.hard_stop_quarantine_reserved
            )
            < self._quarantine_capacity
        )

    def _quarantine_runtime_locked(
        self,
        runtime: _RuntimeJob,
        stage: str,
        reason: str,
    ) -> bool:
        """C-146 P0 supplement (P0-4) / b119: quarantine a non-terminal record.

        C-146 hard-stop gate (12e35d45 门 4): the independent quarantine slot is
        reserved ATOMICALLY in the same lock domain as the conversion — when the
        quarantine quota is full this returns False WITHOUT mutating anything, so
        the caller keeps the record non-quarantined, never writes an over-quota
        state, never guesses a terminal label and never drops a tombstone.

        On success the record keeps its snapshot (non-terminal) with an explicit
        quarantine stage and error, is excluded from executable active capacity,
        and its idempotency binding (if any) is marked ``legacy_isolated`` so a
        same-key request always fails closed and is never reused or started. The
        bounded cleanup reconcile re-arms via the single reaper, so store
        recovery auto-durably reconciles the quarantine + target facts before any
        quota is released. Never writes or claims a FAILED/CANCELLED label from
        memory-only facts."""
        if not self._quarantine_capacity_available_locked():
            return False
        runtime.quarantined = True
        runtime.quarantine_stage = stage
        # The generation bump isolates every registry-facing write (progress
        # reporter, checkpoint, trace summary) from the still-alive operation,
        # exactly like the hard-stop path — a quarantined record's marker must
        # stay stable and the operation must never mutate it.
        runtime.generation += 1
        runtime.snapshot = runtime.snapshot.model_copy(
            update={
                "stage": stage,
                "error": reason,
                "updated_at": self._utc_now(),
                "revision": runtime.snapshot.revision + 1,
            }
        )
        for entry in self._idempotency.values():
            if entry.job_id == runtime.snapshot.id:
                entry.legacy_isolated = True
                if entry.updated_at is None:
                    entry.updated_at = self._utc_now()
        loop = asyncio.get_running_loop()
        runtime.cleanup_retry_round = self._bump_cleanup_retry_round(runtime.cleanup_retry_round)
        runtime.cleanup_next_retry_monotonic = loop.time() + self._cleanup_retry_delay(
            runtime.cleanup_retry_round
        )
        self._ensure_reaper()
        return True

    def _cancel_settler_active(self, runtime: _RuntimeJob) -> bool:
        """True while a cancel()/close() caller is actively settling a record.

        C-145 P0 supplement: the settling caller's own join owns the terminalize
        (and must surface any post-commit write failure). The operation
        done-callback must not spawn a racing owner mid-settle — once the caller
        resolves the future, the done-callback re-enters and spawns as usual."""
        cancel_future = runtime.cancel_future
        return cancel_future is not None and not cancel_future.done()

    def _authenticated_orphan_alive(self, runtime: _RuntimeJob) -> bool:
        """True while a cold worker identity lacks durable death confirmation.

        C-146 P0-3 (RETURN 7de8cf3e): only meaningful for a cold-booted runtime
        (no in-process ``worker_handle``): a live handle's group is owned by the
        in-process kill/confirm machinery. For a cold identity, neither ESRCH nor
        an empty/foreign ``ps`` result is enough here: discovery must first bind
        authentication to the marker and persist
        ``orphan_death_confirmed=True``. Every other shape is unknown/alive for
        terminalization purposes, so an auth failure or process-query failure can
        never be converted into a terminal label by a concurrently restored
        cleanup owner."""
        if runtime.worker_handle is not None:
            return False
        if runtime.worker_pgid is None or not runtime.worker_marker:
            return False
        return runtime.orphan_death_confirmed is not True

    def _executors_stopped(self, runtime: _RuntimeJob) -> bool:
        """Shared terminalize predicate (硬门 B / P0-4).

        True only when BOTH the registry runner task AND the real operation task
        are confirmed done (or never existed). A final CANCELLED / FAILED /
        close-completed label may be published only when this holds — never while
        an executor could still be writing side effects.

        C-146 P0-3 (RETURN 7de8cf3e): a cold-booted runtime has no in-process
        tasks, but its DURABLE worker identity may still point at a LIVE orphan
        process group that this process has NOT yet authenticated + killed +
        reaped. That group is a real executor — never report "stopped" while it
        is alive."""
        if runtime.task is not None and not runtime.task.done():
            return False
        if runtime.operation_task is not None and not runtime.operation_task.done():
            return False
        return not self._authenticated_orphan_alive(runtime)

    def _defer_cleanup_owner_spawn(self, runtime: _RuntimeJob) -> None:
        """Restore the unique cleanup owner for a cold-booted runtime, or defer.

        C-146 hard-stop gate (12e35d45 门 3): ``__init__`` may run with no event
        loop (construction at import time in the API process), so a task cannot
        be created there. When a loop IS running, spawn immediately; otherwise
        defer to ``_spawn_deferred_cleanup_owners`` at the first async entry
        point / ``close()`` — the record is never left permanently dangling."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._deferred_cleanup_owners.append(runtime)
        else:
            self._ensure_cleanup_owner(runtime)

    def _spawn_deferred_cleanup_owners(self) -> None:
        """Spawn every cleanup owner deferred from a loop-less cold start."""
        if not self._deferred_cleanup_owners:
            return
        deferred, self._deferred_cleanup_owners = self._deferred_cleanup_owners, []
        for runtime in deferred:
            self._ensure_cleanup_owner(runtime)

    def _ensure_cleanup_owner(self, runtime: _RuntimeJob) -> None:
        """Ensure a unique, waitable cleanup owner exists for a pending runtime.

        C-145 P1: a runtime whose cancel/close/deadline cleanup failed closed
        keeps a pending terminal outcome. The owner task waits for the REAL
        operation to stop (via a shield, so it outlives the registry runner and
        survives the operation's own cancellation) and then terminalizes the
        record — without an extra retry/close/cold start. Safe to call from the
        operation done-callback and from any cleanup join; re-entrant, so
        repeated calls never spawn a duplicate owner."""
        if runtime.pending_terminal is None and (
            not runtime.quarantined or runtime.quarantine_reconciled
        ):
            return
        if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            return
        owner = runtime.cleanup_owner
        if owner is not None and not owner.done():
            return
        runtime.cleanup_owner = asyncio.create_task(
            self._cleanup_owner(runtime),
            name=f"tripchord:{runtime.snapshot.id}:cleanup",
        )

    async def _cleanup_owner(self, runtime: _RuntimeJob) -> None:
        """The unique owner of a pending terminal outcome (C-145 P1/P0).

        Awaits the shielded operation task — so the owner outlives the registry
        runner and survives the operation being cancelled while it waits — then
        re-checks the shared ``_executors_stopped`` predicate and terminalizes
        the record to the durable pending outcome. The terminal persist is
        retried within a bounded per-round budget with a short backoff. A
        post-commit failure is already durable (confirm and clear the outcome);
        a pre-commit failure keeps ``pending_terminal`` intact for the next
        attempt. When the whole budget is exhausted, the DURABLE retry intent is
        kept and the registry reaper re-spawns this owner after a bounded
        backoff — until the terminal commit succeeds or the process shuts down —
        with no dependence on an external same-key/cancel/close/query."""
        operation_task = runtime.operation_task
        if runtime.intent_persist_pending or runtime.quarantined:
            # C-146 P0 supplement (P0-3 / P0-4), Phase-0: the FIRST durable
            # intent is the HARD PRECONDITION for stopping the executor. For a
            # plain intent-pending record the in-memory isolation AND the
            # FAILED/deadline_exceeded pending outcome are re-committed here
            # (bounded retry) BEFORE any cancel/drain — the real operation and
            # the admission slot stay untouched until the intent is durable. A
            # QUARANTINED record runs this as a RECONCILE: ONE bounded persist
            # attempt per interval; on store recovery the quarantine + in-memory
            # target facts become durable, and only then may quotas be released.
            async with self._lock:
                if runtime.quarantined:
                    try:
                        self._persist_locked()
                    except Exception:
                        intent_committed = False
                    else:
                        runtime.intent_persist_pending = False
                        # C-146 P0 supplement (P0-4): the durable quarantine fact
                        # is now committed. For a quarantined record WITHOUT a
                        # pending terminal outcome (hard-stopped / ambiguous
                        # isolated) this IS the whole reconcile — there is no
                        # terminal settlement, so the record stays quarantined
                        # non-terminal until its bounded retention reclaims it.
                        runtime.quarantine_reconciled = True
                        intent_committed = True
                else:
                    try:
                        await self._persist_locked_with_bounded_retry()
                    except Exception:
                        intent_committed = False
                    else:
                        runtime.intent_persist_pending = False
                        intent_committed = True
            if not intent_committed:
                loop = asyncio.get_running_loop()
                if runtime.intent_persist_started_monotonic == 0.0:
                    runtime.intent_persist_started_monotonic = loop.time()
                if not runtime.quarantined:
                    runtime.intent_persist_attempts += self._cancel_isolation_persist_attempts
                if not runtime.quarantined and self._intent_persist_budget_exhausted_locked(
                    runtime
                ):
                    # C-146 P0 supplement (P0-4): bounded STATE budget reached —
                    # the burst retry STOPS and the record is quarantined
                    # non-terminal. The in-memory intent stays recoverable, but
                    # no FAILED/CANCELLED is ever written or claimed from
                    # memory, and the burst never hammers the store again.
                    # C-146 hard-stop gate (12e35d45 门 4): the quarantine slot
                    # is reserved atomically; when the independent qcap is full
                    # the record stays non-quarantined and the bounded retry
                    # re-arms — never an over-quota write, never a guessed
                    # terminal label.
                    if not self._quarantine_runtime_locked(
                        runtime,
                        _QUARANTINE_INTENT_UNCOMMITTED_STAGE,
                        (
                            "first durable intent could not be committed within "
                            "the bounded state budget; the job is quarantined "
                            "non-terminal"
                        ),
                    ):
                        runtime.cleanup_retry_round = self._bump_cleanup_retry_round(
                            runtime.cleanup_retry_round
                        )
                        runtime.cleanup_next_retry_monotonic = (
                            loop.time()
                            + self._cleanup_retry_delay(runtime.cleanup_retry_round)
                        )
                        self._ensure_reaper()
                    return
                # Every bounded attempt failed again: keep the recoverable
                # in-memory intent and re-arm the single reaper — the same
                # saturating owner backoff auto-continues the persistence (or
                # the bounded reconcile for a quarantined record).
                runtime.cleanup_retry_round = self._bump_cleanup_retry_round(
                    runtime.cleanup_retry_round
                )
                runtime.cleanup_next_retry_monotonic = loop.time() + self._cleanup_retry_delay(
                    runtime.cleanup_retry_round
                )
                self._ensure_reaper()
                return
            # C-146 P0 supplement (P0-4): a quarantined record WITHOUT a pending
            # terminal outcome has no settlement — the reconcile (durable
            # quarantine fact) is complete, so it stays quarantined non-terminal
            # until retention reclaims it. No drain, no terminal label, no
            # unquarantine.
            if runtime.quarantined and runtime.pending_terminal is None:
                return
            # The intent is durable now: only now may the real executor be
            # stopped and drained. A stubborn operation that swallows
            # CancelledError past the budget fails closed into the observable
            # stuck isolation with the committed FAILED intent intact (first
            # intent wins — never overwritten by a later join).
            drain_confirmed = await self._cancel_and_drain_operation(runtime)
            if not drain_confirmed or not (operation_task is None or operation_task.done()):
                if runtime.quarantined:
                    # The reconciled quarantine facts are durable but the
                    # executor is still alive: keep the record quarantined and
                    # re-arm the bounded reconcile. Never unquarantine over
                    # live work, never publish a terminal label.
                    loop = asyncio.get_running_loop()
                    runtime.cleanup_retry_round = self._bump_cleanup_retry_round(
                        runtime.cleanup_retry_round
                    )
                    runtime.cleanup_next_retry_monotonic = loop.time() + self._cleanup_retry_delay(
                        runtime.cleanup_retry_round
                    )
                    self._ensure_reaper()
                    return
                pending = runtime.pending_terminal
                if pending is not None:
                    with suppress(Exception):
                        await self._mark_cancel_stuck(
                            runtime,
                            stage="timeout_pending",
                            error=(
                                "live planning operation did not stop within the "
                                "deadline cleanup budget; the job stays "
                                "non-terminal and the operation is isolated"
                            ),
                            pending_state=pending.state,
                            pending_stage=pending.stage,
                            pending_error=pending.error,
                            pending_safe_failure=pending.safe_failure,
                            pending_cancellation_requested=pending.cancellation_requested,
                        )
                return
            # The operation stopped and the quarantine + target facts are
            # durable: the shared terminalize loop below settles to the durable
            # intent via an ATOMIC settle (unquarantine + terminal commit in the
            # same write). No manual unquarantine here — a pre-commit failure
            # must restore the quarantine flag together with the snapshot.
            # The operation stopped: also await the registry runner so the
            # shared _executors_stopped predicate holds before terminalizing —
            # the runner was mid-exit when it spawned this owner. Awaiting a
            # completed/cancelled task is a safe no-op, so a concurrent
            # close()/cancel() can never deadlock this.
            runner_task = runtime.task
            if (
                runner_task is not None
                and not runner_task.done()
                and runner_task is not asyncio.current_task()
            ):
                with suppress(BaseException):
                    await asyncio.shield(runner_task)
            # Fall through to the shared terminalize loop below, which retries
            # the terminal commit within its own bounded per-round budget.
        if operation_task is not None:
            with suppress(BaseException):
                # The operation was cancelled or raised while the owner waited;
                # either way it is now done, which is all the owner needs to
                # know to proceed.
                await asyncio.shield(operation_task)
        if not self._executors_stopped(runtime):
            return
        pending = runtime.pending_terminal
        if pending is None:
            return
        budget = self._cancel_isolation_persist_attempts
        loop = asyncio.get_running_loop()
        for attempt in range(1, budget + 1):
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                # A concurrent terminalize won; confirm and consume the outcome.
                runtime.pending_terminal = None
                return
            try:
                await self._finish(
                    runtime,
                    pending.state,
                    stage=pending.stage,
                    result=pending.result,
                    error=pending.error,
                    safe_failure=pending.safe_failure,
                    cancellation_requested=pending.cancellation_requested,
                    settle_quarantined=True,
                )
                return
            except LivePlanningJobRegistryPostCommitError:
                # The terminal state was already committed on disk; the
                # in-memory record matches it. Confirm and consume the outcome —
                # never rewrite a conflicting terminal state.
                runtime.pending_terminal = None
                return
            except Exception:
                # A pre-commit persist failure keeps the recoverable cancel_pending
                # isolation and the DURABLE pending outcome for the next attempt.
                if attempt < budget:
                    await asyncio.sleep(min(self._cleanup_retry_backoff_seconds * attempt, 0.1))
        # Budget exhausted: keep the durable retry intent and arm the single
        # registry reaper to re-spawn this owner after a bounded, growing backoff
        # (never a busy-wait, never a second concurrent owner).
        runtime.cleanup_retry_round = self._bump_cleanup_retry_round(runtime.cleanup_retry_round)
        runtime.cleanup_next_retry_monotonic = loop.time() + self._cleanup_retry_delay(
            runtime.cleanup_retry_round
        )
        self._ensure_reaper()

    @staticmethod
    def _bump_cleanup_retry_round(raw_round: object) -> int:
        """Safely increment the cleanup owner's retry round.

        C-145 P0 supplement: a persisted round of ≈1025 (or a malicious
        negative / non-integer / out-of-range value) must never crash the sole
        cleanup owner. The value is validated and normalized BEFORE the backoff
        exponentiation below; the normalization is observable through the
        saturating delay and the surviving owner."""
        if not isinstance(raw_round, int):
            return 1
        if raw_round < 0:
            return 1
        return raw_round + 1

    def _cleanup_retry_delay(self, round_number: object) -> float:
        """Bounded, saturating backoff for an exhausted cleanup owner's retry
        round (C-145 P0 supplement).

        The exponent is capped BEFORE exponentiation so a huge round can never
        OverflowError the owner: the delay saturates to the 0.5s ceiling —
        never a busy-wait, never an unbounded sleep. Malicious values (negative,
        non-integer, out-of-range) are normalized to round 1, not crashed on."""
        if not isinstance(round_number, int):
            round_number = 0
        if round_number < 0:
            round_number = 0
        exponent: int = max(0, min(round_number - 1, 30))
        backoff: float = self._cleanup_retry_backoff_seconds * (2**exponent)
        return min(backoff, 0.5)

    def _hard_stop_next_attempt_locked(self, runtime: _RuntimeJob, now: float) -> float:
        """Next attempt time for a failed hard-stop death-confirmation.

        C-146 P0-4 (RETURN 7de8cf3e): a persistently failing cleanup confirm
        (kill/confirm exception OR death-not-confirmed) must NEVER hot-loop the
        watchdog. The retry uses the SAME saturating exponential backoff as the
        cleanup owner (capped), PLUS a hard per-window call-count upper bound:
        once the budget within the fixed window is consumed, no further attempt
        is scheduled until the window resets. Callers hold ``self._changed``."""
        window = self._hard_stop_confirm_budget_window_seconds
        window_calls = self._hard_stop_confirm_budget_window_calls
        start = runtime.hard_stop_confirm_window_start_monotonic
        if start == 0.0 or now - start >= window:
            start = now
            runtime.hard_stop_confirm_window_start_monotonic = start
            runtime.hard_stop_confirm_window_calls = 0
        runtime.hard_stop_confirm_window_calls += 1
        if runtime.hard_stop_confirm_window_calls >= window_calls:
            # Fixed time-window call-count upper bound reached: no further
            # attempts until the window resets — a hard, non-spinning cap. The
            # round resets so a later window starts from the base backoff.
            runtime.hard_stop_confirm_retry_round = 0
            return start + window
        runtime.hard_stop_confirm_retry_round = self._bump_cleanup_retry_round(
            runtime.hard_stop_confirm_retry_round
        )
        return now + self._cleanup_retry_delay(runtime.hard_stop_confirm_retry_round)

    def _worker_exit_next_attempt_locked(self, runtime: _RuntimeJob, now: float) -> float:
        """Next attempt time for a failed worker-exit group confirmation.

        C-146 P0-2: mirrors ``_hard_stop_next_attempt_locked`` for the
        worker-exit path — a confirmation that returns False, times out, or
        raises must NEVER hot-loop the operation task. The retry uses the SAME
        saturating exponential backoff (capped) PLUS the same hard per-window
        call-count upper bound: once the budget within the fixed window is
        consumed, no further attempt is scheduled until the window resets."""
        window = self._hard_stop_confirm_budget_window_seconds
        window_calls = self._hard_stop_confirm_budget_window_calls
        start = runtime.worker_exit_confirm_window_start_monotonic
        if start == 0.0 or now - start >= window:
            start = now
            runtime.worker_exit_confirm_window_start_monotonic = start
            runtime.worker_exit_confirm_window_calls = 0
        runtime.worker_exit_confirm_window_calls += 1
        if runtime.worker_exit_confirm_window_calls >= window_calls:
            runtime.worker_exit_confirm_retry_round = 0
            return start + window
        runtime.worker_exit_confirm_retry_round = self._bump_cleanup_retry_round(
            runtime.worker_exit_confirm_retry_round
        )
        return now + self._cleanup_retry_delay(runtime.worker_exit_confirm_retry_round)

    def _ensure_reaper(self) -> None:
        """Ensure the single, bounded cleanup reaper is running (C-145 P0).

        Re-entrant: repeated calls never spawn a second reaper. The reaper
        self-terminates when no pending outcome needs a retry, so it is spawned
        lazily here each time a cleanup owner exhausts its per-round budget."""
        reaper = self._reaper_task
        if reaper is not None and not reaper.done():
            return
        self._reaper_task = asyncio.create_task(
            self._cleanup_reaper(),
            name="tripchord:registry:cleanup-reaper",
        )

    async def _cleanup_reaper(self) -> None:
        """Registry-held single reaper that auto-restarts exhausted cleanup
        owners (C-145 P0).

        Sleeps exactly until the next due retry (bounded, no busy-wait), then
        re-spawns every due owner via ``_ensure_cleanup_owner``. It runs while
        any pending outcome needs a retry and self-terminates once none remain —
        a later budget exhaustion re-arms it. Survives close(): the registry's
        closed flag only stops new work, never an in-flight bounded drain."""
        loop = asyncio.get_running_loop()
        while True:
            async with self._lock:
                if self._closed and not self._any_retry_pending_locked(loop.time()):
                    # Explicit shutdown with nothing left to drain: stop.
                    self._reaper_task = None
                    return
                next_at = self._next_cleanup_retry_at_locked(loop.time())
                if next_at is None:
                    self._reaper_task = None
                    return
            delay = max(0.0, next_at - loop.time())
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lock:
                if self._closed and not self._any_retry_pending_locked(loop.time()):
                    self._reaper_task = None
                    return
                now = loop.time()
                for runtime in list(self._records.values()):
                    if not self._cleanup_owner_needed_locked(runtime):
                        continue
                    if runtime.cleanup_owner is not None and not runtime.cleanup_owner.done():
                        continue
                    if now >= runtime.cleanup_next_retry_monotonic:
                        self._ensure_cleanup_owner(runtime)

    def _cleanup_owner_needed_locked(self, runtime: _RuntimeJob) -> bool:
        """True when a runtime still needs its (re)spawned cleanup owner.

        C-145 P0: any pending terminal outcome. C-146 P0 supplement (P0-4): a
        quarantined record whose durable quarantine fact has NOT been committed
        yet also needs the owner — the bounded reconcile keeps persisting until
        the store recovers. A quarantined record WITHOUT a pending outcome whose
        fact IS durable needs no owner (it stays quarantined until retention
        reclaims it)."""
        if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            return False
        if runtime.pending_terminal is not None:
            return True
        return runtime.quarantined and not runtime.quarantine_reconciled

    def _any_retry_pending_locked(self, now: float) -> bool:
        """True when at least one runtime holds a pending outcome that still
        needs a reaper (its owner is done and its retry is not yet due or due
        now). A live owner needs no reaper — it is already retrying."""
        for runtime in self._records.values():
            if not self._cleanup_owner_needed_locked(runtime):
                continue
            if runtime.cleanup_owner is not None and not runtime.cleanup_owner.done():
                continue
            return True
        return False

    def _next_cleanup_retry_at_locked(self, now: float) -> float | None:
        """The next monotonic time at which an exhausted cleanup owner is due for
        a reaper re-spawn, or None when no runtime needs a reaper right now."""
        next_at: float | None = None
        for runtime in self._records.values():
            if not self._cleanup_owner_needed_locked(runtime):
                continue
            if runtime.cleanup_owner is not None and not runtime.cleanup_owner.done():
                continue
            if runtime.cleanup_next_retry_monotonic <= now:
                return now
            if next_at is None or runtime.cleanup_next_retry_monotonic < next_at:
                next_at = runtime.cleanup_next_retry_monotonic
        return next_at

    def _ensure_hard_stop_watchdog(self) -> None:
        """Ensure the single, bounded hard-stop watchdog is running (P0-4).

        Re-entrant: repeated calls never spawn a second watchdog. It wakes
        exactly when a live operation's absolute execution bound
        (``hard_stop_monotonic``) is reached and self-terminates once no
        operation needs it, so a permanent-failure attack can never accumulate
        watchdog timers.

        C-146 P0-3: called each time a NEW live executor is armed — the exact
        moment a fresh (possibly EARLIER) hard-stop bound becomes real. When the
        watchdog is already asleep on an older deadline, wake it so the re-scan
        sees the new live operation immediately instead of sleeping past its
        bound. (The watchdog ignores records whose operation task is not yet
        live, so waking only on admission is not enough — the executor must
        actually be running for the new deadline to be visible.)"""
        watchdog = self._hard_stop_watchdog
        if watchdog is not None and not watchdog.done():
            self._wake_hard_stop_watchdog()
            return
        self._hard_stop_watchdog = asyncio.create_task(
            self._hard_stop_watchdog_loop(),
            name="tripchord:registry:hard-stop-watchdog",
        )

    async def _hard_stop_watchdog_loop(self) -> None:
        """Single, bounded watchdog that enforces the absolute EXECUTION budget.

        It sleeps until the earliest due hard-stop, then quarantines every live
        operation whose ``hard_stop_monotonic`` has passed. ``asyncio.cancel()``
        alone never masquerades as stopped: the operation is hard-stopped by the
        generation isolation plus the bounded slot release, and only a confirmed
        ``operation_task.done()`` is treated as stopped. The loop self-terminates
        when no live operation needs it — no background task outlives its work.

        C-146 P0-3: the loop NEVER sleeps past a deadline that became due while
        it waited. The wait is bounded by the earliest known deadline AND an
        explicit wake event — a hard-stop wrapper completing, a newly-armed
        EARLIER deadline, or quarantine retention freeing a slot all set the
        event — so the re-scan runs immediately instead of only at the oldest
        pre-sleep deadline. Each due operation is stopped by its OWN concurrent
        wrapper task: one stubborn kill/confirm never delays a sibling's hard
        stop past ITS deadline+grace (a ``gather``-blocked re-scan would)."""
        loop = asyncio.get_running_loop()
        if self._hard_stop_wake is None:
            self._hard_stop_wake = asyncio.Event()
        while True:
            due: list[_RuntimeJob] = []
            async with self._lock:
                for runtime in self._records.values():
                    if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                        continue
                    if runtime.hard_stopped:
                        continue
                    if runtime.hard_stop_in_flight:
                        # Already being stopped by a wrapper; the wrapper's
                        # completion re-awakens this loop for the re-scan.
                        continue
                    if runtime.operation_task is None or runtime.operation_task.done():
                        continue
                    if runtime.hard_stop_monotonic <= 0.0:
                        continue
                    if loop.time() < runtime.hard_stop_monotonic:
                        continue
                    # C-146 P0-7: a hard stop refused for a full quarantine quota
                    # is retried on a bounded backoff (and immediately when
                    # retention frees a slot), never busy-spun every scan.
                    if runtime.hard_stop_next_attempt_monotonic > loop.time():
                        continue
                    due.append(runtime)
                    runtime.hard_stop_in_flight = True
            if due:
                # C-146 hard-stop gate (12e35d45 门 2) + P0-3: CONCURRENT
                # per-operation wrappers. Each due operation stops within its own
                # fixed error budget while the loop re-scans immediately when any
                # wrapper completes (or an earlier deadline is armed) — a slow
                # sibling confirm never delays a newly-due job.
                for item in due:
                    wrapper = asyncio.create_task(
                        self._hard_stop_operation(item),
                        name=f"tripchord:{item.snapshot.id}:hard-stop",
                    )
                    self._hard_stop_tasks.add(wrapper)
                    wrapper.add_done_callback(self._on_hard_stop_wrapper_done)
            # Lossless wait window. The event is cleared, then ``next_due`` is
            # re-derived UNDER THE LOCK from the current records: any arming
            # (a newly-armed earlier deadline, a wrapper completion, a retention
            # reclaim) that landed between the due-scan and the clear is captured
            # by this re-derivation, and any arming after it sets the event and
            # wakes the wait — a deadline is never slept past. The loop also
            # never self-terminates while a stop wrapper is still in flight
            # (its completion is the only wake it needs).
            self._hard_stop_wake.clear()
            next_due: float | None = None
            in_flight = False
            async with self._lock:
                for runtime in self._records.values():
                    if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                        continue
                    if runtime.hard_stopped:
                        continue
                    if runtime.operation_task is None or runtime.operation_task.done():
                        continue
                    if runtime.hard_stop_monotonic <= 0.0:
                        continue
                    if runtime.hard_stop_in_flight:
                        in_flight = True
                        continue
                    candidate = runtime.hard_stop_monotonic
                    if loop.time() >= candidate:
                        # Due but not yet in flight (deadline passed between the
                        # scan and this re-derivation, or a deferred backoff
                        # elapsed): re-scan immediately.
                        candidate = runtime.hard_stop_next_attempt_monotonic
                        if candidate <= loop.time():
                            candidate = loop.time()
                    if next_due is None or candidate < next_due:
                        next_due = candidate
            if next_due is None and not in_flight:
                self._hard_stop_watchdog = None
                return
            if next_due is None:
                # Only in-flight stop wrappers remain: wait for one to complete
                # (its completion re-awakens the loop) rather than busy-spinning.
                await self._hard_stop_wake.wait()
            else:
                wait_seconds = max(0.0, next_due - loop.time())
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._hard_stop_wake.wait(), timeout=wait_seconds
                    )

    def _on_hard_stop_wrapper_done(self, wrapper: asyncio.Task[None]) -> None:
        """A hard-stop wrapper finished (success, deferral, cancellation or
        exception).

        C-146 P0-4 (RETURN): the wrapper's outcome is CONSUMED here so no task
        exception is ever left unretrieved (an unhandled ``asyncio`` task
        exception is a runtime symptom, not a control signal — the wrapper's own
        ``except``/``finally`` already armed the bounded retry). The watchdog is
        then woken so a deferred/retryable record is re-scanned on its bounded
        backoff."""
        self._hard_stop_tasks.discard(wrapper)
        if not wrapper.cancelled():
            exc = wrapper.exception()
            if exc is not None:
                logger.warning(
                    "hard-stop wrapper raised for a live job: %s",
                    type(exc).__name__,
                )
        self._wake_hard_stop_watchdog()

    async def _hard_stop_operation(self, runtime: _RuntimeJob) -> None:
        """Hard-stop a live operation past the absolute EXECUTION budget.

        C-146 hard-stop gate (12e35d45 门 1): the record is NOT called hard
        stopped until the real executor's death is PROVEN. For a worker
        subprocess that is SIGKILL + waitpid (``kill_and_confirm``) — the PID is
        dead and any external probe it was appending is permanently frozen; the
        admission permit then releases through the normal operation done-callback
        (communicate drains on process death) and new admission opens. An
        in-process coroutine that swallows ``CancelledError`` past the bounded
        confirm budget can NOT be proven dead: it lands on the explicit
        ``quarantine_orphan_in_process`` stage with ``hard_stopped`` False and
        its admission slot held until the real task is confirmed done — never
        called a hard stop, never force-released.

        C-146 hard-stop gate (12e35d45 门 4) + P0-7: the quarantine-capacity
        check is the ATOMIC fail-closed PRECONDITION of the whole stop: the slot
        is reserved (or the stop REFUSED) in the same lock domain as the
        conversion+persist, BEFORE any stop/kill side effect. When the quarantine
        quota is full the registry REFUSES the stop before any irreversible
        action — the record stays non-quarantined, marked deferred, and the
        watchdog re-attempts on a bounded backoff / when retention frees a slot.
        The reservation blocks a concurrent sibling hard-stop from consuming the
        last slot between the check and the conversion.

        C-146 P0-4 (RETURN 7de8cf3e): the in-flight marker and the quarantine
        slot reservation are released on EVERY exit (kill/confirm exception,
        cancel race, deferred qcap, conversion) via the ``finally`` below, and a
        death-NOT-confirmed outcome is RE-ARMED for a bounded-backoff retry. An
        already-quarantined record is re-attempted WITHOUT consuming a new slot —
        a qcap-full refusal can never permanently strand a record whose death was
        never confirmed."""
        operation_task = runtime.operation_task
        if operation_task is None or operation_task.done():
            return
        try:
            async with self._changed:
                if (
                    runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES
                    or runtime.hard_stopped
                ):
                    return
                if not runtime.quarantined:
                    if not self._quarantine_capacity_available_locked():
                        # 门 4 + P0-7: qcap full — REFUSE the stop BEFORE any
                        # stop/kill. No irreversible side effect may precede a
                        # capacity rejection. The record stays running (deadline
                        # already passed, so it is fail-closed on the quota), is
                        # marked deferred, and the watchdog re-attempts once
                        # retention frees a slot or the bounded backoff elapses —
                        # never a busy-spin.
                        runtime.hard_stop_in_flight = False
                        runtime.hard_stop_deferred = True
                        # C-146 P0-4 (RETURN): a qcap-full refusal is also a
                        # failed stop attempt — it is re-armed on the same
                        # SATURATING + fixed-window-bounded backoff, never a
                        # fixed 1.0s busy-spin.
                        runtime.hard_stop_next_attempt_monotonic = (
                            self._hard_stop_next_attempt_locked(
                                runtime,
                                asyncio.get_running_loop().time(),
                            )
                        )
                        self._changed.notify_all()
                        return
                    runtime.hard_stop_deferred = False
                    # Atomically reserve the quarantine slot in the SAME lock
                    # domain as the conversion+persist below, so a capacity
                    # rejection can never follow an irreversible kill and a
                    # concurrent sibling can never steal the last slot mid-stop.
                    runtime.hard_stop_quarantine_reserved = True
                else:
                    # C-146 P0-4 (RETURN): an ALREADY-quarantined record is
                    # re-attempted without consuming a NEW quarantine slot — it
                    # holds its own. Without this, the qcap-full refusal above
                    # would reject the retry forever and the record would
                    # permanently lose its death-confirmation close-out attempt.
                    runtime.hard_stop_deferred = False
                # The generation bump isolates every registry-facing write while
                # the stop attempt is in flight. The final quarantine stage is
                # assigned below once the stop outcome is known.
                runtime.generation += 1
                self._changed.notify_all()
            # Provably stop the real executor within the bounded confirm budget.
            worker = runtime.worker_handle
            try:
                if worker is not None:
                    death_confirmed = await worker.kill_and_confirm(
                        self._hard_stop_confirm_seconds
                    )
                else:
                    operation_task.cancel()
                    await asyncio.wait(
                        (operation_task,), timeout=self._hard_stop_confirm_seconds
                    )
                    death_confirmed = operation_task.done()
            except asyncio.CancelledError:
                # Cancel race: the watchdog is being torn down (registry close).
                # Re-raise so the wrapper is genuinely cancelled; the ``finally``
                # clears the in-flight/reservation flags and NO retry is armed —
                # never a spurious backoff during shutdown, never a leaked flag.
                raise
            except Exception as exc:
                # C-146 P0-4 (RETURN): a persistent cleanup-confirm EXCEPTION
                # must NOT surface as an unhandled wrapper task exception nor
                # busy-spin the watchdog. The executor death is UNPROVEN: keep
                # the record non-terminal, treat it exactly like a death-NOT-
                # confirmed outcome below (quarantine as orphan-in-process,
                # deferred on a SATURATING + fixed-window-bounded backoff), and
                # consume the exception here so the wrapper completes normally.
                logger.warning(
                    "hard-stop kill/confirm raised for job %s: %s",
                    runtime.snapshot.id,
                    type(exc).__name__,
                )
                death_confirmed = False
            async with self._changed:
                if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                    # A concurrent terminalize won (or the operation finished on
                    # its own); the reserved slot was never consumed. The record
                    # is terminal — clear the in-flight marker so a later re-scan
                    # (or a fresh watchdog) treats it as settled, never as
                    # still-being-stopped.
                    runtime.hard_stop_in_flight = False
                    runtime.hard_stop_quarantine_reserved = False
                    self._changed.notify_all()
                    return
                # The reserved slot is now consumed by the quarantine conversion
                # (P0-7: it was checked+reserved BEFORE any stop/kill side
                # effect).
                runtime.hard_stop_quarantine_reserved = False
                if death_confirmed:
                    runtime.hard_stopped = True
                    runtime.orphan_authenticated = True
                    runtime.orphan_death_confirmed = True
                    runtime.quarantine_stage = _QUARANTINE_HARD_STOPPED_STAGE
                else:
                    # In-process orphan that cannot be proven dead: NOT a hard
                    # stop. C-146 P0-4 (RETURN): re-arm a SATURATING +
                    # fixed-window-bounded retry so the record KEEPS its
                    # close-out opportunity instead of silently losing it
                    # forever.
                    runtime.hard_stopped = False
                    runtime.quarantine_stage = _QUARANTINE_ORPHAN_STAGE
                    runtime.hard_stop_deferred = True
                    runtime.hard_stop_next_attempt_monotonic = (
                        self._hard_stop_next_attempt_locked(
                            runtime,
                            asyncio.get_running_loop().time(),
                        )
                    )
                runtime.quarantined = True
                runtime.snapshot = runtime.snapshot.model_copy(
                    update={
                        "cancel_pending": True,
                        "cancellation_requested": True,
                        "stage": runtime.quarantine_stage,
                        "error": (
                            "live planning operation did not stop within the absolute "
                            "deadline+grace execution budget; the executor is "
                            "hard-stopped and quarantined non-terminal"
                        ),
                        "updated_at": self._utc_now(),
                        "revision": runtime.snapshot.revision + 1,
                    }
                )
                for entry in self._idempotency.values():
                    if entry.job_id == runtime.snapshot.id:
                        entry.legacy_isolated = True
                        if entry.updated_at is None:
                            entry.updated_at = self._utc_now()
                try:
                    self._persist_locked()
                except Exception:
                    # Best-effort: a permanent write failure keeps the in-memory
                    # quarantine observable; the reconcile persists it on
                    # recovery.
                    pass
                else:
                    # The durable quarantine fact is already committed.
                    runtime.quarantine_reconciled = True
                self._changed.notify_all()
            # Arm the bounded reconcile owner so the quarantine fact is committed
            # on store recovery (and, when a durable pending outcome exists, the
            # record settles to it once the executor is provably stopped).
            self._ensure_cleanup_owner(runtime)
        finally:
            # C-146 P0-4 (RETURN 7de8cf3e): the in-flight marker and the
            # quarantine slot reservation are released on EVERY exit — a
            # kill/confirm exception or a cancel race must never leak them, or
            # the watchdog would skip the record forever and it would
            # permanently lose its close-out attempt. Synchronous (no await) so
            # a cancellation delivered mid-stop cannot abort the cleanup; the
            # watchdog only re-scans after this wrapper completes, so the cleared
            # flags are visible to that re-scan.
            runtime.hard_stop_in_flight = False
            runtime.hard_stop_quarantine_reserved = False
            self._wake_hard_stop_watchdog()

    async def _cancel_and_drain_operation(self, runtime: _RuntimeJob) -> bool:
        """Stop the real executor and confirm it is dead within the budget.

        C-146 hard-stop gate (12e35d45 门 1): a worker subprocess is SIGKILLed
        and waitpid-confirmed dead (``kill_and_confirm``) — the process death is
        the proof, and the communicate task drains right after. An in-process
        task is cancelled and awaited. Returns True only when the executor is
        confirmed stopped; False means it is still alive past the budget and the
        caller must fail closed rather than publish a terminal label over live
        work."""
        operation_task = runtime.operation_task
        if operation_task is None or operation_task.done():
            # C-146 P0-5: a cold-booted runtime has no in-memory operation task,
            # but its DURABLE worker identity may still point at a LIVE orphan
            # process group (the parent API was SIGKILLed mid-run; e.g. a
            # stubborn descendant survived the group kill, or the worker is still
            # mid-spawn). Never report "stopped" while an authenticated group is
            # alive: authenticate via the marker nonce in the group's command
            # line, then kill + confirm the WHOLE group before any terminalize /
            # permit release. An unauthenticated or already-dead group is a
            # no-op (reused PGID / natural exit) and stays "stopped".
            if (
                runtime.worker_handle is None
                and runtime.worker_pgid is not None
                and runtime.worker_marker
            ):
                if runtime.orphan_death_confirmed is True:
                    return True
                commands = self._group_commands(runtime.worker_pgid)
                if any(runtime.worker_marker in line for line in commands):
                    runtime.orphan_authenticated = True
                    confirmed = await self._kill_orphan_group(
                        runtime.worker_pgid,
                        runtime.worker_marker,
                    )
                    if confirmed:
                        runtime.orphan_death_confirmed = True
                        runtime.hard_stopped = True
                    return confirmed
                return False
            return True
        worker = runtime.worker_handle
        if worker is not None:
            try:
                confirmed = await worker.kill_and_confirm(
                    self._cancel_wait_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A kill/query failure is not evidence of death.  Consume the
                # exception, keep the record non-terminal and let the existing
                # confirmation/cleanup owner retry on its bounded cadence.
                logger.warning(
                    "live planning worker drain confirmation raised for job %s: %s",
                    runtime.snapshot.id,
                    type(exc).__name__,
                )
                return False
            if confirmed:
                runtime.orphan_authenticated = True
                runtime.orphan_death_confirmed = True
                # The PID is dead; let the communicate task drain its buffers so
                # ``operation_task.done()`` becomes True for the shared
                # ``_executors_stopped`` predicate.
                with suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.shield(operation_task),
                        timeout=self._cancel_wait_seconds,
                    )
            return confirmed
        operation_task.cancel()
        done, _ = await asyncio.wait(
            (operation_task,),
            timeout=self._cancel_wait_seconds,
        )
        return operation_task in done

    async def _persist_locked_with_bounded_retry(self) -> None:
        """Persist the current in-memory isolation with a bounded retry.

        Shared primitive of the durable-intent state machine used by close() and
        the deadline path: the cancellation/timeout intent is atomically written
        BEFORE any real executor is stopped, so a failure here never publishes a
        terminal label over live work and never abandons a runner without a
        durable owner. A post-commit (indeterminate) failure means the isolation
        is already durable — return success. A pre-commit failure is retried up
        to ``_cancel_isolation_persist_attempts`` times with a short backoff. If
        every attempt fails pre-commit, the last error is raised; the caller
        keeps its own rollback policy: close() rolls the isolation markers back
        so the executors stay untouched, while the deadline path KEEPS the
        in-memory isolation as the observable owner and still drains the live
        operation. Callers must hold ``self._lock`` (or ``self._changed``)."""
        last_error: BaseException | None = None
        for attempt in range(1, self._cancel_isolation_persist_attempts + 1):
            try:
                self._persist_locked()
                return
            except LivePlanningJobRegistryPostCommitError:
                # The isolation is already committed; the in-memory record
                # matches it, so the intent phase is done.
                return
            except Exception as exc:
                last_error = exc
                if attempt < self._cancel_isolation_persist_attempts:
                    await asyncio.sleep(min(0.02 * attempt, 0.1))
        assert last_error is not None
        raise last_error

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
            if len(existing) >= 400:
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
        validated = tuple(LiveSourceTerminalEvent.model_validate(event) for event in events)
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
        # C-146 P0 supplement (P0-4): settle a quarantined record whose
        # reconciliation is complete (quarantine + target facts durable AND the
        # executor provably stopped) to its DURABLE pending outcome. The
        # unquarantine is atomic with the terminal commit: a pre-commit failure
        # restores the quarantine flag and every quarantine field together with
        # the snapshot, so memory and disk never split.
        settle_quarantined: bool = False,
    ) -> None:
        async with self._changed:
            if runtime.quarantined and not settle_quarantined:
                # C-146 P0 supplement (P0-4): a quarantined record is
                # NON-terminal by contract — no path may terminalize it. Only
                # the bounded reconcile (after the quarantine + target facts are
                # durable AND the executor is provably stopped) settles it, and
                # it does so atomically via ``settle_quarantined``.
                return
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
            previous_pending_terminal = runtime.pending_terminal
            previous_quarantined = runtime.quarantined
            previous_quarantine_stage = runtime.quarantine_stage
            previous_quarantine_reconciled = runtime.quarantine_reconciled
            if settle_quarantined:
                runtime.quarantined = False
                runtime.quarantine_stage = None
            self._terminalize_locked(
                runtime,
                state,
                stage=stage,
                result=result,
                error=error,
                safe_failure=safe_failure,
                cancellation_requested=cancellation_requested,
            )
            # C-145 P0: consume the durable pending outcome IN THE SAME atomic
            # write as the terminal state — the on-disk record never keeps a
            # stale retry intent once the terminal commit succeeds. A pre-commit
            # failure restores it as the recoverable owner for the next attempt,
            # a close / same-key retry / cold start.
            runtime.pending_terminal = None
            try:
                self._persist_locked()
            except LivePlanningJobRegistryPostCommitError:
                # The terminalized state was already committed to disk; the
                # in-memory record matches it (pending outcome consumed), so keep
                # it and surface the indeterminate terminal outcome to the caller.
                raise
            except Exception:
                # A pre-commit persist failure must leave the whole mutable record —
                # snapshot, generation, the prepared flag, the full
                # activation_operation body including every nested field AND the
                # pending terminal outcome — byte-identical to the untouched disk
                # file, so a same-process retry and a cold restart observe the
                # same facts. A quarantined settlement also restores every
                # quarantine field so the record is quarantined again in memory,
                # matching the untouched disk file.
                runtime.snapshot = previous_snapshot
                runtime.generation = previous_generation
                runtime.prepared = previous_prepared
                runtime.activation_operation = previous_activation_operation
                runtime.pending_terminal = previous_pending_terminal
                runtime.quarantined = previous_quarantined
                runtime.quarantine_stage = previous_quarantine_stage
                runtime.quarantine_reconciled = previous_quarantine_reconciled
                raise
            self._changed.notify_all()

    async def _mark_cancel_stuck(
        self,
        runtime: _RuntimeJob,
        *,
        stage: str = "cancel_timed_out",
        error: str | None = None,
        pending_state: LivePlanningJobState = LivePlanningJobState.CANCELLED,
        pending_stage: str = "cancelled",
        pending_result: dict[str, Any] | None = None,
        pending_error: str | None = None,
        pending_safe_failure: _SafeFailureDiagnostic | None = None,
        # C-145 P0 supplement: every durable pending outcome is a cancel/close
        # (or deadline) intent, so its cancellation flag is ALWAYS true — the
        # strict decoder requires exactly ``True``. A None default would let a
        # cancel/close stuck isolation persist a pending outcome the decoder
        # would reject on a later cold start.
        pending_cancellation_requested: bool | None = True,
    ) -> None:
        """P0-1 fail-closed outcome when the operation did not stop within the
        bounded cancellation budget.

        The job stays NON-terminal with an externally visible ``cancel_pending``
        state and an explicit stuck stage (``cancel_timed_out`` for a cancellation,
        ``timeout_pending`` for a deadline cleanup), so a caller is never told the
        job is cleanly cancelled/failed while the operation may still be running.
        The generation bump isolates the still-alive operation from any further
        registry writes.

        C-145 P0: the caller (cancel/close/deadline) supplies the unambiguous
        DURABLE terminal intent — cancel/close → CANCELLED/cancelled, deadline →
        FAILED/deadline_exceeded with the safe-failure diagnostic — which is
        persisted ATOMICALLY with the stuck isolation. Once the real operation
        stops on its own, the cleanup owner (or a cold restart reading the same
        durable intent) collects the record to exactly that outcome — never a
        guessed cancelled/failed label. A pre-commit persist failure leaves no
        pending outcome in memory or on disk: the record keeps the previously
        durable cancel_pending isolation as the recoverable owner, so the
        operation done-callback never auto-collects over an isolation the disk
        does not agree with.
        """
        previous_pending_terminal = runtime.pending_terminal
        async with self._changed:
            if runtime.quarantined:
                # C-146 P0 supplement (P0-4): a quarantined record is
                # NON-terminal by contract — never re-isolated under a new stuck
                # stage, never given a terminal label. The bounded reconcile
                # owns its settlement.
                return
            if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return
            previous_snapshot = runtime.snapshot
            previous_generation = runtime.generation
            previous_prepared = runtime.prepared
            previous_activation_operation = runtime.activation_operation
            runtime.snapshot = runtime.snapshot.model_copy(
                update={
                    "cancel_pending": True,
                    "stage": stage,
                    "error": (
                        error
                        or (
                            "live planning operation did not stop within the bounded "
                            "cancellation budget; the job stays non-terminal and the "
                            "operation is isolated"
                        )
                    ),
                    "revision": runtime.snapshot.revision + 1,
                    "updated_at": self._utc_now(),
                }
            )
            runtime.generation += 1
            # C-145 P0: the pending outcome is part of the SAME atomic write as
            # the stuck isolation — a pre-commit failure rolls both back, a
            # successful write makes the retry intent durable for the owner and
            # for a cold restart. C-146 P0 supplement: the FIRST durable intent
            # wins — an existing intent (e.g. a deadline's committed
            # FAILED/deadline_exceeded recorded before the operation proved
            # stubborn) is NEVER overwritten by a later join's guess.
            if runtime.pending_terminal is None:
                runtime.pending_terminal = _PendingTerminalOutcome(
                    state=pending_state,
                    stage=pending_stage,
                    result=pending_result,
                    error=pending_error,
                    safe_failure=pending_safe_failure,
                    cancellation_requested=pending_cancellation_requested,
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
                runtime.pending_terminal = previous_pending_terminal
                raise
            self._changed.notify_all()
        # C-145 P1: ONLY a durably committed stuck isolation owns a terminal
        # outcome — the cleanup owner auto-collects the record to the pending
        # state when the real operation stops on its own. The durable intent is
        # already on disk, so a cold restart continues the same collection.
        self._ensure_cleanup_owner(runtime)

    async def _join_pending_cleanup(
        self,
        runtime: _RuntimeJob,
        *,
        fallback_state: LivePlanningJobState,
        fallback_stage: str,
        fallback_cancellation_requested: bool | None = None,
    ) -> None:
        """Terminalize a stopped runtime to its DURABLE pending outcome when one
        is recorded, otherwise to the caller's own fallback terminal label.

        C-145 P0: cancel()/close()/same-key retry join a cleanup that may have
        failed closed earlier. The FIRST cleanup owner's durable intent wins — a
        deadline cleanup stays FAILED/deadline_exceeded even when a later close()
        or same-key retry joins — so the outcome can never drift to a guessed
        label. ``_finish`` is idempotent: a concurrent terminalize returns early."""
        pending = runtime.pending_terminal
        if pending is not None:
            await self._finish(
                runtime,
                pending.state,
                stage=pending.stage,
                result=pending.result,
                error=pending.error,
                safe_failure=pending.safe_failure,
                cancellation_requested=pending.cancellation_requested,
            )
        else:
            await self._finish(
                runtime,
                fallback_state,
                stage=fallback_stage,
                cancellation_requested=fallback_cancellation_requested,
            )

    def _complete_cancel_terminalize_locked(self, runtime: _RuntimeJob) -> None:
        """P0-1: idempotently terminalize a cancel_pending job whose executor has
        already stopped, without re-acquiring the registry lock.

        Called from the idempotent retry path while holding ``self._lock``. A
        pre-commit persist failure restores the durable cancel_pending snapshot
        (never the pre-cancel RUNNING state), so a later retry completes the same
        terminalization and the job is never reported as active over a dead
        executor.

        C-145 P0: the DURABLE pending outcome wins when one is recorded — a
        deadline cleanup stays FAILED/deadline_exceeded even when the same-key
        retry joins — and the caller's own CANCELLED label is only the fallback
        for a cancel_pending record without a recorded intent."""
        if runtime.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
            return
        previous_snapshot = runtime.snapshot
        previous_generation = runtime.generation
        previous_prepared = runtime.prepared
        previous_activation_operation = runtime.activation_operation
        previous_pending_terminal = runtime.pending_terminal
        pending = runtime.pending_terminal
        if pending is not None:
            self._terminalize_locked(
                runtime,
                pending.state,
                stage=pending.stage,
                result=pending.result,
                error=pending.error,
                safe_failure=pending.safe_failure,
                cancellation_requested=pending.cancellation_requested,
            )
        else:
            self._terminalize_locked(
                runtime,
                LivePlanningJobState.CANCELLED,
                stage="cancelled",
                cancellation_requested=True,
            )
        # C-145 P0: consume the durable pending outcome IN THE SAME atomic write
        # as the terminal state — the on-disk record never keeps a stale retry
        # intent once the terminal commit succeeds. A pre-commit failure restores
        # it so a later same-key retry completes the same terminalization.
        runtime.pending_terminal = None
        try:
            self._persist_locked()
        except LivePlanningJobRegistryPostCommitError:
            raise
        except Exception:
            runtime.snapshot = previous_snapshot
            runtime.generation = previous_generation
            runtime.prepared = previous_prepared
            runtime.activation_operation = previous_activation_operation
            runtime.pending_terminal = previous_pending_terminal
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
            raise LivePlanningJobInactiveError("live planning job generation is no longer active")

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
        if runtime.quarantined:
            # C-146 P0 supplement (P0-4): a quarantined record is NON-terminal by
            # contract — the shared terminalize primitive itself refuses to
            # fabricate a terminal label for it, no matter the caller. Only the
            # bounded reconcile (facts durable + executor stopped) unquarantines,
            # and only then may this settle the durable intent.
            return
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
            "safe_failure_details": (safe_failure.details if safe_failure is not None else None),
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
        # C-145 P1: the pending terminal outcome is consumed ONLY after the
        # terminal state is durably persisted (in _finish /
        # _complete_cancel_terminalize_locked), so a pre/post-commit failure
        # keeps the recoverable owner.
        runtime.generation += 1
        # A terminalized job can no longer be an un-activated prepared record; the
        # on-disk invariant requires a prepared record to be QUEUED, so clearing
        # this here keeps every persist (cancel / close / restore) loadable.
        runtime.prepared = False
        if (
            state == LivePlanningJobState.CANCELLED
            and runtime.activation_operation is not None
            and runtime.activation_operation.get("phase") not in {"committed", "cancelled"}
        ):
            # Reassign a fresh operation object instead of mutating the existing
            # dict in place so a caller that snapshots the mutable record before
            # this call can restore the original body on a failed persist.
            runtime.activation_operation = {
                **runtime.activation_operation,
                "phase": "cancelled",
            }

    def _make_capacity_locked(
        self,
    ) -> tuple[_RuntimeJob | None, list[tuple[str, _IdempotencyEntry]]]:
        """Reserve executable admission capacity, evicting the oldest terminal
        record if needed.

        C-146 P0-6: the eviction is RETURNED (not silently dropped) so a caller
        whose admission FAILS before the new record is durably committed can roll
        the eviction back — a failed admission must never destroy the old record's
        idempotency binding. Returns ``(evicted_runtime, evicted_idempotency)``;
        ``(None, [])`` when nothing was evicted.
        """
        # C-146 P0-5: while persistent quarantine overflow is set the registry is
        # fail-closed — no NEW admission (job start) is allowed either. Bounded
        # retention cleanup is the only path that clears the flag and restores
        # admission capacity.
        if self._quarantine_overflow:
            raise LivePlanningJobCapacityError(
                "live planning job quarantine capacity exceeded"
            )
        # C-146 P0 supplement (P0-4) / b119: a quarantined record is NON-terminal
        # and never occupies executable active capacity — it has its own bounded
        # quota. A capacity ghost (e.g. an old isolated_ambiguous_cancel record)
        # can therefore never permanently deny every new key.
        quarantined = sum(1 for item in self._records.values() if item.quarantined)
        if quarantined >= self._quarantine_capacity:
            raise LivePlanningJobCapacityError("live planning job quarantine capacity exceeded")
        if sum(1 for item in self._records.values() if not item.quarantined) < self._capacity:
            return None, []
        terminal = [
            item
            for item in self._records.values()
            if not item.quarantined and item.snapshot.state in TERMINAL_LIVE_PLANNING_JOB_STATES
        ]
        if terminal:
            oldest = min(terminal, key=lambda item: item.snapshot.updated_at)
            evicted_entries = [
                (key, entry)
                for key, entry in self._idempotency.items()
                if entry.job_id == oldest.snapshot.id
            ]
            self._remove_locked(oldest.snapshot.id)
            return oldest, evicted_entries
        if sum(1 for item in self._records.values() if not item.quarantined) >= self._capacity:
            raise LivePlanningJobCapacityError("live planning job capacity exceeded")
        return None, []

    def _restore_capacity_locked(
        self,
        evicted: _RuntimeJob | None,
        evicted_entries: list[tuple[str, _IdempotencyEntry]],
    ) -> None:
        """C-146 P0-6: roll back a ``_make_capacity_locked`` eviction.

        Called when the admission that triggered the eviction FAILS before the
        new record is durably committed, restoring the evicted terminal record
        and its idempotency bindings so the old identity mapping is never
        silently destroyed by a failed admission."""
        if evicted is None:
            return
        self._records[evicted.snapshot.id] = evicted
        for key, entry in evicted_entries:
            self._idempotency[key] = entry

    def _prune_locked(self, now: datetime) -> None:
        expired: list[str] = []
        reclaimed: list[str] = []
        for job_id, runtime in self._records.items():
            if runtime.quarantined:
                # C-146 P0 supplement (P0-4) / b119: a quarantined record is
                # reclaimed after its own bounded retention window, but ONLY
                # once its executor is provably dead (no live in-process
                # operation task / subprocess may still be writing side
                # effects). Retention/compression never deletes an unknown or
                # live orphan. Reclamation keeps a minimal durable tombstone
                # (the legacy_isolated idempotency binding) so a same-key
                # request still fails closed and the key is never silently
                # reused.
                if (
                    runtime.snapshot.updated_at + self._quarantine_retention <= now
                    and self._executors_stopped(runtime)
                ):
                    reclaimed.append(job_id)
                continue
            if self._authenticated_orphan_alive(runtime):
                # C-146 P0-3 (RETURN 7de8cf3e): never prune a record whose
                # DURABLE worker identity still points at a LIVE orphan process
                # group that this process has not yet authenticated + killed +
                # reaped — its executor is not stopped. At cold load this gate
                # runs BEFORE ``restore_after_restart`` discovers and cleans the
                # orphan, so a legitimate orphaned executor is never silently
                # removed/reclaimed over live external side effects.
                continue
            if (runtime.snapshot.expires_at is not None and runtime.snapshot.expires_at <= now) or (
                runtime.prepared and runtime.task is None and runtime.snapshot.deadline_at <= now
            ):
                expired.append(job_id)
        for job_id in reclaimed:
            self._reclaim_quarantine_locked(job_id)
        for job_id in expired:
            self._remove_locked(job_id)
        # C-146 P0-5: bounded retention cleanup is the ONLY path that restores
        # admission capacity. Once reclamation brings the durable quarantined
        # count back to the current qcap, the fail-closed overflow flag is
        # cleared so new conversions/admissions resume.
        overflow_was_set = self._quarantine_overflow
        if overflow_was_set:
            quarantined_now = sum(
                1 for item in self._records.values() if item.quarantined
            )
            if quarantined_now <= self._quarantine_capacity:
                self._quarantine_overflow = False
        # C-146 hard-stop gate (12e35d45 门 5): the durable tombstone /
        # isolated-identity collection has a bounded TTL sweep, memory=disk. A
        # dangling isolated tombstone (no live record) older than
        # ``tombstone_ttl`` is reclaimed; one with a missing ``updated_at``
        # (a pre-gate legacy file) is NEVER reclaimed by TTL — it stays bounded
        # by ``idempotency_capacity`` instead, so the loader can never be forced
        # into an over-quota file and existing recoverable records are never
        # overwritten by the sweep.
        ttl_expired = [
            partition
            for partition, entry in self._idempotency.items()
            if (
                entry.legacy_isolated
                and self._records.get(entry.job_id) is None
                and entry.updated_at is not None
                and entry.updated_at + self._tombstone_ttl <= now
            )
        ]
        for partition in ttl_expired:
            self._idempotency.pop(partition, None)
        overflow_cleared = overflow_was_set and self._quarantine_overflow is False
        if expired or reclaimed or ttl_expired or overflow_cleared:
            self._persist_locked()
        # C-146 P0-7: quarantine retention freed a slot / cleared overflow — a
        # hard stop REFUSED for a full quota can now be retried immediately.
        # Wake the watchdog so it re-scans the P0-7-deferred records without
        # waiting for their bounded backoff to elapse. The deferred records'
        # next-attempt stamps are reset NOW so the re-scan actually processes
        # them instead of sleeping out the old backoff.
        if reclaimed or overflow_cleared:
            for runtime in self._records.values():
                if runtime.hard_stop_deferred:
                    runtime.hard_stop_deferred = False
                    runtime.hard_stop_next_attempt_monotonic = 0.0
            self._wake_hard_stop_watchdog()

    def _reclaim_quarantine_locked(self, job_id: str) -> None:
        """Reclaim a quarantined record after its retention window (P0-4/b119).

        The record is dropped from ``_records`` but its idempotency binding is
        kept as a minimal durable tombstone (``legacy_isolated``) so a same-key
        request always fails closed and the key is never silently reused.

        C-146 hard-stop gate (12e35d45 门 5): the tombstone is stamped with the
        current time, so the bounded tombstone-TTL sweep (``_prune_locked``) can
        later reclaim it — the durable identity chain is bounded by TTL as well
        as by ``idempotency_capacity``."""
        runtime = self._records.pop(job_id, None)
        if runtime is None:
            return
        now = self._utc_now()
        for entry in self._idempotency.values():
            if entry.job_id == job_id:
                entry.legacy_isolated = True
                entry.updated_at = now

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
        return hashlib.sha256(f"{tenant_partition}\0{idempotency_key}".encode()).hexdigest()

    @staticmethod
    def _valid_request_digest(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    def _utc_now(self) -> datetime:
        value = self._now()
        return _aware(value, "now").astimezone(UTC)
