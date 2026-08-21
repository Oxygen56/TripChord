from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from tripchord.agents.live_flexible_worker_runtime import (
    _verified_runtime_spec,
    build_authenticated_runtime_bundle,
)
from tripchord.platform.adapters import default_browser_providers_from_registry
from tripchord.providers.browser_bridge import (
    BrowserClaimError,
    BrowserCompanionBuildIdentity,
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskBridge,
    BrowserTaskCompletion,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
)


def test_source_terminal_events_include_exploration_when_publication_is_empty() -> None:
    from tripchord.main import _source_terminal_events_from_run

    coverage = SimpleNamespace(
        terminal_outcome_source_ids=("source-ctrip-flight",),
        successful_source_ids=("source-ctrip-flight",),
        usable_quote_source_ids=("source-ctrip-flight",),
        failed_source_ids=(),
        failure_reasons=(),
    )
    exploration = SimpleNamespace(
        source_execution_completeness=SimpleNamespace(
            terminal_source_ids=("source-ctrip-flight",)
        ),
        coverage=(coverage,),
    )
    publication = SimpleNamespace(
        source_execution_completeness=SimpleNamespace(terminal_source_ids=()),
        coverage=(),
    )
    run = SimpleNamespace(
        pair_runs=(
            SimpleNamespace(
                exploration_run=exploration,
                run=publication,
            ),
        )
    )

    events = _source_terminal_events_from_run(
        run, datetime(2026, 8, 17, tzinfo=UTC)
    )

    assert [(event.source_task_id, event.terminal_state) for event in events] == [
        ("source-ctrip-flight", "quote_found")
    ]


def test_source_terminal_events_include_typed_failed_sources() -> None:
    from tripchord.main import _source_terminal_events_from_run

    coverage = SimpleNamespace(
        terminal_outcome_source_ids=(),
        successful_source_ids=(),
        usable_quote_source_ids=(),
        failed_source_ids=("source-ctrip-flight",),
        failure_reasons=("source-ctrip-flight: captcha_required",),
    )
    failed_run = SimpleNamespace(
        source_execution_completeness=SimpleNamespace(
            terminal_source_ids=("source-ctrip-flight",)
        ),
        coverage=(coverage,),
    )
    run = SimpleNamespace(
        pair_runs=(
            SimpleNamespace(
                exploration_run=None,
                run=failed_run,
            ),
        )
    )

    events = _source_terminal_events_from_run(
        run, datetime(2026, 8, 17, tzinfo=UTC)
    )

    assert [(event.source_task_id, event.terminal_state) for event in events] == [
        ("source-ctrip-flight", "captcha_required")
    ]


@pytest.mark.asyncio
async def test_settle_reports_typed_source_before_a_later_pair_failure() -> None:
    from tripchord.agents.context import ContextEngine, EvidenceBlackboard
    from tripchord.agents.live_system import LivePackageAgentSystem, _RunState
    from tripchord.agents.models import AgentRole, AgentTask
    from tripchord.agents.tools import ToolRegistry
    from tripchord.providers.browser_bridge import BrowserTaskSnapshot

    now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    reported: list[dict[str, object]] = []

    async def report(events: tuple[dict[str, object], ...]) -> None:
        reported.extend(events)

    system = LivePackageAgentSystem(
        BrowserTaskBridge(now=lambda: now),
        providers=(BrowserProvider.CTRIP,),
        now=lambda: now,
        source_terminal_reporter=report,
    )
    state = _RunState(source_task_ids=("source-ctrip-flight",))
    state.snapshots["source-ctrip-flight"] = BrowserTaskSnapshot(
        id="browser-task-typed-failure",
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.FLIGHT,
        query=BrowserSearchQuery(
            origin="上海",
            destination="马累",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 30),
            adults=2,
        ),
        state=BrowserTaskState.BLOCKED,
        created_at=now,
        updated_at=now,
        attempt_count=1,
        failure=BrowserFailure(
            code=BrowserFailureCode.CAPTCHA_REQUIRED,
            message="visible challenge",
            captured_at=now,
        ),
    )
    settle = system._settle_executor(state)

    await settle(
        AgentTask(
            id="settle-source-barrier",
            role=AgentRole.EXECUTOR,
            goal="report typed source terminal states",
        ),
        ContextEngine(EvidenceBlackboard()),
        ToolRegistry(),
    )

    assert reported == [
        {
            "schema_version": "live-source-terminal-event-v1",
            "source_task_id": "source-ctrip-flight",
            "provider": "ctrip",
            "vertical": "flight",
            "terminal_state": "captcha_required",
            "occurred_at": now.isoformat(),
            "detail": None,
        }
    ]


class _FormalTaskScope:
    """Small authority boundary used only to mark a submitted task formal."""

    def current_execution_capability(self) -> dict[str, object]:
        return {
            "schema_version": "tripchord-formal-execution-capability-v1",
            "challenge_id": "formal-source-attestation-challenge",
            "terminal_job_id": "live-job-formal-source-attestation",
            "request_sha256": "1" * 64,
            "attempt_digest": "2" * 64,
        }

    @contextmanager
    def execution_scope(
        self,
        capability: object,
    ) -> Iterator[None]:
        if capability != self.current_execution_capability():
            raise ValueError("foreign formal execution capability")
        yield


def _formal_runtime_spec(*, model_agents_required: bool) -> dict[str, object]:
    return {
        "runtime": "browser-bridge",
        "bridge_token": "formal-runtime-attestation-token-" + "b" * 32,
        "providers": [
            provider.value
            for provider in default_browser_providers_from_registry()
        ],
        "model_agents_required": model_agents_required,
        "formal_parent_api_origin": "http://127.0.0.1:43122",
        "adaptive_agent_scaling_enabled": False,
        "now_iso": "2026-08-17T09:00:00+08:00",
        "http_host": None,
        "http_port": None,
        "icom_api_origin": None,
        "formal_source_private_key_path": None,
        "formal_source_ledger_path": None,
    }


@pytest.mark.asyncio
async def test_formal_worker_reaches_parent_browser_queue_over_real_tcp() -> None:
    """The worker facade must submit to the parent queue, not a local ASGI fake."""

    from tripchord.providers.browser_bridge import (
        BRIDGE_TOKEN_HEADER,
        create_browser_bridge_app,
        formal_worker_source_token,
    )
    from tripchord.providers.formal_parent_source import FormalParentSourceClient

    token = "formal-parent-real-tcp-token-" + "a" * 32
    worker_source_token = formal_worker_source_token(token)
    authority = _FormalTaskScope()
    bridge = BrowserTaskBridge(source_authority=authority)  # type: ignore[arg-type]
    bridge_app = create_browser_bridge_app(
        bridge,
        bridge_token=token,
        source_authority=authority,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.mount("/browser-bridge", bridge_app)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off")
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    async def wait_until_started() -> None:
        while not server.started:
            if server_task.done():
                await server_task
                raise AssertionError("formal parent TCP server exited before startup")
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_until_started(), timeout=5.0)
    client = FormalParentSourceClient(
        parent_api_origin=f"http://127.0.0.1:{port}",
        source_token=worker_source_token,
        execution_capability=authority.current_execution_capability(),
    )
    submission = BrowserTaskSubmission(
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.LODGING,
        query=BrowserSearchQuery(
            destination="马尔代夫",
            start_date=date(2026, 8, 23),
            end_date=date(2026, 8, 30),
            adults=2,
            rooms=1,
        ),
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
        ) as boundary_client:
            worker_cannot_impersonate_companion = await boundary_client.get(
                "/browser-bridge/v1/companions/status",
                headers={BRIDGE_TOKEN_HEADER: worker_source_token},
            )
            companion_cannot_impersonate_worker = await boundary_client.post(
                "/browser-bridge/v1/formal/tasks",
                headers={BRIDGE_TOKEN_HEADER: token},
                json={
                    "execution_capability": (
                        authority.current_execution_capability()
                    ),
                    "tasks": [submission.model_dump(mode="json")],
                },
            )
        assert worker_cannot_impersonate_companion.status_code == 401
        assert companion_cannot_impersonate_worker.status_code == 401

        submitted = await client.submit_many((submission,))
        assert len(submitted) == 1
        task_id = submitted[0].id
        assert await bridge.formal_execution_capability(task_id) == (
            authority.current_execution_capability()
        )

        cancelled = await client.cancel_many(
            (task_id,),
            reason="bounded control-plane proof",
        )
        assert [item.state for item in cancelled] == [BrowserTaskState.CANCELLED]
        settled = await client.wait_many((task_id,), timeout_seconds=1.0)
        assert [item.state for item in settled] == [BrowserTaskState.CANCELLED]
    finally:
        await client.aclose()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=5.0)
        listener.close()


@pytest.mark.asyncio
async def test_formal_browser_task_rejects_completion_without_source_attestation() -> None:
    """A claimed formal task cannot be completed by knowing only its lease token."""

    now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    bridge = BrowserTaskBridge(
        source_authority=_FormalTaskScope(),  # type: ignore[arg-type]
        now=lambda: now,
    )
    task = (
        await bridge.submit_many(
            (
                BrowserTaskSubmission(
                    provider=BrowserProvider.CTRIP,
                    kind=BrowserVertical.LODGING,
                    query=BrowserSearchQuery(
                        destination="马累",
                        start_date=date(2026, 8, 23),
                        end_date=date(2026, 8, 30),
                        adults=2,
                        rooms=1,
                    ),
                ),
            )
        )
    )[0]
    claim = await bridge.claim_response(
        "formal-companion",
        providers=(BrowserProvider.CTRIP,),
        limit=1,
        build_identity=BrowserCompanionBuildIdentity(
            manifest_version="0.1.0",
            build_sha256="3" * 64,
            content_runtime_version="2026-08-17.1",
        ),
        runtime_instance_id="formal-runtime-instance-0001",
    )
    lease = claim.leases[0]
    completion = BrowserTaskCompletion(
        state=BrowserTaskState.BLOCKED,
        failure=BrowserFailure(
            code=BrowserFailureCode.CAPTCHA_REQUIRED,
            message="visible challenge",
            captured_at=now,
        ),
    )

    with pytest.raises(BrowserClaimError, match="source execution attestation"):
        await bridge.complete(task.id, lease.claim_token, completion)


@pytest.mark.asyncio
async def test_formal_source_receipt_hashes_its_serialized_timestamp() -> None:
    import hashlib
    import json

    from tripchord.providers.browser_bridge import (
        BrowserSourceExecutionAttestation,
    )

    def digest(value: object) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    class AcceptingAuthority:
        def __init__(self) -> None:
            self.capability = {
                "capability_id": "b18ced5a-a8db-45e0-9fe1-64c18d0f859d",
                "challenge_id": "receipt-time-challenge",
                "run_id": "receipt-time-run",
                "terminal_job_id": "live-job-receipt-time",
                "request_sha256": "1" * 64,
                "job_graph_sha256": "2" * 64,
                "attempt_digest": "3" * 64,
            }

        def current_execution_capability(self) -> dict[str, object]:
            return dict(self.capability)

        @contextmanager
        def execution_scope(self, capability: object) -> Any:
            assert capability == self.capability
            yield

        def formal_browser_query(self, **_kwargs: object) -> dict[str, object]:
            return {}

        def record_browser_http(self, *_args: object, **_kwargs: object) -> None:
            return None

    now = datetime(2026, 8, 17, 1, 2, 3, tzinfo=UTC)
    authority = AcceptingAuthority()
    bridge = BrowserTaskBridge(
        source_authority=authority,  # type: ignore[arg-type]
        now=lambda: now,
    )
    query = BrowserSearchQuery(
        destination="马累",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        rooms=1,
    )
    task = (
        await bridge.submit_many(
            (
                BrowserTaskSubmission(
                    provider=BrowserProvider.CTRIP,
                    kind=BrowserVertical.LODGING,
                    query=query,
                ),
            )
        )
    )[0]
    build_identity = BrowserCompanionBuildIdentity(
        manifest_version="0.1.0",
        build_sha256="4" * 64,
        content_runtime_version="2026-08-17.1",
    )
    claim = await bridge.claim_response(
        "formal-companion",
        providers=(BrowserProvider.CTRIP,),
        limit=1,
        build_identity=build_identity,
        runtime_instance_id="formal-runtime-instance-0001",
    )
    completion = BrowserTaskCompletion(
        state=BrowserTaskState.BLOCKED,
        failure=BrowserFailure(
            code=BrowserFailureCode.CAPTCHA_REQUIRED,
            message="visible challenge",
            captured_at=now,
        ),
    )
    query_payload = query.model_dump(mode="json", exclude_none=True)
    observation = {
        "task_id": task.id,
        "provider": "ctrip",
        "kind": "lodging",
        "query": query_payload,
        "quote_evidence_sha256": [],
        "parser_version": "tripchord-visible-dom-v3",
    }
    attestation = BrowserSourceExecutionAttestation(
        task_id=task.id,
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.LODGING,
        companion_id="formal-companion",
        runtime_instance_id="formal-runtime-instance-0001",
        build_identity=build_identity,
        execution_environment="chrome_extension_service_worker",
        parser_version="tripchord-visible-dom-v3",
        query_sha256=digest(query_payload),
        source_observation_sha256=digest(observation),
        completed_at=now,
    )

    await bridge.complete(
        task.id,
        claim.leases[0].claim_token,
        completion,
        attestation,
    )
    receipt = await bridge.source_execution_receipt(task.id)

    assert receipt is not None
    payload = receipt.model_dump(mode="json")
    assert payload["receipt_sha256"] == digest(
        {key: value for key, value in payload.items() if key != "receipt_sha256"}
    )


def test_formal_worker_runtime_rejects_model_agents_disabled() -> None:
    bundle = build_authenticated_runtime_bundle(
        _formal_runtime_spec(model_agents_required=False)
    )

    with pytest.raises(RuntimeError, match="requires model agents"):
        _verified_runtime_spec(bundle)


def test_formal_worker_runtime_rejects_missing_model_identity() -> None:
    bundle = build_authenticated_runtime_bundle(
        _formal_runtime_spec(model_agents_required=True)
    )

    with pytest.raises(RuntimeError, match="model runtime identity"):
        _verified_runtime_spec(bundle)


def test_formal_runtime_bundle_rejects_the_companion_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker bundle may carry only the one-way parent-source credential."""

    import json

    import tripchord.main as main_module

    companion_token = "formal-companion-only-token-" + "c" * 32
    monkeypatch.setattr(
        main_module,
        "settings",
        main_module.settings.model_copy(
            update={"browser_bridge_token": companion_token}
        ),
    )
    spec = _formal_runtime_spec(model_agents_required=True)
    spec["bridge_token"] = companion_token
    spec["model_runtime_identity"] = {
        "provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "primary_model": "gpt-oss:20b",
        "fast_model": "gpt-oss:20b",
    }
    monkeypatch.setenv(
        "TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE",
        json.dumps(spec),
    )

    with pytest.raises(ValueError, match="separated parent-source token"):
        main_module._live_flexible_worker_runtime_bundle()
