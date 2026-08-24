from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from tripchord.agents.live_jobs import (
    _ISOLATED_AMBIGUOUS_CANCEL_STAGE,
    _QUARANTINE_HARD_STOPPED_STAGE,
    _QUARANTINE_INTENT_UNCOMMITTED_STAGE,
    _QUARANTINE_ORPHAN_STAGE,
    LivePlanningJobCancellationPendingError,
    LivePlanningJobCapacityError,
    LivePlanningJobIdempotencyConflictError,
    LivePlanningJobInactiveError,
    LivePlanningJobRegistry,
    LivePlanningJobRegistryPostCommitError,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
    LivePlanningSafeFailureCode,
    LivePlanningSafeFailureDetails,
    LiveSourceTerminalEvent,
    _safe_failure_details_digest,
)

REQUEST_SHA256 = "a" * 64


class _DiagnosticCandidate(BaseModel):
    price_cents: int = Field(gt=0)


class _DiagnosticEnvelope(BaseModel):
    candidate: _DiagnosticCandidate
    provider_counters: dict[str, int]


def _pair_checkpoint(
    sequence: int,
    *,
    state: LivePlanningPairCheckpointState = LivePlanningPairCheckpointState.COMPLETED,
    request_sha256: str = REQUEST_SHA256,
) -> LivePlanningPairCheckpoint:
    common: dict[str, Any] = {
        "sequence": sequence,
        "request_sha256": request_sha256,
        "date_pair_id": f"date-pair:{sequence}",
        "departure_date": date(2026, 1, 1) + timedelta(days=sequence),
        "return_date": date(2026, 1, 1) + timedelta(days=sequence + 5),
        "state": state,
        "query_task_ids": tuple(f"source-{sequence}-{index}" for index in range(11)),
        "captured_at": datetime(2026, 8, 4, 8, tzinfo=UTC) + timedelta(minutes=sequence),
    }
    if state == LivePlanningPairCheckpointState.FAILED:
        return LivePlanningPairCheckpoint.create(
            **common,
            failure_class="TimeoutError",
        )
    return LivePlanningPairCheckpoint.create(
        **common,
        run_purpose="exploration_selection",
        finalization_state="exploration_sealed",
        decision_state="reject",
        source_task_count=11,
        exploration_seal_passed=True,
        all_platforms_complete=False,
    )


async def _wait_for_state(
    registry: LivePlanningJobRegistry,
    job_id: str,
    tenant_id: str,
    state: LivePlanningJobState,
) -> None:
    for _ in range(100):
        snapshot = await registry.get(job_id, tenant_id)
        if snapshot is not None and snapshot.state == state:
            return
        # Let wall-clock based asyncio timeouts advance on both macOS and the
        # slower Linux CI event loop instead of spinning only ready callbacks.
        await asyncio.sleep(0.001)
    raise AssertionError(f"job did not reach {state}")


@pytest.mark.asyncio
async def test_job_lifecycle_reports_progress_and_limits_concurrency() -> None:
    registry = LivePlanningJobRegistry(capacity=4, max_running=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first(report: Any) -> dict[str, Any]:
        first_started.set()
        await report("searching_live_sources", 40)
        await release_first.wait()
        return {"run": {"id": "first"}}

    async def second(report: Any) -> dict[str, Any]:
        second_started.set()
        await report("caching_pair_runs", 90)
        return {"run": {"id": "second"}}

    first_job = await registry.start(tenant_id="tenant-a", operation=first)
    second_job = await registry.start(tenant_id="tenant-a", operation=second)
    await first_started.wait()
    await asyncio.sleep(0)
    assert not second_started.is_set()
    queued = await registry.get(second_job.id, "tenant-a")
    assert queued is not None and queued.state == LivePlanningJobState.QUEUED

    release_first.set()
    await _wait_for_state(registry, first_job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    await _wait_for_state(registry, second_job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    completed = await registry.get(second_job.id, "tenant-a")
    assert completed is not None
    assert completed.progress == 100
    assert completed.stage == "complete"
    assert completed.result == {"run": {"id": "second"}}
    assert completed.expires_at is not None
    await registry.close()


@pytest.mark.asyncio
async def test_deadline_includes_queue_wait_and_never_starts_expired_operation() -> None:
    registry = LivePlanningJobRegistry(capacity=2, max_running=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first(_: Any) -> dict[str, Any]:
        first_started.set()
        await release_first.wait()
        return {"ok": True}

    async def second(_: Any) -> dict[str, Any]:
        second_started.set()
        return {"must_not_run": True}

    first_job = await registry.start(tenant_id="tenant-a", operation=first)
    await first_started.wait()
    second_job = await registry.start(
        tenant_id="tenant-a",
        operation=second,
        deadline_seconds=0.02,
    )
    assert second_job.deadline_at > second_job.created_at
    await asyncio.sleep(0.04)
    await _wait_for_state(
        registry,
        second_job.id,
        "tenant-a",
        LivePlanningJobState.FAILED,
    )
    expired = await registry.get(second_job.id, "tenant-a")
    assert expired is not None
    assert expired.stage == "deadline_exceeded"
    assert expired.error == "TimeoutError: live planning job deadline exceeded"
    assert expired.safe_failure_code == "deadline_exceeded"
    assert expired.safe_failure_details is not None
    assert expired.safe_failure_details.exception_class == "TimeoutError"
    assert expired.safe_failure_details.message_sha256 is None
    assert expired.safe_failure_details.validation_model is None
    assert expired.safe_failure_details.validation_errors == ()
    assert expired.safe_failure_details_digest is not None
    assert not second_started.is_set()
    release_first.set()
    await _wait_for_state(registry, first_job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    await registry.close()


@pytest.mark.asyncio
async def test_self_cancelled_operation_becomes_terminal_failure() -> None:
    registry = LivePlanningJobRegistry(capacity=2, max_running=1)

    async def self_cancel(_: Any) -> dict[str, Any]:
        raise asyncio.CancelledError

    job = await registry.start(tenant_id="tenant-a", operation=self_cancel)
    await _wait_for_state(
        registry,
        job.id,
        "tenant-a",
        LivePlanningJobState.FAILED,
    )
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.stage == "failed"
    assert failed.error == "RuntimeError: live planning execution failed"
    assert failed.safe_failure_code == "execution_exception"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "RuntimeError"
    await registry.close()


@pytest.mark.asyncio
async def test_stubborn_cancel_is_terminal_releases_slot_and_fences_late_mutations() -> None:
    registry = LivePlanningJobRegistry(
        capacity=2,
        max_running=1,
        cancel_wait_seconds=0.01,
    )
    stubborn_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    mutation_fenced = asyncio.Event()
    checkpoint_fenced = asyncio.Event()
    trace_fenced = asyncio.Event()
    release_stubborn = asyncio.Event()
    follower_started = asyncio.Event()

    async def stubborn(report: Any) -> dict[str, Any]:
        stubborn_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            try:
                await report("late_progress", 99)
            except LivePlanningJobInactiveError:
                mutation_fenced.set()
            try:
                await report.report_pair_checkpoint(_pair_checkpoint(1))
            except LivePlanningJobInactiveError:
                checkpoint_fenced.set()
            try:
                await report.report_model_trace_summary(
                    report.job_id,
                    REQUEST_SHA256,
                    1,
                    1,
                    0,
                )
            except LivePlanningJobInactiveError:
                trace_fenced.set()
            await release_stubborn.wait()
            return {"late": True}

    async def follower(_: Any) -> dict[str, Any]:
        follower_started.set()
        return {"ok": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=stubborn,
        request_digest=REQUEST_SHA256,
    )
    await stubborn_started.wait()
    cancelled = await registry.cancel(job.id, "tenant-a")
    assert cancelled is not None and cancelled.state == LivePlanningJobState.CANCELLED
    assert cancellation_seen.is_set()
    assert mutation_fenced.is_set()
    assert checkpoint_fenced.is_set()
    assert trace_fenced.is_set()

    follower_job = await registry.start(tenant_id="tenant-a", operation=follower)
    await asyncio.wait_for(follower_started.wait(), timeout=0.2)
    await _wait_for_state(
        registry,
        follower_job.id,
        "tenant-a",
        LivePlanningJobState.SUCCEEDED,
    )
    release_stubborn.set()
    await asyncio.sleep(0)
    final = await registry.get(job.id, "tenant-a")
    assert final == cancelled
    assert final is not None and final.result is None
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline_seconds", [0, -1, float("inf"), float("nan")])
async def test_job_admission_rejects_non_finite_or_non_positive_deadline(
    deadline_seconds: float,
) -> None:
    registry = LivePlanningJobRegistry()

    async def operation(_: Any) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match="finite positive"):
        await registry.start(
            tenant_id="tenant-a",
            operation=operation,
            deadline_seconds=deadline_seconds,
        )
    await registry.close()


@pytest.mark.asyncio
async def test_pair_checkpoints_accumulate_and_survive_terminal_failure_with_tenant_isolation() -> (
    None
):
    registry = LivePlanningJobRegistry(capacity=2)
    first_reported = asyncio.Event()
    release = asyncio.Event()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_pair_checkpoint(_pair_checkpoint(1))
        first_reported.set()
        await release.wait()
        await report.report_pair_checkpoint(
            _pair_checkpoint(2, state=LivePlanningPairCheckpointState.FAILED)
        )
        raise RuntimeError(
            "Bearer secret-token; Cookie: session=raw-cookie; https://provider.invalid"
        )

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await first_reported.wait()
    running = await registry.get(job.id, "tenant-a")
    assert running is not None
    assert running.state == LivePlanningJobState.RUNNING
    assert tuple(item.sequence for item in running.pair_checkpoints) == (1,)
    assert await registry.get(job.id, "tenant-b") is None

    release.set()
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert tuple(item.state for item in failed.pair_checkpoints) == (
        LivePlanningPairCheckpointState.COMPLETED,
        LivePlanningPairCheckpointState.FAILED,
    )
    serialized = failed.model_dump_json()
    assert "secret-token" not in serialized
    assert "raw-cookie" not in serialized
    assert "provider.invalid" not in serialized
    assert failed.pair_checkpoints[1].failure_class == "TimeoutError"
    await registry.close()


@pytest.mark.asyncio
async def test_formal_job_retains_all_sixty_six_pair_checkpoints_and_rejects_401st() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        for sequence in range(1, 67):
            await report.report_pair_checkpoint(_pair_checkpoint(sequence))
        return {}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await asyncio.sleep(0.05)
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    completed = await registry.get(job.id, "tenant-a")
    assert completed is not None
    assert tuple(item.sequence for item in completed.pair_checkpoints) == tuple(range(1, 67))
    assert tuple(item.date_pair_id for item in completed.pair_checkpoints) == tuple(
        f"date-pair:{sequence}" for sequence in range(1, 67)
    )
    reloaded = LivePlanningJobSnapshot.model_validate_json(completed.model_dump_json())
    assert len(reloaded.pair_checkpoints) == 66

    rejected = asyncio.Event()

    async def overflow_operation(report: Any) -> dict[str, Any]:
        for sequence in range(1, 402):
            try:
                await report.report_pair_checkpoint(_pair_checkpoint(sequence))
            except ValueError as exc:
                if sequence == 401 and "less than or equal to 400" in str(exc):
                    rejected.set()
                    break
                raise
        return {}

    await registry.close()
    overflow_registry = LivePlanningJobRegistry()
    overflow = await overflow_registry.start(
        tenant_id="tenant-a",
        operation=overflow_operation,
        request_digest=REQUEST_SHA256,
    )
    await asyncio.sleep(0.05)
    await _wait_for_state(
        overflow_registry,
        overflow.id,
        "tenant-a",
        LivePlanningJobState.SUCCEEDED,
    )
    assert rejected.is_set()
    overflow_snapshot = await overflow_registry.get(overflow.id, "tenant-a")
    assert overflow_snapshot is not None
    assert len(overflow_snapshot.pair_checkpoints) == 400
    await overflow_registry.close()


def test_formal_worker_binds_parallel_checkpoints_by_pair_id_not_position() -> None:
    executions = tuple(
        SimpleNamespace(
            date_pair=SimpleNamespace(
                id=f"date-pair:{pair}",
                departure_date=date(2026, 1, 1) + timedelta(days=pair),
                return_date=date(2026, 1, 1) + timedelta(days=pair + 5),
            )
        )
        for pair in (1, 2, 3)
    )
    by_pair = {execution.date_pair.id: execution for execution in executions}
    # Completion order is 3, 1, 2, but checkpoint sequence remains the
    # contiguous completion sequence 1, 2, 3.  Final pair_runs stay plan
    # order and must still bind successfully.
    checkpoints = tuple(
        _pair_checkpoint(sequence).model_copy(
            update={
                "date_pair_id": pair_id,
                "departure_date": by_pair[pair_id].date_pair.departure_date,
                "return_date": by_pair[pair_id].date_pair.return_date,
            }
        )
        for sequence, pair_id in enumerate(
            ("date-pair:3", "date-pair:1", "date-pair:2"),
            start=1,
        )
    )
    LivePlanningJobRegistry._validate_formal_pair_checkpoint_alignment(
        checkpoints,
        executions,
        request_sha256=REQUEST_SHA256,
    )

    for invalid in (
        checkpoints[:-1],  # missing planned pair
        (
            *checkpoints[:-1],
            checkpoints[-1].model_copy(update={"date_pair_id": "date-pair:1"}),
        ),
        (
            *checkpoints[:-1],
            checkpoints[-1].model_copy(
                update={"departure_date": date(2030, 1, 1)},
            ),
        ),
    ):
        with pytest.raises(RuntimeError, match="checkpoints differ"):
            LivePlanningJobRegistry._validate_formal_pair_checkpoint_alignment(
                invalid,
                executions,
                request_sha256=REQUEST_SHA256,
            )


@pytest.mark.asyncio
async def test_pair_checkpoint_is_retained_when_later_work_is_cancelled() -> None:
    registry = LivePlanningJobRegistry()
    reported = asyncio.Event()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_pair_checkpoint(_pair_checkpoint(1))
        reported.set()
        await asyncio.Event().wait()
        return {}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await reported.wait()
    cancelled = await registry.cancel(job.id, "tenant-a")
    assert cancelled is not None
    assert cancelled.state == LivePlanningJobState.CANCELLED
    assert tuple(item.date_pair_id for item in cancelled.pair_checkpoints) == ("date-pair:1",)
    await registry.close()


def test_pair_checkpoint_model_rejects_extra_sensitive_payload_and_tampered_hash() -> None:
    checkpoint = _pair_checkpoint(1)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LivePlanningPairCheckpoint.model_validate(
            {
                **checkpoint.model_dump(mode="python"),
                "raw_cookie": "session=must-not-be-stored",
                "authorization_token": "Bearer must-not-be-stored",
            }
        )
    with pytest.raises(ValidationError, match="SHA-256 does not match"):
        LivePlanningPairCheckpoint.model_validate(
            {
                **checkpoint.model_dump(mode="python"),
                "decision_state": "accept",
            }
        )


@pytest.mark.asyncio
async def test_registry_rejects_checkpoint_bound_to_a_different_request_digest() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_pair_checkpoint(_pair_checkpoint(1, request_sha256="b" * 64))
        return {"must_not_succeed": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.request_sha256 == REQUEST_SHA256
    assert failed.pair_checkpoints == ()
    assert failed.error == "ValueError: live planning execution failed"
    assert failed.safe_failure_code == "domain_value_error"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "ValueError"
    assert failed.safe_failure_details.validation_model is None
    assert (
        failed.safe_failure_details.message_sha256
        == hashlib.sha256(
            b"live pair checkpoint request SHA-256 does not match its job"
        ).hexdigest()
    )
    await registry.close()


@pytest.mark.asyncio
async def test_pydantic_failure_exposes_only_typed_locations_and_redacts_dynamic_keys() -> None:
    registry = LivePlanningJobRegistry()
    sensitive_url = "https://provider.invalid/quote?token=secret-token"
    sensitive_input = "raw quote CNY 12345"

    async def operation(_: Any) -> dict[str, Any]:
        _DiagnosticEnvelope.model_validate(
            {
                "candidate": {"price_cents": -1},
                "provider_counters": {sensitive_url: sensitive_input},
            }
        )
        return {"must_not_succeed": True}

    job = await registry.start(tenant_id="tenant-a", operation=operation)
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.error == "ValidationError: live planning execution failed"
    assert failed.safe_failure_code == "pydantic_validation_error"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "ValidationError"
    assert failed.safe_failure_details.message_sha256 is None
    assert failed.safe_failure_details.validation_model == "_DiagnosticEnvelope"
    assert tuple(
        (item.type, item.loc) for item in failed.safe_failure_details.validation_errors
    ) == (
        ("greater_than", ("candidate", "price_cents")),
        ("int_parsing", ("provider_counters", "redacted")),
    )
    assert all(
        len(item.message_sha256) == 64 for item in failed.safe_failure_details.validation_errors
    )
    assert failed.safe_failure_details_digest is not None
    serialized = failed.model_dump_json()
    assert sensitive_url not in serialized
    assert sensitive_input not in serialized
    assert "secret-token" not in serialized
    assert "12345" not in serialized

    tampered = failed.model_dump(mode="python")
    tampered["safe_failure_details_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="details digest does not match"):
        LivePlanningJobSnapshot.model_validate(tampered)
    await registry.close()


@pytest.mark.asyncio
async def test_http_exception_chain_preserves_value_error_digest_without_detail_text() -> None:
    registry = LivePlanningJobRegistry()
    sensitive_message = (
        "required-model validator rejected prompt with quote CNY 54321; "
        "https://provider.invalid/order?api_key=secret"
    )

    async def operation(_: Any) -> dict[str, Any]:
        try:
            raise ValueError(sensitive_message)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = await registry.start(tenant_id="tenant-a", operation=operation)
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.error == "HTTPException: live planning execution failed"
    assert failed.safe_failure_code == "domain_value_error"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "ValueError"
    assert failed.safe_failure_details.validation_model is None
    assert (
        failed.safe_failure_details.message_sha256
        == hashlib.sha256(sensitive_message.encode("utf-8")).hexdigest()
    )
    assert failed.safe_failure_details.validation_errors == ()
    assert failed.safe_failure_details_digest is not None
    serialized = failed.model_dump_json()
    assert sensitive_message not in serialized
    assert "54321" not in serialized
    assert "api_key" not in serialized
    await registry.close()


@pytest.mark.asyncio
async def test_http_timeout_chain_ignores_asyncio_internal_cancellation_cause() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(_: Any) -> dict[str, Any]:
        try:
            async with asyncio.timeout(0.001):
                await asyncio.Event().wait()
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="generic timeout") from exc
        return {"must_not_succeed": True}

    job = await registry.start(tenant_id="tenant-a", operation=operation)
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.error == "HTTPException: live planning execution failed"
    assert failed.safe_failure_code == "timeout_error"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "TimeoutError"
    assert failed.safe_failure_details.message_sha256 is None
    assert failed.safe_failure_details.validation_model is None
    assert failed.safe_failure_details.validation_errors == ()
    await registry.close()


@pytest.mark.asyncio
async def test_model_trace_summary_survives_later_job_failure_and_matches_checkpoints() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_pair_checkpoint(_pair_checkpoint(1))
        await report.report_model_trace_summary(
            report.job_id,
            REQUEST_SHA256,
            3,
            2,
            1,
        )
        raise RuntimeError("raw provider payload must not escape")

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.request_sha256 == REQUEST_SHA256
    assert failed.model_trace_scope_sha256 == REQUEST_SHA256
    assert failed.model_trace_count == 3
    assert failed.model_trace_success_count == 2
    assert failed.model_trace_failure_count == 1
    assert failed.pair_checkpoints[0].request_sha256 == REQUEST_SHA256
    assert "raw provider payload" not in failed.model_dump_json()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_id", "scope_request_sha256"),
    (("different-job", REQUEST_SHA256), (None, "b" * 64)),
)
async def test_registry_rejects_model_trace_summary_from_another_scope(
    scope_id: str | None,
    scope_request_sha256: str,
) -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_model_trace_summary(
            scope_id or report.job_id,
            scope_request_sha256,
            1,
            1,
            0,
        )
        return {"must_not_succeed": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.model_trace_count == 0
    assert failed.model_trace_success_count == 0
    assert failed.model_trace_failure_count == 0
    await registry.close()


@pytest.mark.asyncio
async def test_registry_accepts_only_one_final_model_trace_summary() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_model_trace_summary(
            report.job_id,
            REQUEST_SHA256,
            1,
            1,
            0,
        )
        await report.report_model_trace_summary(
            report.job_id,
            REQUEST_SHA256,
            2,
            2,
            0,
        )
        return {"must_not_succeed": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.FAILED)
    failed = await registry.get(job.id, "tenant-a")
    assert failed is not None
    assert failed.model_trace_count == 1
    assert failed.model_trace_success_count == 1
    await registry.close()


@pytest.mark.asyncio
async def test_cancel_propagates_to_operation_and_is_idempotent() -> None:
    registry = LivePlanningJobRegistry()
    started = asyncio.Event()
    cleanup_observed = asyncio.Event()

    async def operation(report: Any) -> dict[str, Any]:
        await report("searching_live_sources", 30)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_observed.set()

    job = await registry.start(tenant_id="tenant-a", operation=operation)
    await started.wait()
    cancelled = await registry.cancel(job.id, "tenant-a")
    assert cleanup_observed.is_set()
    assert cancelled is not None
    assert cancelled.state == LivePlanningJobState.CANCELLED
    assert cancelled.stage == "cancelled"
    assert cancelled.progress == 100
    assert cancelled.cancellation_requested is True

    duplicate = await registry.cancel(job.id, "tenant-a")
    assert duplicate == cancelled
    await registry.close()


@pytest.mark.asyncio
async def test_tenant_isolation_failure_redaction_capacity_and_terminal_ttl() -> None:
    now = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
    current = [now]
    blocker = asyncio.Event()
    registry = LivePlanningJobRegistry(
        capacity=1,
        max_running=1,
        terminal_ttl=timedelta(seconds=30),
        now=lambda: current[0],
    )

    async def failing(_: Any) -> dict[str, Any]:
        raise RuntimeError("secret prompt; quote CNY 12345; https://provider.invalid/order")

    failed_job = await registry.start(tenant_id="tenant-a", operation=failing)
    await _wait_for_state(registry, failed_job.id, "tenant-a", LivePlanningJobState.FAILED)
    assert await registry.get(failed_job.id, "tenant-b") is None
    failed = await registry.get(failed_job.id, "tenant-a")
    assert failed is not None
    assert failed.error == "RuntimeError: live planning execution failed"
    assert "12345" not in failed.model_dump_json()

    async def blocked(_: Any) -> dict[str, Any]:
        await blocker.wait()
        return {}

    # A new admission may evict the oldest terminal record, but never an active one.
    active = await registry.start(tenant_id="tenant-a", operation=blocked)
    with pytest.raises(LivePlanningJobCapacityError):
        await registry.start(tenant_id="tenant-a", operation=blocked)
    assert await registry.get(failed_job.id, "tenant-a") is None
    await registry.cancel(active.id, "tenant-a")

    current[0] += timedelta(seconds=31)
    assert await registry.get(active.id, "tenant-a") is None
    await registry.close()


@pytest.mark.asyncio
async def test_wait_for_change_emits_revisions_and_close_cancels_queued_work() -> None:
    registry = LivePlanningJobRegistry(capacity=2, max_running=1)
    release = asyncio.Event()

    async def blocked(_: Any) -> dict[str, Any]:
        await release.wait()
        return {}

    first = await registry.start(tenant_id="tenant-a", operation=blocked)
    second = await registry.start(tenant_id="tenant-a", operation=blocked)
    first_update = await registry.wait_for_change(
        first.id,
        "tenant-a",
        after_revision=1,
        timeout_seconds=1,
    )
    assert first_update is not None
    assert first_update.state == LivePlanningJobState.RUNNING

    await registry.close()
    first_final = await registry.get(first.id, "tenant-a")
    second_final = await registry.get(second.id, "tenant-a")
    assert first_final is not None and first_final.state == LivePlanningJobState.CANCELLED
    assert second_final is not None and second_final.state == LivePlanningJobState.CANCELLED


@pytest.mark.asyncio
async def test_cancel_http_wait_is_bounded_when_operation_delays_cooperation() -> None:
    registry = LivePlanningJobRegistry(cancel_wait_seconds=0.01)
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def slow_cancel(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            return {"must_not_be_released": True}

    job = await registry.start(tenant_id="tenant-a", operation=slow_cancel)
    await started.wait()
    cancelling = await registry.cancel(job.id, "tenant-a")
    assert cancellation_seen.is_set()
    assert cancelling is not None
    assert cancelling.state == LivePlanningJobState.CANCELLED
    assert cancelling.stage == "cancelled"
    assert cancelling.cancellation_requested is True

    duplicate = await registry.cancel(job.id, "tenant-a")
    assert duplicate == cancelling
    release.set()
    await asyncio.sleep(0)
    final = await registry.get(job.id, "tenant-a")
    assert final is not None and final.result is None
    await registry.close()


@pytest.mark.asyncio
async def test_idempotency_is_tenant_scoped_and_rejects_payload_conflicts() -> None:
    registry = LivePlanningJobRegistry(capacity=4, max_running=2)
    release = asyncio.Event()
    starts = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal starts
        starts += 1
        await release.wait()
        return {"ok": True}

    digest_a = "a" * 64
    first, first_replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="retry-key",
        request_digest=digest_a,
    )
    replay, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="retry-key",
        request_digest=digest_a,
    )
    for _ in range(10):
        if starts == 1:
            break
        await asyncio.sleep(0)
    assert first_replayed is False
    assert replayed is True
    assert replay.id == first.id
    assert starts == 1

    with pytest.raises(LivePlanningJobIdempotencyConflictError):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="retry-key",
            request_digest="b" * 64,
        )

    other_tenant, other_replayed = await registry.start_idempotent(
        tenant_id="tenant-b",
        operation=operation,
        idempotency_key="retry-key",
        request_digest=digest_a,
    )
    for _ in range(10):
        if starts == 2:
            break
        await asyncio.sleep(0)
    assert other_replayed is False
    assert other_tenant.id != first.id
    assert starts == 2
    release.set()
    await _wait_for_state(registry, first.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    await _wait_for_state(
        registry,
        other_tenant.id,
        "tenant-b",
        LivePlanningJobState.SUCCEEDED,
    )
    await registry.close()


@pytest.mark.asyncio
async def test_idempotency_mapping_expires_with_terminal_job_ttl() -> None:
    current = [datetime(2026, 8, 4, 8, 0, tzinfo=UTC)]
    registry = LivePlanningJobRegistry(
        terminal_ttl=timedelta(seconds=10),
        now=lambda: current[0],
    )
    starts = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal starts
        starts += 1
        return {"start": starts}

    first, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="expiring-key",
        request_digest="c" * 64,
    )
    await _wait_for_state(registry, first.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    current[0] += timedelta(seconds=11)

    replacement, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="expiring-key",
        request_digest="d" * 64,
    )
    assert replayed is False
    assert replacement.id != first.id
    await _wait_for_state(
        registry,
        replacement.id,
        "tenant-a",
        LivePlanningJobState.SUCCEEDED,
    )
    assert starts == 2
    await registry.close()


@pytest.mark.asyncio
async def test_capacity_eviction_removes_idempotency_mapping_with_terminal_job() -> None:
    registry = LivePlanningJobRegistry(capacity=1)

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    first, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="capacity-key",
        request_digest="e" * 64,
    )
    await _wait_for_state(registry, first.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    intervening = await registry.start(tenant_id="tenant-a", operation=operation)
    await _wait_for_state(
        registry,
        intervening.id,
        "tenant-a",
        LivePlanningJobState.SUCCEEDED,
    )
    assert await registry.get(first.id, "tenant-a") is None

    replacement, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="capacity-key",
        request_digest="f" * 64,
    )
    assert replayed is False
    assert replacement.id != first.id
    await registry.cancel(replacement.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_expired_prepared_job_releases_capacity_and_cannot_activate() -> None:
    """RETURN-8db00bb: prepared capacity follows the frozen execution deadline."""

    current = [datetime(2026, 8, 14, 22, 30, tzinfo=UTC)]
    registry = LivePlanningJobRegistry(
        capacity=1,
        now=lambda: current[0],
    )

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    expired, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="prepared-expiry-one",
        request_digest="1" * 64,
        deadline_seconds=5,
        defer_start=True,
    )
    assert replayed is False
    current[0] += timedelta(seconds=6)

    replacement, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="prepared-expiry-two",
        request_digest="2" * 64,
        deadline_seconds=5,
        defer_start=True,
    )
    assert replayed is False
    assert replacement.id != expired.id
    assert await registry.get(expired.id, "tenant-a") is None
    assert await registry.activate(expired.id, "tenant-a") is None
    await registry.cancel(replacement.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_prepared_retry_rejects_same_key_with_foreign_payload_after_expiry() -> None:
    """An expired prepared mapping is pruned atomically, never resurrected."""

    current = [datetime(2026, 8, 14, 22, 30, tzinfo=UTC)]
    registry = LivePlanningJobRegistry(capacity=1, now=lambda: current[0])

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    first, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="prepared-retry",
        request_digest="3" * 64,
        deadline_seconds=1,
        defer_start=True,
    )
    current[0] += timedelta(seconds=2)
    second, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="prepared-retry",
        request_digest="4" * 64,
        deadline_seconds=1,
        defer_start=True,
    )
    assert replayed is False
    assert second.id != first.id
    await registry.cancel(second.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_source_terminal_events_and_barrier_release_survive_success() -> None:
    registry = LivePlanningJobRegistry()

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_source_terminal_events(
            (
                LiveSourceTerminalEvent(
                    source_task_id="source-ctrip-flight",
                    provider="ctrip",
                    vertical="flight",
                    terminal_state="quote_found",
                    occurred_at=datetime(2026, 8, 4, 8, 1, tzinfo=UTC),
                ),
                LiveSourceTerminalEvent(
                    source_task_id="source-qunar-lodging-full",
                    provider="qunar",
                    vertical="lodging",
                    terminal_state="bounded_no_exact_quote",
                    occurred_at=datetime(2026, 8, 4, 8, 2, tzinfo=UTC),
                ),
            )
        )
        await report.report_barrier_released(datetime(2026, 8, 4, 8, 3, tzinfo=UTC))
        return {"ok": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    done = await registry.get(job.id, "tenant-a")
    assert done is not None
    assert done.barrier_released_at == datetime(2026, 8, 4, 8, 3, tzinfo=UTC)
    assert len(done.source_terminal_events) == 2
    assert done.source_terminal_events[0].source_task_id == "source-ctrip-flight"
    assert done.source_terminal_events[0].terminal_state == "quote_found"
    assert done.source_terminal_events[1].terminal_state == "bounded_no_exact_quote"
    assert done.source_terminal_events[1].detail is None
    await registry.close()


@pytest.mark.asyncio
async def test_barrier_release_is_idempotent_and_terminal_events_are_unique() -> None:
    registry = LivePlanningJobRegistry()
    released_at = datetime(2026, 8, 4, 8, 5, tzinfo=UTC)

    async def operation(report: Any) -> dict[str, Any]:
        await report.report_barrier_released(released_at)
        await report.report_barrier_released(released_at)
        await report.report_source_terminal_events(
            (
                LiveSourceTerminalEvent(
                    source_task_id="source-ctrip-flight",
                    provider="ctrip",
                    vertical="flight",
                    terminal_state="timed_out",
                    occurred_at=released_at,
                ),
            )
        )
        with pytest.raises(ValueError):
            await report.report_source_terminal_events(
                (
                    LiveSourceTerminalEvent(
                        source_task_id="source-ctrip-flight",
                        provider="ctrip",
                        vertical="flight",
                        terminal_state="quote_found",
                        occurred_at=released_at,
                    ),
                )
            )
        return {"ok": True}

    job = await registry.start(
        tenant_id="tenant-a",
        operation=operation,
        request_digest=REQUEST_SHA256,
    )
    await _wait_for_state(registry, job.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
    done = await registry.get(job.id, "tenant-a")
    assert done is not None
    assert done.barrier_released_at == released_at
    assert len(done.source_terminal_events) == 1
    assert done.source_terminal_events[0].terminal_state == "timed_out"
    await registry.close()


@pytest.mark.asyncio
async def test_persistent_registry_cold_restart_terminalizes_prepared_attempt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "live-jobs.json"
    started = False

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal started
        started = True
        return {"ok": True}

    first_registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, replayed = await first_registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cold-restart-prepared",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert replayed is False
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert started is False

    # Constructing a new production registry is the cold-start boundary.  No
    # operation closure is reused; the old attempt becomes a durable tombstone.
    restarted = LivePlanningJobRegistry(state_path=state_path)
    recovered = await restarted.get(prepared.id, "tenant-a")
    assert recovered is not None
    assert recovered.state == LivePlanningJobState.CANCELLED
    assert recovered.stage == "restart_cancelled"
    assert recovered.cancellation_requested is True
    same, same_replayed = await restarted.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cold-restart-prepared",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert same_replayed is True
    assert same == recovered
    fresh, fresh_replayed = await restarted.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cold-restart-fresh",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert fresh_replayed is False
    assert fresh.id != prepared.id
    await restarted.cancel(fresh.id, "tenant-a")
    await restarted.close()


def test_persistent_registry_rejects_symlink_state(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o600)
    state_path = tmp_path / "live-jobs.json"
    state_path.symlink_to(target)
    with pytest.raises(RuntimeError, match="owner-only file"):
        LivePlanningJobRegistry(state_path=state_path)


@pytest.mark.asyncio
async def test_persistent_registry_write_failure_rolls_back_without_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(state_path=state_path)
    started = False

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal started
        started = True
        return {"ok": True}

    def fail_write() -> None:
        raise RuntimeError("injected registry write failure")

    monkeypatch.setattr(registry, "_persist_locked", fail_write)
    with pytest.raises(RuntimeError, match="injected registry write failure"):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="failed-prepare",
            request_digest=REQUEST_SHA256,
            defer_start=True,
        )
    assert registry._records == {}
    assert registry._idempotency == {}
    assert started is False

    monkeypatch.undo()
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="failed-activate",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    monkeypatch.setattr(registry, "_persist_locked", fail_write)
    with pytest.raises(RuntimeError, match="injected registry write failure"):
        await registry.activate(prepared.id, "tenant-a")
    assert await registry.is_prepared(
        prepared.id,
        "tenant-a",
        request_sha256=REQUEST_SHA256,
    )
    assert started is False
    monkeypatch.undo()
    await registry.cancel(prepared.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_activation_operation_is_durable_and_never_redispatched_after_restart(
    tmp_path: Path,
) -> None:
    """P0/91648931: a dispatched formal start is exactly-once AND fail-closed on restart."""

    state_path = tmp_path / "live-jobs.json"
    started = asyncio.Event()
    release = asyncio.Event()
    dispatches = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal dispatches
        dispatches += 1
        started.set()
        await release.wait()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="formal-job-prepare-v1",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "1" * 64,
        "idempotency_key": "formal-activation-v1",
        "request_digest": "2" * 64,
        "job_id": prepared.id,
        "challenge_id": "challenge-exactly-once-v1",
        "attempt_digest": "3" * 64,
        "capability_sha256": "4" * 64,
        "companion_identity_sha256": "5" * 64,
        "queued_result": queued_result,
    }

    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"
    assert stored["dispatch_count"] == 0
    activated = await registry.activate(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert activated is not None and activated.id == prepared.id
    await asyncio.wait_for(started.wait(), timeout=1)
    assert dispatches == 1

    durable = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert durable["phase"] == "dispatched"
    assert durable["dispatch_count"] == 1
    assert durable["queued_result"] == queued_result

    # A cold restart cannot continue the in-flight dispatch: the job is
    # terminalized (the durable activation operation proves the activation was
    # interrupted, so restart_cancelled is the contract-allowed outcome) and
    # the activation fails closed instead of replaying the old QUEUED receipt.
    # It is never re-dispatched.
    restarted = LivePlanningJobRegistry(state_path=state_path)
    restarted_snapshot = await restarted.get(prepared.id, "tenant-a")
    assert restarted_snapshot is not None
    assert restarted_snapshot.state == LivePlanningJobState.CANCELLED
    recovered = await restarted.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert recovered == {
        **durable,
        "phase": "cancelled",
        "dispatch_count": 1,
    }
    with pytest.raises(LivePlanningJobInactiveError, match="was cancelled"):
        await restarted.activate(
            prepared.id,
            "tenant-a",
            operation_id=intent["operation_id"],
        )
    assert dispatches == 1
    assert (
        await restarted.activation_operation(
            prepared.id,
            "tenant-a",
            operation_id=intent["operation_id"],
        )
    )["dispatch_count"] == 1
    with pytest.raises(LivePlanningJobInactiveError, match="foreign activation operation"):
        await restarted.activate(
            prepared.id,
            "tenant-a",
            operation_id="6" * 64,
        )

    release.set()
    await registry.close()
    await restarted.close()


@pytest.mark.asyncio
async def test_cancel_persist_failure_rolls_back_full_activation_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143: a persist failure inside cancel must restore the whole mutable
    record (snapshot, generation, activation_operation incl. nested fields) so
    the surviving in-memory facts agree byte-for-byte with the disk file."""

    state_path = tmp_path / "live-jobs.json"

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cancel-persist-fault-prepare",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "c" * 64,
        "idempotency_key": "cancel-persist-fault-v1",
        "request_digest": "d" * 64,
        "job_id": prepared.id,
        "challenge_id": "cancel-persist-fault-challenge",
        "attempt_digest": "e" * 64,
        "capability_sha256": "f" * 64,
        "companion_identity_sha256": "a1" * 32,
        "queued_result": queued_result,
    }
    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"
    assert stored["dispatch_count"] == 0

    runtime = registry._records[prepared.id]
    before_snapshot = runtime.snapshot
    before_generation = runtime.generation
    before_operation_ref = runtime.activation_operation
    assert before_operation_ref is not None
    before_bytes = state_path.read_bytes()

    def fail_write() -> None:
        raise RuntimeError("injected registry write failure")

    monkeypatch.setattr(registry, "_persist_locked", fail_write)
    with pytest.raises(RuntimeError, match="injected registry write failure"):
        await registry.cancel(prepared.id, "tenant-a")
    monkeypatch.undo()

    # The in-memory record must be byte-identical to the pre-call record and to
    # the untouched disk file: same snapshot object, same generation, same
    # activation_operation object with every nested field intact.
    assert runtime.snapshot == before_snapshot
    assert runtime.generation == before_generation
    assert runtime.activation_operation is before_operation_ref
    assert runtime.activation_operation == stored
    assert runtime.activation_operation["phase"] == "intent"
    assert runtime.activation_operation["dispatch_count"] == 0
    assert runtime.activation_operation["queued_result"] == queued_result
    assert state_path.read_bytes() == before_bytes

    # A fresh cold load reads the untouched disk and applies the registry's own
    # cold-start rule (terminalize the still-queued prepared job) deterministically
    # — the disk file is the single source of truth, so there is no memory/disk
    # split between a same-process retry and a restart.
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(prepared.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage in {"restart_cancelled", "cancelled"}
    reloaded_operation = await reloaded.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert reloaded_operation["phase"] == "cancelled"
    assert reloaded_operation["dispatch_count"] == 0
    assert reloaded_operation["queued_result"] == queued_result

    # A normal retry performs exactly one legal terminalization.
    cancelled = await registry.cancel(prepared.id, "tenant-a")
    assert cancelled is not None
    assert cancelled.state == LivePlanningJobState.CANCELLED
    cancelled_operation = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert cancelled_operation["phase"] == "cancelled"
    again = await registry.cancel(prepared.id, "tenant-a")
    assert again is not None and again.state == LivePlanningJobState.CANCELLED

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
async def test_terminalize_persist_failure_rolls_back_full_activation_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143: a persist failure on the terminalize path (_finish) must restore
    the whole mutable record so memory and disk agree, and a later retry can
    only perform one legal terminalization."""

    state_path = tmp_path / "live-jobs.json"

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="terminalize-persist-fault-prepare",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "c1" * 32,
        "idempotency_key": "terminalize-persist-fault-v1",
        "request_digest": "d1" * 32,
        "job_id": prepared.id,
        "challenge_id": "terminalize-persist-fault-challenge",
        "attempt_digest": "e1" * 32,
        "capability_sha256": "f1" * 32,
        "companion_identity_sha256": "a2" * 32,
        "queued_result": queued_result,
    }
    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"

    runtime = registry._records[prepared.id]
    before_snapshot = runtime.snapshot
    before_generation = runtime.generation
    before_operation_ref = runtime.activation_operation
    assert before_operation_ref is not None
    before_bytes = state_path.read_bytes()

    def fail_write() -> None:
        raise RuntimeError("injected registry write failure")

    monkeypatch.setattr(registry, "_persist_locked", fail_write)
    with pytest.raises(RuntimeError, match="injected registry write failure"):
        await registry._finish(
            runtime,
            LivePlanningJobState.CANCELLED,
            stage="cancelled",
            cancellation_requested=True,
        )
    monkeypatch.undo()

    assert runtime.snapshot == before_snapshot
    assert runtime.generation == before_generation
    assert runtime.activation_operation is before_operation_ref
    assert runtime.activation_operation["phase"] == "intent"
    assert runtime.activation_operation["dispatch_count"] == 0
    assert runtime.activation_operation["queued_result"] == queued_result
    assert state_path.read_bytes() == before_bytes

    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(prepared.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage in {"restart_cancelled", "cancelled"}
    reloaded_operation = await reloaded.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert reloaded_operation["phase"] == "cancelled"
    assert reloaded_operation["dispatch_count"] == 0
    assert reloaded_operation["queued_result"] == queued_result

    # A normal retry performs exactly one legal terminalization, then further
    # terminalization attempts are inert.
    await registry._finish(
        runtime,
        LivePlanningJobState.CANCELLED,
        stage="cancelled",
        cancellation_requested=True,
    )
    final_snapshot = await registry.get(prepared.id, "tenant-a")
    assert final_snapshot is not None
    assert final_snapshot.state == LivePlanningJobState.CANCELLED
    assert runtime.activation_operation["phase"] == "cancelled"
    await registry._finish(
        runtime,
        LivePlanningJobState.CANCELLED,
        stage="cancelled",
        cancellation_requested=True,
    )

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ("post_replace_dir_fsync", "post_replace_validation"),
)
async def test_cancel_post_commit_persist_failure_keeps_committed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failpoint: str,
) -> None:
    """C-143 P0 return: when a persist fails AFTER ``os.replace`` has committed
    the terminalized state to disk, cancel must NOT roll the in-memory record
    back. Memory and disk must both keep the committed terminal state, the
    explicit post-commit error propagates, a fresh instance reads the same facts,
    and a retry does not re-terminalize."""

    state_path = tmp_path / "live-jobs.json"

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key=f"cancel-post-commit-{failpoint}-prepare",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "c2" * 32,
        "idempotency_key": f"cancel-post-commit-{failpoint}",
        "request_digest": "d2" * 32,
        "job_id": prepared.id,
        "challenge_id": f"cancel-post-commit-{failpoint}-challenge",
        "attempt_digest": "e2" * 32,
        "capability_sha256": "f2" * 32,
        "companion_identity_sha256": "a3" * 32,
        "queued_result": queued_result,
    }
    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"
    assert stored["dispatch_count"] == 0

    runtime = registry._records[prepared.id]

    monkeypatch.setenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", failpoint)
    with pytest.raises(LivePlanningJobRegistryPostCommitError):
        await registry.cancel(prepared.id, "tenant-a")
    monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")

    # The committed terminal state is kept in memory AND on disk — no
    # memory/disk split, exception not swallowed.
    snapshot = await registry.get(prepared.id, "tenant-a")
    assert snapshot is not None
    assert snapshot.state == LivePlanningJobState.CANCELLED
    assert snapshot.stage == "cancelled"
    operation = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert operation["phase"] == "cancelled"
    assert operation["dispatch_count"] == 0
    assert operation["queued_result"] == queued_result

    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == prepared.id
    )
    assert disk_record["snapshot"] == snapshot.model_dump(mode="json")
    assert disk_record["snapshot"]["state"] == "cancelled"
    assert disk_record["activation_operation"] == operation
    assert disk_record["prepared"] is runtime.prepared

    # A brand-new instance reads the same committed facts from disk — restart
    # consistency without any re-terminalization (the job is already terminal).
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(prepared.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage == "cancelled"
    reloaded_operation = await reloaded.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert reloaded_operation["phase"] == "cancelled"
    assert reloaded_operation == operation

    # A retry observes the terminal state and does not re-terminalize or
    # re-dispatch.
    retried = await registry.cancel(prepared.id, "tenant-a")
    assert retried is not None and retried.state == LivePlanningJobState.CANCELLED
    assert retried.revision == snapshot.revision
    retried_operation = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert retried_operation == operation

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ("post_replace_dir_fsync", "post_replace_validation"),
)
async def test_terminalize_post_commit_persist_failure_keeps_committed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failpoint: str,
) -> None:
    """C-143 P0 return: the same post-commit persist-failure semantics applied to
    the ``_finish`` terminalize path — committed terminal memory and disk are kept,
    the post-commit error propagates, and a retry is inert."""

    state_path = tmp_path / "live-jobs.json"

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key=f"terminalize-post-commit-{failpoint}-prepare",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "c3" * 32,
        "idempotency_key": f"terminalize-post-commit-{failpoint}",
        "request_digest": "d3" * 32,
        "job_id": prepared.id,
        "challenge_id": f"terminalize-post-commit-{failpoint}-challenge",
        "attempt_digest": "e3" * 32,
        "capability_sha256": "f3" * 32,
        "companion_identity_sha256": "a4" * 32,
        "queued_result": queued_result,
    }
    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"

    runtime = registry._records[prepared.id]

    monkeypatch.setenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", failpoint)
    with pytest.raises(LivePlanningJobRegistryPostCommitError):
        await registry._finish(
            runtime,
            LivePlanningJobState.CANCELLED,
            stage="cancelled",
            cancellation_requested=True,
        )
    monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")

    snapshot = await registry.get(prepared.id, "tenant-a")
    assert snapshot is not None
    assert snapshot.state == LivePlanningJobState.CANCELLED
    assert snapshot.stage == "cancelled"
    operation = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert operation["phase"] == "cancelled"
    assert operation["dispatch_count"] == 0
    assert operation["queued_result"] == queued_result

    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == prepared.id
    )
    assert disk_record["snapshot"] == snapshot.model_dump(mode="json")
    assert disk_record["snapshot"]["state"] == "cancelled"
    assert disk_record["activation_operation"] == operation

    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(prepared.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage == "cancelled"
    reloaded_operation = await reloaded.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert reloaded_operation["phase"] == "cancelled"
    assert reloaded_operation == operation

    # A retry observes the committed terminal state; the terminalize guard
    # returns without any further mutation.
    before_revision = snapshot.revision
    await registry._finish(
        runtime,
        LivePlanningJobState.CANCELLED,
        stage="cancelled",
        cancellation_requested=True,
    )
    after_snapshot = await registry.get(prepared.id, "tenant-a")
    assert after_snapshot is not None
    assert after_snapshot.state == LivePlanningJobState.CANCELLED
    assert after_snapshot.revision == before_revision

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ("post_replace_dir_fsync", "post_replace_validation"),
)
async def test_cancel_post_commit_persist_failure_physically_cancels_active_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failpoint: str,
) -> None:
    """C-143 P0 RETURN 22fb1f9c gap A: when a persist fails AFTER ``os.replace``
    has committed a cancellation, cancel must still physically cancel and await the
    running operation_task / real task before surfacing the post-commit error. The
    committed terminal label must be real — the task is stopped and no further
    side effects occur — and memory/disk agree for a retry and a cold restart."""

    state_path = tmp_path / "live-jobs.json"
    started = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        started.set()
        while True:
            side_effects += 1
            await asyncio.sleep(0.001)
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    job, reused = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key=f"cancel-active-{failpoint}",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert reused is False
    await started.wait()
    runtime = registry._records[job.id]
    for _ in range(100):
        if runtime.operation_task is not None and not runtime.operation_task.done():
            break
        await asyncio.sleep(0.001)
    assert runtime.operation_task is not None and not runtime.operation_task.done()
    assert not runtime.task.done()

    monkeypatch.setenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", failpoint)
    with pytest.raises(LivePlanningJobRegistryPostCommitError):
        await registry.cancel(job.id, "tenant-a")
    monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")

    # The post-commit error surfaces only after the running work was physically
    # cancelled and awaited — the committed terminal state is real.
    assert runtime.task.done()
    assert runtime.operation_task.done()
    snapshot = await registry.get(job.id, "tenant-a")
    assert snapshot is not None
    assert snapshot.state == LivePlanningJobState.CANCELLED
    assert snapshot.stage == "cancelled"
    assert snapshot.cancellation_requested is True
    frozen_side_effects = side_effects
    await asyncio.sleep(0.02)
    assert side_effects == frozen_side_effects

    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == job.id
    )
    assert disk_record["snapshot"] == snapshot.model_dump(mode="json")
    assert disk_record["snapshot"]["state"] == "cancelled"

    # A retry observes the committed terminal state without re-terminalizing.
    retried = await registry.cancel(job.id, "tenant-a")
    assert retried is not None and retried.state == LivePlanningJobState.CANCELLED
    assert retried.revision == snapshot.revision

    # A brand-new instance reads the same committed facts from disk.
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(job.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage == "cancelled"

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ("post_replace_dir_fsync", "post_replace_validation"),
)
async def test_activate_post_commit_persist_failure_completes_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failpoint: str,
) -> None:
    """C-143 P0 RETURN 22fb1f9c gap B: when activate's persist fails AFTER
    ``os.replace`` has committed ``phase=dispatched``, the dispatch must be
    completed with a real executor before surfacing the post-commit error — never a
    fake dispatched record with no runner. Memory/disk agree, a same-operation
    retry returns the same running job, a foreign operation is rejected, and a cold
    restart fail-closes the still-nonterminal record."""

    state_path = tmp_path / "live-jobs.json"
    started = asyncio.Event()
    release = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key=f"activate-dispatch-{failpoint}-prepare",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    queued_result = {"job": prepared.model_dump(mode="json")}
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "b4" * 32,
        "idempotency_key": f"activate-dispatch-{failpoint}",
        "request_digest": "c4" * 32,
        "job_id": prepared.id,
        "challenge_id": f"activate-dispatch-{failpoint}-challenge",
        "attempt_digest": "d4" * 32,
        "capability_sha256": "e4" * 32,
        "companion_identity_sha256": "f4" * 32,
        "queued_result": queued_result,
    }
    stored = await registry.prepare_activation_intent(
        prepared.id,
        "tenant-a",
        intent=intent,
    )
    assert stored["phase"] == "intent"
    assert stored["dispatch_count"] == 0

    runtime = registry._records[prepared.id]
    assert runtime.prepared is True

    monkeypatch.setenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", failpoint)
    with pytest.raises(LivePlanningJobRegistryPostCommitError):
        await registry.activate(prepared.id, "tenant-a", operation_id=intent["operation_id"])
    monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")

    # The committed dispatched record now has a real executor, and memory and disk
    # agree on every persisted field (checked before the created task runs).
    assert runtime.task is not None and not runtime.task.done()
    assert runtime.prepared is False
    operation = await registry.activation_operation(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert operation["phase"] == "dispatched"
    assert operation["dispatch_count"] == 1
    assert operation["queued_result"] == queued_result
    snapshot = await registry.get(prepared.id, "tenant-a")
    assert snapshot is not None
    assert snapshot.state == LivePlanningJobState.QUEUED
    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == prepared.id
    )
    assert disk_record["snapshot"] == snapshot.model_dump(mode="json")
    assert disk_record["activation_operation"] == operation
    assert disk_record["prepared"] is runtime.prepared

    # The operation actually runs under the real executor.
    await started.wait()
    assert runtime.operation_task is not None and not runtime.operation_task.done()

    # A same-operation retry returns the same running job — no second dispatch.
    retried = await registry.activate(
        prepared.id,
        "tenant-a",
        operation_id=intent["operation_id"],
    )
    assert retried is not None and retried.id == prepared.id
    assert registry._records[prepared.id].task is runtime.task

    # A foreign operation identity is rejected.
    with pytest.raises(LivePlanningJobInactiveError):
        await registry.activate(prepared.id, "tenant-a", operation_id="ff" * 32)

    # A cold restart fail-closes the still-nonterminal dispatched record: the
    # durable activation operation proves the activation was interrupted, so it
    # is terminalized to restart_cancelled and never re-dispatched.
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(prepared.id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
    assert reloaded_snapshot.stage == "restart_cancelled"

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failpoint",
    ("post_replace_dir_fsync", "post_replace_validation"),
)
async def test_start_post_commit_persist_failure_completes_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failpoint: str,
) -> None:
    """C-143 P0 RETURN 22fb1f9c gap C: when start_idempotent(defer_start=False)
    fails AFTER ``os.replace`` committed the record, the job must still get a real
    executor before surfacing the post-commit error — never a committed
    ``queued/prepared=false/task=None`` record whose same-key retry returns
    ``reused=true`` without running. Memory/disk agree, the same-key retry returns
    the same running job, and a cold restart fail-closes."""

    state_path = tmp_path / "live-jobs.json"
    started = asyncio.Event()
    release = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    monkeypatch.setenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", failpoint)
    with pytest.raises(LivePlanningJobRegistryPostCommitError):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key=f"start-execute-{failpoint}",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
    monkeypatch.delenv("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT")

    (runtime,) = tuple(registry._records.values())
    job_id = runtime.snapshot.id
    # The committed non-prepared record has a real executor; memory and disk agree
    # on every persisted field (checked before the created task runs).
    assert runtime.task is not None and not runtime.task.done()
    assert runtime.prepared is False
    snapshot = await registry.get(job_id, "tenant-a")
    assert snapshot is not None
    assert snapshot.state == LivePlanningJobState.QUEUED
    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == job_id
    )
    assert disk_record["snapshot"] == snapshot.model_dump(mode="json")
    assert disk_record["prepared"] is runtime.prepared

    # The operation actually runs under the real executor.
    await started.wait()
    assert runtime.operation_task is not None and not runtime.operation_task.done()

    # A same-key retry returns the same running job — no second dispatch.
    retried, reused = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key=f"start-execute-{failpoint}",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert reused is True
    assert retried.id == job_id
    assert registry._records[job_id].task is runtime.task

    # A cold restart fail-closes the still-nonterminal record: the operation was
    # running at the crash, so no terminal label is fabricated — it is quarantined
    # NON-terminal (isolated_ambiguous_cancel), never guessed to restart_cancelled.
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    reloaded_snapshot = await reloaded.get(job_id, "tenant-a")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.state == LivePlanningJobState.QUEUED
    assert reloaded_snapshot.stage == "isolated_ambiguous_cancel"

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
async def test_activation_intent_conflict_is_rejected_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = LivePlanningJobRegistry(state_path=tmp_path / "live-jobs.json")
    dispatched = False

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal dispatched
        dispatched = True
        return {"ok": True}

    prepared, _ = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="formal-job-conflict-v1",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "7" * 64,
        "idempotency_key": "formal-activation-conflict-v1",
        "request_digest": "8" * 64,
        "job_id": prepared.id,
        "challenge_id": "challenge-conflict-v1",
        "attempt_digest": "9" * 64,
        "capability_sha256": "a" * 64,
        "companion_identity_sha256": "b" * 64,
        "queued_result": {"job": prepared.model_dump(mode="json")},
    }
    await registry.prepare_activation_intent(prepared.id, "tenant-a", intent=intent)
    with pytest.raises(LivePlanningJobInactiveError, match="activation intent differs"):
        await registry.prepare_activation_intent(
            prepared.id,
            "tenant-a",
            intent={**intent, "request_digest": "c" * 64},
        )
    assert dispatched is False
    assert await registry.is_prepared(
        prepared.id,
        "tenant-a",
        request_sha256=REQUEST_SHA256,
    )
    await registry.cancel(prepared.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_cancel_fails_closed_when_operation_swallows_cancellation_past_budget(
    tmp_path: Path,
) -> None:
    """C-143 P0-1 counter-example: an operation coroutine that catches and swallows
    CancelledError can keep running and producing side effects after cancel() is
    called. cancel() must NOT publish a final CANCELLED while the real
    operation_task is still alive — it must fail closed with a non-terminal,
    externally visible cancel_pending state and an explicit cancellation-timeout
    signal. A final terminal state may appear only once the operation_task is
    confirmed done, and the returned state, memory, disk, task/operation_task,
    same-process retry and cold restart must all agree."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        cancel_wait_seconds=0.05,
    )
    started = asyncio.Event()
    swallowed = asyncio.Event()
    release = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            # Swallow every further cancellation while waiting for the release,
            # then attempt one externally visible side effect and stop. The wait
            # is bounded by `release` so a failed assertion cannot strand a
            # forever-looping operation across the pytest event loop.
            while not release.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.01)
            side_effects += 1
            raise asyncio.CancelledError from None
        return {"ok": True}

    job = await registry.start(tenant_id="tenant-a", operation=operation)
    await started.wait()
    try:
        outcome = await registry.cancel(job.id, "tenant-a")
        assert swallowed.is_set()
        runtime = registry._records[job.id]
        # The real operation is still alive — it swallowed the cancellation and
        # kept working past the bounded cleanup budget.
        assert runtime.operation_task is not None and not runtime.operation_task.done()
        # Fail-closed: never a fake terminal CANCELLED over a running operation.
        assert outcome is not None
        assert outcome.state != LivePlanningJobState.CANCELLED
        assert outcome.cancel_pending is True
        assert outcome.stage == "cancel_timed_out"
        assert outcome.cancellation_requested is True
        frozen = side_effects
        await asyncio.sleep(0.03)
        # The operation is still isolated-but-alive; cancel() did not claim it
        # stopped.
        assert not runtime.operation_task.done()
        assert side_effects == frozen
        release.set()
        for _ in range(100):
            if runtime.operation_task.done():
                break
            await asyncio.sleep(0.01)
        assert runtime.operation_task.done()
        assert side_effects == frozen + 1
        # Once the real operation finally stopped, a repeated cancel (joining the
        # same cleanup semantics without repeating side effects) publishes the true
        # terminal CANCELLED; memory and disk agree.
        retried = await registry.cancel(job.id, "tenant-a")
        assert retried is not None and retried.state == LivePlanningJobState.CANCELLED
        assert retried.stage == "cancelled"
        assert retried.cancel_pending is False
        after = await registry.get(job.id, "tenant-a")
        assert after == retried
        disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk_payload["records"] if record["snapshot"]["id"] == job.id
        )
        assert disk_record["snapshot"] == after.model_dump(mode="json")
        assert disk_record["snapshot"]["cancel_pending"] is False
        # A brand-new instance reads the same committed facts from disk.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        reloaded_snapshot = await reloaded.get(job.id, "tenant-a")
        assert reloaded_snapshot is not None
        assert reloaded_snapshot.state == LivePlanningJobState.CANCELLED
        assert reloaded_snapshot.stage == "cancelled"
        await reloaded.close()
    finally:
        # Ensure the operation stops and the registry closes even when the cancel
        # contract was violated (the red run), so the event loop can tear down.
        release.set()
        runtime = registry._records[job.id]
        if runtime.operation_task is not None and not runtime.operation_task.done():
            runtime.operation_task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(runtime.operation_task, timeout=2)
        await registry.close()


@pytest.mark.asyncio
async def test_idempotency_binds_execution_mode_between_prepared_and_immediate() -> None:
    """C-143 P0-3 counter-example: the idempotency identity must bind the stable
    execution mode (defer_start). The same key + same request digest used first as
    a prepared (defer_start=True) job and then as an immediate job — or the reverse
    — must fail closed with a conflict instead of silently reusing the old receipt
    under a different execution mode."""
    registry = LivePlanningJobRegistry(capacity=4, max_running=2)
    release = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        await release.wait()
        return {"ok": True}

    prepared, prepared_replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="execution-mode-key",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert prepared_replayed is False
    # prepared -> immediate must fail closed, never reuse the prepared receipt.
    with pytest.raises(LivePlanningJobIdempotencyConflictError):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="execution-mode-key",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
    # Same-mode retry stays idempotent.
    prepared_again, prepared_again_replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="execution-mode-key",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert prepared_again_replayed is True
    assert prepared_again.id == prepared.id
    await registry.cancel(prepared.id, "tenant-a")

    # immediate -> prepared must also fail closed.
    immediate, immediate_replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="reverse-mode-key",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert immediate_replayed is False
    with pytest.raises(LivePlanningJobIdempotencyConflictError):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="reverse-mode-key",
            request_digest=REQUEST_SHA256,
            defer_start=True,
        )
    await registry.cancel(immediate.id, "tenant-a")
    await registry.close()


@pytest.mark.asyncio
async def test_idempotency_execution_mode_binding_survives_cold_restart(
    tmp_path: Path,
) -> None:
    """C-143 P0-3: the execution mode is part of the persisted idempotency
    identity, so a real cold restart still rejects a same-key cross-mode request
    (fail-closed conflict) while same-mode retry stays idempotent."""
    state_path = tmp_path / "live-jobs.json"
    release = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        await release.wait()
        return {"ok": True}

    first = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    prepared, _ = await first.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cold-mode-key",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    await first.close()

    second = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    cold = await second.get(prepared.id, "tenant-a")
    assert cold is not None and cold.state == LivePlanningJobState.CANCELLED
    with pytest.raises(LivePlanningJobIdempotencyConflictError):
        await second.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="cold-mode-key",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
    same_mode, replayed = await second.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="cold-mode-key",
        request_digest=REQUEST_SHA256,
        defer_start=True,
    )
    assert replayed is True
    assert same_mode.id == prepared.id
    await second.close()


def _v2_snapshot_model(
    job_id: str,
    state: LivePlanningJobState,
    stage: str,
    progress: int,
    revision: int,
    *,
    cancellation_requested: bool = False,
    expires_at: datetime | None = None,
) -> LivePlanningJobSnapshot:
    # Relative timestamps keep the faithful-v2 fixture valid whenever it runs.
    created = datetime.now(UTC) - timedelta(minutes=5)
    return LivePlanningJobSnapshot(
        id=job_id,
        state=state,
        stage=stage,
        progress=progress,
        revision=revision,
        cancellation_requested=cancellation_requested,
        request_sha256=REQUEST_SHA256,
        model_trace_scope_sha256=REQUEST_SHA256,
        created_at=created,
        updated_at=created,
        deadline_at=created + timedelta(hours=1),
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_cancel_terminalize_precommit_failure_keeps_cancel_pending_not_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143 P0 return counter-example: a pre-commit persist failure on the
    FINAL terminalize of an active cancel must NOT roll the record back to the
    pre-cancel RUNNING state. The cancellation intent (cancel_pending) is
    persisted durably BEFORE the real executor is stopped; once the executor is
    stopped, a failed final persist leaves the recoverable cancel_pending
    isolation state — never an active/RUNNING record over a dead executor. A
    same-key retry completes the terminalization idempotently, a cold restart
    observes the same facts, and foreign identity is still rejected."""

    state_path = tmp_path / "live-jobs.json"
    release = asyncio.Event()
    invocations = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal invocations
        invocations += 1
        await release.wait()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path)
    snapshot, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="active-cancel-precommit",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert replayed is False
    runtime = registry._records[snapshot.id]
    await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)
    assert runtime.operation_task is not None and not runtime.operation_task.done()
    assert invocations == 1

    # Fail the FINAL persist of the cancel (the terminalize), but let the
    # cancel-intent persist succeed. The intent persist happens before the real
    # executor is stopped; the terminalize persist happens only after.
    real_persist = registry._persist_locked

    def fail_after_executor_stopped() -> None:
        if runtime.task is not None and runtime.task.done():
            raise RuntimeError("injected final cancel persist failure")
        real_persist()

    monkeypatch.setattr(registry, "_persist_locked", fail_after_executor_stopped)
    with pytest.raises(RuntimeError, match="injected final cancel persist failure"):
        await registry.cancel(snapshot.id, "tenant-a")
    monkeypatch.undo()

    # The real executor really stopped ...
    assert runtime.task is not None and runtime.task.done()
    assert runtime.operation_task is not None and runtime.operation_task.done()
    # ... but the record is NOT an active/RUNNING claim over that dead executor:
    # the durable cancel_pending isolation state is retained and cancellation was
    # requested, and memory agrees byte-for-byte with the disk.
    assert runtime.snapshot.cancel_pending is True
    assert runtime.snapshot.cancellation_requested is True
    assert runtime.snapshot.stage == "cancelling"
    disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk_payload["records"] if record["snapshot"]["id"] == snapshot.id
    )
    assert disk_record["snapshot"] == runtime.snapshot.model_dump(mode="json")
    assert disk_record["snapshot"]["cancel_pending"] is True

    # A same-key retry completes the terminalization idempotently and never
    # reuses a dead running executor as an active job.
    retried, retried_replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="active-cancel-precommit",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert retried_replayed is True
    assert retried.id == snapshot.id
    assert retried.state == LivePlanningJobState.CANCELLED
    assert retried.stage == "cancelled"
    assert retried.cancel_pending is False
    assert invocations == 1  # no repeated dispatch / side effects

    # A brand-new instance reads the same terminal facts.
    reloaded = LivePlanningJobRegistry(state_path=state_path)
    cold = await reloaded.get(snapshot.id, "tenant-a")
    assert cold is not None
    assert cold.state == LivePlanningJobState.CANCELLED
    assert cold.cancel_pending is False
    # Foreign identity is still rejected.
    assert await reloaded.get(snapshot.id, "other-tenant") is None
    assert await reloaded.cancel(snapshot.id, "other-tenant") is None

    await reloaded.close()
    await registry.close()


@pytest.mark.asyncio
async def test_v2_to_v3_migration_derives_execution_mode_and_survives_consecutive_cold_starts(
    tmp_path: Path,
) -> None:
    """C-143 P0-2 counter-example: a faithful v2 state file must migrate to v3
    with a PROVABLE bool execution mode for every legacy idempotency binding —
    never a null the v3 loader itself rejects — so the FIRST cold start (v2->v3
    migration) AND a SECOND consecutive cold start (v3->v3) both load. After the
    migration and after another cold start, prepared<->immediate same-key mode
    conflicts fail closed in both directions, while the matching derived mode
    idempotently replays; an ambiguous legacy binding fails closed under both
    modes.

    硬门 A: the "immediate" job_a shape (activation_operation=None, prepared=false,
    state=RUNNING) is NOT provably immediate — the old v2 API allowed a formal
    defer_start=true prepared job to activate successfully with operation_id=None,
    leaving exactly this shape. Its mode is therefore isolated (legacy_isolated)
    and a same-key immediate request fails closed instead of replaying. job_b is
    provably prepared (activation intent present) and replays under defer_start=True
    only; job_c is terminal and ambiguous under both modes. No dispatches ever run."""

    state_path = tmp_path / "live-jobs.json"
    tenant_partition = LivePlanningJobRegistry._tenant_partition("tenant-a")

    job_a = "live-job-migrated-immediate"
    job_b = "live-job-migrated-prepared"
    job_c = "live-job-migrated-ambiguous"

    snap_a = _v2_snapshot_model(
        job_a, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1
    ).model_dump(mode="json")
    snap_b = _v2_snapshot_model(job_b, LivePlanningJobState.QUEUED, "queued", 0, 1).model_dump(
        mode="json"
    )
    snap_c = _v2_snapshot_model(
        job_c,
        LivePlanningJobState.CANCELLED,
        "cancelled",
        100,
        2,
        cancellation_requested=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=25),
    ).model_dump(mode="json")

    intent = {
        "schema_version": "tripchord-live-activation-operation-v1",
        "operation_id": "c" * 64,
        "idempotency_key": "v2-prepared",
        "request_digest": "d" * 64,
        "job_id": job_b,
        "challenge_id": "v2-prepared-challenge",
        "attempt_digest": "e" * 64,
        "capability_sha256": "f" * 64,
        "companion_identity_sha256": "a1" * 32,
        "queued_result": {"job": snap_b},
        "phase": "intent",
        "dispatch_count": 0,
    }

    payload = {
        "schema_version": "tripchord-live-job-registry-v2",
        "records": [
            {
                "tenant_partition": tenant_partition,
                "snapshot": snap_a,
                "prepared": False,
                "activation_operation": None,
            },
            {
                "tenant_partition": tenant_partition,
                "snapshot": snap_b,
                "prepared": True,
                "activation_operation": intent,
            },
            {
                "tenant_partition": tenant_partition,
                "snapshot": snap_c,
                "prepared": False,
                "activation_operation": None,
            },
        ],
        "idempotency": [
            {
                "partition": LivePlanningJobRegistry._idempotency_partition(
                    "tenant-a", "v2-immediate"
                ),
                "job_id": job_a,
                "request_digest": REQUEST_SHA256,
            },
            {
                "partition": LivePlanningJobRegistry._idempotency_partition(
                    "tenant-a", "v2-prepared"
                ),
                "job_id": job_b,
                "request_digest": REQUEST_SHA256,
            },
            {
                "partition": LivePlanningJobRegistry._idempotency_partition(
                    "tenant-a", "v2-ambiguous"
                ),
                "job_id": job_c,
                "request_digest": REQUEST_SHA256,
            },
        ],
    }
    state_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    dispatches = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal dispatches
        dispatches += 1
        return {"ok": True}

    async def assert_replay_and_conflicts(registry: LivePlanningJobRegistry) -> None:
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        assert disk["schema_version"] == "tripchord-live-job-registry-v3"
        entries = {entry["job_id"]: entry for entry in disk["idempotency"]}
        # 硬门 A: the immediate-shaped job_a is NOT provably immediate — its mode
        # is isolated (placeholder defer_start=False gated by legacy_isolated).
        assert entries[job_a]["defer_start"] is False
        assert entries[job_a]["legacy_isolated"] is True
        assert entries[job_b]["defer_start"] is True
        assert entries[job_c]["defer_start"] is False  # placeholder, gated by isolation
        assert entries[job_c]["legacy_isolated"] is True

        # C-146 P0-3: an admitted RUNNING record with no cancel intent and a
        # deadline that did NOT pass gives no provable terminal outcome — it is
        # quarantined as isolated_ambiguous_cancel, never guessed to
        # restart_cancelled. A QUEUED (never-admitted) record is provably
        # never-executing, so it keeps the honest restart_cancelled tombstone.
        cold_a = await registry.get(job_a, "tenant-a")
        assert cold_a is not None and cold_a.state == LivePlanningJobState.RUNNING
        assert cold_a.stage == "isolated_ambiguous_cancel"
        assert cold_a.cancel_pending is False
        cold_b = await registry.get(job_b, "tenant-a")
        assert cold_b is not None and cold_b.state == LivePlanningJobState.CANCELLED
        assert cold_b.stage == "restart_cancelled"
        cold_c = await registry.get(job_c, "tenant-a")
        assert cold_c is not None and cold_c.state == LivePlanningJobState.CANCELLED
        assert cold_c.stage == "cancelled"

        # The prepared job's activation operation survives with phase=cancelled
        # and dispatch_count unchanged — no drift on a cold restart.
        operation_b = await registry.activation_operation(job_b, "tenant-a", operation_id="c" * 64)
        assert operation_b["phase"] == "cancelled"
        assert operation_b["dispatch_count"] == 0

        # 硬门 A: the immediate-shaped legacy binding is isolated — a same-key
        # request under EITHER mode fails closed, never replays.
        for mode in (True, False):
            with pytest.raises(LivePlanningJobIdempotencyConflictError):
                await registry.start_idempotent(
                    tenant_id="tenant-a",
                    operation=operation,
                    idempotency_key="v2-immediate",
                    request_digest=REQUEST_SHA256,
                    defer_start=mode,
                )

        # Derived prepared mode replays idempotently; the reverse conflicts.
        replay_b, replay_b_flag = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="v2-prepared",
            request_digest=REQUEST_SHA256,
            defer_start=True,
        )
        assert replay_b_flag is True and replay_b.id == job_b
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="v2-prepared",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )

        # The ambiguous legacy binding fails closed under BOTH modes — never
        # replayed, never re-dispatched.
        for mode in (True, False):
            with pytest.raises(LivePlanningJobIdempotencyConflictError):
                await registry.start_idempotent(
                    tenant_id="tenant-a",
                    operation=operation,
                    idempotency_key="v2-ambiguous",
                    request_digest=REQUEST_SHA256,
                    defer_start=mode,
                )

    # First cold start: v2 -> v3 migration must load and persist valid v3.
    second = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    await assert_replay_and_conflicts(second)
    await second.close()  # full stop before the next cold start

    # Second consecutive cold start: the migrated v3 file must load again.
    third = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    await assert_replay_and_conflicts(third)
    await third.close()

    # No operation was ever dispatched by the migration, the replays, or the
    # conflicts — job/task/operation state never drifted.
    assert dispatches == 0


@pytest.mark.asyncio
async def test_close_fails_closed_over_swallowed_cancel_operation(tmp_path: Path) -> None:
    """C-143 P0 close() counter-example: an active job whose real operation
    swallows CancelledError and keeps writing side effects must NEVER be
    terminalized CANCELLED by close(). The externally visible state stays a
    recoverable non-terminal cancel_pending isolation; memory and disk agree;
    the operation_task is still alive and its side-effect count keeps growing
    after close() returns. Only once the operation truly stops does a repeated
    close() join the same cleanup and publish the final CANCELLED."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        # Faithful swallow-cancel: the operation swallows EVERY CancelledError
        # delivered during the drain window and keeps writing side effects until
        # `stop` is set, so close() can never observe it as stopped.
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"stopped": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    snapshot, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="close-swallow",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert replayed is False
    runtime = registry._records[snapshot.id]
    await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)
    assert runtime.operation_task is not None and not runtime.operation_task.done()

    await registry.close()

    # close() returned, but the operation swallowed the cancel and is still
    # alive — the record must NEVER claim CANCELLED over live work.
    assert swallowed.is_set()
    assert runtime.snapshot.state != LivePlanningJobState.CANCELLED
    assert runtime.snapshot.cancel_pending is True
    assert runtime.operation_task is not None and not runtime.operation_task.done()
    # Disk agrees with memory.
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
    )
    assert disk_record["snapshot"]["state"] != "cancelled"
    assert disk_record["snapshot"]["cancel_pending"] is True
    # Side effects keep growing after close() returned.
    before = side_effects
    await asyncio.sleep(0.05)
    assert side_effects > before

    # Now the operation truly stops; a repeated close() joins the same cleanup
    # and only then publishes the final CANCELLED.
    stop.set()
    await asyncio.sleep(0.05)
    await registry.close()
    assert runtime.operation_task.done()
    final = await registry.get(snapshot.id, "tenant-a")
    assert final is not None and final.state == LivePlanningJobState.CANCELLED
    assert final.stage == "cancelled"


@pytest.mark.asyncio
async def test_same_key_retry_cancel_pending_with_live_operation_fails_closed(
    tmp_path: Path,
) -> None:
    """C-143 硬门 B counter-example: after cancel() fails closed over a
    swallow-cancel operation, a same-key retry must NOT terminalize the record
    CANCELLED or report reused success while the real operation is still
    running. It fails closed with LivePlanningJobCancellationPendingError and
    leaves the identical non-terminal cancel_pending isolation. Once the
    operation truly stops, the retry completes the terminalization."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    snapshot, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="retry-live",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert replayed is False
    runtime = registry._records[snapshot.id]
    await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

    await registry.cancel(snapshot.id, "tenant-a")
    assert swallowed.is_set()
    assert runtime.snapshot.cancel_pending is True
    assert runtime.snapshot.state != LivePlanningJobState.CANCELLED
    assert runtime.operation_task is not None and not runtime.operation_task.done()

    # Red: the retry must fail closed, never terminalize or report reused
    # success while the operation is still running.
    with pytest.raises(LivePlanningJobCancellationPendingError):
        await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="retry-live",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
    assert runtime.snapshot.cancel_pending is True
    assert runtime.snapshot.state != LivePlanningJobState.CANCELLED

    # Once the operation truly stops, the same retry completes the cancel.
    stop.set()
    await asyncio.sleep(0.05)
    snapshot_again, replayed_again = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="retry-live",
        request_digest=REQUEST_SHA256,
        defer_start=False,
    )
    assert replayed_again is True
    assert snapshot_again.id == snapshot.id
    final = await registry.get(snapshot.id, "tenant-a")
    assert final is not None and final.state == LivePlanningJobState.CANCELLED


@pytest.mark.asyncio
async def test_deadline_timeout_publishes_pending_not_failed_over_live_operation(
    tmp_path: Path,
) -> None:
    """C-143 硬门 C counter-example: when the deadline expires and the real
    operation swallows CancelledError and keeps writing side effects, the job
    must NEVER publish FAILED/deadline_exceeded. It first persists a durable
    timeout/cancel-pending isolation, drains within the budget, and only
    publishes FAILED when the operation is confirmed stopped; otherwise it stays
    in a non-terminal cancel_pending isolation that a retry or close joins."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    snapshot, replayed = await registry.start_idempotent(
        tenant_id="tenant-a",
        operation=operation,
        idempotency_key="deadline-live",
        request_digest=REQUEST_SHA256,
        defer_start=False,
        deadline_seconds=0.3,
    )
    assert replayed is False
    runtime = registry._records[snapshot.id]
    await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)
    # Wait for the deadline to fire and the timeout handler to finish draining.
    await asyncio.sleep(0.6)

    # Red: FAILED must never be published while the operation is still alive.
    assert swallowed.is_set()
    assert runtime.snapshot.state != LivePlanningJobState.FAILED
    assert runtime.snapshot.cancel_pending is True
    assert runtime.operation_task is not None and not runtime.operation_task.done()
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    disk_record = next(
        record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
    )
    assert disk_record["snapshot"]["state"] != "failed"
    assert disk_record["snapshot"]["cancel_pending"] is True
    before = side_effects
    await asyncio.sleep(0.05)
    assert side_effects > before

    # Once the operation truly stops, a close() joins the cleanup and settles.
    # C-145 P0: the DURABLE deadline intent is FAILED/deadline_exceeded with the
    # safe-failure diagnostic — a later close() joins that outcome, never a
    # guessed CANCELLED label.
    stop.set()
    await asyncio.sleep(0.05)
    await registry.close()
    assert runtime.operation_task.done()
    final = await registry.get(snapshot.id, "tenant-a")
    assert final is not None and final.state == LivePlanningJobState.FAILED
    assert final.stage == "deadline_exceeded"
    assert final.safe_failure_code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_legacy_immediate_derivation_fails_closed_for_formal_prepared_shape(
    tmp_path: Path,
) -> None:
    """C-143 硬门 A counter-example: a v2 record shaped like a formal prepared
    job that activated successfully with operation_id=None (activation_operation
    =None, prepared=false, state=RUNNING) is NOT provably immediate. The old v2
    API allowed exactly this: a defer_start=true prepared job activated with no
    intent/operation_id and ran. Its mode must migrate to an explicit
    legacy_isolated binding, and a same-key immediate request must fail closed —
    never replay."""

    state_path = tmp_path / "live-jobs.json"
    tenant_partition = LivePlanningJobRegistry._tenant_partition("tenant-a")
    job_id = "live-job-formal-prepared-activated"

    snap = _v2_snapshot_model(
        job_id, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1
    ).model_dump(mode="json")
    payload = {
        "schema_version": "tripchord-live-job-registry-v2",
        "records": [
            {
                "tenant_partition": tenant_partition,
                "snapshot": snap,
                "prepared": False,
                "activation_operation": None,
            }
        ],
        "idempotency": [
            {
                "partition": LivePlanningJobRegistry._idempotency_partition(
                    "tenant-a", "formal-prepared-key"
                ),
                "job_id": job_id,
                "request_digest": REQUEST_SHA256,
            }
        ],
    }
    state_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    registry = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    disk = json.loads(state_path.read_text(encoding="utf-8"))
    entries = {entry["job_id"]: entry for entry in disk["idempotency"]}
    # Red: the legacy mode is NOT provably immediate — it must be isolated.
    assert entries[job_id]["legacy_isolated"] is True

    async def operation(_: Any) -> dict[str, Any]:
        return {"ok": True}

    for mode in (True, False):
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="formal-prepared-key",
                request_digest=REQUEST_SHA256,
                defer_start=mode,
            )
    await registry.close()


async def _settle_leaked_runtime(
    stop: asyncio.Event,
    runtime: Any,
) -> None:
    """Boundedly stop any executor a test left running (e.g. when a native-red
    assertion fails before the happy-path cleanup), so pytest-asyncio's teardown
    never waits forever on a swallow-cancel operation or a hung runner."""
    stop.set()
    if runtime is None:
        return
    operation_task = runtime.operation_task
    if operation_task is not None and not operation_task.done():
        operation_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(operation_task, timeout=3)
    task = runtime.task
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=3)


async def _hard_teardown_registry(
    registry: LivePlanningJobRegistry,
    stop: asyncio.Event,
    runtime: Any,
) -> None:
    """Hard-teardown a NEW-schema registry whose cleanup loops cannot complete
    because the store never recovered: cancel every background task (operation,
    runner, cleanup owner, reaper, watchdog) so no test leaves a pending task
    behind. Only the durable disk facts survive — exactly an unrecovered
    process restart."""
    stop.set()
    operation_task = runtime.operation_task
    if operation_task is not None and not operation_task.done():
        operation_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(operation_task, timeout=3)
    for task in (runtime.cleanup_owner, runtime.task):
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=3)
    for task in (registry._hard_stop_watchdog, registry._reaper_task):
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_close_first_intent_persist_failure_keeps_closed_flag_transactional(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143 P0: the close() lifecycle flag must be transactional with the
    durable cancel intent. A pre-commit failure on the FIRST close's intent
    persist must leave ``self._closed`` False (the registry is NOT closed) and
    the record still RUNNING with its executor untouched — never ``_closed=True``
    over a disk that still claims an active record without a durable owner. A
    second close re-persists the intent durably — carrying the CANCELLED/cancelled
    pending outcome in the SAME atomic write (C-145 P0 supplement) — and a
    final-persist failure after the executor stops keeps the recoverable
    cancel_pending isolation (never a RUNNING record over a dead executor); a
    same-key retry completes the terminalization and a full cold start reads the
    same DURABLE cancel facts (cancelled, never a guessed restart_cancelled)."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"stopped": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    runtime: Any = None
    try:
        snapshot, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="close-intent-precommit",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
        assert replayed is False
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # --- First close: fail the durable-intent persist pre-commit. ---
        real_persist = registry._persist_locked

        def fail_intent_persist() -> None:
            raise RuntimeError("injected close intent persist failure")

        monkeypatch.setattr(registry, "_persist_locked", fail_intent_persist)
        with pytest.raises(RuntimeError, match="injected close intent persist failure"):
            await registry.close()
        monkeypatch.undo()

        # RED: the lifecycle flag must NOT claim closed when the durable intent
        # never committed — the current HEAD leaves _closed=True (divergence).
        assert registry._closed is False
        # The record is untouched: still RUNNING in memory, executor alive, and
        # the failed close wrote NO cancel intent to disk.
        assert runtime.snapshot.state == LivePlanningJobState.RUNNING
        assert runtime.operation_task is not None and not runtime.operation_task.done()
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"]["cancel_pending"] is False
        assert disk_record["snapshot"]["stage"] != "closing"

        # --- Second close: intent persist succeeds; the FINAL terminalize fails
        # pre-commit after the executor is stopped. ---
        def fail_final_terminalize() -> None:
            if runtime.task is not None and runtime.task.done():
                raise RuntimeError("injected final close persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_final_terminalize)
        with pytest.raises(RuntimeError, match="injected final close persist failure"):
            await registry.close()
        monkeypatch.undo()

        # The durable cancel intent was persisted before the executor was
        # stopped, so the record is NOT an active claim over work without an
        # owner: memory == disk on the recoverable closing/cancel_pending
        # isolation — never the plain RUNNING snapshot over a dead executor.
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.cancellation_requested is True
        assert runtime.snapshot.stage == "closing"
        assert registry._closed is True
        disk2 = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record2 = next(
            record for record in disk2["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record2["snapshot"] == runtime.snapshot.model_dump(mode="json")
        assert disk_record2["snapshot"]["cancel_pending"] is True

        # The registry is durably closed, so a same-key retry in-process fails
        # closed — it never reports the RUNNING record as active over the dead
        # executor (and never reuses it).
        stop.set()
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="registry is closed"):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="close-intent-precommit",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )

        # A full cold start reads the closing/cancel_pending isolation and
        # fail-closes it to CANCELLED/cancelled — the DURABLE intent close()
        # committed carries the unambiguous cancel outcome, never a guessed
        # restart_cancelled label; a same-key retry there returns that terminal
        # fact — never a RUNNING record over a dead executor.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        cold = await reloaded.get(snapshot.id, "tenant-a")
        assert cold is not None
        assert cold.state == LivePlanningJobState.CANCELLED
        assert cold.stage == "cancelled"
        cold_retry, cold_retry_replayed = await reloaded.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="close-intent-precommit",
            request_digest=REQUEST_SHA256,
            defer_start=False,
        )
        assert cold_retry_replayed is True
        assert cold_retry.id == snapshot.id
        assert cold_retry.state == LivePlanningJobState.CANCELLED
        await reloaded.close()
        await registry.close()
    finally:
        await _settle_leaked_runtime(stop, runtime)


@pytest.mark.asyncio
async def test_deadline_timeout_isolation_persist_failure_never_abandons_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-143 P0: a pre-commit failure on the FIRST timeout/cancel-pending
    isolation persist must not leave an unowned live operation. The isolation is
    bounded-retried; the durable timeout_pending isolation is reached, the
    operation is drained, a same-key retry fails closed while the operation is
    alive, and a cold start terminalizes truthfully."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    runtime: Any = None
    try:
        snapshot, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="deadline-isolation-fail",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.5,
        )
        assert replayed is False
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        # Arm a fail-once persist: the FIRST timeout-isolation persist fails
        # pre-commit, the bounded retry then succeeds.
        real_persist = registry._persist_locked
        armed = False

        def fail_first_timeout_isolation() -> None:
            nonlocal armed
            loop = asyncio.get_running_loop()
            if (
                not armed
                and runtime.operation_task is not None
                and not runtime.operation_task.done()
                and loop.time() >= runtime.deadline_monotonic
            ):
                armed = True
                raise RuntimeError("injected timeout isolation persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_first_timeout_isolation)
        await asyncio.sleep(0.9)
        monkeypatch.undo()

        # The transient isolation failure was bounded-retried: the record reaches
        # a durable timeout/cancel-pending isolation, and the operation was
        # drained — never restored to RUNNING over a live operation.
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.stage == "timeout_pending"
        assert runtime.snapshot.state != LivePlanningJobState.FAILED
        assert runtime.operation_task is not None and not runtime.operation_task.done()
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"] == runtime.snapshot.model_dump(mode="json")
        assert disk_record["snapshot"]["cancel_pending"] is True
        assert disk_record["snapshot"]["stage"] == "timeout_pending"

        # Same-key retry fails closed while the operation is still alive.
        with pytest.raises(LivePlanningJobCancellationPendingError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="deadline-isolation-fail",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )

        # The operation truly stops; a close() joins the cleanup and settles.
        # C-145 P0: the DURABLE deadline intent is FAILED/deadline_exceeded —
        # the late close() joins that outcome, never a guessed CANCELLED label.
        stop.set()
        await asyncio.sleep(0.05)
        await registry.close()
        assert runtime.operation_task.done()
        final = await registry.get(snapshot.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        assert final.safe_failure_code == "deadline_exceeded"

        # A full cold start reads the terminal facts.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        cold = await reloaded.get(snapshot.id, "tenant-a")
        assert cold is not None and cold.state == LivePlanningJobState.FAILED
        assert cold.stage == "deadline_exceeded"
        assert cold.safe_failure_code == "deadline_exceeded"
        await reloaded.close()
        await registry.close()
    finally:
        await _settle_leaked_runtime(stop, runtime)


@pytest.mark.asyncio
async def test_deadline_timeout_isolation_permanent_persist_failure_keeps_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0-3: the FIRST durable deadline intent is the HARD PRECONDITION
    for stopping the executor. When every bounded pre-commit attempt fails, the
    runner must NOT restore the pre-timeout RUNNING snapshot and must NOT
    cancel/drain the live operation: the unique owner, the real operation and the
    capacity lease stay untouched, and the record enters the explicit
    ``deadline_intent_persist_pending`` retry state with the FAILED/deadline_exceeded
    intent held IN MEMORY. The same bounded cleanup owner auto-continues the
    persistence; once the intent commits, THEN the operation is stopped and drained
    and the record settles to the terminal FAILED/deadline_exceeded — never a
    guessed CANCELLED. A same-key retry fails closed in the uncommitted window."""

    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()
    side_effects = 0

    async def operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"ok": True}

    registry = LivePlanningJobRegistry(state_path=state_path, cancel_wait_seconds=0.15)
    runtime: Any = None
    try:
        snapshot, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="deadline-isolation-permanent",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.4,
        )
        assert replayed is False
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        # Every timeout-isolation persist fails pre-commit (permanent write
        # failure).
        real_persist = registry._persist_locked

        def fail_all_timeout_isolation() -> None:
            loop = asyncio.get_running_loop()
            if (
                runtime.operation_task is not None
                and not runtime.operation_task.done()
                and loop.time() >= runtime.deadline_monotonic
            ):
                raise RuntimeError("injected permanent timeout isolation persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_all_timeout_isolation)
        await asyncio.sleep(0.8)
        monkeypatch.undo()

        # C-146 P0-3: the runner exited (its task is done) but the operation is
        # NOT drained and the capacity lease is still held — the record keeps the
        # in-memory isolation and the FAILED/deadline_exceeded intent in the
        # explicit deadline_intent_persist_pending retry state, never the plain
        # pre-timeout RUNNING snapshot.
        assert runtime.task is not None and runtime.task.done()
        assert not swallowed.is_set()
        assert runtime.intent_persist_pending is True
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.cancellation_requested is True
        assert runtime.snapshot.stage == "deadline_intent_persist_pending"
        assert runtime.operation_task is not None and not runtime.operation_task.done()
        assert runtime.slot_held is True

        # Same-key retry fails closed while the FIRST intent is uncommitted.
        with pytest.raises(LivePlanningJobCancellationPendingError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="deadline-isolation-permanent",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )

        # Persist recovers: the same bounded cleanup owner re-commits the FIRST
        # FAILED intent, THEN stops and drains the operation (swallowed fires),
        # and keeps the durable intent for the terminal settle.
        for _ in range(200):
            if (
                runtime.intent_persist_pending is False
                and swallowed.is_set()
                and runtime.snapshot.stage == "timeout_pending"
                and not runtime.operation_task.done()
            ):
                break
            await asyncio.sleep(0.05)
        assert runtime.intent_persist_pending is False
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.stage == "timeout_pending"
        pending = runtime.pending_terminal
        assert pending is not None and pending.state == LivePlanningJobState.FAILED
        assert pending.stage == "deadline_exceeded"

        # Once the operation truly stops, close() settles the record and heals
        # the disk to the terminal FAILED/deadline_exceeded — the deadline's
        # durable intent (C-145 P0 supplement #2), never a guessed CANCELLED.
        stop.set()
        await asyncio.sleep(0.05)
        await registry.close()
        assert runtime.operation_task.done()
        final = await registry.get(snapshot.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"]["state"] == "failed"
        assert disk_record["snapshot"]["stage"] == "deadline_exceeded"

        # A full cold start reads the terminal facts.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        cold = await reloaded.get(snapshot.id, "tenant-a")
        assert cold is not None and cold.state == LivePlanningJobState.FAILED
        assert cold.stage == "deadline_exceeded"
        await reloaded.close()
        await registry.close()
    finally:
        await _settle_leaked_runtime(stop, runtime)


@pytest.mark.asyncio
async def test_deadline_first_intent_fail_holds_capacity_blocks_new_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0-3 counter-example: with the FIRST FAILED/deadline_exceeded intent
    uncommittable (every bounded pre-commit attempt fails), the unique owner, the
    real operation and the capacity lease all stay in place — the operation is
    NEVER cancelled/drained, the admission slot is NEVER released, and a NEW key
    request cannot start over the held capacity. RED on HEAD: the deadline handler
    drained the operation, so a new key could start over the still-live executor."""
    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    side_effects = 0

    async def stubborn(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        while not stop.is_set():
            try:
                side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"ok": True}

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        max_running=1,
        capacity=4,
        cancel_wait_seconds=0.05,
    )
    runtime: Any = None
    try:
        first, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="deadline-capacity-hold",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.4,
        )
        assert replayed is False
        runtime = registry._records[first.id]
        await _wait_for_state(registry, first.id, "tenant-a", LivePlanningJobState.RUNNING)

        real_persist = registry._persist_locked

        def fail_timeout_isolation() -> None:
            loop = asyncio.get_running_loop()
            if (
                runtime.operation_task is not None
                and not runtime.operation_task.done()
                and loop.time() >= runtime.deadline_monotonic
            ):
                raise RuntimeError("injected permanent timeout isolation persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_timeout_isolation)
        await asyncio.sleep(0.8)
        monkeypatch.undo()

        # The first job is in the intent-persist-pending retry state; the
        # operation is NOT drained and the admission slot is still held.
        assert runtime.task is not None and runtime.task.done()
        assert runtime.intent_persist_pending is True
        assert runtime.slot_held is True
        effects_while_holding = side_effects

        # A NEW key request must NOT start over the held capacity lease: the
        # second job stays QUEUED (never RUNNING) while the first holds the slot,
        # and the first operation is provably still alive (side effects advance).
        second, replayed_second = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="deadline-capacity-new-key",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=10,
        )
        assert replayed_second is False
        await asyncio.sleep(0.1)
        second_snapshot = await registry.get(second.id, "tenant-a")
        assert second_snapshot is not None
        assert second_snapshot.state == LivePlanningJobState.QUEUED
        assert side_effects > effects_while_holding

        # Cleanup: stop the first operation so the shared owner re-commits the
        # intent, drains and terminalizes FAILED; the second job then acquires
        # the released slot and succeeds.
        stop.set()
        for _ in range(300):
            first_final = await registry.get(first.id, "tenant-a")
            if first_final is not None and first_final.state == LivePlanningJobState.FAILED:
                break
            await asyncio.sleep(0.05)
        first_final = await registry.get(first.id, "tenant-a")
        assert first_final is not None and first_final.state == LivePlanningJobState.FAILED
        assert first_final.stage == "deadline_exceeded"
        await _wait_for_state(registry, second.id, "tenant-a", LivePlanningJobState.SUCCEEDED)
        await registry.close()
    finally:
        await _settle_leaked_runtime(stop, runtime)


@pytest.mark.asyncio
async def test_cold_boot_deadline_provenance_recovers_failed_never_restart_cancelled(
    tmp_path: Path,
) -> None:
    """C-146 P0-3/P0-4 counter-example: a force-exit in the deadline-intent window
    (the FIRST FAILED intent never committed) cold-boots from the durable
    unforgeable deadline provenance ONLY — an admitted record whose deadline
    provably passed recovers FAILED/deadline_exceeded with a consistent safe
    failure; an admitted record whose deadline did NOT pass gives no provable
    terminal outcome and is quarantined as ambiguous. A QUEUED non-prepared
    record is NOT provably never-admitted (its operation may already have been
    executing), so it is never guessed to restart_cancelled either — it is
    quarantined as ambiguous. restart_cancelled is NEVER fabricated for an
    admitted record, and two consecutive cold boots observe identical facts."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_expired = "live-job-deadline-expired"
    job_future = "live-job-deadline-future"
    job_queued = "live-job-queued-never-admitted"

    snap_expired = _v3_snapshot(
        job_expired, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1
    ).model_copy(update={"deadline_at": datetime.now(UTC) - timedelta(minutes=1)})
    snap_future = _v3_snapshot(
        job_future, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1
    )
    snap_queued = _v3_snapshot(job_queued, LivePlanningJobState.QUEUED, "queued", 0, 1)

    partition = LivePlanningJobRegistry._tenant_partition(tenant_id)
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": partition,
                "snapshot": snap_expired.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
            {
                "tenant_partition": partition,
                "snapshot": snap_future.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
            {
                "tenant_partition": partition,
                "snapshot": snap_queued.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
        ],
        "idempotency": [
            _v3_idempotency_entry(tenant_id, "deadline-expired", job_expired),
            _v3_idempotency_entry(tenant_id, "deadline-future", job_future),
            _v3_idempotency_entry(tenant_id, "deadline-queued", job_queued),
        ],
    }
    _write_registry_state(payload, state_path)

    async def verify(registry: LivePlanningJobRegistry) -> None:
        expired = await registry.get(job_expired, tenant_id)
        assert expired is not None and expired.state == LivePlanningJobState.FAILED
        assert expired.stage == "deadline_exceeded"
        assert expired.error == "TimeoutError: live planning job deadline exceeded"
        assert expired.safe_failure_code == "deadline_exceeded"
        assert expired.safe_failure_details is not None
        assert expired.safe_failure_details.exception_class == "TimeoutError"
        assert expired.safe_failure_details_digest is not None
        future = await registry.get(job_future, tenant_id)
        assert future is not None
        assert future.state == LivePlanningJobState.RUNNING
        assert future.stage == "isolated_ambiguous_cancel"
        queued = await registry.get(job_queued, tenant_id)
        assert queued is not None and queued.state == LivePlanningJobState.QUEUED
        assert queued.stage == "isolated_ambiguous_cancel"
        assert queued.cancel_pending is False

    first = LivePlanningJobRegistry(state_path=state_path)
    try:
        await verify(first)
    finally:
        await first.close()

    # A second consecutive cold boot observes the SAME facts — no drift.
    second = LivePlanningJobRegistry(state_path=state_path)
    try:
        await verify(second)
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_deadline_intent_pending_cancel_and_close_never_bypass_ordering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0-3 counter-example: while the FIRST FAILED intent is uncommitted
    (deadline_intent_persist_pending), an explicit cancel() returns the observable
    retry-state snapshot unchanged — it neither drains the executor nor guesses
    CANCELLED — and a close() joins the ordering by committing/preserving the
    FAILED intent (first intent wins) instead of overwriting it with CANCELLED.
    Once the operation truly stops, the record settles to FAILED/deadline_exceeded,
    never CANCELLED, and the cold start reads the same durable facts."""
    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    swallowed = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"ok": True}

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        cancel_wait_seconds=0.05,
    )
    runtime: Any = None
    try:
        snapshot, _ = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="deadline-intent-pending-joins",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.4,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        real_persist = registry._persist_locked

        def fail_timeout_isolation() -> None:
            loop = asyncio.get_running_loop()
            if (
                runtime.operation_task is not None
                and not runtime.operation_task.done()
                and loop.time() >= runtime.deadline_monotonic
            ):
                raise RuntimeError("injected permanent timeout isolation persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_timeout_isolation)
        await asyncio.sleep(0.8)

        assert runtime.intent_persist_pending is True
        assert not swallowed.is_set()
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # An explicit cancel during the uncommitted window joins nothing and
        # guesses nothing: it returns the observable retry-state snapshot. The
        # persist is still failing, so the intent stays uncommitted.
        cancelled = await registry.cancel(snapshot.id, "tenant-a")
        assert cancelled is not None
        assert cancelled.stage == "deadline_intent_persist_pending"
        assert runtime.intent_persist_pending is True
        assert not swallowed.is_set()
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # The write failure is only temporary: from here on the shared owner
        # re-commits the FIRST intent, then stops and drains the executor.
        monkeypatch.undo()

        # Stop the operation for real, then close(): close() joins the ordering
        # (committing the FAILED intent, first intent wins) and the record
        # settles to FAILED/deadline_exceeded, never a guessed CANCELLED.
        stop.set()
        for _ in range(200):
            if runtime.operation_task is not None and runtime.operation_task.done():
                break
            await asyncio.sleep(0.05)
        assert runtime.operation_task is not None and runtime.operation_task.done()
        await registry.close()
        final = await registry.get(snapshot.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"]["state"] == "failed"
        assert disk_record["snapshot"]["stage"] == "deadline_exceeded"
        assert disk_record["snapshot"]["safe_failure_code"] == "deadline_exceeded"

        # A full cold start reads the same durable FAILED facts.
        reloaded = LivePlanningJobRegistry(state_path=state_path)
        try:
            cold = await reloaded.get(snapshot.id, "tenant-a")
            assert cold is not None and cold.state == LivePlanningJobState.FAILED
            assert cold.stage == "deadline_exceeded"
        finally:
            await reloaded.close()
    finally:
        await _settle_leaked_runtime(stop, runtime)


@pytest.mark.asyncio
async def test_capacity_slot_held_until_real_operation_stops_after_deadline() -> None:
    """C-143 P0-1: the admission permit must be released only when the REAL
    operation task is done — never by the runner's finally while a stubborn
    operation is still alive and writing side effects. max_running=1: the first
    job dies by deadline (parent done, operation alive) and a NEW key request
    must NOT start until the first operation truly stops and the permit is
    confirmed released."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    first_stop = asyncio.Event()
    second_stop = asyncio.Event()
    first_started = asyncio.Event()
    first_side_effects = 0
    second_started = asyncio.Event()
    second_side_effects = 0

    async def first_operation(_: Any) -> dict[str, Any]:
        nonlocal first_side_effects
        first_started.set()
        while not first_stop.is_set():
            try:
                first_side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"stopped": True}

    async def second_operation(_: Any) -> dict[str, Any]:
        nonlocal second_side_effects
        second_started.set()
        while not second_stop.is_set():
            try:
                second_side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"stopped": True}

    first_runtime: Any = None
    second_runtime: Any = None
    try:
        first, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=first_operation,
            idempotency_key="deadline-first-key",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        first_runtime = registry._records[first.id]
        for _ in range(1000):
            if first_started.is_set():
                break
            await asyncio.sleep(0)
        assert first_started.is_set()
        assert first_side_effects > 0
        # The runner dies by deadline; the real operation stays alive.
        await asyncio.wait_for(first_runtime.task, timeout=5)
        assert first_runtime.task.done()
        assert first_runtime.operation_task is not None and not first_runtime.operation_task.done()
        assert first_runtime.snapshot.cancel_pending is True
        assert first_runtime.snapshot.stage == "timeout_pending"

        # A NEW key must NOT start while the first operation is alive.
        second, _replayed2 = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=second_operation,
            idempotency_key="deadline-second-key",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        second_runtime = registry._records[second.id]
        # RED on HEAD: the first runner's finally already released the permit,
        # so the second operation starts immediately (real concurrency 2).
        for _ in range(100):
            if second_started.is_set():
                break
            await asyncio.sleep(0)
        assert not second_started.is_set()
        assert second_side_effects == 0

        alive = [
            r
            for r in registry._records.values()
            if r.operation_task is not None and not r.operation_task.done()
        ]
        assert len(alive) == 1

        # Once the first operation truly stops, the permit is released and the
        # second operation may start (its own stop signal is separate, so it
        # keeps running until the test settles it).
        first_stop.set()
        await asyncio.wait_for(first_runtime.operation_task, timeout=3)
        for _ in range(2000):
            if second_started.is_set():
                break
            await asyncio.sleep(0)
        assert second_started.is_set()
        assert second_side_effects > 0
    finally:
        first_stop.set()
        second_stop.set()
        await _settle_leaked_runtime(first_stop, first_runtime)
        await _settle_leaked_runtime(second_stop, second_runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_capacity_slot_held_until_real_operation_stops_after_cancel() -> None:
    """C-143 P0-1 cancel variant: same admission binding after a cancel() that
    leaves the operation alive (runner done, cancel_pending). A NEW key request
    must NOT start until the first operation truly stops."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    first_stop = asyncio.Event()
    second_stop = asyncio.Event()
    first_started = asyncio.Event()
    first_side_effects = 0
    second_started = asyncio.Event()
    second_side_effects = 0

    async def first_operation(_: Any) -> dict[str, Any]:
        nonlocal first_side_effects
        first_started.set()
        while not first_stop.is_set():
            try:
                first_side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"stopped": True}

    async def second_operation(_: Any) -> dict[str, Any]:
        nonlocal second_side_effects
        second_started.set()
        while not second_stop.is_set():
            try:
                second_side_effects += 1
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                pass
        return {"stopped": True}

    first_runtime: Any = None
    second_runtime: Any = None
    try:
        first, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=first_operation,
            idempotency_key="cancel-first-key",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        first_runtime = registry._records[first.id]
        for _ in range(1000):
            if first_started.is_set():
                break
            await asyncio.sleep(0)
        assert first_started.is_set()

        cancelled = await registry.cancel(first.id, "tenant-a")
        assert cancelled is not None and cancelled.cancel_pending is True
        # cancel() stops the runner; the stubborn operation stays alive.
        assert first_runtime.task.done()
        assert first_runtime.operation_task is not None and not first_runtime.operation_task.done()

        # A NEW key must NOT start while the first operation is alive.
        second, _replayed2 = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=second_operation,
            idempotency_key="cancel-second-key",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        second_runtime = registry._records[second.id]
        # RED on HEAD: the cancelled runner's finally already released the
        # permit, so the second operation starts immediately (real concurrency 2).
        for _ in range(100):
            if second_started.is_set():
                break
            await asyncio.sleep(0)
        assert not second_started.is_set()
        assert second_side_effects == 0

        alive = [
            r
            for r in registry._records.values()
            if r.operation_task is not None and not r.operation_task.done()
        ]
        assert len(alive) == 1

        first_stop.set()
        await asyncio.wait_for(first_runtime.operation_task, timeout=3)
        for _ in range(2000):
            if second_started.is_set():
                break
            await asyncio.sleep(0)
        assert second_started.is_set()
        assert second_side_effects > 0
    finally:
        first_stop.set()
        second_stop.set()
        await _settle_leaked_runtime(first_stop, first_runtime)
        await _settle_leaked_runtime(second_stop, second_runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_repeated_deadline_attacks_never_exceed_capacity() -> None:
    """C-143 P0-1 repeated-attack proof: a burst of deadline deaths must never
    let the number of simultaneously-alive real operations exceed max_running,
    and the admission permit must be conserved (held by the cleanup owner until
    the operation truly stops) — no orphan permit leak that accumulates 1..N."""
    registry = LivePlanningJobRegistry(
        capacity=32,
        max_running=1,
        cancel_wait_seconds=0.02,
    )
    stop = asyncio.Event()

    async def stubborn_operation(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.001)
        return {"stopped": True}

    runtimes: list[Any] = []
    try:
        alive_counts: list[int] = []
        for attack in range(5):
            snap, _replayed = await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=stubborn_operation,
                idempotency_key=f"attack-{attack}",
                request_digest=REQUEST_SHA256,
                deadline_seconds=0.02,
            )
            runtime = registry._records[snap.id]
            runtimes.append(runtime)
            await asyncio.wait_for(runtime.task, timeout=5)
            alive = [
                r
                for r in registry._records.values()
                if r.operation_task is not None and not r.operation_task.done()
            ]
            alive_counts.append(len(alive))
        # RED on HEAD: every runner's finally frees the permit, so each attack
        # starts another live operation and the count grows 1..N. After the fix
        # only the first operation holds the permit; later runners die queued.
        assert max(alive_counts) == 1
    finally:
        for runtime in runtimes:
            await _settle_leaked_runtime(stop, runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_same_key_retry_while_operation_alive_raises_pending_with_job_id() -> None:
    """C-143 P0-2: the same-key retry while the operation swallows the cancel
    must fail closed with a stable error that carries the original job identity
    (so the HTTP layer can map it to a queryable/retryable conflict instead of a
    bare 500)."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()

    async def operation(_: Any) -> dict[str, Any]:
        started.set()
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=operation,
            idempotency_key="retry-while-pending",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        cancelled = await registry.cancel(snap.id, "tenant-a")
        assert cancelled is not None and cancelled.cancel_pending is True
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        with pytest.raises(LivePlanningJobCancellationPendingError) as excinfo:
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=operation,
                idempotency_key="retry-while-pending",
                request_digest=REQUEST_SHA256,
                deadline_seconds=30,
            )
        # RED on HEAD: the raised error carries no job_id (the HTTP layer cannot
        # map it, so the same-key retry surfaces as a bare 500).
        assert excinfo.value.job_id == snap.id
    finally:
        await _settle_leaked_runtime(stop, runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_capacity_slot_held_until_real_operation_stops_after_close() -> None:
    """C-143 P0-1 close path: close() must never release the admission permit
    while the real operation is still alive — the permit is held by the cleanup
    owner until the operation truly stops, so a later new-key start (after a
    clean restart) never finds a leaked permit."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.02,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    side_effects = 0

    async def stubborn_operation(_: Any) -> dict[str, Any]:
        nonlocal side_effects
        started.set()
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                side_effects += 1
                await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn_operation,
            idempotency_key="close-slot-key",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()

        # close() with a stubborn operation: the permit must remain held
        # (slot_held stays True) because the operation is still alive after the
        # bounded drain — no close entry may release early.
        await registry.close()
        assert runtime.slot_held is True
        assert runtime.operation_task is not None and not runtime.operation_task.done()
        assert runtime.snapshot.cancel_pending is True

        # Once the operation truly stops, the done-callback releases the permit.
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        assert runtime.slot_held is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await registry.close()


async def _wait_for_terminal_state(
    registry: LivePlanningJobRegistry,
    job_id: str,
    tenant_id: str,
    state: LivePlanningJobState,
) -> None:
    """Poll for an AUTO-COLLECTED terminal state (C-145 P1).

    A longer window than ``_wait_for_state`` because the terminalization is now
    driven by the async cleanup owner that must wake, re-check the executors and
    persist, not by a synchronous registry call."""
    for _ in range(1000):
        snapshot = await registry.get(job_id, tenant_id)
        if snapshot is not None and snapshot.state == state:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"job did not reach terminal {state}")


async def _settle_cleanup_owner(runtime: Any) -> None:
    owner = getattr(runtime, "cleanup_owner", None)
    if owner is not None and not owner.done():
        with suppress(BaseException):
            await asyncio.wait_for(owner, timeout=3)


@pytest.mark.asyncio
async def test_late_stop_cancel_auto_collects_terminal_without_extra_cancel() -> None:
    """C-145 P1: after cancel() fails closed over a swallow-cancel operation that
    keeps running past the drain budget, the operation eventually stops on its
    own. The registry's cleanup owner (joined by the operation done-callback)
    must AUTO-COLLECT the record to the terminal CANCELLED state — WITHOUT a
    repeated cancel, same-key retry, close or cold start. RED on HEAD: the
    snapshot stays running+cancel_pending (stage=cancel_timed_out) forever."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-cancel",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert swallowed.is_set()
        assert outcome is not None and outcome.cancel_pending is True
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # The operation stops on its own — no extra cancel / retry / close.
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        final = await registry.get(snap.id, "tenant-a")
        # RED on HEAD: the snapshot never reaches CANCELLED without another cancel.
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.stage == "cancelled"
        assert final.cancel_pending is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_deadline_auto_collects_terminal_without_extra_close() -> None:
    """C-145 P0 deadline variant: the deadline fires, the operation swallows the
    drain cancellation and keeps running (timeout_pending), then stops on its
    own. The cleanup owner must auto-collect the record to the DURABLE deadline
    outcome — FAILED/deadline_exceeded with the safe-failure diagnostic — without
    a repeated close / retry. RED on ccc378b: the snapshot stays
    running+cancel_pending (stage=timeout_pending) forever; even when a
    cancel/close joined it landed on CANCELLED, losing the deadline semantics."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        runtime = registry._records[snap.id]
        await asyncio.wait_for(runtime.task, timeout=5)
        assert runtime.task.done()
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.stage == "timeout_pending"
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # The operation stops on its own — no repeated close / retry / cold start.
        # C-145 P0: the durable deadline intent (FAILED/deadline_exceeded) is what
        # the cleanup owner consumes — never a guessed CANCELLED label.
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(registry, snap.id, "tenant-a", LivePlanningJobState.FAILED)
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        assert final.safe_failure_code == "deadline_exceeded"
        assert final.cancel_pending is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_close_auto_collects_terminal_without_extra_close() -> None:
    """C-145 P1 close variant: close() fails closed over a swallow-cancel
    operation (non-terminal cancel_pending isolation), then the operation stops
    on its own. The cleanup owner must auto-collect the record to the terminal
    CANCELLED state without a repeated close(). RED on HEAD: the snapshot stays
    running+cancel_pending forever."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        while not stop.is_set():
            try:
                await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                swallowed.set()
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-close",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        await _wait_for_state(registry, snap.id, "tenant-a", LivePlanningJobState.RUNNING)
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        await registry.close()
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        # The operation stops on its own — no repeated close().
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.cancel_pending is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_auto_collect_memory_matches_disk(tmp_path: Path) -> None:
    """C-145 P1: the auto-collected terminal state must be PERSISTED — memory and
    disk agree byte-for-byte (same committed snapshot), so a same-process reader
    and a cold restart observe the same terminal facts. RED on HEAD: memory stays
    running+cancel_pending and the disk keeps the non-terminal isolation."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-disk",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not runtime.operation_task.done()

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        disk_payload = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk_payload["records"] if record["snapshot"]["id"] == snap.id
        )
        # RED on HEAD: memory and disk both stay running+cancel_pending.
        assert disk_record["snapshot"] == final.model_dump(mode="json")
        assert disk_record["snapshot"]["state"] == "cancelled"
        assert disk_record["snapshot"]["cancel_pending"] is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_capacity_one_new_key_starts_after_operation_stops() -> None:
    """C-145 P1: with capacity=1, a stuck cancel_pending record must NOT leak
    capacity forever. Once the operation stops and the record auto-collects to a
    terminal state, a NEW key request can start (the terminal record is
    evictable). RED on HEAD: the stuck non-terminal record occupies the only slot
    and the new key is rejected with LivePlanningJobCapacityError."""
    registry = LivePlanningJobRegistry(
        capacity=1,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    first_stop = asyncio.Event()
    second_stop = asyncio.Event()
    first_started = asyncio.Event()

    async def first_operation(_: Any) -> dict[str, Any]:
        first_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not first_stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    async def second_operation(_: Any) -> dict[str, Any]:
        await second_stop.wait()
        return {"ok": True}

    first_runtime: Any = None
    second_runtime: Any = None
    try:
        first, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=first_operation,
            idempotency_key="cap-one-first",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        first_runtime = registry._records[first.id]
        for _ in range(1000):
            if first_started.is_set():
                break
            await asyncio.sleep(0)
        assert first_started.is_set()
        outcome = await registry.cancel(first.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not first_runtime.operation_task.done()

        first_stop.set()
        await asyncio.wait_for(first_runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, first.id, "tenant-a", LivePlanningJobState.CANCELLED
        )

        # The terminal record is evictable: a NEW key can start (RED on HEAD: the
        # stuck non-terminal record makes this raise LivePlanningJobCapacityError).
        second, _replayed2 = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=second_operation,
            idempotency_key="cap-one-second",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        second_runtime = registry._records[second.id]
        assert second.id != first.id
        assert len(registry._records) == 1
    finally:
        first_stop.set()
        second_stop.set()
        await _settle_leaked_runtime(first_stop, first_runtime)
        await _settle_cleanup_owner(first_runtime)
        await _settle_leaked_runtime(second_stop, second_runtime)
        await _settle_cleanup_owner(second_runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_cumulative_attacks_never_exhaust_capacity() -> None:
    """C-145 P1: repeated late-stop attacks must never leak capacity. With
    capacity=1, every attack auto-collects to a terminal state when its operation
    stops, so the next NEW key can always start — the permanent leak that rejects
    new keys with LivePlanningJobCapacityError never accumulates. RED on HEAD:
    each stuck record occupies the only slot forever, so the second attack's new
    key is rejected."""
    registry = LivePlanningJobRegistry(
        capacity=1,
        max_running=1,
        cancel_wait_seconds=0.02,
    )
    stop_events: list[asyncio.Event] = []
    started_events: list[asyncio.Event] = []
    runtimes: list[Any] = []

    def make_operation(stop_event: asyncio.Event, started_event: asyncio.Event) -> Any:
        async def stubborn(_: Any) -> dict[str, Any]:
            started_event.set()
            while not stop_event.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
            return {"stopped": True}

        return stubborn

    try:
        for attack in range(5):
            stop_event = asyncio.Event()
            started_event = asyncio.Event()
            stop_events.append(stop_event)
            started_events.append(started_event)
            snap, _replayed = await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=make_operation(stop_event, started_event),
                idempotency_key=f"late-stop-attack-{attack}",
                request_digest=REQUEST_SHA256,
                deadline_seconds=30,
            )
            runtime = registry._records[snap.id]
            runtimes.append(runtime)
            for _ in range(1000):
                if started_event.is_set():
                    break
                await asyncio.sleep(0)
            assert started_event.is_set()
            outcome = await registry.cancel(snap.id, "tenant-a")
            assert outcome is not None and outcome.cancel_pending is True
            assert not runtime.operation_task.done()
            # Release this attack's operation; it auto-collects to terminal.
            stop_event.set()
            await asyncio.wait_for(runtime.operation_task, timeout=3)
            await _wait_for_terminal_state(
                registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
            )
        # Every new key across all attacks started successfully — capacity never
        # leaked a permanent non-terminal record.
        assert len(runtimes) == 5
    finally:
        for stop_event in stop_events:
            stop_event.set()
        for runtime in runtimes:
            await _settle_leaked_runtime(stop_events[-1], runtime)
            await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_cold_restart_matches_in_process_auto_collect(
    tmp_path: Path,
) -> None:
    """C-145 P1: the auto-collected terminal state must survive a full cold
    restart identically — the in-process reader and a fresh registry loading the
    same state file observe the SAME terminal state and stage. RED on HEAD: the
    in-process snapshot stays running+cancel_pending while a cold restart
    fail-closes it to restart_cancelled — the two disagree."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    reloaded: LivePlanningJobRegistry | None = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-cold",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not runtime.operation_task.done()

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        in_process = await registry.get(snap.id, "tenant-a")
        assert in_process is not None
        assert in_process.state == LivePlanningJobState.CANCELLED

        reloaded = LivePlanningJobRegistry(state_path=state_path)
        cold = await reloaded.get(snap.id, "tenant-a")
        assert cold is not None
        # RED on HEAD: the cold restart publishes restart_cancelled while the
        # in-process record is still running+cancel_pending.
        assert cold.state == in_process.state
        assert cold.stage == in_process.stage
        assert cold.cancel_pending is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        if reloaded is not None:
            await reloaded.close()
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_auto_collect_no_duplicate_terminalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P1: the auto-collect must terminalize exactly ONCE — the cleanup
    owner and the operation done-callback join the same state machine and never
    double-publish the final label. RED on HEAD: the record never auto-collects
    at all (the terminal state is never reached)."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-no-dup",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not runtime.operation_task.done()

        terminalize_calls = 0
        original = registry._terminalize_locked

        def counting(*args: Any, **kwargs: Any) -> None:
            nonlocal terminalize_calls
            terminalize_calls += 1
            original(*args, **kwargs)

        monkeypatch.setattr(registry, "_terminalize_locked", counting)

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        # Exactly one terminalize for the auto-collect; the state machine never
        # double-publishes the final CANCELLED.
        assert terminalize_calls == 1
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_late_stop_cleanup_owner_is_waitable_and_terminalizes() -> None:
    """C-145 P1: the registry holds a UNIQUE, waitable cleanup owner per pending
    runtime. A caller can await it and observe the auto-collected terminal state
    after the operation stops — no extra cancel / retry / close. RED on HEAD: no
    cleanup owner exists and the snapshot never reaches CANCELLED."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn,
            idempotency_key="late-stop-owner",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not runtime.operation_task.done()
        # The unique cleanup owner exists and is waitable (RED on HEAD: None).
        for _ in range(100):
            if getattr(runtime, "cleanup_owner", None) is not None:
                break
            await asyncio.sleep(0)
        assert runtime.cleanup_owner is not None
        stop.set()
        await asyncio.wait_for(runtime.cleanup_owner, timeout=3)
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.cancel_pending is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


# =========================================================================
# C-145 P0 counterexamples — RETURN f598b350 (2026-08-15).
#
# Gap 1 (terminal-persist failure was permanently fatal): a single pre-commit
# failure in the cleanup owner's terminal persist permanently orphaned the
# record — no auto-event re-ensured the owner, so capacity leaked forever and a
# new key was rejected with LivePlanningJobCapacityError.
# Gap 2 (deadline outcome was hardcoded): the deadline timeout reused the cancel
# path, so a late stop landed on CANCELLED, losing the true
# FAILED/deadline_exceeded + safe_failure semantics.
#
# GREEN here means: the owner retries within a bounded per-round budget; budget
# exhaustion keeps a DURABLE retry intent and the single registry reaper
# auto-restarts the owner with NO external API/close/retry until the terminal
# commit succeeds or the process shuts down; the durable deadline outcome is
# FAILED/deadline_exceeded (cancel/close stay CANCELLED); pre-commit AND
# post-commit failures both reconcile idempotently; cold starts continue the
# durable retry intent with no outcome drift. RED on ccc378b: every
# auto-retry / reaper / FAILED-deadline assertion below fails.
# =========================================================================


def _make_stubborn_swallow_cancel(
    stop: asyncio.Event,
    started: asyncio.Event,
    swallowed: asyncio.Event,
):
    """Operation that starts, swallows the drain cancellation, and keeps running
    until ``stop`` is set — the shared late-stop adversary."""

    async def stubborn(_: Any) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()
            while not stop.is_set():
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.001)
        return {"stopped": True}

    return stubborn


async def _settle_reaper(registry: LivePlanningJobRegistry) -> None:
    """Boundedly await the registry reaper so a test never leaves it sleeping at
    teardown (it self-terminates once no retry remains)."""
    reaper = getattr(registry, "_reaper_task", None)
    if reaper is not None and not reaper.done():
        with suppress(BaseException):
            await asyncio.wait_for(reaper, timeout=3)


@pytest.mark.asyncio
async def test_cleanup_owner_retries_terminal_persist_fail_once_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P0 (a) cancel: the cleanup owner's terminal persist fails ONCE
    pre-commit; the owner retries within its bounded per-round budget and
    auto-collects to the DURABLE CANCELLED outcome — parent and operation both
    done, pending cleared, slot recovered, no reaper ever needed. RED on
    ccc378b: the single failure permanently orphaned the record."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="owner-fail-once-cancel",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not runtime.operation_task.done()
        assert runtime.pending_terminal is not None
        assert runtime.pending_terminal.state == LivePlanningJobState.CANCELLED
        assert runtime.pending_terminal.stage == "cancelled"

        real_persist = registry._persist_locked
        persist_calls = 0

        def fail_first_persist() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise RuntimeError("injected terminal persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_first_persist)

        # The operation stops on its own; the owner retries and collects the
        # DURABLE CANCELLED outcome — no extra cancel / retry / close.
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        await _settle_cleanup_owner(runtime)
        monkeypatch.undo()

        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.stage == "cancelled"
        assert final.cancel_pending is False
        assert runtime.pending_terminal is None
        assert runtime.cleanup_owner is not None and runtime.cleanup_owner.done()
        assert runtime.cleanup_retry_round == 0
        assert registry._reaper_task is None
        assert runtime.slot_held is False
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_owner_retries_terminal_persist_fail_once_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 (a) deadline: same fail-once pre-commit retry, but the DURABLE
    intent is FAILED/deadline_exceeded with the safe-failure diagnostic — never
    a guessed CANCELLED label. The auto-collected terminal state equals the
    durable pending outcome and matches the disk (memory=disk)."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="owner-fail-once-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.5,
        )
        runtime = registry._records[snap.id]
        # Ensure the stubborn operation has entered its cancellation-swallowing
        # body before the deadline cleanup can race it on a loaded CI runner.
        await asyncio.wait_for(started.wait(), timeout=3)
        await asyncio.wait_for(runtime.task, timeout=5)
        assert runtime.task.done()
        assert swallowed.is_set()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.stage == "timeout_pending"
        assert runtime.pending_terminal is not None
        assert runtime.pending_terminal.state == LivePlanningJobState.FAILED
        assert runtime.pending_terminal.stage == "deadline_exceeded"
        assert runtime.pending_terminal.error == "TimeoutError: live planning job deadline exceeded"
        assert runtime.pending_terminal.safe_failure is not None
        assert runtime.pending_terminal.safe_failure.code.value == "deadline_exceeded"
        assert runtime.operation_task is not None and not runtime.operation_task.done()

        real_persist = registry._persist_locked
        persist_calls = 0

        def fail_first_persist() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise RuntimeError("injected terminal persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_first_persist)

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(registry, snap.id, "tenant-a", LivePlanningJobState.FAILED)
        await _settle_cleanup_owner(runtime)
        monkeypatch.undo()

        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        assert final.safe_failure_code == "deadline_exceeded"
        assert final.cancel_pending is False
        assert runtime.pending_terminal is None
        assert runtime.cleanup_owner is not None and runtime.cleanup_owner.done()
        assert runtime.cleanup_retry_round == 0
        assert registry._reaper_task is None
        assert runtime.slot_held is False

        # memory == disk: the retried terminal commit wrote the same FAILED state
        # and consumed the pending outcome.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snap.id
        )
        assert disk_record["snapshot"]["state"] == "failed"
        assert disk_record["snapshot"]["stage"] == "deadline_exceeded"
        assert disk_record["snapshot"]["safe_failure_code"] == "deadline_exceeded"
        assert disk_record["pending_terminal"] is None
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_reaper_recollects_cancel_after_budget_exhausted_no_external_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P0 (b) cancel: consecutive terminal-persist failures EXCEED the
    single-round budget. The owner keeps the DURABLE retry intent and arms the
    single registry reaper, which auto-restarts the owner after a bounded
    backoff — the record collects to CANCELLED with NO external API / close /
    retry. With capacity=1, a new key is rejected while the record is pending
    and only starts AFTER the auto-collection. The reaper self-terminates once
    no retry remains. RED on ccc378b: no owner survives the first failure and
    the record stays running+cancel_pending forever (capacity leaks)."""
    registry = LivePlanningJobRegistry(
        capacity=1,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def quick_op(_: Any) -> dict[str, Any]:
        return {"ok": True}

    first_runtime: Any = None
    second_runtime: Any = None
    try:
        first, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="reaper-cancel-first",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        first_runtime = registry._records[first.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(first.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert not first_runtime.operation_task.done()
        assert first_runtime.pending_terminal is not None

        budget = registry._cancel_isolation_persist_attempts
        real_persist = registry._persist_locked
        persist_calls = 0

        def fail_past_budget() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls <= budget + 1:
                raise RuntimeError("injected terminal persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_past_budget)

        # While the record is still cancel_pending and the operation is alive,
        # the single slot is occupied: a new key is rejected (capacity=1).
        with pytest.raises(LivePlanningJobCapacityError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=quick_op,
                idempotency_key="reaper-cancel-new-during",
                request_digest=REQUEST_SHA256,
                deadline_seconds=30,
            )

        # The operation stops on its own. The first owner's whole budget fails;
        # the reaper re-spawns it and the recovered store accepts the terminal
        # commit — no extra cancel / close / retry.
        stop.set()
        await asyncio.wait_for(first_runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, first.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        await _settle_cleanup_owner(first_runtime)
        await _settle_reaper(registry)
        monkeypatch.undo()

        final = await registry.get(first.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.stage == "cancelled"
        assert final.cancel_pending is False
        assert first_runtime.pending_terminal is None
        assert first_runtime.cleanup_retry_round >= 1
        assert first_runtime.cleanup_owner is not None and first_runtime.cleanup_owner.done()
        assert registry._reaper_task is None
        assert first_runtime.slot_held is False

        # Capacity is fully recovered ONLY after the auto-collection: a new key
        # now starts (the terminal record is evictable).
        second, _replayed2 = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=quick_op,
            idempotency_key="reaper-cancel-second",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        second_runtime = registry._records[second.id]
        assert second.id != first.id
        assert len(registry._records) == 1
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, first_runtime)
        await _settle_cleanup_owner(first_runtime)
        await _settle_reaper(registry)
        await _settle_leaked_runtime(stop, second_runtime)
        await _settle_cleanup_owner(second_runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_reaper_recollects_deadline_after_budget_exhausted_no_external_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P0 (b) deadline: consecutive failures exceed the single-round budget;
    the reaper auto-restarts the exhausted owner and the recovered store accepts
    the DURABLE FAILED/deadline_exceeded commit — with NO external API/close/
    retry. A same-key retry after the auto-collection replays the terminal FAILED
    facts. RED on ccc378b: the record stays running+cancel_pending forever and
    would land on a guessed CANCELLED label."""
    registry = LivePlanningJobRegistry(
        capacity=1,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    async def quick_op(_: Any) -> dict[str, Any]:
        return {"ok": True}

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="reaper-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        runtime = registry._records[snap.id]
        await asyncio.wait_for(runtime.task, timeout=5)
        assert runtime.task.done()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.stage == "timeout_pending"
        assert runtime.pending_terminal is not None
        assert runtime.pending_terminal.state == LivePlanningJobState.FAILED
        assert runtime.pending_terminal.stage == "deadline_exceeded"
        assert not runtime.operation_task.done()

        budget = registry._cancel_isolation_persist_attempts
        real_persist = registry._persist_locked
        persist_calls = 0

        def fail_past_budget() -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls <= budget + 1:
                raise RuntimeError("injected terminal persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", fail_past_budget)

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(registry, snap.id, "tenant-a", LivePlanningJobState.FAILED)
        await _settle_cleanup_owner(runtime)
        await _settle_reaper(registry)
        monkeypatch.undo()

        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        assert final.safe_failure_code == "deadline_exceeded"
        assert final.cancel_pending is False
        assert runtime.pending_terminal is None
        assert runtime.cleanup_retry_round >= 1
        assert runtime.cleanup_owner is not None and runtime.cleanup_owner.done()
        assert registry._reaper_task is None
        assert runtime.slot_held is False

        # A same-key retry now replays the terminal FAILED facts — no dispatch.
        replayed_snap, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=quick_op,
            idempotency_key="reaper-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        assert replayed is True and replayed_snap.id == snap.id
        assert replayed_snap.state == LivePlanningJobState.FAILED
        assert replayed_snap.stage == "deadline_exceeded"
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await _settle_reaper(registry)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_owner_post_commit_ambiguous_confirms_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P0 post-commit (indeterminate) failure, cancel path: the terminal
    state is durably committed on disk but the finalize raises. The owner must
    treat the committed state as authoritative — confirm and consume the pending
    outcome, never rewrite a conflicting label. Exactly one terminalize; the
    in-memory record matches the disk; no reaper is ever needed."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="owner-post-commit-cancel",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert runtime.pending_terminal is not None
        assert not runtime.operation_task.done()

        terminalize_calls = 0
        original_terminalize = registry._terminalize_locked

        def counting_terminalize(*args: Any, **kwargs: Any) -> None:
            nonlocal terminalize_calls
            terminalize_calls += 1
            original_terminalize(*args, **kwargs)

        monkeypatch.setattr(registry, "_terminalize_locked", counting_terminalize)
        os.environ["TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT"] = "post_replace_dir_fsync"
        try:
            stop.set()
            await asyncio.wait_for(runtime.operation_task, timeout=3)
            await _wait_for_terminal_state(
                registry, snap.id, "tenant-a", LivePlanningJobState.CANCELLED
            )
        finally:
            os.environ.pop("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", None)
        await _settle_cleanup_owner(runtime)

        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.stage == "cancelled"
        assert final.cancel_pending is False
        assert runtime.pending_terminal is None
        assert terminalize_calls == 1
        assert runtime.cleanup_owner is not None and runtime.cleanup_owner.done()
        assert registry._reaper_task is None
        assert runtime.slot_held is False
    finally:
        stop.set()
        os.environ.pop("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", None)
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_owner_post_commit_ambiguous_confirms_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 post-commit (indeterminate) failure, deadline path: the DURABLE
    FAILED/deadline_exceeded state is committed on disk but the finalize raises.
    The owner confirms and consumes the pending outcome — memory=disk, exactly
    one terminalize, safe_failure preserved."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="owner-post-commit-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        runtime = registry._records[snap.id]
        await asyncio.wait_for(runtime.task, timeout=5)
        assert runtime.task.done()
        assert runtime.snapshot.cancel_pending is True
        assert runtime.pending_terminal is not None
        assert runtime.pending_terminal.state == LivePlanningJobState.FAILED
        assert not runtime.operation_task.done()

        terminalize_calls = 0
        original_terminalize = registry._terminalize_locked

        def counting_terminalize(*args: Any, **kwargs: Any) -> None:
            nonlocal terminalize_calls
            terminalize_calls += 1
            original_terminalize(*args, **kwargs)

        monkeypatch.setattr(registry, "_terminalize_locked", counting_terminalize)
        os.environ["TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT"] = "post_replace_dir_fsync"
        try:
            stop.set()
            await asyncio.wait_for(runtime.operation_task, timeout=3)
            await _wait_for_terminal_state(
                registry, snap.id, "tenant-a", LivePlanningJobState.FAILED
            )
        finally:
            os.environ.pop("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", None)
        await _settle_cleanup_owner(runtime)

        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.FAILED
        assert final.stage == "deadline_exceeded"
        assert final.safe_failure_code == "deadline_exceeded"
        assert final.cancel_pending is False
        assert runtime.pending_terminal is None
        assert terminalize_calls == 1
        assert runtime.cleanup_owner is not None and runtime.cleanup_owner.done()
        assert registry._reaper_task is None
        assert runtime.slot_held is False

        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snap.id
        )
        assert disk_record["snapshot"]["state"] == "failed"
        assert disk_record["snapshot"]["stage"] == "deadline_exceeded"
        assert disk_record["snapshot"]["safe_failure_code"] == "deadline_exceeded"
        assert disk_record["pending_terminal"] is None
    finally:
        stop.set()
        os.environ.pop("TRIPCHORD_TEST_REGISTRY_PERSIST_FAILPOINT", None)
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_repeated_ensure_and_done_callback_no_duplicate_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-145 P0: repeated _ensure_cleanup_owner / _maybe_release_slot calls (the
    operation done-callback, cancel/close joins, same-key retries) NEVER spawn a
    duplicate owner nor double-terminalize — the owner is unique and waitable and
    the final label is published exactly once."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="owner-repeat-ensure",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True
        assert runtime.pending_terminal is not None
        assert not runtime.operation_task.done()

        for _ in range(100):
            if runtime.cleanup_owner is not None:
                break
            await asyncio.sleep(0)
        owner = runtime.cleanup_owner
        assert owner is not None and not owner.done()

        # Repeated ensure / release calls must never replace or duplicate the
        # unique waitable owner.
        for _ in range(5):
            registry._ensure_cleanup_owner(runtime)
            assert runtime.cleanup_owner is owner
            registry._maybe_release_slot(runtime)
            assert runtime.cleanup_owner is owner

        terminalize_calls = 0
        original_terminalize = registry._terminalize_locked

        def counting_terminalize(*args: Any, **kwargs: Any) -> None:
            nonlocal terminalize_calls
            terminalize_calls += 1
            original_terminalize(*args, **kwargs)

        monkeypatch.setattr(registry, "_terminalize_locked", counting_terminalize)

        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await asyncio.wait_for(owner, timeout=3)
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.state == LivePlanningJobState.CANCELLED
        assert final.cancel_pending is False
        assert terminalize_calls == 1
        assert runtime.pending_terminal is None
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_cold_start_continues_durable_deadline_intent(
    tmp_path: Path,
) -> None:
    """C-145 P0 cold start: a process crashed while the record was isolated
    (cancel_pending + DURABLE FAILED/deadline_exceeded intent). A fresh registry
    reading that state must CONTINUE the retry intent — collect to
    FAILED/deadline_exceeded with the same safe failure — never guess
    restart_cancelled. Two consecutive cold starts observe identical facts and
    no reaper / owner residue remains."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()

    runtime: Any = None
    cold_a: LivePlanningJobRegistry | None = None
    cold_b: LivePlanningJobRegistry | None = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="cold-deadline-intent",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        runtime = registry._records[snap.id]
        await asyncio.wait_for(runtime.task, timeout=5)

        # Durable retry intent is on disk: cancel_pending + pending FAILED.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snap.id
        )
        assert disk_record["snapshot"]["cancel_pending"] is True
        assert disk_record["snapshot"]["stage"] == "timeout_pending"
        assert disk_record["pending_terminal"]["state"] == "failed"
        assert disk_record["pending_terminal"]["stage"] == "deadline_exceeded"
        assert disk_record["pending_terminal"]["safe_failure_code"] == "deadline_exceeded"

        # Simulate a crash mid-pending: copy the durable file, then let the
        # original registry settle normally (its own writes don't affect the copy).
        crash_state = tmp_path / "live-jobs-crash.json"
        crash_state.write_bytes(state_path.read_bytes())
        crash_state.chmod(0o600)
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(registry, snap.id, "tenant-a", LivePlanningJobState.FAILED)
        await _settle_cleanup_owner(runtime)
        assert registry._reaper_task is None
        await registry.close()

        # Cold start #1 continues the durable retry intent.
        cold_a = LivePlanningJobRegistry(state_path=crash_state)
        snap_a = await cold_a.get(snap.id, "tenant-a")
        assert snap_a is not None and snap_a.state == LivePlanningJobState.FAILED
        assert snap_a.stage == "deadline_exceeded"
        assert snap_a.safe_failure_code == "deadline_exceeded"
        assert snap_a.cancel_pending is False
        disk_a = json.loads(crash_state.read_text(encoding="utf-8"))
        disk_a_record = next(
            record for record in disk_a["records"] if record["snapshot"]["id"] == snap.id
        )
        assert disk_a_record["snapshot"]["state"] == "failed"
        assert disk_a_record["snapshot"]["stage"] == "deadline_exceeded"
        assert disk_a_record["pending_terminal"] is None

        # Cold start #2 reads the SAME terminal facts — no outcome drift.
        cold_b = LivePlanningJobRegistry(state_path=crash_state)
        snap_b = await cold_b.get(snap.id, "tenant-a")
        assert snap_b is not None
        assert snap_b.state == snap_a.state
        assert snap_b.stage == snap_a.stage
        assert snap_b.safe_failure_code == snap_a.safe_failure_code

        # No reaper / owner residue after the cold recovery.
        assert cold_a._reaper_task is None
        assert cold_b._reaper_task is None
        assert cold_a._records[snap.id].cleanup_owner is None
        await cold_a.close()
        await cold_b.close()
        assert cold_a._reaper_task is None
        assert cold_b._reaper_task is None
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        if cold_a is not None:
            await cold_a.close()
        if cold_b is not None:
            await cold_b.close()
        await registry.close()


@pytest.mark.asyncio
async def test_cleanup_deadline_failed_vs_cancel_cancelled_outcomes_no_drift() -> None:
    """C-145 P0: cancel/close record a CANCELLED pending outcome; the deadline
    records FAILED/deadline_exceeded. The cleanup owner consumes each caller's
    unambiguous target — the two outcomes never drift into each other and each
    auto-collects to its DURABLE terminal state with the safe failure preserved
    on the deadline path."""
    registry = LivePlanningJobRegistry(
        capacity=8,
        max_running=2,
        cancel_wait_seconds=0.05,
    )
    cancel_stop = asyncio.Event()
    cancel_started = asyncio.Event()
    cancel_swallowed = asyncio.Event()
    deadline_stop = asyncio.Event()
    deadline_started = asyncio.Event()
    deadline_swallowed = asyncio.Event()

    cancel_runtime: Any = None
    deadline_runtime: Any = None
    try:
        # Cancel path: the durable pending outcome is CANCELLED/cancelled.
        cancel_snap, _cancel_replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(cancel_stop, cancel_started, cancel_swallowed),
            idempotency_key="outcome-cancel",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        cancel_runtime = registry._records[cancel_snap.id]
        for _ in range(1000):
            if cancel_started.is_set():
                break
            await asyncio.sleep(0)
        assert cancel_started.is_set()
        await registry.cancel(cancel_snap.id, "tenant-a")
        assert cancel_runtime.pending_terminal is not None
        assert cancel_runtime.pending_terminal.state == LivePlanningJobState.CANCELLED
        assert cancel_runtime.pending_terminal.stage == "cancelled"

        # Deadline path: the durable pending outcome is FAILED/deadline_exceeded.
        deadline_snap, _deadline_replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(
                deadline_stop, deadline_started, deadline_swallowed
            ),
            idempotency_key="outcome-deadline",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        deadline_runtime = registry._records[deadline_snap.id]
        await asyncio.wait_for(deadline_runtime.task, timeout=5)
        assert deadline_runtime.pending_terminal is not None
        assert deadline_runtime.pending_terminal.state == LivePlanningJobState.FAILED
        assert deadline_runtime.pending_terminal.stage == "deadline_exceeded"

        # Both operations stop; both auto-collect to their DURABLE targets.
        cancel_stop.set()
        await asyncio.wait_for(cancel_runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, cancel_snap.id, "tenant-a", LivePlanningJobState.CANCELLED
        )
        deadline_stop.set()
        await asyncio.wait_for(deadline_runtime.operation_task, timeout=3)
        await _wait_for_terminal_state(
            registry, deadline_snap.id, "tenant-a", LivePlanningJobState.FAILED
        )

        cancel_final = await registry.get(cancel_snap.id, "tenant-a")
        assert cancel_final is not None
        assert cancel_final.state == LivePlanningJobState.CANCELLED
        assert cancel_final.stage == "cancelled"
        deadline_final = await registry.get(deadline_snap.id, "tenant-a")
        assert deadline_final is not None
        assert deadline_final.state == LivePlanningJobState.FAILED
        assert deadline_final.stage == "deadline_exceeded"
        assert deadline_final.safe_failure_code == "deadline_exceeded"
    finally:
        cancel_stop.set()
        deadline_stop.set()
        await _settle_leaked_runtime(cancel_stop, cancel_runtime)
        await _settle_cleanup_owner(cancel_runtime)
        await _settle_leaked_runtime(deadline_stop, deadline_runtime)
        await _settle_cleanup_owner(deadline_runtime)
        await registry.close()


# ---------------------------------------------------------------------------
# C-145 P0 supplement counterexamples (red on e5946d2 / green after the fix)
# ---------------------------------------------------------------------------


def _v3_snapshot(
    job_id: str,
    state: LivePlanningJobState,
    stage: str,
    progress: int,
    revision: int,
    *,
    cancellation_requested: bool = False,
    cancel_pending: bool = False,
) -> LivePlanningJobSnapshot:
    """A v3-shaped snapshot with deterministic relative timestamps."""
    created = datetime.now(UTC) - timedelta(minutes=5)
    return LivePlanningJobSnapshot(
        id=job_id,
        state=state,
        stage=stage,
        progress=progress,
        revision=revision,
        cancellation_requested=cancellation_requested,
        cancel_pending=cancel_pending,
        request_sha256=REQUEST_SHA256,
        model_trace_scope_sha256=REQUEST_SHA256,
        created_at=created,
        updated_at=created,
        deadline_at=created + timedelta(hours=1),
    )


def _write_registry_state(payload: dict[str, Any], state_path: Path) -> None:
    """Write a hand-built registry state file exactly as the persist would."""
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    state_path.chmod(0o600)


def _v3_idempotency_entry(
    tenant_id: str,
    idempotency_key: str,
    job_id: str,
    *,
    defer_start: bool = True,
) -> dict[str, Any]:
    """A faithful v3 idempotency binding (defer_start present, legacy_isolated
    omitted — the OLD-v3 shape)."""
    return {
        "partition": LivePlanningJobRegistry._idempotency_partition(tenant_id, idempotency_key),
        "job_id": job_id,
        "request_digest": REQUEST_SHA256,
        "defer_start": defer_start,
    }


@pytest.mark.asyncio
async def test_old_v3_schema_without_pending_terminal_loads_rewrites_new_v3(
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement #1: the v3 loader explicitly accepts the OLD-v3 field
    set (records WITHOUT ``pending_terminal``) in addition to the new-v3 set.
    After a successful first load the file is atomically rewritten to the new-v3
    shape, so a second cold start reads new-v3. RED on HEAD: old-v3 records are
    rejected at load."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_running = "live-job-old-v3-running"
    job_pending = "live-job-old-v3-pending"
    job_terminal = "live-job-old-v3-terminal"

    snap_running = _v3_snapshot(
        job_running, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1
    )
    snap_pending = _v3_snapshot(
        job_pending,
        LivePlanningJobState.RUNNING,
        "cancelling",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    snap_terminal = _v3_snapshot(
        job_terminal,
        LivePlanningJobState.CANCELLED,
        "cancelled",
        100,
        3,
        cancellation_requested=True,
    )

    partition = LivePlanningJobRegistry._tenant_partition(tenant_id)
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        # OLD-v3 records: no ``pending_terminal`` key at all.
        "records": [
            {
                "tenant_partition": partition,
                "snapshot": snap_running.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
            {
                "tenant_partition": partition,
                "snapshot": snap_pending.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
            {
                "tenant_partition": partition,
                "snapshot": snap_terminal.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
            },
        ],
        "idempotency": [
            _v3_idempotency_entry(tenant_id, "old-v3-running", job_running),
            _v3_idempotency_entry(tenant_id, "old-v3-pending", job_pending),
            _v3_idempotency_entry(tenant_id, "old-v3-terminal", job_terminal),
        ],
    }
    _write_registry_state(payload, state_path)

    # First cold start: the old-v3 file must load (RED on HEAD: rejected).
    registry = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
    try:
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        assert disk["schema_version"] == "tripchord-live-job-registry-v3"
        records = {record["snapshot"]["id"]: record for record in disk["records"]}
        for job in (job_running, job_pending, job_terminal):
            # Every record was rewritten to the NEW-v3 shape.
            assert "pending_terminal" in records[job]

        # C-146 P0-3: the plain admitted RUNNING record (no cancel intent, deadline
        # not passed) has no provable terminal outcome — it is isolated as
        # ambiguous, NEVER guessed to restart_cancelled. The terminal record stays
        # terminal. The ambiguous cancel-pending record (no provable outcome) is
        # isolated as well.
        assert records[job_running]["snapshot"]["state"] == "running"
        assert records[job_running]["snapshot"]["stage"] == "isolated_ambiguous_cancel"
        assert records[job_running]["snapshot"]["cancel_pending"] is False
        assert records[job_running]["pending_terminal"] is None
        # The terminal record stays terminal.
        assert records[job_terminal]["snapshot"]["state"] == "cancelled"
        # The ambiguous cancel-pending record (no provable outcome) was isolated,
        # NEVER guessed to cancelled/failed.
        assert records[job_pending]["snapshot"]["state"] == "running"
        assert records[job_pending]["snapshot"]["stage"] == "isolated_ambiguous_cancel"
        assert records[job_pending]["snapshot"]["cancel_pending"] is False
        assert records[job_pending]["pending_terminal"] is None

        # The idempotency binding of the isolated record is marked legacy-isolated
        # so a same-key request fails closed instead of replaying an unknown outcome.
        entries = {entry["job_id"]: entry for entry in disk["idempotency"]}
        assert entries[job_pending]["legacy_isolated"] is True

        # A second cold start reads the SAME new-v3 facts — no drift.
        cold = LivePlanningJobRegistry(state_path=state_path, capacity=4, max_running=2)
        try:
            running = await cold.get(job_running, tenant_id)
            assert running is not None
            assert running.state == LivePlanningJobState.RUNNING
            assert running.stage == "isolated_ambiguous_cancel"
            pending = await cold.get(job_pending, tenant_id)
            assert pending is not None
            assert pending.state == LivePlanningJobState.RUNNING
            assert pending.stage == "isolated_ambiguous_cancel"
            terminal = await cold.get(job_terminal, tenant_id)
            assert terminal is not None and terminal.state == LivePlanningJobState.CANCELLED
        finally:
            await cold.close()
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_v3_loader_rejects_unknown_or_missing_record_fields(tmp_path: Path) -> None:
    """C-145 P0 supplement #1: the v3 field-set relaxation accepts ONLY the two
    exact shapes (with / without ``pending_terminal``). An unknown extra field or
    a missing core field is still rejected fail-closed — nothing is silently
    patched or migrated."""
    tenant_id = "tenant-a"
    job_id = "live-job-field-strict"
    snap = _v3_snapshot(job_id, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1)

    def base_payload(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "tripchord-live-job-registry-v3",
            "records": [record],
            "idempotency": [_v3_idempotency_entry(tenant_id, "field-strict", job_id)],
        }

    good = {
        "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
        "snapshot": snap.model_dump(mode="json"),
        "prepared": False,
        "activation_operation": None,
        "pending_terminal": None,
    }
    with_extra = dict(good)
    with_extra["bogus_extra_field"] = True
    missing_snapshot = {key: value for key, value in good.items() if key != "snapshot"}

    # Sanity: the good new-v3 shape loads.
    good_path = tmp_path / "good.json"
    _write_registry_state(base_payload(good), good_path)
    good_registry = LivePlanningJobRegistry(state_path=good_path, capacity=4)
    await good_registry.close()

    # Unknown extra field → fail closed.
    extra_path = tmp_path / "extra.json"
    _write_registry_state(base_payload(with_extra), extra_path)
    with pytest.raises(RuntimeError, match="record is invalid"):
        LivePlanningJobRegistry(state_path=extra_path, capacity=4)

    # Missing core field → fail closed.
    missing_path = tmp_path / "missing.json"
    _write_registry_state(base_payload(missing_snapshot), missing_path)
    with pytest.raises(RuntimeError, match="record is invalid"):
        LivePlanningJobRegistry(state_path=missing_path, capacity=4)


@pytest.mark.asyncio
async def test_deadline_first_intent_durably_carries_failed_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement #2: the deadline's FIRST durable intent (the timeout
    isolation commit) must atomically carry ``pending_terminal=FAILED`` +
    ``stage=deadline_exceeded`` + the safe-failure/error contract. A crash right
    after that commit must cold-start to FAILED/deadline_exceeded, never a
    guessed label. RED on HEAD: the first intent writes only cancel_pending."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()
    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="deadline-first-intent",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.4,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()

        real_bounded = registry._persist_locked_with_bounded_retry
        captured: list[tuple[dict[str, Any], Any]] = []

        async def capture_bounded() -> None:
            await real_bounded()
            if not captured:
                disk = json.loads(state_path.read_text(encoding="utf-8"))
                record = next(
                    record for record in disk["records"] if record["snapshot"]["id"] == snap.id
                )
                captured.append((record["snapshot"], record.get("pending_terminal")))

        monkeypatch.setattr(registry, "_persist_locked_with_bounded_retry", capture_bounded)
        await asyncio.wait_for(runtime.task, timeout=5)

        assert captured, "the deadline first intent was never persisted"
        snapshot, pending = captured[0]
        assert snapshot["cancel_pending"] is True
        assert snapshot["stage"] == "timeout_pending"
        # RED on HEAD: the first durable intent carries NO pending outcome.
        assert pending is not None
        assert pending["state"] == "failed"
        assert pending["stage"] == "deadline_exceeded"
        assert pending["error"] == "TimeoutError: live planning job deadline exceeded"
        assert pending["safe_failure_code"] == "deadline_exceeded"

        # The operation is still alive (stubborn); the first intent committed
        # before any drain — the crash window is exactly this shape.
        assert runtime.operation_task is not None and not runtime.operation_task.done()
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_cancel_first_intent_durably_carries_cancelled_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement #2 control: cancel()'s FIRST durable intent carries
    the full CANCELLED/cancelled outcome in the SAME atomic commit as the
    cancel_pending isolation — a crash right after that commit cold-starts to
    cancelled, never a guessed restart_cancelled. RED on HEAD: the first intent
    writes only cancel_pending."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()
    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="cancel-first-intent",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()

        real_persist = registry._persist_locked
        captured: list[Any] = []

        def capture_persist() -> None:
            real_persist()
            if not captured:
                disk = json.loads(state_path.read_text(encoding="utf-8"))
                record = next(
                    record for record in disk["records"] if record["snapshot"]["id"] == snap.id
                )
                captured.append(record.get("pending_terminal"))

        monkeypatch.setattr(registry, "_persist_locked", capture_persist)
        outcome = await registry.cancel(snap.id, "tenant-a")
        assert outcome is not None and outcome.cancel_pending is True

        assert captured, "the cancel first intent was never persisted"
        pending = captured[0]
        # RED on HEAD: the first durable cancel intent carries NO pending outcome.
        assert pending is not None
        assert pending["state"] == "cancelled"
        assert pending["stage"] == "cancelled"
        assert pending["cancellation_requested"] is True
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_close_first_intent_durably_carries_cancelled_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement #2 control: close()'s FIRST durable intent carries the
    full CANCELLED/cancelled outcome for every still-active record in the SAME
    atomic commit as the closing isolation. RED on HEAD: the first intent writes
    only cancel_pending."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()
    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="close-first-intent",
            request_digest=REQUEST_SHA256,
            deadline_seconds=30,
        )
        runtime = registry._records[snap.id]
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0)
        assert started.is_set()

        real_bounded = registry._persist_locked_with_bounded_retry
        captured: list[Any] = []

        async def capture_bounded() -> None:
            await real_bounded()
            if not captured:
                disk = json.loads(state_path.read_text(encoding="utf-8"))
                record = next(
                    record for record in disk["records"] if record["snapshot"]["id"] == snap.id
                )
                captured.append(record.get("pending_terminal"))

        monkeypatch.setattr(registry, "_persist_locked_with_bounded_retry", capture_bounded)
        await registry.close()

        assert captured, "the close first intent was never persisted"
        pending = captured[0]
        # RED on HEAD: the first durable close intent carries NO pending outcome.
        assert pending is not None
        assert pending["state"] == "cancelled"
        assert pending["stage"] == "cancelled"
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_crash_mid_cancel_without_outcome_isolated_not_guessed(
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement: a disk record with ``cancel_pending=true`` but NO
    provable terminal outcome must be isolated (never replayed, never guessed to
    cancelled or failed). RED on HEAD: the loader guessed restart_cancelled."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-mid-cancel-no-outcome"
    snap = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        "cancelling",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, "mid-cancel-no-outcome", job_id)],
    }
    _write_registry_state(payload, state_path)

    registry = LivePlanningJobRegistry(state_path=state_path, capacity=4)
    try:
        snapshot = await registry.get(job_id, tenant_id)
        # NOT guessed to cancelled/failed — the record stays non-terminal and is
        # explicitly quarantined.
        assert snapshot is not None
        assert snapshot.state == LivePlanningJobState.RUNNING
        assert snapshot.stage == "isolated_ambiguous_cancel"
        assert snapshot.cancel_pending is False
        # The idempotency binding is isolated: a same-key request fails closed.
        with pytest.raises(LivePlanningJobIdempotencyConflictError):

            async def operation(_: Any) -> dict[str, Any]:
                return {"ok": True}

            await registry.start_idempotent(
                tenant_id=tenant_id,
                operation=operation,
                idempotency_key="mid-cancel-no-outcome",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_pending_terminal_non_terminal_state_rejected_fail_closed(
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement: a durable pending outcome targeting RUNNING/QUEUED
    (or any non-terminal value) would terminalize a live record to a label the
    caller never chose. The loader rejects it fail-closed. RED on HEAD: the
    pending RUNNING state was accepted and the record was terminalized to RUNNING
    (a lie)."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-pending-running"
    snap = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        "timeout_pending",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": {
                    "state": "running",
                    "stage": "deadline_exceeded",
                    "cancellation_requested": True,
                },
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, "pending-running", job_id)],
    }
    _write_registry_state(payload, state_path)
    # RED on HEAD: this corrupt shape was accepted (the record was terminalized
    # to RUNNING instead of rejected).
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_safe_failure_requires_failed_state(
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement: a safe-failure diagnostic only ever accompanies a
    FAILED outcome. A CANCELLED intent carrying a safe failure is corruption and
    is rejected fail-closed, never default-patched. RED on HEAD: the inconsistent
    shape was accepted."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-safe-failure-on-cancelled"
    snap = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        "cancelling",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": {
                    "state": "cancelled",
                    "stage": "cancelled",
                    "cancellation_requested": True,
                    "safe_failure_code": "deadline_exceeded",
                    "safe_failure_details": {
                        "exception_class": "TimeoutError",
                        "message_sha256": "c" * 64,
                    },
                    "safe_failure_details_digest": "d" * 64,
                },
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, "safe-failure-on-cancelled", job_id)],
    }
    _write_registry_state(payload, state_path)
    # RED on HEAD: this inconsistent shape was accepted.
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_loader_rejects_idempotency_digest_mismatch(tmp_path: Path) -> None:
    """C-145 P0 supplement: a durable idempotency binding whose request digest
    does not match the record's input digest is corruption — rejected fail-closed,
    never patched or re-derived."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-digest-mismatch"
    snap = _v3_snapshot(job_id, LivePlanningJobState.RUNNING, "interpreting_requirement", 5, 1)
    entry = _v3_idempotency_entry(tenant_id, "digest-mismatch", job_id)
    entry["request_digest"] = "b" * 64  # foreign digest, not the snapshot's
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
            }
        ],
        "idempotency": [entry],
    }
    _write_registry_state(payload, state_path)
    with pytest.raises(RuntimeError, match="idempotency binding is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_cleanup_backoff_round_1025_no_overflow_owner_survives_saturates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-145 P0 supplement #3: the cleanup owner's budget-exhaustion backoff must
    NEVER compute ``2 ** (round - 1)`` with a huge round — that OverflowErrors
    and kills the sole owner (RED on HEAD: the owner task dies, the record never
    auto-collects). The fixed code validates/normalizes the round and saturates
    the exponent so the delay caps at the 0.5s ceiling, the owner survives, the
    reaper is armed, and store recovery still terminalizes."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=8,
        max_running=1,
        cancel_wait_seconds=0.05,
    )
    stop = asyncio.Event()
    started = asyncio.Event()
    swallowed = asyncio.Event()
    runtime: Any = None
    try:
        snap, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=_make_stubborn_swallow_cancel(stop, started, swallowed),
            idempotency_key="backoff-overflow",
            request_digest=REQUEST_SHA256,
            deadline_seconds=0.05,
        )
        runtime = registry._records[snap.id]
        await asyncio.wait_for(runtime.task, timeout=5)

        # Durable retry intent is on disk (cancel_pending + pending FAILED).
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        record = next(record for record in disk["records"] if record["snapshot"]["id"] == snap.id)
        assert record["snapshot"]["cancel_pending"] is True
        assert record["pending_terminal"]["state"] == "failed"

        # Force EVERY terminal persist to fail pre-commit so the owner exhausts
        # its per-round budget and reaches the backoff branch.
        async def fail_finish(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected permanent terminal persist failure")

        monkeypatch.setattr(registry, "_finish", fail_finish)

        # Simulate a huge (≈1025) round — the overflow threshold.
        runtime.cleanup_retry_round = 1025
        stop.set()
        await asyncio.wait_for(runtime.operation_task, timeout=3)

        owner = runtime.cleanup_owner
        assert owner is not None
        # RED on HEAD: the owner dies with OverflowError and the await re-raises it.
        await asyncio.wait_for(owner, timeout=3)
        assert owner.exception() is None
        assert runtime.cleanup_retry_round == 1026
        loop = asyncio.get_running_loop()
        remaining = runtime.cleanup_next_retry_monotonic - loop.time()
        # Saturated to the 0.5s ceiling (never a busy-wait / unbounded sleep).
        assert 0.0 <= remaining <= 0.5 + 0.02
        assert registry._reaper_task is not None and not registry._reaper_task.done()

        # Malicious round values are safely normalized (observable, no crash).
        assert registry._bump_cleanup_retry_round(-3) == 1
        assert registry._bump_cleanup_retry_round("garbage") == 1
        assert registry._bump_cleanup_retry_round(2.5) == 1
        assert registry._cleanup_retry_delay(10**9) == 0.5
        assert registry._cleanup_retry_delay(0) == 0.02
        assert registry._cleanup_retry_delay(-5) == 0.02

        # Store recovery still auto-terminalizes: restore the real _finish, and
        # the reaper re-spawns the owner which now commits FAILED/deadline_exceeded.
        monkeypatch.undo()
        await _wait_for_terminal_state(registry, snap.id, "tenant-a", LivePlanningJobState.FAILED)
        final = await registry.get(snap.id, "tenant-a")
        assert final is not None and final.stage == "deadline_exceeded"
        assert runtime.pending_terminal is None
    finally:
        stop.set()
        await _settle_leaked_runtime(stop, runtime)
        await _settle_cleanup_owner(runtime)
        await _settle_reaper(registry)
        await registry.close()


# C-146 P0 supplement counterexamples — RETURN a6f3e884 on 7a403f1 / green after the fix
# ---------------------------------------------------------------------------------------


def _pending_outcome(**overrides: Any) -> dict[str, Any]:
    """The EXACT producer field set for a CANCELLED/cancelled durable intent."""
    base: dict[str, Any] = {
        "state": "cancelled",
        "stage": "cancelled",
        "result": None,
        "error": None,
        "safe_failure_code": None,
        "safe_failure_details": None,
        "safe_failure_details_digest": None,
        "cancellation_requested": True,
    }
    base.update(overrides)
    return base


def _failed_outcome(**overrides: Any) -> dict[str, Any]:
    """The EXACT producer field set for a FAILED/deadline_exceeded durable intent
    with a complete, digest-consistent safe-failure diagnostic."""
    details = LivePlanningSafeFailureDetails(exception_class="TimeoutError")
    base: dict[str, Any] = {
        "state": "failed",
        "stage": "deadline_exceeded",
        "result": None,
        "error": "TimeoutError: live planning job deadline exceeded",
        "safe_failure_code": LivePlanningSafeFailureCode.DEADLINE_EXCEEDED.value,
        "safe_failure_details": details.model_dump(mode="json"),
        "safe_failure_details_digest": _safe_failure_details_digest(
            LivePlanningSafeFailureCode.DEADLINE_EXCEEDED, details
        ),
        "cancellation_requested": True,
    }
    base.update(overrides)
    return base


def _write_pending_outcome_state(
    state_path: Path,
    job_id: str,
    tenant_id: str,
    idempotency_key: str,
    *,
    pending: dict[str, Any] | None,
) -> None:
    """Write a v3 state file for one RUNNING/cancelling/cancel_pending record with
    the given durable pending outcome (None = the old-v3 ambiguous shape)."""
    snap = _v3_snapshot(
        job_id,
        LivePlanningJobState.RUNNING,
        "cancelling",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": pending,
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, idempotency_key, job_id)],
    }
    _write_registry_state(payload, state_path)


@pytest.mark.asyncio
async def test_pending_terminal_foreign_success_rejected(tmp_path: Path) -> None:
    """C-146 P0 counterexample (C-125 RETURN P0-1): a durable pending outcome
    targeting SUCCEEDED would terminalize a live record to a success label the
    caller never chose. RED on HEAD: the loader accepted ``succeeded/complete``
    and cold-started the record to SUCCEEDED/complete. Fixed: rejected fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    _write_pending_outcome_state(
        state_path,
        "live-job-pending-succeeded",
        "tenant-a",
        "pending-succeeded",
        pending=_pending_outcome(state="succeeded", stage="complete"),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_wrong_state_stage_rejected(tmp_path: Path) -> None:
    """C-146 P0 counterexample: the stage is part of the contract — CANCELLED must
    carry exactly ``cancelled`` and FAILED exactly ``deadline_exceeded``.
    ``cancelled/complete`` and ``failed/cancelled`` (with a complete safe-failure)
    are corruption. RED on HEAD: both accepted. Fixed: rejected fail-closed."""
    cancelled_path = tmp_path / "cancelled-complete.json"
    _write_pending_outcome_state(
        cancelled_path,
        "live-job-cancelled-complete",
        "tenant-a",
        "cancelled-complete",
        pending=_pending_outcome(stage="complete"),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=cancelled_path, capacity=4)

    failed_path = tmp_path / "failed-cancelled.json"
    _write_pending_outcome_state(
        failed_path,
        "live-job-failed-cancelled",
        "tenant-b",
        "failed-cancelled",
        pending=_failed_outcome(stage="cancelled"),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=failed_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_extra_field_rejected(tmp_path: Path) -> None:
    """C-146 P0 counterexample: a legal cancel shape carrying an unknown extra
    field must be rejected — the decoder accepts ONLY the producer's exact field
    set. RED on HEAD: unknown fields were ignored. Fixed: rejected fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    pending = _pending_outcome()
    pending["bogus_extra_field"] = True
    _write_pending_outcome_state(
        state_path,
        "live-job-extra-field",
        "tenant-a",
        "extra-field",
        pending=pending,
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_missing_field_rejected(tmp_path: Path) -> None:
    """C-146 P0 counterexample: a legal cancel shape missing one producer field
    must be rejected — nothing is silently default-patched. RED on HEAD: missing
    fields fell back to None. Fixed: rejected fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    pending = _pending_outcome()
    del pending["safe_failure_code"]
    _write_pending_outcome_state(
        state_path,
        "live-job-missing-field",
        "tenant-a",
        "missing-field",
        pending=pending,
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_deadline_without_safe_failure_rejected(
    tmp_path: Path,
) -> None:
    """C-146 P0 counterexample: FAILED/deadline_exceeded must ALWAYS carry a
    complete safe-failure diagnostic. RED on HEAD: the missing-safe-failure shape
    was accepted and cold-started to FAILED. Fixed: rejected fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    _write_pending_outcome_state(
        state_path,
        "live-job-deadline-no-safe-failure",
        "tenant-a",
        "deadline-no-safe-failure",
        pending=_pending_outcome(
            state="failed",
            stage="deadline_exceeded",
            error="TimeoutError: live planning job deadline exceeded",
        ),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_dangling_safe_failure_rejected(tmp_path: Path) -> None:
    """C-146 P0 counterexample: a CANCELLED intent carrying safe-failure fields is
    corruption — dangling details/digest must never be silently dropped. RED on
    HEAD: the dangling shape was accepted. Fixed: rejected fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    _write_pending_outcome_state(
        state_path,
        "live-job-dangling-safe-failure",
        "tenant-a",
        "dangling-safe-failure",
        pending=_pending_outcome(safe_failure_code="deadline_exceeded"),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_cancellation_flag_not_true_rejected(
    tmp_path: Path,
) -> None:
    """C-146 P0 counterexample: every durable pending outcome is a
    cancel/close/deadline intent, so its cancellation flag must be exactly True.
    RED on HEAD: a False/None flag was accepted. Fixed: rejected fail-closed."""
    for flag in (False, None):
        state_path = tmp_path / f"live-jobs-{flag}.json"
        _write_pending_outcome_state(
            state_path,
            f"live-job-cancel-flag-{flag}",
            "tenant-a",
            f"cancel-flag-{flag}",
            pending=_pending_outcome(cancellation_requested=flag),
        )
        with pytest.raises(RuntimeError, match="pending terminal is invalid"):
            LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_safe_failure_digest_mismatch_rejected(
    tmp_path: Path,
) -> None:
    """C-146 P0 counterexample: a FAILED intent's safe-failure digest must
    recompute from the stored code + details — a hand-written/tampered digest is
    corruption. RED on HEAD: any 64-hex digest was accepted. Fixed: rejected."""
    state_path = tmp_path / "live-jobs.json"
    _write_pending_outcome_state(
        state_path,
        "live-job-bad-digest",
        "tenant-a",
        "bad-digest",
        pending=_failed_outcome(safe_failure_details_digest="f" * 64),
    )
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=state_path, capacity=4)


@pytest.mark.asyncio
async def test_pending_terminal_valid_shapes_load_and_terminalize(
    tmp_path: Path,
) -> None:
    """C-146 P0 control: the EXACT producer shapes for a cancel intent and a
    deadline intent still load and cold-start to the intended terminal state —
    the strict decoder rejects only foreign/tampered shapes, never its own
    output."""
    cancelled_path = tmp_path / "valid-cancelled.json"
    _write_pending_outcome_state(
        cancelled_path,
        "live-job-valid-cancelled",
        "tenant-a",
        "valid-cancelled",
        pending=_pending_outcome(),
    )
    first = LivePlanningJobRegistry(state_path=cancelled_path, capacity=4)
    try:
        snap = await first.get("live-job-valid-cancelled", "tenant-a")
        assert snap is not None and snap.state == LivePlanningJobState.CANCELLED
        assert snap.stage == "cancelled"
        assert snap.cancellation_requested is True
    finally:
        await first.close()

    failed_path = tmp_path / "valid-failed.json"
    _write_pending_outcome_state(
        failed_path,
        "live-job-valid-failed",
        "tenant-b",
        "valid-failed",
        pending=_failed_outcome(),
    )
    second = LivePlanningJobRegistry(state_path=failed_path, capacity=4)
    try:
        snap = await second.get("live-job-valid-failed", "tenant-b")
        assert snap is not None and snap.state == LivePlanningJobState.FAILED
        assert snap.stage == "deadline_exceeded"
        assert snap.safe_failure_code == LivePlanningSafeFailureCode.DEADLINE_EXCEEDED
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_isolated_ambiguous_cancel_survives_close_and_second_cold_boot(
    tmp_path: Path,
) -> None:
    """C-146 P0 counterexample (C-125 RETURN P0-2): after a cold start isolates an
    ambiguous cancel-pending record, close() must NOT guess CANCELLED. RED on
    HEAD: close() included the quarantined record in its active set, wrote a
    durable CANCELLED intent, and the SECOND cold boot drifted it to CANCELLED.
    Fixed: close() leaves it quarantined and the second cold boot still sees the
    same isolation with a fail-closed same-key path."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-close-keeps-isolated"
    _write_pending_outcome_state(
        state_path,
        job_id,
        tenant_id,
        "close-keeps-isolated",
        pending=None,
    )

    first = LivePlanningJobRegistry(state_path=state_path, capacity=4)
    try:
        snapshot = await first.get(job_id, tenant_id)
        assert snapshot is not None
        assert snapshot.state == LivePlanningJobState.RUNNING
        assert snapshot.stage == "isolated_ambiguous_cancel"
        assert snapshot.cancel_pending is False
        # close() must not touch the quarantined record.
        await first.close()
    finally:
        await first.close()

    disk = json.loads(state_path.read_text(encoding="utf-8"))
    record = next(item for item in disk["records"] if item["snapshot"]["id"] == job_id)
    # The on-disk record is STILL quarantined — never rewritten to a guessed label.
    assert record["snapshot"]["state"] == "running"
    assert record["snapshot"]["stage"] == "isolated_ambiguous_cancel"
    assert record["snapshot"]["cancel_pending"] is False
    assert record["pending_terminal"] is None

    second = LivePlanningJobRegistry(state_path=state_path, capacity=4)
    try:
        snapshot = await second.get(job_id, tenant_id)
        assert snapshot is not None
        assert snapshot.state == LivePlanningJobState.RUNNING
        assert snapshot.stage == "isolated_ambiguous_cancel"

        async def operation(_: Any) -> dict[str, Any]:
            return {"ok": True}

        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await second.start_idempotent(
                tenant_id=tenant_id,
                operation=operation,
                idempotency_key="close-keeps-isolated",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_isolated_ambiguous_cancel_survives_explicit_cancel_and_cold_boot(
    tmp_path: Path,
) -> None:
    """C-146 P0 counterexample (P0-2): an explicit cancel() on a quarantined
    ambiguous-cancel record must NOT guess CANCELLED — it returns the isolated
    snapshot unchanged (idempotent, fail-closed) and a later cold boot still sees
    the same isolation. RED on HEAD: cancel() wrote a durable CANCELLED intent and
    drifted the record."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-cancel-keeps-isolated"
    _write_pending_outcome_state(
        state_path,
        job_id,
        tenant_id,
        "cancel-keeps-isolated",
        pending=None,
    )

    first = LivePlanningJobRegistry(state_path=state_path, capacity=4)
    try:
        snapshot = await first.get(job_id, tenant_id)
        assert snapshot is not None
        assert snapshot.stage == "isolated_ambiguous_cancel"
        outcome = await first.cancel(job_id, tenant_id)
        assert outcome is not None
        assert outcome.state == LivePlanningJobState.RUNNING
        assert outcome.stage == "isolated_ambiguous_cancel"
        assert outcome.cancel_pending is False
    finally:
        await first.close()

    disk = json.loads(state_path.read_text(encoding="utf-8"))
    record = next(item for item in disk["records"] if item["snapshot"]["id"] == job_id)
    assert record["snapshot"]["state"] == "running"
    assert record["snapshot"]["stage"] == "isolated_ambiguous_cancel"
    assert record["snapshot"]["cancel_pending"] is False
    assert record["pending_terminal"] is None

    second = LivePlanningJobRegistry(state_path=state_path, capacity=4)
    try:
        snapshot = await second.get(job_id, tenant_id)
        assert snapshot is not None
        assert snapshot.state == LivePlanningJobState.RUNNING
        assert snapshot.stage == "isolated_ambiguous_cancel"

        async def operation(_: Any) -> dict[str, Any]:
            return {"ok": True}

        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await second.start_idempotent(
                tenant_id=tenant_id,
                operation=operation,
                idempotency_key="cancel-keeps-isolated",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
    finally:
        await second.close()


# C-146 P0 supplement counterexamples — P0-4 / b119 (permanent-failure hard-stop,
# bounded reconcile, cold-boot provenance, bounded attacks, quarantine capacity)
# -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_stop_permanent_failure_bounds_side_effects_and_quarantines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / hard-stop gate (12e35d45 门 1): under a
    PERMANENT storage failure a stubborn IN-PROCESS operation that swallows
    CancelledError is isolated by the bounded watchdog within the absolute
    deadline+grace EXECUTION budget. Because its death can NOT be proven (no
    subprocess PID, no waitpid), it is NOT called a hard stop: it lands on the
    explicit ``quarantine_orphan_in_process`` stage with ``hard_stopped`` False.
    Registry-facing side effects stop growing, the record is quarantined
    NON-terminal (never a fabricated FAILED/CANCELLED), the disk keeps the
    original durable facts, and a same-key request fails closed."""
    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    side_effects = 0
    rejected_reports = 0

    async def stubborn_operation(report: Any) -> dict[str, Any]:
        nonlocal side_effects, rejected_reports
        while not stop.is_set():
            try:
                await report("working", 5)
                side_effects += 1
            except asyncio.CancelledError:
                pass
            except Exception:
                rejected_reports += 1
            await asyncio.sleep(0.001)
        return {"ok": True}

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        cancel_wait_seconds=0.02,
        execution_hard_stop_grace_seconds=0.1,
    )
    runtime: Any = None
    try:
        started = asyncio.get_running_loop().time()
        snapshot, replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn_operation,
            idempotency_key="hard-stop-permanent",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.15,
        )
        assert replayed is False
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        # Every deadline-intent / quarantine persist fails pre-commit (permanent
        # write failure).
        def fail_all_persists() -> None:
            raise RuntimeError("injected permanent persist failure")

        monkeypatch.setattr(registry, "_persist_locked", fail_all_persists)

        for _ in range(400):
            if runtime.quarantined:
                break
            await asyncio.sleep(0.005)
        assert runtime.quarantined is True
        elapsed = asyncio.get_running_loop().time() - started
        # The absolute EXECUTION bound (deadline + grace) was respected — the
        # executor is isolated within a bounded window, never left ungoverned.
        assert elapsed <= 0.15 + 0.1 + 2.0

        # This in-process operation does NOT protect its trailing sleep from
        # cancellation, so it provably stops when cancelled — the bounded
        # watchdog confirms its death and labels it a real hard stop. (Only an
        # executor that provably survives cancellation is an orphan stage.)
        assert runtime.hard_stopped is True
        assert runtime.quarantine_stage == _QUARANTINE_HARD_STOPPED_STAGE
        assert runtime.snapshot.cancel_pending is True
        assert runtime.snapshot.cancellation_requested is True
        # No terminal label is ever fabricated from the memory-only intent.
        assert runtime.snapshot.state not in (
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        )

        # Registry-facing side effects froze: the generation bump rejects every
        # further progress report from the still-alive (swallowed-cancel)
        # operation, and the rejections are observable.
        frozen_side_effects = side_effects
        await asyncio.sleep(0.2)
        assert side_effects == frozen_side_effects
        assert rejected_reports > 0

        # The disk still holds the original durable facts (the pre-attack QUEUED
        # record) — no fake terminal, no half-persisted quarantine.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"]["state"] == "queued"
        assert disk_record["snapshot"]["stage"] == "queued"

        # Same-key fails closed; the key is never reused.
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=stubborn_operation,
                idempotency_key="hard-stop-permanent",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
    finally:
        monkeypatch.undo()
        await _hard_teardown_registry(registry, stop, runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_hard_stop_reconciles_quarantine_on_store_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / hard-stop gate (12e35d45 门 1): when the store
    recovers, the bounded cleanup reconcile auto-commits the quarantine facts
    (and the in-memory target facts) durably BEFORE any quota is released — the
    disk record gains the explicit ``quarantine_orphan_in_process`` marker (the
    in-process executor is NOT provably dead, so it is never called a hard stop)
    and stays NON-terminal, and the same-key path remains fail-closed."""
    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()
    fail_writes = True

    async def stubborn_operation(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.001)
        return {"ok": True}

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        cancel_wait_seconds=0.02,
        execution_hard_stop_grace_seconds=0.1,
    )
    runtime: Any = None
    try:
        snapshot, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn_operation,
            idempotency_key="hard-stop-reconcile",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.15,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        real_persist = registry._persist_locked

        def recoverable_fail() -> None:
            if fail_writes:
                raise RuntimeError("injected recoverable persist failure")
            real_persist()

        monkeypatch.setattr(registry, "_persist_locked", recoverable_fail)

        for _ in range(400):
            if runtime.quarantined:
                break
            await asyncio.sleep(0.005)
        assert runtime.quarantined is True
        assert runtime.hard_stopped is False
        assert runtime.quarantine_reconciled is False

        # The quarantine is still in-memory only — the disk never gained it.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["quarantined"] is False
        assert disk_record["quarantine_stage"] is None
        assert disk_record["snapshot"]["stage"] == "queued"

        # Store recovers: the bounded reconcile auto-commits the quarantine +
        # in-memory target facts durably.
        fail_writes = False
        for _ in range(1000):
            if runtime.quarantine_reconciled:
                break
            await asyncio.sleep(0.005)
        assert runtime.quarantine_reconciled is True

        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["quarantined"] is True
        assert disk_record["quarantine_stage"] == _QUARANTINE_ORPHAN_STAGE
        assert disk_record["snapshot"]["stage"] == _QUARANTINE_ORPHAN_STAGE
        assert disk_record["snapshot"]["state"] == "running"

        # The record stays quarantined NON-terminal and the same-key path fails
        # closed even after the durable quarantine commit.
        assert runtime.snapshot.state not in (
            LivePlanningJobState.SUCCEEDED,
            LivePlanningJobState.FAILED,
            LivePlanningJobState.CANCELLED,
        )
        with pytest.raises(LivePlanningJobIdempotencyConflictError):
            await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=stubborn_operation,
                idempotency_key="hard-stop-reconcile",
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )

        # Cleanup: once the operation provably stops, the owner settles to the
        # DURABLE FAILED/deadline_exceeded intent (never a guessed label).
        stop.set()
        for _ in range(1000):
            if runtime.snapshot.state == LivePlanningJobState.FAILED:
                break
            await asyncio.sleep(0.005)
        assert runtime.snapshot.state == LivePlanningJobState.FAILED
        assert runtime.snapshot.stage == "deadline_exceeded"
    finally:
        monkeypatch.undo()
        await _hard_teardown_registry(registry, stop, runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_hard_stopped_unrecovered_restart_two_cold_boots_no_fake_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / hard-stop gate (12e35d45 门 1): if the process
    restarts before the hard-stop quarantine ever reached the store, no terminal
    label is fabricated from the memory-only intent. The IN-PROCESS executor is
    never called a hard stop (its death is unprovable), so the record is the
    orphan stage in memory only. Only the DURABLE deadline provenance
    (``deadline_at`` at creation) may recover the record —
    FAILED/deadline_exceeded once the deadline provably passed — never
    restart_cancelled, never a guessed CANCELLED. A second cold boot reads the
    same durable facts."""
    state_path = tmp_path / "live-jobs.json"
    stop = asyncio.Event()

    async def stubborn_operation(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.001)
        return {"ok": True}

    registry = LivePlanningJobRegistry(
        state_path=state_path,
        cancel_wait_seconds=0.02,
        execution_hard_stop_grace_seconds=0.1,
    )
    runtime: Any = None
    try:
        snapshot, _replayed = await registry.start_idempotent(
            tenant_id="tenant-a",
            operation=stubborn_operation,
            idempotency_key="hard-stop-cold-boot",
            request_digest=REQUEST_SHA256,
            defer_start=False,
            deadline_seconds=0.15,
        )
        runtime = registry._records[snapshot.id]
        await _wait_for_state(registry, snapshot.id, "tenant-a", LivePlanningJobState.RUNNING)

        def fail_all_persists() -> None:
            raise RuntimeError("injected permanent persist failure")

        monkeypatch.setattr(registry, "_persist_locked", fail_all_persists)
        for _ in range(400):
            if runtime.quarantined:
                break
            await asyncio.sleep(0.005)
        assert runtime.quarantined is True
        assert runtime.hard_stopped is False
        # The quarantine was NEVER durable — the disk keeps the original facts.
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        disk_record = next(
            record for record in disk["records"] if record["snapshot"]["id"] == snapshot.id
        )
        assert disk_record["snapshot"]["state"] == "queued"
    finally:
        monkeypatch.undo()
        await _hard_teardown_registry(registry, stop, runtime)
        await registry.close()

    # Unrecovered restart: use a deterministic clock strictly after the
    # durable deadline.  This tests the cold-boot rule itself, not scheduler or
    # wall-clock timing around a 150ms deadline.
    def expired_now() -> datetime:
        return snapshot.deadline_at + timedelta(seconds=1)

    first = LivePlanningJobRegistry(state_path=state_path, now=expired_now)
    try:
        recovered = await first.get(snapshot.id, "tenant-a")
        assert recovered is not None
        assert recovered.state == LivePlanningJobState.FAILED
        assert recovered.stage == "deadline_exceeded"
        assert recovered.cancellation_requested is True
        # Never a guessed cancel / restart label.
        assert recovered.stage != "restart_cancelled"
    finally:
        await first.close()

    # A second cold boot reads exactly the same durable facts — no drift.
    second = LivePlanningJobRegistry(state_path=state_path, now=expired_now)
    try:
        again = await second.get(snapshot.id, "tenant-a")
        assert again is not None
        assert again.state == LivePlanningJobState.FAILED
        assert again.stage == "deadline_exceeded"
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_permanent_failure_attacks_all_within_hard_caps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) counter-example (d): N permanent-failure
    attacks each end quarantined NON-terminal within the bounded quotas — no fake
    terminal is ever written, the number of simultaneously-alive real operations
    stays bounded by max_running, the quarantine count stays within its own
    bounded capacity, and the hard-stop watchdog remains a single task."""
    state_path = tmp_path / "live-jobs.json"
    registry = LivePlanningJobRegistry(
        state_path=state_path,
        capacity=32,
        max_running=1,
        cancel_wait_seconds=0.02,
        execution_hard_stop_grace_seconds=0.1,
        quarantine_capacity=8,
        intent_persist_wallclock_budget_seconds=0.3,
    )
    stop = asyncio.Event()

    async def stubborn_operation(_: Any) -> dict[str, Any]:
        while not stop.is_set():
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0.001)
        return {"ok": True}

    runtimes: list[Any] = []
    try:
        # All starts succeed (future deadlines, no isolation yet); then a
        # permanent write failure hits every later persist.
        for attack in range(4):
            snap, _replayed = await registry.start_idempotent(
                tenant_id="tenant-a",
                operation=stubborn_operation,
                idempotency_key=f"attack-{attack}",
                request_digest=REQUEST_SHA256,
                defer_start=False,
                deadline_seconds=0.3,
            )
            runtimes.append(registry._records[snap.id])

        def fail_all_persists() -> None:
            raise RuntimeError("injected permanent persist failure")

        monkeypatch.setattr(registry, "_persist_locked", fail_all_persists)

        alive_peaks: list[int] = []
        for runtime in runtimes:
            for _ in range(600):
                if runtime.quarantined:
                    break
                await asyncio.sleep(0.005)
            assert runtime.quarantined is True
            alive_peaks.append(
                sum(
                    1
                    for item in registry._records.values()
                    if item.operation_task is not None and not item.operation_task.done()
                )
            )

        quarantined = [item for item in registry._records.values() if item.quarantined]
        assert len(quarantined) == 4
        assert len(quarantined) <= registry._quarantine_capacity
        for runtime in quarantined:
            assert runtime.snapshot.state not in (
                LivePlanningJobState.SUCCEEDED,
                LivePlanningJobState.FAILED,
                LivePlanningJobState.CANCELLED,
            )
        # Attack 0 held the sole admission slot, so only it was a live executor
        # and was stopped by the bounded watchdog; the queued attacks hit the
        # bounded STATE budget and are quarantined intent-uncommitted. Attack 0's
        # executor is an IN-PROCESS coroutine that swallows CancelledError, so
        # its death can never be proven — it is honestly quarantined as an
        # in-process orphan, never labeled a hard stop / never a fake terminal.
        # The deadline-path quarantine lands first; the watchdog's death-confirm
        # budget (hard_stop_confirm_seconds) runs past it and overwrites the
        # stage with the final orphan decision. Wait for that budget to elapse
        # before asserting the honest non-terminal stage.
        confirm_deadline = (
            runtimes[0].hard_stop_monotonic
            + registry._hard_stop_confirm_seconds
            + 0.5
        )
        while asyncio.get_running_loop().time() < confirm_deadline:
            await asyncio.sleep(0.01)
        assert runtimes[0].quarantine_stage == _QUARANTINE_ORPHAN_STAGE
        assert runtimes[0].hard_stopped is False
        for runtime in runtimes[1:]:
            assert runtime.quarantine_stage == _QUARANTINE_INTENT_UNCOMMITTED_STAGE
        # The admission permit is conserved: at most max_running real operations
        # are ever alive at once — no orphan permit accumulation.
        assert max(alive_peaks) == 1
        # The watchdog is a single task (or already self-terminated).
        watchdog = registry._hard_stop_watchdog
        assert watchdog is None or not watchdog.done()
        # The disk never gained a fake terminal — every record is still the
        # original QUEUED fact (no persist ever succeeded after the starts).
        disk = json.loads(state_path.read_text(encoding="utf-8"))
        for record in disk["records"]:
            assert record["snapshot"]["state"] == "queued"
    finally:
        monkeypatch.undo()
        for runtime in runtimes:
            await _hard_teardown_registry(registry, stop, runtime)
        await registry.close()


@pytest.mark.asyncio
async def test_quarantine_records_never_occupy_active_capacity(
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / b119 gate (P0-B): bounded quarantine/
    tombstone records do NOT occupy executable ACTIVE capacity — a legal file
    may hold ``capacity`` ordinary records PLUS ``quarantine_capacity``
    quarantined ones, each quota enforced independently, and a new key is still
    admitted while the quarantine stays under its own quota. RED on HEAD: the
    loader rejected ``len(records) > capacity`` and admission counted quarantined
    records against the active set."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    active_id = "live-job-terminal-active"
    q1 = "live-job-q1"
    q2 = "live-job-q2"
    partition = LivePlanningJobRegistry._tenant_partition(tenant_id)

    active_snap = _v3_snapshot(active_id, LivePlanningJobState.SUCCEEDED, "complete", 100, 3)
    q1_snap = _v3_snapshot(q1, LivePlanningJobState.RUNNING, _QUARANTINE_HARD_STOPPED_STAGE, 5, 2)
    q2_snap = _v3_snapshot(q2, LivePlanningJobState.RUNNING, _ISOLATED_AMBIGUOUS_CANCEL_STAGE, 5, 2)
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": partition,
                "snapshot": active_snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
                "quarantined": False,
                "quarantine_stage": None,
            },
            {
                "tenant_partition": partition,
                "snapshot": q1_snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
                "quarantined": True,
                "quarantine_stage": _QUARANTINE_HARD_STOPPED_STAGE,
            },
            {
                "tenant_partition": partition,
                "snapshot": q2_snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
                "quarantined": True,
                "quarantine_stage": _ISOLATED_AMBIGUOUS_CANCEL_STAGE,
            },
        ],
        "idempotency": [
            _v3_idempotency_entry(tenant_id, "terminal-active", active_id),
            _v3_idempotency_entry(tenant_id, "q1", q1),
            _v3_idempotency_entry(tenant_id, "q2", q2),
        ],
    }
    _write_registry_state(payload, state_path)

    registry = LivePlanningJobRegistry(state_path=state_path, capacity=1, quarantine_capacity=3)
    try:
        # Both quotas load independently: active=1 <= capacity and quarantined=2
        # <= quarantine_capacity — the two quarantined records never count
        # against the executable active capacity.
        assert registry._records[active_id].snapshot.state == LivePlanningJobState.SUCCEEDED
        assert registry._records[q1].quarantined is True
        assert registry._records[q2].quarantined is True

        # A NEW key is admitted: the quarantined records do not occupy ACTIVE
        # capacity, so the terminal SUCCEEDED record is evicted (not the
        # quarantine) to make room.
        async def operation(_: Any) -> dict[str, Any]:
            return {"ok": True}

        fresh, replayed = await registry.start_idempotent(
            tenant_id=tenant_id,
            operation=operation,
            idempotency_key="fresh-key",
            request_digest=REQUEST_SHA256,
            defer_start=True,
        )
        assert replayed is False
        assert registry._records[fresh.id] is not None
        # The two quarantined records are untouched.
        assert registry._records[q1].quarantined is True
        assert registry._records[q2].quarantined is True
        assert active_id not in registry._records
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_reclaimed_quarantine_tombstone_same_key_fails_closed(
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / b119 gate (P0-B): after a quarantined record
    passes its bounded retention AND its executor is provably dead, reclamation
    keeps a minimal durable tombstone (the legacy_isolated idempotency binding)
    so a same-key request always fails closed — the key is never silently popped
    and reused."""
    state_path = tmp_path / "live-jobs.json"
    tenant_id = "tenant-a"
    job_id = "live-job-reclaimed-q"
    key = "reclaimed-q-key"
    aged = datetime.now(UTC) - timedelta(hours=7)
    snapshot = LivePlanningJobSnapshot(
        id=job_id,
        state=LivePlanningJobState.RUNNING,
        stage=_QUARANTINE_HARD_STOPPED_STAGE,
        progress=5,
        revision=2,
        cancellation_requested=True,
        cancel_pending=True,
        request_sha256=REQUEST_SHA256,
        model_trace_scope_sha256=REQUEST_SHA256,
        created_at=aged,
        updated_at=aged,
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )
    payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": snapshot.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": None,
                "quarantined": True,
                "quarantine_stage": _QUARANTINE_HARD_STOPPED_STAGE,
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, key, job_id)],
    }
    _write_registry_state(payload, state_path)

    registry = LivePlanningJobRegistry(state_path=state_path, capacity=4, quarantine_capacity=2)
    try:
        # The loader reclaimed the aged quarantined record at load (retention
        # passed and no executor survives a cold boot): a minimal tombstone
        # remains and the key is never reusable.
        assert job_id not in registry._records

        async def operation(_: Any) -> dict[str, Any]:
            return {"ok": True}

        with pytest.raises(LivePlanningJobIdempotencyConflictError, match="reclaimed or isolated"):
            await registry.start_idempotent(
                tenant_id=tenant_id,
                operation=operation,
                idempotency_key=key,
                request_digest=REQUEST_SHA256,
                defer_start=False,
            )
        # The tombstone survived the failed same-key attempt — never popped.
        assert job_id not in registry._records
        assert (
            registry._idempotency[
                LivePlanningJobRegistry._idempotency_partition(tenant_id, key)
            ].legacy_isolated
            is True
        )
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_legacy_v3_none_cancellation_real_old_producer_migrates_atomic(
    tmp_path: Path,
) -> None:
    """C-146 P0 supplement (P0-4) / b119 gate (P0-A): the loader's
    ``legacy_v3_none_cancellation`` branch is REAL and reachable — it is
    exercised with checked-in, exact old-producer disk fixtures rather than an
    unreachable Git object.
    The exact old cancel stuck shape (``cancellation_requested: null`` at
    ``cancel_timed_out``, no quarantine marker) is accepted ONLY via the legacy
    discriminator, converted to the explicit True semantics, atomically
    rewritten to new-v3, and stable across two cold boots. The deadline old stuck
    shape (``timeout_pending``) migrates to FAILED/deadline_exceeded. A
    new-schema ``None`` (quarantine marker present) stays rejected fail-closed."""
    tenant_id = "tenant-a"

    def assert_old_v3_shape(
        payload: dict[str, Any],
        *,
        job_id: str,
        stage: str,
        revision: int,
        snapshot_error: str | None,
        pending_state: str,
        pending_stage: str,
        pending_error: str | None,
        pending_cancellation_requested: bool | None,
    ) -> None:
        assert payload["schema_version"] == "tripchord-live-job-registry-v3"
        assert len(payload["records"]) == 1
        assert len(payload["idempotency"]) == 1
        record = payload["records"][0]
        assert set(record) == {
            "activation_operation",
            "pending_terminal",
            "prepared",
            "snapshot",
            "tenant_partition",
        }
        assert record["tenant_partition"] == LivePlanningJobRegistry._tenant_partition(tenant_id)
        assert record["prepared"] is False
        assert record["activation_operation"] is None
        snapshot = record["snapshot"]
        assert set(snapshot) == {
            "barrier_released_at",
            "boundary",
            "cancel_pending",
            "cancellation_requested",
            "created_at",
            "deadline_at",
            "error",
            "expires_at",
            "id",
            "model_trace_count",
            "model_trace_failure_count",
            "model_trace_scope_sha256",
            "model_trace_success_count",
            "pair_checkpoints",
            "progress",
            "request_sha256",
            "result",
            "revision",
            "safe_failure_code",
            "safe_failure_details",
            "safe_failure_details_digest",
            "source_terminal_events",
            "stage",
            "state",
            "updated_at",
        }
        assert snapshot["id"] == job_id
        assert snapshot["state"] == "running"
        assert snapshot["stage"] == stage
        assert snapshot["progress"] == 5
        assert snapshot["revision"] == revision
        assert snapshot["cancellation_requested"] is True
        assert snapshot["cancel_pending"] is True
        assert snapshot["error"] == snapshot_error
        assert snapshot["result"] is None
        assert snapshot["safe_failure_code"] is None
        assert snapshot["safe_failure_details"] is None
        assert snapshot["safe_failure_details_digest"] is None
        assert snapshot["model_trace_count"] == 0
        assert snapshot["model_trace_success_count"] == 0
        assert snapshot["model_trace_failure_count"] == 0
        assert snapshot["pair_checkpoints"] == []
        assert snapshot["source_terminal_events"] == []
        assert snapshot["barrier_released_at"] is None
        assert snapshot["expires_at"] is None
        assert snapshot["boundary"] == (
            "本机进程内、单服务进程的长任务控制面；最多并行执行有限个任务，"
            "终态按容量和 TTL 有界保存。进程重启不恢复任务，不能视为持久化生产队列。"
        )
        assert snapshot["request_sha256"] == REQUEST_SHA256
        assert snapshot["model_trace_scope_sha256"] == REQUEST_SHA256
        pending = record["pending_terminal"]
        assert set(pending) == {
            "cancellation_requested",
            "error",
            "result",
            "safe_failure_code",
            "safe_failure_details",
            "safe_failure_details_digest",
            "stage",
            "state",
        }
        assert pending["state"] == pending_state
        assert pending["stage"] == pending_stage
        assert pending["error"] == pending_error
        assert pending["result"] is None
        assert pending["cancellation_requested"] is pending_cancellation_requested
        if pending_state == "failed":
            assert pending["safe_failure_code"] == "deadline_exceeded"
            assert pending["safe_failure_details"] == {
                "exception_class": "TimeoutError",
                "message_sha256": None,
                "validation_errors": [],
                "validation_model": None,
            }
            assert (
                pending["safe_failure_details_digest"]
                == "526ada13001d3e22d8bc548852098af7b5eaa822e7f13b6a3f712ce3d8afc096"
            )
        else:
            assert pending["safe_failure_code"] is None
            assert pending["safe_failure_details"] is None
            assert pending["safe_failure_details_digest"] is None
        binding = payload["idempotency"][0]
        assert set(binding) == {
            "defer_start",
            "job_id",
            "legacy_isolated",
            "partition",
            "request_digest",
        }
        assert binding["job_id"] == job_id
        assert binding["request_digest"] == REQUEST_SHA256
        assert binding["defer_start"] is False
        assert binding["legacy_isolated"] is False

    # -- Old-producer cancel-stuck shape (cancellation_requested null) ---------
    cancel_upgrade_path = tmp_path / "old-cancel-upgrade.json"
    fixture_root = Path(__file__).parent / "fixtures" / "live_jobs"
    cancel_upgrade_path.write_bytes(
        (fixture_root / "legacy-v3-cancel-timed-out.json").read_bytes()
    )
    cancel_upgrade_path.chmod(0o600)

    old_disk = json.loads(cancel_upgrade_path.read_text(encoding="utf-8"))
    assert_old_v3_shape(
        old_disk,
        job_id="legacy-v3-cancel",
        stage="cancel_timed_out",
        revision=3,
        snapshot_error=(
            "live planning operation did not stop within the bounded cancellation "
            "budget; the job stays non-terminal and the operation is isolated"
        ),
        pending_state="cancelled",
        pending_stage="cancelled",
        pending_error=None,
        pending_cancellation_requested=None,
    )
    old_record = next(
        record for record in old_disk["records"] if record["snapshot"]["id"] == "legacy-v3-cancel"
    )
    snap_id = old_record["snapshot"]["id"]
    assert old_record["snapshot"]["stage"] == "cancel_timed_out"
    assert old_record["snapshot"]["cancel_pending"] is True
    assert old_record["pending_terminal"]["cancellation_requested"] is None
    assert "quarantined" not in old_record

    # Upgrade load: the legacy branch accepts the historical None and the record
    # cold-starts to the DURABLE cancel intent (CANCELLED/cancelled).
    first = LivePlanningJobRegistry(state_path=cancel_upgrade_path, capacity=8)
    try:
        recovered = await first.get(snap_id, tenant_id)
        assert recovered is not None
        assert recovered.state == LivePlanningJobState.CANCELLED
        assert recovered.stage == "cancelled"
    finally:
        await first.close()

    # The file was atomically rewritten to new-v3 — terminal facts, no null.
    migrated_disk = json.loads(cancel_upgrade_path.read_text(encoding="utf-8"))
    migrated_record = next(
        record for record in migrated_disk["records"] if record["snapshot"]["id"] == snap_id
    )
    assert migrated_record["snapshot"]["state"] == "cancelled"
    assert migrated_record["snapshot"]["stage"] == "cancelled"
    assert migrated_record["pending_terminal"] is None

    # A second cold boot reads the same terminal facts — no drift.
    second = LivePlanningJobRegistry(state_path=cancel_upgrade_path, capacity=8)
    try:
        again = await second.get(snap_id, tenant_id)
        assert again is not None
        assert again.state == LivePlanningJobState.CANCELLED
        assert again.stage == "cancelled"
    finally:
        await second.close()

    # -- Old-producer deadline-stuck shape (timeout_pending, True) -------------
    deadline_upgrade_path = tmp_path / "old-deadline-upgrade.json"
    deadline_upgrade_path.write_bytes(
        (fixture_root / "legacy-v3-timeout-pending.json").read_bytes()
    )
    deadline_upgrade_path.chmod(0o600)

    old_deadline_disk = json.loads(deadline_upgrade_path.read_text(encoding="utf-8"))
    assert_old_v3_shape(
        old_deadline_disk,
        job_id="legacy-v3-deadline",
        stage="timeout_pending",
        revision=2,
        snapshot_error=None,
        pending_state="failed",
        pending_stage="deadline_exceeded",
        pending_error="TimeoutError: live planning job deadline exceeded",
        pending_cancellation_requested=True,
    )
    old_deadline_record = next(
        record
        for record in old_deadline_disk["records"]
        if record["snapshot"]["id"] == "legacy-v3-deadline"
    )
    snap2_id = old_deadline_record["snapshot"]["id"]
    assert old_deadline_record["snapshot"]["stage"] == "timeout_pending"
    assert old_deadline_record["snapshot"]["cancel_pending"] is True
    assert old_deadline_record["pending_terminal"]["cancellation_requested"] is True
    assert "quarantined" not in old_deadline_record

    first_dl = LivePlanningJobRegistry(state_path=deadline_upgrade_path, capacity=8)
    try:
        recovered_dl = await first_dl.get(snap2_id, tenant_id)
        assert recovered_dl is not None
        assert recovered_dl.state == LivePlanningJobState.FAILED
        assert recovered_dl.stage == "deadline_exceeded"
    finally:
        await first_dl.close()

    second_dl = LivePlanningJobRegistry(state_path=deadline_upgrade_path, capacity=8)
    try:
        again_dl = await second_dl.get(snap2_id, tenant_id)
        assert again_dl is not None
        assert again_dl.state == LivePlanningJobState.FAILED
        assert again_dl.stage == "deadline_exceeded"
    finally:
        await second_dl.close()

    # -- Negative: new-schema None (quarantine marker present) is rejected -----
    new_schema_path = tmp_path / "new-schema-null.json"
    new_snap = _v3_snapshot(
        "live-job-new-null",
        LivePlanningJobState.RUNNING,
        "cancel_timed_out",
        5,
        2,
        cancellation_requested=True,
        cancel_pending=True,
    )
    new_payload = {
        "schema_version": "tripchord-live-job-registry-v3",
        "records": [
            {
                "tenant_partition": LivePlanningJobRegistry._tenant_partition(tenant_id),
                "snapshot": new_snap.model_dump(mode="json"),
                "prepared": False,
                "activation_operation": None,
                "pending_terminal": _pending_outcome(cancellation_requested=None),
                "quarantined": False,
                "quarantine_stage": None,
            }
        ],
        "idempotency": [_v3_idempotency_entry(tenant_id, "new-null", "live-job-new-null")],
    }
    _write_registry_state(new_payload, new_schema_path)
    with pytest.raises(RuntimeError, match="pending terminal is invalid"):
        LivePlanningJobRegistry(state_path=new_schema_path, capacity=4)
