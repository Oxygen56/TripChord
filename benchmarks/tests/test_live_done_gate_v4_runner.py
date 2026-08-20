from __future__ import annotations

import asyncio
import copy
import json
import os
import stat
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter
from tripchord.agents.live_jobs import LivePlanningPairCheckpoint
from tripchord.agents.live_system import (
    LiveDataProvider,
    LiveEvidenceScope,
    LivePackageEvent,
)
from tripchord.planning.event_contracts import EventDisposition
from tripchord.planning.offer_semantics import (
    OfferIdentityConfidence,
    OfferSemanticChange,
)
from tripchord.planning.package import (
    PackageDecisionState,
    PackageEventKind,
    PackageVerificationPhase,
    QuoteAvailability,
)
from tripchord.planning.stay_plans import system_stay_plan_candidate_set
from tripchord.runtime_provenance import local_expected_provenance

from benchmarks import run_live_done_gate_v4
from scripts.browser_companion_release_gate import verify_ci_candidate_build_metadata

SCENARIO = Path(__file__).parents[1] / "scenarios" / "live-hgh-mle-aug-2026-v4.json"
_FIXTURE_CONTROL_PATH = (
    Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    / "tripchord-fixture-formal-source"
    / "control-token"
)


def _request() -> dict[str, Any]:
    return TypeAdapter(dict[str, Any]).validate_python(
        json.loads(SCENARIO.read_text(encoding="utf-8"))
    )


def _runtime_payload(*, model_trace_count: int = 7) -> dict[str, Any]:
    return {
        "codex_runtime_dependency": False,
        "chatgpt_runtime_dependency": False,
        "model_enabled": True,
        "model_required": True,
        "model_provider": "openai_compatible",
        "primary_model": "deepseek-v4-flash",
        "fast_model": "deepseek-v4-flash",
        "model_trace_count": model_trace_count,
        "effective_flexible_timeout_seconds": 600,
        "rag_enabled": True,
        "runtime_provenance": _runtime_provenance_payload(),
        "formal_live_source": {
            "fixture_anchor_available": True,
            "control_token_path": str(_FIXTURE_CONTROL_PATH),
        },
        "worker_model_runtime": None,
    }


def _verified_test_companion_build_identity() -> dict[str, Any]:
    return verify_ci_candidate_build_metadata(
        Path(__file__).resolve().parents[2]
    ).model_dump(mode="json")


def _companion_preflight_payload(
    *,
    build_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verified_build = (
        build_identity
        if build_identity is not None
        else _verified_test_companion_build_identity()
    )
    return {
        "status": "connected",
        "server_time": "2026-08-15T00:00:00+00:00",
        "stale_after_seconds": 45,
        "companions": [
            {
                "companion_id": "formal-companion-v1",
                "providers": ["ctrip", "qunar", "tongcheng"],
                "authorized_scope_keys": [
                    "ctrip:flight",
                    "ctrip:lodging",
                    "qunar:flight",
                    "qunar:lodging",
                    "tongcheng:flight",
                ],
                "adapter_version": "0.2.0",
                "contract_version": "tripchord-capability-v1",
                "runtime_instance_id": "formal-companion-runtime-v1",
                "build_identity": verified_build,
                "last_seen": "2026-08-15T00:00:00+00:00",
                "age_seconds": 0.0,
                "is_fresh": True,
            }
        ],
    }


def _install_failed_run_formal_control_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued: dict[str, object] = {}

    async def issue(
        _client: object,
        _base: str,
        context: dict[str, object],
        _idempotency_key: str,
        _control_path: Path | None = None,
    ) -> dict[str, object]:
        challenge = {
            **context,
            "challenge_id": "fixture-formal-challenge",
            "run_id": context["run_id"],
        }
        issued["challenge"] = challenge
        return {
            "challenge": challenge,
            "execution_capability": {
                "capability_id": "fixture-formal-execution-capability"
            },
        }

    async def activate(*_: object, **__: object) -> None:
        return None

    async def finalize(*_: object, **__: object) -> dict[str, object]:
        return {
            "binding": {"fixture": "failed-run-formal-binding"},
            "authority_receipt": {"fixture": "failed-run-formal-receipt"},
            "challenge": dict(issued["challenge"]),
        }

    async def abort(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_formal_source_control_token",
        lambda _path=None: "fixture-formal-control-token-" + "F" * 64,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_issue_formal_source_challenge_remote",
        issue,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_activate_prepared_flexible_live_job",
        activate,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_finalize_formal_source_binding_remote",
        finalize,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_abort_formal_source_challenge_remote",
        abort,
    )


def test_v4_arguments_defer_formal_control_path_to_certified_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_live_done_gate_v4.py"])

    args = run_live_done_gate_v4._arguments()

    assert args.formal_source_control_token_file is None


def test_v4_formal_control_path_must_be_canonical_and_external(
    tmp_path: Path,
) -> None:
    control_path = tmp_path / "formal-source" / "control-token"
    assert run_live_done_gate_v4._formal_source_control_path_from_runtime(
        {"control_token_path": str(control_path)}
    ) == control_path

    with pytest.raises(RuntimeError, match="outside the repository"):
        run_live_done_gate_v4._formal_source_control_path_from_runtime(
            {
                "control_token_path": str(
                    Path(__file__).resolve().parents[2]
                    / ".runtime"
                    / "formal-source"
                    / "control-token"
                )
            }
        )
    with pytest.raises(RuntimeError, match="not canonical"):
        run_live_done_gate_v4._formal_source_control_path_from_runtime(
            {"control_token_path": str(tmp_path / "foreign-name")}
        )


def test_v4_invalid_issued_challenge_is_aborted_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local verifier failure cannot strand an authority lease."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/challenge"):
            return httpx.Response(
                200,
                json={
                    "challenge": {
                        "challenge_id": "issued-challenge-id",
                        "run_id": "issued-run-id",
                    },
                    "execution_capability": {"capability_id": "issued-capability"},
                },
            )
        if request.url.path.endswith("/abort"):
            body = json.loads(request.content)
            assert body["challenge_id"] == "issued-challenge-id"
            assert body["run_id"] == "issued-run-id"
            return httpx.Response(200, json={"aborted": True})
        raise AssertionError(f"unexpected request {request.url}")

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_formal_source_control_token",
        lambda _path=None: "control-token-" + "c" * 64,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "validate_formal_source_challenge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("foreign verification anchor")
        ),
    )
    control_path = tmp_path / "formal-source" / "control-token"

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ValueError, match="foreign verification anchor"):
                await run_live_done_gate_v4._issue_formal_source_challenge_remote(
                    client,
                    "http://tripchord.test",
                    {"run_id": "issued-run-id"},
                    "challenge-idempotency-key",
                    control_path,
                )

    asyncio.run(exercise())
    assert calls == [
        "/api/v1/internal/formal-live-source/challenge",
        "/api/v1/internal/formal-live-source/abort",
    ]


def _api_payload_sha256() -> str:
    return run_live_done_gate_v4._canonical_sha256(run_live_done_gate_v4._api_payload(_request()))


def _formal_worker_model_receipts(
    *,
    trace_count: int,
    job_id: str = "live-job-fixture",
    request_sha256: str | None = None,
) -> dict[str, object]:
    """Exact unit-fixture receipts for runner boundary tests only."""

    request_digest = request_sha256 or _api_payload_sha256()
    runtime = _runtime_provenance_payload()
    model_identity = {
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "primary_model": "gpt-oss:20b",
        "fast_model": "gpt-oss:20b",
    }
    spec_sha256 = "b" * 64
    worker_receipt: dict[str, object] = {
        "schema_version": "tripchord-live-worker-runtime-receipt-v1",
        "runtime": "browser-bridge",
        "providers": ["ctrip", "qunar", "tongcheng"],
        "spec_sha256": spec_sha256,
        "runtime_provenance": runtime,
        "api_runtime_identity_sha256": (
            run_live_done_gate_v4._canonical_sha256(runtime)
        ),
        "worker_runtime_identity": runtime,
        "model_agents_required": True,
        "model_runtime_identity": model_identity,
    }
    traces = [
        {
            "id": f"fixture-model-trace-{index:04d}",
            "provider": model_identity["provider"],
            "model": model_identity["primary_model"],
            "role": "context",
            "request_digest": f"{index + 1:064x}",
            "scope_id": job_id,
            "scope_request_digest": request_digest,
            "response_schema_requested": True,
            "tool_count": 0,
            "started_at": "2026-08-10T00:00:01+00:00",
            "finished_at": "2026-08-10T00:00:02+00:00",
            "success": True,
            "usage": {"input_tokens": 8, "output_tokens": 4},
            "estimated_cost_usd": 0.0,
            "error_class": None,
        }
        for index in range(trace_count)
    ]
    unsigned_model_receipt: dict[str, object] = {
        "schema_version": "tripchord-model-execution-receipt-v1",
        "job_id": job_id,
        "request_sha256": request_digest,
        "runtime_bundle_spec_sha256": spec_sha256,
        "worker_runtime_identity_sha256": (
            run_live_done_gate_v4._canonical_sha256(runtime)
        ),
        "model_runtime_identity": model_identity,
        "trace_count": trace_count,
        "success_count": trace_count,
        "failure_count": 0,
        "traces": traces,
    }
    return {
        "worker_runtime_receipt": worker_receipt,
        "model_execution_receipt": {
            **unsigned_model_receipt,
            "receipt_sha256": run_live_done_gate_v4._canonical_sha256(
                unsigned_model_receipt
            ),
        },
    }


def _checkpoint(
    sequence: int,
    *,
    request_sha256: str | None = None,
    state: str = "completed",
    date_pair_id: str | None = None,
    departure_date: date | None = None,
    return_date: date | None = None,
    query_task_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    departure = departure_date or date(2026, 8, 10 + sequence)
    returned = return_date or date(2026, 8, 16 + sequence)
    pair_id = date_pair_id or f"date-pair:{departure}:{returned}:fixture-{sequence}"
    task_ids = query_task_ids or (f"query-task-{sequence}",)
    values: dict[str, Any] = {
        "sequence": sequence,
        "request_sha256": request_sha256 or _api_payload_sha256(),
        "date_pair_id": pair_id,
        "departure_date": departure,
        "return_date": returned,
        "state": state,
        "query_task_ids": task_ids,
        "captured_at": datetime(2026, 8, 4, 9, 0, sequence, tzinfo=UTC),
    }
    if state == "completed":
        values.update(
            {
                "run_purpose": "exploration_selection",
                "finalization_state": "exploration_sealed",
                "decision_state": "accept",
                "source_task_count": 13,
                "exploration_seal_passed": True,
                "all_platforms_complete": True,
            }
        )
    else:
        values["failure_class"] = "TimeoutError"
    return LivePlanningPairCheckpoint.create(**values).model_dump(mode="json")


def _three_checkpoints() -> list[dict[str, Any]]:
    return [_checkpoint(sequence) for sequence in range(1, 4)]


def _three_pair_runs() -> tuple[SimpleNamespace, ...]:
    pair_runs: list[SimpleNamespace] = []
    for checkpoint in _three_checkpoints():
        pair_runs.append(
            SimpleNamespace(
                date_pair=SimpleNamespace(
                    id=checkpoint["date_pair_id"],
                    departure_date=date.fromisoformat(checkpoint["departure_date"]),
                    return_date=date.fromisoformat(checkpoint["return_date"]),
                ),
                query_tasks=tuple(
                    SimpleNamespace(id=task_id) for task_id in checkpoint["query_task_ids"]
                ),
                state=SimpleNamespace(value=checkpoint["state"]),
            )
        )
    return tuple(pair_runs)


def _job_snapshot(
    *,
    state: str,
    revision: int,
    stage: str,
    progress: int,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    safe_failure_code: str | None = None,
    safe_failure_details: dict[str, Any] | None = None,
    safe_failure_details_digest: str | None = None,
    job_id: str = "live-job-fixture",
    request_sha256: str | None = None,
    pair_checkpoints: list[dict[str, Any]] | None = None,
    model_trace_success_count: int = 0,
    model_trace_failure_count: int = 0,
) -> dict[str, Any]:
    created_at = "2026-08-04T09:00:00+00:00"
    bound_sha256 = request_sha256 or _api_payload_sha256()
    return {
        "id": job_id,
        "state": state,
        "stage": stage,
        "progress": progress,
        "cancellation_requested": False,
        "revision": revision,
        "result": result,
        "error": error,
        "safe_failure_code": safe_failure_code,
        "safe_failure_details": safe_failure_details,
        "safe_failure_details_digest": safe_failure_details_digest,
        "request_sha256": bound_sha256,
        "model_trace_scope_sha256": bound_sha256,
        "model_trace_count": model_trace_success_count + model_trace_failure_count,
        "model_trace_success_count": model_trace_success_count,
        "model_trace_failure_count": model_trace_failure_count,
        "pair_checkpoints": pair_checkpoints or [],
        "created_at": created_at,
        "updated_at": f"2026-08-04T09:00:{revision:02d}+00:00",
        "deadline_at": "2026-08-04T10:05:00+00:00",
        "expires_at": None,
    }


def _started_job_payload(
    *,
    job_id: str = "live-job-fixture",
    replayed: bool = False,
) -> dict[str, Any]:
    status_url = "/api/v1/agents/live-flexible-plan-from-text/jobs/" + job_id
    return {
        "job": _job_snapshot(
            state="queued",
            revision=1,
            stage="queued",
            progress=0,
            job_id=job_id,
        ),
        "replayed": replayed,
        "status_url": status_url,
        "events_url": f"{status_url}/events",
    }


def _poll_control(*, job_id: str = "live-job-fixture") -> dict[str, Any]:
    request = _request()
    control = run_live_done_gate_v4._new_live_job_control(
        request,
        run_live_done_gate_v4._api_payload(request),
        client_wait_timeout_seconds=900.0,
        attempt_id="1" * 32,
    )
    started = run_live_done_gate_v4.StartLiveFlexibleFromTextJobResponse.model_validate(
        _started_job_payload(job_id=job_id)
    )
    control.update(
        {
            "job_id": job_id,
            "replayed": started.replayed,
            "status_url": started.status_url,
            "events_url": started.events_url,
        }
    )
    run_live_done_gate_v4._record_job_snapshot(control, started.job)
    return control


def test_v4_scenario_binds_the_canonical_candidate_set_before_search() -> None:
    request = _request()
    expected = system_stay_plan_candidate_set()

    run_live_done_gate_v4._validate_request_contract(request)
    payload = run_live_done_gate_v4._api_payload(request)

    assert payload["stay_plan_candidate_set"]["candidate_set_sha256"] == (
        expected.candidate_set_sha256
    )
    assert payload["coverage_mode"] == "strict"
    assert payload["max_pairs"] == 3
    assert payload["timeout_seconds"] == 120
    assert payload["total_timeout_seconds"] == 600
    assert payload["publication_refresh_minimum_options"] == 2
    assert request["done_gate_profile"] == {
        "maximum_quote_age_minutes": 15,
        "minimum_recommendable_options": 2,
    }
    assert "stay_plan_profile" not in payload
    assert "done_gate_profile" not in payload


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("coverage_mode",), "degraded"),
        (("max_pairs",), 2),
        (("timeout_seconds",), 60),
        (("total_timeout_seconds",), 1200),
        (("done_gate_profile", "maximum_quote_age_minutes"), 60),
        (("done_gate_profile", "minimum_recommendable_options"), 1),
        (("stay_plan_profile", "scan_limit_per_platform"), 50),
        (
            (
                "stay_plan_profile",
                "minimum_exact_providers_per_selected_segment",
            ),
            1,
        ),
        (
            (
                "stay_plan_profile",
                "minimum_exact_providers_per_selected_segment",
            ),
            3,
        ),
        (
            ("stay_plan_profile", "expected_candidate_set_sha256"),
            "0" * 64,
        ),
        (
            ("stay_plan_profile", "required_stay_plan_ids"),
            ["hulhumale_continuous", "maafushi_icom"],
        ),
    ],
)
def test_v4_scenario_tampering_fails_before_browser_search(
    path: tuple[str, ...],
    value: object,
) -> None:
    request = copy.deepcopy(_request())
    current = request
    for field in path[:-1]:
        current = current[field]
    current[path[-1]] = value

    with pytest.raises(RuntimeError, match="before search"):
        run_live_done_gate_v4._validate_request_contract(request)


@pytest.mark.parametrize(
    ("maximum_quote_age_minutes", "minimum_recommendable_options"),
    [
        (60, 2),
        (15, 1),
        (15, 0),
    ],
)
def test_v4_cli_cannot_lower_frozen_done_gate_thresholds(
    maximum_quote_age_minutes: int,
    minimum_recommendable_options: int,
) -> None:
    with pytest.raises(RuntimeError, match="before search"):
        run_live_done_gate_v4._frozen_done_gate_thresholds(
            _request(),
            maximum_quote_age_minutes=maximum_quote_age_minutes,
            minimum_recommendable_options=minimum_recommendable_options,
        )


def test_v4_cli_thresholds_must_match_frozen_scenario() -> None:
    assert run_live_done_gate_v4._frozen_done_gate_thresholds(
        _request(),
        maximum_quote_age_minutes=15,
        minimum_recommendable_options=2,
    ) == (15, 2)


def test_v4_client_timeout_defaults_to_server_budget_plus_margin() -> None:
    assert (
        run_live_done_gate_v4._client_request_timeout_seconds(
            _request(),
            None,
        )
        == 900.0
    )


def test_v4_synthetic_sold_out_event_is_explicitly_hypothetical() -> None:
    injected_at = datetime(2026, 8, 4, 12, 34, 56, 123456, tzinfo=UTC)

    body = run_live_done_gate_v4._synthetic_sold_out_event_body(
        "lodging:target",
        "ctrip",
        injected_at=injected_at,
    )
    contract = run_live_done_gate_v4._synthetic_fault_contract()
    parsed = LivePackageEvent.model_validate(body["event"])

    assert body == {
        "event": {
            "id": "live-v4-gate-synthetic-sold-out-20260804123456123456",
            "kind": "sold_out",
            "target_component_id": "lodging:target",
            "affected_provider": "ctrip",
            "occurred_at": "2026-08-04T12:34:56.123456+00:00",
            "source": "tripchord-synthetic-done-gate-fault-injection",
        }
    }
    assert contract["mode"] == "synthetic_sold_out_fault_injection"
    assert parsed.kind == PackageEventKind.SOLD_OUT
    assert parsed.source == "tripchord-synthetic-done-gate-fault-injection"
    assert contract["platform_sold_out_observed"] is False
    assert contract["platform_price_change_observed"] is False
    assert contract["verified_change_scope"] == (
        "different_available_replacement_identity_not_platform_sold_out"
    )
    assert "不是平台售罄信号" in contract["claim_boundary"]


def _synthetic_local_repair_fixture() -> tuple[SimpleNamespace, SimpleNamespace]:
    target_id = "lodging:target"
    replacement_id = "lodging:replacement"
    provider = LiveDataProvider.CTRIP
    target = SimpleNamespace(
        id=target_id,
        provider=provider.value,
        availability=QuoteAvailability.AVAILABLE,
    )
    before_candidate = SimpleNamespace(id="candidate:v1", lodgings=(target,))
    initial = SimpleNamespace(
        package=SimpleNamespace(final_candidate=before_candidate),
    )
    replacement = SimpleNamespace(
        id=replacement_id,
        provider=provider.value,
        availability=QuoteAvailability.AVAILABLE,
    )
    after_candidate = SimpleNamespace(
        id="candidate:v2",
        lodgings=(replacement,),
    )
    diff = SimpleNamespace(
        removed_component_ids=(target_id,),
        added_component_ids=(replacement_id,),
        changed_component_ids=(),
    )
    live_event = SimpleNamespace(
        id="event:synthetic-sold-out",
        kind=PackageEventKind.SOLD_OUT,
        target_component_id=target_id,
        affected_provider=provider,
        source="tripchord-synthetic-done-gate-fault-injection",
    )
    repair_event = SimpleNamespace(
        id=live_event.id,
        kind=PackageEventKind.SOLD_OUT,
        target_component_id=target_id,
        replacement_component_id=replacement_id,
    )
    reverification = SimpleNamespace(
        phase=PackageVerificationPhase.EVENT_REVERIFICATION,
        errors=(),
        matches=lambda candidate: candidate is after_candidate,
    )
    package = SimpleNamespace(
        final_candidate=after_candidate,
        final_decision=SimpleNamespace(state=PackageDecisionState.ACCEPT),
        diff=diff,
        event_handoff=SimpleNamespace(
            repair=SimpleNamespace(event=repair_event),
            reverification=reverification,
        ),
    )
    old_value = SimpleNamespace(
        transient_offer_id=target_id,
        stable_product_key="a" * 64,
        product_identity_confidence=OfferIdentityConfidence.MEDIUM,
        identity_ambiguous=False,
        provider=provider.value,
        availability=QuoteAvailability.AVAILABLE.value,
    )
    new_value = SimpleNamespace(
        transient_offer_id=replacement_id,
        stable_product_key="b" * 64,
        product_identity_confidence=OfferIdentityConfidence.MEDIUM,
        identity_ambiguous=False,
        provider=provider.value,
        availability=QuoteAvailability.AVAILABLE.value,
    )
    semantic_diff = SimpleNamespace(
        change=OfferSemanticChange.DIFFERENT_PRODUCT,
        different_product_confirmed=True,
        same_product=False,
        same_offer=False,
        identity_ambiguous=False,
        price_changed=False,
    )
    resolution = SimpleNamespace(
        disposition=EventDisposition.LOCAL_REPAIR,
        verified_change=True,
        candidate_pool_expansion_required=False,
        replacement_component_id=replacement_id,
        semantic_diff=semantic_diff,
        envelope=SimpleNamespace(
            kind=PackageEventKind.SOLD_OUT,
            source=live_event.source,
            target_component_id=target_id,
            old_value=old_value,
            new_value=new_value,
        ),
    )
    event_run = SimpleNamespace(
        event=live_event,
        event_resolution=resolution,
        applied_disposition=EventDisposition.LOCAL_REPAIR,
        global_run=None,
        requeried_providers=(provider,),
        source_task_ids=("event-source-ctrip-lodging-full",),
        package=package,
        package_reverification_audit=SimpleNamespace(
            passed=True,
            before_candidate_id=before_candidate.id,
            after_candidate_id=after_candidate.id,
        ),
        decision=SimpleNamespace(state=PackageDecisionState.ACCEPT),
    )
    return initial, event_run


def test_v4_synthetic_sold_out_requires_verified_different_product_repair() -> None:
    initial, event = _synthetic_local_repair_fixture()

    evidence = run_live_done_gate_v4._validate_synthetic_sold_out_replan(
        initial,
        event,
        target_component_id="lodging:target",
        affected_provider="ctrip",
    )

    assert evidence["passed"] is True
    assert evidence["stable_different_product_confirmed"] is True
    assert evidence["repair_removed_component_count"] == 1
    assert evidence["repair_added_component_count"] == 1
    assert evidence["event_reverification_passed"] is True
    assert evidence["independent_audit_passed"] is True
    assert evidence["master_accepted"] is True
    assert evidence["platform_sold_out_observed"] is False


def test_v4_synthetic_sold_out_rejects_same_product_refresh() -> None:
    initial, event = _synthetic_local_repair_fixture()
    event.event_resolution.envelope.new_value.stable_product_key = "a" * 64
    event.event_resolution.semantic_diff = SimpleNamespace(
        change=OfferSemanticChange.OBSERVATION_REFRESHED,
        different_product_confirmed=False,
        same_product=True,
        same_offer=True,
        identity_ambiguous=False,
        price_changed=False,
    )

    with pytest.raises(RuntimeError, match="稳定商品身份"):
        run_live_done_gate_v4._validate_synthetic_sold_out_replan(
            initial,
            event,
            target_component_id="lodging:target",
            affected_provider="ctrip",
        )


def test_v4_client_timeout_accepts_explicit_900_seconds() -> None:
    assert (
        run_live_done_gate_v4._client_request_timeout_seconds(
            _request(),
            900.0,
        )
        == 900.0
    )


@pytest.mark.parametrize(
    "configured_timeout_seconds",
    [0.0, 600.0, 899.999, float("inf"), float("nan")],
)
def test_v4_client_timeout_cannot_race_the_frozen_server_budget(
    configured_timeout_seconds: float,
) -> None:
    with pytest.raises(RuntimeError, match="结构化 504/取消证据"):
        run_live_done_gate_v4._client_request_timeout_seconds(
            _request(),
            configured_timeout_seconds,
        )


def test_v4_job_idempotency_key_binds_payload_sha_and_fresh_attempt() -> None:
    request = _request()
    payload = run_live_done_gate_v4._api_payload(request)
    scenario_sha256 = run_live_done_gate_v4._canonical_sha256(request)
    payload_sha256 = run_live_done_gate_v4._canonical_sha256(payload)
    attempt_id = "a" * 32

    key = run_live_done_gate_v4._job_idempotency_key(payload, attempt_id)
    control = run_live_done_gate_v4._new_live_job_control(
        request,
        payload,
        client_wait_timeout_seconds=900.0,
        attempt_id=attempt_id,
    )

    assert scenario_sha256 != payload_sha256
    assert key == f"tripchord-live-v4-{payload_sha256}-{attempt_id}"
    assert control["scenario_sha256"] == scenario_sha256
    assert control["api_payload_sha256"] == payload_sha256
    assert control["idempotency"] == {
        "key": key,
        "attempt_id": attempt_id,
        "derivation": "sha256(canonical API payload) + fresh attempt_id",
        "credential_inputs": False,
    }
    assert "api-secret" not in json.dumps(control)
    assert "bridge-secret" not in json.dumps(control)


def test_v4_fresh_attempt_ids_prevent_terminal_replay_across_deliberate_runs() -> None:
    request = _request()
    payload = run_live_done_gate_v4._api_payload(request)

    first = run_live_done_gate_v4._new_live_job_control(
        request,
        payload,
        client_wait_timeout_seconds=900.0,
        attempt_id="c" * 32,
    )
    second = run_live_done_gate_v4._new_live_job_control(
        request,
        payload,
        client_wait_timeout_seconds=900.0,
        attempt_id="d" * 32,
    )

    assert first["api_payload_sha256"] == second["api_payload_sha256"]
    assert first["idempotency"]["key"] != second["idempotency"]["key"]


@pytest.mark.asyncio
async def test_v4_async_job_records_replay_revision_progress_and_full_terminal_result() -> None:
    request = _request()
    payload = run_live_done_gate_v4._api_payload(request)
    terminal_result = {
        "interpretation": {"state": "ready"},
        "run": {"fixture": "complete-flexible-run"},
        "cached_pair_runs": [],
        "model_enhancement_enabled": True,
        "execution_boundary": "fixture boundary",
    }
    status_responses = iter(
        (
            _job_snapshot(
                state="running",
                revision=2,
                stage="execute_live_search",
                progress=35,
            ),
            _job_snapshot(
                state="running",
                revision=3,
                stage="finalize_live_plan",
                progress=75,
            ),
            _job_snapshot(
                state="succeeded",
                revision=4,
                stage="succeeded",
                progress=100,
                result=terminal_result,
            ),
        )
    )
    observed: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        observed.append(
            {
                "method": http_request.method,
                "path": http_request.url.path,
                "authorization": http_request.headers.get("Authorization"),
                "idempotency_key": http_request.headers.get("Idempotency-Key"),
            }
        )
        if http_request.method == "POST":
            return httpx.Response(
                202,
                json=_started_job_payload(replayed=True),
            )
        return httpx.Response(200, json=next(status_responses))

    async def no_wait(_: float) -> None:
        return None

    control = run_live_done_gate_v4._new_live_job_control(
        request,
        payload,
        client_wait_timeout_seconds=900.0,
        attempt_id="b" * 32,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer tenant-secret-must-not-persist"},
    ) as client:
        await run_live_done_gate_v4._submit_flexible_live_job(
            client,
            "http://tripchord.test",
            payload,
            control,
        )
        terminal = await run_live_done_gate_v4._await_flexible_live_job(
            client,
            "http://tripchord.test",
            control,
            client_wait_timeout_seconds=900.0,
            poll_interval_seconds=0.001,
            sleep=no_wait,
        )

    expected_key = run_live_done_gate_v4._job_idempotency_key(payload, "b" * 32)
    assert [item["method"] for item in observed] == ["POST", "GET", "GET", "GET"]
    assert {item["authorization"] for item in observed} == {"Bearer tenant-secret-must-not-persist"}
    assert observed[0]["idempotency_key"] == expected_key
    assert all(item["idempotency_key"] is None for item in observed[1:])
    assert control["job_id"] == "live-job-fixture"
    assert control["replayed"] is True
    assert control["status_url"] == (
        "/api/v1/agents/live-flexible-plan-from-text/jobs/live-job-fixture"
    )
    assert [item["revision"] for item in control["revision_history"]] == [1, 2, 3, 4]
    assert [item["stage"] for item in control["stage_progress_history"]] == [
        "queued",
        "execute_live_search",
        "finalize_live_plan",
        "succeeded",
    ]
    assert terminal.result == terminal_result
    assert control["terminal_job"]["result_sha256"] == (
        run_live_done_gate_v4._canonical_sha256(terminal_result)
    )
    assert "result" not in control["terminal_job"]
    assert "boundary" not in control["terminal_job"]
    assert "tenant-secret-must-not-persist" not in json.dumps(control)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
async def test_v4_async_job_failed_or_cancelled_terminal_is_fail_closed(
    terminal_state: str,
) -> None:
    control = _poll_control()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_job_snapshot(
                state=terminal_state,
                revision=2,
                stage=terminal_state,
                progress=100,
                error="fixture terminal failure",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match=f"terminal state {terminal_state}"):
            await run_live_done_gate_v4._await_flexible_live_job(
                client,
                "http://tripchord.test",
                control,
                client_wait_timeout_seconds=10.0,
            )

    assert control["terminal_job"]["state"] == terminal_state
    assert control["terminal_job"]["error"] == "fixture terminal failure"


@pytest.mark.asyncio
async def test_v4_async_job_persists_safe_failure_diagnostic_without_raw_detail() -> None:
    control = _poll_control()
    code = "domain_value_error"
    details = {
        "exception_class": "ValueError",
        "message_sha256": "b" * 64,
        "validation_model": None,
        "validation_errors": [],
    }
    details_digest = run_live_done_gate_v4._canonical_sha256(
        {"safe_failure_code": code, "details": details}
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_job_snapshot(
                state="failed",
                revision=2,
                stage="failed",
                progress=100,
                error="ValueError: live planning execution failed",
                safe_failure_code=code,
                safe_failure_details=details,
                safe_failure_details_digest=details_digest,
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as caught:
            await run_live_done_gate_v4._await_flexible_live_job(
                client,
                "http://tripchord.test",
                control,
                client_wait_timeout_seconds=10.0,
            )

    assert f"safe_failure_code={code}" in str(caught.value)
    assert f"safe_failure_details_digest={details_digest}" in str(caught.value)
    assert control["revision_history"][-1]["safe_failure_code"] == code
    assert control["revision_history"][-1]["safe_failure_details_digest"] == details_digest
    assert control["terminal_job"]["safe_failure_code"] == code
    assert control["terminal_job"]["safe_failure_details"] == details
    assert control["terminal_job"]["safe_failure_details_digest"] == details_digest
    assert "prompt" not in json.dumps(control)
    assert "provider.invalid" not in json.dumps(control)


@pytest.mark.asyncio
async def test_v4_async_job_tenant_scoped_404_is_fail_closed() -> None:
    control = _poll_control()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "live job not found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="tenant-scoped job GET returned 404"):
            await run_live_done_gate_v4._await_flexible_live_job(
                client,
                "http://tripchord.test",
                control,
                client_wait_timeout_seconds=10.0,
            )


@pytest.mark.asyncio
async def test_v4_async_job_client_wait_expiry_is_fail_closed() -> None:
    control = _poll_control()
    current_time = 0.0

    def clock() -> float:
        return current_time

    async def advance(seconds: float) -> None:
        nonlocal current_time
        current_time += seconds

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_job_snapshot(
                state="queued",
                revision=1,
                stage="queued",
                progress=0,
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="client wait budget expired"):
            await run_live_done_gate_v4._await_flexible_live_job(
                client,
                "http://tripchord.test",
                control,
                client_wait_timeout_seconds=1.0,
                poll_interval_seconds=0.5,
                monotonic=clock,
                sleep=advance,
            )

    assert control["terminal_job"] is None


@pytest.mark.asyncio
async def test_v4_cancel_receipt_uses_bound_delete_and_persists_no_arbitrary_payload() -> None:
    control = _poll_control()
    observed: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                **_job_snapshot(
                    state="cancelled",
                    revision=2,
                    stage="cancelled",
                    progress=100,
                    error="safe generic error",
                ),
                "boundary": "must not enter cancellation receipt",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret-must-not-persist"},
    ) as client:
        receipt = await run_live_done_gate_v4._cancel_flexible_live_job(
            client,
            "http://tripchord.test",
            control,
        )

    assert observed == {
        "method": "DELETE",
        "path": "/api/v1/agents/live-flexible-plan-from-text/jobs/live-job-fixture",
        "authorization": "Bearer secret-must-not-persist",
    }
    assert receipt["outcome"] == "acknowledged"
    assert receipt["state"] == "cancelled"
    assert receipt["cancellation_requested"] is False
    assert "boundary" not in receipt
    assert "secret-must-not-persist" not in json.dumps(receipt)


def test_v4_async_job_same_revision_cannot_change_stage_or_progress() -> None:
    control = _poll_control()
    changed_without_revision = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=1,
            stage="execute_live_search",
            progress=35,
        )
    )

    with pytest.raises(RuntimeError, match="without increasing its revision"):
        run_live_done_gate_v4._record_job_snapshot(control, changed_without_revision)


def test_v4_async_job_rejects_snapshot_bound_to_wrong_api_payload_sha() -> None:
    control = _poll_control()
    wrong_request = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=2,
            stage="execute_live_search",
            progress=35,
            request_sha256="0" * 64,
        )
    )

    with pytest.raises(RuntimeError, match="canonical API payload"):
        run_live_done_gate_v4._record_job_snapshot(control, wrong_request)


@pytest.mark.parametrize("next_revision", [2, 3])
def test_v4_checkpoint_chain_rejects_mutation_at_any_later_observation(
    next_revision: int,
) -> None:
    control = _poll_control()
    original = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=2,
            stage="execute_live_search",
            progress=35,
            pair_checkpoints=[_checkpoint(1)],
        )
    )
    run_live_done_gate_v4._record_job_snapshot(control, original)
    mutated = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=next_revision,
            stage="execute_live_search",
            progress=35,
            pair_checkpoints=[_checkpoint(1, query_task_ids=("mutated-task",))],
        )
    )

    with pytest.raises(RuntimeError, match="immutable prefix"):
        run_live_done_gate_v4._record_job_snapshot(control, mutated)


def test_v4_checkpoint_chain_rejects_deletion_and_records_ordered_digest() -> None:
    control = _poll_control()
    checkpoints = [_checkpoint(1), _checkpoint(2)]
    grown = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=2,
            stage="execute_live_search",
            progress=50,
            pair_checkpoints=checkpoints,
        )
    )
    run_live_done_gate_v4._record_job_snapshot(control, grown)
    record = control["revision_history"][-1]

    assert record["checkpoint_count"] == 2
    assert record["ordered_checkpoint_sha256"] == [
        item["checkpoint_sha256"] for item in checkpoints
    ]
    assert record["checkpoint_chain_sha256"] == run_live_done_gate_v4._canonical_sha256(
        record["ordered_checkpoint_sha256"]
    )

    deleted = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="running",
            revision=3,
            stage="execute_live_search",
            progress=60,
            pair_checkpoints=[checkpoints[0]],
        )
    )
    with pytest.raises(RuntimeError, match="immutable prefix"):
        run_live_done_gate_v4._record_job_snapshot(control, deleted)


def test_v4_success_requires_three_checkpoints_aligned_to_final_pair_runs() -> None:
    snapshot = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="succeeded",
            revision=4,
            stage="succeeded",
            progress=100,
            result={"run": {}},
            pair_checkpoints=_three_checkpoints(),
        )
    )

    receipt = run_live_done_gate_v4._validate_terminal_pair_checkpoints(
        snapshot,
        SimpleNamespace(pair_runs=_three_pair_runs()),
    )

    assert receipt["passed"] is True
    assert receipt["count"] == 3
    assert receipt["ordered_checkpoint_sha256"] == [
        item["checkpoint_sha256"] for item in _three_checkpoints()
    ]


@pytest.mark.parametrize("failure_kind", ["missing", "date", "state", "query_tasks"])
def test_v4_success_rejects_missing_or_misaligned_pair_checkpoint(
    failure_kind: str,
) -> None:
    checkpoints = _three_checkpoints()
    pair_runs = list(_three_pair_runs())
    if failure_kind == "missing":
        checkpoints.pop()
    elif failure_kind == "date":
        pair_runs[1].date_pair.return_date = date(2026, 8, 31)
    elif failure_kind == "state":
        pair_runs[1].state = SimpleNamespace(value="failed")
    else:
        pair_runs[1].query_tasks = (SimpleNamespace(id="different-task"),)
    snapshot = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="succeeded",
            revision=4,
            stage="succeeded",
            progress=100,
            result={"run": {}},
            pair_checkpoints=checkpoints,
        )
    )

    with pytest.raises(RuntimeError, match=r"exactly 3|does not match"):
        run_live_done_gate_v4._validate_terminal_pair_checkpoints(
            snapshot,
            SimpleNamespace(pair_runs=tuple(pair_runs)),
        )


def test_v4_failure_bundle_records_stage_and_retry_boundary() -> None:
    captured_at = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
    bundle = run_live_done_gate_v4._failure_evidence_bundle(
        request=_request(),
        stage="select_recommendable_option",
        error=RuntimeError("no recommendable option after login_required"),
        captured_at=captured_at,
        context={
            "companion_preflight": {"status": "connected"},
            "flexible_run": {"final_decision": {"state": "human_block"}},
        },
    )

    assert bundle["run_status"] == "failed_before_done_gate"
    assert bundle["failure"]["stage"] == "select_recommendable_option"
    assert bundle["failure"]["type"] == "RuntimeError"
    assert "登录" in bundle["failure"]["retry_policy"]
    assert bundle["scenario_sha256"] == run_live_done_gate_v4._canonical_sha256(_request())
    assert bundle["flexible_run"]["final_decision"]["state"] == "human_block"


@pytest.mark.asyncio
async def test_formal_control_retries_share_one_bounded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """92a0db1a: challenge/activate/finalize cannot each reset retries."""

    calls: list[tuple[str, str]] = []

    class FlakyClient:
        async def post(self, url: str, **kwargs: object) -> object:
            key = str(kwargs["headers"]["Idempotency-Key"])  # type: ignore[index]
            calls.append((url, key))
            if len(calls) in {1, 3}:
                raise httpx.ReadError("committed response was lost")
            return httpx.Response(200, json={"ok": True})

    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(run_live_done_gate_v4.asyncio, "sleep", record_delay)
    budget = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=5,
        wall_seconds=10,
        now=lambda: 0.0,
    )
    client = FlakyClient()
    first = await run_live_done_gate_v4._post_formal_control_with_retry(
        client,  # type: ignore[arg-type]
        "http://tripchord.test/challenge",
        payload={"phase": "challenge"},
        headers={"Idempotency-Key": "same-challenge-key"},
        budget=budget,
    )
    second = await run_live_done_gate_v4._post_formal_control_with_retry(
        client,  # type: ignore[arg-type]
        "http://tripchord.test/activate",
        payload={"phase": "activate"},
        headers={"Idempotency-Key": "same-activate-key"},
        budget=budget,
    )
    assert first.json() == {"ok": True}
    assert second.json() == {"ok": True}
    assert calls == [
        ("http://tripchord.test/challenge", "same-challenge-key"),
        ("http://tripchord.test/challenge", "same-challenge-key"),
        ("http://tripchord.test/activate", "same-activate-key"),
        ("http://tripchord.test/activate", "same-activate-key"),
    ]
    assert budget.remaining_attempts == 1
    assert delays == [0.1, 0.2]


@pytest.mark.asyncio
async def test_formal_control_transport_backoff_is_charged_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e42a7e60: request activity and real backoff are each charged once."""

    clock = [0.0]
    calls = [0]
    observed_timeouts: list[float] = []
    observed_delays: list[float] = []

    class FlakyClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            calls[0] += 1
            clock[0] += 0.4 if calls[0] == 1 else 0.3
            if calls[0] == 1:
                raise httpx.ReadError("committed response was lost")
            return httpx.Response(200, json={"ok": True})

    async def advancing_wait_for(awaitable: object, timeout: float) -> object:
        observed_timeouts.append(timeout)
        return await awaitable  # type: ignore[misc]

    async def advancing_sleep(seconds: float) -> None:
        observed_delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(run_live_done_gate_v4.asyncio, "wait_for", advancing_wait_for)
    monkeypatch.setattr(run_live_done_gate_v4.asyncio, "sleep", advancing_sleep)
    budget = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=2,
        wall_seconds=5.0,
        now=lambda: clock[0],
    )

    response = await run_live_done_gate_v4._post_formal_control_with_retry(
        FlakyClient(),  # type: ignore[arg-type]
        "http://tripchord.test/challenge",
        payload={"phase": "challenge"},
        headers={"Idempotency-Key": "linear-budget-v1"},
        budget=budget,
    )

    assert response.json() == {"ok": True}
    assert observed_timeouts == pytest.approx([5.0, 4.5])
    assert observed_delays == pytest.approx([0.1])
    assert budget.remaining_seconds == pytest.approx(4.2)


@pytest.mark.asyncio
async def test_formal_control_transport_failures_exhaust_linearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """e42a7e60: repeated transport failures stop at the exact remaining budget."""

    clock = [0.0]
    calls = [0]
    observed_timeouts: list[float] = []
    observed_delays: list[float] = []

    class FailedClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            calls[0] += 1
            clock[0] += 0.2
            raise httpx.ReadError("response lost")

    async def advancing_wait_for(awaitable: object, timeout: float) -> object:
        observed_timeouts.append(timeout)
        return await awaitable  # type: ignore[misc]

    async def advancing_sleep(seconds: float) -> None:
        observed_delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(run_live_done_gate_v4.asyncio, "wait_for", advancing_wait_for)
    monkeypatch.setattr(run_live_done_gate_v4.asyncio, "sleep", advancing_sleep)
    budget = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=5,
        wall_seconds=0.65,
        now=lambda: clock[0],
    )

    with pytest.raises(RuntimeError, match="formal control retry budget was exhausted"):
        await run_live_done_gate_v4._post_formal_control_with_retry(
            FailedClient(),  # type: ignore[arg-type]
            "http://tripchord.test/finalize",
            payload={"phase": "finalize"},
            headers={"Idempotency-Key": "linear-exhaustion-v1"},
            budget=budget,
        )

    assert calls[0] == 2
    assert observed_timeouts == pytest.approx([0.65, 0.35])
    assert observed_delays == pytest.approx([0.1, 0.15])
    assert budget._remaining_seconds == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_formal_control_budget_excludes_business_execution_time() -> None:
    """3d7dd448: idle business work cannot expire the control-plane budget."""

    clock = [0.0]

    class StableClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            return httpx.Response(200, json={"ok": True})

    budget = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=2,
        wall_seconds=30,
        now=lambda: clock[0],
    )
    first = await run_live_done_gate_v4._post_formal_control_with_retry(
        StableClient(),  # type: ignore[arg-type]
        "http://tripchord.test/challenge",
        payload={"phase": "challenge"},
        headers={"Idempotency-Key": "challenge-key"},
        budget=budget,
    )
    clock[0] = 45.0
    final = await run_live_done_gate_v4._post_formal_control_with_retry(
        StableClient(),  # type: ignore[arg-type]
        "http://tripchord.test/finalize",
        payload={"phase": "finalize"},
        headers={"Idempotency-Key": "finalize-key"},
        budget=budget,
    )

    assert first.json() == final.json() == {"ok": True}
    assert budget.remaining_attempts == 0


@pytest.mark.asyncio
async def test_formal_control_budget_hard_times_out_one_hung_post() -> None:
    """3d7dd448: each control request is bounded by the remaining active budget."""

    class HungClient:
        async def post(self, url: str, **kwargs: object) -> httpx.Response:
            del url, kwargs
            await asyncio.sleep(60)
            raise AssertionError("hung post unexpectedly returned")

    budget = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=1,
        wall_seconds=0.02,
    )
    with pytest.raises(RuntimeError, match="formal control retry budget was exhausted"):
        await asyncio.wait_for(
            run_live_done_gate_v4._post_formal_control_with_retry(
                HungClient(),  # type: ignore[arg-type]
                "http://tripchord.test/finalize",
                payload={"phase": "finalize"},
                headers={"Idempotency-Key": "hung-finalize-key"},
                budget=budget,
            ),
            timeout=0.2,
        )


def test_formal_control_context_budget_isolated_and_reset() -> None:
    """3d7dd448: one run's ContextVar budget cannot leak into the next run."""

    first = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=1,
        wall_seconds=1,
    )
    second = run_live_done_gate_v4._FormalControlRetryBudget(
        total_attempts=2,
        wall_seconds=1,
    )
    with run_live_done_gate_v4._formal_control_retry_scope(first):
        assert run_live_done_gate_v4._formal_control_retry_budget(None) is first
    assert run_live_done_gate_v4._FORMAL_CONTROL_RETRY_BUDGET.get() is None
    with run_live_done_gate_v4._formal_control_retry_scope(second):
        assert run_live_done_gate_v4._formal_control_retry_budget(None) is second
    assert run_live_done_gate_v4._FORMAL_CONTROL_RETRY_BUDGET.get() is None


def test_formal_companion_binding_requires_one_exact_fresh_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_build_identity = _verified_test_companion_build_identity()

    def bind(preflight: object) -> dict[str, object]:
        return run_live_done_gate_v4._formal_companion_binding_from_preflight(
            preflight,
            expected_build_identity=expected_build_identity,
        )

    companion = {
        "companion_id": "formal-companion-v1",
        "providers": ["ctrip", "qunar", "tongcheng"],
        "authorized_scope_keys": [
            "ctrip:flight",
            "ctrip:lodging",
            "qunar:flight",
            "qunar:lodging",
            "tongcheng:flight",
        ],
        "adapter_version": "0.2.0",
        "contract_version": "tripchord-capability-v1",
        "runtime_instance_id": "formal-companion-runtime-v1",
        "build_identity": expected_build_identity,
        "last_seen": "2026-08-15T00:00:00+00:00",
        "age_seconds": 0.0,
        "is_fresh": True,
    }
    preflight = {
        "server_time": "2026-08-15T00:00:00+00:00",
        "stale_after_seconds": 45,
        "companions": [companion],
    }
    binding = bind(preflight)
    assert binding["companion_id"] == companion["companion_id"]
    assert binding["runtime_instance_id"] == companion["runtime_instance_id"]
    assert binding["build_identity"] == companion["build_identity"]
    assert binding["identity_sha256"] == run_live_done_gate_v4._canonical_sha256(
        {key: value for key, value in binding.items() if key != "identity_sha256"}
    )
    for field, foreign, message in (
        ("adapter_version", "test-companion-v1", "production adapter/contract"),
        ("contract_version", "test-contract-v1", "production adapter/contract"),
    ):
        with pytest.raises(RuntimeError, match=message):
            bind(
                {
                    **preflight,
                    "companions": [{**companion, field: foreign}],
                }
            )
    with pytest.raises(RuntimeError, match="verified release"):
        bind(
            {
                **preflight,
                "companions": [
                    {
                        **companion,
                        "build_identity": {
                            **companion["build_identity"],
                            "build_sha256": "7" * 64,
                        },
                    }
                ],
            }
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        bind(
            {**preflight, "companions": [companion, copy.deepcopy(companion)]}
        )
    with pytest.raises(RuntimeError, match="exactly one"):
        bind(
            {**preflight, "companions": [{**companion, "is_fresh": False}]}
        )

    fresh_foreign = {
        **copy.deepcopy(companion),
        "companion_id": "fresh-foreign-companion-v1",
        "runtime_instance_id": "fresh-foreign-runtime-v1",
        "build_identity": {
            **companion["build_identity"],
            "build_sha256": "7" * 64,
        },
    }
    stale_selected = {**companion, "is_fresh": False, "age_seconds": 46.0}
    with pytest.raises(RuntimeError, match="exactly one"):
        bind(
            {**preflight, "companions": [stale_selected, fresh_foreign]}
        )

    incomplete_selected = {
        **companion,
        "authorized_scope_keys": companion["authorized_scope_keys"][:-1],
    }
    with pytest.raises(RuntimeError, match="exactly one"):
        bind(
            {**preflight, "companions": [incomplete_selected, fresh_foreign]}
        )

    impersonating = {
        **copy.deepcopy(companion),
        "companion_id": "impersonating-companion-v1",
    }
    with pytest.raises(RuntimeError, match="exactly one"):
        bind(
            {**preflight, "companions": [companion, impersonating]}
        )

    with pytest.raises(RuntimeError, match="freshness"):
        bind(
            {
                **preflight,
                "companions": [
                    {
                        **companion,
                        "last_seen": "2026-08-14T23:59:59+00:00",
                        "age_seconds": 0.0,
                    }
                ],
            }
        )

    monkeypatch.setattr(run_live_done_gate_v4, "_REPO_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="build input is missing"):
        run_live_done_gate_v4._formal_companion_binding_from_preflight(preflight)


def test_v4_completed_bundle_rejects_context_without_formal_receipt() -> None:
    captured_at = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    context = {
        "formal_live_source_binding": {"fixture": "already-authority-validated"},
        "timeout_contract": {
            "server_execution_timeout_seconds": 600,
            "client_wait_timeout_seconds": 900.0,
            "minimum_client_margin_seconds": 300.0,
        },
        "runner_contract": {
            "require_model_enhancement": True,
            "maximum_quote_age_minutes": 15,
            "minimum_recommendable_options": 2,
        },
        "model_enhancement_enabled": True,
        "cached_pair_runs": [{"date_pair_id": "date-pair:test", "run_id": "run:test"}],
        "runtime_before_run": _runtime_payload(model_trace_count=10),
        "runtime_after_run": _runtime_payload(model_trace_count=25),
        "model_trace_count_delta": 15,
        "flexible_run": {"final_decision": {"state": "accept"}},
    }
    report = run_live_done_gate_v4.LiveV4DoneGateReport.model_validate(
        {
            "passed": True,
            "checks": [
                {
                    "name": "fixture_contract",
                    "passed": True,
                    "summary": "fixture passed",
                }
            ],
        }
    )

    with pytest.raises(RuntimeError, match="binding/receipt/challenge"):
        run_live_done_gate_v4._completed_evidence_bundle(
            request=_request(),
            report=report,
            captured_at=captured_at,
            context=context,
        )


def test_v4_model_trace_receipt_is_job_bound_and_reconciled_with_result() -> None:
    payload_sha256 = _api_payload_sha256()
    result = {
        "model_trace_scope_sha256": payload_sha256,
        "model_trace_count": 4,
        "model_trace_success_count": 3,
        "model_trace_failure_count": 1,
    }
    snapshot = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="succeeded",
            revision=4,
            stage="succeeded",
            progress=100,
            result=result,
            model_trace_success_count=3,
            model_trace_failure_count=1,
        )
    )

    assert run_live_done_gate_v4._validate_model_trace_receipt(
        snapshot,
        result,
        api_payload_sha256=payload_sha256,
        require_model_enhancement=True,
    ) == {
        "scope_sha256": payload_sha256,
        "total_count": 4,
        "success_count": 3,
        "failure_count": 1,
    }


def test_v4_model_trace_receipt_rejects_failure_only_or_result_tampering() -> None:
    payload_sha256 = _api_payload_sha256()
    failure_only = {
        "model_trace_scope_sha256": payload_sha256,
        "model_trace_count": 1,
        "model_trace_success_count": 0,
        "model_trace_failure_count": 1,
    }
    snapshot = run_live_done_gate_v4.LivePlanningJobSnapshot.model_validate(
        _job_snapshot(
            state="succeeded",
            revision=4,
            stage="succeeded",
            progress=100,
            result=failure_only,
            model_trace_failure_count=1,
        )
    )

    with pytest.raises(RuntimeError, match="success_count must be greater than zero"):
        run_live_done_gate_v4._validate_model_trace_receipt(
            snapshot,
            failure_only,
            api_payload_sha256=payload_sha256,
            require_model_enhancement=True,
        )

    tampered = {**failure_only, "model_trace_failure_count": 0}
    with pytest.raises(RuntimeError, match="does not match"):
        run_live_done_gate_v4._validate_model_trace_receipt(
            snapshot,
            tampered,
            api_payload_sha256=payload_sha256,
            require_model_enhancement=False,
        )


@pytest.mark.asyncio
async def test_v4_runtime_evidence_is_whitelisted_and_excludes_secrets() -> None:
    runtime_payload = {
        **_runtime_payload(),
        "api_key": "must-not-be-persisted",
        "browser_bridge_token": "must-not-be-persisted",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/agents/runtime"
        return httpx.Response(200, json=runtime_payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        evidence = await run_live_done_gate_v4._runtime_evidence(
            client,
            "http://tripchord.test",
            label="fixture runtime",
        )

    assert evidence == _runtime_payload()
    assert "api_key" not in evidence
    assert "browser_bridge_token" not in evidence


def test_v4_runtime_preflight_rejects_stale_server_timeout_override() -> None:
    runtime = {**_runtime_payload(), "effective_flexible_timeout_seconds": 1200}

    with pytest.raises(RuntimeError, match="must equal 600"):
        run_live_done_gate_v4._validate_runtime_timeout_contract(runtime)


def test_v4_recursive_redaction_removes_sensitive_fields_and_url_query_values() -> None:
    evidence = {
        "headers": {
            "Authorization": "Bearer hidden",
            "Cookie": "sid=hidden",
            "X-Request-ID": "safe-request-id",
        },
        "nested": {
            "api_token": "hidden-token",
            "credential_inputs": False,
            "url": "https://example.test/quote?page=2&u=hidden&session_id=safe-session",
        },
        "message": "open /detail?api_key=hidden&currency=CNY for evidence",
    }

    redacted = run_live_done_gate_v4._redact_explicit_secrets(evidence, ())
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert redacted["headers"]["Authorization"] == "[REDACTED]"
    assert redacted["headers"]["Cookie"] == "[REDACTED]"
    assert redacted["headers"]["X-Request-ID"] == "safe-request-id"
    assert redacted["nested"]["api_token"] == "[REDACTED]"
    assert redacted["nested"]["credential_inputs"] is False
    assert "hidden" not in serialized
    assert "page=2" not in serialized
    assert "u=hidden" not in serialized
    assert "session_id=safe-session" not in serialized
    assert "currency=CNY" not in serialized
    assert serialized.count("%5BREDACTED%5D") >= 5


def test_v4_http_error_never_copies_arbitrary_response_body_into_evidence_error() -> None:
    response = httpx.Response(
        500,
        json={
            "detail": "provider failed with password=hidden-password",
            "access_token": "hidden-token",
        },
    )

    with pytest.raises(RuntimeError) as captured:
        run_live_done_gate_v4._safe_response_json(response, "fixture request")

    assert str(captured.value) == "fixture request failed with HTTP 500"
    assert "hidden" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_enabled", "model_required", "invalid_field"),
    [
        (False, True, "runtime.model_enabled must be true"),
        (True, False, "runtime.model_required must be true"),
        (False, False, "runtime.model_enabled must be true"),
    ],
)
async def test_v4_required_model_gate_fails_before_live_network_when_runtime_is_not_strict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    model_enabled: bool,
    model_required: bool,
    invalid_field: str,
) -> None:
    api_secret = "fixture-api-secret-must-not-leak"
    bridge_secret = "fixture-bridge-secret-must-not-leak"
    companion_called = False

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    **_runtime_payload(),
                    "model_enabled": model_enabled,
                    "model_required": model_required,
                    "api_key": "runtime-secret-must-not-leak",
                },
                request=httpx.Request("GET", url),
            )

        async def post(self, *_: object, **__: object) -> httpx.Response:
            raise AssertionError("live request must not start after failed preflight")

    async def companion_preflight(*_: object, **__: object) -> dict[str, Any]:
        nonlocal companion_called
        companion_called = True
        return {"status": "connected"}

    monkeypatch.setattr(
        run_live_done_gate_v4.httpx,
        "AsyncClient",
        lambda **_: FakeClient(),
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_preflight_companion",
        companion_preflight,
    )
    output = tmp_path / "required-model-runtime-preflight.json"
    args = SimpleNamespace(
        request=SCENARIO,
        output=output,
        api_base="http://tripchord.test",
        api_token=api_secret,
        bridge_token=bridge_secret,
        request_timeout_seconds=900.0,
        maximum_quote_age_minutes=15,
        minimum_recommendable_options=2,
        require_model_enhancement=True,
    )

    assert (
        await run_live_done_gate_v4._run(
            args,
            expected_companion_build_identity=_verified_test_companion_build_identity(),
        )
        == 2
    )
    evidence_text = output.read_text(encoding="utf-8")
    evidence = json.loads(evidence_text)
    captured = capsys.readouterr()

    assert companion_called is False
    assert evidence["failure"]["stage"] == "validate_required_model_runtime"
    assert invalid_field in evidence["failure"]["message"]
    assert evidence["runtime_before_run"]["model_enabled"] is model_enabled
    assert evidence["runtime_before_run"]["model_required"] is model_required
    assert "companion_preflight" not in evidence
    assert "done_gate" not in evidence
    for secret in (api_secret, bridge_secret, "runtime-secret-must-not-leak"):
        assert secret not in evidence_text
        assert secret not in captured.out
        assert secret not in captured.err


def test_v4_failed_evidence_never_overwrites_a_passed_bundle(
    tmp_path: Path,
) -> None:
    output = tmp_path / "live-done-gate-v4.json"
    passed = {
        "schema_version": "tripchord-live-evidence-v4",
        "sentinel": "preserve-me",
        "done_gate": {"passed": True},
    }
    output.write_text(json.dumps(passed), encoding="utf-8")
    captured_at = datetime(2026, 7, 31, 6, 1, 2, 345678, tzinfo=UTC)

    actual = run_live_done_gate_v4._write_evidence_bundle(
        output,
        {"run_status": "failed_before_done_gate"},
        passed=False,
        captured_at=captured_at,
    )

    assert actual != output
    assert actual.name == ("live-done-gate-v4.failed-20260731T060102345678Z.json")
    assert json.loads(output.read_text(encoding="utf-8")) == passed
    assert json.loads(actual.read_text(encoding="utf-8")) == {
        "run_status": "failed_before_done_gate"
    }
    assert stat.S_IMODE(actual.stat().st_mode) == 0o600
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_v4_evidence_write_is_atomic_and_forces_mode_0600(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "evidence.json"

    actual = run_live_done_gate_v4._write_evidence_bundle(
        output,
        {"schema_version": "fixture", "value": 1},
        passed=True,
        captured_at=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
    )

    assert actual == output
    assert json.loads(output.read_text(encoding="utf-8"))["value"] == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not tuple(output.parent.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_v4_run_without_recommendation_emits_gate_failure_and_skips_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_failed_run_formal_control_double(monkeypatch)
    response_payload = {
        "interpretation": {"state": "ready"},
        "execution_boundary": "fixture boundary",
        "model_enhancement_enabled": True,
        "model_trace_scope_sha256": _api_payload_sha256(),
        "model_trace_count": 2,
        "model_trace_success_count": 2,
        "model_trace_failure_count": 0,
        "cached_pair_runs": [],
        "run": {"validated": "by test double"},
        **_formal_worker_model_receipts(trace_count=2),
    }
    posts: list[dict[str, Any]] = []
    client_timeouts: list[httpx.Timeout] = []
    runtime_payloads = iter(
        (
            _runtime_payload(model_trace_count=7),
            _runtime_payload(model_trace_count=11),
        )
    )

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            if url.endswith("/api/v1/agents/runtime"):
                return httpx.Response(
                    200,
                    json=next(runtime_payloads),
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json=_job_snapshot(
                    state="succeeded",
                    revision=2,
                    stage="succeeded",
                    progress=100,
                    result=response_payload,
                    pair_checkpoints=_three_checkpoints(),
                    model_trace_success_count=2,
                ),
                request=httpx.Request("GET", url),
            )

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            posts.append({"url": url, "json": json, "headers": headers})
            return httpx.Response(
                202,
                json=_started_job_payload(),
                request=httpx.Request("POST", url, json=json),
            )

    async def companion_preflight(
        *_: object,
        **__: object,
    ) -> dict[str, Any]:
        return _companion_preflight_payload()

    failed_run = SimpleNamespace(
        recommended_option_ids=(),
        pair_runs=_three_pair_runs(),
        model_dump=lambda **_: {
            "final_decision": {"state": "human_block"},
            "pair_runs": [{"fixture": index} for index in range(1, 4)],
        },
    )

    def client_factory(**kwargs: object) -> FakeClient:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        client_timeouts.append(timeout)
        return FakeClient()

    monkeypatch.setattr(
        run_live_done_gate_v4.httpx,
        "AsyncClient",
        client_factory,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_preflight_companion",
        companion_preflight,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "FlexibleLiveAgentRun",
        SimpleNamespace(model_validate=lambda _: failed_run),
    )
    gate_calls: list[dict[str, Any]] = []

    def evaluate_gate(flexible: object, **kwargs: Any) -> Any:
        assert flexible is failed_run
        gate_calls.append(kwargs)
        return run_live_done_gate_v4.LiveV4DoneGateReport.model_validate(
            {
                "passed": False,
                "checks": [
                    {
                        "name": "minimum_recommendable_options",
                        "passed": False,
                        "summary": "0/2 recommendable options",
                    }
                ],
            }
        )

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "evaluate_live_v4_done_gate",
        evaluate_gate,
    )

    def unexpected_event_target(_: object) -> None:
        raise AssertionError("synthetic event must not be injected without a recommendation")

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_event_target",
        unexpected_event_target,
    )
    output = tmp_path / "failed-live-v4.json"
    args = SimpleNamespace(
        request=SCENARIO,
        output=output,
        api_base="http://tripchord.test",
        api_token="",
        bridge_token="fixture-bridge-token-that-is-long-enough",
        request_timeout_seconds=None,
        maximum_quote_age_minutes=15,
        minimum_recommendable_options=2,
        require_model_enhancement=True,
        gate_run_id="fixture-no-recommendation-run",
        formal_source_control_token_file=None,
    )

    assert (
        await run_live_done_gate_v4._run(
            args,
            expected_companion_build_identity=_verified_test_companion_build_identity(),
        )
        == 2
    )
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert [item["url"] for item in posts] == [
        "http://tripchord.test/api/v1/agents/live-flexible-plan-from-text/jobs"
    ]
    assert (
        posts[0]["headers"]["Idempotency-Key"]
        == (evidence["live_job_control"]["idempotency"]["key"])
    )
    assert client_timeouts[0].read == 900.0
    assert evidence["run_status"] == "done_gate_failed"
    assert evidence["timeout_contract"] == {
        "server_execution_timeout_seconds": 600,
        "client_wait_timeout_seconds": 900.0,
        "minimum_client_margin_seconds": 300.0,
    }
    assert evidence["runner_contract"] == {
        "require_model_enhancement": True,
        "maximum_quote_age_minutes": 15,
        "minimum_recommendable_options": 2,
    }
    assert evidence["event_injection_contract"]["source"] == (
        "tripchord-synthetic-done-gate-fault-injection"
    )
    assert evidence["event_injection_contract"]["platform_sold_out_observed"] is False
    assert evidence["runtime_before_run"]["primary_model"] == ("deepseek-v4-flash")
    assert evidence["runtime_before_run"]["model_trace_count"] == 7
    assert evidence["runtime_after_run"]["model_trace_count"] == 11
    assert evidence["process_global_model_trace_diagnostic"]["delta"] == 4
    assert evidence["process_global_model_trace_diagnostic"]["authoritative"] is False
    assert evidence["model_trace_receipt"]["success_count"] == 2
    assert evidence["flexible_run"]["final_decision"]["state"] == "human_block"
    assert evidence["live_job_control"]["job_id"] == "live-job-fixture"
    assert evidence["live_job_control"]["replayed"] is False
    assert evidence["live_job_control"]["status_url"].endswith("/jobs/live-job-fixture")
    assert [item["revision"] for item in evidence["live_job_control"]["revision_history"]] == [
        1,
        2,
    ]
    assert evidence["live_job_control"]["terminal_job"]["result_sha256"] == (
        run_live_done_gate_v4._canonical_sha256(response_payload)
    )
    assert "result" not in evidence["live_job_control"]["terminal_job"]
    assert gate_calls[0]["selected_initial"] is None
    assert gate_calls[0]["event"] is None
    assert evidence["done_gate"]["passed"] is False
    assert evidence["event_execution"] == {
        "status": "skipped",
        "skipped_reason": "no_recommendable_published_option",
        "synthetic_event_injected": False,
    }
    assert "failure" not in evidence
    assert "injected_event" not in evidence


@pytest.mark.asyncio
async def test_v4_required_model_gate_rejects_silent_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_failed_run_formal_control_double(monkeypatch)
    delete_calls: list[str] = []
    response_payload = {
        "interpretation": {"state": "ready"},
        "execution_boundary": "fixture boundary",
        "model_enhancement_enabled": False,
        "cached_pair_runs": [],
        "run": {"validated": "by test double"},
    }

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            if url.endswith("/api/v1/agents/runtime"):
                return httpx.Response(
                    200,
                    json=_runtime_payload(),
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json=_job_snapshot(
                    state="succeeded",
                    revision=2,
                    stage="succeeded",
                    progress=100,
                    result=response_payload,
                ),
                request=httpx.Request("GET", url),
            )

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            return httpx.Response(
                202,
                json=_started_job_payload(),
                request=httpx.Request("POST", url, json=json),
            )

        async def delete(self, url: str) -> httpx.Response:
            delete_calls.append(url)
            return httpx.Response(
                200,
                json=_job_snapshot(
                    state="succeeded",
                    revision=2,
                    stage="succeeded",
                    progress=100,
                    result=response_payload,
                ),
                request=httpx.Request("DELETE", url),
            )

    async def companion_preflight(
        *_: object,
        **__: object,
    ) -> dict[str, Any]:
        return _companion_preflight_payload()

    monkeypatch.setattr(
        run_live_done_gate_v4.httpx,
        "AsyncClient",
        lambda **_: FakeClient(),
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_preflight_companion",
        companion_preflight,
    )
    output = tmp_path / "required-model-mismatch.json"
    args = SimpleNamespace(
        request=SCENARIO,
        output=output,
        api_base="http://tripchord.test",
        api_token="",
        bridge_token="fixture-bridge-token-that-is-long-enough",
        request_timeout_seconds=900.0,
        maximum_quote_age_minutes=15,
        minimum_recommendable_options=2,
        require_model_enhancement=True,
        gate_run_id="fixture-model-mismatch-run",
        formal_source_control_token_file=None,
    )

    assert (
        await run_live_done_gate_v4._run(
            args,
            expected_companion_build_identity=_verified_test_companion_build_identity(),
        )
        == 2
    )
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["failure"]["stage"] == "validate_flexible_live_job_result"
    assert "expected model enhancement to be enabled" in evidence["failure"]["message"]
    assert delete_calls == [
        "http://tripchord.test/api/v1/agents/live-flexible-plan-from-text/jobs/live-job-fixture"
    ]
    assert evidence["live_job_control"]["cancellation_receipt"]["outcome"] == ("acknowledged")
    assert evidence["live_job_control"]["cancellation_receipt"]["state"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(("before_count", "after_count"), [(11, 11), (11, 10)])
async def test_v4_job_bound_trace_receipt_ignores_non_positive_global_delta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    before_count: int,
    after_count: int,
) -> None:
    _install_failed_run_formal_control_double(monkeypatch)
    runtime_payloads = iter(
        (
            _runtime_payload(model_trace_count=before_count),
            _runtime_payload(model_trace_count=after_count),
        )
    )
    response_payload = {
        "interpretation": {"state": "ready"},
        "execution_boundary": "fixture boundary",
        "model_enhancement_enabled": True,
        "model_trace_scope_sha256": _api_payload_sha256(),
        "model_trace_count": 3,
        "model_trace_success_count": 3,
        "model_trace_failure_count": 0,
        "cached_pair_runs": [],
        "run": {"validated": "by test double"},
        **_formal_worker_model_receipts(trace_count=3),
    }

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            if not url.endswith("/api/v1/agents/runtime"):
                return httpx.Response(
                    200,
                    json=_job_snapshot(
                        state="succeeded",
                        revision=2,
                        stage="succeeded",
                        progress=100,
                        result=response_payload,
                        pair_checkpoints=_three_checkpoints(),
                        model_trace_success_count=3,
                        model_trace_failure_count=0,
                    ),
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json=next(runtime_payloads),
                request=httpx.Request("GET", url),
            )

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> httpx.Response:
            return httpx.Response(
                202,
                json=_started_job_payload(),
                request=httpx.Request("POST", url, json=json),
            )

    async def companion_preflight(*_: object, **__: object) -> dict[str, Any]:
        return _companion_preflight_payload()

    flexible_run = SimpleNamespace(
        recommended_option_ids=(),
        pair_runs=_three_pair_runs(),
        model_dump=lambda **_: {"fixture": "flexible"},
    )

    monkeypatch.setattr(
        run_live_done_gate_v4.httpx,
        "AsyncClient",
        lambda **_: FakeClient(),
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "_preflight_companion",
        companion_preflight,
    )
    monkeypatch.setattr(
        run_live_done_gate_v4,
        "FlexibleLiveAgentRun",
        SimpleNamespace(model_validate=lambda _: flexible_run),
    )

    def evaluate_done_gate(*_: object, **__: object) -> Any:
        return run_live_done_gate_v4.LiveV4DoneGateReport.model_validate(
            {
                "passed": False,
                "checks": [
                    {
                        "name": "fixture_no_recommendation",
                        "passed": False,
                        "summary": "fixture",
                    }
                ],
            }
        )

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "evaluate_live_v4_done_gate",
        evaluate_done_gate,
    )
    output = tmp_path / "required-model-trace-delta.json"
    args = SimpleNamespace(
        request=SCENARIO,
        output=output,
        api_base="http://tripchord.test",
        api_token="",
        bridge_token="fixture-bridge-token-that-is-long-enough",
        request_timeout_seconds=900.0,
        maximum_quote_age_minutes=15,
        minimum_recommendable_options=2,
        require_model_enhancement=True,
        gate_run_id="fixture-trace-delta-run",
        formal_source_control_token_file=None,
    )

    assert (
        await run_live_done_gate_v4._run(
            args,
            expected_companion_build_identity=_verified_test_companion_build_identity(),
        )
        == 2
    )
    evidence = json.loads(output.read_text(encoding="utf-8"))
    expected_delta = after_count - before_count

    assert "failure" not in evidence
    assert evidence["done_gate"]["passed"] is False
    assert evidence["model_trace_receipt"] == {
        "scope_sha256": _api_payload_sha256(),
        "total_count": 3,
        "success_count": 3,
        "failure_count": 0,
    }
    assert evidence["process_global_model_trace_diagnostic"]["delta"] == expected_delta
    assert evidence["process_global_model_trace_diagnostic"]["authoritative"] is False
    assert evidence["runtime_before_run"]["model_trace_count"] == before_count
    assert evidence["runtime_after_run"]["model_trace_count"] == after_count


def test_v4_selected_option_uses_ranked_date_pair_not_colon_splitting() -> None:
    selected_plan = system_stay_plan_candidate_set().stay_plan_ids[0]
    pair_id = "date-pair:2026-08-12:2026-08-18:abc123"
    option_id = f"{pair_id}:{selected_plan.value}"
    previous_candidate = SimpleNamespace(id="candidate:exploration")
    refreshed_candidate = SimpleNamespace(id="candidate:publication")
    exploration_run = SimpleNamespace(
        evidence_scope=LiveEvidenceScope.FULL_SEARCH,
        package=SimpleNamespace(final_candidate=previous_candidate),
    )
    publication_run = SimpleNamespace(
        evidence_scope=LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH,
        selected_stay_plan_id=selected_plan,
        source_task_ids=("publication-source-ctrip-flight",),
        package=SimpleNamespace(final_candidate=refreshed_candidate),
    )
    refresh_audit = SimpleNamespace(
        binding_passed=True,
        refreshed_option_id=option_id,
        source_task_ids=publication_run.source_task_ids,
        previous_candidate_id=previous_candidate.id,
        refreshed_candidate_id=refreshed_candidate.id,
    )
    flexible = SimpleNamespace(
        recommended_option_ids=(option_id,),
        ranked_options=(
            SimpleNamespace(
                option_id=option_id,
                date_pair_id=pair_id,
                stay_plan_id=selected_plan,
            ),
        ),
        pair_runs=(
            SimpleNamespace(
                date_pair=SimpleNamespace(id=pair_id),
                run=publication_run,
                exploration_run=exploration_run,
                publication_refresh_audit=refresh_audit,
            ),
        ),
    )

    assert run_live_done_gate_v4._selected_option(flexible) == (
        option_id,
        pair_id,
        exploration_run,
        publication_run,
    )


def test_v4_selected_option_rejects_unbound_publication_evidence() -> None:
    selected_plan = system_stay_plan_candidate_set().stay_plan_ids[0]
    pair_id = "date-pair:2026-08-12:2026-08-18:abc123"
    option_id = f"{pair_id}:{selected_plan.value}"
    publication_run = SimpleNamespace(
        evidence_scope=LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH,
        selected_stay_plan_id=selected_plan,
        source_task_ids=("publication-source-ctrip-flight",),
        package=SimpleNamespace(final_candidate=SimpleNamespace(id="candidate:new")),
    )
    flexible = SimpleNamespace(
        recommended_option_ids=(option_id,),
        ranked_options=(
            SimpleNamespace(
                option_id=option_id,
                date_pair_id=pair_id,
                stay_plan_id=selected_plan,
            ),
        ),
        pair_runs=(
            SimpleNamespace(
                date_pair=SimpleNamespace(id=pair_id),
                run=publication_run,
                exploration_run=None,
                publication_refresh_audit=None,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="separate exploration and publication"):
        run_live_done_gate_v4._selected_option(flexible)


@pytest.mark.asyncio
async def test_v4_runner_posts_event_to_the_cached_run_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    event_body = {
        "event": {
            "id": "event:v4:test",
            "kind": "sold_out",
            "target_component_id": "lodging:test",
            "affected_provider": "ctrip",
            "source": "tripchord-synthetic-done-gate-fault-injection",
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        observed["method"] = request.method
        observed["path"] = request.url.path
        observed["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"run": {"event": "validated-by-test-double"}},
        )

    monkeypatch.setattr(
        run_live_done_gate_v4,
        "LiveEventReplanRun",
        SimpleNamespace(model_validate=lambda payload: payload),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        parsed = await run_live_done_gate_v4._request_event_replan(
            client,
            "http://127.0.0.1:8000",
            "cached-run-v4",
            event_body,
        )

    assert observed == {
        "method": "POST",
        "path": "/api/v1/agents/live-plans/cached-run-v4/events/replan",
        "json": event_body,
    }
    assert parsed == {"event": "validated-by-test-double"}


def test_repo_revision_identifies_committed_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import subprocess as _subprocess

    _subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "gate-test@example.com"],
        check=True,
    )
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Gate Test"],
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "baseline"],
        check=True,
    )

    monkeypatch.setattr(run_live_done_gate_v4, "_REPO_ROOT", tmp_path)
    clean = run_live_done_gate_v4._repo_revision()
    assert clean["worktree_dirty"] is False
    assert isinstance(clean["commit_sha"], str) and len(clean["commit_sha"]) >= 7
    assert clean["toplevel"] == str(tmp_path)
    assert clean["branch"] is not None

    tracked.write_text("modified\n", encoding="utf-8")
    dirty = run_live_done_gate_v4._repo_revision()
    assert dirty["worktree_dirty"] is True
    assert dirty["commit_sha"] == clean["commit_sha"]


def test_repo_revision_ignores_git_dir_env_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Counter-example (defect fix ④): a caller-injected GIT_DIR must not make
    the runner name a different repository as the evidence root."""
    import subprocess as _subprocess

    _subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "gate-test@example.com"],
        check=True,
    )
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Gate Test"],
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "baseline"],
        check=True,
    )
    # A decoy repo whose HEAD the environment would otherwise redirect git to.
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _subprocess.run(["git", "init", "-q", str(decoy)], check=True)
    _subprocess.run(
        ["git", "-C", str(decoy), "config", "user.email", "gate-test@example.com"],
        check=True,
    )
    _subprocess.run(
        ["git", "-C", str(decoy), "config", "user.name", "Gate Test"],
        check=True,
    )
    decoy_tracked = decoy / "decoy.txt"
    decoy_tracked.write_text("decoy\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(decoy), "add", "decoy.txt"], check=True)
    _subprocess.run(
        ["git", "-C", str(decoy), "commit", "-q", "-m", "decoy"],
        check=True,
    )

    monkeypatch.setattr(run_live_done_gate_v4, "_REPO_ROOT", tmp_path)
    env = run_live_done_gate_v4._git_safe_env()
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env

    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    revision = run_live_done_gate_v4._repo_revision()
    assert revision["toplevel"] == str(tmp_path)
    assert revision["commit_sha"] != _subprocess.run(
        ["git", "-C", str(decoy), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_repo_revision_detects_mid_run_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Counter-example (defect fix ④): when the tree changes between the start
    snapshot and bundle time, the revision fails closed with a change flag."""
    import subprocess as _subprocess

    _subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "gate-test@example.com"],
        check=True,
    )
    _subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Gate Test"],
        check=True,
    )
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("baseline\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    _subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "baseline"],
        check=True,
    )
    monkeypatch.setattr(run_live_done_gate_v4, "_REPO_ROOT", tmp_path)

    start = run_live_done_gate_v4._repo_revision()
    tracked.write_text("changed mid-run\n", encoding="utf-8")
    changed = run_live_done_gate_v4._repo_revision(start)
    assert changed["revision_changed_during_run"] is True
    assert changed["start_revision"]["commit_sha"] == start["commit_sha"]

    # A clean end against the same start carries no change flag.
    tracked.write_text("baseline\n", encoding="utf-8")
    _subprocess.run(["git", "-C", str(tmp_path), "checkout", "--", "tracked.txt"], check=True)
    unchanged = run_live_done_gate_v4._repo_revision(start)
    assert unchanged.get("revision_changed_during_run") is None


# ---------------------------------------------------------------------------
# runtime provenance preflight (round 14 counter-examples: all exit 2)
# ---------------------------------------------------------------------------


def _runtime_provenance_payload(
    *,
    commit_sha: str | None = None,
    repo_toplevel: str | None = None,
    dependency_lock_sha256: str | None = None,
    live_system_source_sha256: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    expected = local_expected_provenance()
    return {
        "repo_toplevel": expected["repo_toplevel"] if repo_toplevel is None else repo_toplevel,
        "commit_sha": expected["commit_sha"] if commit_sha is None else commit_sha,
        "dependency_lock_sha256": (
            expected["dependency_lock_sha256"]
            if dependency_lock_sha256 is None
            else dependency_lock_sha256
        ),
        "live_system_source_sha256": (
            expected["live_system_source_sha256"]
            if live_system_source_sha256 is None
            else live_system_source_sha256
        ),
        "started_at": "2026-08-10T00:00:00+00:00",
        "pid": os.getpid() if pid is None else pid,
        "python_version": "3.12",
        "python_executable": sys.executable,
    }


def _runtime_with_provenance(**overrides: Any) -> dict[str, Any]:
    payload = _runtime_payload()
    payload["runtime_provenance"] = _runtime_provenance_payload(**overrides)
    return payload


def test_v4_runtime_provenance_accepts_matching() -> None:
    runtime = _runtime_with_provenance()
    run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_stale_commit() -> None:
    runtime = _runtime_with_provenance(commit_sha="0" * 40)
    with pytest.raises(RuntimeError, match="commit_sha"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_old_api_process() -> None:
    # Counter-example: a worker started before the current HEAD reports its
    # startup commit, which differs from the current tree — hard-fail (exit 2),
    # never a pass based on a clean working tree alone.
    runtime = _runtime_with_provenance(commit_sha="1" * 40)
    with pytest.raises(RuntimeError, match="commit_sha"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_head_changed_without_restart() -> None:
    # Counter-example: HEAD moved after the worker started and the worker was
    # not restarted — the running memory code is stale, exit 2.
    runtime = _runtime_with_provenance(commit_sha="2" * 40)
    with pytest.raises(RuntimeError, match="commit_sha"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_wrong_live_system_fingerprint() -> None:
    # Counter-example: the running live_system source differs from the current
    # tree (changed on disk without a restart) — exit 2.
    runtime = _runtime_with_provenance(live_system_source_sha256="3" * 64)
    with pytest.raises(RuntimeError, match="live_system_source_sha256"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_wrong_lock_fingerprint() -> None:
    runtime = _runtime_with_provenance(dependency_lock_sha256="4" * 64)
    with pytest.raises(RuntimeError, match="dependency_lock_sha256"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_foreign_toplevel() -> None:
    runtime = _runtime_with_provenance(repo_toplevel="/elsewhere")
    with pytest.raises(RuntimeError, match="repo_toplevel"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)


def test_v4_runtime_provenance_rejects_missing_provenance() -> None:
    runtime = dict(_runtime_payload())
    del runtime["runtime_provenance"]
    with pytest.raises(RuntimeError, match="no runtime_provenance"):
        run_live_done_gate_v4._validate_runtime_provenance(runtime)
