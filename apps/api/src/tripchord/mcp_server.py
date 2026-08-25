"""Small, host-facing MCP adapter for TripChord.

The adapter deliberately has no planning state of its own.  It translates four
coarse MCP tools into the existing authenticated REST endpoints, so the API's
TripRun store, provider queries, deterministic solver, and final validator stay
the only source of truth.  A host may disconnect after ``create_plan`` and
resume with the returned run/job id; this process never caches a plan.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

_DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 600.0


class TripChordMCPError(RuntimeError):
    """A user-readable failure returned by the TripChord REST boundary."""


def _api_base_url() -> str:
    return os.environ.get("TRIPCHORD_MCP_API_BASE_URL", _DEFAULT_API_BASE_URL).rstrip("/")


def _timeout_seconds() -> float:
    raw = os.environ.get("TRIPCHORD_MCP_HTTP_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, _MAX_TIMEOUT_SECONDS))


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = os.environ.get("TRIPCHORD_MCP_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _canonical_requirement(requirement: str) -> str:
    return " ".join(requirement.split())


def _stable_request_id(
    requirement: str,
    reference_date: str,
    explicit: str | None,
) -> str:
    if explicit:
        return explicit
    canonical = f"{reference_date.strip()}\n{_canonical_requirement(requirement)}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    # A host may be disconnected immediately after admission and retry the
    # same tool call.  The default key must therefore be deterministic; an
    # explicit request_id is the opt-in escape hatch for a deliberately new
    # run with identical text.
    return f"mcp-{digest}"


def _object_payload(value: object, *, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TripChordMCPError(f"TripChord {operation} returned a non-object response")
    return value


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return response.text[:400] or f"HTTP {response.status_code}"


async def _request(
    method: str,
    path: str,
    *,
    json: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    operation: str,
) -> dict[str, Any]:
    merged_headers = _headers()
    if headers:
        merged_headers.update(headers)
    try:
        async with httpx.AsyncClient(
            base_url=_api_base_url(),
            timeout=httpx.Timeout(_timeout_seconds()),
            headers=merged_headers,
        ) as client:
            response = await client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise TripChordMCPError(
            f"TripChord {operation} could not reach the local API: {exc}"
        ) from exc
    if response.is_error:
        raise TripChordMCPError(
            f"TripChord {operation} failed ({response.status_code}): {_error_detail(response)}"
        )
    try:
        return _object_payload(response.json(), operation=operation)
    except ValueError as exc:
        raise TripChordMCPError(f"TripChord {operation} returned invalid JSON") from exc


def _trip_card(version: Mapping[str, Any] | None) -> dict[str, Any] | None:
    card = version.get("selected_trip_card") if version is not None else None
    if not isinstance(card, dict):
        return None
    public_card = dict(card)
    # Internal role/skill labels are an implementation detail.  The host gets
    # the checked travel result, not a second view of TripChord's orchestration.
    public_card.pop("participating_agent_roles", None)
    public_card.pop("applied_skill_ids", None)
    return public_card


def _version_summary(version: Mapping[str, Any]) -> dict[str, Any]:
    card = _trip_card(version)
    status = version.get("status")
    if status is None and isinstance(card, dict):
        status = card.get("status")
    return {
        "version_id": version.get("id"),
        "version": version.get("version"),
        "status": status,
        "created_at": version.get("created_at"),
        "reason": version.get("reason"),
        "trip_card": card,
    }


def _plan_view(run: Mapping[str, Any], *, include_history: bool = True) -> dict[str, Any]:
    versions = run.get("plan_versions")
    version_items = versions if isinstance(versions, list) else []
    active_id = run.get("active_plan_version_id")
    active = next(
        (item for item in version_items if isinstance(item, dict) and item.get("id") == active_id),
        None,
    )
    output: dict[str, Any] = {
        "run_id": run.get("id"),
        "source_job_id": run.get("source_job_id"),
        "active_plan_version_id": active_id,
        "active_plan_version": _version_summary(active) if isinstance(active, dict) else None,
        "initial_external_query_count": run.get("initial_external_query_count", 0),
        "initial_planning_elapsed_ms": run.get("initial_planning_elapsed_ms", 0),
        "updated_at": run.get("updated_at"),
        "boundary": run.get("boundary"),
    }
    if include_history:
        output["plan_versions"] = [
            _version_summary(item) for item in version_items if isinstance(item, dict)
        ]
        output["change_history"] = run.get("change_history", [])
    return output


def _run_id_from_job(job: Mapping[str, Any]) -> str:
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise TripChordMCPError("TripChord create_plan returned no run/job id")
    # Complex formal runs intentionally use the admitted job id as TripRun id.
    # Keep both names in the public envelope rather than inventing a second id.
    return job_id


mcp = FastMCP(
    "TripChord",
    instructions=(
        "Use only the four TripChord planning tools. TripChord itself owns current "
        "sources, constraints, pricing, and validation; do not infer missing prices "
        "or claim that a plan is bookable."
    ),
)


@mcp.tool()
async def create_plan(
    requirement: str,
    reference_date: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Create one TripChord plan job from natural language.

    The returned id is usable with all three remaining tools.  This call only
    admits the existing API job; it never places an order or accepts terms.
    """

    text = _canonical_requirement(requirement)
    if not text:
        raise TripChordMCPError("requirement must not be empty")
    effective_reference_date = (reference_date or "").strip() or date.today().isoformat()
    idempotency_key = _stable_request_id(
        text,
        effective_reference_date,
        request_id,
    )
    payload = {
        "requirement": {
            "text": text,
            "reference_date": effective_reference_date,
        },
        "coverage_mode": "strict",
        "timeout_seconds": 300,
        "total_timeout_seconds": 600,
        "max_pairs": 400,
        "publication_refresh_minimum_options": 1,
    }
    response = await _request(
        "POST",
        "/api/v1/agents/live-flexible-plan-from-text/jobs",
        json=payload,
        headers={"Idempotency-Key": idempotency_key},
        operation="create_plan",
    )
    job = response.get("job")
    if not isinstance(job, dict):
        raise TripChordMCPError("TripChord create_plan returned no job envelope")
    run_id = _run_id_from_job(job)
    return {
        "run_id": run_id,
        "job_id": run_id,
        "state": job.get("state"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "status_url": response.get("status_url"),
        "replayed": response.get("replayed", False),
        "boundary": response.get("boundary"),
        "idempotency_key": idempotency_key,
        "query_count": 0,
        "note": "创建已提交；请用 get_plan_status 观察完成状态。",
    }


@mcp.tool()
async def get_plan_status(run_id: str) -> dict[str, Any]:
    """Read progress for an existing TripChord run without planning again."""

    identifier = run_id.strip()
    if not identifier:
        raise TripChordMCPError("run_id must not be empty")
    try:
        job = await _request(
            "GET",
            f"/api/v1/agents/live-flexible-plan-from-text/jobs/{identifier}",
            operation="get_plan_status",
        )
    except TripChordMCPError as exc:
        # A durable TripRun may outlive the transient job registry.  Reading it
        # is still a status check, not a second planning request.
        if "(404)" not in str(exc):
            raise
        run = await _request(
            "GET",
            f"/api/v1/trip-runs/{identifier}",
            operation="get_plan_status",
        )
        return {
            "run_id": identifier,
            "state": "succeeded",
            "stage": "trip_run_available",
            "progress": 100,
            "trip_run": _plan_view(run, include_history=False),
            "query_count": run.get("initial_external_query_count", 0),
            "boundary": run.get("boundary"),
        }
    result = job.get("result")
    job_run: object = result.get("trip_run") if isinstance(result, dict) else None
    return {
        "run_id": identifier,
        "state": job.get("state"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "revision": job.get("revision"),
        "error": job.get("error"),
        "trip_run": (
            _plan_view(job_run, include_history=False) if isinstance(job_run, dict) else None
        ),
        "query_count": (
            job_run.get("initial_external_query_count", 0) if isinstance(job_run, dict) else 0
        ),
        "boundary": job.get("boundary"),
    }


@mcp.tool()
async def get_plan(run_id: str) -> dict[str, Any]:
    """Read the authoritative current TripRun and active plan version."""

    identifier = run_id.strip()
    if not identifier:
        raise TripChordMCPError("run_id must not be empty")
    run = await _request(
        "GET",
        f"/api/v1/trip-runs/{identifier}",
        operation="get_plan",
    )
    return _plan_view(run)


@mcp.tool()
async def modify_plan(run_id: str, instruction: str) -> dict[str, Any]:
    """Apply one natural-language change to the same TripRun."""

    identifier = run_id.strip()
    text = instruction.strip()
    if not identifier:
        raise TripChordMCPError("run_id must not be empty")
    if not text:
        raise TripChordMCPError("instruction must not be empty")
    mutation = await _request(
        "POST",
        f"/api/v1/trip-runs/{identifier}/modify",
        json={"text": text},
        operation="modify_plan",
    )
    run = mutation.get("trip_run")
    active = mutation.get("active_plan_version")
    if not isinstance(run, dict) or not isinstance(active, dict):
        raise TripChordMCPError("TripChord modify_plan returned no active plan")
    return {
        "run_id": identifier,
        "status": mutation.get("status"),
        "message": mutation.get("message"),
        "active_plan_version_id": run.get("active_plan_version_id"),
        "active_plan_version": _version_summary(active),
        "diff": mutation.get("diff"),
        "needs_scope_expansion": mutation.get("needs_scope_expansion", []),
        "query_count": mutation.get("external_query_count", 0),
        "elapsed_ms": mutation.get("elapsed_ms", 0),
        "trip_run": _plan_view(run),
        "boundary": run.get("boundary"),
    }


def main() -> None:
    """Run the standard stdio MCP server used by external hosts."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
