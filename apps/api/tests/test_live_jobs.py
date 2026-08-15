from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from tripchord.agents.live_jobs import (
    LivePlanningJobCapacityError,
    LivePlanningJobIdempotencyConflictError,
    LivePlanningJobInactiveError,
    LivePlanningJobRegistry,
    LivePlanningJobRegistryPostCommitError,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
    LiveSourceTerminalEvent,
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
        "departure_date": date(2026, 8, sequence),
        "return_date": date(2026, 8, sequence + 5),
        "state": state,
        "query_task_ids": tuple(f"source-{sequence}-{index}" for index in range(11)),
        "captured_at": datetime(2026, 8, 4, 8, sequence, tzinfo=UTC),
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
async def test_pair_checkpoints_accumulate_and_survive_terminal_failure_with_tenant_isolation(
) -> None:
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
        await report.report_pair_checkpoint(
            _pair_checkpoint(1, request_sha256="b" * 64)
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
    assert failed.request_sha256 == REQUEST_SHA256
    assert failed.pair_checkpoints == ()
    assert failed.error == "ValueError: live planning execution failed"
    assert failed.safe_failure_code == "domain_value_error"
    assert failed.safe_failure_details is not None
    assert failed.safe_failure_details.exception_class == "ValueError"
    assert failed.safe_failure_details.validation_model is None
    assert failed.safe_failure_details.message_sha256 == hashlib.sha256(
        b"live pair checkpoint request SHA-256 does not match its job"
    ).hexdigest()
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
        len(item.message_sha256) == 64
        for item in failed.safe_failure_details.validation_errors
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
    assert failed.safe_failure_details.message_sha256 == hashlib.sha256(
        sensitive_message.encode("utf-8")
    ).hexdigest()
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
    # terminalized and the activation operation fails closed instead of
    # replaying the old QUEUED receipt, and it is never re-dispatched.
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
        record
        for record in disk_payload["records"]
        if record["snapshot"]["id"] == prepared.id
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
        record
        for record in disk_payload["records"]
        if record["snapshot"]["id"] == prepared.id
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
