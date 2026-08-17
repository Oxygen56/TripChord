"""Production worker-runtime handoff for the live flexible-from-text worker.

C-146 P0-1 (RETURN 7de8cf3e): a worker subprocess reconstructs the API app fresh
from its environment; without a configured browser bridge the reconstructed
``flexible_live_agent_system`` is ``None`` and the ready chain fails with an HTTP
503. This module is the PRODUCTION configuration handoff that makes the ready
chain runnable in a REAL independent process WITHOUT monkeypatching a private
runtime into the app:

- The API process embeds a ``runtime_bundle`` (a JSON spec, sourced from
  ``TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE``) into the worker command args
  when it builds the command.
- The worker subprocess, after reconstructing its own app, installs the bundle's
  runtime on it by calling the SAME production composition entry the API process
  uses (``tripchord.main._install_browser_bridge``): a REAL ``BrowserTaskBridge``,
  a REAL ``LivePackageAgentSystem``, and a REAL ``FlexibleLiveAgentSystem`` are
  built IN the worker process from the spec — never an injected/patched
  in-process object and never the former deterministic HUMAN_BLOCK stand-in.

When the bundle carries an ``http_port``, the worker serves the reconstructed app
over REAL loopback HTTP so an external Companion can heartbeat / claim / complete
bridge tasks over the network — the production cross-process ready chain, not an
in-process ASGI substitute.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from tripchord.runtime_provenance import PROVENANCE, provenance_mismatches

_RUNTIME_ENVELOPE_SCHEMA = "tripchord-live-worker-runtime-envelope-v1"
_RUNTIME_RECEIPT_SCHEMA = "tripchord-live-worker-runtime-receipt-v1"
_PROVENANCE_FIELDS = (
    "repo_toplevel",
    "commit_sha",
    "dependency_lock_sha256",
    "live_system_source_sha256",
)
_API_RUNTIME_IDENTITY_FIELDS = frozenset(PROVENANCE.to_dict())
_SPEC_FIELDS = frozenset(
    {
        "runtime",
        "bridge_token",
        "providers",
        "model_agents_required",
        "model_runtime_identity",
        "formal_parent_api_origin",
        "adaptive_agent_scaling_enabled",
        "now_iso",
        "http_host",
        "http_port",
        "icom_api_origin",
        "formal_source_private_key_path",
        "formal_source_ledger_path",
    }
)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("live worker runtime configuration is not canonical JSON") from exc


def _runtime_provenance_identity() -> dict[str, str]:
    raw = PROVENANCE.to_dict()
    identity: dict[str, str] = {}
    for field in _PROVENANCE_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"live worker runtime provenance lacks a canonical {field}"
            )
        identity[field] = value
    return identity


def build_authenticated_runtime_bundle(spec: dict[str, Any]) -> dict[str, Any]:
    """Bind one JSON runtime spec to the API process's immutable provenance.

    The parent calls this before constructing the worker command. The worker
    independently recomputes the spec digest and compares every static runtime
    provenance field against the code it imported. A stale/foreign/tampered
    handoff therefore fails closed before any bridge, provider, HTTP listener or
    planning task is created. The receipt returned by ``install_runtime_bundle``
    exposes the same non-secret binding to the parent job result.
    """
    if not isinstance(spec, dict):
        raise ValueError("live worker runtime configuration must be an object")
    canonical = _canonical_json(spec)
    return {
        "schema_version": _RUNTIME_ENVELOPE_SCHEMA,
        "spec": spec,
        "spec_sha256": hashlib.sha256(canonical).hexdigest(),
        "runtime_provenance": _runtime_provenance_identity(),
        # Full parent-API identity, including PID/start time.  The child checks
        # that this live PID is its direct parent before it may continue the
        # parent's formal source ledger; a copied bundle in an unrelated process
        # cannot impersonate the formal coordinator.
        "api_runtime_identity": PROVENANCE.to_dict(),
    }


def _verified_runtime_spec(
    bundle: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    expected_fields = {
        "schema_version",
        "spec",
        "spec_sha256",
        "runtime_provenance",
        "api_runtime_identity",
    }
    if set(bundle) != expected_fields or bundle.get("schema_version") != (
        _RUNTIME_ENVELOPE_SCHEMA
    ):
        raise RuntimeError("live worker runtime envelope is invalid")
    spec = bundle.get("spec")
    digest = bundle.get("spec_sha256")
    provenance = bundle.get("runtime_provenance")
    api_runtime_identity = bundle.get("api_runtime_identity")
    if not isinstance(spec, dict) or set(spec) - _SPEC_FIELDS:
        raise RuntimeError("live worker runtime spec fields are invalid")
    if not isinstance(digest, str) or len(digest) != 64:
        raise RuntimeError("live worker runtime spec digest is invalid")
    actual_digest = hashlib.sha256(_canonical_json(spec)).hexdigest()
    if not hmac.compare_digest(digest, actual_digest):
        raise RuntimeError("live worker runtime spec digest does not match")
    current_provenance = _runtime_provenance_identity()
    if not isinstance(provenance, dict) or set(provenance) != set(
        _PROVENANCE_FIELDS
    ):
        raise RuntimeError("live worker runtime provenance binding is invalid")
    for field, current in current_provenance.items():
        reported = provenance.get(field)
        if not isinstance(reported, str) or not hmac.compare_digest(
            reported.encode("utf-8"),
            current.encode("utf-8"),
        ):
            raise RuntimeError(
                f"live worker runtime provenance {field} does not match"
            )
    if (
        not isinstance(api_runtime_identity, dict)
        or set(api_runtime_identity) != _API_RUNTIME_IDENTITY_FIELDS
    ):
        raise RuntimeError("live worker API runtime identity is invalid")
    if provenance_mismatches(api_runtime_identity, current_provenance):
        raise RuntimeError("live worker API runtime identity does not match live code")
    formal_paths_present = any(
        spec.get(field) is not None
        for field in (
            "formal_source_private_key_path",
            "formal_source_ledger_path",
        )
    )
    formal_parent_present = spec.get("formal_parent_api_origin") is not None
    formal_runtime = formal_paths_present or formal_parent_present
    if formal_runtime and spec.get("model_agents_required") is not True:
        raise RuntimeError("formal live worker runtime requires model agents")
    if formal_paths_present:
        raise RuntimeError(
            "formal live worker must use the parent-owned source authority"
        )
    model_identity = spec.get("model_runtime_identity")
    expected_model_fields = {
        "provider",
        "base_url",
        "primary_model",
        "fast_model",
    }
    if formal_runtime and (
        not isinstance(model_identity, dict)
        or set(model_identity) != expected_model_fields
        or model_identity.get("provider") not in {"anthropic", "openai_compatible"}
        or any(
            not isinstance(model_identity.get(field), str)
            or not model_identity.get(field)
            for field in ("base_url", "primary_model", "fast_model")
        )
    ):
        raise RuntimeError("formal live worker model runtime identity is invalid")
    return spec, current_provenance, dict(api_runtime_identity)


def _parse_clock(spec: dict[str, Any]) -> Callable[[], datetime]:
    now_iso = spec.get("now_iso")
    if now_iso is None:
        return lambda: datetime.now(UTC)
    if not isinstance(now_iso, str):
        raise RuntimeError("worker browser-bridge runtime requires now_iso")
    try:
        fixed_now = datetime.fromisoformat(now_iso)
    except ValueError as exc:
        raise RuntimeError("worker browser-bridge runtime now_iso is invalid") from exc
    if fixed_now.tzinfo is None or fixed_now.utcoffset() is None:
        raise RuntimeError("worker browser-bridge runtime now_iso must be timezone-aware")

    def fixed_now_clock() -> datetime:
        return fixed_now

    return fixed_now_clock


async def _serve_loopback_http(target_app: Any, host: str, port: int) -> None:
    """Serve ``target_app`` over REAL loopback HTTP until shutdown.

    The app is served with its lifespan disabled: the worker owns its own bridge
    composition (already mounted by ``_install_browser_bridge``) and must not run
    the API process's startup/shutdown resource lifecycle. Only the mounted
    ``/browser-bridge`` routes are consumed by the external Companion.
    """
    import uvicorn

    config = uvicorn.Config(
        target_app,
        host=host,
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    # The worker is a subprocess; uvicorn's default SIGINT/SIGTERM handlers must
    # not touch this process group's signal disposition. Stash the server on the
    # app so the worker entry can shut it down after the operation.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    target_app.state.live_worker_http_server = server
    await server.serve()


def _path_or_none(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


class _CanonicalIComLoopbackTransport(httpx.AsyncBaseTransport):
    """Forward canonical iCom HTTPS requests over a real loopback connection.

    Formal source evidence must retain the exact public HTTPS URL. Replacing the
    provider's configured origin with ``http://127.0.0.1`` makes the fetch real
    but falsifies that signed identity. This transport keeps the request and
    response URL canonical for every provider/formal validator while rewriting
    only the socket destination to the explicitly authenticated loopback
    harness. The default inner transport is a real ``AsyncHTTPTransport``;
    tests may inject a bounded transport to verify the rewrite contract.
    """

    def __init__(
        self,
        loopback_origin: str,
        *,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(loopback_origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("worker iCom loopback transport origin is invalid")
        self._host = parsed.hostname
        self._port = parsed.port
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.scheme != "https"
            or request.url.host.casefold() != "sfs-api.icomtours.com"
            or request.url.port not in {None, 443}
        ):
            raise httpx.ConnectError(
                "iCom loopback transport received a non-canonical origin",
                request=request,
            )
        target = request.url.copy_with(
            scheme="http",
            host=self._host,
            port=self._port,
        )
        headers = request.headers.copy()
        headers["host"] = f"{self._host}:{self._port}"
        forwarded = httpx.Request(
            method=request.method,
            url=target,
            headers=headers,
            content=await request.aread(),
            extensions=request.extensions,
        )
        response = await self._inner.handle_async_request(forwarded)
        # Preserve the ORIGINAL canonical request on the returned response. The
        # provider's response-boundary validator and formal event signer therefore
        # see only the exact public endpoint, never the local socket destination.
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=response.stream,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def install_runtime_bundle(
    target_app: Any,
    bundle: dict[str, Any],
    *,
    formal_execution_capability: dict[str, object] | None = None,
    source_terminal_reporter: Callable[
        [tuple[dict[str, Any], ...]],
        Awaitable[None],
    ]
    | None = None,
) -> dict[str, Any]:
    """Install the worker's ``runtime_bundle`` onto its reconstructed app.

    The worker subprocess imports ``tripchord.main`` fresh; with no browser
    bridge configured its ``flexible_live_agent_system`` is None. This builds a
    REAL composition in THIS process from the bundle spec — via the SAME
    production ``_install_browser_bridge`` entry the API process uses — and
    installs it, so the ready chain's ``_flexible_live_agent_system_from_app``
    resolves a production system. The ``browser-bridge`` runtime is the only
    supported runtime; unknown runtimes fail closed (the former
    ``deterministic-blocking`` HUMAN_BLOCK stand-in is removed).
    """
    spec, provenance, api_runtime_identity = _verified_runtime_spec(bundle)
    if api_runtime_identity.get("pid") != os.getppid():
        raise RuntimeError("live worker API runtime is not this process's direct parent")
    runtime = spec.get("runtime")
    if runtime != "browser-bridge":
        raise RuntimeError(f"unknown live flexible worker runtime: {runtime!r}")
    import tripchord.main as api_main

    token = spec.get("bridge_token")
    if not isinstance(token, str) or len(token) < 32:
        raise RuntimeError(
            "worker browser-bridge runtime requires a bridge token of at least 32 characters"
        )
    providers = spec.get("providers")
    if not isinstance(providers, list) or not providers or not all(
        isinstance(provider, str) for provider in providers
    ):
        raise RuntimeError("worker browser-bridge runtime requires a non-empty providers list")
    from tripchord.platform.adapters import default_browser_providers_from_registry

    expected_providers = tuple(
        provider.value for provider in default_browser_providers_from_registry()
    )
    if len(set(providers)) != len(providers) or tuple(providers) != expected_providers:
        raise RuntimeError(
            "worker browser-bridge providers do not match the production registry"
        )
    for field in ("model_agents_required", "adaptive_agent_scaling_enabled"):
        if type(spec.get(field)) is not bool:
            raise RuntimeError(f"worker browser-bridge runtime {field} must be boolean")
    formal_parent_origin = spec.get("formal_parent_api_origin")
    remote_formal_source = formal_parent_origin is not None
    host = spec.get("http_host")
    port = spec.get("http_port")
    if remote_formal_source:
        if host is not None or port is not None:
            raise RuntimeError(
                "formal parent source runtime cannot start a second browser HTTP queue"
            )
        if not isinstance(formal_execution_capability, dict):
            raise RuntimeError(
                "formal parent source runtime requires the signed execution capability"
            )
    else:
        if host != "127.0.0.1":
            raise RuntimeError("worker browser-bridge HTTP host must be loopback")
        if type(port) is not int or not 1 <= port <= 65_535:
            raise RuntimeError("worker browser-bridge HTTP port is invalid")
    icom_api_origin = spec.get("icom_api_origin")
    icom_http_client: httpx.AsyncClient | None = None
    if remote_formal_source and icom_api_origin is not None:
        raise RuntimeError(
            "formal parent source runtime cannot install a worker-local iCom origin"
        )
    if icom_api_origin is not None:
        if not isinstance(icom_api_origin, str):
            raise RuntimeError("worker iCom API origin is invalid")
        parsed_origin = urlsplit(icom_api_origin)
        if (
            parsed_origin.scheme != "http"
            or parsed_origin.hostname not in {"127.0.0.1", "localhost"}
            or parsed_origin.port is None
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise RuntimeError("worker iCom API origin must be an exact loopback origin")
        icom_http_client = httpx.AsyncClient(
            transport=_CanonicalIComLoopbackTransport(icom_api_origin)
        )
    private_path = _path_or_none(spec.get("formal_source_private_key_path"))
    ledger_path = _path_or_none(spec.get("formal_source_ledger_path"))
    if private_path is not None or ledger_path is not None:
        raise RuntimeError("worker cannot load the parent formal signing authority")
    now = _parse_clock(spec)
    configured = api_main.settings.model_copy(
        update={
            "browser_bridge_enabled": True,
            "browser_bridge_token": token,
            "browser_bridge_control_token": None,
            "browser_companion_auto_reload_enabled": False,
            "model_agents_required": spec["model_agents_required"],
            "adaptive_agent_scaling_enabled": spec[
                "adaptive_agent_scaling_enabled"
            ],
        }
    )
    model_identity = spec.get("model_runtime_identity")
    if remote_formal_source:
        primary = api_main.settings.model_client_config()
        fast = api_main.settings.model_client_config(fast=True)
        actual_model_identity = {
            "provider": primary.provider.value if primary is not None else None,
            "base_url": primary.base_url if primary is not None else None,
            "primary_model": primary.model if primary is not None else None,
            "fast_model": fast.model if fast is not None else None,
        }
        if model_identity != actual_model_identity or api_main.model_router is None:
            raise RuntimeError(
                "worker model runtime identity does not match the imported production router"
            )
    remote_source = None
    if remote_formal_source:
        from tripchord.providers.formal_parent_source import (
            FormalParentSourceClient,
        )

        remote_source = FormalParentSourceClient(
            parent_api_origin=str(formal_parent_origin),
            source_token=token,
            execution_capability=formal_execution_capability,
        )
    bridge, live_system = api_main._install_browser_bridge(
        target_app,
        configured,
        now=now,
        sleep=asyncio.sleep,
        model_router=api_main.model_router,
        context_builder=api_main.context_builder,
        memory_store=api_main.memory_store,
        icom_http_client=icom_http_client,
        source_terminal_reporter=source_terminal_reporter,
        browser_bridge_override=remote_source,
        icom_provider_override=remote_source,
        mount_browser_bridge=not remote_formal_source,
        formal_source_owned_by_parent=remote_formal_source,
    )
    if bridge is None or live_system is None:
        raise RuntimeError("worker browser-bridge install did not produce a live system")
    target_app.state.live_worker_fixed_clock = now
    target_app.state.live_worker_icom_http_client = icom_http_client
    target_app.state.live_worker_parent_source_client = remote_source
    if not remote_formal_source:
        target_app.state.live_worker_http_host = host
        target_app.state.live_worker_http_port = port
        target_app.state.live_worker_http_server_task = asyncio.create_task(
            _serve_loopback_http(target_app, host, port),
            name="tripchord-live-worker-http",
        )
    return {
        "schema_version": _RUNTIME_RECEIPT_SCHEMA,
        "runtime": runtime,
        "providers": providers,
        "spec_sha256": bundle["spec_sha256"],
        "runtime_provenance": provenance,
        "api_runtime_identity_sha256": hashlib.sha256(
            _canonical_json(api_runtime_identity)
        ).hexdigest(),
        "worker_runtime_identity": PROVENANCE.to_dict(),
        "model_agents_required": spec["model_agents_required"],
        "model_runtime_identity": model_identity,
    }


async def start_runtime_model_http(target_app: Any) -> None:
    """Start the worker-owned production model transport before any agent call."""

    import tripchord.main as api_main

    runtime = api_main.model_http_runtime
    await runtime.start()
    target_app.state.live_worker_model_http_runtime = runtime


async def shutdown_runtime_http(target_app: Any) -> None:
    """Stop the worker's loopback HTTP server, if one was started."""
    server = getattr(target_app.state, "live_worker_http_server", None)
    task = getattr(target_app.state, "live_worker_http_server_task", None)
    if server is not None:
        server.should_exit = True
    if isinstance(task, asyncio.Task) and not task.done():
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, Exception):
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
    icom_client = getattr(target_app.state, "live_worker_icom_http_client", None)
    if isinstance(icom_client, httpx.AsyncClient):
        await icom_client.aclose()
    parent_source = getattr(
        target_app.state,
        "live_worker_parent_source_client",
        None,
    )
    if parent_source is not None:
        await parent_source.aclose()
    model_runtime = getattr(
        target_app.state,
        "live_worker_model_http_runtime",
        None,
    )
    if model_runtime is not None:
        await model_runtime.aclose()
