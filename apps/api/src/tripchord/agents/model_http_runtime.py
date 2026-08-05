from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol

import httpx

from tripchord.agents.model_gateway import ModelHTTPPostClient


class ModelHTTPRuntimeError(RuntimeError):
    """Raised when the shared model HTTP client is used outside its lifespan."""


class ModelHTTPRuntimeState(StrEnum):
    NEW = "new"
    STARTED = "started"
    CLOSING = "closing"
    CLOSED = "closed"


class ModelHTTPClientFactory(Protocol):
    def __call__(
        self,
        *,
        http2: bool,
        limits: httpx.Limits,
        timeout: httpx.Timeout,
    ) -> httpx.AsyncClient: ...


def _default_client_factory(
    *,
    http2: bool,
    limits: httpx.Limits,
    timeout: httpx.Timeout,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=http2, limits=limits, timeout=timeout)


class ManagedModelHTTPRuntime:
    """One lifespan-managed HTTP connection pool shared by all model clients.

    Provider adapters depend only on the narrow ``ModelHTTPPostClient`` protocol.
    This stable proxy owns the replace-free, lifespan-bound concrete client;
    connection pooling, process-wide admission and shutdown remain here.
    """

    def __init__(
        self,
        *,
        http2: bool = False,
        max_connections: int = 12,
        max_keepalive_connections: int = 12,
        max_in_flight: int = 12,
        keepalive_expiry_seconds: float = 30,
        timeout_seconds: float = 45,
        client_factory: ModelHTTPClientFactory = _default_client_factory,
    ) -> None:
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        if max_keepalive_connections < 0:
            raise ValueError("max_keepalive_connections cannot be negative")
        if max_keepalive_connections > max_connections:
            raise ValueError(
                "max_keepalive_connections cannot exceed max_connections"
            )
        if max_in_flight < 1 or max_in_flight > 12:
            raise ValueError("max_in_flight must be between 1 and 12")
        if keepalive_expiry_seconds <= 0:
            raise ValueError("keepalive_expiry_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._http2 = http2
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry_seconds,
        )
        self._client_factory = client_factory
        self._max_in_flight = max_in_flight
        self._timeout = httpx.Timeout(timeout_seconds)
        self._condition = asyncio.Condition()
        self._client: httpx.AsyncClient | None = None
        self._state = ModelHTTPRuntimeState.NEW
        self._active_requests = 0
        self._peak_active_requests = 0
        self._close_task: asyncio.Task[None] | None = None

    @property
    def http_client(self) -> ModelHTTPPostClient:
        """Return the stable provider-facing proxy without transferring ownership."""

        return self

    @property
    def state(self) -> ModelHTTPRuntimeState:
        return self._state

    @property
    def active_requests(self) -> int:
        return self._active_requests

    @property
    def peak_active_requests(self) -> int:
        return self._peak_active_requests

    async def start(self) -> None:
        async with self._condition:
            if self._state == ModelHTTPRuntimeState.STARTED:
                return
            if self._state in {
                ModelHTTPRuntimeState.CLOSING,
                ModelHTTPRuntimeState.CLOSED,
            }:
                raise ModelHTTPRuntimeError(
                    "model HTTP runtime cannot start after shutdown"
                )
            try:
                client = self._client_factory(
                    http2=self._http2,
                    limits=self._limits,
                    timeout=self._timeout,
                )
            except ImportError as exc:
                raise ModelHTTPRuntimeError(
                    "model HTTP/2 was enabled but its optional runtime dependency is unavailable"
                ) from exc
            self._client = client
            self._state = ModelHTTPRuntimeState.STARTED
            self._condition.notify_all()

    async def post(
        self,
        url: str | httpx.URL,
        *,
        content: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        json: Any = None,
        params: Any = None,
        headers: Any = None,
        cookies: Any = None,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: bool | Any = httpx.USE_CLIENT_DEFAULT,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
        extensions: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        async with self._condition:
            await self._condition.wait_for(
                lambda: (
                    self._state != ModelHTTPRuntimeState.STARTED
                    or self._active_requests < self._max_in_flight
                )
            )
            client = self._client
            if self._state != ModelHTTPRuntimeState.STARTED or client is None:
                raise ModelHTTPRuntimeError(
                    "model HTTP runtime is not started or has already closed"
                )
            self._active_requests += 1
            self._peak_active_requests = max(
                self._peak_active_requests,
                self._active_requests,
            )
        try:
            return await client.post(
                url,
                content=content,
                data=data,
                files=files,
                json=json,
                params=params,
                headers=headers,
                cookies=cookies,
                auth=auth,
                follow_redirects=follow_redirects,
                timeout=timeout,
                extensions=extensions,
            )
        finally:
            async with self._condition:
                self._active_requests -= 1
                self._condition.notify_all()

    async def _close(self) -> None:
        client: httpx.AsyncClient | None = None
        async with self._condition:
            if self._state == ModelHTTPRuntimeState.NEW:
                self._state = ModelHTTPRuntimeState.CLOSED
                self._condition.notify_all()
                return
            self._state = ModelHTTPRuntimeState.CLOSING
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._active_requests == 0)
            client = self._client
            self._client = None
        try:
            if client is not None:
                await client.aclose()
        finally:
            async with self._condition:
                self._state = ModelHTTPRuntimeState.CLOSED
                self._condition.notify_all()

    async def aclose(self) -> None:
        async with self._condition:
            if self._state == ModelHTTPRuntimeState.CLOSED:
                return
            if self._close_task is None:
                if self._state == ModelHTTPRuntimeState.STARTED:
                    self._state = ModelHTTPRuntimeState.CLOSING
                    self._condition.notify_all()
                self._close_task = asyncio.create_task(self._close())
            close_task = self._close_task
        # A cancelled shutdown caller must not strand the shared runtime in
        # CLOSING.  The one owned task continues and later callers await it.
        await asyncio.shield(close_task)
