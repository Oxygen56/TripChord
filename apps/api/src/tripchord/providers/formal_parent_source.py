"""Production loopback client for a formal worker's parent-owned sources.

The independent planning worker must not create a second Browser queue that the
paired extension cannot see.  It also must not copy the formal-source signing
key merely to observe iCom traffic.  This module keeps both source capabilities
in the API process that owns the challenge and reaches them over authenticated
loopback HTTP from the worker process.

No test transport or domain result is accepted here: the client always uses a
normal ``httpx.AsyncClient`` unless an already-created production client is
explicitly supplied by the composition root.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx

from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    BrowserTaskSnapshot,
    BrowserTaskSubmission,
    SubmitBrowserTasksResponse,
)
from tripchord.providers.icom_transfer import (
    IComTransferQuery,
    IComTransferSearchResult,
)


def _parent_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("formal parent source origin must be exact loopback HTTP")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("formal parent source origin has an invalid port") from exc
    if port is None or not 1 <= port <= 65_535:
        raise ValueError("formal parent source origin requires an explicit port")
    return value.rstrip("/")


class FormalParentSourceClient:
    """Typed Browser/iCom source facade backed by the parent API over TCP."""

    def __init__(
        self,
        *,
        parent_api_origin: str,
        source_token: str,
        execution_capability: dict[str, object],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if len(source_token) < 32:
            raise ValueError("formal parent source token is invalid")
        if not isinstance(execution_capability, dict) or not execution_capability:
            raise ValueError("formal parent source execution capability is invalid")
        self._origin = _parent_origin(parent_api_origin)
        self._capability = dict(execution_capability)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._origin,
            headers={BRIDGE_TOKEN_HEADER: source_token},
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def submit_many(
        self,
        submissions: Iterable[BrowserTaskSubmission],
    ) -> tuple[BrowserTaskSnapshot, ...]:
        tasks = tuple(submissions)
        if not tasks:
            raise ValueError("at least one browser task is required")
        payload = await self._request_json(
            "POST",
            "/browser-bridge/v1/formal/tasks",
            json={
                "execution_capability": self._capability,
                "tasks": [task.model_dump(mode="json") for task in tasks],
            },
            label="formal parent browser submit",
        )
        return SubmitBrowserTasksResponse.model_validate(payload).tasks

    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return ()
        if timeout_seconds <= 0:
            raise ValueError("formal parent browser wait timeout must be positive")

        async with asyncio.timeout(timeout_seconds):
            while True:
                payload = await self._request_json(
                    "POST",
                    "/browser-bridge/v1/formal/tasks/snapshots",
                    json={
                        "execution_capability": self._capability,
                        "task_ids": list(ids),
                    },
                    label="formal parent browser snapshot",
                )
                tasks = payload.get("tasks") if isinstance(payload, dict) else None
                if not isinstance(tasks, list):
                    raise RuntimeError(
                        "formal parent browser snapshot response is invalid"
                    )
                snapshots = tuple(
                    BrowserTaskSnapshot.model_validate(item) for item in tasks
                )
                if tuple(item.id for item in snapshots) != ids:
                    raise RuntimeError(
                        "formal parent browser snapshot identity is foreign"
                    )
                if all(item.state.terminal for item in snapshots):
                    return snapshots
                await asyncio.sleep(0.2)

    async def cancel_many(
        self,
        task_ids: Iterable[str],
        *,
        reason: str,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return ()
        payload = await self._request_json(
            "POST",
            "/browser-bridge/v1/formal/tasks/cancel",
            json={
                "execution_capability": self._capability,
                "task_ids": list(ids),
                "reason": reason,
            },
            label="formal parent browser cancel",
        )
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            raise RuntimeError("formal parent browser cancel response is invalid")
        checked = tuple(BrowserTaskSnapshot.model_validate(item) for item in tasks)
        if tuple(item.id for item in checked) != ids:
            raise RuntimeError("formal parent browser cancel response is foreign")
        return checked

    async def search(
        self,
        query: IComTransferQuery,
        *,
        query_task_id: str | None = None,
    ) -> IComTransferSearchResult:
        if not query_task_id:
            raise ValueError("formal parent iCom search requires query_task_id")
        payload = await self._request_json(
            "POST",
            "/browser-bridge/v1/formal/icom/search",
            json={
                "execution_capability": self._capability,
                "query_task_id": query_task_id,
                "query": query.model_dump(mode="json"),
            },
            label="formal parent iCom search",
        )
        checked = IComTransferSearchResult.model_validate(payload)
        if checked.query != query:
            raise RuntimeError("formal parent iCom result is bound to a foreign query")
        return checked

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        label: str,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{label} transport failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"{label} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{label} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{label} returned a non-object response")
        return payload
